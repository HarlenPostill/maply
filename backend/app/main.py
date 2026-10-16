"""maply API. Runs on localhost; serves the app, the job folders' images and the prompts."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from geoalchemy2.shape import from_shape, to_shape
from pydantic import BaseModel, Field
from shapely.geometry import LineString, MultiPolygon, Point, shape
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import osm, prompts, results
from app.config import get_settings
from app.db import get_db
from app.geo.planning import PlanningError
from app.models import Job, Segment, Shot
from app.shots import JobOptions, plan_shots, slugify, write_job_folder

settings = get_settings()
settings.jobs_dir.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="maply", version="0.2.0", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[h.strip() for h in os.environ.get("ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(",") if h.strip()],
)


def _scope():
    gj = json.loads(settings.scope_geojson_path.read_text())
    return MultiPolygon([shape(f["geometry"]) for f in gj["features"]])


SCOPE = _scope()


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


class AreaRequest(BaseModel):
    area: dict


class PlanRequest(BaseModel):
    streets: dict  # FeatureCollection of LineStrings (name in properties)
    options: JobOptions = Field(default_factory=JobOptions)


class JobCreate(PlanRequest):
    name: str
    area: dict | None = None


class ShotUpdate(BaseModel):
    status: str | None = Field(None, pattern="^(accepted|retake|pending)$")
    note: str | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _job(s: Session, jid: uuid.UUID) -> Job:
    j = s.get(Job, jid)
    if j is None:
        raise HTTPException(404, "Job not found")
    return j


def _counts(s: Session, jid) -> dict:
    rows = s.execute(select(Shot.status, func.count()).where(Shot.job_id == jid).group_by(Shot.status)).all()
    c = {k: n for k, n in rows}
    c["total"] = sum(c.values())
    c["done"] = sum(c.get(k, 0) for k in ("captured", "accepted", "no_imagery", "blocked", "needs_check"))
    return c


def _shot_dict(sh: Shot, slug: str) -> dict:
    r = sh.result or {}
    return {
        "id": str(sh.id), "code": sh.code, "ordinal": sh.ordinal, "batch": sh.batch, "street": sh.street, "kind": sh.kind,
        "side": sh.side, "view_kind": sh.view_kind, "lat": sh.lat, "lng": sh.lng, "heading": sh.heading, "pitch": sh.pitch,
        "fov": sh.fov, "url": sh.url, "filename": sh.filename, "status": sh.status, "flags": sh.flags, "note": sh.note,
        "result": r,
        "image_url": f"/files/{slug}/{r['file']}" if r.get("file") else None,
        "thumb_url": f"/files/{slug}/{r['thumb']}?v={int(r.get('saved_at') or 0)}" if r.get("thumb") else None,
    }


def _job_dict(s: Session, j: Job, full: bool = False) -> dict:
    out = {
        "id": str(j.id), "name": j.name, "slug": j.slug, "created_at": j.created_at.isoformat() if j.created_at else None,
        "checked_at": j.checked_at.isoformat() if j.checked_at else None, "options": j.params, "batch_size": j.batch_size,
        "counts": _counts(s, j.id), "folder": f"{settings.host_jobs_dir}/{j.slug}",
        "batches": s.execute(select(func.max(Shot.batch)).where(Shot.job_id == j.id)).scalar() or 0,
    }
    if full:
        out["area"] = json.loads(json.dumps(to_shape(j.area).__geo_interface__)) if j.area is not None else None
        out["streets"] = j.streets
        out["segments"] = [
            {"key": g.key, "street": g.street, "bearing_deg": g.bearing_deg, "length_m": g.length_m, "coords": list(to_shape(g.geom).coords)}
            for g in s.execute(select(Segment).where(Segment.job_id == j.id).order_by(Segment.ordinal)).scalars()
        ]
        out["shots"] = [_shot_dict(sh, j.slug) for sh in s.execute(select(Shot).where(Shot.job_id == j.id).order_by(Shot.ordinal)).scalars()]
    return out


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/api/status")
def status():
    return {
        "version": "0.2.0",
        "jobs_folder": settings.host_jobs_dir,
        "browser": settings.browser_app,
        "basemap": {"url": settings.basemap_url, "attribution": settings.basemap_attribution},
        "scope": SCOPE.__geo_interface__,
    }


@app.post("/api/streets")
def find_streets(req: AreaRequest):
    try:
        return osm.find_streets(req.area)
    except osm.StreetLookupError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/preview")
def preview(req: PlanRequest):
    try:
        shots, segments, summary, warnings = plan_shots(req.streets, req.options, scope=SCOPE)
    except PlanningError as e:
        raise HTTPException(422, str(e)) from e
    return {
        "summary": summary,
        "warnings": warnings,
        "segments": segments,
        "shots": [{"code": x.code, "lat": x.lat, "lng": x.lng, "heading": x.heading, "fov": x.fov, "side": x.side, "kind": x.kind, "street": x.street, "look": x.look, "url": x.url} for x in shots],
    }


@app.get("/api/jobs")
def list_jobs(s: Session = Depends(get_db)):
    return [_job_dict(s, j) for j in s.execute(select(Job).order_by(Job.created_at.desc())).scalars()]


@app.post("/api/jobs")
def create_job(req: JobCreate, s: Session = Depends(get_db)):
    name = req.name.strip() or "Untitled job"
    try:
        shots, segments, summary, warnings = plan_shots(req.streets, req.options, scope=SCOPE)
    except PlanningError as e:
        raise HTTPException(422, str(e)) from e
    slug, n = slugify(name), 2
    while s.execute(select(Job.id).where(Job.slug == slug)).first() or (settings.jobs_dir / slug).exists():
        slug, n = f"{slugify(name)}-{n}", n + 1
    j = Job(
        name=name, slug=slug, streets=req.streets, params=req.options.model_dump(), batch_size=req.options.batch_size,
        area=from_shape(shape(req.area), srid=4326) if req.area else None,
    )
    s.add(j)
    s.flush()
    for g in segments:
        s.add(Segment(job_id=j.id, key=g["key"], ordinal=g["ordinal"], street=g["street"], geom=from_shape(LineString(g["coords"]), srid=4326), bearing_deg=g["bearing_deg"], length_m=g["length_m"]))
    for x in shots:
        s.add(Shot(
            job_id=j.id, code=x.code, ordinal=x.ordinal, batch=x.batch, segment_key=x.segment_key, street=x.street, kind=x.kind,
            side=x.side, view_kind=x.view_kind, lat=x.lat, lng=x.lng, geom=from_shape(Point(x.lng, x.lat), srid=4326),
            heading=x.heading, pitch=x.pitch, fov=x.fov, url=x.url, filename=x.filename, result={"look": x.look},
        ))
    write_job_folder(slug, name, req.options, shots)
    s.commit()
    out = _job_dict(s, j, full=True)
    out["warnings"] = warnings
    return out


@app.get("/api/jobs/{jid}")
def get_job(jid: uuid.UUID, s: Session = Depends(get_db)):
    return _job_dict(s, _job(s, jid), full=True)


@app.delete("/api/jobs/{jid}")
def delete_job(jid: uuid.UUID, delete_files: bool = False, s: Session = Depends(get_db)):
    j = _job(s, jid)
    if delete_files:
        shutil.rmtree(settings.jobs_dir / j.slug, ignore_errors=True)
    s.delete(j)
    s.commit()
    return {"ok": True}


@app.get("/api/jobs/{jid}/prompt")
def get_prompt(jid: uuid.UUID, batch: int | None = None, remaining: bool = False, s: Session = Depends(get_db)):
    """Prompt for one batch, or for every shot still needing a photo (`remaining=true`)."""
    j = _job(s, jid)
    q = select(Shot).where(Shot.job_id == j.id).order_by(Shot.ordinal)
    total = s.execute(select(func.count()).select_from(Shot).where(Shot.job_id == j.id)).scalar_one()
    batches = s.execute(select(func.max(Shot.batch)).where(Shot.job_id == j.id)).scalar() or 1
    if remaining:
        shots = s.execute(q.where(Shot.status.in_(("pending", "retake", "needs_check")))).scalars().all()[: j.batch_size]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        label, log = "missing photos", f"missing-{stamp}.jsonl"
    else:
        b = batch or 1
        shots = s.execute(q.where(Shot.batch == b)).scalars().all()
        label, log = f"prompt {b} of {batches}", f"prompt-{b:02d}.jsonl"
    data = [{"code": x.code, "url": x.url, "filename": x.filename, "look": (x.result or {}).get("look", "")} for x in shots]
    text = prompts.build_prompt(job_name=j.name, slug=j.slug, shots=data, label=label, log_name=log, total=total) if data else ""
    if text:
        p = settings.jobs_dir / j.slug / "prompts" / (log.replace(".jsonl", ".txt"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return {"text": text, "shots": [d["code"] for d in data], "label": label, "log_file": f"results/{log}", "batches": batches}


@app.post("/api/jobs/{jid}/check")
def check(jid: uuid.UUID, s: Session = Depends(get_db)):
    j = _job(s, jid)
    root = settings.jobs_dir / j.slug
    logs = results.read_logs(root / "results")
    bearings = dict(s.execute(select(Segment.key, Segment.bearing_deg).where(Segment.job_id == j.id)).all())
    changed = 0
    for sh in s.execute(select(Shot).where(Shot.job_id == j.id)).scalars():
        status, flags, res = results.evaluate(sh, root, logs.get(sh.code), bearings.get(sh.segment_key))
        res["look"] = (sh.result or {}).get("look", "")
        if status != sh.status or flags != list(sh.flags or []) or res != sh.result:
            changed += 1
        sh.status, sh.flags, sh.result = status, flags, res
    j.checked_at = datetime.now(timezone.utc)
    s.flush()
    out = _job_dict(s, j, full=True)
    known = {sh["code"] for sh in out["shots"]}
    out["unknown_log_entries"] = sorted(k for k in logs if k not in known)
    (root / "manifest.json").write_text(json.dumps({"job": j.name, "checked_at": j.checked_at.isoformat(), "counts": out["counts"], "shots": out["shots"]}, indent=2, default=str))
    s.commit()
    out["changed"] = changed
    return out


@app.patch("/api/shots/{sid}")
def update_shot(sid: uuid.UUID, req: ShotUpdate, s: Session = Depends(get_db)):
    sh = s.get(Shot, sid)
    if sh is None:
        raise HTTPException(404, "Shot not found")
    if req.status:
        sh.status = req.status
    if req.note is not None:
        sh.note = req.note
    s.commit()
    return _shot_dict(sh, s.get(Job, sh.job_id).slug)


@app.get("/files/{slug}/{rel:path}", include_in_schema=False)
def job_file(slug: str, rel: str):
    root = (settings.jobs_dir / slug).resolve()
    p = (root / rel).resolve()
    if root not in p.parents or p.suffix.lower() not in (".png", ".jpg", ".jpeg") or not p.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(p)


_DIST = Path(os.environ.get("FRONTEND_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist"))
if (_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith(("api/", "files/")):
            raise HTTPException(404)
        return FileResponse(_DIST / "index.html", headers={"Cache-Control": "no-store"})
