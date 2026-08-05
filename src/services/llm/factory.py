"""Factory mapping :class:`LLMSettings` to a concrete :class:`LLMClient`."""

from __future__ import annotations

from src.core.constants import LLMProvider
from src.core.exceptions import LLMConfigError
from src.domain.models import LLMSettings
from src.services.llm.base import LLMClient
from src.services.llm.providers import (
    DeepSeekClient,
    GrokClient,
    OpenAIClient,
    PendingClient,
    QwenClient,
)

# Providers that share the OpenAI-compatible client, ready to use today.
_ACTIVE: dict[LLMProvider, type[LLMClient]] = {
    LLMProvider.GROK: GrokClient,
    LLMProvider.OPENAI: OpenAIClient,
    LLMProvider.DEEPSEEK: DeepSeekClient,
    LLMProvider.QWEN: QwenClient,
}

# Providers whose dedicated (non-OpenAI-compatible) client ships later.
_PENDING: dict[LLMProvider, str] = {
    LLMProvider.CLAUDE: "Module 8b",
    LLMProvider.GEMINI: "Module 8c",
    LLMProvider.LLAMA: "Module 8d",
}


def create_client(settings: LLMSettings) -> LLMClient:
    provider = settings.provider
    if provider in _ACTIVE:
        return _ACTIVE[provider](settings)
    if provider in _PENDING:
        return PendingClient(settings, _PENDING[provider])
    raise LLMConfigError(f"Unknown LLM provider: {provider}")


def supported_providers() -> list[str]:
    """Provider values available today (for UI selection)."""
    return [p.value for p in _ACTIVE]
