# Is route X running late right now, and by how much?

*Submission narrative for Staff Engineers, Data Engineers and Operations Leads.*

> **Evidence base, stated up front.** Every number below comes from a **synthetic bundle generated for this work**
> (4 routes, 3 mornings, 48 trips, 6,979 GPS pings, 777 bookings; `cityflo_dummy_data/`, reproducible byte-for-byte by
> `generate_dummy_data.py`). The real Cityflo bundle was not available to the author when this was built. The findings
> therefore demonstrate that the **method** recovers known patterns. They are **not** statements about Cityflo's buses.
> The same scripts run unchanged on the real files (Section 9, step 1).

---

## 1. Business understanding

Ops asks "is route X late?", but the decision behind it is what to do at 07:45. There are four actions, each with a
different cost of being wrong:

| Action | Wrong call costs |
|---|---|
| Push a delay notice to riders | False push erodes trust; a missed push strands riders |
| Hold (do nothing) | Hides a real problem |
| Call the driver | Needless calls burn depot goodwill; a missing call leaves a breakdown unattended |
| No verdict | Cheap if honest; costly only if used to dodge hard calls |

So the product is **a decision per route with its evidence and certainty**, not a number. A wrong confident answer is worse
than an honest "cannot tell", which drove several design choices below.

## 2. Definition of lateness

**Delay = when the bus was at its current position − when the timetable says it should be there.** Late at +3 min or more,
early at −3 min or less.

Why this definition: a rider notification promises the timetable, so the timetable is the reference. Three alternatives were
considered and kept as *context*, not as the label:
- *Typical runtime from history*: only 2 days of history; Tuesday is contaminated (R04 +11.9 vs +3.3 on Monday).
- *Promised ETA*: differs by route (R01 promise is 4 min earlier than the timetable, R03's 3 min later), so it measures
  rider experience, not operations.
- *Projected arrival*: needs a speed model not justified by 3 mornings.

Rules that make it usable:
- A standing bus gets later by the second, so time since its last update is added.
- A bus still at its origin is late by time past scheduled departure, never negative.
- A bus standing while on or ahead of schedule is **holding**, not broken.
- Missing data is never read as on time. The status is `unknown` unless another trustworthy trip on the route is already late.
- Route roll-up: the worst trustworthy live trip decides.

*Deviation from the brief's output shape:* `status` may also be `unknown`. With only on_time/late/early the engine would have
to guess when data is missing, which is the wrong failure mode.

## 3. Data model

Three layers, one direction of flow: **raw** (as received) → **enriched** (flags, trip, distance along route) → **results**
(snapshot, lateness, verdict). Full schemas are in `docs/design_notes.md`. Decisions that matter:

- **Two clocks kept per ping** (`recorded_at` device, `received_at` server). Position uses device time; *knowability* uses
  server time (a ping delivered after 07:45 was not known at 07:45).
- **Stops keyed by `(route_id, seq)`**, because stop ids repeat across routes (BKC on R01 and R03, Thane on R02 and R04).
- **Trip assignment is its own table** (scheduled and inferred rows), since pings carry a vehicle, not a trip.
- **Flags, never deletes**, so every excluded ping can be shown with a reason.

*Honest scope:* the MVP computes the enriched and result tables in memory per query (about 0.1 s) and does not persist them.

## 4. Computation approach

One pass per query, keyed on an `as_of` timestamp (nothing reads "now"):

1. **Ingest and as-of filter**: only `received_at ≤ as_of`; de-duplicate on content (vehicle, time, lat, lon), because replays carry new ids.
2. **Clock check**: a vehicle whose median delivery delay is far from the fleet's is re-timed by arrival.
3. **Route reconstruction**: stops → polyline → cumulative distance; timetable assumed linear in distance (only start/end are given).
4. **Vehicle assignment**: scheduled vehicle, else an unscheduled spare seen at the origin around departure.
5. **Noise filter and snapping**: bounds, >300 m off route, >90 km/h implied speed; progress is monotone.
6. **Delay** at the last trustworthy ping, with the standing and at-origin rules above.
7. **Trust gating and confidence**: last update older than 8 min or fewer than 3 updates in 10 min means no verdict.
   Otherwise confidence is multiplicative: data age, update density, corrected clock ×0.75, substituted vehicle, near-end standing ×0.6.
8. **Roll-up and dashboard card**: a plain-language card under 90 words (about 27 s): how late, how sure, why, evidence,
   alternative explanations, next action.

Confidence is a heuristic, not a probability.

## 5. Validation strategy

Expected outputs were written before running; fixtures are one trip, 07:00 to 07:30.

| Layer | Result |
|---|---|
| 14 scenario fixtures: late, on time, early, long and short GPS gap, missing trip events (3 kinds), duplicates, out-of-order, long and short stoppage, timing-point hold, diversion | **14/14 pass after fixes; the run found 3 defects**: an empty pings file crashed the engine; a deliberate timing-point hold was flagged as a breakdown; a diversion was reported as "ping is old" |
| As-of leakage check: delete every ping received after 07:45 | All four route answers identical |
| 9 false-late probes (Section 7) | 8 of 9 still labelled late; 2 now carry lower certainty |
| 50 dashboard cards across routes, times and fixtures, 6 checks (length, jargon, structure, sentence length, consistency with the decision, confirm-first wording) | All pass; 39 to 90 words, max read 27 s. Reading the cards by hand found 4 issues the checks missed (all fixed) |
| Blind analysis from source tables only, then compared with the planted traps | Every planted pattern recovered |

*Limit:* fixtures and data are the author's own, so this shows the code does what was designed, not that real buses behave that way.

## 6. Key findings from the data (synthetic)

**Observations**

| Finding | Evidence | Confidence |
|---|---|---|
| R01 is late on almost every trip | Median arrival delay +6.2 (Mon), +5.9 (Tue); 8 of 9 completed trips ≥3 min late | High |
| R01 loses its time in the first half | Median delay 3.0 at 25% of the route, 6.1 at 50%, flat after | High |
| R01's promised ETA is optimistic | Promise is 4.0 min before the timetable; bus arrives a median **7.0 min after the promise**, for **74% of 216 bookings** (R04 17%, R02 7%, R03 0%) | High |
| R04 and R02 had one-day anomalies | R04 +3.3 / +11.9 / +2.9 (Mon/Tue/Wed); R02 +1.7 / +5.6 / +1.0 | High |
| No route runs early | 0 of 38 completed trips at −1 min or earlier (uninformative here: the generator cannot produce early trips) | High |

**Live morning, 07:45 Wednesday**

| Route | Verdict | Confidence | Simulated truth |
|---|---|---|---|
| R01 | late 12.0 min (+ one trip with stale GPS) | 0.85 | 12.0 |
| R02 | on time (1.0) | 0.71 | 1.0 |
| R03 | cannot tell (07:30 trip silent since 07:34) | 0.2 | no data |
| R04 | late 34.6 min, bus V14 stopped 32 min but still reporting, 10 booked riders ahead | 0.95 | 34.7 |

Suggested chase order: R04, R01, R03, R02.

**Interpretation** (medium confidence; I built these patterns, so the cause is not independently inferred): R01's timetable
is probably too tight in its first half, so its ETA promise should be reset before more riders are told 4 min earlier than
the bus can deliver. Tuesday should be excluded from any baseline.

## 7. Cases that looked late but were not

Each probe has a ground truth by construction. This is the honest weak spot: **8 of 9 are still labelled late.**

| Probe | Engine | Why it is not late | Status |
|---|---|---|---|
| Parked 400 m short of terminus (short turn) | late 12.3, now **Low** certainty, action is "check arrival first" | Trip finished on time | Label wrong; escalation softened |
| Ingestion backlog, every update 4 min late | late 4.4, now **Medium** | Recorded times were right; correction was wrong | Label wrong; flagged |
| Planned 8 min halt missing from linear timetable | late 3.3, STOPPED | On its real timetable | Label wrong; alternative named on card |
| Along-track GPS drift, 700 m | late 4.7 at 0.95 | Truly 0.5 min behind | **Undetected** |
| Transient 4 min queue | late 3.5 | Clears in about 6 min | Label wrong; needs persistence rule |
| Late departure that recovers | late 5.0 | Arrives on time | Late *now* is true; forecast overstated |
| Stale trip schedule (+10 min) | late 10.0 | On revised schedule | Needs a schedule-change feed |
| GPS gap after a slow patch | late 4.0, conf 0.51 | Recovered unseen | Low certainty only |
| All device clocks 4 min fast | on time (correct) | n/a | Correct |

Also looked late and was not: stale-but-fine trips (pings delivered in a burst after 07:45, slow device clock 9 min, a
substituted vehicle that left the scheduled one with no data), and 1 junk (0,0) ping, 1 ping 2.6 km off route and 8 replayed duplicates.

## 8. Limitations

- **No real data.** The real-bundle verdict table, decision records with real trip ids, raw session transcript and steering ledger
  that the assignment asks for are **not** produced by this work.
- Timetable is linear in distance; routes are straight lines between stops; nearest-segment snapping can mis-snap on looping routes.
- Thresholds (±3 min, 8 min stale, 0.85/0.6 certainty cut-offs) are uncalibrated.
- 8 of 9 false-late probes still produce a late label; no persistence or trend rule.
- One trip per vehicle per window is assumed.
- History is 2 days; baselines swing with the day chosen (R04 +3.3 to +11.9).
- The "30 seconds" claim is a word-count estimate, not tested with controllers.
- Built interactively with an AI assistant (Claude); the author's own steering decisions must be documented separately.

## 9. Future improvements (priority order)

1. **Run on the real bundle** and replace Sections 6 and 7 with real evidence, real trip ids and a real decision record.
2. **Arrival rule**: treat standing near the end of the route as arrival; confirm against a trip-end event (largest false alarm).
3. **Fleet-level skew**: distinguish one bad device clock from fleet-wide delivery lag; report a delay range when ambiguous.
4. **Persistence and trend**: require the delay to hold across reads before a push; split "late now" from "projected late at the rider's stop".
5. **Real stop times or learned halts** instead of the linear timetable.
6. **Drift check**: device speed vs speed implied by position, and uncertainty carried into the delay interval.
7. **Calibrate** thresholds and confidence by backtesting mid-trip estimates against final arrival on completed trips.
8. **Controller feedback** on the cards; log every verdict with its inputs for audit.
9. **Production path**: event-time streaming, idempotent ingestion, online map matching, schedule-change feed.

---
*Reproduce:* `python generate_dummy_data.py && python validation_scenarios.py && python false_late_probes.py && python dashboard_checks.py && python dataset_analysis.py`
