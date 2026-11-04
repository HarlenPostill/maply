"""Durable review transitions with real isolated PostgreSQL and a fake provider."""
import copy
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from app import review, review_worker, tagging
from app.config import get_settings
from app.db import configure_engine, session_scope
from app.models import Job, Shot, ReviewItem, ReviewBatch, TagTask


def proposal(value='absent'):
    return {'schema_version': 'maply-accessibility/1',
            'footpath': {'value': value, 'confidence': None if value == 'unknown' else 0.95,
                         'visibility': 'clear', 'evidence': 'The verge is fully visible.'},
            'features': [], 'notes': [], 'review_reasons': []}


@pytest.fixture(scope='module', autouse=True)
def isolated_database():
    url = os.environ['DATABASE_URL']
    assert make_url(url).database in ('maply_test', 'maply_review_test'), 'Never use the real dataset for tests'
    configure_engine(url)
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'],
                   cwd=Path(__file__).resolve().parents[1],
                   env={**os.environ, 'ALEMBIC_DATABASE_URL': url}, check=True, capture_output=True)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    settings = lambda: {'openrouter_model': 'fake/vision', 'api_key': 'test-key'}
    monkeypatch.setattr(review, 'load_settings', settings)
    monkeypatch.setattr(review_worker, 'load_settings', settings)
    # Any test that invokes the worker must explicitly replace this stub.
    def forbidden(*a, **k):
        raise AssertionError('Real provider calls are forbidden in review tests')
    monkeypatch.setattr(review_worker, 'annotate_openrouter', forbidden)
    with session_scope() as s:
        s.execute(delete(Job))
        job = Job(name='Review fixture', slug='review-fixture', streets={}, params={}, batch_size=50)
        s.add(job)
        s.flush()
        image = get_settings().jobs_dir / job.slug / 'images' / 'one.jpg'
        image.parent.mkdir(parents=True)
        image.write_bytes(b'isolated photo fixture')
        digest = tagging.digest(image)
        labels = proposal()
        labels['review_reasons'] = [review.ABSENCE_REASON]
        labels['provenance'] = {'image_sha256': digest}
        shot = Shot(job_id=job.id, code='S001-L1', ordinal=1, batch=1, segment_key='segment',
                    street='Fixture Street', kind='street', side='left', view_kind='footpath',
                    lat=-33.9, lng=151.2, geom='SRID=4326;POINT(151.2 -33.9)',
                    heading=90, pitch=0, fov=80, url='https://example.com', filename='one.jpg',
                    footpath='west', status='needs_check', flags=['manual_review'], labels=labels,
                    result={'file': 'images/one.jpg', 'sha256': digest,
                            'manual_review_reason': review.ABSENCE_REASON})
        s.add(shot)
        s.flush()
        return job.id, shot.id, image


def start(jid, limit=20, concurrency=1, auto_confirm=False):
    with session_scope() as s:
        return review.start_batch(jid, review.StartBatch(limit=limit, concurrency=concurrency, auto_confirm=auto_confirm), s)


def ready(jid, value='absent'):
    batch = start(jid)
    with session_scope() as s:
        task = review.claim(s)
    assert task
    with session_scope() as s:
        review.finish(s, task['id'], task['token'], proposal(value))
    return batch, task


def decide(iid, decision='confirm'):
    with session_scope() as s:
        return review.decide(iid, review.Decision(decision=decision), s)


def test_proposal_preserves_labels_then_human_confirmation_updates_db_and_json(seeded):
    jid, sid, _ = seeded
    with session_scope() as s:
        original = copy.deepcopy(s.get(Shot, sid).labels)
    batch, task = ready(jid)
    assert start(jid)['id'] == batch['id']
    with session_scope() as s:
        assert s.get(Shot, sid).labels == original
        assert review.summary(jid, s)['counts']['ready'] == 1
        row = review.items(jid, 'ready', 0, 30, s)['items'][0]
        assert row['review']['proposed_labels'] == proposal()
        assert row['image_url'].startswith('/files/review-fixture/images/one.jpg')
    assert decide(task['id']) == {'ok': True, 'status': 'confirmed'}
    assert decide(task['id']) == {'ok': True, 'status': 'confirmed'}
    with session_scope() as s:
        sh = s.get(Shot, sid)
        assert sh.status == 'accepted'
        assert sh.flags == []
        assert 'manual_review_reason' not in sh.result
        assert sh.labels['review_reasons'] == []
        assert sh.labels['provenance']['footpath_review_id'] == str(task['id'])
        files = list((get_settings().jobs_dir / 'review-fixture' / 'labels').glob('*.json'))
        assert len(files) == 1
        assert json.loads(files[0].read_text()) == sh.labels
        counts = review.summary(jid, s)['counts']
        assert counts['absence'] == counts['needs_check'] == 0
        assert counts['confirmed'] == 1


def test_absence_confirmation_keeps_unrelated_capture_flags_and_reasons(seeded):
    jid, sid, _ = seeded
    with session_scope() as s:
        sh = s.get(Shot, sid)
        sh.flags = ['manual_review', 'wrong_side']
        sh.result = {**sh.result, 'manual_review_reason': review.ABSENCE_REASON + '; Camera is on the wrong side'}
    _, task = ready(jid)
    decide(task['id'])
    with session_scope() as s:
        sh = s.get(Shot, sid)
        assert sh.status == 'needs_check'
        assert 'wrong_side' in sh.flags and 'manual_review' in sh.flags
        assert 'Camera is on the wrong side' in sh.labels['review_reasons']
        assert review.ABSENCE_REASON not in sh.labels['review_reasons']
        assert 'Camera is on the wrong side' in sh.result['manual_review_reason']
        assert review.summary(jid, s)['counts']['absence'] == 0


@pytest.mark.parametrize('change', ['labels', 'image'])
def test_confirmation_rejects_changed_source_and_releases_stale_proposal(seeded, change):
    jid, sid, image = seeded
    _, task = ready(jid)
    if change == 'image':
        image.write_bytes(b'changed image')
    else:
        with session_scope() as s:
            sh = s.get(Shot, sid)
            sh.labels = {**sh.labels, 'notes': ['Changed after request']}
    with pytest.raises(HTTPException) as error:
        decide(task['id'])
    assert error.value.status_code == 409
    with session_scope() as s:
        assert s.get(ReviewItem, task['id']).status == 'stale'
        assert s.get(Shot, sid).labels['footpath']['value'] == 'absent'


def test_unknown_cannot_be_confirmed_and_reject_does_not_certify(seeded):
    jid, sid, _ = seeded
    _, task = ready(jid, 'unknown')
    with pytest.raises(HTTPException) as error:
        decide(task['id'])
    assert error.value.status_code == 422
    assert decide(task['id'], 'reject')['status'] == 'rejected'
    with session_scope() as s:
        assert s.get(Shot, sid).status == 'needs_check'
        assert review.summary(jid, s)['counts']['confirmed'] == 0


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_ordinary_tagging_prevents_confirmation(seeded, status):
    jid, sid, _ = seeded
    _, task = ready(jid)
    with session_scope() as s:
        sh = s.get(Shot, sid)
        s.add(TagTask(shot_id=sid, status=status, image_sha256=sh.result['sha256'], model='fake/vision'))
    with pytest.raises(HTTPException) as error:
        decide(task['id'])
    assert error.value.status_code == 409
    with session_scope() as s:
        assert s.get(ReviewItem, task['id']).status == 'ready'
        assert s.get(Shot, sid).status == 'needs_check'


def test_pause_restart_cancel_are_durable_and_cancelled_response_is_ignored(seeded):
    jid, _, _ = seeded
    batch = start(jid)
    bid = uuid.UUID(batch['id'])
    with session_scope() as s:
        review.control(bid, 'pause', s)
        assert review.claim(s) is None
        review.control(bid, 'resume', s)
        task = review.claim(s)
        assert task
        review.pause_after_restart(s)
        assert s.get(ReviewBatch, bid).status == 'paused'
        assert s.get(ReviewItem, task['id']).status == 'failed'
        assert review.claim(s) is None
        review.control(bid, 'cancel', s)
        review.finish(s, task['id'], task['token'], proposal())
        assert s.get(ReviewItem, task['id']).status == 'failed'
    # An explicit new batch is required for an uncertain interrupted request.
    fresh = start(jid)
    with session_scope() as s:
        task = review.claim(s)
        review.control(uuid.UUID(fresh['id']), 'cancel', s)
        review.finish(s, task['id'], task['token'], proposal())
        assert s.get(ReviewItem, task['id']).status == 'cancelled'


def test_missing_photo_recovery_remains_visible_then_explicit_tag_proposal(seeded):
    jid, sid, image = seeded
    with session_scope() as s:
        sh = s.get(Shot, sid)
        sh.status, sh.labels = 'skipped', None
        sh.result = {'manual_review_reason': 'No usable imagery'}
    with pytest.raises(HTTPException) as error:
        start(jid)
    assert error.value.status_code == 409
    with session_scope() as s:
        review.recover_shot(sid, s)
        sh = s.get(Shot, sid)
        assert sh.status == 'retake'
        assert sh.result['review_recovery']['previous_status'] == 'skipped'
        assert sh.result['review_recovery']['reason'] == 'No usable imagery'
        assert review.summary(jid, s)['counts']['recovery'] == 1
        sh.status = 'captured'
        sh.result = {**sh.result, 'file': 'images/one.jpg', 'sha256': tagging.digest(image)}
    with session_scope() as s:
        assert review.summary(jid, s)['counts']['untagged'] == 1
        batch = review.tag_recovered(sid, s)
        assert batch['limit'] == 1
        task = review.claim(s)
        review.finish(s, task['id'], task['token'], proposal())
    decide(task['id'])
    with session_scope() as s:
        assert review.summary(jid, s)['counts']['untagged'] == 0
        assert s.get(Shot, sid).status == 'accepted'


def test_worker_uses_frozen_model_and_independent_prompt_with_stub_provider(seeded, monkeypatch):
    jid, sid, _ = seeded
    start(jid)
    calls = []
    def fake_provider(path, prompt, schema, model, **kwargs):
        calls.append((path, prompt, model))
        assert 'absence requires review before route exclusion' not in prompt
        assert 'The verge is fully visible.' not in prompt
        kwargs['on_event']({'data': {'usage': {'total_tokens': 123}}})
        return proposal()
    monkeypatch.setattr(review_worker, 'annotate_openrouter', fake_provider)
    monkeypatch.setattr(review_worker, 'load_settings', lambda: {'api_key': 'test', 'openrouter_model': 'changed/model'})
    assert review_worker.run_one()
    assert len(calls) == 1 and calls[0][2] == 'fake/vision'
    with session_scope() as s:
        item = s.scalar(select(ReviewItem).where(ReviewItem.shot_id == sid))
        assert item.status == 'ready' and item.usage == {'total_tokens': 123}
        assert 'footpath_review_id' not in s.get(Shot, sid).labels['provenance']
    assert review_worker.run_one() is False


def present_proposal():
    labels = proposal('present')
    labels['features'] = [{'id': 'path', 'kind': 'footpath_section',
        'region': [0.1, 0.2, 0.9, 0.8], 'location_description': 'A clear path along the verge',
        'tags': {key: {'value': value, 'confidence': 0.95, 'visibility': 'clear', 'evidence': 'Clearly visible'}
                 for key, value in [('presence', 'present'), ('surface', 'concrete'),
                                    ('path_type', 'dedicated_footpath'), ('continuity', 'continuous_visible_extent')]}}]
    return labels


def test_existing_unknown_can_be_explicitly_reviewed_into_full_present_annotation(seeded):
    jid, sid, _ = seeded
    with session_scope() as s:
        sh = s.get(Shot, sid)
        labels = proposal('unknown')
        labels['provenance'] = sh.labels['provenance']
        labels['review_reasons'] = ['footpath: unknown; Not sure from the initial review']
        sh.labels = labels
        sh.result = {**sh.result, 'manual_review_reason': labels['review_reasons'][0]}
    with session_scope() as s:
        assert review.summary(jid, s)['counts']['absence'] == 0
        review.tag_recovered(sid, s)
        task = review.claim(s)
        review.finish(s, task['id'], task['token'], present_proposal())
    decide(task['id'])
    with session_scope() as s:
        sh = s.get(Shot, sid)
        assert sh.status == 'accepted'
        assert sh.labels['footpath']['value'] == 'present'
        assert sh.labels['features'] == present_proposal()['features']
        assert sh.labels['review_reasons'] == []


def test_stale_proposal_can_be_requeued_explicitly(seeded):
    jid, sid, _ = seeded
    batch, task = ready(jid)
    with session_scope() as s:
        sh = s.get(Shot, sid)
        sh.labels = {**sh.labels, 'notes': ['New baseline']}
        assert review.claim(s) is None  # the first batch has completed
    with pytest.raises(HTTPException):
        decide(task['id'])
    fresh = start(jid)
    assert fresh['id'] != batch['id']
    with session_scope() as s:
        task = review.claim(s)
        assert task
        review.finish(s, task['id'], task['token'], proposal())
    decide(task['id'])
    with session_scope() as s:
        assert s.get(Shot, sid).status == 'accepted'


def test_missing_image_is_stale_and_never_sent_to_provider(seeded, monkeypatch):
    jid, sid, image = seeded
    start(jid)
    image.unlink()
    assert review_worker.run_one() is False
    with session_scope() as s:
        item = s.scalar(select(ReviewItem).where(ReviewItem.shot_id == sid))
        assert item.status == 'stale'
        assert 'Image missing' in item.error


def test_concurrent_starts_and_claims_respect_single_batch_and_capacity(seeded):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import inspect
    jid, sid, _ = seeded
    with session_scope() as s:
        original = s.get(Shot, sid)
        fields = {column.key: copy.deepcopy(getattr(original, column.key))
                  for column in inspect(Shot).column_attrs if column.key != 'id'}
        fields.update(code='S002-L1', ordinal=2)
        s.add(Shot(**fields))
    with ThreadPoolExecutor(max_workers=4) as pool:
        starts = list(pool.map(lambda _: start(jid, concurrency=1), range(4)))
    assert len({batch['id'] for batch in starts}) == 1
    def claim_one(_):
        with session_scope() as s:
            return review.claim(s)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(claim_one, range(4)))
    assert sum(task is not None for task in claims) == 1
    with session_scope() as s:
        counts = review.batch_dict(s, s.get(ReviewBatch, uuid.UUID(starts[0]['id'])))['counts']
        assert counts['running'] == counts['queued'] == 1


def test_http_contract_and_explicit_batch_bounds(seeded):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    jid, sid, _ = seeded
    app = FastAPI()
    app.include_router(review.router)
    with TestClient(app) as client:
        summary = client.get(f'/api/jobs/{jid}/review/summary').json()
        assert summary['counts']['absence'] == summary['counts']['absence_only'] == 1
        assert summary['provider'] == {'configured': True, 'model': 'fake/vision'}
        page = client.get(f'/api/jobs/{jid}/review/items?queue=absence').json()
        assert page['total'] == 1 and page['items'][0]['id'] == str(sid)
        assert page['items'][0]['review'] is None
        for body in ({'limit': 5001}, {'concurrency': 21}, {'limit': 0}, {'limit': 1.5}):
            assert client.post(f'/api/jobs/{jid}/review/batches', json=body).status_code == 422
        response = client.post(f'/api/jobs/{jid}/review/batches', json={})
        assert response.status_code == 200
        batch = response.json()
        assert batch['concurrency'] == 2 and batch['counts']['queued'] == 1
        assert batch['auto_confirm'] is True
        assert client.post(f"/api/review/batches/{batch['id']}/pause").json()['status'] == 'paused'
        assert client.post(f"/api/review/batches/{batch['id']}/cancel").json()['status'] == 'cancelled'


def test_twenty_reviews_can_be_claimed_but_twenty_first_waits(seeded):
    from sqlalchemy import inspect
    jid, sid, _ = seeded
    with session_scope() as s:
        original = s.get(Shot, sid)
        fields = {column.key: copy.deepcopy(getattr(original, column.key))
                  for column in inspect(Shot).column_attrs if column.key != 'id'}
        for ordinal in range(2, 22):
            s.add(Shot(**{**fields, 'code': f'S{ordinal:03}-L1', 'ordinal': ordinal}))
    start(jid, limit=21, concurrency=20)
    for _ in range(20):
        with session_scope() as s:
            assert review.claim(s) is not None
    with session_scope() as s:
        assert review.claim(s) is None


def test_executor_runs_twenty_requests_simultaneously(seeded, monkeypatch):
    import asyncio
    import threading
    started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    count = 0
    def blocking_request():
        nonlocal count
        with lock:
            count += 1
            if count == 20:
                started.set()
        assert release.wait(10)
        return True
    monkeypatch.setattr(review_worker, 'run_one', blocking_request)
    async def check():
        stop = asyncio.Event()
        worker = asyncio.create_task(review_worker.serve(stop))
        try:
            assert await asyncio.to_thread(started.wait, 5), 'All 20 requests must start before any completes'
        finally:
            stop.set()
            release.set()
            await asyncio.wait_for(worker, 5)
    asyncio.run(check())
    assert count == 20


@pytest.mark.parametrize('value', ['present', 'absent', 'unknown'])
def test_batch_automatically_saves_every_valid_result_without_human_confirmation(seeded, monkeypatch, value):
    jid, sid, _ = seeded
    start(jid, auto_confirm=True)
    labels = present_proposal() if value == 'present' else proposal(value)
    monkeypatch.setattr(review_worker, 'annotate_openrouter', lambda *a, **k: labels)
    assert review_worker.run_one()
    with session_scope() as s:
        item = s.scalar(select(ReviewItem).where(ReviewItem.shot_id == sid))
        sh = s.get(Shot, sid)
        assert item.status == 'confirmed' and item.decision == 'confirm'
        assert sh.labels['footpath']['value'] == value
        assert sh.labels['provenance']['review_confirmation'] == 'automatic'
        assert 'human_confirmed_at' not in sh.labels['provenance']
        saved = list((get_settings().jobs_dir / 'review-fixture' / 'labels').glob('*.json'))
        assert len(saved) == 1 and json.loads(saved[0].read_text()) == sh.labels
        assert sh.status == ('needs_check' if value == 'unknown' else 'accepted')
        assert review.summary(jid, s)['counts']['ready'] == 0


@pytest.mark.parametrize('outcome', ['changed', 'failed', 'cancelled'])
def test_auto_acceptance_keeps_invalid_work_unconfirmed(seeded, outcome):
    jid, sid, image = seeded
    batch = start(jid, auto_confirm=True)
    with session_scope() as s:
        task = review.claim(s)
    if outcome == 'changed':
        image.write_bytes(b'changed during inference')
    with session_scope() as s:
        before = copy.deepcopy(s.get(Shot, sid).labels)
        if outcome == 'cancelled':
            review.control(uuid.UUID(batch['id']), 'cancel', s)
        review.finish(s, task['id'], task['token'], proposal(), error='Provider failed' if outcome == 'failed' else None)
        assert s.get(ReviewItem, task['id']).status == ('stale' if outcome == 'changed' else outcome)
        assert s.get(Shot, sid).labels == before
