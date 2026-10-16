"""Optional adapter for Google's official Street View Static API.

Only usable when the project permits `streetview_static_api`, the target is
`google_maps` and GOOGLE_MAPS_API_KEY is configured. Uses the documented
endpoints (developers.google.com/maps/documentation/streetview):

* metadata: https://maps.googleapis.com/maps/api/streetview/metadata  (no quota cost)
* image:    https://maps.googleapis.com/maps/api/streetview

The metadata response supplies the panorama ID, camera location, capture date
(``YYYY-MM``) and copyright, which are recorded with provenance
``static_api_metadata``. Requested image size is capped by STATIC_API_MAX_SIZE
(default 640; verify against your plan's limits).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

import httpx

from app.capture.completion import CaptureRecord, ReuseRecord, TaskOutcome
from app.capture.verification import PageState, check_frame, evaluate_capture
from app.config import Settings, get_settings
from app.storage import files
from app.workers.base import CaptureDriver, ManualInterventionRequired, NoImagery, RetryableTaskError, TaskContext

BASE = "https://maps.googleapis.com/maps/api/streetview"


def sign_url(url: str, secret_b64: str) -> str:
    """URL signing as documented for Maps Static APIs (HMAC-SHA1 over path+query)."""
    u = urlparse(url)
    to_sign = (u.path + "?" + u.query).encode()
    key = base64.urlsafe_b64decode(secret_b64)
    sig = base64.urlsafe_b64encode(hmac.new(key, to_sign, hashlib.sha1).digest()).decode()
    return f"{url}&signature={sig}"


class StaticApiDriver(CaptureDriver):
    method = "streetview_static_api"

    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or get_settings()
        if not self.settings.google_maps_api_key:
            raise RuntimeError("GOOGLE_MAPS_API_KEY not configured")
        self.client = httpx.AsyncClient(timeout=30.0, transport=transport)

    def _url(self, path: str, params: dict) -> str:
        params = {**params, "key": self.settings.google_maps_api_key}
        url = f"{BASE}{path}?{urlencode(params)}"
        if self.settings.google_maps_url_signing_secret:
            url = sign_url(url, self.settings.google_maps_url_signing_secret)
        return url

    @staticmethod
    def _redact(url: str) -> str:
        return url.split("&key=")[0] + "&key=REDACTED"

    async def collect(self, ctx: TaskContext) -> TaskOutcome:
        await ctx.runtime.acquire_request_slot()
        # Only documented metadata parameters (location + key [+ signature]).
        # Distance from the requested sample is enforced below from the returned location.
        meta_url = self._url("/metadata", {"location": f"{ctx.sample.lat:.7f},{ctx.sample.lng:.7f}"})
        r = await self.client.get(meta_url)
        if r.status_code != 200:
            raise RetryableTaskError(f"metadata HTTP {r.status_code}")
        meta = r.json()
        status = meta.get("status")
        if status in ("ZERO_RESULTS", "NOT_FOUND"):  # per docs: no panorama near the location
            raise NoImagery(f"Static API metadata status {status}", {"metadata": meta})
        if status in ("OVER_QUERY_LIMIT", "REQUEST_DENIED", "INVALID_REQUEST"):
            raise ManualInterventionRequired(f"Static API status {status}: {meta.get('error_message', '')}", "api")
        if status != "OK":
            raise RetryableTaskError(f"metadata status {status}")
        loc = meta.get("location") or {}
        if loc.get("lat") is not None:
            from app.geo.projection import geodesic_distance

            d = geodesic_distance(ctx.sample.lng, ctx.sample.lat, loc["lng"], loc["lat"])
            if d > ctx.policy.no_imagery_distance_m:
                raise NoImagery(f"nearest panorama is {d:.0f} m away (> {ctx.policy.no_imagery_distance_m:.0f} m)", {"metadata": meta})
        date = meta.get("date")
        precision = "month" if date and len(date) == 7 else ("year" if date and len(date) == 4 else "unknown")
        outcome = TaskOutcome(panorama={"pano_id": meta.get("pano_id"), "camera_lat": loc.get("lat"), "camera_lng": loc.get("lng"), "imagery_date": date})
        for view in ctx.views:
            await ctx.runtime.checkpoint()
            if ctx.policy_reuse and meta.get("pano_id"):
                cand = await ctx.runtime.find_reusable(meta["pano_id"], view)
                if cand:
                    close = cand.camera_to_sample_m is not None and cand.camera_to_sample_m <= ctx.policy.max_camera_offset_m
                    outcome.reuses.append(ReuseRecord(view.id, cand.observation_id, cand.camera_to_sample_m, cand.heading_delta_deg, "geometry_consistent" if close else "uncertain"))
                    continue
            cap = self.settings.static_api_max_size
            scale = min(1.0, cap / view.viewport_w, cap / view.viewport_h)
            w, h = max(1, int(view.viewport_w * scale)), max(1, int(view.viewport_h * scale))
            await ctx.runtime.acquire_request_slot()
            img_url = self._url(
                "",
                {
                    "size": f"{w}x{h}",
                    "pano": meta["pano_id"],
                    "heading": f"{view.heading:.2f}",
                    "pitch": f"{view.pitch:.2f}",
                    "fov": f"{view.fov:.1f}",
                    "return_error_code": "true",
                },
            )
            ir = await self.client.get(img_url)
            if ir.status_code != 200 or not ir.headers.get("content-type", "").startswith("image/"):
                outcome.missing_views.append({"view_id": view.id, "reason": f"image HTTP {ir.status_code}"})
                continue
            data = ir.content
            st = PageState(
                mode="street_view",
                url=self._redact(img_url),
                loaded=True,
                camera_lat=loc.get("lat"),
                camera_lng=loc.get("lng"),
                camera_provenance="static_api_metadata",
                heading=view.heading,
                heading_provenance="requested_static_api",
                pitch=view.pitch,
                pitch_provenance="requested_static_api",
                fov=view.fov,
                fov_provenance="requested_static_api",
                pano_id=meta.get("pano_id"),
                pano_id_provenance="static_api_metadata",
                imagery_date=date,
                imagery_date_precision=precision,
                imagery_date_provenance="static_api_metadata" if date else "unknown",
                attribution=meta.get("copyright"),
            )
            frame = check_frame(data)
            ev = evaluate_capture(
                state=st, view=view, requested_lat=ctx.sample.lat, requested_lng=ctx.sample.lng,
                segment_bearing=ctx.sample.segment_bearing, frame=frame, policy=ctx.policy,
                in_scope=ctx.in_scope(st.camera_lat, st.camera_lng),
            )
            if scale < 1.0:
                ev.flags.append("static_api_size_capped")
            if ev.verification_status == "rejected":
                outcome.missing_views.append({"view_id": view.id, "reason": ev.reject_reason, "flags": ev.flags})
                continue
            info = files.image_info(data)
            obs_id = uuid.uuid4()
            p = files.image_path(ctx.job.project_id, ctx.job.id, obs_id, info.format or "jpeg")
            sha, n = files.atomic_write_bytes(p, data)
            tp = files.thumb_path(ctx.job.project_id, ctx.job.id, obs_id)
            files.atomic_write_bytes(tp, files.make_thumbnail_bytes(data))
            view_for_record = type(view)(**{**view.__dict__, "viewport_w": info.width, "viewport_h": info.height})
            outcome.captures.append(
                CaptureRecord(
                    observation_id=obs_id, view=view_for_record, image_rel=files.rel(p), image_format=info.format or "jpeg",
                    width=info.width, height=info.height, nbytes=n, sha256=sha, thumb_rel=files.rel(tp), state=st,
                    evaluation=ev, collected_at=datetime.now(timezone.utc), source_url=self._redact(img_url),
                    capture_method="streetview_static_api",
                )
            )
            await ctx.runtime.publish_preview(data, f"static api {view.side}/{view.view_kind}")
        if not outcome.captures and not outcome.reuses:
            raise RetryableTaskError("no Static API image could be captured")
        return outcome

    async def close(self) -> None:
        await self.client.aclose()
