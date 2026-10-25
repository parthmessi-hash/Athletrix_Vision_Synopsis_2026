# Athletrix Engine

Backend/engine for the Athletrix real-time multi-sport athlete monitoring platform.
The frontend lives in the separate `Athletrix_Web` repository and talks to this service over HTTP.

New to the project or starting a fresh session? Read [PROJECT_STATUS.md](PROJECT_STATUS.md)
first: it is the concise handoff (purpose, repositories, exact commands, endpoints,
calculations, limitations and the next milestone).

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
  scripts/           # register_demo_roster.py, simulate_athlete.py (local demo helpers),
                     # replay_samples.py (recorded JSONL replay) + examples/fictional_replay.jsonl
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
| `AthleteWindow`       | Both streams for one athlete, last N secs, plus `tracking_rules` |
| `TrackingRules`       | Applied `min_confidence` (= `DISTANCE_MIN_CONFIDENCE`) |
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

The window response is self-contained: it also carries a small
`tracking_rules` block, `{"min_confidence": 0.5}` by default, echoing the
`DISTANCE_MIN_CONFIDENCE` setting that the distance and high-speed estimates
apply (there is no second confidence default). A consumer can therefore
classify the tracking samples of a response from that response alone,
without waiting for the summary endpoint. Samples below the rule are still
returned unchanged; nothing is filtered server-side. Comparing a sample's
`tracking_confidence` with this rule only reflects the confidence the
tracking source reported: it does not validate the accuracy of the
underlying speed or position measurement.

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
not a time-weighted average.

The tracking summary also carries a gap-aware distance estimate:
`estimated_distance_m`, `included_duration_seconds`,
`included_window_fraction`, `excluded_interval_count` and the applied
`distance_rules`. Consecutive tracking samples in the window form
intervals; an interval counts only when its elapsed time is positive and at
most `DISTANCE_MAX_GAP_SECONDS` (default 3 s) and both endpoint
`tracking_confidence` values are at least `DISTANCE_MIN_CONFIDENCE` (default
0.5). Each counted interval adds `(v_prev + v_cur) / 2 * elapsed` metres (the
trapezoidal rule, which assumes speed changes linearly between the two
samples). Rejected intervals are counted once and contribute nothing: bad
samples are not dropped and bridged across, and nothing is extrapolated
before the first sample, after the last, or over an excluded interval. The
estimate is null when no interval was eligible and a real 0.0 when the
eligible intervals were stationary; the fraction is included duration over
the requested window length. Both rules are adjustable engineering defaults
set in `.env`, not validated sport-specific standards, and the estimate
covers only the eligible part of the window, bounded by the 300-sample
history cap.

Over exactly the same eligible intervals, the summary also reports
`high_speed_duration_seconds` and `high_speed_distance_m`: the time and
distance spent at or above `HIGH_SPEED_THRESHOLD_MPS` (default 5.0 m/s, a
demonstration value that is not a validated standard for every sport or
athlete; the applied value is echoed in `distance_rules`). Within an
interval speed is assumed to change linearly; when it crosses the threshold
the crossing time is interpolated and only the qualifying part is integrated
(for 2 -> 6 m/s over 2 s with a 4 m/s threshold: crossing after 1 s,
duration 1 s, distance (4 + 6) / 2 x 1 = 5 m). An interval held at the
threshold counts in full; one that only touches it at an endpoint counts
zero. Both values are null when no interval was eligible and real zeros when
eligible intervals existed but none reached the threshold. Duration never
exceeds the eligible duration and distance never exceeds the total estimate.
These are descriptive workload estimates, not sprint detection, fatigue
classification or substitution advice. All of these describe the samples available: transmission gaps
and the history caps can limit coverage. No condition scores, baselines or
recovery claims are derived. A sample whose timestamp is
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
to choose the identifiers used in the samples, and `--profile` to pick the
scenario (default `sine`, the single rise-and-fall above).

### Effort-rest scenario

```bash
python scripts/simulate_athlete.py --profile effort-rest --with-tracking --seconds 120   # three full cycles
```

A repeating 40 s cycle: accelerate 0 to 6 m/s over 10 s (+0.6 m/s²), hold
6 m/s for 10 s, slow to 0 over 10 s (-0.6 m/s²), then stand still for 10 s.
Position is the exact integral of speed (120 m per cycle) and keeps
accumulating across cycles. Heart rate is a toy delayed response: the target
is `70 + 100 * speed(t - 5 s) / 6` bpm and the reported value follows it
through a first-order lag with a 5 s time constant, stepped once per second
(`hr(t) = hr(t-1) + (target - hr(t-1)) * (1 - e^(-1/5))`, `hr(0) = 70`). So
heart rate starts rising about 5 s after movement, peaks a few seconds after
the sustained phase ends, and declines through the stationary phase. Built
to demonstrate timing on the dashboard timeline; not a validated
physiological model. Equations are in `scripts/simulate_athlete.py`. With
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

## Replaying recorded samples

`scripts/replay_samples.py` sends previously recorded samples from a file into
a running backend, exactly as recorded. This is the first non-simulator data
path. It reads **Athletrix's normalized interchange format**: it does not
parse arbitrary vendor CSV exports and does not connect to wearable or camera
hardware; mapping a vendor export onto this format is a separate step.

### File format (JSON Lines)

One JSON object per line, UTF-8, with exactly the keys `type` and `sample`:

```
{"type": "physiological", "sample": {"athlete_id": "athlete-1", "timestamp": "2026-10-20T10:00:00Z", "source": "simulated", "device_id": "fictional-recorder-1", "heart_rate_bpm": 112}}
{"type": "tracking",      "sample": {"athlete_id": "athlete-1", "timestamp": "2026-10-20T10:00:00Z", "x_position": 10.0, "y_position": 5.0, "speed_mps": 0.0, "acceleration_mps2": 0.0, "tracking_confidence": 0.9}}
```

`sample` is validated with the same Pydantic models as the API
(`PhysiologicalSample`, `TrackingSample`): same required fields and ranges,
timestamps must carry a timezone, unknown fields are rejected. Blank lines are
ignored. Records may be in any order. Recorded timestamps, `source`,
`athlete_id`/`device_id`, null measurements, real zeros and the supplied
`tracking_confidence` are all preserved and sent as written; nothing is
invented, defaulted or shifted to the current time.
`scripts/examples/fictional_replay.jsonl` is a tiny fictional example: two
athletes, both streams, a null heart rate, a zero speed, a `+02:00` offset
and one out-of-order record.

### Commands

```bash
python scripts/replay_samples.py scripts/examples/fictional_replay.jsonl --dry-run     # validate + describe, send nothing
python scripts/replay_samples.py scripts/examples/fictional_replay.jsonl               # replay at recorded pace
python scripts/replay_samples.py recording.jsonl --url http://127.0.0.1:8010 --speed 4 # another backend, 4x faster
```

Flags: `--url` (default `http://127.0.0.1:8000`), `--speed` (positive, finite
playback multiplier, default 1), `--dry-run`. Exit codes: 0 all submitted or
valid dry run; 1 invalid or unreadable file; 2 the replay stopped on a failure.

### Playback behaviour

- The **whole file is validated first**; if any line is invalid, every error
  is listed with its line number and field (e.g. `line 3: sample.timestamp:
  Input should have timezone info`) and **no request is sent**.
- Records are posted one at a time in **chronological order** of recorded
  timestamp (compared as instants, so `12:00:01+02:00` equals `10:00:01Z`);
  records with equal timestamps keep their input order. Each goes to its own
  endpoint (`/api/v1/samples/physiological` or `/api/v1/samples/tracking`).
- Pacing uses the recorded timestamp differences divided by `--speed`, on a
  monotonic clock. Speed changes only the pacing, never the timestamps sent.
  If a request takes longer than the next gap, the next one is sent at once.
- The output starts with a `RECORDED DATA REPLAY` header, the athlete ids and
  per-stream counts, the recorded time range and the scheduled playback
  duration, then one line per request, then
  `Result: N submitted, N failed, N not sent`.
- On the first HTTP error (for example the backend's own 422 for a timestamp
  more than 5 s in the future) or connection failure the replay **stops** and
  reports exactly which line failed and how many records were never posted.
  Nothing is retried or skipped silently, and backend validation is never
  bypassed. For a timeout the backend's answer was not received, so that one
  sample may or may not have been stored. Rerunning the file is safe: the
  backend replaces the stored sample for an equal timestamp.

### Historical timestamps stay historical

Because timestamps are preserved, a replayed recording is stored at its
recorded times. Consequences, by design:

- The dashboard's rolling 60-second window and summary look at the last 60 s
  of backend time, so an old recording will not appear there; use the
  `history` and `latest` endpoints (or a window covering that time) to read it.
- The newest-timestamp rules still apply: a replayed sample becomes the
  "latest" only if it is newer than what is already stored, and the newest
  300 samples per athlete per stream are kept, so a long recording is trimmed
  to its newest 300 samples per stream.
- The script does **not** rebase recordings onto the current time. Shifting
  timestamps would make recorded data look live and break the "recorded time
  only" guarantee of the window and summary; if a demo needs live-looking
  data, use the simulator.

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

Every test runs against its own temporary SQLite file (see `tests/conftest.py`);
the development database under `data/` is never read or written by the suite.
The frontend's timeline behaviour tests are a static page in the `Athletrix_Web`
repository (`tests/timeline_confidence.test.html`, see its README).
