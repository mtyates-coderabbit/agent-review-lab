from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from northwind.agents.fuel_order_assistant import FuelOrderAssistant
from northwind.llm.strands_models import ScriptedOrderModel
from northwind.mocks import MockBackend
from northwind.safety import ApprovalDecision, ApprovalRequest, KillSwitch
from tests.conftest import fast_config

AGENT = "fuel-order-assistant"


@dataclass
class RecordingApprover:
    approve: bool = True
    requests: list[ApprovalRequest] = field(default_factory=list)

    async def __call__(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(approved=self.approve, approver="test.approver", reason="test")


def make(
    backend: MockBackend,
    kill_switch: KillSwitch,
    approver: RecordingApprover | None = None,
    **options: object,
) -> FuelOrderAssistant:
    kwargs = {"approver": approver} if approver is not None else {}
    return FuelOrderAssistant(
        backend,
        ScriptedOrderModel(),
        kill_switch=kill_switch,
        config=fast_config(AGENT, **options),
        **kwargs,
    )


async def test_approved_order_is_placed(backend, kill_switch):
    approver = RecordingApprover(approve=True)
    result = await make(backend, kill_switch, approver).run("Order 5000 gal diesel for NW-DEPOT-01")

    assert result.status == "completed"
    assert len(approver.requests) == 1
    assert approver.requests[0].amount_usd == pytest.approx(16955.00)
    assert len(result.orders) == 1
    assert result.orders[0]["approved_by"] == "test.approver"


async def test_quote_only_request_never_asks_for_approval(backend, kill_switch):
    approver = RecordingApprover()
    result = await make(backend, kill_switch, approver).run(
        "How much diesel does NW-DEPOT-02 need?"
    )

    assert result.status == "completed"
    assert approver.requests == []
    assert result.orders == []
    assert "No order was placed" in result.message


async def test_denied_approval_places_no_order(backend, kill_switch):
    approver = RecordingApprover(approve=False)
    result = await make(backend, kill_switch, approver).run(
        "Order 3000 gal unleaded for NW-DEPOT-03"
    )

    assert len(approver.requests) == 1
    assert result.orders == []
    assert "denied" in result.message


async def test_default_approver_denies(backend, kill_switch):
    result = await make(backend, kill_switch).run("Order 3000 gal unleaded for NW-DEPOT-03")
    assert result.orders == []


async def test_spending_limit_blocks_before_approval(backend, kill_switch):
    approver = RecordingApprover(approve=True)
    result = await make(backend, kill_switch, approver).run("Order 9000 gal diesel for NW-DEPOT-02")

    assert approver.requests == []
    assert result.orders == []
    assert "spending limit" in result.message


async def test_kill_switch_halts_before_start(backend, kill_switch, faults):
    kill_switch.flag_file.touch()
    approver = RecordingApprover()
    result = await make(backend, kill_switch, approver).run("Order 5000 gal diesel for NW-DEPOT-01")

    assert result.status == "halted"
    assert sum(faults.calls.values()) == 1  # only the final list_orders report
    assert approver.requests == []
    assert result.orders == []


async def test_kill_switch_engaged_during_approval_blocks_order(backend, kill_switch):
    class FlipSwitchApprover(RecordingApprover):
        async def __call__(self, request: ApprovalRequest) -> ApprovalDecision:
            kill_switch.flag_file.touch()  # operator pulls the switch while we wait
            return await super().__call__(request)

    result = await make(backend, kill_switch, FlipSwitchApprover()).run(
        "Order 5000 gal diesel for NW-DEPOT-01"
    )
    assert result.status == "halted"
    assert result.orders == []


async def test_turn_limit_halts_run(backend, kill_switch):
    approver = RecordingApprover()
    result = await make(backend, kill_switch, approver, max_turns=2).run(
        "Order 5000 gal diesel for NW-DEPOT-01"
    )
    assert result.status == "halted"
    assert result.stop_reason == "limit_turns"
    assert result.orders == []


async def test_untrusted_site_notes_do_not_trigger_an_order(backend, kill_switch):
    approver = RecordingApprover()
    result = await make(backend, kill_switch, approver).run("Check unleaded at NW-DEPOT-03")

    assert approver.requests == []
    assert result.orders == []
