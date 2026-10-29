"""Portable recording packages: a versioned ZIP that carries one stopped recording to another local engine.

FORMAT (format "athletrix-recording-package", format_version 2; version 1 is still read)
  A ZIP archive (deflate) with exactly these members, all UTF-8:
    manifest.json   format, format_version, generated_at, the recording reference, member list with SHA-256 and byte
                    counts, record counts, and `settings_at_export` (the engine settings in force when the package was
                    written; NOT necessarily the settings in force while the data was acquired or reviewed)
    recording.json  the recording: original id, name, status ("stopped"), started_at and stopped_at (the backend's
                    clock while recording), the roster snapshot (`athletes`, in roster order) and, from version 2,
                    `configuration_at_start`: the snapshot captured when the recording started (app/models/configuration.py),
                    or null when the recording has none. Version 1 packages have no such key: unavailable, never inferred.
    samples.jsonl   every recorded sample of every roster athlete, both streams, in the existing replay-compatible
                    interchange format: one {"type": "physiological"|"tracking", "sample": {...}} per line, ordered by
                    athlete in roster order, then recorded instant, physiological before tracking at equal instants;
                    payloads exactly as stored (timestamps with their offsets, ids, sources, nulls, zeros, confidences)
    periods.json    the saved effort periods: original id, athlete_id, label, effort_start, effort_end (as saved) and
                    created_at
  The manifest's checksums and counts let an importer detect accidental corruption; they are not a signature, and a
  package proves nothing about the accuracy of the measurements it carries. It is not a database backup: no registry,
  rolling history, device status, other recordings, configuration files, credentials, paths or logs are included.

IMPORT POLICY (see scripts/recording_package.py)
  The whole package is validated before anything is written; the import writes the recording (as stopped, with its
  original start and stop times), its roster snapshot, every sample and every saved period in one transaction under
  newly generated ids, plus a provenance row mapping the new ids to the original ones. Samples are historical: they keep
  their recorded timestamps and are never rebased, and live-ingestion rules that depend on arrival time are not
  re-applied, except that no sample may be timestamped more than the live skew allowance after the recording's stop
  time (a live engine could not have accepted it). Imports never touch the athlete registry, rolling history, device
  status or an active recording.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import ValidationError

from app.core.clock import MAX_FUTURE_TIMESTAMP_SKEW
from app.core.effort import MAX_EFFORT_SECONDS, MIN_EFFORT_SECONDS, EffortRequestError, parse_effort_timestamp
from app.core.export import export_records
from app.db import to_utc_iso
from app.models import Athlete, PhysiologicalSample, TrackingSample
from app.models.configuration import RecordingConfiguration

FORMAT = "athletrix-recording-package"
FORMAT_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)
MEMBERS = ("manifest.json", "recording.json", "samples.jsonl", "periods.json")
DATA_MEMBERS = ("recording.json", "samples.jsonl", "periods.json")
MODELS = {"physiological": PhysiologicalSample, "tracking": TrackingSample}

# Bounds on what a reader will process (accidental or hostile oversized archives are refused, never extracted).
MAX_MEMBER_BYTES = 1024 * 1024 * 1024        # 1 GiB uncompressed per member
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024   # 2 GiB uncompressed in total
MAX_LINE_BYTES = 64 * 1024                   # one sample record
MAX_ERRORS_REPORTED = 50


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- writing ------------------------------------------------------------------------------------------------------------

def build_package(recording: dict, athletes: Sequence[Athlete], samples: dict[str, dict[str, list]], periods: Sequence, settings: dict, generated_at: datetime,
                  configuration: RecordingConfiguration | None = None) -> bytes:
    """The ZIP bytes for one stopped recording. `samples[athlete_id][stream]` are the stored models, oldest first."""
    rec_doc = {**recording, "athletes": [a.model_dump(mode="json") for a in athletes],
               "configuration_at_start": configuration.model_dump(mode="json") if configuration is not None else None}
    lines = []
    counts = {"physiological": 0, "tracking": 0}
    per_athlete = {}
    for a in athletes:
        p, t = samples.get(a.id, {}).get("physiological", []), samples.get(a.id, {}).get("tracking", [])
        counts["physiological"] += len(p); counts["tracking"] += len(t)
        per_athlete[a.id] = {"physiological": len(p), "tracking": len(t)}
        lines.extend(json.dumps(r, ensure_ascii=False) for r in export_records(p, t))
    periods_doc = [{"id": p.id, "athlete_id": p.athlete_id, "label": p.label, "effort_start": p.effort_start.isoformat().replace("+00:00", "Z"),
                    "effort_end": p.effort_end.isoformat().replace("+00:00", "Z"), "created_at": iso(p.created_at)} for p in periods]
    data = {
        "recording.json": json.dumps(rec_doc, ensure_ascii=False, indent=2).encode("utf-8"),
        "samples.jsonl": ("".join(line + "\n" for line in lines)).encode("utf-8"),
        "periods.json": json.dumps(periods_doc, ensure_ascii=False, indent=2).encode("utf-8"),
    }
    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "generated_at": iso(generated_at),
        "description": "A portable copy of one stopped Athletrix recording: roster snapshot, every recorded sample and the saved effort periods. "
                       "Checksums detect accidental corruption only. Not a database backup and not evidence that the measurements are accurate.",
        "recording": {"id": recording["id"], "name": recording["name"], "started_at": recording["started_at"], "stopped_at": recording["stopped_at"]},
        "counts": {"athletes": len(athletes), "physiological_samples": counts["physiological"], "tracking_samples": counts["tracking"], "samples_by_athlete": per_athlete, "periods": len(periods_doc)},
        "members": {name: {"sha256": sha256(blob), "bytes": len(blob)} for name, blob in data.items()},
        "settings_at_export": {**settings, "note": "Engine settings in force when this package was written. They are not necessarily the settings in force while the data was recorded or reviewed, and an importing engine applies its own settings."},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        for name in DATA_MEMBERS:
            z.writestr(name, data[name])
    return buf.getvalue()


# ---- reading and validating ---------------------------------------------------------------------------------------------

class PackageError(Exception):
    """The package cannot be imported; `errors` lists every problem found (bounded)."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:5]) + (f" (and {len(errors) - 5} more)" if len(errors) > 5 else ""))
        self.errors = errors


@dataclass
class Package:
    manifest: dict
    recording: dict
    athletes: list[Athlete]
    samples: dict[str, dict[str, list]] = field(default_factory=dict)   # athlete_id -> stream -> models, as in the file
    periods: list[dict] = field(default_factory=list)                   # validated: label, start/end datetimes, created_at datetime, original id
    package_sha256: str = ""
    configuration: RecordingConfiguration | None = None                 # start-time snapshot; None = unavailable (version 1, or none recorded)

    def summary(self) -> dict:
        def bounds(models):
            if not models:
                return None
            ts = sorted(m.timestamp for m in models)
            return {"earliest": iso(ts[0]), "latest": iso(ts[-1])}
        return {
            "format": self.manifest["format"], "format_version": self.manifest["format_version"], "generated_at": self.manifest["generated_at"],
            "package_sha256": self.package_sha256,
            "recording": {k: self.recording[k] for k in ("id", "name", "started_at", "stopped_at")},
            "athletes": [{"id": a.id, "name": a.name, "jersey_number": a.jersey_number, "sport": a.sport, "team_id": a.team_id,
                          "physiological": {"count": len(self.samples[a.id]["physiological"]), "bounds": bounds(self.samples[a.id]["physiological"])},
                          "tracking": {"count": len(self.samples[a.id]["tracking"]), "bounds": bounds(self.samples[a.id]["tracking"])},
                          "periods": sum(1 for p in self.periods if p["athlete_id"] == a.id)} for a in self.athletes],
            "counts": {"physiological_samples": sum(len(v["physiological"]) for v in self.samples.values()), "tracking_samples": sum(len(v["tracking"]) for v in self.samples.values()), "periods": len(self.periods)},
            "settings_at_export": self.manifest.get("settings_at_export"),
            "configuration_at_start": {"status": "captured", "snapshot": self.configuration.model_dump(mode="json")} if self.configuration is not None
                                      else {"status": "unavailable", "snapshot": None},
        }


def _parse_dt(value, what: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str):
        errors.append(f"{what} must be an ISO 8601 timestamp with a timezone")
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{what} {value!r} is not a valid timestamp")
        return None
    if dt.tzinfo is None:
        errors.append(f"{what} {value!r} has no timezone")
        return None
    return dt


def read_package(data: bytes) -> Package:
    """Parse and fully validate package bytes; raise PackageError listing the problems. Nothing is written anywhere."""
    errors: list[str] = []

    def fail():
        raise PackageError(errors[:MAX_ERRORS_REPORTED] + ([f"... {len(errors) - MAX_ERRORS_REPORTED} more problems"] if len(errors) > MAX_ERRORS_REPORTED else []))

    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        errors.append(f"not a readable ZIP archive ({exc})")
        fail()
    with z:
        infos = {i.filename: i for i in z.infolist()}
        unexpected = sorted(n for n in infos if n not in MEMBERS)
        if unexpected:
            errors.append(f"unexpected members (only {', '.join(MEMBERS)} are allowed; nothing is extracted): {', '.join(unexpected[:10])}")
        missing = [n for n in MEMBERS if n not in infos]
        if missing:
            errors.append(f"missing required members: {', '.join(missing)}")
        if sum(i.file_size for i in infos.values()) > MAX_PACKAGE_BYTES or any(i.file_size > MAX_MEMBER_BYTES for i in infos.values()):
            errors.append("package members exceed the size this reader processes (1 GiB per member, 2 GiB in total)")
        if errors:
            fail()
        blobs = {}
        for name in MEMBERS:
            try:
                blob = z.read(name)                       # read in memory by exact name; never written to disk
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                errors.append(f"{name}: cannot be read ({exc})")
                continue
            if len(blob) > MAX_MEMBER_BYTES:
                errors.append(f"{name}: larger than declared")
            blobs[name] = blob
        if errors:
            fail()

    try:
        manifest = json.loads(blobs["manifest.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"manifest.json is not valid JSON ({exc})")
        fail()
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        errors.append(f"manifest.json: format must be {FORMAT!r}")
        fail()
    if manifest.get("format_version") not in SUPPORTED_VERSIONS:
        errors.append(f"unsupported format_version {manifest.get('format_version')!r}; this engine reads {', '.join(map(str, SUPPORTED_VERSIONS))}")
        fail()
    members = manifest.get("members")
    if not isinstance(members, dict):
        errors.append("manifest.json: members are missing")
        fail()
    for name in DATA_MEMBERS:
        m = members.get(name)
        if not isinstance(m, dict) or m.get("sha256") != sha256(blobs[name]) or m.get("bytes") != len(blobs[name]):
            errors.append(f"{name}: checksum or size does not match the manifest (the file was changed or damaged)")
    if errors:
        fail()

    try:
        rec = json.loads(blobs["recording.json"].decode("utf-8"))
        periods_raw = json.loads(blobs["periods.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"recording.json or periods.json is not valid JSON ({exc})")
        fail()
    if not isinstance(rec, dict):
        errors.append("recording.json must be an object")
        fail()
    for key in ("id", "name", "status", "started_at", "stopped_at", "athletes"):
        if key not in rec:
            errors.append(f"recording.json: {key} is missing")
    if errors:
        fail()
    if rec["status"] != "stopped":
        errors.append("recording.json: only stopped recordings can be imported")
    if not isinstance(rec["name"], str) or not rec["name"].strip():
        errors.append("recording.json: name must be a non-blank string")
    started, stopped = _parse_dt(rec["started_at"], "recording started_at", errors), _parse_dt(rec["stopped_at"], "recording stopped_at", errors)
    if started and stopped and stopped < started:
        errors.append("recording.json: stopped_at is before started_at")
    if manifest.get("recording", {}).get("id") != rec.get("id"):
        errors.append("manifest and recording.json name different recordings")
    athletes: list[Athlete] = []
    if not isinstance(rec["athletes"], list) or not rec["athletes"]:
        errors.append("recording.json: athletes must be a non-empty list")
    else:
        for n, a in enumerate(rec["athletes"]):
            try:
                athletes.append(Athlete.model_validate(a))
            except ValidationError as exc:
                errors.append(f"recording.json: athlete {n + 1} is invalid ({exc.errors()[0]['msg']})")
        if len({a.id for a in athletes}) != len(athletes):
            errors.append("recording.json: athlete ids repeat in the roster")
    configuration = None
    if "configuration_at_start" in rec:
        if manifest["format_version"] < 2:
            errors.append("recording.json: configuration_at_start is not part of format version 1")
        elif rec["configuration_at_start"] is not None:
            try:
                configuration = RecordingConfiguration.model_validate(rec["configuration_at_start"])
            except ValidationError as exc:
                first = exc.errors()[0]
                errors.append(f"recording.json: configuration_at_start is invalid ({'.'.join(map(str, first['loc']))}: {first['msg']})")
    if errors:
        fail()

    roster = {a.id for a in athletes}
    samples = {a.id: {"physiological": [], "tracking": []} for a in athletes}
    seen: dict[tuple, str] = {}
    latest_allowed = stopped + MAX_FUTURE_TIMESTAMP_SKEW
    try:
        text = blobs["samples.jsonl"].decode("utf-8")
    except UnicodeDecodeError:
        errors.append("samples.jsonl is not UTF-8")
        fail()
    for line_no, line in enumerate(text.splitlines(), start=1):
        if len(errors) > MAX_ERRORS_REPORTED:
            break
        if not line.strip():
            errors.append(f"samples.jsonl line {line_no}: blank line")
            continue
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            errors.append(f"samples.jsonl line {line_no}: longer than {MAX_LINE_BYTES} bytes")
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"samples.jsonl line {line_no}: not valid JSON ({exc.msg})")
            continue
        if not isinstance(record, dict) or set(record) != {"type", "sample"} or record.get("type") not in MODELS:
            errors.append(f"samples.jsonl line {line_no}: must be {{\"type\": \"physiological\"|\"tracking\", \"sample\": {{...}}}}")
            continue
        try:
            model = MODELS[record["type"]].model_validate(record["sample"])
        except ValidationError as exc:
            first = exc.errors()[0]
            errors.append(f"samples.jsonl line {line_no}: invalid {record['type']} sample ({'.'.join(map(str, first['loc']))}: {first['msg']})")
            continue
        if model.athlete_id not in roster:
            errors.append(f"samples.jsonl line {line_no}: athlete {model.athlete_id!r} is not in the recording's roster")
            continue
        if model.timestamp > latest_allowed:
            errors.append(f"samples.jsonl line {line_no}: timestamped {iso(model.timestamp)}, more than {MAX_FUTURE_TIMESTAMP_SKEW.total_seconds():g} s after the recording stopped; a live engine could not have accepted it")
            continue
        key = (model.athlete_id, record["type"], to_utc_iso(model.timestamp))
        canonical = model.model_dump_json()
        if key in seen:
            errors.append(f"samples.jsonl line {line_no}: a second {record['type']} sample for {model.athlete_id} at {iso(model.timestamp)}" + (" with different values (conflicting duplicate)" if seen[key] != canonical else " (duplicate)"))
            continue
        seen[key] = canonical
        samples[model.athlete_id][record["type"]].append(model)
    counts = manifest.get("counts", {})
    got = {"physiological_samples": sum(len(v["physiological"]) for v in samples.values()), "tracking_samples": sum(len(v["tracking"]) for v in samples.values())}
    if not errors and (counts.get("physiological_samples") != got["physiological_samples"] or counts.get("tracking_samples") != got["tracking_samples"] or counts.get("athletes") != len(athletes)):
        errors.append(f"sample or athlete counts do not match the manifest (manifest {counts.get('physiological_samples')}/{counts.get('tracking_samples')}/{counts.get('athletes')}, file {got['physiological_samples']}/{got['tracking_samples']}/{len(athletes)})")

    periods: list[dict] = []
    if not isinstance(periods_raw, list):
        errors.append("periods.json must be a list")
    else:
        ids = set()
        for n, p in enumerate(periods_raw, start=1):
            if not isinstance(p, dict):
                errors.append(f"periods.json item {n}: must be an object")
                continue
            if p.get("athlete_id") not in roster:
                errors.append(f"periods.json item {n}: athlete {p.get('athlete_id')!r} is not in the recording's roster")
                continue
            label = p.get("label")
            if not isinstance(label, str) or not label.strip() or len(label.strip()) > 80:
                errors.append(f"periods.json item {n}: label must be 1 to 80 non-blank characters")
                continue
            if not isinstance(p.get("id"), str) or not p["id"] or p["id"] in ids:
                errors.append(f"periods.json item {n}: id is missing or repeats")
                continue
            ids.add(p["id"])
            try:
                start = parse_effort_timestamp("effort_start", p.get("effort_start"))
                end = parse_effort_timestamp("effort_end", p.get("effort_end"))
            except EffortRequestError as exc:
                errors.append(f"periods.json item {n}: {exc}")
                continue
            created = _parse_dt(p.get("created_at"), f"periods.json item {n} created_at", errors)
            duration = (end - start).total_seconds()
            if not (MIN_EFFORT_SECONDS <= duration <= MAX_EFFORT_SECONDS):
                errors.append(f"periods.json item {n}: duration {duration:g} s must be between {MIN_EFFORT_SECONDS:g} and {MAX_EFFORT_SECONDS:g} s")
                continue
            if created is None:
                continue
            if end > created:
                errors.append(f"periods.json item {n}: ends after it was saved")
                continue
            periods.append({"original_id": p["id"], "athlete_id": p["athlete_id"], "label": label.strip(), "effort_start": start, "effort_end": end, "created_at": created})
        if counts.get("periods") != len(periods_raw):
            errors.append(f"period count does not match the manifest (manifest {counts.get('periods')}, file {len(periods_raw)})")
    if errors:
        fail()
    return Package(manifest=manifest, recording=rec, athletes=athletes, samples=samples, periods=periods, package_sha256=sha256(data), configuration=configuration)


def latest_sample_after_stop(pkg: Package) -> timedelta | None:
    """How far the newest sample lies after the recording's stop time, if it does (informational)."""
    stopped = datetime.fromisoformat(pkg.recording["stopped_at"].replace("Z", "+00:00"))
    newest = max((m.timestamp for v in pkg.samples.values() for s in v.values() for m in s), default=None)
    return (newest - stopped) if newest is not None and newest > stopped else None
