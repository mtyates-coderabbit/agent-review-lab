"""Mock of the site tank-gauge telemetry feed.

Gauges report the physical level in each tank. The vendor SDK is synchronous;
readings can take a moment to settle after a delivery or a large draw.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from northwind.mocks.db import MockDatabase
from northwind.mocks.errors import RecordNotFoundError

# Gallons drawn since the last ERP sync, per site (fictional).
GAUGE_DRAW_GAL = {"NW-DEPOT-01": 180, "NW-DEPOT-02": 420, "NW-DEPOT-03": 95}


class TankGaugeClient:
    def __init__(self, db: MockDatabase) -> None:
        self._db = db
        self._reads: dict[str, int] = defaultdict(int)
        self.events: list[dict[str, Any]] = []

    def read_gauges(self, site_id: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT product, current_gal FROM tanks WHERE site_id = ? ORDER BY product",
            (site_id,),
        )
        if not rows:
            raise RecordNotFoundError(f"no gauges registered for {site_id}")
        self._reads[site_id] += 1
        settled = self._reads[site_id] > 1
        draw = GAUGE_DRAW_GAL.get(site_id, 0)
        return [
            {
                "product": r["product"],
                "gallons": max(r["current_gal"] - draw, 0),
                "settled": settled,
            }
            for r in rows
        ]

    async def publish_refresh_event(self, site_id: str, readings: list[dict[str, Any]]) -> None:
        self.events.append({"site_id": site_id, "readings": len(readings)})
