"""LLM engine — provider-agnostic abstraction layer.

The application must never depend on a single LLM. This package defines a stable
:class:`LLMClient` contract, concrete provider clients, and a factory that
selects one from :class:`~src.domain.models.LLMSettings`. Grok is implemented
first; additional OpenAI-compatible providers (OpenAI, DeepSeek, Qwen) are thin
subclasses, and non-compatible providers (Claude, Gemini, Llama) plug in later
without any change to callers.

Strict boundary: the LLM only *reasons* over the deterministic AnalysisContext
Python assembled. It never reads datasources, runs SQL, parses dashboards or
performs comparisons.
"""

from src.services.llm.base import LLMClient, LLMMessage, LLMResponse
from src.services.llm.factory import create_client

__all__ = ["LLMClient", "LLMMessage", "LLMResponse", "create_client"]
