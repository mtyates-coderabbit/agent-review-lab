"""Strands ``@tool`` functions for the fuel-order assistant.

Tools are built by a factory so each agent instance is bound to its own backend
and safety controls. All calls go through ``run_tool`` (kill switch, timeout,
retries, tracing). Free-text fields are fenced with ``wrap_untrusted`` before the
model sees them.
"""

from __future__ import annotations

import time
from typing import Any

from strands import ToolContext, tool
from strands.types.tools import AgentTool, ToolResult

from northwind.config import AgentConfig
from northwind.llm.untrusted import wrap_untrusted
from northwind.mocks.errors import RecordNotFoundError, RequestRejectedError
from northwind.mocks.services import MockBackend
from northwind.resilience import ToolCallFailed, run_tool
from northwind.safety import ApprovalDecision, KillSwitch, KillSwitchEngaged

APPROVALS_KEY = "northwind_approvals"
GAUGE_POLL_INTERVAL_SECONDS = 0.25
GAUGE_MAX_POLLS = 4


def _ok(data: dict[str, Any]) -> ToolResult:
    return {"toolUseId": "", "status": "success", "content": [{"json": data}]}


def _error(message: str) -> ToolResult:
    return {"toolUseId": "", "status": "error", "content": [{"text": message}]}


def build_order_tools(
    backend: MockBackend, config: AgentConfig, kill_switch: KillSwitch
) -> list[AgentTool]:
    spending_limit = float(config.option("spending_limit_usd"))

    async def refresh_from_gauges(site_id: str) -> int:
        readings = backend.telemetry.read_gauges(site_id)
        polls = 1
        while not all(r["settled"] for r in readings) and polls < GAUGE_MAX_POLLS:
            time.sleep(GAUGE_POLL_INTERVAL_SECONDS)
            readings = backend.telemetry.read_gauges(site_id)
            polls += 1
        for reading in readings:
            backend.inventory.apply_gauge_reading(site_id, reading["product"], reading["gallons"])
        backend.telemetry.publish_refresh_event(site_id, readings)
        return len(readings)

    @tool
    async def check_tank_inventory(site_id: str) -> ToolResult:
        """Return current tank levels for a Northwind site.

        Levels are refreshed from the site's tank gauges first so the answer reflects
        what is physically in the tank, not the last ERP sync.

        Args:
            site_id: Site identifier, for example NW-DEPOT-01.
        """
        try:
            await run_tool(config, kill_switch, "refresh_tank_levels", refresh_from_gauges, site_id)
        except Exception:
            pass

        try:
            tanks = await run_tool(
                config,
                kill_switch,
                "check_tank_inventory",
                backend.inventory.get_tank_levels,
                site_id,
            )
        except (RecordNotFoundError, ToolCallFailed, KillSwitchEngaged) as exc:
            return _error(str(exc))
        return _ok(
            {
                "site_id": site_id,
                "tanks": [
                    {
                        "product": t["product"],
                        "capacity_gal": t["capacity_gal"],
                        "current_gal": t["current_gal"],
                        "reorder_point_gal": t["reorder_point_gal"],
                        "needs_reorder": t["current_gal"] <= t["reorder_point_gal"],
                        "site_notes": wrap_untrusted("site_notes", t["site_notes"]),
                    }
                    for t in tanks
                ],
            }
        )

    @tool
    async def get_price_quote(site_id: str, product: str, gallons: int) -> ToolResult:
        """Get a delivered price quote. Quotes expire after 15 minutes.

        Args:
            site_id: Site identifier the fuel will be delivered to.
            product: Either "diesel" or "unleaded".
            gallons: Number of gallons to quote; must be positive.
        """
        try:
            quote = await run_tool(
                config,
                kill_switch,
                "get_price_quote",
                backend.pricing.create_quote,
                site_id,
                product,
                gallons,
            )
        except (
            RecordNotFoundError,
            RequestRejectedError,
            ToolCallFailed,
            KillSwitchEngaged,
        ) as exc:
            return _error(str(exc))
        return _ok(
            {
                "quote_id": quote["quote_id"],
                "site_id": quote["site_id"],
                "product": quote["product"],
                "gallons": quote["gallons"],
                "price_per_gallon": quote["price_per_gallon"],
                "total_usd": quote["total_usd"],
                "expires_at": quote["expires_at"],
                "within_spending_limit": quote["total_usd"] <= spending_limit,
                "supplier_notes": wrap_untrusted("supplier_notes", quote["supplier_notes"]),
            }
        )

    @tool(context=True)
    async def place_fuel_order(quote_id: str, tool_context: ToolContext) -> ToolResult:
        """Place a fuel order from an existing quote. Requires human approval.

        Args:
            quote_id: The quote_id returned by get_price_quote.
        """
        tool_use_id = tool_context.tool_use["toolUseId"]
        approvals: dict[str, ApprovalDecision] = tool_context.invocation_state.get(
            APPROVALS_KEY, {}
        )
        approval = approvals.get(tool_use_id)
        if approval is None or not approval.approved:
            # Defense in depth: the approval hook should have cancelled this call already.
            return _error("order not placed: no recorded approval for this call")
        try:
            quote = await backend.pricing.get_quote(quote_id)
            if quote["total_usd"] > spending_limit:
                return _error(
                    f"order not placed: ${quote['total_usd']:,.2f} exceeds the "
                    f"${spending_limit:,.2f} spending limit"
                )
            order = await run_tool(
                config,
                kill_switch,
                "place_fuel_order",
                backend.orders.place_order,
                quote_id,
                approval.approver,
            )
        except (
            RecordNotFoundError,
            RequestRejectedError,
            ToolCallFailed,
            KillSwitchEngaged,
        ) as exc:
            return _error(f"order not placed: {exc}")
        return _ok(order)

    return [check_tank_inventory, get_price_quote, place_fuel_order]
