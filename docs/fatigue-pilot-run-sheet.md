# Fatigue pilot run sheet (field kit)

**Scope (2026-11-09):** an offline fatigue estimation engine. The product output is the **combined** estimate for the
vest wearer (Fatigue estimate card); wearable-only and camera-only are research comparisons. No substitution, ranking,
tactical or performance-grade features. Method: `docs/fatigue-method.md`.

One vest, one fixed camera, one offline laptop. Every command here was run in a FICTIONAL rehearsal (see
`docs/fatigue-pilot-checklist.md`). Nothing here shows field accuracy or fatigue detection. The pilot gives no
substitution or safety advice; the coach decides.

## Routes: pick one, follow it, stop where it says

**O** = operator (laptop), **T** = independent timer (never sees scores). Commands run in the backend folder.

### Route A: fictional demo, today (no hardware, no participant)
0. Fastest software check (no browser, about 2 minutes, simulated clock):
   `.venv/bin/python scripts/pilot_rehearsal.py --mode accelerated --out demo-fast`, then
   `.venv/bin/python scripts/session_verify.py --package demo-fast/pilot-package.zip --out demo-fast-verify --manifest scripts/examples/pilot/fictional_study_manifest.json --expect-rounds P1:3,P2:3,P3:3`
   and open demo-fast-verify/report/report.md ("Result at a glance", "Which methods could score each round").
1. O: `.venv/bin/python scripts/demo_launcher.py --keep-output demo-output` (the fatigue workflow is the default
   session), open the printed address, click **Live Demo**; the **Fatigue estimate** card is the first engine card
   (section 0 below).
2. O: `.venv/bin/python scripts/session_verify.py --package demo-output/pilot-package.zip --out demo-verify`
- Success: the launcher ends with "reproduced in a fresh database: True"; the verification prints VERDICT COMPLETE
  with 9 rounds, 7 eligible, and the deliberately missed save (P2 round 1) and invalid round (P1 round 3) listed as
  excluded with their reasons (checked 2026-11-09 on a launcher export).
- Stop and investigate if: the card says "could not be read" for more than a few seconds (engine not running), or the
  verification shows a prediction that does not reproduce.
- Optional display check (you at the laptop, Chrome in front): `.venv/bin/python scripts/display_check.py --out display-1`
  (section 5d). Only a COMPLETE result counts as a display measurement.

### Route B: camera bench check (permitted camera position or permitted clip; no participant, no wearable)
1. O: `.venv/bin/python scripts/camera_field_check.py template --out FIELD/`; fill in FIELD/points.json from the tape
   (at least 4 fit points and 2 separate check points) and the camera settings as they are now (zoom, focus, mount).
2. O: `.venv/bin/python scripts/camera_field_check.py fit --points FIELD/points.json --out FIELD/calibration.json`
3. O: `.venv/bin/python scripts/camera_field_check.py bench --points FIELD/points.json --calibration FIELD/calibration.json --now-settings FIELD/now.json --max-check-error-m TOL --out FIELD/bench [--clip CLIP] [--model models/yolox_tiny.onnx --config scripts/examples/camera/video_tracking_config_tiny.json]`
- Success: three separate answers: **source** PASS (the clip is readable at the calibrated resolution), **geometry**
  PASS (every check point within the team's tolerance TOL, camera unchanged), **tracking** EXERCISED only if a clip
  with people was given. A board or an empty field gives geometry only: "NOT EXERCISED" or "NO PERSON FOUND" for
  tracking is expected and is not a failure of the geometry.
- Stop and investigate if: geometry FAIL names a check point (re-measure that point or the fit points) or a changed
  setting (recalibrate); source FAIL (wrong resolution or unreadable file).
- To see what a failure looks like first: `.venv/bin/python scripts/camera_field_check.py bench-example --out bench-example`.
- Then, with a permitted clip of the course: section 2b (tracking, assignment, interval timing).

### Route C: controlled pilot (only when hardware, permission, manifest and study choices are in place)
1. O: the checks of section 1 (field check: "controlled pilot" must say yes), the session, then each round as in
   section 2; T: the timing sheet as in section 3.
2. End of session in this order (section 3b): last samples in -> T applies the sheet -> O ends the round -> O stops.
3. O: export, then `.venv/bin/python scripts/session_verify.py --package day1.zip --out day1-verify --manifest MANIFEST.json --expect-rounds P1:3,P2:3`
- Success: VERDICT COMPLETE, or INCOMPLETE with every reason understood and written in the session notes.
- Stop and investigate if: a round shows "sprint started but no independent result" (the timer's sheet was not applied
  before Stop), the last samples end well before Stop, or a prediction does not reproduce.
- Stopping point: route C needs the approved procedure and permissions, a permitted camera setup with a passed bench
  check, an independent timing reference, the frozen profile and manifest, and a confirmed wearable (Zephyr access is
  unconfirmed). Without them, stay on routes A and B.

## 0. Rehearse first (one command, fictional)

```bash
.venv/bin/python scripts/demo_launcher.py --session pilot --keep-output rehearsal-output
```
It starts an isolated engine and a local dashboard copy. Open the printed address and click **Live Demo**; the
**Fatigue pilot** card is at the top. A script then plays the operator:
- three fictional participants take turns in one vest;
- each score is saved and confirmed, then the sprint is started;
- times are entered on the blind timing sheet;
- the script misses one save, marks one round invalid, drops the strap, loses the camera before a save, and restarts
  the engine;
- at the end it exports, reproduces the saved predictions in a fresh database and writes the report.

Ctrl-C stops only what it started.

## 1. Before the session (operator)

1. Charge the laptop. Copy the person-detector model (`scripts/fetch_person_model.py`) **before going offline** if
   video will be processed; without it, video processing stops with a clear message and never downloads.
2. Start the engine with the team's **frozen** profile, and serve the dashboard locally:
   - engine: `FATIGUE_PROFILE_PATH=/path/frozen_profile.json .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1`
   - dashboard: `python3 -m http.server 3000 --bind 127.0.0.1`, run in the Athletrix_Web folder.
3. Record each participant's seated-rest baseline and accept it. Note the baseline ids.
4. Fill in `scripts/examples/pilot/study_manifest_template.json` (profile hash, baseline procedure, timing policy and
   methods, camera calibration and clock alignment, valid-round rule, analysis) **before** any outcome is collected.
5. Start the recording: `python scripts/sideline_session.py start PLAN.json`.
6. Field check: `python scripts/sideline_session.py field-check --dashboard http://localhost:3000/index.html --export-dir EXPORTS --plan PLAN.json --manifest MANIFEST.json`.
   Read the three answers separately: **fictional rehearsal / record inputs / controlled pilot**. "Record inputs" needs a
   real (non-simulated) stream. "Controlled pilot" needs a non-fictional, approved manifest, a usable calibration, a
   stated clock alignment, an active wearer, and both streams arriving fresh from real sources.
7. Open the study session (this freezes the profile **and records the study manifest's fingerprint**):
   `python scripts/pilot_session.py session --label day1 --protocol controlled_study --camera-alignment operator_estimate:0.04 --study-manifest MANIFEST.json`.
   The engine refuses it if the manifest names a different profile from the one it would freeze. Without
   `--study-manifest` the report can only say the freeze timing is SELF-DECLARED.

## 2. Each round (operator), one vest at a time

| Step | Dashboard (Fatigue pilot card) | Or type (re-running is safe) |
|---|---|---|
| Put the vest on | — | `pilot_session.py wearer px1 --code P1 --device VEST --baseline BL --hr-max 201 --hr-max-origin "measured test"` |
| Wait for **READY TO START** | the **Next** line at the top of the box | `pilot_session.py ready px1` (exit 0 = ready) |
| Start the round | **Start round** | `pilot_session.py round-start px1 --round 1` |
| **1. Save the score** | enter the round, press **1. Save score**, read "SAVED … all three estimates available: yes/NO" | `pilot_session.py save-score px1 --round 1` |
| **2. Start the sprint** (only after step 1 is confirmed) | press **2. Start timed sprint**, then give the start signal | `pilot_session.py sprint-start px1 --round 1 --confirm PREDICTION_ID` |
| Sprint over | — | `pilot_session.py sprint-end px1 --round 1`, then `round-end px1 --round 1` (or `--invalid "false start"`) |

- The **Next** line names one of: NO STREAM (nothing arriving), NOT USABLE (arriving but stale, from another device,
  still tagged for the previous wearer, or camera identity not confirmed), READY TO START, WARMING UP (round open,
  reference or windows still building, with seconds left), READY TO SAVE (with the estimates that will be available).
- A round start is **refused** while the streams are not usable. To record the round anyway, type the reason (box next
  to **Start round**, or `round-start ... --partial "strap being refitted"`). The reason goes into the report; such a
  start never opens the camera reference, and the reference is never moved to make a score appear.
- The first round started with both streams usable opens the athlete's camera reference period (the card and CLI say
  "camera reference OPENED"). Right after a sprint, Next says how many seconds of the sprint exclusion would fall inside
  that period; waiting that long gives a fuller reference.
- If the save says "NO" for some estimate, the reason is on the card (for example "wearable stream: newest sample
  20 s old"). Fix it if possible and save again. The round stays recorded either way.
- If you forget to save, start the sprint anyway: the round is kept but labelled ORDER UNVERIFIED, and it is not in the
  primary comparison.
- If the wearer or round changes after a save, the card refuses the start; save again.
- Never type a time for "save score". A sprint marker stated more than 2 s in the past is labelled retrospective.

## 2b. Camera-only field check (before the first camera session; camera only, no participant)

Only with a permitted camera position and a permitted clip. Every step below was run on the SYNTHETIC geometry
(`python scripts/camera_field_check.py synthetic --samples SAMPLES --anchor-utc 2026-11-08T10:00:00Z --out DIR`).

1. **Points.** `python scripts/camera_field_check.py template --out FIELD/`. On the field, with a tape: mark at least 4
   fit points spread over the area you will use (no 3 of any 4 in a line) and **at least 2 more check points** inside
   it. In one frame of the fixed camera, read each mark's pixel position. Enter both in `FIELD/points.json`, with the
   camera settings (camera id, zoom, focus, mount). Do not move or zoom the camera after this.
2. **Course.** In `FIELD/course.json`: the axis the course runs along, the lane (the other coordinate's range), and the
   timing marks in metres, measured from the same origin.
3. **Clip.** Record a short permitted clip of the course being run. Then:
   `python scripts/camera_track_video.py CLIP --model models/yolox_s.onnx --out OBS.jsonl --log TRACK.json --gallery TRACKS.png --assignments-template ASSIGN.json`.
   Look at the gallery, fill in ASSIGN.json for the runner (identity "confirmed" only when you are sure; "uncertain" is
   kept for audit and never produces samples), then
   `python scripts/camera_to_samples.py export OBS.jsonl --calibration FIELD/report/calibration.json --config ADAPTER.json --assignments ASSIGN.json --anchor-utc ISO --out samples.jsonl --log adapter.json`
   (ADAPTER.json: start from `scripts/examples/camera/adapter_config_example.json` and set `"source": "camera"`; the
   calibration file is the one fitted in route B step 2: `camera_field_check.py fit --points FIELD/points.json --out FIELD/calibration.json`).
4. **Check.** `python scripts/camera_field_check.py run --points FIELD/points.json --calibration FIELD/calibration.json --course FIELD/course.json --samples samples.jsonl --athlete ID --out FIELD/report --now-settings NOW.json`
   (NOW.json: the camera settings as they are at that moment). Read, in this order:
   - each **check point's error in metres** (the only independent accuracy figure, and only at those points);
   - the **usable field region** (outside the measured marks, positions are extrapolated and left out);
   - **compatibility now**: exit 1 means the camera changed (zoom, resolution, camera id, mount); recalibrate, nothing
     is timed;
   - the **share of the course tracked**, and each interval's **average speed** with its time uncertainty, or the reason
     it was excluded (a tracking gap inside it, samples too far apart at a mark, no crossing).
5. Average interval speed and peak speed are separate. **Peak-speed accuracy is not measured** without an independent
   peak reference: on the synthetic course the camera's peak read 4.19 m/s against a true 4.00 m/s while the interval
   average was within 0.6 %.

## 3. Timing (independent timer, who never sees the scores)

1. Operator: `python scripts/sprint_timing.py sheet --out times.csv`. This round list has no scores.
2. Timer, for each round, fills in:
   - the **timing gate** time and the gate's stated precision; or
   - for **video**, the presentation timestamps of the start and finish frames read from the file
     (`ffprobe -select_streams v -show_entries frame=pts_time -of csv CLIP`), never frame number divided by a nominal
     frame rate, plus their uncertainty (half the actual interval to the neighbouring frames plus judgement).
   - For an invalid attempt, give its reason in `invalid_reason`; for a missing one, `missing_reason`.
   - If the gate logs its own start time and you checked its clock against the laptop, add `gate_start_device_time`,
     `gate_clock_offset_s` and `gate_clock_uncertainty_s`. Only then can a round count as "saved before the recorded
     start".
3. `python scripts/sprint_timing.py validate times.csv --video-dir CLIPS` shows new, unchanged, correction, duplicate,
   ambiguous and invalid rows; nothing is stored.
4. `python scripts/sprint_timing.py apply times.csv --yes --video-dir CLIPS`. To change a stored time, edit the row and
   add `correction_reason`; the original stays in the history. A clip that is missing or unreadable stays missing.
   Clips stay on this laptop; only their checksums are recorded.

## 3b. Ending the session (order matters; verified in the code 2026-11-09)

What the engine does at Stop:
- A reading that arrives **after** Stop is accepted by the live view but **not stored in the recording**, even if it was
  measured before Stop. So wait until the last expected samples have arrived (the card's freshness shows them).
- A study entry (result, rating, note, round end) after Stop is **refused** unless dated inside the recording, and
  `sprint_timing.py apply` refuses a stopped recording as a whole (nothing stored, nothing back-dated). A stopped
  recording is never reopened.

Order:
1. O: after the last sprint, wait until the card shows both streams' newest samples after the sprint end.
2. T: apply the timing sheet (`sprint_timing.py validate`, then `apply`); O confirms "stored N result(s)".
3. O: `round-end` for the last round (or `--invalid "reason"`).
4. O: Stop (`sideline_session.py stop` or the dashboard).
5. O: export and verify (section 4). A result that arrives too late cannot be added: keep the timer's sheet with the
   export and let the verification list the round as missing.

## 4. After the session

1. O: export: `curl -o day1.zip http://127.0.0.1:8000/api/v1/recordings/REC_ID/package.zip` (or the dashboard). Keep the
   export outside any launcher temporary folder.
2. O: `.venv/bin/python scripts/session_verify.py --package day1.zip --out day1-verify --manifest MANIFEST.json --expect-rounds P1:3,P2:3`
   It copies the export into day1-verify/export/ (checksum checked; the original is not touched) and saves, step by
   step: which recording and frozen profile were exported; each participant's last sample against Stop; every round
   with its prediction, independent result or missing/invalid reason, timing evidence and eligibility or exclusion
   reason; reproduction of every saved prediction on a fresh copy; and the report (day1-verify/report/report.md).
   VERDICT: COMPLETE, INCOMPLETE (each reason listed) or STEP FAILED (earlier files kept; rerun with
   `session_verify.py --resume day1-verify`).
   Read the report's first lines: the result (INCOMPLETE / NEGATIVE / ...), coverage per method on the same rounds, and
   **Freeze: RECORDED** (manifest recorded at session setup before the first outcome) or **SELF-DECLARED, NOT VERIFIED**.
3. Removal (`scripts/local_data.py remove-athlete ID --execute --confirm ID`) deletes this laptop's copy only. Packages
   already exported are not touched.

## 5. What the report's timing labels mean

| Label | Meaning | Primary comparison? |
|---|---|---|
| saved_before_recorded_start | the score was saved, then an independently recorded start (gate or video, with a recorded clock mapping) came later by more than the mapping's uncertainty | yes |
| order_unverified / confirmed_sequence | saved and confirmed before the operator's start marker, but no independent start reference | only under policy `confirmed_sequence` |
| order_unverified / marker_only, no_mapping, within_uncertainty, no_start | the order cannot be established | no (exploratory figure only) |
| retrospective | backdated marker, or the recorded start came before the save | no |

## 5b. Example from the FICTIONAL operator rehearsal (2026-11-08)

| | Round 1 (confirmed) | Round 2 (save missed) |
|---|---|---|
| Before the sprint | operator pressed **Save score** at 04:22:20.996 UTC: Combined 13, Vest only 26, Camera only 4 (all three available) | no save; the score was calculated when the start marker arrived (04:23:28.072): Combined 19.29, Vest 25.41, Camera 5.96 |
| Start | **Start timed sprint** confirming that save (04:22:32.438); the gate's own start time 04:22:33.600 with a recorded clock offset of −0.2 s ± 0.05 s | start marker only, no gate start time |
| Entered afterwards (timer, blind) | 3.18 s by timing gates | 3.27 s by timing gates |
| In the report | `saved_before_recorded_start / timing_gate`: primary comparison; increase 0.0 % (best valid time) | `order_unverified / marker_only`: listed under "Rounds left out of the primary comparison"; increase 2.83 % is shown in the trace only |

Tip from the rehearsal: start a round only when the card shows both streams fresh. Since 2026-11-08 the engine refuses
a start without usable data unless the operator gives a reason (see section 2).

## 5c. Readiness (2026-11-08, one laptop)

| | What ran | Conditions | Not shown |
|---|---|---|---|
| **Runs today with fictional inputs** | dashboard and fictional demo (`demo_launcher.py --session pilot`), round readiness and refused/partial starts, save-then-start, blind timing sheet, export, reproduction in a fresh database, report; run from clean snapshots with the scripts' outbound requests sent to a dead proxy (nothing failed) and no detector model on disk | fictional heart rate, synthetic tracking, isolated engine; the browser itself was online and its only remote request was the Google Fonts stylesheet (without it, system fonts) | anything about real people; a laptop with networking switched off |
| **Passed through the actual video software path** | decoding, YOLOX-S person detection, ByteTrack, calibration, samples, paced playback into an isolated pilot session (`scripts/video_pilot_check.py`): attached to the intended wearer, early round start refused, camera reference opened at the recorded start, a deliberate 1 s picture loss left a gap (no invented samples), an identity lapse or an "uncertain" identity removed that player's samples; camera-only field check (`scripts/camera_field_check.py synthetic`) | a GENERATED 20 s clip of drawn figures, 1280x720 at 25 fps; identities from a simulated operator; with the baseline YOLOX-S setting this laptop ran at 9-11 frames/s and paced playback dropped 54-63 % of frames; the `field-rehearsal` setting (YOLOX-Tiny, every other frame) dropped none (section 5d) | real footage, real kits, lighting, crowds; live camera capture; camera-to-engine latency; physical position accuracy |
| **Needs an approved field setup and an independent timing reference** | positional accuracy on the field (measured check points), interval timing against gates or measured video timing, peak speed accuracy, coverage with real players | permission, a fixed camera position, tape measurements | — |
| **Waits on Zephyr** | the vest stream from real hardware; until then the source label says "simulated", and nothing on screen claims a connected device | see the list below | — |

## 5d. Laptop performance checks (2026-11-08; generated data only)

Run these on the sideline laptop itself, on mains power, with other apps closed (background load changed results by
up to 3x in our measurements).

1. **Dashboard speed late in a session** (LONG-HISTORY BENCHMARK: an exported package imported into a temporary
   database and reopened as live; not a live run):
   `.venv/bin/python scripts/pilot_view_benchmark.py --package EXPORT.zip --out bench/ [--point late --setting dashboard]`
   Reads: response median and p95, score calculations per poll, rows read, and time in database reads, selection,
   fingerprints and readiness, early / middle / late in the session.
2. **Camera setting**:
   `.venv/bin/python scripts/camera_settings_compare.py --out camera-work/settings --best tiny-every2`
   Compares YOLOX-S (640) and YOLOX-Tiny (416), every frame or every other frame, recorded and paced, on the generated
   clip. Then the integrated check with the chosen setting:
   `.venv/bin/python scripts/video_pilot_check.py --out camera-work/check --setting field-rehearsal`
   (`--setting baseline` keeps the current YOLOX-S, every-frame setting.)
3. **Display delay** (you at the laptop; about 5 minutes):
   `.venv/bin/python scripts/display_check.py --out display-1`
   Wait for "OPEN IN CHROME: http://127.0.0.1:PORT/index.html#demo", open it in Chrome, bring the window to the front
   with nothing covering it, wait until the yellow banner says "Chrome is drawing", then click **START**. Leave the
   window in front for the 3-minute recording (the banner turns green while recording, blue when done). The terminal
   prints the result; display-1/ keeps report.md, trace.json, pilot-package.zip and api_latency.json either way.
   - COMPLETE needs frames drawn for at least 90 % of the recording. Otherwise INCOMPLETE: the visible time is shown,
     and samples received while the window was dark count as unobserved and stay in the "of all samples" figure.
   - Longer run: `--duration-s 1800`. If the display might sleep, start it as
     `caffeinate -d .venv/bin/python scripts/display_check.py --out display-30 --duration-s 1800` (keeps the display
     awake only while the command runs; no setting is changed).
   - API time is measured separately (api_latency.json) and is not part of the display figure.
   - Status: tested with generated traces and a scripted browser; one real 60 s attempt on 2026-11-09 was INCOMPLETE
     (Chrome drew for 19.8 of 60 s; 18 of 54 samples measured, all within 2 s). No COMPLETE display measurement exists yet.

**Camera setting for a permitted field rehearsal: `field-rehearsal` (YOLOX-Tiny 416, every other frame).** On this
laptop it was the only setting that kept up with a 25 fps feed with headroom (0 frames dropped, lag p95 0.04 s); the
cost is coarser positions on the generated clip (median about 7 cm instead of 1.5 cm for the near figure, and a
heavier tail for the far figure) and a slightly longer identity gap at one encounter. For recorded review afterwards,
keep `baseline` (YOLOX-S, every frame), which is the most precise but runs at about 10 frames/s. Neither figure says
anything about accuracy on a real field: that needs the permitted setup of section 2b and an independent reference.

When to use which: try `field-rehearsal` when samples must reach the dashboard during the session (live-style); use
recorded review (`baseline`, every frame, after the session) when positions matter more than latency. Generated-clip
evidence only: `field-rehearsal` processed 12.5 frames/s with no unplanned drops in that test; its position error was
higher at distance (far figure p95 about 0.4 m). Before using it for study data, the team must measure with the real
setup: check-point errors over the area used, tracked share of the course with real players, identity losses per
session, interval times against an independent reference, and frames dropped on the sideline laptop itself.

## 5e. Evidence for the science fair: what is shown and what is not

| Claim | Shown in software (fictional or generated inputs) | Supporting artifact or test | Still unmeasured |
|---|---|---|---|
| A score is saved before each timed sprint and can be reproduced later | yes: 57/57 and 24/24 saved predictions reproduced on fresh copies; equivalence with the original code after the speed-up | checklist "90-minute run", "Performance batch"; tests/test_study_pilot.py | nothing further in software |
| The sideline laptop can ingest a long session | 90.1-minute FICTIONAL ingest, 0 failed requests | checklist "90-minute wall-clock run" | real wearable and camera streams |
| The dashboard stays responsive late in a session | late request about 430 ms (was about 3.6 s) on a long-history benchmark | checklist "Performance batch"; scripts/pilot_view_benchmark.py | display delay on screen: no COMPLETE measurement yet |
| Rounds cannot start silently without data; sessions end with a verified record | readiness refusals, partial starts with reasons, post-session verification | tests/test_study_pilot.py; scripts/session_verify.py | an operator using it on a real session |
| The camera software path works | YOLOX + ByteTrack + calibration on a GENERATED clip; gaps and uncertain identity never invent data | camera_settings_compare.py, video_pilot_check.py outputs | person tracking and accuracy with real people and a real camera |
| Positions and interval speeds are accurate on a field | only on synthetic geometry | camera_field_check.py synthetic and bench-example | check-point errors, interval timing and peak speed on a permitted field with an independent reference |
| The fatigue estimate tracks fatigue | NOT shown: all scores so far come from fictional data | — | the controlled pilot (route C) and its analysis |
| The combined estimate tracks sprint slowdown better than single-source estimates | NOT shown: fictional runs give INCONCLUSIVE (too few rounds) by design | evaluate.py pilot "Result at a glance"; session_verify.py | an approved pilot with frozen choices (docs/fatigue-method.md section 6) |
| The experimental heart-rate-vs-movement candidate behaves better | on 8 fictional cases: a slower drill is no longer read as maximal, a same-movement rise is clearer, a hidden clock offset matters less; more "not enough data" | scripts/fatigue_method_compare.py; tests/test_fatigue_method_compare.py | anything about athletes |
| Substitution decisions | DROPPED from scope (2026-11-09): not built, not claimed | — | — |

## 5f. Method checks (fictional; no hardware)

- How the score works and what is still open: `docs/fatigue-method.md`.
- Current versus experimental heart-rate-vs-movement feature on fictional cases:
  `.venv/bin/python scripts/fatigue_method_compare.py --out method-compare`
- The experimental profile is `scripts/examples/pilot/experimental_matched_profile.json` (FICTIONAL, not the team's
  rules). To see it in the workflow, start a rehearsal with it:
  `.venv/bin/python scripts/pilot_rehearsal.py --mode accelerated --profile scripts/examples/pilot/experimental_matched_profile.json --out demo-experimental`
  It runs and every saved estimate reproduces, but with the rehearsal's short rounds its 120 s windows never fill:
  the combined estimate was "not available" in all 9 rounds (checked 2026-11-09). That is the honest answer, not a bug.

- Reference coverage (experimental profile only): `python scripts/pilot_session.py coverage` prints, for the active
  wearer, which speed bands are comparable with their own reference, the share of recent movement in those bands, what
  data was lost and why (missing heart rate, uncertain camera identity, camera gap, test sprint), and what to check next.
  The same box appears in the Fatigue estimate card. It decides nothing; with the default profile it says so.
- Challenge cases (2026-11-09) are in the same comparison; the main limitation found is lingering heart rate after an
  abrupt slowdown (docs/fatigue-method.md section 4).

## 6. Still needed before the real pilot

**Team decisions:**
- the scoring features, windows, bounds and weights (see `docs/fatigue-pilot-guide.md` section 7);
- the baseline procedure;
- what makes a round valid;
- the sprint timing reference (gates and their clock check, or video with timestamps);
- the timing policy;
- the minimum usable wearable coverage and camera tracked share;
- the analysis split and anything held out.

**Field inputs:**
- an approved procedure and permissions;
- a permitted camera position and field setup, or permitted local footage, with a measured course and check points;
- an independent timing reference;
- confirmed wearable access.

**Zephyr (preferred; purchase, licence, hardware and live access unconfirmed).** The smallest set needed:
1. a supported local interface for a third-party program, and permission to use its documentation;
2. one sample payload with normal readings, a missing value, a disconnect and a reconnect;
3. timestamp semantics (whose clock, resolution, time zone, and how it relates to receipt);
4. how disconnects, low signal and "not worn" are represented;
5. heart-rate units and update rate.

Nothing else is assumed: no live beat-to-beat R–R, no raw accelerometry, no vendor jump or impact counts.
