"""Strands hook providers enforcing Northwind's agent safety controls.

* ``KillSwitchHook`` stops the run before the invocation, before every model
  call, and before every tool call when the kill switch is engaged.
* ``ApprovalGateHook`` requires an explicit human approval for every high-risk
  tool call and refuses anything over the spending limit before asking.
* ``ToolAuditHook`` records the outcome of every tool call as a trace decision.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from strands.hooks import (
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from northwind.agents.fuel_order_assistant.tools import APPROVALS_KEY
from northwind.config import AgentConfig
from northwind.mocks.errors import RecordNotFoundError
from northwind.mocks.services import PricingService
from northwind.safety import ApprovalRequest, Approver, KillSwitch
from northwind.tracing import record_decision

logger = logging.getLogger(__name__)

APPROVER_TIMEOUT_SECONDS = 300.0


class KillSwitchHook(HookProvider):
    def __init__(self, agent_name: str, kill_switch: KillSwitch) -> None:
        self.agent_name = agent_name
        self.kill_switch = kill_switch
        self.halted = False

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:  # noqa: ARG002
        registry.add_callback(BeforeInvocationEvent, self.before_invocation)
        registry.add_callback(BeforeModelCallEvent, self.before_model_call)
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)

    def _halt(self, where: str) -> str:
        self.halted = True
        message = f"Kill switch engaged; {self.agent_name} halted before {where}."
        record_decision(self.agent_name, "halted", reason="kill_switch", where=where)
        return message

    def before_invocation(self, event: BeforeInvocationEvent) -> None:
        self.halted = False
        if self.kill_switch.is_engaged():
            event.cancel = self._halt("starting")

    def before_model_call(self, event: BeforeModelCallEvent) -> None:
        if self.kill_switch.is_engaged():
            event.cancel = self._halt("model call")

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        if self.kill_switch.is_engaged():
            event.cancel_tool = self._halt(f"tool call {event.tool_use['name']}")


class ApprovalGateHook(HookProvider):
    def __init__(
        self,
        config: AgentConfig,
        approver: Approver,
        pricing: PricingService,
    ) -> None:
        self.config = config
        self.approver = approver
        self.pricing = pricing
        self.spending_limit = float(config.option("spending_limit_usd"))

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:  # noqa: ARG002
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)

    async def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use["name"]
        if name not in self.config.high_risk_tools or event.cancel_tool:
            return
        tool_input = event.tool_use.get("input") or {}
        quote_id = tool_input.get("quote_id") if isinstance(tool_input, dict) else None
        if not isinstance(quote_id, str):
            event.cancel_tool = f"{name} denied: a quote_id is required"
            return
        try:
            quote = await self.pricing.get_quote(quote_id)
        except RecordNotFoundError:
            event.cancel_tool = f"{name} denied: quote {quote_id} not found"
            return

        amount = float(quote["total_usd"])
        if amount > self.spending_limit:
            record_decision(
                self.config.name, "denied", tool=name, reason="spending_limit", amount_usd=amount
            )
            event.cancel_tool = (
                f"{name} denied: ${amount:,.2f} exceeds the ${self.spending_limit:,.2f} "
                "spending limit"
            )
            return

        request = ApprovalRequest(
            agent=self.config.name,
            tool=name,
            summary=(
                f"Order {quote['gallons']:,} gal {quote['product']} for {quote['site_id']} "
                f"at ${amount:,.2f} (quote {quote_id})"
            ),
            amount_usd=amount,
            details={"quote_id": quote_id},
        )
        try:
            async with asyncio.timeout(APPROVER_TIMEOUT_SECONDS):
                decision = await self.approver(request)
        except TimeoutError:
            event.cancel_tool = f"{name} denied: approval timed out"
            record_decision(self.config.name, "denied", tool=name, reason="approval_timeout")
            return

        record_decision(
            self.config.name,
            "approved" if decision.approved else "denied",
            tool=name,
            approver=decision.approver,
            amount_usd=amount,
        )
        if not decision.approved:
            event.cancel_tool = f"{name} denied by {decision.approver}: {decision.reason}"
            return
        approvals = event.invocation_state.setdefault(APPROVALS_KEY, {})
        approvals[event.tool_use["toolUseId"]] = decision


class ToolAuditHook(HookProvider):
    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:  # noqa: ARG002
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        status = event.result.get("status", "unknown") if event.result else "unknown"
        record_decision(
            self.agent_name,
            f"tool_{status}",
            tool=event.tool_use["name"],
            cancelled=bool(event.cancel_message),
        )
