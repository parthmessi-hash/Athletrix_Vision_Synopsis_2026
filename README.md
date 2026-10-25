# Athletrix Engine

Backend/engine for the Athletrix real-time multi-sport athlete monitoring platform.
The frontend lives in the separate `Athletrix_Web` repository and talks to this service over HTTP.

## Stack

- Python 3.11+
- FastAPI (HTTP API, automatic OpenAPI docs)
- Uvicorn (ASGI server)
- pydantic-settings (configuration from environment / `.env`)
- SQLite via Python's built-in `sqlite3` (local persistence, no extra dependency)

## Layout

```
app/
  main.py            # app factory, CORS, router registration
  core/config.py     # settings (env vars / .env)
  api/router.py      # aggregates routers under /api/v1
  api/health.py      # GET  /api/v1/health
  api/athletes.py    # POST /api/v1/athletes (register), GET /api/v1/athletes, GET /api/v1/athletes/{id}
                     # POST /api/v1/athletes/validate
                     # GET  /api/v1/athletes/{id}/physiological/latest, /tracking/latest
                     # GET  /api/v1/athletes/{id}/physiological/history?limit=60, /tracking/history?limit=60
                     # GET  /api/v1/athletes/{id}/window?seconds=60, /summary?seconds=60
  api/samples.py     # POST /api/v1/samples/physiological, /api/v1/samples/tracking
  api/devices.py     # POST /api/v1/devices/status, GET /api/v1/devices/{id}/status/latest
  models/            # provider-agnostic Pydantic models (units documented in each file)
  scripts/           # register_demo_roster.py, simulate_athlete.py (local demo helpers)
  db.py              # SQLite storage layer (single source of truth; schema created at startup)
  store/             # SQLite-backed athlete registry, sample histories (latest = newest row), device status
tests/
```

## Data models

All models live in `app/models/` and are vendor-agnostic. Timestamps must be
ISO 8601 with a timezone (UTC recommended). Unknown fields are rejected.

| Model                 | Purpose                                   |
|-----------------------|-------------------------------------------|
| `Athlete`             | Identity: id, name, jersey, sport, team   |
| `PhysiologicalSample` | HR, breathing, HRV, SpO2, temp, posture   |
| `TrackingSample`      | Position (m), speed (m/s), accel (m/s²)   |
| `DeviceStatus`        | Connection, battery %, signal quality     |
| `AthleteWindow`       | Both streams for one athlete, last N secs |
| `AthleteSummary`      | Counts, HR mean/peak, peak speed, times   |

`POST /api/v1/athletes` registers an athlete (201; 409 if the id exists,
keeping the original). `GET /api/v1/athletes` lists
them sorted by id and `GET /api/v1/athletes/{id}` fetches one (404 if not
registered). Registration is not yet required for sample ingestion, so the
simulator works without it.

The ingest endpoints validate the payload, check the timestamp, persist in
one short SQLite transaction and echo the payload back. A sample's "latest"
is simply the newest row of its persisted history (ties go to the most
recently received; older samples are stored in history but do not become
the latest). Device status keeps the newest status per device with the same
tie rule; older statuses are accepted but not stored. The
`/athletes/{id}/.../latest` and `/devices/{id}/status/latest` endpoints read
these. Timestamps are compared in UTC, so `14:00:00+02:00` and `12:00:00Z`
are the same instant.

Physiological and tracking samples are kept in separate per-athlete
histories, served by `GET /api/v1/athletes/{id}/physiological/history?limit=60`
and `GET /api/v1/athletes/{id}/tracking/history?limit=60` (limit 1 to 300,
newest samples returned oldest-to-newest, empty list if none). History is
kept sorted by timestamp, accepts out-of-order arrivals, and replaces an
equal-timestamp entry with the latest arrival. Each athlete keeps only the
newest `MAX_HISTORY_SAMPLES` (300, in `app/store/history.py`) per stream, and
that bounded history is what is persisted: 300 samples is a count, not a
time window (about five minutes at one sample per second, less at a higher
rate), and this is not match or season storage.

`GET /api/v1/athletes/{id}/window?seconds=60` (1 to 300) returns both streams
for the last `seconds`, read from those histories. Backend UTC time is
captured once as `window_end`; `window_start` is derived from it, and
samples are included when `window_start <= timestamp <= window_end`, oldest
to newest. Samples timestamped after `window_end` are excluded even though
ingestion accepts up to 5 s of clock skew. Because each history keeps at most
300 samples, a window can return less than its full span. The window groups
samples by recorded time only: it does not correct device clock differences
or account for the lag between effort and physiological response.

`GET /api/v1/athletes/{id}/summary?seconds=60` uses the same window selection
(same `seconds` validation, one captured `window_end`, stores read directly)
and returns descriptive metrics instead of the samples: per stream the sample
count, first/last sample timestamps, `latest_sample_age_seconds`
(`window_end` minus the newest sample timestamp; null if no samples) and
`max_inter_sample_gap_seconds` (largest gap between consecutive sample
timestamps; null with fewer than two samples), plus for physiological the
count of non-null heart rates with their mean and peak, and for tracking the
peak `speed_mps`. The age and gap values describe recorded sample timestamps
only: the maximum gap covers consecutive samples that were available and does
not prove the requested window was fully covered, and no interpolation,
thresholds or labels are applied. Null heart rates are ignored; with no measurements the metrics
and timestamps are null and counts are 0. A peak speed of 0.0 is a real value.
The heart-rate mean is an arithmetic average of the measurements received,
not a time-weighted average. All of these describe the samples available in
memory: transmission gaps and the 300-sample history caps can limit coverage.
No condition scores, baselines or recovery claims are derived. A sample whose timestamp is
more than 5 seconds ahead of the backend's UTC clock is rejected with 422 and
the stores are left unchanged (`MAX_FUTURE_TIMESTAMP_SKEW` in
`app/core/clock.py`, an initial clock-skew allowance). Nothing is persisted
across restarts yet.

## Storage

Everything the API stores lives in one local SQLite file, by default
`data/athletrix.db` relative to the folder you start the backend from
(`DATABASE_PATH` in `.env`, see `.env.example`). The file, its folder and the
tables are created automatically at startup, and the `data/` folder plus
`*.db`, `*.db-wal`, `*.db-shm` and `*.db-journal` files are git-ignored.

What persists across restarts:

- registered athletes
- each device's latest status
- each athlete's newest 300 physiological samples (the latest sample is the
  newest of these)
- each athlete's newest 300 tracking samples (likewise)

SQLite is the only source of truth: there is no separate in-memory copy.
Each sample write (insert or equal-instant replacement plus the trim to 300)
is one short transaction, so a successful POST means it was committed.
Timestamps are stored as fixed-width UTC text so equivalent instants with
different offsets compare equal, while each sample's payload keeps the
timestamp exactly as it was sent.

Supported local server configuration: one uvicorn process (the default;
`--reload` is fine). One SQLite connection is shared across its worker
threads behind a lock, in WAL mode with a 5 s busy timeout. Multiple backend
processes on the same file are not part of this milestone. To run an
isolated demo, point `DATABASE_PATH` at a different file.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open:

- http://127.0.0.1:8000/api/v1/health
- http://127.0.0.1:8000/docs (interactive API docs)

## Configuration

Copy `.env.example` to `.env` to override defaults. `CORS_ORIGINS` must include the
origin the `Athletrix_Web` dev server runs on (e.g. `http://localhost:3000`).

## Demo roster

Register the two fictional demo athletes (`athlete-1` Alex Example, football;
`athlete-2` Sam Sample, basketball) through the athlete API so the frontend's
athlete selector has something to show:

```bash
python scripts/register_demo_roster.py          # or --url http://host:port
```

It is safe to rerun: identical existing athletes are left alone, and an
athlete that exists with different details is reported as a conflict and
never overwritten (exit status 1). The registry is persisted in the SQLite
database, so this is normally a one-time step per database file. Sample
ingestion does not require registration.

## Demo simulator

With the backend running, send 60 seconds of fictional heart-rate data for
`athlete-1` (one sample per second) to the physiological endpoint:

```bash
python scripts/simulate_athlete.py                  # heart rate only
python scripts/simulate_athlete.py --with-tracking  # heart rate + straight-line movement
```

All simulator output is fictional demo data. Optional flags: `--seconds 10`
to run shorter, `--url http://host:port` for a different backend,
`--athlete-id` (default `athlete-1`) and `--device-id` (default `simulator-1`)
to choose the identifiers used in the samples. With
`--with-tracking`, each second also sends a `TrackingSample` with the same
timestamp: speed rises and falls, `x_position` accumulates accordingly,
`y_position` stays at 0, and `acceleration_mps2` goes negative while slowing.
While it runs, the latest samples are visible at:

- http://127.0.0.1:8000/api/v1/athletes/athlete-1/physiological/latest
- http://127.0.0.1:8000/api/v1/athletes/athlete-1/tracking/latest

To simulate two athletes at once, run one instance per terminal with
different identifiers (no registration needed; the backend keeps each
athlete's latest readings, histories and summaries separate):

```bash
# Terminal A
python scripts/simulate_athlete.py --athlete-id athlete-1 --device-id simulator-1 --with-tracking

# Terminal B
python scripts/simulate_athlete.py --athlete-id athlete-2 --device-id simulator-2 --with-tracking
```

Then compare, for example,
http://127.0.0.1:8000/api/v1/athletes/athlete-1/summary?seconds=60 with
http://127.0.0.1:8000/api/v1/athletes/athlete-2/summary?seconds=60.

## Running with the Athletrix_Web frontend

The frontend is a separate static site in the `Athletrix_Web` repository. Its
dashboard loads the athlete roster from this backend and polls the selected
athlete's latest samples and 60-second summary. Full local demo, each line in
its own terminal from this folder unless noted (activate `.venv` first):

```bash
uvicorn app.main:app --reload                                   # 1. backend
python scripts/register_demo_roster.py                          # 2. roster (once per database file; safe to rerun)
python scripts/simulate_athlete.py --athlete-id athlete-1 --device-id simulator-1 --with-tracking   # 3a
python scripts/simulate_athlete.py --athlete-id athlete-2 --device-id simulator-2 --with-tracking   # 3b
python3 -m http.server 3000                                     # 4. frontend, from the Athletrix_Web folder
```

Then open http://localhost:3000, click **Live Demo** in the top navigation,
and use the **Athlete** selector above the cards to switch between the two
athletes. The roster, device statuses and the newest 300 samples per athlete
per stream survive backend restarts (see Storage). See
`Athletrix_Web/README.md` for details.

## Tests

```bash
pytest
```
