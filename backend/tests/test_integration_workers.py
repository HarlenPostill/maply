"""End-to-end worker tests: real supervisor process, real Chromium, local mock site.

These are the repeatable synthetic versions of the main workflow:
route -> plan -> worker -> local captures -> pause/resume -> crash/restart,
plus manual intervention and the OpenAI adapter path against a local fake
provider. Nothing here contacts Google or OpenAI.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, text

from app.db import session_scope
from app.models import EgressEvent, Job, ModelUsage, Observation, Task, Worker
from app.scheduler import jobs as jobsvc
from tests.conftest import BACKEND, _free_port, chromium_available
from tests.factories import make_job, make_plan, make_project

pytestmark = [pytest.mark.db, pytest.mark.browser]

if not chromium_available():
    pytest.skip("Playwright Chromium not installed", allow_module_level=True)


def start_supervisor(**env) -> subprocess.Popen:
    e = {**os.environ, **{k.upper(): str(v) for k, v in env.items()}}
    return subprocess.Popen([sys.executable, "-m", "app.workers.main"], cwd=BACKEND, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(proc: subprocess.Popen, sig=signal.SIGTERM):
    if proc.poll() is None:
        proc.send_signal(sig)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


def wait_for(cond, timeout=90.0, interval=0.5, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = cond()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


def job_state(job_id):
    with session_scope() as s:
        j = s.get(Job, job_id)
        return j.status, jobsvc.task_counts(s, job_id), j.pause_kind, j.pause_reason


def current_obs(job_id) -> int:
    with session_scope() as s:
        return s.execute(select(func.count()).select_from(Observation).where(Observation.job_id == job_id, Observation.is_current.is_(True))).scalar_one()


def duplicate_views(job_id) -> int:
    with session_scope() as s:
        return s.execute(
            text("SELECT count(*) FROM (SELECT planned_view_id FROM observations WHERE job_id = :j AND is_current GROUP BY planned_view_id HAVING count(*) > 1) d"),
            {"j": job_id},
        ).scalar_one()


def _new_job(db, **job_kw):
    p = make_project(db)
    plan = make_plan(db, p, sampling_interval_m=20, include_crossing_approaches=True)
    return make_job(db, plan, **job_kw)


def test_synthetic_job_runs_to_completion_with_honest_metadata(db, api_server, data_dir):
    job = _new_job(db, mock={"fault_by_ordinal": {"2": "blank", "4": "error"}})
    sup = start_supervisor()
    try:
        wait_for(lambda: job_state(job.id)[0] == "completed", what="job completion")
    finally:
        stop(sup)
    status, counts, _, _ = job_state(job.id)
    assert set(counts) <= {"verified", "needs_review", "no_imagery"}
    assert duplicate_views(job.id) == 0
    with session_scope() as s:
        obs = s.execute(select(Observation).where(Observation.job_id == job.id)).scalars().all()
        assert obs
        for o in obs:
            p = data_dir / o.image_path
            assert p.is_file() and o.sha256 and o.image_width == 800 and o.image_height == 500
            assert o.camera_position_provenance == "page_url" and o.target == "synthetic"
            assert o.collected_at is not None
            if o.imagery_date is None:
                assert o.imagery_date_precision == "unknown" and "imagery_date_unknown" in o.quality_flags
            assert "Synthetic" in (o.attribution or "")
        # injected faults were rejected/retried and the evidence is kept in the attempt record
        ev = dict(s.execute(text("SELECT t.ordinal, a.evidence FROM task_attempts a JOIN tasks t ON t.id = a.task_id WHERE a.job_id = :j AND a.outcome = 'captured'"), {"j": job.id}).all())
        assert any({"blank_frame", "uniform_frame"} & set(f.get("flags", [])) for f in ev[2].get("rejected_frames", []))
        assert ev[4].get("navigation_retries", [{}])[0].get("mode") == "error"


def test_pause_resume_and_kill9_restart_preserve_progress(db, api_server):
    job = _new_job(db, requests_per_minute=40)
    sup = start_supervisor()
    try:
        wait_for(lambda: current_obs(job.id) >= 3, what="first captures")
        with session_scope() as s:
            jobsvc.pause_job(s, s.get(Job, job.id), "operator", "test pause")
        # workers release their task and stop claiming
        wait_for(lambda: job_state(job.id)[1].get("running", 0) == 0, timeout=40, what="release on pause")
        before = current_obs(job.id)
        time.sleep(3)
        assert current_obs(job.id) == before  # nothing collected while paused
        with session_scope() as s:
            jobsvc.resume_job(s, s.get(Job, job.id))
        wait_for(lambda: current_obs(job.id) > before, what="progress after resume")
        # simulate a hard crash mid-task
        wait_for(lambda: job_state(job.id)[1].get("running", 0) >= 1, timeout=30, what="a running task")
        sup.send_signal(signal.SIGKILL)
        sup.wait(timeout=10)
        at_crash = current_obs(job.id)
    finally:
        stop(sup)
    assert job_state(job.id)[0] == "running"
    sup2 = start_supervisor()  # restart: dead slot is detected and its task requeued
    try:
        wait_for(lambda: job_state(job.id)[0] == "completed", timeout=150, what="completion after restart")
    finally:
        stop(sup2)
    assert current_obs(job.id) >= at_crash
    assert duplicate_views(job.id) == 0
    with session_scope() as s:
        crashed = s.execute(text("SELECT count(*) FROM task_attempts WHERE job_id = :j AND outcome = 'lease_expired'"), {"j": job.id}).scalar_one()
    assert crashed >= 1


def test_captcha_pauses_for_operator_and_resumes_after_manual_click(db, api_server):
    job = _new_job(db, mock={"fault_by_ordinal": {"2": "captcha"}})
    sup = start_supervisor()
    try:
        wait_for(lambda: job_state(job.id)[2] == "manual_intervention", what="manual intervention pause")
        status, counts, kind, reason = job_state(job.id)
        assert status == "paused" and counts.get("manual_required") == 1 and "operator" in reason.lower()
        with session_scope() as s:
            w = s.execute(select(Worker).where(Worker.job_id == job.id, Worker.status == "awaiting_operator")).scalars().one()
            wid = w.id
        # the operator clicks the interstitial's continue button via the command channel
        r = httpx.post(f"{api_server}/api/workers/{wid}/commands", json={"command": "click", "payload": {"x": 400, "y": 420}})
        assert r.status_code == 200, r.text
        wait_for(lambda: httpx.get(f"{api_server}/api/workers/{wid}/commands").json()[0]["status"] == "done", what="command executed")
        httpx.post(f"{api_server}/api/jobs/{job.id}/resume")
        wait_for(lambda: job_state(job.id)[0] == "completed", what="completion after intervention")
    finally:
        stop(sup)
    assert duplicate_views(job.id) == 0


def _fake_openai():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.fake_openai:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"], cwd=BACKEND)
    base = f"http://127.0.0.1:{port}"
    wait_for(lambda: _ok(base + "/_received"), timeout=20, what="fake openai")
    return proc, base


def _ok(url):
    try:
        return httpx.get(url, timeout=1).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _openai_job(db, **cfg):
    p = make_project(db, permitted_access_methods=["openai_agent", "deterministic_browser"], **cfg)
    plan = make_plan(db, p, sampling_interval_m=60, include_crossing_approaches=False)
    from app.config import get_settings
    from app.schemas import JobCreate

    settings = get_settings().model_copy(update={"openai_api_key": "sk-fake", "openai_computer_use_model": "fake-cua", "allow_synthetic_cloud_image_processing": True})
    job = jobsvc.create_job(db, plan, JobCreate(target="synthetic", method="openai_agent", model_budget_usd=5, requests_per_minute=600, mock={"fault_by_ordinal": {"1": "overlay"}}), settings=settings)
    jobsvc.start_job(db, job, settings=settings)
    db.commit()
    return job


def test_openai_agent_recovers_overlay_with_validated_actions(db, api_server):
    fake, base = _fake_openai()
    job = _openai_job(db)
    sup = start_supervisor(openai_api_key="sk-fake", openai_computer_use_model="fake-cua", openai_base_url=base + "/v1", allow_synthetic_cloud_image_processing="true", openai_startup_check="models")
    try:
        wait_for(lambda: job_state(job.id)[0] == "completed", what="agent job completion")
        received = httpx.get(base + "/_received").json()
    finally:
        stop(sup)
        fake.terminate()
    assert len(received) == 2  # screenshot request, then an action batch; state verified without a 3rd call
    assert received[0]["has_image"] is False and received[1]["has_image"] is True
    assert all(r["store"] is False for r in received) and received[0]["tools"] == [{"type": "computer"}]
    with session_scope() as s:
        usage = s.execute(select(ModelUsage).where(ModelUsage.job_id == job.id)).scalars().all()
        assert len(usage) == 2 and all(u.estimated_cost_usd > 0 for u in usage)
        rejected = s.execute(text("SELECT message FROM events WHERE job_id = :j AND kind = 'agent_actions_rejected'"), {"j": job.id}).scalars().all()
        assert rejected and "keys not permitted" in rejected[0]
        first = s.execute(select(Observation).join(Task, Task.id == Observation.task_id).where(Observation.job_id == job.id, Task.ordinal == 1)).scalars().first()
        assert first.capture_method == "openai_agent"
        decisions = s.execute(select(EgressEvent.decision, EgressEvent.data_class).where(EgressEvent.job_id == job.id)).all()
        assert ("allowed", "synthetic") in decisions


def test_openai_agent_cannot_send_screenshot_when_image_egress_disabled(db, api_server):
    fake, base = _fake_openai()
    job = _openai_job(db)
    # Job was validated with the synthetic flag on; the worker now runs with it OFF.
    sup = start_supervisor(openai_api_key="sk-fake", openai_computer_use_model="fake-cua", openai_base_url=base + "/v1", allow_synthetic_cloud_image_processing="false", allow_cloud_image_processing="false")
    try:
        wait_for(lambda: job_state(job.id)[2] == "error", what="job paused on blocked egress")
        received = httpx.get(base + "/_received").json()
    finally:
        stop(sup)
        fake.terminate()
    assert received and all(r["has_image"] is False for r in received)  # no image ever reached the provider
    status, counts, kind, reason = job_state(job.id)
    assert status == "paused" and "blocked" in reason.lower()
    with session_scope() as s:
        blocked = s.execute(select(EgressEvent).where(EgressEvent.job_id == job.id, EgressEvent.decision == "blocked")).scalars().all()
        assert blocked and blocked[0].image_count > 0


def test_two_workers_share_a_job_without_duplicates(db, api_server):
    job = _new_job(db, worker_count=2, requests_per_minute=600)
    sup = start_supervisor()
    try:
        wait_for(lambda: job_state(job.id)[0] == "completed", what="two-worker completion")
    finally:
        stop(sup)
    assert duplicate_views(job.id) == 0
    with session_scope() as s:
        owners = set(s.execute(text("SELECT DISTINCT worker_id FROM task_attempts WHERE job_id = :j"), {"j": job.id}).scalars())
        profiles = set(s.execute(text("SELECT DISTINCT browser_profile_dir FROM workers WHERE job_id = :j"), {"j": job.id}).scalars())
    assert len(owners) == 2  # both slots did work
    assert len(profiles) == 2  # isolated browser profiles
