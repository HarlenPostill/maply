"""App-managed OpenRouter tagging. Durable queue and concurrency limits stay in PostgreSQL."""
from __future__ import annotations
import asyncio
import logging
import threading
import uuid
from fastapi import HTTPException
from pydantic import ValidationError
from app import tagging
from app.config import get_settings
from app.db import session_scope
from app.models import Job, Shot, TagTask
from sqlalchemy import select, func
from app.openrouter_provider import annotate_openrouter
from app.provider_settings import load_settings
from app.tag_schema import LabelResult
from app.vision_common import VisionProviderError

log = logging.getLogger(__name__)


def execute(function, *args):
    with session_scope() as s:
        return function(*args, s=s)


def run_one(worker_id):
    if not load_settings().get('api_key'):
        return False
    req = tagging.WorkerRequest(worker_id=worker_id, model=load_settings()['openrouter_model'])
    task = execute(tagging.claim, req).get('task')
    if not task:
        return False
    tid = uuid.UUID(task['id'])
    auth = tagging.LeaseRequest(worker_id=worker_id, lease_token=task['lease_token'])
    done, lost = threading.Event(), threading.Event()

    def event(value):
        execute(tagging.task_event, tid, tagging.EventRequest(**auth.model_dump(), **value))

    def renew():
        while not done.wait(25):
            try:
                execute(tagging.task_heartbeat, tid, auth)
            except Exception:
                lost.set()
                return
    thread = threading.Thread(target=renew, daemon=True, name='openrouter-lease')
    thread.start()
    try:
        with session_scope() as s:
            sh = s.get(Shot, uuid.UUID(task['shot_id']))
            path = tagging.image_path(s.get(Job, sh.job_id), sh)
            if tagging.digest(path) != task['image_sha256']:
                raise VisionProviderError('Image changed before analysis')
        labels = annotate_openrouter(path, task['prompt'], task['schema'], task['model'],
            api_key=task['api_key'], timeout=120, on_event=event)
        # Unknown is valid. Never pressure the model to replace uncertain observations with guesses.
        result = LabelResult.model_validate(labels)
        if lost.is_set():
            raise VisionProviderError('Tagging reservation was revoked')
        execute(tagging.complete, tid, tagging.CompleteRequest(**auth.model_dump(), labels=result, model=task['model']))
    except (VisionProviderError, ValidationError, HTTPException, OSError) as exc:
        message = 'OpenRouter returned an invalid annotation; retry or review this photo.' if isinstance(exc, ValidationError) else (
            str(exc.detail) if isinstance(exc, HTTPException) else str(exc))
        try:
            execute(tagging.fail, tid, tagging.FailRequest(**auth.model_dump(), error=message[:1500]))
        except HTTPException:
            pass  # Cancelled/replaced task: stale work cannot overwrite its replacement.
    finally:
        done.set()
        thread.join(timeout=2)
    return True


def capacity(worker_id):
    execute(tagging.heartbeat, tagging.WorkerRequest(worker_id=worker_id, model=load_settings()['openrouter_model']))
    with session_scope() as s:
        return tagging.settings_dict(s)['parallel_sessions'], s.scalar(select(func.count()).select_from(TagTask).where(TagTask.status == 'queued'))


async def serve(stop):
    identity = 'openrouter-app-' + uuid.uuid4().hex[:12]
    tasks = set()
    try:
        while not stop.is_set():
            finished = {t for t in tasks if t.done()}
            for t in finished:
                try:
                    t.result()
                except Exception:
                    log.exception('OpenRouter tagging task failed; its lease will be recovered')
            tasks -= finished
            try:
                limit, queued = await asyncio.to_thread(capacity, identity)
                if load_settings().get('api_key'):
                    for _ in range(min(queued, max(0, limit - len(tasks)))):
                        tasks.add(asyncio.create_task(asyncio.to_thread(run_one, identity + '-' + uuid.uuid4().hex[:8])))
            except Exception:
                log.exception('Cannot refresh OpenRouter tagging queue')
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass
    finally:
        # Shutdown stops new claims and drains existing leased work.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
