"""FastAPI application (bind to localhost; see app.config / docker-compose)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import mock_routes, routes
from app.config import get_settings
from app.providers.egress import install_log_redaction
from app.storage import files

os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
install_log_redaction()

settings = get_settings()
settings.ensure_dirs()

app = FastAPI(
    title="maply — local Street View collection supervisor",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)

# DNS-rebinding protection: only loopback names (and the compose service name
# used by the worker container to reach the mock site) are accepted.
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "127.0.0.1,localhost,api,testserver").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

_LOCAL_ORIGINS = ("http://127.0.0.1", "http://localhost")


@app.middleware("http")
async def local_origin_guard(request: Request, call_next):
    """Reject cross-site state-changing requests (no CORS is enabled at all)."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and not origin.startswith(_LOCAL_ORIGINS):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        ct = request.headers.get("content-type", "")
        if request.headers.get("content-length", "0") not in ("0", "") and "application/json" not in ct:
            return JSONResponse({"detail": "JSON body required"}, status_code=415)
    return await call_next(request)


app.include_router(routes.router)
if settings.enable_mock_panorama:
    app.include_router(mock_routes.router)

_ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".json", ".geojson", ".jsonl", ".txt"}


@app.get("/files/{relpath:path}", include_in_schema=False)
def serve_file(relpath: str):
    try:
        p = files.resolve_rel(relpath)
    except files.StorageError as e:
        raise HTTPException(403, str(e)) from e
    if p.suffix.lower() not in _ALLOWED_SUFFIXES or not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, headers={"Cache-Control": "no-store" if "/workers/" in relpath else "private, max-age=3600"})


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


# Built dashboard (frontend/dist), served from the same localhost origin.
_DIST = Path(os.environ.get("FRONTEND_DIST", Path(__file__).resolve().parents[3] / "frontend" / "dist"))
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith(("api/", "files/", "mock/")):
            raise HTTPException(404)
        candidate = (_DIST / path).resolve()
        if path and candidate.is_file() and _DIST in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html", headers={"Cache-Control": "no-store"})


def run() -> None:
    import uvicorn

    uvicorn.run("app.api.main:app", host=settings.app_host, port=settings.app_port, reload=False)


if __name__ == "__main__":
    run()
