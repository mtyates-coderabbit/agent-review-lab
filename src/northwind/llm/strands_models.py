"""Model providers for the Strands agent.

``ScriptedOrderModel`` implements the Strands ``Model`` interface with a small
deterministic planner, so the fuel-order agent runs end-to-end offline. It reads
the conversation (user request plus tool results) and decides the next tool
call exactly as a real model would emit it: as a streamed ``toolUse`` block.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any

from strands.models.anthropic import AnthropicModel
from strands.models.model import Model
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

from northwind.config import ConfigError, RuntimeSettings

_SITE = re.compile(r"\bNW-DEPOT-\d{2}\b")
_PRODUCT = re.compile(r"\b(diesel|unleaded)\b", re.IGNORECASE)
_GALLONS = re.compile(r"\b(\d[\d,]*)\s*(?:gal|gallons)\b", re.IGNORECASE)
_ORDER_INTENT = re.compile(r"\b(order|place|buy|purchase)\b", re.IGNORECASE)
FILL_TARGET_RATIO = 0.9


def _first_user_text(messages: Messages) -> str:
    for message in messages:
        if message["role"] == "user":
            for block in message["content"]:
                if "text" in block:
                    return block["text"]
    return ""


def _tool_results(messages: Messages) -> dict[str, dict[str, Any]]:
    """Map tool name -> latest result, joining toolUse and toolResult blocks by id."""
    names: dict[str, str] = {}
    results: dict[str, dict[str, Any]] = {}
    for message in messages:
        for block in message["content"]:
            if "toolUse" in block:
                names[block["toolUse"]["toolUseId"]] = block["toolUse"]["name"]
            elif "toolResult" in block:
                result = block["toolResult"]
                name = names.get(result["toolUseId"], "unknown")
                payload: dict[str, Any] = {"status": result.get("status", "success")}
                for item in result.get("content", []):
                    if "json" in item:
                        payload.update(item["json"])
                    elif "text" in item:
                        payload.setdefault("text", item["text"])
                results[name] = payload
    return results


class ScriptedOrderModel(Model):
    """Deterministic planner for the fuel-order workflow (no network, no API key)."""

    def __init__(self) -> None:
        self._config: dict[str, Any] = {"model_id": "scripted-order-model"}
        self._counter = 0

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)

    async def structured_output(  # type: ignore[override]
        self, output_model: Any, prompt: Messages, system_prompt: str | None = None, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        raise NotImplementedError("structured output is not used by this demo")
        yield {}  # pragma: no cover

    def _plan(self, messages: Messages) -> tuple[str, dict[str, Any]] | str:  # noqa: PLR0911
        request = _first_user_text(messages)
        results = _tool_results(messages)
        site = _SITE.search(request)
        if not site:
            return "Please tell me which site (for example NW-DEPOT-01) you want me to check."
        site_id = site.group(0)
        product_match = _PRODUCT.search(request)
        product = product_match.group(1).lower() if product_match else "diesel"

        for name, result in results.items():
            if result.get("status") == "error":
                return f"I stopped because {name} did not succeed: {result.get('text', 'error')}"

        inventory = results.get("check_tank_inventory")
        if inventory is None:
            return ("check_tank_inventory", {"site_id": site_id})

        if "get_price_quote" not in results:
            gallons_match = _GALLONS.search(request)
            if gallons_match:
                gallons = int(gallons_match.group(1).replace(",", ""))
            else:
                tank = next(
                    (t for t in inventory.get("tanks", []) if t["product"] == product), None
                )
                if tank is None:
                    return f"{site_id} has no {product} tank."
                gallons = int(tank["capacity_gal"] * FILL_TARGET_RATIO - tank["current_gal"])
                gallons -= gallons % 100
                if gallons <= 0:
                    return f"The {product} tank at {site_id} does not need a refill."
            return (
                "get_price_quote",
                {"site_id": site_id, "product": product, "gallons": gallons},
            )

        quote = results["get_price_quote"]
        if _ORDER_INTENT.search(request) and "place_fuel_order" not in results:
            return ("place_fuel_order", {"quote_id": quote["quote_id"]})

        order = results.get("place_fuel_order")
        if order:
            return (
                f"Order {order['order_id']} placed: {order['gallons']} gal {order['product']} "
                f"for {order['site_id']} at ${order['total_usd']:,.2f}."
            )
        return (
            f"Quote {quote['quote_id']}: {quote['gallons']} gal {quote['product']} for "
            f"{quote['site_id']} at ${quote['total_usd']:,.2f}. No order was placed."
        )

    async def stream(  # type: ignore[override]
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        plan = self._plan(messages)
        yield {"messageStart": {"role": "assistant"}}
        if isinstance(plan, str):
            yield {"contentBlockDelta": {"delta": {"text": plan}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return
        name, tool_input = plan
        available = {spec["name"] for spec in tool_specs or []}
        if name not in available:
            yield {"contentBlockDelta": {"delta": {"text": f"Tool {name} is unavailable."}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return
        self._counter += 1
        tool_use_id = f"tooluse_mock_{self._counter:03d}"
        yield {
            "contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_use_id, "name": name}}}
        }
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def build_strands_model(settings: RuntimeSettings) -> Model:
    if settings.mock_llm:
        return ScriptedOrderModel()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ConfigError("MOCK_LLM is off but ANTHROPIC_API_KEY is not set (see .env.example)")
    return AnthropicModel(
        client_args={"api_key": api_key},
        model_id=settings.anthropic_model_id,
        max_tokens=1024,
    )
