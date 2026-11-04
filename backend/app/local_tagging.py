"""SSH-only RPC for tagging checksum-verified backup images without restoring them.

Run with ``python -m app.local_tagging`` inside the cloud app container. The
authenticated laptop supplies image bytes to OpenRouter; cloud leases, schema
validation, concurrency and label persistence remain authoritative.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import timedelta

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import raiseload

from app import tagging
from app.db import session_scope
from app.models import Job, Shot, TagTask


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    shot_id: uuid.UUID
    image_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    worker_id: str = Field(min_length=1, max_length=100)


def reserve(s, req: ImageRequest):
    settings = tagging.settings_row(s, lock=True)
    if tagging.active_sessions(s) >= settings.parallel_sessions:
        return {'reason': 'capacity'}
    sh = s.scalar(select(Shot).options(raiseload(Shot.tag_task)).where(Shot.id == req.shot_id).with_for_update(of=Shot))
    if not sh:
        raise HTTPException(404, 'Photo not found')
    if sh.status not in tagging.ELIGIBLE or sh.result.get('sha256') != req.image_sha256:
        raise HTTPException(409, 'Photo changed or is no longer eligible')
    if tagging.labels_for(sh):
        return {'reason': 'completed'}
    task = s.scalar(select(TagTask).where(TagTask.shot_id == sh.id).with_for_update())
    if task and task.status == 'running' and task.lease_until and task.lease_until > tagging.now():
        return {'reason': 'running'}
    provider = tagging.provider_settings()
    if not provider.get('api_key'):
        raise HTTPException(422, 'OpenRouter API key is missing')
    if task is None:
        task = TagTask(shot_id=sh.id, attempts=0)
        s.add(task)
    task.status, task.provider, task.model = 'running', 'openrouter', provider['openrouter_model']
    task.image_sha256, task.worker_id = req.image_sha256, req.worker_id
    task.lease_token = uuid.uuid4().hex
    task.lease_until = tagging.now() + timedelta(seconds=tagging.LEASE_SECONDS)
    task.started_at = task.updated_at = tagging.now()
    task.attempts += 1
    task.error, task.confidence, task.needs_review = None, None, 0
    tagging.touch_worker(s, tagging.WorkerRequest(worker_id=req.worker_id, model=task.model))
    s.flush()
    tagging.add_event(s, task, 'stage', 'Verified local backup image reserved.', {'stage': 'claimed'})
    metadata = {'shot_id': str(sh.id), 'code': sh.code, 'street': sh.street, 'segment_key': sh.segment_key,
                'side': sh.side, 'footpath': sh.footpath, 'view_kind': sh.view_kind, 'kind': sh.kind,
                'planned_heading': sh.heading, 'camera_heading': sh.result.get('camera_heading'),
                'imagery_date': sh.result.get('image_date_iso'), 'quality_flags': sh.flags}
    return {'task': {'id': str(task.id), 'lease_token': task.lease_token, 'model': task.model,
                     'api_key': provider['api_key'], 'prompt': tagging.build_prompt(metadata),
                     'schema': tagging.annotation_schema()}}


def complete(s, tid, req, image_sha256):
    target = s.get(TagTask, tid)
    if not target:
        raise HTTPException(404, 'Task not found')
    sh = s.scalar(select(Shot).options(raiseload(Shot.tag_task)).where(Shot.id == target.shot_id).with_for_update(of=Shot))
    task = tagging.leased(s, tid, req)
    if sh.status not in tagging.ELIGIBLE or sh.result.get('sha256') != task.image_sha256 or image_sha256 != task.image_sha256:
        raise HTTPException(409, 'Photo changed during local analysis')
    if req.model != task.model:
        raise HTTPException(422, 'Result model does not match the reserved model')
    tagging.store_labels(s, sh, task, req.labels.model_dump(mode='json'), req.model,
                         provenance={'image_source': 'checksum_verified_local_backup'})
    return {'ok': True}


def dispatch(message):
    action = message['action']
    with session_scope() as s:
        if action == 'inventory':
            rows = s.execute(select(Shot.id, Job.slug, Shot.result['file'].astext, Shot.result['sha256'].astext)
                             .join(Job, Job.id == Shot.job_id).where(tagging.eligible(), Shot.labels.is_(None)))
            return [{'shot_id': str(sid), 'slug': slug, 'file': file, 'image_sha256': sha}
                    for sid, slug, file, sha in rows]
        if action == 'reserve':
            return reserve(s, ImageRequest.model_validate(message['request']))
        tid = uuid.UUID(message['task_id'])
        request = message['request']
        if action == 'complete':
            return complete(s, tid, tagging.CompleteRequest.model_validate(request), message['image_sha256'])
        if action == 'heartbeat':
            return tagging.task_heartbeat(tid, tagging.LeaseRequest.model_validate(request), s=s)
        if action == 'event':
            return tagging.task_event(tid, tagging.EventRequest.model_validate(request), s=s)
        if action == 'fail':
            return tagging.fail(tid, tagging.FailRequest.model_validate(request), s=s)
        raise ValueError('Unsupported action')


def main():
    for line in sys.stdin:
        try:
            result = {'ok': True, 'result': dispatch(json.loads(line))}
        except HTTPException as exc:
            result = {'ok': False, 'error': str(exc.detail)}
        except Exception:
            # Request payloads and provider keys must never appear in logs.
            result = {'ok': False, 'error': 'Local tagging RPC failed; inspect cloud state before retrying'}
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
