"""Abstract base class for LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .types import LLMResponse


class LLMProvider(ABC):
    """Interface for LLM API providers.

    All inputs use Anthropic-flavored format (the internal "lingua franca"):
    - system: list of {"type": "text", "text": ..., "cache_control": ...}
    - tools: Anthropic tool-use schema (name, description, input_schema)
    - messages: list of {"role": "user"|"assistant", "content": str|list}
    - context_management: Anthropic compaction config (ignored by non-Anthropic)
    """

    @abstractmethod
    def create_message(
        self,
        *,
        model: str,
        max_tokens: int,
        system: list[dict] | None = None,
        tools: list[dict] | None = None,
        messages: list[dict[str, Any]],
        context_management: dict | None = None,
    ) -> LLMResponse:
        """Send a message with tools/system/history and return a normalized response."""
        ...

    @abstractmethod
    def create_triage_message(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        """Simple message creation for triage (no tools, no system, no history)."""
        ...

    def supports_compaction(self) -> bool:
        """Whether this provider supports server-side context compaction."""
        return False

    def supports_cache_control(self) -> bool:
        """Whether this provider supports explicit cache_control annotations."""
        return False
