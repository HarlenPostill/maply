"""Fast checks of the parts that must be right: geometry, links, prompt, result matching."""

from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="maply-test-"))

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from app.geo.projection import geodesic_distance, side_of_heading  # noqa: E402
from app.geo.streetview_url import parse_streetview_url  # noqa: E402
from app.prompts import build_prompt  # noqa: E402
from app.results import evaluate  # noqa: E402
from app.shots import JobOptions, plan_shots  # noqa: E402

# George St, Sydney CBD, drawn northbound (bearing ~7.5 deg)
GEORGE = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": "George Street"}, "geometry": {"type": "LineString", "coordinates": [[151.20668, -33.87501], [151.20690, -33.87360]]}}]}


def test_shots_have_correct_sides_links_and_files():
    shots, segments, summary, _ = plan_shots(GEORGE, JobOptions(spacing_m=30))
    assert summary["shots"] == len(shots) == 2 * summary["points"]
    left = next(s for s in shots if s.side == "left")
    right = next(s for s in shots if s.side == "right")
    assert left.heading == pytest.approx(277.5, abs=1) and "west footpath of George Street" in left.look
    assert right.heading == pytest.approx(97.5, abs=1) and "east footpath" in right.look
    p = parse_streetview_url(left.url)
    assert p.url_form == "maps_urls_api" and p.heading == pytest.approx(left.heading, abs=0.1) and p.lat == pytest.approx(left.lat, abs=1e-6)
    assert left.filename.startswith(f"george-street/{left.code}_{left.lat:.6f}_{left.lng:.6f}_h") and left.filename.endswith(".jpg")
    assert left.footpath == "west" and right.footpath == "east"
    pts = sorted({(s.lng, s.lat) for s in shots}, key=lambda t: t[1])
    assert geodesic_distance(*pts[0], *pts[1]) == pytest.approx(30, abs=0.5)  # spacing is metres


def test_reverse_drawn_street_still_describes_the_same_footpath():
    rev = json.loads(json.dumps(GEORGE))
    rev["features"][0]["geometry"]["coordinates"].reverse()
    shots, *_ = plan_shots(rev, JobOptions(spacing_m=30))
    left = next(s for s in shots if s.side == "left")  # left of a southbound line is east
    assert left.heading == pytest.approx(97.5, abs=1) and "east footpath" in left.look


# A divided road drawn as two one-way carriageways ~10 m apart, and a different street ~10 m further east.
DIVIDED = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"name": "Primrose Ave"}, "geometry": {"type": "LineString", "coordinates": [[151.20000, -33.9000], [151.20000, -33.8985]]}},
    {"type": "Feature", "properties": {"name": "Primrose Ave"}, "geometry": {"type": "LineString", "coordinates": [[151.20011, -33.8985], [151.20011, -33.9000]]}},
    {"type": "Feature", "properties": {"name": "Other Street"}, "geometry": {"type": "LineString", "coordinates": [[151.20022, -33.9000], [151.20022, -33.8985]]}},
]}


def test_divided_roads_photograph_only_the_footpath_beside_each_carriageway():
    both, *_ = plan_shots(DIVIDED, JobOptions(spacing_m=30, divided_roads_near_side=False))
    near, _, summary, _ = plan_shots(DIVIDED, JobOptions(spacing_m=30))
    west, east = [s for s in near if s.lng == 151.2], [s for s in near if s.lng == 151.20011]
    assert west and {s.footpath for s in west} == {"west"}
    assert east and {s.footpath for s in east} == {"east"}
    assert {s.footpath for s in near if s.street == "Other Street"} == {"west", "east"}  # a different street is not a carriageway
    assert summary["divided_road_views_skipped"] == len(both) - len(near) == len(west) + len(east)
    # the shots that remain are the same shots, with the same codes
    old = {s.code: (s.lat, s.lng, s.heading, s.footpath) for s in both}
    assert all(old[s.code] == (s.lat, s.lng, s.heading, s.footpath) for s in near)


def test_batches_and_prompt_contents():
    shots, *_ = plan_shots(GEORGE, JobOptions(spacing_m=15, batch_size=5))
    assert max(s.batch for s in shots) == (len(shots) - 1) // 5 + 1
    batch1 = [s for s in shots if s.batch == 1]
    text = build_prompt(job_name="George St", slug="george-st", shots=batch1, label="batch 1 of 3", log_name="batch-01.jsonl", total=len(shots))
    for s in batch1:
        assert s.url in text and f"images/{s.filename}" in text and s.code in text
    assert "/jobs/george-st" in text and "results/batch-01.jsonl" in text and "final_url" in text
    assert "computer use" in text and "screencapture" in text


def _png(path: Path, w=1280, h=800):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), (90, 120, 140)).save(path, format="PNG")


def _shot(**kw):
    base = dict(code="P001-L1", filename="george-street/P001-L1_-33.875010_151.206680_h277.jpg", lat=-33.87501, lng=151.20668, heading=277.5, side="left", status="pending", result={}, flags=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_result_matching_and_flags(tmp_path):
    sh = _shot()
    assert evaluate(sh, tmp_path, None, 7.5)[0] == "pending"
    _png(tmp_path / "images" / sh.filename)
    good = f"https://www.google.com/maps/@-33.8750150,151.2066830,3a,80y,{280:.2f}h,82t/data=!3m6!1e1!3m4!1sPANO!2e0"
    status, flags, res = evaluate(sh, tmp_path, {"shot": sh.code, "status": "captured", "final_url": good, "image_date": "Mar 2023"}, 7.5)
    assert status == "captured" and flags == [] and res["camera_offset_m"] < 2 and res["pano_id"] == "PANO"
    wrong = good.replace("280.00h", "95.00h")  # facing the east footpath instead
    status, flags, _ = evaluate(sh, tmp_path, {"shot": sh.code, "status": "captured", "final_url": wrong}, 7.5)
    assert status == "needs_check" and {"heading_off", "wrong_side", "no_image_date"} <= set(flags)
    far = good.replace("-33.8750150", "-33.8760000")  # ~110 m south
    assert "far_from_plan" in evaluate(sh, tmp_path, {"shot": sh.code, "final_url": far, "image_date": "x"}, 7.5)[1]
    # a file without a log line still counts, but is flagged
    status, flags, _ = evaluate(sh, tmp_path, None, 7.5)
    assert status == "captured" and "not_logged" in flags and "no_final_url" in flags


def test_no_imagery_and_operator_decisions(tmp_path):
    sh = _shot()
    assert evaluate(sh, tmp_path, {"shot": sh.code, "status": "no_imagery"}, 7.5)[0] == "no_imagery"
    _png(tmp_path / "images" / sh.filename)
    _, _, res = evaluate(sh, tmp_path, None, 7.5)
    kept = _shot(status="accepted", result=res)
    assert evaluate(kept, tmp_path, None, 7.5)[0] == "accepted"  # unchanged file keeps the decision
    _png(tmp_path / "images" / sh.filename, w=1440)  # retaken -> re-checked
    assert evaluate(kept, tmp_path, None, 7.5)[0] == "captured"


def test_side_convention():
    assert side_of_heading(270, 0) == "left" and side_of_heading(90, 0) == "right"
    assert side_of_heading(90, 180) == "left"  # same east footpath is 'left' of a southbound segment
