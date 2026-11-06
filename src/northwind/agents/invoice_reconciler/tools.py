"""Tools the invoice-reconciler graph calls. Each goes through ``run_tool``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from northwind.config import AgentConfig
from northwind.mocks.services import ErpService
from northwind.resilience import run_tool
from northwind.safety import KillSwitch


@dataclass(frozen=True)
class InvoiceTools:
    erp: ErpService
    config: AgentConfig
    kill_switch: KillSwitch

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return await run_tool(
            self.config, self.kill_switch, "get_invoice", self.erp.get_invoice, invoice_id
        )

    async def get_purchase_order(self, po_number: str) -> dict[str, Any]:
        return await run_tool(
            self.config,
            self.kill_switch,
            "get_purchase_order",
            self.erp.get_purchase_order,
            po_number,
        )

    async def get_delivery_tickets(self, po_number: str) -> list[dict[str, Any]]:
        return await run_tool(
            self.config,
            self.kill_switch,
            "get_delivery_tickets",
            self.erp.get_delivery_tickets,
            po_number,
        )

    async def post_invoice_decision(
        self, invoice_id: str, decision: str, reason: str, decided_by: str
    ) -> dict[str, Any]:
        return await run_tool(
            self.config,
            self.kill_switch,
            "post_invoice_decision",
            self.erp.record_invoice_decision,
            invoice_id,
            decision,
            reason,
            decided_by,
        )
