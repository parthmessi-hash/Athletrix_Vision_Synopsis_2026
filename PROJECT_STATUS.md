# Athletrix: project status and handoff

Concise handoff for anyone (or any fresh conversation) picking the project up.
Everything below was checked against the code on 2026-10-25. Where something
is a plan rather than working code it is marked **planned**.

## 1. Purpose and terminology

Athletrix is a local-first, multi-sport athlete-monitoring platform. It combines
wearable physiology and camera-derived movement data for the athletes a coach
is watching, and shows them side by side with descriptive performance language.

Ground rules that apply to all code and copy in the engine and its connected cards:

- **Provider agnostic.** Models describe data shapes, never a vendor's payload.
  `source` is a generic category (`wearable`, `camera`, `manual`, `simulated`).
- **Descriptive, not prescriptive.** The engine reports counts, means, peaks,
  ages, gaps, distances and durations. It does not produce medical diagnoses,
  fatigue classifications, readiness or condition scores, or substitution
  instructions. Rules it applies are "adjustable engineering defaults", not
  validated sport standards, and the responses say so.
- **Fictional demo data.** Everything the simulator and the browser demo
  produce is fictional and labelled as such.

Terms used throughout:

| Term | Meaning |
|------|---------|
| Engine / backend | The FastAPI service in this repository (port 8000). |
| Frontend / Athletrix_Web | The single static page `index.html` (port 3000 locally; assistantcoachai.net publicly). |
| Stream | One of two per-athlete sample series: `physiological` or `tracking`. |
| Latest | The newest persisted sample of a stream for an athlete. |
| History | The newest 300 samples per athlete per stream (a count, not a time span). |
| Window | Both streams for one athlete between `window_start` and one captured backend instant `window_end`. |
| Summary | Descriptive metrics over a window. |
| Engine mode / public mode | Whether the frontend polls the engine (localhost) or shows an explanation card and makes no engine requests (any other host). |
| Usable tracking point | A tracking sample whose `tracking_confidence` is at or above the engine's `min_confidence` rule. Says nothing about the accuracy of the speed value itself. |

## 2. Repositories and roles

| Role | Local path | GitHub | Notes |
|------|-----------|--------|-------|
| Engine (this repo) | `/Users/parthgupta/Athletrix Vision Synopsis 2026` | `parthmessi-hash/Athletrix_Vision_Synopsis_2026` | FastAPI + SQLite, tests, demo scripts, this file. |
| Frontend | `/Users/parthgupta/Athletrix Fixed Website` | `parthmessi-hash/Athletrix_Web` | One static `index.html` (marketing site + demo dashboard), `images/`, `tests/`. Netlify deploys `main` to assistantcoachai.net; DNS and hosting settings are not in the repo. |

Both repositories commit as `parthmessi-hash` and push to `origin/main`.

## 3. Exact commands

All engine commands run from the engine folder with its virtual environment active.

```bash
# one-time setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional; every value has a local default

# 1. backend (port 8000, auto-reload)
uvicorn app.main:app --reload

# 2. fictional demo roster: athlete-1 Alex Example (football), athlete-2 Sam Sample (basketball)
#    one-time per database file; safe to rerun (identical records are left alone, conflicts exit 1)
python scripts/register_demo_roster.py                     # or --url http://host:port

# 3. simulators (fictional data, one sample per second; one terminal each)
python scripts/simulate_athlete.py --athlete-id athlete-1 --device-id simulator-1 --with-tracking
python scripts/simulate_athlete.py --athlete-id athlete-2 --device-id simulator-2 --with-tracking
python scripts/simulate_athlete.py --profile effort-rest --with-tracking --seconds 120   # repeating 40 s effort/rest cycle
#    flags: --seconds N (default 60) --url URL --athlete-id ID --device-id ID --with-tracking --profile sine|effort-rest

# 4. frontend, from the Athletrix_Web folder
python3 -m http.server 3000
```

Then open http://localhost:3000, click **Live Demo**, and pick an athlete in the
**Athlete** selector. API docs: http://127.0.0.1:8000/docs.

## 4. Models, endpoints and configuration

Models live in `app/models/` and reject unknown fields (typos become 422s).
Timestamps must be ISO 8601 with a timezone.

| Model | Fields (units) |
|-------|----------------|
| `Athlete` | `id`, `name`, `jersey_number` (0-999), `sport` (free text), `team_id` |
| `PhysiologicalSample` | `athlete_id`, `timestamp`, `source`, `device_id`; optional `heart_rate_bpm` (20-260), `breathing_rate_bpm`, `hrv_rmssd_ms`, `hrv_sdnn_ms`, `activity_level` (0-1), `posture`, `skin_temperature_c`, `spo2_percent`, `signal_quality` (0-1) |
| `TrackingSample` | `athlete_id`, `timestamp`, `x_position` (m), `y_position` (m), `speed_mps` (>= 0), `acceleration_mps2`, `tracking_confidence` (0-1); all required |
| `DeviceStatus` | `device_id`, `timestamp`, `connected`; optional `battery_percent`, `signal_quality` |
| `AthleteWindow` | `athlete_id`, `window_start`, `window_end`, `physiological_samples`, `tracking_samples`, `tracking_rules` |
| `TrackingRules` | `min_confidence` (0-1): the applied `DISTANCE_MIN_CONFIDENCE` |
| `AthleteSummary` | `athlete_id`, window bounds, `physiological` (`PhysiologicalSummary`), `tracking` (`TrackingSummary` incl. distance, high-speed and `distance_rules`) |

Endpoints, all under `/api/v1`:

| Method and path | Purpose |
|-----------------|---------|
| `GET /health` | `{status, service, version}` |
| `POST /athletes` | Register (201; 409 if the id exists, original kept) |
| `GET /athletes`, `GET /athletes/{id}` | List sorted by id / fetch one (404) |
| `POST /athletes/validate` | Validate an athlete payload without storing it |
| `POST /samples/physiological`, `POST /samples/tracking` | Ingest one sample: validate, reject timestamps more than 5 s ahead of backend UTC (422), persist, echo. Registration is not required. |
| `POST /devices/status`, `GET /devices/{id}/status/latest` | Device status ingest (newest per device kept) and read |
| `GET /athletes/{id}/physiological/latest`, `.../tracking/latest` | Newest persisted sample (404 if none) |
| `GET /athletes/{id}/physiological/history?limit=60`, `.../tracking/history?limit=60` | Newest `limit` (1-300) samples, oldest to newest |
| `GET /athletes/{id}/window?seconds=60` | Both streams in the last `seconds` (1-300), one captured `window_end`, plus `tracking_rules` |
| `GET /athletes/{id}/summary?seconds=60` | Descriptive metrics over the same window selection |

Configuration (`app/core/config.py`, overridable via environment or `.env`; see `.env.example`):

| Setting | Default | Meaning |
|---------|---------|---------|
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Informational; uvicorn flags decide the bind address |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | Origins allowed to call the API |
| `DATABASE_PATH` | `data/athletrix.db` | SQLite file, relative to the start folder; created automatically |
| `DISTANCE_MAX_GAP_SECONDS` | `3.0` | Longest consecutive-sample interval counted (0 < v <= 60) |
| `DISTANCE_MIN_CONFIDENCE` | `0.5` | Minimum confidence at both ends of an interval; also echoed as `tracking_rules.min_confidence` in the window |
| `HIGH_SPEED_THRESHOLD_MPS` | `5.0` | Speed at or above which time and distance count as high-speed (demonstration value) |

Other fixed constants: `MAX_HISTORY_SAMPLES = 300` (`app/store/history.py`),
`MAX_WINDOW_SECONDS = 300` (`app/api/athletes.py`), `MAX_FUTURE_TIMESTAMP_SKEW = 5 s`
(`app/core/clock.py`).

## 5. Persistence and retention

- One local SQLite file (`DATABASE_PATH`), Python's built-in `sqlite3`, WAL mode,
  5 s busy timeout, one connection per process shared behind a lock. Schema is
  created at startup. Supported setup: a single uvicorn process (`--reload` is fine).
- Persisted across restarts: registered athletes; each device's newest status;
  each athlete's newest 300 physiological and 300 tracking samples. "Latest" is
  simply the newest persisted row, so it survives restarts too.
- Each sample write (insert or equal-instant replacement plus trim to 300) is one
  short transaction. Equal timestamps: the latest arrival replaces the earlier one.
  Timestamps are stored as fixed-width UTC text; payloads keep the original offset.
- Retention is a sample count, not a time span: at 1 Hz about five minutes per
  stream. There is no match or season storage and no export.
- `data/` and `*.db*` are git-ignored. Tests never touch it: every test uses its
  own temporary file (`tests/conftest.py`).

## 6. Public mode versus engine mode (frontend)

The page resolves a mode on load (`resolveEngineMode` in `index.html`):

| Mode | When | Behaviour |
|------|------|-----------|
| `engine` | hostname is `localhost`, `127.0.0.1`, `[::1]` or `*.localhost`; or `?engine=1`; or `engineMode: "engine"` in `window.ATHLETRIX_CONFIG` | Shows the athlete selector and four engine-connected cards (Live Heart Rate, Live Tracking, Last 60 seconds, Recent movement and heart rate) and polls `apiBase` (default `http://127.0.0.1:8000`). |
| `public` | any other hostname (the published site), or `?engine=0`, or `engineMode: "public"` | Shows an explanation card with the local startup steps. **No request is made to the engine.** |

Polling: live cards every 1 s, summary and timeline every 5 s, only while the
dashboard view is visible. Each request carries the selection generation it was
made for; responses from an earlier generation (a previous athlete) are dropped.
The rest of the dashboard is a browser-only fictional demo, labelled with an amber
banner, and is never substituted for engine data.

## 7. Implemented calculations and their assumptions

All of these are descriptive and cover only the samples available in the window
(bounded by transmission gaps and the 300-sample history cap).

- **Window selection.** Backend UTC time is read once as `window_end`;
  `window_start = window_end - seconds`; samples included when
  `window_start <= timestamp <= window_end`. Groups by recorded time only: no
  device-clock correction, no effort-to-response lag modelling.
- **Heart rate mean and peak.** Arithmetic mean and max of non-null
  `heart_rate_bpm`; not time-weighted. Nulls ignored, counts reported.
- **Peak speed.** Max `speed_mps`; `0.0` is a real value.
- **Sample age and gap.** `latest_sample_age_seconds` = `window_end` minus newest
  timestamp; `max_inter_sample_gap_seconds` = largest gap between consecutive
  samples (null with fewer than two). Neither proves full-window coverage.
- **Gap-aware distance.** Consecutive tracking samples form intervals; an interval
  is eligible when elapsed time is positive and <= `DISTANCE_MAX_GAP_SECONDS` and
  both endpoint confidences are >= `DISTANCE_MIN_CONFIDENCE`. Each eligible
  interval adds `(v_prev + v_cur) / 2 * elapsed` (trapezoidal rule: speed assumed
  linear between samples). Rejected intervals are counted once and never bridged;
  nothing is extrapolated. Null when nothing was eligible; `0.0` when eligible
  intervals were stationary. Also reports included duration and window fraction.
- **High-speed time and distance.** Over exactly the same eligible intervals, the
  part at or above `HIGH_SPEED_THRESHOLD_MPS`, with threshold crossings
  interpolated linearly. Duration <= eligible duration; distance <= total estimate.
  Not sprint detection.
- **Window tracking rules.** The window echoes the applied `min_confidence` so a
  consumer classifies its samples from that response alone. Samples below the rule
  are still returned. Comparing confidence with the rule does not validate the
  accuracy of the speed measurement.
- **Timeline display rules (frontend).** Points are recorded samples on one time
  axis from the backend's window bounds. A tracking point is usable only when the
  response carries a valid rule (number in 0..1) and its confidence is >= that
  rule; below-rule points and null heart rates are hollow amber markers;
  if the rule is missing or invalid, every speed point is a dashed grey
  unclassified marker and no line is drawn. Lines join consecutive usable points
  only when <= 3 s apart (a display rule). Nothing is interpolated.
- **Simulator profiles.** `sine`: one rise-and-fall of heart rate (110 to 150 bpm)
  and straight-line speed (peak 8 m/s), confidence 1.0. `effort-rest`: repeating
  40 s cycle (accelerate 10 s to 6 m/s, hold 10 s, slow 10 s, stand 10 s), position
  the exact integral of speed, heart rate a toy delayed first-order response.
  Fictional; not physiological models.

## 8. Known limitations and unfinished capabilities

Implemented today is listed above; the following are **not** implemented:

- No real device or camera adapters: all data so far comes from the simulator or
  manual POSTs. Vendor payload mapping onto the generic models is **planned**.
- No authentication, authorisation or multi-user handling; the engine is meant to
  run on a local sideline machine behind CORS only.
- Single-process SQLite only; multiple backend processes on one file are unsupported.
- Retention is 300 samples per stream; no match/season storage, no export or replay.
- Devices are not linked to athletes in the data model (a sample names its
  `device_id`, but device status is independent of the athlete registry).
- Athlete registration is not required for ingestion, so unregistered ids can
  accumulate samples.
- The window and summary group by recorded time only; no clock alignment between
  devices and no modelling of the lag between movement and physiological response.
- The frontend's engine-connected cards use descriptive language, but the
  browser-only demo portions of `index.html` (fictional team dashboard, "fatigue
  index", "substitution" panels, marketing copy) predate that rule and still use
  prescriptive wording. Bringing them in line is **planned**, not done.
- Frontend tests run as a static page in a browser (no JavaScript runtime is
  assumed on the development machine); they are not wired into CI.
- `HOST`/`PORT` settings are informational; uvicorn's own flags bind the address.

## 9. Verification commands

```bash
# engine: full suite, isolated temporary databases (308 tests as of this handoff)
cd "/Users/parthgupta/Athletrix Vision Synopsis 2026" && source .venv/bin/activate && pytest -q

# engine: window carries the applied rule (backend running)
curl -s "http://127.0.0.1:8000/api/v1/athletes/athlete-1/window?seconds=60" | python3 -m json.tool | grep -A2 tracking_rules

# frontend: timeline behaviour tests (frontend served on port 3000; no engine needed)
open http://localhost:3000/tests/timeline_confidence.test.html      # expect "15 tests, 15 passed"

# frontend: public mode locally (must make no engine requests; check the Network tab)
open "http://localhost:3000/?engine=0#demo"
```

## 10. Recommended next milestone

**First real-data path: a file replay adapter.** Add a provider-agnostic
"replay" script or endpoint that reads an exported wearable or tracking file
(CSV/JSON), maps it onto `PhysiologicalSample` / `TrackingSample` with explicit
unit and timezone handling, and feeds it through the existing ingestion with
the clock-skew check. This exercises the models against non-simulated data,
surfaces the device-to-athlete linkage gap, and gives the dashboard something
other than the simulator to show, without touching the descriptive-language
constraints or persistence design.
