"""LLM reasoning service (Module 8).

Feeds the deterministic :class:`AnalysisContext` to the configured LLM and turns
the model's response into a persisted :class:`AIReasoning` (executive summary,
root-cause analysis, recommendations). This is the ONLY place the product calls
an LLM, and it happens strictly AFTER the deterministic context exists.
"""

from __future__ import annotations

import json
import re

from src.core.exceptions import LLMError, LLMResponseError
from src.core.logger import get_logger
from src.core.constants import LLMProvider
from src.domain.models import AIReasoning, AnalysisContext, LLMSettings, Project
from src.services.llm import create_client
from src.services.llm.base import LLMClient
from src.services.llm.prompt_builder import SYSTEM_PROMPT, build_user_prompt
from src.storage.project_repository import ProjectRepository

_logger = get_logger()

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


class LLMService:
    """Generates and persists AI reasoning over an AnalysisContext."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    def load_settings(self, project: Project) -> LLMSettings:
        """Project settings if configured, otherwise the machine-level default.

        The API creates a project per run, so requiring per-project LLM setup
        would mean re-entering the key every time. Global defaults are
        inherited; an explicit per-project override still wins.
        """
        saved = self._repo.load_llm_settings(project)
        if saved and saved.is_configured:
            return saved
        return self.load_global_settings()

    # --- machine-level defaults ------------------------------------------
    @staticmethod
    def load_global_settings() -> LLMSettings:
        from src.core.config import load_config

        config = load_config()
        provider = LLMProvider.from_value(config.default_llm_provider)
        api_key = (config.default_api_keys or {}).get(provider.value, "")
        return LLMSettings(
            provider=provider,
            api_key=api_key,
            model=config.default_llm_model,
            base_url=config.default_llm_base_url,
            temperature=config.default_llm_temperature,
            max_tokens=config.default_llm_max_tokens,
            is_configured=bool(api_key.strip()),
        )

    @staticmethod
    def save_global_settings(settings: LLMSettings) -> LLMSettings:
        from src.core.config import load_config, save_config

        config = load_config()
        config.default_llm_provider = settings.provider.value
        config.default_llm_model = settings.model.strip()
        config.default_llm_base_url = settings.base_url.strip()
        config.default_llm_temperature = float(settings.temperature)
        config.default_llm_max_tokens = int(settings.max_tokens)

        keys = dict(config.default_api_keys or {})
        if settings.api_key.strip():
            keys[settings.provider.value] = settings.api_key.strip()
        else:
            keys.pop(settings.provider.value, None)
        config.default_api_keys = keys

        save_config(config)
        settings.is_configured = bool(settings.api_key.strip())
        _logger.info("Saved global LLM settings (provider=%s)", settings.provider)
        return settings

    def save_settings(self, project: Project, settings: LLMSettings) -> LLMSettings:
        settings.is_configured = bool(settings.api_key.strip())
        self._repo.save_llm_settings(project, settings)
        return settings

    def load_reasoning(self, project: Project) -> AIReasoning | None:
        return self._repo.load_ai_reasoning(project)

    def list_models(self, settings: LLMSettings) -> list[str]:
        """Return model ids available for the configured provider/key."""
        client = create_client(settings)
        lister = getattr(client, "list_models", None)
        if not callable(lister):
            return []
        return lister()

    # --- main use case ----------------------------------------------------
    def generate(
        self,
        project: Project,
        context: AnalysisContext,
        settings: LLMSettings | None = None,
        *,
        client: LLMClient | None = None,
    ) -> AIReasoning:
        """Call the LLM to reason over *context* and persist the result.

        ``client`` may be injected for testing; otherwise it is built from
        ``settings`` (or the project's saved settings).
        """
        settings = settings or self.load_settings(project)
        client = client or create_client(settings)

        # A summary + RCA + a few recommendations needs ~1.2k tokens, not the
        # user's full ceiling — unused reservation still bills against quota.
        response = client.complete(
            SYSTEM_PROMPT, build_user_prompt(context), max_tokens=1200
        )
        parsed = self._parse(response.content)

        reasoning = AIReasoning(
            provider=settings.provider,
            model=response.model or client.model,
            executive_summary=parsed["executive_summary"],
            root_cause_analysis=parsed["root_cause_analysis"],
            recommendations=parsed["recommendations"],
            raw_response=response.content,
        )
        self._repo.save_ai_reasoning(project, reasoning)
        _logger.info("AI reasoning generated for %s via %s", project.id, settings.provider)
        return reasoning

    # --- response parsing -------------------------------------------------
    @staticmethod
    def _extract_json(text: str) -> dict | None:
        text = (text or "").strip()
        if not text:
            return None
        # 1) fenced code block
        m = _JSON_FENCE.search(text)
        candidates = [m.group(1)] if m else []
        # 2) first {...} span
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidates.append(text[start : end + 1])
        candidates.append(text)
        for cand in candidates:
            try:
                data = json.loads(cand)
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, TypeError):
                continue
        return None

    def _parse(self, content: str) -> dict:
        data = self._extract_json(content)
        if data is None:
            # Graceful fallback: keep the raw text as the summary rather than fail.
            if not (content or "").strip():
                raise LLMResponseError("The LLM returned an empty response.")
            return {
                "executive_summary": content.strip(),
                "root_cause_analysis": "",
                "recommendations": [],
            }
        recs = data.get("recommendations", [])
        if isinstance(recs, str):
            recs = [r.strip("-• ").strip() for r in recs.splitlines() if r.strip()]
        elif isinstance(recs, list):
            recs = [str(r).strip() for r in recs if str(r).strip()]
        else:
            recs = []
        return {
            "executive_summary": str(data.get("executive_summary", "")).strip(),
            "root_cause_analysis": str(data.get("root_cause_analysis", "")).strip(),
            "recommendations": recs,
        }
