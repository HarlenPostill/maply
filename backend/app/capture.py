"""Visual capture orchestration. Browser interaction happens only on the host desktop."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import and_, exists, or_, select, func
from sqlalchemy.orm import Session

from app.capture_schema import CaptureDecision, PROMPT, COMBINED_INSTRUCTIONS, combined_schema
from app.db import get_db
from app.config import get_settings
from app.capture_desktop import maps_url
from app.geo.streetview_url import parse_streetview_url
from app.models import CaptureEvent, CaptureRun, CaptureTask, Job, Shot, TagTask
from app import backlog
from app.tag_schema import build_prompt, LabelResult
from app.provider_settings import load_settings

router = APIRouter(prefix='/api/capture')
LEASE_SECONDS = 300


def now():
    return datetime.now(timezone.utc)


def worker_header(x_maply: str | None = Header(None)):
    if x_maply != 'capture-worker':
        raise HTTPException(403, 'Capture worker header required')


class RunConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, allow_inf_nan=False)
    model: str = Field(default='google/gemini-3.1-flash-lite', min_length=3, max_length=100, pattern=r'^[^\s/]+/[^\s]+$')
    fallback_models: list[str] = Field(default_factory=list, max_length=5)
    max_photos: int = Field(default=200, ge=1, le=100000)
    max_requests: int = Field(default=1000, ge=1, le=1000000)
    max_cost_usd: float = Field(default=5, gt=0, le=10000)
    hours: float = Field(default=8, gt=0, le=72)
    max_steps: int = Field(default=6, ge=1, le=20)
    min_confidence: float = Field(default=.9, ge=.5, le=1)
    load_seconds: float = Field(default=4, ge=2, le=30)

    @field_validator('fallback_models')
    @classmethod
    def valid_models(cls, models):
        if any(len(m) > 100 or '/' not in m or len(m.split('/')[0]) == 0 or len(m.split('/')[-1]) == 0 or any(c.isspace() for c in m) for m in models):
            raise ValueError('Fallbacks must be full OpenRouter model IDs')
        return models


class NewRun(RunConfig):
    job_id: uuid.UUID


class RunControl(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: str | None = Field(default=None, max_length=600)


class LeaseRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=100, pattern=r'^[\w.-]+$')


class LeaseAuth(LeaseRequest):
    token: str = Field(min_length=32, max_length=64)


class Assessment(LeaseAuth):
    image: str = Field(max_length=16000000)
    page_url: str = Field(max_length=20000)


class Completion(Assessment):
    pass


class Release(LeaseAuth):
    reason: str = Field(max_length=600)


def public_run(r):
    return {'id': str(r.id), 'job_id': str(r.job_id), 'status': r.status, 'config': r.config,
            'requests': r.requests, 'cost_usd': r.cost_usd, 'finished': r.finished,
            'error': r.error, 'created_at': r.created_at.isoformat(), 'expires_at': r.expires_at.isoformat()}


def locked_run(s, rid):
    r = s.scalar(select(CaptureRun).where(CaptureRun.id == rid).with_for_update())
    if r is None:
        raise HTTPException(404, 'Capture run not found')
    return r


def revoke(s, rid):
    for task in s.scalars(select(CaptureTask).where(CaptureTask.run_id == rid, CaptureTask.status == 'leased').with_for_update()):
        if task.steps == 0:
            task.attempts = max(0, task.attempts - 1)
        task.status, task.decision, task.lease_until = 'released', None, now()


def active(r, s, *, limits=True):
    if r.status == 'running' and (r.expires_at <= now() or r.finished >= r.config['max_photos']):
        r.status, r.error = 'completed', 'Run duration or photo limit reached'
        revoke(s, r.id)
        s.commit()
    if limits and r.status == 'running' and (r.requests >= r.config['max_requests'] or r.cost_usd >= r.config['max_cost_usd']):
        r.status, r.error = 'paused', 'Request or reported USD cost limit reached'
        revoke(s, r.id)
        s.commit()
    if r.status != 'running':
        raise HTTPException(409, r.error or f'Run is {r.status}')


def locked_task(s, rid, sid, auth):
    r = locked_run(s, rid)
    active(r, s, limits=False)
    sh = s.scalar(select(Shot).where(Shot.id == sid).with_for_update())
    t = s.scalar(select(CaptureTask).where(CaptureTask.shot_id == sid).with_for_update())
    if not t or t.run_id != rid or t.worker_id != auth.worker_id or t.token != auth.token or t.status != 'leased' or t.lease_until <= now():
        raise HTTPException(409, 'Shot reservation expired or belongs to another worker')
    if sh.status not in ('pending', 'retake'):
        raise HTTPException(409, 'Shot already handled elsewhere')
    return r, sh, t


def reserved_shot():
    return exists().where(CaptureTask.shot_id == Shot.id, CaptureTask.status == 'leased', CaptureTask.lease_until > now())


def image_bytes(value):
    try:
        header, encoded = value.split(',', 1)
        if header not in ('data:image/jpeg;base64', 'data:image/png;base64'):
            raise ValueError()
        data = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(data)) as im:
            if im.width < 640 or im.height < 400 or im.width * im.height > 20000000:
                raise ValueError()
            im.verify()
        return data
    except (ValueError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, 'Expected a readable desktop JPEG/PNG between 640×400 and 20 megapixels')


def review(r, t, reason, sh):
    t.status, t.error, t.updated_at = 'review', reason[:600], now()
    r.finished += 1
    backlog.flag(sh, reason)


@router.post('/runs')
def create_run(req: NewRun, s: Session = Depends(get_db)):
    if not s.get(Job, req.job_id):
        raise HTTPException(404, 'Job not found')
    if not load_settings().get('api_key'):
        raise HTTPException(422, 'Save an OpenRouter key in Model settings first')
    r = CaptureRun(job_id=req.job_id, config=req.model_dump(exclude={'job_id'}), expires_at=now() + timedelta(hours=req.hours))
    s.add(r)
    s.commit()
    return public_run(r)


@router.get('/jobs/{jid}')
def job_runs(jid: uuid.UUID, s: Session = Depends(get_db)):
    runs = s.scalars(select(CaptureRun).where(CaptureRun.job_id == jid).order_by(CaptureRun.created_at.desc()).limit(10)).all()
    tasks = s.execute(select(CaptureTask, Shot.code).join(Shot, Shot.id == CaptureTask.shot_id).where(
        Shot.job_id == jid, CaptureTask.status.in_(['review', 'leased'])).order_by(CaptureTask.updated_at.desc()).limit(100)).all()
    return {'runs': [public_run(r) for r in runs], 'tasks': [
        {'shot_id': str(t.shot_id), 'code': code, 'status': t.status, 'worker_id': t.worker_id,
         'lease_until': t.lease_until.isoformat(), 'error': t.error, 'steps': t.steps,
         'updated_at': t.updated_at.isoformat(), 'has_evidence': bool(t.decision and t.decision.get('value', {}).get('action') == 'review')} for t, code in tasks]}


@router.get('/runs/{rid}')
def get_run(rid: uuid.UUID, s: Session = Depends(get_db)):
    r = s.get(CaptureRun, rid)
    if not r:
        raise HTTPException(404, 'Capture run not found')
    return public_run(r)


@router.post('/runs/{rid}/{action}')
def control_run(rid: uuid.UUID, action: Literal['pause', 'resume', 'stop'], req: RunControl | None = None, s: Session = Depends(get_db)):
    r = locked_run(s, rid)
    if r.status in ('stopped', 'completed'):
        raise HTTPException(409, 'Create a new run after this run ends')
    r.status, r.error = {'pause': 'paused', 'resume': 'running', 'stop': 'stopped'}[action], None
    if action == 'pause' and req:
        r.error = req.reason
    if action == 'resume':
        active(r, s)
    if action in ('pause', 'stop'):
        revoke(s, rid)
    s.commit()
    return public_run(r)


@router.put('/runs/{rid}')
def configure_run(rid: uuid.UUID, req: RunConfig, s: Session = Depends(get_db)):
    r = locked_run(s, rid)
    if r.status != 'paused':
        raise HTTPException(409, 'Pause before changing the capture model or limits')
    r.config = req.model_dump()
    r.expires_at = now() + timedelta(hours=req.hours)
    s.commit()
    return public_run(r)


@router.post('/review/{sid}/retry')
def retry_review(sid: uuid.UUID, s: Session = Depends(get_db)):
    # Same lock order as worker commits: run -> shot -> task.
    task = s.get(CaptureTask, sid)
    if not task:
        raise HTTPException(404, 'Capture task not found')
    locked_run(s, task.run_id)
    sh = s.scalar(select(Shot).where(Shot.id == sid).with_for_update())
    t = s.scalar(select(CaptureTask).where(CaptureTask.shot_id == sid).with_for_update())
    if t.status != 'review':
        raise HTTPException(409, 'Only review tasks can be retried')
    t.status, t.attempts, t.error = 'released', 0, None
    backlog.resolve(s, sh)
    sh.status = 'retake' if (sh.result or {}).get('file') else 'pending'
    s.commit()
    return {'ok': True}


@router.post('/worker/runs/{rid}/claim', dependencies=[Depends(worker_header)])
def claim(rid: uuid.UUID, req: LeaseRequest, s: Session = Depends(get_db)):
    from app.main import _shot_dict
    r = locked_run(s, rid)
    active(r, s)
    leased_count = s.scalar(select(func.count()).select_from(CaptureTask).where(
        CaptureTask.run_id == rid, CaptureTask.status == 'leased', CaptureTask.lease_until > now()))
    if r.finished + leased_count >= r.config['max_photos']:
        s.commit()
        return {'task': None, 'status': 'running'}
    # Locking the run serialises workers on this run; locking the shot also protects cross-run claims.
    busy = exists().where(CaptureTask.shot_id == Shot.id, or_(CaptureTask.status == 'review',
        and_(CaptureTask.status == 'leased', CaptureTask.lease_until > now())))
    sh = s.scalar(select(Shot).where(Shot.job_id == r.job_id, Shot.status.in_(['pending', 'retake']), ~backlog.pending_review(), ~busy)
                  .order_by(Shot.ordinal).with_for_update(skip_locked=True).limit(1))
    if sh is None:
        in_flight = s.scalar(select(CaptureTask.shot_id).join(Shot, Shot.id == CaptureTask.shot_id).where(
            Shot.job_id == r.job_id, CaptureTask.status == 'leased', CaptureTask.lease_until > now()).limit(1))
        remaining = s.scalar(select(Shot.id).where(Shot.job_id == r.job_id, Shot.status.in_(['pending', 'retake']), ~backlog.pending_review(), ~busy).limit(1))
        if not in_flight and not remaining:
            r.status = 'completed'
        s.commit()
        return {'task': None, 'status': r.status}
    t = s.get(CaptureTask, sh.id)
    if t is None:
        t = CaptureTask(shot_id=sh.id, attempts=0)
        s.add(t)
    t.run_id, t.worker_id, t.token = rid, req.worker_id, uuid.uuid4().hex
    t.status, t.steps, t.decision, t.error = 'leased', 0, None, None
    t.attempts += 1
    t.lease_until, t.updated_at = now() + timedelta(seconds=LEASE_SECONDS), now()
    if t.attempts > 3:
        review(r, t, 'Three interrupted attempts; manual review required', sh)
        s.commit()
        return {'task': None, 'status': 'running'}
    job = s.get(Job, r.job_id)
    payload = _shot_dict(sh, job.slug)
    s.commit()
    return {'task': {'shot': payload, 'token': t.token, 'config': r.config, 'lease_seconds': LEASE_SECONDS}}


@router.post('/worker/runs/{rid}/shots/{sid}/heartbeat', dependencies=[Depends(worker_header)])
def heartbeat(rid: uuid.UUID, sid: uuid.UUID, req: LeaseAuth, s: Session = Depends(get_db)):
    _, _, t = locked_task(s, rid, sid, req)
    t.lease_until, t.updated_at = now() + timedelta(seconds=LEASE_SECONDS), now()
    s.commit()
    return {'ok': True}


@router.post('/worker/runs/{rid}/shots/{sid}/review', dependencies=[Depends(worker_header)])
def release_review(rid: uuid.UUID, sid: uuid.UUID, req: Release, s: Session = Depends(get_db)):
    r, sh, t = locked_task(s, rid, sid, req)
    review(r, t, req.reason, sh)
    s.commit()
    return {'ok': True}


@router.post('/worker/runs/{rid}/shots/{sid}/assess', dependencies=[Depends(worker_header)])
async def assess(rid: uuid.UUID, sid: uuid.UUID, req: Assessment, s: Session = Depends(get_db)):
    from app.results import camera_check
    from app.models import Segment
    data = image_bytes(req.image)
    r, sh, t = locked_task(s, rid, sid, req)
    active(r, s)
    if t.decision and t.decision.get('pending'):
        raise HTTPException(409, 'An assessment is already in flight')
    if t.steps >= r.config['max_steps']:
        review(r, t, 'Visual adjustment limit reached', sh)
        s.commit()
        raise HTTPException(409, 'Visual adjustment limit reached; queued for review')
    key = load_settings().get('api_key')
    if not key:
        raise HTTPException(422, 'OpenRouter key is not configured')
    bearing = s.scalar(select(Segment.bearing_deg).where(Segment.job_id == sh.job_id, Segment.key == sh.segment_key))
    flags, camera = camera_check(sh, req.page_url, None, bearing)
    # The vision model reads imagery dates later; their absence here is not a camera failure.
    flags = [flag for flag in flags if flag != 'no_image_date']
    parsed_camera = parse_streetview_url(req.page_url)
    camera_valid = (maps_url(req.page_url) and parsed_camera.url_form == 'client_path'
        and parsed_camera.is_street_view and parsed_camera.pano_kind != '10'
        and camera.get('heading_delta') is not None and abs(camera['heading_delta']) <= 15
        and camera.get('camera_offset_m') is not None and camera['camera_offset_m'] <= 40
        and not set(flags) & {'wrong_side', 'heading_off', 'far_from_plan'})
    shot_context = {k: getattr(sh, k) for k in ['code', 'street', 'lat', 'lng', 'heading', 'pitch', 'fov', 'side', 'footpath']}
    shot_context.update(look=(sh.result or {}).get('look'), camera=camera, camera_flags=flags)
    config = dict(r.config)
    t.steps += 1
    step = t.steps
    t.decision = {'pending': True, 'step': step}
    t.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
    r.requests += 1  # Reserve before network IO: retries and concurrent workers cannot exceed the request limit.
    s.commit()
    body = {'model': config['model'], 'models': [config['model'], *config['fallback_models']],
            'provider': {'require_parameters': True}, 'max_tokens': 16000,
            'session_id': f'maply-capture-{rid}',
            'messages': [{'role': 'system', 'content': PROMPT + COMBINED_INSTRUCTIONS + '\nTagging rules:\n' + build_prompt({})}, {'role': 'user', 'content': [
                {'type': 'text', 'text': json.dumps(shot_context, default=str)},
                {'type': 'image_url', 'image_url': {'url': req.image}}]}],
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'capture_decision', 'strict': False, 'schema': combined_schema()}}}
    error, response, decision = None, {}, None
    try:
        async with httpx.AsyncClient(timeout=65, follow_redirects=False) as client:
            result = await client.post('https://openrouter.ai/api/v1/chat/completions',
                headers={'Authorization': f'Bearer {key}'}, json=body)
            result.raise_for_status()
            response = result.json()
        choice = response['choices'][0]
        if choice.get('finish_reason') == 'content_filter' or choice.get('message', {}).get('refusal'):
            decision = CaptureDecision(action='pass', reason='The model declined to assess this view; manual review required.',
                confidence=0, start_x=.5, start_y=.5, end_x=.5, end_y=.5, image_date=None, labels=None)
        else:
            if choice.get('finish_reason') != 'stop':
                raise ValueError('Incomplete model response')
            decision = CaptureDecision.model_validate_json(choice['message']['content'])
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, AttributeError, ValidationError):
        error = 'Model request failed or returned an invalid capture decision; run paused'
    # Expire ORM state after IO so pause/stop and lease changes are observed.
    s.expire_all()
    r = locked_run(s, rid)
    t = s.scalar(select(CaptureTask).where(CaptureTask.shot_id == sid).with_for_update())
    usage = response.get('usage', {}) if isinstance(response, dict) else {}
    cost = usage.get('cost') if isinstance(usage, dict) else None
    known_cost = type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
    if known_cost:
        r.cost_usd += cost
    else:
        error = error or 'Provider did not report request cost; run paused to check spend'
    actual_model = response.get('model') if isinstance(response, dict) else None
    if not isinstance(actual_model, str) or len(actual_model) > 200:
        actual_model = config['model']
    safe_usage = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens')
                  if isinstance(usage, dict) and type(usage.get(k)) is int and 0 <= usage[k] <= 10**12}
    safe_usage['cost'] = cost if known_cost else None
    details = usage.get('prompt_tokens_details', {}) if isinstance(usage, dict) else {}
    if isinstance(details, dict):
        safe_usage.update({k: details[k] for k in ('cached_tokens', 'cache_write_tokens') if type(details.get(k)) is int and details[k] >= 0})
    s.add(CaptureEvent(run_id=rid, shot_id=sid, data={
        'model': actual_model,
        'usage': safe_usage,
        'decision': decision.model_dump() if decision else None, 'error': error, 'camera_valid': camera_valid}))
    valid = t and t.run_id == rid and t.token == req.token and t.status == 'leased' and t.lease_until > now() and t.steps == step
    if valid:
        t.decision = None
    if error:
        if r.status == 'running':
            r.status, r.error = 'paused', error
    elif valid and r.status == 'running':
        if decision.action == 'pass':
            decision.action = 'review'
        if decision.action in ('save', 'no_imagery', 'blocked') and decision.confidence < config['min_confidence']:
            decision.action, decision.reason = 'review', 'Confidence below capture threshold: ' + decision.reason[:500]
        if decision.action == 'save' and not camera_valid:
            decision.action, decision.reason = 'review', 'Camera URL validation failed: ' + (', '.join(flags) or 'missing or out-of-tolerance camera position/heading')
            # Repeated parser/camera failures are a run problem, not hundreds of independent bad photos.
            recent = s.scalars(select(CaptureEvent).where(CaptureEvent.run_id == rid)
                .order_by(CaptureEvent.id.desc()).limit(3)).all()
            if len(recent) == 3 and all(e.data.get('camera_valid') is False and
                (e.data.get('decision') or {}).get('action') == 'save' for e in recent):
                r.status, r.error = 'paused', 'Camera URL validation failed on three consecutive model-approved views. Run paused to check the camera format before collecting more backlog.'

        if decision.action == 'save' and decision.labels and isinstance(decision.labels.get('footpath'), dict) and decision.labels['footpath'].get('value') == 'unknown':
            decision.action, decision.reason = 'review', 'Target footpath is uncertain: ' + decision.reason[:500]
        t.decision = {'value': decision.model_dump(), 'sha256': hashlib.sha256(data).hexdigest(), 'page_url': req.page_url,
                      'model': actual_model}
        if decision.action == 'stop':
            r.status, r.error = 'paused', decision.reason
        if r.cost_usd >= config['max_cost_usd']:
            r.status, r.error = 'paused', 'Reported USD cost limit reached (last request may exceed it)'
    if r.status != 'running':
        revoke(s, rid)
    s.commit()
    if error or not valid or r.status != 'running':
        raise HTTPException(409, error or r.error or 'Reservation changed while model was running')
    return {'decision': decision.model_dump(), 'requests': r.requests, 'cost_usd': r.cost_usd}


@router.post('/worker/runs/{rid}/shots/{sid}/complete', dependencies=[Depends(worker_header)])
def complete(rid: uuid.UUID, sid: uuid.UUID, req: Completion, s: Session = Depends(get_db)):
    from app.main import CaptureRequest, _save_capture
    from app import results
    r, sh, t = locked_task(s, rid, sid, req)
    d = t.decision or {}
    if not d.get('value') or d.get('sha256') != hashlib.sha256(image_bytes(req.image)).hexdigest() or d.get('page_url') != req.page_url:
        raise HTTPException(409, 'Completion must match the last assessed image and camera URL')
    decision = CaptureDecision.model_validate(d['value'])
    if decision.action not in ('save', 'review', 'no_imagery', 'blocked'):
        raise HTTPException(409, 'This decision cannot complete a photo')
    # A review outcome leaves the shot pending and prevents automatic reclaims until explicit retry.
    if decision.action == 'review':
        job = s.get(Job, sh.job_id)
        evidence = get_settings().jobs_dir / job.slug / 'capture-review' / f'{sid}.jpg'
        evidence.parent.mkdir(parents=True, exist_ok=True)
        tmp = evidence.with_suffix('.part')
        with Image.open(io.BytesIO(image_bytes(req.image))) as im:
            im.convert('RGB').save(tmp, format='JPEG', quality=90)
        os.replace(tmp, evidence)
        review(r, t, decision.reason, sh)
        s.commit()
        return {'status': 'review'}
    t.status, t.updated_at = 'finished', now()
    r.finished += 1
    if decision.action == 'save':
        # Reuse the existing atomic file writing, thumbnails and position checks.
        out = _save_capture(CaptureRequest(shot_id=sid, image=req.image, page_url=req.page_url,
            image_date_text=decision.image_date), False, s, commit=False)
        sh.capture_source = 'automation'
        sh.result = {**sh.result, 'capture_run_id': str(rid), 'capture_model': d['model'], 'capture_decision': decision.model_dump(exclude={'labels'})}
        # The saved bytes are exactly the assessed screenshot; the checksum binds tags to this view.
        tag_task = s.scalar(select(TagTask).where(TagTask.shot_id == sid).with_for_update())
        if tag_task is None:
            tag_task = TagTask(shot_id=sid, model=d['model'], provider='openrouter',
                image_sha256=sh.result['sha256'], attempts=0)
            s.add(tag_task)
        tag_task.model, tag_task.provider, tag_task.image_sha256 = d['model'], 'openrouter', sh.result['sha256']
        tag_task.lease_token, tag_task.lease_until, tag_task.worker_id = None, None, None
        tag_task.attempts += 1
        s.flush()
        from app.tagging import store_labels, add_event, selected_model
        try:
            labels = LabelResult.model_validate(decision.labels).model_dump(mode='json')
        except ValidationError:
            # Keep a good photo even if annotation generation failed; a separate OpenRouter request can retry it.
            tag_task.status, tag_task.model = 'queued', selected_model()
            tag_task.error, tag_task.needs_review, tag_task.confidence = None, 0, None
            tag_task.updated_at = now()
            sh.labels = None
            add_event(s, tag_task, 'warning', 'Combined annotation invalid or missing; queued for OpenRouter tagging.')
        else:
            store_labels(s, sh, tag_task, labels, d['model'], provenance={'capture_run_id': str(rid), 'combined_capture': True})
        if 'manual_review' in sh.flags:
            t.status, t.error = 'review', sh.result.get('manual_review_reason', 'Annotation needs manual review')
        if results.SERIOUS & set(sh.flags):
            t.status, t.error = 'review', 'Saved image needs camera review: ' + ', '.join(sh.flags)
        s.commit()
        out['saved'].update(status=sh.status, flags=sh.flags)
        return {'status': sh.status, 'saved': out['saved']}
    sh.status = decision.action
    sh.result = {**(sh.result or {}), 'capture_run_id': str(rid), 'capture_model': d['model'], 'capture_decision': decision.model_dump(exclude={'labels'})}
    s.commit()
    return {'status': sh.status}


@router.get('/runs/{rid}/events')
def events(rid: uuid.UUID, s: Session = Depends(get_db)):
    return {'events': [{'id': e.id, 'shot_id': str(e.shot_id), 'data': e.data, 'created_at': e.created_at.isoformat()}
        for e in s.scalars(select(CaptureEvent).where(CaptureEvent.run_id == rid).order_by(CaptureEvent.id.desc()).limit(100))]}


@router.get('/review/{sid}/image')
def review_image(sid: uuid.UUID, s: Session = Depends(get_db)):
    sh = s.get(Shot, sid)
    if not sh:
        raise HTTPException(404, 'Shot not found')
    job = s.get(Job, sh.job_id)
    path = get_settings().jobs_dir / job.slug / 'capture-review' / f'{sid}.jpg'
    if not path.is_file():
        raise HTTPException(404, 'No review screenshot for this shot')
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})
