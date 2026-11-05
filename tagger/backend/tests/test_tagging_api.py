"""Queue/worker/map contracts against an isolated PostGIS test database.

The worker results are deterministic fixtures: these tests exercise orchestration
and validation, not a claim about a vision model's image recognition accuracy.
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

WORKER_HEADERS = {"X-Maply": "tagging-worker"}
WORKER = "/api/tagging/worker"
STREETS = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {"name": name},
         "geometry": {"type": "LineString", "coordinates": [[lng, -33.90], [lng, -33.8994]]}}
        for name, lng in [("Alpha Street", 151.20), ("Alpha Street", 151.202), ("Beta Street", 151.204)]
    ],
}


@pytest.fixture(scope="module")
def api():
    import psycopg
    from sqlalchemy.engine import make_url

    database_url = os.environ["DATABASE_URL"]
    assert make_url(database_url).database == "maply_test", "Never reset a non-test database"
    try:
        connection = psycopg.connect(database_url.replace("+psycopg", ""), autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("maply_test database not reachable")
    with connection as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, check=True, capture_output=True)
    from app.db import configure_engine
    from app.main import app
    from fastapi.testclient import TestClient

    configure_engine(database_url)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/tagging/settings").json() == {
            "parallel_sessions": 1, "max_parallel_sessions": 20, "running": 0,
        }
        yield client


@pytest.fixture
def client(api):
    # Claim is intentionally global; isolate outstanding work between test cases.
    from sqlalchemy import delete
    from app.db import session_scope
    from app.models import TagTask, TagWorker

    with session_scope() as session:
        session.execute(delete(TagTask))
        session.execute(delete(TagWorker))
    # Existing queue/lease contracts are independent of the newly configurable cap.
    response = api.patch("/api/tagging/settings", json={"parallel_sessions": 8})
    assert response.status_code == 200, response.text
    return api


def capture(client, shot, color=(100, 120, 130)):
    image = io.BytesIO()
    Image.new("RGB", (1440, 900), color).save(image, format="JPEG", quality=90)
    data = "data:image/jpeg;base64," + base64.b64encode(image.getvalue()).decode()
    url = f"https://www.google.com/maps/@{shot['lat']:.7f},{shot['lng']:.7f},3a,80y,{shot['heading']:.2f}h,82t/data=!3m4!1e1!3m2!1sTESTPANO!2e0"
    response = client.post("/api/ext/capture", headers={"X-Maply": "extension"}, json={
        "shot_id": shot["id"], "image": data, "page_url": url, "image_date_text": "Apr 2023",
    })
    assert response.status_code == 200, response.text
    return client.get(f"/api/shots/{shot['id']}").json()


@pytest.fixture
def job(client):
    response = client.post("/api/jobs", json={
        "name": f"Tagging test {uuid.uuid4().hex[:8]}", "streets": STREETS,
        "options": {"spacing_m": 30},
    })
    assert response.status_code == 200, response.text
    job = response.json()
    shots = client.get(f"/api/jobs/{job['id']}/shots?limit=100").json()["items"]
    groups = {}
    for shot in shots:
        groups.setdefault((shot["segment_key"], shot["side"]), []).append(shot)
    # Two captured views on each side allow a true partial -> complete transition.
    saved = [capture(client, shot) for group in groups.values() for shot in group[:2]]
    return job, shots, saved


def queue(client, job, selection):
    return client.post(f"/api/jobs/{job['id']}/tagging", json=selection)


def claim(client, worker_id="test-worker"):
    from app.tagging import MODEL
    response = client.post(f"{WORKER}/claim", headers=WORKER_HEADERS, json={"worker_id": worker_id, "model": MODEL})
    assert response.status_code == 200, response.text
    return response.json()["task"]


def observation(value, confidence=0.97):
    return {"value": value, "confidence": confidence, "evidence": "Concrete path clearly visible", "visibility": "clear"}


def labels():
    return {
        "schema_version": "maply-accessibility/1", "footpath": observation("present"),
        "features": [{
            "id": "f1", "kind": "footpath_section", "region": [0.1, 0.4, 0.9, 0.8],
            "location_description": "Near-side pedestrian strip",
            "tags": {
                "presence": observation("present"), "surface": observation("concrete", 0.93),
                "path_type": observation("dedicated_footpath"),
                "continuity": observation("continuous_visible_extent"),
            },
        }], "notes": [], "review_reasons": [],
    }


def complete(client, task, worker_id="test-worker", **overrides):
    payload = {"worker_id": worker_id, "lease_token": task["lease_token"], "model": task["model"], "labels": labels(), **overrides}
    return client.post(f"{WORKER}/tasks/{task['id']}/complete", headers=WORKER_HEADERS, json=payload)


@pytest.mark.parametrize("selector", ["street", "segment", "side", "shot", "intersection"])
def test_selectors_only_queue_requested_eligible_photos(client, job, selector):
    current, _, saved = job
    first = saved[0]
    if selector == "street":
        selection = {"streets": [first["street"]]}
        expected = {s["id"] for s in saved if s["street"] == first["street"]}
    elif selector == "segment":
        selection = {"segment_keys": [first["segment_key"]]}
        expected = {s["id"] for s in saved if s["segment_key"] == first["segment_key"]}
    elif selector == "side":
        selection = {"segment_keys": [first["segment_key"]], "side": "left"}
        expected = {s["id"] for s in saved if s["segment_key"] == first["segment_key"] and s["side"] == "left"}
    elif selector == "shot":
        selection = {"shot_ids": [first["id"]]}
        expected = {first["id"]}
    else:
        selection = {"streets": ["Beta Street"], "shot_ids": [first["id"]]}
        expected = set()
    response = queue(client, current, selection)
    assert response.status_code == 200, response.text
    assert response.json()["queued"] == len(expected)
    records = client.get(f"/api/jobs/{current['id']}/shots?limit=100&tagging_status=queued").json()["items"]
    assert {s["id"] for s in records} == expected
    # Retrying the same selection cannot duplicate active work.
    assert queue(client, current, selection).json()["queued"] == 0


def test_explicit_selection_and_capture_eligibility(client, job):
    from app.db import session_scope
    from app.models import Shot

    current, shots, saved = job
    assert queue(client, current, {}).status_code == 422
    assert queue(client, current, {"side": "left"}).status_code == 422
    assert queue(client, current, {"streets": ["Alpha Street"], "typo": True}).status_code == 422
    states = ["captured", "accepted", "needs_check", "retake", "pending", "no_imagery"]
    selected = saved[:len(states)]
    with session_scope() as session:
        for shot, state in zip(selected, states):
            session.get(Shot, uuid.UUID(shot["id"])).status = state
    pending = next(s for s in shots if s["id"] not in {item["id"] for item in saved})
    response = queue(client, current, {"shot_ids": [s["id"] for s in selected] + [pending["id"]]})
    assert response.json() == {"queued": 3, "skipped": 4}


def test_worker_leases_validation_completion_and_export(client, job):
    current, _, saved = job
    shot = saved[0]
    assert client.post(f"{WORKER}/claim", json={"worker_id": "test-worker"}).status_code == 403
    assert queue(client, current, {"shot_ids": [shot["id"]]}).json()["queued"] == 1
    task = claim(client)
    assert task["shot_id"] == shot["id"]
    assert task["schema"]["additionalProperties"] is False
    assert shot["street"] in task["prompt"] and "confidence=null" in task["prompt"]
    assert client.get(task["image_url"]).status_code == 200
    assert claim(client, "other-worker") is None
    assert complete(client, task, worker_id="other-worker").status_code == 409
    assert complete(client, task, model="wrong-model").status_code == 422
    invalid = labels()
    invalid["footpath"]["confidence"] = 1.4
    assert complete(client, task, labels=invalid).status_code == 422
    heartbeat = client.post(f"{WORKER}/tasks/{task['id']}/heartbeat", headers=WORKER_HEADERS,
                            json={"worker_id": "test-worker", "lease_token": task["lease_token"]})
    assert heartbeat.status_code == 200
    assert complete(client, task).json() == {"ok": True, "needs_review": False}
    assert complete(client, task).status_code == 409  # Exactly one result is applied.
    result = client.get(f"/api/shots/{shot['id']}").json()
    assert result["tagging"]["status"] == "completed"
    assert result["labels"]["features"][0]["tags"]["surface"]["confidence"] == 0.93
    assert result["labels"]["provenance"]["confidence_kind"] == "model_reported_uncalibrated"
    assert result["labels"]["provenance"]["image_sha256"] == shot["result"]["sha256"]
    listed = client.get(f"/api/jobs/{current['id']}/shots?tagging_status=completed&include_labels=false").json()["items"]
    assert listed[0]["labels"] is None and listed[0]["tagging"]["status"] == "completed"
    assert queue(client, current, {"shot_ids": [shot["id"]]}).json()["queued"] == 0
    response = client.post(f"/api/jobs/{current['id']}/dataset")
    assert response.status_code == 200, response.text
    root = Path(os.environ["DATA_DIR"]) / "jobs" / current["slug"]
    exported = [json.loads(line) for line in (root / "dataset/manifest.jsonl").read_text().splitlines()]
    record = next(r for r in exported if r["id"].endswith("/" + shot["code"]))
    assert record["labels"] == result["labels"]
    artifacts = list((root / "labels").glob("*.json"))
    assert len(artifacts) == 1 and json.loads(artifacts[0].read_text()) == result["labels"]
    assert queue(client, current, {"shot_ids": [shot["id"]], "force": True}).json()["queued"] == 1


def test_failed_and_cancelled_tasks_require_explicit_retry(client, job):
    current, _, saved = job
    selection = {"shot_ids": [saved[0]["id"]]}
    queue(client, current, selection)
    task = claim(client)
    failure = client.post(f"{WORKER}/tasks/{task['id']}/fail", headers=WORKER_HEADERS,
                          json={"worker_id": "test-worker", "lease_token": task["lease_token"], "error": "Quota exhausted"})
    assert failure.status_code == 200
    assert queue(client, current, selection).json()["queued"] == 0
    assert queue(client, current, {**selection, "retry": True}).json()["queued"] == 1
    retried = claim(client)
    assert retried["id"] == task["id"] and retried["lease_token"] != task["lease_token"]
    response = client.post(f"/api/jobs/{current['id']}/tagging/cancel", json=selection)
    assert response.json() == {"cancelled": 1}
    assert complete(client, retried).status_code == 409
    assert queue(client, current, selection).json()["queued"] == 0
    assert queue(client, current, {**selection, "retry": True}).json()["queued"] == 1
    final = claim(client)
    assert complete(client, final).status_code == 200
    assert client.get(f"/api/shots/{saved[0]['id']}").json()["tagging"]["attempts"] == 3


def test_overview_keeps_sides_separate_and_reports_partial_then_complete(client, job):
    current, _, saved = job
    first = saved[0]
    same_side = [s for s in saved if s["segment_key"] == first["segment_key"] and s["side"] == first["side"]]
    assert len(same_side) == 2

    def segment_props():
        data = client.get(f"/api/jobs/{current['id']}/tagging/segments").json()
        assert data["type"] == "FeatureCollection"
        return [f["properties"] for f in data["features"] if f["properties"]["segment_key"] == first["segment_key"]]

    queue(client, current, {"shot_ids": [same_side[0]["id"]]})
    assert complete(client, claim(client)).status_code == 200
    sides = {p["side"]: p for p in segment_props()}
    assert sides[first["side"]]["status"] == "partial"
    opposite = "right" if first["side"] == "left" else "left"
    assert sides[opposite]["status"] == "unlabelled"
    queue(client, current, {"shot_ids": [same_side[1]["id"]]})
    uncertain = labels()
    uncertain["features"][0]["tags"]["surface"] = observation("concrete", 0.65)
    response = complete(client, claim(client), labels=uncertain)
    assert response.json()["needs_review"] is True
    sides = {p["side"]: p for p in segment_props()}
    assert sides[first["side"]]["status"] == "completed"
    assert sides[first["side"]]["needs_review"] == 1
    assert sides[opposite]["completed"] == 0
    summary = client.get(f"/api/jobs/{current['id']}/tagging").json()
    assert summary["counts"]["completed"] == 2 and summary["counts"]["needs_review"] == 1
    assert summary["counts"]["eligible"] == len(saved)
    assert summary["worker"]["online"] is True
    street = next(s for s in summary["streets"] if s["street"] == first["street"])
    assert street["completed"] == 2


def test_expired_lease_is_recovered_and_old_worker_cannot_submit(client, job):
    from app.db import session_scope
    from app.models import TagTask

    current, _, saved = job
    selection = {"shot_ids": [saved[0]["id"]]}
    queue(client, current, selection)
    task = claim(client)
    with session_scope() as session:
        session.get(TagTask, uuid.UUID(task["id"])).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert complete(client, task).status_code == 409
    assert post_event(client, task).status_code == 409
    # A map-only refresh also recovers expired workers; it need not wait for summary polling.
    features = client.get(f"/api/jobs/{current['id']}/tagging/segments").json()["features"]
    affected = next(f["properties"] for f in features
                    if f["properties"]["segment_key"] == saved[0]["segment_key"]
                    and f["properties"]["side"] == saved[0]["side"])
    assert affected["status"] == "failed" and affected["running"] == 0
    summary = client.get(f"/api/jobs/{current['id']}/tagging").json()
    assert summary["counts"]["running"] == 0 and summary["counts"]["failed"] == 1
    assert claim(client) is None  # Expiry does not silently spend another inference.
    assert queue(client, current, {**selection, "retry": True}).json()["queued"] == 1
    next_task = claim(client)
    assert next_task["lease_token"] != task["lease_token"]
    assert complete(client, task).status_code == 409
    assert complete(client, next_task).status_code == 200


def test_recapture_rejects_in_flight_result_and_accepts_new_image(client, job):
    current, _, saved = job
    shot = saved[0]
    selection = {"shot_ids": [shot["id"]]}
    queue(client, current, selection)
    old_task = claim(client)
    updated = capture(client, shot, color=(30, 60, 90))
    assert updated["result"]["sha256"] != old_task["image_sha256"]
    assert complete(client, old_task).status_code == 409
    assert client.get(f"/api/shots/{shot['id']}").json()["labels"] is None
    assert queue(client, current, selection).json()["queued"] == 1
    new_task = claim(client)
    assert new_task["image_sha256"] == updated["result"]["sha256"]
    assert complete(client, new_task).status_code == 200


def test_recapture_hides_old_labels_in_api_overview_and_export(client, job):
    current, _, saved = job
    shot = saved[0]
    queue(client, current, {"shot_ids": [shot["id"]]})
    assert complete(client, claim(client)).status_code == 200
    updated = capture(client, shot, color=(80, 60, 40))
    assert updated["labels"] is None and updated["tagging"] is None
    summary = client.get(f"/api/jobs/{current['id']}/tagging").json()
    assert summary["counts"]["completed"] == 0
    assert client.post(f"/api/jobs/{current['id']}/dataset").status_code == 200
    manifest = Path(os.environ["DATA_DIR"]) / "jobs" / current["slug"] / "dataset/manifest.jsonl"
    record = next(json.loads(line) for line in manifest.read_text().splitlines() if json.loads(line)["id"].endswith("/" + shot["code"]))
    assert record["labels"] is None


@pytest.mark.parametrize("running", [False, True])
def test_changed_active_photo_can_be_requeued_without_waiting_for_old_worker(client, job, running):
    current, _, saved = job
    shot = saved[0]
    selection = {"shot_ids": [shot["id"]]}
    queue(client, current, selection)
    old_task = claim(client) if running else None
    updated = capture(client, shot, color=(15, 45, 75))
    assert queue(client, current, selection).json()["queued"] == 1
    task = claim(client)
    assert task["image_sha256"] == updated["result"]["sha256"]
    if old_task:
        assert complete(client, old_task).status_code == 409
    assert complete(client, task).status_code == 200


def test_replan_preserves_only_current_completed_observations_without_new_inference(client, job):
    from app.carry_over import carry_over
    from app.db import session_scope
    from app.main import add_job
    from app.models import Job
    from app.shots import JobOptions

    current, _, saved = job
    completed, stale, running, queued = saved[:4]
    queue(client, current, {"shot_ids": [completed["id"]]})
    complete_task = claim(client)
    low_confidence = labels()
    low_confidence["features"][0]["tags"]["surface"] = observation("concrete", 0.60)
    assert complete(client, complete_task, labels=low_confidence).status_code == 200
    queue(client, current, {"shot_ids": [stale["id"]]})
    assert complete(client, claim(client)).status_code == 200
    capture(client, stale, color=(200, 100, 50))
    queue(client, current, {"shot_ids": [running["id"]]})
    running_task = claim(client)
    queue(client, current, {"shot_ids": [queued["id"]]})
    source_before = {s["id"]: client.get(f"/api/shots/{s['id']}").json() for s in saved[:4]}

    with session_scope() as session:
        source = session.get(Job, uuid.UUID(current["id"]))
        destination, _ = add_job(session, "Tagging carried over", source.streets, JobOptions(spacing_m=30))
        report = carry_over(session, source, destination)
        copied_id, copied_slug = str(destination.id), destination.slug
    assert report["copied"] == len(saved)
    destination_shots = client.get(f"/api/jobs/{copied_id}/shots?limit=100").json()["items"]
    by_code = {s["code"]: s for s in destination_shots}
    copied = by_code[completed["code"]]
    assert copied["tagging"]["status"] == "completed" and copied["tagging"]["needs_review"] is True
    assert copied["tagging"]["attempts"] == 1
    assert copied["confidence"] == source_before[completed["id"]]["confidence"]
    assert copied["confidence"]["minimum"] == 0.60
    old_labels = source_before[completed["id"]]["labels"]
    assert copied["labels"]["features"] == old_labels["features"]
    provenance = copied["labels"]["provenance"]
    assert provenance["tagged_at"] == old_labels["provenance"]["tagged_at"]
    assert provenance["model"] == old_labels["provenance"]["model"]
    assert provenance["copied_from_task_id"] == complete_task["id"]
    assert provenance["original_task_id"] == complete_task["id"]
    assert provenance["original_shot_id"] == completed["id"]
    assert provenance["task_id"] != complete_task["id"]
    assert provenance["shot_id"] == copied["id"]
    assert provenance["segment_key"] == copied["segment_key"] and provenance["side"] == copied["side"]
    for source_shot in (stale, running, queued):
        assert by_code[source_shot["code"]]["labels"] is None
        assert by_code[source_shot["code"]]["tagging"] is None
    summary = client.get(f"/api/jobs/{copied_id}/tagging").json()["counts"]
    assert summary["completed"] == 1 and summary["needs_review"] == 1
    assert summary["queued"] == 0 and summary["running"] == 0
    assert queue(client, {"id": copied_id}, {"shot_ids": [copied["id"]]}).json() == {"queued": 0, "skipped": 1}
    artifacts = list((Path(os.environ["DATA_DIR"]) / "jobs" / copied_slug / "labels").glob("*.json"))
    assert len(artifacts) == 1 and json.loads(artifacts[0].read_text()) == copied["labels"]
    for source_shot in saved[:4]:
        assert client.get(f"/api/shots/{source_shot['id']}").json() == source_before[source_shot["id"]]
    # The source worker still owns its original lease, unaffected by copying.
    assert complete(client, running_task).status_code == 200
    with session_scope() as session:
        report = carry_over(session, session.get(Job, uuid.UUID(current["id"])), session.get(Job, uuid.UUID(copied_id)))
    assert report["copied"] == 0


def test_confidence_aggregates_per_image_and_excludes_recaptured_results(client, job):
    current, _, saved = job
    same_street = [s for s in saved if s["street"] == saved[0]["street"]][:3]
    assert len(same_street) == 3
    observed = labels()
    observed["footpath"]["confidence"] = 0.8
    for tag in observed["features"][0]["tags"].values():
        tag["confidence"] = 0.8
    absent = {"schema_version": "maply-accessibility/1", "footpath": observation("absent", 0.4), "features": [], "notes": [], "review_reasons": []}
    unknown = {**absent, "footpath": {**observation("unknown", None), "visibility": "occluded", "evidence": "Route is hidden by a parked truck"}}
    for shot, result in zip(same_street, [observed, absent, unknown]):
        queue(client, current, {"shot_ids": [shot["id"]]})
        assert complete(client, claim(client), labels=result).status_code == 200
    summary = client.get(f"/api/jobs/{current['id']}/tagging").json()
    for metrics in [summary["confidence"], next(s for s in summary["streets"] if s["street"] == saved[0]["street"])["confidence"]]:
        assert metrics["mean"] == pytest.approx(0.6)
        assert metrics["minimum"] == 0.4
        assert metrics["known"] == 6 and metrics["unknown"] == 1 and metrics["images"] == 3
    light = client.get(f"/api/jobs/{current['id']}/shots?include_labels=false&tagging_status=completed").json()["items"]
    by_id = {shot["id"]: shot for shot in light}
    assert all(shot["labels"] is None for shot in light)
    assert by_id[same_street[0]["id"]]["confidence"]["mean"] == 0.8
    assert by_id[same_street[2]["id"]]["confidence"] == {"mean": None, "minimum": None, "known": 0, "unknown": 1}
    updated = capture(client, same_street[0], color=(70, 80, 90))
    assert updated["confidence"] is None
    metrics = client.get(f"/api/jobs/{current['id']}/tagging").json()["confidence"]
    assert metrics == {"mean": 0.4, "minimum": 0.4, "known": 1, "unknown": 1, "images": 2}


def post_event(client, task, **overrides):
    return client.post(f"{WORKER}/tasks/{task['id']}/events", headers=WORKER_HEADERS, json={
        "worker_id": "test-worker", "lease_token": task["lease_token"],
        "kind": "stage", "message": "Preparing image", "data": {"stage": "preparing"}, **overrides,
    })


def activity(client, job, query=""):
    response = client.get(f"/api/jobs/{job['id']}/tagging/activity{query}")
    assert response.status_code == 200, response.text
    return response.json()


def test_events_require_current_lease_and_retries_show_only_current_attempt(client, job):
    current, _, saved = job
    selection = {"shot_ids": [saved[0]["id"]]}
    queue(client, current, selection)
    first = claim(client)
    assert post_event(client, first, worker_id="wrong-worker").status_code == 409
    assert post_event(client, first, lease_token="wrong-token").status_code == 409
    assert post_event(client, first, message="First attempt preparation").status_code == 200
    item = activity(client, current)["items"][0]
    assert item["status"] == "running" and item["started_at"] is not None
    assert item["queue_position"] is None
    assert any(e["message"] == "First attempt preparation" for e in item["events"])
    failure = client.post(f"{WORKER}/tasks/{first['id']}/fail", headers=WORKER_HEADERS, json={
        "worker_id": "test-worker", "lease_token": first["lease_token"], "error": "Temporary provider error",
    })
    assert failure.status_code == 200
    assert activity(client, current)["items"][0]["events"][-1]["kind"] == "failed"
    assert post_event(client, first).status_code == 409

    queue(client, current, {**selection, "retry": True})
    assert activity(client, current)["items"][0]["events"] == []
    second = claim(client)
    assert post_event(client, first).status_code == 409
    assert post_event(client, second, message="Second attempt preparation").status_code == 200
    events = activity(client, current)["items"][0]["events"]
    assert any(e["message"] == "Second attempt preparation" for e in events)
    assert not any(e["message"] == "First attempt preparation" or e["kind"] == "failed" for e in events)
    assert client.post(f"/api/jobs/{current['id']}/tagging/cancel", json=selection).status_code == 200
    assert activity(client, current)["items"][0]["events"][-1]["kind"] == "cancelled"
    assert post_event(client, second).status_code == 409

    queue(client, current, {**selection, "retry": True})
    third = claim(client)
    assert complete(client, third).status_code == 200
    item = activity(client, current)["items"][0]
    assert item["status"] == "completed" and item["attempts"] == 3
    assert item["events"][-1]["kind"] == "completed"
    assert not any(e["kind"] in {"cancelled", "failed"} for e in item["events"])
    assert item["confidence"]["minimum"] == 0.93


def test_activity_event_payloads_are_bounded_and_exclude_hidden_reasoning(client, job):
    current, _, saved = job
    queue(client, current, {"shot_ids": [saved[0]["id"]]})
    task = claim(client)
    invalid = [
        {"kind": "thinking"},
        {"data": {"reasoning": "private internal content"}},
        {"data": {"tool": {"reasoning": "private internal content"}}},
        {"data": {"usage": {"thinking": "private internal content"}}},
        {"data": {"final": {"thinking": "private internal content"}}},
        {"message": "x" * 501},
        {"data": {"output_text": "x" * 2001}},
        {"data": {"usage": {"input_tokens": -1}}},
    ]
    for payload in invalid:
        response = post_event(client, task, **payload)
        assert response.status_code == 422, response.text
    assert post_event(client, task, kind="tool", message="Image opened", data={"tool": "view_image"}).status_code == 200
    assert post_event(client, task, kind="usage", message="Usage reported", data={"usage": {
        "input_tokens": 123, "output_tokens": 45, "thinking_tokens": 8, "total_tokens": 176,
    }}).status_code == 200
    assert post_event(client, task, kind="output", message="Output received", data={"output_text": "Visible footpath", "final": True}).status_code == 200
    events = activity(client, current)["items"][0]["events"]
    assert "private internal content" not in json.dumps(events)
    assert [e["kind"] for e in events][-3:] == ["tool", "usage", "output"]
    assert events[-2]["data"]["usage"]["thinking_tokens"] == 8  # Counts, never hidden reasoning text.


def test_activity_scope_queue_positions_pagination_filters_and_stale_images(client, job):
    current, _, saved = job
    other = client.post("/api/jobs", json={"name": "Unrelated activity", "streets": STREETS, "options": {"spacing_m": 30}}).json()
    other_shot = client.get(f"/api/jobs/{other['id']}/shots?limit=1").json()["items"][0]
    capture(client, other_shot)
    queue(client, other, {"shot_ids": [other_shot["id"]]})
    ordered = [saved[2], saved[0], saved[1]]
    for shot in ordered:
        assert queue(client, current, {"shot_ids": [shot["id"]]}).json()["queued"] == 1
    body = activity(client, current, "?status=queued,running&limit=1&offset=1")
    assert body["total"] == 3 and len(body["items"]) == 1
    assert body["items"][0]["shot_id"] == ordered[1]["id"]
    assert body["items"][0]["queue_position"] == 2
    assert body["counts"]["queued"] == 3
    assert activity(client, other)["total"] == 1
    assert other_shot["id"] not in {item["shot_id"] for item in activity(client, current)["items"]}
    for query in ["?limit=101", "?limit=0", "?offset=-1", "?status=invalid"]:
        assert client.get(f"/api/jobs/{current['id']}/tagging/activity{query}").status_code == 422
    assert client.get(f"/api/jobs/{uuid.uuid4()}/tagging/activity").status_code == 404
    assert activity(client, current, "?status=completed")["total"] == 0
    capture(client, ordered[0], color=(75, 25, 15))
    body = activity(client, current)
    assert body["total"] == 2
    assert ordered[0]["id"] not in {item["shot_id"] for item in body["items"]}
    assert [item["queue_position"] for item in body["items"]] == [1, 2]


def test_local_quota_payloads_are_rejected(client, job):
    current, _, _ = job
    r = client.post(f'{WORKER}/heartbeat', headers=WORKER_HEADERS, json={'worker_id': 'old', 'usage': {'status': 'available'}})
    assert r.status_code == 422
    assert 'usage' not in activity(client, current)


@pytest.mark.asyncio
async def test_activity_stream_emits_sse_snapshot_without_waiting_for_a_change(client, job):
    from app.tagging import activity_stream

    current, _, saved = job
    queue(client, current, {"shot_ids": [saved[0]["id"]]})

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    response = await activity_stream(ConnectedRequest(), uuid.UUID(current["id"]), status="queued", limit=10, offset=0)
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    try:
        event = await anext(response.body_iterator)
        assert event.startswith("event: activity\ndata: ") and event.endswith("\n\n")
        payload = json.loads(event.split("data: ", 1)[1])
        assert payload["total"] == 1
        assert payload["items"][0]["shot_id"] == saved[0]["id"]
        assert payload["items"][0]["queue_position"] == 1
    finally:
        await response.body_iterator.aclose()


def test_image_missing_at_claim_produces_terminal_failure_event(client, job):
    current, _, saved = job
    shot = saved[0]
    queue(client, current, {"shot_ids": [shot["id"]]})
    image_path = Path(os.environ["DATA_DIR"]) / "jobs" / current["slug"] / shot["result"]["file"]
    image_path.unlink()
    assert claim(client) is None
    item = activity(client, current)["items"][0]
    assert item["status"] == "failed" and "Image missing" in item["error"]
    assert item["events"][-1]["kind"] == "failed"


def test_parallel_settings_validate_bounds_and_persist(client):
    response = client.patch("/api/tagging/settings", json={"parallel_sessions": 1})
    assert response.status_code == 200
    assert response.json() == {"parallel_sessions": 1, "max_parallel_sessions": 20, "running": 0}
    assert client.get("/api/tagging/settings").json() == response.json()
    for value in [0, 21, -1, 1.5, "2", True, None]:
        invalid = client.patch("/api/tagging/settings", json={"parallel_sessions": value})
        assert invalid.status_code == 422, invalid.text
    assert client.patch("/api/tagging/settings", json={}).status_code == 422
    assert client.patch("/api/tagging/settings", json={"parallel_sessions": 2, "unsupported": True}).status_code == 422
    assert client.get("/api/tagging/settings").json()["parallel_sessions"] == 1
    for value in [9, 20]:
        assert client.patch("/api/tagging/settings", json={"parallel_sessions": value}).status_code == 200
        assert client.get("/api/tagging/settings").json()["parallel_sessions"] == value


@pytest.mark.parametrize("capacity", [1, 3, 8])
def test_concurrent_claims_enforce_global_cap_without_duplicate_leases(client, job, capacity):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.db import session_scope
    from app.main import app
    from app.models import TagTask
    from app.tagging import MODEL

    current, _, saved = job
    other = client.post("/api/jobs", json={
        "name": f"Concurrent second job {capacity}", "streets": STREETS, "options": {"spacing_m": 30},
    }).json()
    other_shots = client.get(f"/api/jobs/{other['id']}/shots?limit=2").json()["items"]
    for shot in other_shots:
        capture(client, shot)
    assert queue(client, other, {"shot_ids": [s["id"] for s in other_shots]}).json()["queued"] == 2
    assert queue(client, current, {"shot_ids": [s["id"] for s in saved]}).json()["queued"] == len(saved)
    assert client.patch("/api/tagging/settings", json={"parallel_sessions": capacity}).status_code == 200
    barrier = Barrier(10)

    def attempt(worker_number):
        # Separate clients give every contender an independent ASGI request/session.
        with TestClient(app, base_url="http://127.0.0.1") as contender:
            barrier.wait(timeout=10)
            response = contender.post(f"{WORKER}/claim", headers=WORKER_HEADERS, json={
                "worker_id": f"concurrent-{worker_number}", "model": MODEL,
            })
            assert response.status_code == 200, response.text
            return response.json()

    with ThreadPoolExecutor(max_workers=10) as executor:
        responses = list(executor.map(attempt, range(10)))
    tasks = [r["task"] for r in responses if r["task"] is not None]
    blocked = [r for r in responses if r["task"] is None]
    assert len(tasks) == capacity
    assert len({t["id"] for t in tasks}) == capacity
    assert len({t["shot_id"] for t in tasks}) == capacity
    assert len({t["lease_token"] for t in tasks}) == capacity
    assert len(blocked) == 10 - capacity and all(r["reason"] == "capacity" for r in blocked)
    assert any(t["shot_id"] in {s["id"] for s in other_shots} for t in tasks)
    if capacity > 2:
        assert any(t["shot_id"] in {s["id"] for s in saved} for t in tasks)
    assert client.get("/api/tagging/settings").json() == {
        "parallel_sessions": capacity, "max_parallel_sessions": 20, "running": capacity,
    }
    with session_scope() as session:
        records = session.scalars(select(TagTask)).all()
        assert sum(t.status == "running" for t in records) == capacity
        assert all(t.attempts == (1 if t.status == "running" else 0) for t in records)
        assert all(t.lease_token is None for t in records if t.status == "queued")


def test_parallel_limit_increases_immediately_and_reductions_drain_without_cancelling(client, job):
    current, _, saved = job
    assert queue(client, current, {"shot_ids": [s["id"] for s in saved[:4]]}).json()["queued"] == 4
    client.patch("/api/tagging/settings", json={"parallel_sessions": 1})
    first = claim(client)
    denied = client.post(f"{WORKER}/claim", headers=WORKER_HEADERS, json={"worker_id": "idle-worker"}).json()
    assert denied == {"task": None, "reason": "capacity"}
    increased = client.patch("/api/tagging/settings", json={"parallel_sessions": 3}).json()
    assert increased["running"] == 1
    second, third = claim(client), claim(client)
    assert second is not None and third is not None
    reduced = client.patch("/api/tagging/settings", json={"parallel_sessions": 1}).json()
    assert reduced == {"parallel_sessions": 1, "max_parallel_sessions": 20, "running": 3}
    assert claim(client) is None
    assert complete(client, first).status_code == 200
    assert client.get("/api/tagging/settings").json()["running"] == 2
    assert claim(client) is None
    assert complete(client, second).status_code == 200
    assert client.get("/api/tagging/settings").json()["running"] == 1
    assert claim(client) is None
    assert complete(client, third).status_code == 200
    fourth = claim(client)
    assert fourth is not None
    assert client.get("/api/tagging/settings").json()["running"] == 1


def test_parallel_limit_is_shared_across_models(client, job):
    from sqlalchemy import select
    from app.db import session_scope
    from app.models import TagTask

    current, _, saved = job
    queue(client, current, {"shot_ids": [s["id"] for s in saved[:2]]})
    with session_scope() as session:
        task = session.scalar(select(TagTask).where(TagTask.shot_id == uuid.UUID(saved[1]["id"])))
        task.model = "other-vision-model"
    client.patch("/api/tagging/settings", json={"parallel_sessions": 1})
    assert claim(client) is not None
    payload = {"worker_id": "other-model-worker", "model": "other-vision-model"}
    blocked = client.post(f"{WORKER}/claim", headers=WORKER_HEADERS, json=payload)
    assert blocked.json() == {"task": None, "reason": "capacity"}
    client.patch("/api/tagging/settings", json={"parallel_sessions": 2})
    allowed = client.post(f"{WORKER}/claim", headers=WORKER_HEADERS, json=payload).json()["task"]
    assert allowed["model"] == "google/gemini-3.1-flash-lite" and allowed["shot_id"] == saved[1]["id"]
    assert client.get("/api/tagging/settings").json()["running"] == 2


def test_expired_lease_releases_global_capacity(client, job):
    from app.db import session_scope
    from app.models import TagTask

    current, _, saved = job
    queue(client, current, {"shot_ids": [s["id"] for s in saved[:2]]})
    client.patch("/api/tagging/settings", json={"parallel_sessions": 1})
    expired = claim(client)
    with session_scope() as session:
        session.get(TagTask, uuid.UUID(expired["id"])).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    next_task = claim(client)
    assert next_task is not None and next_task["id"] != expired["id"]
    assert client.get("/api/tagging/settings").json()["running"] == 1
    assert complete(client, expired).status_code == 409
    assert complete(client, next_task).status_code == 200


def test_model_switch_pins_inflight_and_routes_existing_queue(client, job, monkeypatch):
    from app import tagging
    current, _, saved = job
    queue(client, current, {'shot_ids': [saved[0]['id'], saved[1]['id']]})
    config = {'provider': 'openrouter', 'openrouter_model': 'google/gemini-3.1-flash-lite', 'api_key': 'private-test-key'}
    monkeypatch.setattr(tagging, 'provider_settings', lambda: dict(config))
    first = claim(client)
    config['openrouter_model'] = 'other/vision'
    second = claim(client)
    assert first['model'] == 'google/gemini-3.1-flash-lite' and second['model'] == 'other/vision'
    assert first['provider'] == second['provider'] == 'openrouter'
    assert complete(client, first).status_code == 200
    assert complete(client, second).status_code == 200
    for task in [first, second]:
        observed = client.get(f"/api/shots/{task['shot_id']}").json()['labels']
        assert observed['provenance']['provider'] == 'openrouter'
        assert 'private-test-key' not in json.dumps(observed)


@pytest.mark.parametrize('uncertain', [False, True])
def test_app_managed_openrouter_tags_and_routes_uncertainty_to_backlog(client, job, monkeypatch, uncertain):
    from app import openrouter_worker, tagging
    current, _, saved = job
    queue(client, current, {'shot_ids': [saved[0]['id']]})
    config = {'provider': 'openrouter', 'openrouter_model': 'test/vision', 'api_key': 'test-key'}
    monkeypatch.setattr(openrouter_worker, 'load_settings', lambda: dict(config))
    monkeypatch.setattr(tagging, 'provider_settings', lambda: dict(config))
    annotation = labels() if not uncertain else {
        'schema_version': 'maply-accessibility/1', 'footpath': {
            'value': 'unknown', 'confidence': None, 'visibility': 'occluded', 'evidence': 'A truck obscures the target route'},
        'features': [], 'notes': [], 'review_reasons': ['Please inspect this image manually']}
    requests = []
    def annotate(path, prompt, schema, model, **kwargs):
        requests.append(model)
        return annotation
    monkeypatch.setattr(openrouter_worker, 'annotate_openrouter', annotate)
    assert openrouter_worker.run_one('test-app-service')
    result = client.get(f"/api/shots/{saved[0]['id']}").json()
    assert result['tagging']['status'] == 'completed'
    assert result['labels']['provenance']['provider'] == 'openrouter'
    assert requests == ['test/vision']
    review = client.get(f"/api/jobs/{current['id']}/shots?manual_backlog=true").json()
    assert review['total'] == (1 if uncertain else 0)
    if uncertain:
        assert result['status'] == 'needs_check' and 'manual_review' in result['flags']
        assert client.patch(f"/api/shots/{saved[0]['id']}", json={'status': 'accepted'}).status_code == 200
        assert client.get(f"/api/jobs/{current['id']}/shots?manual_backlog=true").json()['total'] == 0


@pytest.mark.asyncio
async def test_app_tagger_respects_concurrency_and_drains_on_shutdown(monkeypatch):
    import asyncio
    import threading
    from app import openrouter_worker
    lock, started, release = threading.Lock(), threading.Event(), threading.Event()
    active = peak = 0
    identities = set()
    def run_one(identity):
        nonlocal active, peak
        with lock:
            identities.add(identity)
            active += 1
            peak = max(peak, active)
            if active == 2:
                started.set()
        release.wait(5)
        with lock:
            active -= 1
        return True
    monkeypatch.setattr(openrouter_worker, 'run_one', run_one)
    monkeypatch.setattr(openrouter_worker, 'capacity', lambda identity: (2, 10))
    monkeypatch.setattr(openrouter_worker, 'load_settings', lambda: {'api_key': 'test-key'})
    stop = asyncio.Event()
    manager = asyncio.create_task(openrouter_worker.serve(stop))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        stop.set()
        await asyncio.sleep(.05)
        assert not manager.done()
        release.set()
        await asyncio.wait_for(manager, 2)
        assert peak == 2 and len(identities) == 2 and active == 0
    finally:
        stop.set()
        release.set()
        await manager
