"""Placeholder extractors for platforms not yet implemented.

Tableau, Qlik and MicroStrategy extraction ships after Power BI. Until then a
:class:`PendingExtractor` raises a clear, actionable error via the factory so
the rest of the app (and the pipeline) behaves predictably instead of failing
in an obscure way.
"""

from __future__ import annotations

from pathlib import Path

from src.core.constants import BIPlatform
from src.core.exceptions import UnsupportedPlatformError
from src.domain.models import DashboardMetadata
from src.services.extractors.base import MetadataExtractor


class PendingExtractor(MetadataExtractor):
    """Extractor whose platform support is planned for a later build step."""

    def __init__(self, platform: BIPlatform, build_step: str):
        self.platform = platform
        self._build_step = build_step

    def extract(self, file_path: Path) -> DashboardMetadata:  # noqa: ARG002
        raise UnsupportedPlatformError(
            f"Metadata extraction for {self.platform} is not implemented yet "
            f"(planned: {self._build_step}). Power BI is fully supported today."
        )
