"""HTTP API: GET /v1/health, GET /v1/coverage, POST /v1/route-preview.

Nothing about a request is stored. Logs hold the request id, method, path, status,
error code and duration, never coordinates or connection details.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
import uuid

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from atlas.routing.policy import API_VERSION
from atlas.routing.router import Cancelled, RouteError, coverage
from atlas.routing.service import Service

log = logging.getLogger('atlas.api')
MAX_BODY = 4096


class LatLng(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class RoutePreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    origin: LatLng
    destination: LatLng
    expected_graph_version: str | None = Field(default=None, max_length=64)


def error(status: int, code: str, message: str, details: dict | None = None, retry_after: int | None = None) -> JSONResponse:
    headers = {'Cache-Control': 'no-store'}
    if retry_after is not None:
        headers['Retry-After'] = str(retry_after)
    response = JSONResponse({'api_version': API_VERSION, 'error': {'code': code, 'message': message, 'details': details or {}}},
                            status_code=status, headers=headers)
    response.headers['X-Error-Code'] = code
    return response


class RequestLog:
    """Plain ASGI middleware: request id, no-store, one log line without coordinates.

    It passes `receive` through untouched. Starlette's BaseHTTPMiddleware wraps receive in
    a way that hides client disconnects from the endpoint, which would stop cancellation.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        rid = uuid.uuid4().hex[:12]
        started = time.monotonic()
        seen = {'status': 500, 'code': '-'}

        async def send_with_headers(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                headers['X-Request-Id'] = rid
                if 'cache-control' not in headers:
                    headers['Cache-Control'] = 'no-store'
                seen['status'], seen['code'] = message['status'], headers.get('x-error-code', '-')
            await send(message)
        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            log.info('%s %s %s %d %s %dms', rid, scope.get('method'), scope.get('path'), seen['status'], seen['code'],
                     (time.monotonic() - started) * 1000)


def create_app(service: Service, max_concurrency: int = 4) -> FastAPI:
    app = FastAPI(title='Maply route preview', version=API_VERSION, docs_url=None, redoc_url=None)
    slots = threading.BoundedSemaphore(max_concurrency)
    app.state.route_slots = slots

    app.add_middleware(RequestLog)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 405:
            return error(405, 'method_not_allowed', 'Method not allowed.')
        if exc.status_code == 404:
            return error(404, 'not_found', 'Not found.')
        return error(exc.status_code, 'invalid_request', 'The request was not accepted.')

    @app.exception_handler(Exception)
    async def internal(request: Request, exc: Exception):
        log.error('internal error: %s', type(exc).__name__)
        return error(500, 'internal_error', 'Something went wrong.')

    def unavailable():
        state = service.state
        return error(503, 'data_unavailable', 'The atlas snapshot is not available yet.' if state.error == 'loading'
                     else 'The atlas snapshot could not be loaded.', {'reason': state.error}, retry_after=30)

    @app.get('/v1/health')
    async def health():
        state = service.state
        if state.graph is None:
            return unavailable()
        return {'status': 'ready', 'api_version': API_VERSION, 'release_id': state.graph.snapshot.release.id,
                'graph_version': state.graph.version}

    @app.get('/v1/coverage')
    async def get_coverage():
        state = service.state
        if state.graph is None:
            return unavailable()
        return coverage(state.graph)

    @app.post('/v1/route-preview')
    async def route_preview(request: Request):
        state = service.state
        if state.graph is None:
            return unavailable()
        media = request.headers.get('content-type', '').split(';')[0].strip().lower()
        if media != 'application/json':
            return error(415, 'unsupported_media_type', 'Send application/json.')
        declared = request.headers.get('content-length')
        if declared and declared.isdigit() and int(declared) > MAX_BODY:
            return error(413, 'payload_too_large', 'The request body is too large.', {'max_bytes': MAX_BODY})
        body = b''
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                return error(413, 'payload_too_large', 'The request body is too large.', {'max_bytes': MAX_BODY})
        try:
            req = RoutePreviewRequest.model_validate(json.loads(body))
        except (ValueError, ValidationError) as exc:
            fields = ['.'.join(str(p) for p in e['loc']) for e in exc.errors()] if isinstance(exc, ValidationError) else []
            return error(400, 'invalid_request', 'The request body is not a valid route preview request.', {'fields': fields})
        graph_version = state.graph.version
        if req.expected_graph_version and req.expected_graph_version != graph_version:
            return error(409, 'snapshot_changed', 'The atlas changed since the client last read it.',
                         {'graph_version': graph_version})
        if not slots.acquire(blocking=False):
            return error(503, 'busy', 'Too many route searches at once.', retry_after=2)
        cancel = threading.Event()

        async def watch():
            while not cancel.is_set():
                if await request.is_disconnected():
                    cancel.set()
                    return
                await asyncio.sleep(0.1)
        watcher = asyncio.create_task(watch())
        try:
            result = await anyio.to_thread.run_sync(
                lambda: state.router.route((req.origin.lng, req.origin.lat), (req.destination.lng, req.destination.lat), cancel))
            return JSONResponse(result, headers={'Cache-Control': 'no-store'})
        except RouteError as exc:
            return error(exc.status, exc.code, exc.message, exc.details)
        except Cancelled:
            return Response(status_code=499)
        finally:
            cancel.set()
            watcher.cancel()
            slots.release()

    return app


def serve(service: Service, host: str, port: int, refresh_s: float):
    import uvicorn
    service.run_background(refresh_s)
    uvicorn.run(create_app(service), host=host, port=port, access_log=False, log_level='info')
