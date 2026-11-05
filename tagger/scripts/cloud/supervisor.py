"""Coordinate local and remote desktops for runs created/resumed in Maply.

On service startup, pause existing running runs so a reboot/crash cannot silently
resume paid work. The user can resume through Maply. Worker failures also pause
their run and require an explicit resume, rather than retrying model calls.
YOLO runs ignore these conditional pauses, so they are restarted until a goal
or the user ends them.
"""
import json
import ipaddress
import os
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

BASE = 'http://127.0.0.1:8000/api/capture/'
WORKER = Path(__file__).resolve().parents[1] / 'capture_worker.py'
FLEET = Path(os.environ.get('MAPLY_CAPTURE_FLEET', '/etc/maply/capture-fleet.json'))


def api(path, body=None):
    request = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={'X-Maply': 'capture-worker', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def queue():
    return [str(uuid.UUID(run['id'])) for run in api('worker/runs')['runs']]


def pause(run_id, reason):
    api(f'runs/{run_id}/pause', {'reason': reason, 'if_running': True})


def desktops():
    """Reload the inventory so adding/removing a VM never restarts capture."""
    if not FLEET.exists():
        return ['local']
    addresses = json.loads(FLEET.read_text())
    if not isinstance(addresses, list) or len(addresses) > 10:
        raise ValueError('Capture fleet must contain at most ten desktops')
    hosts = []
    for value in addresses:
        if isinstance(value, dict):
            if set(value) != {'host', 'slot'} or type(value['slot']) is not int or value['slot'] not in (1, 2):
                raise ValueError('Desktop requires an IPv4 host and slot 1 or 2')
            address, slot = value['host'], value['slot']
        else:
            address, slot = value, 1
        address = str(ipaddress.IPv4Address(address))
        hosts.append(address if slot == 1 else f'{address}/2')
    if len({host.split('/')[0] for host in hosts}) > 5:
        raise ValueError('Capture fleet must contain at most five VM addresses')
    if len(set(hosts)) != len(hosts):
        raise ValueError('Each capture desktop must appear only once')
    return ['local', *hosts]


def launch(host, run_id):
    if host == 'local':
        return subprocess.Popen([sys.executable, str(WORKER), '--run', run_id])
    address, _, slot = host.partition('/')
    second = slot == '2'
    account, port = ('maply2', 18001) if second else ('maply', 18000)
    # The VM key forces remote_worker.py and permits only this loopback listener.
    # An open pipe ties the remote worker's lifetime to this SSH connection.
    return subprocess.Popen([
        'ssh', '-T', '-i', '/home/maply/.ssh/capture_ed25519',
        '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
        '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3',
        '-o', 'ExitOnForwardFailure=yes',
        '-R', f'127.0.0.1:{port}:127.0.0.1:8000',
        f'{account}@{address}', run_id,
    ], stdin=subprocess.PIPE)


def terminate(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=110)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    if getattr(child, 'stdin', None):
        child.stdin.close()


def main():
    children = {}
    retry_after = {}
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        for child, _ in children.values():
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    initialized = False
    try:
        while not stopping:
            try:
                hosts = desktops()
                pending = queue()
                if not initialized:
                    for existing in pending:
                        pause(existing, 'Cloud capture service restarted. Resume this run in Maply to continue.')
                    initialized = True
                    pending = []
                for host, (child, run_id) in list(children.items()):
                    result = child.poll()
                    if result is not None:
                        terminate(child)
                        del children[host]
                        if result:
                            retry_after[host] = time.monotonic() + 30
                            if api(f'runs/{run_id}')['status'] == 'running':
                                pause(run_id, f'Cloud browser worker on {host} stopped. Inspect the desktop, then resume in Maply.')
                        pending = queue()
                    elif run_id not in pending or host not in hosts:
                        terminate(child)
                        del children[host]
                for host in hosts:
                    if stopping:
                        break
                    if host not in children and pending and time.monotonic() >= retry_after.get(host, 0):
                        run_id = pending[0]
                        print(f'Starting capture run {run_id} on {host}', flush=True)
                        children[host] = (launch(host, run_id), run_id)
            except Exception as exc:
                print(f'Cloud capture waiting: {type(exc).__name__}: {exc}', flush=True)
            for _ in range(3):
                if stopping:
                    break
                time.sleep(1)
    finally:
        for child, _ in children.values():
            terminate(child)


if __name__ == '__main__':
    main()
