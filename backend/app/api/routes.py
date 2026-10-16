from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import shapely
from fastapi import APIRouter, Depends, HTTPException, Query
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api import serializers as ser
from app.capture.completion import finalize_task
from app.config import get_settings
from app.db import get_db
from app.events import emit
from app.geo.planning import PlanningError
from app.geo.projection import within_area_of_use
from app.geo.streetview_url import build_maps_pano_url, parse_streetview_url
from app.models import (
    CapturePlan,
    Crop,
    EgressEvent,
    Event,
    Export,
    Job,
    Observation,
    ObservationLink,
    Project,
    Route,
    Sample,
    SystemCheck,
    Task,
    TaskAttempt,
    Worker,
    WorkerCommand,
)
from app.scheduler import jobs as jobsvc
from app.schemas import (
    CropRequest,
    ExportRequest,
    JobCreate,
    PlanCreate,
    ProjectCreate,
    ProjectConfig,
    ProjectUpdate,
    RecaptureRequest,
    ReviewRequest,
    RouteCreate,
    SampleUpdate,
    StreetViewParseRequest,
    WorkerCommandRequest,
    effective_limits,
)
from app.services import export as exportsvc
from app.services import plans as plansvc
from app.services.coverage import job_coverage
from app.storage import files

router = APIRouter(prefix="/api")


def _get(s: Session, model, id_, what: str):
    obj = s.get(model, id_)
    if obj is None:
        raise HTTPException(404, f"{what} not found")
    return obj


def _scope_from_geojson(gj: dict | None):
    settings = get_settings()
    if gj is None:
        gj = json.loads(settings.scope_geojson_path.read_text())
    if gj.get("type") == "FeatureCollection":
        geoms = [shape(f["geometry"]) for f in gj["features"]]
    elif gj.get("type") == "Feature":
        geoms = [shape(gj["geometry"])]
    else:
        geoms = [shape(gj)]
    polys = []
    for g in geoms:
        if isinstance(g, Polygon):
            polys.append(g)
        elif isinstance(g, MultiPolygon):
            polys.extend(g.geoms)
    if not polys:
        raise HTTPException(422, "Scope must contain at least one polygon")
    return MultiPolygon(polys)


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------

_size_cache: dict = {"t": None, "v": None}


@router.get("/system/status")
def system_status(s: Session = Depends(get_db)):
    settings = get_settings()
    now = datetime.now(timezone.utc)
    if _size_cache["t"] is None or (now - _size_cache["t"]).total_seconds() > 30:
        _size_cache["v"] = {
            "projects": files.dir_size_bytes(settings.projects_dir),
            "exports": files.dir_size_bytes(settings.exports_dir),
            "browser_state": files.dir_size_bytes(settings.browser_state_dir),
            "orphans": files.dir_size_bytes(settings.orphans_dir),
        }
        _size_cache["t"] = now
    alive = s.execute(select(func.count()).select_from(Worker).where(Worker.heartbeat_at > now - timedelta(seconds=30), Worker.status != "stopped")).scalar_one()
    checks = [{"name": c.name, "status": c.status, "detail": c.detail, "checked_at": ser.iso(c.checked_at)} for c in s.execute(select(SystemCheck)).scalars()]
    egress24 = dict(
        s.execute(select(EgressEvent.decision, func.count()).where(EgressEvent.created_at > now - timedelta(hours=24)).group_by(EgressEvent.decision)).all()
    )
    return {
        "version": "0.1.0",
        "processing_mode": settings.processing_mode(),
        "storage": {
            "data_dir": str(settings.data_root),
            "host_data_dir": settings.host_data_dir or str(settings.data_root),
            "projects_dir": str(settings.projects_dir),
            "exports_dir": str(settings.exports_dir),
            "browser_state_dir": str(settings.browser_state_dir),
            "bytes": _size_cache["v"],
            "note": "All images, metadata, job state and logs are stored on this machine.",
        },
        "database": {"ok": s.execute(text("SELECT 1")).scalar() == 1},
        "checks": checks,
        "workers_alive": alive,
        "mock_panorama_enabled": settings.enable_mock_panorama,
        "basemap": {"url": settings.basemap_url, "attribution": settings.basemap_attribution, "max_zoom": settings.basemap_max_zoom},
        "operator_limits": {
            "permitted_access_methods": settings.permitted_access_methods,
            "permitted_targets": settings.permitted_targets,
            "max_workers": settings.max_workers,
            "max_requests_per_minute": settings.max_requests_per_minute,
            "max_captures_per_job": settings.max_captures_per_job,
        },
        "egress_last_24h": {"allowed": egress24.get("allowed", 0), "blocked": egress24.get("blocked", 0)},
        "external_contacts": [
            {"service": "Planning basemap tiles", "from": "your browser (dashboard)", "data": "tile requests only", "host": settings.basemap_url},
            {"service": "Google Maps (google_maps target)", "from": "local Chromium worker", "data": "normal page requests, as a person browsing", "when": "only for jobs targeting google_maps"},
            {"service": "Google Street View Static API", "from": "worker", "data": "metadata + image requests with API key", "when": "only if method streetview_static_api is permitted and configured"},
            {"service": "OpenAI API", "from": "worker", "data": "task text; screenshots ONLY if cloud image processing is permitted", "when": "only for openai_agent jobs / startup model check"},
        ],
    }


@router.get("/egress")
def egress_events(limit: int = 100, s: Session = Depends(get_db)):
    rows = s.execute(select(EgressEvent).order_by(EgressEvent.id.desc()).limit(min(limit, 500))).scalars().all()
    return [
        {"id": e.id, "created_at": ser.iso(e.created_at), "provider": e.provider, "endpoint": e.endpoint, "data_class": e.data_class, "image_count": e.image_count, "decision": e.decision, "reason": e.reason, "job_id": str(e.job_id) if e.job_id else None}
        for e in rows
    ]


@router.post("/streetview/parse")
def parse_url(req: StreetViewParseRequest):
    p = parse_streetview_url(req.url)
    if p.lat is None:
        raise HTTPException(422, "No position found in this URL")
    out = p.to_dict()
    out["documented_url"] = build_maps_pano_url(p.lat, p.lng, p.heading or 0, p.pitch or 0, p.fov or 90, p.pano_id)
    out["note"] = "Values parsed from the URL are recorded with provenance; they are a starting observation, not verified data."
    return out


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------


@router.get("/projects")
def list_projects(s: Session = Depends(get_db)):
    return [ser.project_dict(p) for p in s.execute(select(Project).order_by(Project.created_at)).scalars()]


@router.post("/projects")
def create_project(req: ProjectCreate, s: Session = Depends(get_db)):
    scope = _scope_from_geojson(req.scope_geojson)
    p = Project(name=req.name, description=req.description, config=req.config.model_dump(mode="json"), scope_geom=from_shape(scope, srid=4326))
    s.add(p)
    s.flush()
    emit(s, "project_created", f"Project '{p.name}' created", project_id=p.id)
    s.commit()
    return ser.project_dict(p)


@router.get("/projects/{pid}")
def get_project(pid: uuid.UUID, s: Session = Depends(get_db)):
    p = _get(s, Project, pid, "project")
    out = ser.project_dict(p)
    out["effective_limits"] = effective_limits(ProjectConfig(**p.config), get_settings()).model_dump(mode="json")
    return out


@router.patch("/projects/{pid}")
def update_project(pid: uuid.UUID, req: ProjectUpdate, s: Session = Depends(get_db)):
    p = _get(s, Project, pid, "project")
    if req.name is not None:
        p.name = req.name
    if req.description is not None:
        p.description = req.description
    if req.config is not None:
        p.config = req.config.model_dump(mode="json")
    if req.scope_geojson is not None:
        p.scope_geom = from_shape(_scope_from_geojson(req.scope_geojson), srid=4326)
    emit(s, "project_updated", "Project configuration updated", project_id=p.id)
    s.commit()
    return get_project(pid, s)


# --------------------------------------------------------------------------
# Routes (operator geometry)
# --------------------------------------------------------------------------


@router.get("/projects/{pid}/routes")
def list_routes(pid: uuid.UUID, s: Session = Depends(get_db)):
    return [ser.route_dict(r) for r in s.execute(select(Route).where(Route.project_id == pid).order_by(Route.created_at)).scalars()]


def _validated_route_geometry(p: Project, req: RouteCreate):
    gj = req.geojson
    props = {}
    if gj.get("type") == "Feature":
        props = gj.get("properties") or {}
        gj = gj["geometry"]
    if gj.get("type") == "FeatureCollection":
        raise HTTPException(422, "Import a FeatureCollection with /routes/import")
    try:
        g = shape(gj)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"Invalid geometry: {e}") from e
    allowed = {
        "route": ("LineString", "MultiLineString"),
        "import": ("LineString", "MultiLineString", "Polygon", "MultiPolygon"),
        "polygon": ("Polygon", "MultiPolygon"),
        "streetview_seed": ("Point",),
    }[req.kind]
    if g.geom_type not in allowed:
        raise HTTPException(422, f"{req.kind} requires {allowed}, got {g.geom_type}")
    if not g.is_valid:
        raise HTTPException(422, "Geometry is not valid")
    for x, y in shapely.get_coordinates(g):
        if not within_area_of_use(float(x), float(y)):
            raise HTTPException(422, "Coordinates outside the planning CRS area (MGA zone 56)")
    scope = to_shape(p.scope_geom)
    warnings = [] if scope.buffer(1e-6).contains(g) else ["Geometry extends outside the approved scope; planning will refuse it"]
    return g, props, warnings


@router.post("/projects/{pid}/routes")
def create_route(pid: uuid.UUID, req: RouteCreate, s: Session = Depends(get_db)):
    p = _get(s, Project, pid, "project")
    g, props, warnings = _validated_route_geometry(p, req)
    r = Route(project_id=pid, name=req.name or props.get("name", ""), kind=req.kind, geom=from_shape(g, srid=4326), properties={**props, **req.properties})
    s.add(r)
    s.commit()
    out = ser.route_dict(r)
    out["warnings"] = warnings
    return out


@router.put("/routes/{rid}")
def replace_route_geometry(rid: uuid.UUID, req: RouteCreate, s: Session = Depends(get_db)):
    """Update a route after operator editing. Existing plans keep their own copied geometry."""
    r = _get(s, Route, rid, "route")
    p = _get(s, Project, r.project_id, "project")
    g, props, warnings = _validated_route_geometry(p, req)
    r.geom = from_shape(g, srid=4326)
    r.kind = req.kind
    r.name = req.name or props.get("name", r.name)
    r.properties = {**props, **req.properties}
    s.commit()
    out = ser.route_dict(r)
    out["warnings"] = warnings
    return out


@router.post("/projects/{pid}/routes/import")
def import_routes(pid: uuid.UUID, geojson: dict, s: Session = Depends(get_db)):
    """All-or-nothing import: every feature is validated before any is saved."""
    p = _get(s, Project, pid, "project")
    feats = geojson.get("features") if geojson.get("type") == "FeatureCollection" else [geojson]
    prepared, errors = [], []
    for i, f in enumerate(feats or []):
        geom = f.get("geometry") if f.get("type") == "Feature" else f
        gtype = (geom or {}).get("type")
        kind = "polygon" if gtype in ("Polygon", "MultiPolygon") else ("streetview_seed" if gtype == "Point" else "import")
        req = RouteCreate(name=(f.get("properties") or {}).get("name", ""), kind=kind, geojson=f)
        try:
            prepared.append((req, *_validated_route_geometry(p, req)))
        except HTTPException as e:
            errors.append({"feature": i, "error": e.detail})
    if errors or not prepared:
        raise HTTPException(422, {"errors": errors or [{"feature": None, "error": "no features"}]})
    out = []
    for req, g, props, warnings in prepared:
        r = Route(project_id=pid, name=req.name or props.get("name", ""), kind=req.kind, geom=from_shape(g, srid=4326), properties={**props, **req.properties})
        s.add(r)
        s.flush()
        out.append({**ser.route_dict(r), "warnings": warnings})
    s.commit()
    return out


@router.put("/routes/{rid}")
def replace_route_geometry(rid: uuid.UUID, req: RouteCreate, s: Session = Depends(get_db)):
    """Update a route after operator editing. Existing plans keep their own copied geometry."""
    r = _get(s, Route, rid, "route")
    p = _get(s, Project, r.project_id, "project")
    g, props, warnings = _validated_route_geometry(p, req)
    r.geom = from_shape(g, srid=4326)
    r.kind = req.kind
    r.name = req.name or props.get("name", r.name)
    r.properties = {**props, **req.properties}
    s.commit()
    out = ser.route_dict(r)
    out["warnings"] = warnings
    return out


@router.post("/projects/{pid}/routes/import")
def import_routes(pid: uuid.UUID, geojson: dict, s: Session = Depends(get_db)):
    _get(s, Project, pid, "project")
    feats = geojson.get("features") if geojson.get("type") == "FeatureCollection" else [geojson]
    created = []
    for f in feats or []:
        geom = f.get("geometry") if f.get("type") == "Feature" else f
        kind = "polygon" if (geom or {}).get("type") in ("Polygon", "MultiPolygon") else ("streetview_seed" if (geom or {}).get("type") == "Point" else "import")
        created.append(create_route(pid, RouteCreate(name=(f.get("properties") or {}).get("name", ""), kind=kind, geojson=f), s))
    return created


@router.delete("/routes/{rid}")
def delete_route(rid: uuid.UUID, s: Session = Depends(get_db)):
    r = _get(s, Route, rid, "route")
    s.delete(r)
    s.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------


@router.post("/projects/{pid}/plans")
def create_plan(pid: uuid.UUID, req: PlanCreate, s: Session = Depends(get_db)):
    p = _get(s, Project, pid, "project")
    try:
        plan = plansvc.create_plan(s, p, req.route_ids, req.params, req.seed_urls, req.name)
    except PlanningError as e:
        s.rollback()
        raise HTTPException(422, str(e)) from e
    emit(s, "plan_created", f"Capture plan created: {plan.summary.get('samples')} samples, {plan.summary.get('planned_views')} views", project_id=pid)
    s.commit()
    return {**plansvc.plan_dict(plan), "geojson": plansvc.plan_geojson(s, plan)}


@router.get("/projects/{pid}/plans")
def list_plans(pid: uuid.UUID, s: Session = Depends(get_db)):
    return [plansvc.plan_dict(p) for p in s.execute(select(CapturePlan).where(CapturePlan.project_id == pid).order_by(CapturePlan.created_at.desc())).scalars()]


@router.get("/plans/{plan_id}")
def get_plan(plan_id: uuid.UUID, s: Session = Depends(get_db)):
    plan = _get(s, CapturePlan, plan_id, "plan")
    return {**plansvc.plan_dict(plan), "geojson": plansvc.plan_geojson(s, plan)}


@router.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: uuid.UUID, s: Session = Depends(get_db)):
    plan = _get(s, CapturePlan, plan_id, "plan")
    if plan.status != "draft":
        raise HTTPException(409, f"Plan is {plan.status}")
    n = s.execute(select(func.count()).select_from(Sample).where(Sample.plan_id == plan.id, Sample.excluded.is_(False))).scalar_one()
    if n == 0:
        raise HTTPException(422, "Plan has no included samples")
    plan.status = "approved"
    plan.approved_at = datetime.now(timezone.utc)
    emit(s, "plan_approved", f"Plan approved with {n} samples", project_id=plan.project_id)
    s.commit()
    return {**plansvc.plan_dict(plan), "geojson": plansvc.plan_geojson(s, plan)}


@router.delete("/plans/{plan_id}")
def delete_plan(plan_id: uuid.UUID, s: Session = Depends(get_db)):
    plan = _get(s, CapturePlan, plan_id, "plan")
    if s.execute(select(Job.id).where(Job.plan_id == plan.id)).first():
        raise HTTPException(409, "Plan has jobs and cannot be deleted")
    s.delete(plan)
    s.commit()
    return {"ok": True}


@router.patch("/samples/{sid}")
def update_sample(sid: uuid.UUID, req: SampleUpdate, s: Session = Depends(get_db)):
    smp = _get(s, Sample, sid, "sample")
    try:
        plansvc.update_sample(s, smp, lng=req.lng, lat=req.lat, excluded=req.excluded, note=req.note, reset=req.reset_position)
    except PlanningError as e:
        s.rollback()
        raise HTTPException(422, str(e)) from e
    s.commit()
    plan = s.get(CapturePlan, smp.plan_id)
    return {**plansvc.plan_dict(plan), "geojson": plansvc.plan_geojson(s, plan)}


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


@router.post("/plans/{plan_id}/jobs")
def create_job(plan_id: uuid.UUID, req: JobCreate, s: Session = Depends(get_db)):
    plan = _get(s, CapturePlan, plan_id, "plan")
    try:
        job = jobsvc.create_job(s, plan, req)
    except jobsvc.JobConfigError as e:
        s.rollback()
        raise HTTPException(422, {"errors": e.errors}) from e
    s.commit()
    return ser.job_dict(s, job)


@router.post("/jobs/validate")
def validate_job(plan_id: uuid.UUID, req: JobCreate, s: Session = Depends(get_db)):
    plan = _get(s, CapturePlan, plan_id, "plan")
    project = s.get(Project, plan.project_id)
    errors = [] if plan.status == "approved" else ["Plan must be approved before a job can be created"]
    errors += jobsvc.validate_job_permissions(
        project=project, target=req.target, method=req.method, worker_count=req.worker_count,
        requests_per_minute=req.requests_per_minute, model_budget_usd=req.model_budget_usd, settings=get_settings(),
        max_captures=req.max_captures,
    )
    return {"ok": not errors, "errors": errors}


@router.get("/projects/{pid}/jobs")
def list_jobs(pid: uuid.UUID, s: Session = Depends(get_db)):
    return [ser.job_dict(s, j, with_workers=False) for j in s.execute(select(Job).where(Job.project_id == pid).order_by(Job.created_at.desc())).scalars()]


@router.get("/jobs/{jid}")
def get_job(jid: uuid.UUID, s: Session = Depends(get_db)):
    return ser.job_dict(s, _get(s, Job, jid, "job"))


def _job_action(jid, s, fn, *args):
    job = s.execute(select(Job).where(Job.id == jid).with_for_update()).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        fn(s, job, *args)
    except jobsvc.JobConfigError as e:
        s.rollback()
        raise HTTPException(422, {"errors": e.errors}) from e
    s.commit()
    return ser.job_dict(s, job)


@router.post("/jobs/{jid}/start")
def start_job(jid: uuid.UUID, s: Session = Depends(get_db)):
    return _job_action(jid, s, jobsvc.start_job)


@router.post("/jobs/{jid}/pause")
def pause_job(jid: uuid.UUID, s: Session = Depends(get_db)):
    return _job_action(jid, s, jobsvc.pause_job, "operator", "Paused by operator")


@router.post("/jobs/{jid}/resume")
def resume_job(jid: uuid.UUID, s: Session = Depends(get_db)):
    return _job_action(jid, s, jobsvc.resume_job)


@router.post("/jobs/{jid}/cancel")
def cancel_job(jid: uuid.UUID, s: Session = Depends(get_db)):
    return _job_action(jid, s, jobsvc.cancel_job)


@router.delete("/jobs/{jid}")
def delete_job(jid: uuid.UUID, confirm: str = Query(..., description="Must equal 'delete'"), s: Session = Depends(get_db)):
    """Delete a job, its records AND its local images. Irreversible; refused while running."""
    job = _get(s, Job, jid, "job")
    if confirm != "delete":
        raise HTTPException(422, "Pass confirm=delete to delete this job and its local images")
    if job.status == "running" or s.execute(select(Task.id).where(Task.job_id == jid, Task.status.in_(("running", "manual_required")))).first():
        raise HTTPException(409, "Pause or cancel the job and wait for workers to release their tasks first")
    import shutil

    shutil.rmtree(files.job_dir(job.project_id, job.id), ignore_errors=True)
    emit(s, "job_deleted", f"Job {job.name} deleted with its local images", project_id=job.project_id)
    s.execute(text("UPDATE workers SET job_id = NULL WHERE job_id = :j"), {"j": jid})
    s.delete(job)
    s.commit()
    return {"ok": True}


@router.delete("/projects/{pid}")
def delete_project(pid: uuid.UUID, confirm: str = Query(..., description="Type the project name"), s: Session = Depends(get_db)):
    """Delete a project with all plans, jobs, local images and local exports. Irreversible."""
    p = _get(s, Project, pid, "project")
    if confirm != p.name:
        raise HTTPException(422, "Confirmation does not match the project name")
    if s.execute(select(Job.id).where(Job.project_id == pid, Job.status == "running")).first():
        raise HTTPException(409, "A job in this project is running; pause or cancel it first")
    import shutil

    for e in s.execute(select(Export).where(Export.project_id == pid)).scalars():
        shutil.rmtree(files.resolve_rel(e.path), ignore_errors=True)
    shutil.rmtree(get_settings().projects_dir / str(pid), ignore_errors=True)
    s.execute(text("UPDATE workers SET job_id = NULL WHERE job_id IN (SELECT id FROM jobs WHERE project_id = :p)"), {"p": pid})
    s.delete(p)
    s.commit()
    return {"ok": True}


@router.get("/jobs/{jid}/tasks")
def list_tasks(jid: uuid.UUID, status: str | None = None, s: Session = Depends(get_db)):
    q = select(Task).where(Task.job_id == jid).order_by(Task.ordinal)
    if status:
        q = q.where(Task.status.in_(status.split(",")))
    return [ser.task_dict(t) for t in s.execute(q).scalars()]


@router.get("/tasks/{tid}")
def get_task(tid: uuid.UUID, s: Session = Depends(get_db)):
    t = _get(s, Task, tid, "task")
    attempts = s.execute(select(TaskAttempt).where(TaskAttempt.task_id == tid).order_by(TaskAttempt.attempt_no)).scalars().all()
    out = ser.task_dict(t)
    out["attempts"] = [
        {"attempt_no": a.attempt_no, "worker_id": a.worker_id, "outcome": a.outcome, "error": a.error, "started_at": ser.iso(a.started_at), "ended_at": ser.iso(a.ended_at), "model_calls": a.model_calls, "actions": a.actions}
        for a in attempts
    ]
    return out


def _reopen_for_recapture(s: Session, job: Job, reason: str) -> None:
    if job.status == "completed":
        job.status = "paused"
        job.pause_kind = "operator"
        job.pause_reason = f"Reopened: {reason}. Resume to collect."
        job.finished_at = None


@router.post("/tasks/{tid}/recapture")
def recapture_task(tid: uuid.UUID, req: RecaptureRequest, s: Session = Depends(get_db)):
    t = s.execute(select(Task).where(Task.id == tid).with_for_update()).scalar_one_or_none()
    if t is None:
        raise HTTPException(404, "task not found")
    if t.status in ("running", "manual_required"):
        raise HTTPException(409, "Task is currently leased by a worker")
    job = s.get(Job, t.job_id)
    if job.status == "cancelled":
        raise HTTPException(409, "Job is cancelled")
    t.status = "queued" if job.status != "draft" else "planned"
    t.failure_count = 0
    t.view_filter = [str(v) for v in req.view_ids] if req.view_ids else None
    t.next_attempt_at = datetime.now(timezone.utc)
    t.finished_at = None
    _reopen_for_recapture(s, job, req.reason)
    emit(s, "recapture_requested", f"Recapture requested for task {t.ordinal}: {req.reason}", job_id=job.id, task_id=t.id)
    s.commit()
    return ser.task_dict(t)


@router.post("/tasks/{tid}/retry")
def retry_task(tid: uuid.UUID, s: Session = Depends(get_db)):
    return recapture_task(tid, RecaptureRequest(reason="operator retry"), s)


@router.get("/jobs/{jid}/coverage")
def coverage(jid: uuid.UUID, s: Session = Depends(get_db)):
    return job_coverage(s, _get(s, Job, jid, "job"))


@router.get("/jobs/{jid}/events")
def job_events(jid: uuid.UUID, after_id: int = 0, limit: int = 200, s: Session = Depends(get_db)):
    q = select(Event).where(Event.job_id == jid, Event.id > after_id)
    # after_id > 0: oldest-first page for incremental polling; otherwise the latest events, newest first.
    q = q.order_by(Event.id.asc() if after_id else Event.id.desc()).limit(min(limit, 1000))
    rows = s.execute(q).scalars().all()
    return [{"id": e.id, "created_at": ser.iso(e.created_at), "level": e.level, "kind": e.kind, "message": e.message, "worker_id": e.worker_id, "task_id": str(e.task_id) if e.task_id else None} for e in rows]


@router.get("/projects/{pid}/events")
def project_events(pid: uuid.UUID, limit: int = 100, s: Session = Depends(get_db)):
    rows = s.execute(select(Event).where(Event.project_id == pid).order_by(Event.id.desc()).limit(min(limit, 500))).scalars().all()
    return [{"id": e.id, "created_at": ser.iso(e.created_at), "level": e.level, "kind": e.kind, "message": e.message} for e in rows]


# --------------------------------------------------------------------------
# Observations & review
# --------------------------------------------------------------------------


@router.get("/jobs/{jid}/observations")
def list_observations(
    jid: uuid.UUID,
    status: str | None = None,
    flag: str | None = None,
    side: str | None = None,
    segment_key: str | None = None,
    include_superseded: bool = False,
    limit: int = Query(200, le=2000),
    offset: int = 0,
    s: Session = Depends(get_db),
):
    q = select(Observation, Sample.ordinal).join(Sample, Sample.id == Observation.sample_id).where(Observation.job_id == jid)
    if not include_superseded:
        q = q.where(Observation.is_current.is_(True) | (Observation.verification_status == "rejected"))
    if status:
        q = q.where(Observation.verification_status.in_(status.split(",")))
    if flag:
        q = q.where(Observation.quality_flags.any(flag))
    if side:
        q = q.where(Observation.intended_side == side)
    if segment_key:
        q = q.where(Observation.segment_key == segment_key)
    total = s.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = s.execute(q.order_by(Sample.ordinal, Observation.view_kind, Observation.intended_side).limit(limit).offset(offset)).all()
    return {"total": total, "items": [ser.observation_summary(o, ordn) for o, ordn in rows]}


@router.get("/observations/{oid}")
def get_observation(oid: uuid.UUID, s: Session = Depends(get_db)):
    o = _get(s, Observation, oid, "observation")
    rec = exportsvc.observation_record(o)
    smp = s.get(Sample, o.sample_id)
    links = s.execute(select(ObservationLink).where(ObservationLink.observation_id == o.id)).scalars().all()
    crops = s.execute(select(Crop).where(Crop.observation_id == o.id)).scalars().all()
    rec.update(
        {
            "image_url": ser.file_url(o.image_path),
            "thumb_url": ser.file_url(o.thumb_path),
            "sample_ordinal": smp.ordinal,
            "is_current": o.is_current,
            "links": [{"planned_view_id": str(lk.planned_view_id), "sample_id": str(lk.sample_id), "kind": lk.link_kind, "association_status": lk.association_status, "camera_to_sample_m": lk.camera_to_sample_m, "active": lk.active} for lk in links],
            "crops": [{"id": str(c.id), "x": c.x, "y": c.y, "width": c.width, "height": c.height, "purpose": c.purpose, "url": ser.file_url(c.path), "raw_sha256": c.raw_sha256} for c in crops],
        }
    )
    return rec


@router.post("/observations/{oid}/review")
def review_observation(oid: uuid.UUID, req: ReviewRequest, s: Session = Depends(get_db)):
    o = s.execute(select(Observation).where(Observation.id == oid).with_for_update()).scalar_one_or_none()
    if o is None:
        raise HTTPException(404, "observation not found")
    if not o.is_current and o.verification_status != "rejected":
        raise HTTPException(409, "Observation has been superseded")
    task = s.execute(select(Task).where(Task.id == o.task_id).with_for_update()).scalar_one()
    if task.status in ("running", "manual_required"):
        raise HTTPException(409, "Task is currently leased by a worker; review after it finishes")
    job = s.get(Job, o.job_id)
    now = datetime.now(timezone.utc)
    o.review_note = req.note
    o.reviewed_at = now
    if req.decision == "accept":
        if not o.is_current:
            raise HTTPException(409, "Rejected observations cannot be re-accepted; request a recapture")
        o.review_status = "accepted"
        o.verification_status = "verified"
        if req.confirm_association:
            o.association_status = "operator_verified"
        emit(s, "observation_accepted", f"Observation accepted by operator ({o.intended_side}/{o.view_kind})", job_id=o.job_id, task_id=o.task_id)
        s.flush()
        finalize_task(s, task.id, statuses=("verified", "needs_review"))
    else:
        o.review_status = "rejected"
        o.verification_status = "rejected"
        o.is_current = False
        s.execute(ObservationLink.__table__.update().where(ObservationLink.observation_id == o.id).values(active=False))
        emit(s, "observation_rejected", f"Observation rejected by operator: {req.note or 'no note'}", level="warning", job_id=o.job_id, task_id=o.task_id)
        s.flush()
        if req.recapture and job.status != "cancelled":
            task.status = "queued" if job.status != "draft" else "planned"
            task.failure_count = 0
            task.view_filter = [str(o.planned_view_id)]
            task.next_attempt_at = now
            task.finished_at = None
            _reopen_for_recapture(s, job, "recapture after review")
        else:
            finalize_task(s, task.id, statuses=("verified", "needs_review"))
    s.commit()
    return get_observation(oid, s)


@router.post("/observations/{oid}/crops")
def create_crop(oid: uuid.UUID, req: CropRequest, s: Session = Depends(get_db)):
    o = _get(s, Observation, oid, "observation")
    raw = files.resolve_rel(o.image_path).read_bytes()
    if files.sha256_bytes(raw) != o.sha256:
        raise HTTPException(409, "Raw image checksum mismatch; refusing to derive a crop")
    try:
        cb = files.crop_bytes(raw, req.x, req.y, req.width, req.height)
    except files.StorageError as e:
        raise HTTPException(422, str(e)) from e
    cid = uuid.uuid4()
    p = files.crop_path(o.project_id, o.job_id, cid)
    sha, _ = files.atomic_write_bytes(p, cb)
    c = Crop(id=cid, observation_id=o.id, raw_sha256=o.sha256, x=req.x, y=req.y, width=req.width, height=req.height, purpose=req.purpose, path=files.rel(p), sha256=sha)
    s.add(c)
    s.commit()
    return {"id": str(cid), "url": ser.file_url(c.path), "raw_observation_id": str(o.id), "raw_sha256": o.sha256, "geometry": {"x": c.x, "y": c.y, "width": c.width, "height": c.height}}


# --------------------------------------------------------------------------
# Workers & operator commands
# --------------------------------------------------------------------------


@router.get("/workers/{wid}")
def get_worker(wid: str, s: Session = Depends(get_db)):
    return ser.worker_dict(s, _get(s, Worker, wid, "worker"))


@router.post("/workers/{wid}/commands")
def send_command(wid: str, req: WorkerCommandRequest, s: Session = Depends(get_db)):
    w = _get(s, Worker, wid, "worker")
    if w.status not in ("awaiting_operator", "running"):
        raise HTTPException(409, f"Worker is {w.status}; commands are accepted while it awaits an operator")
    c = WorkerCommand(worker_id=wid, task_id=w.current_task_id, command=req.command, payload=req.payload)
    s.add(c)
    emit(s, "operator_command", f"Operator command '{req.command}' sent to {wid}", job_id=w.job_id, worker_id=wid, data={"payload": req.payload})
    s.commit()
    return {"id": c.id, "status": c.status}


@router.get("/workers/{wid}/commands")
def list_commands(wid: str, limit: int = 20, s: Session = Depends(get_db)):
    rows = s.execute(select(WorkerCommand).where(WorkerCommand.worker_id == wid).order_by(WorkerCommand.id.desc()).limit(limit)).scalars().all()
    return [{"id": c.id, "command": c.command, "payload": c.payload, "status": c.status, "result": c.result, "created_at": ser.iso(c.created_at), "executed_at": ser.iso(c.executed_at)} for c in rows]


# --------------------------------------------------------------------------
# Exports & retention (local only)
# --------------------------------------------------------------------------


@router.post("/projects/{pid}/exports")
def create_export(pid: uuid.UUID, req: ExportRequest, s: Session = Depends(get_db)):
    p = _get(s, Project, pid, "project")
    e = exportsvc.create_export(s, p, req.job_id, req.include, req.copy_mode)
    s.commit()
    return _export_dict(e)


@router.get("/projects/{pid}/exports")
def list_exports(pid: uuid.UUID, s: Session = Depends(get_db)):
    rows = s.execute(select(Export).where(Export.project_id == pid).order_by(Export.created_at.desc())).scalars().all()
    return [_export_dict(e) for e in rows]


def _export_dict(e: Export) -> dict:
    return {
        "id": str(e.id), "path": e.path, "absolute_path": str(files.resolve_rel(e.path)), "observation_count": e.observation_count,
        "manifest_sha256": e.manifest_sha256, "created_at": ser.iso(e.created_at), "job_id": str(e.job_id) if e.job_id else None,
        "include": e.include, "copy_mode": e.copy_mode,
    }


@router.get("/projects/{pid}/retention")
def retention_preview(pid: uuid.UUID, s: Session = Depends(get_db)):
    p = _get(s, Project, pid, "project")
    cutoff, rows = exportsvc.retention_candidates(s, p)
    return {"cutoff": cutoff.isoformat(), "observations": len(rows), "bytes": sum(o.image_bytes for o in rows), "retention_days": ProjectConfig(**p.config).retention_days}


@router.post("/projects/{pid}/retention/apply")
def retention_apply(pid: uuid.UUID, confirm: str = Query(..., description="Type the project name to confirm"), s: Session = Depends(get_db)):
    p = _get(s, Project, pid, "project")
    if confirm != p.name:
        raise HTTPException(422, "Confirmation does not match the project name")
    out = exportsvc.apply_retention(s, p)
    s.commit()
    return out
