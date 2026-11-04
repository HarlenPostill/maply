"""Move a restored local Maply dataset into this checkout without replacing its DB.

The source data and database volume are retained. Existing checkout data is moved
to a private backup before the restored files replace it. macOS APFS clones keep
the original local snapshot independent without duplicating every photo block.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def compose(project, *args, **kwargs):
    return subprocess.run(['docker', 'compose', *args], cwd=project,
                          env=dict(os.environ, PWD=str(project)), check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    if source == ROOT or not (source / 'local-verification.json').is_file():
        raise SystemExit('Source must be a separate, verified local workspace.')
    marker = ROOT / 'data/local-server.json'
    if marker.exists() and json.loads(marker.read_text()).get('project') == str(ROOT):
        raise SystemExit('This checkout already owns the local dataset.')
    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = ROOT / 'data/backups' / ('local-development-cutover-' + stamp)
    backup.mkdir(parents=True)
    print(f'Cutover backup: {backup}', flush=True)
    compose(source, 'stop', 'app')
    try:
        with (backup / 'database.dump').open('wb') as output:
            compose(source, 'exec', '-T', 'db', 'pg_dump', '-U', 'maply', '-d',
                    'maply', '-Fc', '--no-owner', '--no-acl', stdout=output)
        staging = backup / 'restored-data'
        subprocess.run(['cp', '-cR', str(source / 'data'), str(staging)], check=True)
        previous = backup / 'previous-checkout-data'
        previous.mkdir()
        for entry in staging.iterdir():
            destination = ROOT / 'data' / entry.name
            if destination.exists():
                shutil.move(str(destination), previous / entry.name)
            shutil.move(str(entry), destination)
        staging.rmdir()
        for name in ['source-inventory.json', 'local-verification.json']:
            shutil.copy2(source / name, ROOT / 'data' / name)
        env = source.joinpath('.env').read_text().splitlines()
        env = [line for line in env if not line.startswith(('DB_PORT=', 'HOST_DATA_DIR='))]
        env += ['DB_PORT=5433', 'HOST_DATA_DIR=' + json.dumps(str(ROOT / 'data'))]
        if (ROOT / '.env').exists():
            shutil.copy2(ROOT / '.env', backup / 'previous.env')
        (ROOT / '.env').write_text('\n'.join(env) + '\n')
        os.chmod(ROOT / '.env', 0o600)
        marker.write_text(json.dumps({
            'role': 'local-development', 'project': str(ROOT),
            'url': 'http://127.0.0.1:8000', 'compose_project': 'maply-local-final',
            'database_volume': 'maply-local-final_pgdata', 'cutover_backup': str(backup),
            'snapshot': '/Users/harlenpostill/Maply Backups/cloud-final-20261104',
        }, indent=2) + '\n')
        compose(source, 'down')
        print('Local files adopted; database volume retained. Start Compose in this checkout.', flush=True)
    except BaseException:
        compose(source, 'up', '-d', 'app')
        raise


if __name__ == '__main__':
    main()
