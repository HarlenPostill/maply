"""Bring finished shots from one job into another planned over the same streets.

Used when a job is re-planned with a newer planner. A shot with the same code,
position and heading in both jobs is the same photo, so its file, thumbnail,
camera checks and status are copied across. The source job is only read.

Only shots still pending in the target are filled, so running it again later
picks up anything finished in the source since, without touching new work.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import results
from app.config import get_settings
from app.models import Job, Shot, TagTask


def _copy_completed_labels(s: Session, old: Shot, new: Shot, src: Job, dst_root: Path, image: Path | None) -> None:
    """Carry an existing successful observation, never an inference lease."""
    task = old.tag_task
    provenance = (old.labels or {}).get("provenance", {})
    sha = (new.result or {}).get("sha256")
    if (image is None or old.status not in ("captured", "accepted", "needs_check")
            or task is None or task.status != "completed" or not old.labels or not sha
            or task.image_sha256 != sha or provenance.get("image_sha256") != sha):
        return
    with image.open("rb") as file:
        actual_sha = hashlib.file_digest(file, "sha256").hexdigest()
    if actual_sha != sha:
        new.result = {**new.result, "sha256": actual_sha}
        return  # Files edited outside Maply must not inherit stale observations.
    copied_at = datetime.now(timezone.utc)
    from app.confidence import confidence_for_labels
    copied_task = TagTask(
        id=uuid.uuid4(), shot_id=new.id, status="completed", model=task.model,
        image_sha256=sha, attempts=task.attempts, needs_review=task.needs_review,
        created_at=copied_at, updated_at=copied_at,
        confidence=confidence_for_labels(old.labels),
    )
    copied_labels = deepcopy(old.labels)
    copied_labels["provenance"] = {
        **provenance,
        "task_id": str(copied_task.id), "shot_id": str(new.id),
        "segment_key": new.segment_key, "side": new.side,
        "original_task_id": provenance.get("original_task_id", provenance.get("task_id", str(task.id))),
        "original_shot_id": provenance.get("original_shot_id", str(old.id)),
        "copied_from_task_id": str(task.id), "copied_from_shot_id": str(old.id),
        "copied_from_job_id": str(src.id), "copied_at": copied_at.isoformat(),
    }
    # Keep the same durable JSON artifact guarantee as an ordinary completion.
    directory = dst_root / "labels"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"{new.code}_{copied_task.id}_{copied_task.attempts}.json"
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(copied_labels, indent=2))
    temporary.replace(output)
    new.labels = copied_labels
    s.add(copied_task)
    # The relationship is view-only and may already be loaded as None.
    s.expire(new, ["tag_task"])


def carry_over(s: Session, src: Job, dst: Job) -> dict:
    """Copy finished shots from `src` into matching pending shots of `dst`. The caller commits."""
    jobs_dir = get_settings().jobs_dir
    src_root, dst_root = jobs_dir / src.slug, jobs_dir / dst.slug
    targets = {sh.code: sh for sh in s.execute(select(Shot).where(Shot.job_id == dst.id)).scalars()}
    copied: Counter[str] = Counter()
    not_in_plan: Counter[str] = Counter()
    already_done, moved, missing = [], [], []
    for old in s.execute(select(Shot).where(Shot.job_id == src.id, Shot.status != "pending").order_by(Shot.ordinal)).scalars():
        new = targets.get(old.code)
        if new is None:
            not_in_plan[old.status] += 1
            continue
        if (new.lat, new.lng, new.heading) != (old.lat, old.lng, old.heading):
            moved.append(old.code)  # same code but a different view: not the same photo
            continue
        if new.status != "pending" or new.tag_task is not None:
            already_done.append(old.code)
            continue
        res = dict(old.result or {})
        copied_image = None
        if res.get("file"):
            image = src_root / res["file"]
            if not image.is_file():
                missing.append(old.code)
                continue
            dest = (dst_root / "images" / new.filename).with_suffix(image.suffix)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(image, dest)  # keeps the modified time that result["saved_at"] records
            copied_image = dest
            thumb = results.thumb_for(dst_root, dest)
            old_thumb = src_root / res["thumb"] if res.get("thumb") else None
            if old_thumb is not None and old_thumb.is_file():
                thumb.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_thumb, thumb)
            else:
                results.make_thumb(dest, thumb)
            res["file"], res["thumb"] = str(dest.relative_to(dst_root)), str(thumb.relative_to(dst_root))
        res["look"] = (new.result or {}).get("look", "")
        res["carried_over_from"] = f"{src.slug}/{old.code}"
        new.status, new.flags, new.result, new.note = old.status, list(old.flags or []), res, old.note
        new.captured_at, new.capture_source, new.labels = old.captured_at, old.capture_source, None
        _copy_completed_labels(s, old, new, src, dst_root, copied_image)
        copied[old.status] += 1
    return {
        "copied": sum(copied.values()),
        "copied_by_status": dict(copied),
        "not_in_new_plan": sum(not_in_plan.values()),
        "not_in_new_plan_by_status": dict(not_in_plan),
        "already_done_in_target": len(already_done),
        "moved": moved,
        "missing_files": missing,
    }
