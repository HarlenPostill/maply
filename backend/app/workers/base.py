"""Worker interface shared by all capture methods.

A driver receives one bounded task ("collect the planned views for sample k
of segment X, within these geographic, action and model limits") and returns
a `TaskOutcome`. The runner -- not the driver, and never a model -- owns
leases, persistence, verification of stored artefacts and job state.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.capture.completion import TaskOutcome
from app.capture.verification import CapturePolicy, PlannedViewInfo
from app.scheduler.queue import Claim

# --------------------------------------------------------------------------
# Outcomes signalled by drivers
# --------------------------------------------------------------------------


class TaskInterrupted(Exception):
    """Job paused/cancelled or worker stopping: release without counting a failure."""


class LeaseLost(TaskInterrupted):
    """Our lease token is no longer current: abandon immediately, never complete."""


class ManualInterventionRequired(Exception):
    def __init__(self, reason: str, kind: str = "manual"):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


class NoImagery(Exception):
    def __init__(self, reason: str, evidence: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.evidence = evidence or {}


class RetryableTaskError(Exception):
    pass


class FatalTaskError(Exception):
    pass


class LimitReached(Exception):
    pass


class TaskSkipped(Exception):
    pass


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------


@dataclass
class JobSnapshot:
    id: uuid.UUID
    project_id: uuid.UUID
    plan_id: uuid.UUID
    target: str
    method: str
    settings: dict
    config_version_id: uuid.UUID
    requests_per_minute: float
    cloud_image_processing_allowed: bool


@dataclass
class SampleSnapshot:
    id: uuid.UUID
    ordinal: int
    kind: str
    lat: float
    lng: float
    segment_id: uuid.UUID
    segment_key: str
    segment_bearing: float
    local_bearing: float


@dataclass
class ReuseCandidate:
    observation_id: uuid.UUID
    camera_to_sample_m: float | None
    heading_delta_deg: float | None


class Runtime(Protocol):
    async def checkpoint(self) -> None: ...
    async def acquire_request_slot(self) -> None: ...
    async def publish_preview(self, png: bytes, note: str = "") -> None: ...
    async def find_reusable(self, pano_id: str, view: PlannedViewInfo) -> ReuseCandidate | None: ...
    async def record_model_usage(self, usage: Any, response_id: str | None, images_sent: int) -> None: ...
    async def budget_remaining_usd(self) -> float: ...
    async def set_status(self, status: str, message: str | None = None) -> None: ...
    async def next_command(self, timeout_s: float = 1.0) -> dict | None: ...
    async def finish_command(self, command_id: int, status: str, result: dict | None = None) -> None: ...
    async def event(self, kind: str, message: str, level: str = "info", data: dict | None = None) -> None: ...


@dataclass
class TaskContext:
    claim: Claim
    worker_id: str
    job: JobSnapshot
    sample: SampleSnapshot
    views: list[PlannedViewInfo]
    policy: CapturePolicy
    runtime: Runtime
    scope_check: Any  # callable(lat, lng) -> bool
    extra: dict = field(default_factory=dict)
    policy_reuse: bool = True

    def in_scope(self, lat: float | None, lng: float | None) -> bool | None:
        if lat is None or lng is None:
            return None
        return bool(self.scope_check(lat, lng))

    def mock_fault(self, first_navigation: bool) -> str | None:
        """Synthetic-only fault injection configured on the job (for demos/tests)."""
        if self.job.target != "synthetic":
            return None
        mock = self.job.settings.get("mock") or {}
        f = (mock.get("fault_by_ordinal") or {}).get(str(self.sample.ordinal))
        if not f:
            return None
        if f == "captcha":
            return f if first_navigation else None
        # transient faults only on the first attempt so retries can succeed
        return f if (first_navigation and self.claim.attempt_no == 1) else None


class CaptureDriver(ABC):
    method: str

    async def start(self) -> None:  # noqa: B027
        pass

    @abstractmethod
    async def collect(self, ctx: TaskContext) -> TaskOutcome: ...

    async def resume_after_intervention(self, ctx: TaskContext) -> TaskOutcome:
        """Called after an operator resolves a manual-intervention state."""
        return await self.collect(ctx)

    async def operator_action(self, cmd: dict) -> dict:
        """Execute an operator command while awaiting intervention (browser drivers)."""
        return {"ok": False, "error": "not supported by this driver"}

    async def close(self) -> None:  # noqa: B027
        pass
