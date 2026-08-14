"""Tableau routing — recognised, explicitly not parsed.

The adapter seam exists so a Tableau parser can be added without touching the
QA engine: it would produce a :class:`~src.domain.bi_report.BIReport` exactly
as the Power BI path does, and every downstream rule would work unchanged.

What must not happen is a Tableau file being accepted quietly. An empty model
looks to the pipeline like a dashboard with no measures, and the run then
"succeeds" with nothing validated — a green report that proves nothing is worse
than a clear refusal. So this raises, naming the format and saying exactly what
is missing.
"""

from __future__ import annotations

from pathlib import Path

from src.core.constants import BIPlatform
from src.core.exceptions import UnsupportedPlatformError
from src.domain.models import DashboardMetadata
from src.services.extractors.base import MetadataExtractor

__all__ = ["TableauExtractor"]

#: What each format would need before it could be read.
_FORMAT_NOTES = {
    ".twb": "an XML workbook — needs a worksheet/datasource parser",
    ".twbx": "a packaged workbook (zip) — needs unpacking plus the .twb parser",
    ".tds": "a datasource definition only — carries no visuals to validate",
    ".tdsx": "a packaged datasource — carries no visuals to validate",
    ".hyper": "an extract database — needs the Tableau Hyper API",
    ".tde": "a legacy extract — needs the Tableau SDK",
}


class TableauExtractor(MetadataExtractor):
    """Recognises Tableau files and refuses them with a specific reason."""

    platform = BIPlatform.TABLEAU

    def extract(self, file_path: Path) -> DashboardMetadata:
        path = Path(file_path)
        note = _FORMAT_NOTES.get(path.suffix.casefold(), "an unrecognised Tableau format")
        raise UnsupportedPlatformError(
            f"Tableau format detected ({path.suffix or 'no extension'}) but the "
            f"parser is not available: {note}. Nothing was analysed — a Tableau "
            "file cannot be validated as if it were Power BI. Upload a Power BI "
            "file (.pbix, .pbit, .pbip), which is fully supported."
        )
