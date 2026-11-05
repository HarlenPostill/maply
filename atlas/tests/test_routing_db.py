"""Read-only snapshot loading against a disposable database whose name contains "test"."""
from __future__ import annotations

from datetime import date

import psycopg
import pytest

from atlas.publish import publish
from atlas.routing.graph import Graph
from atlas.routing.policy import Policy
from atlas.routing.router import Router
from atlas.routing.snapshot import SnapshotError, connect_readonly, load_snapshot, read_marker
from test_atlas import URL, db, grid, lnglat  # noqa: F401  (db is a fixture)


@pytest.fixture
def published(db):  # noqa: F811
    publish(db, grid())
    return db


def test_snapshot_is_read_in_a_read_only_session(published):
    with connect_readonly(URL) as conn:
        snap = load_snapshot(conn)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with conn.transaction():
                conn.execute("INSERT INTO atlas.coverage_areas (name, geom) VALUES ('x', ST_GeomFromText('MULTIPOLYGON EMPTY', 4326))")
        assert read_marker(conn) == snap.marker
    assert snap.release.id == 1 and len(snap.content_hash) == 64
    assert len(snap.segments) == 6 and len(snap.observations) == 12
    assert any(e.kind == 'connector' for e in snap.edges)
    assert sum(len(o.features) for o in snap.observations) > 0


def test_snapshot_routes_and_changes_version_with_data(published):
    with connect_readonly(URL) as conn:
        snap = load_snapshot(conn)
    graph = Graph(snap, Policy(), None, date(2026, 11, 5))
    # Bottom St has no footpath (absent, unflagged) on either side: excluded even in previews.
    assert all(graph.pieces[e.id]['elements'][side].excluded == ['footpath_absent']
               for e in snap.edges if e.kind == 'street' and snap.segments[e.segment_id].street_name == 'Bottom St'
               for side in ('left', 'right'))
    route = Router(graph).route(lnglat(-5, 50), lnglat(205, 50))
    assert route['status'] == 'unverified_preview' and route['navigation_allowed'] is False
    assert 'Bottom St' not in {leg.get('street_name') for leg in route['legs']}
    published.execute("INSERT INTO atlas.observations (source, segment_id, side, geom, observed_on, footpath) "
                      "SELECT 'community', id, 'left', ST_StartPoint(geom), current_date, 'present' FROM atlas.segments LIMIT 1")
    published.commit()
    with connect_readonly(URL) as conn:
        assert read_marker(conn) != snap.marker
        assert load_snapshot(conn).content_hash != snap.content_hash


def test_release_that_does_not_match_its_tables_is_refused(published):
    published.execute("""UPDATE atlas.releases SET stats = jsonb_set(stats, '{features_by_kind,footpath_section}', '999')""")
    published.commit()
    with connect_readonly(URL) as conn, pytest.raises(SnapshotError) as info:
        load_snapshot(conn)
    assert info.value.reason == 'integrity_failed' and 'feature counts' in str(info.value)


def test_survey_status_correction_invalidates_release_marker(published):
    with connect_readonly(URL) as conn:
        before = load_snapshot(conn)
    published.execute("UPDATE atlas.survey_points SET status = 'no_imagery' "
                      "WHERE id = (SELECT min(id) FROM atlas.survey_points WHERE status = 'observed')")
    published.commit()
    with connect_readonly(URL) as conn:
        assert read_marker(conn) != before.marker
        with pytest.raises(SnapshotError) as info:
            load_snapshot(conn)
    assert info.value.reason == 'integrity_failed' and 'survey point statuses' in str(info.value)


@pytest.mark.parametrize('change', [
    "UPDATE atlas.observation_features SET attrs = attrs || '{\"surface\": \"asphalt\"}' WHERE id = (SELECT min(id) FROM atlas.observation_features)",
    "UPDATE atlas.observations SET footpath = 'absent' WHERE id = (SELECT min(id) FROM atlas.observations WHERE footpath = 'present')",
    "UPDATE atlas.observations SET needs_verification = NOT needs_verification WHERE id = (SELECT min(id) FROM atlas.observations)",
    "UPDATE atlas.observations SET geom = ST_Translate(geom, 0.00001, 0) WHERE id = (SELECT min(id) FROM atlas.observations)",
    "UPDATE atlas.routing_nodes SET geom = ST_Translate(geom, 0.00001, 0) WHERE id = (SELECT min(id) FROM atlas.routing_nodes)",
])
def test_in_place_edits_change_the_marker_and_content(published, change):
    with connect_readonly(URL) as conn:
        before = load_snapshot(conn)
        assert read_marker(conn) == before.marker          # the load records the same marker it would read
    published.execute(change)
    published.commit()
    with connect_readonly(URL) as conn:
        assert read_marker(conn) != before.marker
        assert load_snapshot(conn).content_hash != before.content_hash
