"""Load one consistent, read-only snapshot of the atlas for routing.

Everything is read in a single REPEATABLE READ, READ ONLY transaction that ends in
a rollback, on a session that is itself default_transaction_read_only. The release
is checked against the tables before the snapshot is accepted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import psycopg

from atlas.extract import SCHEMA_VERSION

log = logging.getLogger('atlas.routing')


class SnapshotError(Exception):
    """The atlas could not be read (reason: database_unreachable or integrity_failed)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass
class Release:
    id: int
    published_at: datetime
    schema_version: str
    fingerprint: str
    stats: dict

    def public(self) -> dict:
        return {'id': self.id, 'published_at': self.published_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
                'schema_version': self.schema_version, 'fingerprint': self.fingerprint[:12]}


@dataclass
class SegmentRow:
    id: int
    street_name: str | None
    highway: str
    length_m: float
    coords: list


@dataclass
class EdgeRow:
    id: int
    kind: str
    segment_id: int | None
    source: int
    target: int
    start_m: float
    end_m: float
    length_m: float
    coords: list


@dataclass
class ObservationRow:
    id: int
    segment_id: int
    side: str
    position_m: float
    lng: float
    lat: float
    observed_on: date | None
    footpath: str | None
    needs_verification: bool
    source: str
    features: list[tuple[str, dict]] = field(default_factory=list)


@dataclass
class Snapshot:
    release: Release
    coverage: list[tuple[str, dict]]                # (name, GeoJSON MultiPolygon)
    segments: dict[int, SegmentRow]
    edges: list[EdgeRow]
    nodes: dict[int, tuple[float, float]]
    observations: list[ObservationRow]
    loaded_at: datetime
    marker: tuple                                    # database content marker read in the same transaction
    content_hash: str = ''

    def __post_init__(self):
        if not self.content_hash:
            self.content_hash = compute_content_hash(self)


def compute_content_hash(s: Snapshot) -> str:
    """sha256 of every loaded value the graph and the reports use."""
    digest = hashlib.sha256()
    put = lambda *values: digest.update(json.dumps(values, default=str, separators=(',', ':')).encode() + b'\n')
    r = s.release
    put('release', r.id, r.published_at.isoformat(), r.schema_version, r.fingerprint, json.dumps(r.stats, sort_keys=True))
    for name, geom in s.coverage:
        put('coverage', name, json.dumps(geom, sort_keys=True))
    for sid in sorted(s.segments):
        g = s.segments[sid]
        put('segment', g.id, g.street_name, g.highway, g.length_m, g.coords)
    for e in s.edges:
        put('edge', e.id, e.kind, e.segment_id, e.source, e.target, e.start_m, e.end_m, e.length_m, e.coords)
    for nid in sorted(s.nodes):
        put('node', nid, s.nodes[nid])
    for o in s.observations:
        put('observation', o.id, o.segment_id, o.side, o.position_m, o.lng, o.lat, o.observed_on, o.footpath,
            o.needs_verification, o.source, [(k, json.dumps(a, sort_keys=True)) for k, a in o.features])
    return digest.hexdigest()


def connect_readonly(url: str, timeout_s: int = 20) -> psycopg.Connection:
    kwargs = {} if 'sslmode=' in url or '127.0.0.1' in url or 'localhost' in url else {'sslmode': 'require'}
    conn = psycopg.connect(url, connect_timeout=timeout_s, application_name='maply-route-preview',
                           options='-c default_transaction_read_only=on -c statement_timeout=120000', **kwargs)
    conn.read_only = True
    return conn


# A content marker over everything the snapshot reads, so in-place edits (feature attrs,
# footpath, needs_verification, geometry) are detected, not only new rows or releases.
_AGG = "(SELECT md5(coalesce(string_agg(concat_ws(',', {cols}), ';' ORDER BY id), '')) FROM atlas.{table})"
_HEX = "encode(ST_AsEWKB(geom), 'hex')"
MARKER_SQL = 'SELECT md5(concat_ws(\'|\', ' + ', '.join(_AGG.format(table=t, cols=c) for t, c in (
    ('releases', 'id, quote_nullable(source_fingerprint), quote_nullable(schema_version), quote_nullable(published_at), stats::text'),
    ('coverage_areas', 'id, quote_nullable(name), ' + _HEX),
    ('segments', 'id, quote_nullable(street_name), quote_nullable(highway), length_m, ' + _HEX),
    ('routing_edges', 'id, kind, quote_nullable(segment_id), source, target, start_m, end_m, length_m, ' + _HEX),
    ('routing_nodes', 'id, ' + _HEX),
    ('survey_points', 'id, status, quote_nullable(release_id)'),
    ('observations', 'id, source, segment_id, side, quote_nullable(observed_on), quote_nullable(footpath), '
                     'needs_verification, quote_nullable(release_id), ' + _HEX),
    ('observation_features', 'id, observation_id, kind, attrs::text'),
)) + '))'


def read_marker(conn: psycopg.Connection) -> tuple:
    with conn.transaction():
        _assert_readonly(conn)
        row = conn.execute(MARKER_SQL).fetchone()
        raise_rollback()
    return (row[0],)


def raise_rollback():
    raise psycopg.Rollback()


def _assert_readonly(conn):
    if conn.execute('SHOW transaction_read_only').fetchone()[0] != 'on':
        raise SnapshotError('integrity_failed', 'Refusing to read: the transaction is not read-only.')


def load_snapshot(conn: psycopg.Connection) -> Snapshot:
    out: dict = {}
    with conn.transaction():
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        _assert_readonly(conn)
        out['marker'] = (conn.execute(MARKER_SQL).fetchone()[0],)
        row = conn.execute('SELECT id, published_at, schema_version, source_fingerprint, stats '
                           'FROM atlas.releases ORDER BY id DESC LIMIT 1').fetchone()
        if not row:
            raise SnapshotError('integrity_failed', 'The atlas has no release.')
        release = Release(row[0], row[1], row[2], row[3], row[4] if isinstance(row[4], dict) else json.loads(row[4]))
        _check_release(conn, release)
        out['coverage'] = [(n, json.loads(g)) for n, g in conn.execute(
            'SELECT name, ST_AsGeoJSON(geom) FROM atlas.coverage_areas ORDER BY id')]
        out['segments'] = {r[0]: SegmentRow(r[0], r[1], r[2], float(r[3]), json.loads(r[4])['coordinates'])
                           for r in conn.execute("""SELECT id, street_name, highway, ST_Length(ST_Transform(geom, 7856)),
                                                           ST_AsGeoJSON(geom) FROM atlas.segments ORDER BY id""")}
        out['edges'] = [EdgeRow(r[0], r[1], r[2], r[3], r[4], float(r[5]), float(r[6]), float(r[7]), json.loads(r[8])['coordinates'])
                        for r in conn.execute("""SELECT id, kind, segment_id, source, target, start_m, end_m, length_m,
                                                        ST_AsGeoJSON(geom) FROM atlas.routing_edges ORDER BY id""")]
        out['nodes'] = {r[0]: (r[1], r[2]) for r in conn.execute(
            'SELECT id, ST_X(geom), ST_Y(geom) FROM atlas.routing_nodes ORDER BY id')}
        observations = {}
        for r in conn.execute("""
                SELECT o.id, o.segment_id, o.side,
                       ST_LineLocatePoint(s.geom, o.geom) * ST_Length(ST_Transform(s.geom, 7856)),
                       ST_X(o.geom), ST_Y(o.geom), o.observed_on, o.footpath, o.needs_verification, o.source
                FROM atlas.observations o JOIN atlas.segments s ON s.id = o.segment_id ORDER BY o.id"""):
            observations[r[0]] = ObservationRow(r[0], r[1], r[2], float(r[3]), r[4], r[5], r[6], r[7], r[8], r[9])
        for oid, kind, attrs in conn.execute(
                'SELECT observation_id, kind, attrs FROM atlas.observation_features ORDER BY observation_id, id'):
            observations[oid].features.append((kind, attrs if isinstance(attrs, dict) else json.loads(attrs)))
        _check_graph(out['edges'], out['nodes'], out['segments'])
        if not out['coverage']:
            raise SnapshotError('integrity_failed', 'The atlas has no coverage area.')
        raise_rollback()  # end the read-only transaction with ROLLBACK
    return Snapshot(release=release, coverage=out['coverage'], segments=out['segments'], edges=out['edges'],
                    nodes=out['nodes'], observations=list(observations.values()),
                    loaded_at=datetime.now(timezone.utc), marker=out['marker'])


def _check_release(conn, release: Release):
    problems = []
    if release.schema_version != SCHEMA_VERSION:
        problems.append(f'schema version {release.schema_version}')
    one = lambda sql: conn.execute(sql).fetchone()[0]
    stats = release.stats
    if one('SELECT count(*) FROM atlas.survey_points') != stats.get('survey_points'):
        problems.append('survey point count')
    if one('SELECT count(*) FROM atlas.survey_points WHERE release_id <> %d' % release.id):
        problems.append('survey points from an older release')
    if one("SELECT count(*) FROM atlas.observations WHERE source = 'maply_survey'") != stats.get('observations'):
        problems.append('survey observation count')
    if one("SELECT count(*) FROM atlas.observations WHERE source = 'maply_survey' AND release_id <> %d" % release.id):
        problems.append('survey observations from an older release')
    if one('SELECT count(*) FROM atlas.segments') < stats.get('segments', 0):
        problems.append('segment count')
    kinds = {k: v for k, v in conn.execute("""SELECT f.kind, count(*) FROM atlas.observation_features f
                                              JOIN atlas.observations o ON o.id = f.observation_id
                                              WHERE o.source = 'maply_survey' GROUP BY 1""")}
    if kinds != stats.get('features_by_kind', {}):
        problems.append('feature counts')
    statuses = {k: v for k, v in conn.execute('SELECT status, count(*) FROM atlas.survey_points GROUP BY 1')}
    if statuses != stats.get('points_by_status', {}):
        problems.append('survey point statuses')
    if problems:
        raise SnapshotError('integrity_failed', 'Release %d does not match the tables: %s.' % (release.id, ', '.join(problems)))


def _check_graph(edges, nodes, segments):
    if not edges:
        raise SnapshotError('integrity_failed', 'The routing graph is empty.')
    for e in edges:
        if e.source not in nodes or e.target not in nodes or (e.kind == 'street' and e.segment_id not in segments):
            raise SnapshotError('integrity_failed', f'Routing edge {e.id} references a missing node or segment.')


def load_with_retry(url: str, attempts: int = 4, first_delay_s: float = 3.0) -> Snapshot:
    """The free-plan database sleeps; the first connection after a pause is often dropped."""
    delay = first_delay_s
    for attempt in range(1, attempts + 1):
        try:
            with connect_readonly(url) as conn:
                return load_snapshot(conn)
        except SnapshotError:
            raise
        except psycopg.Error as exc:
            # Never log the connection string or server address.
            log.warning('atlas read failed (attempt %d/%d): %s', attempt, attempts, type(exc).__name__)
            if attempt == attempts:
                raise SnapshotError('database_unreachable', 'The atlas database could not be read.') from None
            time.sleep(delay)
            delay *= 2
    raise AssertionError('unreachable')
