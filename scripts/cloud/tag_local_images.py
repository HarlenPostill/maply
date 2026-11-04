"""Tag a fixed set of local backup originals, saving validated labels to the cloud."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from app.openrouter_provider import annotate_openrouter
from app.tag_schema import LabelResult


class RPC:
    def __init__(self, host):
        self.lock = threading.Lock()
        self.process = subprocess.Popen([
            'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', host,
            'sudo docker compose --project-directory /opt/maply -f /opt/maply/docker-compose.yml '
            'exec -T app python -m app.local_tagging',
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

    def call(self, action, **data):
        with self.lock:
            self.process.stdin.write(json.dumps({'action': action, **data}) + '\n')
            self.process.stdin.flush()
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError('Cloud tagging connection closed')
            response = json.loads(line)
        if not response['ok']:
            raise RuntimeError(response['error'])
        return response['result']

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=30)


def verified_path(root, item):
    path = (root / 'data' / 'jobs' / item['slug'] / item['file']).resolve()
    jobs = (root / 'data' / 'jobs').resolve()
    if jobs not in path.parents or not path.is_file():
        raise ValueError('Backup original missing or outside backup folder')
    if file_hash(path) != item['image_sha256']:
        raise ValueError('Backup checksum does not match the current cloud photo')
    return path


def file_hash(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def run(args):
    os.umask(0o077)
    rpc = RPC(args.host)
    try:
        if args.prepare:
            items = rpc.call('inventory')
            for item in items:
                verified_path(args.backup, item)
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_text(json.dumps(items, indent=2) + '\n')
            print(json.dumps({'verified': len(items), 'manifest': str(args.manifest)}), flush=True)
            return
        items = json.loads(args.manifest.read_text())
        work = Queue()
        for item in items:
            work.put(item)
        counters = {'total': len(items), 'completed': 0, 'skipped': 0, 'failed': 0, 'active': 0}
        lock = threading.Lock()
        identity = 'local-backup-' + uuid.uuid4().hex[:10]
        status = args.manifest.with_suffix('.status.json')

        def update(**changes):
            with lock:
                for key, value in changes.items():
                    counters[key] += value
                snapshot = {**counters, 'updated_at': time.time(), 'pid': os.getpid()}
                temp = status.with_suffix('.tmp')
                temp.write_text(json.dumps(snapshot, indent=2) + '\n')
                temp.replace(status)
                print(json.dumps(snapshot), flush=True)

        def worker(slot):
            worker_id = f'{identity}-{slot:02d}'
            while True:
                try:
                    item = work.get_nowait()
                except Empty:
                    return
                task = None
                done = threading.Event()
                lost = threading.Event()
                heartbeat = None
                auth = None
                try:
                    path = verified_path(args.backup, item)
                    while True:
                        response = rpc.call('reserve', request={'shot_id': item['shot_id'],
                            'image_sha256': item['image_sha256'], 'worker_id': worker_id})
                        if response.get('reason') != 'capacity':
                            break
                        time.sleep(2)
                    task = response.get('task')
                    if not task:
                        update(skipped=1)
                        continue
                    auth = {'worker_id': worker_id, 'lease_token': task['lease_token']}
                    update(active=1)

                    def renew():
                        while not done.wait(25):
                            try:
                                rpc.call('heartbeat', task_id=task['id'], request=auth)
                            except Exception:
                                lost.set()
                                return

                    heartbeat = threading.Thread(target=renew, daemon=True)
                    heartbeat.start()

                    def event(value):
                        rpc.call('event', task_id=task['id'], request={**auth, **value})

                    labels = annotate_openrouter(path, task['prompt'], task['schema'], task['model'],
                        api_key=task['api_key'], timeout=120, on_event=event)
                    labels = LabelResult.model_validate(labels).model_dump(mode='json')
                    if lost.is_set() or file_hash(path) != item['image_sha256']:
                        raise ValueError('Local image changed or cloud lease was lost')
                    rpc.call('complete', task_id=task['id'], image_sha256=item['image_sha256'],
                             request={**auth, 'model': task['model'], 'labels': labels})
                    update(completed=1)
                except Exception as exc:
                    # Do not log arbitrary model, HTTP or RPC payloads containing credentials.
                    print(json.dumps({'shot_id': item['shot_id'], 'error_type': type(exc).__name__}), flush=True)
                    if task:
                        try:
                            rpc.call('fail', task_id=task['id'], request={**auth,
                                'error': 'Local backup analysis failed; inspect local tagging log and retry explicitly.'})
                        except Exception:
                            pass
                    update(failed=1)
                finally:
                    done.set()
                    if heartbeat:
                        heartbeat.join(timeout=30)
                    if task:
                        update(active=-1)
                    work.task_done()

        update()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(worker, range(args.workers)))
        if counters['failed']:
            raise SystemExit(1)
    finally:
        rpc.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--host', default='maply-cloud')
    parser.add_argument('--workers', type=int, choices=range(1, 21), default=20)
    parser.add_argument('--prepare', action='store_true')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
