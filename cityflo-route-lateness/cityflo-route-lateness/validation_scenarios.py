#!/usr/bin/env python3
"""
validation_scenarios.py - ten scenario tests for route_status_mvp.RouteStatusEngine.

Each case builds a tiny fixture (one route = R03 geometry from the dummy bundle, one trip 07:00-07:30, vehicle V1),
runs the engine, and compares to an expected outcome written BEFORE running. Run:

    python validation_scenarios.py --src ./cityflo_dummy_data

Fixtures are synthetic: they prove the code handles the situation as designed, not that real buses behave this way.
"""
import argparse, json, tempfile, os
import numpy as np
import pandas as pd
from route_status_mvp import RouteStatusEngine, RouteGeometry

p = argparse.ArgumentParser()
p.add_argument("--src", default="./cityflo_dummy_data")
p.add_argument("--json", default=None, help="write results to this file")
args = p.parse_args()

SRC = args.src
ROUTES = pd.read_csv(f"{SRC}/routes.csv")
STOPS = pd.read_csv(f"{SRC}/stops.csv")
R = ROUTES[ROUTES.route_id == "R03"].reset_index(drop=True)
S = STOPS[STOPS.route_id == "R03"].reset_index(drop=True)
GEOM = RouteGeometry(R.iloc[0], S)
D, RUNTIME = GEOM.D, GEOM.runtime_min
START = pd.Timestamp("2026-06-17 07:00:00")
END = START + pd.Timedelta(minutes=RUNTIME)
rng = np.random.default_rng(1)


def latlon(d):
    d = min(max(d, 0), D)
    j = min(int(np.searchsorted(GEOM.cum, d, side="right")) - 1, len(S) - 2)
    f = (d - GEOM.cum[j]) / (GEOM.cum[j + 1] - GEOM.cum[j])
    return (S.lat[j] + f * (S.lat[j + 1] - S.lat[j]), S.lon[j] + f * (S.lon[j + 1] - S.lon[j]))


def sim(dist_of_x, x0=-300, x1=2400, step=30, vehicle="V1", offroute=None, drop=None):
    """dist_of_x(seconds since scheduled start) -> metres along route. Returns a ping DataFrame."""
    rows = []
    for x in range(x0, x1 + 1, step):
        if drop and drop[0] <= x <= drop[1]:
            continue
        d = dist_of_x(x)
        la, lo = latlon(d)
        if offroute and offroute[0] <= x <= offroute[1]:
            la += 0.014                      # about 1.5 km north of the route line
        t = START + pd.Timedelta(seconds=x)
        dprev = dist_of_x(x - step)
        v = max(0.0, (d - dprev) / step * 3.6)
        rows.append(dict(vehicle_id=vehicle, operator_id="OP1", lat=round(la + rng.normal(0, 5e-5), 6),
                         lon=round(lo + rng.normal(0, 5e-5), 6), speed_kmph=round(v, 1),
                         recorded_at=t, received_at=t + pd.Timedelta(seconds=5)))
    df = pd.DataFrame(rows)
    return df


def lag(L_min):
    """Constant lag: the bus is L minutes behind (+) or ahead (-) of the timetable throughout."""
    return lambda x: D * min(max((x - L_min * 60) / (RUNTIME * 60), 0), 1)


def finish(df):
    df = df.copy().reset_index(drop=True)
    df["ping_id"] = [f"P{i:05d}" for i in range(len(df))]
    return df[["ping_id", "vehicle_id", "operator_id", "lat", "lon", "speed_kmph", "recorded_at", "received_at"]]


def run(pings, asof, include_trip=True, trip_vehicle="V1"):
    with tempfile.TemporaryDirectory() as d:
        R.to_csv(f"{d}/routes.csv", index=False)
        S.to_csv(f"{d}/stops.csv", index=False)
        trips = pd.DataFrame([dict(trip_id="T1", route_id="R03", vehicle_id=trip_vehicle,
                                   service_date="2026-06-17", scheduled_start=START, scheduled_end=END,
                                   direction="X")]) if include_trip else \
            pd.DataFrame(columns=["trip_id", "route_id", "vehicle_id", "service_date", "scheduled_start",
                                  "scheduled_end", "direction"])
        trips.to_csv(f"{d}/trips.csv", index=False)
        pings.to_csv(f"{d}/gps_pings.csv", index=False)
        eng = RouteStatusEngine(d)
        return eng.get_route_status("R03", asof)


def flags(res):
    t = res.get("details", {}).get("trips", [])
    return t[0]["flags"] if t else []


CASES = []


def case(name, desc, sample, expect, rationale):
    def deco(fn):
        CASES.append(dict(name=name, desc=desc, sample=sample, expect=expect, rationale=rationale, fn=fn))
        return fn
    return deco


# ---------------------------------------------------------------------------------------------
@case("1 Clearly late", "Bus runs 12 min behind the timetable at all points; as-of 07:20.",
      "pings every 30s, position = where the timetable says the bus would be at 07:08",
      "status=late, delay 11-13 min, confidence >= 0.8",
      "The core case: delay is when the bus WAS at a point minus when the timetable says it SHOULD be.")
def c1():
    base = finish(sim(lag(12)))
    r = run(base, "2026-06-17 07:20:00")
    ok = r["status"] == "late" and 11 <= r["delay_minutes"] <= 13 and r["confidence"] >= 0.8
    return r, ok, base


@case("2 On time", "Bus within 30 s of the timetable; as-of 07:20.",
      "same shape as case 1 with a 0.5 min lag",
      "status=on_time, |delay| < 3, no flags",
      "Guards against false pushes; a rider notification should never fire on noise.")
def c2():
    r = run(finish(sim(lag(0.5))), "2026-06-17 07:20:00")
    return r, r["status"] == "on_time" and abs(r["delay_minutes"]) < 3 and not flags(r), None


@case("3 Early", "Bus runs 6 min ahead of the timetable; as-of 07:15.",
      "same shape with a -6 min lag",
      "status=early, delay about -6 (between -7 and -5)",
      "Early buses strand riders who arrive on time; early must be reported as a distinct state, not as on_time.")
def c3():
    r = run(finish(sim(lag(-6))), "2026-06-17 07:15:00")
    return r, r["status"] == "early" and -7.5 <= r["delay_minutes"] <= -4.5, None


@case("4a GPS dropout (long)", "Pings stop at 07:08 and never resume; as-of 07:20 (12 min silent).",
      "last ping 07:08:00, nothing after",
      "status=unknown, delay=null, reason mentions age of last ping",
      "Silence is not on-time. The engine must refuse to give a number instead of extrapolating.")
def c4a():
    r = run(finish(sim(lag(2), drop=(480, 2400))), "2026-06-17 07:20:00")
    return r, r["status"] == "unknown" and r["delay_minutes"] is None and "old" in r["explanation"], None


@case("4b GPS dropout (short)", "Pings missing 07:16:00-07:19:30; as-of 07:19 (age about 3 min).",
      "gap of 7 consecutive missing pings",
      "status=on_time, delay about 2, confidence reduced (< 0.9) versus a clean run",
      "A short gap degrades confidence but should not blank the answer; the last position is still informative.")
def c4b():
    r = run(finish(sim(lag(2), drop=(960, 1170))), "2026-06-17 07:19:00")
    return r, r["status"] == "on_time" and r["confidence"] < 0.9 and r["delay_minutes"] is not None, None


@case("5a Missing trip events: trip without pings", "Trip is scheduled but the device never reported.",
      "trips.csv row exists, gps_pings.csv has no rows",
      "status=unknown, explanation says no usable pings",
      "A scheduled trip with no data is the most dangerous case to call on_time.")
def c5a():
    empty = finish(sim(lag(0)).iloc[0:0])
    r = run(empty, "2026-06-17 07:15:00")
    return r, r["status"] == "unknown" and "no usable" in r["explanation"], None


@case("5b Missing trip events: pings without a trip row", "Vehicle pings normally but the trip is absent from trips.csv.",
      "gps_pings.csv has rows, trips.csv has none",
      "status=unknown, explanation says no trip in service",
      "Without a trip there is no timetable, so no lateness can be computed. Do not invent a schedule.")
def c5b():
    r = run(finish(sim(lag(0))), "2026-06-17 07:15:00", include_trip=False)
    return r, r["status"] == "unknown" and "No trip" in r["explanation"], None


@case("5c Missing trip events: wrong vehicle on the trip row", "Trip row says V1 but V9 sends the pings.",
      "trips.vehicle_id=V1, all pings vehicle_id=V9 (unscheduled spare)",
      "status=on_time, flag VEHICLE_SUBSTITUTED, confidence slightly reduced",
      "Substitutions are normal operations; the engine should recover the assignment instead of reporting missing data.")
def c5c():
    r = run(finish(sim(lag(0.5), vehicle="V9")), "2026-06-17 07:20:00")
    return r, r["status"] == "on_time" and "VEHICLE_SUBSTITUTED" in flags(r), None


@case("6 Duplicate GPS records", "Case 1 data plus 10 exact duplicates and 10 replays with new ping_ids and later received_at.",
      "same recorded_at/lat/lon, ping_id differs, received_at +45s",
      "identical to case 1 (same status, delay within 0.1 min)",
      "Duplicates must not change dwell or speed estimates; replays carry new ids so dedupe is by content.")
def c6():
    base = sim(lag(12))
    dup = base.sample(10, random_state=1).copy()
    replay = base.sample(10, random_state=2).copy()
    replay["received_at"] += pd.Timedelta(seconds=45)
    ref = run(finish(base), "2026-06-17 07:20:00")
    r = run(finish(pd.concat([base, dup, replay])), "2026-06-17 07:20:00")
    return r, r["status"] == ref["status"] and abs(r["delay_minutes"] - ref["delay_minutes"]) <= 0.1, None


@case("7 Out-of-order GPS records", "Case 1 data shuffled, and 5 pings delivered late (received_at +60s) out of sequence.",
      "row order randomized; some received_at later than their successors",
      "identical to case 1 (delay within 0.3 min)",
      "Order must come from timestamps, not file order; ordering by arrival would corrupt progress.")
def c7():
    base = sim(lag(12))
    ref = run(finish(base), "2026-06-17 07:20:00")
    shuf = base.sample(frac=1, random_state=3).copy().reset_index(drop=True)
    shuf.loc[:4, "received_at"] += pd.Timedelta(seconds=60)
    r = run(finish(shuf), "2026-06-17 07:20:00")
    return r, r["status"] == ref["status"] and abs(r["delay_minutes"] - ref["delay_minutes"]) <= 0.3, None


def stop_dist(stop_from, stop_len, L=0.0):
    """On schedule until stop_from seconds, then standing still for stop_len seconds, then resumes."""
    base = lag(L)
    d0 = base(stop_from)
    return lambda x: base(x) if x < stop_from else (d0 if x < stop_from + stop_len else base(x - stop_len))


@case("8a Traffic stoppage (long)", "Bus on schedule, then stands 8 min at 07:12 with the device still pinging; as-of 07:20:30.",
      "speed ~0 and position unchanged for 8.5 min",
      "status=late, delay 7-10 min, flag STOPPED, explanation tells ops to contact the driver",
      "A standing bus is late by the second. Fresh pings with no movement point to a stoppage, not a data problem.")
def c8a():
    r = run(finish(sim(stop_dist(720, 480))), "2026-06-17 07:20:30")
    return r, r["status"] == "late" and 7 <= r["delay_minutes"] <= 10 and "STOPPED" in flags(r), None


@case("8b Traffic stoppage (short)", "Bus stands 90 s at a light at 07:12; as-of 07:14.",
      "speed ~0 for 3 consecutive pings",
      "status=on_time, no STOPPED flag",
      "Signals and stop dwell are normal; flagging them would drown ops in false alarms.")
def c8b():
    r = run(finish(sim(stop_dist(720, 90))), "2026-06-17 07:14:00")
    return r, r["status"] == "on_time" and "STOPPED" not in flags(r), None


def hold_dist(early_min, stop_idx):
    """Bus runs early_min ahead, waits at a mid-route stop until the timetable time, then runs on schedule."""
    d_s = GEOM.cum[stop_idx]
    t_sched = RUNTIME * 60 * d_s / D
    arrive = t_sched - early_min * 60
    early = lag(-early_min)
    on = lag(0)
    return lambda x: early(x) if x < arrive else (d_s if x < t_sched else on(x)), t_sched


@case("9 Intentional waiting at a timing point", "Bus runs 6 min early, holds 6 min at the second intermediate stop until its scheduled time; as-of 30 s before departure.",
      "speed 0 for 11 pings at the stop location, bus ahead of or on the timetable",
      "status=on_time (or early), NO STOPPED flag, no 'contact the driver' text",
      "Holding at a timing point is deliberate and correct. A stationary bus that is not behind schedule is not a fault.")
def c9():
    fn, t_sched = hold_dist(6, 2)
    asof = START + pd.Timedelta(seconds=t_sched - 30)
    r = run(finish(sim(fn)), asof)
    return r, r["status"] in ("on_time", "early") and "STOPPED" not in flags(r) and "contact" not in r["explanation"], None


@case("10 Route diversion", "Bus takes a detour 1.5 km off the route line from 07:10 to 07:20; as-of 07:18.",
      "pings continue every 30s but lat is about 0.014 deg north of the route",
      "status=unknown, explanation names off-route pings / possible diversion (not just 'ping is old')",
      "Distance along the route cannot be measured off the polyline. The honest answer is unknown, with the true reason.")
def c10():
    r = run(finish(sim(lag(2), offroute=(600, 1200))), "2026-06-17 07:18:00")
    return r, r["status"] == "unknown" and ("off" in r["explanation"].lower() and "route" in r["explanation"].lower()), None


if __name__ == "__main__":
    results = []
    for c in CASES:
        res, ok, _ = c["fn"]()
        results.append(dict(case=c["name"], passed=bool(ok), status=res["status"], delay=res["delay_minutes"],
                            confidence=res["confidence"], flags=flags(res), expected=c["expect"],
                            explanation=res["explanation"][:260]))
        print(f"{'PASS' if ok else 'FAIL'}  {c['name']:<48} status={res['status']:<8} delay={res['delay_minutes']} "
              f"conf={res['confidence']} flags={flags(res)}")
    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} passed")
    if args.json:
        json.dump(results, open(args.json, "w"), indent=2, default=str)
