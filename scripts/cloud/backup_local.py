"""Pull and checksum a private cloud backup without staging it on the full VPS."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--host', default='maply-cloud')
    args = parser.parse_args()
    os.umask(0o077)
    root = args.destination.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / 'verified.json'
    if receipt.exists():
        raise SystemExit('This backup is already verified; choose a new destination.')

    def ssh(command, **kwargs):
        return subprocess.run(['ssh', args.host, command], check=True, **kwargs)

    def pull(remote, local, verify=False):
        local.mkdir(exist_ok=True)
        command = ['rsync', '-rlt', '--partial', '--stats', '--rsync-path=sudo rsync']
        if verify:
            command += ['--checksum', '--dry-run', '--itemize-changes', '--delete']
        command += [f'{args.host}:{remote}/', str(local) + '/']
        if not verify:
            subprocess.run(command, check=True)
            return
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        (root / (local.name + '-verification.txt')).write_text(result.stdout)
        changes = [line for line in result.stdout.splitlines()
                   if line.startswith(('>', '<', 'c', '*deleting'))]
        if changes:
            raise RuntimeError(f'Checksum verification failed for {local}: {changes[:5]}')

    def initial_data_copy():
        local = root / 'data'
        local.mkdir(exist_ok=True)
        listing = ssh("sudo find /opt/maply/data -type f -printf '%P\\n'",
                      capture_output=True, text=True).stdout.splitlines()
        lists = []
        for index in range(4):
            path = root / f'copy-files-{index}.txt'
            path.write_text('\n'.join(listing[index::4]) + '\n')
            lists.append(path)

        def copy_group(path):
            subprocess.run(['rsync', '-rlt', '--partial', '--stats',
                            '--rsync-path=sudo rsync', '--files-from=' + str(path),
                            f'{args.host}:/opt/maply/data/', str(local) + '/'], check=True)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(copy_group, lists))

    print(f'Backup destination: {root}', flush=True)
    print('Copying cloud data while Maply remains available…', flush=True)
    initial_data_copy()
    print('Copying historical migration bundle…', flush=True)
    pull('/home/ubuntu/maply-transfer', root / 'migration-bundle')
    running = ssh('curl -fsS -H "X-Maply: capture-worker" '
                  'http://127.0.0.1:8000/api/capture/worker/runs',
                  capture_output=True, text=True)
    if json.loads(running.stdout)['runs']:
        raise RuntimeError('Capture has started; refusing to interrupt it for the final snapshot.')

    print('Briefly stopping app writes for the final consistent snapshot…', flush=True)
    try:
        ssh('sudo docker stop --time 150 maply-app-1')
        pull('/opt/maply/data', root / 'data')
        print('Streaming database backup to laptop…', flush=True)
        with (root / 'database.dump').open('wb') as output:
            ssh('sudo docker exec maply-db-1 pg_dump -U maply -d maply '
                '-Fc --no-owner --no-acl', stdout=output)
        with (root / 'database.dump').open('rb') as source:
            ssh('sudo docker exec -i maply-db-1 pg_restore --file=/dev/null',
                stdin=source, stdout=subprocess.DEVNULL)
        print('Archiving deployed source…', flush=True)
        entries = ['backend', 'frontend', 'scripts', 'config', 'assets', 'extension',
                   'docker-compose.yml', 'README.md']
        command = ('sudo tar -C /opt/maply --exclude=.venv --exclude=node_modules '
                   '--exclude=__pycache__ --exclude=.pytest_cache --exclude=*.egg-info '
                   '-czf - ' + ' '.join(shlex.quote(entry) for entry in entries))
        with (root / 'source.tar.gz').open('wb') as output:
            ssh(command, stdout=output)
        print('Verifying every cloud data file against the laptop copy…', flush=True)
        pull('/opt/maply/data', root / 'data', verify=True)
        pull('/home/ubuntu/maply-transfer', root / 'migration-bundle', verify=True)
        subprocess.run(['tar', '-tzf', str(root / 'source.tar.gz')],
                       check=True, stdout=subprocess.DEVNULL)
        receipt.write_text(json.dumps({
            'verified_at': datetime.now(timezone.utc).isoformat(),
            'host': args.host,
            'data_files': sum(1 for p in (root / 'data').rglob('*') if p.is_file()),
            'verification': 'rsync checksum dry run, full pg_restore decode without database writes, tar listing',
            'original_photos': 'retained on VPS',
        }, indent=2) + '\n')
    finally:
        print('Starting Maply app…', flush=True)
        ssh('sudo docker start maply-app-1')
    print(f'VERIFIED: {receipt}', flush=True)


if __name__ == '__main__':
    main()
