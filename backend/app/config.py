"""Local app settings. Optional vision inference uses a separately running host
worker and the existing Antigravity CLI login; no API keys are stored here."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://maply:maply@127.0.0.1:5433/maply"

    # Where job folders live, as seen by this process...
    data_dir: Path = Path("../data")
    # ...and as seen on your Mac (used in prompts, because Codex runs on the host).
    # In Docker this is set by docker-compose; on the host it defaults to data_dir.
    host_data_dir: str | None = None

    scope_geojson_path: Path = Path("../config/city_of_sydney_approx.geojson")

    # Browser Codex should use (named in prompts and the screenshot command).
    browser_app: str = "Google Chrome"

    # Street lookup for "block out an area" (OpenStreetMap, no key). If the local
    # street file exists (scripts/fetch_streets.py) it is used and no network is needed;
    # otherwise these Overpass servers are tried in order.
    streets_file: Path = Path("../data/osm/city_of_sydney_streets.geojson")
    overpass_urls: str = "https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter,https://overpass.private.coffee/api/interpreter"
    max_area_km2: float = 3.0

    basemap_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    basemap_attribution: str = "&copy; OpenStreetMap contributors"

    @property
    def data_root(self) -> Path:
        return self.data_dir.resolve()

    @property
    def jobs_dir(self) -> Path:
        return self.data_root / "jobs"

    @property
    def host_jobs_dir(self) -> str:
        base = self.host_data_dir or str(self.data_root)
        return base.rstrip("/") + "/jobs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
