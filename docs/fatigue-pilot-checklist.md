# Science-fair fatigue pilot batch (started 2026-11-07): checkpoint

Resume from this file and the code after an interruption; do not restart completed milestones.
Start: backend `5951617`, frontend `bded804` (835 backend tests, harness 89/89, video 6/6). The p06 regression stays covered
(tests/test_substitution_review.py, verify_operational_workflow.py).

| # | Milestone | State | Where / verification |
|---|-----------|-------|----------------------|
| 1 | Study session workflow | done | `app/models/study.py`, `app/store/study.py`, `app/api/study.py` (POST/GET /recordings/{id}/study); retries, corrections, one active wearer, own baseline enforced; package format 8 (study.json); tests/test_study_pilot.py |
| 2 | Three explainable candidate scores | done | `app/core/fatigue.py`, `app/core/sprints.py`, `scripts/examples/pilot/fictional_fatigue_profile.json`; tests/test_fatigue_scores.py (hand-worked values) |
| 3 | Prediction cutoff and leakage protection | done | prediction saved at each sprint_start (as entered); exclusions in both streams; reproduce endpoint; leakage regression in tests/test_study_pilot.py |
| 4 | Paired analysis and report | done | `app/core/pilot_analysis.py`, `scripts/evaluate.py pilot` / `pilot-pin`; fixture with separately computed values `scripts/examples/pilot/fictional_analysis_rows.json`; tests/test_pilot_analysis.py |
| 5 | Camera verification and sprint detector | done (synthetic fixtures) | `camera_adapter/verification.py`, `scripts/camera_verify.py` (make-synthetic, report); `app/core/sprints.py`; tests/test_camera_verify.py; jumps/tackles automatic = unavailable; peak speed unmeasured without a reference |
| 6 | Pilot view and display delay | done (fictional wall-clock run, real browser) | dashboard Fatigue pilot card (harness 91/91); `GET /recordings/{id}/study/pilot`; preflight `fatigue_pilot` area (substitution review not needed); `scripts/pilot_display_delay.py` (proxy with slowed responses + analysis; tests/test_display_delay.py). Run 2026-11-08 02:01-02:09 UTC, foreground tab, 1 s polling, every 10th response delayed 2.5 s: 384 samples, 0 missing, median 0.656 s, p95 1.837 s, max 8.483 s (inside an 8.9 s display gap during a layout check), 97.4 % within 2 s, clock mapping ±0.022 s. Fixed on the way: a millisecond-resolution bug in the analysis (first result had a false 21 s) |
| 7 | Rehearsal, reliability check, handoff | done, with the 90-minute run only partly recorded (see below) | `scripts/pilot_rehearsal.py` (accelerated PASS: 9 predictions reproduced, restart, invalid round, removal on a copy); `docs/fatigue-pilot-run-sheet.md`; guide section 7 compares the draft protocol and the profile |

Design (decided in this batch):
- Study records: one append-only table `recording_study` (kinds session, wearer, round_start, round_end, sprint_start,
  sprint_end, sprint_result, self_rating, note, prediction), idempotent per client_token, corrections via supersedes.
- A prediction is written by the engine when a sprint_start is marked: as-entered at the cutoff, with the session's
  frozen fatigue profile text, the baseline values used, the exclusion intervals and input digests.
- Scores: app/core/fatigue.py, profile FATIGUE_PROFILE_PATH (none ships; FICTIONAL example in scripts/examples/pilot/).
- Package: format 8 adds study.json (a new member, so older readers would reject it); versions 1-7 still read.

Fictional only: no participant data, no real readings, no footage. Zephyr: not implemented, access unconfirmed.

## Resume batch (2026-11-08): what changed after the first pass

- Reproduction failure (tests/test_study_pilot.py "differs"): already fixed before this pass. Cause: after import, the
  participant's rest baseline is not on the new engine; reproduction substituted the values saved in the prediction but
  gave them the status "accepted (as recorded in the prediction)", which failed the profile's require_status "accepted",
  so the vest features became unavailable and the scores differed. Separate bug: same-instant study entries were ordered
  by their random id; they are now ordered by insertion (rowid), which export and import preserve (tested).
- Added: backdated sprint markers are RETROSPECTIVE-only (marker lag > 2 s) and never eligible; operators can mark a
  round invalid (reason required; its time is never the best reference); "not_reproducible" when the baseline values are
  unavailable; features and the sprint detector never bridge excluded intervals or invalid camera samples; incomplete
  sprints are counted but marked and not used for sprint quality; the analysis keeps units, frozen profiles and the
  analysis choices; `scripts/pilot_session.py` operator command; pilot card freshness, session and last saved prediction.
- Known limitation of the 90-minute run: it was started before these changes, so it ran the code at commit 578c28e plus
  the then-uncommitted rehearsal script (synthetic tracking, no video processing).

## The 90-minute wall-clock run (2026-11-08 02:13:41 to 03:44:50 UTC): what it actually covered

- **Command:** `python scripts/pilot_rehearsal.py --mode wall --duration-min 90 --out DIR`. One real engine process on a
  loopback port. FICTIONAL readings were sent at their real times: heart rate 1 Hz for the wearer, synthetic camera
  tracking 10 Hz for the wearer and two camera-only players. The script also polled the pilot view through the API once a
  second. No browser, no video processing, no camera, no wearable.
- **Ingest:** completed and exported, 91.15 minutes:
  - 3,868 heart-rate and 131,089 tracking samples, every one with a receipt;
  - 62 rounds, each with a saved prediction, sprint, result and self-rating;
  - 32 wearer turns over 3 fictional participants.
- **Crashed at the end:** the step after the export (report and summary) crashed. The running process imported the
  analysis module from disk after I had changed the study model during the run (the process still held the old model
  without `invalid`). So `summary.json` was never written, and these were lost: request failures, scheduling lateness,
  pilot-view API latency and engine memory. My mistake, not an engine failure: never edit code under a running
  rehearsal.
- **Recovered from the export** (receipt minus sample timestamp):
  - heart rate p50 0.001 s, p99 0.091 s, max 0.24 s;
  - tracking p50 0.003 s, p99 0.88 s, max 13.0 s;
  - 1,053 of 134,957 samples (0.78 %) more than 1 s late, more often in the last 40 minutes (03:02-03:44). I ran other
    work on the laptop only around 02:27-02:41, so that does not explain the later growth.
  - Probable causes, not established: the per-second pilot view with trend recalculation, and database growth.
  - Some rounds' camera coverage fell below the profile's minimum (e.g. "recent 9 of 30 s").
- **Report (current code, FICTIONAL):** 57 matched rows from 3 people. Pooled r: vest-only 0.712, camera-only 0.642,
  combined 0.706. Combined r ≥ 0.5 MET; combined above both NOT MET. r(round, slowdown) 0.52.
- **Reproduction in a fresh database with today's code:** 56 reproduced, 6 "differs". Same inputs, different scores,
  because the scoring rules changed after they were saved (no bridging, complete sprints only). Predictions now record
  a scoring version so this is named explicitly.
- **To get the missing numbers,** run the same command again without editing code during it (about 92 minutes), ideally
  with the dashboard open in a foreground tab.

## Field-kit batch (started 2026-11-08 03:50 UTC): checkpoint

| # | Milestone | State | Where |
|---|-----------|-------|-------|
| 1 | Close out the pilot | done | 90-minute run recorded above; commit bd31333 |
| 2 | Save score, then start sprint | done | `score_save` + `sprint_start --confirm` (app/api/study.py), `app/core/timing_evidence.py` (saved_before_recorded_start / order_unverified / retrospective; policy independent_start_reference or confirmed_sequence; legacy grace never promoted), RULES_VERSION in predictions, CLI `save-score`, dashboard Save / Start controls (harness 93/93), tests in tests/test_study_pilot.py |
| 3 | Independent sprint timing worksheet | done | `GET .../study/timing-sheet` (blind: no scores), `scripts/sprint_timing.py` sheet / validate / apply (preview: new, unchanged, correction, duplicate, ambiguous, invalid; video by PTS + uncertainty, local SHA-256, missing clip = missing), templates `scripts/examples/pilot/sprint_timing_template.csv` and `fictional_sprint_timing.csv`; test in tests/test_study_pilot.py |
| 4 | Study manifest and traceable report | done | `scripts/examples/pilot/study_manifest_template.json` (blank) and `fictional_study_manifest.json`; `evaluate.py pilot --manifest`; outcome renamed `time_increase_pct` (percent increase in sprint time); reference attempts vs prediction eligibility; best-reference table with result ids and corrections; trace columns; exploratory unverified block; mixed fictional/real and mixed profiles refused; fixture extended (tests/test_pilot_analysis.py); rehearsal now uses save-then-start and the blind timing sheet |
| 5 | One-laptop offline preflight | done (software); operator run pending in 6 | `sideline_session.py field-check` (three separate verdicts; fictional sessions and simulated labels never count as real), `demo_launcher.py --session pilot` (one command: isolated engine, dashboard copy, rehearsal, restart, export, fresh-database reproduction, report); dashboard's only remote asset is a Google Fonts stylesheet (offline: system fonts); missing model: camera_track_video.py exits 1 with the fetch command, never downloads |
| 6 | Operator rehearsal and handoff | done (FICTIONAL) | see "Operator rehearsal 2026-11-08" below; `docs/fatigue-pilot-run-sheet.md` (printable, with timer instructions, decisions and the Zephyr minimum) |

## Operator rehearsal 2026-11-08 04:04-04:24 UTC (FICTIONAL; one laptop; synthetic tracking, no video processing)

- **Launch:** `demo_launcher.py --session pilot --frontend-port 3920 --keep-output DIR`, one command: isolated engine,
  dashboard copy, scripted operator. Field check against it: fictional rehearsal ready = yes; record inputs = NO (no real
  source); controlled pilot = NO (fictional profile and manifest, no calibration, no real streams).
- **What the script did:** 3 wearers with their own baselines; save score, then confirmed start, every round; blind
  timing sheet; P2 round 1 save missed (order_unverified / marker_only); P1 round 3 invalid; P2 strap dropout and
  reconnect; P3 camera gap before a save; P1 round 2 gate start with a recorded mapping (saved_before_recorded_start);
  engine restart after P1 (1.2 s; the recording stayed active); export, fresh-database reproduction 9/9; removal on a
  copy (P1's entries gone, 46 others kept, the exported package still holds P1's 22). Report: 4 primary rows (small
  sample), so the targets are inconclusive.
- **Real browser, foreground tab:**
  - during the restart the card showed "could not be read" and recovered with the new wearer;
  - the dropout showed "Wearable newest 11-20 s old (STALE)" while the camera stayed fresh.
- **Display delay** (engine receipt -> first frame after a visible update; browser clock mapped onto the engine clock,
  ±0.037 s): 592 heart-rate samples, 592 displayed, 0 missing, median 0.625 s, p95 1.105 s, max 12.688 s, 98.1 % within
  2 s. The maximum falls in the 12.9 s update gap of the deliberate engine restart.
- **API timing, measured separately** (script polling once a second, 640 polls): pilot view p50 83 ms, p95 180 ms,
  max 668 ms; 1 failed poll, during the restart. Ingest scheduling lateness p50 5 ms, p95 6.5 ms, max 0.70 s.
- **Operator path by hand in the real browser** (same servers, a second recording):
  - session, wearer and round by `pilot_session.py`;
  - "1. Save score" clicked: "SAVED for P1 round 1 at 04:22:20.996Z … all three estimates available: yes · Combined 13 ·
    Vest only 26 · Camera only 4";
  - "2. Start timed sprint" clicked: marked 04:22:32.438Z, confirming the save;
  - round 2 started without a save (CLI), so it is ORDER UNVERIFIED;
  - blind timing sheet: round 1 by gate 3.18 s with the gate's own start time (04:22:33.600, offset −0.2 s ± 0.05 s),
    round 2 by gate 3.27 s without a start reference;
  - report with the FICTIONAL study manifest: round 1 is saved_before_recorded_start (gap 12.4 s), primary; round 2 is
    order_unverified / marker_only, listed and left out of the primary comparison.
  - A first attempt was started before any data arrived (the feeder had not started): the card said "no usable wearable
    sample at the cutoff". That round was marked invalid and the setup repeated with data flowing. Lesson for the run
    sheet: start the round only when both streams arrive.

## Single-laptop field-kit and camera-path batch (2026-11-08 04:40 UTC onward): checkpoint

| # | Milestone | State | Where |
|---|-----------|-------|-------|
| 1 | Repeatable, recoverable long run | code done (bb15b72); 90-minute run: see below | `scripts/pilot_rehearsal.py` writes run.json (commit, uncommitted changes, rules version, profile SHA-256, workload, command), progress.json every 30 s, then ingest / export / reproduce / analysis as separate files; `--resume DIR --steps reproduce,analysis` reruns only the late steps; test with a deliberate failure after export |
| 2 | No round starts quietly without usable data | done (bac044d, 4a5e5de; dashboard e2d7cf4) | readiness states in `app/api/study.py` (`GET .../study/readiness`, pilot view), refused round start unless `partial_reason`, `opens_camera_reference` on the entry, report section "Rounds started without usable data", `pilot_session.py ready` and `round-start --partial`, dashboard Next line and Start round (harness 94/94); tests: early start, dropout during warm-up, wearer still tagged for the previous wearer, camera identity loss, recovery |
| 3 | Actual video path, separate from synthetic ingest | done on a GENERATED clip (a117f67) | `scripts/video_pilot_check.py`; results below |
| 4 | Camera-only field check | done on SYNTHETIC geometry (a117f67) | `scripts/camera_field_check.py` template / run / synthetic; run sheet section 2b |
| 5 | Offline handoff and readiness summary | see below | run sheet section 5c |

### Actual video path (2026-11-08 05:10 UTC; `scripts/video_pilot_check.py --out camera-work/...`)

Clip: generated by `camera_adapter/synthetic_people.py`, 1280x720, 25 fps, 20 s, five drawn figures; red = fictional
wearer vx1, blue = camera-only vx2, the others never assigned. Model YOLOX-S ONNX (pinned SHA-256, local file), ByteTrack,
default `VideoTrackingConfig`; identities from a simulated operator reading the synthetic truth.

| | A. recorded, every frame in order | B. paced playback (bounded queue of 2) |
|---|---|---|
| Frames | 500 decoded, 0 decoder time mismatches | 500 released, **317 dropped**, 183 processed (longest drop run 0.24 s) |
| Processing rate | 9.3 frames/s (offline, slower than real time) | 9.0 frames/s; did not keep up with 25 fps; lag median 0.17 s, p95 0.21 s |
| Samples | red 156, blue 140; times exactly on frame times (anchor + video time) | engine received red 108, blue 96 (gaps where frames dropped) |
| Against the truth | red: tracked 94 % of the clip, position error median 0.014 m (max 0.09), speed error median 0.007 m/s; blue: 86 %, max position error 0.32 m; 0 identity errors | — |
| Identity | gaps at encounters: red 5.44-6.48 s, blue 3.40-4.52 and 5.48-7.40 s (assignments lapse and are re-confirmed) | same rule |
| Identity lapse without re-confirmation | red's samples stop at 5.44 s (43 instead of 156), blue's at 3.40 s: the score loses that evidence, nothing is guessed | — |
| Ingest | — | capture-to-ingest median 0.020 s, p95 0.026 s (send pacing + HTTP + receipt; processing ran before sending, so this is NOT camera latency) |
| Pilot session | — | round start before the camera arrived: refused (409, stale_or_unassigned); then ready_to_start -> started, opened the camera reference; warming_up -> ready_to_save; after the clip ended the camera went stale (readiness says so, round open) |
| Saved score | — | vest only 0, camera only 100, combined NOT available: too few heart-rate/speed pairs in the recent window (red is resting, below 0.5 m/s); session clock reference operator_estimate ± 0.04 s retained |

Profile: a FICTIONAL 6-second-window variant written into the output folder (not the team's rules). Not shown: real
footage, real kits, lighting, crowds, a live camera, camera-to-engine latency. Most important finding: at 1280x720 the
detector runs at about 9 frames/s on this laptop, so a live 25 fps feed would lose about two thirds of its frames; use
`--every-nth` or a lower resolution and measure again before a field session.

### Camera-only field check on synthetic geometry (`scripts/camera_field_check.py synthetic`)

Samples from path A above; course: red's run along y = 3 m, marks at 6, 12 and 18 m.
- Check points (not used in the fit): 0.018, 0.058, 0.029 and 0.046 m. Usable region: x 0-24 m, y 0-14 m.
- Course tracked: 2.27 s of 2.96 s on the course (77 %); all three marks crossed.
- 6 -> 12 m: 1.595 s (truth 1.586 s), average 3.762 m/s (truth 3.783 m/s); peak (camera estimate) 4.19 m/s against a true
  4.00 m/s: the peak is about 5 % high while the average is within 0.6 %. On the field, peak accuracy stays NOT
  ESTABLISHED without an independent reference.
- 12 -> 18 m: excluded, "tracking gap of 1.04 s inside the interval" (red's encounter with blue).
- A zoom change and a resolution change after calibration are both refused (exit 1, nothing timed).

### Offline handoff from clean snapshots (2026-11-08 05:22-05:38 UTC)

Snapshots: backend a117f67 and frontend e2d7cf4 as detached worktrees (no models folder, no local data).
- `demo_launcher.py --session pilot --frontend SNAPSHOT --keep-output DIR --exit-after-demo`, with HTTP(S)_PROXY set to a
  dead local port (outbound requests from the scripts would fail): exit 0; 9 rounds; every saved prediction reproduced in
  a fresh database; 1 failed pilot-view poll (during the deliberate engine restart); pilot view API p50 98 ms, p95 197
  ms, max 389 ms. Report: P1 round 3 invalid and P2 round 1 order_unverified listed and left out; camera reference opened
  at each participant's round 1; no round started without usable data.
- The browser tab showed the Next line live (WARMING UP with seconds left, READY TO SAVE). It exposed one problem,
  fixed in 4a5e5de: READY TO SAVE listed the missing estimates by name only ("not available: vest_only, camera_only,
  combined"); it now gives each one's reason.
- Remote requests from the page: the dashboard's own two local ports and fonts.googleapis.com only.
- Missing model (the snapshot has none): `video_pilot_check.py` and `camera_track_video.py` both exit 1 with the copy
  instruction; nothing is downloaded or written, no models folder is created. The launcher has no model-fetch code.
- By hand in the real browser (snapshot engine 4a5e5de, fictional profile, fictional feeder): session and wearer by
  `pilot_session.py`; `ready` said NO_STREAM (exit 1); **Start round** with no data: "Round NOT started" (nothing stored);
  feeder on: READY TO START; **Start round**: "camera reference OPENED by this start"; WARMING UP about 57 s; READY TO
  SAVE; **1. Save score**: all three available (combined 1, vest only 3, camera only 0); **2. Start timed sprint**
  confirmed that save; `sprint_timing.py sheet` (no score columns), validate, apply 3.24 s by gates; stop; export;
  `evaluate.py pilot`: the row traces to its prediction and start, order_unverified / confirmed_sequence (no
  independent start reference). Wording fixed afterwards on the dashboard: the refusal no longer shows the engine's
  field name "partial_reason".
- Source label vs hardware: every stream in these runs was labelled simulated; the session setup card and field check
  report sources by label only and never claim a connected device.

### The 90-minute wall-clock run from frozen snapshots (2026-11-08 05:38:13 to 07:08:21 UTC)

Command (from a detached worktree of backend 4a5e5de, no uncommitted changes; dashboard copy of frontend e2d7cf4 served on
3918 and pointed at the display-timing proxy on 3919):
`.venv/bin/python SNAPSHOT/scripts/pilot_rehearsal.py --mode wall --duration-min 90 --out OUT --port 3930 --hold --cors-origin http://127.0.0.1:3918`
with `scripts/pilot_display_delay.py proxy --engine http://127.0.0.1:3930 --port 3919 --sink display.json`.
run.json: rules version 2, profile fictional-rehearsal-short-windows-1 (SHA-256 18f0fef87471…). Workload: FICTIONAL heart
rate 1 Hz, SYNTHETIC camera tracking 10 Hz for the wearer and two camera-only players; no video decoding, detection or
tracking; no wearable; no person. Separate artifacts: run, progress, ingest, export, reproduce, analysis, summary.

| | Result |
|---|---|
| Wall time of ingest | 90.1 min; 57 rounds (3 participants in turn), each started only after the readiness said ready |
| Inputs | 3,414 heart-rate and 119,203 tracking samples sent and stored (export counts match); 0 failed requests of any kind |
| Lateness of the sender against its schedule | p50 0.005 s, p95 1.98 s, max 25.6 s (38,948 steps) |
| Pilot view API (script polling once a second, 2,820 polls) | p50 0.43 s, p95 1.79 s, max 5.94 s |
| Export | 57 saved predictions, package SHA-256 a8fc471c… |
| Reproduction on a second database, same code | 57 of 57 reproduced. Deliberately unavailable when saved (stays unavailable, not a failure): vest only 4, camera only 5, combined 6 |
| Report | written; no round started without usable data; camera reference opened at each participant's round 1 |
| Engine memory | 69 MB at start, max 134 MB |
| Display delay (foreground browser) | **UNMEASURED**: the display slept (2-minute display sleep) and Chrome painted frames for only about 5 of the 90 minutes (174 updates; gaps of 29, 5 and 61 minutes). The analysis tool's figure (median 1,159 s) measures the time without painting, not a display delay. |

Interruptions and machine state: battery 43 % and Low Power Mode at 06:14 (charging and Low Power Mode off by 06:35); an
unrelated `caffeinate -i -t 300` re-created every few minutes; the operator paused the session work at about 06:25 (the run
itself kept going); only documentation and one frontend wording edit during the run.

**Most important issue: the pilot view gets slower as the session gets longer.** Measured on this run's export at its end
(P1, 1,288 heart-rate and 15,539 tracking samples since the first round start): loading 85 ms, one score 187 ms, readiness
112 ms, the pilot view 1.9 s with a 120 s trend and 3.5 s with the dashboard's 300 s trend. Every trend point re-scores
all samples since the athlete's first round start, so the cost grows with session length; the engine process sat at about
one full core and the sender fell behind (the same pattern explains the late samples of the first 90-minute run). The
readiness block added this batch is about 5 % of it. A fix (load only the camera reference window and the recent lookback,
or cache past trend points) changes what a saved prediction's input digest covers, so it needs a rules-version change
and a decision; until then, a shorter dashboard trend span reduces the load.

## Resume of the batch (2026-11-08, afternoon): what was added

- **Readiness regression and mid-round loss** (tests/test_study_pilot.py,
  `test_sprint_exclusion_keeps_streams_usable_but_out_of_the_score_and_a_lost_stream_can_be_saved_or_invalidated`):
  readings arriving during a timed sprint's exclusion window keep both streams "fresh" and the state ready_to_start, while
  the same readings stay excluded from the score (test_effort exclusions counted; the score's own freshness stale).
  Verified to fail without the fix (it then reported the camera "stale" while it was sending). A strap lost mid-round:
  readiness says round 2 is open and offers "save anyway" or "end the round as invalid with a reason"; the incomplete
  save keeps its reasons; the invalid reason reaches the report row with the saved prediction id. One reason was also
  tightened: "readings from before this wearer's assignment" now counts only the last 30 s, not the whole session.
- **Video path, extended** (`scripts/video_pilot_check.py`, outputs in camera-work/, ignored by git): a deliberate 1 s
  picture loss (frames 250-274 replaced by plain field colour, frame count and times unchanged); an "uncertain" identity
  variant; snapshots of the pilot view (what the dashboard card renders) at the end of the clip and 4 s after.

| | Run 1 (09:08 UTC, dashboard watching) | Run 2 (09:13 UTC) |
|---|---|---|
| A. recorded, every frame | 500 frames, 11.3 frames/s, 0 decoder time mismatches, sample times on frame times | 11.6 frames/s, same results |
| Against the truth | red (vx1) 145 samples, 89 % of the clip, position error median 0.016 m, 0 identity errors; blue (vx2) 128, 80 %, 0 identity errors | same |
| Picture loss 10.0-11.0 s | 0 observations, 0 samples inside; next samples at 11.4 s (new tracks + estimator warm-up) | same |
| Identity lapse (no re-confirmation after an encounter) | — | red 0 samples in 6.43-10.0 s (30 with re-confirmation); blue 0 in 4.47-10.0 s (30) |
| Identity "uncertain" after red's first assignment | red 43 samples, 0 after the first encounter: nothing for a camera or combined score | same |
| B. paced playback | 270 of 500 frames dropped, 230 processed, 11.4 frames/s; engine got vx1 112, vx2 99 samples, all on the intended athlete ids | 274 dropped, 11.2 frames/s; vx1 105, vx2 93 |
| Pilot session | early start refused (409); round start opened the camera reference at its recorded start; pilot view at the end of the clip: wearer vx1, camera fresh (0.03 s), camera only 100, combined not available ("too few heart-rate / speed pairs": red rests below 0.5 m/s); 4 s later: camera stale with the reason, camera only not available | same states |

  The real browser (dashboard f2faf7f pointed at the check's engine) updated only about once a minute because Chrome
  throttles a window that is not being painted (display asleep), so it never showed the fresh 20 s; it did show the end
  state correctly (wearer, camera STALE with its age, camera and combined not available with reasons, the saved
  prediction). The pilot-view snapshots above are the evidence for the live part. Processing was faster than in the first
  run (9 frames/s) because the laptop was on mains power with Low Power Mode off.
- **Camera-only run sheet** (section 2b) now has the exact steps: points with check points, course file, clip,
  tracking, assignment, export, the check and how to read it; verified on the synthetic geometry.

## Checkpoint for the next session (2026-11-08, after the resume)

Committed and pushed: backend bb15b72, bac044d, a117f67, 4a5e5de, 827287a and the resume commit after it; frontend
e2d7cf4, f2faf7f. The 90-minute run is finished (artifacts were in the session scratchpad; results recorded above).
Do not repeat: the snapshot handoff, the 90-minute run, the video path check, the synthetic field check.
Next, in order:
1. Decide and implement the pilot-view scaling fix (see "Most important issue" above; rules version 3, digest coverage
   stated), then a shorter wall run (about 30 minutes) on mains power with the display kept awake, to measure display
   delay and to watch the video path live in the browser.
2. Measure the detector at lower resolution or with `--every-nth 2` (9-11 frames/s at 1280x720 drops about half to two
   thirds of a 25 fps feed).
3. On a permitted field: run sheet section 2b, with an independent timing reference.
