"""uv run python -m atlas {migrate,publish,verify,status,serve,designations}

migrate       Apply pending schema migrations to ATLAS_DATABASE_URL.
publish       Extract the tagger dataset (read-only) and replace the survey layer.
verify        Compare the production database with a fresh tagger extract.
status        Print the production database summary.
serve         Run the read-only route preview API (see ROUTING_API.md).
designations  Copy per-way OSM street classes from the tagger street file (read-only) into var/.
api-role      Create or update the read-only login for the public API (see api/provision.sh).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

from atlas.config import COVERAGE_FILE, DESIGNATIONS_FILE, STREET_FILE, source_url, target_url
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
    p = sub.add_parser('serve')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--refresh-s', type=float, default=600, help='how often to check atlas content, designations and the date for changes')
    p.add_argument('--designations', type=Path, default=DESIGNATIONS_FILE)
    p = sub.add_parser('designations')
    p.add_argument('--street-file', type=Path, default=STREET_FILE)
    p.add_argument('--output', type=Path, default=DESIGNATIONS_FILE)
    p = sub.add_parser('api-role')
    p.add_argument('--role', default='atlas_api')
    p.add_argument('--password-file', type=Path, required=True, help='private file holding the password (never printed)')
    p.add_argument('--check-only', action='store_true', help='only log in as the role and check it')
    args = parser.parse_args(argv)

    if args.command == 'designations':
        from atlas.routing.designation import build
        print(json.dumps(build(args.street_file, args.output), indent=2))
        return 0
    if args.command == 'serve':
        import logging
        from atlas.routing.api import serve
        from atlas.routing.service import database_service
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
        if not args.designations.is_file():
            print(f'No designations file at {args.designations}: no street gets the pedestrian/shared allowance. '
                  'Run `python -m atlas designations` to create it.', file=sys.stderr)
        serve(database_service(target_url(), args.designations), args.host, args.port, args.refresh_s)
        return 0

    if args.command == 'api-role':
        from atlas.routing.production import credential_problems, provision_role, role_url
        password = args.password_file.read_text().strip()
        if not args.check_only:
            with connect_target() as conn:
                provision_role(conn, args.role, password)
        owner = target_url()
        tls = {} if 'sslmode=' in owner or '127.0.0.1' in owner or 'localhost' in owner else {'sslmode': 'require'}
        with psycopg.connect(role_url(owner, args.role, password), connect_timeout=15,
                             application_name='maply-atlas-api-role', **tls) as conn:
            problems = credential_problems(conn)
        print(json.dumps({'role': args.role, 'provisioned': not args.check_only, 'safe': not problems, 'problems': problems}))
        return 1 if problems else 0
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
