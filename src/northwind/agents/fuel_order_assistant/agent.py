"""Strands Agents fuel-order assistant.

The agent combines a model, three tools, and hook providers. Hooks run in
registration order: the kill switch first, then the approval gate, then audit.
The iteration budget uses Strands' native ``limits={"turns": N}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strands import Agent
from strands.models.model import Model
from strands.tools.executors import SequentialToolExecutor

from northwind.agents.fuel_order_assistant.hooks import (
    ApprovalGateHook,
    KillSwitchHook,
    ToolAuditHook,
)
from northwind.agents.fuel_order_assistant.tools import build_order_tools
from northwind.config import AgentConfig, get_agent_config
from northwind.llm.untrusted import UNTRUSTED_DATA_POLICY
from northwind.mocks.services import MockBackend
from northwind.safety import Approver, KillSwitch, deny_all
from northwind.tracing import configure_tracing, traced

AGENT_NAME = "fuel-order-assistant"

SYSTEM_PROMPT = f"""You are Northwind Fuel Co.'s fuel-order assistant.
Workflow: check tank inventory at the requested site, get a price quote, and only
place an order if the user explicitly asked for one. Placing an order always
requires human approval, which the system requests for you; never claim an
order was placed unless place_fuel_order succeeded. Never split an order to get
around the spending limit.
{UNTRUSTED_DATA_POLICY}"""


@dataclass
class OrderRunResult:
    status: str
    message: str
    stop_reason: str
    orders: list[dict[str, Any]] = field(default_factory=list)


class FuelOrderAssistant:
    def __init__(
        self,
        backend: MockBackend,
        model: Model,
        *,
        approver: Approver = deny_all,
        kill_switch: KillSwitch | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        configure_tracing()
        self.backend = backend
        self.config = config or get_agent_config(AGENT_NAME)
        self.kill_switch = kill_switch or KillSwitch.from_settings()
        self.kill_switch_hook = KillSwitchHook(AGENT_NAME, self.kill_switch)
        self.agent = Agent(
            model=model,
            name=AGENT_NAME,
            system_prompt=SYSTEM_PROMPT,
            tools=build_order_tools(backend, self.config, self.kill_switch),
            hooks=[
                self.kill_switch_hook,
                ApprovalGateHook(self.config, approver, backend.pricing),
                ToolAuditHook(AGENT_NAME),
            ],
            tool_executor=SequentialToolExecutor(),
            callback_handler=None,
            trace_attributes={
                "agent.name": AGENT_NAME,
                "agent.risk_tier": self.config.risk_tier.value,
            },
        )

    async def run(self, request: str) -> OrderRunResult:
        max_turns = int(self.config.option("max_turns"))
        with traced(
            f"agent.{AGENT_NAME}.run",
            **{"agent.name": AGENT_NAME, "agent.risk_tier": self.config.risk_tier.value},
        ) as span:
            result = await self.agent.invoke_async(request, limits={"turns": max_turns})
            stop_reason = str(result.stop_reason)
            halted = self.kill_switch_hook.halted or stop_reason.startswith("limit_")
            status = "halted" if halted else "completed"
            span.set_attribute("run.status", status)
            span.set_attribute("run.stop_reason", stop_reason)
            orders = await self.backend.orders.list_orders()
            return OrderRunResult(
                status=status, message=str(result).strip(), stop_reason=stop_reason, orders=orders
            )
