# Fatigue pilot run sheet (field kit)

One vest, one fixed camera, one offline laptop. Every command here was run in a FICTIONAL rehearsal (see
`docs/fatigue-pilot-checklist.md`). Nothing here shows field accuracy or fatigue detection. The pilot gives no
substitution or safety advice; the coach decides.

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
7. Open the study session (this freezes the profile):
   `python scripts/pilot_session.py session --label day1 --protocol controlled_study --camera-alignment operator_estimate:0.04`.

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
   calibration file comes from running step 4 once, or from `scripts/camera_calibrate.py fit FIELD/points.json --out CAL.json`).
4. **Check.** `python scripts/camera_field_check.py run --points FIELD/points.json --course FIELD/course.json --samples samples.jsonl --athlete ID --out FIELD/report --now-settings NOW.json`
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

## 4. After the session

1. `python scripts/sideline_session.py stop`, then export the package (dashboard, or `GET /api/v1/recordings/{id}/package.zip`).
2. Report: `python scripts/evaluate.py pilot --packages day1.zip --manifest MANIFEST.json --out report-day1`.
   Each row traces to its saved prediction, start marker, result and its corrections, invalidation and frozen profile.
   Each participant-session's best reference names its round and result.
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
3. **Display delay** (foreground browser): start the timing proxy
   `python scripts/pilot_display_delay.py proxy --engine http://127.0.0.1:PORT --port 3919 --sink display.json`,
   serve a dashboard copy whose `ATHLETRIX_CONFIG.apiBase` is `http://127.0.0.1:3919`, keep the display awake for this
   run only with `caffeinate -d -w RUN_PID`, keep the Chrome window in front and uncovered, and add a paint heartbeat in
   the page console before the run:
   `window.__beats=[];let l=0;(function f(){const n=Date.now();if(n-l>=250){__beats.push(n);l=n}requestAnimationFrame(f)})()`
   At the end, post `{"display": window.__athletrixPilotDisplay, "painted": window.__beats}` to
   `http://127.0.0.1:3919/__display_log`, then
   `python scripts/pilot_display_delay.py analyze --display display.json --package EXPORT.zip`.
   Only samples received and displayed while the page was drawing frames count; the rest are "not observable". A
   covered Chrome window draws nothing even with the display awake: on 2026-11-08 only 61 s of a 31-minute run were
   visible, so that run is not a display measurement. Someone must keep Chrome in front for the whole run.

**Camera setting for a permitted field rehearsal: `field-rehearsal` (YOLOX-Tiny 416, every other frame).** On this
laptop it was the only setting that kept up with a 25 fps feed with headroom (0 frames dropped, lag p95 0.04 s); the
cost is coarser positions on the generated clip (median about 7 cm instead of 1.5 cm for the near figure, and a
heavier tail for the far figure) and a slightly longer identity gap at one encounter. For recorded review afterwards,
keep `baseline` (YOLOX-S, every frame), which is the most precise but runs at about 10 frames/s. Neither figure says
anything about accuracy on a real field: that needs the permitted setup of section 2b and an independent reference.

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
