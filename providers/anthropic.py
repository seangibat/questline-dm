"""Anthropic (Claude) provider implementation."""

from __future__ import annotations

from typing import Any

from anthropic import Anthropic

from .base import LLMProvider
from .types import ContentBlock, LLMResponse, Usage


class AnthropicProvider(LLMProvider):
    """Wraps the Anthropic SDK, normalizing responses to common types."""

    def __init__(self) -> None:
        self.client = Anthropic()  # reads ANTHROPIC_API_KEY from env

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
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        if context_management:
            kwargs["context_management"] = context_management
            response = self.client.beta.messages.create(
                betas=["context-management-2025-06-27", "compact-2026-01-12"],
                **kwargs,
            )
        else:
            response = self.client.messages.create(**kwargs)

        return self._normalize(response)

    def create_triage_message(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )
        return self._normalize(response)

    def supports_compaction(self) -> bool:
        return True

    def supports_cache_control(self) -> bool:
        return True

    @staticmethod
    def _normalize(response: Any) -> LLMResponse:
        """Convert Anthropic SDK response to normalized LLMResponse."""
        blocks: list[ContentBlock] = []
        for b in response.content:
            if b.type == "text":
                blocks.append(ContentBlock(type="text", text=b.text))
            elif b.type == "tool_use":
                blocks.append(
                    ContentBlock(
                        type="tool_use",
                        id=b.id,
                        name=b.name,
                        input=b.input,
                    )
                )
            elif b.type == "compaction":
                blocks.append(
                    ContentBlock(
                        type="compaction",
                        text=getattr(b, "text", None),
                    )
                )
            else:
                # Pass through unknown block types as-is
                blocks.append(ContentBlock(type=b.type, text=getattr(b, "text", None)))

        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        )

        return LLMResponse(
            content=blocks,
            stop_reason=response.stop_reason,
            usage=usage,
        )
