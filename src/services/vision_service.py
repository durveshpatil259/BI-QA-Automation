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
    ChartDataPoint,
    DashboardExtraction,
    DashboardFilter,
    DashboardKPI,
    DashboardView,
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

        paths = self._repo.paths_for(project)
        files = fm.list_dir(paths.screenshots_dir, SCREENSHOT_EXTENSIONS)[:_MAX_IMAGES]
        if not files:
            raise ValidationError("No screenshots to analyse. Upload some first.")

        extraction = DashboardExtraction(source="screenshot", provider=settings.provider)
        raw_parts: list[str] = []

        # One request per screenshot: each image is a distinct filter scenario,
        # so its KPIs must stay bound to the slicer values shown in THAT image.
        for path in files:
            data, fmt = self._downscale(path)
            response = client.vision_complete(
                VISION_SYSTEM_PROMPT, VISION_USER_PROMPT, [data], image_formats=[fmt]
            )
            extraction.model = response.model or client.model
            raw_parts.append(f"--- {path.name} ---\n{response.content}")
            view = self._parse_view(response.content, path.name)
            extraction.views.append(view)

            # Flatten for back-compat displays.
            extraction.kpis.extend(view.kpis)
            extraction.visuals.extend(view.visuals)
            for f in view.filter_selections:
                extraction.filters.append(f.name)
                extraction.filter_selections.append(f)
            if view.visible_text and not extraction.visible_text:
                extraction.visible_text = view.visible_text

        extraction.raw_response = "\n\n".join(raw_parts)
        self._repo.save_dashboard_extraction(project, extraction)
        _logger.info(
            "Vision extraction for %s: %d view(s), %d KPI(s) total",
            project.id, len(extraction.views), len(extraction.kpis),
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
    def _parse_view(self, content: str, view_name: str) -> DashboardView:
        data = extract_json(content)
        if not isinstance(data, dict):
            raise LLMResponseError(
                "The vision model did not return the expected JSON object. "
                "Ensure the selected model supports image input."
            )
        extraction = DashboardView(name=view_name)

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
            values_visible = bool(c.get("values_visible", False))
            points: list[ChartDataPoint] = []
            for dp in c.get("data_points", []) or []:
                if not isinstance(dp, dict):
                    continue
                dim = str(dp.get("dimension", dp.get("category", ""))).strip()
                if not dim:
                    continue
                raw = str(dp.get("value", "")).strip()
                numeric, _ = parse_value(raw) if raw else (None, "")
                points.append(ChartDataPoint(dimension=dim, raw_value=raw, numeric_value=numeric))

            extraction.visuals.append(DetectedVisual(
                visual_type=str(c.get("visual_type", c.get("type", ""))).strip(),
                title=str(c.get("title", c.get("chart_title", ""))).strip(),
                fields=[str(f) for f in fields if str(f).strip()],
                text=str(c.get("text", "")).strip(),
                dimension_field=str(c.get("dimension_field", "")).strip(),
                measure_field=str(c.get("measure_field", "")).strip(),
                values_visible=values_visible,
                data_points=points,
                source="screenshot",
            ))

        # Filters may arrive as plain names (older prompt) or as
        # {"name", "selected"} objects — accept both.
        for f in data.get("filters", []) or []:
            if isinstance(f, dict):
                name = str(f.get("name", "")).strip()
                selected = str(f.get("selected", f.get("value", ""))).strip()
            else:
                name, selected = str(f).strip(), ""
            if not name:
                continue
            extraction.filter_selections.append(
                DashboardFilter(name=name, selected=selected)
            )
        extraction.visible_text = str(data.get("visible_text", "")).strip()
        return extraction
