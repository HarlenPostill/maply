"""Production entry point for the public route preview service (Railway `atlas-api`).

    python -m atlas.routing.production

Configuration comes from the process environment only. It never reads `atlas/.env` or
`ATLAS_DATABASE_URL`, which hold the owner credentials used by `publish`.

| Variable | Default | |
| --- | --- | --- |
| `ATLAS_API_DATABASE_URL` | required | A read-only login (see `api/provision.sh`). |
| `PORT` | required | Set by Railway. |
| `ATLAS_API_HOST` | `0.0.0.0` | |
| `ATLAS_DESIGNATIONS_FILE` | `/app/var/osm_way_designations.json` | Must exist. |
| `ATLAS_API_REFRESH_S` | `1800` | 60 to 86400. |
| `ATLAS_API_CLIENT_IP_HEADER` | none (shared bucket) | Set only after proxy overwrite behaviour is verified. |
| `ATLAS_API_MAX_CONNECTIONS` | `64` | Concurrent connections before uvicorn answers 503. |

Before any atlas data is read, every load checks that the login is safe to run with: not a
superuser, no role or database creation, no replication or RLS bypass, no create privilege on
any database or schema, no write, truncate or maintain privilege on any atlas relation, no
membership in a privileged built-in role, and `default_transaction_read_only = on` stored on
the role itself. A failed check refuses the snapshot (`misconfigured`) and stops the process.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from atlas.routing import designation
from atlas.routing.limits import Limits, RateLimiter
from atlas.routing.service import Service
from atlas.routing.snapshot import SnapshotError, connect_readonly, load_with_retry, read_marker

log = logging.getLogger('atlas.production')

DEFAULT_DESIGNATIONS = '/app/var/osm_way_designations.json'
# Logins the public service must never use: the database owner and the tagger's roles.
FORBIDDEN_USERS = frozenset({'atlas', 'postgres', 'maply', 'tagger'})
PRIVILEGED_BUILTINS = ('pg_write_all_data', 'pg_read_server_files', 'pg_write_server_files',
                       'pg_execute_server_program', 'pg_signal_backend', 'pg_checkpoint',
                       'pg_create_subscription', 'pg_maintain', 'pg_use_reserved_connections')
EXIT_CONFIG = 78   # EX_CONFIG


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    database_url: str
    host: str
    port: int
    designations: Path
    refresh_s: float
    client_ip_header: str | None
    max_connections: int


def _int(env: dict, name: str, default: int | None, lo: int, hi: int) -> int:
    raw = env.get(name, '').strip()
    if not raw:
        if default is None:
            raise ConfigError(f'{name} is required.')
        return default
    if not raw.isdigit() or not lo <= int(raw) <= hi:
        raise ConfigError(f'{name} must be an integer from {lo} to {hi}.')
    return int(raw)


def load_config(env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    url = env.get('ATLAS_API_DATABASE_URL', '').strip()
    if not url:
        raise ConfigError('ATLAS_API_DATABASE_URL is required (a read-only login; see api/provision.sh).')
    try:
        user = conninfo_to_dict(url).get('user')
    except psycopg.ProgrammingError:
        raise ConfigError('ATLAS_API_DATABASE_URL is not a valid connection string.') from None
    if not user:
        raise ConfigError('ATLAS_API_DATABASE_URL must name its login.')
    if user.lower() in FORBIDDEN_USERS:
        raise ConfigError(f'ATLAS_API_DATABASE_URL uses the {user!r} login. Use the read-only API login.')
    header = env.get('ATLAS_API_CLIENT_IP_HEADER', '').strip().lower() or None
    if header == 'x-forwarded-for':
        raise ConfigError('X-Forwarded-For is client-controlled. Use the single header the proxy sets.')
    return Config(
        database_url=url,
        host=env.get('ATLAS_API_HOST', '').strip() or '0.0.0.0',
        port=_int(env, 'PORT', None, 1, 65535),
        designations=Path(env.get('ATLAS_DESIGNATIONS_FILE', '').strip() or DEFAULT_DESIGNATIONS),
        refresh_s=float(_int(env, 'ATLAS_API_REFRESH_S', 1800, 60, 86400)),
        client_ip_header=header,
        max_connections=_int(env, 'ATLAS_API_MAX_CONNECTIONS', 64, 8, 1024),
    )


def credential_problems(conn: psycopg.Connection) -> list[str]:
    """Reasons this login is not safe for the public service. Empty means safe. Reads catalogs only."""
    problems = []
    row = conn.execute("""
        SELECT r.rolname, session_user = current_user, r.rolsuper, r.rolcreaterole, r.rolcreatedb,
               r.rolreplication, r.rolbypassrls,
               has_database_privilege(current_database(), 'CREATE'),
               has_table_privilege('atlas.releases', 'SELECT'),
               EXISTS (SELECT 1 FROM pg_db_role_setting s
                       WHERE s.setrole = r.oid AND s.setdatabase IN (0, (SELECT oid FROM pg_database WHERE datname = current_database()))
                         AND 'default_transaction_read_only=on' = ANY (s.setconfig)),
               current_setting('server_version_num')::int
        FROM pg_roles r WHERE r.rolname = current_user""").fetchone()
    (name, same_user, superuser, createrole, createdb, replication, bypassrls,
     db_create, can_read, role_read_only, version) = row
    if name.lower() in FORBIDDEN_USERS:
        problems.append(f'login {name!r} is reserved')
    if not same_user:
        problems.append('session user differs from current user')
    for flag, label in ((superuser, 'superuser'), (createrole, 'createrole'), (createdb, 'createdb'),
                        (replication, 'replication'), (bypassrls, 'bypassrls'), (db_create, 'database create')):
        if flag:
            problems.append(label)
    if not can_read:
        problems.append('cannot read atlas.releases')
    if not role_read_only:
        problems.append('role lacks default_transaction_read_only=on')
    writes = ['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'] + (['MAINTAIN'] if version >= 170000 else [])
    writable = conn.execute("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'atlas' AND c.relkind IN ('r', 'p', 'm', 'v', 'f', 'S')
          AND (pg_has_role(c.relowner, 'USAGE') OR """ +
        ' OR '.join(f"has_table_privilege(c.oid, '{p}')" for p in writes) + ')').fetchone()[0]
    if writable:
        problems.append(f'write or owner privilege on {writable} atlas relations')
    schemas = conn.execute("""
        SELECT array_agg(nspname ORDER BY nspname) FROM pg_namespace
        WHERE (has_schema_privilege(oid, 'CREATE') OR pg_has_role(nspowner, 'USAGE'))
          AND nspname !~ '^pg_(toast_)?temp_'""").fetchone()[0]
    if schemas:
        problems.append('create or owner privilege on schemas ' + ', '.join(schemas[:6]) + (' …' if len(schemas) > 6 else ''))
    builtins = conn.execute("""
        SELECT array_agg(rolname ORDER BY rolname) FROM pg_roles
        WHERE rolname = ANY(%s) AND pg_has_role(current_user, oid, 'USAGE')""", [list(PRIVILEGED_BUILTINS)]).fetchone()[0]
    if builtins:
        problems.append('member of ' + ', '.join(builtins))
    return problems


def require_safe_credentials(conn: psycopg.Connection) -> None:
    problems = credential_problems(conn)
    if problems:
        # Role facts only: never the connection string, host or password.
        log.error('refusing the database login: %s', '; '.join(problems))
        raise SnapshotError('misconfigured', 'The database login is not a read-only API login.')


def provision_role(conn: psycopg.Connection, role: str, password: str, connection_limit: int = 5) -> None:
    """Create or update the read-only API login. Idempotent; run as the database owner.

    The password is sent to the server only as a SCRAM verifier made on this machine.
    The role holds nothing directly: it reads through `atlas_read` (SELECT on the atlas
    schema), and stores read-only and timeout defaults on itself.
    """
    if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', role) or role in FORBIDDEN_USERS:
        raise ValueError(f'Refusing role name {role!r}.')
    if len(password) < 24:
        raise ValueError('Password too short.')
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError('Provision on an idle connection, so the role change commits on its own.')
    verifier = conn.pgconn.encrypt_password(password.encode(), role.encode(), b'scram-sha-256').decode()
    r = sql.Identifier(role)
    with conn.transaction():
        if conn.execute('SELECT 1 FROM pg_roles WHERE rolname = %s', [role]).fetchone() is None:
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN').format(r))
        conn.execute(sql.SQL('ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS '
                             'INHERIT CONNECTION LIMIT {} PASSWORD {}').format(r, sql.Literal(connection_limit), sql.Literal(verifier)))
        for name, value in (('default_transaction_read_only', 'on'), ('statement_timeout', '120s'),
                            ('idle_in_transaction_session_timeout', '60s')):
            conn.execute(sql.SQL('ALTER ROLE {} SET {} = {}').format(r, sql.Identifier(name), sql.Literal(value)))
        db = sql.Identifier(conn.execute('SELECT current_database()').fetchone()[0])
        # Drop any direct grants, so only atlas_read's SELECT remains.
        conn.execute(sql.SQL('REVOKE ALL ON ALL TABLES IN SCHEMA atlas FROM {}').format(r))
        conn.execute(sql.SQL('REVOKE ALL ON ALL SEQUENCES IN SCHEMA atlas FROM {}').format(r))
        conn.execute(sql.SQL('REVOKE ALL ON SCHEMA atlas FROM {}').format(r))
        conn.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM {}').format(db, r))
        conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(db, r))
        conn.execute(sql.SQL('GRANT atlas_read TO {}').format(r))


def role_url(owner_url: str, role: str, password: str) -> str:
    """The owner's connection string with the API login in place of the owner's."""
    params = conninfo_to_dict(owner_url)
    params.update(user=role, password=password)
    return make_conninfo(**params)


def production_service(cfg: Config) -> Service:
    url = cfg.database_url

    def marker():
        with connect_readonly(url, timeout_s=10) as conn:
            return read_marker(conn)
    return Service(lambda: load_with_retry(url, check=require_safe_credentials),
                   designation.FileSource(cfg.designations).current, marker=marker)


def main(env: dict | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s', stream=sys.stdout)
    try:
        cfg = load_config(env)
        if not cfg.designations.is_file() or designation.load(cfg.designations) is None:
            raise ConfigError(f'Designations file {cfg.designations} is missing or unreadable.')
    except ConfigError as exc:
        log.error('configuration: %s', exc)
        return EXIT_CONFIG

    import uvicorn
    from atlas.routing.api import create_app

    service = production_service(cfg)
    # Load before listening, so the platform health check and any woken request meet a ready
    # service. If the database stays unreachable, serve 503s and keep retrying in the background.
    service.refresh()
    if service.state.error == 'misconfigured':
        return EXIT_CONFIG
    service.run_background(cfg.refresh_s)
    limiter = RateLimiter(Limits(client_ip_header=cfg.client_ip_header))
    log.info('listening on %s:%d (client limits: %s)', cfg.host, cfg.port, cfg.client_ip_header or 'shared bucket')
    # Keep-alive outlasts the edge's 60 s idle timeout, so the proxy never reuses a closed connection.
    uvicorn.run(create_app(service, limiter=limiter), host=cfg.host, port=cfg.port,
                access_log=False, proxy_headers=False, server_header=False, log_level='info',
                limit_concurrency=cfg.max_connections, timeout_keep_alive=65, timeout_graceful_shutdown=10)
    return 0


if __name__ == '__main__':
    sys.exit(main())
