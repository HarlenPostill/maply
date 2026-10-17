"""Bring finished shots from one job into another planned over the same streets.

Used when a job is re-planned with a newer planner. A shot with the same code,
position and heading in both jobs is the same photo, so its file, thumbnail,
camera checks and status are copied across. The source job is only read.

Only shots still pending in the target are filled, so running it again later
picks up anything finished in the source since, without touching new work.
"""

from __future__ import annotations

import shutil
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import results
from app.config import get_settings
from app.models import Job, Shot


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
        if new.status != "pending":
            already_done.append(old.code)
            continue
        res = dict(old.result or {})
        if res.get("file"):
            image = src_root / res["file"]
            if not image.is_file():
                missing.append(old.code)
                continue
            dest = (dst_root / "images" / new.filename).with_suffix(image.suffix)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(image, dest)  # keeps the modified time that result["saved_at"] records
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
        new.captured_at, new.capture_source, new.labels = old.captured_at, old.capture_source, old.labels
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
