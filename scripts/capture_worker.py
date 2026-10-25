"""Run a model-independent visual capture worker against an unlocked macOS Chrome desktop.

    cd backend && uv run --extra capture python ../scripts/capture_worker.py --run RUN_ID
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
from urllib.parse import urlparse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import httpx
from app.capture_desktop import DesktopError, MacChrome
from app.capture_schema import CaptureDecision


class Worker:
    def __init__(self, server, run_id, desktop, client):
        parsed = urlparse(server)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.path not in ('', '/') or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Use a localhost Maply base URL; remote machines should use an SSH tunnel.')
        self.server, self.run_id, self.desktop, self.client = server.rstrip('/'), str(uuid.UUID(run_id)), desktop, client
        self.worker_id = f'{socket.gethostname()[:60]}-{uuid.uuid4().hex[:10]}'
        self.stopped = asyncio.Event()
        self.last_pause_message = None

    async def api(self, path, body=None):
        operation = path.rsplit('/', 1)[-1]
        labels = {'heartbeat': 'renewing the photo reservation', 'claim': 'claiming a photo',
                  'assess': 'requesting the model assessment', 'complete': 'saving the assessed photo',
                  'review': 'adding the photo to manual review', 'pause': 'pausing the run'}
        description = labels.get(operation, 'reading the run status')
        # Only status reads and heartbeat renewals can be replayed after a lost response.
        # An assessment/claim/save may already have committed: never blindly repeat one.
        attempts = 2 if body is None or operation == 'heartbeat' else 1
        for attempt in range(attempts):
            try:
                response = await self.client.request('GET' if body is None else 'POST', self.server + '/api/capture/' + path,
                    json=body, headers={'X-Maply': 'capture-worker', 'Connection': 'close'})
                break
            except httpx.TransportError as exc:
                if attempt + 1 < attempts:
                    print(f'Maply connection interrupted while {description}; reconnecting once.', flush=True)
                    await self.delay(.3)
                    continue
                outcome = ' The request may have reached Maply; it has not been repeated.' if operation in ('assess', 'claim', 'complete', 'review') else ''
                raise DesktopError(f'Maply connection failed while {description} ({type(exc).__name__}: {exc}).{outcome}') from None
        if response.status_code >= 400:
            try:
                detail = response.json()['detail']
            except (ValueError, KeyError):
                detail = f'HTTP {response.status_code}'
            raise DesktopError(f'Maply rejected the request while {description}: {str(detail)[:800]}')
        return response.json()

    async def delay(self, seconds):
        try:
            await asyncio.wait_for(self.stopped.wait(), timeout=seconds)
        except TimeoutError:
            pass
        if self.stopped.is_set():
            raise DesktopError('Worker interrupted')

    async def run(self):
        await asyncio.to_thread(self.desktop.start)
        while not self.stopped.is_set():
            state = await self.api(f'runs/{self.run_id}')
            if state['status'] in ('completed', 'stopped'):
                print(f"Run {state['status']}", flush=True)
                return
            if state['status'] == 'paused':
                message = state.get('error') or 'Paused in Maply'
                if message != self.last_pause_message:
                    print(f'Run paused: {message} Resume in Maply to continue.', flush=True)
                    self.last_pause_message = message
                await self.delay(3)
                continue
            self.last_pause_message = None
            claimed = await self.api(f'worker/runs/{self.run_id}/claim', {'worker_id': self.worker_id})
            task = claimed['task']
            if not task:
                if claimed['status'] == 'completed':
                    print('No unreserved photos remain; run completed.', flush=True)
                    return
                await self.delay(3)
                continue
            shot = task['shot']
            auth = {'worker_id': self.worker_id, 'token': task['token']}
            path = f'worker/runs/{self.run_id}/shots/{shot["id"]}'
            print(f"Opening {shot['code']} · {shot['street']}", flush=True)
            try:
                await asyncio.to_thread(self.desktop.open, shot['url'])
                print(f"{shot['code']}: waiting for Street View to load…", flush=True)
                await self.delay(task['config']['load_seconds'])
                for _ in range(task['config']['max_steps']):
                    await self.api(path + '/heartbeat', auth)
                    print(f"{shot['code']}: reading the camera URL and taking a screenshot…", flush=True)
                    image, page_url = await asyncio.to_thread(self.desktop.observe)
                    assessment = {**auth, 'image': image, 'page_url': page_url}
                    print(f"{shot['code']}: asking {task['config'].get('model', 'the capture model')} to assess the view…", flush=True)
                    result = await self.api(path + '/assess', assessment)
                    decision = CaptureDecision.model_validate(result['decision'])
                    print(f"{shot['code']}: {decision.action} · {decision.reason} · US${result['cost_usd']:.4f} total", flush=True)
                    # Check pause/stop/ownership immediately before any physical action or save.
                    await self.api(path + '/heartbeat', auth)
                    if self.stopped.is_set():
                        raise DesktopError('Worker interrupted')
                    if decision.action == 'wait':
                        await self.delay(2)
                    elif decision.action == 'drag':
                        await asyncio.to_thread(self.desktop.drag, decision, page_url)
                        await self.delay(2)
                    elif decision.action in ('save', 'review', 'no_imagery', 'blocked'):
                        await self.api(path + '/complete', assessment)
                        break
                    else:
                        raise DesktopError('Model requested a stop')
                else:
                    await self.api(path + '/review', {**auth, 'reason': 'Maximum visual adjustments reached'})
            except DesktopError as original:
                # UI pause/stop revokes the lease. Wait for resume and claim afresh.
                try:
                    state = await self.api(f'runs/{self.run_id}')
                except DesktopError:
                    raise original
                if state['status'] in ('paused', 'stopped', 'completed') and not self.stopped.is_set():
                    continue
                raise


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='http://127.0.0.1:8000')
    parser.add_argument('--run', required=True, help='Capture run UUID from the job page')
    parser.add_argument('--check', action='store_true', help='Check foreground Chrome and screenshot permission without API calls or mouse input')
    args = parser.parse_args()
    # One OS input stream per login session, even when workers target different runs.
    lock = open(Path(tempfile.gettempdir()) / f'maply-capture-{os.getuid()}.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another Maply capture worker already controls this desktop.')
    desktop = MacChrome()
    if args.check:
        permissions = desktop.permissions()
        print(f"macOS permissions: Accessibility={permissions['accessibility']}, Screen Recording={permissions['screen_recording']}", flush=True)
        missing = [name for name, granted in permissions.items() if not granted]
        if missing:
            raise DesktopError('Enable ' + ', '.join(missing).replace('_', ' ') + ' for Terminal in System Settings → Privacy & Security, reopen Terminal, then run this check again.')
        desktop.script('tell application "Google Chrome" to activate')
        desktop.bounds = desktop.geometry()
        desktop.observe()
        print('Chrome foreground, geometry, clipboard and screen capture checks passed. No model calls made.')
        return
    print('Starting in 5 seconds. Use this desktop only for capture; move the mouse to a screen corner to stop input.', flush=True)
    # Native browser work can outlast Uvicorn/Docker's idle keep-alive timeout.
    # Fresh local connections avoid racing a closed socket when input/screenshot work finishes.
    async with httpx.AsyncClient(timeout=100, follow_redirects=False,
                                 limits=httpx.Limits(max_keepalive_connections=0)) as client:
        worker = Worker(args.server, args.run, desktop, client)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, worker.stopped.set)
        await worker.delay(5)
        try:
            await worker.run()
        except Exception as exc:
            # A desktop/input/network failure pauses the run; it must not keep consuming model calls.
            try:
                await worker.api(f'runs/{worker.run_id}/pause', {'reason': str(exc)[:600]})
            except Exception:
                pass
            raise


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (DesktopError, httpx.HTTPError) as exc:
        raise SystemExit(f'Capture worker stopped: {exc}') from None
