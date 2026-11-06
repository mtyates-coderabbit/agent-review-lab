from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest

from northwind.config import AgentConfig, RetrySettings, get_agent_config
from northwind.mocks import FaultPlan, MockBackend, create_backend
from northwind.safety import KillSwitch
from northwind.tracing import configure_tracing

TEST_KILL_ENV = "NORTHWIND_TEST_KILL_SWITCH"


@pytest.fixture(scope="session", autouse=True)
def _trace_to_tmp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    trace_dir = tmp_path_factory.mktemp("traces")
    configure_tracing(trace_dir)
    return trace_dir


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NORTHWIND_KILL_SWITCH", raising=False)


@pytest.fixture
def kill_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KillSwitch:
    monkeypatch.delenv(TEST_KILL_ENV, raising=False)
    return KillSwitch(env_var=TEST_KILL_ENV, flag_file=tmp_path / ".killswitch")


@pytest.fixture
def faults() -> FaultPlan:
    return FaultPlan()


@pytest.fixture
def backend(faults: FaultPlan) -> Iterator[MockBackend]:
    b = create_backend(faults)
    yield b
    b.close()


def fast_config(name: str, **options: object) -> AgentConfig:
    """Agent config with tiny backoff so retry tests stay fast."""
    base = get_agent_config(name)
    return dataclasses.replace(
        base,
        retry=RetrySettings(max_attempts=3, initial_backoff_seconds=0.01, max_backoff_seconds=0.02),
        options={**base.options, **options},
    )
