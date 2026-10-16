"""Manual mode: an operator drives the local browser from the dashboard.

The worker navigates to the planned position, publishes a live preview, and
executes operator commands (validated by the same action policy as agents).
Captures still go through the same verification and persistence path.
"""

from __future__ import annotations

import time

from app.capture.completion import TaskOutcome
from app.workers.base import NoImagery, TaskContext, TaskSkipped
from app.workers.deterministic import BrowserDriverBase

IDLE_TIMEOUT_S = 30 * 60


class ManualDriver(BrowserDriverBase):
    method = "manual"
    capture_method = "manual"

    async def collect(self, ctx: TaskContext) -> TaskOutcome:
        outcome = TaskOutcome()
        views = {v.id: v for v in ctx.views}
        st = await self.goto(ctx, ctx.views[0])
        pano = st.pano_id
        outcome.panorama = {"pano_id": pano, "camera_lat": st.camera_lat, "camera_lng": st.camera_lng}
        done: set[str] = set()
        await ctx.runtime.set_status(
            "awaiting_operator",
            f"Manual capture for sample {ctx.sample.ordinal} ({len(views)} planned views). Use the dashboard controls.",
        )
        last = time.monotonic()
        while True:
            await ctx.runtime.checkpoint()
            cmd = await ctx.runtime.next_command(timeout_s=1.0)
            if cmd is None:
                if time.monotonic() - last > IDLE_TIMEOUT_S:
                    raise TaskSkipped("manual task idle timeout")
                continue
            last = time.monotonic()
            name, payload = cmd["command"], cmd.get("payload") or {}
            result: dict = {"ok": True}
            try:
                if name in ("click", "double_click", "drag", "key", "scroll", "refresh"):
                    result = await self.operator_action(cmd)
                elif name == "goto_view":
                    v = views.get(str(payload.get("view_id")))
                    if not v:
                        result = {"ok": False, "error": "unknown view"}
                    else:
                        st = await self.goto(ctx, v, pano_id=pano)
                        pano = pano or st.pano_id
                elif name == "capture":
                    v = views.get(str(payload.get("view_id")))
                    if not v:
                        result = {"ok": False, "error": "unknown view"}
                    else:
                        rec, rej = await self.capture_current(ctx, v, "manual")
                        if rec:
                            outcome.captures = [c for c in outcome.captures if c.view.id != v.id] + [rec]
                            done.add(v.id)
                            result = {"ok": True, "status": rec.evaluation.verification_status, "flags": rec.evaluation.flags}
                        else:
                            result = {"ok": False, "error": rej.get("reason"), "flags": rej.get("flags")}
                elif name == "no_imagery":
                    await ctx.runtime.finish_command(cmd["id"], "done", {"ok": True})
                    raise NoImagery(payload.get("reason") or "operator: no usable imagery")
                elif name == "skip":
                    await ctx.runtime.finish_command(cmd["id"], "done", {"ok": True})
                    raise TaskSkipped(payload.get("reason") or "skipped by operator")
                elif name == "done":
                    await ctx.runtime.finish_command(cmd["id"], "done", {"ok": True})
                    break
                else:
                    result = {"ok": False, "error": f"unsupported command {name}"}
            except (NoImagery, TaskSkipped):
                raise
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "error": str(e)}
            await self._preview(ctx, f"operator: {name}")
            await ctx.runtime.finish_command(cmd["id"], "done" if result.get("ok") else "failed", result)
        for v in ctx.views:
            if v.id not in done:
                outcome.missing_views.append({"view_id": v.id, "side": v.side, "view_kind": v.view_kind, "reason": "not captured by operator"})
        await ctx.runtime.set_status("running", None)
        return outcome
