"""Duplicate-capture prevention, stale completions, crash recovery and orphan handling."""

from __future__ import annotations

import os
import time
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.capture.completion import ArtifactInvalid, TaskOutcome, complete_task
from app.db import session_scope
from app.models import Job, Observation, ObservationLink, Sample, Segment, Task, TaskAttempt, Worker
from app.scheduler import queue
from app.services.coverage import job_coverage
from app.storage import files
from app.workers.main import recover_dead_slots, sweep
from tests.factories import make_job, make_plan, make_project
from tests.helpers import complete_with_captures, views_for, write_capture

pytestmark = pytest.mark.db


def _job(db, **kw):
    p = make_project(db)
    plan = make_plan(db, p, **kw)
    return make_job(db, plan)


def _claim(job_id, w):
    with session_scope() as s:
        return queue.claim_next_task(s, job_id, w)


def _current_obs(job_id) -> int:
    with session_scope() as s:
        return s.execute(select(func.count()).select_from(Observation).where(Observation.job_id == job_id, Observation.is_current.is_(True))).scalar_one()


def test_completion_is_accepted_once_and_verified_by_supervisor(db):
    job = _job(db)
    c = _claim(job.id, "w1")
    res, outcome = complete_with_captures(c, "w1")
    assert res.accepted
    n_views = len(outcome.captures)
    assert _current_obs(job.id) == n_views
    with session_scope() as s:
        assert s.get(Task, c.task_id).status == "verified"
    # a duplicate completion with the same (now consumed) token is rejected as stale
    res2, out2 = complete_with_captures(c, "w1")
    assert not res2.accepted and res2.reason == "stale lease"
    assert _current_obs(job.id) == n_views


def test_two_workers_cannot_both_accept_the_same_task(db):
    job = _job(db)
    a = _claim(job.id, "worker-A")
    with session_scope() as s:  # A stalls; its lease expires and B takes over
        s.execute(text("UPDATE tasks SET lease_expires_at = now() - interval '1 s' WHERE id = :t"), {"t": a.task_id})
    with session_scope() as s:
        queue.recover_expired_leases(s)
        s.execute(text("UPDATE tasks SET next_attempt_at = now() WHERE id = :t"), {"t": a.task_id})
    b = _claim(job.id, "worker-B")
    assert b.task_id == a.task_id
    res_b, _ = complete_with_captures(b, "worker-B", pano_id="PANO-B")
    res_a, out_a = complete_with_captures(a, "worker-A", pano_id="PANO-A")  # A wakes up late
    assert res_b.accepted and not res_a.accepted
    with session_scope() as s:
        panos = set(s.execute(select(Observation.pano_id).where(Observation.job_id == job.id, Observation.is_current.is_(True))).scalars())
        assert panos == {"PANO-B"}
        outcomes = [x.outcome for x in s.execute(select(TaskAttempt).where(TaskAttempt.task_id == a.task_id).order_by(TaskAttempt.attempt_no)).scalars()]
        assert outcomes == ["stale_rejected", "captured"]
    # A's files were never referenced; reconciliation moves them out of the dataset
    refs = {o for o in _all_image_paths(job.id)}
    for c in out_a.captures:
        assert c.image_rel not in refs


def _all_image_paths(job_id):
    with session_scope() as s:
        return set(s.execute(select(Observation.image_path).where(Observation.job_id == job_id)).scalars())


def test_partial_unique_index_blocks_second_current_observation(db):
    job = _job(db)
    c = _claim(job.id, "w")
    complete_with_captures(c, "w")
    with session_scope() as s:
        o = s.execute(select(Observation).where(Observation.job_id == job.id)).scalars().first()
        dup = {col.name: getattr(o, col.key) for col in Observation.__table__.columns}
        dup.update(id=uuid.uuid4(), idempotency_key="other")
        s.add(Observation(**dup))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_tampered_artifact_is_rejected_by_supervisor(db):
    job = _job(db)
    c = _claim(job.id, "w")
    with session_scope() as s:
        j, smp = s.get(Job, job.id), s.get(Sample, c.sample_id)
        seg = s.get(Segment, smp.segment_id)
        rec = write_capture(j, smp, seg, views_for(s, smp.id)[0])
    files.resolve_rel(rec.image_rel).write_bytes(b"not the image that was verified")
    with session_scope() as s:
        j, smp = s.get(Job, job.id), s.get(Sample, c.sample_id)
        with pytest.raises(ArtifactInvalid):
            complete_task(s, claim=c, worker_id="w", outcome=TaskOutcome(captures=[rec]), job=j, sample=smp, segment=s.get(Segment, smp.segment_id))
    assert _current_obs(job.id) == 0


def test_agent_claim_without_artifact_is_rejected(db):
    job = _job(db)
    c = _claim(job.id, "w")
    with session_scope() as s:
        j, smp = s.get(Job, job.id), s.get(Sample, c.sample_id)
        seg = s.get(Segment, smp.segment_id)
        rec = write_capture(j, smp, seg, views_for(s, smp.id)[0])
    files.resolve_rel(rec.image_rel).unlink()
    with session_scope() as s:
        j, smp = s.get(Job, job.id), s.get(Sample, c.sample_id)
        with pytest.raises(ArtifactInvalid, match="missing"):
            complete_task(s, claim=c, worker_id="w", outcome=TaskOutcome(captures=[rec]), job=j, sample=smp, segment=s.get(Segment, smp.segment_id))


def test_reused_panorama_is_linked_not_recaptured_and_reported(db):
    job = _job(db)
    a = _claim(job.id, "w")
    res, out = complete_with_captures(a, "w", pano_id="SHARED")
    b = _claim(job.id, "w")
    res2, out2 = complete_with_captures(b, "w", pano_id="SHARED", reuse_from=out.captures[0].observation_id)
    assert res2.accepted and out2.captures == []
    with session_scope() as s:
        links = s.execute(select(ObservationLink).where(ObservationLink.job_id == job.id, ObservationLink.link_kind == "reused_same_panorama")).scalars().all()
        assert links and all(lk.association_status == "uncertain" for lk in links)
        cov = job_coverage(s, s.get(Job, job.id))
    assert cov["views"]["reused"] == len(links)
    assert cov["samples_resolved_by_reuse"] == 1
    assert cov["association"].get("reused_uncertain") == len(links)


def test_coverage_separates_completeness_from_verified_and_reports_gaps(db):
    job = _job(db, sampling_interval_m=20)
    c = _claim(job.id, "w")
    complete_with_captures(c, "w")
    with session_scope() as s:
        cov = job_coverage(s, s.get(Job, job.id))
    assert 0 < cov["collection"]["completeness"] < 1
    assert 0 < cov["verified_coverage"] < 1
    assert cov["views"]["pending"] > 0
    assert cov["gaps"], "uncaptured stretches must be reported as gaps"


def test_crash_recovery_dead_slot_requeues_and_orphans_files(db, data_dir):
    job = _job(db)
    with session_scope() as s:  # a worker row whose process is gone (no advisory lock held)
        s.add(Worker(id="dead-worker", job_id=job.id, slot=0, host="x", status="running"))
    c = _claim(job.id, "dead-worker")
    with session_scope() as s:
        j, smp = s.get(Job, job.id), s.get(Sample, c.sample_id)
        rec = write_capture(j, smp, s.get(Segment, smp.segment_id), views_for(s, smp.id)[0])  # crashed after write, before commit
    assert recover_dead_slots() == 1
    sweep()
    with session_scope() as s:
        t = s.get(Task, c.task_id)
        assert t.status == "queued" and t.failure_count == 1 and t.lease_token is None
    old = time.time() - 3600
    os.utime(files.resolve_rel(rec.image_rel), (old, old))
    moved = files.reconcile_orphans(_all_image_paths(job.id))
    assert rec.image_rel in moved
    assert (data_dir / "orphans" / rec.image_rel).exists()
    with session_scope() as s:
        s.execute(text("UPDATE tasks SET next_attempt_at = now()"))
    c2 = _claim(job.id, "new-worker")
    assert c2.task_id == c.task_id and c2.attempt_no == 2
    res, _ = complete_with_captures(c2, "new-worker")
    assert res.accepted and _current_obs(job.id) == len(views_for(db, c.sample_id))


def test_live_slot_is_not_recovered(db):
    job = _job(db)
    from app.db import get_engine
    from app.workers.runner import slot_lock_key

    with session_scope() as s:
        s.add(Worker(id="live-worker", job_id=job.id, slot=3, host="x", status="running"))
    _claim(job.id, "live-worker")
    conn = get_engine().connect()
    k1, k2 = slot_lock_key(job.id, 3)
    conn.execute(text("SELECT pg_advisory_lock(:a, :b)"), {"a": k1, "b": k2})
    try:
        assert recover_dead_slots() == 0
    finally:
        conn.execute(text("SELECT pg_advisory_unlock_all()"))
        conn.close()
