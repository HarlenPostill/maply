"""Remove cloud originals only after comparing them with a verified local backup."""
import argparse
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys


def archive_images(root, manifest):
    root = root.resolve()
    verified = []
    changed = []
    missing = 0
    for index, item in enumerate(manifest):
        relative = PurePosixPath(item['path'])
        if (relative.is_absolute() or '..' in relative.parts or len(relative.parts) < 4
                or relative.parts[0] != 'jobs' or relative.parts[2] != 'images'
                or relative.suffix.lower() not in ('.jpg', '.jpeg', '.png')):
            raise ValueError('Not an original image path: ' + str(relative))
        image_root = root / 'jobs' / relative.parts[1] / 'images'
        path = root / str(relative)
        if image_root.resolve() != image_root or path.is_symlink():
            raise ValueError('Refusing a symlink image path')
        if not path.exists():
            missing += 1
            continue
        if image_root not in path.resolve().parents:
            raise ValueError('Image path escaped its job folder')
        before = path.stat()
        if before.st_size != item['bytes']:
            changed.append(str(relative))
            continue
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = path.stat()
        fingerprint = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if digest != item['sha256'] or fingerprint(before) != fingerprint(after):
            changed.append(str(relative))
            continue
        verified.append((path, fingerprint(after), item['bytes']))
        if index % 5000 == 0:
            print(f'Checked {index + 1} backup originals', file=sys.stderr, flush=True)

    removed = 0
    removed_bytes = 0
    for path, previous, size in verified:
        current = path.stat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != previous:
            changed.append(str(path.relative_to(root)))
            continue
        path.unlink()
        removed += 1
        removed_bytes += size
    return {'removed': removed, 'bytes': removed_bytes, 'missing': missing,
            'changed_and_retained': changed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup', type=Path)
    parser.add_argument('--host', default='maply-cloud')
    args = parser.parse_args()
    os.umask(0o077)
    root = args.backup.expanduser().resolve()
    receipt = json.loads((root / 'verified.json').read_text())
    if receipt['host'] != args.host or receipt['data_files'] < 1:
        raise RuntimeError('Backup receipt does not identify this cloud host')
    manifest = []
    for job in sorted((root / 'data' / 'jobs').iterdir()):
        for path in sorted((job / 'images').rglob('*')):
            if path.is_file() and path.suffix.lower() in ('.jpg', '.jpeg', '.png'):
                if path.is_symlink():
                    raise ValueError('Local backup image is a symlink')
                with path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                manifest.append({'path': path.relative_to(root / 'data').as_posix(),
                                 'bytes': path.stat().st_size, 'sha256': digest})
    if not manifest:
        raise RuntimeError('Backup contains no original photos')
    manifest_path = root / 'image-archive-manifest.json'
    manifest_path.write_text(json.dumps(manifest))
    print(f'Locally hashed {len(manifest)} originals; verifying cloud copies before removal.', flush=True)
    remote = ('import hashlib,json,sys\nfrom pathlib import Path,PurePosixPath\n'
              + inspect.getsource(archive_images)
              + '\nprint(json.dumps(archive_images(Path("/opt/maply/data"),json.load(sys.stdin))))\n')
    with manifest_path.open('rb') as source, (root / 'image-archive-result.json').open('wb') as output:
        subprocess.run(['ssh', args.host, 'sudo python3 -c ' + shlex.quote(remote)],
                       stdin=source, stdout=output, check=True)
    result = json.loads((root / 'image-archive-result.json').read_text())
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    result['backup'] = str(root)
    (root / 'image-archive-result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    main()
