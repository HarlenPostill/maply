"""Re-plan a job with the current planner as a NEW job, and bring its finished photos across.

    cd backend && uv run python ../scripts/replan_job.py city-of-sydney --name "City of Sydney (divided roads fixed)"

The new job uses the original job's streets and options, so every shot the new
plan still has keeps its code, position and heading, and its photo, checks and
status are copied over. Shots the new plan dropped stay in the original job only.

The original job is only read: its database rows and folder are left exactly as
they are. To pick up photos taken in the original job after re-planning, run
again with --into <new job folder name>; shots already done there are kept.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from geoalchemy2.shape import to_shape  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.carry_over import carry_over  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.main import _counts, add_job  # noqa: E402
from app.models import Job  # noqa: E402
from app.shots import JobOptions  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", help="folder name (slug) of the job to re-plan, e.g. city-of-sydney")
    ap.add_argument("--name", help="name for the new job (default: '<source name> (re-planned)')")
    ap.add_argument("--into", help="carry photos into this existing job instead of creating one")
    a = ap.parse_args()

    created = None
    try:
        with session_scope() as s:
            src = s.execute(select(Job).where(Job.slug == a.source)).scalar_one_or_none()
            if src is None:
                print(f"No job with folder name {a.source!r}", file=sys.stderr)
                return 1
            if a.into:
                dst = s.execute(select(Job).where(Job.slug == a.into)).scalar_one_or_none()
                if dst is None:
                    print(f"No job with folder name {a.into!r}", file=sys.stderr)
                    return 1
            else:
                opts = JobOptions(**{k: v for k, v in src.params.items() if k in JobOptions.model_fields})
                area = to_shape(src.area) if src.area is not None else None
                dst, warnings = add_job(s, a.name or f"{src.name} (re-planned)", src.streets, opts, bool(src.params.get("whole_city")), area)
                created = dst.slug
                for w in warnings:
                    print("note:", w)
            report = carry_over(s, src, dst)
            s.flush()
            before, after = _counts(s, src.id), _counts(s, dst.id)
            print(json.dumps({"source": {"job": src.slug, **before}, "target": {"job": dst.slug, **after}, "carried_over": report}, indent=2))
    except BaseException:
        if created:  # the database rolled back; remove the half-made folder too
            shutil.rmtree(get_settings().jobs_dir / created, ignore_errors=True)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
