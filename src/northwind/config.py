"""Agent configuration and runtime settings.

Agent policy (risk tier, limits, timeouts) lives in ``config/agents.toml`` so it
can be reviewed alongside code. Runtime switches (mock LLM, trace directory,
kill switch) come from environment variables, optionally loaded from ``.env``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "agents.toml"


class RiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ConfigError(ValueError):
    """Raised when agent configuration is missing or invalid."""


@dataclass(frozen=True)
class RetrySettings:
    max_attempts: int
    initial_backoff_seconds: float
    max_backoff_seconds: float


@dataclass(frozen=True)
class AgentConfig:
    name: str
    description: str
    risk_tier: RiskTier
    default_tool_timeout_seconds: float
    retry: RetrySettings
    tool_timeouts_seconds: dict[str, float] = field(default_factory=dict)
    high_risk_tools: frozenset[str] = frozenset()
    options: dict[str, Any] = field(default_factory=dict)

    def tool_timeout(self, tool_name: str) -> float:
        return self.tool_timeouts_seconds.get(tool_name, self.default_tool_timeout_seconds)

    def option(self, key: str) -> Any:
        try:
            return self.options[key]
        except KeyError as exc:
            raise ConfigError(f"agent {self.name!r} is missing required option {key!r}") from exc


_RESERVED_KEYS = {"description", "risk_tier", "tool_timeouts_seconds", "high_risk_tools"}


def _parse_agent(name: str, raw: dict[str, Any], defaults: dict[str, Any]) -> AgentConfig:
    if "risk_tier" not in raw:
        raise ConfigError(f"agent {name!r} must declare a risk_tier")
    try:
        tier = RiskTier(raw["risk_tier"])
    except ValueError as exc:
        raise ConfigError(f"agent {name!r} has invalid risk_tier {raw['risk_tier']!r}") from exc

    retry = RetrySettings(
        max_attempts=int(defaults.get("tool_max_attempts", 3)),
        initial_backoff_seconds=float(defaults.get("retry_initial_backoff_seconds", 0.2)),
        max_backoff_seconds=float(defaults.get("retry_max_backoff_seconds", 2.0)),
    )
    return AgentConfig(
        name=name,
        description=str(raw.get("description", "")),
        risk_tier=tier,
        default_tool_timeout_seconds=float(defaults.get("tool_timeout_seconds", 5.0)),
        retry=retry,
        tool_timeouts_seconds={
            k: float(v) for k, v in raw.get("tool_timeouts_seconds", {}).items()
        },
        high_risk_tools=frozenset(raw.get("high_risk_tools", [])),
        options={k: v for k, v in raw.items() if k not in _RESERVED_KEYS},
    )


@lru_cache(maxsize=4)
def load_agent_configs(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, AgentConfig]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"agent config not found at {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"agent config at {path} is not valid TOML: {exc}") from exc

    defaults = data.get("defaults", {})
    agents = data.get("agents", {})
    return {name: _parse_agent(name, raw, defaults) for name, raw in agents.items()}


def get_agent_config(name: str) -> AgentConfig:
    configs = load_agent_configs()
    try:
        return configs[name]
    except KeyError as exc:
        raise ConfigError(f"no configuration for agent {name!r}") from exc


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RuntimeSettings:
    mock_llm: bool
    anthropic_model_id: str
    trace_dir: Path
    kill_switch_env: str
    kill_switch_file: Path


def load_runtime_settings() -> RuntimeSettings:
    """Read runtime settings. ``MOCK_LLM`` defaults to on so the demo needs no keys."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    return RuntimeSettings(
        mock_llm=_env_flag("MOCK_LLM", default=True),
        anthropic_model_id=os.environ.get("ANTHROPIC_MODEL_ID", "claude-sonnet-5-5"),
        trace_dir=Path(os.environ.get("NORTHWIND_TRACE_DIR", PROJECT_ROOT / "traces")),
        kill_switch_env="NORTHWIND_KILL_SWITCH",
        kill_switch_file=Path(
            os.environ.get("NORTHWIND_KILL_SWITCH_FILE", PROJECT_ROOT / ".killswitch")
        ),
    )
