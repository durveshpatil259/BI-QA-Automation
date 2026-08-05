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
    def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        """Send *messages* to the provider and return the normalised response."""

    # Convenience for the common system+user pattern.
    def complete(self, system: str, user: str) -> LLMResponse:
        return self.chat([LLMMessage("system", system), LLMMessage("user", user)])

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
