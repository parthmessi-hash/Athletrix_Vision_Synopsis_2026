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
| 6 | Pilot view and display delay | pending | |
| 7 | Rehearsal, reliability check, handoff | pending | |

Design (decided in this batch):
- Study records: one append-only table `recording_study` (kinds session, wearer, round_start, round_end, sprint_start,
  sprint_end, sprint_result, self_rating, note, prediction), idempotent per client_token, corrections via supersedes.
- A prediction is written by the engine when a sprint_start is marked: as-entered at the cutoff, with the session's
  frozen fatigue profile text, the baseline values used, the exclusion intervals and input digests.
- Scores: app/core/fatigue.py, profile FATIGUE_PROFILE_PATH (none ships; FICTIONAL example in scripts/examples/pilot/).
- Package: format 8 adds study.json (a new member, so older readers would reject it); versions 1-7 still read.

Fictional only: no participant data, no real readings, no footage. Zephyr: not implemented, access unconfirmed.
