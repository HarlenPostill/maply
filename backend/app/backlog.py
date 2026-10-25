"""One manual backlog shared by capture, photo review and the Chrome extension."""
from app.models import Shot, CaptureTask
from sqlalchemy import exists, any_


def pending_review():
    return ('manual_review' == any_(Shot.flags)) | exists().where(CaptureTask.shot_id == Shot.id, CaptureTask.status == 'review')


def flag(sh, reason):
    sh.flags = list(dict.fromkeys([*(sh.flags or []), 'manual_review']))
    sh.result = {**(sh.result or {}), 'manual_review_reason': reason[:1200]}
    sh.status = 'needs_check' if (sh.result or {}).get('file') else 'skipped'


def resolve(s, sh):
    sh.flags = [f for f in (sh.flags or []) if f != 'manual_review']
    sh.result = {k: v for k, v in (sh.result or {}).items() if k != 'manual_review_reason'}
    task = s.get(CaptureTask, sh.id)
    if task and task.status == 'review':
        task.status, task.error = 'finished', None
