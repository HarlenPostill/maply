"""URL parsing, date provenance and capture verification (no database)."""

from __future__ import annotations

import io
from datetime import date

import numpy as np
import pytest
from PIL import Image

from app.capture.verification import (
    CapturePolicy,
    PageState,
    PlannedViewInfo,
    check_frame,
    evaluate_capture,
    parse_capture_date_label,
)
from app.geo.streetview_url import build_maps_pano_url, parse_streetview_url
from app.mock.panorama import client_path, resolve_viewpoint


def _png(arr) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr.astype("uint8")).save(buf, format="PNG")
    return buf.getvalue()


def _scene(seed=1) -> bytes:
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 255, (200, 320, 3))
    a[:100] = [120, 160, 220]
    return _png(a)


def test_parse_client_street_view_url():
    url = "https://www.google.com/maps/@-33.8688197,151.2092955,3a,75y,93.5h,85t/data=!3m6!1e1!3m4!1sAF1QipXYZ!2e0!7i16384!8i8192"
    p = parse_streetview_url(url)
    assert p.is_street_view and p.lat == pytest.approx(-33.8688197) and p.lng == pytest.approx(151.2092955)
    assert p.heading == 93.5 and p.fov == 75 and p.pitch == pytest.approx(-5)
    assert p.pano_id == "AF1QipXYZ" and p.pano_kind == "0" and p.provenance == "page_url"


def test_parse_map_url_is_not_street_view():
    p = parse_streetview_url("https://www.google.com/maps/@-33.87,151.21,17z")
    assert not p.is_street_view and p.heading is None


def test_build_documented_maps_url_and_parse_back():
    u = build_maps_pano_url(-33.87, 151.2, 400, -120, 5, pano_id="abc")
    assert u.startswith("https://www.google.com/maps/@?api=1&map_action=pano")
    p = parse_streetview_url(u)
    assert p.url_form == "maps_urls_api" and p.heading == 360 and p.pitch == -90 and p.fov == 10 and p.pano_id == "abc"


def test_mock_client_path_uses_same_format():
    pano = resolve_viewpoint(-33.875, 151.2067)
    assert pano is not None
    p = parse_streetview_url(client_path(pano, 90, -5, 80))
    assert p.is_street_view and p.pano_id == pano.pano_id and p.heading == 90 and p.pitch == pytest.approx(-5)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Street View · Image capture: Mar 2023", ("2023-03", "month")),
        ("Image capture: September 2019", ("2019-09", "month")),
        ("Image capture: 2021", ("2021", "year")),
        ("© 2024 Google", (None, "unknown")),  # copyright year is NOT an imagery date
        ("Terms  © 2025 Google  Image may be subject to copyright", (None, "unknown")),
        ("", (None, "unknown")),
    ],
)
def test_capture_date_only_from_explicit_label(text, expected):
    assert parse_capture_date_label(text) == expected


def test_frame_checks_reject_blank_and_transition_frames():
    assert "blank_frame" in check_frame(_png(np.zeros((200, 320, 3)))).flags
    ok = check_frame(_scene(1), second=_scene(1))
    assert ok.ok, ok.flags
    moving = check_frame(_scene(1), second=_scene(2))
    assert "unstable_transition_frame" in moving.flags


VIEW = PlannedViewInfo("v1", "left", "perpendicular", 270.0, -5.0, 80.0, 800, 500)


def _state(**kw) -> PageState:
    base = dict(
        mode="street_view", loaded=True, camera_lat=-33.8700, camera_lng=151.2120, camera_provenance="page_url",
        heading=270.0, heading_provenance="page_url", pitch=-5.0, pitch_provenance="page_url", fov=80.0, fov_provenance="page_url",
        pano_id="P1", pano_id_provenance="page_url", imagery_date="2024-05", imagery_date_precision="month",
        imagery_date_provenance="ui_text", attribution="© Example",
    )
    base.update(kw)
    return PageState(**base)


def _eval(state, policy=None, in_scope=True, req=(-33.8700, 151.2120)):
    return evaluate_capture(
        state=state, view=VIEW, requested_lat=req[0], requested_lng=req[1], segment_bearing=0.0,
        frame=check_frame(_scene()), policy=policy or CapturePolicy(), in_scope=in_scope, today=date(2026, 10, 16),
    )


def test_good_capture_is_verified_and_geometry_consistent():
    ev = _eval(_state())
    assert ev.verification_status == "verified" and ev.association_status == "geometry_consistent" and ev.flags == []


def test_far_camera_is_uncertain_and_needs_review():
    ev = _eval(_state(), req=(-33.8702, 151.2120))  # ~22 m south
    assert ev.verification_status == "needs_review"
    assert ev.association_status == "uncertain"
    assert "camera_far_from_sample" in ev.flags


def test_camera_beyond_no_imagery_distance_rejected():
    ev = _eval(_state(), req=(-33.8710, 151.2120))  # ~110 m
    assert ev.verification_status == "rejected"


def test_out_of_scope_camera_rejected():
    assert _eval(_state(), in_scope=False).verification_status == "rejected"


def test_unknown_camera_is_unresolved():
    ev = _eval(_state(camera_lat=None, camera_lng=None, camera_provenance="unknown"))
    assert ev.association_status == "unresolved" and "camera_position_unknown" in ev.flags
    assert ev.verification_status == "needs_review"


def test_heading_mismatch_and_wrong_side():
    ev = _eval(_state(heading=95.0))
    assert "heading_mismatch" in ev.flags and "observed_heading_on_other_side" in ev.flags
    assert ev.association_status == "uncertain"


@pytest.mark.parametrize("policy,status", [("needs_review", "needs_review"), ("accept_flagged", "verified"), ("reject", "rejected")])
def test_unknown_date_policy(policy, status):
    ev = _eval(_state(imagery_date=None, imagery_date_precision="unknown", imagery_date_provenance="unknown"), CapturePolicy(unknown_date_policy=policy))
    assert ev.verification_status == status
    assert "imagery_date_unknown" in ev.flags


def test_old_imagery_flagged():
    ev = _eval(_state(imagery_date="2012-01"), CapturePolicy(max_imagery_age_months=60))
    assert "imagery_older_than_desired" in ev.flags and ev.verification_status == "needs_review"


def test_missing_attribution_flagged():
    ev = _eval(_state(attribution=None))
    assert "attribution_missing" in ev.flags and ev.verification_status == "needs_review"


def test_loading_or_error_page_rejected():
    assert _eval(_state(mode="error", loaded=False)).verification_status == "rejected"
    assert _eval(_state(loaded=False)).verification_status == "rejected"
