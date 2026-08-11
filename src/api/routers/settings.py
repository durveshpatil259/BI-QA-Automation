"""Machine-level settings — LLM provider configuration for the SPA.

Settings are global rather than per-project because the SPA creates a fresh
project on every run; a project inherits these unless it overrides them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import Container, container
from src.api.schemas import (
    ConnectionTestResponse,
    LLMSettingsRequest,
    LLMSettingsResponse,
    ModelListResponse,
)
from src.core.constants import LLMProvider
from src.core.exceptions import LLMError
from src.domain.models import LLMSettings
from src.services.llm.factory import supported_providers

router = APIRouter(prefix="/api/settings", tags=["settings"])

#: Presets keyed by provider, so choosing a provider fills in the right base
#: URL and a sensible model. ``provider`` must match an LLMProvider value.
PROVIDER_PRESETS = {
    "Groq (free tier)": {
        "provider": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
    },
    "Gemini (free tier)": {
        "provider": "Gemini",
        # MUST end in /openai — the plain /v1beta path is the native API and
        # rejects Bearer tokens with a misleading 401.
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.0-flash",
    },
    "Grok (xAI)": {
        "provider": "Grok",
        "base_url": "https://api.x.ai/v1",
        "model": "grok-3",
    },
    "OpenAI": {
        "provider": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "DeepSeek": {
        "provider": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "Qwen": {
        "provider": "Qwen",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
}


def _to_settings(body: LLMSettingsRequest, existing: LLMSettings) -> LLMSettings:
    # A blank key means "keep the stored one" so the UI never has to round-trip
    # the secret back to the browser.
    api_key = body.api_key if body.api_key else existing.api_key
    return LLMSettings(
        provider=LLMProvider.from_value(body.provider),
        api_key=api_key,
        model=body.model,
        base_url=body.base_url,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
    )


def _to_response(settings: LLMSettings) -> LLMSettingsResponse:
    return LLMSettingsResponse(
        provider=settings.provider.value,
        model=settings.model,
        base_url=settings.base_url,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        is_configured=settings.is_configured,
        # Never return the key itself — only whether one is stored.
        has_api_key=bool(settings.api_key.strip()),
        providers=supported_providers(),
        presets=PROVIDER_PRESETS,
    )


@router.get("/llm", response_model=LLMSettingsResponse)
def get_llm_settings(c: Container = Depends(container)):
    return _to_response(c.llm_service.load_global_settings())


@router.post("/llm", response_model=LLMSettingsResponse)
def save_llm_settings(body: LLMSettingsRequest, c: Container = Depends(container)):
    existing = c.llm_service.load_global_settings()
    saved = c.llm_service.save_global_settings(_to_settings(body, existing))
    return _to_response(saved)


@router.post("/llm/models", response_model=ModelListResponse)
def list_llm_models(body: LLMSettingsRequest, c: Container = Depends(container)):
    """Ask the provider which models this key can actually use."""
    existing = c.llm_service.load_global_settings()
    settings = _to_settings(body, existing)
    if not settings.api_key.strip():
        raise HTTPException(400, "Enter an API key first.")
    try:
        return ModelListResponse(models=c.llm_service.list_models(settings))
    except LLMError as exc:
        raise HTTPException(502, str(exc))


@router.post("/llm/test", response_model=ConnectionTestResponse)
def test_llm(body: LLMSettingsRequest, c: Container = Depends(container)):
    """Round-trip a tiny prompt so the user gets a definitive yes/no."""
    existing = c.llm_service.load_global_settings()
    settings = _to_settings(body, existing)
    if not settings.api_key.strip():
        return ConnectionTestResponse(ok=False, message="Enter an API key first.")

    from src.services.llm import create_client

    try:
        client = create_client(settings)
        # Groq bills prompt + max_tokens against the daily quota, so a test
        # that inherits the user's 10,000 ceiling burns 10% of the free tier
        # per click. The reply is one word.
        response = client.complete(
            "You are a test.", "Reply with the word: ready", max_tokens=5
        )
    except LLMError as exc:
        return ConnectionTestResponse(ok=False, message=str(exc))
    except Exception as exc:  # noqa: BLE001 - network/provider errors
        return ConnectionTestResponse(ok=False, message=str(exc))

    return ConnectionTestResponse(
        ok=True,
        message=f"Connected to {settings.provider} ({response.model or client.model}).",
        details={"reply": (response.content or "").strip()[:80]},
    )
