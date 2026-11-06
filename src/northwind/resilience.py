"""Guarded tool execution: kill switch, timeout, retries with backoff, and tracing.

Every tool call in both agents goes through :func:`run_tool`, so the safety
controls are applied in one place rather than re-implemented per tool.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from northwind.config import AgentConfig
from northwind.mocks.errors import TransientServiceError
from northwind.safety import KillSwitch
from northwind.tracing import traced

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

RETRYABLE_ERRORS: tuple[type[Exception], ...] = (TransientServiceError, TimeoutError)


class ToolCallFailed(RuntimeError):
    """Raised when a tool exhausts its retries."""

    def __init__(self, tool: str, attempts: int, cause: Exception) -> None:
        super().__init__(f"{tool} failed after {attempts} attempt(s): {cause}")
        self.tool = tool
        self.attempts = attempts


def _backoff(attempt: int, config: AgentConfig) -> float:
    base = config.retry.initial_backoff_seconds * (2 ** (attempt - 1))
    capped = min(base, config.retry.max_backoff_seconds)
    return capped * (0.5 + random.random() / 2)  # noqa: S311 - jitter, not crypto


async def run_tool(
    config: AgentConfig,
    kill_switch: KillSwitch,
    tool_name: str,
    func: Callable[P, Awaitable[T]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run ``func`` as a tool call under the agent's safety controls.

    Order of operations per attempt: kill-switch check, then the call under a
    per-tool timeout. Only transient errors and timeouts are retried; anything
    else propagates immediately so the caller can handle it specifically.
    """
    timeout = config.tool_timeout(tool_name)
    attempts = config.retry.max_attempts
    with traced(
        f"tool.{tool_name}",
        **{
            "agent.name": config.name,
            "agent.risk_tier": config.risk_tier.value,
            "tool.name": tool_name,
            "tool.timeout_s": timeout,
            "tool.high_risk": tool_name in config.high_risk_tools,
        },
    ) as span:
        for attempt in range(1, attempts + 1):
            kill_switch.check(agent=config.name, action=f"tool call {tool_name}")
            span.set_attribute("tool.attempt", attempt)
            try:
                async with asyncio.timeout(timeout):
                    result = await func(*args, **kwargs)
            except RETRYABLE_ERRORS as exc:
                span.add_event("tool.retryable_error", {"attempt": attempt, "error": repr(exc)})
                if attempt == attempts:
                    raise ToolCallFailed(tool_name, attempt, exc) from exc
                delay = _backoff(attempt, config)
                logger.warning(
                    "tool %s attempt %d/%d failed (%s); retrying in %.2fs",
                    tool_name,
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                span.set_attribute("tool.outcome", "ok")
                return result
    raise AssertionError("unreachable")  # pragma: no cover
