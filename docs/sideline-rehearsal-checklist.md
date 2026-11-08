# Sideline rehearsal batch (started 2026-11-07): checkpoint

Resume from this file and the code after an interruption; do not restart completed milestones.
Starting point: backend `b1ea01c`, frontend `26a7e04` (808 backend tests, harness 87/87, operational verifier 16/16).

| # | Milestone | State | Where / verification |
|---|-----------|-------|----------------------|
| 1 | Missing-evidence decision semantics | done | decision rules 2 in `app/core/substitution.py` (keep needs every required input; unconfirmed reading -> review later; explicit performance-only route needs `routes.performance_only` AND roster `inputs.physiology = not_used`; context-only observation). Tests: `tests/test_substitution_review.py`, `tests/test_evidence_quality.py`; verifier p06 check; before/after: old code `keep`, new `insufficient_evidence` on the same inputs |
| 2 | Evidence quality view | done | `app/core/evidence.py`, team review `evidence`, `GET /recordings/{id}/evidence`; retries keep first receipt (`duplicate_arrivals`), replacements counted, rejected writes logged (`ingest_rejections`); team card evidence line + technical details; harness 88/88 |
| 3 | Session setup and preflight | done | `app/models/session.py`, `POST/GET /api/v1/preflight` (three readiness areas), `scripts/sideline_session.py` (preflight/start/status/stop/inspect, resumable), dashboard Session setup card (harness 89/89), `scripts/verify_session_workflow.py` 14/14 (startup failure, kill/restart, clean shutdown); templates in `scripts/examples/session/` |
| 4 | Calibration and clock alignment workflow | done on synthetic inputs | `camera_adapter/setup.py` + `scripts/camera_setup.py` (checklist, inspect, compatible: resolution/camera/settings invalidation), calibration records `camera_settings`; `app/core/alignment.py` + `scripts/time_align.py` (bases, offset/drift, leave-one-out uncertainty, mismatched events flagged, replay anchor, cross-stream availability). Tests: `tests/test_session_setup.py`. Physical camera and real clocks untested |
| 5 | Identity confirmation queue | done on synthetic fixtures and the drawn-figure clip | frontend `tools/video-review.js` queue (track start / possible restart / after encounter hold / after gap; confirm, uncertain, leave unassigned; summary in `operator_work.confirmation_tasks`), `tests/video_review.test.html` 5/5; backend `apply_assignments` lapses after a gap > 1 s and reports `usable_intervals`; the pipeline starts a new segment at each separately confirmed interval. Real-browser: drawn clip gave 16 tasks; found and fixed a hold split by one missed detection. Human operator time NOT measured (no person in this batch) |
| 6 | Annotation and reviewer workflow | done (fictional reviewers) | `app/core/annotation.py`, `scripts/annotate.py` (blank, validate, compare, adjudicate, import), examples in `scripts/examples/annotation/` (completed pass judged poor, failed pass judged reasonable, role-specific positioning, off-camera unable, instruction with exception); performance evidence gains `confidence`, `rationale`, `instruction_exception`; blind Annotation sheet panel in `tools/video-review.html` (refuses a copy holding another reviewer's labels). Tests: `tests/test_annotation.py`, video_review 6/6 |
| 7 | Evaluation pilot rehearsal | pending | |
| 8 | Failure recovery and saved evidence | pending | |
| 9 | Rehearsal guide and decision checklist | pending | |

Open defects: none known. Fixed on the way: `compute_index` always appended the "available signals carry x%" reason (a for/else), even when the index was available.

Verified commands: `python scripts/verify_operational_workflow.py` (18/18), `python scripts/verify_session_workflow.py` (14/14), `.venv/bin/python -m pytest -q` (827 passed).
