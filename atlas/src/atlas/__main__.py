"""uv run python -m atlas {migrate,publish,verify,status}

migrate  Apply pending schema migrations to ATLAS_DATABASE_URL.
publish  Extract the tagger dataset (read-only) and replace the survey layer.
verify   Compare the production database with a fresh tagger extract.
status   Print the production database summary.
"""
from __future__ import annotations

import argparse
import json
import sys

import psycopg

from atlas.config import COVERAGE_FILE, source_url, target_url
from atlas.extract import extract
from atlas.migrate import migrate
from atlas.publish import publish
from atlas.verify import compare, summary


def connect_target() -> psycopg.Connection:
    url = target_url()
    kwargs = {} if 'sslmode=' in url or '127.0.0.1' in url or 'localhost' in url else {'sslmode': 'require'}
    return psycopg.connect(url, connect_timeout=15, application_name='maply-atlas', **kwargs)


def connect_source() -> psycopg.Connection:
    # The tagger database is only ever read.
    return psycopg.connect(source_url(), connect_timeout=10, application_name='maply-atlas-extract',
                           options='-c default_transaction_read_only=on')


def read_dataset():
    with connect_source() as source:
        return extract(source, COVERAGE_FILE)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='atlas', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('migrate')
    p = sub.add_parser('publish')
    p.add_argument('--force', action='store_true', help='publish even if the tagger data is unchanged')
    p.add_argument('--dry-run', action='store_true', help='extract and print stats without connecting to production')
    sub.add_parser('verify')
    sub.add_parser('status')
    args = parser.parse_args(argv)

    if args.command == 'publish' and args.dry_run:
        print(json.dumps(read_dataset().stats(), indent=2))
        return 0
    with connect_target() as conn:
        if args.command == 'migrate':
            applied = migrate(conn)
            print('Applied: ' + ', '.join(applied) if applied else 'Schema is up to date.')
        elif args.command == 'publish':
            migrate(conn)
            result = publish(conn, read_dataset(), force=args.force)
            print(json.dumps(result, indent=2, default=str))
        elif args.command == 'status':
            print(json.dumps(summary(conn), indent=2, default=str))
        elif args.command == 'verify':
            report = summary(conn)
            problems = compare(report, read_dataset())
            print(json.dumps({**report, 'problems': problems}, indent=2, default=str))
            return 1 if problems else 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
