"""HTTP API: workflow, editing, review, export and local-only guards."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.db


@pytest.fixture
def client(db):
    from app.api.main import app

    return TestClient(app, base_url="http://127.0.0.1")


def _project(client) -> str:
    r = client.post("/api/projects", json={"name": "API test", "config": {"approval_reference": "REF-1"}})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _plan(client, pid, **params) -> dict:
    r = client.post(f"/api/projects/{pid}/routes", json={"name": "George", "kind": "route", "geojson": {"type": "LineString", "coordinates": [[151.20668, -33.87501], [151.20690, -33.87360]]}})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    r = client.post(f"/api/projects/{pid}/plans", json={"route_ids": [rid], "params": {"sampling_interval_m": 30, **params}})
    assert r.status_code == 200, r.text
    return r.json()


def test_plan_preview_edit_and_approve(client):
    pid = _project(client)
    plan = _plan(client, pid)
    gj = plan["geojson"]
    assert gj["segments"]["features"] and gj["samples"]["features"] and gj["footpaths"]["features"]
    smp = next(f for f in gj["samples"]["features"] if f["properties"]["kind"] == "street")
    lng, lat = smp["geometry"]["coordinates"]
    # move 3 m east: allowed, views re-derived
    r = client.patch(f"/api/samples/{smp['properties']['id']}", json={"lng": lng + 0.00003, "lat": lat})
    assert r.status_code == 200, r.text
    moved = next(f for f in r.json()["geojson"]["samples"]["features"] if f["properties"]["id"] == smp["properties"]["id"])
    assert moved["properties"]["edited"] is True
    # move 300 m: refused
    r = client.patch(f"/api/samples/{smp['properties']['id']}", json={"lng": lng + 0.003, "lat": lat})
    assert r.status_code == 422
    r = client.patch(f"/api/samples/{smp['properties']['id']}", json={"excluded": True})
    assert r.status_code == 200
    assert client.post(f"/api/plans/{plan['id']}/approve").json()["status"] == "approved"
    # approved plans are immutable
    assert client.patch(f"/api/samples/{smp['properties']['id']}", json={"excluded": False}).status_code == 422


def test_route_outside_scope_refused_at_planning(client):
    pid = _project(client)
    r = client.post(f"/api/projects/{pid}/routes", json={"kind": "route", "geojson": {"type": "LineString", "coordinates": [[151.30, -33.80], [151.31, -33.80]]}})
    assert r.status_code == 200 and r.json()["warnings"]
    r2 = client.post(f"/api/projects/{pid}/plans", json={"route_ids": [r.json()["id"]]})
    assert r2.status_code == 422 and "scope" in r2.json()["detail"]


def test_streetview_seed_url(client):
    r = client.post("/api/streetview/parse", json={"url": "https://www.google.com/maps/@-33.8740,151.2068,3a,75y,280h,85t/data=!3m4!1e1!3m2!1sABC!2e0"})
    assert r.status_code == 200
    d = r.json()
    assert d["heading"] == 280 and d["pano_id"] == "ABC" and d["documented_url"].startswith("https://www.google.com/maps/@?api=1&map_action=pano")
    pid = _project(client)
    rr = client.post(f"/api/projects/{pid}/routes", json={"kind": "route", "geojson": {"type": "LineString", "coordinates": [[151.20668, -33.87501], [151.20690, -33.87360]]}})
    seed = client.post(f"/api/projects/{pid}/routes", json={"kind": "streetview_seed", "geojson": {"type": "Point", "coordinates": [151.2068, -33.8740]}, "properties": {"heading": 280, "pitch": -5, "fov": 75}})
    plan = client.post(f"/api/projects/{pid}/plans", json={"route_ids": [rr.json()["id"], seed.json()["id"]]}).json()
    seeds = [f for f in plan["geojson"]["samples"]["features"] if f["properties"]["kind"] == "seed"]
    assert len(seeds) == 1 and seeds[0]["properties"]["views"][0]["heading_deg"] == 280
    assert seeds[0]["properties"]["views"][0]["side"] == "left"  # west of a northbound segment


def test_job_lifecycle_endpoints_and_openai_refusal(client):
    pid = _project(client)
    plan = _plan(client, pid)
    client.post(f"/api/plans/{plan['id']}/approve")
    r = client.post(f"/api/plans/{plan['id']}/jobs", json={"target": "synthetic", "method": "openai_agent", "model_budget_usd": 1})
    assert r.status_code == 422
    errs = r.json()["detail"]["errors"]
    assert any("not set" in e or "OPENAI" in e for e in errs) and any("egress is disabled" in e or "screenshots" in e for e in errs)
    j = client.post(f"/api/plans/{plan['id']}/jobs", json={"target": "synthetic", "method": "deterministic_browser", "requests_per_minute": 30}).json()
    assert j["status"] == "draft"
    assert client.post(f"/api/jobs/{j['id']}/start").json()["status"] == "running"
    p = client.post(f"/api/jobs/{j['id']}/pause").json()
    assert p["status"] == "paused" and p["pause_kind"] == "operator"
    assert client.post(f"/api/jobs/{j['id']}/resume").json()["status"] == "running"
    c = client.post(f"/api/jobs/{j['id']}/cancel").json()
    assert c["status"] == "cancelled" and set(c["task_counts"]) == {"cancelled"}
    cov = client.get(f"/api/jobs/{j['id']}/coverage").json()
    assert cov["collection"]["completeness"] == 1.0 and cov["verified_coverage"] == 0.0  # complete != covered


def test_review_accept_reject_and_export(client, db, data_dir):
    from app.db import session_scope
    from app.models import Job
    from app.scheduler import queue
    from tests.helpers import complete_with_captures

    pid = _project(client)
    plan = _plan(client, pid)
    client.post(f"/api/plans/{plan['id']}/approve")
    j = client.post(f"/api/plans/{plan['id']}/jobs", json={"target": "synthetic", "method": "deterministic_browser"}).json()
    client.post(f"/api/jobs/{j['id']}/start")
    import uuid

    with session_scope() as s:
        c = queue.claim_next_task(s, uuid.UUID(j["id"]), "w")
    complete_with_captures(c, "w")
    obs = client.get(f"/api/jobs/{j['id']}/observations").json()["items"]
    assert obs
    o1, o2 = obs[0], obs[1]
    detail = client.get(f"/api/observations/{o1['id']}").json()
    assert detail["camera_position"]["meaning"].startswith("camera/panorama position")
    assert detail["imagery_date"]["provenance"] == "ui_text" and detail["collected_at"]
    r = client.post(f"/api/observations/{o1['id']}/review", json={"decision": "accept", "confirm_association": True, "note": "ramp visible"})
    assert r.json()["association_status"] == "operator_verified" and r.json()["review"]["status"] == "accepted"
    r = client.post(f"/api/observations/{o2['id']}/review", json={"decision": "reject", "recapture": True, "note": "bus in the way"})
    assert r.json()["verification_status"] == "rejected"
    t = client.get(f"/api/tasks/{o2['task_id']}").json()
    assert t["status"] == "queued" and t["view_filter"] == [r.json()["planned_view_id"]]
    crop = client.post(f"/api/observations/{o1['id']}/crops", json={"x": 10, "y": 10, "width": 50, "height": 40, "purpose": "kerb"}).json()
    assert crop["raw_observation_id"] == o1["id"] and crop["geometry"]["width"] == 50
    exp = client.post(f"/api/projects/{pid}/exports", json={"include": "all_current"}).json()
    root = data_dir / exp["path"]
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["format"] == "maply-dataset/1" and "LOCAL research use only" in manifest["usage_restrictions"]
    for f in manifest["files"]:
        assert not f["path"].startswith("/") and (root / f["path"]).is_file()
    recs = [json.loads(line) for line in (root / "observations.jsonl").read_text().splitlines()]
    assert all(r["image"]["path"].startswith("images/") for r in recs)
    assert o2["id"] not in {r["observation_id"] for r in recs}  # rejected capture not exported


def test_local_only_guards(client):
    from app.api.main import app

    evil = TestClient(app, base_url="http://evil.example")
    assert evil.get("/api/projects").status_code == 400  # DNS-rebinding protection
    r = client.post("/api/projects", json={"name": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/projects", content="name=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    for path in ("/files/../../etc/passwd", "/files/%2e%2e/%2e%2e/etc/passwd", "/files/..%2f..%2fetc/passwd", "/etc/passwd", "/assets/../../../../etc/passwd"):
        r = client.get(path)
        assert "root:" not in r.text, path  # never serves files outside DATA_DIR / the built dashboard
    from app.api.main import serve_file
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        serve_file("../../../etc/passwd")
    assert e.value.status_code == 403
    st = client.get("/api/system/status").json()
    assert st["processing_mode"]["label"] == "LOCAL ONLY"
    assert not any(k in json.dumps(st).lower() for k in ("upload", "publish"))


def test_mock_site_is_labelled_synthetic(client):
    r = client.get("/mock/streetview?viewpoint=-33.8745,151.2067&heading=90&pitch=0&fov=80")
    assert r.status_code == 200 and "SYNTHETIC MOCK PANORAMA" in r.text and "NOT Google" in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"]


def test_import_is_all_or_nothing_and_segments_link_routes(client):
    pid = _project(client)
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "ok"}, "geometry": {"type": "LineString", "coordinates": [[151.20668, -33.87501], [151.20690, -33.87360]]}},
        {"type": "Feature", "properties": {"name": "bad"}, "geometry": {"type": "LineString", "coordinates": [[10.0, 50.0], [10.1, 50.0]]}},
    ]}
    r = client.post(f"/api/projects/{pid}/routes/import", json=fc)
    assert r.status_code == 422 and r.json()["detail"]["errors"][0]["feature"] == 1
    assert client.get(f"/api/projects/{pid}/routes").json() == []  # nothing saved
    fc["features"].pop()
    routes = client.post(f"/api/projects/{pid}/routes/import", json=fc).json()
    plan = client.post(f"/api/projects/{pid}/plans", json={"route_ids": [routes[0]["id"]]}).json()
    from app.db import session_scope
    from app.models import Segment
    from sqlalchemy import select
    import uuid

    with session_scope() as s:
        rids = set(s.execute(select(Segment.route_id).where(Segment.plan_id == uuid.UUID(plan["id"]))).scalars())
    assert rids == {uuid.UUID(routes[0]["id"])}


def test_delete_job_requires_confirmation_and_removes_files(client, data_dir):
    pid = _project(client)
    plan = _plan(client, pid)
    client.post(f"/api/plans/{plan['id']}/approve")
    j = client.post(f"/api/plans/{plan['id']}/jobs", json={"target": "synthetic", "method": "deterministic_browser"}).json()
    assert client.delete(f"/api/jobs/{j['id']}?confirm=nope").status_code == 422
    client.post(f"/api/jobs/{j['id']}/start")
    assert client.delete(f"/api/jobs/{j['id']}?confirm=delete").status_code == 409  # running
    client.post(f"/api/jobs/{j['id']}/cancel")
    assert client.delete(f"/api/jobs/{j['id']}?confirm=delete").status_code == 200
    assert client.get(f"/api/jobs/{j['id']}").status_code == 404
    assert client.delete(f"/api/projects/{pid}?confirm=wrong").status_code == 422
    assert client.delete(f"/api/projects/{pid}?confirm=API test").status_code == 200
