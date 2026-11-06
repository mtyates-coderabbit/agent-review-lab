"""Async clients for the mocked ERP, inventory, pricing, and ordering systems.

These stand in for remote services. Each call runs its SQLite work in a worker
thread (``asyncio.to_thread``) and can be configured to fail transiently or to
respond slowly, so retry and timeout handling can be exercised in tests.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from northwind.mocks.db import MockDatabase
from northwind.mocks.errors import (
    RecordNotFoundError,
    RequestRejectedError,
    TransientServiceError,
)
from northwind.mocks.telemetry import TankGaugeClient

QUOTE_TTL = timedelta(minutes=15)


@dataclass
class FaultPlan:
    """Inject transient failures or latency into named service operations."""

    failures: dict[str, int] = field(default_factory=dict)
    latency_seconds: dict[str, float] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    async def before(self, operation: str) -> None:
        self.calls[operation] += 1
        delay = self.latency_seconds.get(operation, 0.0)
        if delay:
            await asyncio.sleep(delay)
        remaining = self.failures.get(operation, 0)
        if remaining > 0:
            self.failures[operation] = remaining - 1
            raise TransientServiceError(f"{operation}: upstream temporarily unavailable")


class _Service:
    def __init__(self, db: MockDatabase, faults: FaultPlan) -> None:
        self._db = db
        self._faults = faults

    async def _query(self, op: str, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        await self._faults.before(op)
        return await asyncio.to_thread(self._db.query, sql, params)

    async def _execute(self, op: str, sql: str, params: tuple[Any, ...]) -> int:
        await self._faults.before(op)
        return await asyncio.to_thread(self._db.execute, sql, params)


class ErpService(_Service):
    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        rows = await self._query(
            "erp.get_invoice",
            "SELECT i.*, s.name AS supplier_name FROM invoices i "
            "JOIN suppliers s USING (supplier_id) WHERE invoice_id = ?",
            (invoice_id,),
        )
        if not rows:
            raise RecordNotFoundError(f"invoice {invoice_id} not found")
        return rows[0]

    async def get_purchase_order(self, po_number: str) -> dict[str, Any]:
        rows = await self._query(
            "erp.get_purchase_order",
            "SELECT * FROM purchase_orders WHERE po_number = ?",
            (po_number,),
        )
        if not rows:
            raise RecordNotFoundError(f"purchase order {po_number} not found")
        return rows[0]

    async def get_delivery_tickets(self, po_number: str) -> list[dict[str, Any]]:
        return await self._query(
            "erp.get_delivery_tickets",
            "SELECT * FROM delivery_tickets WHERE po_number = ? ORDER BY ticket_id",
            (po_number,),
        )

    async def record_invoice_decision(
        self, invoice_id: str, decision: str, reason: str, decided_by: str
    ) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise RequestRejectedError(f"unsupported decision {decision!r}")
        now = datetime.now(UTC).isoformat()
        await self._execute(
            "erp.record_invoice_decision",
            "INSERT OR REPLACE INTO invoice_decisions VALUES (?, ?, ?, ?, ?)",
            (invoice_id, decision, reason, decided_by, now),
        )
        return {"invoice_id": invoice_id, "decision": decision, "decided_at": now}

    async def get_invoice_decision(self, invoice_id: str) -> dict[str, Any] | None:
        rows = await self._query(
            "erp.get_invoice_decision",
            "SELECT * FROM invoice_decisions WHERE invoice_id = ?",
            (invoice_id,),
        )
        return rows[0] if rows else None


class InventoryService(_Service):
    async def get_tank_levels(self, site_id: str) -> list[dict[str, Any]]:
        rows = await self._query(
            "inventory.get_tank_levels",
            "SELECT * FROM tanks WHERE site_id = ? ORDER BY product",
            (site_id,),
        )
        if not rows:
            raise RecordNotFoundError(f"site {site_id} not found")
        return rows

    def apply_gauge_reading(self, site_id: str, product: str, gallons: int) -> None:
        self._db.execute(
            "UPDATE tanks SET current_gal = ? WHERE site_id = ? AND product = ?",
            (gallons, site_id, product),
        )


class PricingService(_Service):
    async def create_quote(self, site_id: str, product: str, gallons: int) -> dict[str, Any]:
        if gallons <= 0:
            raise RequestRejectedError("gallons must be positive")
        offers = await self._query(
            "pricing.list_offers",
            "SELECT * FROM price_list WHERE product = ? ORDER BY price_per_gallon LIMIT 1",
            (product,),
        )
        if not offers:
            raise RecordNotFoundError(f"no pricing for product {product!r}")
        best = offers[0]
        quote = {
            "quote_id": f"Q-{uuid.uuid4().hex[:8].upper()}",
            "site_id": site_id,
            "product": product,
            "gallons": gallons,
            "supplier_id": best["supplier_id"],
            "price_per_gallon": best["price_per_gallon"],
            "total_usd": round(best["price_per_gallon"] * gallons, 2),
            "expires_at": (datetime.now(UTC) + QUOTE_TTL).isoformat(),
            "supplier_notes": best["supplier_notes"],
        }
        await self._execute(
            "pricing.create_quote",
            "INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(quote.values()),
        )
        return quote

    async def get_quote(self, quote_id: str) -> dict[str, Any]:
        rows = await self._query(
            "pricing.get_quote", "SELECT * FROM quotes WHERE quote_id = ?", (quote_id,)
        )
        if not rows:
            raise RecordNotFoundError(f"quote {quote_id} not found")
        return rows[0]


class OrderService(_Service):
    def __init__(self, db: MockDatabase, faults: FaultPlan, pricing: PricingService) -> None:
        super().__init__(db, faults)
        self._pricing = pricing

    async def place_order(self, quote_id: str, approved_by: str) -> dict[str, Any]:
        quote = await self._pricing.get_quote(quote_id)
        if datetime.fromisoformat(quote["expires_at"]) < datetime.now(UTC):
            raise RequestRejectedError(f"quote {quote_id} has expired")
        existing = await self._query(
            "orders.find_by_quote", "SELECT * FROM orders WHERE quote_id = ?", (quote_id,)
        )
        if existing:
            return existing[0]  # idempotent: retries never double-order
        order = {
            "order_id": f"ORD-{uuid.uuid4().hex[:8].upper()}",
            "quote_id": quote_id,
            "site_id": quote["site_id"],
            "product": quote["product"],
            "gallons": quote["gallons"],
            "total_usd": quote["total_usd"],
            "approved_by": approved_by,
            "placed_at": datetime.now(UTC).isoformat(),
        }
        await self._execute(
            "orders.place_order",
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(order.values()),
        )
        return order

    async def list_orders(self) -> list[dict[str, Any]]:
        return await self._query("orders.list", "SELECT * FROM orders ORDER BY placed_at", ())


@dataclass
class MockBackend:
    """All mocked systems sharing one seeded in-memory database."""

    db: MockDatabase
    faults: FaultPlan
    erp: ErpService
    inventory: InventoryService
    pricing: PricingService
    orders: OrderService
    telemetry: TankGaugeClient

    def close(self) -> None:
        self.db.close()


def create_backend(faults: FaultPlan | None = None) -> MockBackend:
    db = MockDatabase()
    plan = faults or FaultPlan()
    pricing = PricingService(db, plan)
    return MockBackend(
        db=db,
        faults=plan,
        erp=ErpService(db, plan),
        inventory=InventoryService(db, plan),
        pricing=pricing,
        orders=OrderService(db, plan, pricing),
        telemetry=TankGaugeClient(db),
    )
