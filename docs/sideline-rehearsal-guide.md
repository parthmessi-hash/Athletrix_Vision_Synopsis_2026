# Sideline rehearsal guide (2026-11-07)

> **Superseded (2026-11-09):** Athletrix is now a fatigue estimation engine only. Substitution recommendations, bench rankings, tactical evaluations and performance grades are dropped (see `PROJECT_STATUS.md`, Scope). This file is kept as history.


A rehearsal runs the whole local workflow with FICTIONAL athletes, readings, profiles, reviewers and coach decisions.
It shows that the software behaves as described. It does not show that any score measures fatigue or performance, that
a recommendation is right, or that the camera or a wearable works on a real pitch. The coach decides; nothing here is
ready to drive real substitutions.

## 1. Verified commands

Run from the engine repository with its virtual environment (`.venv/bin/python`, or `python` once activated).

| What | Command | Last result (this Mac, 2026-11-07) |
|------|---------|------------------------------------|
| One-command fictional match with the dashboard | `python scripts/demo_launcher.py --session match` | used for the browser checks below |
| Session workflow on real engine processes | `python scripts/verify_session_workflow.py` | 14/14 |
| Live workflow, outage, recovery, export/import, reproduction | `python scripts/verify_operational_workflow.py` | 22/22 |
| Team-scale rehearsal load (20 athletes, 1 Hz + 10 Hz) | `python scripts/rehearsal_load.py --duration 90` | 19,930 inputs, 0 failed, PASS |
| Evaluation pilot rehearsal | `python scripts/evaluate.py make-fictional --out ev` then `python scripts/evaluate.py manifest ev/dataset.json --out ev-manifest.json` then `python scripts/evaluate.py run ev/dataset.json --out ev-report --split all --expected ev/expected-outcomes.json --manifest ev-manifest.json` | report written; self-check 10/10, planted errors caught |
| All checks | `python scripts/check.py` (core) or `python scripts/check.py --all` | see PROJECT_STATUS |
| Browser test pages (Athletrix_Web) | serve the folder on loopback and open `tests/timeline_confidence.test.html` and `tests/video_review.test.html` | 89/89 and 6/6 |

## 2. Operator walkthrough: setup to export

1. **Plan.** Copy `scripts/examples/session/session_plan_template.json` and fill it in: team, each athlete's status
   (on the field or on the bench), role and the roles they can play. For each athlete, `inputs` says whether physiology
   and camera are `expected` or `not_used`, plus their stated sampling intervals if known. Name the wearable assigned to
   each athlete and the selected sources. `fictional_session_plan.json` is a filled FICTIONAL example.
2. **Camera (if selected).** Run `python scripts/camera_setup.py checklist` and follow it. Then check the calibration
   against the camera as it is now:
   `python scripts/camera_setup.py compatible CAL.json --width W --height H --camera-id ID --settings now.json`.
   Fit the clock alignment from reference events: `python scripts/time_align.py fit EVENTS.json --out ALIGN.json`.
   Without an alignment, the camera and wearable timelines stay independent.
3. **Preflight.** `python scripts/sideline_session.py preflight PLAN.json --engine URL`. Readiness is reported
   separately for raw recording, camera review and the experimental recommendation. A missing rubric blocks only the
   recommendation. A wearable that is not sending is reported as not receiving; nothing is filled in.
4. **Start.** `python scripts/sideline_session.py start PLAN.json --engine URL`. This registers missing athletes, starts
   one recording, and enters the roster with its input plans and the stated constraints. Running it again (after a
   refresh, a crash or an engine restart) resumes the same recording and repeats nothing. A different plan is refused
   while a recording is active.
5. **Monitor.** `python scripts/sideline_session.py status --engine URL [--watch 10]` lists, per player, what is
   present, delayed, stale, disconnected, unavailable or not used. The dashboard shows the same in two places: the
   Session setup card and the evidence line of each Team review row, with technical detail under "Evidence details".
6. **Respond.** In the Team review card, choose a player, then acknowledge, keep, review later or dismiss, or record a
   lineup change. Each action saves the review it answers. A retry or double click is stored once.
7. **Annotate (after the session).** See section 5.
8. **Stop and inspect.** `python scripts/sideline_session.py stop --engine URL`; stopping twice is harmless. Then
   `python scripts/sideline_session.py inspect REC_ID --engine URL`.
9. **Export.** The dashboard's recording card, or `GET /api/v1/recordings/{id}/package.zip`, writes package format 7.
   Import elsewhere with `python scripts/recording_package.py import PACKAGE --database DB`. Saved reviews reproduce
   from their pinned configuration. An altered or removed sample is detected and reported as `evidence_changed`.

Raw video never enters a package or an export: the video review tool reads the clip from disk and sends nothing but
the observations you choose, and only to an engine on this computer.

## 3. What the review outputs mean (decision rules version 2)

- **Keep** needs affirmative evidence: every required input (the physiological index and the performance composite)
  is available, and neither shows a sustained concern. A stated coach constraint can also keep a player, and says so.
- **Review later** means a specific reason to wait. Examples: one reading crossed a threshold but the persistence window
  has not matured; an elevated index on its own; context the coach must weigh; no suitable bench option yet.
- **Insufficient evidence** means a required input is missing, stale, disconnected, uncertain or incompatible. This also
  covers no profile, an unknown roster status and a player not on the field. The other input is never used as a
  fallback. Before this batch, the reported p06 case (strap stopped, performance still available) gave "keep"; it now
  gives "insufficient evidence".
- **Context-only observation**: when a required input is missing but the coach-entered roles do not match, the output
  is still insufficient evidence. The mismatch is shown as an observation that names the comparisons not made.
- **Performance-only route**: only when the profile enables `routes.performance_only` and the player's roster entry
  says `inputs.physiology = "not_used"` (no wearable in the plan). It is labelled with its limits. A sensor that was
  expected and stopped sending never takes this route.
- **Saved reviews** keep the result they had. Reproducing a review saved under the older rules says the decision rules
  changed (version 1 then, 2 now) instead of overwriting it.

## 4. First physical-camera calibration: exact evidence required

1. The camera identity, capture resolution and frame rate, and the lens, zoom, focus, stabilisation and mount settings.
   All of them are recorded as `camera_settings` in the calibration points file.
2. Optional lens model: 15–30 chessboard photos at that exact resolution and zoom, covering the frame's corners and
   edges at several tilts, with about one in four kept aside as held-out views (`scripts/camera_intrinsics.py calibrate`).
3. At least 4 (better 6–10) field reference points measured in metres in one coordinate system, spread over the area
   players use, with their pixel positions read on a still frame from the same camera position.
4. At least 3 separate check points, measured the same way and NOT used for fitting, to report independent error.
5. A clock reference at the start AND the end of the session, seen by the camera (a phone showing UTC, a clap or a
   whistle). These are the events for `scripts/time_align.py`, so offset and drift can be estimated with an uncertainty.
6. Permission to film and to use the clip for this purpose. No clip is used without it.

Until these exist, physical calibration, lens correction and camera timing remain **untested**. Everything so far was
checked on synthetic geometry and drawn figures only.

## 5. Independent reviewer annotations

1. Write the moments: copy `scripts/examples/annotation/annotation_sheet_template.json`. For each moment give the
   athlete, role, frame range, the questions asked (`execution_pass`, `decision`, `positioning`, `tactical`), the stated
   criterion per question and any active instruction with its stated exception.
2. Make one blank copy per reviewer: `python scripts/annotate.py blank MOMENTS.json --reviewer "Reviewer A" --out a.json`.
3. Each reviewer fills their own copy, by hand or in `tools/video-review.html` (Annotation sheet panel), without seeing
   the other's labels or any Athletrix output. The page refuses a copy that already holds another reviewer's labels.
   Use "unable to assess" with a reason (for example off camera), or "no opportunity".
4. Compare: `python scripts/annotate.py compare a.json b.json`. This gives assessable counts, agreement where both
   assessed, and every disagreement with both rationales. Agreement is not accuracy.
5. Optionally adjudicate:
   `python scripts/annotate.py adjudicate a.json b.json --decisions D.json --adjudicator ID --out adj.json`. The
   originals are unchanged and are named by hash; unresolved items stay unresolved.
6. Import into a recording:
   `python scripts/annotate.py import adj.json --recording REC_ID --engine URL [--dry-run]`. This needs
   `clip.frame0_utc`, from the time alignment. Execution and decision stay separate observations. Nothing recognises
   passes or grades decisions automatically.

## 6. Zephyr: information still needed, and where it fits

Zephyr is the preferred wearable. Purchase, hardware and API access are **not confirmed**, and nothing in Athletrix
talks to any Zephyr software. The list of open questions is in `docs/zephyr-integration-checklist.md`, section 3. Here
is where each answer plugs in:

| Needed from the vendor | Where it fits |
|------------------------|---------------|
| Permission to use the documentation; current API version | gate for writing `wearable/providers/zephyr.py` (it refuses to run today) |
| Sample payloads: normal data, missing values, a disconnect and reconnect, low signal | reader tests; the evidence view's present / stale / disconnected labels |
| Supported client or transport (Python or a bridge on the OmniSense computer) | the reader's transport inside `wearable/providers/` |
| Units, ranges, missing-value conventions per field | `wearable/mapping.py` field mapping into `PhysiologicalSample` |
| Timestamp semantics (whose clock, resolution) | `wearable/clock.py` `ClockMapping` basis and uncertainty; `scripts/time_align.py` for camera alignment |
| Which HRV statistic each live value is | mapped to `hrv_rmssd_ms` or `hrv_sdnn_ms` only, never substituted |
| Worn / confidence / signal indicators | `signal_quality`, a skip rule, or provenance only |
| Connection, battery and signal reports | `POST /api/v1/devices/status` → the evidence view's device report |
| Sampling rate | the session plan's `physiology_interval_s` (expected counts stay unknown without it) |
| Evaluation and licence terms, hardware cost | a team decision; not software |

## 7. Decisions that belong to the team

Use `docs/team-decisions-template.md`. Each field is blank until the team decides it.

- **Baseline protocol:** resting condition, duration, device placement, how many baselines, who reviews and accepts them.
- **The real index, rubric and substitution rules:** today only FICTIONAL profiles exist, and none ships with the engine.
- **Input freshness requirements:** how old a reading may be, when a stream counts as disconnected, and how late a
  sample may arrive. The current labels use adjustable engineering defaults: stale after the index profile's
  staleness, disconnected after 30 s, delayed after 5 s.
- **Feasible confirmation workload:** how many identity tasks one operator can resolve per half, and what happens to
  unresolved intervals. The drawn-figure clip produced 16 tasks; no person's time has been measured.
- **Whether a performance-only route is acceptable** for players without a wearable, and its limits.
- **Evaluation design:** reviewers, held-out examples, the questions to answer, and ethics or study approval. No
  approval exists or is claimed.

## 8. Verified synthetically versus unverified in the field

| Area | Verified (software, synthetic or fictional) | Not verified in the field |
|------|---------------------------------------------|---------------------------|
| Missing-data decisions | unit tests, a frozen-clock API test, the p06 reproduction in the verifier, the real dashboard | real dropouts of a real wearable |
| Evidence quality | receipts, retries, replacements, rejected writes, device reports, stale and disconnected on fictional streams | real sensor and network behaviour |
| Session workflow | real engine processes, startup failure, kill and restart, resume, clean shutdown | a real sideline laptop, power and network |
| Calibration and lens | synthetic geometry; compatibility and invalidation logic | a physical camera; accuracy away from check points |
| Clock alignment | known synthetic offsets, drift, a mismatched event | real camera and wearable clocks |
| Identity confirmation | five synthetic cases, a drawn-figure clip | real players, kits, occlusions; operator time |
| Annotations | fictional reviewers, blind sheets, comparison, adjudication | real reviewers' agreement |
| Evaluation | fictional dataset; planted errors caught | any real outcome; agreement with a real coach |
| Load | 20 fictional athletes for 90 s on one laptop | a full match; another machine |
