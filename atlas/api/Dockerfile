# Public route preview API (Railway service `atlas-api`). Built only from the staged
# directory that api/stage.sh assembles: see that script for exactly what is included.
FROM ghcr.io/astral-sh/uv:0.11.28 AS uv

FROM python:3.13.7-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /build
COPY pyproject.toml uv.lock ./
# Locked runtime dependencies only: no dev group, no build backend, no project install.
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.13.7-slim-bookworm
RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app
COPY --from=build /app/.venv /app/.venv
COPY src/atlas /app/src/atlas
# OSM-derived street classes (ODbL). Attribution is inside the file and in NOTICE.
COPY var/osm_way_designations.json /app/var/osm_way_designations.json
COPY NOTICE /app/NOTICE
# Remote upload/build caches can retain restrictive source directory modes. Set the
# runtime permissions in the image itself, independently of the uploader's umask.
RUN chmod -R a+rX /app/src /app/var
ENV PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ATLAS_DESIGNATIONS_FILE=/app/var/osm_way_designations.json \
    ATLAS_API_CLIENT_IP_HEADER="" \
    PORT=8080
WORKDIR /app
USER 10001:10001
RUN python -c "from pathlib import Path; import atlas.routing.production; from atlas.routing.designation import load; assert load(Path('/app/var/osm_way_designations.json'))"
EXPOSE 8080
CMD ["python", "-m", "atlas.routing.production"]
