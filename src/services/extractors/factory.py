"""Factory selecting a metadata extractor by BI platform."""

from __future__ import annotations

from pathlib import Path

from src.core.constants import BIPlatform
from src.core.exceptions import MetadataExtractionError, UnsupportedPlatformError
from src.core.logger import get_logger
from src.domain.models import DashboardMetadata
from src.services.extractors.base import MetadataExtractor
from src.services.extractors.power_bi import PowerBIExtractor
from src.services.extractors.power_bi.pbixray_extractor import (
    PbixRayExtractor,
    pbixray_available,
)
from src.services.extractors.tableau import TableauExtractor
from src.services.extractors.unsupported import PendingExtractor

_logger = get_logger()


class BestPowerBIExtractor(MetadataExtractor):
    """Chooses the best available Power BI extraction strategy per file.

    * ``.pbit`` / ``.pbip`` / ``.zip`` store the model as text (TOM JSON or
      TMDL) and additionally carry measure **format strings**, so the stdlib
      parser is preferred for them.
    * ``.pbix`` keeps the model in a binary VertiPaq blob, which only
      :class:`PbixRayExtractor` can read.

    Whichever runs first, the other is used as a fallback, so a file that
    surprises one parser still yields metadata from the other.
    """

    platform = BIPlatform.POWER_BI

    #: Formats whose model is plain text — the stdlib parser handles these best.
    _TEXT_MODEL_SUFFIXES = {".pbit", ".pbip", ".pbir", ".zip"}

    def extract(self, file_path: Path) -> DashboardMetadata:
        file_path = Path(file_path)
        native = PowerBIExtractor()

        strategies: list[tuple[str, MetadataExtractor]] = []
        if file_path.suffix.lower() in self._TEXT_MODEL_SUFFIXES:
            strategies.append(("native", native))
            if pbixray_available():
                strategies.append(("pbixray", PbixRayExtractor()))
        else:
            if pbixray_available():
                strategies.append(("pbixray", PbixRayExtractor()))
            strategies.append(("native", native))

        errors: list[str] = []
        best: DashboardMetadata | None = None
        best_name = ""

        for name, extractor in strategies:
            try:
                metadata = extractor.extract(file_path)
            except MetadataExtractionError as exc:
                errors.append(f"{name}: {exc}")
                continue

            if metadata.tables:
                _logger.info("Power BI extraction strategy: %s", name)
                return metadata

            # No model tables — another strategy may do better, but keep this
            # result: a report layout with visuals is still worth validating,
            # and discarding it would fail a file we could partly handle.
            errors.append(f"{name}: produced no model tables")
            if best is None or len(metadata.all_visuals) > len(best.all_visuals):
                best, best_name = metadata, name

        if best is not None:
            best.extraction_warnings.append(
                "No data model could be read from this file, so only the report "
                "layout (pages and visuals) was extracted. Measure, DAX and "
                "data validation are unavailable."
            )
            _logger.info(
                "Power BI extraction strategy: %s (layout only, no data model)",
                best_name,
            )
            return best

        raise MetadataExtractionError(
            f"Could not extract metadata from '{file_path.name}'. "
            + " | ".join(errors)
        )


def create_extractor_for_file(file_path, fallback: BIPlatform) -> MetadataExtractor:
    """Route by what the file *is*, falling back to the project's platform.

    A Tableau workbook uploaded to a Power BI project used to reach the Power
    BI parser, which reads no model from it; the pipeline then treated the
    result as a dashboard with nothing to validate and reported success.
    """
    from src.services.extractors.file_detector import detect_platform

    detected = detect_platform(file_path)
    if detected and detected != fallback:
        _logger.info("File %s looks like %s, not %s — routing to the %s adapter.",
                     Path(file_path).name, detected, fallback, detected)
    return create_extractor(detected or fallback)


def create_extractor(platform: BIPlatform) -> MetadataExtractor:
    if platform == BIPlatform.POWER_BI:
        return BestPowerBIExtractor()
    if platform == BIPlatform.TABLEAU:
        # A dedicated extractor rather than the generic placeholder, so the
        # refusal names the actual format and what it would need.
        return TableauExtractor()
    if platform == BIPlatform.QLIK:
        return PendingExtractor(BIPlatform.QLIK, "Module 5c")
    if platform == BIPlatform.MICROSTRATEGY:
        return PendingExtractor(BIPlatform.MICROSTRATEGY, "Module 5d")
    raise UnsupportedPlatformError(f"No extractor registered for {platform}")
