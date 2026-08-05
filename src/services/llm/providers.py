"""Concrete LLM provider clients.

Grok is the primary, fully-supported provider for the MVP. OpenAI, DeepSeek and
Qwen share the identical OpenAI-compatible wire format and are therefore thin
subclasses — demonstrating that new providers plug in without architectural
change. Non-compatible providers (Claude, Gemini, Llama) are represented by
:class:`PendingClient` until their dedicated clients ship.
"""

from __future__ import annotations

from src.core.exceptions import LLMConfigError
from src.core.constants import LLMProvider
from src.services.llm.base import LLMClient, LLMMessage, LLMResponse
from src.services.llm.openai_compatible import OpenAICompatibleClient


class GrokClient(OpenAICompatibleClient):
    """xAI Grok — OpenAI-compatible chat completions."""

    default_base_url = "https://api.x.ai/v1"
    # xAI model ids change over time; use "Fetch available models" in the UI to
    # pick one your key supports if this default is not available to your plan.
    default_model = "grok-3"


class OpenAIClient(OpenAICompatibleClient):
    default_base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"


class DeepSeekClient(OpenAICompatibleClient):
    default_base_url = "https://api.deepseek.com/v1"
    default_model = "deepseek-chat"


class QwenClient(OpenAICompatibleClient):
    default_base_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    default_model = "qwen-plus"


class PendingClient(LLMClient):
    """Placeholder for providers whose dedicated client is not implemented yet."""

    def __init__(self, settings, build_step: str):
        super().__init__(settings)
        self._build_step = build_step

    def chat(self, messages: list[LLMMessage]) -> LLMResponse:  # noqa: ARG002
        raise LLMConfigError(
            f"{self.settings.provider} support is planned ({self._build_step}). "
            f"Use Grok (fully supported) for now."
        )
