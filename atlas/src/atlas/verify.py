"""Check the production database against the tagger extract and report health."""
from __future__ import annotations

import psycopg

from atlas.extract import Dataset


def summary(conn: psycopg.Connection) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]
    pairs = lambda sql: {k: v for k, v in conn.execute(sql).fetchall()}
    release = conn.execute('SELECT id, published_at, stats FROM atlas.releases ORDER BY id DESC LIMIT 1').fetchone()
    report = {
        'release': {'id': release[0], 'published_at': release[1].isoformat()} if release else None,
        'segments': one('SELECT count(*) FROM atlas.segments'),
        'survey_points': one('SELECT count(*) FROM atlas.survey_points'),
        'points_by_status': pairs('SELECT status, count(*) FROM atlas.survey_points GROUP BY 1 ORDER BY 1'),
        'observations': pairs('SELECT source, count(*) FROM atlas.observations GROUP BY 1 ORDER BY 1'),
        'footpath': pairs("""SELECT coalesce(footpath, 'unknown'), count(*) FROM atlas.observations
                             WHERE source = 'maply_survey' GROUP BY 1 ORDER BY 1"""),
        'features_by_kind': pairs('SELECT kind, count(*) FROM atlas.observation_features GROUP BY 1 ORDER BY 1'),
        'side_grades': pairs('SELECT grade, count(*) FROM atlas.segment_sides GROUP BY 1 ORDER BY 1'),
        'surveyed_km_by_grade': pairs("""SELECT grade, round(sum(s.length_m)::numeric / 1000, 1) FROM atlas.segment_sides d
                                          JOIN atlas.segments s ON s.id = d.segment_id GROUP BY 1 ORDER BY 1"""),
        'routing': {'nodes': one('SELECT count(*) FROM atlas.routing_nodes'),
                    'edges': one('SELECT count(*) FROM atlas.routing_network'),
                    'pgrouting': one("SELECT extversion FROM pg_extension WHERE extname = 'pgrouting'")},
        'verification_targets': pairs('SELECT reason, count(*) FROM atlas.verification_targets GROUP BY 1 ORDER BY 1'),
        'postgis': one("SELECT extversion FROM pg_extension WHERE extname = 'postgis'"),
    }
    if report['routing']['pgrouting']:
        report['routing']['largest_component_share'] = float(one("""
            WITH c AS (SELECT component, count(*) n FROM pgr_connectedComponents(
                'SELECT id, source, target, cost, reverse_cost FROM atlas.routing_network') GROUP BY component)
            SELECT round(max(n)::numeric / nullif(sum(n), 0), 4) FROM c"""))
    return report


def compare(report: dict, dataset: Dataset) -> list[str]:
    """Differences between what was extracted and what the database holds."""
    expected = dataset.stats()
    problems = []
    checks = {
        'segments': (report['segments'], expected['segments']),
        'survey_points': (report['survey_points'], expected['survey_points']),
        'points_by_status': (report['points_by_status'], expected['points_by_status']),
        'survey observations': (report['observations'].get('maply_survey', 0), expected['observations']),
        'footpath': ({k: v for k, v in report['footpath'].items()}, {k: v for k, v in expected['footpath'].items() if v}),
        'features_by_kind': (report['features_by_kind'], expected['features_by_kind']),
    }
    for name, (actual, wanted) in checks.items():
        if actual != wanted:
            problems.append(f'{name}: database has {actual}, tagger extract has {wanted}')
    return problems
