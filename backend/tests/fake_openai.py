"""A tiny local stand-in for the OpenAI Responses API (tests only).

It never forwards anything. It records every request body it receives so a
test can prove whether an image ever arrived, and scripts a computer-use
conversation:

  turn 1 (no computer_call_output yet) -> computer_call [screenshot]
  turn 2                               -> computer_call [keypress CTRL+L (must be rejected), keypress ESC]
  turn 3+                              -> message "READY"

Run: uvicorn tests.fake_openai:app --port N
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request

app = FastAPI()
RECEIVED: list[dict] = []


def _usage():
    return {"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280, "input_tokens_details": {"cached_tokens": 200}, "output_tokens_details": {"reasoning_tokens": 30}}


def _resp(rid: str, output: list[dict]) -> dict:
    return {
        "id": rid, "object": "response", "created_at": 0, "status": "completed", "model": "fake-cua",
        "output": output, "parallel_tool_calls": False, "tool_choice": "auto", "tools": [], "usage": _usage(),
    }


@app.post("/v1/responses")
async def responses(request: Request):
    raw = await request.body()
    body = json.loads(raw)
    RECEIVED.append({"bytes": len(raw), "has_image": b"data:image/" in raw or b"computer_screenshot" in raw, "store": body.get("store"), "tools": body.get("tools")})
    items = body.get("input") or []
    outputs = [i for i in items if isinstance(i, dict) and i.get("type") == "computer_call_output"]
    n = len(RECEIVED)
    if not outputs:
        return _resp(f"resp_{n}", [{"type": "computer_call", "id": f"cu_{n}", "call_id": f"call_{n}", "status": "completed", "pending_safety_checks": [], "actions": [{"type": "screenshot"}]}])
    if len(outputs) == 1:
        return _resp(
            f"resp_{n}",
            [{"type": "computer_call", "id": f"cu_{n}", "call_id": f"call_{n}", "status": "completed", "pending_safety_checks": [],
              "actions": [{"type": "keypress", "keys": ["CTRL", "L"]}, {"type": "keypress", "keys": ["ESC"]}]}],
        )
    return _resp(f"resp_{n}", [{"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "READY", "annotations": []}]}])


@app.get("/v1/models/{model}")
def model(model: str):
    return {"id": model, "object": "model", "created": 0, "owned_by": "fake"}


@app.get("/_received")
def received():
    return RECEIVED
