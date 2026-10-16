"""Cloud-image transmission blocking, through every model adapter and logging path.

The OpenAI adapter is exercised through the real openai-python SDK with an
in-process httpx2 MockTransport standing in for the network, so we can prove
exactly which bytes would have left the machine.
"""

from __future__ import annotations

import ast
import base64
import io
import json
import logging
from pathlib import Path

import httpx2
import pytest
from PIL import Image

from app.config import Settings
from app.providers.egress import (
    AsyncGuardedTransport,
    CloudImageTransmissionBlocked,
    EgressContext,
    GuardedTransport,
    count_image_markers,
    decide,
    egress_context,
    install_log_redaction,
    redact,
)
from app.providers.openai_adapter import OpenAIComputerUseAdapter, screenshot_output_item

APP = Path(__file__).resolve().parents[1] / "app"


def _png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 200, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _settings(**kw) -> Settings:
    base = dict(openai_api_key="sk-test", openai_computer_use_model="test-computer-model", allow_cloud_image_processing=False, allow_synthetic_cloud_image_processing=False)
    base.update(kw)
    return Settings(**base)


class Recorder:
    def __init__(self):
        self.records: list[dict] = []

    def __call__(self, rec: dict):
        self.records.append(rec)


def _fake_response(**over):
    body = {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": "test-computer-model",
        "output": [
            {"type": "computer_call", "id": "cu_1", "call_id": "call_1", "status": "completed", "pending_safety_checks": [], "actions": [{"type": "screenshot"}]}
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 5}},
    }
    body.update(over)
    return body


# --------------------------------------------------------------------------
# detection + decision
# --------------------------------------------------------------------------


def test_detects_every_image_encoding():
    b64 = _png_b64()
    assert count_image_markers(json.dumps({"image_url": f"data:image/png;base64,{b64}"}).encode()) > 0
    assert count_image_markers(json.dumps({"type": "input_image", "file_id": "file_1"}).encode()) > 0
    assert count_image_markers(json.dumps({"type": "computer_screenshot", "file_id": "f"}).encode()) > 0
    assert count_image_markers(json.dumps({"x": b64}).encode()) > 0  # bare base64 PNG
    assert count_image_markers(base64.b64decode(b64)) > 0  # raw PNG bytes
    assert count_image_markers(b"--b\r\nContent-Type: image/jpeg\r\n\r\n...", "multipart/form-data; boundary=b") > 0
    assert count_image_markers(json.dumps({"input": "text only, mentions the word image"}).encode()) == 0


@pytest.mark.parametrize(
    "cloud,synth,ctx,expected",
    [
        (False, False, EgressContext(data_class="research", project_allows_cloud=True), False),
        (True, False, EgressContext(data_class="research", project_allows_cloud=False), False),
        (True, False, EgressContext(data_class="research", project_allows_cloud=True), True),
        (False, False, EgressContext(data_class="synthetic"), False),
        (False, True, EgressContext(data_class="synthetic"), True),
        (True, True, None, False),  # unknown context => strictest rule (research, no project consent)
    ],
)
def test_decision_matrix(cloud, synth, ctx, expected):
    allowed, reason, _ = decide(_settings(allow_cloud_image_processing=cloud, allow_synthetic_cloud_image_processing=synth), ctx, image_markers=1)
    assert allowed is expected, reason


def test_text_only_requests_are_allowed_when_disabled():
    allowed, reason, _ = decide(_settings(), None, image_markers=0)
    assert allowed and "no image" in reason


# --------------------------------------------------------------------------
# transport level
# --------------------------------------------------------------------------


def test_sync_transport_blocks_before_network():
    seen = []
    inner = httpx2.MockTransport(lambda req: seen.append(req) or httpx2.Response(200, json={}))
    rec = Recorder()
    client = httpx2.Client(transport=GuardedTransport(inner, "openai", recorder=rec, settings_provider=_settings))
    body = {"input": [{"type": "input_image", "image_url": f"data:image/png;base64,{_png_b64()}"}]}
    with egress_context(data_class="research", project_allows_cloud=True):
        with pytest.raises(CloudImageTransmissionBlocked):
            client.post("https://api.openai.com/v1/responses", json=body)
    assert seen == []  # nothing reached the network layer
    assert rec.records[-1]["decision"] == "blocked"
    assert "payload" not in json.dumps(rec.records)  # records never contain the payload
    assert _png_b64() not in json.dumps(rec.records, default=str)


# --------------------------------------------------------------------------
# through the real OpenAI SDK
# --------------------------------------------------------------------------


async def test_openai_adapter_blocks_screenshot_when_cloud_disabled():
    bodies: list[bytes] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        bodies.append(req.read())
        return httpx2.Response(200, json=_fake_response())

    rec = Recorder()
    adapter = OpenAIComputerUseAdapter(_settings(), transport=httpx2.MockTransport(handler), recorder=rec)
    first = {"role": "user", "content": [{"type": "input_text", "text": "bounded task"}]}
    with egress_context(data_class="research", project_allows_cloud=True):
        # Turn 1: text only -> allowed
        resp = await adapter.create_turn(instructions="x", input_items=[first])
        assert resp.output[0].type == "computer_call"
        # Turn 2 would carry a screenshot -> blocked, and NOT retried
        item = screenshot_output_item("call_1", _png_b64())
        with pytest.raises(CloudImageTransmissionBlocked):
            await adapter.create_turn(instructions="x", input_items=[first, item])
    assert len(bodies) == 1
    assert count_image_markers(bodies[0]) == 0
    sent = json.loads(bodies[0])
    assert sent["store"] is False  # request-level data control
    assert sent["tools"] == [{"type": "computer"}]
    assert [r["decision"] for r in rec.records] == ["allowed", "blocked"]
    await adapter.aclose()


async def test_openai_adapter_sends_only_permitted_image_when_enabled():
    bodies: list[bytes] = []

    def handler(req):
        bodies.append(req.read())
        return httpx2.Response(200, json=_fake_response())

    adapter = OpenAIComputerUseAdapter(_settings(allow_cloud_image_processing=True), transport=httpx2.MockTransport(handler), recorder=Recorder())
    item = screenshot_output_item("call_1", _png_b64())
    with egress_context(data_class="research", project_allows_cloud=True):
        await adapter.create_turn(instructions="x", input_items=[{"role": "user", "content": "t"}, item])
    body = json.loads(bodies[0])
    out = [i for i in body["input"] if i.get("type") == "computer_call_output"][0]
    assert out["output"]["type"] == "computer_screenshot"
    assert out["output"]["detail"] == "original"
    assert body["include"] == ["reasoning.encrypted_content"]
    await adapter.aclose()


async def test_project_without_consent_blocks_even_if_operator_enabled():
    adapter = OpenAIComputerUseAdapter(_settings(allow_cloud_image_processing=True), transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=_fake_response())), recorder=Recorder())
    with egress_context(data_class="research", project_allows_cloud=False):
        with pytest.raises(CloudImageTransmissionBlocked):
            await adapter.create_turn(instructions="x", input_items=[screenshot_output_item("c", _png_b64())])
    await adapter.aclose()


async def test_synthetic_images_need_their_own_flag():
    calls = []
    t = httpx2.MockTransport(lambda r: calls.append(1) or httpx2.Response(200, json=_fake_response()))
    a = OpenAIComputerUseAdapter(_settings(allow_cloud_image_processing=True), transport=t, recorder=Recorder())
    with egress_context(data_class="synthetic"):
        with pytest.raises(CloudImageTransmissionBlocked):
            await a.create_turn(instructions="x", input_items=[screenshot_output_item("c", _png_b64())])
    b = OpenAIComputerUseAdapter(_settings(allow_synthetic_cloud_image_processing=True), transport=t, recorder=Recorder())
    with egress_context(data_class="synthetic"):
        await b.create_turn(instructions="x", input_items=[screenshot_output_item("c", _png_b64())])
    assert calls == [1]
    await a.aclose()
    await b.aclose()


async def test_model_access_check_is_image_free_and_guarded():
    reqs = []

    def handler(req):
        reqs.append(req)
        return httpx2.Response(200, json={"id": "test-computer-model", "object": "model", "created": 0, "owned_by": "openai"})

    rec = Recorder()
    a = OpenAIComputerUseAdapter(_settings(), transport=httpx2.MockTransport(handler), recorder=rec)
    out = await a.check_model_access()
    assert out["id"] == "test-computer-model"
    assert reqs[0].url.path.endswith("/models/test-computer-model")
    assert rec.records[0]["decision"] == "allowed" and rec.records[0]["image_count"] == 0
    await a.aclose()


async def test_async_transport_unknown_body_fails_closed():
    class Boom(httpx2.AsyncByteStream):
        async def __aiter__(self):
            raise RuntimeError("cannot read")
            yield b""  # pragma: no cover

    inner = httpx2.MockTransport(lambda r: httpx2.Response(200))
    t = AsyncGuardedTransport(inner, "openai", recorder=Recorder(), settings_provider=_settings)
    req = httpx2.Request("POST", "https://api.openai.com/v1/responses", stream=Boom())
    with pytest.raises(CloudImageTransmissionBlocked):
        await t.handle_async_request(req)


# --------------------------------------------------------------------------
# no other path can reach a model provider
# --------------------------------------------------------------------------


def _py_files():
    return [p for p in APP.rglob("*.py")]


def test_openai_client_constructed_only_in_adapter():
    offenders = []
    for p in _py_files():
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in ("OpenAI", "AsyncOpenAI", "AzureOpenAI", "AsyncAzureOpenAI") and p.name != "openai_adapter.py":
                    offenders.append(f"{p}:{node.lineno}")
    assert offenders == []


def test_adapter_always_uses_guarded_transport():
    src = (APP / "providers" / "openai_adapter.py").read_text()
    assert "AsyncGuardedTransport(" in src and "http_client=DefaultAsyncHttpx2Client(transport=guarded)" in src


def test_no_tracing_or_telemetry_dependencies():
    banned = ("sentry_sdk", "opentelemetry", "langsmith", "agents.tracing", "posthog", "mixpanel", "segment")
    for p in _py_files():
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for m in mods:
                    assert not m.startswith(banned), f"{p} imports {m}"
    import os

    assert os.environ.get("OPENAI_AGENTS_DISABLE_TRACING") == "1"


def test_logs_never_contain_image_bytes(caplog):
    install_log_redaction()
    b64 = _png_b64() * 40
    with caplog.at_level(logging.INFO):
        logging.getLogger("openai").warning("request body: data:image/png;base64,%s", b64)
        logging.getLogger("maply.test").info("payload %s", "A" * 500)
    text = caplog.text
    assert b64 not in text and "A" * 500 not in text
    assert redact(f"x data:image/jpeg;base64,{b64} y") == "x data:image/...;base64,[REDACTED] y"
