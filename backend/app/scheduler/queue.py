"""Durable task queue on PostgreSQL.

Guarantees
----------
* A task is claimed by at most one worker at a time (`FOR UPDATE SKIP LOCKED`).
* Each claim mints a new `lease_token`. Heartbeats, failures and completions
  must present the current token while the task is still owned; anything else
  is a stale worker and is rejected (fencing).
* Expired leases (crashed / stalled workers) are recovered by a sweeper that
  requeues with backoff or fails the task once `max_attempts` is exhausted.
* Request pacing is a shared leaky bucket stored on the job row, so the rate
  limit holds across workers and processes.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.events import emit
from app.models import TaskAttempt


@dataclass
class Claim:
    task_id: uuid.UUID
    job_id: uuid.UUID
    sample_id: uuid.UUID
    segment_key: str
    ordinal: int
    lease_token: uuid.UUID
    attempt_no: int
    attempt_id: uuid.UUID
    view_filter: list | None


def claim_next_task(s: Session, job_id: uuid.UUID, worker_id: str, lease_seconds: int | None = None) -> Claim | None:
    lease_seconds = lease_seconds or get_settings().lease_seconds
    token = uuid.uuid4()
    row = s.execute(
        text(
            """
            WITH next AS (
                SELECT t.id
                FROM tasks t
                JOIN jobs j ON j.id = t.job_id
                WHERE t.job_id = :job
                  AND j.status = 'running'
                  AND t.status = 'queued'
                  AND t.next_attempt_at <= now()
                ORDER BY t.ordinal
                FOR UPDATE OF t SKIP LOCKED
                LIMIT 1
            )
            UPDATE tasks t
               SET status = 'running',
                   lease_owner = :worker,
                   lease_token = :token,
                   lease_expires_at = now() + make_interval(secs => :lease),
                   heartbeat_at = now(),
                   attempt_count = t.attempt_count + 1,
                   started_at = COALESCE(t.started_at, now()),
                   updated_at = now()
              FROM next
             WHERE t.id = next.id
         RETURNING t.id, t.job_id, t.sample_id, t.segment_key, t.ordinal, t.attempt_count, t.view_filter
            """
        ),
        {"job": job_id, "worker": worker_id, "token": token, "lease": lease_seconds},
    ).first()
    if row is None:
        return None
    attempt = TaskAttempt(
        task_id=row.id,
        job_id=row.job_id,
        attempt_no=row.attempt_count,
        worker_id=worker_id,
        lease_token=token,
    )
    s.add(attempt)
    s.flush()
    s.execute(
        text("UPDATE workers SET current_task_id = :t, status = 'running', heartbeat_at = now() WHERE id = :w"),
        {"t": row.id, "w": worker_id},
    )
    return Claim(
        task_id=row.id,
        job_id=row.job_id,
        sample_id=row.sample_id,
        segment_key=row.segment_key,
        ordinal=row.ordinal,
        lease_token=token,
        attempt_no=row.attempt_count,
        attempt_id=attempt.id,
        view_filter=row.view_filter,
    )


def renew_lease(s: Session, task_id: uuid.UUID, token: uuid.UUID, lease_seconds: int | None = None) -> bool:
    lease_seconds = lease_seconds or get_settings().lease_seconds
    r = s.execute(
        text(
            """
            UPDATE tasks
               SET lease_expires_at = now() + make_interval(secs => :lease), heartbeat_at = now()
             WHERE id = :t AND lease_token = :tok AND status IN ('running', 'manual_required')
         RETURNING id
            """
        ),
        {"t": task_id, "tok": token, "lease": lease_seconds},
    ).first()
    return r is not None


def lock_owned_task(s: Session, task_id: uuid.UUID, token: uuid.UUID, statuses=("running", "manual_required")):
    """Row-lock the task iff `token` is still the current lease. Returns the row or None (stale)."""
    return _lock(s, task_id, token, statuses)


def _lock(s: Session, task_id, token, statuses):
    row = s.execute(text("SELECT * FROM tasks WHERE id = :t FOR UPDATE"), {"t": task_id}).mappings().first()
    if row is None or row["lease_token"] != token or row["status"] not in statuses:
        return None
    return row


def backoff_seconds(failures: int) -> float:
    st = get_settings()
    base = st.retry_backoff_base_seconds * (2 ** max(0, failures - 1))
    return min(st.retry_backoff_max_seconds, base) * (0.8 + 0.4 * random.random())


def _end_attempt(s: Session, task_id, token, outcome: str, error: str | None = None, evidence: dict | None = None, model_calls=None, actions=None):
    s.execute(
        text(
            """
            UPDATE task_attempts
               SET outcome = :o, error = :e, ended_at = now(),
                   evidence = COALESCE(CAST(:ev AS jsonb), evidence),
                   model_calls = COALESCE(:mc, model_calls),
                   actions = COALESCE(:ac, actions)
             WHERE task_id = :t AND lease_token = :tok AND outcome IN ('running', 'manual_required')
            """
        ),
        {"o": outcome, "e": error, "t": task_id, "tok": token, "ev": None if evidence is None else _json(evidence), "mc": model_calls, "ac": actions},
    )


def _json(d: dict) -> str:
    import json

    return json.dumps(d, default=str)


def fail_task(s: Session, task_id: uuid.UUID, token: uuid.UUID, error: str, *, retryable: bool = True, worker_id: str | None = None, evidence: dict | None = None) -> str:
    """Record a failed attempt. Returns the new status, or 'stale' if the lease was lost."""
    row = _lock(s, task_id, token, ("running", "manual_required"))
    if row is None:
        return "stale"
    failures = row["failure_count"] + 1
    if retryable and failures < row["max_attempts"]:
        status = "queued"
        delay = backoff_seconds(failures)
    else:
        status = "failed"
        delay = 0.0
    s.execute(
        text(
            """
            UPDATE tasks
               SET status = :st, failure_count = :f, last_error = :e,
                   next_attempt_at = now() + make_interval(secs => :d),
                   lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                   finished_at = CASE WHEN :is_failed THEN now() ELSE NULL END,
                   outcome_reason = CASE WHEN :is_failed THEN :e ELSE outcome_reason END,
                   updated_at = now()
             WHERE id = :t
            """
        ),
        {"st": status, "f": failures, "e": error[:2000], "d": delay, "t": task_id, "is_failed": status == "failed"},
    )
    _end_attempt(s, task_id, token, "failed", error[:2000], evidence)
    emit(
        s,
        "task_failed" if status == "failed" else "task_retry_scheduled",
        f"Task {row['ordinal']} attempt failed: {error[:300]}" + (f" (retry in {delay:.0f}s)" if status == "queued" else " (no retries left)"),
        level="warning",
        job_id=row["job_id"],
        task_id=task_id,
        worker_id=worker_id,
    )
    _clear_worker(s, worker_id)
    return status


def release_task(s: Session, task_id: uuid.UUID, token: uuid.UUID, reason: str, *, worker_id: str | None = None) -> str:
    """Give the task back without counting a failure (pause / shutdown)."""
    row = _lock(s, task_id, token, ("running", "manual_required"))
    if row is None:
        return "stale"
    job_status = s.execute(text("SELECT status FROM jobs WHERE id = :j"), {"j": row["job_id"]}).scalar()
    new_status = "cancelled" if job_status == "cancelled" else "queued"
    s.execute(
        text(
            """
            UPDATE tasks SET status = :st, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                   next_attempt_at = now(), updated_at = now(),
                   finished_at = CASE WHEN :is_cancelled THEN now() ELSE finished_at END
             WHERE id = :t
            """
        ),
        {"st": new_status, "t": task_id, "is_cancelled": new_status == "cancelled"},
    )
    _end_attempt(s, task_id, token, "interrupted", reason)
    _clear_worker(s, worker_id)
    return new_status


def mark_manual_required(s: Session, task_id: uuid.UUID, token: uuid.UUID, reason: str) -> bool:
    row = _lock(s, task_id, token, ("running",))
    if row is None:
        return False
    s.execute(
        text("UPDATE tasks SET status = 'manual_required', outcome_reason = :r, updated_at = now() WHERE id = :t"),
        {"r": reason[:2000], "t": task_id},
    )
    s.execute(
        text("UPDATE task_attempts SET outcome = 'manual_required' WHERE task_id = :t AND lease_token = :tok AND outcome = 'running'"),
        {"t": task_id, "tok": token},
    )
    return True


def mark_running_again(s: Session, task_id: uuid.UUID, token: uuid.UUID) -> bool:
    row = _lock(s, task_id, token, ("manual_required",))
    if row is None:
        return False
    s.execute(text("UPDATE tasks SET status = 'running', updated_at = now() WHERE id = :t"), {"t": task_id})
    s.execute(
        text("UPDATE task_attempts SET outcome = 'running' WHERE task_id = :t AND lease_token = :tok AND outcome = 'manual_required'"),
        {"t": task_id, "tok": token},
    )
    return True


def _clear_worker(s: Session, worker_id: str | None):
    if worker_id:
        s.execute(text("UPDATE workers SET current_task_id = NULL, status = 'idle' WHERE id = :w"), {"w": worker_id})


def recover_expired_leases(s: Session) -> list[dict]:
    """Requeue (or fail) tasks whose lease expired. Safe to run concurrently."""
    rows = s.execute(
        text(
            """
            SELECT id, job_id, ordinal, lease_owner, lease_token, failure_count, max_attempts
              FROM tasks
             WHERE status IN ('running', 'manual_required') AND lease_expires_at < now()
             FOR UPDATE SKIP LOCKED
            """
        )
    ).mappings().all()
    recovered = []
    for r in rows:
        failures = r["failure_count"] + 1
        status = "queued" if failures < r["max_attempts"] else "failed"
        delay = backoff_seconds(failures) if status == "queued" else 0
        s.execute(
            text(
                """
                UPDATE tasks SET status = :st, failure_count = :f,
                       last_error = 'lease expired: worker stalled or crashed',
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                       next_attempt_at = now() + make_interval(secs => :d),
                       finished_at = CASE WHEN :is_failed THEN now() ELSE NULL END,
                       outcome_reason = CASE WHEN :is_failed THEN 'lease expired; retries exhausted' ELSE outcome_reason END,
                       updated_at = now()
                 WHERE id = :t
                """
            ),
            {"st": status, "f": failures, "d": delay, "t": r["id"], "is_failed": status == "failed"},
        )
        s.execute(
            text("UPDATE task_attempts SET outcome = 'lease_expired', ended_at = now() WHERE task_id = :t AND lease_token = :tok AND outcome IN ('running','manual_required')"),
            {"t": r["id"], "tok": r["lease_token"]},
        )
        emit(
            s,
            "lease_expired",
            f"Recovered task {r['ordinal']} from {r['lease_owner']} (lease expired) -> {status}",
            level="warning",
            job_id=r["job_id"],
            task_id=r["id"],
            worker_id=r["lease_owner"],
        )
        recovered.append({"task_id": r["id"], "status": status, "worker": r["lease_owner"]})
    return recovered


def mark_stalled_workers(s: Session) -> int:
    st = get_settings()
    r = s.execute(
        text(
            """
            UPDATE workers SET status = 'stalled', message = 'no heartbeat for ' || :s || 's'
             WHERE status IN ('starting', 'idle', 'running', 'awaiting_operator')
               AND heartbeat_at < now() - make_interval(secs => :s)
            RETURNING id
            """
        ),
        {"s": st.stalled_after_seconds},
    ).all()
    return len(r)


def reserve_request_slot(s: Session, job_id: uuid.UUID, requests_per_minute: float) -> float:
    """Reserve the next request slot for a job. Returns seconds to wait before using it."""
    interval = 60.0 / max(0.001, requests_per_minute)
    row = s.execute(
        text(
            """
            UPDATE jobs
               SET last_dispatch_at = GREATEST(now(), COALESCE(last_dispatch_at, now() - make_interval(secs => :iv)) + make_interval(secs => :iv))
             WHERE id = :j
         RETURNING EXTRACT(EPOCH FROM (last_dispatch_at - now())) AS wait
            """
        ),
        {"j": job_id, "iv": interval},
    ).first()
    return max(0.0, float(row.wait)) if row else 0.0


def now_db(s: Session) -> datetime:
    return s.execute(text("SELECT now()")).scalar_one()
