"""Test configuration.

Tests never touch project data: they use the `maply_test` database
(TEST_DATABASE_URL) and a temporary DATA_DIR. No test contacts Google or
OpenAI; provider calls go to in-process mock transports.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://maply:maply@127.0.0.1:5433/maply_test")
_TMP = Path(tempfile.mkdtemp(prefix="maply-test-data-"))

TEST_ENV = {
    "DATABASE_URL": TEST_DB,
    "DATA_DIR": str(_TMP),
    "SCOPE_GEOJSON_PATH": str(BACKEND.parent / "config" / "city_of_sydney_approx.geojson"),
    "ALLOW_CLOUD_IMAGE_PROCESSING": "false",
    "ALLOW_SYNTHETIC_CLOUD_IMAGE_PROCESSING": "false",
    "OPENAI_API_KEY": "",
    "OPENAI_COMPUTER_USE_MODEL": "",
    "ENABLE_MOCK_PANORAMA": "true",
    "MOCK_FAULT_RATE": "0",
    "LEASE_SECONDS": "6",
    "HEARTBEAT_SECONDS": "1",
    "STALLED_AFTER_SECONDS": "5",
    "RETRY_BACKOFF_BASE_SECONDS": "0.2",
    "RETRY_BACKOFF_MAX_SECONDS": "1",
    "WORKER_HEADLESS": "true",
    "OPENAI_AGENTS_DISABLE_TRACING": "1",
    "OPENAI_STARTUP_CHECK": "off",
}
os.environ.update(TEST_ENV)

from app.config import reset_settings_cache  # noqa: E402

reset_settings_cache()


def _db_available() -> bool:
    try:
        import psycopg

        url = TEST_DB.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(url, connect_timeout=3) as c:
            c.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


DB_OK = _db_available()


@pytest.fixture(scope="session")
def migrated():
    if not DB_OK:
        pytest.skip("test database not reachable (set TEST_DATABASE_URL)")
    from app.db import configure_engine

    env = {**os.environ, "ALEMBIC_DATABASE_URL": TEST_DB}
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    configure_engine(TEST_DB)
    return True


TABLES = [
    "egress_events", "events", "model_usage", "crops", "observation_links", "observations", "panoramas",
    "worker_commands", "workers", "task_attempts", "tasks", "jobs", "planned_views", "samples", "plan_nodes",
    "segments", "capture_plans", "routes", "exports", "config_versions", "system_checks", "projects",
]


@pytest.fixture
def db(migrated):
    from sqlalchemy import text

    from app.db import get_engine, session_factory

    with get_engine().begin() as c:
        c.execute(text("TRUNCATE " + ", ".join(TABLES) + " CASCADE"))
    s = session_factory()()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def data_dir() -> Path:
    return _TMP


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


@pytest.fixture(scope="session")
def api_server(migrated):
    """Run the real API (incl. mock panorama site) on a free localhost port."""
    port = _free_port()
    env = {**os.environ, "MOCK_PANORAMA_BASE_URL": f"http://127.0.0.1:{port}", "APP_PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=BACKEND,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    import httpx

    for _ in range(100):
        try:
            if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("API server did not start")
    os.environ["MOCK_PANORAMA_BASE_URL"] = base
    reset_settings_cache()
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:  # noqa: BLE001
        return False
