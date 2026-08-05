"""Abstract metadata-extractor contract."""

from __future__ import annotations

import abc
from pathlib import Path

from src.core.constants import BIPlatform
from src.domain.models import DashboardMetadata


class MetadataExtractor(abc.ABC):
    """Turns one dashboard file into a :class:`DashboardMetadata`."""

    #: Platform this extractor handles (set by subclasses).
    platform: BIPlatform

    @abc.abstractmethod
    def extract(self, file_path: Path) -> DashboardMetadata:
        """Parse *file_path* and return populated metadata.

        Implementations must be defensive: partial failures are recorded as
        ``extraction_warnings`` on the returned metadata rather than raised,
        so a dashboard that yields *some* metadata never blocks the pipeline.
        A :class:`MetadataExtractionError` should be raised only when nothing
        usable can be produced at all.
        """
