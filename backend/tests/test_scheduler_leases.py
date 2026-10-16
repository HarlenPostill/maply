"""Durable queue: claims, leases, fencing tokens, retries, rate limiting."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import select, text

from app.db import session_scope
from app.models import Job, Task, TaskAttempt
from app.scheduler import jobs as jobsvc
from app.scheduler import queue
from tests.factories import make_job, make_plan, make_project

pytestmark = pytest.mark.db


def _setup(db, **plan_kw):
    p = make_project(db)
    plan = make_plan(db, p, **plan_kw)
    job = make_job(db, plan)
    return p, plan, job


def _claim(job_id, worker):
    with session_scope() as s:
        return queue.claim_next_task(s, job_id, worker)


def test_concurrent_claims_never_share_a_task(db):
    _, _, job = _setup(db)
    n_tasks = db.execute(select(Task).where(Task.job_id == job.id)).scalars().all()
    results: list = []
    lock = threading.Lock()

    def worker(i):
        while True:
            c = _claim(job.id, f"w{i}")
            if c is None:
                return
            with lock:
                results.append(c.task_id)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == len(n_tasks)
    assert len(set(results)) == len(results)  # no task claimed twice


def test_paused_job_cannot_be_claimed(db):
    _, _, job = _setup(db)
    jobsvc.pause_job(db, db.get(Job, job.id), "operator", "test")
    db.commit()
    assert _claim(job.id, "w") is None


def test_expired_lease_is_recovered_and_stale_worker_fenced(db):
    _, _, job = _setup(db)
    a = _claim(job.id, "worker-A")
    with session_scope() as s:
        s.execute(text("UPDATE tasks SET lease_expires_at = now() - interval '1 second' WHERE id = :t"), {"t": a.task_id})
    with session_scope() as s:
        rec = queue.recover_expired_leases(s)
    assert [r["task_id"] for r in rec] == [a.task_id]
    with session_scope() as s:
        s.execute(text("UPDATE tasks SET next_attempt_at = now() WHERE id = :t"), {"t": a.task_id})
    b = _claim(job.id, "worker-B")
    assert b.task_id == a.task_id and b.lease_token != a.lease_token and b.attempt_no == 2
    with session_scope() as s:
        assert queue.renew_lease(s, a.task_id, a.lease_token) is False  # stale heartbeat
        assert queue.renew_lease(s, b.task_id, b.lease_token) is True
        assert queue.fail_task(s, a.task_id, a.lease_token, "late failure") == "stale"
        assert queue.release_task(s, a.task_id, a.lease_token, "late release") == "stale"
    with session_scope() as s:
        attempts = s.execute(select(TaskAttempt).where(TaskAttempt.task_id == a.task_id).order_by(TaskAttempt.attempt_no)).scalars().all()
        assert [x.outcome for x in attempts] == ["lease_expired", "running"]
        t = s.get(Task, a.task_id)
        assert t.lease_owner == "worker-B" and t.failure_count == 1


def test_bounded_retries_with_backoff(db):
    _, _, job = _setup(db)
    c = _claim(job.id, "w")
    tid = c.task_id
    with session_scope() as s:
        assert queue.fail_task(s, tid, c.lease_token, "boom 1") == "queued"
        t = s.get(Task, tid)
        assert t.next_attempt_at > queue.now_db(s)  # backoff
    statuses = []
    for _ in range(5):
        with session_scope() as s:
            s.execute(text("UPDATE tasks SET next_attempt_at = now() WHERE id = :t"), {"t": tid})
        with session_scope() as s:
            row = s.execute(text("SELECT status FROM tasks WHERE id = :t"), {"t": tid}).scalar()
            if row != "queued":
                break
        c2 = None
        while c2 is None or c2.task_id != tid:
            c2 = _claim(job.id, "w")
            if c2 and c2.task_id != tid:
                with session_scope() as s:
                    queue.release_task(s, c2.task_id, c2.lease_token, "not this one")
        with session_scope() as s:
            statuses.append(queue.fail_task(s, tid, c2.lease_token, "boom"))
    assert statuses[-1] == "failed"
    with session_scope() as s:
        t = s.get(Task, tid)
        assert t.failure_count == t.max_attempts == 3 and t.status == "failed"


def test_release_does_not_count_failure(db):
    _, _, job = _setup(db)
    c = _claim(job.id, "w")
    with session_scope() as s:
        assert queue.release_task(s, c.task_id, c.lease_token, "paused") == "queued"
        assert s.get(Task, c.task_id).failure_count == 0


def test_cancel_marks_pending_tasks_and_release_cancels_running(db):
    _, _, job = _setup(db)
    c = _claim(job.id, "w")
    with session_scope() as s:
        jobsvc.cancel_job(s, s.get(Job, job.id))
    with session_scope() as s:
        assert queue.release_task(s, c.task_id, c.lease_token, "job cancelled") == "cancelled"
        counts = jobsvc.task_counts(s, job.id)
    assert set(counts) == {"cancelled"}


def test_shared_rate_limiter_spaces_requests(db):
    _, _, job = _setup(db)
    waits = []
    for _ in range(4):
        with session_scope() as s:
            waits.append(queue.reserve_request_slot(s, job.id, requests_per_minute=60))
    assert waits[0] == pytest.approx(0, abs=0.05)
    assert waits[1] == pytest.approx(1, abs=0.1) and waits[3] == pytest.approx(3, abs=0.15)


def test_pause_resume_accumulates_runtime_and_respects_limits(db):
    _, _, job = _setup(db)
    j = db.get(Job, job.id)
    jobsvc.pause_job(db, j, "operator", "x")
    assert j.status == "paused" and j.runtime_s >= 0 and j.running_since is None
    jobsvc.resume_job(db, j)
    assert j.status == "running" and j.pause_reason is None
    j.max_duration_s = 0
    assert jobsvc.enforce_limits(db, j) is not None and j.status == "paused" and j.pause_kind == "limit"
    with pytest.raises(jobsvc.JobConfigError):
        jobsvc.resume_job(db, j)  # still over the limit
    db.rollback()


def test_job_permissions_enforced(db):
    p = make_project(db, permitted_access_methods=["deterministic_browser"])
    plan = make_plan(db, p)
    with pytest.raises(jobsvc.JobConfigError) as e:
        make_job(db, plan, method="manual")
    assert "not permitted" in str(e.value)
    with pytest.raises(jobsvc.JobConfigError) as e:
        make_job(db, plan, method="deterministic_browser", target="google_maps", requests_per_minute=500)
    assert "exceeds the limit" in str(e.value)
    db.rollback()


def test_openai_method_refused_while_cloud_processing_disabled(db):
    p = make_project(db, permitted_access_methods=["openai_agent"], allow_third_party_processing=True, third_party_processors=["openai"])
    plan = make_plan(db, p)
    from app.config import get_settings

    s = get_settings().model_copy(update={"openai_api_key": "sk", "openai_computer_use_model": "m"})
    from app.schemas import JobCreate

    with pytest.raises(jobsvc.JobConfigError) as e:
        jobsvc.create_job(db, plan, JobCreate(target="google_maps", method="openai_agent", model_budget_usd=1, requests_per_minute=5), settings=s)
    assert any("cloud image processing is not permitted" in m for m in e.value.errors)
    db.rollback()


def test_unknown_token_cannot_complete(db):
    _, _, job = _setup(db)
    c = _claim(job.id, "w")
    with session_scope() as s:
        assert queue.lock_owned_task(s, c.task_id, uuid.uuid4()) is None
        assert queue.lock_owned_task(s, c.task_id, c.lease_token) is not None
