"""
dashboard_explain.py - turns one route status decision into a card an operations controller can read in 30 seconds.

Input : the dict returned by RouteStatusEngine.get_route_status (needs result["details"]).
Output: {"headline", "severity", "how_late", "certainty", "why", "evidence", "alternatives", "action",
         "text", "word_count", "read_seconds"}

Design rules
    R1  Under 90 words in total (about 27 s at 200 words/min). Lowest-priority lines are dropped first.
    R2  Plain words only: no "ping", "skew", "snap", "baseline", "confidence score". Times as HH:MM, ages in seconds/minutes.
    R3  Every card answers five questions in a fixed order: how late, how sure, why, what supports it, what else could explain it,
        followed by one action. A fixed order lets controllers scan instead of read.
    R4  Alternatives come from the actual state (stopped, clock corrected, silent tracker...), never from generic boilerplate,
        and they reflect the false-late paths found in false_late_probes.py.
    R5  When certainty is not High, the action says to confirm before acting.
    R6  Severity colours: critical (red), warning (amber), ok (green), info (grey).
"""
from __future__ import annotations

MAX_WORDS = 90
WORDS_PER_MIN = 200          # a conservative reading speed for a quick glance at a dashboard
JARGON = ["ping", "skew", "snap", "offset", "polyline", "baseline", "median", "quartile", "untrustworthy",
          "confidence score", "t_eff", "vehicle_id", "heuristic", "threshold"]


def _hm(ts):
    return ts[11:16] if ts else "?"


def _ago(sec):
    if sec is None:
        return "unknown time"
    sec = int(round(sec))
    if sec < 90:
        return f"{sec} seconds ago"
    return f"{sec / 60:.0f} min ago" if sec < 3600 else f"{sec / 3600:.1f} h ago"


def _certainty(result, dec, status):
    c = result["confidence"]
    label = "High" if c >= 0.85 else "Medium" if c >= 0.6 else "Low"
    if status == "unknown":
        return {"label": "Cannot tell", "reason": "the data is not good enough to give a number"}
    bits = []
    if dec and dec.get("data_age_s") is not None:
        bits.append(f"GPS updated {_ago(dec['data_age_s'])}")
    flags = dec.get("flags", []) if dec else []
    if "CLOCK_CORRECTED" in flags:
        bits.append("tracker clock corrected")
    if "VEHICLE_SUBSTITUTED" in flags:
        bits.append("a different bus than scheduled")
    return {"label": label, "reason": "; ".join(bits[:2]) if bits else "based on the latest GPS data"}


def build_dashboard_card(result: dict, route_name: str) -> dict:
    status = result["status"]
    delay = result["delay_minutes"]
    details = result.get("details", {})
    trips = details.get("trips", [])
    dec = next((t for t in trips if t["trip_id"] == details.get("decision_trip")), None)
    flags = dec.get("flags", []) if dec else []
    blind = [t for t in trips if t["kind"] != "completed" and t["trust"] == "untrustworthy"]
    base = details.get("baseline", {})
    rid = result["route_id"]
    label = f"{rid} {route_name}"
    pct = f"{dec['pct_complete'] * 100:.0f}%" if dec and dec.get("pct_complete") is not None else None
    veh = dec["vehicle_id"] if dec else None
    cert = _certainty(result, dec, status)
    evidence, alts = [], []

    # ------------------------------------------------------------------ no bus in service
    if status == "unknown" and not details.get("decision_trip") and not blind:
        unsched = details.get("unscheduled_vehicles") or []
        hm = _hm(result["timestamp"])
        if unsched:
            c2 = {"label": "Cannot tell", "reason": "the schedule does not list this bus"}
            why = f"No trip is scheduled at {hm}, but bus {unsched[0]} is reporting GPS on this route."
            alts = ["The schedule data may be missing this trip.", "The bus may be running without a planned trip."]
            return _finish(label, "warning", "NO SCHEDULED TRIP", c2, why, [], alts,
                           "Check the schedule; phone the driver if needed.", result)
        c2 = {"label": "High", "reason": "no trip is scheduled right now"}
        why = result["explanation"]
        return _finish(label, "info", "No bus in service", c2, why, [], [], "No action needed.", result)

    # ------------------------------------------------------------------ headline, why, action
    if status == "late":
        mins = f"{delay:.0f}"
        how = f"about {mins} min behind the timetable"
        severity = "critical" if ("STOPPED" in flags or delay >= 15) else "warning"
        near_end = "NEAR_END" in flags
        if near_end:
            severity = "warning"       # may simply have arrived: do not shout
        headline = f"LATE by about {mins} min"
        if "STOPPED" in flags:
            why = f"Bus {veh} has barely moved for {dec['stopped_minutes']:.0f} min, but its GPS is still updating."
            action = ("Check whether the bus has reached its last stop; if not, call the driver."
                      if near_end else "Call the driver now.")
        elif "AT_ORIGIN" in flags:
            why = f"Bus {veh} has not left the start yet. It was due out at {_hm(dec['expected_time_at_position'])}."
            action = "Check the bus has left; warn riders if it has not."
        else:
            why = (f"Bus {veh} reached its current point at {_hm(dec['observed_time_at_position'])}. "
                   f"The timetable expects it there at {_hm(dec['expected_time_at_position'])}.")
            action = "Call the driver and warn riders ahead." if delay >= 15 else "Warn riders at stops ahead."
        if cert["label"] != "High" and not near_end:
            action = "Confirm first, then: " + action[0].lower() + action[1:]
    elif status == "on_time":
        severity = "ok"
        headline = "ON TIME"
        how = "within 3 min of the timetable"
        if "HOLDING" in flags:
            why = f"Bus {veh} is waiting at a stop and is not behind schedule."
        else:
            if abs(delay) < 1:
                gap = "on schedule"
            else:
                gap = f"{abs(delay):.0f} min {'behind' if delay > 0 else 'ahead of'} the timetable"
            why = f"Bus {veh} is {pct} of the way and {gap}."
        action = "No action needed." if cert["label"] == "High" else "No action; recheck in a few minutes."
    elif status == "early":
        severity = "warning"
        headline = f"EARLY by about {abs(delay):.0f} min"
        how = f"about {abs(delay):.0f} min ahead of the timetable"
        why = (f"Bus {veh} reached its current point at {_hm(dec['observed_time_at_position'])}. "
               f"The timetable expects it at {_hm(dec['expected_time_at_position'])}.")
        action = "Hold the bus at its next stop so riders do not miss it."
        if cert["label"] != "High":
            action = "Confirm first, then: " + action[0].lower() + action[1:]
    else:   # unknown because some live trip cannot be assessed
        severity = "warning"
        headline = "CANNOT TELL"
        how = "unknown"
        b = blind[0]
        when = f"the {b['departure']} trip" if b.get("departure") else "one trip"
        if b.get("data_age_s") is None:
            why = f"No GPS has been received from the bus on {when}."
        else:
            at = f", {b['pct_complete'] * 100:.0f}% of the way" if b.get("pct_complete") is not None else ""
            why = f"The last GPS update for {when} was {_ago(b['data_age_s'])}{at}."
        if dec:
            why += f" The {dec['departure']} trip looks fine."
        action = "Phone the driver to confirm where the bus is."

    # ------------------------------------------------------------------ evidence
    # Listed from most to least important: the word-budget trimmer removes items from the END of this list,
    # so a caveat about an unassessable trip must come before routine position detail.
    if status == "late" and "STOPPED" in flags and pct:
        evidence.append(f"It is {pct} of the way along the route, {delay:.0f} min behind where the timetable puts it.")
    if dec and dec.get("riders_ahead_of_bus"):
        evidence.append(f"{dec['riders_ahead_of_bus']} booked riders are still waiting at stops ahead.")
    if status != "unknown":
        for b in blind:
            if dec is None or b["trip_id"] != dec["trip_id"]:
                age = "no recent GPS" if b.get("data_age_s") is None else f"GPS last seen {_ago(b['data_age_s'])}"
                evidence.append(f"The {b['departure']} trip could not be checked ({age}).")
    if status == "late" and "STOPPED" not in flags and "AT_ORIGIN" not in flags and pct:
        evidence.append(f"It is {pct} of the way along the route.")

    # ------------------------------------------------------------------ alternative explanations
    if status in ("late", "early") and "CLOCK_CORRECTED" in flags:
        alts.append("We corrected the tracker clock; if data is simply arriving late, the real gap may be smaller.")
    if status == "late":
        if "STOPPED" in flags:
            if dec["pct_complete"] and dec["pct_complete"] >= 0.85:
                alts.append("The bus may have finished and parked away from the last stop.")
            else:
                alts.append("A planned wait or driver break that our timetable does not know about.")
            alts.append("Heavy traffic or an incident on the road.")
        else:
            if base.get("available") and base["n"] >= 6 and base["median_delay_min"] >= 2 and delay > base["median_delay_min"]:
                alts.append(f"This route usually ends about {base['median_delay_min']:.0f} min late, "
                            f"so only about {delay - base['median_delay_min']:.0f} min is unusual.")
            if "AT_ORIGIN" in flags:
                alts.append("The departure time may have been changed or boarding is still under way.")
            elif delay < 8:
                alts.append("A short traffic delay that may clear within minutes.")
            if delay < 5 and "AT_ORIGIN" not in flags:
                alts.append("GPS error can add 1 to 2 min to a small delay.")
            # Always offer at least two real alternatives: the schedule itself may be what is wrong.
            if len(alts) < 2 and "AT_ORIGIN" not in flags:
                alts.append("The departure time may have been changed; check the dispatch sheet.")
            if len(alts) < 2:
                alts.append("Our timetable may not include a planned wait on this stretch.")
    elif status == "on_time":
        if "CLOCK_CORRECTED" in flags:
            alts.append("The tracker clock was wrong; we corrected it, so the time may be off by a minute.")
        if "VEHICLE_SUBSTITUTED" in flags:
            alts.append("A different bus than scheduled is running this trip.")
        if "HOLDING" in flags:
            alts.append("If the wait continues past its planned time, it becomes a delay.")
        if not alts:
            alts.append("GPS error could hide a delay of 1 to 2 min.")
    elif status == "early":
        alts.append("The timetable may be too slow on this stretch.")
        if "CLOCK_CORRECTED" in flags:
            alts.append("The tracker clock may be fast.")
    else:
        b = blind[0]
        if "POSSIBLE_DIVERSION" in b.get("flags", []):
            alts.append("The bus may be on a detour, off its normal route.")
        elif "NO_DATA" in b.get("flags", []):
            alts.append("The tracker may be switched off, or the trip has not started.")
        else:
            alts.append("The tracker or phone may have lost signal; the bus could be running fine.")
        alts.append("We do not treat missing data as on time.")

    return _finish(label, severity, headline, cert, why, evidence, alts, action, result, how)


def _finish(label, severity, headline, cert, why, evidence, alts, action, result, how=None):
    def assemble(ev, al):
        lines = [f"{headline} · {label}",
                 f"Certainty: {cert['label']} ({cert['reason']})",
                 f"Why: {why}"]
        if ev:
            lines.append("Evidence: " + " ".join(ev))
        if al:
            lines.append("Could also be: " + " ".join(al))
        lines.append(f"Do next: {action}")
        return "\n".join(lines)

    ev, al = list(evidence), list(alts)
    text = assemble(ev, al)
    # R1: drop the least important content until the card fits the word budget.
    while len(text.split()) > MAX_WORDS and (len(al) > 1 or len(ev) > 0):
        if len(al) > 1:
            al.pop()
        elif ev:
            ev.pop()
        text = assemble(ev, al)
    n = len(text.split())
    return {"headline": headline, "severity": severity, "how_late": how or headline, "certainty": cert,
            "why": why, "evidence": ev, "alternatives": al, "action": action, "text": text,
            "word_count": n, "read_seconds": round(n / WORDS_PER_MIN * 60)}
