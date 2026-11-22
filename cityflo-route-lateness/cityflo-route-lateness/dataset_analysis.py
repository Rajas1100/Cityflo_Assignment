#!/usr/bin/env python3
"""
dataset_analysis.py - operational analysis of the bundle using ONLY the source tables
(routes, stops, trips, gps_pings, bookings). It never reads the answer key.

    python dataset_analysis.py --data ./cityflo_dummy_data
"""
import argparse
import numpy as np
import pandas as pd
from route_status_mvp import RouteStatusEngine, hav_m

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="./cityflo_dummy_data")
ap.add_argument("--extract_cut", default="2026-06-17 08:15:00", help="last time the live-day extract covers")
a = ap.parse_args()

eng = RouteStatusEngine(a.data)
CUT = pd.Timestamp(a.extract_cut)
ctx = eng._build_ctx(pd.Timestamp("2026-06-18 00:00:00"))      # every ping, as if queried the next morning
pings, trips, bookings = eng.pings, eng.trips, eng.bookings
pd.set_option("display.width", 200, "display.max_columns", 30)

# ---------------------------------------------------------------------------------------------
# Per-trip tracks
# ---------------------------------------------------------------------------------------------
rows, tracks = [], {}
for _, t in trips.iterrows():
    g = eng.geoms[t.route_id]
    tr, meta = eng._track(t, g, ctx)
    tracks[t.trip_id] = (tr, meta)
    r = dict(trip_id=t.trip_id, route=t.route_id, date=t.service_date.date(), idx=int(t.trip_id[-2:]),
             start=t.scheduled_start, vehicle=meta["vehicle"], substituted=meta["substituted"],
             n_pings=meta["n_raw"], n_rejected=meta["n_rejected"], skewed=meta["skew_corrected"])
    if tr is not None:
        arr = tr[tr.dist >= g.D - eng.cfg.terminal_tol_m]
        r["arrived"] = not arr.empty
        if not arr.empty:
            a0 = arr.iloc[0]
            r["end_delay"] = (a0.t_eff - g.sched_time(t.scheduled_start, a0.dist)).total_seconds() / 60
        for frac in (0.25, 0.5, 0.75):
            sub = tr[tr.dist >= frac * g.D]
            if not sub.empty:
                s0 = sub.iloc[0]
                r[f"delay@{int(frac * 100)}%"] = (s0.t_eff - g.sched_time(t.scheduled_start, s0.dist)).total_seconds() / 60
        mv = tr[tr.dist >= 300]
        if not mv.empty:
            m0 = mv.iloc[0]
            r["departure_delay"] = (m0.t_eff - g.sched_time(t.scheduled_start, m0.dist)).total_seconds() / 60
        pre = tr[tr.t_eff < t.scheduled_start]
        r["pre_start_pings"] = len(pre)
        r["first_ping_before_start_min"] = (t.scheduled_start - tr.t_eff.iloc[0]).total_seconds() / 60
        # longest standing run away from origin/terminal
        d = tr.dist.to_numpy(); tt = tr.t_eff.to_numpy()
        best = 0.0; i = 0
        while i < len(tr):
            j = i
            while j + 1 < len(tr) and d[j + 1] - d[i] < eng.cfg.stop_move_m:
                j += 1
            if d[i] > eng.cfg.origin_tol_m and d[j] < g.D - eng.cfg.terminal_tol_m:
                best = max(best, (tt[j] - tt[i]) / np.timedelta64(1, "m"))
            i = j + 1 if j > i else i + 1
        r["longest_stand_min"] = best
        r["last_dist_pct"] = tr.dist.iloc[-1] / g.D
        r["last_ping"] = tr.t_eff.iloc[-1]
    else:
        r["arrived"] = False
    rows.append(r)
T = pd.DataFrame(rows)
T["extract_complete"] = ~((T.date == pd.Timestamp("2026-06-17").date()) &
                          (T.start + pd.to_timedelta(eng.routes.set_index("route_id").loc[T.route, "scheduled_runtime_min"].to_numpy(), unit="m") > CUT - pd.Timedelta(minutes=15)))

print("=" * 100, "\n1. LATENESS BY ROUTE AND DAY (arrival delay vs timetable, minutes, completed trips only)")
done = T[T.arrived]
print(done.pivot_table(index="route", columns="date", values="end_delay", aggfunc=["median", "count"]).round(1))
print("\nmin/max end delay per route:\n", done.groupby("route").end_delay.agg(["min", "median", "max"]).round(1))
print("\ntrips with end_delay <= -3 (early):", int((done.end_delay <= -3).sum()), "| <= -1:", int((done.end_delay <= -1).sum()))
print("share of completed trips >= 3 min late, by route:\n", (done.assign(l=done.end_delay >= 3).groupby("route").l.mean() * 100).round(0).to_dict())

print("\nDELAY ACCRUAL (median delay at 25/50/75% progress, completed trips):")
print(done.groupby("route")[["delay@25%", "delay@50%", "delay@75%", "end_delay"]].median().round(1))
print("\nDEPARTURE DELAY (first ping >= 300 m vs timetable), median by route/date:")
print(T.pivot_table(index="route", columns="date", values="departure_delay", aggfunc="median").round(1))

# ---------------------------------------------------------------------------------------------
print("=" * 100, "\n2. DATA QUALITY")
p = pings.copy()
print("rows:", len(p), "| unique ping_id:", p.ping_id.is_unique)
dup_key = ["vehicle_id", "recorded_at", "lat", "lon"]
dups = p[p.duplicated(dup_key, keep="first")]
print("content duplicates (same vehicle,time,lat,lon):", len(dups), "| by vehicle:", dups.vehicle_id.value_counts().to_dict(),
      "| distinct days:", sorted(dups.recorded_at.dt.date.astype(str).unique()))
print("null speed_kmph:", int(p.speed_kmph.isna().sum()), f"({p.speed_kmph.isna().mean() * 100:.2f}%)")
la0, la1, lo0, lo1 = eng.cfg.bounds
oob = p[~(p.lat.between(la0, la1) & p.lon.between(lo0, lo1))]
print("out-of-bounds pings:", len(oob), oob[["ping_id", "vehicle_id", "lat", "lon", "recorded_at"]].to_dict("records"))
p["skew_s"] = (p.received_at - p.recorded_at).dt.total_seconds()
p["day"] = p.received_at.dt.date
vd = p.groupby(["vehicle_id", "day"]).skew_s.agg(median="median", p95=lambda s: s.quantile(.95), max="max").round(0)
print("fleet median delivery delay (s):", round(p.skew_s.median(), 1), "| 95th pct:", round(p.skew_s.quantile(.95), 1))
print("vehicle-days with median clock skew > 120 s:\n", vd[vd["median"].abs() > 120])
late_rx = p[p.skew_s > 120]
print("pings delivered > 2 min after recorded:", len(late_rx),
      "\n", late_rx.groupby(["vehicle_id", "day"]).agg(n=("ping_id", "count"), first=("recorded_at", "min"), last=("recorded_at", "max"),
                                                      rx_first=("received_at", "min"), rx_last=("received_at", "max")))
print("vehicles pinging that are in no trip row:", sorted(set(p.vehicle_id) - set(trips.vehicle_id)))
print("scheduled vehicle-days with zero pings:",
      [(r.vehicle_id, str(r.service_date.date())) for _, r in trips.drop_duplicates(["vehicle_id", "service_date"]).iterrows()
       if not ((p.vehicle_id == r.vehicle_id) & (p.recorded_at.dt.normalize() == r.service_date)).any()])
# cadence
gaps = []
for tid, (tr, meta) in tracks.items():
    if tr is None: continue
    dts = tr.t_eff.diff().dt.total_seconds().dropna()
    gaps.append(dict(trip_id=tid, median_gap=dts.median(), max_gap=dts.max(), n_gaps_gt90=int((dts > 90).sum())))
G = pd.DataFrame(gaps)
print("ping cadence: median gap", G.median_gap.median(), "s | trips with a gap > 90 s:", int((G.n_gaps_gt90 > 0).sum()),
      "| longest gap:", G.max_gap.max(), "s")

print("=" * 100, "\n3. MISSING EVENTS")
cols_done = T[T.extract_complete]
dropped = cols_done[(~cols_done.arrived)]
print("trips whose pings stop before the terminal (extract-complete trips only):\n",
      dropped[["trip_id", "vehicle", "last_dist_pct", "last_ping"]].round(2).to_string(index=False) if len(dropped) else "none")
print("trips with no pings at all:", T[T.n_pings == 0].trip_id.tolist())
print("trips run by a different vehicle than scheduled:", T[T.substituted][["trip_id", "vehicle"]].to_dict("records"))
b = bookings
print("bookings:", len(b), "| trips referenced that are not in trips.csv:", sorted(set(b.trip_id) - set(trips.trip_id)))
print("bookings with null promised_eta:", int(b.promised_eta.isna().sum()), b[b.promised_eta.isna()].booking_id.tolist())
term = trips.merge(eng.routes[["route_id", "dest_stop"]], on="route_id")[["trip_id", "dest_stop"]]
bt = b.merge(term, on="trip_id", how="left")
print("bookings boarding at the terminus:", bt[bt.boarding_stop_id == bt.dest_stop].booking_id.tolist())
print("trips with no bookings:", sorted(set(trips.trip_id) - set(b.trip_id)))

print("=" * 100, "\n4. GPS ANOMALIES")
off, spikes = [], []
for tid, (tr, meta) in tracks.items():
    t = trips[trips.trip_id == tid].iloc[0]
    g = eng.geoms[t.route_id]
    c = ctx.window(meta["vehicle"], t.scheduled_start - pd.Timedelta(minutes=20), t.scheduled_end + pd.Timedelta(minutes=90))
    c = c[c.lat.between(la0, la1) & c.lon.between(lo0, lo1)]
    if c.empty: continue
    _, o = g.snap(c.lat.to_numpy(), c.lon.to_numpy())
    bad = c[o > eng.cfg.max_snap_offset_m]
    for (_, rr), oo in zip(bad.iterrows(), o[o > eng.cfg.max_snap_offset_m]):
        off.append(dict(trip_id=tid, ping_id=rr.ping_id, offset_m=round(oo), at=rr.recorded_at))
print("pings > 300 m off their route line (after dropping out-of-bounds):", off)
# implied speed between consecutive raw pings of the same vehicle
q = p.sort_values(["vehicle_id", "recorded_at"]).copy()
q["dt"] = q.groupby("vehicle_id").recorded_at.diff().dt.total_seconds()
q["dd"] = hav_m(q.groupby("vehicle_id").lat.shift().to_numpy(), q.groupby("vehicle_id").lon.shift().to_numpy(), q.lat.to_numpy(), q.lon.to_numpy())
q["v"] = q.dd / q.dt * 3.6
fast = q[(q.dt >= 5) & (q.v > 90) & q.lat.between(la0, la1)]
print("implied speed > 90 km/h between consecutive in-bounds pings:", len(fast), fast[["ping_id", "vehicle_id", "v"]].round(0).to_dict("records")[:6])
cmp_ = q[(q.dt.between(20, 40)) & q.speed_kmph.notna() & q.lat.between(la0, la1) & (q.dd < 2000)]
print("reported vs implied speed: median abs difference =", round((cmp_.speed_kmph - cmp_.v).abs().median(), 1), "km/h over", len(cmp_), "pairs")

print("=" * 100, "\n5. IRREGULAR ROUTE BEHAVIOUR")
irr = T[(T.longest_stand_min >= 5)][["trip_id", "date", "longest_stand_min", "last_dist_pct"]].round(1)
print("trips with a standing run >= 5 min away from origin/terminal:\n", irr.to_string(index=False) if len(irr) else "none")
print("\npre-start presence at origin (median minutes before scheduled start the first ping appears):",
      T.groupby("route").first_ping_before_start_min.median().round(1).to_dict())

print("=" * 100, "\n6. PROMISED ETA vs ACTUAL BUS ARRIVAL AT THE BOARDING STOP (completed trips)")
recs = []
for _, bk in b.dropna(subset=["promised_eta"]).iterrows():
    if bk.trip_id not in tracks: continue
    tr, meta = tracks[bk.trip_id]
    if tr is None: continue
    t = trips[trips.trip_id == bk.trip_id].iloc[0]
    g = eng.geoms[t.route_id]
    st = g.stops[g.stops.stop_id == bk.boarding_stop_id]
    if st.empty or st.index[0] == len(g.stops) - 1: continue
    cumd = g.cum[st.index[0]]
    if tr.dist.max() < cumd: continue
    x = tr.drop_duplicates("dist")
    tsec = np.interp(cumd, x.dist.to_numpy(), (x.t_eff - pd.Timestamp("2026-01-01")).dt.total_seconds().to_numpy())
    actual = pd.Timestamp("2026-01-01") + pd.to_timedelta(tsec, unit="s")
    sched = g.sched_time(t.scheduled_start, cumd)
    recs.append(dict(route=t.route_id, date=t.service_date.date(), vs_promise=(actual - bk.promised_eta).total_seconds() / 60,
                     vs_timetable=(actual - sched).total_seconds() / 60, promise_vs_timetable=(bk.promised_eta - sched).total_seconds() / 60))
P = pd.DataFrame(recs)
print("bookings evaluated:", len(P))
print(P.groupby("route").agg(n=("vs_promise", "size"), promise_minus_timetable=("promise_vs_timetable", "median"),
                             bus_vs_promise_median=("vs_promise", "median"),
                             pct_bus_gt5_after_promise=("vs_promise", lambda s: (s > 5).mean() * 100),
                             pct_bus_gt3_after_timetable=("vs_timetable", lambda s: (s > 3).mean() * 100)).round(1))

print("=" * 100, "\n7. TRIP-LEVEL TABLE (extract-complete trips, Wed only)")
w = T[(T.date == pd.Timestamp("2026-06-17").date())]
print(w[["trip_id", "vehicle", "substituted", "skewed", "n_pings", "n_rejected", "arrived", "end_delay", "longest_stand_min", "last_ping"]].round(1).to_string(index=False))
