"""The extension's loop against the real API: next -> capture -> mark -> dataset."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

GEORGE = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": "George Street", "highway": "primary", "osm_way_ids": [1], "osm_tags": {"sidewalk": "both"}}, "geometry": {"type": "LineString", "coordinates": [[151.20668, -33.87501], [151.20690, -33.87360]]}}]}
H = {"X-Maply": "extension"}


@pytest.fixture(scope="module")
def client():
    import psycopg

    url = os.environ["DATABASE_URL"].replace("+psycopg", "")
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=3) as c:
            c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    except Exception:
        pytest.skip("test database not reachable")
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, check=True, capture_output=True)
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, base_url="http://127.0.0.1")


def _jpeg() -> str:
    b = io.BytesIO()
    Image.new("RGB", (1440, 900), (100, 120, 130)).save(b, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


def test_capture_loop_and_dataset(client):
    job = client.post("/api/jobs", json={"name": "George", "streets": GEORGE, "options": {"spacing_m": 30}}).json()
    assert client.get(f"/api/ext/next?job_id={job['id']}").status_code == 403  # needs the extension header
    nxt = client.get(f"/api/ext/next?job_id={job['id']}", headers=H).json()
    shot = nxt["shot"]
    assert shot["ordinal"] == 1 and nxt["progress"]["pending"] == nxt["progress"]["total"]
    # lined up 2 m from the plan, facing 3 degrees right of plan
    url = f"https://www.google.com/maps/@{shot['lat'] + 0.00002:.7f},{shot['lng']:.7f},3a,80y,{shot['heading'] + 3:.2f}h,82t/data=!3m4!1e1!3m2!1sPANO!2e0"
    r = client.post("/api/ext/capture", headers=H, json={"shot_id": shot["id"], "image": _jpeg(), "page_url": url, "image_date_text": "Mar 2024", "device_pixel_ratio": 2})
    body = r.json()
    assert r.status_code == 200 and body["saved"]["status"] == "captured" and body["saved"]["flags"] == []
    assert body["shot"]["ordinal"] == 2 and body["progress"]["captured"] == 1
    saved = client.get(f"/api/shots/{shot['id']}").json()
    assert saved["result"]["image_date_iso"] == "2024-03" and saved["result"]["pano_id"] == "PANO"
    path = Path(os.environ["DATA_DIR"]) / "jobs" / job["slug"] / saved["result"]["file"]
    assert path.is_file() and path.parent.name == "george-street" and path.suffix == ".jpg"
    # mark the next one as no imagery and get the third
    r = client.post("/api/ext/mark", headers=H, json={"shot_id": body["shot"]["id"], "status": "no_imagery"}).json()
    assert r["shot"]["ordinal"] == 3 and r["progress"]["no_imagery"] == 1
    # back: fetch a shot by id
    assert client.get(f"/api/ext/shot/{shot['id']}", headers=H).json()["shot"]["status"] == "captured"
    # map and gallery queries
    b = client.get(f"/api/jobs/{job['id']}").json()["bounds"]
    assert client.get(f"/api/jobs/{job['id']}/shots?bbox={','.join(map(str, b))}").json()["total"] == job["counts"]["total"]
    assert client.get(f"/api/jobs/{job['id']}/shots?status=captured&order=recent").json()["total"] == 1
    # dataset export
    ds = client.post(f"/api/jobs/{job['id']}/dataset").json()
    assert ds["photos"] == 1
    rec = json.loads((Path(os.environ["DATA_DIR"]) / "jobs" / job["slug"] / "dataset" / "manifest.jsonl").read_text().splitlines()[0])
    assert rec["street"]["osm_tags"] == {"sidewalk": "both"} and rec["footpath"]["side"] in ("west", "east")
    assert rec["camera"]["offset_from_plan_m"] < 5 and rec["imagery_date"]["value"] == "2024-03" and rec["labels"] is None
