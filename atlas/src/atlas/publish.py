"""Replace the survey layer of the production database in one transaction.

Community observations are never touched. Segment and survey point ids are
kept stable by their tagger keys, so player data stays attached across publishes.
"""
from __future__ import annotations

import json

import psycopg

from atlas.extract import SCHEMA_VERSION, Dataset


def publish(conn: psycopg.Connection, dataset: Dataset, force: bool = False) -> dict:
    fingerprint = dataset.fingerprint()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('atlas.publish'))")
        latest = conn.execute('SELECT id, source_fingerprint FROM atlas.releases ORDER BY id DESC LIMIT 1').fetchone()
        if latest and latest[1] == fingerprint and not force:
            return {'release_id': latest[0], 'changed': False}
        stats = dataset.stats()
        release_id = conn.execute(
            'INSERT INTO atlas.releases (source, schema_version, source_fingerprint, stats) VALUES (%s, %s, %s, %s) RETURNING id',
            ('maply_survey', SCHEMA_VERSION, fingerprint, json.dumps(stats))).fetchone()[0]

        _segments(conn, dataset)
        _points(conn, dataset, release_id)
        _observations(conn, dataset, release_id)
        _coverage(conn, dataset)

        nodes, edges = conn.execute('SELECT * FROM atlas.rebuild_routing_graph()').fetchone()
        conn.execute('SELECT atlas.refresh_derived()')
    return {'release_id': release_id, 'changed': True, 'routing_nodes': nodes, 'routing_edges': edges, **stats}


def _segments(conn, dataset: Dataset):
    conn.execute("""CREATE TEMP TABLE _segments (maply_key text, street_name text, highway text, length_m real,
                    bearing_deg real, osm jsonb, wkt text) ON COMMIT DROP""")
    with conn.cursor().copy('COPY _segments FROM STDIN') as copy:
        for s in dataset.segments:
            copy.write_row((s.key, s.street_name, s.highway, s.length_m, s.bearing_deg, json.dumps(s.osm), s.wkt))
    conn.execute("""INSERT INTO atlas.segments (maply_key, street_name, highway, length_m, bearing_deg, osm, geom)
                    SELECT maply_key, street_name, highway, length_m, bearing_deg, osm, ST_GeomFromText(wkt, 4326)
                    FROM _segments
                    ON CONFLICT (maply_key) DO UPDATE SET street_name = EXCLUDED.street_name,
                        highway = EXCLUDED.highway, length_m = EXCLUDED.length_m,
                        bearing_deg = EXCLUDED.bearing_deg, osm = EXCLUDED.osm, geom = EXCLUDED.geom""")


def _points(conn, dataset: Dataset, release_id: int):
    conn.execute("""CREATE TEMP TABLE _points (maply_shot_id uuid, code text, segment_key text, side text,
                    facing text, status text, heading real, lng float8, lat float8) ON COMMIT DROP""")
    with conn.cursor().copy('COPY _points FROM STDIN') as copy:
        for p in dataset.points:
            copy.write_row((p.shot_id, p.code, p.segment_key, p.side, p.facing, p.status, p.heading, p.lng, p.lat))
    # Survey observations are rebuilt below; remove them before points or segments go.
    conn.execute("DELETE FROM atlas.observations WHERE source = 'maply_survey'")
    conn.execute('DELETE FROM atlas.survey_points p WHERE NOT EXISTS (SELECT 1 FROM _points n WHERE n.maply_shot_id = p.maply_shot_id)')
    conn.execute("""INSERT INTO atlas.survey_points (maply_shot_id, code, segment_id, side, facing, status, heading, geom, release_id)
                    SELECT n.maply_shot_id, n.code, s.id, n.side, n.facing, n.status, n.heading,
                           ST_SetSRID(ST_MakePoint(n.lng, n.lat), 4326), %s
                    FROM _points n JOIN atlas.segments s ON s.maply_key = n.segment_key
                    ON CONFLICT (maply_shot_id) DO UPDATE SET code = EXCLUDED.code, segment_id = EXCLUDED.segment_id,
                        side = EXCLUDED.side, facing = EXCLUDED.facing, status = EXCLUDED.status,
                        heading = EXCLUDED.heading, geom = EXCLUDED.geom, release_id = EXCLUDED.release_id""",
                 (release_id,))
    # Segments that left the survey go too, unless a player observation still uses them.
    conn.execute("""DELETE FROM atlas.segments g
                    WHERE NOT EXISTS (SELECT 1 FROM _segments n WHERE n.maply_key = g.maply_key)
                      AND NOT EXISTS (SELECT 1 FROM atlas.observations o WHERE o.segment_id = g.id)""")


def _observations(conn, dataset: Dataset, release_id: int):
    conn.execute("""CREATE TEMP TABLE _observations (maply_shot_id uuid, footpath text, observed_on date,
                    needs_verification boolean) ON COMMIT DROP""")
    conn.execute('CREATE TEMP TABLE _features (maply_shot_id uuid, kind text, attrs jsonb) ON COMMIT DROP')
    with conn.cursor().copy('COPY _observations FROM STDIN') as copy:
        for p in dataset.points:
            if p.observation:
                o = p.observation
                copy.write_row((p.shot_id, o.footpath, o.observed_on, o.needs_verification))
    with conn.cursor().copy('COPY _features FROM STDIN') as copy:
        for p in dataset.points:
            for f in p.observation.features if p.observation else ():
                copy.write_row((p.shot_id, f.kind, json.dumps(f.attrs)))
    conn.execute("""INSERT INTO atlas.observations (source, survey_point_id, segment_id, side, geom, observed_on,
                        footpath, needs_verification, release_id)
                    SELECT 'maply_survey', p.id, p.segment_id, p.side, p.geom, n.observed_on,
                           n.footpath, n.needs_verification, %s
                    FROM _observations n JOIN atlas.survey_points p USING (maply_shot_id)""", (release_id,))
    conn.execute("""INSERT INTO atlas.observation_features (observation_id, kind, attrs)
                    SELECT o.id, f.kind, f.attrs
                    FROM _features f
                    JOIN atlas.survey_points p USING (maply_shot_id)
                    JOIN atlas.observations o ON o.survey_point_id = p.id AND o.source = 'maply_survey'""")


def _coverage(conn, dataset: Dataset):
    for name, geojson in dataset.coverage.items():
        conn.execute("""INSERT INTO atlas.coverage_areas (name, geom)
                        VALUES (%s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)))
                        ON CONFLICT (name) DO UPDATE SET geom = EXCLUDED.geom""", (name, geojson))
