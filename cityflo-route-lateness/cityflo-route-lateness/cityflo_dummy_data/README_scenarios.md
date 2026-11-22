# Cityflo-style dummy dataset (synthetic)

Four routes, three mornings (Mon 2026-06-15, Tue 06-16, Wed 06-17 = live). As-of is 07:45 IST on Wednesday.
All identifiers, riders and coordinates are invented; coordinates are approximate and for illustration only.

## Files

| File | Rows | Notes |
|---|---|---|
| routes.csv | 4 | R01 Borivali (W) to BKC, R02 Thane to Navi Mumbai (Vashi), R03 Ghatkopar to BKC, R04 Thane to Andheri |
| stops.csv | 26 | Key is (route_id, seq). S_BKC is shared by R01/R03, S_THN by R02/R04, S_ANDM by R01/R04 |
| trips.csv | 48 | 4 trips per route per day |
| gps_pings.csv | ~7,000 | 30 s cadence. Wed pings are cut at 08:15, so rows after 07:45 are "future" and must be excluded by as-of |
| bookings.csv | ~777 | ~12-20 riders per trip |
| vehicle_trip_assignment.csv | 96 | Scheduled rows for all trips, inferred-from-pings rows from the generator |
| ANSWER_KEY_ground_truth_wed_0745.csv | 16 | Simulation truth for Wednesday. Do not open before attempting your own verdicts |

Not seeded on purpose: ping_enriched, trip_stop_events, route_progress_snapshots, lateness_calculations. Those are pipeline outputs.

## Baseline behaviour (Mon/Tue)

Typical lateness against the timetable: R01 about +6 min (timetable is optimistic), R02 about +1.5, R03 about +2, R04 about +3.
Tuesday pollutes the baseline: R04 runs about +12 on every trip (incident), R02 about +5 (rain).
R01 promised_eta is 4 min earlier than the timetable arrival; R03 is 3 min later.

## Planted traps on Wednesday

| Trip | Trap | What a naive read gets wrong |
|---|---|---|
| T0617-R01-02 | Genuine congestion, true delay 12 min at 07:45 | Real push candidate. Against R01's own history, excess is only about 6 min |
| T0617-R01-03 | Pings recorded 07:34-07:45 arrive in a burst at about 07:46 (24 pings) | At 07:45 last known ping is about 11 min old, looks stalled; bus is fine |
| T0617-R02-02 | Device clock 9 min slow (recorded_at vs received_at) | recorded_at makes the bus look late or stale; it is on time |
| T0617-R02-03 | Scheduled V07 never pings; spare V18 (operator OP9) runs the trip | Joining pings to trips by scheduled vehicle finds nothing |
| T0617-R03-02 | Two junk pings: a (0,0) ping and a roughly 4 km jump (ids below) | Position jumps; the trip is fine |
| T0617-R03-03 | Pings stop at 07:34 and never resume | Missing data is not evidence of on-time |
| T0617-R04-02 | Stopped from about 07:12 for 45 min, speed about 0, still pinging | True delay about 35 min; a push will not fix it |
| T0617-R04-03 | Pings 07:32-07:36 replayed with new ping_ids (8 rows) | Duplicates distort speed and dwell |
| T0617-R04-04 | Scheduled start equals the as-of time | Not started or just starting, not late |

All trips also have about 12 min of pre-start dwell pings at the origin with speed near 0.
About 19 pings have null speed_kmph.

Example ids: junk pings P006092, P006171; burst starts at P006410; duplicate examples P006158, P006170, P006180.
Booking junk: null promised_eta B000202, B000694, B000345; boarding at terminus B000592, B000593; orphan trip B000777.

## Designer's intended read at 07:45 (a suggestion, not the only defensible answer)

| Route | Intended verdict | Why |
|---|---|---|
| R04 | CALL_DRIVER | T0617-R04-02 stopped about 33 min but still reporting |
| R01 | PUSH_LATE about 12 min | T0617-R01-02 genuinely late. R01-03 is stale, not late |
| R03 | NO_VERDICT for trip 03 | Silent device. Trip 02 is fine |
| R02 | HOLD | Skew and substitution make it look wrong, but both trips are on time |

Suggested worry order: R04, R01, R03, R02.
Route-level roll-up (worst trip vs next trip) is an open ambiguity, and R01's baseline excess versus timetable lateness is the main judgment call.
