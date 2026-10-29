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
  core/timing.py     # receipt time and Server-Timing for every request (developer timing)
  core/configuration.py  # the configuration snapshot captured at recording start (models/configuration.py)
  api/router.py      # aggregates routers under /api/v1
  api/health.py      # GET  /api/v1/health, GET /api/v1/clock (engine UTC time for clock-offset estimates)
  api/athletes.py    # POST /api/v1/athletes (register), GET /api/v1/athletes, GET /api/v1/athletes/{id}
                     # POST /api/v1/athletes/validate
                     # GET  /api/v1/athletes/{id}/physiological/latest, /tracking/latest
                     # GET  /api/v1/athletes/{id}/physiological/history?limit=60, /tracking/history?limit=60
                     # GET  /api/v1/athletes/{id}/window?seconds=60, /summary?seconds=60
                     # GET  /api/v1/athletes/{id}/live (coherent snapshot: newest sample per stream at one captured instant)
                     # GET  /api/v1/athletes/{id}/retained-data (per-stream count and UTC range of retained rows)
                     # GET  /api/v1/athletes/{id}/effort-review?effort_start=..&effort_end=.. (manually selected effort)
                     # GET  /api/v1/athletes/{id}/retained-samples.jsonl (replayable export of retained samples)
  api/teams.py       # GET  /api/v1/teams/{team_id}/live (one snapshot of every registered athlete on a team)
  api/samples.py     # POST /api/v1/samples/physiological, /api/v1/samples/tracking
  api/devices.py     # POST /api/v1/devices/status, GET /api/v1/devices/{id}/status/latest
  models/            # provider-agnostic Pydantic models (units documented in each file)
  scripts/           # register_demo_roster.py, simulate_athlete.py (local demo helpers),
                     # replay_samples.py (recorded JSONL replay), demo_launcher.py (one-command isolated demo), validate_team_scale.py (isolated
                     # multi-athlete load check), convert_heart_rate_csv.py and
                     # convert_tracking_csv.py (CSV adapters), examples/fictional_replay.jsonl,
                     # examples/fictional_heart_rate.csv, examples/fictional_physiology.csv, examples/fictional_tracking.csv
                     # recording_package.py (inspect/import packages), local_data.py (inventory and scoped removal),
                     # verify_*.py (isolated end-to-end checks, including verify_evaluation_workflow.py),
                     # camera_calibrate.py, camera_to_samples.py, camera_synthetic_check.py, verify_camera_pipeline.py
camera_adapter/      # camera adapter prototype (outside the engine): observations, calibration, causal estimator, pipeline;
                     # synthetic_truth.py + synthetic_video.py (OpenCV) for the synthetic marker check
docs/                # individual-performance-design-note.md (proposed, not implemented)
  db.py              # SQLite storage layer (single source of truth; schema created at startup)
  store/             # SQLite-backed athlete registry, sample histories (latest = newest row), device status,
                     # recordings, saved periods, package import, removal plans (store/removal.py)
tests/
```

## Data models

All models live in `app/models/` and are vendor-agnostic. Timestamps must be
ISO 8601 with a timezone (UTC recommended). Unknown fields are rejected.

| Model                 | Purpose                                   |
|-----------------------|-------------------------------------------|
| `Athlete`             | Identity: id, name, jersey, sport, team   |
| `PhysiologicalSample` | HR, breathing, HRV, SpO2, temp, posture   |
| `TrackingSample`      | Position (m), speed (m/s), accel (m/s²); optional `source`, `device_id` |
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
and `GET /api/v1/athletes/{id}/tracking/history?limit=60` (limit 1 to 300 per
request, newest samples returned oldest-to-newest, empty list if none). History
is kept sorted by timestamp, accepts out-of-order arrivals, and replaces an
equal-timestamp entry with the latest arrival. Each athlete keeps only the
newest samples per stream up to that stream's retention cap
(`PHYSIOLOGICAL_RETENTION_SAMPLES` and `TRACKING_RETENTION_SAMPLES`, default
300 each; see "Retention caps" under Configuration), and that bounded history
is what is persisted. A cap is a count, not a time window (300 samples is about
five minutes at one sample per second and 30 s at ten per second), and this is
not match or season storage. The history `limit` is a page size for one
request, separate from the caps: a stream whose cap is larger than 300 is read
in full through the retained-samples export.

`GET /api/v1/athletes/{id}/window?seconds=60` (1 to 300) returns both streams
for the last `seconds`, read from those histories. Backend UTC time is
captured once as `window_end` (or, with the optional `end=` parameter, the
requested recorded instant is used instead; see "Reviewing a recorded
window" below); `window_start` is derived from it, and
samples are included when `window_start <= timestamp <= window_end`, oldest
to newest. Samples timestamped after `window_end` are excluded even though
ingestion accepts up to 5 s of clock skew. Because each history keeps at most
its stream's cap, a window can return less than its full span. The window groups
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
covers only the eligible part of the window, bounded by the tracking
retention cap.

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
- each athlete's newest physiological samples up to the physiological cap
  (default 300; the latest sample is the newest of these)
- each athlete's newest tracking samples up to the tracking cap (default 300)

SQLite is the only source of truth: there is no separate in-memory copy.
Each sample write (insert or equal-instant replacement plus the trim to that
stream's cap) is one short transaction, so a successful POST means it was
committed. Opening a database never prunes or rewrites it (see "Retention
caps" below for when a changed cap takes effect).
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
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open:

- http://127.0.0.1:8000/api/v1/health
- http://127.0.0.1:8000/docs (interactive API docs)

### Local binding (recommended setup)

Bind both local servers to the loopback address so only this computer can reach them:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000          # backend, from this folder (add --reload while editing code)
python3 -m http.server 3000 --bind 127.0.0.1               # frontend, from the Athletrix_Web folder
```

Check what is listening (macOS):

```bash
lsof -nP -iTCP:8000 -iTCP:3000 -sTCP:LISTEN
```

Both lines should show `127.0.0.1:8000` and `127.0.0.1:3000`. A `*:3000`, `*:8000` or `0.0.0.0` address means
the server accepts connections from other computers on the network, and the engine has no authentication. The
startup command controls the binding; the engine does not enforce it (`HOST`/`PORT` in `.env` are informational,
and `python3 -m http.server` without `--bind` listens on every interface). Ports stay configurable: use another
`--port` and point the frontend and `CORS_ORIGINS` at it. Open the dashboard at http://localhost:3000, which
resolves to the loopback address; to open http://127.0.0.1:3000 instead, add that origin to `CORS_ORIGINS`.
`scripts/demo_launcher.py` and the validation scripts already start their own servers on 127.0.0.1 only.

## Configuration

Copy `.env.example` to `.env` to override defaults. `CORS_ORIGINS` must include the
origin the `Athletrix_Web` dev server runs on (e.g. `http://localhost:3000`).

### Retention caps (`PHYSIOLOGICAL_RETENTION_SAMPLES`, `TRACKING_RETENTION_SAMPLES`)

Each stream keeps, per athlete, only its newest samples up to its own cap.
Both default to 300 and each must be a positive integer (anything else stops
the backend from starting with a validation error). The two are independent:
the physiological cap never applies to tracking rows or the other way round.
An example profile for a 10 Hz tracking source:

```bash
PHYSIOLOGICAL_RETENTION_SAMPLES=300    # 1 Hz heart rate: about five minutes
TRACKING_RETENTION_SAMPLES=3000        # 10 Hz tracking: about five minutes, only at 10 Hz
```

A cap is a row count, never a guaranteed time span: 3000 tracking samples is
about five minutes at 10 Hz, 50 minutes at 1 Hz and 1 minute 40 s at 30 Hz,
and a stream with gaps spans longer than its count suggests. This remains
bounded local storage, not a recording archive.

When a cap takes effect: a cap is applied only by the insert-plus-trim
transaction of an accepted sample, for that athlete and that stream. Starting
the backend never prunes or rewrites stored rows, so after reducing a cap an
athlete's stream keeps its existing rows until its next sample is accepted,
and then trims to the new cap; raising a cap applies to new samples at once
and never brings trimmed rows back. Ingestion, the latest endpoints and the
live snapshots are unchanged by the caps. The retained-data endpoint and the
export headers report the caps in force; the history `limit` (at most 300 per
request) and the window length (at most 300 s) are separate API limits that
the caps do not change. The validation tool and the demo launcher can start
their own isolated backend with other caps (`--physiological-cap`,
`--tracking-cap`) through that process's environment only; `.env` and the
development database are untouched.

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
hardware; mapping an export onto this format is a separate step (see
"Converting a heart-rate CSV" and "Converting a tracking CSV" below for the two
adapters that exist).

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

### Converting a heart-rate CSV (`scripts/convert_heart_rate_csv.py`)

A documented, provider-agnostic **CSV adapter** that turns a heart-rate CSV
into the interchange format above, one `physiological` record per row. It is
not support for any particular vendor's export: real exports may need a
different column mapping (`--timestamp-column`, `--heart-rate-column`), a
different timestamp form, or another adapter. It makes no network requests
and does not connect to any hardware.

```bash
# describe and validate without writing anything
python scripts/convert_heart_rate_csv.py scripts/examples/fictional_heart_rate.csv /tmp/hr.jsonl \
    --athlete-id athlete-1 --device-id fictional-hr-csv-1 --source simulated --dry-run
# convert (the output file must not already exist)
python scripts/convert_heart_rate_csv.py scripts/examples/fictional_heart_rate.csv /tmp/hr.jsonl \
    --athlete-id athlete-1 --device-id fictional-hr-csv-1 --source simulated
# replay it, then review the 60 s ending at its last timestamp
python scripts/replay_samples.py /tmp/hr.jsonl --dry-run
python scripts/replay_samples.py /tmp/hr.jsonl
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/summary?seconds=60&end=2026-10-20T10:00:04Z"
```

Rules of this first version:

- The CSV needs a header row. The timestamp and heart-rate columns are named
  explicitly (defaults `timestamp` and `heart_rate_bpm`) and each must appear
  exactly once; a missing or duplicated column is an error. Other columns
  are ignored.
- **Heart rate must already be in bpm.** No unit conversion is done. A blank
  cell becomes `null` (not reported), never `0`; a non-numeric, non-finite or
  out-of-range value (the `PhysiologicalSample` limits, 20 to 260) is an error.
- Timestamps must be ISO 8601 with a `T` separator and an explicit timezone
  (`2026-10-20T10:00:00Z`, `2026-10-20T12:00:00+02:00`). Naive timestamps,
  numeric epochs, invalid dates and other layouts are rejected; nothing is
  guessed. The timestamp text, offset included, is copied into the output.
- `--athlete-id`, `--device-id` and `--source` are required and apply to
  every row; `--source` must be one of `wearable`, `camera`, `manual`,
  `simulated` and is never inferred from the file. No other measurement is
  invented.
- The whole file is validated with the API's own model before anything is
  written. Errors are listed with their CSV row number (header = row 1) and
  field, and then nothing is written. The output is written through a
  temporary file in the same folder and an existing output file is never
  overwritten, so a failure leaves no partial output. Rows keep their CSV
  order; the replay orders them by timestamp when sending.

#### Optional measurements (explicit column flags)

The other fields of `PhysiologicalSample` can be mapped as well, each with
its own flag. A field is written **only** when its flag is given: a column
that happens to be named like a model field is ignored without the flag, so
nothing is guessed from the header. Values are taken as written in the
model's units and scales; no conversion is done.

| Flag | Field | Required unit or scale |
|------|-------|------------------------|
| `--breathing-rate-column` | `breathing_rate_bpm` | breaths per minute, 0 to 100 |
| `--hrv-rmssd-column` | `hrv_rmssd_ms` | milliseconds, 0 to 1000 |
| `--hrv-sdnn-column` | `hrv_sdnn_ms` | milliseconds, 0 to 1000 |
| `--activity-level-column` | `activity_level` | unitless, 0.0 to 1.0 |
| `--posture-column` | `posture` | exactly one of `standing`, `sitting`, `lying`, `walking`, `running`, `unknown` |
| `--skin-temperature-column` | `skin_temperature_c` | degrees Celsius, 20 to 45 |
| `--spo2-column` | `spo2_percent` | percent, 0 to 100 |
| `--signal-quality-column` | `signal_quality` | unitless, 0.0 to 1.0 |

Rules: a mapped column must exist exactly once in the header and no two
fields may share a column (conflicting mappings are rejected before any row
is read). A blank cell in a mapped column becomes `null`; a zero is kept
wherever the model allows it (for example `activity_level` 0.0 or
`hrv_rmssd_ms` 0). Unmapped fields are absent from the records, meaning not
reported. Non-numeric or non-finite values, values outside the model's
ranges and postures outside the list above are row errors, named with the
CSV column, and nothing is written. Whole-file validation, dry run,
no-overwrite protection and failure cleanup work exactly as before, and the
heart-rate-only command keeps producing heart-rate-only records.

```bash
# the fictional richer fixture: every optional field mapped (fictional values; source=simulated)
python scripts/convert_heart_rate_csv.py scripts/examples/fictional_physiology.csv /tmp/phys.jsonl \
    --athlete-id athlete-1 --device-id fictional-phys-csv-1 --source simulated \
    --breathing-rate-column breathing_rate_bpm --hrv-rmssd-column hrv_rmssd_ms --hrv-sdnn-column hrv_sdnn_ms \
    --activity-level-column activity_level --posture-column posture --skin-temperature-column skin_temperature_c \
    --spo2-column spo2_percent --signal-quality-column signal_quality
python scripts/replay_samples.py /tmp/phys.jsonl
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/physiological/history?limit=10"   # the fields come back as written
```

`scripts/examples/fictional_physiology.csv` has the same five recorded
timestamps as the heart-rate fixture with every optional column: blank HRV
and SpO2 cells (null), a breathing rate of 0 and HRV values of 0 (kept), a
blank posture (null) and the posture `unknown`. The engine stores and echoes
these fields; the summary and effort-review heart-rate figures still use
`heart_rate_bpm` only, and no new metric or interpretation is derived from
the other measurements.

`scripts/examples/fictional_heart_rate.csv` is a tiny fictional example with
`+02:00` and `-05:00` offsets, a blank heart rate and an out-of-order row; its
documented conversion uses `--source simulated`. Everything said under
"Historical timestamps stay historical" applies to converted files too: the
recorded times are preserved, so they stay out of the live window and are
subject to the newest-timestamp rules and the per-stream retention caps.

### Converting a tracking CSV (`scripts/convert_tracking_csv.py`)

The matching adapter for the tracking stream: a provider-agnostic converter
that turns a position/motion CSV into the interchange format, one `tracking`
record per row. Like the heart-rate adapter it is not support for any
vendor's export, makes no network requests and does not connect to any camera
or positioning hardware. The replay script stays format-agnostic; the two
adapters are separate scripts.

```bash
# describe and validate without writing anything
python scripts/convert_tracking_csv.py scripts/examples/fictional_tracking.csv /tmp/trk.jsonl \
    --athlete-id athlete-1 --source simulated --device-id fictional-trk-csv-1 --dry-run
# convert (the output file must not already exist)
python scripts/convert_tracking_csv.py scripts/examples/fictional_tracking.csv /tmp/trk.jsonl \
    --athlete-id athlete-1 --source simulated --device-id fictional-trk-csv-1
# a real export with other column names: every column is mapped explicitly; the source is what you state, never guessed
python scripts/convert_tracking_csv.py export.csv /tmp/trk2.jsonl --athlete-id athlete-2 --source camera \
    --timestamp-column time --x-position-column x --y-position-column y \
    --speed-mps-column speed --acceleration-mps2-column accel --tracking-confidence-column conf
# replay it (with the converted heart-rate file, if any), then review the 60 s ending at the last timestamp
python scripts/replay_samples.py /tmp/trk.jsonl --dry-run
python scripts/replay_samples.py /tmp/trk.jsonl
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/window?seconds=60&end=2026-10-20T10:00:04Z"
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/summary?seconds=60&end=2026-10-20T10:00:04Z"
```

Rules of this first version:

- The CSV needs a header row. The six columns (timestamp, `x_position`,
  `y_position`, `speed_mps`, `acceleration_mps2`, `tracking_confidence`) are
  named explicitly, default to the model's field names, must each appear
  exactly once, and must be six different names. Other columns are ignored.
- **Measurements must already be in the model's units**: positions in metres
  in the venue's coordinate frame, speed in m/s (never negative),
  acceleration in m/s² (negative = decelerating), confidence on the model's
  0.0 to 1.0 scale. No unit or scale conversion is done.
- **Every measurement is required on every row.** A blank cell is an error:
  missing confidence is never filled in with 1, and speed or acceleration is
  never calculated from positions. Zeros are real values and are kept
  (a stationary row stays `0.0`). Non-numeric or non-finite values and values
  outside the `TrackingSample` ranges are errors.
- Timestamps follow the same rule as the heart-rate adapter: ISO 8601 with a
  `T` separator and an explicit timezone, copied into the output as written.
  Naive timestamps, numeric epochs, invalid dates and other layouts are
  rejected.
- `--athlete-id` and `--source` are required and apply to every row;
  `--source` is one of `wearable`, `camera`, `manual`, `simulated` and is
  never inferred from the file name or contents. `--device-id` is optional
  and written to every row when given. The fictional fixture is converted
  with `--source simulated`.
- Whole-file validation with the API's own model, row-numbered errors naming
  the CSV column, dry run, atomic output and no-overwrite protection work
  exactly as for the heart-rate adapter. Rows keep their CSV order; the
  replay orders them by timestamp when sending.

`scripts/examples/fictional_tracking.csv` is a tiny fictional example aligned
in recorded time with `fictional_heart_rate.csv` (both cover
`2026-10-20T10:00:00Z` to `10:00:04Z`, one row per second): it has a
stationary row of real zeros, a row with negative acceleration, a row with
low (0.3) tracking confidence, `+02:00` and `-05:00` offsets and an
out-of-order row. Converting and replaying both fixtures for the same athlete
and reviewing the window ending at `2026-10-20T10:00:04Z` gives, with the
default rules, 5 samples in each stream, the heart-rate values above, and a
tracking summary of 10.5 m over 2.0 s of eligible intervals (two intervals
excluded by the confidence rule) with 1.5 s and 8.25 m at or above 5 m/s.
These numbers describe the fictional file only. The two files line up only
because their recorded timestamps already do: the engine does not
synchronize or correct device clocks, so a real heart-rate export and a real
tracking export align in a review only as far as the recording devices'
clocks agreed. Converted tracking files are historical and subject to the
tracking retention cap, exactly like converted heart-rate files are to the
physiological cap.

### Coherent single-athlete live snapshot (`athletes/{id}/live`)

`GET /api/v1/athletes/{id}/live` is the single-athlete form of the team
overview, built on the same selection code, so the two agree whenever they
share a captured instant. Backend time is read once; the newest retained
sample per stream **at or before** that instant is chosen; ages are that
instant minus the sample timestamp, never negative; device associations
and statuses (with explicit clock skew) follow the same rules as the team
overview; and the whole snapshot is one database read.

```bash
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/live"
# {"athlete_id":"athlete-1","captured_at":"...","registered":true,"athlete":{...},
#  "physiological":{...},"tracking":{...},"physiological_age_seconds":0.4,"tracking_age_seconds":0.4,
#  "physiological_newest_ahead_seconds":null,"tracking_newest_ahead_seconds":null,"devices":[...]}
```

- A sample timestamped after the captured instant (accepted within the 5 s
  ingestion clock-skew allowance) stays stored but is **not** returned as the
  current reading until backend time reaches it. If an earlier eligible
  sample exists it is returned with its real age, and
  `*_newest_ahead_seconds` says how far ahead the stored newer sample is; if
  no eligible sample exists the stream is null with the same figure. Nothing
  is clamped into an apparently fresh reading.
- The plain `physiological/latest` and `tracking/latest` endpoints are
  unchanged: they still return the newest stored sample, future-dated or
  not, and 404 when there is none.
- Registration is not required, exactly as for ingestion and history: an
  unregistered or unknown id answers with `registered: false`,
  `athlete: null` and whatever samples it has (possibly none). It is never a
  404.
- Ingestion, the timestamp allowance and retention are unchanged.

The dashboard's Live Heart Rate and Live Tracking cards poll this endpoint
once per second (one request for both cards) and use its ages.

### Live team overview (`teams/{team_id}/live`)

`GET /api/v1/teams/{team_id}/live` returns, in one consistent read, every
athlete registered with that `team_id` and the newest retained sample per
stream at or before one captured backend instant, with each sample's age.

```bash
curl "http://127.0.0.1:8000/api/v1/teams/demo-team/live"
# {"team_id":"demo-team","captured_at":"2026-10-26T05:23:44Z","athletes":[
#   {"athlete":{"id":"athlete-1",...},"physiological":{...},"tracking":{...},"physiological_age_seconds":0.1,"tracking_age_seconds":0.1},
#   {"athlete":{"id":"athlete-2",...},"physiological":{...},"tracking":null,"physiological_age_seconds":145.0,"tracking_age_seconds":null}]}
```

- Membership is `Athlete.team_id` in the registry; there is no team
  registry, so an unknown team id returns an empty list. Athletes are
  sorted by jersey number, then id.
- Backend time is read once. A sample timestamped after it, even one
  accepted within the ingestion clock-skew allowance, is not shown, so ages
  are never negative. An old sample is still returned with its age, which is
  how a consumer tells a stale reading from no reading (`null`).
- The response carries the samples as stored, sources included, and makes
  no judgment about freshness. `*_newest_ahead_seconds` says, per stream,
  how far ahead of the captured instant the newest stored sample is when it
  is not yet current.
- `devices` lists, per athlete, the device ids named by those two newest
  samples (deduplicated; `streams` says which named it) with the latest
  `DeviceStatus` report held for each id and its age, read in one query for
  the whole team. The association is only that the sample reported the id:
  it is a reported association, not a verified identity or a permanent
  assignment, and an id named only by an older sample is not listed. A
  report describes the moment it was sent, so an old `connected: true` is
  not proof of a current connection; nothing infers connection or
  disconnection from samples arriving or stopping. `status` is null when
  no report was ever received; battery and signal quality are null when
  not reported and 0 when reported as 0. Clock skew is explicit: a report
  timestamped after `captured_at` (accepted within the 5 s ingestion skew
  allowance) is still the newest report held, so it is returned with
  `status_age_seconds` 0 and `status_ahead_seconds` saying how far ahead it
  is; ages are never negative and such a report is not presented as more
  current than `captured_at`.

The simulator can send clearly fictional status reports for its own device
id every 5 s with `--with-device-status` (off by default; existing commands
are unchanged):

```bash
python scripts/simulate_athlete.py --with-tracking --with-device-status
```

### Reviewing a recorded window (`end` parameter)

Both `GET /api/v1/athletes/{id}/window` and `GET /api/v1/athletes/{id}/summary`
accept an optional `end` query parameter: an ISO 8601 timestamp **with a
timezone**. When it is given, that exact instant is `window_end` and
`window_start` is `seconds` before it; when it is omitted the live behaviour is
unchanged (backend time now). Selection, inclusive boundaries, ordering,
`tracking_rules` and every calculation are shared between the two modes.

```bash
# the 60 s ending at the end of the fictional replay example (encode '+' as %2B in offsets)
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/window?seconds=60&end=2026-10-20T10:00:02Z"
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/summary?seconds=60&end=2026-10-20T10:00:02Z"
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-2/summary?seconds=60&end=2026-10-20T12:00:02%2B02:00"   # same instant
```

Rules:

- `end` must parse as a timezone-aware timestamp; an invalid or naive value is
  a 422 from validation (`query.end: Input should have timezone info`).
- `end` must not be after backend time (read once per request); a future end
  is a 422 with the message `... is after backend time ...; a recorded review
  cannot end in the future`. Exactly "now" is allowed.
- Equivalent instants in different offsets select the same samples; the
  response echoes `window_end` in the offset you sent.
- Summary ages (`latest_sample_age_seconds`) are always relative to
  `window_end`. In a recorded review they mean **seconds before the selected
  window end**, not age relative to the present.

**This is a review of retained samples, not a recording archive.** The
backend keeps only the newest samples per athlete per stream up to each
stream's cap (default 300), so a window can only show what is still in SQLite:
older records may already have been removed, and a window may come back empty
or partial for that reason. Nothing is rebased and ingestion is unchanged.

### Source information (`source`, `device_id` and the per-stream breakdown)

Every `PhysiologicalSample` carries a `source` (one of the generic categories
`wearable`, `camera`, `manual`, `simulated`) and a `device_id`. Since this
milestone `TrackingSample` accepts the same two fields as **optional**
metadata: payloads and stored records without them stay valid and mean
"not reported". The simulator marks its tracking samples `simulated` with its
configured device id, the tracking CSV adapter requires an explicit
`--source`, and the replay sends whatever the file holds, so older files that
omit the fields are replayed as not reported. Nothing is ever inferred from a
file name, a value, a confidence, or the fact that data was replayed: replay
describes delivery, not the original measurement source.

The window and summary responses add a small per-stream breakdown of exactly
the samples they selected:

```json
"sources": {"reported": {"camera": 1, "wearable": 3}, "not_reported": 2}
```

`reported` counts each category that occurs and `not_reported` counts samples
without a source; together they always equal the stream's `sample_count`
(a physiological sample with a null heart rate still counts under its
source). The window carries `sources.physiological` and `sources.tracking`;
each summary stream carries its own `sources`. Existing metrics, window
selection and `tracking_rules` are unchanged.

**A source is supplied metadata, not proof of authenticity.** It says what
the sender labelled the sample, not where the data really came from or how
accurate a sensor was; a `wearable` label on fictional test values does not
make them real, and `simulated` is a label like any other. No live hardware
integration has been added: data still arrives from the simulator, manual
POSTs, the replay script or the CSV adapters.

### Finding retained data (`retained-data` endpoint)

`GET /api/v1/athletes/{id}/retained-data` reports, per stream, how many
samples the engine currently retains for an athlete, the recorded timestamps
of the oldest and newest retained rows in UTC, and that stream's retention
cap (`max_samples`). It reads the persisted rows of both streams under one
read and needs no registration: an unknown athlete id simply reports empty
streams. `max_samples_per_stream` is kept for older consumers: it repeats the
shared value when both caps are equal and is `null` when they differ.

```bash
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/retained-data"
# {"athlete_id":"athlete-1",
#  "physiological":{"sample_count":5,"max_samples":300,"earliest_timestamp":"2026-10-20T10:00:00Z","latest_timestamp":"2026-10-20T10:00:04Z"},
#  "tracking":{"sample_count":5,"max_samples":300,"earliest_timestamp":"2026-10-20T10:00:00Z","latest_timestamp":"2026-10-20T10:00:04Z"},
#  "max_samples_per_stream":300}
# then review the 60 s ending at the newest retained timestamp:
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/summary?seconds=60&end=2026-10-20T10:00:04Z"
```

### Requested period versus available history (`availability`)

The window, summary and effort-review responses carry an additive
`availability` block so a consumer can tell a period that reaches beyond the
retained history from one that was never sampled. Per stream it holds the
same facts as `retained-data`, read in the same database read as the samples
it accompanies, plus what the request selected:

```json
"availability": {
  "physiological": {"max_samples": 300, "retained_count": 300, "retained_earliest": "2026-10-20T10:00:00Z", "retained_latest": "2026-10-20T10:04:59Z", "at_cap": true,
                    "selected_count": 60, "selected_first": "2026-10-20T10:04:00Z", "selected_last": "2026-10-20T10:04:59Z"},
  "tracking":      {"max_samples": 3000, "retained_count": 0, "retained_earliest": null, "retained_latest": null, "at_cap": false,
                    "selected_count": 0, "selected_first": null, "selected_last": null}
}
```

The window and summary carry one block for the window (`selected_count`
equals each stream's `sample_count`). The effort review carries
`availability.movement` (tracking over the effort) and
`availability.heart_rate.before/during/after` (physiological per period, the
retained facts repeated in each); the period boundaries and every figure are
computed exactly as before. What the block does not say: a selection whose
first and last timestamps reach the period bounds is not thereby complete
(nothing counts gaps inside it); no expected count, sampling rate or coverage
fraction is implied; and retained history that begins after the period
starts, or ends before it ends, does not show why. `at_cap` is the engine's
only evidence about removal: at the cap, any older sample that was received
has been trimmed; below it, the engine cannot tell whether earlier data was
never received or was removed under an earlier cap. A consumer that does not
know the block (or an engine that does not send it) loses nothing else.

What the retained-data numbers mean, and what they do not:

- `sample_count` is the number of rows still stored (never more than that
  stream's `max_samples`). The timestamps are the
  recorded timestamps of the oldest and newest of those rows, normalised to
  UTC; the stored samples keep their original offsets. An empty stream has
  count 0 and null timestamps.
- **The range describes retained records only.** It does not show that every
  instant between the bounds was sampled, it is not a session list or a
  recording archive, and nothing is inferred about gaps, earlier recordings
  or rows removed by the retention rule. A count of 0 does not establish
  whether data was never collected or has since been removed.
- The two streams are counted independently and can cover different times.
  A window ending at the newest timestamp of one stream may therefore contain
  nothing from the other.

The dashboard's Recorded mode uses this endpoint behind its **Find retained
data** and **Review latest retained** buttons (see the `Athletrix_Web`
README). Nothing about ingestion, retention or the stored timestamps changes.

### Reviewing a manually selected effort (`effort-review` endpoint)

`GET /api/v1/athletes/{id}/effort-review?effort_start=<ISO>&effort_end=<ISO>`
describes one effort period that **you** select: the movement recorded during
it and the heart rate recorded before, during and after it. Nothing is
detected or classified: the bounds are yours, the periods are fixed
engineering defaults, and every figure is a description of retained samples.

```bash
# the 10:00:01Z .. 10:00:04Z part of the fictional CSV fixtures (encode '+' as %2B in offsets)
curl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/effort-review?effort_start=2026-10-20T10:00:01Z&effort_end=2026-10-20T10:00:04Z"
```

Request rules (each failure is a 422 with the reason):

- `effort_start` and `effort_end` are required ISO 8601 timestamps with an
  explicit timezone (`2026-10-20T10:00:01Z`, `2026-10-20T12:00:01+02:00`).
  Numeric epochs and timezone-naive values are rejected; equivalent instants
  in different offsets select the same samples and are echoed as sent.
- `effort_start` must precede `effort_end`; the duration must be between
  1 and 120 seconds inclusive; `effort_end` must not be after backend time,
  which is read once per request and returned as `captured_at`.

Response (`EffortReview`):

- `movement`: the existing tracking summary over `[effort_start, effort_end]`
  (sample count, peak speed, first/last timestamps, largest gap, eligible
  distance, included duration and fraction of the effort, excluded
  intervals, high-speed time and distance, the applied `distance_rules`, and
  the `sources` breakdown). The same gap and confidence rules as the summary
  endpoint apply; excluded intervals are never bridged and nothing is
  extrapolated.
- `heart_rate.before`, `.during`, `.after`: for each period its applied
  bounds with `start_inclusive`/`end_inclusive`, the physiological sample
  count and non-null heart-rate count, arithmetic mean and peak, first/last
  sample timestamps, largest inter-sample gap and `sources` breakdown.
  Periods never overlap:

  | Period | Bounds |
  |--------|--------|
  | before | `[effort_start − 10 s, effort_start)` |
  | during | `[effort_start, effort_end]` |
  | after  | `(effort_end, effort_end + 30 s]`, capped at `captured_at` |

- `follow_up_elapsed`: whether `effort_end + 30 s` is at or before
  `captured_at`. **Elapsed time does not establish that samples for the
  period were received, retained or complete**; read the counts.
- `rules`: the applied lengths and limits (`before_seconds` 10,
  `after_seconds` 30, `min_effort_seconds` 1, `max_effort_seconds` 120).
  They are named engineering defaults for a readable review, not
  physiological standards or validated recovery intervals.

What the response does and does not say:

- Null heart rates stay missing (counted as samples, not as measurements,
  never replaced by 0). Empty periods report zero counts and null
  measurements. Heart rate is never interpolated. The mean is the arithmetic
  average of the measurements present, not time-weighted.
- Recorded timestamps are used as stored: there is no device-clock
  correction between the heart-rate and tracking streams and no automatic
  estimate of the lag between physical effort and heart-rate response. Where
  the two devices' clocks disagreed, the periods are offset by that much.
- Only retained samples (the newest per athlete up to each stream's cap) are
  reviewed, so any period, including the effort itself, can be partially
  populated or empty without that being an error.
- No recovery rate, baseline comparison, fatigue label, diagnosis or
  recommendation is produced, and none should be read into the numbers: a
  heart-rate difference between periods on its own does not show recovery or
  any change in condition.

### Exporting retained samples for replay (`retained-samples.jsonl`)

`GET /api/v1/athletes/{id}/retained-samples.jsonl` downloads every sample
the engine currently retains for one athlete, both streams, in the JSON
Lines interchange format above, so it can be replayed into another local
Athletrix database with `scripts/replay_samples.py`.

```bash
# export from one backend, replay into another (second backend on port 8010 with its own DATABASE_PATH)
curl -fsS -o athlete-1.jsonl "http://127.0.0.1:8000/api/v1/athletes/athlete-1/retained-samples.jsonl"
python scripts/replay_samples.py athlete-1.jsonl --dry-run
python scripts/replay_samples.py athlete-1.jsonl --url http://127.0.0.1:8010
```

What the export is:

- Both streams are read under one lock, so the file is one consistent
  snapshot. Records are sorted by recorded instant; at equal instants the
  physiological record comes before the tracking record, which makes the
  order deterministic (an athlete has at most one row per instant per
  stream).
- Every record is written exactly as stored: timestamps with their recorded
  offsets (`2026-10-20T12:00:02+02:00` stays so), athlete and device ids,
  sources (a tracking row without one exports `"source": null`), optional
  physiological measurements, nulls, real zeros and confidences. Nothing is
  rebased to the current time and nothing is invented.
- The response is `application/x-ndjson` with a `Content-Disposition`
  download name of the form
  `athletrix-retained-samples-<athlete>-<UTC capture time>.jsonl` and
  `X-Athletrix-Record-Count`, `-Physiological-Count` and `-Tracking-Count`
  headers. If neither stream has retained samples the response is a 404
  with the reason; nothing else is exported.

What it is not: **a snapshot of retained samples is not a recording archive
or a database backup.** Only the newest samples per stream up to its cap
exist, so a record already removed by a cap cannot be recovered by exporting.
The export always holds every retained row whatever the caps are, and its
`X-Athletrix-Physiological-Cap` and `X-Athletrix-Tracking-Cap` headers say
which caps were in force. The
file holds samples for the one athlete only: no roster, device status,
configuration, credentials or other athletes. Replaying it into a database
that already has samples for the same athlete replaces equal-instant rows
and leaves the rest, under the same retention rule.

The dashboard's Recorded mode has a **Download retained samples** button
next to Find retained data that saves this file directly on the computer
running the browser; nothing is uploaded anywhere.

### Historical timestamps stay historical

Because timestamps are preserved, a replayed recording is stored at its
recorded times. Consequences, by design:

- The dashboard's **Live** mode and the default window/summary look at the
  last 60 s of backend time, so an old recording will not appear there. Read it
  with the `history` and `latest` endpoints, with `end=` on the window and
  summary endpoints (previous section), or with the dashboard's **Recorded**
  review mode (see the `Athletrix_Web` README).
- The newest-timestamp rules still apply: a replayed sample becomes the
  "latest" only if it is newer than what is already stored, and only the
  newest samples per athlete up to each stream's cap are kept, so a long
  recording is trimmed to that many samples per stream.
- The script does **not** rebase recordings onto the current time. Shifting
  timestamps would make recorded data look live and break the "recorded time
  only" guarantee of the window and summary; if a demo needs live-looking
  data, use the simulator.

## Explicit local recordings (`recordings` endpoints)

An explicitly started recording preserves the samples accepted for a fixed
set of registered athletes in its own SQLite tables, independently of the
rolling retention caps, so a practice or match can be reviewed locally later.
It is local storage on this machine: nothing is uploaded, nothing is deleted
automatically, and there is no automatic recording.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/recordings -H 'content-type: application/json' \
     -d '{"name":"Tuesday practice","athlete_ids":["athlete-1","athlete-2"]}'      # 201: active recording
curl http://127.0.0.1:8000/api/v1/recordings/active                                   # {"recording": {...}} or {"recording": null}
curl http://127.0.0.1:8000/api/v1/recordings                                          # all recordings, newest start first
curl http://127.0.0.1:8000/api/v1/recordings/<id>                                     # detail: roster snapshot + recorded counts per athlete and stream
curl -X POST http://127.0.0.1:8000/api/v1/recordings/<id>/stop                        # stopped (a second stop returns the same state)
```

Rules of this first version:

- A recording has a generated id (`rec-<UTC start>-<6 hex>`), a user-provided
  name, the selected athlete ids in the order given, backend UTC `started_at`
  and `stopped_at`, and a status of `active` or `stopped`.
- The whole selection is validated before anything is created: an
  unregistered id is a 422 naming every unknown id, a blank name, an empty or
  duplicated selection is a 422, and nothing is created in those cases. The
  selected athletes' roster details are snapshotted into the recording and
  stay with it even if the registry changes later.
- Only one recording can be active on a backend at a time; a second start is
  a 409 naming the active one. The database enforces this too.
- Starting records samples accepted from then on; it never copies the rolling
  history that already exists. Stopping an already stopped recording returns
  its existing state with its original stop time.
- **An active recording survives a backend restart and stays active until it
  is stopped explicitly.** The dashboard shows the active recording in a
  persistent indicator for that reason.
- There is no cloud upload and no background pruning, so recordings consume
  local disk until removed with `scripts/local_data.py` (an explicit, offline,
  dry-run-first CLI; see "Local data inventory and removal").

### What a recording preserves

While a recording is active, every accepted physiological and tracking sample
for one of its athletes is written to the recording's own tables in the
**same transaction** as the rolling store's insert-plus-trim (`app/core/ingest.py`),
so a successful POST means both took the sample and a failed recording write
leaves the rolling store unchanged too. Membership is decided at ingestion,
while the recording is active, not by the sample's own timestamp: a delayed
sample or a historical replay received during the recording is recorded as
received, and a sample timestamped inside the session window but received
after the stop is not. Validation and the 5 s future-timestamp rule are
unchanged, and the sample is stored exactly as normalised for the rolling
store: timestamp with its offset, ids, source, nulls, zeros and tracking
confidence. Within one recording, athlete and stream an equal instant
replaces the earlier row, exactly as in the rolling store, so a recording is a
normalised record of what was accepted, not an audit log of every arrival.
Recorded rows are never trimmed by the rolling caps and survive restarts;
samples for athletes outside the selection never enter; the live endpoints
and rolling retention behave exactly as before.

### Reviewing and exporting a recording

Session-scoped endpoints read **only that recording's samples** for one of
its athletes: no fallback to the rolling history or to another recording, and
no retention cap. They reuse the rolling endpoints' calculations and rules
unchanged (window selection with inclusive bounds and one captured backend
time, the summary, the effort review's periods and distance rules, source
breakdowns, null handling, the 300 s window limit and the 1 to 120 s effort
limits):

```bash
R=http://127.0.0.1:8000/api/v1/recordings/<id>/athletes/athlete-1
curl "$R/recorded-data"                                                  # per stream: count and recorded-timestamp bounds in this recording
curl "$R/window?seconds=60&end=2026-10-26T20:30:00Z"                     # RecordingWindow: samples + session_availability
curl "$R/summary?seconds=60&end=2026-10-26T20:30:00Z"                    # RecordingSummary
curl "$R/effort-review?effort_start=2026-10-26T20:10:00Z&effort_end=2026-10-26T20:10:20Z"
curl -OJ "$R/recorded-samples.jsonl"                                     # replayable export of every recorded sample, both streams
```

Each response names the recording (`recording`: id, name, status, start and
stop times on the backend's clock) separately from the samples' own recorded
timestamps and from the requested analysis period, and carries
`session_availability` (per stream: recorded count and bounds, selected count
and bounds; no cap, because none applies) instead of the rolling
`availability` block, which is null here. Unknown recordings, athletes
outside the recording's roster and empty exports are 404s. The export uses
the same `{type, sample}` JSON Lines format as the rolling export, written
as stored, with a safe filename
(`athletrix-recording-<id>-<athlete>-<UTC capture time>.jsonl`) and the
record-count headers plus `X-Athletrix-Recording-Id`; roster and session
metadata stay in the recording detail rather than in the file.

The dashboard's **Local recording** card (engine mode only) starts and stops a
recording explicitly, shows a persistent indicator naming the active
recording and its athletes, lists the recordings and opens one in
**Recording** review, where the existing summary, timeline, effort review,
data lookup and download read only that recording (see the `Athletrix_Web`
README).

### Saved effort periods in a stopped recording (`periods` endpoints)

A manually selected effort period of a stopped recording can be saved with a
short label and reopened later. A saved period is the selection only (label,
exact start and end instants as sent with their offsets, creation time on the
backend's clock); it is never a frozen calculation, so reviewing it later uses
the engine's rules in force at that time, and an empty or partial phase is a
valid outcome, not an error.

```bash
P=http://127.0.0.1:8000/api/v1/recordings/<id>/athletes/athlete-1/periods
curl -X POST "$P" -H 'content-type: application/json' \
     -d '{"label":"second sprint","effort_start":"2026-10-26T20:10:00Z","effort_end":"2026-10-26T20:10:20Z"}'   # 201 SavedPeriod
curl "$P"                      # the athlete's saved periods in this recording, by start instant
curl "$P/<period id>"          # one saved period
# reopen it with the existing session effort review:
curl "http://127.0.0.1:8000/api/v1/recordings/<id>/athletes/athlete-1/effort-review?effort_start=...&effort_end=..."
```

Rules: the recording must be stopped (409 otherwise, so the samples under a
period are stable); the athlete must be in the recording's roster (404
otherwise); the label must not be blank (at most 80 characters) and the bounds
follow the effort review's validation (explicit timezone, 1 to 120 s, not
after backend time), with a 422 and nothing created on any failure. Labels
are not identifiers and may repeat; the generated id identifies a period.
Periods are isolated by recording and athlete and survive restarts. No
editing, deletion, automatic detection or cross-athlete comparison exists.

### Exporting a descriptive review snapshot of a saved period (`review.json`)

`GET /api/v1/recordings/{id}/athletes/{athlete}/periods/{period}/review.json`
downloads a JSON document (attachment, safe filename
`athletrix-period-review-<recording>-<athlete>-<period>-<UTC time>.json`,
`X-Athletrix-Format: athletrix-period-review/1`) produced from **one database
read and one captured set of engine settings**, reusing the session effort
review's selection and calculations. It is an analysis export, separate from
the replay-compatible sample JSONL export, whose format is unchanged.

```bash
curl -OJ "http://127.0.0.1:8000/api/v1/recordings/<id>/athletes/athlete-1/periods/<period id>/review.json"
```

The document keeps three things apart: the **saved selection**
(`saved_period`: id, label, exact bounds, creation time), the **rules used
now** (`calculation`: capture time, the review's period rules, the distance
rules and tracking rule from the engine settings in force at that moment) and
**this snapshot of the result** (`review`: movement and the before, during and
after heart-rate phases with session availability and source breakdowns;
`periods`: the applied phase bounds and inclusivity; `samples`: the normalised
physiological samples across the before-to-after span and the tracking
samples within the effort, as stored, with nulls, zeros, confidences, sources
and device ids). `format`, `format_version` and `generated_at` identify the
file; `recording` carries the recording reference and its saved roster. A
later export after a change to an engine setting is a different snapshot with
different figures and the same saved bounds; an earlier file stays a
standalone record of the earlier calculation. Nothing interpretive is in it:
no diagnosis, score, recovery judgement, baseline comparison or advice.

### Portable recording package (`package.zip`, format `athletrix-recording-package` version 2)

`GET /api/v1/recordings/{id}/package.zip` downloads one **stopped** recording
(409 while active, 404 if unknown) as a ZIP read from one database snapshot,
with a safe filename `athletrix-recording-package-<id>-<UTC time>.zip`. It
contains exactly five members (version 1 packages, written before 2026-10-28,
have the first four and are still read):

| Member | Content |
|--------|---------|
| `manifest.json` | `format`, `format_version` (2), `generated_at`, the recording reference, `members` (SHA-256 and byte count of each data member), `counts` (athletes, samples per stream, per athlete, periods, receipts) and `settings_at_export` |
| `recording.json` | original `id`, `name`, `status` ("stopped"), `started_at` and `stopped_at` (the backend's clock while recording), `athletes` (the roster snapshot, in roster order) and `configuration_at_start` (the start-time snapshot, or null when the recording has none; see "Recording configuration snapshot") |
| `samples.jsonl` | every recorded sample of every roster athlete in the existing `{type, sample}` interchange format, by athlete in roster order, then instant, physiological first at equal instants; payloads exactly as stored |
| `periods.json` | saved periods: original `id`, `athlete_id`, `label`, `effort_start`, `effort_end` (as saved), `created_at` |
| `receipts.jsonl` | receipt metadata for the samples that have it: one `{type, athlete_id, timestamp, received_at}` per line, `received_at` being when the engine where the recording was made received the sample live (see "Timing instrumentation"); kept apart so `samples.jsonl` stays in the interchange format |

Every recorded sample is included, independent of the rolling caps and the
300-row history page. Recording times are kept apart from sample timestamps.
`settings_at_export` records the engine settings when the file was written; it
does not claim they were in force while the data was recorded or reviewed, and
an importing engine applies its own settings; the start-time snapshot in
`recording.json` is a separate, earlier record. An engine from before version 2
refuses a version 2 package with "unsupported format_version 2" rather than
silently dropping the new fields. The checksums detect
accidental corruption only. The package is not a database backup and not
evidence that the measurements are accurate: it holds no registry, rolling
history, device status, other recordings, configuration, credentials, paths or
logs. The per-athlete JSONL and the period review JSON downloads are
unchanged. The format is defined in `app/core/recording_package.py`.

```bash
curl -OJ "http://127.0.0.1:8000/api/v1/recordings/<id>/package.zip"
```

### Inspecting and importing a package locally (`scripts/recording_package.py`)

```bash
python scripts/recording_package.py inspect athletrix-recording-package-<id>-<time>.zip                       # validate and summarise; opens no database
python scripts/recording_package.py import  athletrix-recording-package-<id>-<time>.zip --database /path/to/dest.db --dry-run
python scripts/recording_package.py import  athletrix-recording-package-<id>-<time>.zip --database /path/to/dest.db
python scripts/recording_package.py import  athletrix-recording-package-<id>-<time>.zip --database /path/to/dest.db --allow-duplicate
#   --json for a machine-readable summary; exit 0 success, 1 invalid/refused/failed, 2 bad arguments
```

- **Coordination.** A real import takes the destination's maintenance lease
  (see "Local data inventory and removal") and refuses while an engine of this
  version has that file open; an engine started during the import refuses to
  open the file until the import finishes. Dry runs and `inspect` take no lease.
- **Validation first.** The whole package is checked in memory before
  anything is written: supported `format_version`, exactly that version's members (four in v1, five in v2)
  (nothing is ever extracted to disk; oversized archives are refused),
  checksums and sizes, counts, every sample against the engine's own models,
  roster membership, no duplicate or conflicting sample for one athlete,
  stream and instant, saved periods against the effort rules (label, timezone,
  1 to 120 s, not ending after their creation), and the timestamp policy below.
- **Dry run** (`--dry-run`) validates everything and looks at the destination
  read-only (does it exist, was this package already imported there); it
  writes nothing and does not even create the file.
- **Import** needs an explicit `--database` and writes, in **one
  transaction**, a new stopped recording with the original name, start and
  stop times, the roster snapshot, every sample exactly as normalised in the
  package and every saved period with its original label and exact bounds,
  all under **new ids**. A `recording_imports` row keeps the provenance:
  the original recording id, the package SHA-256, format and generation time,
  the import time and the mapping of original period ids to new ones;
  `GET /recordings/{id}` shows it as `imported`. Any failure rolls the whole
  import back.
- **Never touched:** the athlete registry (the roster snapshot is enough),
  rolling history, device status, an active recording, or any existing
  recording. Nothing is overwritten or merged.
- **Repeated import:** a package already imported into that database (same
  SHA-256) is refused; `--allow-duplicate` imports it as a separate recording
  with its own new ids and provenance row.
- **Timestamp policy:** an import is historical data, not live ingestion.
  Recording times and sample timestamps are kept exactly; nothing is rebased
  and arrival-time rules are not re-applied. Because recording membership is
  decided by acceptance while the recording is active, not by timestamp, a
  legitimate package can hold samples timestamped **before** `started_at`
  (delayed or replayed data) and samples up to 5 s **after** `stopped_at`
  (accepted just before the stop within the live future-timestamp
  allowance); both import. The only timestamp rule is the bound live
  ingestion implies: nothing later than `stopped_at` + 5 s, which a live
  engine could not have accepted. Tests export and import exactly these edge
  cases (an hour-early sample, a sample 5 s after the stop, equal-instant
  replacement in another offset, nulls, zeros, an athlete with no samples,
  repeated labels, more rows than the rolling caps and the history page).
- Import into a separate file, or stop the destination engine first, so a
  running backend and the tool do not use one file at the same time.

### Reviewing several saved periods together (`period-reviews`, `period-reviews.csv`)

```bash
R=http://127.0.0.1:8000/api/v1/recordings/<id>/athletes/athlete-1
curl "$R/period-reviews"          # JSON: recording, athlete, captured_at, rules, distance_rules, one item per saved period
curl -OJ "$R/period-reviews.csv"  # the same set as CSV, one row per saved period
```

The batch reviews every saved period of one athlete in one **stopped**
recording (409 while active) with **one captured time, one capture of the
engine settings and one consistent read**, using the same selection and
calculation functions as the single effort review and the review JSON. Each
item carries the saved period (id, label, exact bounds) and its full review
(movement, before/during/after heart-rate phases, session availability,
sources). Items are listed by start instant, never ranked or combined across
athletes; periods differ in length and content, so their figures are not
equivalents, and nothing judges fatigue, recovery, readiness or safety. A
missing value stays null.

CSV columns (one row per saved period, UTF-8, header row, `\r\n` line ends):

| Column | Unit / meaning |
|--------|----------------|
| `recording_id`, `athlete_id`, `period_id`, `label` | text (a label starting with `=`, `+`, `-`, `@`, tab or carriage return is written with a leading `'` so a spreadsheet does not evaluate it) |
| `effort_start`, `effort_end` | ISO 8601 as saved |
| `duration_s` | s |
| `calculated_at` | ISO 8601 UTC, the batch's captured time |
| `max_gap_s`, `min_confidence`, `high_speed_threshold_mps` | engine settings applied (s, 0 to 1, m/s) |
| `before_period_s`, `after_period_s`, `follow_up_elapsed` | the review's phase lengths (s) and whether the after period had fully elapsed (true/false) |
| `movement_sample_count`, `distance_m`, `included_duration_s`, `excluded_interval_count`, `speed_peak_mps`, `high_speed_duration_s`, `high_speed_distance_m` | count, m, s, count, m/s, s, m |
| `hr_<phase>_start`, `hr_<phase>_end`, `hr_<phase>_sample_count`, `hr_<phase>_measured_count`, `hr_<phase>_mean_bpm`, `hr_<phase>_peak_bpm` for `before`, `during`, `after` | applied phase bounds (UTC), counts, bpm |

A null is an **empty cell**; a real zero is written as `0` or `0.0`. The
detailed per-period `review.json` remains the place for exact samples and
calculation details.

### The whole workflow, command by command

Record, stop, save periods, review them together, export a package, import it
into another local engine and reproduce the selections and calculations:

```bash
B=http://127.0.0.1:8000/api/v1
# 1. record (the athletes must be registered; samples arrive from your sender, the simulator or a replay)
curl -s -X POST $B/recordings -H 'content-type: application/json' -d '{"name":"Tuesday practice","athlete_ids":["athlete-1","athlete-2"]}'
python scripts/simulate_athlete.py --athlete-id athlete-1 --profile effort-rest --with-tracking --seconds 120
curl -s -X POST $B/recordings/<id>/stop
# 2. save effort periods (labels may repeat; ids identify)
curl -s -X POST $B/recordings/<id>/athletes/athlete-1/periods -H 'content-type: application/json' \
     -d '{"label":"effort","effort_start":"<ISO with timezone>","effort_end":"<ISO with timezone>"}'
# 3. review several periods together, as JSON and CSV
curl -s $B/recordings/<id>/athletes/athlete-1/period-reviews
curl -OJ $B/recordings/<id>/athletes/athlete-1/period-reviews.csv
# 4. export a portable package
curl -OJ $B/recordings/<id>/package.zip
# 5. inspect, dry-run and import it into another local database (stop that engine first, or use a new file)
python scripts/recording_package.py inspect athletrix-recording-package-<id>-<time>.zip
python scripts/recording_package.py import  athletrix-recording-package-<id>-<time>.zip --database /path/to/other.db --dry-run
python scripts/recording_package.py import  athletrix-recording-package-<id>-<time>.zip --database /path/to/other.db
# 6. start the other engine on that file and reopen: same samples, periods (new ids, mapping under "imported") and figures
DATABASE_PATH=/path/to/other.db uvicorn app.main:app --host 127.0.0.1 --port 8001
curl -s http://127.0.0.1:8001/api/v1/recordings/inventory
curl -s http://127.0.0.1:8001/api/v1/recordings/<new id>/athletes/athlete-1/period-reviews
```

The dashboard offers the same steps without curl: Local recording (start,
stop, Download package), Recording review (navigation, Saved periods,
Saved-period reviews with Refresh reviews and Download CSV, Open and Review
JSON per period). Importing is the local command above; the page has no
upload.

**What exists today and what still needs real data.** Everything above runs
on fictional or replayed samples: the simulator, the replay and CSV tools and
the validation scripts. The engine stores, reviews and moves what it is
given, and the checks prove that the software reproduces its own selections
and calculations exactly across engines. Nothing here has been run against a
real wearable or camera system, so nothing establishes measurement accuracy,
real heart-rate behaviour, tracking quality in a stadium, clock agreement
between real devices, or behaviour over a full match with real traffic. A
real integration still needs a sender for the device's export or stream
(the provider-agnostic sample models and the CSV adapters are the starting
point), field data to check timing and confidence handling, and decisions
about the engine settings for the sport.

**Storage limits.** Recordings and imported packages live in the local SQLite
file and are never pruned automatically; `scripts/local_data.py` removes one
recording or one athlete explicitly, but there is no archive management, so
they consume disk until removed (a 10-minute, 20-athlete,
1 Hz + 10 Hz recording measured about 90 MB in the long run below). One recording can be
active at a time, and one backend process should write a database file.

### A longer recorded run (`validate_team_scale.py --record`)

```bash
python scripts/validate_team_scale.py --athletes 20 --duration 600 --physiological-rate 1 --tracking-rate 10 \
    --physiological-cap 300 --tracking-cap 3000 --record --session-read-interval 15 --size-interval 60
```

`--athlete-read-interval N` adds an individual coherent live read
(`GET /athletes/{id}/live`, rotating athletes) every N seconds; each must name
its athlete, never show a negative age and never move backwards.
`--record` starts a recording covering every athlete before the timed run,
adds a session window read of the active recording every
`--session-read-interval` seconds (rotating athletes) next to the team
overview reads, samples the database size (file plus WAL and shared-memory
sidecars) every `--size-interval` seconds, and afterwards stops the
recording, compares rolling and recorded rows and spans per athlete and
stream, restarts the backend and checks the recording is identical, and runs
early, middle and late 20 s effort reviews of the first athlete,
cross-checked against an independent calculation from the accepted samples.
Schedule time (when a sample is due), send time (when it was issued),
request latency (the round trip) and the recorded timestamp (the tick's own
instant, never rewritten) are reported separately; nothing catches up by
rewriting timestamps and nothing is dropped to hide overload.

Observed on this Mac (8 CPUs, macOS, Python 3.14; fictional data, one run on
2026-10-28 with team overview reads every 2 s, individual live reads every
1 s and recording window reads every 15 s; no threshold, not a capacity,
hardware or match claim):

```bash
python scripts/validate_team_scale.py --athletes 20 --duration 600 --physiological-rate 1 --tracking-rate 10 \
    --physiological-cap 300 --tracking-cap 3000 --record --session-read-interval 15 --athlete-read-interval 1
```

| | Physiological 1 Hz | Tracking 10 Hz |
|---|---|---|
| Scheduled / attempted / accepted | 12000 / 12000 / 12000 | 120000 / 120000 / 120000 |
| Failed / timeout (unknown) / omitted / unsent | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| Delivered rate | 1.0 Hz over 600.0 s | 10.0 Hz over 600.0 s |
| Write latency p50 / p95 / max | 7.7 / 15.1 / 87.7 ms | 7.7 / 15.6 / 326.3 ms |
| Scheduling lateness p50 / p95 / max | 13.0 / 25.0 / 106.6 ms | 14.1 / 34.1 / 334.1 ms |
| Rolling rows per athlete (span) | 300 (299.0 s) | 3000 (299.9 s) |
| Recorded rows per athlete (span) | 600 (599.0 s) | 6000 (599.9 s) |

| Reads during the run | OK | Latency p50 / p95 / max | Checks |
|---|---|---|---|
| Team overview, every 2 s | 300 of 300 | 6.6 / 13.0 / 43.5 ms | all passed |
| Individual live, every 1 s, rotating | 600 of 600 | 5.6 / 15.7 / 126.6 ms | all passed (own athlete, no negative age, never backwards) |
| Recording window, every 15 s, rotating | 40 of 40 | 22.6 / 86.8 / 128.3 ms | all passed |

Recorded rows equalled accepted samples for all 20 athletes and were identical
after a backend restart. Database size (file plus WAL and shared-memory
sidecars): 0.4 MB at the start, 15.4 MB after 60 s, 60.1 MB after 300 s (when
the rolling caps filled), 90.0 MB at 600 s, unchanged after the stop and the
restart; after the caps filled, growth was about 6 MB per minute, all of it
the recording. Early, middle and late 20 s effort reviews of the first
athlete matched the independent calculation (201 tracking samples each;
16.72 m, 159.94 m with 20.0 s at or above 5 m/s, and 25.02 m). The run took
600.13 s from start to last response; its backend and temporary folder were
removed afterwards. An earlier run without individual live reads gave
matching figures.

### Proving the portable workflow (`scripts/verify_portable_workflow.py`)

```bash
python scripts/verify_portable_workflow.py          # two isolated engines, fictional data; exit 0 when every check passed
```

A source engine (rolling caps 50/50) records three fictional athletes with
unequal rates (physiology 1 Hz, tracking 4 Hz), missing heart rates, real
zeros, low-confidence tracking points, a gap, samples delivered late with
their original timestamps and an equal-instant replacement, plus a
hand-checkable athlete (speeds 2, 4, 6 m/s; heart rate 100, null, 104).
The recording is stopped, periods are saved (two with the same label), and
the package is exported to the run's temporary folder. The script then shows
that dry runs write nothing (a missing destination is not created, an existing
one stays byte-for-byte identical), that a corrupted package and a forced
failure in the middle of the sample import leave the destination unchanged,
and that a real import followed by a destination start and restart
reproduces the samples line for line, the roster, the recording times, the
labels and exact bounds under new ids with a complete id mapping, and the
single review, batch review, CSV and detailed JSON of every period, identical
to the source under the same settings. Movement figures are cross-checked
against an independent calculation from the samples that were sent, and the
hand-checkable period against values worked out by hand in the script (8 m,
0.5 s and 2.75 m at or above 5 m/s, three samples with two measured and a mean
of 102 bpm). Finally the destination is restarted with a 3 m/s threshold: the
reported rules and the affected figures change (1.5 s by hand) while the
imported samples and saved selections stay identical, and the earlier snapshot
still records 5 m/s. This is a software replay test with fictional values,
not validation of real physiology or of wearable or camera accuracy.

### Save, restart, reopen, export: a reproducible example

```bash
B=http://127.0.0.1:8000/api/v1
# 1. a stopped recording of athlete-1 exists (start one, send samples, stop it); then save a period
curl -s -X POST "$B/recordings/<id>/athletes/athlete-1/periods" -H 'content-type: application/json' \
     -d '{"label":"second sprint","effort_start":"2026-10-26T20:10:00Z","effort_end":"2026-10-26T20:10:20Z"}'
# 2. restart the backend (Ctrl-C and start uvicorn again): the period is still listed with the same id and bounds
curl -s "$B/recordings/<id>/athletes/athlete-1/periods"
# 3. reopen it: the existing session effort review at its exact bounds (the engine's current rules apply)
curl -s "$B/recordings/<id>/athletes/athlete-1/effort-review?effort_start=2026-10-26T20:10:00Z&effort_end=2026-10-26T20:10:20Z"
# 4. export the descriptive snapshot (saved selection, rules in force now, result and samples)
curl -OJ "$B/recordings/<id>/athletes/athlete-1/periods/<period id>/review.json"
```

The same steps on isolated backends with fictional data, including a changed
engine setting between two exports, are run by `scripts/verify_period_workflow.py`:

```bash
python scripts/verify_period_workflow.py                   # isolated backend, 120 s fictional effort-rest session, exit 0 when every check passed
```

It feeds a deterministic session built from the simulator's effort-rest
profile (accelerate, sustain, slow, rest) with missing heart-rate
measurements, low-confidence tracking points and a gap, saves several periods
(an acceleration, a sustained effort, a rest, the gap, two with the same
label) plus one for another athlete and one in a second recording, downloads
each snapshot, restarts the backend and checks that the periods are listed
again with their exact bounds, that identical labels coexist, that athletes
and recordings stay separate, that reopened reviews match expectations
computed independently in the script (trapezoid distance over eligible
intervals, threshold crossings, the documented phase boundaries, null heart
rates as samples but not measurements), that an empty phase and a missing
distance stay null, that each snapshot agrees with the live review, and that
after restarting the backend with a different high-speed threshold a new
review and a new snapshot reflect it while the saved bounds and the earlier
snapshot do not change.

### End-to-end check (`scripts/verify_recording_flow.py`)

```bash
python scripts/verify_recording_flow.py                      # isolated backend, rolling caps 3/3, 8 samples while recording, 4 after
python scripts/verify_recording_flow.py --cap 3 --samples 8 --after 4 --report reports/recording-flow.json   # reports/ is ignored
```

On an isolated backend with deliberately small rolling caps it registers
three fictional athletes, sends a few rolling samples before any recording
exists, starts a recording for two athletes, sends more samples than the caps
for all three (including a delayed sample with an older timestamp, an
equal-instant replacement, a null heart rate and a real zero speed), stops,
sends more, starts and stops a second recording for one athlete, restarts the
backend on the same database file, and then checks against expectations
computed in the script: the rolling history holds only its capped newest rows,
the first recording holds exactly the samples accepted for its two athletes
while active (the delayed sample at its own timestamp, one row for the equal
instant with the later payload, the null and the zero as stored) and nothing
from before the start, after the stop or for the third athlete, the second
recording holds only its own samples, both stay visible and stopped after the
restart, the session window, summary and effort review match independent
distance and heart-rate-period expectations while the rolling effort over the
same period has no samples and a null distance, and the export holds every
recorded sample and replays identically into a second isolated backend. Exit 0
when every check passed. The transaction-rollback case (a failed recording
write leaves the rolling store unchanged) is covered by the backend test
`tests/test_recording_ingest.py`, since it cannot be induced from outside.

## Recording configuration snapshot (`configuration_at_start`)

When a recording starts, the engine stores a small versioned snapshot (snapshot version 1) of the configuration in
force, in the same transaction that creates the recording. `GET /api/v1/recordings/{id}` returns it as
`configuration_at_start`:

```json
{"status": "captured", "origin": "this_engine", "note": "...",
 "snapshot": {"snapshot_version": 1, "captured_at": "2026-10-28T18:00:00Z",
   "engine": {"application": "Athletrix Engine", "version": "0.1.0", "code_revision": null, "package_format_version": 2},
   "movement": {"distance_max_gap_seconds": 3.0, "distance_min_confidence": 0.5, "high_speed_threshold_mps": 5.0},
   "retention": {"physiological_retention_samples": 300, "tracking_retention_samples": 300},
   "timing": {"max_future_timestamp_skew_seconds": 5.0},
   "effort_review": {"min_effort_seconds": 1.0, "max_effort_seconds": 120.0, "before_seconds": 10.0, "after_seconds": 30.0},
   "operator_supplied": {"note": "wearable bridge v0.3, 1 Hz", "metadata": {"sample_rate_hz": 1}, "verified_by_engine": false}}}
```

- **What it holds**: only the settings that shape acquisition, storage and calculation, the application version as
  configured (`APP_VERSION`), and a code revision **only if** one was supplied at startup with `CODE_REVISION` (for
  example `CODE_REVISION=$(git rev-parse --short HEAD) uvicorn ...`). The engine never runs git; unset means
  `null`, i.e. unknown. Nothing else is read from the environment: no paths, hosts, ports, CORS origins, usernames
  or secrets.
- **Operator-supplied adapter information**: `POST /api/v1/recordings` accepts an optional
  `adapter_configuration` with a `note` (up to 500 characters) and flat `metadata` (up to 32 keys; string, number,
  boolean or null values; strings up to 200 characters). It is stored as given with `verified_by_engine: false`:
  the engine checks none of it.
- **Three different settings records, never mixed**: `configuration_at_start` (what was in force when acquisition
  began), a package's `settings_at_export` (when the file was written), and the settings the engine applies now
  (reported with every review as `distance_rules`). Changing settings later, restarting, or exporting never alters
  the snapshot.
- **Origin**: `this_engine` when captured here; `source_engine` when it arrived in an imported package (it is then
  the snapshot of the engine where the recording was made, and stays so if the recording is exported again).
- **Compatibility**: recordings made before snapshots existed, and recordings imported from version 1 packages,
  have `status: "unavailable"` with a note saying the settings at their start are unknown. Nothing is reconstructed
  from current settings. Older databases gain the empty `recording_configurations` table when first opened.

## Timing instrumentation (receipt time, Server-Timing, developer timing panel)

The sample `timestamp` is the **source observation time**: supplied by the sender on the sender's clock. It remains
the only identity and ordering key (newest-record selection, equal-instant replacement, history order and recording
windows are unchanged). The engine now adds, separately:

| Measurement | Where | Clock | Meaning |
|---|---|---|---|
| Application receipt (request-entry) time | `receipt.received_at` on every sample row; `physiological_receipt` / `tracking_receipt` in `GET /athletes/{id}/live`; `receipts.jsonl` in packages | engine UTC wall clock | read once when the engine's application begins handling the request (after the server parsed its headers, before the body is read and validated); **not** when the first byte reached the computer: network-stack and server queueing time before that point is not included |
| Server processing interval | `Server-Timing: app;dur=<ms>` header on every response (exposed to the dashboard via CORS) | engine monotonic | from that same receipt point to the start of the response; for a sample POST it covers validation, waiting for the database lock, the write and the commit; not stored |
| Browser request duration | developer timing panel | browser monotonic (`performance.now`) | fetch start to response body parsed |
| Browser DOM update / next frame | developer timing panel | browser monotonic | body parsed to the cards' DOM writes done, and to the next animation frame; rendering proxies, not proof of when pixels appeared |
| Receipt → DOM (estimate) | developer timing panel | cross-clock | browser wall clock at the DOM update, corrected by the measured offset, minus `received_at`; includes waiting for the next poll |
| Source → DOM (cross-clock) | developer timing panel | cross-clock | the same minus the sample timestamp; also depends on the sender's clock |

Rules and compatibility:

- Persisted wall-clock times are UTC; durations within one process use monotonic clocks, and no monotonic reading is
  ever stored or compared across restarts or machines.
- Rejected samples (HTTP 422 for an invalid body, or a timestamp more than 5 s ahead of engine time) store nothing:
  no row, no receipt, and an existing row at that instant keeps its values and receipt.
- `receipt` is `{"received_at", "origin"}`. `origin: "this_engine"` means this engine received it live;
  `"source_engine"` means the engine where an imported recording was made received it. **Imported samples never
  appear to have been received live by the destination.** NULL (shown as `null`, counted by `receipt_count` in a
  recording's per-stream detail) means not recorded: rows stored before this column existed, rows written by tools
  without a request, and samples from version 1 packages.
- **Equal-instant replacement** replaces the receipt together with the values: the row's receipt is always the
  arrival that delivered the values it holds, and the earlier arrival's receipt is not kept anywhere.
- A delayed sample keeps its own timestamp; its receipt time is later than those of samples with newer
  timestamps, and it does not become the current reading.
- **Replay** (`scripts/replay_samples.py`) posts samples live, so the destination records the replay time as their
  receipt; the interchange JSONL never carries receipts.
- Older databases gain the nullable `receipt` column on the four sample tables when first opened; existing rows keep
  NULL and nothing else changes.
- `GET /api/v1/clock` returns the engine's UTC time to microseconds for clock-offset estimates.

**Clock alignment method.** The developer panel probes `/api/v1/clock` every 10 s, reading the browser wall clock
before (t0) and after (t1): offset = engine time - (t0 + t1)/2, keeping the probe with the shortest round trip,
uncertainty ± round trip / 2 (an asymmetric path can be off by up to that). A wall-clock adjustment on either computer
invalidates it. A cross-clock subtraction is therefore an **estimate within the stated uncertainty**, never a
verified receipt-to-display latency, and source → DOM additionally depends on how the sender's clock relates to the
engine's.

**Developer timing panel.** Open the dashboard with `?timing=1` (for example `http://localhost:3000/?timing=1#demo`).
The normal coach view shows and measures nothing and makes no extra requests. The panel lists the six measurements
above (last, median, p95, max, n) for the live-card poll, measures display delay only at a sample's first display,
drops replies that arrive after an athlete or mode change, excludes polls made while the tab was hidden, and offers
**Download JSON**. Polling stops while another page of the site is shown instead of Dashboard; it continues in a
background browser tab, although the browser may slow timers. Measure with Dashboard open in the visible foreground
tab.

**Measured once on this Mac (2026-10-28, fictional data).** `demo_launcher.py --athletes 2` (1 Hz per stream, sender
in-process on the same machine), Chrome, Dashboard visible in the foreground with `?timing=1`, 127 polls, 254 first
displays, none while hidden:

| Measurement | median | p95 | max |
|---|---|---|---|
| HTTP request duration (live poll) | 14.5 ms | 30.1 ms | 75.7 ms |
| Engine processing (Server-Timing) | 5.1 ms | 14.5 ms | 40.0 ms |
| DOM update after response | 0.4 ms | 1.4 ms | 12.1 ms |
| Next animation frame after response | 4.9 ms | 93.1 ms | 114.6 ms |
| Receipt → DOM, first display (estimate, ± 6.8 ms) | 503 ms | 593 ms | 723 ms |
| Source → receipt (same machine, same clock) | 6 ms | 13 ms | 71 ms |

Clock offset engine minus browser: 2.0 ms ± 6.8 ms (best of 17 probes). Receipt → DOM is dominated by the 1 s poll
interval (a sample waits for the next poll); minimum 420 ms. These are separate observations on one computer over
localhost, not a device, network or display-latency validation, and **not** evidence that the research plan's
two-second target is met for real devices. Simulation write latency (from `validate_team_scale.py`) is a fourth,
separate figure and is not combined with any of these.

## Local data inventory and removal (`scripts/local_data.py`)

```bash
python scripts/local_data.py inventory --database data/other.db                          # read-only counts, recordings, athletes
python scripts/local_data.py remove-recording <rec id> --database data/other.db          # dry run: exactly what would go
python scripts/local_data.py remove-recording <rec id> --database data/other.db --execute --confirm <rec id>
python scripts/local_data.py remove-athlete <athlete id> --database data/other.db [--include-device-status <device id>]
python scripts/local_data.py remove-athlete <athlete id> --database data/other.db --execute --confirm <athlete id>
#   add --json for a machine-readable plan; exit 0 done/clean dry run, 1 unknown/refused/in use/failed, 2 bad arguments
```

| Scope | Removed | Kept |
|---|---|---|
| `remove-recording` | the recording row, roster snapshot (`recording_athletes`), recorded samples (both streams), saved periods, start-time snapshot, import provenance | registry, rolling history, device status, every other recording |
| `remove-athlete` | registry entry, rolling samples, and in every recording that athlete's membership, recorded samples and saved periods; a recording whose roster becomes empty is removed entirely; removed period ids are dropped from an import mapping | shared recordings (row, snapshot, other athletes' samples, periods and reviews); device status unless named with `--include-device-status` and no other athlete's samples name that device |

- **Dry run first.** Without `--execute` the database is opened read-only and the exact tables, row counts,
  relationships, kept records, refusals and limitations are printed. No data changes (SQLite may create empty
  `-wal`/`-shm` sidecar files when it opens a WAL database read-only). `--execute` requires `--confirm` with the
  exact id. A missing database file is never created; an unknown id changes nothing.
- **Concurrency (fail closed, two layers).** Stop the engine first. (1) A cooperative maintenance lease: every engine
  of this version holds a shared `flock` on `<database>.athletrix-lock` while its database is open; removal and
  package import take it exclusively and refuse while it is held, and an engine refuses to open the file while a
  tool holds it. The operating system releases it when a process exits, so a crash leaves no stale lock. (2) SQLite's
  exclusive lock, which SQLite refuses while another connection that has read the file is attached; a running engine
  always has (verified idle, after writes and under sustained writes). Layer 2 alone was found **not** to detect a
  connection that has opened but not yet read the file, or an idle connection to a rollback-journal database, which
  is why layer 1 exists. The lease is cooperative: it does not stop engines from before it existed, an `sqlite3` shell
  or other programs (layer 2 still catches those that have read the file), and neither layer is a process check or
  reliable on network file systems. The empty `.athletrix-lock` file stays next to the database (git-ignored).
- **Atomic.** The plan is recomputed under the lock and applied in one transaction; any failure rolls everything back.
  Active recordings, and athletes in an active recording, are refused.
- **Device status** is keyed by device id (the model has no athlete), and a device id may be reused, so it is only
  reported unless explicitly named; a device that other athletes' samples also name is refused.
- **Logical deletion, not erasure.** `secure_delete` is on for the transaction and the WAL is checkpointed and
  truncated afterwards, but deleted content may survive in free file-system blocks or SSD cells, snapshots and
  backups. The tool cannot remove exported JSONL/CSV/JSON files or packages already copied elsewhere (including other
  engines that imported them), camera footage, adapter logs, backups, Time Machine or cloud copies, or external
  systems. Free text such as recording names, period labels and operator notes is not rewritten (a note that mentions
  the athlete id is reported). It is **not** a complete consent-withdrawal or secure-erasure procedure.
- Older databases without the newer tables or columns can be inventoried and cleaned; no schema is added. Receipt
  metadata lives in the sample rows, so it is removed with them.

## One reproducible evaluation (`scripts/verify_evaluation_workflow.py`)

```bash
python scripts/verify_evaluation_workflow.py          # isolated engines and temporary files; exit 0 when every check passed
```

Exercises the batch's features together with fictional data: the snapshot captured at start (and unchanged after a
restart with another setting), nulls, zeros, an equal-instant replacement and a delayed arrival, receipt times bounded
by the script's own clock around each POST (same machine), Server-Timing never above the client round trip, the
package round trip with snapshot and receipts arriving as `source_engine`, identical samples and reviews, a version 1
package and a database with the tables as written before this batch, removal dry run, an athlete removal that leaves
the other athlete's samples and reviews identical, a recording removal that keeps rolling data and the other
recording, a forced failure that rolls back, imported settings that do not change the destination's own, and a
real running engine that makes removal and import refuse with nothing changed. Each removal scenario runs on its own
copy of a stopped database. The JSON report under `reports/` (ignored by git) lists expected versus actual per check
(pass/fail), unknowns and limitations. Measured on this Mac on 2026-10-28: 46 of 46 checks passed.

## Before real-device or human-participant evaluation

Not done in software and still required: a real wearable or camera adapter and its own logs; a measured relation
between each device clock and the engine clock (receipt time now makes delivery delay observable, but source
timestamps from another clock are not corrected); display timing measured on the actual sideline computer and
browser; an agreed procedure for exports, packages, backups and footage outside the database (the removal tool covers
one database file only); authentication or a physically controlled computer; and the approvals and procedures that
govern human participants, which no code here provides.

## Camera adapter prototype (synthetic markers; `camera_adapter/`)

A first, local path from video to the engine: **video/frame observations -> field calibration -> movement estimates
-> TrackingSample -> engine -> dashboard and recording**. It establishes the adapter's geometry, timestamps, movement
calculations and integration using a **generated video with coloured markers**. It does not detect people, identify
players or assess performance.

### Install and run

```bash
pip install -r requirements.txt                 # the engine (unchanged; no vision packages needed)
pip install -r requirements-vision.txt          # optional: OpenCV + NumPy, once, for video generation and marker extraction
python scripts/camera_synthetic_check.py        # ~3 s: video, calibration, extraction, estimates, errors vs truth
python scripts/verify_camera_pipeline.py        # ~40 s: the same stream through an isolated engine, recording and package
# watch it on a copy of the dashboard (Dashboard in the visible foreground tab):
python scripts/verify_camera_pipeline.py --serve-frontend "../Athletrix Fixed Website" --wait-before-send 30
```

The adapter core (`observations`, `calibration`, `estimator`, `pipeline`) is plain Python and runs with the engine's
own requirements; only `synthetic_video.py` and the two check scripts need OpenCV/NumPy. After installation everything
runs offline. Reports go to `reports/` and generated videos to a temporary folder (both git-ignored).

### 1. Observation contract (`camera_adapter/observations.py`, internal format)

JSON Lines: a header `{"format": "athletrix-camera-observations", "format_version": 1, "video": {width, height, fps,
name}, "time_base": "video_relative", "extractor": ...}` then one observation per line:
`{"frame", "video_time_s", "track_id", "u_px", "v_px", "confidence", "athlete_id"}`.

- `video_time_s`: seconds since the first frame (= frame / fps): **video-relative**, never file time, processing time or
  camera capture time.
- `track_id`: a temporary id for one continuous blob track; never an athlete id (enforced). A target that disappears and
  reappears may get a new one.
- `u_px, v_px`: image position of the ground-contact point, OpenCV pixel convention ((0, 0) = centre of the top-left
  pixel, u right, v down).
- `confidence`: the extractor's own score in [0, 1] (synthetic markers: visible fraction of the disc). **Not** a
  calibrated probability that position or identity is correct.
- `athlete_id`: explicit assignment or null. **Null observations are never sent** under any athlete id. Assignments
  come from the extractor's operator or an assignment file (`athletrix-track-assignments`: track id, athlete id,
  optional frame range, who assigned it); an assignment never overwrites a different existing one, and two tracks
  assigned to one athlete in the same frame are both suppressed.
- Examples: `scripts/examples/camera/observations_example.jsonl`, `assignments_example.json`. Validation is whole-file
  and per line (unknown keys, non-finite values, positions outside the frame, times not equal to frame / fps,
  repeated or out-of-order frames of a track).

This format is the adapter's input only; the engine's public models are unchanged.

### 2. Field calibration (`scripts/camera_calibrate.py`, `camera_adapter/calibration.py`)

```bash
python scripts/camera_calibrate.py fit scripts/examples/camera/calibration_points_example.json --out my-calibration.json
python scripts/camera_calibrate.py check my-calibration.json
```

Input: image size, **fitting points** (at least 4 image pixel <-> measured ground metres) and optional **check points**
that are never used in the fit. A planar homography is fitted by normalised DLT least squares (plain Python; it agrees
with OpenCV's `findHomography` to 2.5 µm on exact points). Refused: fewer than 4 points, non-finite values, points
outside the frame, coinciding points, degenerate arrangements (no four points with three not collinear), check points
repeating fitting points, transformations that are singular or put the points on both sides of the horizon. The saved
file (`athletrix-camera-calibration` v1) holds image size, both point sets, the transformation, conventions, the
**fitting residual (not independent accuracy)**, **check-point errors in metres** (the only independent figure),
assumptions and software identifiers; `load` refits and refuses an edited file. Assumptions: fixed camera (moving or
zooming requires recalibration), approximately planar ground, no lens-distortion model. No universal acceptable-error
threshold is applied.

### 3. Movement estimates (`camera_adapter/estimator.py`, `pipeline.py`, `scripts/camera_to_samples.py`)

Per athlete, a **causal** quadratic least-squares fit over the last `window_s` (0.5 s) of that athlete's valid
observations, evaluated at the newest observation: position, velocity, acceleration vector. `speed_mps` = |v|
(standing still is a legitimate 0.0); `acceleration_mps2` = tangential d|v|/dt, negative when slowing; below 0.3 m/s,
where direction is too uncertain, |a| with the sign of the fitted speed's change over the last half window. Rules:

- no future observations; no estimate (sample skipped, reason logged) while warming up (fewer than 5 points or less
  than 0.3 s covered), when the fit residual exceeds 0.3 m, or within the output-rate limit (10 Hz; outputs only at real
  observation times, never upsampled);
- a gap longer than `max_gap_s` (0.3 s) or a change of temporary track **resets** the history (never bridged);
- unassigned, conflicting, low-confidence (< 0.25) and outside-calibration observations (beyond the horizon, or more
  than 2 m outside the calibrated points' extent) are not used; an unusable calibration or a size mismatch stops all
  output;
- **confidence**: `tracking_confidence` = the lowest observation score in the window. The engine excludes a distance
  interval when either end is below its own `DISTANCE_MIN_CONFIDENCE` (0.5); the adapter neither knows nor changes that.

All of these live in one adapter configuration (`athletrix-camera-adapter-config` v1,
`scripts/examples/camera/adapter_config_example.json`), which is also sent as the recording's operator-supplied
`adapter_configuration` metadata (with the calibration's SHA-256 and the time-mapping rule).

```bash
# validate (writes and sends nothing), export JSON Lines + adapter log, or send to a local engine
python scripts/camera_to_samples.py validate OBS.jsonl --calibration CAL.json --config CONFIG.json --assignments A.json
python scripts/camera_to_samples.py export OBS.jsonl --calibration CAL.json --config CONFIG.json --assignments A.json \
    --anchor-utc 2026-10-20T10:00:00Z --out samples.jsonl --log adapter-log.json
python scripts/camera_to_samples.py send OBS.jsonl ... --anchor-utc now --url http://127.0.0.1:8000 --speed 1
```

**Time mapping**: sample timestamp = anchor + video_time_s. `--anchor-utc` is required; its meaning is recorded in the
log ("anchor stated with --anchor-utc; not camera capture time", or for `now` a live-paced playback whose first sample
is timestamped when sending starts). Timestamps keep microseconds, so 10 Hz samples never collapse, and are never
rewritten to pass the engine's checks. `send` reuses `scripts/replay_samples.py` (validation, monotonic pacing, stop at
the first failure, each record counted as submitted, failed or not sent; a timed-out request is reported as unknown).
The log keeps every sample's video time, frame, track and fit size, all skips with reasons, resets and unassigned
tracks. Synthetic markers are sent as `source: "simulated"` with the adapter's device id; `"camera"` is reserved for a
real camera's measurements (no schema change was needed).

### 4. Synthetic check: measured on this Mac (2026-10-29)

`scripts/camera_synthetic_check.py`: a 30 s, 20 fps, 960x540 MJPG video of a fictional 40 x 20 m field from a virtual
pinhole camera (ground truth and projection computed independently of the adapter, `camera_adapter/synthetic_truth.py`).
Red and blue move through rest, acceleration (±2 m/s²), constant speed (4, 3, 2 m/s) and a 4 m-radius curve, both at
once for 7 s; red passes behind a grey occluder (about 0.6 s hidden, partly hidden at the edges); blue is not drawn for
0.2 s and 0.6 s; blue's first 1.5 s and a yellow target are never assigned. Calibration points are true image positions
rounded to whole pixels (6 fitting, 4 check points).

| | red (fictional-athlete-01) | blue (fictional-athlete-02) |
|---|---|---|
| samples sent / assigned observations / warm-up skips | 288 / 590 / 12 | 271 / 554 / 12 |
| position error (median / p95 / max) | 0.019 / 0.041 / 0.117 m | 0.033 / 0.045 / 0.073 m |
| speed error, steady phases (median / p95 / max) | 0.00 / 0.22 / 0.72 m/s | 0.04 / 0.30 / 0.41 m/s |
| speed error within 0.5 s of an acceleration change (p95 / max) | 0.38 / 0.56 m/s | 0.42 / 0.64 m/s |
| acceleration error, steady (median / p95 / max) | 0.00 / 0.80 / 7.05 m/s² | 0.16 / 1.24 / 1.63 m/s² |
| acceleration error near changes (median / p95 / max) | 1.09 / 2.98 / 3.90 m/s² | 0.95 / 2.70 / 3.58 m/s² |
| first window after a reset (3 samples each) | speed 0.76, acceleration 5.6 m/s² median | 0 (blue was standing) |
| settle after a change (speed within 0.2 m/s; acceleration within 0.5 m/s²) | 0–1.1 s; 0.2–1.2 s | 0–0.7 s; 0.2–1.3 s |

Calibration: fitting residual 0.019 m rms (not independent); **check points 0.065 m rms, 0.104 m max**. Exclusions: 151
unassigned observations (blue's first 30 frames, all 121 yellow), 2 below the 0.25 confidence floor, 24 warm-up skips,
2 resets (red's occlusion, blue's 0.6 s dropout; the 0.2 s dropout did not reset). Identity swaps 0; no sample's window
bridges a gap. Findings: (1) far-field pixel jitter (about 0.4 px from compression, where 1 px is about 0.2 m) makes
0.5 s acceleration noisy; the same data with a 1.0 s window gives steady-phase acceleration p95 0.25 / 0.36 m/s² (max
0.40 / 0.55) but similar errors near changes (p95 about 2.1–2.2 m/s²), i.e. less noise for more lag; the default stays
0.5 s. (2) A marker emerging from behind the occluder has a shifted centroid: right after red's reset the estimates are
wrong by up to 0.8 m/s and 9.3 m/s², while its visible-area score (0.51) still passed the engine's 0.5 limit, showing
that the score is not a probability of correctness. These are measurements of this synthetic setup with no acceptance
threshold; they say nothing about athletes, real footage, lighting, occlusion by people, lens distortion or a full field.

### 5. Through the engine (`scripts/verify_camera_pipeline.py`)

On an isolated engine: two fictional athletes, a recording started with the adapter configuration as unverified
operator metadata, the 559 samples sent as live-paced playback with a fictional 1 Hz heart-rate feed alongside. 14 of
14 checks passed: every sample submitted (0 failed, 0 unsent), recorded tracking samples identical to the exported ones
per athlete, rolling history, live snapshots naming the right athlete with `source: simulated` and the adapter's device
id, the start snapshot carrying the calibration hash and estimator settings, and a package import keeping every
sample and the snapshot. Engine distances over known phases versus the analytic path: 28.39 vs 28.0 m, 10.18 vs 10.0 m,
21.19 vs 21.0 m, 16.28 vs 16.0 m (noise adds length). The engine's own distance rule spans red's 1.0 s gap behind the occluder by integrating the average of the two endpoint speeds (a trapezoid, not x/y positions; intervals up to `DISTANCE_MAX_GAP_SECONDS`, 3 s); the adapter itself never invents samples. The
dashboard showed the stream through the existing cards and timelines ("Live · reported source: simulated · device:
synthetic-marker-cam-1"; the speed timeline follows the designed profile). With 10 Hz tracking and the cards' 1 s poll,
the cards show about one sample in ten (all are stored and charted); the developer timing panel measured receipt to DOM
update of the displayed samples at a median of 117 ms (11–216 ms, ± 0.9 ms clock uncertainty) on localhost, not a
real-camera or two-second-target result.

### Next requirements for real player tracking

The synthetic marker extractor must be replaced or supplemented by validated **person detection**, **multi-object
tracking** through occlusions and crossings, **athlete assignment** (identity) with an auditable operator step, and,
where needed, **ball and event detection**; plus lens-distortion calibration, a measured capture-time mapping for the
camera, real-footage ground truth (for example surveyed positions or a reference system) to measure errors, and the
approvals that govern filming people. The prototype cannot know whether a pass was the right decision. A proposed,
unimplemented design for individual performance review is in `docs/individual-performance-design-note.md`.

## One-command isolated demo (`scripts/demo_launcher.py`)

```bash
python scripts/demo_launcher.py                                              # frontend folder guessed next to this repository
python scripts/demo_launcher.py --frontend "/path/to/Athletrix_Web" --athletes 4 --duration 90 --device-status
python scripts/demo_launcher.py --exit-after-demo                             # stop when data generation ends
python scripts/demo_launcher.py --tracking-cap 3000                           # demo backend keeps 3000 tracking samples per athlete
#   flags: --frontend DIR --athletes N (default 4) --duration S (default 90) --device-status
#          --backend-port P / --frontend-port P (default: free ports; an occupied port is an error, never taken over)
#          --physiological-cap N / --tracking-cap N (default: the backend's 300; set in the demo backend's own environment only)
#          --exit-after-demo; exit 0 normal, 1 startup failure or a failed sample, 2 bad arguments or missing frontend
```

The launcher starts a throwaway demo without touching the normal setup:
a fresh temporary SQLite database and its own `uvicorn app.main:app` on a
free localhost port; a copy of the frontend folder (without `.git`) served
by `python -m http.server` on a second free port, with that copy's
`index.html` pointed at the demo backend (the repository's files are never
edited and `.env` is never read or changed); a clearly fictional multi-sport
roster on team `demo-team` registered through the API; and, in-process, one
physiological and one tracking sample per athlete per second from the
simulator's deterministic profile (plus a fictional device-status report
every 5 s with `--device-status`) for the bounded duration. It prints the
dashboard URL (`http://localhost:<port>/#demo`) and short instructions.

When data generation ends it says so; nothing more is sent, nothing is
rebased or replayed to look live, the live cards show the retained readings
as "stopped updating", and Recorded mode still works. The servers stay up
until Ctrl-C (or stop at once with `--exit-after-demo`). On exit, failure or
Ctrl-C the launcher stops only the two processes it started and removes
only its temporary folder (frontend copy, database). CORS is configured only
for its own backend and only for its own frontend port. Startup failures,
a missing frontend folder and occupied ports are reported and leave nothing
running.

Offline: the engine dashboard depends on no remote script or asset. The
only external references in `index.html` are Google Fonts stylesheets,
which are cosmetic; with every external origin blocked in the browser the
page falls back to system fonts and the engine-connected cards, team
overview, summary and timeline keep working. The rest of the marketing
site was not tested offline.

## Team-scale validation (`scripts/validate_team_scale.py`)

A repeatable local check of how this software behaves with 11 to 20
fictional athletes sending samples at once. It is **not** a hardware test,
not proof of production capacity and not a match-length soak: a short run
on one machine, reported as observations with the machine context, with no
latency pass threshold invented.

```bash
python scripts/validate_team_scale.py                          # 20 athletes, ~15 s, 1 Hz per stream, overview read every 2 s
python scripts/validate_team_scale.py --athletes 11 --duration 15
python scripts/validate_team_scale.py --athletes 20 --duration 60                        # healthy run with concurrent reads
python scripts/validate_team_scale.py --athletes 20 --duration 30 --scenario interrupted # paused and late streams
python scripts/validate_team_scale.py --athletes 20 --duration 40 --physiological-rate 1 --tracking-rate 10   # unequal rates
python scripts/validate_team_scale.py --athletes 20 --duration 20 --physiological-rate 1 --tracking-rate 4 --scenario interrupted
python scripts/validate_team_scale.py --athletes 20 --report reports/my-run.json
#   flags: --athletes N (default 20) --duration S (default 15) --rate HZ (default 1, both streams)
#          --physiological-rate HZ / --tracking-rate HZ (each overrides --rate for its stream) --timeout S (default 5)
#          --workers N (default 8 request threads) --port P (default: a free port) --report PATH
#          --read-interval S (overview read during ingestion, default 2; 0 disables) --scenario healthy|interrupted
#   exit 0 when every verification and overview check passed and nothing was left unsent; 1 otherwise; 2 for bad arguments
```

Before the timed run the tool registers its fictional athletes to the
fictional team `fictional-team`; that setup is reported apart from the
measured traffic. During the run it reads `GET /teams/fictional-team/live`
every `--read-interval` seconds from the same client and checks each
response: one entry per registered athlete, every entry's samples naming
that athlete and no other, ages never negative, latest timestamps never
going backwards between reads, and device ids matching the samples. Read
latency is reported separately from write latency.

**Different rates per stream.** `--physiological-rate` and
`--tracking-rate` give each stream its own rate; `--rate` sets both, and a
per-stream option overrides it for that stream. Each stream runs on its own
monotonic grid (tick t of a stream is due at t divided by that stream's
rate and carries that instant as its timestamp), so a 10 Hz tracking stream
never duplicates physiological measurements onto its grid. Overview reads
(every `--read-interval` seconds) and the scenario's device-status reports
keep their own cadence in seconds. The report lists, per stream, the
requested rate, expected / attempted / accepted / failed / unknown (timed
out) / intentionally omitted / unsent requests, the delivered rate per
athlete with the schedule span it was measured over, scheduling lateness
and request latency, and the retained row count with the actual retained
timestamp span per athlete. A cap is a row count per stream, not a duration:
with the default 300, at 10 Hz the retained tracking history spans about
30 s while a 1 Hz physiological history of the same run spans the whole run.
`--physiological-cap` and `--tracking-cap` start the isolated backend with
other caps through its own environment (never `.env`); the verification then
expects each stream's newest rows up to its cap and checks that the backend
reports those caps. The verification accounts for the caps, duplicate-instant replacement,
the live overview selection and the recorded window and summary on each
stream's own grid.

`--scenario interrupted` adds a small, fixed disturbance defined in
seconds and mapped onto each stream's grid (it needs at least 3 athletes
and 13 s): the tracking streams of the first two athletes are paused for
samples due between 5 s and 10 s (those samples are counted as *omitted by
scenario*, apart from anything the tool failed to send), the third
athlete's physiological samples due in that window are delivered at 12 s,
newest first, with their original recorded timestamps, and the first three
athletes report a device status at 0 s and 10 s with the first report
re-sent at 12 s. The verification then also checks that the overview's
latest reading per stream is the newest accepted timestamp (so an older
late arrival never replaced a newer one), that the paused samples are
absent while the other stream's samples for those ticks are present, and
that each device's status is the newest report. The pause is also checked
while it is happening: on every overview read taken during the pause the
paused athletes' tracking reading must still be from before the pause while
their physiological reading is current, and the summary reports how many
reads evaluated that check. Checks whose outcome depends on a timed-out
request are reported as unknown, never as passed, and the exit code treats
unknown as not passed. It is one reproducible scenario, not a
fault-injection framework, and timestamp validation is never bypassed: late
samples are late only in delivery.

What a run does:

- Starts its own backend (`uvicorn app.main:app`) on a free localhost port
  with a fresh temporary SQLite database. The development backend and
  database are not involved, and on exit, failure or Ctrl-C the tool stops
  only the process it started and removes its temporary folder.
- Sends, per athlete and per tick, one physiological and one tracking
  sample built from the simulator's deterministic sine profile (phase-shifted
  per athlete), with `source=simulated`, distinct athlete ids
  (`fictional-athlete-NN`) and distinct device ids per stream. Ticks are
  scheduled on a monotonic clock; a small thread pool issues the requests,
  so no separate process is spawned per athlete. Every request records its
  due time, actual issue time, round-trip latency and outcome. Failures are
  counted by kind (HTTP error, timeout, connection error) and never retried;
  requests that could not be issued before the deadline are reported as
  unsent, never as successes.
- Reads the backend back and checks, per athlete: retained counts against
  the accepted samples (with the newest-300 cap), latest timestamps,
  athlete and device ids, both streams in one window, and a recorded
  summary (counts, heart-rate mean, peak speed, sources) against the
  accepted samples. If any of an athlete's requests timed out, acceptance is
  unknown, so that athlete's sample-set checks are reported as unknown
  rather than forced to pass or fail.
- Writes a JSON report (default `reports/team-scale-<UTC time>.json`; the
  `reports/` folder is ignored by git) and prints a summary: requested and
  actual duration, expected/attempted/accepted/failed/unsent counts overall
  and per stream, HTTP status counts and timeouts, latency p50/p95/max
  (the client-side round trip around one POST, which includes connection
  reuse and serialisation, not backend-internal time), scheduling lateness
  p50/p95/max (issue time minus due time), the backend configuration,
  athlete count and requested rate, verification results and machine
  context.

## Retention comparison (`scripts/compare_retention.py`)

A reproducible comparison of retention and effort review under two cap
profiles, each on its own isolated backend (fresh temporary database, free
port; the development backend and database are never touched):

```bash
python scripts/compare_retention.py                                                        # 20 athletes, 1 Hz / 10 Hz, 60 s, B = 300 / 3000
python scripts/compare_retention.py --athletes 20 --duration 60 --tracking-cap 3000 --effort-start 5 --effort-end 15
#   profile A: the backend defaults (300 physiological / 300 tracking); profile B: --physiological-cap / --tracking-cap
#   --effort-start / --effort-end: whole seconds after the run start; the effort must end before the oldest tracking
#   sample the default cap keeps at the end of the run, or the tool refuses (exit 2)
#   exit 0 when every check passed; 1 when anything failed or is unknown; reports/ is ignored by git
```

Both runs send the same deterministic fictional values on the same relative
schedule (the team-scale tool's machinery, with concurrent overview reads),
and each keeps its genuine timestamps. After each run the tool verifies the
retained counts and spans under that profile's caps, latest readings,
athlete and device separation and the recorded window and summary (the
team-scale verification), then reviews an early effort (default 5 s to 15 s)
whose tracking samples have fallen outside the default cap by the end of a
60 s run at 10 Hz (the newest 300 rows begin at 30 s) but remain under the
larger cap. Per athlete it checks the review's movement figures (sample
count, distance, included duration, high-speed time and distance) against
expectations calculated independently in the tool from the accepted samples
under the newest-N rule, the Before / During / After heart-rate selection
against the same samples, the availability metadata (cap, retained count and
bounds, `at_cap`, selected count and bounds), that a missing distance stays
null rather than becoming zero, and the recorded window over the effort. It
also exports one athlete's retained samples, checks every retained row is in
the file, replays the file into a third isolated backend started with the
same caps and compares the replayed export line for line. Finally the two
runs are compared: heart-rate periods must agree (same deterministic values)
and the movement review must differ only in what the caps explain.

The summary prints, per profile, the request accounting (errors,
intentionally omitted, unsent and timed-out requests kept apart), write and
read latency, scheduling lateness, retained rows and spans per stream with
the cap, and the per-athlete effort comparison. Observations on one machine
with no threshold; not a capacity or match-length claim.

## Running with the Athletrix_Web frontend

The frontend is a separate static site in the `Athletrix_Web` repository. Its
dashboard loads the athlete roster from this backend and polls the selected
athlete's latest samples and 60-second summary. Full local demo, each line in
its own terminal from this folder unless noted (activate `.venv` first):

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000              # 1. backend
python scripts/register_demo_roster.py                          # 2. roster (once per database file; safe to rerun)
python scripts/simulate_athlete.py --athlete-id athlete-1 --device-id simulator-1 --with-tracking   # 3a
python scripts/simulate_athlete.py --athlete-id athlete-2 --device-id simulator-2 --with-tracking   # 3b
python3 -m http.server 3000 --bind 127.0.0.1                    # 4. frontend, from the Athletrix_Web folder
lsof -nP -iTCP:8000 -iTCP:3000 -sTCP:LISTEN                     # 5. both should show 127.0.0.1, never * or 0.0.0.0
```

Then open http://localhost:3000, click **Live Demo** in the top navigation,
and use the **Athlete** selector above the cards to switch between the two
athletes. The roster, device statuses and the newest samples per athlete per
stream (up to each stream's cap, default 300) survive backend restarts (see
Storage). See
`Athletrix_Web/README.md` for details.

## Tests

```bash
pytest
```

Every test runs against its own temporary SQLite file (see `tests/conftest.py`);
the development database under `data/` is never read or written by the suite.
The frontend's timeline behaviour tests are a static page in the `Athletrix_Web`
repository (`tests/timeline_confidence.test.html`, see its README).
