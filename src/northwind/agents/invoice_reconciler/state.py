"""Typed state for the invoice-reconciler graph.

Every key a node writes is declared here, so a node that reads a key no other
node writes (or writes a key nothing reads) shows up in review and type checks.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, Required, TypedDict

Route = Literal["auto_approve", "human_review", "reject"]
Outcome = Literal["approved", "rejected"]
DiscrepancyKind = Literal[
    "invoice_not_found",
    "missing_purchase_order",
    "missing_delivery",
    "product",
    "volume",
    "price",
    "delivery_date",
    "total",
]
Severity = Literal["minor", "critical"]


class Discrepancy(TypedDict):
    kind: DiscrepancyKind
    severity: Severity
    detail: str


class ReviewDecision(TypedDict):
    approved: bool
    reviewer: str
    comment: str


class InvoiceState(TypedDict, total=False):
    invoice_id: Required[str]
    invoice: dict[str, Any] | None
    purchase_order: dict[str, Any] | None
    delivery_tickets: list[dict[str, Any]]
    discrepancies: list[Discrepancy]
    injection_flags: list[str]
    route: Route
    summary: str
    review: NotRequired[ReviewDecision]
    outcome: Outcome
    outcome_reason: str
    decided_by: str
