"""LLM provider abstraction layer.

Supports Anthropic (Claude) and Google (Gemini) via a common interface.
"""

from .base import LLMProvider
from .types import ContentBlock, LLMResponse, Usage


def create_provider(config: dict) -> LLMProvider:
    """Create an LLM provider from config.

    Uses lazy imports so only the active provider's SDK is required.
    """
    provider_name = config.get("provider", "anthropic")

    if provider_name == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider()
    elif provider_name == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider()
    else:
        raise ValueError(f"Unknown provider: {provider_name!r}. Use 'anthropic' or 'gemini'.")
