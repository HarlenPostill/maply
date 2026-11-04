"""Assemble the final cloud snapshot with originals previously archived on this Mac.

Never deletes source files or overwrites an existing verified snapshot. Run with
Python 3.11+ on the Mac; SSH and rsync must already reach maply-cloud.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess


def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--rsync', default='/opt/homebrew/bin/rsync')
    args = parser.parse_args()
    os.umask(0o077)
    root = args.destination.expanduser().resolve()
    archive = args.archive.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / 'verified.json').exists() or (root / 'snapshot.json').exists():
        raise SystemExit('Snapshot already complete; choose another destination.')
    if not (root / 'data').exists():
        print('Cloning existing archive without changing its files…', flush=True)
        run('cp', '-cR', str(archive / 'data'), str(root / 'data'))

    def ssh(command, **kwargs):
        return run('ssh', 'maply-cloud', command, **kwargs)

    def sync():
        run(args.rsync, '-rlt', '--partial', '--stats', '--timeout=120', '--rsync-path=sudo rsync',
            'maply-cloud:/opt/maply/data/', str(root / 'data') + '/')

    print('Pulling remaining cloud files; archived originals are retained…', flush=True)
    listing = ssh("sudo find /opt/maply/data -type f -printf '%P\\n'",
                  capture_output=True, text=True).stdout.splitlines()
    groups = []
    for slot in range(4):
        path = root / f'copy-files-{slot}.txt'
        path.write_text('\n'.join(listing[slot::4]) + '\n')
        groups.append(path)

    def copy_group(path):
        run(args.rsync, '-rlt', '--partial', '--stats', '--timeout=120',
            '--rsync-path=sudo rsync', '--files-from=' + str(path),
            'maply-cloud:/opt/maply/data/', str(root / 'data') + '/')

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(copy_group, groups))
    running = json.loads(ssh('curl -fsS -H "X-Maply: capture-worker" '
                             'http://127.0.0.1:8000/api/capture/worker/runs',
                             capture_output=True, text=True).stdout)
    if running['runs']:
        raise RuntimeError('Capture is active; refusing to stop the app.')
    try:
        print('Stopping cloud writes for a consistent final snapshot…', flush=True)
        ssh('sudo docker stop --time 150 maply-app-1')
        sync()
        with (root / 'database.dump').open('wb') as out:
            ssh('sudo docker exec maply-db-1 pg_dump -U maply -d maply '
                '-Fc --no-owner --no-acl', stdout=out)
        query = """SELECT json_build_object(
            'shots',(SELECT count(*) FROM shots),
            'images',(SELECT count(*) FROM shots WHERE result ? 'file'),
            'labels',(SELECT count(*) FROM shots WHERE labels IS NOT NULL),
            'capture_runs',(SELECT count(*) FROM capture_runs),
            'tag_tasks',(SELECT count(*) FROM tag_tasks))"""
        inventory = json.loads(ssh('sudo docker exec maply-db-1 psql -U maply -d maply -At -c '
                                   + shlex.quote(query), capture_output=True, text=True).stdout)
        (root / 'inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
        with (root / 'source.tar.gz').open('wb') as out:
            ssh('sudo tar -C /opt/maply --exclude=.venv --exclude=node_modules '
                '--exclude=__pycache__ --exclude=.pytest_cache --exclude=*.egg-info '
                '-czf - backend frontend scripts config assets extension '
                'docker-compose.yml README.md .env.example .dockerignore .gitignore', stdout=out)
        print('Comparing every cloud file by checksum…', flush=True)
        verification = run(args.rsync, '-rltcn', '--itemize-changes', '--timeout=120', '--rsync-path=sudo rsync',
                           'maply-cloud:/opt/maply/data/', str(root / 'data') + '/',
                           capture_output=True, text=True).stdout
        (root / 'cloud-checksums.txt').write_text(verification)
        if verification.strip():
            raise RuntimeError('Cloud checksum comparison found changes: ' + verification[:1000])
    finally:
        ssh('sudo docker start maply-app-1')

    print('Verifying archived originals by SHA256…', flush=True)
    archived = json.loads((archive / 'image-archive-manifest.json').read_text())
    for item in archived:
        path = root / 'data' / item['path']
        # A later retake may legitimately replace an archived original. Check the
        # archived bytes only when cloud sync has not superseded that version.
        original = archive / 'data' / item['path']
        if path.stat().st_mtime_ns != original.stat().st_mtime_ns:
            continue
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:
                raise RuntimeError('Archived checksum mismatch: ' + item['path'])
    run('tar', '-tzf', str(root / 'source.tar.gz'), stdout=subprocess.DEVNULL)
    receipt = {'snapshot_at': datetime.now(timezone.utc).isoformat(),
               'archive': str(archive), 'inventory': inventory,
               'archived_originals': len(archived),
               'cloud_files': 'rsync SHA checksum comparison passed',
               'database': 'logical dump; restore and photo-reference validation required'}
    (root / 'snapshot.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2), flush=True)


if __name__ == '__main__':
    main()
