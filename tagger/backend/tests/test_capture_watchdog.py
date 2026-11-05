"""Watchdog recovery against real processes, without Chrome, a database or model IO."""
import fcntl
import os
from pathlib import Path
import select
import signal
import subprocess
import sys

import pytest


WATCHDOG = Path(__file__).parents[2] / 'scripts/capture_watchdog.py'


def launch(source, tmp_path, timeout=.8):
    child = tmp_path / 'child.py'
    child.write_text(source)
    code = (f"import runpy,sys; m=runpy.run_path({str(WATCHDOG)!r}); "
            f"sys.exit(m['supervise']({[sys.executable, str(child)]!r}, timeout={timeout}, grace=.2))")
    return subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


def test_stuck_worker_is_force_killed_and_releases_desktop_lock(tmp_path):
    lock = tmp_path / 'desktop.lock'
    process = launch(f'''import fcntl,signal,time
lock = open({str(lock)!r}, 'w')
fcntl.flock(lock, fcntl.LOCK_EX)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('ready', flush=True)
while True:
    time.sleep(.1)
''', tmp_path)
    output, _ = process.communicate(timeout=5)
    assert process.returncode == 124, output
    assert 'ready' in output and 'watchdog' in output.lower()
    assert 'SIGKILL' in output
    with lock.open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_completed_cycles_extend_deadline(tmp_path):
    process = launch('''import os,time
fd = int(os.environ['MAPLY_CAPTURE_PROGRESS_FD'])
for _ in range(12):
    time.sleep(.15)
    os.write(fd, b'.')
print('completed', flush=True)
''', tmp_path)
    output, _ = process.communicate(timeout=5)
    assert process.returncode == 0, output
    assert 'completed' in output and 'resetting' not in output


def test_retry_logs_do_not_count_as_progress(tmp_path):
    process = launch('''import time
while True:
    print('retrying', flush=True)
    time.sleep(.05)
''', tmp_path)
    output, _ = process.communicate(timeout=5)
    assert process.returncode == 124, output
    assert 'retrying' in output


def test_shutdown_kills_unresponsive_worker_without_waiting_for_stall_deadline(tmp_path):
    process = launch('''import signal,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('ready', flush=True)
while True:
    time.sleep(.1)
''', tmp_path, timeout=60)
    try:
        assert select.select([process.stdout], [], [], 3)[0]
        assert process.stdout.readline().strip() == 'ready'
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=3)
        assert process.returncode == 0, output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_worker_failure_is_preserved(tmp_path):
    process = launch('raise SystemExit(7)', tmp_path)
    output, _ = process.communicate(timeout=3)
    assert process.returncode == 7, output


def test_closed_progress_pipe_cannot_disable_timeout(tmp_path):
    process = launch('''import os,time
os.close(int(os.environ['MAPLY_CAPTURE_PROGRESS_FD']))
while True:
    time.sleep(.1)
''', tmp_path)
    output, _ = process.communicate(timeout=5)
    assert process.returncode == 124, output


def test_blocked_desktop_thread_cannot_prevent_reset(tmp_path):
    worker_path = WATCHDOG.with_name('capture_worker.py')
    process = launch(f'''import asyncio,runpy,time,uuid
module = runpy.run_path({str(worker_path)!r})
class Desktop:
    def start(self):
        print('desktop blocked', flush=True)
        while True:
            time.sleep(.1)
async def run():
    worker = module['Worker']('http://127.0.0.1:8000', str(uuid.uuid4()), Desktop(), None)
    loop = asyncio.get_running_loop()
    import signal
    loop.add_signal_handler(signal.SIGTERM, worker.stopped.set)
    await worker.run()
asyncio.run(run())
''', tmp_path, timeout=1.5)
    output, _ = process.communicate(timeout=5)
    assert process.returncode == 124, output
    assert 'desktop blocked' in output and 'SIGKILL' in output


def test_reset_also_removes_helpers_holding_desktop_lock(tmp_path):
    lock = tmp_path / 'helper.lock'
    helper = (f"import fcntl,signal,time; lock=open({str(lock)!r},'w'); "
              "fcntl.flock(lock,fcntl.LOCK_EX); signal.signal(signal.SIGTERM,signal.SIG_IGN); "
              "print('helper ready',flush=True); time.sleep(60)")
    process = launch(f'''import subprocess,sys,time
subprocess.Popen([sys.executable, '-c', {helper!r}])
time.sleep(60)
''', tmp_path)
    output, _ = process.communicate(timeout=5)
    assert process.returncode == 124, output
    assert 'helper ready' in output
    with lock.open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.asyncio
async def test_worker_signals_progress_after_completion_but_not_failed_retry(monkeypatch):
    import importlib.util
    import uuid
    from types import SimpleNamespace
    path = WATCHDOG.with_name('capture_worker.py')
    spec = importlib.util.spec_from_file_location('watched_capture_worker', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    read_fd, write_fd = os.pipe()
    try:
        monkeypatch.setenv('MAPLY_CAPTURE_PROGRESS_FD', str(write_fd))
        worker = module.Worker('http://127.0.0.1:8000', str(uuid.uuid4()), SimpleNamespace(start=lambda: None), None)
        attempts = 0

        async def step():
            nonlocal attempts
            assert not select.select([read_fd], [], [], 0)[0]
            attempts += 1
            if attempts == 1:
                raise module.DesktopError('broken X11 connection')
            worker.stopped.set()

        async def api(*args):
            return {'status': 'running', 'config': {'yolo': True}}

        async def delay(*args):
            pass

        worker.step, worker.api, worker.delay = step, api, delay
        await worker.run()
        assert attempts == 2
        assert os.read(read_fd, 4096) == b'.'
    finally:
        os.close(read_fd)
        os.close(write_fd)
