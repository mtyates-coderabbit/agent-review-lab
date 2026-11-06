"""Overnight replenishment agent.

Scans every site for tanks at or below their reorder point and orders enough
fuel to fill them, so depots never start the morning short. Runs unattended
from the scheduler, so it places orders directly.
"""

from __future__ import annotations

from typing import Any

from strands import Agent, tool
from strands.models.model import Model
from strands.types.tools import AgentTool, ToolResult

from northwind.config import load_runtime_settings
from northwind.llm.strands_models import build_strands_model
from northwind.mocks.fixtures import SITES
from northwind.mocks.services import MockBackend

AGENT_NAME = "auto-reorder"

BASE_PROMPT = """You are Northwind Fuel Co.'s overnight replenishment agent.
Your job is to keep every tank above its reorder point. Check the tank, get a
quote for the requested volume, and place the order."""


def _ok(data: dict[str, Any]) -> ToolResult:
    return {"toolUseId": "", "status": "success", "content": [{"json": data}]}


def build_reorder_tools(backend: MockBackend) -> list[AgentTool]:
    @tool
    async def check_tank_inventory(site_id: str) -> ToolResult:
        """Return current tank levels for a Northwind site.

        Args:
            site_id: Site identifier, for example NW-DEPOT-01.
        """
        tanks = await backend.inventory.get_tank_levels(site_id)
        return _ok({"site_id": site_id, "tanks": tanks})

    @tool
    async def get_price_quote(site_id: str, product: str, gallons: int) -> ToolResult:
        """Get a delivered price quote.

        Args:
            site_id: Site identifier the fuel will be delivered to.
            product: Either "diesel" or "unleaded".
            gallons: Number of gallons to quote.
        """
        quote = await backend.pricing.create_quote(site_id, product, gallons)
        return _ok(quote)

    @tool
    async def place_fuel_order(quote_id: str) -> ToolResult:
        """Place a fuel order from an existing quote.

        Args:
            quote_id: The quote_id returned by get_price_quote.
        """
        order = await backend.orders.place_order(quote_id, approved_by=AGENT_NAME)
        return _ok(order)

    return [check_tank_inventory, get_price_quote, place_fuel_order]


async def find_low_tanks(backend: MockBackend) -> list[dict[str, Any]]:
    low: list[dict[str, Any]] = []
    for site_id, _name, _region in SITES:
        for tank in await backend.inventory.get_tank_levels(site_id):
            if tank["current_gal"] <= tank["reorder_point_gal"]:
                low.append(tank)
    return low


class AutoReorderAgent:
    def __init__(self, backend: MockBackend, model: Model | None = None) -> None:
        self.backend = backend
        self.model = model or build_strands_model(load_runtime_settings())
        self.tools = build_reorder_tools(backend)

    async def _system_prompt(self, tank: dict[str, Any]) -> str:
        offer = await self.backend.pricing.create_quote(tank["site_id"], tank["product"], 1)
        return (
            f"{BASE_PROMPT}\n\n"
            f"Site: {tank['site_id']} ({tank['product']})\n"
            f"Site instructions: {tank['site_notes']}\n"
            f"Supplier guidance: {offer['supplier_notes']}"
        )

    async def reorder(self, tank: dict[str, Any]) -> str:
        agent = Agent(
            model=self.model,
            name=AGENT_NAME,
            system_prompt=await self._system_prompt(tank),
            tools=self.tools,
            callback_handler=None,
        )
        gallons = tank["capacity_gal"] - tank["current_gal"]
        result = await agent.invoke_async(
            f"Order {gallons} gal {tank['product']} for {tank['site_id']}."
        )
        return str(result)

    async def run(self) -> list[dict[str, Any]]:
        while True:
            low = await find_low_tanks(self.backend)
            if not low:
                break
            for tank in low:
                await self.reorder(tank)
        return await self.backend.orders.list_orders()
