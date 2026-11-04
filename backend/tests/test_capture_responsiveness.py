"""A waiting database operation must not freeze the API event loop."""
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import capture


def test_assessment_lock_wait_does_not_block_other_requests(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    async def blocked_assessment(*args):
        entered.set()
        assert release.wait(5), 'Independent request could not release the simulated database lock'
        return {'finished': True}

    monkeypatch.setattr(capture, 'assess', blocked_assessment)
    app = FastAPI()
    app.include_router(capture.router)
    app.dependency_overrides[capture.get_db] = lambda: object()

    @app.get('/release')
    async def release_lock():
        release.set()
        return {'released': True}

    with TestClient(app) as client, ThreadPoolExecutor(2) as pool:
        request = pool.submit(client.post,
            f'/api/capture/worker/runs/{uuid.uuid4()}/shots/{uuid.uuid4()}/assess',
            headers={'X-Maply': 'capture-worker'},
            json={'worker_id': 'worker', 'token': 'a' * 32, 'image': '', 'page_url': ''})
        try:
            assert entered.wait(2)
            response = pool.submit(client.get, '/release').result(timeout=2)
            assert response.status_code == 200
            assert request.result(timeout=2).json() == {'finished': True}
        finally:
            release.set()
