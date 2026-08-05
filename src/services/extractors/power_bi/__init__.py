"""Power BI metadata extraction.

Supports the common Power BI artifacts:

* **PBIX / PBIT / ZIP** — OPC (ZIP) packages. The report layout
  (``Report/Layout``) yields pages, visuals, filters and bookmarks; the data
  model yields tables/columns/measures/relationships when it is present as text
  (``DataModelSchema`` / ``*.bim`` TOM JSON, or TMDL files). A binary
  ``DataModel`` part (native PBIX) cannot be parsed without export and is
  reported as a warning.
* **PBIP / PBIR (zipped project folder)** — TMDL semantic model +
  enhanced-report (PBIR) JSON pages/visuals.

Every sub-parser is defensive: failures become ``extraction_warnings`` rather
than exceptions, so partial metadata is always better than none.
"""

from src.services.extractors.power_bi.extractor import PowerBIExtractor

__all__ = ["PowerBIExtractor"]
