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
  `source` is a generic category (`wearable`, `camera`, `manual`, `simulated`): supplied metadata, never proof of authenticity or sensor accuracy. Required on physiological samples, optional on tracking samples (absent = not reported).
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
| History | The newest samples per athlete per stream up to that stream's retention cap (`PHYSIOLOGICAL_RETENTION_SAMPLES`, `TRACKING_RETENTION_SAMPLES`, default 300 each; a count, not a time span). |
| Window | Both streams for one athlete between `window_start` and one captured backend instant `window_end`. |
| Replay / interchange format | Athletrix's own JSON Lines format (`{"type": ..., "sample": {...}}`) replayed by `scripts/replay_samples.py` with recorded timestamps preserved. Not a vendor format. |
| Demo launcher | `scripts/demo_launcher.py`: one command starts a temporary database, an isolated backend and a frontend copy on free ports, registers a fictional multi-sport roster and feeds simulated data for a bounded time; nothing in the repositories, `.env` or the development database is touched. |
| Team-scale validation | `scripts/validate_team_scale.py`: starts an isolated backend, registers N fictional athletes to a fictional team, sends samples concurrently on independent per-stream grids (`--rate`, or `--physiological-rate` / `--tracking-rate`) while reading the team overview every 2 s, reports write and read latency, scheduling lateness and counts (omitted-by-scenario apart from unsent), verifies retained data, overview consistency (no substituted readings, newest-timestamp ordering, paused-stream recency checked on the reads taken during the pause) and device statuses, with timeout-dependent checks reported as unknown rather than passed; `--scenario interrupted` adds a fixed paused/late-stream disturbance. Observations on one machine, not a capacity claim. |
| Retention comparison | `scripts/compare_retention.py`: the team-scale scenario run twice on two isolated backends, profile A (default caps) and profile B (larger tracking cap), same deterministic values and relative schedule, genuine timestamps per run; verifies retention, an early effort review (movement against independently calculated expectations, heart-rate periods, availability metadata, null distance preserved), the recorded window, export and replay into a third isolated backend, and compares the runs. Observations, not thresholds. |
| CSV adapters | `scripts/convert_heart_rate_csv.py` (heart-rate CSV, bpm, plus explicitly mapped optional physiological fields) and `scripts/convert_tracking_csv.py` (position/motion CSV: metres, m/s, m/s², confidence 0 to 1): documented, provider-agnostic converters with explicit column names and ISO timestamps with timezone, into the interchange format. Not support for any vendor's export; no hardware, no network, no clock synchronization. |
| Live mode / Recorded review | Live: window and summary end at backend time now and the dashboard polls. Recorded review: the same endpoints with `end=<timestamp>` and the dashboard's Recorded mode, showing the 60 s ending at a chosen past instant. A review covers only samples still retained (the newest per athlete up to each stream's cap); it is not a recording archive. |
| Summary | Descriptive metrics over a window. |
| Effort review | `GET /athletes/{id}/effort-review?effort_start&effort_end`: a manually selected 1-120 s period described by the tracking summary during it and heart-rate figures before [start-10 s, start), during [start, end] and after (end, end+30 s] (capped at backend time). Not sprint detection, not an effort classification. |
| Live team overview | `GET /teams/{id}/live` and the dashboard's team card: every registered athlete on a team with the newest retained reading per stream and its age; the 5 s "stopped updating" label is a display rule, not a judgment. |
| Retained-samples export | `GET /athletes/{id}/retained-samples.jsonl`: both streams of one athlete as replayable JSON Lines, written as stored. A snapshot of retained rows, not an archive. |
| Retained data | `GET /athletes/{id}/retained-data`: per stream, the count of rows currently stored, the UTC recorded timestamps of the oldest and newest, and that stream's cap (`max_samples`). Describes retained rows only, not coverage, sessions or an archive; the dashboard's Find retained data / Review latest retained buttons use it. |
| Recording | An explicitly started local recording (`POST /recordings`): a generated id, a name, a fixed roster snapshot of selected registered athletes, backend UTC start and stop times, status active or stopped. One active at a time; survives restarts until stopped. Not automatic, never uploaded, never pruned. |
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
#    flags: --seconds N (default 60) --url URL --athlete-id ID --device-id ID --with-tracking --with-device-status --profile sine|effort-rest

# 3b. replay a recorded JSON Lines file exactly as recorded (fictional example included)
python scripts/replay_samples.py scripts/examples/fictional_replay.jsonl --dry-run      # validate + describe, send nothing
python scripts/replay_samples.py scripts/examples/fictional_replay.jsonl                # replay at recorded pace
python scripts/replay_samples.py recording.jsonl --url http://127.0.0.1:8010 --speed 4  # other backend, 4x pacing
#    flags: --url URL --speed POSITIVE_FINITE (default 1) --dry-run; exit 0 ok, 1 invalid file, 2 stopped on a failure

# 3b'. convert a heart-rate CSV (bpm, ISO timestamps with timezone) into that format, then replay it
python scripts/convert_heart_rate_csv.py scripts/examples/fictional_heart_rate.csv /tmp/hr.jsonl \
    --athlete-id athlete-1 --device-id fictional-hr-csv-1 --source simulated --dry-run   # validate + describe only
python scripts/convert_heart_rate_csv.py scripts/examples/fictional_heart_rate.csv /tmp/hr.jsonl \
    --athlete-id athlete-1 --device-id fictional-hr-csv-1 --source simulated             # output must not exist yet
python scripts/replay_samples.py /tmp/hr.jsonl
#    flags: --timestamp-column NAME --heart-rate-column NAME (defaults timestamp, heart_rate_bpm); --source is one of
#    wearable|camera|manual|simulated and is never inferred; exit 0 ok, 1 invalid CSV / output exists, 2 bad arguments
#    optional measurements, each written only when its flag is given (values as written in the model's units, blank = null):
#    --breathing-rate-column --hrv-rmssd-column --hrv-sdnn-column --activity-level-column --posture-column
#    --skin-temperature-column --spo2-column --signal-quality-column; richer fictional fixture:
python scripts/convert_heart_rate_csv.py scripts/examples/fictional_physiology.csv /tmp/phys.jsonl \
    --athlete-id athlete-1 --device-id fictional-phys-csv-1 --source simulated --breathing-rate-column breathing_rate_bpm \
    --hrv-rmssd-column hrv_rmssd_ms --hrv-sdnn-column hrv_sdnn_ms --activity-level-column activity_level --posture-column posture \
    --skin-temperature-column skin_temperature_c --spo2-column spo2_percent --signal-quality-column signal_quality

# 3b''. convert a tracking CSV (metres, m/s, m/s², confidence 0-1, ISO timestamps with timezone) and replay it
python scripts/convert_tracking_csv.py scripts/examples/fictional_tracking.csv /tmp/trk.jsonl --athlete-id athlete-1 --source simulated --device-id fictional-trk-csv-1 --dry-run
python scripts/convert_tracking_csv.py scripts/examples/fictional_tracking.csv /tmp/trk.jsonl --athlete-id athlete-1 --source simulated --device-id fictional-trk-csv-1
python scripts/replay_samples.py /tmp/trk.jsonl
#    flags: --source REQUIRED (wearable|camera|manual|simulated, never inferred) --device-id optional; --timestamp-column
#    --x-position-column --y-position-column --speed-mps-column --acceleration-mps2-column --tracking-confidence-column
#    (defaults = model field names, all six must be distinct and present exactly once); every measurement is required
#    (blank = error, nothing filled in or calculated); exit 0 ok, 1 invalid CSV / output exists, 2 bad arguments

# 3c. review the 60 s ending at a recorded instant (same endpoints, optional end=; encode '+' as %2B)
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/summary?seconds=60&end=2026-10-20T10:00:02Z"
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/window?seconds=60&end=2026-10-20T10:00:02Z"
#    or in the dashboard: Review bar -> Recorded -> enter 2026-10-20T10:00:02Z -> Review

# 3d. find what is retained for an athlete (per stream: count, oldest and newest UTC timestamps, cap), then open the
#     60 s ending at the newest retained timestamp (dashboard: Recorded -> Find retained data -> Review latest retained)
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/retained-data"
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/summary?seconds=60&end=<latest_timestamp from the previous call>"

# 3e. describe a manually selected effort: movement during it, heart rate 10 s before, during, and up to 30 s after
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/effort-review?effort_start=2026-10-20T10:00:01Z&effort_end=2026-10-20T10:00:04Z"
#    both bounds ISO 8601 with timezone (no epochs, no naive values); 1 s <= duration <= 120 s; end not after backend time

# 3i. one-command isolated demo: temporary database, own backend and frontend copy on free ports, fictional roster, simulated data
python scripts/demo_launcher.py --frontend "/Users/parthgupta/Athletrix Fixed Website" --athletes 4 --duration 90 --device-status
#    prints http://localhost:<port>/#demo; Ctrl-C stops only what it started and removes its temporary folder
#    add --tracking-cap 3000 (and/or --physiological-cap N) to start the demo backend with other retention caps (its environment only)

# 3g. team-scale validation on its own isolated backend (fresh temporary database, free port; the dev backend is untouched)
python scripts/validate_team_scale.py --athletes 11 --duration 15
python scripts/validate_team_scale.py --athletes 20 --duration 60                          # healthy, overview read every 2 s
python scripts/validate_team_scale.py --athletes 20 --duration 30 --scenario interrupted   # paused/late streams + device statuses
python scripts/validate_team_scale.py --athletes 20 --duration 40 --physiological-rate 1 --tracking-rate 10   # unequal rates, crosses the tracking cap
python scripts/validate_team_scale.py --athletes 20 --duration 20 --physiological-rate 1 --tracking-rate 4 --scenario interrupted
python scripts/validate_team_scale.py --athletes 20 --duration 60 --physiological-rate 1 --tracking-rate 10 --tracking-cap 3000   # isolated backend with a larger tracking cap

# 3j. retention comparison: the same 20-athlete, 1 Hz / 10 Hz, 60 s scenario on two isolated backends (A: 300/300, B: 300/3000),
#     an early effort (5-15 s) reviewed under both, export + replay into a third isolated backend; report under reports/ (ignored)
python scripts/compare_retention.py --athletes 20 --duration 60 --physiological-rate 1 --tracking-rate 10 --tracking-cap 3000 --effort-start 5 --effort-end 15

# 3k. explicit local recording: start for registered athletes, inspect, stop (one active at a time; survives restarts until stopped)
curl -s -X POST http://127.0.0.1:8000/api/v1/recordings -H 'content-type: application/json' -d '{"name":"Tuesday practice","athlete_ids":["athlete-1","athlete-2"]}'
curl -s http://127.0.0.1:8000/api/v1/recordings/active
curl -s -X POST "http://127.0.0.1:8000/api/v1/recordings/<id>/stop"
#    JSON reports under reports/ (ignored by git); registers fictional athletes to team fictional-team first (reported apart)

# 3h. live team overview: every registered athlete on a team, newest retained reading per stream with its age
curl "http://127.0.0.1:8000/api/v1/teams/demo-team/live"

# 3f. export every retained sample for an athlete (both streams) and replay it into another local database
curl -fsS -o athlete-1.jsonl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/retained-samples.jsonl"
python scripts/replay_samples.py athlete-1.jsonl --url http://127.0.0.1:8010      # a second backend with its own DATABASE_PATH
#    dashboard: Recorded -> Download retained samples (saved on this computer, uploaded nowhere)

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
| `TrackingSample` | `athlete_id`, `timestamp`, `x_position` (m), `y_position` (m), `speed_mps` (>= 0), `acceleration_mps2`, `tracking_confidence` (0-1); all required. Optional, backward-compatible `source` and `device_id` (absent = not reported; older stored rows still load) |
| `DeviceStatus` | `device_id`, `timestamp`, `connected`; optional `battery_percent`, `signal_quality` |
| `AthleteWindow` | `athlete_id`, `window_start`, `window_end`, `physiological_samples`, `tracking_samples`, `tracking_rules`, `sources` (`physiological` and `tracking` breakdowns of exactly the listed samples) |
| `SourceBreakdown` | `reported` (count per source category that occurs), `not_reported`; always adds up to the stream's sample count. Also on `PhysiologicalSummary.sources` and `TrackingSummary.sources` |
| `TrackingRules` | `min_confidence` (0-1): the applied `DISTANCE_MIN_CONFIDENCE` |
| `AthleteSummary` | `athlete_id`, window bounds, `physiological` (`PhysiologicalSummary`), `tracking` (`TrackingSummary` incl. distance, high-speed and `distance_rules`) |
| `EffortReview` | `athlete_id`, `effort_start`, `effort_end`, `effort_duration_seconds`, `captured_at`, `follow_up_elapsed`, `rules` (`EffortRules`: before 10 s, after 30 s, effort 1-120 s), `movement` (`TrackingSummary` over the effort), `heart_rate.before/during/after` (`HeartRatePeriod`: applied bounds with inclusivity flags, sample and heart-rate counts, mean, peak, first/last, max gap, `sources`) |
| `AthleteLive` | `athlete_id`, `captured_at`, `registered`, `athlete` or null, `physiological`, `tracking` or null, ages, `physiological_newest_ahead_seconds`, `tracking_newest_ahead_seconds`, `devices` |
| `TeamLive` | `team_id`, `captured_at`, `athletes` (`TeamAthleteLive`: `athlete`, `physiological`, `tracking` or null, `physiological_age_seconds`, `tracking_age_seconds`, `devices`: `ReportedDevice` with `device_id`, `streams`, `status` or null, `status_age_seconds`) |
| `RetainedData` | `athlete_id`, `physiological` and `tracking` (`StreamRetention`: `sample_count`, `max_samples` (that stream's cap), `earliest_timestamp`, `latest_timestamp` in UTC, null when empty), `max_samples_per_stream` (the shared cap when both are equal, null when they differ; kept for older consumers) |
| `RecordingStart` | `name` (1-120 characters, not blank), `athlete_ids` (distinct, registered) |
| `Recording` | `id`, `name`, `status` (`active` or `stopped`), `started_at`, `stopped_at` or null, `athlete_ids`, `athletes` (roster snapshot) |
| `RecordingDetail` | `Recording` plus `recorded`: per athlete, `physiological` and `tracking` (`RecordedStream`: `sample_count`, `earliest_timestamp`, `latest_timestamp`) |
| `ActiveRecording` | `recording` or null |
| `StreamAvailability` | `max_samples`, `retained_count`, `retained_earliest`, `retained_latest`, `at_cap`, `selected_count`, `selected_first`, `selected_last`: what is retained for one stream against what one request selected from it. Carried as `availability` (`WindowAvailability`: `physiological`, `tracking`) on `AthleteWindow` and `AthleteSummary`, and as `EffortAvailability` (`movement`, `heart_rate.before/during/after`) on `EffortReview`. Read with the samples; no expected count, coverage fraction or reason for absence is implied; `at_cap` is the only evidence of removal. |

Endpoints, all under `/api/v1`:

| Method and path | Purpose |
|-----------------|---------|
| `GET /health` | `{status, service, version}` |
| `POST /athletes` | Register (201; 409 if the id exists, original kept) |
| `GET /athletes`, `GET /athletes/{id}` | List sorted by id / fetch one (404) |
| `POST /athletes/validate` | Validate an athlete payload without storing it |
| `POST /samples/physiological`, `POST /samples/tracking` (while a recording is active, a sample for one of its athletes is also written to the recording, same transaction) | Ingest one sample: validate, reject timestamps more than 5 s ahead of backend UTC (422), persist, echo. Registration is not required. |
| `POST /devices/status`, `GET /devices/{id}/status/latest` | Device status ingest (newest per device kept) and read |
| `GET /athletes/{id}/physiological/latest`, `.../tracking/latest` | Newest persisted sample (404 if none) |
| `GET /athletes/{id}/physiological/history?limit=60`, `.../tracking/history?limit=60` | Newest `limit` (1-300 per request, a page-size limit separate from the retention caps) samples, oldest to newest |
| `GET /athletes/{id}/window?seconds=60[&end=...]` | Both streams in the `seconds` (1-300) before `end`, plus `tracking_rules`. Without `end`: live, one captured backend `window_end`. With `end` (ISO 8601 with timezone, not after backend time, `+` encoded as `%2B`): recorded review ending at that exact instant. Invalid, naive or future `end` is a 422. |
| `GET /athletes/{id}/summary?seconds=60[&end=...]` | Descriptive metrics over the same window selection; ages are relative to `window_end` (in a review: seconds before the selected end) |
| `GET /athletes/{id}/effort-review?effort_start=...&effort_end=...` | Descriptive review of a manually selected effort (ISO timestamps with timezone, 1-120 s, end not in the future; 422 otherwise). Movement = tracking summary over the effort; heart rate for three non-overlapping periods; `follow_up_elapsed` says only whether 30 s have passed. No interpolation, extrapolation, recovery or fatigue inference. |
| `GET /athletes/{id}/live` | Coherent single-athlete snapshot on the team overview's selection: one captured instant, newest retained sample per stream at or before it, ages, `*_newest_ahead_seconds` for a stored sample not yet current, device associations with explicit skew. Registration not required (`registered: false`, never 404). The `latest` endpoints keep newest-stored semantics. |
| `GET /teams/{team_id}/live` | One snapshot of every athlete registered with the team id (sorted by jersey then id) with the newest retained sample per stream at or before one captured instant and its age (never negative; samples after the instant excluded). Unknown team: empty list. Each athlete also carries `devices`: the ids named by those newest samples (deduplicated) with each id's latest DeviceStatus report, age and `status_ahead_seconds` (explicit clock skew for a report timestamped after the captured instant; age 0, never negative, never presented as current), read in one query; a reported association, not a verified identity. |
| `GET /athletes/{id}/retained-samples.jsonl` | Download of every retained sample for the athlete, both streams, as replayable JSON Lines (`application/x-ndjson`, attachment filename, record-count headers): one consistent read, oldest first with physiological before tracking at equal instants, written as stored. 404 when neither stream has rows. A snapshot of retained rows, not an archive or backup. |
| `POST /recordings` | Start a recording for registered athletes (201); 422 naming unknown ids or an invalid selection, nothing created; 409 while another is active |
| `GET /recordings`, `GET /recordings/active`, `GET /recordings/{id}` | All recordings newest first; the active one or null; detail with roster snapshot and recorded counts and bounds per athlete and stream (404 unknown) |
| `POST /recordings/{id}/stop` | Stop at backend time now; an already stopped recording is returned unchanged |
| `GET /athletes/{id}/retained-data` | Per stream: rows currently retained, oldest and newest recorded timestamps (UTC), and that stream's cap; both streams under one read. Reads persisted rows; unknown ids report empty streams (200). Retained rows only: no coverage, session or eviction inference. |

Configuration (`app/core/config.py`, overridable via environment or `.env`; see `.env.example`):

| Setting | Default | Meaning |
|---------|---------|---------|
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Informational; uvicorn flags decide the bind address |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | Origins allowed to call the API |
| `DATABASE_PATH` | `data/athletrix.db` | SQLite file, relative to the start folder; created automatically |
| `DISTANCE_MAX_GAP_SECONDS` | `3.0` | Longest consecutive-sample interval counted (0 < v <= 60) |
| `DISTANCE_MIN_CONFIDENCE` | `0.5` | Minimum confidence at both ends of an interval; also echoed as `tracking_rules.min_confidence` in the window |
| `HIGH_SPEED_THRESHOLD_MPS` | `5.0` | Speed at or above which time and distance count as high-speed (demonstration value) |
| `PHYSIOLOGICAL_RETENTION_SAMPLES` | `300` | Newest physiological samples kept per athlete (positive integer; a count, not a time span) |
| `TRACKING_RETENTION_SAMPLES` | `300` | Newest tracking samples kept per athlete (positive integer; 3000 is about five minutes only at 10 Hz) |

Changing a cap: it is applied only by the insert-plus-trim write of an accepted
sample, for that athlete and stream. Startup never prunes or rewrites stored
rows, so a reduced cap takes effect for an athlete's stream at its next accepted
sample and a raised cap never brings trimmed rows back. Example profile:
physiological 300, tracking 3000.

Other fixed constants: `MAX_HISTORY_QUERY_LIMIT = 300` and `MAX_WINDOW_SECONDS = 300`
(`app/api/athletes.py`: page size of one history request and longest window; API
limits separate from the retention caps, deliberately unchanged by them),
`MAX_FUTURE_TIMESTAMP_SKEW = 5 s` (`app/core/clock.py`).

## 5. Persistence and retention

- One local SQLite file (`DATABASE_PATH`), Python's built-in `sqlite3`, WAL mode,
  5 s busy timeout, one connection per process shared behind a lock. Schema is
  created at startup. Supported setup: a single uvicorn process (`--reload` is fine).
- Persisted across restarts: registered athletes; each device's newest status;
  each athlete's newest physiological and tracking samples up to each stream's
  cap (default 300 each). "Latest" is simply the newest persisted row, so it
  survives restarts too.
- Each sample write (insert or equal-instant replacement plus trim to that
  stream's cap) is one short transaction. Equal timestamps: the latest arrival
  replaces the earlier one. Timestamps are stored as fixed-width UTC text;
  payloads keep the original offset. Opening a database prunes nothing.
- Retention is a sample count per stream, not a time span: 300 is about five
  minutes at 1 Hz and 30 s at 10 Hz; 3000 tracking samples is about five minutes
  only at 10 Hz. There is no match or season storage; the export is a snapshot
  of retained rows, not an archive.
- Explicit recordings live in `recordings`, `recording_athletes` and
  `recording_*_samples` tables in the same file (created on open, never
  pruned); an active recording is persisted state and stays active across
  restarts until stopped. While active, each accepted sample for a selected
  athlete is written to the recording in the same transaction as the rolling
  insert-plus-trim (`app/core/ingest.py`): membership is decided at ingestion,
  equal instants replace within the recording, recorded rows are never trimmed
  by the rolling caps, and a stopped recording is never written again.
  Recordings consume local disk until an explicit management feature exists.
- `data/` and `*.db*` are git-ignored. Tests never touch it: every test uses its
  own temporary file (`tests/conftest.py`); the fallback `DATABASE_PATH` folder
  the suite sets as a safeguard belongs to that test process and is removed
  when it exits.

## 6. Public mode versus engine mode (frontend)

The page resolves a mode on load (`resolveEngineMode` in `index.html`):

| Mode | When | Behaviour |
|------|------|-----------|
| `engine` | hostname is `localhost`, `127.0.0.1`, `[::1]` or `*.localhost`; or `?engine=1`; or `engineMode: "engine"` in `window.ATHLETRIX_CONFIG` | Shows the athlete selector, the Live / Recorded review bar, a Live team overview (one `teams/{id}/live` request about every 2 s in Live mode, tiles per registered athlete with per-stream recency; hidden in Recorded mode) and four engine-connected cards (Live Heart Rate and Live Tracking from one `athletes/{id}/live` snapshot per second with engine-provided ages, Last 60 seconds, Recent movement and heart rate) and polls `apiBase` (default `http://127.0.0.1:8000`). Each card's source badge is derived from the data it shows: the two latest-reading cards from that sample's own `source`/`device_id`, the summary and timeline cards from their response's `sources` breakdown ("Reported source: simulated", "Reported source: wearable", "Mixed sources: camera 1, wearable 3, not reported 2", "Source not reported", "No samples", "Source information unavailable"); badges are cleared on athlete, mode and review-time changes. Browser-generated demonstration panels keep their own "Browser demo · fictional data" labels. In Recorded mode the two live cards are hidden, the summary and timeline are fetched with `end=` only on Review, athlete change or Refresh (no timer), and both are badged "Recorded-time review". A **Retained data** block (Recorded mode only) fetches `retained-data` on **Find retained data** (never on a timer), shows each stream's count and UTC range, and **Review latest retained** opens the 60 s review ending at the newest retained timestamp across the non-empty streams; the block is cleared on athlete or mode change and late responses are dropped. **Download retained samples** in the same block fetches `retained-samples.jsonl` on click and saves it through a browser download link (nothing uploaded); a 404 is shown as "no retained samples", errors can be retried, and a response for an earlier athlete, mode or request is never saved. In Recorded mode a **Selected effort review** card calls `effort-review` only on Review effort / Refresh and shows the engine's movement figures, the before/during/after heart-rate comparison with per-period bounds, first/last samples and sources, the applied rules and the follow-up flag with its "elapsed time is not completeness" wording; results are invalidated on input edits, athlete and mode changes. The timeline card's **Select an effort from the timeline** toolbar (Recorded mode) copies a clicked or list-chosen sample's recorded timestamp text into that form, shades the selected span on both charts as a user selection, and is cleared on athlete, window or mode change; picking never requests anything. A **Compare two selected efforts** card keeps up to two explicitly saved effort-review snapshots (deep copies, page memory only) side by side with an arithmetic B − A column and context notes on duration, rules, follow-up and sample availability; no ranking, scoring or baseline; cleared on athlete change or return to Live. |
| `public` | any other hostname (the published site), or `?engine=0`, or `engineMode: "public"` | Shows an explanation card with the local startup steps. **No request is made to the engine.** |

Polling: live cards every 1 s, summary and timeline every 5 s, only while the
dashboard view is visible. Each request carries the selection generation it was
made for; responses from an earlier generation (a previous athlete) are dropped.
The rest of the dashboard is a browser-only fictional demo, labelled with an amber
banner, and is never substituted for engine data.

## 7. Implemented calculations and their assumptions

All of these are descriptive and cover only the samples available in the window
(bounded by transmission gaps and each stream's retention cap).

- **Window selection.** `window_end` is backend UTC time read once (live) or
  the `end` query parameter (recorded review; timezone-aware, not after backend
  time, which is still read once for that check); `window_start = window_end -
  seconds`; samples included when `window_start <= timestamp <= window_end`.
  Both modes share the same code path, boundaries, ordering, `tracking_rules`
  and calculations. Groups by recorded time only: no device-clock correction,
  no effort-to-response lag modelling. Summary ages are relative to
  `window_end`, so in a review they are seconds before the selected end.
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
- **Recorded replay (`scripts/replay_samples.py`).** Validates every line of
  a JSON Lines file with the API's own models before sending anything (errors
  reported per line and field); posts records in chronological order of
  recorded timestamp, input order for equal timestamps, each to its existing
  sample endpoint; paces requests by recorded gaps divided by `--speed` on a
  monotonic clock without altering the timestamps sent; stops at the first
  HTTP or connection failure and reports submitted / failed / not sent counts.
  Preserves timestamps, sources, ids, nulls, zeros and confidences; never
  rebases to the current time, so old recordings stay out of the rolling
  60 s window and are subject to the newest-timestamp rules and the per-stream caps.
- **Heart-rate CSV conversion (`scripts/convert_heart_rate_csv.py`).** One
  `physiological` record per CSV row with the given athlete, device and
  source; heart rate taken as bpm as written (blank = null, never 0);
  timestamps must be ISO 8601 with an explicit timezone and are copied as
  written (naive values, numeric epochs and invalid dates are rejected);
  every row validated with `PhysiologicalSample` before anything is written;
  errors carry CSV row numbers and field names; existing output is never
  overwritten and a failure leaves no partial file. Nothing is inferred
  from the file name. The other `PhysiologicalSample` fields
  (`breathing_rate_bpm`, `hrv_rmssd_ms`, `hrv_sdnn_ms`, `activity_level`,
  `posture`, `skin_temperature_c`, `spo2_percent`, `signal_quality`) are
  written only when mapped with their explicit column flags, as written in
  the model's units (blank = null, zeros kept, exact posture values only,
  conflicting or ambiguous mappings rejected); unmapped fields stay absent.
  They are stored and echoed; no metric or interpretation is derived from
  them.
- **Tracking CSV conversion (`scripts/convert_tracking_csv.py`).** One
  `tracking` record per CSV row with the given athlete id; x/y positions
  (metres), speed (m/s), acceleration (m/s²) and confidence (0 to 1) taken as
  written with no unit or scale conversion; every measurement required
  (blank cells are errors: confidence is never defaulted to 1, speed and
  acceleration are never derived from positions); real zeros and negative
  acceleration kept; same timestamp rule, whole-file `TrackingSample`
  validation, row/column errors, dry run, atomic output and no-overwrite
  protection as the heart-rate adapter. `--source` is required and
  `--device-id` optional; both are written as given and never inferred.
  Reuses the heart-rate adapter's timestamp, CSV-reading and output helpers.
- **Source breakdown (`app/core/sources.py`).** For the samples selected into
  a window, per stream: how many reported each source category and how many
  reported none. Shared by the window response (`sources`) and both summary
  streams (`sources`); every selected sample counts once, null heart rates
  included, so the counts equal `sample_count`. The dashboard derives its
  card labels from these counts and from each latest sample's own `source`
  ("Reported source: wearable", "Mixed sources: ...", "Source not
  reported"); it never says "real" or "verified".
- **Effort review (`app/core/effort.py`).** For a caller-selected effort:
  the tracking summary above over `[effort_start, effort_end]` (same rules,
  fraction relative to the effort length) and, for the heart-rate periods
  before `[start-10 s, start)`, during `[start, end]` and after
  `(end, end+30 s]` capped at the captured backend time: sample count,
  non-null heart-rate count, arithmetic mean, peak, first/last timestamps,
  largest gap and source breakdown. Half-open boundaries where periods meet,
  so no sample is counted twice. Null heart rates stay missing; nothing is
  interpolated; recorded timestamps are used without clock correction or
  lag estimation. 10 s and 30 s are named engineering defaults.
- **Simulator profiles.** `sine`: one rise-and-fall of heart rate (110 to 150 bpm)
  and straight-line speed (peak 8 m/s), confidence 1.0. `effort-rest`: repeating
  40 s cycle (accelerate 10 s to 6 m/s, hold 10 s, slow 10 s, stand 10 s), position
  the exact integral of speed, heart rate a toy delayed first-order response.
  Fictional; not physiological models.

## 8. Known limitations and unfinished capabilities

Implemented today is listed above; the following are **not** implemented:

- No live device or camera connections. Data comes from the simulator, manual
  POSTs, recorded files in Athletrix's own JSON Lines interchange format
  (`scripts/replay_samples.py`), or CSV files converted by
  `scripts/convert_heart_rate_csv.py` (heart rate) and
  `scripts/convert_tracking_csv.py` (position/motion). Both converters are
  generic adapters, not vendor support: they take values already in the
  model's units (bpm; metres, m/s, m/s², confidence 0 to 1) plus ISO
  timestamps with a timezone, so a real export may need its columns named
  explicitly, a unit or timestamp reformatting step, or another adapter.
  The heart-rate adapter can map the model's other optional measurements
  with explicit flags; live hardware is **not** integrated.
- `source` and `device_id` are metadata supplied by whoever sent the sample.
  The engine validates the category, stores and counts it, and echoes it; it
  does not verify it, and a source label is not a claim about authenticity or
  sensor accuracy. Tracking rows stored before the fields existed (and any
  sender that omits them) read as "not reported", never as camera, wearable
  or simulated. No live hardware integration exists.
- The effort review describes a period the caller chose. It does not detect
  sprints or efforts, does not validate the selection beyond its length and
  timing, and produces no recovery rate, baseline comparison, fatigue label
  or recommendation; a heart-rate difference between its periods is not
  evidence of recovery or of a change in condition. Its 10 s / 30 s periods
  are engineering defaults, `follow_up_elapsed` only reports elapsed time,
  and the retention caps can leave any period partial or empty.
- The single-athlete live cards and the team overview share one selection
  (`app/core/live.py`): a sample timestamped ahead of backend time stays
  stored and is reported as "ahead", never shown as current. The plain
  `latest` endpoints deliberately keep returning the newest stored sample.
- Device status is associated with an athlete only through the device id
  the athlete's newest sample reported: a reported association, not a
  verified identity or an assignment. A status report describes its own
  moment ("last reported connected" with its age); the engine and
  dashboard never infer connection or disconnection from samples arriving
  or stopping, and the 60 s "outdated" mark is a display rule.
- The demo launcher is a local convenience: it serves a copy of the
  frontend with `python -m http.server`, feeds fictional data for a bounded
  time and then stops sending; it is not a deployment and changes no hosting
  or DNS setting. Only the engine dashboard was verified with external
  origins blocked; the marketing pages were not.
- A retention cap is a row count per stream, not a duration: under the
  default 300 a 10 Hz tracking stream keeps about 30 s while a 1 Hz
  physiological stream keeps 300 s, and a tracking cap of 3000 keeps about
  five minutes only at 10 Hz. The caps are configurable per stream but
  remain bounded local storage, not an archive. The validation report shows
  each stream's cap, retained rows and timestamp span so the difference is
  visible. A synthetic 10 Hz tracking stream is not a camera integration.
- The retention comparison runs the same scenario under two cap profiles and
  re-implements the documented distance and high-speed rules to cross-check
  the effort review; it is a consistency check of this software on one
  machine, not a ground truth about any athlete and not a capacity claim.
  Under the default caps an effort older than the newest 300 tracking rows
  has no movement samples and a null distance, which the availability
  metadata explains; a larger cap only changes how many rows are kept.
- The team-scale validation tool measures a short run on one machine with
  fictional athletes. Its latency figures are client-side round trips with
  no pass threshold, and 15 s runs do not establish sustained match-length
  behaviour; a timed-out request leaves acceptance unknown and is reported
  as such.
- Recordings are a first version: one active at a time, no deletion, no
  upload, no automatic start, no management of disk use; an active recording
  survives a restart until stopped explicitly.
- The `availability` block and the dashboard's Available data lines describe
  retained rows against a request's selection only. They never call a period
  complete, never derive an expected count or coverage from a rate, never
  interpolate, and claim removal only when a stream is at its cap; below the
  cap, "not retained" may mean never received or removed earlier.
- The retained-samples export is a snapshot of what is stored at that
  moment for one athlete: every retained row under each stream's cap,
  written as stored, with the caps in its headers. It is
  not a recording archive or a database backup; rows the cap has already
  removed cannot be recovered, and the roster, device status and
  configuration are not part of it.
- Recorded clocks are not synchronized or corrected. A heart-rate file and a
  tracking file replayed for the same athlete line up on the dashboard only
  as far as their recorded timestamps already agree; the fictional fixtures
  align because they were written that way.
- The replay script posts one request at a time with no retries and no resume
  point; after a failure, rerunning the whole file is the supported recovery
  (equal-timestamp samples are replaced, so it is safe). Only the file's own
  samples are sent: it does not register athletes or report device status.
- No authentication, authorisation or multi-user handling; the engine is meant to
  run on a local sideline machine behind CORS only.
- Single-process SQLite only; multiple backend processes on one file are unsupported.
- Retention is a per-stream sample count (default 300 each, configurable); no
  match/season storage. Recorded review (`end=` and the dashboard's Recorded mode) can
  therefore show only what is still retained: it is a review of retained
  samples, not a recording archive, and older records may already be gone.
- Retained-data lookup (`retained-data`, dashboard Find retained data)
  describes rows present at the moment of the request only: count, oldest and
  newest UTC timestamps per stream. It is not a session list, does not show
  continuous coverage between the bounds, and cannot say whether an empty
  stream was never sent or has been trimmed. The streams are independent, so
  "Review latest retained" may open a window holding only one of them.
- Recorded review uses a fixed 60 s duration in the dashboard (the API accepts
  1-300 s), one window at a time, with a typed timestamp; there is no
  recording index, scrubber or list of available time ranges.
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
# engine: full suite, isolated temporary databases (479 tests as of this handoff)
cd "/Users/parthgupta/Athletrix Vision Synopsis 2026" && source .venv/bin/activate && pytest -q

# engine: window carries the applied rule (backend running)
curl -s "http://127.0.0.1:8000/api/v1/athletes/athlete-1/window?seconds=60" | python3 -m json.tool | grep -A2 tracking_rules

# engine: replay the fictional example into an isolated backend (own database, own port), then read it back
DATABASE_PATH=/tmp/athletrix-replay-check.db uvicorn app.main:app --port 8010      # separate terminal
python scripts/replay_samples.py scripts/examples/fictional_replay.jsonl --dry-run --url http://127.0.0.1:8010
python scripts/replay_samples.py scripts/examples/fictional_replay.jsonl --url http://127.0.0.1:8010 --speed 4
curl -s "http://127.0.0.1:8010/api/v1/athletes/athlete-1/tracking/history?limit=10"  # expect 3 samples at 2026-10-20T10:00:00Z..02Z
curl -s "http://127.0.0.1:8010/api/v1/athletes/athlete-1/summary?seconds=60&end=2026-10-20T10:00:02Z"  # expect 2 phys (mean 113.5), 3 tracking, 2.5 m
curl -s "http://127.0.0.1:8010/api/v1/athletes/athlete-2/summary?seconds=60&end=2026-10-20T10:00:02Z"  # expect 1 of 2 heart rates (98), 1 tracking, distance null

# engine: CSV adapter round trip into the same isolated backend
python scripts/convert_heart_rate_csv.py scripts/examples/fictional_heart_rate.csv /tmp/hr.jsonl --athlete-id athlete-3 --device-id fictional-hr-csv-1 --source simulated
python scripts/replay_samples.py /tmp/hr.jsonl --url http://127.0.0.1:8010
curl -s "http://127.0.0.1:8010/api/v1/athletes/athlete-3/summary?seconds=60&end=2026-10-20T10:00:04Z"  # expect 5 samples, 4 heart rates, mean 116.375, peak 120.5

# engine: both CSV adapters for one athlete, reviewed together (same isolated backend)
python scripts/convert_tracking_csv.py scripts/examples/fictional_tracking.csv /tmp/trk.jsonl --athlete-id athlete-3
python scripts/replay_samples.py /tmp/trk.jsonl --url http://127.0.0.1:8010
curl -s "http://127.0.0.1:8010/api/v1/athletes/athlete-3/window?seconds=60&end=2026-10-20T10:00:04Z"   # expect 5 + 5 samples, bounds 09:59:04Z..10:00:04Z
curl -s "http://127.0.0.1:8010/api/v1/athletes/athlete-3/summary?seconds=60&end=2026-10-20T10:00:04Z"  # expect tracking: peak 6.0, 10.5 m over 2.0 s, 2 excluded, high speed 1.5 s / 8.25 m
curl -s "http://127.0.0.1:8010/api/v1/athletes/athlete-3/summary?seconds=60"                            # expect 0 samples in both streams (historical)
curl -s "http://127.0.0.1:8010/api/v1/athletes/athlete-3/retained-data"                                 # expect 5 + 5 rows, both ranges 10:00:00Z..10:00:04Z, cap 300
curl -fsS -o /tmp/athlete-3.jsonl "http://127.0.0.1:8010/api/v1/athletes/athlete-3/retained-samples.jsonl"  # expect 10 lines, oldest first
python scripts/replay_samples.py /tmp/athlete-3.jsonl --dry-run                                            # expect a valid file: 5 physiological, 5 tracking
curl -s "http://127.0.0.1:8010/api/v1/athletes/athlete-3/effort-review?effort_start=2026-10-20T10:00:01Z&effort_end=2026-10-20T10:00:04Z"
#    expect movement 4 samples, peak 6.0, 10.5 m over 2.0 s (1 excluded), high speed 1.5 s / 8.25 m; heart rate before 1 sample (112),
#    during 4 samples / 3 measurements mean 117.833 peak 120.5, after 0 samples; follow_up_elapsed true
#    dashboard: Recorded -> Selected effort review -> enter 2026-10-20T10:00:01Z and 2026-10-20T10:00:04Z -> Review effort -> same figures
#    or: Recorded -> Review 2026-10-20T10:00:04Z -> Pick effort start -> click the 10:00:01Z point -> click the 10:00:04Z point -> Review effort
# dashboard against that backend (serve a copy of index.html with apiBase pointed at :8010): Recorded -> Find retained data ->
# expect both streams "5 retained · 2026-10-20T10:00:00Z → 2026-10-20T10:00:04Z UTC"; Review latest retained -> the review above

# frontend: open http://localhost:3000/tests/timeline_confidence.test.html (expect all tests passing), then on the
# dashboard switch Review to Recorded, enter 2026-10-20T10:00:02Z, click Review (after replaying the example)

# frontend: timeline behaviour tests (frontend served on port 3000; no engine needed)
open http://localhost:3000/tests/timeline_confidence.test.html      # expect "53 tests, 53 passed"
# retention comparison on isolated backends (about three minutes; exit 0 means every check passed, unknown is not a pass)
python scripts/compare_retention.py --athletes 20 --duration 60 --physiological-rate 1 --tracking-rate 10 --tracking-cap 3000

# frontend: public mode locally (must make no engine requests; check the Network tab)
open "http://localhost:3000/?engine=0#demo"
```

## 10. Recommended next milestone

**Effort comparison and team-scale validation.** The effort review is on
the dashboard (done 2026-10-26) with typed bounds and timeline point
selection (done 2026-10-26).
With both CSV adapters, the replay, recorded review,
the retained-data lookup and source-derived card labels (all done
2026-10-25), a converted session can be found and reviewed end to end on
the dashboard without the simulator, and every engine-connected card says
where its samples were reported to come from. The remaining gap in the data path is
that the heart-rate adapter maps one column only, while `PhysiologicalSample`
already accepts optional `signal_quality`, `posture`, `activity_level` and
the other documented fields. Extending the adapter with explicit, optional
column options for those fields (same no-guessing, blank-means-null,
whole-file validation rules; never derived or filled in) would let a
recorded export carry its own quality information into the review. Keep it
descriptive: no interpretation of the extra fields, no clock correction
between files, no scores or baselines.
