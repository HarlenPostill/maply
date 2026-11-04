"""Forced SSH command: one run UUID, with worker lifetime tied to SSH stdin.

The coordinator keeps stdin open and reverse-forwards its private API to port
18000 (or 18001 for the isolated maply2 account). EOF means the coordinator disconnected: terminate the desktop worker.
"""
import os
import pwd
from pathlib import Path
import select
import signal
import subprocess
import sys
import uuid


def supervise(command, stream=sys.stdin):
    child = subprocess.Popen(command, start_new_session=True)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while child.poll() is None and not stopping:
            ready, _, _ = select.select([stream], [], [], 1)
            if ready and not os.read(stream.fileno(), 1024):
                break
        if child.poll() is not None:
            return child.returncode
        return 0
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=110)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


def main():
    run = str(uuid.UUID(os.environ['SSH_ORIGINAL_COMMAND']))
    account = pwd.getpwuid(os.getuid())
    if account.pw_name not in ('maply', 'maply2'):
        raise ValueError('Unsupported capture account')
    second = account.pw_name == 'maply2'
    os.environ.update(DISPLAY=':100' if second else ':99',
                      HOME=account.pw_dir, XAUTHORITY=f'{account.pw_dir}/.Xauthority')
    port = 18001 if second else 18000
    worker = Path(__file__).resolve().parents[1] / 'capture_worker.py'
    return supervise([sys.executable, str(worker), '--server', f'http://127.0.0.1:{port}', '--run', run])


if __name__ == '__main__':
    sys.exit(main())
