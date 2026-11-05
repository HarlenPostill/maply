"""Independent annotation proposals, committed only by an explicit human decision."""
from __future__ import annotations
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, exists, func, select
from sqlalchemy.orm import Session

from app import backlog, tagging
from app.config import get_settings
from app.db import get_db
from app.models import Job, Shot, TagTask, CaptureTask, ReviewBatch, ReviewItem
from app.provider_settings import load_settings
from app.tag_schema import LabelResult, build_prompt, annotation_schema

router = APIRouter(prefix='/api')
MAX_CONCURRENT_REVIEWS = 20
ABSENCE_REASON = 'footpath: absence requires review before route exclusion'
Queue = Literal['absence', 'needs_check', 'skipped', 'no_imagery', 'blocked', 'ready', 'confirmed', 'recovery', 'untagged']
QUEUES = ('absence', 'needs_check', 'skipped', 'no_imagery', 'blocked', 'ready', 'confirmed', 'recovery', 'untagged')


def now():
    return datetime.now(timezone.utc)


def labels_digest(labels):
    return hashlib.sha256(json.dumps(labels, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def confirmed():
    return exists().where(ReviewItem.shot_id == Shot.id, ReviewItem.status == 'confirmed',
        ReviewItem.source_sha256 == Shot.result['sha256'].astext,
        Shot.labels['provenance']['footpath_review_id'].astext == ReviewItem.id.cast(String))


def condition(queue):
    if queue == 'absence':
        return (Shot.labels['footpath']['value'].astext == 'absent') & tagging.eligible() & ~confirmed()
    if queue == 'confirmed':
        return confirmed()
    if queue == 'ready':
        return exists().where(ReviewItem.shot_id == Shot.id, ReviewItem.status == 'ready',
                              ReviewItem.source_sha256 == Shot.result['sha256'].astext)
    if queue == 'recovery':
        return Shot.status.in_(('pending', 'retake'))
    if queue == 'untagged':
        return tagging.eligible() & ((Shot.labels.is_(None)) |
            Shot.labels['provenance']['image_sha256'].astext.is_distinct_from(Shot.result['sha256'].astext))
    return Shot.status == queue


def batch_dict(s, batch):
    counts = dict.fromkeys(('queued', 'running', 'ready', 'failed', 'confirmed', 'rejected', 'stale', 'cancelled'), 0)
    counts.update(dict(s.execute(select(ReviewItem.status, func.count()).where(ReviewItem.batch_id == batch.id).group_by(ReviewItem.status)).all()))
    return dict(id=str(batch.id), status=batch.status, model=batch.model, limit=batch.item_limit,
                concurrency=batch.concurrency, auto_confirm=batch.auto_confirm, counts=counts, created_at=batch.created_at.isoformat())


def item_dict(item, model):
    return dict(id=str(item.id), batch_id=str(item.batch_id), status=item.status, model=model,
        proposed_labels=item.proposed_labels, error=item.error, usage=item.usage,
        created_at=item.created_at.isoformat(), decided_at=item.decided_at.isoformat() if item.decided_at else None)


@router.get('/jobs/{jid}/review/summary')
def summary(jid: uuid.UUID, s: Session = Depends(get_db)):
    tagging._job(s, jid)
    counts = {q: s.scalar(select(func.count()).select_from(Shot).where(Shot.job_id == jid, condition(q))) for q in QUEUES}
    counts['absence_only'] = s.scalar(select(func.count()).select_from(Shot).where(
        Shot.job_id == jid, condition('absence'), Shot.result['manual_review_reason'].astext == ABSENCE_REASON))
    batches = s.scalars(select(ReviewBatch).where(ReviewBatch.job_id == jid).order_by(ReviewBatch.created_at.desc()).limit(20)).all()
    provider = load_settings()
    return dict(counts=counts, batches=[batch_dict(s, b) for b in batches],
                provider={'model': provider['openrouter_model'], 'configured': bool(provider['api_key'])})


@router.get('/jobs/{jid}/review/items')
def items(jid: uuid.UUID, queue: Queue = 'absence', offset: int = Query(0, ge=0),
          limit: int = Query(30, ge=1, le=100), s: Session = Depends(get_db)):
    job = tagging._job(s, jid)
    where = (Shot.job_id == jid, condition(queue))
    total = s.scalar(select(func.count()).select_from(Shot).where(*where))
    shots = s.scalars(select(Shot).where(*where).order_by(Shot.ordinal).offset(offset).limit(limit)).all()
    latest = {}
    if shots:
        for item, model in s.execute(select(ReviewItem, ReviewBatch.model).join(ReviewBatch).where(
            ReviewItem.shot_id.in_([sh.id for sh in shots])).order_by(ReviewItem.created_at.desc(), ReviewItem.id.desc())):
            latest.setdefault(item.shot_id, (item, model))
    output = []
    for sh in shots:
        r = sh.result or {}
        image = lambda key: f'/files/{quote(job.slug)}/{quote(r[key], safe="/")}?v={r.get("sha256", "")}' if r.get(key) else None
        evidence = get_settings().jobs_dir / job.slug / 'capture-review' / f'{sh.id}.jpg'
        output.append(dict(id=str(sh.id), code=sh.code, street=sh.street, status=sh.status,
            flags=sh.flags, note=sh.note, result=r, labels=tagging.labels_for(sh),
            image_url=image('file'), thumb_url=image('thumb'),
            review_image_url=f'/api/capture/review/{sh.id}/image' if 'manual_review' in sh.flags and evidence.is_file() else None,
            url=sh.url, footpath=sh.footpath, side=sh.side, lat=sh.lat, lng=sh.lng, heading=sh.heading,
            review=item_dict(*latest[sh.id]) if sh.id in latest else None))
    return dict(items=output, total=total, offset=offset, limit=limit)


class StartBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    limit: int = Field(20, ge=1, le=5000, strict=True)
    concurrency: int = Field(2, ge=1, le=MAX_CONCURRENT_REVIEWS, strict=True)
    auto_confirm: bool = Field(True, strict=True)


def create_batch(s, jid, req, shot_id=None):
    job = s.scalar(select(Job).where(Job.id == jid).with_for_update())
    if not job:
        raise HTTPException(404, 'Job not found')
    active = s.scalar(select(ReviewBatch).where(ReviewBatch.job_id == jid, ReviewBatch.status.in_(('running', 'paused'))))
    if active:
        if shot_id:
            raise HTTPException(409, 'Finish or cancel the current review batch before starting another photo.')
        return batch_dict(s, active)
    provider = load_settings()
    if not provider['api_key']:
        raise HTTPException(422, 'Save your OpenRouter key in Model settings first.')
    busy = exists().where(ReviewItem.shot_id == Shot.id, ReviewItem.status.in_(('queued', 'running', 'ready')))
    q = select(Shot).where(Shot.job_id == jid, tagging.eligible() if shot_id else condition('absence'), ~busy)
    if shot_id:
        q = q.where(Shot.id == shot_id)
    shots = s.scalars(q.order_by(Shot.ordinal).limit(req.limit).with_for_update(of=Shot)).all()
    if not shots:
        raise HTTPException(409, 'No eligible photos remain. Review existing proposals or recover missing photos first.')
    batch = ReviewBatch(job_id=jid, status='running', model=provider['openrouter_model'],
                        item_limit=len(shots), concurrency=req.concurrency, auto_confirm=req.auto_confirm)
    s.add(batch)
    s.flush()
    for sh in shots:
        s.add(ReviewItem(batch_id=batch.id, shot_id=sh.id, source_sha256=sh.result.get('sha256', ''),
                         source_labels_digest=labels_digest(sh.labels), status='queued', usage={}))
    s.flush()
    result = batch_dict(s, batch)
    s.commit()
    return result


@router.post('/jobs/{jid}/review/batches')
def start_batch(jid: uuid.UUID, req: StartBatch, s: Session = Depends(get_db)):
    return create_batch(s, jid, req)


@router.post('/review/shots/{sid}/tag')
def tag_recovered(sid: uuid.UUID, s: Session = Depends(get_db)):
    sh = s.get(Shot, sid)
    if not sh:
        raise HTTPException(404, 'Photo not found')
    return create_batch(s, sh.job_id, StartBatch(limit=1, concurrency=1, auto_confirm=False), sid)


@router.post('/review/batches/{bid}/{action}')
def control(bid: uuid.UUID, action: Literal['pause', 'resume', 'cancel'], s: Session = Depends(get_db)):
    batch = s.scalar(select(ReviewBatch).where(ReviewBatch.id == bid).with_for_update())
    if not batch:
        raise HTTPException(404, 'Batch not found')
    if action == 'cancel':
        batch.status = 'cancelled'
        for item in s.scalars(select(ReviewItem).where(ReviewItem.batch_id == bid, ReviewItem.status.in_(('queued', 'running'))).with_for_update()):
            item.status, item.token, item.lease_until = 'cancelled', None, None
    elif action == 'pause' and batch.status == 'running':
        batch.status = 'paused'
    elif action == 'resume' and batch.status == 'paused':
        batch.status = 'running'
    result = batch_dict(s, batch)
    s.commit()
    return result


def check_source(s, item, sh):
    if sh.status not in tagging.ELIGIBLE or sh.result.get('sha256') != item.source_sha256 or labels_digest(sh.labels) != item.source_labels_digest:
        raise HTTPException(409, 'Photo or labels changed since this review. Request a fresh review.')
    if tagging.digest(tagging.image_path(s.get(Job, sh.job_id), sh)) != item.source_sha256:
        raise HTTPException(409, 'Photo file changed since this review. Request a fresh review.')


def remaining_reasons(sh, capture_task, clear_footpath=False):
    reasons = list((sh.labels or {}).get('review_reasons', []))
    texts = [sh.result.get('manual_review_reason') or '']
    if capture_task and capture_task.status == 'review':
        texts.append(capture_task.error or '')
    # Known annotation reasons can themselves contain semicolons. Remove their
    # complete text before splitting the remaining capture-only reasons.
    for text in texts:
        residual = '; ' + text + '; '
        for reason in sorted(reasons, key=len, reverse=True):
            residual = residual.replace('; ' + reason + '; ', '; ')
        reasons.extend(residual.strip('; ').split('; '))
    ignored = {'', ABSENCE_REASON, 'Source photo capture flags: manual_review',
               'Source photo capture flags: no_image_date, manual_review',
               'Source photo capture flags: manual_review, no_image_date'}
    if clear_footpath:
        ignored.update(r for r in reasons if r.startswith('footpath: unknown; '))
        ignored.update({'footpath: model confidence below 0.80', 'footpath: partial evidence',
                        'footpath: occluded evidence', 'footpath: blurred evidence', 'footpath: outside_frame evidence'})
    return list(dict.fromkeys(r for r in reasons if r not in ignored))


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['confirm', 'reject', 'needs_capture']


def prepare_recovery(s, sh):
    task = s.scalar(select(TagTask).where(TagTask.shot_id == sh.id).with_for_update().execution_options(populate_existing=True))
    if task and task.status in ('queued', 'running'):
        raise HTTPException(409, 'Cancel existing tagging before preparing this photo for capture.')
    if s.scalar(select(CaptureTask).where(CaptureTask.shot_id == sh.id, CaptureTask.status == 'leased', CaptureTask.lease_until > now())):
        raise HTTPException(409, 'A capture worker currently owns this photo.')
    if sh.status not in ('pending', 'retake'):
        sh.result = {**sh.result, 'review_recovery': {'previous_status': sh.status,
            'reason': sh.result.get('manual_review_reason', ''), 'requested_at': now().isoformat()}}
    backlog.resolve(s, sh)
    sh.status = 'retake'


@router.post('/review/shots/{sid}/recover')
def recover_shot(sid: uuid.UUID, s: Session = Depends(get_db)):
    sh = s.scalar(select(Shot).where(Shot.id == sid).with_for_update())
    if not sh:
        raise HTTPException(404, 'Photo not found')
    prepare_recovery(s, sh)
    s.commit()
    return {'ok': True, 'status': 'retake'}


@router.post('/review/items/{iid}/decision')
def decide(iid: uuid.UUID, req: Decision, s: Session = Depends(get_db)):
    return apply_decision(iid, req, s)


def apply_decision(iid, req, s, *, automatic=False):
    target = s.get(ReviewItem, iid)
    if not target:
        raise HTTPException(404, 'Review not found')
    sh = s.scalar(select(Shot).where(Shot.id == target.shot_id).with_for_update())
    task = s.scalar(select(TagTask).where(TagTask.shot_id == sh.id).with_for_update().execution_options(populate_existing=True))
    item = s.scalar(select(ReviewItem).where(ReviewItem.id == iid).with_for_update().execution_options(populate_existing=True))
    if item.decision == req.decision:
        return {'ok': True, 'status': item.status}
    if item.status != 'ready':
        raise HTTPException(409, 'This proposal is not awaiting a decision.')
    try:
        check_source(s, item, sh)
    except HTTPException:
        item.status, item.error = 'stale', 'Photo or labels changed; start a fresh review.'
        s.commit()
        raise
    if task and task.status in ('queued', 'running'):
        raise HTTPException(409, 'Ordinary tagging is active for this photo. Finish or cancel it first.')
    if req.decision == 'confirm':
        labels = LabelResult.model_validate(item.proposed_labels).model_dump(mode='json')
        if labels['footpath']['value'] == 'unknown' and not automatic:
            raise HTTPException(422, 'An uncertain footpath cannot be confirmed. Keep it unresolved or capture better evidence.')
        capture_task = s.get(CaptureTask, sh.id)
        reasons = remaining_reasons(sh, capture_task,
            labels['footpath']['visibility'] == 'clear' and (labels['footpath']['confidence'] or 0) >= 0.8)
        labels['review_reasons'] = list(dict.fromkeys([r for r in labels['review_reasons'] if r != ABSENCE_REASON] + reasons))
        if not reasons and not [f for f in sh.flags if f not in ('manual_review', 'no_image_date')]:
            sh.flags = [f for f in sh.flags if f != 'manual_review']
        if task is None:
            task = TagTask(shot_id=sh.id, image_sha256=item.source_sha256, model=s.get(ReviewBatch, item.batch_id).model, provider='openrouter', status='completed', attempts=0)
            s.add(task)
            s.flush()
        task.attempts += 1
        task.image_sha256 = item.source_sha256
        task.model = s.get(ReviewBatch, item.batch_id).model
        provenance = {'footpath_review_id': str(item.id),
                      'review_confirmation': 'automatic' if automatic else 'human',
                      'review_accepted_at': now().isoformat()}
        if not automatic:
            provenance['human_confirmed_at'] = provenance['review_accepted_at']
        tagging.store_labels(s, sh, task, labels, task.model,
            provenance=provenance,
            resolved_review_reasons={ABSENCE_REASON})
        if not task.needs_review:
            backlog.resolve(s, sh)
            sh.status = 'accepted'
        item.status = 'confirmed'
    elif req.decision == 'needs_capture':
        prepare_recovery(s, sh)
        item.status = 'rejected'
    else:
        item.status = 'rejected'
    item.decision, item.decided_at = req.decision, now()
    s.commit()
    return {'ok': True, 'status': item.status}


def pause_after_restart(s):
    for batch in s.scalars(select(ReviewBatch).where(ReviewBatch.status == 'running').with_for_update()):
        batch.status = 'paused'
    for item in s.scalars(select(ReviewItem).where(ReviewItem.status == 'running').with_for_update()):
        item.status, item.token, item.lease_until = 'failed', None, None
        item.error = 'App restarted during a request. Its outcome is uncertain; start a new batch to retry explicitly.'
    s.commit()


def claim(s):
    batches = s.scalars(select(ReviewBatch).where(ReviewBatch.status == 'running').order_by(ReviewBatch.created_at).with_for_update(skip_locked=True)).all()
    for batch in batches:
        expired = s.scalars(select(ReviewItem).where(ReviewItem.batch_id == batch.id, ReviewItem.status == 'running', ReviewItem.lease_until < now()).with_for_update()).all()
        if expired:
            batch.status = 'paused'
            for item in expired:
                item.status, item.token = 'failed', None
                item.error = 'Review timed out; resume only after checking the provider outcome.'
            continue
        running = s.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.batch_id == batch.id, ReviewItem.status == 'running'))
        if running >= batch.concurrency:
            continue
        item = s.scalar(select(ReviewItem).where(ReviewItem.batch_id == batch.id, ReviewItem.status == 'queued').order_by(ReviewItem.created_at, ReviewItem.id).with_for_update(skip_locked=True).limit(1))
        if not item:
            if not running:
                batch.status = 'completed'
            continue
        sh = s.get(Shot, item.shot_id)
        try:
            check_source(s, item, sh)
        except HTTPException as exc:
            item.status, item.error = 'stale', str(exc.detail)
            continue
        item.status, item.token, item.lease_until = 'running', uuid.uuid4().hex, now() + timedelta(seconds=180)
        metadata = {'shot_id': str(sh.id), 'code': sh.code, 'street': sh.street, 'segment_key': sh.segment_key,
                    'side': sh.side, 'footpath': sh.footpath, 'view_kind': sh.view_kind, 'kind': sh.kind,
                    'planned_heading': sh.heading, 'camera_heading': sh.result.get('camera_heading'),
                    'imagery_date': sh.result.get('image_date_iso'), 'quality_flags': [f for f in sh.flags if f != 'manual_review']}
        payload = dict(id=item.id, token=item.token, path=tagging.image_path(s.get(Job, sh.job_id), sh),
            model=batch.model, prompt=build_prompt(metadata) + '\nIndependently inspect the indicated side of the street. Distinguish a real absent footpath from occlusion, a verge, a driveway, a path further back, or the wrong side. Use unknown when coverage is insufficient. Give specific visible evidence for your footpath finding and full annotations. Do not infer absence from a road or an obscured view.', schema=annotation_schema())
        s.commit()
        return payload
    s.commit()
    return None


def finish(s, iid, token, proposal=None, usage=None, error=None):
    target = s.get(ReviewItem, iid)
    if not target:
        return
    s.scalar(select(Shot).where(Shot.id == target.shot_id).with_for_update())
    item = s.scalar(select(ReviewItem).where(ReviewItem.id == iid).with_for_update().execution_options(populate_existing=True))
    if not item or item.status != 'running' or item.token != token:
        return
    if error:
        item.status, item.error = 'failed', error[:1500]
    else:
        item.proposed_labels = LabelResult.model_validate(proposal).model_dump(mode='json')
        item.status = 'ready'
    item.usage, item.token, item.lease_until = usage or {}, None, None
    if item.status == 'ready' and s.get(ReviewBatch, item.batch_id).auto_confirm:
        try:
            apply_decision(iid, Decision(decision='confirm'), s, automatic=True)
        except HTTPException as exc:
            item.error = str(exc.detail)
    s.commit()
