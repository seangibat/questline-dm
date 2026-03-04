"""Common types for the LLM provider abstraction layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContentBlock:
    """A single block in an LLM response.

    Mirrors Anthropic's block shape so agent.py attribute access
    (.type, .text, .id, .name, .input) works unchanged.
    """

    type: str  # "text", "tool_use", "compaction"
    text: str | None = None
    # Tool use fields (only when type == "tool_use")
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None


@dataclass
class Usage:
    """Token usage for a single API call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""

    content: list[ContentBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)
