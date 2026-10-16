"""Geometry, projection and side-of-street conventions (no database)."""

from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString, Point, Polygon

from app.geo.planning import (
    PlanningError,
    PlanParams,
    generate_plan,
    lines_from_geojson,
    recompute_sample_for_position,
    segment_key_for,
    side_views,
)
from app.geo.projection import (
    geodesic_azimuth,
    geodesic_distance,
    heading_delta,
    normalize_heading,
    perpendicular_heading,
    side_of_heading,
    to_projected,
)

# North-south line along Macquarie St area; bearing ~0 (northbound)
NORTHBOUND = LineString([(151.2120, -33.8700), (151.2120, -33.8650)])
SOUTHBOUND = LineString(list(NORTHBOUND.coords)[::-1])
EASTBOUND = LineString([(151.2000, -33.8800), (151.2060, -33.8800)])


def test_heading_normalisation_and_delta():
    assert normalize_heading(-90) == 270
    assert normalize_heading(360) == 0
    assert normalize_heading(725) == pytest.approx(5)
    assert heading_delta(10, 350) == pytest.approx(20)
    assert heading_delta(350, 10) == pytest.approx(-20)
    assert heading_delta(180, 0) == pytest.approx(180)


def test_left_right_relative_to_stored_direction():
    b = geodesic_azimuth(*NORTHBOUND.coords[0], *NORTHBOUND.coords[1])
    assert b == pytest.approx(0, abs=0.5)
    assert perpendicular_heading(b, "left") == pytest.approx(270, abs=0.5)  # west
    assert perpendicular_heading(b, "right") == pytest.approx(90, abs=0.5)  # east
    assert side_of_heading(270, b) == "left"
    assert side_of_heading(90, b) == "right"
    assert side_of_heading(0, b) == "none"
    assert side_of_heading(180, b) == "none"


def test_side_labels_do_not_flip_for_reverse_travel():
    """A worker travelling south on a northbound segment still labels the west side 'left'."""
    b = geodesic_azimuth(*NORTHBOUND.coords[0], *NORTHBOUND.coords[1])
    worker_travel = normalize_heading(b + 180)  # southbound
    west_view = normalize_heading(worker_travel + 90)  # worker's own right is west
    assert west_view == pytest.approx(270, abs=0.5)
    assert side_of_heading(west_view, b) == "left"  # relative to stored direction, not travel


def test_reversed_segment_swaps_sides_and_gets_different_key():
    pn = generate_plan([(NORTHBOUND, "n")], PlanParams(sampling_interval_m=100))
    ps = generate_plan([(SOUTHBOUND, "s")], PlanParams(sampling_interval_m=100))
    assert pn.segments[0].key != ps.segments[0].key
    ln = [v for v in pn.samples[0].views if v.side == "left"][0]
    ls = [v for v in ps.samples[0].views if v.side == "left"][0]
    assert ln.heading_deg == pytest.approx(270, abs=0.6)
    assert ls.heading_deg == pytest.approx(90, abs=0.6)


def test_bearings_use_true_north_not_grid_north():
    # Grid bearing in MGA56 differs from true north by the meridian convergence (~1 deg in Sydney).
    a, b = to_projected(Point(NORTHBOUND.coords[0])), to_projected(Point(NORTHBOUND.coords[1]))
    grid = math.degrees(math.atan2(b.x - a.x, b.y - a.y))
    true = geodesic_azimuth(*NORTHBOUND.coords[0], *NORTHBOUND.coords[1])
    assert abs(heading_delta(true, grid)) > 0.5  # they genuinely differ
    assert true == pytest.approx(0, abs=0.05)


def test_sampling_interval_is_in_metres():
    p = generate_plan([(EASTBOUND, "e")], PlanParams(sampling_interval_m=50, sides="left", include_crossing_approaches=False))
    pts = [(s.lon, s.lat) for s in p.samples]
    length = geodesic_distance(*EASTBOUND.coords[0], *EASTBOUND.coords[1])
    assert len(pts) == int(length // 50) + 1 + (1 if length % 50 > 50 / 3 else 0)
    gaps = [geodesic_distance(*pts[i], *pts[i + 1]) for i in range(len(pts) - 2)]
    for g in gaps:
        assert g == pytest.approx(50, rel=0.005)
    # 0.0005 degrees of longitude is NOT 0.0005 m; sanity check scale
    assert geodesic_distance(151.2, -33.88, 151.2005, -33.88) == pytest.approx(46.2, abs=0.5)


def test_footpath_offset_is_on_correct_side():
    p = generate_plan([(NORTHBOUND, "n")], PlanParams(sampling_interval_m=100, footpath_offset_m=8))
    seg = p.segments[0]
    left_x = sum(x for x, _ in seg.footpath_left) / len(seg.footpath_left)
    right_x = sum(x for x, _ in seg.footpath_right) / len(seg.footpath_right)
    centre_x = NORTHBOUND.coords[0][0]
    assert left_x < centre_x < right_x  # west of a northbound line is left
    d = geodesic_distance(centre_x, -33.8675, left_x, -33.8675)
    assert d == pytest.approx(8, abs=0.2)


def test_segment_keys_are_stable_across_replans():
    a = generate_plan([(EASTBOUND, "x")], PlanParams(segment_max_length_m=200))
    b = generate_plan([(EASTBOUND, "y")], PlanParams(segment_max_length_m=200, sampling_interval_m=7))
    assert [s.key for s in a.segments] == [s.key for s in b.segments]
    assert segment_key_for([(1.0, 2.0), (3.0, 4.0)]) == segment_key_for([(1.0000000001, 2.0), (3.0, 4.0)])


def test_views_per_side_and_obliques():
    params = PlanParams(views_per_side=3, oblique_angle_deg=45)
    left = side_views(0.0, "left", params)
    right = side_views(0.0, "right", params)
    assert [v.view_kind for v in left] == ["perpendicular", "oblique_forward", "oblique_backward"]
    assert [v.heading_deg for v in left] == pytest.approx([270, 315, 225])
    assert [v.heading_deg for v in right] == pytest.approx([90, 45, 135])
    for v in left:
        assert side_of_heading(v.heading_deg, 0.0) == "left"
    for v in right:
        assert side_of_heading(v.heading_deg, 0.0) == "right"


def test_crossing_detection_and_approach_side_labels():
    cross = LineString([(151.2090, -33.8675), (151.2150, -33.8675)])
    p = generate_plan([(NORTHBOUND, "n"), (cross, "c")], PlanParams(sampling_interval_m=100, segment_max_length_m=1000))
    kinds = {n.kind for n in p.nodes}
    assert "route_crossing" in kinds
    crossing = [n for n in p.nodes if n.kind == "route_crossing"][0]
    approaches = [s for s in p.samples if s.kind == "crossing_approach" and s.node_key == crossing.key]
    assert len(approaches) == 4  # two on each crossing route (before and after)
    for s in approaches:
        ahead = [v for v in s.views if v.view_kind == "approach_ahead"][0]
        assert ahead.side == "none"
        for v in s.views:
            if v.view_kind != "approach_ahead":
                assert v.side == side_of_heading(v.heading_deg, s.local_bearing_deg)


def test_start_node_approach_looks_backward_with_swapped_corner_sides():
    p = generate_plan([(NORTHBOUND, "n")], PlanParams(sampling_interval_m=100))
    start = [s for s in p.samples if s.kind == "crossing_approach"][0]
    ahead = [v for v in start.views if v.view_kind == "approach_ahead"][0]
    assert ahead.heading_deg == pytest.approx(180, abs=0.6)  # looking back toward the start node
    # Travel-left while looking south is EAST, which is the segment's RIGHT side.
    al = [v for v in start.views if v.view_kind == "approach_ahead_left"][0]
    assert al.side == "right"


def test_route_outside_scope_is_rejected():
    scope = Polygon([(151.20, -33.88), (151.22, -33.88), (151.22, -33.86), (151.20, -33.86)])
    outside = LineString([(151.25, -33.90), (151.26, -33.90)])
    with pytest.raises(PlanningError):
        generate_plan([(outside, "x")], PlanParams(), scope=scope)


def test_polygon_perimeter_is_ccw_interior_left():
    poly = {"type": "Polygon", "coordinates": [[[151.20, -33.88], [151.20, -33.879], [151.201, -33.879], [151.201, -33.88], [151.20, -33.88]]]}
    lines, _ = lines_from_geojson(poly, "perimeter")
    ring = list(lines[0][0].coords)
    area2 = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:]))
    assert area2 > 0  # counter-clockwise


def test_moving_sample_too_far_is_rejected():
    params = PlanParams()
    with pytest.raises(PlanningError):
        recompute_sample_for_position(NORTHBOUND, 151.2150, -33.8675, params, ["left"])
    c, b, views = recompute_sample_for_position(NORTHBOUND, 151.21205, -33.8675, params, ["left", "right"])
    assert 0 < c < 600 and b == pytest.approx(0, abs=0.6) and len(views) == 2


def test_turns_split_segments_and_create_nodes():
    l_shape = LineString([(151.2000, -33.8800), (151.2000, -33.8780), (151.2025, -33.8780)])
    p = generate_plan([(l_shape, "L")], PlanParams(sampling_interval_m=40, segment_max_length_m=1000))
    assert len(p.segments) == 2
    assert any(n.kind == "turn_vertex" for n in p.nodes)
    assert p.segments[0].bearing_deg == pytest.approx(0, abs=1)
    assert p.segments[1].bearing_deg == pytest.approx(90, abs=1)
