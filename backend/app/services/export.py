"""Local dataset packaging for downstream research tools.

Creates a directory under DATA_DIR/exports with a manifest, relative file
paths and checksums. There is deliberately no upload, publish or share
function anywhere in this application.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.events import emit
from app.models import ConfigVersion, Crop, Export, Job, Observation, ObservationLink, Project
from app.schemas import ProjectConfig
from app.services.coverage import job_coverage
from app.services.plans import plan_geojson
from app.storage import files

FORMAT = "maply-dataset/1"


def observation_record(o: Observation, rel_image: str | None = None) -> dict:
    req = to_shape(o.requested_geom)
    cam = to_shape(o.camera_geom) if o.camera_geom is not None else None
    return {
        "observation_id": str(o.id),
        "project_id": str(o.project_id),
        "job_id": str(o.job_id),
        "task_id": str(o.task_id),
        "attempt_id": str(o.attempt_id),
        "worker_id": o.worker_id,
        "plan_id": str(o.plan_id),
        "segment_id": str(o.segment_id),
        "segment_key": o.segment_key,
        "sample_id": str(o.sample_id),
        "planned_view_id": str(o.planned_view_id),
        "intended_side": o.intended_side,
        "side_convention": "relative to stored segment direction (first vertex -> last vertex)",
        "view_kind": o.view_kind,
        "requested_position": {"lat": req.y, "lng": req.x, "meaning": "requested sample on the drawn route"},
        "camera_position": None if cam is None else {"lat": cam.y, "lng": cam.x, "provenance": o.camera_position_provenance, "meaning": "camera/panorama position as reported by the source; NOT the location of any visible feature"},
        "camera_offset_m": o.camera_offset_m,
        "panorama": {"source_pano_id": o.pano_id, "provenance": o.pano_id_provenance, "note": "source IDs may change; observation_id is the stable key"},
        "source_url": o.source_url,
        "orientation": {
            "planned": {"heading_deg": o.planned_heading_deg, "pitch_deg": o.planned_pitch_deg, "fov_deg": o.planned_fov_deg},
            "observed": {
                "heading_deg": o.heading_deg, "heading_provenance": o.heading_provenance,
                "pitch_deg": o.pitch_deg, "pitch_provenance": o.pitch_provenance,
                "fov_deg": o.fov_deg, "fov_provenance": o.fov_provenance,
            },
        },
        "viewport": {"width": o.viewport_w, "height": o.viewport_h, "device_scale_factor": o.device_scale_factor},
        "imagery_date": {"value": o.imagery_date, "precision": o.imagery_date_precision, "provenance": o.imagery_date_provenance},
        "collected_at": o.collected_at.isoformat(),
        "image": {"path": rel_image or o.image_path, "format": o.image_format, "width": o.image_width, "height": o.image_height, "bytes": o.image_bytes, "sha256": o.sha256},
        "capture_method": o.capture_method,
        "target": o.target,
        "config_version_id": str(o.config_version_id),
        "attribution": o.attribution,
        "verification_status": o.verification_status,
        "association_status": o.association_status,
        "quality_flags": list(o.quality_flags or []),
        "review": {"status": o.review_status, "note": o.review_note, "reviewed_at": o.reviewed_at.isoformat() if o.reviewed_at else None},
        "verification_evidence": o.verification_evidence,
    }


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()[:40] or "project"


def create_export(s: Session, project: Project, job_id=None, include: str = "verified_and_review", copy_mode: str = "hardlink") -> Export:
    settings = get_settings()
    cfg = ProjectConfig(**(project.config or {}))
    q = select(Observation).where(Observation.project_id == project.id, Observation.is_current.is_(True))
    if job_id:
        q = q.where(Observation.job_id == job_id)
    if include == "verified":
        q = q.where(Observation.verification_status == "verified")
    elif include == "verified_and_review":
        q = q.where(Observation.verification_status.in_(("verified", "needs_review")))
    observations = s.execute(q.order_by(Observation.collected_at)).scalars().all()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = settings.exports_dir / f"{_slug(project.name)}-{ts}"
    (out / "images").mkdir(parents=True, exist_ok=False)
    manifest_files = []
    with open(out / "observations.jsonl", "w") as f:
        for o in observations:
            src = files.resolve_rel(o.image_path)
            rel_image = f"images/{o.id}.{o.image_format}"
            dst = out / rel_image
            if copy_mode == "hardlink":
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
            else:
                shutil.copy2(src, dst)
            manifest_files.append({"path": rel_image, "sha256": o.sha256, "bytes": o.image_bytes})
            f.write(json.dumps(observation_record(o, rel_image), default=str) + "\n")

    job_ids = sorted({o.job_id for o in observations}, key=str) or ([job_id] if job_id else [])
    coverage = {}
    plans_written = set()
    for jid in job_ids:
        job = s.get(Job, jid)
        if job is None:
            continue
        coverage[str(jid)] = {k: v for k, v in job_coverage(s, job).items() if k not in ("samples", "cameras")}
        if job.plan_id not in plans_written:
            from app.models import CapturePlan

            gj = plan_geojson(s, s.get(CapturePlan, job.plan_id))
            for name, fc in gj.items():
                p = out / f"plan-{str(job.plan_id)[:8]}-{name}.geojson"
                p.write_text(json.dumps(fc))
                manifest_files.append({"path": p.name, "sha256": files.sha256_file(p), "bytes": p.stat().st_size})
            plans_written.add(job.plan_id)
    (out / "coverage.json").write_text(json.dumps(coverage, indent=2, default=str))
    cams = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": mapping(to_shape(o.camera_geom)), "properties": {"observation_id": str(o.id), "meaning": "reported camera position, not a feature location"}}
            for o in observations
            if o.camera_geom is not None
        ],
    }
    (out / "camera_positions.geojson").write_text(json.dumps(cams))
    cvs = s.execute(select(ConfigVersion).where(ConfigVersion.id.in_({o.config_version_id for o in observations}))).scalars().all() if observations else []
    (out / "config_versions.json").write_text(json.dumps([{"id": str(c.id), "version": c.version, "kind": c.kind, "digest": c.digest, "config": c.config} for c in cvs], indent=2, default=str))
    for name in ("observations.jsonl", "coverage.json", "camera_positions.geojson", "config_versions.json"):
        p = out / name
        manifest_files.append({"path": name, "sha256": files.sha256_file(p), "bytes": p.stat().st_size})

    restrictions = (
        f"{cfg.purpose}\n"
        f"Approval reference: {cfg.approval_reference or '(not recorded)'}\n"
        "This package is for LOCAL research use only. Do not publish, redistribute or reuse it for unrelated purposes.\n"
        f"Retention: delete within {cfg.retention_days} days of collection unless the approval says otherwise.\n"
        "Imagery remains subject to its source's terms and attribution (see each record's 'attribution')."
    )
    manifest = {
        "format": FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project": {"id": str(project.id), "name": project.name, "approval_reference": cfg.approval_reference},
        "usage_restrictions": restrictions,
        "selection": {"job_id": str(job_id) if job_id else None, "include": include},
        "counts": {"observations": len(observations)},
        "conventions": {
            "crs": "EPSG:4326 for all coordinates; distances computed in EPSG:7856 (GDA2020 / MGA zone 56)",
            "side": "left/right relative to stored segment direction",
            "camera_position": "position reported by the source; never a feature location",
            "imagery_date": "only from explicit source date evidence; 'unknown' otherwise; never inferred from copyright years or URL parameters",
            "collection_time": "collected_at is when this tool saved the image, separate from imagery_date",
        },
        "files": manifest_files,
    }
    mpath = out / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2))
    (out / "README.txt").write_text("maply local dataset package\n\n" + restrictions + "\n\nSee manifest.json for file checksums and conventions.\n")
    exp = Export(project_id=project.id, job_id=job_id, path=files.rel(out), observation_count=len(observations), manifest_sha256=files.sha256_file(mpath), include=include, copy_mode=copy_mode)
    s.add(exp)
    emit(s, "export_created", f"Local dataset package created at {files.rel(out)} ({len(observations)} observations)", project_id=project.id, job_id=job_id)
    return exp


def retention_candidates(s: Session, project: Project) -> tuple[datetime, list[Observation]]:
    cfg = ProjectConfig(**(project.config or {}))
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.retention_days)
    rows = s.execute(select(Observation).where(Observation.project_id == project.id, Observation.collected_at < cutoff)).scalars().all()
    return cutoff, rows


def apply_retention(s: Session, project: Project) -> dict:
    cutoff, rows = retention_candidates(s, project)
    removed = 0
    for o in rows:
        for rp in [o.image_path, o.thumb_path] + [c.path for c in s.execute(select(Crop).where(Crop.observation_id == o.id)).scalars()]:
            if rp:
                try:
                    files.resolve_rel(rp).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
        s.execute(delete(ObservationLink).where(ObservationLink.observation_id == o.id))
        s.delete(o)
        removed += 1
    exports = s.execute(select(Export).where(Export.project_id == project.id, Export.created_at < cutoff)).scalars().all()
    for e in exports:
        shutil.rmtree(files.resolve_rel(e.path), ignore_errors=True)
        s.delete(e)
    emit(s, "retention_applied", f"Retention applied: {removed} observations and {len(exports)} exports older than {cutoff:%Y-%m-%d} deleted", project_id=project.id)
    return {"cutoff": cutoff.isoformat(), "observations_deleted": removed, "exports_deleted": len(exports)}
