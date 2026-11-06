from __future__ import annotations

import dataclasses

import pytest

from northwind.agents.invoice_reconciler import InvoiceReconciler
from northwind.agents.invoice_reconciler.graph import (
    ApprovalRequiredError,
    InvalidReviewDecisionError,
)
from northwind.llm.text import MockTextModel
from northwind.mocks import FaultPlan, MockBackend, create_backend
from northwind.safety import KillSwitch
from tests.conftest import fast_config

AGENT = "invoice-reconciler"


def make(backend: MockBackend, kill_switch: KillSwitch, **options: object) -> InvoiceReconciler:
    return InvoiceReconciler(
        backend, MockTextModel(), kill_switch=kill_switch, config=fast_config(AGENT, **options)
    )


async def test_clean_invoice_is_auto_approved(backend, kill_switch):
    result = await make(backend, kill_switch).start("INV-1001")

    assert result.status == "completed"
    assert result.state["route"] == "auto_approve"
    assert result.state["outcome"] == "approved"
    assert result.state["decided_by"] == "auto"
    decision = await backend.erp.get_invoice_decision("INV-1001")
    assert decision is not None
    assert decision["decision"] == "approved"


@pytest.mark.parametrize(
    ("invoice_id", "kind"),
    [
        ("INV-1003", "price"),
        ("INV-1006", "missing_purchase_order"),
        ("INV-0000", "invoice_not_found"),
    ],
)
async def test_critical_discrepancies_are_rejected(backend, kill_switch, invoice_id, kind):
    result = await make(backend, kill_switch).start(invoice_id)

    assert result.status == "completed"
    assert result.state["outcome"] == "rejected"
    assert any(d["kind"] == kind for d in result.state["discrepancies"])


async def test_high_value_invoice_interrupts_for_human_review(backend, kill_switch):
    reconciler = make(backend, kill_switch)
    result = await reconciler.start("INV-1004")

    assert result.status == "awaiting_review"
    assert result.review_request is not None
    assert result.review_request["total_usd"] == 81552.00
    assert result.state["discrepancies"] == []
    # Nothing is posted until a reviewer answers.
    assert await backend.erp.get_invoice_decision("INV-1004") is None

    resumed = await reconciler.resume(
        result.thread_id, {"approved": True, "reviewer": "r.alvarez", "comment": "ok"}
    )
    assert resumed.status == "completed"
    assert resumed.state["outcome"] == "approved"
    assert resumed.state["decided_by"] == "r.alvarez"


async def test_reviewer_can_reject(backend, kill_switch):
    reconciler = make(backend, kill_switch)
    result = await reconciler.start("INV-1002")
    assert result.status == "awaiting_review"

    resumed = await reconciler.resume(
        result.thread_id, {"approved": False, "reviewer": "r.alvarez", "comment": "short load"}
    )
    assert resumed.state["outcome"] == "rejected"
    decision = await backend.erp.get_invoice_decision("INV-1002")
    assert decision is not None
    assert decision["decided_by"] == "r.alvarez"


async def test_injected_supplier_note_does_not_change_routing(backend, kill_switch):
    result = await make(backend, kill_switch).start("INV-1005")

    assert result.status == "awaiting_review"
    assert result.state["route"] == "human_review"
    assert result.state["injection_flags"] == ["supplier_notes"]
    assert "ignored" in result.state["summary"]


async def test_invalid_resume_payload_is_refused(backend, kill_switch):
    reconciler = make(backend, kill_switch)
    result = await reconciler.start("INV-1004")
    with pytest.raises(InvalidReviewDecisionError):
        await reconciler.resume(result.thread_id, {"approved": "yes"})  # type: ignore[typeddict-item]


async def test_approve_node_refuses_without_authorization(backend, kill_switch):
    reconciler = make(backend, kill_switch)
    state = {"invoice_id": "INV-1002", "route": "human_review", "discrepancies": []}
    with pytest.raises(ApprovalRequiredError):
        await reconciler.approve(state)  # type: ignore[arg-type]
    assert await backend.erp.get_invoice_decision("INV-1002") is None


async def test_kill_switch_halts_before_any_tool_call(backend, kill_switch, faults):
    kill_switch.flag_file.touch()
    result = await make(backend, kill_switch).start("INV-1001")

    assert result.status == "halted"
    assert "kill switch" in result.reason
    assert sum(faults.calls.values()) == 0


async def test_kill_switch_engaged_during_review_blocks_decision(backend, kill_switch, monkeypatch):
    reconciler = make(backend, kill_switch)
    result = await reconciler.start("INV-1004")
    assert result.status == "awaiting_review"

    monkeypatch.setenv(kill_switch.env_var, "1")
    resumed = await reconciler.resume(
        result.thread_id, {"approved": True, "reviewer": "r.alvarez", "comment": ""}
    )
    assert resumed.status == "halted"
    assert await backend.erp.get_invoice_decision("INV-1004") is None


async def test_transient_failures_are_retried(kill_switch):
    faults = FaultPlan(failures={"erp.get_invoice": 2})
    backend = create_backend(faults)
    try:
        result = await make(backend, kill_switch).start("INV-1001")
    finally:
        backend.close()
    assert result.status == "completed"
    assert faults.calls["erp.get_invoice"] == 3


async def test_exhausted_retries_halt_cleanly(kill_switch):
    faults = FaultPlan(failures={"erp.get_delivery_tickets": 10})
    backend = create_backend(faults)
    try:
        result = await make(backend, kill_switch).start("INV-1001")
    finally:
        backend.close()
    assert result.status == "halted"
    assert "get_delivery_tickets failed after 3" in result.reason


async def test_slow_tool_times_out(kill_switch):
    faults = FaultPlan(latency_seconds={"erp.get_invoice": 0.5})
    backend = create_backend(faults)
    reconciler = make(backend, kill_switch)
    reconciler.tools = dataclasses.replace(
        reconciler.tools,
        config=dataclasses.replace(reconciler.config, tool_timeouts_seconds={"get_invoice": 0.05}),
    )
    try:
        result = await reconciler.start("INV-1001")
    finally:
        backend.close()
    assert result.status == "halted"
    assert "get_invoice failed" in result.reason


async def test_recursion_limit_halts_cleanly(backend, kill_switch):
    result = await make(backend, kill_switch, recursion_limit=2).start("INV-1001")
    assert result.status == "halted"
    assert "recursion limit" in result.reason
