"""Requeue unassessed navigation failures from one run, with a private rollback receipt.

Stop maply-capture first. Run inside the app container; defaults to a dry run.
"""
import argparse
import json
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import inspect, select

from app import backlog
from app.capture import now
from app.config import get_settings
from app.db import session_scope
from app.models import CaptureRun, CaptureTask, Shot


def repair(run_id, apply=False):
    with session_scope() as s:
        run = s.scalar(select(CaptureRun).where(CaptureRun.id == run_id).with_for_update())
        if run is None:
            raise ValueError('Run not found')
        live = s.scalar(select(CaptureTask.shot_id).where(
            CaptureTask.run_id == run_id, CaptureTask.status == 'leased', CaptureTask.lease_until > now()).limit(1))
        if live:
            raise ValueError('Stop the worker and wait for its reservations to expire before repairing')
        rows = s.execute(select(Shot, CaptureTask).join(CaptureTask, CaptureTask.shot_id == Shot.id).where(
            CaptureTask.run_id == run_id, CaptureTask.status == 'review', CaptureTask.steps == 0,
            CaptureTask.error == 'Three interrupted attempts; manual review required',
            Shot.result['file'].astext.is_(None),
        ).with_for_update()).all()
        if not apply or not rows:
            return {'eligible': len(rows), 'requeued': 0}
        if run.finished < len(rows):
            raise ValueError('Run count does not cover the selected reviews')
        def record(obj):
            return {a.key: getattr(obj, a.key) for a in inspect(obj).mapper.column_attrs}
        receipt = {'run': record(run), 'shots': [record(sh) for sh, _ in rows],
                   'tasks': [record(task) for _, task in rows]}
        folder = get_settings().data_root / 'repairs'
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / f'navigation-{run_id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.json'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(receipt, f, default=str)
            f.flush()
            os.fsync(f.fileno())
        for sh, task in rows:
            task.status, task.attempts, task.error = 'released', 0, None
            task.decision, task.lease_until, task.updated_at = None, now(), now()
            backlog.resolve(s, sh)
            sh.status = 'pending'
        run.finished -= len(rows)
        return {'eligible': len(rows), 'requeued': len(rows), 'receipt': str(path)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=uuid.UUID, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(repair(args.run, args.apply)))
