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

import re
import time

from src.core import cancellation, usage
from src.core.exceptions import LLMConfigError, LLMProviderError, LLMResponseError
from src.core.logger import get_logger
from src.services.llm.base import LLMClient, LLMMessage, LLMResponse
from src.services.llm.rate_limiter import TokenRateLimiter

#: One budget for the whole process — concurrent runs share the same quota.
_PACER: TokenRateLimiter | None = None


def _pacer() -> TokenRateLimiter:
    global _PACER
    if _PACER is None:
        from src.core.config import load_config

        _PACER = TokenRateLimiter(
            getattr(load_config(), "llm_tokens_per_minute", 12000)
        )
    return _PACER

_logger = get_logger()

_DEFAULT_TIMEOUT = 90  # seconds
#: Free tiers throttle aggressively and a full run makes ~20 calls, so a few
#: automatic retries turn a hard failure into a short pause.
_MAX_RETRIES = 3
_MAX_RETRY_WAIT = 30  # seconds — never stall a run longer than this


class OpenAICompatibleClient(LLMClient):
    """Base for providers speaking the OpenAI chat-completions protocol."""

    #: Default API base URL (no trailing slash); overridden per provider.
    default_base_url: str = ""

    @property
    def model(self) -> str:
        """Model id, normalised for the OpenAI-compatible wire format.

        Gemini's ``/models`` endpoint returns ids as ``models/gemini-2.5-flash``
        while its chat endpoint expects ``gemini-2.5-flash``. Accepting either
        means pasting straight from the model list just works.
        """
        name = super().model
        return name[len("models/"):] if name.startswith("models/") else name

    @property
    def base_url(self) -> str:
        url = (self.settings.base_url or self.default_base_url).strip().rstrip("/")
        return self._normalise_base_url(url)

    @staticmethod
    def _normalise_base_url(url: str) -> str:
        """Repair base URLs that are *almost* right.

        Google's Gemini exposes two different APIs under the same host:
        ``/v1beta`` is the native one (``x-goog-api-key`` header) and
        ``/v1beta/openai`` is the OpenAI-compatible one (``Bearer`` token).
        Pointing an OpenAI-style client at the former fails with a misleading
        ``401 Expected OAuth 2 access token``, so append the missing segment
        rather than let the user hit that.
        """
        if not url:
            return url
        if "generativelanguage.googleapis.com" in url and not url.endswith("/openai"):
            return url + "/openai"
        return url

    # --- request/response (pure, testable) --------------------------------
    def _build_payload(
        self, messages: list[LLMMessage], max_tokens: int | None = None
    ) -> dict:
        return {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": float(self.settings.temperature),
            "max_tokens": self.effective_max_tokens(max_tokens),
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
        """POST with automatic back-off on rate limits.

        A full analysis issues ~20 calls, which reliably trips the per-minute
        limits of every free tier. Retrying with the provider's own suggested
        delay turns a hard failure into a short pause.
        """
        import requests

        endpoint = f"{self.base_url}/chat/completions"
        last_detail = ""

        for attempt in range(_MAX_RETRIES + 1):
            # Cheap check before spending another call: a user who cancelled
            # during the previous back-off should not trigger a fresh request.
            cancellation.raise_if_cancelled()
            try:
                resp = requests.post(
                    endpoint, json=payload, headers=self._headers(),
                    timeout=_DEFAULT_TIMEOUT,
                )
            except requests.RequestException as exc:
                raise LLMProviderError(f"Network error calling {endpoint}: {exc}") from exc

            if resp.status_code < 400:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise LLMResponseError("Provider returned non-JSON response.") from exc

            last_detail = self._error_detail(resp)
            retryable = resp.status_code == 429 or resp.status_code >= 500

            # A zero quota is a configuration problem, not congestion — no
            # amount of waiting will fix it, so fail fast with clear guidance.
            body_text = resp.text or ""
            # Billing exhaustion looks like a rate limit but never clears on
            # its own — retrying just wastes the user's time.
            if resp.status_code == 429 and any(
                s in body_text.lower() for s in
                ("credits are depleted", "insufficient_quota", "billing details",
                 "exceeded your current quota, please check your plan")
            ) and "limit: 0" not in body_text:
                raise LLMProviderError(
                    f"{self.settings.provider}: this account is out of credit — "
                    "the API key works but has no remaining balance. Top up "
                    "billing for the project, or switch provider (Groq has a "
                    f"working free tier). Provider said: {last_detail}"
                )

            if resp.status_code == 429 and "limit: 0" in body_text:
                raise LLMProviderError(
                    f"'{self.model}' has NO quota on this API key (limit: 0), so "
                    "even the first request is rejected — quota is granted "
                    "per-model, so the key itself may be fine.\n"
                    + self._suggest_models()
                )

            if not retryable or attempt == _MAX_RETRIES:
                raise LLMProviderError(
                    f"{self.settings.provider} API error {resp.status_code}: {last_detail}"
                )

            delay = self._retry_after_seconds(resp, body_text) or (2 ** attempt)
            delay = min(delay + 0.5, _MAX_RETRY_WAIT)
            _logger.info(
                "%s returned %s; retrying in %.1fs (attempt %d/%d)",
                self.settings.provider, resp.status_code, delay,
                attempt + 1, _MAX_RETRIES,
            )
            # Cancellable: a plain sleep here is what made Cancel take minutes
            # to take effect (3 retries x 30s, on every batch).
            cancellation.sleep(delay)

        raise LLMProviderError(f"{self.settings.provider}: {last_detail}")

    #: Model ids that cannot serve a chat-completion request, or that are
    #: research/preview endpoints unsuitable for bulk SQL generation.
    _NOT_CHAT = (
        "embed", "aqa", "imagen", "veo", "tts", "-image", "computer-use",
        "deep-research", "antigravity", "learnlm", "-vision",
    )

    @staticmethod
    def _rank_model(name: str) -> tuple:
        """Sort key preferring stable, general-purpose chat models."""
        n = name.lower()
        preview = any(t in n for t in ("preview", "exp", "experimental"))
        # Prefer newest major line, then flash (cheap/fast) over pro.
        version = 0
        for v in ("3.0", "2.5", "2.0", "1.5"):
            if v in n:
                version = -float(v)
                break
        return (preview, "lite" in n, version, "pro" in n, len(n))

    def _suggest_models(self) -> str:
        """Name models this key can actually use, so the fix is one click away.

        A ``limit: 0`` response means the *model* has no allocation, not that
        the key is bad — naming a concrete alternative turns a dead end into a
        single edit.
        """
        try:
            available = self.list_models()
        except Exception:  # noqa: BLE001 - diagnostics must never mask the error
            available = []

        if not available:
            return (
                "Click 'Fetch models' to see what this key supports, pick one, "
                "and try again — or switch to another provider (Groq's free tier "
                "works well here)."
            )

        def bare(name: str) -> str:
            n = name.lower()
            return n[len("models/"):] if n.startswith("models/") else n

        # Compare on the bare id so the failing model is excluded whether the
        # list returns it prefixed or not.
        current = bare(self.model)
        chat = [
            m for m in available
            if not any(skip in m.lower() for skip in self._NOT_CHAT)
            and bare(m) != current
        ]
        chat.sort(key=self._rank_model)
        if not chat:
            return "No alternative chat models are available on this key."

        best = chat[0]
        others = ", ".join(chat[1:6])
        return (
            f"Try '{best}' instead — set it in the Model field and click Test."
            + (f"\nOther options: {others}" if others else "")
        )

    @staticmethod
    def _error_detail(resp) -> str:
        """Extract the human-readable message from a provider error body.

        Providers nest the useful sentence at different depths (and Gemini
        wraps it in a list), so dumping the raw JSON at the user is unhelpful.
        """
        try:
            body = resp.json()
        except ValueError:
            return (resp.text or "")[:300]

        # Gemini returns [{"error": {...}}]; OpenAI-style returns {"error": {...}}
        if isinstance(body, list) and body:
            body = body[0]
        if isinstance(body, dict):
            err = body.get("error", body)
            if isinstance(err, dict):
                message = str(err.get("message", "")).strip()
                if message:
                    # Keep the first sentence/line — the rest is quota tables
                    # and doc links that overwhelm the UI.
                    return message.split("\n")[0].strip()
                return str(err)[:300]
            return str(err)[:300]
        return str(body)[:300]

    @staticmethod
    def _retry_after_seconds(resp, body_text: str) -> float | None:
        """Seconds to wait before retrying, per the provider's own guidance."""
        header = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        # Gemini embeds {"retryDelay": "7s"} in the error payload.
        match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body_text or "")
        if match:
            return float(match.group(1))
        match = re.search(r"retry in (\d+(?:\.\d+)?)s", body_text or "", re.IGNORECASE)
        return float(match.group(1)) if match else None

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
    def chat(
        self, messages: list[LLMMessage], *, max_tokens: int | None = None
    ) -> LLMResponse:
        if not self.settings.api_key.strip():
            raise LLMConfigError(
                f"No API key configured for {self.settings.provider}. "
                "Add your key in the LLM configuration."
            )
        if not self.base_url:
            raise LLMConfigError(f"No base URL configured for {self.settings.provider}.")
        payload = self._build_payload(messages, max_tokens)
        _logger.info(
            "Calling %s model=%s max_tokens=%s",
            self.settings.provider, self.model, payload["max_tokens"],
        )
        # Providers bill prompt + max_tokens against the per-minute cap, and
        # charge rejected requests too — so wait for room rather than burst.
        _pacer().acquire(self._estimate_tokens(payload))
        response = self._parse_response(self._post(payload))
        usage.record(response.model or self.model, response.usage)
        return response

    @staticmethod
    def _estimate_tokens(payload: dict) -> int:
        """Prompt + reservation, the same sum the provider meters. ~4 chars/token."""
        chars = sum(len(m.get("content") or "") for m in payload.get("messages", []))
        return chars // 4 + int(payload.get("max_tokens") or 0)

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
        max_tokens: int | None = None,
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
            "max_tokens": self.effective_max_tokens(max_tokens),
        }
        _logger.info(
            "Calling %s vision model=%s with %d image(s)",
            self.settings.provider, self.model, len(images),
        )
        return self._parse_response(self._post(payload))
