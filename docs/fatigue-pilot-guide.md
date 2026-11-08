# Science-fair fatigue pilot: operator guide and handoff (2026-11-08)

**Everything verified so far used FICTIONAL participants, made-up readings and made-up sprint times on one laptop. No
camera, video processing, wearable, Zephyr software or person was involved. Nothing here shows that any score measures
fatigue.** The coach makes every substitution decision. The pilot view gives no substitution advice.

## 1. What the pilot software does

- **One vest, one fixed camera, one offline laptop.** The engine stays multi-athlete. The study workflow allows one
  active wearer at a time: each `wearer` entry ends the previous one. A wearer uses only their own baseline (checked
  when the entry is made), only readings from the assigned device, and only readings inside their own assignment.
  Camera-only players never get a vest-only or combined score; both are unavailable, with the reason "not the active
  wearer".
- **Study records** (`POST /api/v1/recordings/{id}/study`, `app/models/study.py`) cover: the session (with the frozen
  fatigue profile text and SHA-256, calibration hash and clock references), wearer, round start and end, sprint start
  and end, sprint result (time with method, or null with a reason), self-rating (0–10 or null with a reason), and notes.
  - Every entry has a client token, so a retry is stored once.
  - Corrections supersede an entry and keep the history.
  - Event time (`at`), receipt time (`created_at`), sample timestamps, sample receipts and any adapter clock mapping
    (the session's `clock_references`) are separate fields.
  - **Round readiness** (`GET .../study/readiness?athlete_id=`, also in the pilot view and `pilot_session.py ready`):
    no_stream, stale_or_unassigned, ready_to_start, warming_up (with seconds left), ready_to_save. A round start is
    refused (409, with the readiness) while the streams are not usable, unless it carries `partial_reason`, which is
    stored and listed in the report ("Rounds started without usable data").
  - **Camera reference opening** (since 2026-11-08): the camera reference period starts at the first round start of
    that athlete that was made with both streams usable; the entry records `opens_camera_reference` and the readiness
    the engine saw. A partial start never opens it, and nothing moves it later. Recordings made before this change
    have no such field and keep the old rule (the athlete's first round start). The frozen profiles' `reference.note`
    text still says "first round_start"; the engine rule above is what applies.
- **Three candidate scores** (`app/core/fatigue.py`, profile `FATIGUE_PROFILE_PATH`): vest-only, camera-only and
  combined, each 0–100 relative to the athlete's own reference. 0 is a real value; "not available" is never 0.
  - Features: heart-rate strain (heart-rate reserve), time above a heart-rate threshold (elapsed time, gaps over 5 s not
    bridged), recovery after a qualifying effort (needs a complete, covered window), heart rate per camera speed
    (combined only; near-zero speed excluded; needs a stated camera clock alignment), physical output change, and
    sprint quality (no sprint in the recent window means no observed opportunity, not a drop).
  - Each feature declares its true inputs, and the profile is refused if a candidate contains a feature outside its
    streams.
  - The combined score is unavailable with a reason whenever a required input is stale, missing or unassigned. It is
    never turned into a one-source score.
  - Maximum heart rate is an explicit per-wearer input with its origin. It is not a safety limit.
  - Not used: sleep, soreness, HRV, jumps and tackles.
- **Save, then start.** The operator saves the score (`score_save`, engine-timed) and only then starts the sprint with a
  confirmation of that saved prediction. Timing evidence per round: saved_before_recorded_start (needs an independent
  start reference with a recorded clock mapping), order_unverified (with its basis), or retrospective. The study
  manifest declares which counts as primary. The older behaviour (a prediction computed when the start marker arrives,
  with a two-second grace period) is kept for old recordings and labelled order_unverified / marker_only, never verified.
- **No peeking.** Marking `sprint_start` without a saved score makes the engine save a prediction from the data entered and received by that
  cutoff, using the session's frozen profile.
  - Every timed sprint is excluded from both streams, from 5 s before it until a set time after it, in all later rolling
    and reference windows.
  - Sprint times, self-ratings and coach labels are not score inputs.
  - Later corrections and late samples never rewrite a saved prediction. A retrospective view is a separate request.
  - `GET .../study/predictions/{id}/reproduce` recomputes and compares.
- **Paired analysis** (`scripts/evaluate.py pilot`): per-candidate coverage and missing reasons, and Pearson r against
  percent sprint slowdown on the matched rows.
  - The slowdown is measured against the participant's best valid time in that session, used for the outcome only.
  - Results are given per participant, per session and pooled, with a round-order comparison, and self-ratings
    separately.
  - The plan's targets (combined r ≥ 0.5 and above both single-source r) are shown as MET, NOT MET or UNDEFINED. They are
    engineering targets, not proof.
  - `pilot-pin` pins the profile and analysis before any held-out data. Rows recorded before the pin are never called
    held out. With three participants, a held-out figure covers one or two people.
- **Camera verification** (`scripts/camera_verify.py`):
  - course distance and average speed over timed intervals, with tracked share, against independent references;
  - exclusions for gaps and uncertain identity;
  - sprint detection (5.5 m/s, 3 consecutive valid samples, exit hysteresis, no bridging of gaps or segment changes)
    against manual labels;
  - jumps and tackles: manual counts only, automatic detection unavailable;
  - peak speed: unmeasured without an independent reference;
  - targets shown as targets: 10 % distance and speed, 15 % event counts.
- **Pilot view** (dashboard "Fatigue pilot" card): the current wearer's three candidates, recent trend, main factors,
  missing-data reasons, live or recorded and fictional labels, with technical detail folded away. It makes no requests
  in public mode.
- **Display delay** (`scripts/pilot_display_delay.py`): engine receipt of each heart-rate sample to the first animation
  frame after the card showed a response calculated after that receipt. Browser time is mapped onto the engine clock
  with a measured offset (± half the probe's round trip). Coalesced samples take the delay of the first update that
  covers them; uncovered samples are counted as missing updates.

## 2. Commands

The printable operator and timer instructions, with the save-then-start flow, the blind timing sheet, the field check
and the timing labels, are in `docs/fatigue-pilot-run-sheet.md`. The commands below are the developer view.

```bash
# the whole fictional pilot in-process with a simulated clock (about 20 s; a software check, not a timed run)
python scripts/pilot_rehearsal.py --mode accelerated --out pilot-acc
# the same against a real engine process at real time (about 9 minutes)
python scripts/pilot_rehearsal.py --mode wall --rounds 2 --out pilot-wall
# the one-laptop reliability check (90 minutes of wall-clock ingest, pilot view polled every second)
python scripts/pilot_rehearsal.py --mode wall --duration-min 90 --out pilot-90
# paired report from exported packages (or --rows for the fixture), pinned first
python scripts/evaluate.py pilot-pin --profile PROFILE.json --analysis-split participant --held-out P3 --out pin.json
python scripts/evaluate.py pilot --packages pilot-wall/pilot-package.zip --out pilot-report [--pin pin.json]
python scripts/evaluate.py pilot --rows scripts/examples/pilot/fictional_analysis_rows.json --out fixture-report
# camera verification (synthetic fixture shown; use approved local references for a real check)
python scripts/camera_verify.py make-synthetic --out cv && python scripts/camera_verify.py report cv/inputs.json --out cv-report
# display delay: run the rehearsal with --hold, point a dashboard copy at the proxy, open it in a foreground tab,
# then post window.__athletrixPilotDisplay to the proxy's /__display_log and analyse
python scripts/pilot_display_delay.py proxy --engine ENGINE_URL --port 3919 --slow-every 10 --slow-s 2.5 --sink display.json
python scripts/pilot_display_delay.py analyze --display display.json --database ENGINE.db
```

A real session uses `scripts/sideline_session.py` for the recording, then `POST .../study` entries (session first, then
a wearer, then rounds and sprints), with the team's own frozen profile in `FATIGUE_PROFILE_PATH`.
`scripts/examples/pilot/fictional_fatigue_profile.json` (development) and `fictional_rehearsal_profile.json` (short
windows) are FICTIONAL.

## 3. Results so far (fictional, this Mac)

See `docs/fatigue-pilot-checklist.md` for the exact numbers and dates. In short:

- **Accelerated rehearsal:** all six saved predictions reproduced after export and import into a second database; the
  report was produced.
- **Wall-clock rehearsal (9 minutes):**
  - P2's dropout before round 2's sprint made vest-only and combined unavailable at that cutoff, while camera-only stayed
    available;
  - the camera-only players never had a combined score;
  - the targets were NOT MET (n = 4, flagged as a small sample), and the round-order comparison shows every candidate
    rising with the round.
- **Display delay:** median 0.66 s, p95 1.84 s, 97.4 % within 2 s, 0 of 384 samples missing, in a foreground tab
  polling every second with every 10th response deliberately delayed 2.5 s.
- **Reliability:** see the checklist for the 90-minute run.

## 4. Decisions the team must make before collecting validation data

- **The approved procedure and permissions:** the protocol (rounds, rest, sprint course, timing method), consent and
  assent, who may hold the data, and the club's permission for any filming. No approval exists or is claimed.
- **The frozen scoring profile:**
  - features, windows, normalisation bounds and weights;
  - the maximum heart rate source per participant;
  - the test-sprint exclusion times;
  - the camera reference window;
  - which features are required.
  The development profile's values are placeholders.
- **Baselines:** resting condition, duration and acceptance.
- **The analysis pin:** split by participant or by session, which data is held out, and what counts as a result. With
  three participants, decide whether any held-out claim is meaningful at all.
- **Freshness rules:** how old a reading may be before a candidate is unavailable (currently 10 s).

## 5. Field dependencies still open

1. **Wearable (Zephyr preferred; purchase, licence, hardware and live access unconfirmed).** Needed:
   - permission to use the documentation;
   - sample Live API payloads with gaps and disconnects;
   - the transport for a third-party client;
   - the units and timestamps of heart rate;
   - whether breathing rate or quality indicators exist.
   The pilot needs only heart rate at about 1 Hz. Live R–R intervals, raw accelerometry and vendor jump or impact counts
   are not assumed. Ingestion stays vendor-agnostic (`wearable/`).
2. **Camera:**
   - a fixed camera calibrated as in `docs/sideline-rehearsal-guide.md` §4;
   - a tape-measured course and timing gates for independent distance and average speed;
   - manually labelled sprints, jumps and tackles from an approved clip;
   - an independent peak-speed reference, if peak speed matters.
3. **Clock alignment:** reference events seen by the camera and the vest's clock, so the combined features can pair
   heart rate with speed (`scripts/time_align.py`).
4. **Timed sprints:** independently timed 20 m sprints. A sprint time is an average speed over the interval, not a
   top speed.

## 6. What the software does not do

- It does not diagnose, decide substitutions or claim fatigue measurement.
- It does not recognise passes, jumps or tackles automatically.
- It does not use a vendor protocol.
- It does not produce a held-out claim from rows recorded before the pin.

## 7. Draft protocol versus the FICTIONAL development profile

The research-plan document is not in either repository, so this compares the profile with the plan points given in the
project instructions. The team should check exact plan values against the document itself. None of the profile's
values are team decisions.

| Topic | Draft protocol (as given) | Development profile now (`fictional_fatigue_profile.json`) | Decision needed |
|---|---|---|---|
| Heart-rate strain | "recent heart-rate strain" | mean HR over 120 s as a fraction of heart-rate **reserve** (needs the resting baseline); 0 at 50 %, 100 at 90 % | %HRR or %HRmax; the window; the bounds |
| Time above threshold | "time above a configured heart-rate threshold" | elapsed time ≥ 85 % of **HR max** (not of reserve) over 300 s, as a share of covered time | which basis (the strain feature uses reserve, this uses max: inconsistent on purpose until the team chooses); threshold; window |
| Maximum heart rate | an explicit input with its origin | per wearer entry (`hr_max_bpm` + `hr_max_origin`); the rehearsal uses "208 - 0.7 x age" with made-up ages | measured test or formula, per participant |
| Recovery | "recovery after a qualifying effort" | HR drop over 60 s after the last effort ≥ 85 % HR max, against an absolute 30 bpm reference; needs a complete, 70 %-covered window | qualifying effort, recovery length, relative or absolute reference |
| HR relative to movement | combined feature | mean HR / mean camera speed (speed ≥ 0.5 m/s), recent 120 s against the athlete's own first 120 s of tracked play this session | the reference window; the minimum speed; the pairing tolerance |
| Physical output change | "recent physical-output change" | mean camera speed over covered time, recent 120 s against the same early-session reference | distance/min, high-speed distance, or speed; the reference |
| Tracked sprint efforts | "tracked sprint efforts" (5.5 m/s draft threshold) | peak speed of complete detected sprints, recent 300 s against the reference window. Not a count, so lack of opportunity is never read as a drop | count or quality; how to tell "no opportunity" from "could not" |
| Baselines | per player | vest: resting HR from an accepted seated-rest recording; camera: an early-session reference window, not a separate rest baseline | the baseline procedure for each stream |
| Combined | combined vs single-source | weights 0.3 strain, 0.1 time above, 0.1 recovery, 0.3 HR per speed, 0.2 output; strain, HR per speed and output required | weights and required features (freeze before validation) |
| Excluded inputs | — | sleep, soreness, HRV, jumps, tackles | whether any becomes a defined, measured input |
| Test sprint exclusion | sprint must not feed the score | 5 s before to 90 s after each timed sprint, both streams | the exclusion length |
| Freshness | — | a stream older than 10 s makes its candidates unavailable | the freshness rule |
