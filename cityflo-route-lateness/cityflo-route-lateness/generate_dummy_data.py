import math, os, shutil, datetime as dt
import numpy as np
import pandas as pd

rng = np.random.default_rng(617)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cityflo_dummy_data")
os.makedirs(OUT, exist_ok=True)
FMT = "%Y-%m-%d %H:%M:%S"
ASOF = dt.datetime(2026, 6, 17, 7, 45, 0)
WED_CUT = dt.datetime(2026, 6, 17, 8, 15, 0)   # extract "pulled" at 08:15 on the live day
DATES = [dt.date(2026, 6, 15), dt.date(2026, 6, 16), dt.date(2026, 6, 17)]  # Mon, Tue, Wed

# ---------------------------------------------------------------- routes / stops
# NOTE: coordinates are approximate and for illustration only.
ROUTES = {
    "R01": dict(name="Borivali (W) to BKC", runtime=70, starts=["06:30", "07:00", "07:30", "08:00"], typ_lag=6.0,
                stops=[("S_BORW", "Borivali Station (W)", 19.2307, 72.8567),
                       ("S_KNDV", "Kandivali Thakur Village", 19.2070, 72.8740),
                       ("S_MALD", "Malad Mindspace", 19.1795, 72.8365),
                       ("S_GORG", "Goregaon Oberoi Mall", 19.1663, 72.8526),
                       ("S_JOGE", "Jogeshwari WEH", 19.1360, 72.8490),
                       ("S_ANDM", "Andheri WEH Metro", 19.1197, 72.8464),
                       ("S_ARPT", "Airport Santacruz WEH", 19.0970, 72.8553),
                       ("S_BKC", "BKC Jio Garden", 19.0690, 72.8680)]),
    "R02": dict(name="Thane to Navi Mumbai (Vashi)", runtime=55, starts=["06:30", "07:00", "07:30", "08:00"], typ_lag=1.5,
                stops=[("S_THN", "Thane Station", 19.1860, 72.9759),
                       ("S_TEEN", "Teen Hath Naka", 19.1790, 72.9830),
                       ("S_AIRO", "Airoli Naka", 19.1650, 72.9990),
                       ("S_RABL", "Rabale MIDC", 19.1400, 73.0030),
                       ("S_GHAN", "Ghansoli", 19.1170, 73.0090),
                       ("S_KOPR", "Koparkhairane", 19.1030, 73.0110),
                       ("S_VASH", "Vashi Sector 17", 19.0745, 72.9990)]),
    "R03": dict(name="Ghatkopar to BKC", runtime=30, starts=["06:45", "07:15", "07:30", "07:50"], typ_lag=2.0,
                stops=[("S_GHAT", "Ghatkopar Station", 19.0860, 72.9080),
                       ("S_VIDY", "Vidyavihar", 19.0790, 72.8970),
                       ("S_KURK", "Kurla Kamani", 19.0720, 72.8880),
                       ("S_KURS", "Kurla Station (W)", 19.0650, 72.8790),
                       ("S_BKC", "BKC Jio Garden", 19.0690, 72.8680)]),
    "R04": dict(name="Thane to Andheri", runtime=80, starts=["06:15", "06:45", "07:15", "07:45"], typ_lag=3.0,
                stops=[("S_THN", "Thane Station", 19.1860, 72.9759),
                       ("S_MULD", "Mulund Check Naka", 19.1740, 72.9560),
                       ("S_BHAN", "Bhandup Pump House", 19.1520, 72.9380),
                       ("S_POWA", "Powai Hiranandani", 19.1190, 72.9050),
                       ("S_JVLR", "JVLR SEEPZ", 19.1290, 72.8790),
                       ("S_ANDM", "Andheri WEH Metro", 19.1197, 72.8464)]),
}

def hav(a, b):
    R = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dphi = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))

routes_rows, stops_rows = [], []
for rid, r in ROUTES.items():
    cum = [0.0]
    for i in range(1, len(r["stops"])):
        cum.append(cum[-1] + hav(r["stops"][i - 1][2:], r["stops"][i][2:]))
    r["cum"] = cum
    r["D"] = cum[-1]
    for i, s in enumerate(r["stops"], 1):
        stops_rows.append(dict(stop_id=s[0], stop_name=s[1], lat=s[2], lon=s[3], route_id=rid, seq=i))
    routes_rows.append(dict(route_id=rid, route_name=r["name"], origin_stop=r["stops"][0][0],
                            dest_stop=r["stops"][-1][0], scheduled_runtime_min=r["runtime"],
                            stops_count=len(r["stops"])))

def position(r, d):
    pts = r["stops"]
    cum = r["cum"]
    d = min(max(d, 0.0), cum[-1])
    j = min(int(np.searchsorted(cum, d, side="right")) - 1, len(pts) - 2)
    seg = cum[j + 1] - cum[j]
    f = 0 if seg == 0 else (d - cum[j]) / seg
    return (pts[j][2] + f * (pts[j + 1][2] - pts[j][2]), pts[j][3] + f * (pts[j + 1][3] - pts[j][3]))

# ---------------------------------------------------------------- trips
def vid(ri, ti):
    return f"V{(ri - 1) * 4 + ti:02d}"

OPERATOR = {f"V{n:02d}": ("OP1" if n <= 6 else "OP2" if n <= 12 else "OP3") for n in range(1, 17)}
OPERATOR["V17"] = "OP3"
OPERATOR["V18"] = "OP9"   # spare vehicle from an operator that is not in any schedule

trips = []
for d in DATES:
    for ri, (rid, r) in enumerate(ROUTES.items(), 1):
        for ti, st in enumerate(r["starts"], 1):
            h, m = map(int, st.split(":"))
            s = dt.datetime.combine(d, dt.time(h, m))
            trips.append(dict(trip_id=f"T{d:%m%d}-{rid}-{ti:02d}", route_id=rid, vehicle_id=vid(ri, ti),
                              service_date=d, scheduled_start=s,
                              scheduled_end=s + dt.timedelta(minutes=r["runtime"]),
                              direction="TO_DESTINATION", _ti=ti))

# ---------------------------------------------------------------- scenarios (Wednesday only)
WED = dt.date(2026, 6, 17)
SCEN = {
    "T0617-R01-02": dict(L=12.0, trap="GENUINE_LATE: congestion builds, +12 vs timetable (R01 normally runs ~+6, so ~+6 vs usual)"),
    "T0617-R01-03": dict(L=5.0, burst=True, trap="LOOKS_STALE_NOT_LATE: pings recorded 07:34-07:45 arrive in a burst at ~07:46, so at 07:45 the last known ping is ~11 min old; bus is fine (+5 typical, ramping)"),
    "T0617-R02-02": dict(L=1.0, skew_min=-9, trap="CLOCK_SKEW: device clock 9 min slow; recorded_at looks late, received_at shows bus is on time"),
    "T0617-R02-03": dict(L=2.0, sub="V18", trap="VEHICLE_SUBSTITUTION: scheduled V07 never pings, spare V18 (operator OP9) runs the trip"),
    "T0617-R03-02": dict(L=2.0, junk=True, trap="GPS_JUNK: one (0,0) ping and one ~4 km jump ping; trip is otherwise fine"),
    "T0617-R03-03": dict(L=3.0, silent_after=dt.datetime(2026, 6, 17, 7, 34, 0), trap="SILENT_DEVICE: pings stop at 07:34 and never resume; absence of data is not on-time"),
    "T0617-R04-02": dict(L=3.0, stuck=(27 * 60, 45 * 60), trap="STOPPED_BUT_PINGING: from ~07:12 speed ~0 for 45 min, device keeps pinging; ~33+ min behind"),
    "T0617-R04-03": dict(L=4.0, dup=(dt.datetime(2026, 6, 17, 7, 32), dt.datetime(2026, 6, 17, 7, 36)), trap="DUPLICATE_REPLAY: pings 07:32-07:36 are replayed with new ping_ids"),
    "T0617-R04-04": dict(L=3.0, trap="BOUNDARY: scheduled start == as-of; bus is still at/near origin, not late"),
}
HIST_EXTRA = {("2026-06-16", "R04"): 9.0, ("2026-06-16", "R02"): 3.5}   # Tue incident on R04, rain on R02
SHAPE = {1: 0.6, 2: 1.0, 3: 1.1, 4: 1.0}

def lag_fn(runtime_min, L, stuck=None):
    ramp = 0.6 * runtime_min * 60
    base = lambda x: L * 60 * min(1.0, max(x, 0) / ramp)
    if stuck is None:
        return base
    x0, dur = stuck
    return lambda x: base(x) if x < x0 else base(x0) + min(x - x0, dur)

pings, assign_rows, gt_rows = [], [], []
notes = {"junk": [], "burst": [], "dup": []}

for t in trips:
    r = ROUTES[t["route_id"]]
    D, R = r["D"], r["runtime"]
    d_iso = t["service_date"].isoformat()
    sc = SCEN.get(t["trip_id"], {}) if t["service_date"] == WED else {}
    if sc:
        L = sc["L"]
    else:
        L = max(0.0, r["typ_lag"] * SHAPE[t["_ti"]] + HIST_EXTRA.get((d_iso, t["route_id"]), 0.0) + rng.normal(0, 1.0))
    lagf = lag_fn(R, L, sc.get("stuck"))
    veh = sc.get("sub", t["vehicle_id"])
    start = t["scheduled_start"]
    skew = dt.timedelta(minutes=sc.get("skew_min", 0))

    def dist_at(x):
        return D * min(max((x - lagf(x)) / (R * 60), 0.0), 1.0)

    rows = []
    x = -12 * 60.0
    arrived = 0
    while x < 4 * 3600:
        xj = x + rng.uniform(-3, 3)
        true_t = start + dt.timedelta(seconds=xj)
        d = dist_at(xj)
        dprev = dist_at(xj - 30)
        if xj < 0:
            v = abs(rng.normal(0, 0.6))
        else:
            v = max(0.0, (d - dprev) / 30 * 3.6 + rng.normal(0, 0.8))
            if sc.get("stuck") and sc["stuck"][0] <= xj <= sum(sc["stuck"]):
                v = abs(rng.normal(0, 0.4))
        lat, lon = position(r, d)
        lat += rng.normal(0, 0.00007)
        lon += rng.normal(0, 0.00007)
        recv = true_t + dt.timedelta(seconds=float(rng.uniform(2, 9)))
        if t["service_date"] == WED and true_t > WED_CUT:
            break
        rows.append(dict(vehicle_id=veh, operator_id=OPERATOR[veh], lat=round(lat, 6), lon=round(lon, 6),
                         speed_kmph=round(v, 1), recorded_at=true_t + skew, received_at=recv, _true=true_t, _trip=t["trip_id"]))
        if d >= D:
            arrived += 1
            if arrived > 6:
                break
        x += 30

    if sc.get("silent_after"):
        rows = [q for q in rows if q["_true"] <= sc["silent_after"]]
    if sc.get("burst"):
        b0, b1 = dt.datetime(2026, 6, 17, 7, 34), dt.datetime(2026, 6, 17, 7, 46)
        for q in rows:
            if b0 <= q["_true"] < b1:
                q["received_at"] = dt.datetime(2026, 6, 17, 7, 46, 30) + dt.timedelta(seconds=float(rng.uniform(0, 20)))
                q["_burst"] = True
    if sc.get("junk"):
        tgt = [min(rows, key=lambda q: abs((q["_true"] - dt.datetime(2026, 6, 17, 7, h)).total_seconds()))
               for h in (30, 34)]
        tgt[0]["lat"], tgt[0]["lon"] = 0.0, 0.0
        tgt[1]["lon"] = round(tgt[1]["lon"] + 0.04, 6)
        tgt[0]["_junk"] = tgt[1]["_junk"] = True
    if sc.get("dup"):
        extra = []
        for q in rows:
            if sc["dup"][0] <= q["_true"] <= sc["dup"][1]:
                c = dict(q)
                c["received_at"] = q["received_at"] + dt.timedelta(seconds=45)
                c["_dup"] = True
                extra.append(c)
        rows += extra
    for q in rows:
        if rng.random() < 0.003:
            q["speed_kmph"] = np.nan
    pings += rows

    # vehicle assignment rows
    assign_rows.append(dict(trip_id=t["trip_id"], vehicle_id=t["vehicle_id"], source="scheduled",
                            valid_from=start, valid_to=t["scheduled_end"],
                            confidence="high", note=""))
    mv = [q for q in rows if not q.get("_junk")]
    if mv:
        first_move = next((q["_true"] for q in mv if q["speed_kmph"] and q["speed_kmph"] > 3), mv[0]["_true"])
        assign_rows.append(dict(trip_id=t["trip_id"], vehicle_id=veh, source="inferred_from_pings",
                                valid_from=first_move, valid_to=max(q["_true"] for q in mv),
                                confidence="high" if veh == t["vehicle_id"] else "medium",
                                note="" if veh == t["vehicle_id"] else "pings come from a different vehicle than scheduled"))

    # ground truth for the live morning
    if t["service_date"] == WED:
        xa = (ASOF - start).total_seconds()
        if xa < 0:
            status, tdist, tdelay = "NOT_STARTED", 0.0, 0.0
        else:
            dd = dist_at(xa)
            if dd >= D:
                status, tdist, tdelay = "COMPLETED", D, round(lagf(R * 60 + 3600) / 60, 1)
            else:
                status, tdist, tdelay = "IN_PROGRESS", round(dd), round(lagf(xa) / 60, 1)
        known = [q for q in rows if q["received_at"] <= ASOF and not q.get("_dup")]
        lastq = max(known, key=lambda q: q["received_at"]) if known else None
        gt_rows.append(dict(trip_id=t["trip_id"], route_id=t["route_id"], scheduled_start=start.strftime(FMT),
                            status_at_0745=status, true_dist_along_m=tdist, true_delay_min=tdelay,
                            last_ping_known_at_0745=(lastq["recorded_at"].strftime(FMT) if lastq else ""),
                            minutes_since_last_known_ping=(round((ASOF - lastq["_true"]).total_seconds() / 60, 1) if lastq else ""),
                            planted_trap=sc.get("trap", "none (ordinary trip)")))

# ---------------------------------------------------------------- ping ids
pdf = pd.DataFrame(pings).sort_values(["received_at", "vehicle_id"]).reset_index(drop=True)
pdf["ping_id"] = [f"P{n:06d}" for n in range(1, len(pdf) + 1)]
for c in ("_junk", "_burst", "_dup"):
    if c not in pdf:
        pdf[c] = False
    pdf[c] = pdf[c].fillna(False).astype(bool)
notes["junk"] = pdf.loc[pdf["_junk"], "ping_id"].tolist()
notes["burst_n"] = int(pdf["_burst"].sum())
notes["burst_example"] = pdf.loc[pdf["_burst"], "ping_id"].head(3).tolist()
notes["dup_n"] = int(pdf["_dup"].sum())
notes["dup_example"] = pdf.loc[pdf["_dup"], "ping_id"].head(3).tolist()
out_pings = pdf[["ping_id", "vehicle_id", "operator_id", "lat", "lon", "speed_kmph", "recorded_at", "received_at"]].copy()
for c in ("recorded_at", "received_at"):
    out_pings[c] = out_pings[c].dt.strftime(FMT)
out_pings.to_csv(f"{OUT}/gps_pings.csv", index=False)

# ---------------------------------------------------------------- bookings
POOL = {rid: [f"RD{i}{k:03d}" for k in range(60)] for i, rid in enumerate(ROUTES, 1)}
NB = {"R01": 20, "R02": 16, "R03": 12, "R04": 18}
PROMISE_SHIFT = {"R01": -4, "R03": 3, "R02": 0, "R04": 0}   # R01 promise is optimistic, R03 padded
bk = []
for t in trips:
    r = ROUTES[t["route_id"]]
    n = max(6, NB[t["route_id"]] + int(rng.integers(-3, 4)))
    k = len(r["stops"]) - 1
    w = np.linspace(1.0, 0.3, k)
    w = w / w.sum()
    riders = rng.choice(POOL[t["route_id"]], size=n, replace=False)
    for rd in riders:
        si = int(rng.choice(k, p=w))
        stop = r["stops"][si]
        sched_arr = t["scheduled_start"] + dt.timedelta(seconds=r["runtime"] * 60 * r["cum"][si] / r["D"])
        eta = sched_arr + dt.timedelta(minutes=PROMISE_SHIFT[t["route_id"]])
        prev = dt.datetime.combine(t["service_date"] - dt.timedelta(days=1), dt.time(18, 0)) + \
            dt.timedelta(minutes=int(rng.integers(0, 330)))
        bk.append(dict(trip_id=t["trip_id"], rider_id=rd, boarding_stop_id=stop[0], booked_at=prev, promised_eta=eta))
bdf = pd.DataFrame(bk)
bdf["booking_id"] = [f"B{n:06d}" for n in range(1, len(bdf) + 1)]
# deliberate junk
junk_idx = rng.choice(len(bdf), size=3, replace=False)
bdf.loc[junk_idx, "promised_eta"] = pd.NaT
term = bdf[(bdf["trip_id"].str.startswith("T0617")) & (bdf["trip_id"].str.contains("R02"))].index[:2]
bdf.loc[term, "boarding_stop_id"] = "S_VASH"                       # boarding at the terminus: invalid
orphan = bdf.iloc[[0]].copy()
orphan["trip_id"] = "T0617-R05-01"
orphan["booking_id"] = f"B{len(bdf) + 1:06d}"
bdf = pd.concat([bdf, orphan], ignore_index=True)
notes["bk_null_eta"] = bdf.loc[junk_idx, "booking_id"].tolist()
notes["bk_terminus"] = bdf.loc[term, "booking_id"].tolist()
notes["bk_orphan"] = orphan["booking_id"].tolist()
out_b = bdf[["booking_id", "trip_id", "rider_id", "boarding_stop_id", "booked_at", "promised_eta"]].copy()
for c in ("booked_at", "promised_eta"):
    out_b[c] = pd.to_datetime(out_b[c]).dt.strftime(FMT)
out_b.to_csv(f"{OUT}/bookings.csv", index=False)

# ---------------------------------------------------------------- other tables
pd.DataFrame(routes_rows).to_csv(f"{OUT}/routes.csv", index=False)
pd.DataFrame(stops_rows).to_csv(f"{OUT}/stops.csv", index=False)
tdf = pd.DataFrame(trips).drop(columns=["_ti"])
for c in ("scheduled_start", "scheduled_end"):
    tdf[c] = tdf[c].dt.strftime(FMT)
tdf.to_csv(f"{OUT}/trips.csv", index=False)
adf = pd.DataFrame(assign_rows)
for c in ("valid_from", "valid_to"):
    adf[c] = pd.to_datetime(adf[c]).dt.strftime(FMT)
adf.to_csv(f"{OUT}/vehicle_trip_assignment.csv", index=False)
gdf = pd.DataFrame(gt_rows)
gdf.to_csv(f"{OUT}/ANSWER_KEY_ground_truth_wed_0745.csv", index=False)

print(len(pdf), "pings;", len(trips), "trips;", len(bdf), "bookings")
print(notes)
print(gdf[["trip_id", "status_at_0745", "true_delay_min", "minutes_since_last_known_ping"]].to_string())
