# Fatigue pilot run sheet

For one session with one vest and one fixed camera on one offline laptop. Everything below was verified with
FICTIONAL data only (see `docs/fatigue-pilot-checklist.md`). The pilot gives no substitution or safety advice.

## Before the session

1. Have the approved procedure, permissions and the frozen scoring profile. Without them, run only a rehearsal.
2. Start the engine with the team's profile: `FATIGUE_PROFILE_PATH=/path/to/frozen_profile.json`, then
   `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1`.
3. Resting baselines: record each participant seated at rest, stop the recording, create the baseline and accept it.
   The dashboard's "Baselines and profiles" card does this, or use `POST /athletes/{id}/baselines` and `.../decision`.
   Note each baseline id.
4. Plan and start the recording: `python scripts/sideline_session.py preflight PLAN.json`, then
   `python scripts/sideline_session.py start PLAN.json`. Check that the "Fatigue pilot estimate" readiness is not
   blocked. The substitution review is not needed.
5. Open the study session (this freezes the profile):
   `python scripts/pilot_session.py session --label day1 --protocol controlled_study --camera-alignment operator_estimate:0.04`.
   Use the alignment from `scripts/time_align.py`. Without a stated alignment, the combined score stays unavailable.

## For each participant and round (one vest at a time)

| Step | Command (re-running it is safe) | What the dashboard shows |
|---|---|---|
| Put the vest on | `pilot_session.py wearer px1 --code P1 --device VEST --baseline BL --hr-max 201 --hr-max-origin "measured test"` | Fatigue pilot card: "P1 (vest VEST, since …)" |
| Start the round | `pilot_session.py round-start px1 --round 1` | estimate, trend, factors, freshness |
| **Before** the timed sprint | `pilot_session.py sprint-start px1 --round 1` | the command prints the saved prediction; the card's "Last saved pre-sprint prediction" |
| After the sprint | `pilot_session.py sprint-end px1 --round 1`, then `round-end px1 --round 1` (or `--invalid "reason"`) | the sprint is excluded from both streams from now on |
| Enter the time separately | `pilot_session.py result px1 --round 1 --time 3.21 --method timing_gates` (or `--missing equipment_failure`) | — |
| Optional self-rating | `pilot_session.py rating px1 --round 1 --value 6` (or `--missing not_collected`) | — |
| Check | `pilot_session.py rounds` | — |

- Press sprint-start before the sprint. If the marker reaches the engine more than 2 s after the stated time, the
  prediction is labelled RETROSPECTIVE and never counts as a pre-sprint prediction.
- To correct a time: `result px1 --round 1 --time 3.19 --method timing_gates --correct ENTRY_ID --why "gate re-read"`.
  The original stays in the history; the saved prediction never changes.
- To hand the vest to the next participant: run `wearer` for them, with their own baseline. Nothing carries over.

## After the session

1. `python scripts/sideline_session.py stop`, then export the recording package (dashboard, or
   `GET /api/v1/recordings/{id}/package.zip`). Format 8 carries the study log and the saved predictions.
2. Pin the analysis before any held-out data:
   `python scripts/evaluate.py pilot-pin --profile frozen_profile.json --analysis-split participant --out pin.json`.
3. Report: `python scripts/evaluate.py pilot --packages day1.zip day2.zip --out report --pin pin.json`.
4. Removal (`scripts/local_data.py remove-athlete ID --execute --confirm ID`) deletes this laptop's copy only. Exported
   packages and their copies are not touched.

## Worked FICTIONAL round (wall-clock rehearsal, 2026-11-08, P1 round 2)

- **Known before the sprint** (saved at 02:03:45.966 UTC, when the marker reached the engine; prospective):
  - vest-only **27.39** = heart-rate strain 41.08 (mean 151.3 bpm = 66.4 % of reserve) × 0.67 + time above 85 % HR max
    0 × 0.33; recovery unavailable ("no qualifying effort");
  - camera-only **17.3** = output change 8.64 (speed 3.35 vs 3.47 m/s reference) × 0.6 + sprint quality 30.3 (peak
    6.72 vs 7.04 m/s) × 0.4;
  - combined **24.59** = strain, time above, HR per speed 26.95 (+8.1 % bpm per m/s) and output change;
  - round 1's sprint was excluded from both streams (366 samples left out);
  - the 30 s windows come from the FICTIONAL short-window rehearsal profile.
- **Entered afterwards:** sprint end 02:03:50.4; time **3.126 s** by fictional timing gates; self-rating "not_collected".
- **In the report:** the row `P1 | rehearsal-day1 | 2 | 27.39 | 17.3 | 24.59 | 3.126 | 2.6264`. The slowdown is
  100 × (3.126 − 3.046) / 3.046, against P1's best valid time that session (round 1). In that 9-minute run, both targets
  were NOT MET (n = 4, flagged small).

## Decisions to make before freezing the real protocol

- **Scoring:** the features (see the guide, section 7), %HRR or %HRmax, windows, bounds, weights, required features,
  freshness, and the test-sprint exclusion.
- **Baselines:** the resting procedure (position, duration, device placement, acceptance) and the camera reference.
- **Valid rounds:** what makes a round invalid (false start, slip, interrupted drill, missing gate time), and how many
  rounds per participant per day.
- **Sprint timing reference:** gates, stopwatch or video frames; the course and its measurement; who times it,
  independently of the scores.
- **Minimum coverage:** the camera tracked share, the wearable coverage and confirmed-identity time needed for a round
  to count.
- **The analysis pin:** split by participant or session, what is held out, and what result would count.

## Still needs real hardware or approved field data

- the wearable (Zephyr access is unconfirmed; the pilot needs only about 1 Hz heart rate);
- a calibrated fixed camera with measured references;
- clock alignment between the two;
- independently timed sprints;
- approved participants and procedure.
