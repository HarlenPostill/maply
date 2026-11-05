"""Verify every database photo and thumbnail in a restored local Maply workspace."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    parser.add_argument('--expected-inventory', type=Path)
    args = parser.parse_args()
    project = args.project.resolve()

    def sql(query):
        return subprocess.run(['docker', 'compose', 'exec', '-T', 'db', 'psql',
                               '-U', 'maply', '-d', 'maply', '-At', '-v',
                               'ON_ERROR_STOP=1', '-c', query], cwd=project,
                              check=True, capture_output=True, text=True).stdout

    inventory = json.loads(sql("""SELECT json_build_object(
        'shots',(SELECT count(*) FROM shots),
        'images',(SELECT count(*) FROM shots WHERE result ? 'file'),
        'labels',(SELECT count(*) FROM shots WHERE labels IS NOT NULL),
        'capture_runs',(SELECT count(*) FROM capture_runs),
        'tag_tasks',(SELECT count(*) FROM tag_tasks))"""))
    if args.expected_inventory:
        expected = json.loads(args.expected_inventory.read_text())
        if inventory != expected:
            raise SystemExit(f'Database inventory mismatch: {inventory} != {expected}')

    rows = [json.loads(line) for line in sql("""SELECT json_build_object(
        'slug',j.slug,'code',s.code,'result',s.result,
        'provenance',s.labels->'provenance')
        FROM shots s JOIN jobs j ON s.job_id=j.id WHERE s.result ? 'file'
        ORDER BY j.slug,s.ordinal""").splitlines()]
    failures = []
    hashes = 0
    labels = 0
    data = (project / 'data' / 'jobs').resolve()
    for index, row in enumerate(rows):
        result = row['result']
        for key in ['file', 'thumb']:
            if not result.get(key):
                failures.append([row['code'], key, 'no path'])
                continue
            path = (data / row['slug'] / result[key]).resolve()
            if data not in path.parents or not path.is_file():
                failures.append([row['code'], key, 'missing'])
                continue
            if key == 'file':
                if not result.get('sha256'):
                    failures.append([row['code'], key, 'no database checksum'])
                    continue
                with path.open('rb') as source:
                    digest = hashlib.file_digest(source, 'sha256').hexdigest()
                if digest != result['sha256']:
                    failures.append([row['code'], key, 'checksum mismatch'])
                hashes += 1
        provenance = row.get('provenance')
        if provenance:
            label = data / row['slug'] / 'labels' / (
                f"{row['code']}_{provenance['task_id']}_{provenance['attempt']}.json")
            if not label.is_file():
                failures.append([row['code'], 'label', 'missing'])
            elif json.loads(label.read_text()).get('provenance') != provenance:
                failures.append([row['code'], 'label', 'provenance mismatch'])
            labels += 1
        if (index + 1) % 10000 == 0:
            print(f'Checked {index + 1}/{len(rows)} photo references', flush=True)
    receipt = {'verified_at': datetime.now(timezone.utc).isoformat(),
               'database_photos': len(rows), 'photo_sha256_checks': hashes,
               'thumbnail_references_checked': len(rows), 'failures': failures}
    receipt['label_files_checked'] = labels
    receipt['inventory'] = inventory
    (project / 'data/local-verification.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2), flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
