"""Public deployment: rate limits, client addresses, production config and the read-only login."""
from __future__ import annotations

import asyncio
import logging
import secrets

import psycopg
import pytest
from fastapi.testclient import TestClient

from atlas.routing import production
from atlas.routing.api import create_app
from atlas.routing.limits import READ, ROUTE, Limits, Rate, RateLimiter, client_address
from atlas.routing.service import Service
from routing_town import AS_OF, Town, designations, ll


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def small_limits(header='x-real-ip', **kw):
    return Limits(client={ROUTE: Rate(3, 1.0), READ: Rate(5, 1.0)},
                  total={ROUTE: Rate(10, 2.0), READ: Rate(100, 50.0)}, client_ip_header=header, **kw)


def service():
    svc = Service(lambda: Town().snapshot(), designations(), today=lambda: AS_OF)
    svc.refresh()
    return svc


def body():
    return {'origin': ll(130, 6), 'destination': ll(170, 6)}


def scope(headers=(), client=('10.0.0.1', 1234)):
    return {'type': 'http', 'headers': [(k.encode(), v.encode()) for k, v in headers], 'client': client}


# --- client address -------------------------------------------------------------------------

def test_client_address_trusts_only_the_configured_single_header():
    assert client_address(scope([('X-Real-IP', '203.0.113.7')]), 'x-real-ip') == '203.0.113.7'
    # X-Forwarded-For is never read, whatever it says.
    assert client_address(scope([('X-Forwarded-For', '1.2.3.4'), ('X-Real-IP', '203.0.113.7')]), 'x-real-ip') == '203.0.113.7'
    assert client_address(scope([('X-Forwarded-For', '1.2.3.4')]), 'x-real-ip') == 'untrusted'
    # Missing, repeated, list-valued or malformed: one shared bucket.
    assert client_address(scope(), 'x-real-ip') == 'untrusted'
    assert client_address(scope([('x-real-ip', '1.1.1.1'), ('x-real-ip', '2.2.2.2')]), 'x-real-ip') == 'untrusted'
    assert client_address(scope([('x-real-ip', '1.1.1.1, 2.2.2.2')]), 'x-real-ip') == 'untrusted'
    assert client_address(scope([('x-real-ip', 'evil')]), 'x-real-ip') == 'untrusted'
    # Without a verified header setting all clients share a bucket, including different proxy peers.
    assert client_address(scope([('x-real-ip', '203.0.113.7')]), None) == 'shared'
    assert client_address(scope([('x-real-ip', '1.1.1.1')], client=('10.9.8.7', 12)), None) == 'shared'
    # IPv6 by /64; IPv4-mapped as IPv4.
    a = client_address(scope([('x-real-ip', '2001:db8:1:2::1')]), 'x-real-ip')
    assert a == client_address(scope([('x-real-ip', '2001:db8:1:2:ffff::9')]), 'x-real-ip') == '2001:db8:1:2::/64'
    assert client_address(scope([('x-real-ip', '::ffff:203.0.113.7')]), 'x-real-ip') == '203.0.113.7'


# --- limiter --------------------------------------------------------------------------------

def test_per_client_bucket_refills_and_clients_are_independent():
    clock = Clock()
    lim = RateLimiter(small_limits(), clock)
    a, b = lim.client_key(scope([('x-real-ip', '1.1.1.1')])), lim.client_key(scope([('x-real-ip', '2.2.2.2')]))
    assert [lim.check(ROUTE, a) for _ in range(3)] == [0, 0, 0]
    wait = lim.check(ROUTE, a)
    assert 0 < wait <= 1.0
    assert lim.check(ROUTE, b) == 0          # another client is unaffected
    assert lim.check(READ, a) == 0           # and reads have their own bucket
    clock.t += 1.0
    assert lim.check(ROUTE, a) == 0 and lim.check(ROUTE, a) > 0


def test_global_cap_holds_against_rotating_addresses():
    clock = Clock()
    lim = RateLimiter(small_limits(), clock)
    allowed = sum(lim.check(ROUTE, lim.client_key(scope([('x-real-ip', f'198.51.100.{i}')]))) == 0 for i in range(200))
    assert allowed == 10                      # the global burst, whatever the header says
    clock.t += 1.0
    allowed = sum(lim.check(ROUTE, lim.client_key(scope([('x-real-ip', f'198.18.0.{i}')]))) == 0 for i in range(200))
    assert allowed == 2


def test_global_rejection_refunds_the_client():
    clock = Clock()
    lim = RateLimiter(Limits(client={ROUTE: Rate(2, 0.001), READ: Rate(2, 0.001)},
                             total={ROUTE: Rate(1, 0.001), READ: Rate(1, 0.001)}), clock)
    a, b = b'a' * 16, b'b' * 16
    assert lim.check(ROUTE, a) == 0
    assert lim.check(ROUTE, b) > 0            # global empty: b keeps its tokens
    lim._total[ROUTE].tokens = 1
    assert lim.check(ROUTE, b) == 0


def test_client_table_is_bounded():
    lim = RateLimiter(small_limits(max_clients=50), Clock())
    for i in range(5000):
        lim.check(READ, i.to_bytes(4, 'big'))
    assert lim.tracked_clients == 50


def test_keys_are_salted_hashes():
    s = scope([('x-real-ip', '203.0.113.7')])
    a, b = RateLimiter(small_limits()), RateLimiter(small_limits())
    assert a.client_key(s) != b.client_key(s) and b'203.0.113.7' not in a.client_key(s)


# --- HTTP -----------------------------------------------------------------------------------

def test_rate_limited_response_and_log(caplog):
    caplog.set_level(logging.INFO)
    client = TestClient(create_app(service(), limiter=RateLimiter(small_limits())))
    ip = {'X-Real-IP': '203.0.113.7'}
    codes = [client.post('/v1/route-preview', json=body(), headers=ip).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = client.post('/v1/route-preview', json=body(), headers=ip)
    assert r.status_code == 429 and r.headers['retry-after'] == '1' and r.headers['cache-control'] == 'no-store'
    assert r.json()['error']['code'] == 'rate_limited' and r.headers['x-request-id']
    # Spoofed X-Forwarded-For does not open a new bucket.
    assert client.post('/v1/route-preview', json=body(), headers={**ip, 'X-Forwarded-For': '9.9.9.9'}).status_code == 429
    assert client.post('/v1/route-preview', json=body(), headers={'X-Real-IP': '203.0.113.8'}).status_code == 200
    text = '\n'.join(rec.getMessage() for rec in caplog.records)
    assert 'rate_limited' in text and '203.0.113' not in text and '9.9.9.9' not in text


def test_coverage_is_built_once_per_graph():
    svc = service()
    client = TestClient(create_app(svc))
    first = client.get('/v1/coverage').json()
    assert client.get('/v1/coverage').json() == first
    svc._load = lambda: Town().observe('main2', 'left', 55, footpath='absent').snapshot()
    svc.refresh()                             # a new graph from changed data: a new body
    second = client.get('/v1/coverage').json()
    assert second != first and second['snapshot']['graph_version'] != first['snapshot']['graph_version']


def test_slow_body_times_out():
    app = create_app(service(), body_timeout_s=0.2)
    sent = []

    async def run():
        async def receive():
            await asyncio.sleep(5)
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message):
            sent.append(message)
        await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST',
                   'scheme': 'http', 'path': '/v1/route-preview', 'raw_path': b'/v1/route-preview', 'query_string': b'',
                   'root_path': '', 'headers': [(b'content-type', b'application/json'), (b'host', b'test')],
                   'client': ('127.0.0.1', 1), 'server': ('test', 80)}, receive, send)
    asyncio.run(asyncio.wait_for(run(), 3))
    assert sent[0]['status'] == 408


def test_no_openapi_or_docs():
    client = TestClient(create_app(service()))
    for path in ('/openapi.json', '/docs', '/redoc'):
        assert client.get(path).status_code == 404


# --- production config ----------------------------------------------------------------------

GOOD_URL = 'postgresql://atlas_api:pw@atlas-db.railway.internal:5432/atlas'


def test_config_reads_only_its_own_variables():
    with pytest.raises(production.ConfigError, match='ATLAS_API_DATABASE_URL'):
        production.load_config({'ATLAS_DATABASE_URL': 'postgresql://atlas:x@h/atlas', 'PORT': '8080'})
    with pytest.raises(production.ConfigError, match='PORT'):
        production.load_config({'ATLAS_API_DATABASE_URL': GOOD_URL})
    cfg = production.load_config({'ATLAS_API_DATABASE_URL': GOOD_URL, 'PORT': '8080', 'ATLAS_API_CLIENT_IP_HEADER': 'X-Real-IP'})
    assert (cfg.host, cfg.port, cfg.refresh_s, cfg.client_ip_header, cfg.max_connections) == ('0.0.0.0', 8080, 1800.0, 'x-real-ip', 64)


@pytest.mark.parametrize('env, match', [
    ({'ATLAS_API_DATABASE_URL': 'postgresql://atlas:x@h:5432/atlas'}, 'atlas'),
    ({'ATLAS_API_DATABASE_URL': 'postgresql://postgres:x@h/atlas'}, 'postgres'),
    ({'ATLAS_API_DATABASE_URL': 'postgresql://maply:maply@127.0.0.1:5433/maply'}, 'maply'),
    ({'ATLAS_API_DATABASE_URL': 'postgresql://h/atlas'}, 'name its login'),
    ({'ATLAS_API_DATABASE_URL': GOOD_URL, 'ATLAS_API_CLIENT_IP_HEADER': 'X-Forwarded-For'}, 'client-controlled'),
    ({'ATLAS_API_DATABASE_URL': GOOD_URL, 'PORT': '99999'}, 'PORT'),
    ({'ATLAS_API_DATABASE_URL': GOOD_URL, 'ATLAS_API_REFRESH_S': '5'}, 'REFRESH'),
])
def test_config_refuses_unsafe_settings(env, match):
    with pytest.raises(production.ConfigError, match=match):
        production.load_config({'PORT': '8080', **env})


def test_main_exits_on_missing_designations_without_logging_the_url(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    code = production.main({'ATLAS_API_DATABASE_URL': 'postgresql://atlas_api:s3cret@db.example:5432/atlas',
                            'PORT': '8080', 'ATLAS_DESIGNATIONS_FILE': str(tmp_path / 'none.json')})
    text = '\n'.join(rec.getMessage() for rec in caplog.records)
    assert code == production.EXIT_CONFIG and 'Designations' in text
    assert 's3cret' not in text and 'db.example' not in text


# --- read-only login (needs the disposable test database) -----------------------------------

from test_atlas import URL, db, grid  # noqa: E402,F401  (db is a fixture)
from atlas.publish import publish     # noqa: E402

ROLE = 'atlas_api_test'


@pytest.fixture
def api_role(db):  # noqa: F811
    publish(db, grid())
    password = secrets.token_urlsafe(32)
    production.provision_role(db, ROLE, password)
    production.provision_role(db, ROLE, password)        # idempotent
    yield production.role_url(URL, ROLE, password)
    with db.transaction():
        db.execute(f'DROP OWNED BY {ROLE}')
        db.execute(f'DROP ROLE {ROLE}')


def test_provisioned_role_is_safe_and_serves(api_role):
    with psycopg.connect(api_role, autocommit=True) as conn:
        assert production.credential_problems(conn) == []
        assert conn.execute("SELECT current_setting('default_transaction_read_only')").fetchone()[0] == 'on'
        # Even with the session default switched off, privileges stop every write.
        conn.execute('SET default_transaction_read_only = off')
        assert conn.execute("SELECT current_setting('default_transaction_read_only')").fetchone()[0] == 'off'
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO atlas.coverage_areas (name, geom) VALUES ('x', ST_GeomFromText('MULTIPOLYGON EMPTY', 4326))")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute('CREATE TABLE atlas.x (id int)')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute('CREATE TABLE public.x (id int)')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute('SELECT atlas.refresh_derived()')
    cfg = production.Config(api_role, '127.0.0.1', 1, None, 60, None, 8)
    svc = production.production_service(cfg)
    svc.refresh()
    assert svc.state.graph is not None and svc.state.graph.snapshot.release.id == 1


def test_owner_and_widened_roles_are_refused(api_role, db):  # noqa: F811
    with psycopg.connect(URL) as conn:      # the superuser owner
        assert {'superuser', 'database create'} <= set(production.credential_problems(conn))
    with db.transaction():
        db.execute(f'GRANT INSERT ON atlas.coverage_areas TO {ROLE}')
        db.execute(f'ALTER ROLE {ROLE} RESET default_transaction_read_only')
    with psycopg.connect(api_role) as conn:
        problems = production.credential_problems(conn)
    assert 'write or owner privilege on 1 atlas relations' in problems
    assert 'role lacks default_transaction_read_only=on' in problems
    svc = production.production_service(production.Config(api_role, '127.0.0.1', 1, None, 60, None, 8))
    svc.refresh()
    assert svc.state.graph is None and svc.state.error == 'misconfigured'
    # Re-provisioning restores least privilege.
    production.provision_role(db, ROLE, api_role.split('password=')[1].split()[0])
    with psycopg.connect(api_role) as conn:
        assert production.credential_problems(conn) == []
