"""Run a visual capture worker on a dedicated macOS or Linux X11 Chrome desktop.

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
from app.capture_desktop import BlackLoadingFrame, DesktopError, MacChrome, LinuxChrome, TransientNavigationError
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
        self.progress_fd = os.environ.get('MAPLY_CAPTURE_PROGRESS_FD')
        if self.progress_fd is not None:
            self.progress_fd = int(self.progress_fd)
            os.set_inheritable(self.progress_fd, False)

    async def api(self, path, body=None):
        operation = path.rsplit('/', 1)[-1]
        labels = {'heartbeat': 'renewing the photo reservation', 'claim': 'claiming a photo',
                  'assess': 'requesting the model assessment', 'complete': 'saving the assessed photo',
                  'review': 'adding the photo to manual review', 'no-imagery': 'marking no imagery', 'pause': 'pausing the run'}
        description = labels.get(operation, 'reading the run status')
        # Only status reads and heartbeat renewals can be replayed after a lost response.
        # An assessment/claim/save may already have committed: never blindly repeat one.
        replay_safe = body is None or operation == 'heartbeat'
        failures = 0
        while True:
            try:
                response = await self.client.request('GET' if body is None else 'POST', self.server + '/api/capture/' + path,
                    json=body, headers={'X-Maply': 'capture-worker', 'Connection': 'close'})
            except httpx.TransportError as exc:
                if not replay_safe:
                    outcome = ' The request may have reached Maply; it has not been repeated.' if operation in ('assess', 'claim', 'complete', 'review', 'no-imagery') else ''
                    raise DesktopError(f'Maply connection failed while {description} ({type(exc).__name__}: {exc}).{outcome}') from None
                failures += 1
                retry_delay = min(30, 2 ** min(failures, 6))
                print(f'Maply connection interrupted while {description}; retrying in {retry_delay}s.', flush=True)
                await self.delay(retry_delay)
                continue
            if replay_safe and response.status_code >= 500:
                failures += 1
                retry_delay = min(30, 2 ** min(failures, 6))
                print(f'Maply returned HTTP {response.status_code} while {description}; retrying in {retry_delay}s.', flush=True)
                await self.delay(retry_delay)
                continue
            break
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

    async def open_shot(self, path, shot, auth):
        failures = 0
        while True:
            await self.api(path + '/heartbeat', auth)
            try:
                await asyncio.to_thread(self.desktop.open, shot['url'])
                return
            except TransientNavigationError as exc:
                failures += 1
                retry_delay = min(30, 2 ** min(failures, 5))
                print(f"{shot['code']}: {exc} Retrying in {retry_delay}s.", flush=True)
                await self.delay(retry_delay)

    async def load_observation(self, path, shot, auth, load_seconds):
        black_attempt = 0
        navigation_failures = 0
        while True:
            await self.api(path + '/heartbeat', auth)
            print(f"{shot['code']}: reading the camera URL and taking a screenshot…", flush=True)
            try:
                return await asyncio.to_thread(self.desktop.observe)
            except TransientNavigationError as exc:
                navigation_failures += 1
                black_attempt = 0
                retry_delay = min(30, 2 ** min(navigation_failures, 6))
                print(f"{shot['code']}: {exc} Retrying in {retry_delay}s.", flush=True)
                await self.delay(retry_delay)
            except BlackLoadingFrame as exc:
                if black_attempt == 2:
                    await self.api(path + '/heartbeat', auth)
                    print(f"{shot['code']}: still black after three loads; marking no imagery.", flush=True)
                    await self.api(path + '/no-imagery', {**auth, 'page_url': exc.page_url})
                    return None
                black_attempt += 1
                print(f"{shot['code']}: black loading frame; reopening Street View…", flush=True)
            await self.api(path + '/heartbeat', auth)
            await self.open_shot(path, shot, auth)
            print(f"{shot['code']}: waiting for Street View to load…", flush=True)
            await self.delay(load_seconds)

    async def yolo(self):
        try:
            state = await self.api(f'runs/{self.run_id}')
        except Exception:
            return False
        return state['status'] == 'running' and bool(state['config'].get('yolo'))

    async def run(self):
        started, failures = False, 0
        while not self.stopped.is_set():
            try:
                if not started:
                    await asyncio.to_thread(self.desktop.start)
                    started = True
                ended = await self.step()
                if self.progress_fd is not None:
                    os.write(self.progress_fd, b'.')
                if ended:
                    return
                failures = 0
            except Exception as exc:
                # YOLO runs keep capturing; an unfinished photo's lease expires and is retried later.
                if self.stopped.is_set() or not await self.yolo():
                    raise
                failures += 1
                await self.api(f'runs/{self.run_id}/pause', {'reason': str(exc)[:600], 'if_running': True})
                retry_delay = min(60, 2 ** min(failures, 6))
                print(f'YOLO mode: continuing after {type(exc).__name__}: {str(exc)[:300]} Retrying in {retry_delay}s.', flush=True)
                await self.delay(retry_delay)

    async def step(self):
        """Handle one run state check or photo. Returns True when the run has ended."""
        state = await self.api(f'runs/{self.run_id}')
        if state['status'] in ('completed', 'stopped'):
            print(f"Run {state['status']}", flush=True)
            return True
        if state['status'] == 'paused':
            message = state.get('error') or 'Paused in Maply'
            if message != self.last_pause_message:
                print(f'Run paused: {message} Resume in Maply to continue.', flush=True)
                self.last_pause_message = message
            await self.delay(3)
            return
        self.last_pause_message = None
        claimed = await self.api(f'worker/runs/{self.run_id}/claim', {'worker_id': self.worker_id})
        task = claimed['task']
        if not task:
            if claimed['status'] == 'completed':
                print('No unreserved photos remain; run completed.', flush=True)
                return True
            await self.delay(3)
            return
        shot = task['shot']
        auth = {'worker_id': self.worker_id, 'token': task['token']}
        path = f'worker/runs/{self.run_id}/shots/{shot["id"]}'
        print(f"Opening {shot['code']} · {shot['street']}", flush=True)
        try:
            await self.open_shot(path, shot, auth)
            print(f"{shot['code']}: waiting for Street View to load…", flush=True)
            await self.delay(task['config']['load_seconds'])
            for _ in range(task['config']['max_steps']):
                observation = await self.load_observation(path, shot, auth, task['config']['load_seconds'])
                if observation is None:
                    break
                image, page_url = observation
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
                return
            raise


async def pause_failed_run(worker, error):
    # The supervisor terminates workers after pause/stop/completion. Signals
    # acknowledge that state; they are not new run failures.
    if worker.stopped.is_set():
        return
    try:
        await worker.api(f'runs/{worker.run_id}/pause', {'reason': str(error)[:600], 'if_running': True})
    except Exception:
        pass


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
    desktop = LinuxChrome() if sys.platform == 'linux' else MacChrome()
    if args.check:
        permissions = desktop.permissions()
        print('Desktop permissions: ' + ', '.join(f'{name}={granted}' for name, granted in permissions.items()), flush=True)
        missing = [name for name, granted in permissions.items() if not granted]
        if missing:
            raise DesktopError('Enable ' + ', '.join(missing).replace('_', ' ') + ' for Terminal in System Settings → Privacy & Security, reopen Terminal, then run this check again.')
        desktop.activate()
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
        try:
            await worker.delay(5)
            await worker.run()
        except Exception as exc:
            if worker.stopped.is_set():
                print('Desktop worker stopped after pause, stop or service shutdown.', flush=True)
                return
            # A desktop/input/network failure pauses the run; it must not keep consuming model calls.
            await pause_failed_run(worker, exc)
            raise


if __name__ == '__main__':
    try:
        if 'MAPLY_CAPTURE_PROGRESS_FD' not in os.environ and '--check' not in sys.argv:
            from capture_watchdog import CYCLE_TIMEOUT_SECONDS, supervise
            print(f'Capture watchdog active: {CYCLE_TIMEOUT_SECONDS}s per photo or idle status check.', flush=True)
            raise SystemExit(supervise([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]))
        else:
            asyncio.run(main())
    except (DesktopError, httpx.HTTPError) as exc:
        raise SystemExit(f'Capture worker stopped: {exc}') from None
