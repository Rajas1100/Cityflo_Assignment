#!/usr/bin/env python3
"""
route_status_mvp.py - MVP "is route X running late right now, and by how much?"

Usage
-----
    python route_status_mvp.py --data ./cityflo_dummy_data --ts "2026-06-17 07:45:00"
    python route_status_mvp.py --data ./cityflo_dummy_data --route R01 --ts "2026-06-17 07:45:00" --details

    from route_status_mvp import RouteStatusEngine
    engine = RouteStatusEngine("./cityflo_dummy_data")
    engine.get_route_status("R01", "2026-06-17 07:45:00")

Expected files in the data directory (same shape as the take-home bundle):
    routes.csv, stops.csv, trips.csv, gps_pings.csv, bookings.csv (bookings optional)

Pipeline (one pass per query, nothing reads "now"; everything is keyed on `timestamp`)
    1. Ingest + as-of filter       only pings with received_at <= timestamp are "known"
    2. Clock check                 devices whose clock disagrees with the server are re-timed
    3. Route reconstruction        stops -> polyline -> cumulative distance, schedule by distance
    4. Trip/vehicle assignment     scheduled vehicle, else an inferred spare vehicle
    5. Noise filter + snapping     bounds, off-route, speed spikes; ping -> distance along route
    6. Actual vs expected progress delay = when it WAS at this point - when the timetable says it SHOULD be
    7. Trust gating + confidence   stale / silent / skewed data lowers confidence or blocks a verdict
    8. Route roll-up + explanation worst trustworthy live trip decides; text says why

ASSUMPTIONS (also repeated where they are used)
    A1  Timestamps are naive IST and consistent across files.
    A2  The timetable is a straight line in distance: only trip start/end are given, so the
        scheduled time at distance d is start + runtime * d / D (constant speed).
    A3  The route is straight segments between consecutive stops (no road geometry).
    A4  "Late" means late against the TIMETABLE at the bus's current position, because that is what
        a rider notification promises. Historical typical lateness is shown as context only.
    A5  Delay is measured at the last trustworthy ping. If the bus is standing still, the time since
        that ping is added, because a standing bus gets later by the second.
    A6  A ping is "known" at time T only if received_at <= T (late-delivered pings do not count).
    A7  Missing data is never read as "on time": stale or silent trips make the route "unknown"
        unless another trustworthy trip on the route is already late.
    A8  A trip is "live" from scheduled start until scheduled end + 90 min, unless its pings show
        it reached the terminal.
    A9  Each vehicle runs at most one trip per time window (no back-to-back reuse handling).
    A10 Thresholds below are starting values to be calibrated on completed historical trips.

Deviation from the requested return shape: `status` can also be "unknown". With only
on_time/late/early the engine would have to guess when data is missing, which is the wrong failure mode.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from dashboard_explain import build_dashboard_card


# --------------------------------------------------------------------------------------
# Configuration (A10: all thresholds are assumptions to calibrate)
# --------------------------------------------------------------------------------------
@dataclass
class Config:
    late_threshold_min: float = 3.0      # delay >= this -> "late"
    early_threshold_min: float = -3.0    # delay <= this -> "early"
    # noise filters
    bounds: tuple = (18.8, 19.5, 72.7, 73.2)   # lat_min, lat_max, lon_min, lon_max (Mumbai region)
    max_snap_offset_m: float = 300.0     # farther than this from the route line -> reject ping (A3 slack)
    max_speed_kmph: float = 90.0         # implied speed between kept pings above this -> reject
    # clock handling
    skew_threshold_s: float = 120.0      # device clock off by more than this vs the fleet -> re-time it
    # trust gates (data age measured at the query timestamp)
    degraded_age_s: float = 180.0
    max_age_s: float = 480.0             # older than this -> untrustworthy
    min_pings_10min: int = 3
    # position logic
    origin_tol_m: float = 300.0          # within this of the origin = "at origin"
    terminal_tol_m: float = 250.0        # within this of the end = "arrived"
    stop_min_minutes: float = 5.0        # standing this long away from origin/terminal = "stopped"
    stop_move_m: float = 100.0           # moved less than this counts as standing
    # scope
    pre_start_min: int = 20              # look for pings this long before scheduled start
    live_grace_min: int = 90             # trip stays live this long after scheduled end (A8)
    # history
    baseline_k: float = 4.0              # shrinkage strength toward 0 delay: n / (n + k)
    baseline_min_n: int = 4              # fewer historical trips than this -> no baseline


# --------------------------------------------------------------------------------------
# Small geo helpers
# --------------------------------------------------------------------------------------
def hav_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres (numpy-friendly)."""
    r = 6371000.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dl = np.radians(lon2) - np.radians(lon1)
    h = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(h))


class RouteGeometry:
    """Ordered stops -> polyline with cumulative distance; snaps pings onto it (A3)."""

    def __init__(self, route_row: pd.Series, stops: pd.DataFrame):
        s = stops.sort_values("seq").reset_index(drop=True)   # key is (route_id, seq), not stop_id
        self.route_id = route_row["route_id"]
        self.name = route_row["route_name"]
        self.runtime_min = float(route_row["scheduled_runtime_min"])
        self.stops = s
        lat, lon = s["lat"].to_numpy(), s["lon"].to_numpy()
        seg = hav_m(lat[:-1], lon[:-1], lat[1:], lon[1:])
        self.cum = np.concatenate([[0.0], np.cumsum(seg)])          # metres from origin to each stop
        self.D = float(self.cum[-1])
        # Planar (equirectangular) coordinates in metres, fine at city scale, used for projection only.
        lat0 = float(lat.mean())
        self._kx = 111320.0 * math.cos(math.radians(lat0))
        self._ky = 110540.0
        self._lat_ref, self._lon_ref = lat[0], lon[0]
        self._x = (lon - self._lon_ref) * self._kx
        self._y = (lat - self._lat_ref) * self._ky

    def snap(self, lats, lons):
        """Return (distance_along_route_m, offset_from_route_m) for each ping.

        Nearest segment globally. Known weakness: a route that doubles back near itself could snap to the
        wrong leg; production would restrict candidates to a window around the previous position.
        """
        x = (np.asarray(lons) - self._lon_ref) * self._kx
        y = (np.asarray(lats) - self._lat_ref) * self._ky
        ax, ay = self._x[:-1], self._y[:-1]
        bx, by = self._x[1:], self._y[1:]
        abx, aby = bx - ax, by - ay
        l2 = abx ** 2 + aby ** 2
        t = ((x[:, None] - ax) * abx + (y[:, None] - ay) * aby) / l2
        t = np.clip(t, 0.0, 1.0)
        px, py = ax + t * abx, ay + t * aby
        d = np.hypot(x[:, None] - px, y[:, None] - py)
        k = d.argmin(axis=1)
        rows = np.arange(len(k))
        along = self.cum[k] + t[rows, k] * (self.cum[k + 1] - self.cum[k])
        return along, d[rows, k]

    def sched_time(self, start: pd.Timestamp, dist_m: float) -> pd.Timestamp:
        """Timetable time at a given distance (A2: constant speed between start and end)."""
        frac = min(max(dist_m / self.D, 0.0), 1.0)
        return start + timedelta(minutes=self.runtime_min * frac)


# --------------------------------------------------------------------------------------
# Result containers
# --------------------------------------------------------------------------------------
@dataclass
class TripState:
    trip_id: str
    vehicle_id: Optional[str] = None
    kind: str = "no_data"            # live | completed | no_data
    trust: str = "untrustworthy"     # ok | degraded | untrustworthy
    confidence: float = 0.0
    delay_min: Optional[float] = None
    trip_status: str = "unknown"     # on_time | late | early | unknown (trip level, before roll-up)
    dist_m: Optional[float] = None
    pct_complete: Optional[float] = None
    last_ping_time: Optional[pd.Timestamp] = None
    age_s: Optional[float] = None
    expected_time: Optional[pd.Timestamp] = None
    observed_time: Optional[pd.Timestamp] = None
    stopped_min: float = 0.0
    flags: list = field(default_factory=list)
    reasons: list = field(default_factory=list)   # why trust/confidence was lowered
    riders_ahead: Optional[int] = None
    departure: Optional[str] = None      # scheduled departure HH:MM, used to name trips for humans

    def to_dict(self):
        f = lambda t: None if t is None else t.strftime("%Y-%m-%d %H:%M:%S")
        return {
            "trip_id": self.trip_id, "vehicle_id": self.vehicle_id, "kind": self.kind,
            "trip_status": self.trip_status,
            "delay_minutes": None if self.delay_min is None else round(self.delay_min, 1),
            "trust": self.trust, "confidence": round(self.confidence, 2),
            "distance_along_m": None if self.dist_m is None else round(self.dist_m),
            "pct_complete": None if self.pct_complete is None else round(self.pct_complete, 3),
            "last_ping_time": f(self.last_ping_time),
            "data_age_s": None if self.age_s is None else round(self.age_s),
            "expected_time_at_position": f(self.expected_time),
            "observed_time_at_position": f(self.observed_time),
            "stopped_minutes": round(self.stopped_min, 1),
            "flags": self.flags, "reasons": self.reasons, "riders_ahead_of_bus": self.riders_ahead,
            "departure": self.departure,
        }


class _Ctx:
    """Everything derived from the as-of filter, built once per query."""

    def __init__(self, pool: pd.DataFrame, latency_ref: float, sched_by_date: dict, asof=None):
        self.asof = asof
        self.pool = pool
        self.latency_ref = latency_ref
        self.by_vehicle = {v: g.sort_values(["t_eff", "received_at"]) for v, g in pool.groupby("vehicle_id")}
        self._sched_by_date = sched_by_date

    def window(self, vehicle, lo, hi):
        g = self.by_vehicle.get(vehicle)
        if g is None:
            return self.pool.iloc[0:0]
        return g[(g.t_eff >= lo) & (g.t_eff <= hi)]

    def spares(self, date):
        """Vehicles that ping on `date` but are not scheduled on any trip that day (A9 substitution pool)."""
        scheduled = self._sched_by_date.get(date, set())
        out = []
        for v, g in self.by_vehicle.items():
            if v not in scheduled and (g.t_eff.dt.normalize() == date).any():
                out.append(v)
        return out


# --------------------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------------------
class RouteStatusEngine:
    def __init__(self, data_dir: str, config: Optional[Config] = None):
        self.cfg = config or Config()
        d = Path(data_dir)
        # ---- 1. Ingest -------------------------------------------------------------
        self.routes = pd.read_csv(d / "routes.csv")
        self.stops = pd.read_csv(d / "stops.csv")
        self.trips = pd.read_csv(d / "trips.csv", parse_dates=["service_date", "scheduled_start", "scheduled_end"])
        self.pings = pd.read_csv(d / "gps_pings.csv", parse_dates=["recorded_at", "received_at"])
        # A header-only CSV (e.g. no pings yet this morning) is read as object dtype, so coerce explicitly.
        for col in ("recorded_at", "received_at"):
            self.pings[col] = pd.to_datetime(self.pings[col])
        for col in ("service_date", "scheduled_start", "scheduled_end"):
            self.trips[col] = pd.to_datetime(self.trips[col])
        bpath = d / "bookings.csv"
        self.bookings = (pd.read_csv(bpath, parse_dates=["booked_at", "promised_eta"])
                         if bpath.exists() else pd.DataFrame())
        # ---- 3. Route reconstruction (static, independent of the query time) -----------
        self.geoms = {r["route_id"]: RouteGeometry(r, self.stops[self.stops.route_id == r["route_id"]])
                      for _, r in self.routes.iterrows()}
        self._sched_by_date = {d_: set(g.vehicle_id) for d_, g in self.trips.groupby("service_date")}

    # ------------------------------------------------------------------ public API
    def get_route_status(self, route_id: str, timestamp) -> dict:
        ts = pd.Timestamp(timestamp)
        if route_id not in self.geoms:
            raise KeyError(f"unknown route_id {route_id!r}")
        geom = self.geoms[route_id]
        ctx = self._build_ctx(ts)
        out = {"route_id": route_id, "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S")}

        day = self.trips[(self.trips.route_id == route_id) & (self.trips.service_date == ts.normalize())]
        in_window = day[(day.scheduled_start <= ts) &
                        (ts <= day.scheduled_end + timedelta(minutes=self.cfg.live_grace_min))]
        states = [self._assess_trip(row, geom, ctx, ts) for _, row in in_window.iterrows()]
        live = [s for s in states if s.kind != "completed"]       # reached terminal -> no longer relevant
        baseline = self._baseline(route_id, ts, ctx)

        if not live:
            nxt = day[day.scheduled_start > ts].scheduled_start.min()
            msg = f"No trip on {geom.name} is in service at {ts:%H:%M}."
            if pd.notna(nxt):
                msg += f" Next scheduled departure is {nxt:%H:%M}."
            unsched = self._unscheduled_vehicles(geom, ts, ctx, {s.vehicle_id for s in states})
            if unsched:
                msg += f" However, vehicle(s) {', '.join(unsched)} are reporting GPS on this route with no scheduled trip."
            out.update(status="unknown", delay_minutes=None, confidence=0.0, explanation=msg,
                       details={"trips": [s.to_dict() for s in states], "baseline": baseline,
                                "unscheduled_vehicles": unsched})
            out["dashboard"] = build_dashboard_card(out, geom.name)
            return out

        trusted = [s for s in live if s.trust != "untrustworthy"]
        untrusted = [s for s in live if s.trust == "untrustworthy"]
        worst = max(trusted, key=lambda s: s.delay_min) if trusted else None   # roll-up: worst trusted trip

        # A7: do not read missing data as "on time".
        if worst is None or (untrusted and worst.delay_min < self.cfg.late_threshold_min):
            status, delay, conf = "unknown", None, 0.2
        else:
            status = worst.trip_status
            delay = round(worst.delay_min, 1)
            conf = worst.confidence * (0.9 if untrusted else 1.0)   # coverage penalty if a live trip is unassessable

        out.update(status=status, delay_minutes=delay, confidence=round(conf, 2),
                   explanation=self._explain(geom, ts, status, worst, untrusted, baseline),
                   details={"roll_up_rule": "worst trustworthy live trip decides; any live trip without trustworthy "
                                            "data blocks an 'on_time' answer",
                            "decision_trip": worst.trip_id if worst else None,
                            "trips": [s.to_dict() for s in states], "baseline": baseline})
        out["dashboard"] = build_dashboard_card(out, geom.name)
        return out

    def _unscheduled_vehicles(self, geom, ts, ctx, assigned):
        """Vehicles pinging along this route in the last 10 min that belong to no live trip (missing trip row)."""
        day = self.trips[self.trips.service_date == ts.normalize()]
        live = day[(day.scheduled_start - timedelta(minutes=self.cfg.pre_start_min) <= ts) &
                   (ts <= day.scheduled_end + timedelta(minutes=self.cfg.live_grace_min))]
        covered = set(live.vehicle_id) | {v for v in assigned if v}
        out = []
        for veh, g in ctx.by_vehicle.items():
            if veh in covered:
                continue
            recent = g[(g.t_eff > ts - timedelta(minutes=10)) & (g.t_eff <= ts)]
            if len(recent) < 3:
                continue
            _, off = geom.snap(recent.lat.to_numpy(), recent.lon.to_numpy())
            if (off <= self.cfg.max_snap_offset_m).mean() >= 0.8:
                out.append(veh)
        return sorted(out)

    # ------------------------------------------------------------------ stage: as-of pool
    def _build_ctx(self, asof: pd.Timestamp) -> _Ctx:
        p = self.pings[self.pings.received_at <= asof].copy()        # A6: only what was known at `asof`
        # Replays carry new ping_ids, so de-duplicate on content, keeping the first-received copy.
        p = p.sort_values(["received_at", "ping_id"]).drop_duplicates(
            ["vehicle_id", "recorded_at", "lat", "lon"], keep="first")
        p["skew_s"] = (p.received_at - p.recorded_at).dt.total_seconds()
        p["rdate"] = p.received_at.dt.normalize()
        med = p.groupby(["vehicle_id", "rdate"]).skew_s.transform("median")
        healthy = med[(med >= 0) & (med <= 60)]
        latency_ref = float(healthy.median()) if len(healthy) else 5.0   # normal delivery delay in seconds
        # A device whose median skew is far from the fleet norm has a wrong clock: trust arrival time instead.
        p["skew_corrected"] = (med - latency_ref).abs() > self.cfg.skew_threshold_s
        p["t_eff"] = p.recorded_at.where(~p.skew_corrected,
                                         p.received_at - pd.to_timedelta(latency_ref, unit="s"))
        return _Ctx(p, latency_ref, self._sched_by_date, asof)

    # ------------------------------------------------------------------ stage: vehicle assignment
    def _infer_vehicle(self, trip, geom, ctx, lo, hi):
        """Scheduled vehicle sent nothing: look for an unscheduled vehicle that sat at the origin around departure."""
        o_lat, o_lon = geom.stops.lat.iloc[0], geom.stops.lon.iloc[0]
        best = None
        for v in ctx.spares(trip.service_date):
            c = ctx.window(v, lo, hi)
            early = c[c.t_eff <= trip.scheduled_start + timedelta(minutes=15)]
            if len(c) < 3 or early.empty:
                continue
            near = int((hav_m(early.lat.to_numpy(), early.lon.to_numpy(), o_lat, o_lon) <= 400).sum())
            if near >= 3 and (best is None or near > best[1]):
                best = (v, near)
        return best[0] if best else None

    # ------------------------------------------------------------------ stage: clean + snap
    def _track(self, trip, geom, ctx):
        """Return (cleaned ping DataFrame with distance along route, meta) for one trip."""
        cfg = self.cfg
        lo = trip.scheduled_start - timedelta(minutes=cfg.pre_start_min)
        hi = trip.scheduled_end + timedelta(minutes=cfg.live_grace_min)
        veh, substituted = trip.vehicle_id, False
        cand = ctx.window(veh, lo, hi)
        if len(cand) < 3:
            alt = self._infer_vehicle(trip, geom, ctx, lo, hi)
            if alt:
                veh, substituted, cand = alt, True, ctx.window(alt, lo, hi)
        meta = {"vehicle": veh, "substituted": substituted, "n_raw": int(len(cand)), "n_rejected": 0,
                "off_route_recent": 0, "off_route_max_m": 0.0,
                "skew_corrected": bool(len(cand) and cand.skew_corrected.any())}
        if cand.empty:
            return None, meta

        # Filter 1: bounds (catches (0,0) and nulls).
        la0, la1, lo0, lo1 = cfg.bounds
        c = cand[cand.lat.between(la0, la1) & cand.lon.between(lo0, lo1)].copy()
        if c.empty:
            meta["n_rejected"] = meta["n_raw"]
            return None, meta
        # Filter 2: snap and reject pings far from the route line.
        along, off = geom.snap(c.lat.to_numpy(), c.lon.to_numpy())
        c["dist"], c["offset"] = along, off
        # Diversion evidence: pings in the last 10 min that sit far from the route line (A3).
        if ctx.asof is not None:
            off_recent = c[(c.offset > cfg.max_snap_offset_m) & (c.t_eff > ctx.asof - timedelta(minutes=10))]
            meta["off_route_recent"] = int(len(off_recent))
            meta["off_route_max_m"] = float(off_recent.offset.max()) if len(off_recent) else 0.0
        c = c[c.offset <= cfg.max_snap_offset_m]
        # Filter 3: implied-speed spikes between consecutive kept pings.
        keep, last = [], None
        for i, (t, la, lo_) in enumerate(zip(c.t_eff, c.lat, c.lon)):
            if last is not None:
                dt_s = (t - last[0]).total_seconds()
                if dt_s >= 5 and hav_m(last[1], last[2], la, lo_) / dt_s * 3.6 > cfg.max_speed_kmph:
                    continue
            keep.append(i)
            last = (t, la, lo_)
        c = c.iloc[keep].copy()
        meta["n_rejected"] = meta["n_raw"] - len(c)
        if c.empty:
            return None, meta
        # Progress along a route should not go backwards; a running max absorbs standing-still jitter.
        c = c.sort_values(["t_eff", "received_at"])
        c["dist"] = np.maximum.accumulate(c["dist"].to_numpy())
        return c, meta

    # ------------------------------------------------------------------ stage: per-trip assessment
    def _assess_trip(self, trip, geom, ctx, asof) -> TripState:
        cfg = self.cfg
        st = TripState(trip_id=trip.trip_id, departure=trip.scheduled_start.strftime("%H:%M"))
        track, meta = self._track(trip, geom, ctx)
        st.vehicle_id = meta["vehicle"]
        if track is None:
            st.reasons.append("no usable GPS pings for this trip")
            st.flags.append("NO_DATA")
            return st
        if meta["substituted"]:
            st.flags.append("VEHICLE_SUBSTITUTED")
        if meta["skew_corrected"]:
            st.flags.append("CLOCK_CORRECTED")

        last = track.iloc[-1]
        st.dist_m = float(last.dist)
        st.pct_complete = st.dist_m / geom.D
        st.last_ping_time = last.t_eff
        st.age_s = max(0.0, (asof - last.t_eff).total_seconds())

        # Arrived at the terminal -> not relevant for "right now".
        if (track.dist >= geom.D - cfg.terminal_tol_m).any():
            st.kind, st.trust, st.trip_status, st.delay_min = "completed", "ok", "on_time", 0.0
            return st
        st.kind = "live"

        # Standing-still detection over the most recent pings.
        tail = track[track.t_eff >= last.t_eff - timedelta(minutes=5)]
        moved = float(last.dist - tail.dist.iloc[0])
        at_origin = st.dist_m < cfg.origin_tol_m and moved < cfg.stop_move_m
        near_terminal = st.dist_m > geom.D - cfg.terminal_tol_m
        # contiguous standing run ending at the last ping
        idx = np.where(track.dist.to_numpy() < last.dist - cfg.stop_move_m)[0]
        run = track.iloc[(idx[-1] + 1):] if len(idx) else track
        st.stopped_min = (last.t_eff - run.t_eff.iloc[0]).total_seconds() / 60
        fresh = st.age_s <= cfg.degraded_age_s
        standing = (st.stopped_min >= cfg.stop_min_minutes and not at_origin and not near_terminal and fresh)
        # A standing bus that is on or ahead of the timetable is most likely holding at a timing point,
        # which is deliberate. Only a standing bus that is also behind schedule is flagged as a fault.
        behind_now = ((last.t_eff - geom.sched_time(trip.scheduled_start, st.dist_m)).total_seconds()
                      + st.age_s) / 60 >= cfg.late_threshold_min
        stopped = standing and behind_now
        if stopped:
            st.flags.append("STOPPED")
        elif standing:
            st.flags.append("HOLDING")

        # ---- delay (A4, A5) ---------------------------------------------------------
        if at_origin:
            # Not departed yet: lateness is how far past the scheduled departure we are, never negative.
            st.delay_min = max(0.0, (asof - trip.scheduled_start).total_seconds() / 60)
            st.expected_time, st.observed_time = trip.scheduled_start, None
            st.flags.append("AT_ORIGIN")
        else:
            st.expected_time = geom.sched_time(trip.scheduled_start, st.dist_m)
            st.observed_time = last.t_eff
            st.delay_min = (last.t_eff - st.expected_time).total_seconds() / 60
            if standing:
                st.delay_min += st.age_s / 60      # still standing since the last ping

        # ---- trust gating + confidence (A7) ------------------------------------------
        recent = int((track.t_eff > asof - timedelta(minutes=10)).sum())
        conf = 0.95
        if st.age_s > cfg.max_age_s:
            st.trust = "untrustworthy"
            st.reasons.append(f"last ping is {st.age_s / 60:.1f} min old (limit {cfg.max_age_s / 60:.0f} min)")
        elif recent < cfg.min_pings_10min:
            st.trust = "untrustworthy"
            st.reasons.append(f"only {recent} usable pings in the last 10 min")
        else:
            st.trust = "ok"
            if st.age_s > cfg.degraded_age_s:
                conf *= 0.6
                st.trust = "degraded"
                st.reasons.append(f"last ping is {st.age_s / 60:.1f} min old")
            elif st.age_s > 90:
                conf *= 0.9
            if recent < 8:
                conf *= 0.7
                st.trust = "degraded"
                st.reasons.append(f"only {recent} pings in the last 10 min")
            elif recent < 15:
                conf *= 0.9
            if meta["skew_corrected"]:
                # Uniform offset could equally be delivery lag instead of a wrong device clock (see probe P7c),
                # so a corrected clock is never "High" certainty.
                conf *= 0.75
                st.reasons.append("device clock disagrees with the server, so arrival time was used instead")
            if meta["substituted"]:
                conf *= 0.9
                st.reasons.append(f"pings come from {meta['vehicle']}, not the scheduled {trip.vehicle_id}")
            if "STOPPED" in st.flags and st.pct_complete >= 0.85:
                # Parked short of the last stop looks identical to a breakdown (probe P5): require a human check.
                conf *= 0.6
                st.flags.append("NEAR_END")
                st.reasons.append("bus is standing near the end of the route and may already have arrived")
            if float(last.offset) > 100:
                conf *= 0.85
                st.reasons.append(f"last ping is {float(last.offset):.0f} m off the route line")
            if meta["n_raw"] and meta["n_rejected"] / meta["n_raw"] > 0.10:
                conf *= 0.85
                st.reasons.append("more than 10% of this trip's pings were rejected as noise")
            if len(track) < 5:
                conf *= 0.8
                st.reasons.append("very few pings so far")
        if st.trust == "untrustworthy" and meta.get("off_route_recent", 0) >= 3:
            st.flags.append("POSSIBLE_DIVERSION")
            st.reasons.append(f"{meta['off_route_recent']} recent pings were up to {meta['off_route_max_m'] / 1000:.1f} km "
                              "off the route line, so the bus may be on a diversion and its position along the route "
                              "cannot be measured")
        st.confidence = 0.0 if st.trust == "untrustworthy" else round(min(conf, 1.0), 2)

        if st.trust != "untrustworthy":
            st.trip_status = ("late" if st.delay_min >= cfg.late_threshold_min else
                              "early" if st.delay_min <= cfg.early_threshold_min else "on_time")
        else:
            st.trip_status = "unknown"
            # still report where it was and how late it WAS, as a lead for a human
            st.expected_time = geom.sched_time(trip.scheduled_start, st.dist_m)
            st.observed_time = last.t_eff
            st.delay_min = (last.t_eff - st.expected_time).total_seconds() / 60

        # Riders who have not yet reached their boarding stop (uses bookings if present).
        if not self.bookings.empty:
            b = self.bookings[self.bookings.trip_id == trip.trip_id]
            cum = dict(zip(geom.stops.stop_id, geom.cum))
            st.riders_ahead = int(sum(1 for s in b.boarding_stop_id if s in cum and cum[s] > st.dist_m))
        return st

    # ------------------------------------------------------------------ stage: history context
    def _baseline(self, route_id, ts, ctx):
        """Typical lateness on EARLIER days (context only, A4). Thin history is shrunk toward zero."""
        geom = self.geoms[route_id]
        hist = self.trips[(self.trips.route_id == route_id) & (self.trips.service_date < ts.normalize())]
        delays = []
        for _, t in hist.iterrows():
            track, _ = self._track(t, geom, ctx)
            if track is None:
                continue
            arrived = track[track.dist >= geom.D - self.cfg.terminal_tol_m]
            if arrived.empty:
                continue
            a = arrived.iloc[0]
            delays.append((a.t_eff - geom.sched_time(t.scheduled_start, a.dist)).total_seconds() / 60)
        n = len(delays)
        if n < self.cfg.baseline_min_n:
            return {"n": n, "available": False}
        med = float(np.median(delays))
        return {"n": n, "available": True, "median_delay_min": round(med, 1),
                "shrunk_delay_min": round(med * n / (n + self.cfg.baseline_k), 1),
                "q25": round(float(np.percentile(delays, 25)), 1), "q75": round(float(np.percentile(delays, 75)), 1)}

    # ------------------------------------------------------------------ stage: explanation
    def _explain(self, geom, ts, status, worst, untrusted, baseline) -> str:
        hm = lambda t: t.strftime("%H:%M")
        parts = []
        if status == "unknown":
            parts.append(f"Cannot tell whether {geom.name} is late at {hm(ts)}.")
            if worst is not None and untrusted:
                parts.append(f"Trip {worst.trip_id} looks fine ({worst.delay_min:+.1f} min), but another live trip has no "
                             "trustworthy position, and missing data is not treated as on time.")
        else:
            verb = {"late": "LATE", "on_time": "on time", "early": "EARLY"}[status]
            parts.append(f"{geom.name} is {verb} at {hm(ts)}"
                         + (f" by about {abs(worst.delay_min):.0f} min." if status != "on_time" else
                            f" (within {self.cfg.late_threshold_min:.0f} min of the timetable)."))
        if worst is not None:
            w = worst
            if "AT_ORIGIN" in w.flags:
                parts.append(f"Trip {w.trip_id} has not left the origin; it was due out at {hm(w.expected_time)}.")
            else:
                parts.append(f"Trip {w.trip_id} (vehicle {w.vehicle_id}) was last seen {w.age_s:.0f}s ago, "
                             f"{w.dist_m / 1000:.1f} km along the route ({w.pct_complete:.0%}); the timetable has it "
                             f"there at {hm(w.expected_time)} and it was there at {hm(w.observed_time)}.")
            if "STOPPED" in w.flags:
                parts.append(f"It has barely moved for {w.stopped_min:.0f} min while still reporting, so a rider "
                             "notification will not fix this: contact the driver.")
            if w.riders_ahead:
                parts.append(f"{w.riders_ahead} booked riders have not yet reached their boarding stop on this trip.")
            for r in w.reasons:
                parts.append(f"Confidence note: {r}.")
        for u in untrusted:
            why = "; ".join(u.reasons) or "no data"
            extra = (f" Last known position was {u.dist_m / 1000:.1f} km along the route."
                     if u.dist_m is not None else "")
            parts.append(f"Trip {u.trip_id} could not be assessed: {why}.{extra}")
        if baseline.get("available"):
            parts.append(f"Context: over {baseline['n']} earlier trips this route typically reaches the end "
                         f"{baseline['median_delay_min']:+.1f} min vs the timetable (middle half "
                         f"{baseline['q25']:+.1f} to {baseline['q75']:+.1f}); history this thin is indicative only.")
            if status == "late" and worst is not None:
                parts.append(f"Relative to that usual pattern, today is about "
                             f"{worst.delay_min - baseline['median_delay_min']:+.0f} min.")
        return " ".join(parts)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Route lateness MVP")
    ap.add_argument("--data", default="./cityflo_dummy_data", help="directory with the CSV files")
    ap.add_argument("--ts", default="2026-06-17 07:45:00", help="as-of timestamp (IST)")
    ap.add_argument("--route", default=None, help="route id; default: all routes")
    ap.add_argument("--details", action="store_true", help="include per-trip details in the output")
    a = ap.parse_args()
    eng = RouteStatusEngine(a.data)
    ids = [a.route] if a.route else list(eng.geoms)
    for rid in ids:
        res = eng.get_route_status(rid, a.ts)
        if not a.details:
            res = {k: v for k, v in res.items() if k != "details"}
        print(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    main()
