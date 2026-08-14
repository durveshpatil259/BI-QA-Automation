"""Metadata extractors.

Each extractor turns a platform-specific dashboard file into the neutral
:class:`~src.domain.models.DashboardMetadata` graph. This is pure, deterministic
Python work — the LLM never parses dashboard files.

Mirrors the datasource-connector design: an abstract contract
(:class:`MetadataExtractor`), concrete implementations per platform, and a
factory that selects one by :class:`~src.core.constants.BIPlatform`.
"""

from src.services.extractors.base import MetadataExtractor
from src.services.extractors.factory import (create_extractor,
                                            create_extractor_for_file)
from src.services.extractors.file_detector import detect_platform

__all__ = ["MetadataExtractor", "create_extractor",
           "create_extractor_for_file", "detect_platform"]
