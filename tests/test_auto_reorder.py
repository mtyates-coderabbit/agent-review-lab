from __future__ import annotations

from northwind.agents.auto_reorder import find_low_tanks


async def test_finds_tanks_at_or_below_reorder_point(backend):
    low = await find_low_tanks(backend)
    assert {(t["site_id"], t["product"]) for t in low} == {
        ("NW-DEPOT-01", "diesel"),
        ("NW-DEPOT-02", "diesel"),
        ("NW-DEPOT-03", "unleaded"),
    }
