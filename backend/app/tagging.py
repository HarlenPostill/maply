"""Durable per-photo vision queue. A host worker claims leased tasks over localhost.

Camera coordinates are never converted into purported feature coordinates. Map
progress aggregates observations by planned segment and side, not surveyed access.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from geoalchemy2.shape import to_shape
from pydantic import BaseModel, ConfigDict, Field
from shapely.geometry import mapping
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, session_scope
from app.models import Job, Segment, Shot, TagTask, TagWorker, TagTaskEvent
from app.tag_schema import LabelResult, SCHEMA_VERSION, build_prompt, cli_json_schema, review_reasons
from app.confidence import confidence_for_labels, aggregate_confidence

router = APIRouter(prefix='/api')
MODEL = 'gemini-3.8-flash-low'
ELIGIBLE = ('captured', 'accepted', 'needs_check')
LEASE_SECONDS = 300


def now():
    return datetime.now(timezone.utc)


def eligible():
    return Shot.status.in_(ELIGIBLE) & Shot.result.has_key('file')


def current_task():
    return TagTask.image_sha256 == Shot.result['sha256'].astext


def task_status():
    return case((current_task(), TagTask.status), else_='unlabelled')


def task_dict(sh: Shot):
    t = sh.tag_task
    if not t or t.image_sha256 != (sh.result or {}).get('sha256'):
        return None
    return {'status': t.status, 'error': t.error, 'needs_review': bool(t.needs_review),
            'model': t.model, 'updated_at': t.updated_at.isoformat(), 'attempts': t.attempts}


def labels_for(sh: Shot):
    if sh.labels and sh.labels.get('provenance', {}).get('image_sha256') == (sh.result or {}).get('sha256'):
        return sh.labels
    return None


def shot_confidence(sh: Shot):
    task = sh.tag_task
    if task and task.status == 'completed' and task.image_sha256 == (sh.result or {}).get('sha256'):
        return task.confidence
    return None


def add_event(s, task, kind, message, data=None):
    s.add(TagTaskEvent(task_id=task.id, attempt=task.attempts, kind=kind, message=message[:500], data=data or {}))


def _job(s, jid):
    j = s.get(Job, jid)
    if not j:
        raise HTTPException(404, 'Job not found')
    return j


def image_path(j: Job, sh: Shot) -> Path:
    root = (get_settings().jobs_dir / j.slug).resolve()
    p = (root / (sh.result or {}).get('file', '')).resolve()
    if root not in p.parents or p.suffix.lower() not in ('.jpg', '.jpeg', '.png') or not p.is_file():
        raise HTTPException(422, f'Image missing for {sh.code}; check or recapture the photo')
    return p


def digest(p):
    with p.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def recover(s):
    # A dead worker never leaves photos permanently stuck as running. Explicit retry
    # is required, avoiding endless paid retries for a repeatedly failing image.
    tasks = s.scalars(select(TagTask).where(TagTask.status == 'running', TagTask.lease_until < now()).with_for_update(skip_locked=True)).all()
    for task in tasks:
        task.status, task.error = 'failed', 'Worker disconnected or timed out. Retry this photo.'
        task.lease_token, task.lease_until, task.updated_at = None, None, now()
        add_event(s, task, 'failed', task.error)


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    streets: list[str] = Field(default_factory=list, max_length=2000)
    segment_keys: list[str] = Field(default_factory=list, max_length=10000)
    shot_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10000)
    side: Literal['left', 'right', 'none'] | None = None
    retry: bool = False
    force: bool = False

    def query(self, jid, require=True):
        if require and not (self.streets or self.segment_keys or self.shot_ids):
            raise HTTPException(422, 'Select at least one street, section or photo')
        q = select(Shot).where(Shot.job_id == jid)
        # Multiple selectors narrow the scope (intersection), never broaden it.
        if self.streets:
            q = q.where(Shot.street.in_(self.streets))
        if self.segment_keys:
            q = q.where(Shot.segment_key.in_(self.segment_keys))
        if self.shot_ids:
            q = q.where(Shot.id.in_(self.shot_ids))
        if self.side:
            q = q.where(Shot.side == self.side)
        return q


@router.post('/jobs/{jid}/tagging')
def enqueue(jid: uuid.UUID, req: Selection, s: Session = Depends(get_db)):
    j = _job(s, jid)
    recover(s)
    shots = s.execute(req.query(jid).order_by(Shot.ordinal).with_for_update(of=Shot)).scalars().all()
    queued = skipped = 0
    for sh in shots:
        if sh.status not in ELIGIBLE or not (sh.result or {}).get('file'):
            skipped += 1
            continue
        t = s.execute(select(TagTask).where(TagTask.shot_id == sh.id).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        sha = digest(image_path(j, sh))
        if t and t.status in ('queued', 'running') and t.image_sha256 == sha:
            skipped += 1
            continue
        current = t and t.image_sha256 == sha
        if current and ((t.status == 'completed' and not req.force) or (t.status in ('failed', 'cancelled') and not (req.retry or req.force))):
            skipped += 1
            continue
        if sh.result.get('sha256') != sha:
            sh.result = {**sh.result, 'sha256': sha}
            sh.labels = None
        if t is None:
            t = TagTask(shot_id=sh.id, status='queued', model=MODEL, image_sha256=sha)
            s.add(t)
        else:
            t.status, t.model, t.image_sha256 = 'queued', MODEL, sha
            t.error, t.worker_id, t.lease_token, t.lease_until = None, None, None, None
            t.needs_review = 0
            t.confidence, t.started_at = None, None
        t.updated_at = now()
        queued += 1
    s.commit()
    return {'queued': queued, 'skipped': skipped}


@router.post('/jobs/{jid}/tagging/cancel')
def cancel(jid: uuid.UUID, req: Selection, s: Session = Depends(get_db)):
    _job(s, jid)
    ids = req.query(jid, require=False).with_only_columns(Shot.id)
    tasks = s.scalars(select(TagTask).where(TagTask.shot_id.in_(ids), TagTask.status.in_(('queued', 'running'))).with_for_update()).all()
    for t in tasks:
        t.status, t.lease_token, t.lease_until, t.updated_at, t.error = 'cancelled', None, None, now(), None
        add_event(s, t, 'cancelled', 'Tagging cancelled; any in-flight result will be discarded.')
    s.commit()
    return {'cancelled': len(tasks)}


def empty_counts():
    return dict(total=0, eligible=0, unlabelled=0, queued=0, running=0, completed=0, failed=0, cancelled=0, needs_review=0)


def grouped(s, jid):
    st = case((eligible(), func.coalesce(task_status(), 'unlabelled')), else_='unavailable')
    nr = case((eligible() & current_task() & (TagTask.status == 'completed'), TagTask.needs_review), else_=0)
    return s.execute(select(Shot.segment_key, Shot.street, Shot.side, st, nr, func.count()).outerjoin(
        TagTask, TagTask.shot_id == Shot.id).where(Shot.job_id == jid).group_by(Shot.segment_key, Shot.street, Shot.side, st, nr)).all()


def add_count(c, st, review, n):
    c['total'] += n
    if st != 'unavailable':
        c['eligible'] += n
        c[st] += n
        if review:
            c['needs_review'] += n


def worker_status(s):
    workers = s.execute(select(TagWorker).order_by(TagWorker.last_seen.desc())).scalars().all()
    w = next((w for w in workers if not w.error and w.last_seen > now() - timedelta(seconds=90)), None)
    w = w or next(iter(workers), None)
    return {'online': bool(w and not w.error and w.last_seen > now() - timedelta(seconds=90)),
            'last_seen': w.last_seen.isoformat() if w else None, 'model': w.model if w else MODEL,
            'error': w.error if w else None}


@router.get('/jobs/{jid}/tagging')
def summary(jid: uuid.UUID, s: Session = Depends(get_db)):
    _job(s, jid)
    recover(s)
    c, streets = empty_counts(), defaultdict(empty_counts)
    for _, street, _, st, review, n in grouped(s, jid):
        add_count(c, st, review, n)
        add_count(streets[street], st, review, n)
    metrics = defaultdict(list)
    for street, confidence in s.execute(select(Shot.street, TagTask.confidence).join(TagTask, TagTask.shot_id == Shot.id).where(
        Shot.job_id == jid, eligible(), current_task(), TagTask.status == 'completed', TagTask.confidence.is_not(None))):
        metrics[street].append(confidence)
    s.commit()
    return {'counts': c, 'streets': [{'street': street, **v, 'confidence': aggregate_confidence(metrics[street])} for street, v in sorted(streets.items())],
            'confidence': aggregate_confidence([m for values in metrics.values() for m in values]), 'worker': worker_status(s), 'model': MODEL}


def activity_snapshot(s, jid, status=None, limit=50, offset=0):
    _job(s, jid)
    recover(s)
    statuses = status.split(',') if status else None
    if statuses and any(st not in {'queued', 'running', 'completed', 'failed', 'cancelled'} for st in statuses):
        raise HTTPException(422, 'Unsupported activity status')
    base = (Shot.job_id == jid, current_task())
    filters = (*base, TagTask.status.in_(statuses)) if statuses else base
    total = s.scalar(select(func.count()).select_from(TagTask).join(Shot, Shot.id == TagTask.shot_id).where(*filters))
    priority = case((TagTask.status == 'running', 0), (TagTask.status == 'queued', 1), else_=2)
    rows = s.execute(select(TagTask, Shot.code, Shot.street, Shot.side).join(Shot, Shot.id == TagTask.shot_id).where(*filters).order_by(
        priority, case((TagTask.status == 'queued', TagTask.updated_at)).asc(), TagTask.updated_at.desc(), TagTask.id
    ).offset(offset).limit(limit)).all()
    queue_order = s.scalars(select(TagTask.id).join(Shot, Shot.id == TagTask.shot_id).where(*base, TagTask.status == 'queued').order_by(TagTask.updated_at, TagTask.id)).all()
    positions = {tid: n + 1 for n, tid in enumerate(queue_order)}
    events = defaultdict(list)
    ids = [t.id for t, *_ in rows]
    if ids:
        ranked = select(TagTaskEvent.id, func.row_number().over(partition_by=TagTaskEvent.task_id, order_by=TagTaskEvent.id.desc()).label('rank')).where(TagTaskEvent.task_id.in_(ids)).subquery()
        for event in s.scalars(select(TagTaskEvent).join(ranked, ranked.c.id == TagTaskEvent.id).where(ranked.c.rank <= 30).order_by(TagTaskEvent.id)):
            events[(event.task_id, event.attempt)].append({'id': event.id, 'kind': event.kind, 'message': event.message, 'data': event.data, 'created_at': event.created_at.isoformat()})
    counts = empty_counts()
    for _, _, _, st, review, n in grouped(s, jid):
        add_count(counts, st, review, n)
    usage = s.scalar(select(TagWorker.usage).where(TagWorker.usage.is_not(None)).order_by(TagWorker.usage['fetched_at'].astext.desc()).limit(1))
    items = [{'task_id': str(t.id), 'shot_id': str(t.shot_id), 'code': code, 'street': street, 'side': side,
              'status': t.status, 'model': t.model, 'attempts': t.attempts, 'error': t.error,
              'updated_at': t.updated_at.isoformat(), 'started_at': t.started_at.isoformat() if t.started_at else None,
              'queue_position': positions.get(t.id), 'events': [] if t.status == 'queued' else events[(t.id, t.attempts)],
              'confidence': t.confidence if t.status == 'completed' else None}
             for t, code, street, side in rows]
    s.commit()
    return {'items': items, 'total': total, 'counts': counts, 'usage': usage}


@router.get('/jobs/{jid}/tagging/activity')
def activity(jid: uuid.UUID, status: str | None = None, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), s: Session = Depends(get_db)):
    return activity_snapshot(s, jid, status, limit, offset)


@router.get('/jobs/{jid}/tagging/activity/stream')
async def activity_stream(request: Request, jid: uuid.UUID, status: str | None = None, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    def read():
        with session_scope() as session:
            return activity_snapshot(session, jid, status, limit, offset)
    initial = await asyncio.to_thread(read)

    async def stream():
        snapshot, previous = initial, None
        while not await request.is_disconnected():
            payload = json.dumps(snapshot, separators=(',', ':'))
            if payload != previous:
                yield f'event: activity\ndata: {payload}\n\n'
                previous = payload
            else:
                yield ': heartbeat\n\n'
            await asyncio.sleep(2)
            snapshot = await asyncio.to_thread(read)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def progress_status(c):
    if not c['eligible']:
        return 'unavailable'
    for key in ('running', 'queued', 'failed'):
        if c[key]:
            return key
    if c['completed'] == c['eligible']:
        return 'completed'
    return 'partial' if c['completed'] else 'unlabelled'


@router.get('/jobs/{jid}/tagging/segments')
def segments(jid: uuid.UUID, s: Session = Depends(get_db)):
    _job(s, jid)
    recover(s)
    groups = defaultdict(empty_counts)
    for key, street, side, st, review, n in grouped(s, jid):
        add_count(groups[(key, side)], st, review, n)
    features = []
    for seg in s.execute(select(Segment).where(Segment.job_id == jid).order_by(Segment.ordinal)).scalars():
        for side in ('left', 'right', 'none'):
            c = groups.get((seg.key, side))
            if not c:
                continue
            features.append({'type': 'Feature', 'geometry': mapping(to_shape(seg.geom)), 'properties': {
                'segment_key': seg.key, 'street': seg.street, 'side': side, **c, 'status': progress_status(c)}})
    s.commit()
    return {'type': 'FeatureCollection', 'features': features}


def _worker_header(x_maply: str | None = Header(None)):
    if x_maply != 'tagging-worker':
        raise HTTPException(403, 'Worker requests require X-Maply: tagging-worker')


worker_router = APIRouter(prefix='/tagging/worker', dependencies=[Depends(_worker_header)])


class UsageBucket(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(max_length=100)
    name: str = Field(max_length=100)
    window: str = Field(max_length=100)
    remaining_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)
    reset_time: str | None = Field(default=None, max_length=100)


class UsageGroup(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(max_length=100)
    buckets: list[UsageBucket] = Field(max_length=30)


class UsageSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['available', 'unavailable']
    fetched_at: datetime
    source: Literal['antigravity-cli']
    groups: list[UsageGroup] = Field(default_factory=list, max_length=20)
    error: str | None = Field(default=None, max_length=500)


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    worker_id: str = Field(min_length=1, max_length=100)
    model: str = Field(default=MODEL, min_length=1, max_length=100)
    error: str | None = Field(default=None, max_length=4000)
    usage: UsageSnapshot | None = None


class LeaseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    worker_id: str = Field(min_length=1, max_length=100)
    lease_token: str = Field(min_length=1, max_length=64)


class CompleteRequest(LeaseRequest):
    labels: LabelResult
    model: str = Field(min_length=1, max_length=100)


class FailRequest(LeaseRequest):
    error: str = Field(min_length=1, max_length=4000)


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    thinking_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class EventData(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stage: str | None = Field(default=None, max_length=100)
    attempt: int | None = Field(default=None, ge=1, le=2)
    usage: TokenUsage | None = None
    final: bool | None = None
    tool: str | None = Field(default=None, max_length=100)
    output_text: str | None = Field(default=None, max_length=2000)


class EventRequest(LeaseRequest):
    kind: Literal['stage', 'tool', 'usage', 'output', 'warning']
    message: str = Field(min_length=1, max_length=500)
    data: EventData = Field(default_factory=EventData)


def touch_worker(s, req):
    values = {'model': req.model, 'last_seen': now(), 'error': req.error}
    if req.usage is not None:
        values['usage'] = req.usage.model_dump(mode='json')
    s.execute(insert(TagWorker).values(id=req.worker_id, **values).on_conflict_do_update(index_elements=[TagWorker.id], set_=values))


@worker_router.post('/heartbeat')
def heartbeat(req: WorkerRequest, s: Session = Depends(get_db)):
    touch_worker(s, req)
    recover(s)
    s.commit()
    return {'ok': True}


@worker_router.post('/claim')
def claim(req: WorkerRequest, s: Session = Depends(get_db)):
    touch_worker(s, req)
    recover(s)
    t = s.execute(select(TagTask).where(TagTask.status == 'queued', TagTask.model == req.model).order_by(
        TagTask.updated_at, TagTask.id).with_for_update(skip_locked=True).limit(1)).scalar_one_or_none()
    if not t:
        s.commit()
        return {'task': None}
    sh = s.get(Shot, t.shot_id)
    j = s.get(Job, sh.job_id)
    try:
        if sh.status not in ELIGIBLE:
            raise HTTPException(409, 'Photo is no longer eligible for tagging')
        if digest(image_path(j, sh)) != t.image_sha256:
            raise HTTPException(409, 'Image changed after queuing; queue the new photo')
    except HTTPException as e:
        t.status, t.error, t.updated_at = 'failed', str(e.detail), now()
        add_event(s, t, 'failed', t.error)
        s.commit()
        return {'task': None}
    t.status, t.worker_id, t.lease_token = 'running', req.worker_id, uuid.uuid4().hex
    t.lease_until, t.updated_at = now() + timedelta(seconds=LEASE_SECONDS), now()
    t.attempts += 1
    t.started_at = now()
    add_event(s, t, 'stage', 'Worker claimed image.', {'stage': 'claimed'})
    metadata = {'shot_id': str(sh.id), 'code': sh.code, 'street': sh.street, 'segment_key': sh.segment_key,
                'side': sh.side, 'footpath': sh.footpath, 'view_kind': sh.view_kind, 'kind': sh.kind,
                'planned_heading': sh.heading, 'camera_heading': sh.result.get('camera_heading'),
                'imagery_date': sh.result.get('image_date_iso'), 'quality_flags': sh.flags}
    task = {'id': str(t.id), 'lease_token': t.lease_token, 'shot_id': str(sh.id),
            'image_url': f'/files/{quote(j.slug)}/{quote(sh.result["file"], safe="/")}?sha={t.image_sha256}',
            'image_sha256': t.image_sha256, 'prompt': build_prompt(metadata),
            'schema': cli_json_schema(), 'model': t.model}
    s.commit()
    return {'task': task}


def leased(s, tid, req):
    t = s.execute(select(TagTask).where(TagTask.id == tid).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if not t:
        raise HTTPException(404, 'Tagging task not found')
    if t.status != 'running' or t.worker_id != req.worker_id or t.lease_token != req.lease_token or not t.lease_until or t.lease_until < now():
        raise HTTPException(409, 'Task lease expired or task cancelled; result was not applied')
    return t


@worker_router.post('/tasks/{tid}/heartbeat')
def task_heartbeat(tid: uuid.UUID, req: LeaseRequest, s: Session = Depends(get_db)):
    t = leased(s, tid, req)
    t.lease_until, t.updated_at = now() + timedelta(seconds=LEASE_SECONDS), now()
    w = s.get(TagWorker, req.worker_id)
    if w:
        w.last_seen = now()
    s.commit()
    return {'ok': True}


@worker_router.post('/tasks/{tid}/events')
def task_event(tid: uuid.UUID, req: EventRequest, s: Session = Depends(get_db)):
    t = leased(s, tid, req)
    count = s.scalar(select(func.count()).select_from(TagTaskEvent).where(TagTaskEvent.task_id == tid, TagTaskEvent.attempt == t.attempts))
    if count >= 200:
        raise HTTPException(429, 'Activity event limit reached for this attempt')
    add_event(s, t, req.kind, req.message, req.data.model_dump(mode='json', exclude_none=True))
    s.commit()
    return {'ok': True}


@worker_router.post('/tasks/{tid}/complete')
def complete(tid: uuid.UUID, req: CompleteRequest, s: Session = Depends(get_db)):
    # Match enqueue's Shot -> TagTask lock ordering to avoid a retry/completion deadlock.
    target = s.get(TagTask, tid)
    if not target:
        raise HTTPException(404, 'Tagging task not found')
    sh = s.execute(select(Shot).where(Shot.id == target.shot_id).with_for_update(of=Shot)).scalar_one()
    t = leased(s, tid, req)
    j = s.get(Job, sh.job_id)
    if sh.status not in ELIGIBLE or digest(image_path(j, sh)) != t.image_sha256 or sh.result.get('sha256') != t.image_sha256:
        t.status, t.error, t.lease_token, t.updated_at = 'failed', 'Image changed during analysis; queue the new photo', None, now()
        add_event(s, t, 'failed', t.error)
        s.commit()
        raise HTTPException(409, t.error)
    if req.model != t.model:
        raise HTTPException(422, 'Result model does not match the queued model')
    labels = req.labels.model_dump(mode='json')
    labels['review_reasons'] = list(dict.fromkeys(labels['review_reasons'] + review_reasons(labels)))
    if sh.flags:
        labels['review_reasons'].append('Source photo capture flags: ' + ', '.join(sh.flags))
    labels['provenance'] = {'task_id': str(t.id), 'attempt': t.attempts, 'image_sha256': t.image_sha256,
                            'model': req.model, 'provider': 'antigravity-cli', 'schema_version': SCHEMA_VERSION,
                            'tagged_at': now().isoformat(), 'imagery_date': sh.result.get('image_date_iso'),
                            'shot_id': str(sh.id), 'segment_key': sh.segment_key, 'side': sh.side,
                            'confidence_kind': 'model_reported_uncalibrated'}
    # Keep each successful attempt on disk as well as the current labels in Postgres.
    out = get_settings().jobs_dir / j.slug / 'labels'
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f'{sh.code}_{t.id}_{t.attempts}.json'
    tmp = dest.with_suffix('.tmp')
    tmp.write_text(json.dumps(labels, indent=2))
    tmp.replace(dest)
    sh.labels = labels
    t.status, t.error, t.needs_review = 'completed', None, int(bool(labels['review_reasons']))
    t.confidence = confidence_for_labels(labels)
    add_event(s, t, 'completed', 'Validated annotation saved.')
    t.lease_token, t.lease_until, t.updated_at = None, None, now()
    s.commit()
    return {'ok': True, 'needs_review': bool(t.needs_review)}


@worker_router.post('/tasks/{tid}/fail')
def fail(tid: uuid.UUID, req: FailRequest, s: Session = Depends(get_db)):
    t = leased(s, tid, req)
    t.status, t.error, t.updated_at = 'failed', req.error, now()
    add_event(s, t, 'failed', req.error)
    t.lease_token, t.lease_until = None, None
    s.commit()
    return {'ok': True}


router.include_router(worker_router)
