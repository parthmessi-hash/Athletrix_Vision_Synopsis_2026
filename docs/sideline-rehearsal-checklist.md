# Sideline rehearsal batch (started 2026-11-07): checkpoint

Resume from this file and the code after an interruption; do not restart completed milestones.
Starting point: backend `b1ea01c`, frontend `26a7e04` (808 backend tests, harness 87/87, operational verifier 16/16).

| # | Milestone | State | Where / verification |
|---|-----------|-------|----------------------|
| 1 | Missing-evidence decision semantics | done | decision rules 2 in `app/core/substitution.py` (keep needs every required input; unconfirmed reading -> review later; explicit performance-only route needs `routes.performance_only` AND roster `inputs.physiology = not_used`; context-only observation). Tests: `tests/test_substitution_review.py`, `tests/test_evidence_quality.py`; verifier p06 check; before/after: old code `keep`, new `insufficient_evidence` on the same inputs |
| 2 | Evidence quality view | done | `app/core/evidence.py`, team review `evidence`, `GET /recordings/{id}/evidence`; retries keep first receipt (`duplicate_arrivals`), replacements counted, rejected writes logged (`ingest_rejections`); team card evidence line + technical details; harness 88/88 |
| 3 | Session setup and preflight | pending | |
| 4 | Calibration and clock alignment workflow | pending | |
| 5 | Identity confirmation queue | pending | |
| 6 | Annotation and reviewer workflow | pending | |
| 7 | Evaluation pilot rehearsal | pending | |
| 8 | Failure recovery and saved evidence | pending | |
| 9 | Rehearsal guide and decision checklist | pending | |

Open defects: none known. Fixed on the way: `compute_index` always appended the "available signals carry x%" reason (a for/else), even when the index was available.

Verified commands: `python scripts/verify_operational_workflow.py` (18/18), `.venv/bin/python -m pytest -q` (820 passed).
