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
| Start the round | — | `pilot_session.py round-start px1 --round 1` |
| **1. Save the score** | enter the round, press **1. Save score**, read "SAVED … all three estimates available: yes/NO" | `pilot_session.py save-score px1 --round 1` |
| **2. Start the sprint** (only after step 1 is confirmed) | press **2. Start timed sprint**, then give the start signal | `pilot_session.py sprint-start px1 --round 1 --confirm PREDICTION_ID` |
| Sprint over | — | `pilot_session.py sprint-end px1 --round 1`, then `round-end px1 --round 1` (or `--invalid "false start"`) |

- If the save says "NO" for some estimate, the reason is on the card (for example "wearable stream: newest sample
  20 s old"). Fix it if possible and save again. The round stays recorded either way.
- If you forget to save, start the sprint anyway: the round is kept but labelled ORDER UNVERIFIED, and it is not in the
  primary comparison.
- If the wearer or round changes after a save, the card refuses the start; save again.
- Never type a time for "save score". A sprint marker stated more than 2 s in the past is labelled retrospective.

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

Tip from the rehearsal: start a round only when the card shows both streams fresh. A round started without data keeps
an empty camera reference, and its camera and combined scores stay unavailable.

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
