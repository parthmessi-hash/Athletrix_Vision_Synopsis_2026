"""Session preflight: what is ready for (1) recording raw data, (2) reviewing camera tracking and (3) calculating the
experimental recommendation, kept separate so that, for example, a missing rubric makes the score unavailable without
blocking a valid raw recording. Read-only: nothing is started, registered or stored.

Checks made by the engine: database writable, free storage, the active recording (none / this plan's / another),
registered athletes, recent samples and device status reports per planned athlete, the configured profiles and, per
athlete expecting physiology, a usable baseline. Local files (calibration, time mapping) are checked by
scripts/sideline_session.py, which runs where the files are; the engine never reads them.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter

from app.core import clock
from app.core.config import get_settings
from app.core.physiology_index import iso, select_baseline
from app.db import get_database
from app.models.session import SessionPlan
from app.store.athletes import athlete_registry
from app.store.baselines import baseline_store
from app.store.device import latest_device_status_store
from app.store.physiological import physiological_store
from app.store.recordings import recording_store
from app.store.tracking import tracking_store

router = APIRouter(tags=["session"])
RECENT_S = 30.0


def plan_sha256(plan: SessionPlan) -> str:
    return hashlib.sha256(plan.model_dump_json().encode()).hexdigest()


def _profiles() -> dict:
    from app.api.review import configured_profiles  # noqa: PLC0415
    return configured_profiles()


def preflight(plan: SessionPlan | None) -> dict:
    now = clock.utc_now()
    s = get_settings()
    db = get_database()
    checks: dict[str, list] = {"raw_recording": [], "camera_review": [], "experimental_recommendation": []}

    def add(area, level, text):   # level: ok | warn | block
        checks[area].append({"level": level, "text": text})

    # engine and storage
    try:
        with db.transaction() as c:
            c.execute("SELECT 1")
        add("raw_recording", "ok", "database accepts writes")
    except Exception as exc:  # noqa: BLE001
        add("raw_recording", "block", f"database does not accept writes ({type(exc).__name__}: {exc})")
    free_mb = None
    if db.path != ":memory:":
        free_mb = shutil.disk_usage(Path(db.path).expanduser().resolve().parent).free / 1e6
        need = plan.min_free_storage_mb if plan else 500
        add("raw_recording", "ok" if free_mb >= need else "block", f"{free_mb:,.0f} MB free next to the database (plan needs {need:g} MB)")
    active = recording_store.active()
    sha = plan_sha256(plan) if plan else None
    started = None
    if active is not None:
        meta = (recording_store.configuration(active.id).snapshot or None)
        op = meta.operator_supplied.metadata if meta and meta.operator_supplied else {}
        if sha and op.get("session_plan_sha256") == sha:
            started = active.id
            add("raw_recording", "ok", f"this plan's recording {active.id} is already active (starting again resumes it)")
        else:
            add("raw_recording", "block", f"another recording is active ({active.id}, \"{active.name}\"); stop it first")
    else:
        add("raw_recording", "ok", "no recording is active")
    athletes = []
    if plan:
        on_field = [a for a in plan.athletes if a.status == "on_field"]
        if not on_field:
            add("experimental_recommendation", "warn", "nobody is planned on the field")
        idx = _profiles()["profiles"]["physiological_index"]
        idx_prof = None
        if idx and idx.get("valid"):
            from app.api.review import _index_profile  # noqa: PLC0415
            idx_prof = _index_profile()
        for a in plan.athletes:
            reg = athlete_registry.get(a.id)
            row = {"id": a.id, "registered": reg is not None, "status": a.status, "inputs": a.inputs.model_dump(mode="json")}
            if reg is not None and (reg.name != a.name or reg.jersey_number != a.jersey_number):
                add("raw_recording", "warn", f"{a.id} is registered as \"{reg.name}\" #{reg.jersey_number}; the plan says \"{a.name}\" #{a.jersey_number} (the registry is not changed)")
            newest = physiological_store.get(a.id) if reg else None
            age = (now - newest.timestamp).total_seconds() if newest else None
            row["wearable"] = {"newest_reading_age_s": None if age is None else round(age, 1), "device_id": newest.device_id if newest else None,
                               "receiving": age is not None and age <= RECENT_S}
            if a.wearable_device_id:
                ds = latest_device_status_store.get(a.wearable_device_id)
                row["wearable"]["device_status"] = None if ds is None else {"connected": ds.connected, "reported_at": iso(ds.timestamp), "battery_percent": ds.battery_percent}
                if newest and newest.device_id and newest.device_id != a.wearable_device_id:
                    add("raw_recording", "warn", f"{a.id}: newest reading came from {newest.device_id}, the plan assigns {a.wearable_device_id}")
            if a.inputs.physiology == "expected":
                if not row["wearable"]["receiving"]:
                    add("raw_recording", "warn", f"{a.id}: no wearable reading in the last {RECENT_S:g} s (recording can start; physiology will be missing until it arrives)")
                if idx_prof:
                    b, timing, why = select_baseline(baseline_store.list(a.id), idx_prof[0], now)
                    row["baseline"] = {"usable": b is not None, "id": b.id if b else None, "reasons": why}
                    if b is None and a.status == "on_field":
                        add("experimental_recommendation", "warn", f"{a.id}: no usable baseline ({'; '.join(why) or 'none recorded'}): this player's review will say insufficient evidence")
            tn = tracking_store.get(a.id) if reg else None
            row["tracking"] = {"newest_age_s": None if tn is None else round((now - tn.timestamp).total_seconds(), 1)}
            athletes.append(row)
        if plan.sources.camera.selected:
            recent = [r for r in athletes if r["tracking"]["newest_age_s"] is not None and r["tracking"]["newest_age_s"] <= RECENT_S]
            add("camera_review", "ok" if recent else "warn", f"tracking samples received in the last {RECENT_S:g} s for {len(recent)} of {len(athletes)} planned athletes")
            add("camera_review", "warn", "calibration, lens model and time mapping are local files: run scripts/sideline_session.py preflight to check them")
        else:
            add("camera_review", "ok", "camera not selected in the plan: nothing to review")
        if plan.fictional:
            for k in checks:
                add(k, "warn", "FICTIONAL plan: a demonstration, not a real session")
    # profiles
    prof = _profiles()
    for kind, label in (("substitution", "substitution profile"), ("physiological_index", "physiological index profile"), ("performance_rubric", "performance rubric")):
        r = prof["profiles"][kind]
        if r is None:
            add("experimental_recommendation", "block", f"no {label} is configured: the experimental recommendation is unavailable (raw recording is unaffected)")
        elif not r.get("valid"):
            add("experimental_recommendation", "block", f"the {label} is invalid: {'; '.join(r.get('errors', []))}")
        else:
            add("experimental_recommendation", "warn" if r.get("fictional") else "ok", f"{label} {r.get('version', '')} configured" + (" (FICTIONAL)" if r.get("fictional") else ""))
    for p in prof.get("compatibility") or []:
        add("experimental_recommendation", "warn", f"profile check: {p}")

    def verdict(items):
        return "blocked" if any(i["level"] == "block" for i in items) else ("ready_with_warnings" if any(i["level"] == "warn" for i in items) else "ready")
    return {"checked_at": iso(now), "engine": {"application": s.app_name, "version": s.app_version, "code_revision": s.code_revision},
            "free_storage_mb": None if free_mb is None else round(free_mb), "plan": None if plan is None else {"name": plan.name, "sha256": sha, "fictional": plan.fictional},
            "active_recording": None if active is None else {"id": active.id, "name": active.name, "started_at": iso(active.started_at), "is_this_plan": started is not None},
            "readiness": {k: {"verdict": verdict(v), "checks": v} for k, v in checks.items()}, "athletes": athletes,
            "note": "Read-only. Readiness for raw recording, camera review and the experimental recommendation is separate; a missing wearable is reported as missing, never filled in."}


@router.get("/preflight")
def get_preflight() -> dict:
    """Engine-side readiness without a plan (storage, active recording, profiles)."""
    return preflight(None)


@router.post("/preflight")
def post_preflight(plan: SessionPlan) -> dict:
    """Readiness for this session plan (nothing is started or stored)."""
    return preflight(plan)
