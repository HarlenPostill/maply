"""OpenAI Responses API adapter for the GA computer tool.

Based on the official docs as of 2026-10 (developers.openai.com):
* guides/tools-computer-use and guides/tools-computer-use-integration
* api/reference/resources/responses/methods/create

Facts relied on (verified against docs and the installed openai-python 3.x):
* Tool definition: ``{"type": "computer"}`` (configurable: OPENAI_COMPUTER_TOOL_TYPE).
* Output item ``computer_call`` with ``call_id``, ``actions[]`` (batched) and
  ``pending_safety_checks``. Legacy single ``action`` is also handled.
* Return ``computer_call_output`` with
  ``output={"type": "computer_screenshot", "image_url": "data:image/png;base64,...", "detail": "original"}``.
* ``store`` defaults to true when omitted; we send ``store=False`` by default
  and replay prior items ourselves (``previous_response_id`` needs stored
  responses). ``store=False`` is a data control, NOT a zero-retention
  guarantee: abuse-monitoring logs may be retained (up to 30 days by default)
  unless the organisation has a separate ZDR arrangement.
* openai-python 3.x uses HTTPX2; we pass our guarded transport through
  ``DefaultAsyncHttpx2Client(transport=...)``.

The model name is never hard-coded: OPENAI_COMPUTER_USE_MODEL must be set and
is checked with ``models.retrieve`` at startup.

This is the ONLY place an OpenAI client is constructed (enforced by a test).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx2
from openai import AsyncOpenAI, DefaultAsyncHttpx2Client

from app.config import Settings, get_settings
from app.providers.egress import AsyncGuardedTransport, Recorder, db_recorder

# The Agents SDK is not used, but make sure no tracing exporter could ever be
# enabled implicitly by a transitive import.
os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")


class ProviderNotConfigured(RuntimeError):
    pass


@dataclass
class TurnUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @classmethod
    def from_response(cls, resp: Any) -> TurnUsage:
        u = getattr(resp, "usage", None)
        if u is None:
            return cls()
        itd = getattr(u, "input_tokens_details", None)
        otd = getattr(u, "output_tokens_details", None)
        return cls(
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            cached_input_tokens=int(getattr(itd, "cached_tokens", 0) or 0) if itd else 0,
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            reasoning_tokens=int(getattr(otd, "reasoning_tokens", 0) or 0) if otd else 0,
        )


def estimate_cost_usd(usage: TurnUsage, settings: Settings) -> tuple[float, dict]:
    """Estimate only: configured prices, not an invoice."""
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens)
    cost = (
        uncached * settings.openai_price_input_per_mtok
        + usage.cached_input_tokens * settings.openai_price_cached_input_per_mtok
        + usage.output_tokens * settings.openai_price_output_per_mtok
    ) / 1_000_000
    basis = {
        "kind": "estimate",
        "input_per_mtok": settings.openai_price_input_per_mtok,
        "cached_input_per_mtok": settings.openai_price_cached_input_per_mtok,
        "output_per_mtok": settings.openai_price_output_per_mtok,
        "source": "configured prices (OPENAI_PRICE_*); verify against current OpenAI pricing",
    }
    return round(cost, 6), basis


class OpenAIComputerUseAdapter:
    provider = "openai"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        recorder: Recorder | None = db_recorder,
    ):
        self.settings = settings or get_settings()
        if not self.settings.openai_api_key:
            raise ProviderNotConfigured("OPENAI_API_KEY is not set")
        if not self.settings.openai_computer_use_model:
            raise ProviderNotConfigured("OPENAI_COMPUTER_USE_MODEL is not set")
        self.model = self.settings.openai_computer_use_model
        guarded = AsyncGuardedTransport(
            transport or httpx2.AsyncHTTPTransport(),
            provider=self.provider,
            recorder=recorder,
            settings_provider=lambda: self.settings,
        )
        self._client = AsyncOpenAI(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_base_url or None,
            http_client=DefaultAsyncHttpx2Client(transport=guarded),
            max_retries=2,
        )

    @property
    def tool(self) -> dict:
        return {"type": self.settings.openai_computer_tool_type}

    async def check_model_access(self) -> dict:
        """Text-only, image-free check that the configured model is accessible."""
        m = await self._client.models.retrieve(self.model)
        return {
            "id": getattr(m, "id", self.model),
            "owned_by": getattr(m, "owned_by", None),
            "shutdown_date": str(getattr(m, "shutdown_date", None) or "") or None,
        }

    async def probe_computer_tool(self) -> dict:
        """Optional: confirm the model accepts the computer tool, with no image input."""
        resp = await self._client.responses.create(
            model=self.model,
            tools=[self.tool],
            input="Capability check only. Do not use any tool. Reply with the single word OK.",
            store=False,
            max_output_tokens=64,
        )
        return {"response_id": resp.id, "status": getattr(resp, "status", None)}

    async def create_turn(self, *, instructions: str, input_items: list[dict]) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "tools": [self.tool],
            "instructions": instructions,
            "input": input_items,
            "store": self.settings.openai_store,
        }
        if not self.settings.openai_store:
            # Stateless continuation: replay reasoning items with encrypted content.
            kwargs["include"] = ["reasoning.encrypted_content"]
        return await self._client.responses.create(**kwargs)

    async def aclose(self) -> None:
        await self._client.close()


def screenshot_output_item(call_id: str, png_b64: str, acknowledged: list[dict] | None = None) -> dict:
    item: dict[str, Any] = {
        "type": "computer_call_output",
        "call_id": call_id,
        "output": {
            "type": "computer_screenshot",
            "image_url": f"data:image/png;base64,{png_b64}",
            "detail": "original",
        },
    }
    if acknowledged:
        item["acknowledged_safety_checks"] = acknowledged
    return item


def response_items_for_replay(resp: Any) -> list[dict]:
    """Convert response output items to input items for stateless continuation."""
    out = []
    for item in getattr(resp, "output", []) or []:
        if hasattr(item, "model_dump"):
            d = item.model_dump(exclude_none=True)
        else:
            d = dict(item)
        out.append(d)
    return out
