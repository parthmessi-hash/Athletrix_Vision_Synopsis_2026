# Operational prototype batch (started 2026-11-03): checklist

Resume from this file and the code if a session is interrupted; do not restart completed milestones.
Starting point: backend `e5adf26`, frontend `05c001a` (792 backend tests, 82 browser checks, demo 27 checks).

| # | Milestone | State | Where |
|---|-----------|-------|-------|
| 1 | One-command live fictional session | pending | `scripts/demo_launcher.py --session match` |
| 2 | Team review endpoint | done (tests/test_team_review.py) | `GET /api/v1/recordings/{id}/team-review` |
| 3 | Coach team view | pending | frontend `index.html` (Team review card) |
| 4 | Coach responses and lineup changes | backend done (tests/test_coach_responses.py; package format 6); UI pending | `recording_responses`, `POST .../responses` |
| 5 | Baseline and profile inspection | pending | `GET /api/v1/profiles`, `scripts/profile_tool.py`, baseline preview |
| 6 | Local video review and annotation | pending | frontend `tools/video-review.html` |
| 7 | Optional lens-distortion calibration | pending | `camera_adapter/intrinsics.py` |
| 8 | Evaluation workbench | pending | `scripts/evaluate.py` |
| 9 | End-to-end integration and failure checks | pending | `scripts/verify_operational_workflow.py` |
| 10 | Delivery, docs, check entry point | pending | `scripts/check.py`, README, PROJECT_STATUS |

Rules carried through: fictional profiles only (never enabled by default), manual performance evidence, camera identity
checked only on drawn figures, phantom distance unresolved, noise suppression off, Zephyr not implemented.
