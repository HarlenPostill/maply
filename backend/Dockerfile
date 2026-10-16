# syntax=docker/dockerfile:1
# Build context is the repository root (see docker-compose.yml).
# The dataset (./data) is excluded by .dockerignore and only ever bind-mounted.

FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Ships Chromium matching playwright==1.63.0 (pinned in requirements.txt).
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    DO_NOT_TRACK=1 \
    OPENAI_AGENTS_DISABLE_TRACING=1 \
    DATA_DIR=/data \
    FRONTEND_DIST=/app/frontend/dist
WORKDIR /app/backend
COPY backend/requirements.txt backend/requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt
COPY backend/ ./
COPY config/ /app/config/
COPY --from=web /web/dist /app/frontend/dist
RUN mkdir -p /data && chown pwuser:pwuser /data
USER pwuser
EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
