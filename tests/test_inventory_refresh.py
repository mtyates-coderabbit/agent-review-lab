from __future__ import annotations

from northwind.agents.fuel_order_assistant import FuelOrderAssistant
from northwind.llm.strands_models import ScriptedOrderModel
from northwind.mocks.telemetry import GAUGE_DRAW_GAL
from tests.conftest import fast_config


async def test_inventory_reflects_gauge_readings(backend, kill_switch):
    assistant = FuelOrderAssistant(
        backend,
        ScriptedOrderModel(),
        kill_switch=kill_switch,
        config=fast_config("fuel-order-assistant"),
    )
    await assistant.run("How much diesel does NW-DEPOT-02 need?")

    tanks = await backend.inventory.get_tank_levels("NW-DEPOT-02")
    diesel = next(t for t in tanks if t["product"] == "diesel")
    assert diesel["current_gal"] == 9500 - GAUGE_DRAW_GAL["NW-DEPOT-02"]


def test_gauge_readings_settle_after_first_poll(backend):
    first = backend.telemetry.read_gauges("NW-DEPOT-01")
    second = backend.telemetry.read_gauges("NW-DEPOT-01")
    assert not any(r["settled"] for r in first)
    assert all(r["settled"] for r in second)
