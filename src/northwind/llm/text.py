"""Plain text-completion models used by the LangGraph agent.

``MockTextModel`` is deterministic and offline. ``AnthropicTextModel`` calls the
Anthropic Messages API and is only used when ``MOCK_LLM=0`` and an API key is set.
"""

from __future__ import annotations

import os
from typing import Protocol

from anthropic import AsyncAnthropic

from northwind.config import ConfigError, RuntimeSettings

REAL_MODEL_TIMEOUT_SECONDS = 30.0


class TextModel(Protocol):
    name: str

    async def complete(self, *, system: str, prompt: str) -> str: ...


class MockTextModel:
    """Deterministic stand-in: echoes a fixed-format summary of the structured prompt."""

    name = "mock-text-model"

    async def complete(self, *, system: str, prompt: str) -> str:  # noqa: ARG002
        facts = [
            line.removeprefix("FACT: ").strip()
            for line in prompt.splitlines()
            if line.startswith("FACT: ")
        ]
        if not facts:
            return "No discrepancies found; invoice matches purchase order and deliveries."
        return "Reviewer summary: " + "; ".join(facts) + "."


class AnthropicTextModel:
    def __init__(self, model_id: str, api_key: str) -> None:
        self.name = model_id
        self._client = AsyncAnthropic(api_key=api_key, timeout=REAL_MODEL_TIMEOUT_SECONDS)

    async def complete(self, *, system: str, prompt: str) -> str:
        response = await self._client.messages.create(
            model=self.name,
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text")


def build_text_model(settings: RuntimeSettings) -> TextModel:
    if settings.mock_llm:
        return MockTextModel()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ConfigError("MOCK_LLM is off but ANTHROPIC_API_KEY is not set (see .env.example)")
    return AnthropicTextModel(settings.anthropic_model_id, api_key)
