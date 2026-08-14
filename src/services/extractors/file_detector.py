"""Decide which BI platform an uploaded file belongs to.

The platform used to come from the project record, which meant a Tableau
workbook uploaded to a Power BI project was handed to the Power BI parser. That
either fails obscurely or — worse — yields an empty model that the rest of the
pipeline treats as a dashboard with no measures, and validation silently
proceeds against nothing.

Detection is by extension first and by content second, because ``.zip`` is
genuinely ambiguous: a Power BI project export and a Tableau packaged workbook
are both zip archives.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from src.core.constants import BIPlatform
from src.core.logger import get_logger

_logger = get_logger()

__all__ = ["detect_platform", "describe_support"]

_POWER_BI_SUFFIXES = {".pbix", ".pbit", ".pbip", ".pbir"}
#: Tableau's own formats. ``.twbx``/``.tdsx`` are zip archives; ``.twb``/``.tds``
#: are XML; ``.hyper`` is an extract database.
_TABLEAU_SUFFIXES = {".twb", ".twbx", ".tds", ".tdsx", ".hyper", ".tde"}

#: Entries that identify what a zip actually contains.
_POWER_BI_MARKERS = ("semanticmodel", "report/report.json", "datamodelschema",
                     "definition.pbir", ".report/", "layout")
_TABLEAU_MARKERS = (".twb", ".tds", ".hyper")


def _zip_platform(path: Path) -> BIPlatform | None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [n.casefold() for n in archive.namelist()[:400]]
    except (OSError, zipfile.BadZipFile):
        return None
    joined = "\n".join(names)
    if any(marker in joined for marker in _POWER_BI_MARKERS):
        return BIPlatform.POWER_BI
    if any(name.endswith(marker) for marker in _TABLEAU_MARKERS for name in names):
        return BIPlatform.TABLEAU
    return None


def detect_platform(file_path) -> BIPlatform | None:
    """The platform this file belongs to, or None when it cannot be told.

    None means "do not guess" — the caller keeps whatever the project already
    says rather than routing the file to a parser that will misread it.
    """
    path = Path(file_path)
    suffix = path.suffix.casefold()

    if suffix in _POWER_BI_SUFFIXES:
        return BIPlatform.POWER_BI
    if suffix in _TABLEAU_SUFFIXES:
        return BIPlatform.TABLEAU
    if suffix == ".zip":
        found = _zip_platform(path)
        if found:
            _logger.info("Detected %s from the contents of %s", found, path.name)
        return found
    return None


def describe_support(platform: BIPlatform) -> str:
    """What a user can expect from this platform today. Empty when fully supported."""
    if platform == BIPlatform.POWER_BI:
        return ""
    return (
        f"{platform} files are recognised, but no {platform} parser is "
        "installed, so no metadata can be read from them. Power BI (.pbix, "
        ".pbit, .pbip) is fully supported today."
    )
