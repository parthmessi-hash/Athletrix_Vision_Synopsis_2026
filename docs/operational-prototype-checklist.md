# Operational prototype batch (started 2026-11-03): checklist

Resume from this file and the code if a session is interrupted; do not restart completed milestones.
Starting point: backend `e5adf26`, frontend `05c001a` (792 backend tests, 82 browser checks, demo 27 checks).

| # | Milestone | State | Where |
|---|-----------|-------|-------|
| 1 | One-command live fictional session | done (speed-1 run: p08 R then K, p05 C after the tactic, p02 C after the decline) | `scripts/demo_launcher.py --session match` |
| 2 | Team review endpoint | done (tests/test_team_review.py) | `GET /api/v1/recordings/{id}/team-review` |
| 3 | Coach team view | done (harness 86/86 at that point) | frontend `index.html` (Team review card) |
| 4 | Coach responses and lineup changes | done: backend (tests/test_coach_responses.py; package format 6) and team-card responses | `recording_responses`, `POST .../responses` |
| 5 | Baseline and profile inspection | done (tests/test_profiles.py; Baselines and profiles card; harness 87/87) | `GET /api/v1/profiles`, `scripts/profile_tool.py`, baseline preview |
| 6 | Local video review and annotation | done on synthetic drawn figures (tests/video_review.test.html 3/3; real page: video, overlay, encounter lapse, hash matches Python, export loads in the backend); real footage pending | frontend `tools/video-review.html` |
| 7 | Optional lens-distortion calibration | done on synthetic geometry (tests/test_intrinsics.py; check-point max 1.04 m without vs 0.085 m with the lens model); no physical camera calibrated | `camera_adapter/intrinsics.py` |
| 8 | Evaluation workbench | done (tests/test_evaluate.py; fictional dataset only: no real reference annotations exist) | `scripts/evaluate.py` |
| 9 | End-to-end integration and failure checks | done (verify_operational_workflow.py 16/16; team-scale 20 athletes 60 s) | `scripts/verify_operational_workflow.py` |
| 10 | Delivery, docs, check entry point | done (check.py; README Operational prototype; PROJECT_STATUS checkpoint) | `scripts/check.py`, README, PROJECT_STATUS |

Rules carried through: fictional profiles only (never enabled by default), manual performance evidence, camera identity
checked only on drawn figures, phantom distance unresolved, noise suppression off, Zephyr not implemented.

Commands (all verified in this batch): `python scripts/demo_launcher.py --session match`, `python scripts/check.py [--all]`,
`python scripts/verify_operational_workflow.py`, `python scripts/evaluate.py make-fictional --out D && python scripts/evaluate.py run D/dataset.json --out R`,
`python scripts/profile_tool.py inspect FILE`, `python scripts/video_review_sample.py --out D`, `python scripts/camera_intrinsics.py synthetic|calibrate ...`.
