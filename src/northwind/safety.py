"""Safety controls shared by every agent: kill switch and approval gate types."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from northwind.config import load_runtime_settings


class AgentHalted(RuntimeError):
    """Base class for conditions that must stop an agent run."""


class KillSwitchEngaged(AgentHalted):
    """Raised when the operator kill switch is on."""


class IterationLimitExceeded(AgentHalted):
    """Raised when an agent exceeds its configured iteration budget."""


@dataclass(frozen=True)
class KillSwitch:
    """Operator kill switch.

    The switch is engaged when either the environment variable is truthy or the
    flag file exists. Both are re-read on every check so an operator can halt a
    running agent without restarting it.
    """

    env_var: str
    flag_file: Path

    @classmethod
    def from_settings(cls) -> KillSwitch:
        settings = load_runtime_settings()
        return cls(env_var=settings.kill_switch_env, flag_file=settings.kill_switch_file)

    def is_engaged(self) -> bool:
        raw = os.environ.get(self.env_var, "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        return self.flag_file.exists()

    def check(self, *, agent: str, action: str) -> None:
        if self.is_engaged():
            raise KillSwitchEngaged(f"kill switch engaged; {agent} halted before {action}")


@dataclass(frozen=True)
class ApprovalRequest:
    agent: str
    tool: str
    summary: str
    amount_usd: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    approver: str
    reason: str = ""


Approver = Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]


async def deny_all(request: ApprovalRequest) -> ApprovalDecision:
    """Default approver: refuse everything. Callers must opt in to approvals."""
    return ApprovalDecision(
        approved=False, approver="policy", reason=f"no approver for {request.tool}"
    )
