"""Build realistic TaskOutcomes without a browser (for DB-level tests)."""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone

import numpy as np
from geoalchemy2.shape import to_shape
from PIL import Image
from sqlalchemy import select

from app.capture.completion import CaptureRecord, ReuseRecord, TaskOutcome, complete_task, finalize_task
from app.capture.verification import CapturePolicy, PageState, PlannedViewInfo, check_frame, evaluate_capture
from app.db import session_scope
from app.models import Job, PlannedView, Sample, Segment
from app.storage import files


def scene_png(seed: int = 0, w: int = 320, h: int = 200) -> bytes:
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 255, (h, w, 3)).astype("uint8")
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="PNG")
    return buf.getvalue()


def views_for(s, sample_id) -> list[PlannedViewInfo]:
    vs = s.execute(select(PlannedView).where(PlannedView.sample_id == sample_id).order_by(PlannedView.ordinal)).scalars().all()
    return [PlannedViewInfo(str(v.id), v.side, v.view_kind, v.heading_deg, v.pitch_deg, v.fov_deg, v.viewport_w, v.viewport_h) for v in vs]


def write_capture(job: Job, sample: Sample, seg: Segment, view: PlannedViewInfo, pano_id: str = "PANO-1", seed: int = 0, cam_offset_deg=0.00001) -> CaptureRecord:
    pt = to_shape(sample.requested_geom)
    data = scene_png(seed)
    obs_id = uuid.uuid4()
    p = files.image_path(job.project_id, job.id, obs_id)
    sha, n = files.atomic_write_bytes(p, data)
    st = PageState(
        mode="street_view", url="http://test/mock", loaded=True, camera_lat=pt.y + cam_offset_deg, camera_lng=pt.x, camera_provenance="page_url",
        heading=view.heading, heading_provenance="page_url", pitch=view.pitch, pitch_provenance="page_url", fov=view.fov, fov_provenance="page_url",
        pano_id=pano_id, pano_id_provenance="page_url", imagery_date="2025-01", imagery_date_precision="month", imagery_date_provenance="ui_text",
        attribution="Synthetic test",
    )
    ev = evaluate_capture(state=st, view=view, requested_lat=pt.y, requested_lng=pt.x, segment_bearing=seg.bearing_deg, frame=check_frame(data), policy=CapturePolicy(), in_scope=True)
    info = files.image_info(data)
    return CaptureRecord(
        observation_id=obs_id, view=view, image_rel=files.rel(p), image_format="png", width=info.width, height=info.height, nbytes=n,
        sha256=sha, thumb_rel=None, state=st, evaluation=ev, collected_at=datetime.now(timezone.utc), source_url=st.url, capture_method="test",
    )


def complete_with_captures(claim, worker_id: str, pano_id: str = "PANO-1", reuse_from: uuid.UUID | None = None):
    """Write one capture per planned view (or reuse links) and run the real completion transaction."""
    with session_scope() as s:
        job = s.get(Job, claim.job_id)
        smp = s.get(Sample, claim.sample_id)
        seg = s.get(Segment, smp.segment_id)
        outcome = TaskOutcome(panorama={"pano_id": pano_id})
        for i, v in enumerate(views_for(s, smp.id)):
            if reuse_from:
                outcome.reuses.append(ReuseRecord(v.id, reuse_from, 12.0, 0.5, "uncertain"))
            else:
                outcome.captures.append(write_capture(job, smp, seg, v, pano_id=pano_id, seed=i))
        res = complete_task(s, claim=claim, worker_id=worker_id, outcome=outcome, job=job, sample=smp, segment=seg)
    if res.accepted:
        with session_scope() as s:
            finalize_task(s, claim.task_id)
    return res, outcome
