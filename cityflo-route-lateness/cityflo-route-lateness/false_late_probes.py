#!/usr/bin/env python3
"""
false_late_probes.py - fixtures where a bus is NOT truly late, to see whether route_status_mvp labels it late.

    python false_late_probes.py --src ./cityflo_dummy_data

Re-uses the fixture helpers in validation_scenarios.py (R03 geometry, one trip 07:00-07:30, vehicle V1).
Each probe states the ground truth by construction, so a "FALSE LATE" verdict is a real defect, not a matter of opinion.
"""
import sys
import numpy as np
import pandas as pd

_src = sys.argv[sys.argv.index("--src") + 1] if "--src" in sys.argv else "./cityflo_dummy_data"
sys.argv = ["x", "--src", _src]
import validation_scenarios as v   # noqa: E402  (builds the R03 fixture helpers)
from route_status_mvp import RouteStatusEngine   # noqa: E402

D, R, START = v.D, v.RUNTIME, v.START
cum = v.GEOM.cum
rows = []


def record(name, truth, res, false_when, note=""):
    t = res.get("details", {}).get("trips", [{}])
    fl = t[0].get("flags", []) if t else []
    flagged = res["status"] in false_when
    rows.append(dict(probe=name, truth=truth, status=res["status"], delay=res["delay_minutes"],
                     confidence=res["confidence"], flags=fl, false_late=flagged, note=note))


def asof(x_sec):
    return START + pd.Timedelta(seconds=x_sec)


# ---- P1  GPS gap right after a slow patch: last known delay is stale, the bus has since recovered
def gap_dist(x):
    if x <= 690:
        return v.lag(4)(x)                     # 4 min behind when the signal is lost (07:11:30)
    f = min(1, (x - 690) / 270)                # then recovers to 1 min behind by 07:16 (never observed)
    return v.lag(4 - 3 * f)(x)
res = v.run(v.finish(v.sim(gap_dist, drop=(720, 2400))), asof(16 * 60))
record("P1 gap after a slow patch", "bus recovered to ~1 min behind; not late", res, {"late"}, "last ping 4.5 min old")

# ---- P2  signal drift: along-track error grows to 700 m behind the true position, bus really 0.5 min behind
def drift_dist(x):
    f = min(1, max(0, (x - 480) / 480))
    return max(0.0, v.lag(0.5)(x) - 700 * f)
res = v.run(v.finish(v.sim(drift_dist)), asof(20 * 60))
record("P2 along-track drift (700 m)", "true delay 0.5 min; on time", res, {"late"}, "700 m is ~3 min at this route's pace")

# ---- P3a route start delay that is recovered by running faster
def recover_dist(x):
    return D * min(1, max(0, (x - 240) / (R * 60 - 240)))   # leaves 4 min late, arrives on time
res = v.run(v.finish(v.sim(recover_dist)), asof(5 * 60))
record("P3 late departure that recovers", "will arrive on time; late only at the start", res, {"late"},
       "late NOW is real; the forecast for downstream riders is overstated")

# ---- P4  planned 8 min halt at the second intermediate stop that the linear timetable does not model
d_s = cum[2]
v_run = D / (R * 60 - 480)
T1 = d_s / v_run
def halt_dist(x):
    return v_run * x if x < T1 else (d_s if x < T1 + 480 else d_s + v_run * (x - T1 - 480))
res = v.run(v.finish(v.sim(halt_dist)), asof(T1 + 450))
record("P4 planned halt not in the timetable", "on its real timetable; deliberate 8 min halt", res, {"late"})

# ---- P5  driver parks 400 m short of the terminus (bay / short turn); trip is finished
def park_dist(x):
    return min(v.lag(0)(x), D - 400)
res = v.run(v.finish(v.sim(park_dist, x1=2400 + 900)), asof(R * 60 + 600))
record("P5 parks short of terminus / short-turn", "trip completed on time; bus idle", res, {"late"},
       "arrival rule needs the bus within 250 m of the last stop")

# ---- P6  transient queue: stands 4 min, then recovers to the timetable by 07:20
def queue_dist(x):
    if x < 600:
        return v.lag(0)(x)
    if x < 840:
        return v.lag(0)(600)
    f = min(1, (x - 840) / 360)
    return v.lag(4 * (1 - f))(x)
res = v.run(v.finish(v.sim(queue_dist)), asof(14 * 60))
record("P6 transient bottleneck", "4 min queue that clears within ~6 min", res, {"late"})

# ---- P7a stale schedule: ops moved the departure to 07:10 but trips.csv still says 07:00
res = v.run(v.finish(v.sim(v.lag(10))), asof(20 * 60))
record("P7a stale trip schedule (+10 min)", "on its revised schedule; on time", res, {"late"})

# ---- P7b every device clock is 4 min fast; arrival time (received_at) is correct
fast = v.sim(v.lag(0.5))
fast["recorded_at"] += pd.Timedelta(minutes=4)
res = v.run(v.finish(fast), asof(20 * 60))
record("P7b uniform device clock +4 min", "on time", res, {"late"}, "arrival-time correction should absorb it")

# ---- P7c ingestion backlog: every ping is delivered 4 min after it was recorded
back = v.sim(v.lag(0.5))
back["received_at"] = back["recorded_at"] + pd.Timedelta(minutes=4)
res = v.run(v.finish(back), asof(20 * 60))
record("P7c ingestion backlog (4 min)", "on time", res, {"late"}, "uniform lag looks like a clock fault")

pd.set_option("display.width", 220, "display.max_colwidth", 60)
df = pd.DataFrame(rows)
print(df[["probe", "status", "delay", "confidence", "flags", "false_late"]].to_string(index=False))
print("\nfalse-late verdicts:", int(df.false_late.sum()), "of", len(df))

# ---- P8  sparse baseline: how much does 'usual lateness' move with the days used?
eng = RouteStatusEngine(_src)
ctx = eng._build_ctx(pd.Timestamp("2026-06-17 07:45:00"))
hist = eng.trips[eng.trips.service_date < pd.Timestamp("2026-06-17")]
out = []
for _, t in hist.iterrows():
    g = eng.geoms[t.route_id]
    tr, _ = eng._track(t, g, ctx)
    arr = tr[tr.dist >= g.D - eng.cfg.terminal_tol_m]
    a = arr.iloc[0]
    out.append(dict(route=t.route_id, day=t.service_date.day, delay=(a.t_eff - g.sched_time(t.scheduled_start, a.dist)).total_seconds() / 60))
H = pd.DataFrame(out)
tab = H.pivot_table(index="route", columns="day", values="delay", aggfunc="median").round(1)
tab["both days"] = H.groupby("route").delay.median().round(1)
tab["mon-vs-tue gap"] = (tab[16] - tab[15]).round(1)
print("\nP8 median arrival delay by baseline window (days 15=Mon, 16=Tue):\n", tab)
