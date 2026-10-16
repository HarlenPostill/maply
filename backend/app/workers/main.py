"""Worker supervisor process: `python -m app.workers.main`.

Responsibilities (all durable, all in PostgreSQL):
* startup recovery: requeue tasks held by dead slots (detected through their
  released advisory locks) without waiting for lease expiry;
* periodic sweeps: expired leases, stalled workers, `captured` tasks awaiting
  verification, cancelled jobs, job completion, orphaned files;
* spawn up to `worker_count` slots for every running job.

Run one supervisor in Docker or on the host (headed mode for interventions).
Running both is safe: slot advisory locks prevent duplicate slots.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid

from sqlalchemy import select, text

from app.capture.completion import finalize_captured_tasks
from app.config import get_settings
from app.db import get_engine, session_scope
from app.models import Job, Observation, Crop, SystemCheck
from app.providers.egress import install_log_redaction
from app.scheduler import jobs as jobsvc
from app.scheduler import queue
from app.storage import files
from app.workers.runner import WorkerSlot, slot_lock_key

log = logging.getLogger("maply.supervisor")
MAX_CONSECUTIVE_CRASHES = 4


def recover_dead_slots() -> int:
    """Expire leases held by workers whose slot lock is free (their process is gone)."""
    n = 0
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT t.id AS task_id, w.id AS worker_id, w.job_id, w.slot
                  FROM tasks t JOIN workers w ON w.id = t.lease_owner
                 WHERE t.status IN ('running', 'manual_required')
                """
            )
        ).mappings().all()
    if not rows:
        return 0
    conn = get_engine().connect()
    try:
        for r in rows:
            k1, k2 = slot_lock_key(r["job_id"], r["slot"])
            if conn.execute(text("SELECT pg_try_advisory_lock(:a, :b)"), {"a": k1, "b": k2}).scalar():
                conn.execute(text("SELECT pg_advisory_unlock(:a, :b)"), {"a": k1, "b": k2})
                with session_scope() as s:
                    s.execute(text("UPDATE tasks SET lease_expires_at = now() - interval '1 second' WHERE id = :t"), {"t": r["task_id"]})
                    s.execute(text("UPDATE workers SET status = 'stopped', message = 'process ended; task recovered' WHERE id = :w"), {"w": r["worker_id"]})
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def sweep() -> dict:
    out: dict = {}
    with session_scope() as s:
        out["recovered"] = queue.recover_expired_leases(s)
    with session_scope() as s:
        out["stalled"] = queue.mark_stalled_workers(s)
    with session_scope() as s:
        out["finalized"] = finalize_captured_tasks(s)
    with session_scope() as s:
        s.execute(
            text(
                """
                UPDATE tasks SET status = 'cancelled', finished_at = now(), outcome_reason = 'job cancelled'
                 WHERE status IN ('planned', 'queued') AND job_id IN (SELECT id FROM jobs WHERE status = 'cancelled')
                """
            )
        )
        for job in s.execute(select(Job).where(Job.status == "running")).scalars():
            jobsvc.enforce_limits(s, job)
            jobsvc.maybe_complete_job(s, job)
    return out


def reconcile_files() -> list[str]:
    with session_scope() as s:
        refs = set(s.execute(select(Observation.image_path)).scalars())
        refs |= {p for p in s.execute(select(Observation.thumb_path)).scalars() if p}
        refs |= set(s.execute(select(Crop.path)).scalars())
    return files.reconcile_orphans(refs)


async def startup_checks() -> None:
    settings = get_settings()
    status, detail = "unconfigured", {"reason": "OPENAI_API_KEY / OPENAI_COMPUTER_USE_MODEL not set"}
    if settings.openai_api_key and settings.openai_computer_use_model and settings.openai_startup_check != "off":
        from app.providers.egress import egress_context
        from app.providers.openai_adapter import OpenAIComputerUseAdapter

        try:
            adapter = OpenAIComputerUseAdapter(settings)
            with egress_context(data_class="none"):
                detail = {"model": await adapter.check_model_access()}
                if settings.openai_startup_check == "probe":
                    detail["probe"] = await adapter.probe_computer_tool()
            status = "ok"
            await adapter.aclose()
        except Exception as e:  # noqa: BLE001
            status, detail = "failed", {"error": f"{type(e).__name__}: {e}"[:1000], "model": settings.openai_computer_use_model}
    elif settings.openai_startup_check == "off":
        status, detail = "skipped", {"reason": "OPENAI_STARTUP_CHECK=off"}
    with session_scope() as s:
        s.merge(SystemCheck(name="openai_model_access", status=status, detail=detail))
    log.info("startup check openai_model_access: %s %s", status, detail)


class Supervisor:
    def __init__(self):
        self.settings = get_settings()
        self.slots: dict[tuple[uuid.UUID, int], tuple[WorkerSlot, asyncio.Task]] = {}
        self.crashes: dict[tuple[uuid.UUID, int], tuple[int, float]] = {}
        self.stopping = False

    @staticmethod
    def _pause_job(job_id: uuid.UUID, reason: str) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is not None:
                jobsvc.pause_job(s, job, "error", reason)

    async def _wait_for_schema(self, timeout_s: float = 300.0) -> None:
        """The api container applies migrations; wait until they are in place."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            try:
                def q():
                    with session_scope() as s:
                        s.execute(text("SELECT 1 FROM workers LIMIT 1"))

                await asyncio.to_thread(q)
                return
            except Exception as e:  # noqa: BLE001
                if loop.time() > deadline:
                    raise
                log.info("waiting for database schema: %s", type(e).__name__)
                await asyncio.sleep(2.0)

    async def run(self) -> None:
        self.settings.ensure_dirs()
        await self._wait_for_schema()
        n = await asyncio.to_thread(recover_dead_slots)
        if n:
            log.warning("startup recovery: %d task(s) held by dead workers will be requeued", n)
        await startup_checks()
        last_reconcile = 0.0
        loop = asyncio.get_running_loop()
        while not self.stopping:
            try:
                res = await asyncio.to_thread(sweep)
                if res["recovered"]:
                    log.warning("recovered %d expired lease(s)", len(res["recovered"]))
                if loop.time() - last_reconcile > 60:
                    moved = await asyncio.to_thread(reconcile_files)
                    if moved:
                        log.warning("moved %d unreferenced file(s) to orphans/", len(moved))
                    last_reconcile = loop.time()
                await self._spawn()
            except Exception:  # noqa: BLE001
                log.exception("supervisor iteration failed")
            await asyncio.sleep(2.0)
        await self.shutdown()

    async def _spawn(self) -> None:
        loop = asyncio.get_running_loop()
        for key in [k for k, (_, t) in self.slots.items() if t.done()]:
            ws, _ = self.slots.pop(key)
            if ws.crash_error:
                n = self.crashes.get(key, (0, 0.0))[0] + 1
                self.crashes[key] = (n, loop.time() + min(60.0, 2.0**n))
                if n >= MAX_CONSECUTIVE_CRASHES:
                    await asyncio.to_thread(self._pause_job, key[0], f"Worker slot {key[1]} failed {n} times: {ws.crash_error}")
                    self.crashes.pop(key, None)
            else:
                self.crashes.pop(key, None)

        def running_jobs():
            with session_scope() as s:
                return [(j.id, j.worker_count) for j in s.execute(select(Job).where(Job.status == "running")).scalars()]

        for job_id, count in await asyncio.to_thread(running_jobs):
            for slot in range(count):
                if (job_id, slot) in self.slots:
                    continue
                if (job_id, slot) in self.crashes and self.crashes[(job_id, slot)][1] > loop.time():
                    continue  # backoff after a crash
                ws = WorkerSlot(job_id, slot, self.settings)
                self.slots[(job_id, slot)] = (ws, asyncio.create_task(ws.run(), name=ws.worker_id))
                log.info("spawned worker %s", ws.worker_id)

    async def shutdown(self) -> None:
        for ws, _ in self.slots.values():
            ws.stopping = True
        tasks = [t for _, t in self.slots.values()]
        if tasks:
            await asyncio.wait(tasks, timeout=20)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_log_redaction()
    sup = Supervisor()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop(*_):
        sup.stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, _stop)
    loop.run_until_complete(sup.run())


if __name__ == "__main__":
    main()
