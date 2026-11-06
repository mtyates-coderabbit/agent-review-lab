"""Deterministic reconciliation rules.

Routing decisions are made here from structured fields only. The model never
decides whether an invoice is approved, and free-text notes never influence it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from northwind.agents.invoice_reconciler.state import Discrepancy, Route
from northwind.config import AgentConfig

CENT = 0.01


def find_discrepancies(
    invoice: dict[str, Any],
    purchase_order: dict[str, Any] | None,
    tickets: list[dict[str, Any]],
    config: AgentConfig,
) -> list[Discrepancy]:
    found: list[Discrepancy] = []
    if purchase_order is None:
        found.append(
            {
                "kind": "missing_purchase_order",
                "severity": "critical",
                "detail": f"no purchase order {invoice['po_number']} on file",
            }
        )
        return found
    if not tickets:
        found.append(
            {
                "kind": "missing_delivery",
                "severity": "critical",
                "detail": f"no delivery tickets recorded for {purchase_order['po_number']}",
            }
        )
        return found

    if invoice["product"] != purchase_order["product"]:
        found.append(
            {
                "kind": "product",
                "severity": "critical",
                "detail": (
                    f"invoice product {invoice['product']} does not match "
                    f"PO product {purchase_order['product']}"
                ),
            }
        )

    delivered = sum(t["delivered_gallons"] for t in tickets)
    billed = invoice["billed_gallons"]
    volume_tolerance = float(config.option("volume_tolerance_pct"))
    volume_variance_pct = abs(billed - delivered) / delivered * 100
    if volume_variance_pct > volume_tolerance:
        found.append(
            {
                "kind": "volume",
                "severity": "minor",
                "detail": (
                    f"billed {billed:,} gal but delivery tickets total {delivered:,} gal "
                    f"({volume_variance_pct:.1f}% variance)"
                ),
            }
        )

    po_price = purchase_order["price_per_gallon"]
    invoice_price = invoice["price_per_gallon"]
    price_diff = invoice_price - po_price
    if abs(price_diff) > float(config.option("price_tolerance_per_gallon_usd")):
        pct = price_diff / po_price * 100
        severity = "critical" if pct > float(config.option("price_reject_pct")) else "minor"
        found.append(
            {
                "kind": "price",
                "severity": severity,
                "detail": (
                    f"invoice price ${invoice_price:.3f}/gal vs PO ${po_price:.3f}/gal "
                    f"({pct:+.1f}%)"
                ),
            }
        )

    expected = date.fromisoformat(purchase_order["expected_delivery"])
    actual = max(date.fromisoformat(t["delivery_date"]) for t in tickets)
    window = int(config.option("delivery_window_days"))
    if abs((actual - expected).days) > window:
        found.append(
            {
                "kind": "delivery_date",
                "severity": "minor",
                "detail": (
                    f"delivered {actual.isoformat()}, expected {expected.isoformat()} "
                    f"(window ±{window} days)"
                ),
            }
        )

    expected_total = round(billed * invoice_price, 2)
    if abs(expected_total - invoice["total_usd"]) > CENT:
        found.append(
            {
                "kind": "total",
                "severity": "critical",
                "detail": (
                    f"invoice total ${invoice['total_usd']:,.2f} does not equal "
                    f"billed volume x price (${expected_total:,.2f})"
                ),
            }
        )
    return found


def contract_discrepancies(invoice: dict[str, Any], contract: dict[str, Any]) -> list[Discrepancy]:
    """Compare an invoice against the supplier's negotiated contract terms."""
    cap = contract["max_price_per_gallon"]
    if invoice["price_per_gallon"] > cap:
        return [
            {
                "kind": "contract_price",
                "severity": "critical",
                "detail": (
                    f"invoice price ${invoice['price_per_gallon']:.3f}/gal exceeds "
                    f"{invoice['supplier_id']} contract cap ${cap:.3f}/gal"
                ),
            }
        ]
    return []


def choose_route(
    discrepancies: list[Discrepancy], invoice_total: float, config: AgentConfig
) -> Route:
    if any(d["severity"] == "critical" for d in discrepancies):
        return "reject"
    if discrepancies or invoice_total >= float(config.option("high_value_threshold_usd")):
        return "human_review"
    return "auto_approve"
