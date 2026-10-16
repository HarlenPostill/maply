from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.config import ALL_ACCESS_METHODS, Settings
from app.geo.planning import PlanParams

DEFAULT_PURPOSE = (
    "Wheelchair-accessibility mapping research for an educational institution. "
    "Imagery is stored and used locally for this research purpose only; "
    "no publication or unrelated reuse."
)


class ProjectConfig(BaseModel):
    """Per-project operational limits derived from the research approval."""

    purpose: str = DEFAULT_PURPOSE
    approval_reference: str = ""
    approval_expires_on: date | None = None
    permitted_access_methods: list[str] = Field(default_factory=lambda: ["deterministic_browser", "manual", "openai_agent"])
    permitted_targets: list[str] = Field(default_factory=lambda: ["synthetic", "google_maps"])
    # Third-party (cloud) processing of research imagery. Must ALSO be enabled
    # by the operator via ALLOW_CLOUD_IMAGE_PROCESSING.
    allow_third_party_processing: bool = False
    third_party_processors: list[str] = Field(default_factory=list)
    max_captures_total: int = Field(2000, ge=1)
    max_workers: int = Field(2, ge=1, le=16)
    max_requests_per_minute: float = Field(10.0, gt=0)
    retention_days: int = Field(365, ge=1)
    scope_buffer_m: float = Field(0.0, ge=0.0, le=200.0)


class EffectiveLimits(BaseModel):
    permitted_access_methods: list[str]
    permitted_targets: list[str]
    max_workers: int
    max_requests_per_minute: float
    max_captures_total: int
    cloud_image_processing_allowed: bool
    synthetic_cloud_image_processing_allowed: bool
    retention_days: int
    approval_expires_on: date | None
    reasons: list[str]


def effective_limits(cfg: ProjectConfig, settings: Settings) -> EffectiveLimits:
    """Project settings may only narrow operator-level settings."""
    reasons: list[str] = []
    methods = [m for m in cfg.permitted_access_methods if m in settings.permitted_access_methods and m in ALL_ACCESS_METHODS]
    targets = [t for t in cfg.permitted_targets if t in settings.permitted_targets]
    cloud = bool(settings.allow_cloud_image_processing and cfg.allow_third_party_processing and "openai" in cfg.third_party_processors)
    if not settings.allow_cloud_image_processing:
        reasons.append("ALLOW_CLOUD_IMAGE_PROCESSING is disabled by the operator")
    elif not cfg.allow_third_party_processing:
        reasons.append("Project does not permit third-party processing")
    elif "openai" not in cfg.third_party_processors:
        reasons.append("OpenAI is not listed as a permitted third-party processor for this project")
    return EffectiveLimits(
        permitted_access_methods=methods,
        permitted_targets=targets,
        max_workers=min(cfg.max_workers, settings.max_workers),
        max_requests_per_minute=min(cfg.max_requests_per_minute, settings.max_requests_per_minute),
        max_captures_total=cfg.max_captures_total,
        cloud_image_processing_allowed=cloud,
        synthetic_cloud_image_processing_allowed=settings.allow_synthetic_cloud_image_processing,
        retention_days=cfg.retention_days,
        approval_expires_on=cfg.approval_expires_on,
        reasons=reasons,
    )


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    config: ProjectConfig = Field(default_factory=ProjectConfig)
    scope_geojson: dict | None = None  # defaults to SCOPE_GEOJSON_PATH


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    config: ProjectConfig | None = None
    scope_geojson: dict | None = None


class RouteCreate(BaseModel):
    name: str = ""
    kind: Literal["route", "polygon", "import", "streetview_seed"] = "route"
    geojson: dict
    properties: dict = Field(default_factory=dict)


class StreetViewParseRequest(BaseModel):
    url: str


class PlanCreate(BaseModel):
    name: str = ""
    route_ids: list[uuid.UUID]
    params: PlanParams = Field(default_factory=PlanParams)
    seed_urls: list[str] = Field(default_factory=list)


class SampleUpdate(BaseModel):
    lng: float | None = None
    lat: float | None = None
    excluded: bool | None = None
    note: str | None = None
    reset_position: bool = False


class JobCreate(BaseModel):
    name: str = ""
    target: Literal["synthetic", "google_maps"] = "synthetic"
    method: Literal["deterministic_browser", "openai_agent", "manual", "streetview_static_api"] = "deterministic_browser"
    worker_count: int = Field(1, ge=1, le=16)
    requests_per_minute: float = Field(6.0, gt=0, le=600)
    max_captures: int = Field(200, ge=1)
    max_duration_minutes: float = Field(60.0, gt=0)
    model_budget_usd: float = Field(0.0, ge=0)
    max_attempts: int = Field(3, ge=1, le=10)
    # Optional analysis crop applied after capture (raw is always kept).
    analysis_crop: dict | None = None
    # Mock-site fault injection for synthetic demonstrations/tests.
    mock: dict = Field(default_factory=dict)


class ReviewRequest(BaseModel):
    decision: Literal["accept", "reject"]
    note: str = ""
    recapture: bool = False
    # Operator confirms the intended footpath is actually visible in the image.
    confirm_association: bool = False


class RecaptureRequest(BaseModel):
    view_ids: list[uuid.UUID] | None = None
    reason: str = "operator requested recapture"


class CropRequest(BaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    purpose: str = "analysis"


class WorkerCommandRequest(BaseModel):
    command: Literal[
        "click",
        "double_click",
        "drag",
        "key",
        "scroll",
        "goto_view",
        "capture",
        "no_imagery",
        "done",
        "skip",
        "resume_task",
        "refresh",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


class ExportRequest(BaseModel):
    job_id: uuid.UUID | None = None
    include: Literal["verified", "verified_and_review", "all_current"] = "verified_and_review"
    copy_mode: Literal["hardlink", "copy"] = "hardlink"
