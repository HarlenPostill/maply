"""maply API. Runs on localhost; serves the app, job images, prompts and the capture extension's endpoints."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import binascii
import hashlib
import io
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from geoalchemy2.shape import from_shape, to_shape
from PIL import Image
from pydantic import BaseModel, Field
from shapely.geometry import LineString, MultiPolygon, Point, mapping, shape
from sqlalchemy import BigInteger, func, insert, select
from sqlalchemy.orm import Session, defer
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import dataset, osm, results, tagging, provider_settings, capture, backlog
from app.config import get_settings
from app.db import get_db
from app.geo.planning import PlanningError
from app.models import Job, Segment, Shot, TagTask
from app.shots import JobOptions, plan_shots, slugify, write_job_folder

settings = get_settings()
settings.jobs_dir.mkdir(parents=True, exist_ok=True)

@asynccontextmanager
async def lifespan(app):
    from app.openrouter_worker import serve
    stop = asyncio.Event()
    worker = asyncio.create_task(serve(stop)) if settings.tagging_autostart else None
    yield
    stop.set()
    if worker:
        await worker


app = FastAPI(lifespan=lifespan, title="maply", version="0.3.0", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[h.strip() for h in os.environ.get("ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(",") if h.strip()],
)
app.include_router(tagging.router)
app.include_router(provider_settings.router)
app.include_router(capture.router)

SCOPE = MultiPolygon([shape(f["geometry"]) for f in json.loads(settings.scope_geojson_path.read_text())["features"]])
DONE = ("captured", "accepted", "no_imagery", "blocked", "needs_check")
TO_TAKE = ("pending", "retake")
STREETS_INLINE_LIMIT = 400  # bigger jobs don't send their street lines to the browser
PREVIEW_SHOTS_LIMIT = 6000  # bigger previews show a summary only


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


class AreaRequest(BaseModel):
    area: dict


class PlanRequest(BaseModel):
    streets: dict | None = None  # FeatureCollection of LineStrings (name in properties)
    whole_city: bool = False  # every street inside the City of Sydney boundary
    options: JobOptions = Field(default_factory=JobOptions)


class JobCreate(PlanRequest):
    name: str
    area: dict | None = None


class ShotUpdate(BaseModel):
    status: str | None = Field(None, pattern="^(accepted|retake|pending|skipped)$")
    note: str | None = None


class CaptureRequest(BaseModel):
    shot_id: uuid.UUID
    image: str  # data URL from chrome.tabs.captureVisibleTab
    page_url: str  # address bar after lining up: records the real camera position and heading
    image_date_text: str | None = None  # e.g. "Image capture: Mar 2023", read from the page
    device_pixel_ratio: float | None = None
    viewport: dict | None = None


class MarkRequest(BaseModel):
    shot_id: uuid.UUID
    status: str = Field(pattern="^(no_imagery|blocked|skipped|pending)$")
    page_url: str | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _job(s: Session, jid: uuid.UUID) -> Job:
    j = s.get(Job, jid)
    if j is None:
        raise HTTPException(404, "Job not found")
    return j


def _counts(s: Session, jid) -> dict:
    c = {k: n for k, n in s.execute(select(Shot.status, func.count()).where(Shot.job_id == jid).group_by(Shot.status)).all()}
    c["total"] = sum(c.values())
    c["done"] = sum(c.get(k, 0) for k in DONE)
    c["manual_review"] = s.scalar(select(func.count()).select_from(Shot).where(Shot.job_id == jid, backlog.pending_review()))
    return c


def _shot_dict(sh: Shot, slug: str, include_labels: bool = True) -> dict:
    r = sh.result or {}
    v = r.get("sha256") or int(r.get("saved_at") or 0)
    evidence = settings.jobs_dir / slug / 'capture-review' / f'{sh.id}.jpg'
    review_image = f'/api/capture/review/{sh.id}/image' if 'manual_review' in (sh.flags or []) and evidence.is_file() else None
    return {
        "review_image_url": review_image,
        "id": str(sh.id), "code": sh.code, "ordinal": sh.ordinal, "batch": sh.batch, "street": sh.street, "kind": sh.kind,
        "side": sh.side, "footpath": sh.footpath, "view_kind": sh.view_kind, "lat": sh.lat, "lng": sh.lng, "heading": sh.heading,
        "pitch": sh.pitch, "fov": sh.fov, "url": sh.url, "filename": sh.filename, "status": sh.status, "flags": sh.flags,
        "note": sh.note, "near_node_m": sh.near_node_m, "captured_at": sh.captured_at.isoformat() if sh.captured_at else None,
        "capture_source": sh.capture_source, "result": r, "look": r.get("look", ""),
        "segment_key": sh.segment_key, "labels": tagging.labels_for(sh) if include_labels else None, "tagging": tagging.task_dict(sh),
        "confidence": tagging.shot_confidence(sh),
        "image_url": f"/files/{slug}/{r['file']}?v={v}" if r.get("file") else None,
        "thumb_url": f"/files/{slug}/{r['thumb']}?v={v}" if r.get("thumb") else None,
    }


def _job_dict(s: Session, j: Job, full: bool = False) -> dict:
    counts = _counts(s, j.id)
    saved_bytes, saved_n = s.execute(
        select(func.coalesce(func.sum(Shot.result["bytes"].astext.cast(BigInteger)), 0), func.count(Shot.result["bytes"].astext))
        .where(Shot.job_id == j.id, Shot.result.has_key("bytes"))
    ).one()
    out = {
        "id": str(j.id), "name": j.name, "slug": j.slug, "created_at": j.created_at.isoformat() if j.created_at else None,
        "checked_at": j.checked_at.isoformat() if j.checked_at else None, "options": j.params, "batch_size": j.batch_size,
        "counts": counts, "folder": f"{settings.host_jobs_dir}/{j.slug}",
        "batches": s.execute(select(func.max(Shot.batch)).where(Shot.job_id == j.id)).scalar() or 0,
        "storage": {
            "saved_bytes": int(saved_bytes),
            "estimated_total_bytes": int(saved_bytes / saved_n * counts["total"]) if saved_n else None,
        },
    }
    if full:
        out["area"] = mapping(to_shape(j.area)) if j.area is not None else None
        feats = (j.streets or {}).get("features", [])
        out["streets"] = j.streets if len(feats) <= STREETS_INLINE_LIMIT else None
        out["street_count"] = len(feats)
        b = s.execute(select(func.ST_Extent(Shot.geom)).where(Shot.job_id == j.id)).scalar()
        out["bounds"] = _parse_box(b)
    return out


def _parse_box(b: str | None):
    if not b:
        return None
    a, c = b[4:-1].split(",")
    w, s_ = map(float, a.split())
    e, n = map(float, c.split())
    return [w, s_, e, n]


def _plan_inputs(req: PlanRequest) -> dict:
    if req.whole_city:
        try:
            return osm.whole_scope_streets(SCOPE)
        except osm.StreetLookupError as e:
            raise HTTPException(422, str(e)) from e
    if not req.streets:
        raise HTTPException(422, "Choose some streets first")
    return req.streets


def _extension(x_maply: str | None = Header(default=None)):
    """Custom header: makes cross-site requests from ordinary web pages impossible without a CORS preflight."""
    if x_maply != "extension":
        raise HTTPException(403, "Extension requests must send the X-Maply header")


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


@app.get("/api/status")
def status():
    return {
        "version": "0.3.0",
        "jobs_folder": settings.host_jobs_dir,
        "browser": settings.browser_app,
        "basemap": {"url": settings.basemap_url, "attribution": settings.basemap_attribution},
        "scope": mapping(SCOPE),
        "local_streets": osm.local_streets_available(),
    }


@app.post("/api/streets")
def find_streets(req: AreaRequest):
    try:
        return osm.find_streets(req.area)
    except osm.StreetLookupError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/preview")
def preview(req: PlanRequest):
    streets = _plan_inputs(req)
    try:
        shots, _segments, summary, warnings = plan_shots(streets, req.options, scope=SCOPE)
    except PlanningError as e:
        raise HTTPException(422, str(e)) from e
    small = len(shots) <= PREVIEW_SHOTS_LIMIT
    return {
        "summary": summary,
        "warnings": warnings,
        "shots": [{"code": x.code, "lat": x.lat, "lng": x.lng, "heading": x.heading, "fov": x.fov, "side": x.side, "kind": x.kind, "street": x.street, "look": x.look, "url": x.url} for x in shots] if small else [],
        "shots_omitted": not small,
    }


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


@app.get("/api/jobs")
def list_jobs(s: Session = Depends(get_db)):
    return [_job_dict(s, j) for j in s.execute(select(Job).order_by(Job.created_at.desc())).scalars()]


@app.post("/api/jobs")
def create_job(req: JobCreate, s: Session = Depends(get_db)):
    name = req.name.strip() or ("City of Sydney" if req.whole_city else "Untitled job")
    try:
        j, warnings = add_job(s, name, _plan_inputs(req), req.options, req.whole_city, shape(req.area) if req.area else None)
    except PlanningError as e:
        raise HTTPException(422, str(e)) from e
    s.commit()
    out = _job_dict(s, j, full=True)
    out["warnings"] = warnings
    return out


def add_job(s: Session, name: str, streets: dict, options: JobOptions, whole_city: bool = False, area=None) -> tuple[Job, list[str]]:
    """Plan the shots and add the job, its segments and shots, and its folder. The caller commits."""
    shots, segments, summary, warnings = plan_shots(streets, options, scope=SCOPE)
    slug, n = slugify(name), 2
    while s.execute(select(Job.id).where(Job.slug == slug)).first() or (settings.jobs_dir / slug).exists():
        slug, n = f"{slugify(name)}-{n}", n + 1
    area = SCOPE if whole_city else area
    j = Job(
        name=name, slug=slug, streets=streets, params={**options.model_dump(), "whole_city": whole_city},
        batch_size=options.batch_size, area=from_shape(area, srid=4326) if area is not None else None,
    )
    s.add(j)
    s.flush()
    rows = [
        {"id": uuid.uuid4(), "job_id": j.id, "key": g["key"], "ordinal": g["ordinal"], "street": g["street"], "geom": from_shape(LineString(g["coords"]), srid=4326),
         "bearing_deg": g["bearing_deg"], "length_m": g["length_m"], "osm": g["osm"]}
        for g in segments
    ]
    for i in range(0, len(rows), 5000):
        s.execute(insert(Segment), rows[i : i + 5000])
    rows = [
        {"id": uuid.uuid4(), "job_id": j.id, "code": x.code, "ordinal": x.ordinal, "batch": x.batch, "segment_key": x.segment_key, "street": x.street,
         "kind": x.kind, "side": x.side, "view_kind": x.view_kind, "lat": x.lat, "lng": x.lng, "geom": from_shape(Point(x.lng, x.lat), srid=4326),
         "heading": x.heading, "pitch": x.pitch, "fov": x.fov, "url": x.url, "filename": x.filename, "footpath": x.footpath,
         "near_node_m": x.near_node_m, "status": "pending", "flags": [], "result": {"look": x.look}, "note": ""}
        for x in shots
    ]
    for i in range(0, len(rows), 5000):
        s.execute(insert(Shot), rows[i : i + 5000])
    write_job_folder(slug, name, options, shots, summary)
    return j, warnings


@app.get("/api/jobs/{jid}")
def get_job(jid: uuid.UUID, s: Session = Depends(get_db)):
    return _job_dict(s, _job(s, jid), full=True)


@app.get("/api/jobs/{jid}/shots")
def list_shots(
    jid: uuid.UUID,
    bbox: str | None = Query(None, description="west,south,east,north"),
    status: str | None = None,
    street: str | None = None,
    segment_key: str | None = None,
    side: str | None = Query(None, pattern="^(left|right|none)$"),
    tagging_status: str | None = Query(None, pattern="^(unlabelled|queued|running|completed|failed|cancelled)$"),
    include_labels: bool = True,
    manual_backlog: bool = False,
    batch: int | None = None,
    order: str = Query("ordinal", pattern="^(ordinal|recent)$"),
    offset: int = 0,
    limit: int = Query(200, le=5000),
    s: Session = Depends(get_db),
):
    j = _job(s, jid)
    q = select(Shot).where(Shot.job_id == jid)
    if manual_backlog:
        q = q.where(backlog.pending_review())
    if bbox:
        w, so, e, n = (float(v) for v in bbox.split(","))
        q = q.where(Shot.geom.intersects(func.ST_MakeEnvelope(w, so, e, n, 4326)))
    if status:
        q = q.where(Shot.status.in_(status.split(",")))
    if street is not None:
        q = q.where(Shot.street == street)
    if segment_key is not None:
        q = q.where(Shot.segment_key == segment_key)
    if side is not None:
        q = q.where(Shot.side == side)
    if tagging_status:
        q = q.outerjoin(TagTask, TagTask.shot_id == Shot.id).where(tagging.eligible(), tagging.task_status() == tagging_status)
    if batch:
        q = q.where(Shot.batch == batch)
    total = s.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    q = q.order_by(Shot.captured_at.desc().nulls_last(), Shot.ordinal) if order == "recent" else q.order_by(Shot.ordinal)
    if not include_labels:
        q = q.options(defer(Shot.labels))
    items = [_shot_dict(x, j.slug, include_labels) for x in s.execute(q.offset(offset).limit(limit)).scalars()]
    return {"total": total, "items": items}


@app.get("/api/shots/{sid}")
def get_shot(sid: uuid.UUID, s: Session = Depends(get_db)):
    sh = s.get(Shot, sid)
    if sh is None:
        raise HTTPException(404, "Shot not found")
    return _shot_dict(sh, s.get(Job, sh.job_id).slug)


@app.patch("/api/shots/{sid}")
def update_shot(sid: uuid.UUID, req: ShotUpdate, s: Session = Depends(get_db)):
    sh = _manual_shot(s, sid)
    if sh is None:
        raise HTTPException(404, "Shot not found")
    if req.status:
        backlog.resolve(s, sh)
        sh.status = req.status
    if req.note is not None:
        sh.note = req.note
    s.commit()
    return _shot_dict(sh, s.get(Job, sh.job_id).slug)


@app.delete("/api/jobs/{jid}")
def delete_job(jid: uuid.UUID, delete_files: bool = False, s: Session = Depends(get_db)):
    j = _job(s, jid)
    if delete_files:
        shutil.rmtree(settings.jobs_dir / j.slug, ignore_errors=True)
    s.delete(j)
    s.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# Legacy file reconciliation
# --------------------------------------------------------------------------


@app.post("/api/jobs/{jid}/check")
def check(jid: uuid.UUID, s: Session = Depends(get_db)):
    """Reconcile historical image files and capture metadata with shots. Unchanged files are not re-read."""
    j = _job(s, jid)
    changed, unknown = _check_files(s, j)
    out = _job_dict(s, j, full=True)
    out.update(unknown_log_entries=unknown, changed=changed)
    return out


def _check_files(s: Session, j: Job, *, unfinished_only: bool = False):
    """Import disk results into the shared progress used by prompts and Chrome."""
    root = settings.jobs_dir / j.slug
    logs = results.read_logs(root / "results")
    bearings = dict(s.execute(select(Segment.key, Segment.bearing_deg).where(Segment.job_id == j.id)).all())
    codes_with_files = set()
    images = root / "images"
    if images.is_dir():
        for dirpath, _dirs, names in os.walk(images):
            for nm in names:
                codes_with_files.add(nm.split("_")[0].rsplit(".", 1)[0])
    changed, known = 0, set()
    q = select(Shot).where(Shot.job_id == j.id, ~capture.reserved_shot()).execution_options(yield_per=2000)
    if unfinished_only:
        q = q.where(Shot.status.in_(TO_TAKE), Shot.code.in_(codes_with_files | set(logs)))
    for sh in s.execute(q).scalars():
        known.add(sh.code)
        if sh.code not in codes_with_files and sh.code not in logs and not (sh.result or {}).get("file"):
            continue
        before = (sh.status, list(sh.flags or []))
        status_, flags, res = results.evaluate(sh, root, logs.get(sh.code), bearings.get(sh.segment_key))
        res["look"] = (sh.result or {}).get("look", "")
        if status_ in ("captured", "needs_check") and sh.captured_at is None:
            sh.captured_at = datetime.fromtimestamp(res.get("saved_at") or datetime.now().timestamp(), timezone.utc)
            sh.capture_source = sh.capture_source or "legacy"
        sh.status, sh.flags, sh.result = status_, flags, res
        changed += before != (status_, flags)
    if changed or not unfinished_only:
        j.checked_at = datetime.now(timezone.utc)
    s.commit()
    return changed, sorted(k for k in logs if k not in known)


@app.post("/api/jobs/{jid}/dataset")
def export_dataset(jid: uuid.UUID, s: Session = Depends(get_db)):
    return dataset.export(s, _job(s, jid))


# --------------------------------------------------------------------------
# Chrome extension: step through shots, save each lined-up view
# --------------------------------------------------------------------------


def _next_shot(s: Session, jid, after: int, include_skipped: bool, backlog_only: bool = False) -> Shot | None:
    wanted = TO_TAKE + (("skipped", "needs_check") if backlog_only else (("skipped",) if include_skipped else ()))
    q = select(Shot).where(Shot.job_id == jid, Shot.status.in_(wanted), ~capture.reserved_shot())
    if backlog_only:
        q = q.where(backlog.pending_review())
    nxt = s.execute(q.where(Shot.ordinal > after).order_by(Shot.ordinal).limit(1)).scalar_one_or_none()
    return nxt or s.execute(q.order_by(Shot.ordinal).limit(1)).scalar_one_or_none()


def _ext_payload(s: Session, j: Job, shot: Shot | None, **extra) -> dict:
    return {"job": {"id": str(j.id), "name": j.name}, "shot": _shot_dict(shot, j.slug) if shot else None, "progress": _counts(s, j.id), **extra}


@app.get("/api/ext/next", dependencies=[Depends(_extension)])
def ext_next(job_id: uuid.UUID, after: int = 0, include_skipped: bool = False,
             current: uuid.UUID | None = None, backlog_only: bool = False, s: Session = Depends(get_db)):
    j = _job(s, job_id)
    shot = s.get(Shot, current) if current else None
    if shot is not None and shot.job_id == j.id:
        in_backlog = bool(s.scalar(select(Shot.id).where(Shot.id == shot.id, backlog.pending_review())))
        eligible_current = in_backlog if backlog_only else (shot.status in TO_TAKE or (include_skipped and shot.status == "skipped"))
        if eligible_current and not s.scalar(select(Shot.id).where(Shot.id == shot.id, capture.reserved_shot())):
            return _ext_payload(s, j, shot)
        after = shot.ordinal
    return _ext_payload(s, j, _next_shot(s, j.id, after, include_skipped, backlog_only))


@app.get("/api/ext/shot/{sid}", dependencies=[Depends(_extension)])
def ext_shot(sid: uuid.UUID, s: Session = Depends(get_db)):
    sh = s.get(Shot, sid)
    if sh is None:
        raise HTTPException(404, "Shot not found")
    return _ext_payload(s, s.get(Job, sh.job_id), sh)


@app.post("/api/ext/capture", dependencies=[Depends(_extension)])
def ext_capture(req: CaptureRequest, include_skipped: bool = False, backlog_only: bool = False, s: Session = Depends(get_db)):
    sh = _manual_shot(s, req.shot_id)
    out = _save_capture(req, include_skipped, s, commit=False, backlog_only=backlog_only)
    backlog.resolve(s, sh)
    s.commit()
    # Resolve the backlog before selecting its next photo.
    out['shot'] = _shot_dict(nxt, s.get(Job, sh.job_id).slug) if (nxt := _next_shot(s, sh.job_id, sh.ordinal, include_skipped, backlog_only)) else None
    out['progress'] = _counts(s, sh.job_id)
    return out


def _manual_shot(s, sid):
    sh = s.scalar(select(Shot).where(Shot.id == sid).with_for_update())
    if sh and s.scalar(select(Shot.id).where(Shot.id == sid, capture.reserved_shot())):
        raise HTTPException(409, "Photo reserved by a capture worker; pause the worker and wait for its lease to expire")
    return sh


def _save_capture(req: CaptureRequest, include_skipped: bool, s: Session, *, commit: bool = True, backlog_only: bool = False):
    sh = s.get(Shot, req.shot_id)
    if sh is None:
        raise HTTPException(404, "Shot not found")
    j = s.get(Job, sh.job_id)
    try:
        header, b64 = req.image.split(",", 1)
        data = base64.b64decode(b64, validate=True)
        with Image.open(io.BytesIO(data)) as im:
            w, h, fmt = im.width, im.height, (im.format or "").lower()
    except (ValueError, binascii.Error, OSError) as e:
        raise HTTPException(422, f"The capture isn't a readable image: {e}") from e
    if fmt not in ("jpeg", "png"):
        raise HTTPException(422, f"Unsupported image format {fmt}")
    root = settings.jobs_dir / j.slug
    dest = (root / "images" / sh.filename).with_suffix(".jpg" if fmt == "jpeg" else ".png")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)  # atomic: a crash never leaves half a photo
    thumb = results.thumb_for(root, dest)
    results.make_thumb(dest, thumb)
    seg_bearing = s.execute(select(Segment.bearing_deg).where(Segment.job_id == j.id, Segment.key == sh.segment_key)).scalar()
    flags, cres = results.camera_check(sh, req.page_url, req.image_date_text, seg_bearing)
    st = dest.stat()
    sh.result = {
        **cres, "look": (sh.result or {}).get("look", ""), "file": str(dest.relative_to(root)), "thumb": str(thumb.relative_to(root)),
        "sha256": hashlib.sha256(data).hexdigest(), "width": w, "height": h, "format": fmt, "bytes": st.st_size, "saved_at": st.st_mtime,
        "device_pixel_ratio": req.device_pixel_ratio, "viewport": req.viewport,
    }
    sh.flags = flags
    sh.status = "needs_check" if results.SERIOUS & set(flags) else "captured"
    sh.captured_at = datetime.now(timezone.utc)
    sh.capture_source = "extension"
    if commit:
        s.commit()
    else:
        s.flush()
    saved = {"code": sh.code, "status": sh.status, "flags": flags, "camera_offset_m": cres.get("camera_offset_m"), "heading_delta": cres.get("heading_delta")}
    return _ext_payload(s, j, _next_shot(s, j.id, sh.ordinal, include_skipped, backlog_only), saved=saved)


@app.post("/api/ext/mark", dependencies=[Depends(_extension)])
def ext_mark(req: MarkRequest, include_skipped: bool = False, backlog_only: bool = False, s: Session = Depends(get_db)):
    sh = _manual_shot(s, req.shot_id)
    if sh is None:
        raise HTTPException(404, "Shot not found")
    j = s.get(Job, sh.job_id)
    if req.status != "skipped":
        backlog.resolve(s, sh)
    sh.status = req.status
    if req.page_url:
        sh.result = {**(sh.result or {}), "marked_at_url": req.page_url}
    s.commit()
    return _ext_payload(s, j, _next_shot(s, j.id, sh.ordinal, include_skipped, backlog_only), saved={"code": sh.code, "status": sh.status, "flags": []})


# --------------------------------------------------------------------------
# Files and the built app
# --------------------------------------------------------------------------


@app.get("/files/{slug}/{rel:path}", include_in_schema=False)
def job_file(slug: str, rel: str):
    root = (settings.jobs_dir / slug).resolve()
    p = (root / rel).resolve()
    if root not in p.parents or p.suffix.lower() not in (".png", ".jpg", ".jpeg") or not p.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=86400"})


_DIST = Path(os.environ.get("FRONTEND_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist"))
if (_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith(("api/", "files/")):
            raise HTTPException(404)
        return FileResponse(_DIST / "index.html", headers={"Cache-Control": "no-store"})

