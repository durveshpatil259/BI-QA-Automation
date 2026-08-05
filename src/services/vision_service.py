"""AI Vision screenshot extraction (redesign V3).

Sends dashboard screenshots to a vision-capable model and turns the response
into a structured :class:`DashboardExtraction` (KPIs, charts, filters, text).
The AI only *reads* the image into JSON; Python then parses every KPI value into
a number with the deterministic value engine (V1). Values become the "dashboard
values" later compared against the datasource.
"""

from __future__ import annotations

import io
from pathlib import Path

from src.core.constants import SCREENSHOT_EXTENSIONS
from src.core.exceptions import LLMResponseError, ValidationError
from src.core.logger import get_logger
from src.domain.models import (
    DashboardExtraction,
    DashboardKPI,
    DetectedVisual,
    LLMSettings,
    Project,
)
from src.services.llm import create_client
from src.services.llm.base import LLMClient
from src.services.llm.json_utils import extract_json
from src.services.llm.prompt_builder import VISION_SYSTEM_PROMPT, VISION_USER_PROMPT
from src.services.validation import parse_value
from src.storage import file_manager as fm
from src.storage.project_repository import ProjectRepository

_logger = get_logger()

_MAX_IMAGES = 6
_MAX_WIDTH = 1600  # downscale wide screenshots to control token cost


class VisionService:
    """Extracts a DashboardExtraction from screenshots via an AI vision model."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    def has_screenshots(self, project: Project) -> bool:
        paths = self._repo.paths_for(project)
        return bool(fm.list_dir(paths.screenshots_dir, SCREENSHOT_EXTENSIONS))

    def load(self, project: Project) -> DashboardExtraction | None:
        return self._repo.load_dashboard_extraction(project)

    # --- extraction -------------------------------------------------------
    def extract(
        self,
        project: Project,
        settings: LLMSettings | None = None,
        *,
        client: LLMClient | None = None,
    ) -> DashboardExtraction:
        settings = settings or (self._repo.load_llm_settings(project) or LLMSettings())
        client = client or create_client(settings)

        images, formats = self._load_images(project)
        if not images:
            raise ValidationError("No screenshots to analyse. Upload some first.")

        response = client.vision_complete(
            VISION_SYSTEM_PROMPT, VISION_USER_PROMPT, images, image_formats=formats
        )
        extraction = self._parse(response.content)
        extraction.source = "screenshot"
        extraction.provider = settings.provider
        extraction.model = response.model or client.model
        extraction.raw_response = response.content

        self._repo.save_dashboard_extraction(project, extraction)
        _logger.info(
            "Vision extraction for %s: %d KPI(s), %d visual(s)",
            project.id, len(extraction.kpis), len(extraction.visuals),
        )
        return extraction

    # --- image loading / downscaling -------------------------------------
    def _load_images(self, project: Project) -> tuple[list[bytes], list[str]]:
        paths = self._repo.paths_for(project)
        files = fm.list_dir(paths.screenshots_dir, SCREENSHOT_EXTENSIONS)[:_MAX_IMAGES]
        images: list[bytes] = []
        formats: list[str] = []
        for f in files:
            data, fmt = self._downscale(f)
            images.append(data)
            formats.append(fmt)
        return images, formats

    @staticmethod
    def _downscale(path: Path) -> tuple[bytes, str]:
        """Return image bytes (downscaled if very wide) and its format."""
        raw = path.read_bytes()
        try:
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as img:
                if img.width > _MAX_WIDTH:
                    ratio = _MAX_WIDTH / img.width
                    resized = img.resize((_MAX_WIDTH, int(img.height * ratio)))
                    buf = io.BytesIO()
                    fmt = "PNG" if (img.format or "PNG").upper() == "PNG" else "JPEG"
                    if fmt == "JPEG" and resized.mode in ("RGBA", "P"):
                        resized = resized.convert("RGB")
                    resized.save(buf, format=fmt)
                    return buf.getvalue(), fmt.lower()
                return raw, (img.format or "png").lower()
        except Exception:  # noqa: BLE001 - fall back to raw bytes
            return raw, path.suffix.lstrip(".").lower() or "png"

    # --- response parsing -------------------------------------------------
    def _parse(self, content: str) -> DashboardExtraction:
        data = extract_json(content)
        if not isinstance(data, dict):
            raise LLMResponseError(
                "The vision model did not return the expected JSON object. "
                "Ensure the selected model supports image input."
            )
        extraction = DashboardExtraction()

        for k in data.get("kpis", []) or []:
            if not isinstance(k, dict):
                continue
            name = str(k.get("name", "")).strip()
            raw = str(k.get("value", k.get("kpi_value", ""))).strip()
            if not name and not raw:
                continue
            numeric, unit = parse_value(raw)
            extraction.kpis.append(DashboardKPI(
                name=name, raw_value=raw, numeric_value=numeric,
                unit=unit, source="screenshot",
            ))

        for c in data.get("charts", data.get("visuals", [])) or []:
            if not isinstance(c, dict):
                continue
            fields = c.get("fields", []) or []
            extraction.visuals.append(DetectedVisual(
                visual_type=str(c.get("visual_type", c.get("type", ""))).strip(),
                title=str(c.get("title", c.get("chart_title", ""))).strip(),
                fields=[str(f) for f in fields if str(f).strip()],
                text=str(c.get("text", "")).strip(),
                source="screenshot",
            ))

        filters = data.get("filters", []) or []
        extraction.filters = [str(f).strip() for f in filters if str(f).strip()]
        extraction.visible_text = str(data.get("visible_text", "")).strip()
        return extraction
