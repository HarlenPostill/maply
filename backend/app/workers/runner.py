"""Worker slot: claims tasks, runs a driver, persists results.

Each slot:
* holds a PostgreSQL advisory lock for (job, slot) for its whole life, so a
  slot can never run twice even across processes/hosts, and a crashed
  process's slot is detectably dead (its lock is released with its connection);
* holds a second advisory lock on a browser-profile index, so persistent
  browser profiles are never shared between concurrently running workers;
* heartbeats its worker row and renews its task lease; if the lease is lost
  the driver is stopped at the next checkpoint and its results are discarded.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
import zlib
from dataclasses import dataclass

from geoalchemy2.shape import to_shape
from shapely.geometry import Point
from shapely.prepared import prep
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.capture.completion import ArtifactInvalid, TaskOutcome, complete_task, finalize_task
from app.capture.verification import CapturePolicy, PlannedViewInfo
from app.config import Settings, get_settings
from app.db import get_engine, session_scope
from app.events import emit
from app.geo.projection import heading_delta, to_projected, to_wgs84
from app.models import Job, ModelUsage, Observation, PlannedView, Project, Sample, Segment, Worker
from app.providers.egress import CloudImageTransmissionBlocked
from app.providers.openai_adapter import TurnUsage, estimate_cost_usd
from app.scheduler import jobs as jobsvc
from app.scheduler import queue
from app.schemas import ProjectConfig, effective_limits
from app.storage import files
from app.workers.base import (
    CaptureDriver,
    FatalTaskError,
    JobSnapshot,
    LeaseLost,
    LimitReached,
    ManualInterventionRequired,
    NoImagery,
    ReuseCandidate,
    RetryableTaskError,
    SampleSnapshot,
    TaskContext,
    TaskInterrupted,
    TaskSkipped,
)

log = logging.getLogger("maply.worker")
HOST = os.environ.get("WORKER_HOST_LABEL") or socket.gethostname().split(".")[0][:24]


def _lock_key(namespace: str, n: int) -> tuple[int, int]:
    k1 = zlib.crc32(namespace.encode()) & 0x7FFFFFFF
    return k1, n


def slot_lock_key(job_id: uuid.UUID, slot: int) -> tuple[int, int]:
    return _lock_key(f"slot:{job_id}", slot)


def build_scope_checker(scope_geom, buffer_m: float):
    scope = to_shape(scope_geom)
    if buffer_m > 0:
        scope = to_wgs84(to_projected(scope).buffer(buffer_m))
    prepared = prep(scope)
    return lambda lat, lng: prepared.contains(Point(lng, lat))


def build_driver(job: Job, settings: Settings, profile_index: int) -> CaptureDriver:
    from app.workers.browser import BrowserSession
    from app.workers.sites.google_maps import GoogleMapsSite
    from app.workers.sites.mock_site import MockPanoramaSite

    if job.method == "streetview_static_api":
        from app.workers.static_api import StaticApiDriver

        return StaticApiDriver(settings)
    site = MockPanoramaSite(settings.mock_panorama_base_url) if job.target == "synthetic" else GoogleMapsSite()
    vp = job.settings.get("viewport") or {}
    session = BrowserSession(
        profile_dir=settings.browser_state_dir / job.target / f"profile-{profile_index}",
        viewport=(int(vp.get("width", 1440)), int(vp.get("height", 900))),
        headless=settings.worker_headless,
        site=site,
    )
    if job.method == "deterministic_browser":
        from app.workers.deterministic import DeterministicBrowserDriver

        return DeterministicBrowserDriver(session, site)
    if job.method == "openai_agent":
        from app.workers.openai_agent import OpenAIAgentDriver

        return OpenAIAgentDriver(session, site, settings=settings)
    if job.method == "manual":
        from app.workers.manual import ManualDriver

        return ManualDriver(session, site)
    raise FatalTaskError(f"unknown method {job.method}")


@dataclass
class _Current:
    claim: queue.Claim
    lease_lost: bool = False


class SlotRuntime:
    """Runtime services exposed to a driver for one claimed task."""

    def __init__(self, slot: WorkerSlot, cur: _Current, job: Job):
        self.slot = slot
        self.cur = cur
        self.job = job
        self._status_cache: tuple[float, str] | None = None

    async def _job_status(self) -> str:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._status_cache and now - self._status_cache[0] < 0.75:
            return self._status_cache[1]

        def q():
            with session_scope() as s:
                return s.execute(text("SELECT status FROM jobs WHERE id = :j"), {"j": self.job.id}).scalar_one()

        st = await asyncio.to_thread(q)
        self._status_cache = (now, st)
        return st

    async def checkpoint(self) -> None:
        if self.cur.lease_lost:
            raise LeaseLost("lease lost")
        if self.slot.stopping:
            raise TaskInterrupted("worker stopping")
        st = await self._job_status()
        if st in ("paused", "cancelled"):
            raise TaskInterrupted(f"job {st}")

    async def acquire_request_slot(self) -> None:
        def r():
            with session_scope() as s:
                return queue.reserve_request_slot(s, self.job.id, self.job.requests_per_minute)

        wait = await asyncio.to_thread(r)
        while wait > 0:
            await self.checkpoint()
            step = min(wait, 0.5)
            await asyncio.sleep(step)
            wait -= step
        await self.checkpoint()

    async def publish_preview(self, png: bytes, note: str = "") -> None:
        p = files.preview_path(self.job.project_id, self.job.id, self.slot.worker_id)

        def w():
            files.atomic_write_bytes(p, files.make_preview_bytes(png))
            with session_scope() as s:
                s.execute(
                    text("UPDATE workers SET latest_screenshot_path = :p, latest_screenshot_at = now(), message = COALESCE(:m, message) WHERE id = :w"),
                    {"p": files.rel(p), "m": note or None, "w": self.slot.worker_id},
                )

        await asyncio.to_thread(w)

    async def find_reusable(self, pano_id: str, view: PlannedViewInfo) -> ReuseCandidate | None:
        sample_id = self.slot.current_sample_id

        def q():
            with session_scope() as s:
                rows = s.execute(
                    select(Observation.id, Observation.planned_heading_deg, Observation.camera_geom).where(
                        Observation.job_id == self.job.id,
                        Observation.pano_id == pano_id,
                        Observation.is_current.is_(True),
                        Observation.intended_side == view.side,
                        Observation.view_kind == view.view_kind,
                        Observation.verification_status.in_(("verified", "needs_review")),
                    )
                ).all()
                smp = s.get(Sample, sample_id)
                sp = to_shape(smp.requested_geom)
                best = None
                for oid, ph, cam in rows:
                    hd = heading_delta(ph, view.heading)
                    if abs(hd) > self.slot.policy.heading_tolerance_deg:
                        continue
                    dist = None
                    if cam is not None:
                        from app.geo.projection import geodesic_distance

                        c = to_shape(cam)
                        dist = geodesic_distance(sp.x, sp.y, c.x, c.y)
                    if best is None or abs(hd) < abs(best.heading_delta_deg or 0):
                        best = ReuseCandidate(oid, None if dist is None else round(dist, 2), round(hd, 2))
                return best

        return await asyncio.to_thread(q)

    async def record_model_usage(self, usage: TurnUsage, response_id: str | None, images_sent: int) -> None:
        settings = self.slot.settings
        cost, basis = estimate_cost_usd(usage, settings)

        def w():
            with session_scope() as s:
                s.add(
                    ModelUsage(
                        job_id=self.job.id,
                        task_id=self.cur.claim.task_id,
                        attempt_id=self.cur.claim.attempt_id,
                        worker_id=self.slot.worker_id,
                        provider="openai",
                        model=settings.openai_computer_use_model or "unknown",
                        response_id=response_id,
                        input_tokens=usage.input_tokens,
                        cached_input_tokens=usage.cached_input_tokens,
                        output_tokens=usage.output_tokens,
                        reasoning_tokens=usage.reasoning_tokens,
                        images_sent=images_sent,
                        estimated_cost_usd=cost,
                        price_basis=basis,
                    )
                )

        await asyncio.to_thread(w)

    async def budget_remaining_usd(self) -> float:
        def q():
            with session_scope() as s:
                return self.job.model_budget_usd - jobsvc.model_spend_usd(s, self.job.id)

        return await asyncio.to_thread(q)

    async def set_status(self, status: str, message: str | None = None) -> None:
        await self.slot.set_status(status, message)

    async def next_command(self, timeout_s: float = 1.0) -> dict | None:
        return await self.slot.next_command(timeout_s)

    async def finish_command(self, command_id: int, status: str, result: dict | None = None) -> None:
        await self.slot.finish_command(command_id, status, result)

    async def event(self, kind: str, message: str, level: str = "info", data: dict | None = None) -> None:
        def w():
            with session_scope() as s:
                emit(s, kind, message, level=level, job_id=self.job.id, task_id=self.cur.claim.task_id, worker_id=self.slot.worker_id, data=data)

        await asyncio.to_thread(w)


class WorkerSlot:
    def __init__(self, job_id: uuid.UUID, slot: int, settings: Settings | None = None, driver_factory=None):
        self.job_id = job_id
        self.slot = slot
        self.settings = settings or get_settings()
        self.worker_id = f"{HOST}-{str(job_id)[:8]}-s{slot}"
        self.stopping = False
        self.current: _Current | None = None
        self.current_sample_id: uuid.UUID | None = None
        self.driver: CaptureDriver | None = None
        self.policy = CapturePolicy()
        self._driver_factory = driver_factory or build_driver
        self._lock_conn = None
        self._profile_index: int | None = None
        self.crash_error: str | None = None

    # ------------------------------------------------------------ locking
    def _acquire_locks(self) -> bool:
        conn = get_engine().connect()
        k1, k2 = slot_lock_key(self.job_id, self.slot)
        if not conn.execute(text("SELECT pg_try_advisory_lock(:a, :b)"), {"a": k1, "b": k2}).scalar():
            conn.close()
            return False
        job = conn.execute(text("SELECT target FROM jobs WHERE id = :j"), {"j": self.job_id}).scalar_one()
        for n in range(64):
            a, b = _lock_key(f"profile:{job}", n)
            if conn.execute(text("SELECT pg_try_advisory_lock(:a, :b)"), {"a": a, "b": b}).scalar():
                self._profile_index = n
                break
        conn.commit()
        self._lock_conn = conn
        return self._profile_index is not None

    def _release_locks(self) -> None:
        if self._lock_conn is not None:
            try:
                self._lock_conn.execute(text("SELECT pg_advisory_unlock_all()"))
                self._lock_conn.commit()
            finally:
                self._lock_conn.close()
                self._lock_conn = None

    # ------------------------------------------------------------- worker row
    async def set_status(self, status: str, message: str | None = None) -> None:
        def w():
            with session_scope() as s:
                s.execute(
                    text("UPDATE workers SET status = :st, message = :m, heartbeat_at = now() WHERE id = :w"),
                    {"st": status, "m": message, "w": self.worker_id},
                )

        await asyncio.to_thread(w)

    def _register(self) -> None:
        with session_scope() as s:
            s.execute(
                text(
                    """
                    INSERT INTO workers (id, job_id, slot, host, pid, status, method, heartbeat_at, started_at, browser_profile_dir, message)
                    VALUES (:id, :j, :slot, :host, :pid, 'starting', (SELECT method FROM jobs WHERE id = :j), now(), now(), :prof, 'starting')
                    ON CONFLICT (id) DO UPDATE SET status = 'starting', pid = :pid, heartbeat_at = now(), started_at = now(),
                        stopped_at = NULL, current_task_id = NULL, browser_profile_dir = :prof, message = 'starting'
                    """
                ),
                {"id": self.worker_id, "j": self.job_id, "slot": self.slot, "host": HOST, "pid": os.getpid(), "prof": f"profile-{self._profile_index}"},
            )

    async def _heartbeat_loop(self) -> None:
        while not self.stopping:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            cur = self.current

            def hb():
                with session_scope() as s:
                    s.execute(text("UPDATE workers SET heartbeat_at = now() WHERE id = :w"), {"w": self.worker_id})
                    if cur is not None and not cur.lease_lost:
                        if not queue.renew_lease(s, cur.claim.task_id, cur.claim.lease_token):
                            cur.lease_lost = True

            try:
                await asyncio.to_thread(hb)
            except Exception:  # noqa: BLE001
                log.exception("heartbeat failed")

    async def next_command(self, timeout_s: float = 1.0) -> dict | None:
        def q():
            with session_scope() as s:
                row = s.execute(
                    text(
                        """
                        UPDATE worker_commands SET status = 'running'
                         WHERE id = (SELECT id FROM worker_commands WHERE worker_id = :w AND status = 'pending'
                                      ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
                     RETURNING id, command, payload
                        """
                    ),
                    {"w": self.worker_id},
                ).mappings().first()
                return dict(row) if row else None

        cmd = await asyncio.to_thread(q)
        if cmd is None:
            await asyncio.sleep(timeout_s)
        return cmd

    async def finish_command(self, command_id: int, status: str, result: dict | None = None) -> None:
        import json

        def w():
            with session_scope() as s:
                s.execute(
                    text("UPDATE worker_commands SET status = :st, result = CAST(:r AS jsonb), executed_at = now() WHERE id = :id"),
                    {"st": status, "r": json.dumps(result or {}, default=str), "id": command_id},
                )

        await asyncio.to_thread(w)

    # ------------------------------------------------------------------ main
    async def run(self) -> None:
        if not await asyncio.to_thread(self._acquire_locks):
            log.info("slot %s already owned elsewhere", self.worker_id)
            self._release_locks()
            return
        hb = None
        try:
            await asyncio.to_thread(self._register)
            job, scope_check = await asyncio.to_thread(self._load_job)
            self.scope_check = scope_check
            self.policy = CapturePolicy.from_params(job.settings.get("policy") or {})
            self.driver = self._driver_factory(job, self.settings, self._profile_index or 0)
            await self.driver.start()
            await self.set_status("idle", "ready")
            hb = asyncio.create_task(self._heartbeat_loop())
            while not self.stopping:
                status = await asyncio.to_thread(self._job_status)
                if status != "running":
                    break
                claim = await asyncio.to_thread(self._claim)
                if claim is None:
                    await asyncio.to_thread(self._idle_housekeeping)
                    await asyncio.sleep(1.0)
                    continue
                await self._run_task(claim, job)
        except Exception as e:  # noqa: BLE001
            log.exception("worker slot crashed")
            self.crash_error = f"{type(e).__name__}: {e}"[:500]
            await asyncio.to_thread(self._log_event, "worker_error", f"Worker error: {self.crash_error}", "error")
        finally:
            if hb:
                hb.cancel()
            if self.driver:
                try:
                    await self.driver.close()
                except Exception:  # noqa: BLE001
                    log.exception("driver close failed")
            try:
                await asyncio.to_thread(self._mark_stopped)
            finally:
                await asyncio.to_thread(self._release_locks)

    def _load_job(self):
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            project = s.get(Project, job.project_id)
            cfg = ProjectConfig(**(project.config or {}))
            checker = build_scope_checker(project.scope_geom, cfg.scope_buffer_m)
            s.expunge(job)
            return job, checker

    def _job_status(self) -> str:
        with session_scope() as s:
            return s.execute(text("SELECT status FROM jobs WHERE id = :j"), {"j": self.job_id}).scalar_one()

    def _claim(self):
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            if jobsvc.enforce_limits(s, job):
                return None
            return queue.claim_next_task(s, self.job_id, self.worker_id, self.settings.lease_seconds)

    def _idle_housekeeping(self):
        with session_scope() as s:
            s.execute(text("UPDATE workers SET status = 'idle', heartbeat_at = now(), current_task_id = NULL WHERE id = :w"), {"w": self.worker_id})
            job = s.get(Job, self.job_id)
            jobsvc.maybe_complete_job(s, job)

    def _mark_stopped(self):
        with session_scope() as s:
            s.execute(
                text("UPDATE workers SET status = 'stopped', stopped_at = now(), current_task_id = NULL, message = 'stopped' WHERE id = :w"),
                {"w": self.worker_id},
            )

    def _log_event(self, kind: str, message: str, level: str = "info", task_id=None):
        with session_scope() as s:
            emit(s, kind, message, level=level, job_id=self.job_id, task_id=task_id, worker_id=self.worker_id)

    def _build_context(self, claim: queue.Claim, job: Job, runtime: SlotRuntime) -> TaskContext:
        with session_scope() as s:
            smp = s.get(Sample, claim.sample_id)
            seg = s.get(Segment, smp.segment_id)
            q = select(PlannedView).where(PlannedView.sample_id == smp.id).order_by(PlannedView.ordinal)
            views = s.execute(q).scalars().all()
            if claim.view_filter:
                wanted = {str(v) for v in claim.view_filter}
                views = [v for v in views if str(v.id) in wanted]
            pt = to_shape(smp.requested_geom)
            project = s.get(Project, job.project_id)
            lim = effective_limits(ProjectConfig(**(project.config or {})), self.settings)
            return TaskContext(
                claim=claim,
                worker_id=self.worker_id,
                job=JobSnapshot(
                    id=job.id,
                    project_id=job.project_id,
                    plan_id=job.plan_id,
                    target=job.target,
                    method=job.method,
                    settings=job.settings or {},
                    config_version_id=job.config_version_id,
                    requests_per_minute=job.requests_per_minute,
                    cloud_image_processing_allowed=lim.cloud_image_processing_allowed,
                ),
                sample=SampleSnapshot(
                    id=smp.id,
                    ordinal=smp.ordinal,
                    kind=smp.kind,
                    lat=pt.y,
                    lng=pt.x,
                    segment_id=seg.id,
                    segment_key=seg.segment_key,
                    segment_bearing=seg.bearing_deg,
                    local_bearing=smp.local_bearing_deg,
                ),
                views=[
                    PlannedViewInfo(str(v.id), v.side, v.view_kind, v.heading_deg, v.pitch_deg, v.fov_deg, v.viewport_w, v.viewport_h)
                    for v in views
                ],
                policy=self.policy,
                runtime=runtime,
                scope_check=self.scope_check,
                policy_reuse=bool((job.settings.get("policy") or {}).get("reuse_panorama_views", True)),
            )

    async def _run_task(self, claim: queue.Claim, job: Job) -> None:
        cur = _Current(claim)
        self.current = cur
        self.current_sample_id = claim.sample_id
        runtime = SlotRuntime(self, cur, job)
        try:
            ctx = await asyncio.to_thread(self._build_context, claim, job, runtime)
            await self.set_status("running", f"task {claim.ordinal} ({claim.segment_key}) attempt {claim.attempt_no}")
            await self._execute(ctx, cur, job, depth=0)
        finally:
            self.current = None
            self.current_sample_id = None

    async def _execute(self, ctx: TaskContext, cur: _Current, job: Job, depth: int) -> None:
        claim = cur.claim
        try:
            outcome = await (self.driver.collect(ctx) if depth == 0 else self.driver.resume_after_intervention(ctx))
            if cur.lease_lost:
                raise LeaseLost("lease lost before completion")
            await self._complete(ctx, cur, job, outcome)
        except LeaseLost:
            await asyncio.to_thread(self._log_event, "lease_lost", f"Worker lost lease on task {claim.ordinal}; result discarded", "warning", claim.task_id)
        except TaskInterrupted as e:
            await asyncio.to_thread(self._tx, queue.release_task, claim.task_id, claim.lease_token, str(e), worker_id=self.worker_id)
        except ManualInterventionRequired as e:
            await self._await_operator(ctx, cur, job, e, depth)
        except NoImagery as e:
            await self._complete(ctx, cur, job, TaskOutcome(kind="no_imagery", no_imagery_reason=e.reason, evidence=e.evidence))
        except TaskSkipped as e:
            await asyncio.to_thread(self._tx, queue.fail_task, claim.task_id, claim.lease_token, f"skipped: {e}", retryable=False, worker_id=self.worker_id)
        except LimitReached as e:
            await asyncio.to_thread(self._tx, queue.release_task, claim.task_id, claim.lease_token, str(e), worker_id=self.worker_id)
            await asyncio.to_thread(self._pause, "limit", str(e))
        except CloudImageTransmissionBlocked as e:
            await asyncio.to_thread(self._tx, queue.fail_task, claim.task_id, claim.lease_token, f"blocked: {e}", retryable=False, worker_id=self.worker_id)
            await asyncio.to_thread(self._pause, "error", f"Cloud image transmission blocked: {e}")
        except FatalTaskError as e:
            await asyncio.to_thread(self._tx, queue.fail_task, claim.task_id, claim.lease_token, str(e), retryable=False, worker_id=self.worker_id)
        except RetryableTaskError as e:
            await asyncio.to_thread(self._tx, queue.fail_task, claim.task_id, claim.lease_token, str(e), retryable=True, worker_id=self.worker_id)
        except Exception as e:  # noqa: BLE001
            log.exception("task error")
            await asyncio.to_thread(self._tx, queue.fail_task, claim.task_id, claim.lease_token, f"unexpected error: {e!r}", retryable=True, worker_id=self.worker_id)

    def _tx(self, fn, *args, **kwargs):
        with session_scope() as s:
            return fn(s, *args, **kwargs)

    def _pause(self, kind: str, reason: str):
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            jobsvc.pause_job(s, job, kind, reason)

    async def _complete(self, ctx: TaskContext, cur: _Current, job: Job, outcome: TaskOutcome) -> None:
        claim = cur.claim

        def tx():
            with session_scope() as s:
                j = s.get(Job, job.id)
                smp = s.get(Sample, claim.sample_id)
                seg = s.get(Segment, smp.segment_id)
                return complete_task(s, claim=claim, worker_id=self.worker_id, outcome=outcome, job=j, sample=smp, segment=seg)

        try:
            res = await asyncio.to_thread(tx)
        except (ArtifactInvalid, IntegrityError) as e:
            await asyncio.to_thread(self._orphan, outcome)
            await asyncio.to_thread(self._tx, queue.fail_task, claim.task_id, claim.lease_token, f"completion rejected: {e}", retryable=True, worker_id=self.worker_id)
            return
        if not res.accepted:
            await asyncio.to_thread(self._orphan, outcome)
            return

        def after():
            with session_scope() as s:
                finalize_task(s, claim.task_id)
            with session_scope() as s:
                j = s.get(Job, job.id)
                jobsvc.enforce_limits(s, j)

        await asyncio.to_thread(after)

    def _orphan(self, outcome: TaskOutcome) -> None:
        import shutil

        for r in outcome.all_rel_paths():
            try:
                src = files.resolve_rel(r)
                if src.exists():
                    dest = self.settings.orphans_dir / r
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dest))
            except Exception:  # noqa: BLE001
                log.exception("orphan move failed")

    async def _await_operator(self, ctx: TaskContext, cur: _Current, job: Job, e: ManualInterventionRequired, depth: int) -> None:
        claim = cur.claim
        ok = await asyncio.to_thread(self._tx, queue.mark_manual_required, claim.task_id, claim.lease_token, e.reason)
        if not ok:
            return
        await asyncio.to_thread(self._pause, "manual_intervention", f"{self.worker_id} task {claim.ordinal}: {e.reason}")
        await self.set_status("awaiting_operator", f"Manual intervention required: {e.reason}")
        runtime = SlotRuntime(self, cur, job)
        while True:
            if cur.lease_lost or self.stopping:
                if not cur.lease_lost:
                    await asyncio.to_thread(self._tx, queue.release_task, claim.task_id, claim.lease_token, "worker stopping", worker_id=self.worker_id)
                return
            status = await runtime._job_status()
            if status == "cancelled":
                await asyncio.to_thread(self._tx, queue.release_task, claim.task_id, claim.lease_token, "job cancelled", worker_id=self.worker_id)
                return
            if status == "running":
                await asyncio.to_thread(self._tx, queue.mark_running_again, claim.task_id, claim.lease_token)
                await self.set_status("running", f"resuming task {claim.ordinal} after intervention")
                if depth >= 3:
                    await asyncio.to_thread(self._tx, queue.fail_task, claim.task_id, claim.lease_token, "repeated manual interventions", retryable=True, worker_id=self.worker_id)
                    return
                await self._execute(ctx, cur, job, depth + 1)
                return
            cmd = await runtime.next_command(timeout_s=1.0)
            if cmd is None:
                continue
            if cmd["command"] == "skip":
                await runtime.finish_command(cmd["id"], "done", {"ok": True})
                await asyncio.to_thread(self._tx, queue.fail_task, claim.task_id, claim.lease_token, "skipped by operator during intervention", retryable=False, worker_id=self.worker_id)
                return
            try:
                result = await self.driver.operator_action(cmd)
            except Exception as ex:  # noqa: BLE001
                result = {"ok": False, "error": str(ex)}
            try:
                if hasattr(self.driver, "session"):
                    await runtime.publish_preview(await self.driver.session.screenshot(), f"operator: {cmd['command']}")
            except Exception:  # noqa: BLE001
                pass
            await runtime.finish_command(cmd["id"], "done" if result.get("ok") else "failed", result)
