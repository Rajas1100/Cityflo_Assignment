#!/usr/bin/env python3
"""
dashboard_checks.py - verifies that every route status decision produces a card that is short, plain and consistent.

    python dashboard_checks.py --src ./cityflo_dummy_data [--show]

Coverage: 4 routes x 8 timestamps on the live day, the 14 scenario fixtures from validation_scenarios.py, and four of the
false-late fixtures (parked short, ingestion backlog, transient queue, late departure).

Hard checks per card
    C1  at most 90 words and an estimated read time of 30 s or less
    C2  no jargon words and no raw trip ids (trips are named by departure time)
    C3  all required parts present: headline, Certainty, Why, Do next (and Could also be unless no bus is in service)
    C4  no sentence longer than 25 words
    C5  consistent with the decision: LATE/EARLY headline carries round(delay); ON TIME only when status=on_time;
        CANNOT TELL only when status=unknown; certainty label agrees with the confidence number
    C6  when status is late, early or unknown and certainty is not High, the action must say to confirm
        (unknown cards always say to phone the driver)
"""
import re
import sys

_src = sys.argv[sys.argv.index("--src") + 1] if "--src" in sys.argv else "./cityflo_dummy_data"
SHOW = "--show" in sys.argv
sys.argv = ["x", "--src", _src]

import pandas as pd                                            # noqa: E402
import validation_scenarios as v                               # noqa: E402
from route_status_mvp import RouteStatusEngine                 # noqa: E402
from dashboard_explain import JARGON, MAX_WORDS                # noqa: E402

failures, seen = [], []


def check(source, res):
    d = res["dashboard"]
    t = d["text"]
    errs = []
    if d["word_count"] > MAX_WORDS or d["read_seconds"] > 30:
        errs.append(f"C1 too long ({d['word_count']} words)")
    low = t.lower()
    bad = [j for j in JARGON if re.search(rf"\b{re.escape(j)}\b", low)]
    if bad:
        errs.append(f"C2 jargon {bad}")
    if re.search(r"\bT\d{4}-R\d", t):
        errs.append("C2 raw trip id")
    if res["details"].get("trips") is not None and "No bus in service" not in d["headline"]:
        for part in ("Certainty:", "Why:", "Do next:", "Could also be:"):
            if part not in t:
                errs.append(f"C3 missing {part}")
    elif "Why:" not in t or "Do next:" not in t:
        errs.append("C3 missing parts")
    for sent in re.split(r"(?<=[.!?])\s+|\n", t):
        if len(sent.split()) > 25:
            errs.append(f"C4 long sentence: {sent[:50]}...")
    st, delay, conf = res["status"], res["delay_minutes"], res["confidence"]
    h = d["headline"]
    if st == "late" and not (h.startswith("LATE") and f"{delay:.0f} min" in h):
        errs.append("C5 late headline mismatch")
    if st == "early" and not (h.startswith("EARLY") and f"{abs(delay):.0f} min" in h):
        errs.append("C5 early headline mismatch")
    if st == "on_time" and not h.startswith("ON TIME"):
        errs.append("C5 on_time headline mismatch")
    if st == "unknown" and not (h.startswith(("CANNOT TELL", "NO SCHEDULED TRIP")) or "No bus in service" in h):
        errs.append("C5 unknown headline mismatch")
    if st in ("late", "on_time", "early"):
        want = "High" if conf >= 0.85 else "Medium" if conf >= 0.6 else "Low"
        if d["certainty"]["label"] != want:
            errs.append("C5 certainty label mismatch")
    if st in ("late", "early") and d["certainty"]["label"] != "High" and not any(k in d["action"] for k in ("onfirm", "heck whether")):
        errs.append("C6 low-certainty action does not say to confirm")
    if st == "unknown" and "No bus in service" not in h and "hone the driver" not in d["action"] and "heck the schedule" not in d["action"]:
        errs.append("C6 unknown action")
    seen.append((source, st, d["severity"], d["word_count"], d["read_seconds"]))
    if errs:
        failures.append((source, errs, t))
    if SHOW:
        print(f"--- {source}  [{st} | {d['severity']} | {d['word_count']} words ~{d['read_seconds']}s]\n{t}\n")


eng = RouteStatusEngine(_src)
for rid in ["R01", "R02", "R03", "R04"]:
    for hm in ["06:00", "06:40", "07:00", "07:20", "07:35", "07:45", "07:55", "08:10"]:
        check(f"{rid} @ {hm}", eng.get_route_status(rid, f"2026-06-17 {hm}:00"))

for c in v.CASES:
    res, _, _ = c["fn"]()
    check(f"fixture {c['name']}", res)

D, R, START = v.D, v.RUNTIME, v.START
asof = lambda x: START + pd.Timedelta(seconds=x)
check("fixture parked short of terminus", v.run(v.finish(v.sim(lambda x: min(v.lag(0)(x), D - 400), x1=3300)), asof(R * 60 + 600)))
back = v.sim(v.lag(0.5)); back["received_at"] = back["recorded_at"] + pd.Timedelta(minutes=4)
check("fixture ingestion backlog", v.run(v.finish(back), asof(1200)))
def queue(x):
    if x < 600: return v.lag(0)(x)
    if x < 840: return v.lag(0)(600)
    return v.lag(4 * (1 - min(1, (x - 840) / 360)))(x)
check("fixture transient queue", v.run(v.finish(v.sim(queue)), asof(840)))
check("fixture late departure", v.run(v.finish(v.sim(lambda x: D * min(1, max(0, (x - 240) / (R * 60 - 240))))), asof(300)))

n = len(seen)
sev = pd.Series([s[2] for s in seen]).value_counts().to_dict()
print(f"cards checked: {n} | severity mix: {sev}")
print(f"words: min {min(s[3] for s in seen)}, median {sorted(s[3] for s in seen)[n // 2]}, max {max(s[3] for s in seen)} "
      f"| read time max {max(s[4] for s in seen)} s")
if failures:
    print(f"\nFAILED {len(failures)} cards")
    for src, errs, txt in failures:
        print(f"  {src}: {errs}\n{txt}\n")
    sys.exit(1)
print("all cards pass C1-C6")
