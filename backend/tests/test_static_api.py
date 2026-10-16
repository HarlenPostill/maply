"""Official Street View Static API adapter against a mock transport (no network)."""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from PIL import Image

from app.capture.verification import CapturePolicy, PlannedViewInfo
from app.config import Settings
from app.workers.base import JobSnapshot, NoImagery, SampleSnapshot, TaskContext
from app.workers.static_api import StaticApiDriver, sign_url


def _jpeg():
    a = np.random.default_rng(3).integers(0, 255, (400, 640, 3)).astype("uint8")
    b = io.BytesIO()
    Image.fromarray(a).save(b, format="JPEG")
    return b.getvalue()


class RT:
    async def checkpoint(self): ...
    async def acquire_request_slot(self): ...
    async def publish_preview(self, png, note=""): ...
    async def find_reusable(self, pano_id, view): return None
    async def event(self, *a, **k): ...


def _ctx():
    claim = SimpleNamespace(task_id=uuid.uuid4(), attempt_no=1)
    job = JobSnapshot(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "google_maps", "streetview_static_api", {}, uuid.uuid4(), 5, False)
    smp = SampleSnapshot(uuid.uuid4(), 1, "street", -33.8700, 151.2120, uuid.uuid4(), "seg_x", 0.0, 0.0)
    views = [PlannedViewInfo(str(uuid.uuid4()), "left", "perpendicular", 270, -5, 80, 1440, 900)]
    return TaskContext(claim=claim, worker_id="w", job=job, sample=smp, views=views, policy=CapturePolicy(), runtime=RT(), scope_check=lambda la, ln: True)


async def test_static_api_metadata_drives_provenance(data_dir):
    seen = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        if req.url.path.endswith("/metadata"):
            return httpx.Response(200, json={"status": "OK", "pano_id": "PANO123", "location": {"lat": -33.87001, "lng": 151.21201}, "date": "2023-08", "copyright": "© Google"})
        return httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"})

    d = StaticApiDriver(Settings(google_maps_api_key="KEY"), transport=httpx.MockTransport(handler))
    out = await d.collect(_ctx())
    c = out.captures[0]
    assert c.state.imagery_date == "2023-08" and c.state.imagery_date_precision == "month" and c.state.imagery_date_provenance == "static_api_metadata"
    assert c.state.camera_provenance == "static_api_metadata" and c.state.heading_provenance == "requested_static_api"
    assert c.width == 640 and c.height == 400 and "static_api_size_capped" in c.evaluation.flags
    assert "KEY" not in c.source_url  # API key never stored
    assert "pano=PANO123" in seen[1] and "size=640x400" in seen[1]
    await d.close()


async def test_static_api_zero_results_is_no_imagery(data_dir):
    d = StaticApiDriver(Settings(google_maps_api_key="KEY"), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ZERO_RESULTS"})))
    with pytest.raises(NoImagery):
        await d.collect(_ctx())
    await d.close()


def test_url_signing_is_hmac_sha1_over_path_and_query():
    import base64
    import hashlib
    import hmac

    url = "https://maps.googleapis.com/maps/api/streetview?size=400x400&pano=P&key=K"
    secret = base64.urlsafe_b64encode(b"test-secret-bytes").decode()
    signed = sign_url(url, secret)
    expected = base64.urlsafe_b64encode(hmac.new(b"test-secret-bytes", b"/maps/api/streetview?size=400x400&pano=P&key=K", hashlib.sha1).digest()).decode()
    assert signed == url + "&signature=" + expected
