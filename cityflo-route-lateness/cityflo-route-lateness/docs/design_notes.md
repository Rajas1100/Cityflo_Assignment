# Design notes

Condensed record of the analysis, data model and pipeline design produced during this work. The code in the repository
root is the implementation; this file is the reasoning behind it. Everything here was derived from the assignment brief
and the synthetic bundle, not from the real Cityflo files.

## 1. Problem analysis

**Business question.** For each route at 07:45 IST on 2026-06-17, which action is justified by the evidence available then:
push a delay notice, hold, call the driver, or give no verdict. Plus a ranked worry-order.

**Entities (inferred).** Route, Stop (ordered), Trip (one scheduled run), Vehicle, Operator, GPS ping (two clocks), Booking, Rider.
Pings carry a vehicle, not a trip, so trip assignment must be inferred.

**Minimum information needed.** (1) which trip each ping belongs to; (2) position along the route; (3) a reference for "on time";
(4) a trust signal on the position; (5) a way to separate late from look-alikes; (6) riders still ahead of the bus.

**Ambiguities in the prompt.**
- "Late" is undefined: against timetable, typical runtime, promised ETA, or projected arrival.
- Late at what point: now, next stop, destination, rider's stop.
- "Route X" can mean several concurrent trips; the roll-up rule is not given.
- "Now" by `recorded_at` or `received_at`.
- Delay thresholds for push versus call are not given.
- Whether a trip that has not started can be late; whether history is a baseline or a comparison.

**Assumptions to validate rather than accept.** Field definitions in the data guide; device clocks; units and lat/lon order;
stable vehicle-to-trip mapping; unique `ping_id`; consistent timezones; Mon/Tue representative; GPS accuracy sufficient for snapping.

**MVP means.** Loads and cleans; maps pings to trips and distance along route; one stated definition of late; trust assessment with a
`NO_VERDICT` path; a verdict table with a one-line reason and worry-order; a short findings list; re-runnable for a new as-of and dataset.

## 2. Data model

```
routes ──< route_stops
  └──< trips >── vehicle_trip_assignment >── (vehicle_id)
         ├──< bookings
         ├──< trip_stop_events
         └──< route_progress_snapshots ──< lateness_calculations
raw_gps_pings ──1:1── ping_enriched (carries trip_id)
```

| Table | Purpose | Primary key | Foreign keys |
|---|---|---|---|
| `raw_gps_pings` | Telemetry exactly as received | `ping_id` | none |
| `ping_enriched` | Derived trip, distance along route, quality flags | `ping_id` | `ping_id`, `trip_id` |
| `routes` | Route definitions and scheduled runtime | `route_id` | none |
| `route_stops` | Ordered stops with cumulative distance | `(route_id, seq)` | `route_id` |
| `trips` | The schedule | `trip_id` | `route_id` |
| `trip_stop_events` | Expected and observed arrival per stop | `(trip_id, stop_id)` | `trip_id`, `stop_id` |
| `bookings` | Who is affected, and the promised ETA | `booking_id` | `trip_id`, `boarding_stop_id` |
| `vehicle_trip_assignment` | Scheduled and inferred vehicle per trip | `(trip_id, vehicle_id, source)` | `trip_id` |
| `route_progress_snapshots` | Facts about where a trip is at `as_of_ts` | `(trip_id, as_of_ts)` | `trip_id`, `last_ping_id` |
| `lateness_calculations` | Delay under a named method, trust and candidate verdict | `(trip_id, as_of_ts, method)` | snapshot key |

Key columns:
- `raw_gps_pings(ping_id, vehicle_id, operator_id, lat, lon, speed_kmph, recorded_at, received_at)`
- `ping_enriched(trip_id, dist_along_m, snap_offset_m, clock_skew_sec, is_duplicate, is_out_of_bounds, is_usable)`
- `route_stops(route_id, seq, stop_id, stop_name, lat, lon, cum_dist_m)`
- `route_progress_snapshots(trip_status, last_ping_id, data_age_sec, dist_along_m, pct_complete, recent_speed_kmph, pings_last_10min, usable_ping_ratio)`
- `lateness_calculations(method, expected_time, observed_time, delay_sec, trust, trust_reason, candidate_verdict, riders_affected)`

Principles: raw layer never edited; everything keyed by `as_of_ts`; only data knowable at `as_of_ts`; flags instead of deletes.

**Implementation status.** The MVP implements the enriched and result tables as in-memory DataFrames per query and does not
persist them. `vehicle_trip_assignment`, `trip_stop_events` and persisted snapshots are design only; the engine derives
equivalent values on the fly.

## 3. Pipeline stages

| Stage | Inputs → outputs | Main failure modes | Validation check |
|---|---|---|---|
| Ingest + as-of filter | CSVs, `as_of` → known pings, quarantine | timezone mix-ups, replays with new ids, future leakage | rows conserved; `max(received_at) ≤ as_of`; rerun identical |
| Route reconstruction | stops → polyline, cumulative distance, scheduled time by distance | substituted vehicle, linear-timetable error | `cum_dist` strictly increasing; plausible implied speed |
| GPS ordering | pings → ordered sequences, per-vehicle clock stats | slow or fast device clock, burst delivery | skew outliers listed; backward progress within tolerance |
| Route progress | clean pings → distance along route, snapshot status | wrong-segment snap, terminal dwell | offset within bound; replay completed trips reach D |
| Delay estimation | snapshot + schedule → delay by named method | optimistic timetable, stale last ping | backtest mid-trip vs final delay |
| Confidence | snapshot flags → trust level and reasons | arbitrary thresholds, over-blanking | share of "ok" rows with error > 5 min is low |
| Missing data | schedule + snapshot → one status per trip | reading silence as on time | no verdict is HOLD while stale/no data |
| Noisy GPS | ordered pings → usable flags | over-filtering after a gap | rejected share small; known junk caught |
| Sparse history | completed trips → shrunk baseline with n and spread | one anomalous day dominates | leave-one-day-out stability |

Thresholds used by the implementation are in `Config` in `route_status_mvp.py` and are starting values, not calibrated.
