"""Prepare current Linux-capable source with a verified existing laptop snapshot.

Does not start a local app/database or alter the original transfer bundle.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import tarfile

from server_transfer import ROOT, load_verified, sha256, source_files


def prepare(bundle: Path, output: Path):
    bundle, output = bundle.resolve(), output.resolve()
    manifest = load_verified(bundle)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ('database.dump', 'data.tar'):
        # Hard links avoid duplicating gigabytes; no writes to these files follow.
        try:
            os.link(bundle / name, output / name)
        except OSError:
            shutil.copy2(bundle / name, output / name)
    with tarfile.open(output / 'source.tar.gz', 'w:gz') as archive:
        for path in source_files(ROOT):
            archive.add(path, arcname=path.relative_to(ROOT), recursive=False)
    manifest['archives']['source.tar.gz'] = {
        'bytes': (output / 'source.tar.gz').stat().st_size,
        'sha256': sha256(output / 'source.tar.gz'),
    }
    manifest['cloud_source_prepared_from'] = str(bundle)
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    shutil.copy2(ROOT / 'scripts/server_transfer.py', output / 'server_transfer.py')
    shutil.copy2(ROOT / 'scripts/cloud/bootstrap.sh', output / 'bootstrap.sh')
    for path in output.iterdir():
        os.chmod(path, 0o600)
    load_verified(output)
    print(json.dumps({'bundle': str(output), 'inventory': manifest['inventory']}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    prepare(args.bundle, args.output)
