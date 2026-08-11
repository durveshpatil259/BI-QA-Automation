"""LLM client contract and shared value types."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from src.domain.models import LLMSettings


@dataclass
class LLMMessage:
    """A single chat message."""

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResponse:
    """Normalised response from any provider."""

    content: str
    model: str = ""
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)


class LLMClient(abc.ABC):
    """Uniform chat interface over any LLM provider."""

    #: Default model used when settings.model is blank.
    default_model: str = ""

    def __init__(self, settings: LLMSettings):
        self.settings = settings

    @property
    def model(self) -> str:
        return (self.settings.model or self.default_model).strip()

    @abc.abstractmethod
    def chat(
        self, messages: list[LLMMessage], *, max_tokens: int | None = None
    ) -> LLMResponse:
        """Send *messages* to the provider and return the normalised response."""

    def effective_max_tokens(self, needed: int | None = None) -> int:
        """Output budget for one call.

        Providers charge ``max_tokens`` against per-minute AND per-day quotas
        even when the reply is far shorter, so reserving a blanket 10,000 for a
        1,500-token answer silently burns the daily allowance. Each caller
        declares what it actually needs; the user's setting remains the ceiling.
        """
        ceiling = int(self.settings.max_tokens or 2048)
        return max(256, min(ceiling, int(needed))) if needed else ceiling

    # Convenience for the common system+user pattern.
    def complete(
        self, system: str, user: str, *, max_tokens: int | None = None
    ) -> LLMResponse:
        return self.chat(
            [LLMMessage("system", system), LLMMessage("user", user)],
            max_tokens=max_tokens,
        )

    def vision_complete(
        self,
        system: str,
        user: str,
        images: list[bytes],
        *,
        image_formats: list[str] | None = None,
    ) -> LLMResponse:
        """Send a system+user prompt together with one or more images.

        Only vision-capable providers/models support this; others raise.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support image input."
        )
