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
| 5 | One-laptop offline preflight | pending | |
| 6 | Operator rehearsal and handoff | pending | |
