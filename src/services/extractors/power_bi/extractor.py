"""Power BI extractor orchestrator.

Opens a Power BI package (ZIP-based PBIX/PBIT/zipped-PBIP), locates the best
available model source (TOM JSON or TMDL) and report layout (classic or PBIR),
and assembles a single :class:`DashboardMetadata`. All failures degrade to
warnings; only a completely unreadable file raises.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from src.core.constants import BIPlatform
from src.core.exceptions import MetadataExtractionError
from src.core.logger import get_logger
from src.domain.models import DashboardMetadata
from src.services.extractors.base import MetadataExtractor
from src.services.extractors.power_bi import model_parser, report_parser, tmdl_parser
from src.services.extractors.power_bi.pbix_io import PbiPackage, parse_json_bytes

_logger = get_logger()


class PowerBIExtractor(MetadataExtractor):
    platform = BIPlatform.POWER_BI

    def extract(self, file_path: Path) -> DashboardMetadata:
        file_path = Path(file_path)
        metadata = DashboardMetadata(
            platform=BIPlatform.POWER_BI,
            source_file=file_path.name,
            model_name=file_path.stem,
        )

        if not zipfile.is_zipfile(file_path):
            # A bare .pbip pointer or loose file: no package to read.
            raise MetadataExtractionError(
                f"'{file_path.name}' is not a Power BI package. Upload a .pbix/.pbit "
                "file, or zip the PBIP/PBIR project folder and upload the .zip."
            )

        package_names: list[str] = []
        try:
            with PbiPackage.open(file_path) as pkg:
                package_names = list(pkg.names)
                self._extract_model(pkg, metadata)
                self._extract_report(pkg, metadata)
        except MetadataExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001 - corrupt zip etc.
            raise MetadataExtractionError(f"Could not read Power BI package: {exc}") from exc

        counts = metadata.summary_counts()
        if all(v == 0 for v in counts.values()):
            has_binary_model = any(
                "datamodel" in n.replace("\\", "/").lower()
                and "datamodelschema" not in n.replace("\\", "/").lower()
                for n in package_names
            )
            top = sorted({n.replace("\\", "/").split("/")[0] for n in package_names})[:12]
            if has_binary_model:
                reason = (
                    "This is a native .pbix whose data model is stored in a binary "
                    "format that cannot be read outside Power BI."
                )
            else:
                reason = "No readable data-model or report-layout parts were found."
            raise MetadataExtractionError(
                f"No metadata could be extracted from '{file_path.name}'. {reason}\n\n"
                "Fix: in Power BI Desktop use File → Export → Power BI template (.pbit) "
                "and upload the .pbit, or save in the PBIP project format and upload a "
                f".zip of the project folder.\n\nPackage contents: {', '.join(top) or '(empty)'}"
            )
        _logger.info("Power BI extraction complete: %s", counts)
        return metadata

    # --- model ------------------------------------------------------------
    def _extract_model(self, pkg: PbiPackage, metadata: DashboardMetadata) -> None:
        # 1) TOM JSON: DataModelSchema (PBIT) or *.bim
        schema_entry = next(iter(pkg.find_in_dir("datamodelschema")), None)
        bim_entries = pkg.find_endswith(".bim")
        if schema_entry or bim_entries:
            entry = schema_entry or bim_entries[0]
            try:
                root = pkg.read_json(entry)
                tables, rels = model_parser.parse_model(root)
                metadata.tables = tables
                metadata.relationships = rels
                if isinstance(root, dict) and root.get("name"):
                    metadata.model_name = root["name"]
                _logger.info("Parsed TOM model from %s", entry)
                return
            except Exception as exc:  # noqa: BLE001
                metadata.extraction_warnings.append(f"TOM model parse failed: {exc}")

        # 2) TMDL semantic model (PBIP)
        tmdl_tables = [n for n in pkg.find_endswith(".tmdl") if "/tables/" in n.lower()]
        if tmdl_tables:
            for entry in tmdl_tables:
                try:
                    table = tmdl_parser.parse_table_tmdl(pkg.read_text(entry))
                    if table and table.name:
                        metadata.tables.append(table)
                except Exception as exc:  # noqa: BLE001
                    metadata.extraction_warnings.append(
                        f"TMDL table parse failed ({entry}): {exc}"
                    )
            rel_entry = next(iter(pkg.find_endswith("relationships.tmdl")), None)
            if rel_entry:
                try:
                    metadata.relationships = tmdl_parser.parse_relationships_tmdl(
                        pkg.read_text(rel_entry)
                    )
                except Exception as exc:  # noqa: BLE001
                    metadata.extraction_warnings.append(f"TMDL relationships failed: {exc}")
            if metadata.tables:
                _logger.info("Parsed TMDL model (%d tables)", len(metadata.tables))
                return

        # 3) Binary data model — cannot parse without export.
        if pkg.find_in_dir("datamodel") and not pkg.find_in_dir("datamodelschema"):
            metadata.extraction_warnings.append(
                "Data model is stored in binary form (native PBIX). Model metadata "
                "(tables/measures/DAX) is unavailable; export to PBIP/PBIT to include it."
            )
        else:
            metadata.extraction_warnings.append("No data-model part found in the package.")

    # --- report -----------------------------------------------------------
    def _extract_report(self, pkg: PbiPackage, metadata: DashboardMetadata) -> None:
        # 1) Classic Report/Layout
        layout_entry = next(iter(pkg.find_endswith("report/layout")), None)
        if layout_entry is None:
            layout_entry = next(
                (n for n in pkg.names if n.replace("\\", "/").lower().endswith("/layout")),
                None,
            )
        if layout_entry:
            try:
                layout = parse_json_bytes(pkg.read_bytes(layout_entry))
                pages, bookmarks, filters = report_parser.parse_classic_layout(layout)
                metadata.pages = pages
                metadata.bookmarks = bookmarks
                metadata.report_level_filters = filters
                _logger.info("Parsed classic report layout (%d pages)", len(pages))
                return
            except Exception as exc:  # noqa: BLE001
                metadata.extraction_warnings.append(f"Report layout parse failed: {exc}")

        # 2) PBIR enhanced report
        if self._extract_pbir_report(pkg, metadata):
            return

        metadata.extraction_warnings.append("No report layout found in the package.")

    def _extract_pbir_report(self, pkg: PbiPackage, metadata: DashboardMetadata) -> bool:
        pages_entry = next(iter(pkg.find_endswith("pages/pages.json")), None)
        if pages_entry is None:
            return False
        try:
            base = pages_entry.replace("\\", "/").rsplit("/", 1)[0]  # .../pages
            pages_meta = parse_json_bytes(pkg.read_bytes(pages_entry))
            order = pages_meta.get("pageOrder") or []
            page_names = order or [
                n.replace("\\", "/").split("/")[-2]
                for n in pkg.find_endswith("page.json")
            ]
            for pname in page_names:
                page_json = next(
                    iter(pkg.find_in_dir(f"{base}/{pname}/page.json".lower())), None
                )
                if not page_json:
                    continue
                page = report_parser.parse_pbir_page(parse_json_bytes(pkg.read_bytes(page_json)))
                # Visuals under .../<page>/visuals/<id>/visual.json
                visual_prefix = f"{base}/{pname}/visuals/".lower()
                for vjson in pkg.find_endswith("visual.json"):
                    if visual_prefix in vjson.replace("\\", "/").lower():
                        try:
                            vis = report_parser.parse_pbir_visual(
                                parse_json_bytes(pkg.read_bytes(vjson))
                            )
                            vis.page = page.display_name
                            page.visuals.append(vis)
                        except Exception:  # noqa: BLE001
                            continue
                metadata.pages.append(page)
            if metadata.pages:
                _logger.info("Parsed PBIR report (%d pages)", len(metadata.pages))
                return True
        except Exception as exc:  # noqa: BLE001
            metadata.extraction_warnings.append(f"PBIR report parse failed: {exc}")
        return False
