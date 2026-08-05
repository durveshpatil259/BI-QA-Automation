"""Shared client for OpenAI-compatible chat-completion APIs.

Grok (xAI), OpenAI, DeepSeek and Qwen all expose the same wire format:
``POST {base_url}/chat/completions`` with a Bearer token and a
``{model, messages, temperature, max_tokens}`` body, returning
``choices[0].message.content``. This base implements that once; providers differ
only by default base URL and default model.

The HTTP call is isolated in :meth:`_post` so request building and response
parsing can be unit-tested without network access.
"""

from __future__ import annotations

from src.core.exceptions import LLMConfigError, LLMProviderError, LLMResponseError
from src.core.logger import get_logger
from src.services.llm.base import LLMClient, LLMMessage, LLMResponse

_logger = get_logger()

_DEFAULT_TIMEOUT = 90  # seconds


class OpenAICompatibleClient(LLMClient):
    """Base for providers speaking the OpenAI chat-completions protocol."""

    #: Default API base URL (no trailing slash); overridden per provider.
    default_base_url: str = ""

    @property
    def base_url(self) -> str:
        url = (self.settings.base_url or self.default_base_url).strip().rstrip("/")
        return url

    # --- request/response (pure, testable) --------------------------------
    def _build_payload(self, messages: list[LLMMessage]) -> dict:
        return {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": float(self.settings.temperature),
            "max_tokens": int(self.settings.max_tokens),
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }

    def _parse_response(self, data: dict) -> LLMResponse:
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(f"Unexpected response shape: {exc}") from exc
        return LLMResponse(
            content=content or "",
            model=data.get("model", self.model),
            finish_reason=choice.get("finish_reason", ""),
            usage=data.get("usage", {}) or {},
        )

    # --- transport (isolated for testing) ---------------------------------
    def _post(self, payload: dict) -> dict:
        import requests

        endpoint = f"{self.base_url}/chat/completions"
        try:
            resp = requests.post(
                endpoint, json=payload, headers=self._headers(), timeout=_DEFAULT_TIMEOUT
            )
        except requests.RequestException as exc:
            raise LLMProviderError(f"Network error calling {endpoint}: {exc}") from exc

        if resp.status_code >= 400:
            detail = self._error_detail(resp)
            raise LLMProviderError(
                f"{self.settings.provider} API error {resp.status_code}: {detail}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise LLMResponseError("Provider returned non-JSON response.") from exc

    @staticmethod
    def _error_detail(resp) -> str:
        try:
            body = resp.json()
            if isinstance(body, dict):
                err = body.get("error")
                if isinstance(err, dict):
                    return err.get("message", str(err))
                return str(err or body)
            return str(body)
        except ValueError:
            return (resp.text or "")[:300]

    # --- discovery --------------------------------------------------------
    def list_models(self) -> list[str]:
        """Return model ids available to this API key (``GET /models``)."""
        import requests

        if not self.settings.api_key.strip():
            raise LLMConfigError(
                f"No API key configured for {self.settings.provider}."
            )
        endpoint = f"{self.base_url}/models"
        try:
            resp = requests.get(endpoint, headers=self._headers(), timeout=30)
        except requests.RequestException as exc:
            raise LLMProviderError(f"Network error calling {endpoint}: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMProviderError(
                f"{self.settings.provider} API error {resp.status_code}: "
                f"{self._error_detail(resp)}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMResponseError("Provider returned non-JSON model list.") from exc
        items = data.get("data", data) if isinstance(data, dict) else data
        ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
        return sorted(ids)

    # --- public -----------------------------------------------------------
    def chat(self, messages: list[LLMMessage]) -> LLMResponse:
        if not self.settings.api_key.strip():
            raise LLMConfigError(
                f"No API key configured for {self.settings.provider}. "
                "Add your key in the LLM configuration."
            )
        if not self.base_url:
            raise LLMConfigError(f"No base URL configured for {self.settings.provider}.")
        payload = self._build_payload(messages)
        _logger.info("Calling %s model=%s", self.settings.provider, self.model)
        return self._parse_response(self._post(payload))

    # --- vision (multimodal chat completions) -----------------------------
    @staticmethod
    def _detect_image_format(data: bytes) -> str:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if data[:3] == b"\xff\xd8\xff":
            return "jpeg"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        if data[:2] == b"BM":
            return "bmp"
        return "png"

    def vision_complete(
        self,
        system: str,
        user: str,
        images: list[bytes],
        *,
        image_formats: list[str] | None = None,
    ) -> LLMResponse:
        import base64

        if not self.settings.api_key.strip():
            raise LLMConfigError(
                f"No API key configured for {self.settings.provider}."
            )
        if not self.base_url:
            raise LLMConfigError(f"No base URL configured for {self.settings.provider}.")

        content: list[dict] = [{"type": "text", "text": user}]
        for i, img in enumerate(images):
            fmt = (
                image_formats[i] if image_formats and i < len(image_formats)
                else self._detect_image_format(img)
            )
            b64 = base64.b64encode(img).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/{fmt};base64,{b64}"},
            })

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": float(self.settings.temperature),
            "max_tokens": int(self.settings.max_tokens),
        }
        _logger.info(
            "Calling %s vision model=%s with %d image(s)",
            self.settings.provider, self.model, len(images),
        )
        return self._parse_response(self._post(payload))
