"""Cloud-image transmission guard.

Every request to an external model provider must pass through
`GuardedTransport` / `AsyncGuardedTransport`. The guard runs at the HTTP
transport layer -- below the SDK, its retries and any request builders -- so a
new code path cannot bypass it by constructing a request differently.

Decision rule (fail-closed):

* No image content in the request body            -> allowed.
* Image content, data class "synthetic"           -> allowed only if
  ALLOW_SYNTHETIC_CLOUD_IMAGE_PROCESSING=true.
* Image content, data class "research" or unknown -> allowed only if
  ALLOW_CLOUD_IMAGE_PROCESSING=true AND the project permits third-party
  processing by this provider.
* Body cannot be inspected                        -> treated as image content.

Each decision is recorded as an `EgressEvent` without the payload.
"""

from __future__ import annotations

import contextvars
import logging
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx2

from app.config import Settings, get_settings

log = logging.getLogger("maply.egress")


class CloudImageTransmissionBlocked(PermissionError):
    """Raised before any byte of an image-bearing request leaves the machine."""


@dataclass
class EgressContext:
    data_class: str = "research"  # none | synthetic | research
    project_allows_cloud: bool = False
    job_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    worker_id: str | None = None
    extra: dict = field(default_factory=dict)


_current: contextvars.ContextVar[EgressContext | None] = contextvars.ContextVar("maply_egress_ctx", default=None)


@contextmanager
def egress_context(**kwargs) -> Iterator[EgressContext]:
    ctx = EgressContext(**kwargs)
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def current_context() -> EgressContext | None:
    return _current.get()


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

_PATTERNS = [
    re.compile(rb"data:image/[a-zA-Z0-9.+-]+;base64,"),
    re.compile(rb'"type"\s*:\s*"(?:input_image|computer_screenshot|image_url|image)"'),
    re.compile(rb'"image_url"\s*:'),
    re.compile(rb"iVBORw0KGgo"),  # base64 PNG signature
    re.compile(rb'"/9j/'),  # base64 JPEG signature at the start of a JSON string
    re.compile(rb"UklGR[a-zA-Z0-9+/]{2}AAV0VCUA"),  # base64 RIFF/WEBP
]
_BINARY_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"RIFF")


def count_image_markers(body: bytes, content_type: str = "") -> int:
    if not body:
        return 0
    n = 0
    for pat in _PATTERNS:
        n += len(pat.findall(body))
    ct = content_type.lower()
    if "multipart" in ct and re.search(rb"Content-Type:\s*image/", body, re.I):
        n += 1
    if body[:12].startswith(_BINARY_MAGIC) or ct.startswith("image/"):
        n += 1
    return n


def decide(settings: Settings, ctx: EgressContext | None, image_markers: int) -> tuple[bool, str, str]:
    data_class = (ctx.data_class if ctx else "research") if image_markers else (ctx.data_class if ctx else "none")
    if image_markers == 0:
        return True, "no image content detected", data_class
    if data_class == "synthetic":
        if settings.allow_synthetic_cloud_image_processing:
            return True, "synthetic imagery; ALLOW_SYNTHETIC_CLOUD_IMAGE_PROCESSING enabled", data_class
        return False, "synthetic image egress disabled (ALLOW_SYNTHETIC_CLOUD_IMAGE_PROCESSING=false)", data_class
    # research or unknown -> strictest rule
    if not settings.allow_cloud_image_processing:
        return False, "cloud image processing disabled (ALLOW_CLOUD_IMAGE_PROCESSING=false)", "research"
    if not (ctx and ctx.project_allows_cloud):
        return False, "project does not permit third-party image processing", "research"
    return True, "research imagery; operator and project permit cloud processing", "research"


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------

Recorder = Callable[[dict], None]


def db_recorder(record: dict) -> None:
    """Persist an egress decision. Best effort: a DB failure never allows a blocked request."""
    try:
        from app.db import session_scope
        from app.models import EgressEvent

        with session_scope() as s:
            s.add(EgressEvent(**record))
    except Exception:  # noqa: BLE001
        log.exception("failed to record egress event")


def _record(recorder: Recorder | None, provider: str, request, ctx, image_markers, size, allowed, reason, data_class):
    rec = {
        "provider": provider,
        "endpoint": f"{request.method} {request.url.host}{request.url.path}",
        "job_id": ctx.job_id if ctx else None,
        "task_id": ctx.task_id if ctx else None,
        "worker_id": ctx.worker_id if ctx else None,
        "data_class": data_class,
        "image_count": image_markers,
        "request_bytes": size,
        "decision": "allowed" if allowed else "blocked",
        "reason": reason,
    }
    log.info("egress %s %s images=%d class=%s: %s", rec["decision"], rec["endpoint"], image_markers, data_class, reason)
    if recorder:
        recorder(rec)


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------


class GuardedTransport(httpx2.BaseTransport):
    def __init__(
        self,
        inner: httpx2.BaseTransport,
        provider: str,
        recorder: Recorder | None = db_recorder,
        settings_provider: Callable[[], Settings] = get_settings,
    ):
        self._inner = inner
        self._provider = provider
        self._recorder = recorder
        self._settings = settings_provider

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            body = request.read()
            markers = count_image_markers(body, request.headers.get("content-type", ""))
        except Exception:  # noqa: BLE001 - uninspectable => assume image (fail closed)
            body, markers = b"", 1
        ctx = current_context()
        allowed, reason, data_class = decide(self._settings(), ctx, markers)
        _record(self._recorder, self._provider, request, ctx, markers, len(body), allowed, reason, data_class)
        if not allowed:
            raise CloudImageTransmissionBlocked(reason)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


class AsyncGuardedTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        provider: str,
        recorder: Recorder | None = db_recorder,
        settings_provider: Callable[[], Settings] = get_settings,
    ):
        self._inner = inner
        self._provider = provider
        self._recorder = recorder
        self._settings = settings_provider

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            body = await request.aread()
            markers = count_image_markers(body, request.headers.get("content-type", ""))
        except Exception:  # noqa: BLE001
            body, markers = b"", 1
        ctx = current_context()
        allowed, reason, data_class = decide(self._settings(), ctx, markers)
        _record(self._recorder, self._provider, request, ctx, markers, len(body), allowed, reason, data_class)
        if not allowed:
            raise CloudImageTransmissionBlocked(reason)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


# --------------------------------------------------------------------------
# Log redaction: no image bytes in any local log either.
# --------------------------------------------------------------------------

_REDACT = [
    (re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+"), "data:image/...;base64,[REDACTED]"),
    (re.compile(r"[A-Za-z0-9+/]{400,}={0,2}"), "[REDACTED-BASE64]"),
]


def redact(text: str) -> str:
    for pat, repl in _REDACT:
        text = pat.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        red = redact(msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True


def install_log_redaction() -> None:
    root = logging.getLogger()
    f = RedactingFilter()
    root.addFilter(f)
    for h in root.handlers:
        h.addFilter(f)
    # SDK debug logging can print request bodies; keep it at WARNING.
    for name in ("openai", "httpx2", "httpx", "httpcore"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        lg.addFilter(f)
