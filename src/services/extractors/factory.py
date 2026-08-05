"""Factory selecting a metadata extractor by BI platform."""

from __future__ import annotations

from src.core.constants import BIPlatform
from src.core.exceptions import UnsupportedPlatformError
from src.services.extractors.base import MetadataExtractor
from src.services.extractors.power_bi import PowerBIExtractor
from src.services.extractors.unsupported import PendingExtractor


def create_extractor(platform: BIPlatform) -> MetadataExtractor:
    if platform == BIPlatform.POWER_BI:
        return PowerBIExtractor()
    if platform == BIPlatform.TABLEAU:
        return PendingExtractor(BIPlatform.TABLEAU, "Module 5b")
    if platform == BIPlatform.QLIK:
        return PendingExtractor(BIPlatform.QLIK, "Module 5c")
    if platform == BIPlatform.MICROSTRATEGY:
        return PendingExtractor(BIPlatform.MICROSTRATEGY, "Module 5d")
    raise UnsupportedPlatformError(f"No extractor registered for {platform}")
