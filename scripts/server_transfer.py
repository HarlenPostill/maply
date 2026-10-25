"""Move a complete Maply workspace to a permanent server Mac.

Export stops the source app and leaves it stopped. Restore only accepts an empty
project directory and a database without Maply tables. Neither operation deletes
the source data. Python standard library only; Docker Compose runs PostgreSQL.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', 'dist', '.DS_Store'}
SOURCE_ROOTS = ['backend', 'frontend', 'extension', 'config', 'scripts', 'assets']
SOURCE_FILES = ['README.md', 'docker-compose.yml', '.env.example', '.dockerignore', '.gitignore']


def run(args, cwd, **kwargs):
    return subprocess.run(args, cwd=cwd, check=True, **kwargs)


def compose(project, *args, **kwargs):
    kwargs.setdefault('env', dict(os.environ, PWD=str(project)))
    return run(['docker', 'compose', *args], project, **kwargs)


def sql(project, query):
    return compose(project, 'exec', '-T', 'db', 'psql', '-U', 'maply', '-d', 'maply', '-At', '-v', 'ON_ERROR_STOP=1', '-c', query,
                   capture_output=True, text=True).stdout.strip()


def start_db(project):
    compose(project, 'up', '-d', 'db')
    for _ in range(60):
        result = subprocess.run(['docker', 'compose', 'exec', '-T', 'db', 'pg_isready', '-U', 'maply', '-d', 'maply'],
                                cwd=project, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError('PostgreSQL did not become ready within 60 seconds')


def inventory(project):
    return json.loads(sql(project, """SELECT json_build_object(
        'jobs', (SELECT json_agg(x ORDER BY x.slug) FROM (SELECT id::text,slug,name FROM jobs) x),
        'shots', (SELECT count(*) FROM shots),
        'images', (SELECT count(*) FROM shots WHERE result ? 'file'),
        'labels', (SELECT count(*) FROM shots WHERE labels IS NOT NULL),
        'shot_statuses', (SELECT json_object_agg(status,n) FROM (SELECT status,count(*) n FROM shots GROUP BY status) x),
        'tag_tasks', (SELECT count(*) FROM tag_tasks),
        'capture_runs', (SELECT count(*) FROM capture_runs))"""))


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def source_files(project):
    for name in SOURCE_FILES:
        p = project / name
        if p.is_file():
            yield p
    for name in SOURCE_ROOTS:
        for directory, dirs, files in os.walk(project / name):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.endswith('.egg-info') and not (Path(directory) / d).is_symlink())
            for filename in sorted(files):
                p = Path(directory) / filename
                if filename not in SKIP and not p.is_symlink() and not filename.startswith('.env'):
                    yield p


def export_workspace(project, destination):
    project, destination = project.resolve(), destination.resolve()
    if not (project / 'docker-compose.yml').is_file():
        raise RuntimeError('Not a Maply project directory')
    if (project / 'data/client-server.json').exists() and not (project / 'data/server-transfer-receipt.json').exists():
        raise RuntimeError('This workspace is a client. Export on the permanent server Mac so the backup contains current progress.')
    if destination.exists():
        raise RuntimeError('Transfer destination already exists; choose a new directory')
    destination.mkdir(parents=True, mode=0o700)
    os.chmod(destination, 0o700)
    # Freeze all image/annotation writes before taking the database and file snapshot.
    print('Stopping the source app for a consistent transfer. It will stay stopped.', flush=True)
    compose(project, 'stop', 'app')
    start_db(project)
    summary = inventory(project)
    dump = destination / 'database.dump'
    with dump.open('xb') as output:
        os.chmod(dump, 0o600)
        compose(project, 'exec', '-T', 'db', 'pg_dump', '-U', 'maply', '-d', 'maply', '-Fc', '--no-owner', '--no-acl', stdout=output)
    # Confirm the custom archive is readable before packing the rest of the workspace.
    with dump.open('rb') as source:
        compose(project, 'exec', '-T', 'db', 'pg_restore', '--list', stdin=source, stdout=subprocess.DEVNULL)
    print('Packing current source, including uncommitted changes…', flush=True)
    with tarfile.open(destination / 'source.tar.gz', 'w:gz', compresslevel=1) as archive:
        for p in source_files(project):
            archive.add(p, arcname=p.relative_to(project).as_posix(), recursive=False)
    data_manifest = []
    print('Packing and checksumming images, labels and preferences…', flush=True)
    with tarfile.open(destination / 'data.tar', 'w') as archive:
        for directory, dirs, files in os.walk(project / 'data'):
            dirs[:] = sorted(d for d in dirs if d not in {'transfers', 'backups', '__pycache__'} and not (Path(directory) / d).is_symlink())
            for filename in sorted(files):
                p = Path(directory) / filename
                if filename == '.DS_Store' or filename.endswith('.part'):
                    continue
                if p.is_symlink():
                    raise RuntimeError('Data symlinks must be replaced with real files before transfer: ' + str(p))
                relative = p.relative_to(project).as_posix()
                digest = sha256(p)
                archive.add(p, arcname=relative, recursive=False)
                data_manifest.append({'path': relative, 'bytes': p.stat().st_size, 'sha256': digest})
    manifest = {'format': 'maply-server-transfer/1', 'created_at': datetime.now(timezone.utc).isoformat(),
                'inventory': summary, 'data_files': data_manifest,
                'archives': {name: {'sha256': sha256(destination / name), 'bytes': (destination / name).stat().st_size}
                             for name in ['database.dump', 'source.tar.gz', 'data.tar']},
                'contains_saved_openrouter_key': (project / 'data/provider-settings.json').is_file()}
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    shutil.copy2(__file__, destination / 'server_transfer.py')
    for p in destination.iterdir():
        os.chmod(p, 0o600)
    print(json.dumps({'transfer': str(destination), 'files': len(data_manifest), 'inventory': summary}, indent=2), flush=True)
    return manifest


def load_verified(bundle):
    manifest = json.loads((bundle / 'manifest.json').read_text())
    if manifest.get('format') != 'maply-server-transfer/1':
        raise RuntimeError('Unsupported transfer format')
    for name in ['database.dump', 'source.tar.gz', 'data.tar']:
        info = manifest['archives'][name]
        p = bundle / name
        if p.stat().st_size != info['bytes'] or sha256(p) != info['sha256']:
            raise RuntimeError('Transfer checksum mismatch: ' + name)
    return manifest


def safe_extract(archive_path, destination):
    # No absolute paths, parent traversal, links or special-device entries.
    with tarfile.open(archive_path) as archive:
        for entry in archive:
            relative = PurePosixPath(entry.name)
            if relative.is_absolute() or '..' in relative.parts or not (entry.isfile() or entry.isdir()):
                raise RuntimeError('Unsafe archive entry: ' + entry.name)
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise RuntimeError('Archive entry escapes destination')
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(entry) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                os.chmod(target, entry.mode & 0o777 & ~0o022)


def verify_data(project, manifest):
    for item in manifest['data_files']:
        relative = PurePosixPath(item['path'])
        if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[0] != 'data':
            raise RuntimeError('Invalid data manifest path')
        p = project.joinpath(*relative.parts)
        if p.stat().st_size != item['bytes'] or sha256(p) != item['sha256']:
            raise RuntimeError('Restored data checksum mismatch: ' + item['path'])


def restore_workspace(bundle, project):
    bundle, project = bundle.resolve(), project.resolve()
    if project.exists() and any(project.iterdir()):
        raise RuntimeError('Restore requires an empty project directory; existing data is never overwritten')
    run(['docker', 'info'], bundle, stdout=subprocess.DEVNULL)
    print('Verifying all transfer archives…', flush=True)
    manifest = load_verified(bundle)
    project.mkdir(parents=True, exist_ok=True)
    safe_extract(bundle / 'source.tar.gz', project)
    safe_extract(bundle / 'data.tar', project)
    verify_data(project, manifest)
    # Disable paid background work until the restored database/files have been checked.
    (project / '.env').write_text('TAGGING_AUTOSTART=false\nHOST_DATA_DIR=' + json.dumps(str(project / 'data')) + '\n')
    os.chmod(project / '.env', 0o600)
    start_db(project)
    if sql(project, "SELECT to_regclass('public.jobs') IS NOT NULL") == 't':
        raise RuntimeError('This Docker server already has a Maply database. Restore refused; existing database left intact.')
    print('Restoring PostgreSQL…', flush=True)
    with (bundle / 'database.dump').open('rb') as source:
        compose(project, 'exec', '-T', 'db', 'pg_restore', '-U', 'maply', '-d', 'maply', '--no-owner', '--no-acl', '--exit-on-error', stdin=source)
    actual = inventory(project)
    if actual != manifest['inventory']:
        raise RuntimeError('Restored database inventory does not match the source snapshot')
    # In-flight work cannot be resumed with credentials/leases from another computer.
    # Completed progress and manual review decisions remain unchanged.
    sql(project, """BEGIN;
        UPDATE capture_runs SET status='paused', error='Moved to the server Mac; resume after starting its desktop worker.' WHERE status='running';
        UPDATE capture_tasks SET status='released', token=md5(random()::text), lease_until=now(), decision=NULL WHERE status='leased';
        UPDATE tag_tasks SET status='queued', lease_token=NULL, lease_until=NULL, worker_id=NULL WHERE status='running';
        DELETE FROM tag_workers;
        COMMIT;""")
    (project / 'data/server-transfer-receipt.json').write_text(json.dumps({
        'verified_at': datetime.now(timezone.utc).isoformat(), 'source_inventory': actual,
        'verified_data_files': len(manifest['data_files']), 'bundle': str(bundle)}, indent=2))
    print('Database inventory and every transferred data file verified.', flush=True)
    print('Starting Maply with background tagging still disabled for initial review.', flush=True)
    compose(project, 'up', '-d', '--build', 'app')
    print(json.dumps({'project': str(project), 'url': 'http://127.0.0.1:8000', 'inventory': actual,
                      'next': 'Run activate after checking the restored workspace.'}, indent=2), flush=True)


def activate(project):
    project = project.resolve()
    if not (project / 'data/server-transfer-receipt.json').is_file():
        raise RuntimeError('No verified restore receipt found')
    env = project / '.env'
    lines = env.read_text().splitlines()
    env.write_text('\n'.join(line for line in lines if not line.startswith('TAGGING_AUTOSTART=')) + '\nTAGGING_AUTOSTART=true\n')
    compose(project, 'up', '-d', 'app')
    print('OpenRouter tagging enabled. Desktop capture still starts only when you run its worker.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export')
    export.add_argument('--project', type=Path, default=ROOT)
    export.add_argument('--output', type=Path, required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--bundle', type=Path, required=True)
    restore.add_argument('--project', type=Path, required=True)
    enable = commands.add_parser('activate')
    enable.add_argument('--project', type=Path, default=ROOT)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'export':
        export_workspace(args.project, args.output)
    elif args.command == 'restore':
        restore_workspace(args.bundle, args.project)
    else:
        activate(args.project)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit('Transfer stopped: ' + str(exc))
