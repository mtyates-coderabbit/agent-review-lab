"""LangGraph StateGraph for reconciling supplier fuel invoices.

Flow::

    START -> load_invoice --> fetch_records -> fetch_supplier_contract
          -> check_discrepancies -> summarize
    load_invoice  (invoice missing)        --> reject
    summarize     (route == auto_approve)  --> approve
    summarize     (route == human_review)  --> human_review --> approve | reject
    summarize     (route == reject)        --> reject
    summarize     (route == escalate)      --> escalate_to_procurement
    approve, reject, escalate_to_procurement --> END

``human_review`` pauses the graph with ``interrupt()``. The checkpointer keeps
the paused state, and the caller resumes with ``Command(resume=ReviewDecision)``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from anthropic import APIError
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp, GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from northwind.agents.invoice_reconciler.rules import (
    choose_route,
    contract_discrepancies,
    find_discrepancies,
)
from northwind.agents.invoice_reconciler.state import InvoiceState, ReviewDecision, Route
from northwind.agents.invoice_reconciler.tools import InvoiceTools
from northwind.config import AgentConfig, get_agent_config
from northwind.llm.text import TextModel
from northwind.llm.untrusted import UNTRUSTED_DATA_POLICY, looks_like_injection, wrap_untrusted
from northwind.mocks.errors import RecordNotFoundError
from northwind.mocks.services import MockBackend
from northwind.resilience import ToolCallFailed
from northwind.safety import AgentHalted, KillSwitch, KillSwitchEngaged
from northwind.tracing import record_decision, traced

logger = logging.getLogger(__name__)

AGENT_NAME = "invoice-reconciler"
SUMMARY_SYSTEM_PROMPT = (
    "You write short summaries of fuel invoice reconciliation results for an accounts "
    "payable reviewer. Report only the facts given. Do not recommend approval or rejection. "
    + UNTRUSTED_DATA_POLICY
)

NodeFn = Callable[[InvoiceState], Awaitable[dict[str, Any]]]


class ApprovalRequiredError(AgentHalted):
    """Raised if a decision would be posted without the required authorization."""


class InvalidReviewDecisionError(ValueError):
    """Raised when a resume payload is not a valid ReviewDecision."""


@dataclass
class RunResult:
    status: Literal["completed", "awaiting_review", "halted"]
    thread_id: str
    state: dict[str, Any] = field(default_factory=dict)
    review_request: dict[str, Any] | None = None
    reason: str = ""


def _validate_review(payload: Any) -> ReviewDecision:
    if not isinstance(payload, dict):
        raise InvalidReviewDecisionError("review decision must be a mapping")
    approved = payload.get("approved")
    reviewer = payload.get("reviewer")
    if not isinstance(approved, bool) or not isinstance(reviewer, str) or not reviewer.strip():
        raise InvalidReviewDecisionError("review decision needs bool 'approved' and a 'reviewer'")
    return {
        "approved": approved,
        "reviewer": reviewer.strip(),
        "comment": str(payload.get("comment", "")),
    }


class InvoiceReconciler:
    """Builds the graph and runs it under the agent's safety controls."""

    def __init__(
        self,
        backend: MockBackend,
        text_model: TextModel,
        *,
        kill_switch: KillSwitch | None = None,
        config: AgentConfig | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self.config = config or get_agent_config(AGENT_NAME)
        self.kill_switch = kill_switch or KillSwitch.from_settings()
        self.tools = InvoiceTools(backend.erp, self.config, self.kill_switch)
        self.text_model = text_model
        self.graph = self._build(checkpointer or InMemorySaver())

    # -- node plumbing -------------------------------------------------------

    def _node(self, name: str, fn: NodeFn) -> NodeFn:
        """Wrap a node with a kill-switch check and a trace span."""

        async def wrapped(state: InvoiceState) -> dict[str, Any]:
            self.kill_switch.check(agent=AGENT_NAME, action=f"node {name}")
            with traced(
                f"node.{name}",
                control_flow=(GraphBubbleUp,),
                **{"agent.name": AGENT_NAME, "invoice.id": state.get("invoice_id")},
            ):
                return await fn(state)

        wrapped.__name__ = name
        return wrapped

    # -- nodes ---------------------------------------------------------------

    async def load_invoice(self, state: InvoiceState) -> dict[str, Any]:
        try:
            invoice = await self.tools.get_invoice(state["invoice_id"])
        except RecordNotFoundError:
            return {
                "invoice": None,
                "discrepancies": [
                    {
                        "kind": "invoice_not_found",
                        "severity": "critical",
                        "detail": f"invoice {state['invoice_id']} not found",
                    }
                ],
                "route": "reject",
            }
        return {"invoice": invoice}

    async def fetch_records(self, state: InvoiceState) -> dict[str, Any]:
        invoice = state["invoice"]
        assert invoice is not None  # guaranteed by route_after_load  # noqa: S101
        po_number = invoice["po_number"]

        async def purchase_order() -> dict[str, Any] | None:
            try:
                return await self.tools.get_purchase_order(po_number)
            except RecordNotFoundError:
                return None

        try:
            async with asyncio.TaskGroup() as tg:
                po_task = tg.create_task(purchase_order())
                tickets_task = tg.create_task(self.tools.get_delivery_tickets(po_number))
        except* (KillSwitchEngaged, ToolCallFailed) as group:
            # Surface the first failure as-is so the runner can halt on its concrete type.
            raise group.exceptions[0] from None
        return {"purchase_order": po_task.result(), "delivery_tickets": tickets_task.result()}

    async def fetch_supplier_contract(self, state: InvoiceState) -> dict[str, Any]:
        invoice = state["invoice"]
        assert invoice is not None  # noqa: S101
        try:
            contract = await self.tools.get_supplier_contract(
                invoice["product"], invoice["supplier_id"]
            )
        except RecordNotFoundError:
            logger.info("no contract on file for %s", invoice["supplier_id"])
            contract = None
        return {"supplier_contracts": contract}

    async def check_discrepancies(self, state: InvoiceState) -> dict[str, Any]:
        invoice = state["invoice"]
        assert invoice is not None  # noqa: S101
        discrepancies = find_discrepancies(
            invoice, state.get("purchase_order"), state.get("delivery_tickets", []), self.config
        )
        contract = state.get("supplier_contract")
        if contract is not None:
            discrepancies += contract_discrepancies(invoice, contract)
        route = choose_route(discrepancies, invoice["total_usd"], self.config)
        flags = []
        if looks_like_injection(invoice.get("supplier_notes")):
            flags.append("supplier_notes")
        for ticket in state.get("delivery_tickets", []):
            if looks_like_injection(ticket.get("driver_notes")):
                flags.append(f"driver_notes:{ticket['ticket_id']}")
        record_decision(
            AGENT_NAME,
            route,
            **{
                "invoice.id": state["invoice_id"],
                "invoice.total_usd": invoice["total_usd"],
                "discrepancy.count": len(discrepancies),
                "injection.flags": ",".join(flags) or None,
            },
        )
        return {"discrepancies": discrepancies, "route": route, "injection_flags": flags}

    async def summarize(self, state: InvoiceState) -> dict[str, Any]:
        invoice = state["invoice"]
        assert invoice is not None  # noqa: S101
        lines = [
            f"Invoice {invoice['invoice_id']} from {invoice['supplier_name']} "
            f"for ${invoice['total_usd']:,.2f} against {invoice['po_number']}.",
        ]
        lines += [
            f"FACT: {d['kind']} ({d['severity']}): {d['detail']}" for d in state["discrepancies"]
        ]
        lines.append(wrap_untrusted("supplier_notes", invoice.get("supplier_notes")))
        try:
            summary = await self.text_model.complete(
                system=SUMMARY_SYSTEM_PROMPT, prompt="\n".join(lines)
            )
        except (APIError, TimeoutError) as exc:
            logger.warning("summary model unavailable for %s: %s", invoice["invoice_id"], exc)
            summary = "; ".join(d["detail"] for d in state["discrepancies"]) or "no discrepancies"
        if state.get("injection_flags"):
            summary += (
                " Note: free-text fields contain instruction-like text and were ignored: "
                + ", ".join(state["injection_flags"])
                + "."
            )
        return {"summary": summary}

    async def human_review(self, state: InvoiceState) -> dict[str, Any]:
        invoice = state["invoice"]
        assert invoice is not None  # noqa: S101
        # No side effects before interrupt(): this node re-runs from the top on resume.
        payload = interrupt(
            {
                "invoice_id": invoice["invoice_id"],
                "supplier": invoice["supplier_name"],
                "total_usd": invoice["total_usd"],
                "discrepancies": state["discrepancies"],
                "summary": state.get("summary", ""),
                "injection_flags": state.get("injection_flags", []),
            }
        )
        review = _validate_review(payload)
        record_decision(
            AGENT_NAME,
            "reviewer_approved" if review["approved"] else "reviewer_rejected",
            **{"invoice.id": invoice["invoice_id"], "reviewer": review["reviewer"]},
        )
        return {"review": review}

    def _authorize(self, state: InvoiceState, decision: str) -> tuple[str, str]:
        """Approval gate for the high-risk ``post_invoice_decision`` tool.

        Approvals are only posted when the deterministic route allowed
        auto-approval or a named human reviewer approved. Returns (decided_by, reason).
        """
        review = state.get("review")
        if decision == "approved":
            if review is not None and review["approved"]:
                return review["reviewer"], review["comment"] or "approved by reviewer"
            if state.get("route") == "auto_approve" and not state.get("discrepancies"):
                return "auto", "matched PO and deliveries within tolerance"
            raise ApprovalRequiredError(
                f"refusing to approve {state['invoice_id']} without authorization"
            )
        if review is not None and not review["approved"]:
            return review["reviewer"], review["comment"] or "rejected by reviewer"
        reasons = "; ".join(d["detail"] for d in state.get("discrepancies", []))
        return "auto", reasons or "rejected by policy"

    async def _post(
        self, state: InvoiceState, decision: Literal["approved", "rejected"]
    ) -> dict[str, Any]:
        decided_by, reason = self._authorize(state, decision)
        await self.tools.post_invoice_decision(state["invoice_id"], decision, reason, decided_by)
        record_decision(
            AGENT_NAME, decision, **{"invoice.id": state["invoice_id"], "decided_by": decided_by}
        )
        return {"outcome": decision, "outcome_reason": reason, "decided_by": decided_by}

    async def approve(self, state: InvoiceState) -> dict[str, Any]:
        return await self._post(state, "approved")

    async def reject(self, state: InvoiceState) -> dict[str, Any]:
        return await self._post(state, "rejected")

    async def escalate_to_procurement(self, state: InvoiceState) -> dict[str, Any]:
        record_decision(AGENT_NAME, "escalated", **{"invoice.id": state["invoice_id"]})
        result = await self._post(state, "rejected")
        result["outcome_reason"] = f"held for procurement review: {result['outcome_reason']}"
        return result

    # -- edges ---------------------------------------------------------------

    @staticmethod
    def route_after_load(state: InvoiceState) -> Literal["fetch_records", "reject"]:
        return "reject" if state.get("invoice") is None else "fetch_records"

    @staticmethod
    def route_after_summary(state: InvoiceState) -> Route:
        return state["route"]

    @staticmethod
    def route_after_review(state: InvoiceState) -> Literal["approve", "reject"]:
        review = state.get("review")
        return "approve" if review is not None and review["approved"] else "reject"

    def _build(self, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
        builder = StateGraph(InvoiceState)
        for name in (
            "load_invoice",
            "fetch_records",
            "fetch_supplier_contract",
            "check_discrepancies",
            "summarize",
            "human_review",
            "approve",
            "reject",
            "escalate_to_procurement",
        ):
            builder.add_node(name, self._node(name, getattr(self, name)))

        builder.add_edge(START, "load_invoice")
        builder.add_conditional_edges(
            "load_invoice",
            self.route_after_load,
            {"fetch_records": "fetch_records", "reject": "reject"},
        )
        builder.add_edge("fetch_records", "fetch_supplier_contract")
        builder.add_edge("fetch_supplier_contract", "check_discrepancies")
        builder.add_edge("check_discrepancies", "summarize")
        builder.add_conditional_edges(
            "summarize",
            self.route_after_summary,
            {
                "auto_approve": "approve",
                "human_review": "human_review",
                "reject": "reject",
                "escalate": "escalate_to_procurement",
            },
        )
        builder.add_conditional_edges(
            "human_review", self.route_after_review, {"approve": "approve", "reject": "reject"}
        )
        builder.add_edge("approve", END)
        builder.add_edge("reject", END)
        builder.add_edge("escalate_to_procurement", END)
        return builder.compile(checkpointer=checkpointer, name=AGENT_NAME)

    # -- running -------------------------------------------------------------

    def _run_config(self, thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": int(self.config.option("recursion_limit")),
        }

    async def _drive(self, graph_input: Any, thread_id: str) -> RunResult:
        run_config = self._run_config(thread_id)
        with traced(
            f"agent.{AGENT_NAME}.run",
            **{
                "agent.name": AGENT_NAME,
                "agent.risk_tier": self.config.risk_tier.value,
                "thread.id": thread_id,
            },
        ) as span:
            try:
                await self.graph.ainvoke(graph_input, run_config)
            except KillSwitchEngaged as exc:
                span.set_attribute("run.status", "halted")
                return RunResult("halted", thread_id, reason=str(exc))
            except GraphRecursionError as exc:
                span.set_attribute("run.status", "halted")
                return RunResult("halted", thread_id, reason=f"recursion limit reached: {exc}")
            except (ToolCallFailed, ApprovalRequiredError) as exc:
                span.set_attribute("run.status", "halted")
                logger.warning("%s halted on thread %s: %s", AGENT_NAME, thread_id, exc)
                return RunResult("halted", thread_id, reason=str(exc))

            snapshot = await self.graph.aget_state(run_config)
            values = dict(snapshot.values)
            if snapshot.interrupts:
                span.set_attribute("run.status", "awaiting_review")
                return RunResult(
                    "awaiting_review",
                    thread_id,
                    state=values,
                    review_request=snapshot.interrupts[0].value,
                )
            span.set_attribute("run.status", "completed")
            return RunResult("completed", thread_id, state=values)

    async def start(self, invoice_id: str, thread_id: str | None = None) -> RunResult:
        thread = thread_id or f"{AGENT_NAME}-{invoice_id}-{uuid.uuid4().hex[:8]}"
        return await self._drive({"invoice_id": invoice_id}, thread)

    async def resume(self, thread_id: str, decision: ReviewDecision) -> RunResult:
        _validate_review(decision)
        return await self._drive(Command(resume=decision), thread_id)
