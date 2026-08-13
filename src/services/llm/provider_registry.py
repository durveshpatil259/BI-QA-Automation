"""Single source of truth for provider configuration.

Endpoint, credential source, model list, default model and output budget used
to live in two places — ``PROVIDER_PRESETS`` in the API router and
``default_base_url``/``default_model`` on each client class — so adding a
provider meant editing both and the UI asked the user for values the backend
already knew.

Everything the frontend must never see lives here: the API key is read from the
environment (or the machine config) on the server, and only ``provider`` and
``model`` ever cross the wire.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Imported for its side effect: loading .env into os.environ. resolve_api_key()
# reads the environment BEFORE falling back to the config file, so without this
# a key set only in .env would be invisible whenever the registry is used
# before anything else has imported the config module.
import src.core.config  # noqa: F401
from src.core.constants import LLMProvider

__all__ = [
    "ProviderConfig",
    "PROVIDERS",
    "get_config",
    "available_providers",
    "resolve_api_key",
    "default_model_for",
    "max_tokens_for",
    "friendly_name",
]


@dataclass(frozen=True)
class ProviderConfig:
    """Everything the backend needs to talk to one provider."""

    provider: LLMProvider
    base_url: str
    env_var: str
    default_model: str
    #: Fallback catalogue used when the provider cannot be queried live —
    #: no key configured, offline, or the provider has no /models endpoint.
    known_models: tuple[tuple[str, str], ...] = ()
    #: Output-token budget. Per-call budgets clamp below this; it is only a cap.
    max_tokens: int = 3000
    #: Free-tier tokens per day, counted by the local ledger. 0 disables
    #: enforcement and leaves only tracking, which is the right default for a
    #: paid account where the real limit is money rather than tokens.
    tokens_per_day: int = 0
    #: Per-model overrides for the above, as ``(model_id, tokens_per_day)``.
    tokens_per_day_by_model: tuple[tuple[str, int], ...] = ()

    @property
    def label(self) -> str:
        return self.provider.value


#: Only providers with a working client. Claude and Llama exist in the enum but
#: their clients are not implemented, so offering them would guarantee failure.
PROVIDERS: dict[LLMProvider, ProviderConfig] = {
    LLMProvider.GROQ: ProviderConfig(
        provider=LLMProvider.GROQ,
        base_url="https://api.groq.com/openai/v1",
        env_var="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
        known_models=(
            ("llama-3.3-70b-versatile", "Llama 3.3 70B Versatile"),
            ("llama-3.1-8b-instant", "Llama 3.1 8B Instant"),
            ("openai/gpt-oss-120b", "GPT-OSS 120B"),
            ("openai/gpt-oss-20b", "GPT-OSS 20B"),
        ),
        # Groq's free tier is the one that actually runs out mid-run, so it is
        # the only provider given defaults here. Check your own console — the
        # allowance differs per model and Groq revises it; `llm_tokens_per_day`
        # in app_config.json overrides everything below.
        tokens_per_day=100_000,
        tokens_per_day_by_model=(
            ("llama-3.3-70b-versatile", 100_000),
            ("llama-3.1-8b-instant", 500_000),
            ("openai/gpt-oss-120b", 200_000),
            ("openai/gpt-oss-20b", 200_000),
        ),
    ),
    LLMProvider.GEMINI: ProviderConfig(
        provider=LLMProvider.GEMINI,
        # MUST end in /openai — the plain /v1beta path is the native API and
        # rejects Bearer tokens with a misleading 401.
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env_var="GEMINI_API_KEY",
        default_model="gemini-2.0-flash",
        known_models=(
            ("gemini-2.5-flash", "Gemini 2.5 Flash"),
            ("gemini-2.5-pro", "Gemini 2.5 Pro"),
            ("gemini-2.0-flash", "Gemini 2.0 Flash"),
        ),
    ),
    LLMProvider.OPENAI: ProviderConfig(
        provider=LLMProvider.OPENAI,
        base_url="https://api.openai.com/v1",
        env_var="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
        known_models=(
            ("gpt-4o-mini", "GPT-4o mini"),
            ("gpt-4o", "GPT-4o"),
            ("gpt-4.1-mini", "GPT-4.1 mini"),
        ),
    ),
    LLMProvider.GROK: ProviderConfig(
        provider=LLMProvider.GROK,
        base_url="https://api.x.ai/v1",
        env_var="XAI_API_KEY",
        default_model="grok-3",
        known_models=(
            ("grok-3", "Grok 3"),
            ("grok-3-mini", "Grok 3 Mini"),
        ),
    ),
    LLMProvider.DEEPSEEK: ProviderConfig(
        provider=LLMProvider.DEEPSEEK,
        base_url="https://api.deepseek.com/v1",
        env_var="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
        known_models=(
            ("deepseek-chat", "DeepSeek Chat"),
            ("deepseek-reasoner", "DeepSeek Reasoner"),
        ),
    ),
    LLMProvider.QWEN: ProviderConfig(
        provider=LLMProvider.QWEN,
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        env_var="DASHSCOPE_API_KEY",
        default_model="qwen-plus",
        known_models=(
            ("qwen-plus", "Qwen Plus"),
            ("qwen-max", "Qwen Max"),
            ("qwen-turbo", "Qwen Turbo"),
        ),
    ),
}


def available_providers() -> list[LLMProvider]:
    """Providers with a working client, in a stable display order."""
    return list(PROVIDERS)


def get_config(provider: LLMProvider) -> ProviderConfig | None:
    return PROVIDERS.get(provider)


def resolve_api_key(provider: LLMProvider, stored: str = "") -> str:
    """Server-side credential lookup. Never reaches the browser.

    Precedence: environment variable, then the machine config file, then any
    key already saved with the project — so an existing installation keeps
    working without setting environment variables.
    """
    config = PROVIDERS.get(provider)
    if config:
        from_env = os.environ.get(config.env_var, "").strip()
        if from_env:
            return from_env
    try:
        from src.core.config import load_config

        defaults = getattr(load_config(), "default_api_keys", {}) or {}
        for key, value in defaults.items():
            if key.strip().casefold() == provider.value.casefold() and str(value).strip():
                return str(value).strip()
    except Exception:  # noqa: BLE001 - config is optional
        pass
    return (stored or "").strip()


def default_model_for(provider: LLMProvider) -> str:
    config = PROVIDERS.get(provider)
    return config.default_model if config else ""


def max_tokens_for(provider: LLMProvider) -> int:
    config = PROVIDERS.get(provider)
    return config.max_tokens if config else 3000


def tokens_per_day_for(provider: LLMProvider, model: str = "") -> int:
    """Free-tier daily token cap, or 0 when there is no cap to enforce.

    A per-model override wins: on Groq the same key gets a very different
    daily allowance for a 70B model than for an 8B one, so a single
    provider-wide number would either throttle the small model needlessly or
    let the large one overrun.
    """
    config = PROVIDERS.get(provider)
    if config is None:
        return 0
    wanted = (model or "").strip().casefold()
    for model_id, limit in config.tokens_per_day_by_model:
        if model_id.casefold() == wanted:
            return limit
    return config.tokens_per_day


def friendly_name(provider: LLMProvider, model_id: str) -> str:
    """Human label for a model id, falling back to the id itself."""
    config = PROVIDERS.get(provider)
    if config:
        for known_id, label in config.known_models:
            if known_id.casefold() == (model_id or "").casefold():
                return label
    return model_id or ""
