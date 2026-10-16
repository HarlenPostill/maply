from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import Event
from app.providers.egress import redact

log = logging.getLogger("maply.events")


def emit(
    s: Session,
    kind: str,
    message: str,
    *,
    level: str = "info",
    project_id=None,
    job_id=None,
    task_id=None,
    worker_id: str | None = None,
    data: dict | None = None,
) -> None:
    message = redact(message)
    s.add(
        Event(
            kind=kind,
            message=message,
            level=level,
            project_id=project_id,
            job_id=job_id,
            task_id=task_id,
            worker_id=worker_id,
            data=data or {},
        )
    )
    getattr(log, "warning" if level == "warning" else ("error" if level == "error" else "info"))(
        "[%s] %s", kind, message
    )
