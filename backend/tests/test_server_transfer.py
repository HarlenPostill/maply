"""Transfer integrity and non-destructive restore rules, without touching a real database."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import pytest


def module(name):
    p = Path(__file__).parents[2] / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, p)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_export_includes_current_source_but_excludes_local_caches_and_env(tmp_path):
    transfer = module('server_transfer')
    for name in ['backend/app/main.py', 'backend/.venv/private', 'frontend/node_modules/cache', '.env', '.env.example', 'scripts/uncommitted.py']:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('content')
    files = {p.relative_to(tmp_path).as_posix() for p in transfer.source_files(tmp_path)}
    assert files == {'backend/app/main.py', '.env.example', 'scripts/uncommitted.py'}


@pytest.mark.parametrize('name,kind', [('../outside', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)])
def test_archive_restore_rejects_escaping_paths_and_links(tmp_path, name, kind):
    transfer = module('server_transfer')
    archive = tmp_path / 'unsafe.tar'
    with tarfile.open(archive, 'w') as output:
        entry = tarfile.TarInfo(name)
        entry.type = kind
        if kind == tarfile.REGTYPE:
            entry.size = 3
            output.addfile(entry, io.BytesIO(b'bad'))
        else:
            entry.linkname = '/tmp/outside'
            output.addfile(entry)
    dest = tmp_path / 'project'
    dest.mkdir()
    with pytest.raises(RuntimeError, match='Unsafe'):
        transfer.safe_extract(archive, dest)
    assert not list(dest.iterdir())


def test_restore_wont_overwrite_existing_workspace(tmp_path):
    transfer = module('server_transfer')
    target = tmp_path / 'existing'
    target.mkdir()
    (target / 'keep').write_text('original')
    with pytest.raises(RuntimeError, match='empty project directory'):
        transfer.restore_workspace(tmp_path / 'bundle', target)
    assert (target / 'keep').read_text() == 'original'


def test_data_hash_verification_detects_changes(tmp_path):
    transfer = module('server_transfer')
    p = tmp_path / 'data/jobs/photo.jpg'
    p.parent.mkdir(parents=True)
    p.write_bytes(b'original')
    manifest = {'data_files': [{'path': 'data/jobs/photo.jpg', 'bytes': 8, 'sha256': transfer.sha256(p)}]}
    transfer.verify_data(tmp_path, manifest)
    p.write_bytes(b'modified')
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        transfer.verify_data(tmp_path, manifest)


def test_ssh_client_binds_only_loopback_and_rejects_option_injection():
    client = module('connect_server')
    cfg = {'host': 'user@192.168.0.249', 'identity_file': '/private/key', 'local_port': 8000}
    args = client.ssh_arguments(cfg)
    assert args[-2:] == ['127.0.0.1:8000:127.0.0.1:8000', 'user@192.168.0.249']
    assert 'ExitOnForwardFailure=yes' in args
    with pytest.raises(ValueError):
        client.ssh_arguments({**cfg, 'host': '-oProxyCommand=anything'})


def test_client_workspace_cannot_export_stale_progress(tmp_path):
    transfer = module('server_transfer')
    (tmp_path / 'docker-compose.yml').write_text('name: maply')
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/client-server.json').write_text('{}')
    with pytest.raises(RuntimeError, match='workspace is a client'):
        transfer.export_workspace(tmp_path, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()
