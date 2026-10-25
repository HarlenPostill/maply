"""Capture orchestration with isolated PostGIS data and simulated model/desktop IO.

These tests never drive a browser, collect Street View imagery or spend API credits.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import uuid

import httpx
import pytest
from pydantic import ValidationError

from app import capture
from app.capture_schema import CaptureDecision
from app.capture_desktop import MacChrome, DesktopError, maps_url
from app.db import session_scope
from app.models import CaptureRun, CaptureTask
from tests.test_extension_api import client, GEORGE, _jpeg  # noqa: F401

H = {'X-Maply': 'capture-worker'}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(capture, 'load_settings', lambda: {'api_key': 'test-key'})


def new_run(client, **overrides):
    job = client.post('/api/jobs', json={'name': 'Capture test', 'streets': GEORGE, 'options': {'spacing_m': 30}}).json()
    r = client.post('/api/capture/runs', json={'job_id': job['id'], **overrides})
    assert r.status_code == 200, r.text
    return job, r.json()


def claim(client, run, worker='worker-1'):
    return client.post(f"/api/capture/worker/runs/{run['id']}/claim", headers=H, json={'worker_id': worker})


def payload(task, worker='worker-1'):
    sh = task['shot']
    url = f"https://www.google.com/maps/@{sh['lat']:.7f},{sh['lng']:.7f},3a,80y,{sh['heading']:.2f}h,82t/data=!3m4!1e1!3m2!1sPANO!2e0"
    return {'worker_id': worker, 'token': task['token'], 'image': _jpeg(), 'page_url': url}


def path(run, task, operation):
    return f"/api/capture/worker/runs/{run['id']}/shots/{task['shot']['id']}/{operation}"


def mock_model(monkeypatch, *, action='save', confidence=.98, cost=.002, invalid=False, callback=None, annotation=None):
    calls = []
    def respond(request):
        calls.append(json.loads(request.content))
        if callback:
            callback()
        decision = {'action': action, 'confidence': confidence, 'reason': 'Footpath and kerb are visible',
                    'start_x': .5, 'start_y': .5, 'end_x': .5, 'end_y': .5, 'image_date': 'Mar 2024', 'labels': annotation}
        return httpx.Response(200, json={'model': 'other/discounted-model', 'choices': [{'finish_reason': 'stop',
            'message': {'content': '{bad' if invalid else json.dumps(decision)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 40, **({'cost': cost} if cost is not None else {})}})
    original = httpx.AsyncClient
    monkeypatch.setattr(capture.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    return calls


def test_exclusive_claims_and_manual_interoperation(client):
    job, run = new_run(client)
    assert client.post(f"/api/capture/worker/runs/{run['id']}/claim", json={'worker_id': 'x'}).status_code == 403
    with ThreadPoolExecutor(2) as pool:
        a, b = list(pool.map(lambda name: claim(client, run, name).json()['task'], ['one', 'two']))
    assert a['shot']['id'] != b['shot']['id']
    nxt = client.get(f"/api/ext/next?job_id={job['id']}", headers={'X-Maply': 'extension'}).json()['shot']
    assert nxt['id'] not in [a['shot']['id'], b['shot']['id']]
    manual = client.post('/api/ext/capture', headers={'X-Maply': 'extension'}, json={'shot_id': a['shot']['id'], 'image': _jpeg(), 'page_url': a['shot']['url']})
    assert manual.status_code == 409
    assert client.patch(f"/api/shots/{a['shot']['id']}", json={'status': 'retake'}).status_code == 409


def test_capture_reuses_saving_records_model_and_rejects_replay(client, monkeypatch):
    _, run = new_run(client, fallback_models=['other/discounted-model'])
    task = claim(client, run).json()['task']
    body = payload(task)
    calls = mock_model(monkeypatch)
    assessment = client.post(path(run, task, 'assess'), headers=H, json=body)
    assert assessment.status_code == 200, assessment.text
    assert calls[0]['models'] == ['google/gemini-3.1-flash-lite', 'other/discounted-model']
    assert 'labels' in calls[0]['response_format']['json_schema']['schema']['properties']
    assert client.post(path(run, task, 'complete'), headers=H, json={**body, 'page_url': body['page_url'] + 'changed'}).status_code == 409
    response = client.post(path(run, task, 'complete'), headers=H, json=body)
    assert response.status_code == 200, response.text
    sh = client.get(f"/api/shots/{task['shot']['id']}").json()
    assert sh['status'] == 'captured'
    assert sh['capture_source'] == 'automation'
    assert sh['result']['capture_model'] == 'other/discounted-model'
    assert sh['result']['image_date_iso'] == '2024-03'
    assert client.post(path(run, task, 'complete'), headers=H, json=body).status_code == 409
    events = client.get(f"/api/capture/runs/{run['id']}/events").json()['events']
    assert events[0]['data']['model'] == 'other/discounted-model'
    assert events[0]['data']['usage']['cost'] == .002


def test_pause_revoke_resume_and_switch_model(client):
    _, run = new_run(client)
    task = claim(client, run).json()['task']
    assert client.post(f"/api/capture/runs/{run['id']}/pause", json={}).status_code == 200
    assert client.post(path(run, task, 'heartbeat'), headers=H, json=payload(task)).status_code == 409
    config = {**run['config'], 'model': 'different/vision'}
    assert client.put(f"/api/capture/runs/{run['id']}", json=config).status_code == 200
    assert client.post(f"/api/capture/runs/{run['id']}/resume", json={}).status_code == 200
    other = claim(client, run).json()['task']
    assert other['shot']['id'] == task['shot']['id'] and other['token'] != task['token']
    assert other['config']['model'] == 'different/vision'
    assert client.post(path(run, task, 'heartbeat'), headers=H, json=payload(task)).status_code == 409


@pytest.mark.parametrize('mode', ['cost', 'unknown', 'invalid', 'paused_during_request'])
def test_fail_closed_and_account_for_calls(client, monkeypatch, mode):
    _, run = new_run(client, max_cost_usd=.01)
    task = claim(client, run).json()['task']
    def pause():
        with session_scope() as s:
            s.get(CaptureRun, uuid.UUID(run['id'])).status = 'paused'
    mock_model(monkeypatch, cost=None if mode == 'unknown' else .02 if mode == 'cost' else .002,
               invalid=mode == 'invalid', callback=pause if mode == 'paused_during_request' else None)
    result = client.post(path(run, task, 'assess'), headers=H, json=payload(task))
    assert result.status_code == 409
    state = client.get(f"/api/capture/runs/{run['id']}").json()
    assert state['status'] == 'paused' and state['requests'] == 1
    assert client.post(path(run, task, 'complete'), headers=H, json=payload(task)).status_code == 409


def test_review_and_camera_validation(client, monkeypatch):
    _, run = new_run(client)
    task = claim(client, run).json()['task']
    mock_model(monkeypatch)
    body = {**payload(task), 'page_url': 'https://www.google.com/maps/@0.000000,0.000000,3a,80y,0h,82t'}
    result = client.post(path(run, task, 'assess'), headers=H, json=body)
    assert result.json()['decision']['action'] == 'review', result.text
    assert client.post(path(run, task, 'complete'), headers=H, json=body).json()['status'] == 'review'
    assert claim(client, run).json()['task']['shot']['id'] != task['shot']['id']
    assert client.post(f"/api/capture/review/{task['shot']['id']}/retry", json={}).status_code == 200
    assert claim(client, run).json()['task']['shot']['id'] == task['shot']['id']


def test_confidence_and_limits(client, monkeypatch):
    _, run = new_run(client, max_photos=1, max_requests=1)
    task = claim(client, run).json()['task']
    assert claim(client, run, 'worker-2').json()['task'] is None
    mock_model(monkeypatch, confidence=.6)
    assert client.post(path(run, task, 'assess'), headers=H, json=payload(task)).json()['decision']['action'] == 'review'
    assert client.post(path(run, task, 'complete'), headers=H, json=payload(task)).status_code == 200
    assert claim(client, run).status_code == 409
    assert client.get(f"/api/capture/runs/{run['id']}").json()['status'] == 'completed'


def test_expired_lease_can_be_reclaimed_but_stale_result_cannot_save(client):
    _, run = new_run(client)
    task = claim(client, run).json()['task']
    with session_scope() as s:
        s.get(CaptureTask, uuid.UUID(task['shot']['id'])).lease_until = capture.now() - timedelta(seconds=1)
    other = claim(client, run, 'worker-2').json()['task']
    assert other['shot']['id'] == task['shot']['id']
    assert client.post(path(run, task, 'complete'), headers=H, json=payload(task)).status_code == 409


def test_decision_only_allows_small_validated_actions():
    fields = dict(reason='adjust', confidence=.9, start_x=.5, start_y=.5, end_x=.6, end_y=.5, image_date=None)
    assert CaptureDecision(action='drag', **fields)
    for changes in ({'action': 'execute'}, {'action': 'drag', 'end_x': .9}, {'action': 'drag', 'end_y': .74}, {'action': 'save', 'command': 'anything'}):
        with pytest.raises(ValidationError):
            CaptureDecision.model_validate({'action': 'save', **fields, **changes})
    assert maps_url('https://www.google.com/maps/@-33,151,3a')
    assert not maps_url('https://www.google.com.evil.test/maps')
    assert not maps_url('https://evil.test/maps')


def test_native_drag_maps_fractional_image_to_window_and_checks_url():
    from types import SimpleNamespace
    actions = []
    desktop = object.__new__(MacChrome)
    desktop.gui = SimpleNamespace(moveTo=lambda *a, **kw: actions.append(a), dragTo=lambda *a, **kw: actions.append(a))
    desktop.url = lambda: 'expected'
    desktop.check = lambda: (100, 100, 1100, 900)
    decision = CaptureDecision(action='drag', reason='adjust', confidence=.9, start_x=.5, start_y=.5, end_x=.6, end_y=.5, image_date=None)
    desktop.drag(decision, 'expected')
    assert actions == [(600, 500), (700, 500)]
    with pytest.raises(DesktopError):
        desktop.drag(decision, 'changed')
    assert len(actions) == 2


@pytest.mark.asyncio
async def test_worker_observe_assess_drag_reinspect_and_save_without_browser():
    spec = importlib.util.spec_from_file_location('capture_worker', Path(__file__).parents[2] / 'scripts/capture_worker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    actions = []
    class Desktop:
        def start(self): actions.append('start')
        def open(self, url): actions.append('open')
        def observe(self): actions.append('observe'); return 'image', 'url'
        def drag(self, decision, url): actions.append('drag')
    task = {'shot': {'id': str(uuid.uuid4()), 'code': 'shot', 'street': 'street', 'url': 'url'},
            'token': 'x'*32, 'config': {'max_steps': 3, 'load_seconds': 0}}
    calls = []
    decisions = iter(['drag', 'save'])
    worker = module.Worker('http://localhost:8000', str(uuid.uuid4()), Desktop(), None)
    async def api(path, body=None):
        calls.append(path.split('/')[-1])
        if '/shots/' in path:
            if path.endswith('/assess'):
                return {'decision': dict(action=next(decisions), reason='okay', confidence=.99, start_x=.5,
                    start_y=.5, end_x=.6, end_y=.5, image_date=None), 'cost_usd': .004}
            if path.endswith('/complete'):
                worker.stopped.set()
            return {'ok': True}
        if path.endswith('claim'): return {'task': task}
        return {'status': 'running'}
    worker.api = api
    async def delay(seconds): pass
    worker.delay = delay
    await worker.run()
    assert actions == ['start', 'open', 'observe', 'drag', 'observe']
    assert calls.count('complete') == 1 and calls.count('assess') == 2


def test_cross_run_claims_do_not_duplicate_photos(client):
    job, first = new_run(client)
    second = client.post('/api/capture/runs', json={'job_id': job['id']}).json()
    with ThreadPoolExecutor(2) as pool:
        tasks = list(pool.map(lambda r: claim(client, r).json()['task'], [first, second]))
    assert tasks[0]['shot']['id'] != tasks[1]['shot']['id']


def test_request_limit_reserves_before_inference_and_allows_final_save(client, monkeypatch):
    _, run = new_run(client, max_requests=1)
    task = claim(client, run).json()['task']
    calls = mock_model(monkeypatch)
    assert client.post(path(run, task, 'assess'), headers=H, json=payload(task)).status_code == 200
    assert client.post(path(run, task, 'complete'), headers=H, json=payload(task)).status_code == 200
    assert claim(client, run).status_code == 409
    assert len(calls) == 1
    assert client.get(f"/api/capture/runs/{run['id']}").json()['status'] == 'paused'


def test_review_screenshot_is_separate_from_dataset(client, monkeypatch):
    job, run = new_run(client)
    task = claim(client, run).json()['task']
    mock_model(monkeypatch, action='review')
    assert client.post(path(run, task, 'assess'), headers=H, json=payload(task)).status_code == 200
    assert client.post(path(run, task, 'complete'), headers=H, json=payload(task)).status_code == 200
    image = client.get(f"/api/capture/review/{task['shot']['id']}/image")
    assert image.status_code == 200 and image.headers['content-type'] == 'image/jpeg'
    shot = client.get(f"/api/shots/{task['shot']['id']}").json()
    assert shot['status'] == 'skipped' and shot['image_url'] is None
    assert 'manual_review' in shot['flags']
    queue = client.get(f"/api/capture/jobs/{job['id']}").json()['tasks']
    assert queue[0]['has_evidence']


def test_model_step_limit_reviews_without_more_paid_requests(client, monkeypatch):
    _, run = new_run(client, max_steps=1)
    task = claim(client, run).json()['task']
    calls = mock_model(monkeypatch, action='wait')
    assert client.post(path(run, task, 'assess'), headers=H, json=payload(task)).status_code == 200
    assert client.post(path(run, task, 'assess'), headers=H, json=payload(task)).status_code == 409
    assert len(calls) == 1
    with session_scope() as s:
        assert s.get(CaptureTask, uuid.UUID(task['shot']['id'])).status == 'review'


def test_combined_capture_tags_exact_saved_image_without_queuing_another_call(client, monkeypatch):
    from tests.test_tagging_api import labels
    from app.models import TagTask
    job, run = new_run(client)
    task = claim(client, run).json()['task']
    body = payload(task)
    original = httpx.AsyncClient
    calls = []
    def respond(req):
        calls.append(json.loads(req.content))
        decision = dict(action='save', confidence=.99, reason='Clear near-side path', start_x=.5, start_y=.5,
                        end_x=.5, end_y=.5, image_date='Mar 2024', labels=labels())
        return httpx.Response(200, json={'model': 'test/combined', 'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(decision)}}],
            'usage': {'cost': .004, 'prompt_tokens_details': {'cached_tokens': 1200}}})
    monkeypatch.setattr(capture.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    assert client.post(path(run, task, 'assess'), headers=H, json=body).status_code == 200
    done = client.post(path(run, task, 'complete'), headers=H, json=body)
    assert done.status_code == 200, done.text
    saved = client.get(f"/api/shots/{task['shot']['id']}").json()
    assert saved['tagging']['status'] == 'completed'
    assert saved['labels']['provenance']['combined_capture'] is True
    assert saved['labels']['provenance']['model'] == 'test/combined'
    assert saved['labels']['provenance']['image_sha256'] == saved['result']['sha256']
    assert len(calls) == 1 and calls[0]['session_id'] == f"maply-capture-{run['id']}"
    events = client.get(f"/api/capture/runs/{run['id']}/events").json()['events']
    assert events[0]['data']['usage']['cached_tokens'] == 1200
    assert client.get(f"/api/jobs/{job['id']}/tagging").json()['counts']['queued'] == 0


def test_pass_enters_manual_backlog_and_extension_can_resolve_it(client, monkeypatch):
    job, run = new_run(client)
    task = claim(client, run).json()['task']
    calls = mock_model(monkeypatch, action='pass')
    body = payload(task)
    result = client.post(path(run, task, 'assess'), headers=H, json=body)
    assert result.json()['decision']['action'] == 'review'
    assert client.post(path(run, task, 'complete'), headers=H, json=body).json()['status'] == 'review'
    assert len(calls) == 1
    manual = {'X-Maply': 'extension'}
    backlog = client.get(f"/api/ext/next?job_id={job['id']}&backlog_only=true", headers=manual).json()
    assert backlog['shot']['id'] == task['shot']['id'] and backlog['progress']['manual_review'] == 1
    gallery = client.get(f"/api/jobs/{job['id']}/shots?manual_backlog=true").json()
    assert gallery['total'] == 1 and gallery['items'][0]['review_image_url']
    other = claim(client, run).json()['task']
    assert other['shot']['id'] != task['shot']['id']
    saved = client.post('/api/ext/capture?backlog_only=true', headers=manual, json={
        'shot_id': task['shot']['id'], 'image': body['image'], 'page_url': body['page_url'], 'image_date_text': 'Mar 2024'})
    assert saved.status_code == 200, saved.text
    assert saved.json()['shot'] is None
    assert saved.json()['progress']['manual_review'] == 0
    assert client.get(f"/api/jobs/{job['id']}/shots?manual_backlog=true").json()['total'] == 0


def test_invalid_combined_tags_preserve_capture_and_queue_openrouter_retry(client, monkeypatch):
    _, run = new_run(client)
    task = claim(client, run).json()['task']
    mock_model(monkeypatch)
    assert client.post(path(run, task, 'assess'), headers=H, json=payload(task)).status_code == 200
    assert client.post(path(run, task, 'complete'), headers=H, json=payload(task)).status_code == 200
    saved = client.get(f"/api/shots/{task['shot']['id']}").json()
    assert saved['image_url'] and saved['labels'] is None and saved['tagging']['status'] == 'queued'


def test_model_refusal_becomes_pass_instead_of_stopping_the_run(client, monkeypatch):
    _, run = new_run(client)
    task = claim(client, run).json()['task']
    original = httpx.AsyncClient
    monkeypatch.setattr(capture.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={'model': 'test/refusal', 'choices': [{'finish_reason': 'content_filter',
            'message': {'content': None, 'refusal': 'Unable to assess this view'}}], 'usage': {'cost': 0}})), **kw))
    result = client.post(path(run, task, 'assess'), headers=H, json=payload(task))
    assert result.status_code == 200 and result.json()['decision']['action'] == 'review'
    assert client.post(path(run, task, 'complete'), headers=H, json=payload(task)).json()['status'] == 'review'
    assert client.get(f"/api/capture/runs/{run['id']}").json()['status'] == 'running'


def worker_module():
    spec = importlib.util.spec_from_file_location('capture_worker_transport', Path(__file__).parents[2] / 'scripts/capture_worker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['status', 'heartbeat', 'claim', 'assess', 'complete'])
async def test_worker_only_retries_status_and_heartbeat_after_disconnect(operation):
    module = worker_module()
    requests = []
    def respond(req):
        requests.append(req)
        assert req.headers['connection'] == 'close'
        if len(requests) == 1:
            raise httpx.RemoteProtocolError('Server disconnected without sending a response.', request=req)
        return httpx.Response(200, json={'ok': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        worker = module.Worker('http://127.0.0.1:8000', str(uuid.uuid4()), None, client)
        path = 'runs/test' if operation == 'status' else f'worker/runs/test/{operation}'
        if operation in ('status', 'heartbeat'):
            assert await worker.api(path, None if operation == 'status' else {}) == {'ok': True}
            assert len(requests) == 2
        else:
            with pytest.raises(DesktopError, match='has not been repeated'):
                await worker.api(path, {})
            assert len(requests) == 1


@pytest.mark.asyncio
async def test_worker_fresh_connections_survive_idle_browser_work():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import time
    connections = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_GET(self):
            connections.append(self.client_address[1])
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    module = worker_module()
    try:
        async with httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0)) as client:
            worker = module.Worker(f'http://127.0.0.1:{server.server_port}', str(uuid.uuid4()), None, client)
            for _ in range(3):
                assert await worker.api('runs/test') == {}
                time.sleep(.05)  # Simulates native GUI work while the server has its own thread.
        assert len(set(connections)) == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_startup_failures_do_not_consume_visual_attempts_and_pause_reason_is_saved(client):
    _, run = new_run(client)
    for _ in range(4):
        task = claim(client, run).json()['task']
        result = client.post(f"/api/capture/runs/{run['id']}/pause", json={'reason': 'Connection lost before assessment'})
        assert result.json()['error'] == 'Connection lost before assessment'
        with session_scope() as s:
            t = s.get(CaptureTask, uuid.UUID(task['shot']['id']))
            assert t.attempts == 0 and t.status == 'released'
        assert client.post(f"/api/capture/runs/{run['id']}/resume", json={}).status_code == 200
    state = client.get(f"/api/capture/runs/{run['id']}").json()
    assert state['finished'] == 0 and state['requests'] == 0


@pytest.mark.parametrize('mode', ['15a', '16a'])
def test_observed_chrome_urls_save_with_combined_tags_and_the_visible_date(client, monkeypatch, mode):
    from tests.test_tagging_api import labels
    _, run = new_run(client)
    task = claim(client, run).json()['task']
    body = payload(task)
    body['page_url'] = body['page_url'].replace(',3a,', f',{mode},')
    calls = mock_model(monkeypatch, annotation=labels())
    result = client.post(path(run, task, 'assess'), headers=H, json=body)
    assert result.status_code == 200, result.text
    assert result.json()['decision']['action'] == 'save'
    context = json.loads(calls[0]['messages'][1]['content'][0]['text'])
    assert 'no_image_date' not in context['camera_flags']
    saved = client.post(path(run, task, 'complete'), headers=H, json=body)
    assert saved.status_code == 200, saved.text
    photo = client.get(f"/api/shots/{task['shot']['id']}").json()
    assert photo['status'] == 'captured' and photo['flags'] == []
    assert photo['result']['image_date_iso'] == '2024-03'
    assert photo['tagging']['status'] == 'completed'


def test_repeated_camera_rejections_pause_instead_of_filling_the_backlog(client, monkeypatch):
    job, run = new_run(client)
    mock_model(monkeypatch)
    for index in range(3):
        task = claim(client, run).json()['task']
        body = {**payload(task), 'page_url': 'https://www.google.com/maps/@0.00000,0.00000,16a,80y,0h,82t'}
        result = client.post(path(run, task, 'assess'), headers=H, json=body)
        if index < 2:
            assert result.status_code == 200
            assert client.post(path(run, task, 'complete'), headers=H, json=body).status_code == 200
        else:
            assert result.status_code == 409 and 'three consecutive' in result.json()['detail']
    state = client.get(f"/api/capture/runs/{run['id']}").json()
    assert state['status'] == 'paused' and state['requests'] == 3
    assert client.get(f"/api/jobs/{job['id']}/shots?manual_backlog=true").json()['total'] == 2


def test_native_url_copy_waits_for_clipboard_without_exposing_clipboard_contents(monkeypatch):
    from types import SimpleNamespace
    from app import capture_desktop
    desktop = object.__new__(MacChrome)
    desktop.check = lambda: (0, 0, 1000, 800)
    desktop.gui = SimpleNamespace(hotkey=lambda *args: None, press=lambda key: None)
    values = iter(['', 'https://www.google.com/maps/@-33.9,151.2,3a'])
    desktop.clipboard = SimpleNamespace(copy=lambda value: None, paste=lambda: next(values))
    monkeypatch.setattr(capture_desktop.time, 'sleep', lambda seconds: None)
    assert desktop.url().startswith('https://www.google.com/maps/')
    desktop.clipboard = SimpleNamespace(copy=lambda value: None, paste=lambda: 'PRIVATE CLIPBOARD TEXT')
    with pytest.raises(DesktopError) as error:
        desktop.url()
    assert 'Accessibility' in str(error.value) and 'PRIVATE' not in str(error.value)


@pytest.mark.asyncio
async def test_capture_preflight_reports_missing_permissions_before_any_browser_input(monkeypatch, tmp_path):
    import sys
    module = worker_module()
    class Desktop:
        def permissions(self): return {'accessibility': True, 'screen_recording': False}
        def script(self, code): raise AssertionError('Browser input must wait for permissions')
    monkeypatch.setattr(module, 'MacChrome', Desktop)
    monkeypatch.setattr(module.tempfile, 'gettempdir', lambda: str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['capture_worker.py', '--run', str(uuid.uuid4()), '--check'])
    with pytest.raises(DesktopError, match='screen recording'):
        await module.main()
