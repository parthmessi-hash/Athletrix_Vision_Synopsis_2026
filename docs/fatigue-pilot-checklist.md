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
| 7 | Rehearsal, reliability check, handoff | rehearsal done; 90-min run in progress (started 02:13:39 UTC 2026-11-08, out in the scratch folder `rel90/`) | `scripts/pilot_rehearsal.py` (accelerated PASS: 9 predictions reproduced, restart, invalid round, removal on a copy); `docs/fatigue-pilot-run-sheet.md`; guide section 7 compares the draft protocol and the profile |

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
