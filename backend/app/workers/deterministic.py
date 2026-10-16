"""Deterministic browser driver: documented URLs, no model.

For each task: navigate to the requested position with the first planned
orientation, resolve the panorama the site actually serves, then visit each
planned orientation on that same panorama, verify, and save.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from app.capture.completion import CaptureRecord, CropRecord, ReuseRecord, TaskOutcome
from app.capture.verification import MANUAL_MODES, PageState, PlannedViewInfo, check_frame, evaluate_capture
from app.geo.projection import geodesic_distance
from app.storage import files
from app.workers.base import (
    CaptureDriver,
    ManualInterventionRequired,
    NoImagery,
    RetryableTaskError,
    TaskContext,
)
from app.workers.browser import BrowserSession
from app.workers.sites.base import SiteAdapter

FRAME_RETRIES = 3
SETTLE_TIMEOUT_S = 25.0
STABILITY_DELAY_S = 0.6


class BrowserDriverBase(CaptureDriver):
    capture_method = "deterministic_browser"

    def __init__(self, session: BrowserSession, site: SiteAdapter):
        self.session = session
        self.site = site

    @property
    def page(self):
        return self.session.page

    async def start(self) -> None:
        await self.session.start()

    async def close(self) -> None:
        await self.session.close()

    # ----------------------------------------------------------------- nav
    async def goto(self, ctx: TaskContext, view: PlannedViewInfo, pano_id: str | None = None, fault: str | None = None) -> PageState:
        await ctx.runtime.acquire_request_slot()
        url = self.site.view_url(ctx.sample.lat, ctx.sample.lng, view.heading, view.pitch, view.fov, pano_id, fault)
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:  # noqa: BLE001
            raise RetryableTaskError(f"navigation failed: {e}") from e
        st = await self.site.wait_settled(self.page, SETTLE_TIMEOUT_S)
        await self._preview(ctx, f"{st.mode} after navigation")
        return st

    async def _preview(self, ctx: TaskContext, note: str = "") -> None:
        try:
            await ctx.runtime.publish_preview(await self.session.screenshot(), note)
        except Exception:  # noqa: BLE001
            pass

    def raise_for_blocking(self, st: PageState) -> None:
        if st.mode in MANUAL_MODES:
            raise ManualInterventionRequired(st.message or f"{st.mode} page requires an operator", st.mode)
        if not self.session.top_level_allowed():
            raise ManualInterventionRequired(f"browser left the permitted site ({self.page.url})", "blocked")

    def no_imagery_check(self, ctx: TaskContext, st: PageState) -> None:
        if st.mode in ("no_imagery", "map"):
            raise NoImagery(st.message or "no imagery at requested position", {"page_state": st.to_dict()})
        if st.mode == "street_view" and st.camera_lat is not None:
            d = geodesic_distance(ctx.sample.lng, ctx.sample.lat, st.camera_lng, st.camera_lat)
            if d > ctx.policy.no_imagery_distance_m:
                raise NoImagery(f"nearest panorama is {d:.0f} m away (> {ctx.policy.no_imagery_distance_m:.0f} m)", {"page_state": st.to_dict()})

    # ------------------------------------------------------------- capture
    async def capture_current(self, ctx: TaskContext, view: PlannedViewInfo, method: str) -> tuple[CaptureRecord | None, dict]:
        first = await self.session.screenshot()
        await asyncio.sleep(STABILITY_DELAY_S)
        shot = await self.session.screenshot()
        st = await self.site.read_state(self.page)
        frame = check_frame(shot, second=first)
        in_scope = ctx.in_scope(st.camera_lat, st.camera_lng)
        ev = evaluate_capture(
            state=st,
            view=view,
            requested_lat=ctx.sample.lat,
            requested_lng=ctx.sample.lng,
            segment_bearing=ctx.sample.segment_bearing,
            frame=frame,
            policy=ctx.policy,
            in_scope=in_scope,
        )
        if ev.verification_status == "rejected":
            # Out-of-scope or invalid frames are never written to the dataset.
            return None, {"reason": ev.reject_reason, "flags": ev.flags, "frame": frame.stats}
        obs_id = uuid.uuid4()
        info = files.image_info(shot)
        img_path = files.image_path(ctx.job.project_id, ctx.job.id, obs_id, info.format or "png")
        sha, n = files.atomic_write_bytes(img_path, shot)
        th_path = files.thumb_path(ctx.job.project_id, ctx.job.id, obs_id)
        files.atomic_write_bytes(th_path, files.make_thumbnail_bytes(shot))
        crop = None
        crop_cfg = ctx.job.settings.get("analysis_crop")
        if crop_cfg:
            try:
                cb = files.crop_bytes(shot, int(crop_cfg["x"]), int(crop_cfg["y"]), int(crop_cfg["width"]), int(crop_cfg["height"]))
                cid = uuid.uuid4()
                cp = files.crop_path(ctx.job.project_id, ctx.job.id, cid)
                csha, _ = files.atomic_write_bytes(cp, cb)
                crop = CropRecord(cid, int(crop_cfg["x"]), int(crop_cfg["y"]), int(crop_cfg["width"]), int(crop_cfg["height"]), crop_cfg.get("purpose", "analysis"), files.rel(cp), csha)
            except Exception as e:  # noqa: BLE001
                ev.flags.append("analysis_crop_failed")
                ev.evidence["crop_error"] = str(e)
        rec = CaptureRecord(
            observation_id=obs_id,
            view=view,
            image_rel=files.rel(img_path),
            image_format=info.format or "png",
            width=info.width,
            height=info.height,
            nbytes=n,
            sha256=sha,
            thumb_rel=files.rel(th_path),
            state=st,
            evaluation=ev,
            collected_at=datetime.now(timezone.utc),
            source_url=st.url,
            capture_method=method,
            crop=crop,
        )
        await ctx.runtime.publish_preview(shot, f"captured {view.side}/{view.view_kind} → {ev.verification_status}")
        return rec, {}

    async def reuse_or_none(self, ctx: TaskContext, pano_id: str | None, view: PlannedViewInfo) -> ReuseRecord | None:
        if not pano_id or not ctx.policy_reuse:
            return None
        cand = await ctx.runtime.find_reusable(pano_id, view)
        if cand is None:
            return None
        close = cand.camera_to_sample_m is not None and cand.camera_to_sample_m <= ctx.policy.max_camera_offset_m
        return ReuseRecord(
            view_id=view.id,
            observation_id=cand.observation_id,
            camera_to_sample_m=cand.camera_to_sample_m,
            heading_delta_deg=cand.heading_delta_deg,
            association_status="geometry_consistent" if close else "uncertain",
        )

    async def capture_views(self, ctx: TaskContext, first_state: PageState, method: str, outcome: TaskOutcome) -> TaskOutcome:
        pano = first_state.pano_id
        outcome.panorama = {
            "pano_id": pano,
            "pano_id_provenance": first_state.pano_id_provenance,
            "camera_lat": first_state.camera_lat,
            "camera_lng": first_state.camera_lng,
            "imagery_date": first_state.imagery_date,
            "imagery_date_precision": first_state.imagery_date_precision,
        }
        for idx, view in enumerate(ctx.views):
            await ctx.runtime.checkpoint()
            reuse = await self.reuse_or_none(ctx, pano, view)
            if reuse:
                outcome.reuses.append(reuse)
                continue
            rec, last_reject = None, {}
            for attempt in range(FRAME_RETRIES):
                if not (idx == 0 and attempt == 0):
                    st = await self.goto(ctx, view, pano_id=pano)
                    self.raise_for_blocking(st)
                    if st.mode != "street_view":
                        last_reject = {"reason": f"page state {st.mode}", "message": st.message}
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                rec, last_reject = await self.capture_current(ctx, view, method)
                if rec:
                    break
                outcome.evidence.setdefault("rejected_frames", []).append({"view_id": view.id, "attempt": attempt + 1, **last_reject})
                if "camera_outside_scope" in last_reject.get("flags", []):
                    break  # never retry into out-of-scope imagery
                await asyncio.sleep(0.5 * (attempt + 1))
            if rec:
                outcome.captures.append(rec)
            else:
                outcome.missing_views.append({"view_id": view.id, "side": view.side, "view_kind": view.view_kind, **last_reject})
        outcome.evidence.setdefault("blocked_hosts", dict(self.session.blocked_hosts))
        if not outcome.captures and not outcome.reuses:
            reasons = "; ".join(str(m.get("reason")) for m in outcome.missing_views)
            raise RetryableTaskError(f"no planned view could be captured: {reasons}")
        return outcome

    # ----------------------------------------------------- operator actions
    async def operator_action(self, cmd: dict) -> dict:
        from app.workers.actions import ActionPolicy, execute_action, validate_action

        name, payload = cmd["command"], cmd.get("payload") or {}
        w, h = self.session.viewport
        policy = ActionPolicy(viewport_w=w, viewport_h=h)
        mapping = {
            "click": {"type": "click", "x": payload.get("x"), "y": payload.get("y"), "button": "left"},
            "double_click": {"type": "double_click", "x": payload.get("x"), "y": payload.get("y")},
            "drag": {"type": "drag", "path": payload.get("path") or []},
            "key": {"type": "keypress", "keys": [payload.get("key", "")]},
            "scroll": {"type": "scroll", "x": payload.get("x", w // 2), "y": payload.get("y", h // 2), "scroll_x": payload.get("dx", 0), "scroll_y": payload.get("dy", 0)},
        }
        if name == "refresh":
            return {"ok": True}
        if name not in mapping:
            return {"ok": False, "error": f"unknown command {name}"}
        v = validate_action(mapping[name], policy)
        if not v.ok:
            return {"ok": False, "error": v.reason}
        await execute_action(self.page, v.action, policy)
        if not self.session.top_level_allowed():
            await self.page.go_back()
            return {"ok": False, "error": "navigation outside permitted site was reverted"}
        return {"ok": True}


class DeterministicBrowserDriver(BrowserDriverBase):
    method = "deterministic_browser"
    capture_method = "deterministic_browser"

    async def collect(self, ctx: TaskContext) -> TaskOutcome:
        outcome = TaskOutcome()
        first = ctx.views[0]
        st = await self.goto(ctx, first, fault=ctx.mock_fault(first_navigation=True))
        for _ in range(2):
            self.raise_for_blocking(st)
            if st.mode == "street_view" and st.loaded:
                break
            if st.mode in ("no_imagery", "map"):
                break
            outcome.evidence.setdefault("navigation_retries", []).append({"mode": st.mode, "message": st.message})
            await asyncio.sleep(1.0)
            st = await self.goto(ctx, first)
        self.raise_for_blocking(st)
        self.no_imagery_check(ctx, st)
        if st.mode != "street_view" or not st.loaded:
            raise RetryableTaskError(f"site did not reach a loaded Street View state (mode={st.mode}: {st.message})")
        return await self.capture_views(ctx, st, self.capture_method, outcome)
