"""Provider failures must never turn into completed annotations."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from app import vision_provider as provider


def test_prefers_structured_output_over_tool_metadata():
    labels = {"surface": {"value": "concrete", "confidence": 0.95}}
    assert provider.parse_output(json.dumps({
        "status": "SUCCESS", "structured_output": labels,
        "response": '{"toolAction":"internal metadata"}',
    })) == labels


def test_accepts_exact_json_response_when_cli_omits_structured_output():
    output = {"status": "SUCCESS", "response": json.dumps({"surface": "concrete", "toolAction": "submit", "toolSummary": "done"})}
    assert provider.parse_output(json.dumps(output)) == {"surface": "concrete"}


def test_does_not_extract_json_from_prose_or_fences():
    with pytest.raises(provider.VisionProviderError):
        provider.parse_output(json.dumps({"status": "SUCCESS", "response": '```json\n{"surface":"concrete"}\n```'}))


@pytest.mark.parametrize("output", [
    "not json", "null", "[]", '{"status":"ERROR","structured_output":{}}',
    '{"status":"SUCCESS","response":"some unvalidated text"}',
    '{"status":"SUCCESS","structured_output":[]}',
])
def test_rejects_missing_failed_or_malformed_annotation(output):
    with pytest.raises(provider.VisionProviderError):
        provider.parse_output(output)


def test_cli_isolates_image_and_uses_schema_without_shell(tmp_path, monkeypatch):
    image = tmp_path / "original.jpg"
    image.write_bytes(b"image fixture")
    schema = {"type": "object", "properties": {"surface": {"type": "string"}}}
    workspaces = []
    monkeypatch.setattr(provider, "find_agy", lambda _: "/fake/agy")

    def invoke(command, **kwargs):
        cwd = kwargs["cwd"]
        workspaces.append(cwd)
        assert cwd != tmp_path
        assert (cwd / "image.jpg").read_bytes() == image.read_bytes()
        assert json.loads(command[command.index("--json-schema") + 1]) == schema
        assert str((cwd / "image.jpg").resolve()) in command[command.index("--print") + 1]
        assert "--dangerously-skip-permissions" not in command
        assert "--disable-slash-commands" not in command
        assert command[command.index("--mode") + 1] == "plan"
        assert kwargs.get("shell", False) is False
        return subprocess.CompletedProcess(command, 0, json.dumps({"status": "SUCCESS", "structured_output": {"surface": "concrete"}}), "")

    monkeypatch.setattr(provider.subprocess, "run", invoke)
    assert provider.annotate_image(image, "tag visible surface", schema) == {"surface": "concrete"}
    assert not workspaces[0].exists()
    assert image.exists()


@pytest.mark.parametrize("timeout", [False, True])
def test_cli_failure_cannot_return_partial_result(tmp_path, monkeypatch, timeout):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"fixture")
    monkeypatch.setattr(provider, "find_agy", lambda _: "/fake/agy")

    def invoke(command, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 3, '{"status":"SUCCESS","structured_output":{}}', "sensitive account information")

    monkeypatch.setattr(provider.subprocess, "run", invoke)
    with pytest.raises(provider.VisionProviderError) as error:
        provider.annotate_image(image, "tag", {"type": "object"})
    assert "sensitive" not in str(error.value)


def test_cli_timeout_rejects_success_envelope_with_stale_labels(tmp_path, monkeypatch):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"fixture")
    monkeypatch.setattr(provider, "find_agy", lambda _: "/fake/agy")
    monkeypatch.setattr(provider.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 0, json.dumps({"status": "SUCCESS", "structured_output": {"surface": "concrete"}}),
        "[agy] print timeout after 1m0s with turn in progress; returning partial output",
    ))
    with pytest.raises(provider.VisionProviderError, match="timed out"):
        provider.annotate_image(image, "tag", {"type": "object"})


def _worker_module():
    path = Path(__file__).resolve().parents[2] / "scripts/tag_worker.py"
    spec = importlib.util.spec_from_file_location("maply_tag_worker_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_labels():
    return {
        "schema_version": "maply-accessibility/1",
        "footpath": {"value": "unknown", "confidence": None, "evidence": "Route is hidden.", "visibility": "occluded"},
        "features": [], "notes": [], "review_reasons": [],
    }


@pytest.mark.parametrize("problem", ["missing_section", "partial_absence"])
def test_corrects_validation_error_once(monkeypatch, tmp_path, problem):
    module = _worker_module()
    invalid = valid_labels()
    invalid["footpath"] = {"value": "present", "confidence": 0.9, "evidence": "Paved route", "visibility": "clear"}
    expected = "present footpath requires at least one present footpath_section"
    if problem == "partial_absence":
        invalid["footpath"].update(value="absent", visibility="partial")
        expected = "absence and no-defect claims require clear observation coverage"
    prompts = []

    def infer(path, prompt, *args, **kwargs):
        prompts.append(prompt)
        return invalid if len(prompts) == 1 else valid_labels()

    monkeypatch.setattr(module, "annotate_image", infer)
    task = {"shot_id": "shot", "prompt": "Inspect image", "schema": {}, "model": "model"}
    result = module.validated_annotation(tmp_path / "image.jpg", task, binary=None, timeout=120, lease_lost=threading.Event())
    assert result == valid_labels()
    assert len(prompts) == 2
    assert expected in prompts[1]


def test_invalid_correction_fails_with_reason_and_no_further_retry(monkeypatch, tmp_path):
    module = _worker_module()
    calls = []

    def infer(*args, **kwargs):
        calls.append(True)
        result = valid_labels()
        result["footpath"]["confidence"] = 0.9
        return result

    monkeypatch.setattr(module, "annotate_image", infer)
    task = {"shot_id": "shot", "prompt": "Inspect image", "schema": {}, "model": "model"}
    with pytest.raises(provider.VisionProviderError, match="unknown observations require confidence=null"):
        module.validated_annotation(tmp_path / "image.jpg", task, binary=None, timeout=120, lease_lost=threading.Event())
    assert len(calls) == 2


def test_validation_diagnostics_keep_reason_without_input():
    module = _worker_module()
    error = module.validation_details([{"loc": ["body", "labels"], "msg": "Value error, feature ids must be unique within an image", "input": "PRIVATE INPUT"}])
    assert "body.labels: Value error, feature ids must be unique" in error
    assert "PRIVATE INPUT" not in error


@pytest.mark.parametrize("problem", [None, "hash", "external_url", "validation"])
def test_worker_claim_download_complete_and_fail(monkeypatch, problem):
    module = _worker_module()
    monkeypatch.setattr(module, "usage_snapshot", lambda _: {"status": "unavailable", "groups": []})
    image = b"existing captured image"
    task = {
        "id": "task-1", "lease_token": "lease-1", "shot_id": 10,
        "image_url": "/files/image.jpg", "image_sha256": hashlib.sha256(image).hexdigest(),
        "prompt": "tag this", "schema": {"type": "object"}, "model": "requested-model",
    }
    if problem == "hash":
        task["image_sha256"] = "wrong"
    if problem == "external_url":
        task["image_url"] = "https://example.com/image.jpg"
    calls = []
    inferred = []

    def infer(path, prompt, schema, model, **kwargs):
        assert path.read_bytes() == image
        assert model == "requested-model"
        inferred.append(True)
        return valid_labels()

    def request(req):
        calls.append((req.url.path, json.loads(req.content) if req.content else None))
        assert req.url.host == "127.0.0.1"
        if req.method == "POST":
            assert req.headers["X-Maply"] == "tagging-worker"
        if req.url.path.endswith("/claim"):
            return httpx.Response(200, json={"task": task})
        if req.url.path == "/files/image.jpg":
            return httpx.Response(200, content=image)
        if req.url.path.endswith("/complete") and problem == "validation":
            return httpx.Response(422, json={"detail": [{"loc": ["body", "labels"], "msg": "Value error, feature ids must be unique within an image", "input": "PRIVATE INPUT"}]})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(module, "annotate_image", infer)
    worker = module.Worker("http://127.0.0.1:8000", "default-model")
    worker.client.close()
    worker.client = httpx.Client(base_url=worker.api_url, transport=httpx.MockTransport(request), headers={"X-Maply": "tagging-worker"})
    try:
        assert worker.run_one()
    finally:
        worker.client.close()
    complete = [(p, body) for p, body in calls if p.endswith("/complete")]
    failed = [(p, body) for p, body in calls if p.endswith("/fail")]
    if problem:
        assert len(failed) == 1 and worker.failed_tasks == 1
        if problem != "validation":
            assert not complete and not inferred
        else:
            assert "feature ids must be unique" in failed[0][1]["error"]
            assert "PRIVATE INPUT" not in failed[0][1]["error"]
    else:
        assert not failed
        assert complete[0][1] == {"worker_id": worker.worker_id, "lease_token": "lease-1", "labels": valid_labels(), "model": "requested-model"}
        events = [body for path, body in calls if path.endswith("/events")]
        assert {e["data"]["stage"] for e in events} >= {"download", "model", "validation", "saving"}


@pytest.mark.parametrize("url", ["http://example.com", "http://localhost@evil.com", "http://localhost/api", "file:///tmp/test"])
def test_worker_rejects_nonlocal_api(url):
    with pytest.raises(ValueError):
        _worker_module().Worker(url, provider.DEFAULT_MODEL)


def _stream_script(records, after=""):
    return [sys.executable, "-u", "-c", "import json,time,os\nrecords=" + repr(records)
            + "\nfor record in records: print(json.dumps(record),flush=True)\n" + after]


def test_stream_progress_filters_reasoning_arguments_and_deduplicates_usage(tmp_path):
    usage = {"input_tokens": 10, "output_tokens": 3, "thinking_tokens": 1, "total_tokens": 13, "secret": "PRIVATE"}
    records = [
        {"event": "init", "init": {"account": "PRIVATE ACCOUNT"}},
        {"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "state": "DONE", "text_delta": "PRIVATE REASONING", "usage": usage}},
        {"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "state": "DONE", "usage": usage}},
        {"event": "step_update", "step_update": {"step_index": 2, "step_type": "tool", "state": "ACTIVE", "tool_name": "view_file", "tool_info": {"parameters": {"AbsolutePath": "PRIVATE PATH"}, "output": "PRIVATE TOOL OUTPUT"}}},
        {"event": "step_update", "step_update": {"step_index": 2, "step_type": "tool", "state": "DONE", "tool_name": "view_file"}},
        {"event": "result", "result": {"status": "SUCCESS", "structured_output": {"surface": "concrete"}, "usage": usage}},
    ]
    events = []
    result = provider._stream_command(_stream_script(records), cwd=tmp_path, timeout=3, on_event=events.append)
    assert provider.parse_output(result.stdout) == {"surface": "concrete"}
    assert "PRIVATE" not in json.dumps(events)
    assert [event["data"]["tool"] for event in events if event["kind"] == "tool"] == ["view_file", "view_file"]
    usage_events = [event for event in events if event["kind"] == "usage"]
    assert all(event["data"]["usage"]["total_tokens"] == 13 for event in usage_events)
    assert usage_events[-1]["data"]["final"] is True
    assert not any(event["kind"] == "output" for event in events)


def test_stream_deadline_kills_and_reaps_process(tmp_path):
    pidfile = tmp_path / "pid.txt"
    command = _stream_script([], f"open({str(pidfile)!r},'w').write(str(os.getpid()))\ntime.sleep(60)\n")
    with pytest.raises(subprocess.TimeoutExpired):
        provider._stream_command(command, cwd=tmp_path, timeout=0.2, on_event=lambda event: None)
    import os
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.parametrize("records", [[], [{"event": "result", "result": None}]])
def test_stream_requires_terminal_result(tmp_path, records):
    with pytest.raises(provider.VisionProviderError):
        provider._stream_command(_stream_script(records), cwd=tmp_path, timeout=3, on_event=lambda event: None)


def test_annotation_only_publishes_terminal_structured_preview(tmp_path, monkeypatch):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"fixture")
    monkeypatch.setattr(provider, "find_agy", lambda _: "/fake/agy")
    labels = {"evidence": "x" * 3000}

    def stream(command, **kwargs):
        assert command[command.index("--output-format") + 1] == "stream-json"
        return subprocess.CompletedProcess(command, 0, json.dumps({"status": "SUCCESS", "structured_output": labels}), "")

    monkeypatch.setattr(provider, "_stream_command", stream)
    events = []
    assert provider.annotate_image(image, "annotate", {"type": "object"}, on_event=events.append) == labels
    assert events == [{"kind": "output", "message": "Structured annotation received (preview)",
                       "data": {"output_text": json.dumps(labels)[:2000], "final": True}}]


def test_usage_snapshot_reads_real_documented_shape_and_drops_account_data(monkeypatch):
    envelope = {"status": "SUCCESS", "command": {"data": {
        "email": "PRIVATE", "groups": [{"name": "Gemini Models", "buckets": [
            {"id": "gemini-5h", "name": "Five Hour Limit Remaining", "window": "5h", "remaining_fraction": 0.42,
             "reset_time": "2026-10-19T07:38:04Z", "private_field": "PRIVATE"},
            {"id": "invalid", "remaining_fraction": 9},
        ]}],
    }}}
    monkeypatch.setattr(provider, "find_agy", lambda _: "/fake/agy")
    def invoke(command, **kwargs):
        assert command == ["/fake/agy", "--print", "/usage", "--output-format", "json"]
        return subprocess.CompletedProcess(command, 0, json.dumps(envelope), "")
    monkeypatch.setattr(provider.subprocess, "run", invoke)
    result = provider.usage_snapshot()
    assert result["status"] == "available"
    assert result["groups"][0]["buckets"][0]["remaining_fraction"] == 0.42
    assert len(result["groups"][0]["buckets"]) == 1
    assert "PRIVATE" not in json.dumps(result)
    assert result["fetched_at"]


def test_usage_unavailable_is_honest_and_sanitized(monkeypatch):
    monkeypatch.setattr(provider, "find_agy", lambda _: "/fake/agy")
    monkeypatch.setattr(provider.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 3, "not JSON", "PRIVATE ACCOUNT"))
    result = provider.usage_snapshot()
    assert result["status"] == "unavailable" and result["groups"] == []
    assert "PRIVATE" not in json.dumps(result)


def test_usage_refresh_is_nonblocking_and_throttled(monkeypatch):
    module = _worker_module()
    started, release = threading.Event(), threading.Event()
    calls = []
    def fetch(binary):
        calls.append(True)
        started.set()
        release.wait(2)
        return {"status": "available", "groups": []}
    monkeypatch.setattr(module, "usage_snapshot", fetch)
    worker = module.Worker("http://127.0.0.1:8000", "model")
    payloads = []
    monkeypatch.setattr(worker, "post", lambda path, payload: payloads.append(payload) or {"ok": True})
    try:
        before = time.monotonic()
        worker.heartbeat()
        assert time.monotonic() - before < 0.5
        assert started.wait(1)
        worker.heartbeat()
        assert len(calls) == 1
        assert "usage" not in payloads[-1]
        release.set()
        worker._usage_thread.join(timeout=2)
        worker.heartbeat()
        assert payloads[-1]["usage"]["status"] == "available"
        assert len(calls) == 1
    finally:
        release.set()
        worker.client.close()


def test_correction_events_carry_attempt_and_stages(monkeypatch, tmp_path):
    module = _worker_module()
    calls, events = [], []
    def infer(path, prompt, *args, **kwargs):
        calls.append(True)
        kwargs["on_event"]({"kind": "usage", "message": "Tokens", "data": {"usage": {"total_tokens": 10}, "final": True}})
        result = valid_labels()
        if len(calls) == 1:
            result["footpath"]["confidence"] = 0.9
        return result
    monkeypatch.setattr(module, "annotate_image", infer)
    task = {"shot_id": "shot", "prompt": "Inspect", "schema": {}, "model": "model"}
    module.validated_annotation(tmp_path / "image.jpg", task, binary=None, timeout=120,
                                lease_lost=threading.Event(), on_event=events.append)
    assert {e["data"]["attempt"] for e in events if e["kind"] == "usage"} == {1, 2}
    assert any(e["data"].get("stage") == "correction" for e in events)


def _pool_components(limit, execute):
    created, closed = [], []
    coordinator = SimpleNamespace(api_url="http://127.0.0.1:8000", model="model", binary=None, timeout=120,
                                  parallel_sessions=limit, heartbeat=lambda: None)

    class Lane:
        def __init__(self, api_url, model, **kwargs):
            assert kwargs["fetch_usage"] is False
            assert kwargs["usage_source"] is coordinator
            self.failed_tasks = 0
            self.last_claim_reason = None
            self.worker_id = len(created)
            self.client = SimpleNamespace(close=lambda: closed.append(self.worker_id))
            created.append(self)

        def run_one(self):
            return execute(self)

    return coordinator, Lane, created, closed


@pytest.mark.parametrize("budget", [1, 2])
def test_pool_reserves_global_budget_and_never_duplicates_a_lane(budget):
    module = _worker_module()
    lock, barrier = threading.Lock(), threading.Barrier(budget)
    calls, active_ids = [], set()
    peak = 0

    def execute(worker):
        nonlocal peak
        with lock:
            assert worker.worker_id not in active_ids
            active_ids.add(worker.worker_id)
            calls.append(worker.worker_id)
            peak = max(peak, len(active_ids))
        barrier.wait(timeout=2)
        time.sleep(0.01)
        with lock:
            active_ids.remove(worker.worker_id)
        return True

    coordinator, factory, created, closed = _pool_components(lambda: 8, execute)
    assert module.run_pool(coordinator, max_tasks=budget, poll=0.01, worker_factory=factory) == 0
    assert len(calls) == budget and peak == budget
    assert len(created) == budget and sorted(closed) == list(range(budget))


def test_pool_reacts_to_cap_changes_and_drains_without_new_claims():
    module = _worker_module()
    condition = threading.Condition()
    stop, lowered_observed = threading.Event(), threading.Event()
    state = {"limit": 1, "active": 0}
    starts, releases = [], []

    def limit():
        with condition:
            if state["limit"] == 1 and len(starts) >= 3:
                lowered_observed.set()
            return state["limit"]

    def execute(worker):
        release = threading.Event()
        with condition:
            starts.append(worker.worker_id)
            releases.append(release)
            state["active"] += 1
            condition.notify_all()
        assert release.wait(3)
        with condition:
            state["active"] -= 1
            condition.notify_all()
        return True

    coordinator, factory, created, closed = _pool_components(limit, execute)
    result = []
    manager = threading.Thread(target=lambda: result.append(module.run_pool(
        coordinator, poll=0.01, worker_factory=factory, stop_event=stop)))
    manager.start()
    try:
        with condition:
            assert condition.wait_for(lambda: len(starts) == 1, timeout=2)
            assert state["active"] == 1
            state["limit"] = 3
            assert condition.wait_for(lambda: len(starts) == 3, timeout=2)
            assert state["active"] == 3
            state["limit"] = 1
        assert lowered_observed.wait(2)
        assert len(starts) == 3  # reducing the cap did not cancel those tasks
        for release in releases[:3]:
            release.set()
        with condition:
            assert condition.wait_for(lambda: len(starts) == 4, timeout=2)
            assert state["active"] == 1
        stop.set()
        time.sleep(0.04)
        assert manager.is_alive() and len(starts) == 4
        releases[3].set()
        manager.join(timeout=2)
        assert not manager.is_alive() and result == [0]
        assert len(created) == 3 and len(closed) == 3
    finally:
        stop.set()
        for release in releases:
            release.set()
        manager.join(timeout=3)


def test_pool_capacity_denial_does_not_count_as_empty_or_spend_budget():
    module = _worker_module()
    calls = []
    def execute(worker):
        calls.append(True)
        if len(calls) == 1:
            worker.last_claim_reason = "capacity"
            return False
        if len(calls) == 2:
            worker.last_claim_reason = None
            return True
        worker.last_claim_reason = "empty"
        return False
    coordinator, factory, _, _ = _pool_components(lambda: 1, execute)
    assert module.run_pool(coordinator, max_tasks=2, poll=0.01, worker_factory=factory) == 0
    assert len(calls) == 3


def test_pool_isolates_one_session_error_and_finishes_its_peer():
    module = _worker_module()
    barrier = threading.Barrier(2)
    completed = []
    def execute(worker):
        barrier.wait(timeout=2)
        if worker.worker_id == 0:
            raise RuntimeError("One session failed")
        time.sleep(0.03)
        completed.append(worker.worker_id)
        return True
    coordinator, factory, created, closed = _pool_components(lambda: 2, execute)
    assert module.run_pool(coordinator, max_tasks=2, poll=0.01, worker_factory=factory) == 1
    assert completed == [1] and len(created) == 2 and sorted(closed) == [0, 1]


def test_pool_keyboard_interrupt_drains_leased_work():
    module = _worker_module()
    started, interrupted, release = threading.Event(), threading.Event(), threading.Event()
    def limit():
        if started.is_set():
            interrupted.set()
            raise KeyboardInterrupt()
        return 1
    def execute(worker):
        started.set()
        assert release.wait(3)
        return True
    coordinator, factory, _, _ = _pool_components(limit, execute)
    results = []
    manager = threading.Thread(target=lambda: results.append(module.run_pool(coordinator, poll=0.01, worker_factory=factory)))
    manager.start()
    try:
        assert interrupted.wait(2)
        assert manager.is_alive()
        release.set()
        manager.join(timeout=2)
        assert results == [0] and not manager.is_alive()
    finally:
        release.set()
        manager.join(timeout=3)


def test_pool_children_share_one_quota_fetcher(monkeypatch):
    module = _worker_module()
    calls = []
    snapshot = {"status": "available", "groups": []}
    monkeypatch.setattr(module, "usage_snapshot", lambda binary: calls.append(binary) or snapshot)
    coordinator = module.Worker("http://127.0.0.1:8000", "model")
    children = [module.Worker(coordinator.api_url, "model", fetch_usage=False, usage_source=coordinator) for _ in range(3)]
    payloads = []
    for worker in [coordinator, *children]:
        monkeypatch.setattr(worker, "post", lambda path, payload: payloads.append(payload) or {"ok": True})
    try:
        coordinator.heartbeat()
        coordinator._usage_thread.join(timeout=2)
        for child in children:
            child.heartbeat()
        assert len(calls) == 1
        assert all(payload["usage"] == snapshot for payload in payloads[-3:])
        assert len({child.worker_id for child in children}) == 3
        assert len({id(child.client) for child in children}) == 3
    finally:
        for worker in [coordinator, *children]:
            worker.client.close()


def test_cli_once_overrides_pool_budget_and_preflights_once(monkeypatch):
    module = _worker_module()
    catalog_calls, limits = [], []
    worker = SimpleNamespace(model="model", client=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(module, "Worker", lambda *args, **kwargs: worker)
    monkeypatch.setattr(module, "available_models", lambda binary: catalog_calls.append(True) or {"model"})
    monkeypatch.setattr(module, "run_pool", lambda coordinator, **kwargs: limits.append(kwargs["max_tasks"]) or 0)
    monkeypatch.setattr(sys, "argv", ["tag_worker.py", "--model", "model", "--once", "--max-tasks", "8"])
    assert module.main() == 0
    assert limits == [1] and len(catalog_calls) == 1
