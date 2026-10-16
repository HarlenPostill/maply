"""Application settings.

All operational limits that come from the research approval are configurable
here (operator-level, via environment) or per project (stored in the database
and editable in the dashboard). Project settings may only *narrow* the
operator-level limits, never widen them.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Access methods a job can use to obtain imagery.
AccessMethod = Literal["deterministic_browser", "openai_agent", "manual", "streetview_static_api"]
# What the browser points at. "synthetic" is the local mock panorama site.
Target = Literal["synthetic", "google_maps"]

ALL_ACCESS_METHODS: tuple[str, ...] = (
    "deterministic_browser",
    "openai_agent",
    "manual",
    "streetview_static_api",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- service binding -------------------------------------------------
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    database_url: str = "postgresql+psycopg://maply:maply@127.0.0.1:5433/maply"

    # --- local storage ---------------------------------------------------
    data_dir: Path = Path("../data")
    # Where DATA_DIR lives on the host when running in a container (display only).
    host_data_dir: str | None = None

    # --- processing boundary ---------------------------------------------
    # Master switch. When false, no screenshot or image-bearing payload may be
    # sent to any external model provider, from any adapter or tracing path.
    allow_cloud_image_processing: bool = False
    # Synthetic mock imagery is not research data. This lets the OpenAI adapter
    # be exercised against the local mock site without enabling research-image
    # egress. Off by default too.
    allow_synthetic_cloud_image_processing: bool = False

    # --- OpenAI adapter --------------------------------------------------
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_computer_use_model: str | None = None
    # Tool type string sent in the Responses API tool definition. Configurable
    # so a documented change does not require a code change.
    openai_computer_tool_type: str = "computer"
    # Supported request-level data control. This is NOT a zero-retention
    # guarantee; see docs/DATA_BOUNDARIES.md.
    openai_store: bool = False
    openai_startup_check: Literal["off", "models", "probe"] = "models"
    openai_max_model_calls_per_task: int = 12
    openai_max_actions_per_task: int = 30
    # USD per 1M tokens, used for *estimates* only. Defaults are the published
    # standard short-context rates for gpt-6.1-sol as of 2026-10; set these to
    # match the model you configure and re-check the pricing page.
    openai_price_input_per_mtok: float = 2.0
    openai_price_output_per_mtok: float = 10.0
    openai_price_cached_input_per_mtok: float = 0.10

    # --- Google official Street View Static API (optional) ---------------
    google_maps_api_key: str | None = None
    google_maps_url_signing_secret: str | None = None
    # Largest image side requested from the Static API. Google's size limit is
    # not stated on the request page; 640 is the long-standing limit -- verify for your plan.
    static_api_max_size: int = 640

    # --- operator-level ceilings (projects may narrow these) -------------
    max_workers: int = 4
    max_requests_per_minute: float = 20.0
    max_captures_per_job: int = 2000
    permitted_access_methods: list[str] = Field(
        default_factory=lambda: ["deterministic_browser", "manual", "openai_agent"]
    )
    permitted_targets: list[str] = Field(default_factory=lambda: ["synthetic", "google_maps"])
    scope_geojson_path: Path = Path("../config/city_of_sydney_approx.geojson")

    # --- worker runtime --------------------------------------------------
    worker_headless: bool = True
    lease_seconds: int = 60
    heartbeat_seconds: int = 10
    stalled_after_seconds: int = 45
    retry_backoff_base_seconds: float = 5.0
    retry_backoff_max_seconds: float = 120.0

    # --- mock panorama site ----------------------------------------------
    enable_mock_panorama: bool = True
    # Base URL the *worker's browser* uses to reach the mock site.
    mock_panorama_base_url: str = "http://127.0.0.1:8000"
    mock_fault_rate: float = 0.06

    # --- planning basemap (frontend) -------------------------------------
    basemap_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    basemap_attribution: str = "&copy; OpenStreetMap contributors"
    basemap_max_zoom: int = 19

    @field_validator("permitted_access_methods", "permitted_targets", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    # Derived paths ------------------------------------------------------
    @property
    def data_root(self) -> Path:
        return self.data_dir.resolve()

    @property
    def projects_dir(self) -> Path:
        return self.data_root / "projects"

    @property
    def tmp_dir(self) -> Path:
        return self.data_root / "tmp"

    @property
    def browser_state_dir(self) -> Path:
        return self.data_root / "browser-state"

    @property
    def exports_dir(self) -> Path:
        return self.data_root / "exports"

    @property
    def orphans_dir(self) -> Path:
        return self.data_root / "orphans"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    def ensure_dirs(self) -> None:
        for p in (
            self.projects_dir,
            self.tmp_dir,
            self.browser_state_dir,
            self.exports_dir,
            self.orphans_dir,
            self.logs_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)

    def processing_mode(self) -> dict:
        return {
            "allow_cloud_image_processing": self.allow_cloud_image_processing,
            "allow_synthetic_cloud_image_processing": self.allow_synthetic_cloud_image_processing,
            "label": "CLOUD IMAGE PROCESSING ENABLED"
            if self.allow_cloud_image_processing
            else "LOCAL ONLY",
            "openai_model": self.openai_computer_use_model,
            "openai_configured": bool(self.openai_api_key and self.openai_computer_use_model),
            "openai_store": self.openai_store,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
