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
uvicorn app.main:app --reload
```

Then open:

- http://127.0.0.1:8000/api/v1/health
- http://127.0.0.1:8000/docs (interactive API docs)

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
