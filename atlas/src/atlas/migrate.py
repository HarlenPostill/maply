"""Apply migrations/*.sql in order, once each, refusing edited history."""
from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg

from atlas.config import MIGRATIONS


def migrations(directory: Path = MIGRATIONS) -> list[tuple[str, str, str]]:
    found = []
    for path in sorted(directory.glob('*.sql')):
        sql = path.read_text()
        found.append((path.stem, sql, hashlib.sha256(sql.encode()).hexdigest()))
    return found


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS) -> list[str]:
    conn.execute("""CREATE TABLE IF NOT EXISTS public.schema_migrations (
        version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())""")
    conn.commit()
    applied_now = []
    for version, sql, checksum in migrations(directory):
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('atlas.migrate'))")
            row = conn.execute('SELECT checksum FROM public.schema_migrations WHERE version = %s', (version,)).fetchone()
            if row:
                if row[0] != checksum:
                    raise SystemExit(f'Migration {version} was edited after it was applied. Add a new migration instead.')
                continue
            conn.execute(sql)
            conn.execute('INSERT INTO public.schema_migrations (version, checksum) VALUES (%s, %s)', (version, checksum))
        applied_now.append(version)
    return applied_now
