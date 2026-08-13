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
    ModelOption,
    ProviderListResponse,
    ProviderOption,
)
from src.core.constants import LLMProvider
from src.core.exceptions import LLMError
from src.core.logger import get_logger
from src.domain.models import LLMSettings
from src.services.llm import provider_registry as registry

_logger = get_logger()

#: Speech/audio models a provider lists alongside chat models. They accept no
#: chat completion, so offering them guarantees an undiagnosable failure.
_SPEECH_MODELS = ("whisper", "orpheus", "playai", "allam", "audio", "guard")

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _provider_or_400(value: str) -> LLMProvider:
    try:
        provider = LLMProvider.from_value(value)
    except Exception:  # noqa: BLE001 - unknown string
        raise HTTPException(400, f"Unknown provider '{value}'.")
    if registry.get_config(provider) is None:
        raise HTTPException(400, f"Provider '{value}' is not available.")
    return provider


def _resolved(settings: LLMSettings) -> LLMSettings:
    """Fill endpoint, credential and budget from the registry, server-side.

    The browser only ever sends provider + model; everything else is looked up
    here so no secret or endpoint has to exist in frontend state.
    """
    config = registry.get_config(settings.provider)
    if config is None:
        return settings
    return LLMSettings(
        provider=settings.provider,
        api_key=registry.resolve_api_key(settings.provider, settings.api_key),
        model=settings.model or config.default_model,
        base_url=config.base_url,
        temperature=settings.temperature,
        max_tokens=config.max_tokens,
        is_configured=True,
    )


def _to_response(settings: LLMSettings) -> LLMSettingsResponse:
    resolved = _resolved(settings)
    return LLMSettingsResponse(
        provider=settings.provider.value,
        model=resolved.model,
        model_label=registry.friendly_name(settings.provider, resolved.model),
        is_configured=settings.is_configured,
        # Whether the backend holds a key — never the key itself.
        has_api_key=bool(resolved.api_key.strip()),
    )


@router.get("/llm", response_model=LLMSettingsResponse)
def get_llm_settings(c: Container = Depends(container)):
    return _to_response(c.llm_service.load_global_settings())


@router.get("/llm/providers", response_model=ProviderListResponse)
def list_providers(c: Container = Depends(container)):
    """Providers with a working client, flagged by whether a key is configured."""
    current = c.llm_service.load_global_settings()
    options = [
        ProviderOption(
            id=p.value,
            label=p.value,
            configured=bool(registry.resolve_api_key(p).strip()),
        )
        for p in registry.available_providers()
    ]
    return ProviderListResponse(providers=options, selected=current.provider.value)


@router.get("/llm/providers/{provider}/models", response_model=ModelListResponse)
def list_provider_models(provider: str, c: Container = Depends(container)):
    """Models for a provider, live where possible and from the registry otherwise.

    A GET with no body — the browser cannot supply a key even by accident.
    """
    resolved_provider = _provider_or_400(provider)
    config = registry.get_config(resolved_provider)
    catalogue = [ModelOption(id=i, label=l) for i, l in config.known_models]

    settings = _resolved(LLMSettings(provider=resolved_provider))
    if not settings.api_key.strip():
        return ModelListResponse(
            models=catalogue, default=config.default_model,
            notice=(f"No {resolved_provider.value} key configured on the server — "
                    f"showing the built-in model list."),
        )

    try:
        live = c.llm_service.list_models(settings)
    except LLMError as exc:
        _logger.info("Live model discovery failed for %s: %s", provider, exc)
        return ModelListResponse(
            models=catalogue, default=config.default_model,
            notice=(f"Could not reach {resolved_provider.value} to list models — "
                    f"showing the built-in list."),
        )

    # A provider's /models list includes speech, embedding and vision models
    # that cannot serve a chat completion. Offering them guarantees a failure
    # the user cannot diagnose, so reuse the client's own exclusion list.
    from src.services.llm.openai_compatible import OpenAICompatibleClient

    # Gemini reports ids as "models/gemini-2.5-flash"; the client strips the
    # prefix when calling, so store the bare id or the saved value will not
    # match the registry or the default.
    live = [m.split("/", 1)[1] if m.startswith("models/") else m for m in live]
    skip_terms = OpenAICompatibleClient._NOT_CHAT + _SPEECH_MODELS
    chat_only = [
        m for m in live if not any(skip in m.lower() for skip in skip_terms)
    ]
    if not chat_only:
        return ModelListResponse(models=catalogue, default=config.default_model)

    labels = dict(config.known_models)
    # Known models first, in registry order, then the rest alphabetically.
    order = {mid: i for i, (mid, _) in enumerate(config.known_models)}
    chat_only.sort(key=lambda m: (order.get(m, len(order)), m))
    models = [ModelOption(id=m, label=labels.get(m, m)) for m in chat_only]
    default = config.default_model if config.default_model in chat_only else chat_only[0]
    return ModelListResponse(models=models, default=default)


@router.post("/llm", response_model=LLMSettingsResponse)
def save_llm_settings(body: LLMSettingsRequest, c: Container = Depends(container)):
    provider = _provider_or_400(body.provider)
    if not body.model.strip():
        raise HTTPException(400, "Select a model.")
    saved = c.llm_service.save_global_settings(
        _resolved(LLMSettings(provider=provider, model=body.model.strip()))
    )
    return _to_response(saved)


@router.post("/llm/test", response_model=ConnectionTestResponse)
def test_llm(body: LLMSettingsRequest, c: Container = Depends(container)):
    """Round-trip a tiny prompt so the user gets a definitive yes/no."""
    provider = _provider_or_400(body.provider)
    settings = _resolved(LLMSettings(provider=provider, model=body.model.strip()))
    if not settings.api_key.strip():
        config = registry.get_config(provider)
        return ConnectionTestResponse(
            ok=False,
            message=(f"No {provider.value} key is configured on the server. "
                     f"Set {config.env_var} and restart."),
        )

    from src.services.llm import create_client

    try:
        client = create_client(settings)
        # Providers bill prompt + max_tokens against the daily quota, so a test
        # that inherits the full ceiling burns quota for a one-word reply.
        response = client.complete(
            "You are a test.", "Reply with the word: ready", max_tokens=5
        )
    except LLMError as exc:
        return ConnectionTestResponse(ok=False, message=str(exc))
    except Exception as exc:  # noqa: BLE001 - network/provider errors
        return ConnectionTestResponse(ok=False, message=str(exc))

    model = response.model or client.model
    return ConnectionTestResponse(
        ok=True,
        message=f"Connected to {provider.value} — {model}",
        details={"reply": (response.content or "").strip()[:80]},
    )
