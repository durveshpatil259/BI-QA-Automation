"""Power BI extractor backed by :mod:`pbixray`.

A native ``.pbix`` stores its data model as a compressed binary VertiPaq blob,
which the pure-stdlib parser cannot read — that is why native files previously
yielded 0 tables and 0 measures. ``pbixray`` decompresses that blob in pure
Python, so the model (tables, columns, measures, relationships) becomes
available with no .NET, no ``pbi-tools`` and no Power BI Desktop.

``pbixray`` covers only the **data model**; the report layout (pages, visuals,
bookmarks) still comes from the ZIP's ``Report/Layout`` part. This extractor
merges both into one :class:`DashboardMetadata`.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from src.core.constants import BIPlatform
from src.core.exceptions import MetadataExtractionError
from src.core.logger import get_logger
from src.domain.models import (
    Column,
    DashboardMetadata,
    Measure,
    Relationship,
    Table,
)
from src.services.extractors.base import MetadataExtractor
from src.services.extractors.power_bi.extractor import PowerBIExtractor

_logger = get_logger()

# TOM cross-filter codes -> the vocabulary used across this app.
_CROSSFILTER = {1: "single", 2: "both", 0: "automatic"}


def pbixray_available() -> bool:
    try:
        import pbixray  # noqa: F401
        return True
    except ImportError:
        return False


class PbixRayExtractor(MetadataExtractor):
    """Reads the binary data model with pbixray; layout with the ZIP parser."""

    platform = BIPlatform.POWER_BI

    def extract(self, file_path: Path) -> DashboardMetadata:
        file_path = Path(file_path)
        try:
            from pbixray import PBIXRay
        except ImportError as exc:  # pragma: no cover - guarded by factory
            raise MetadataExtractionError(
                "pbixray is not installed. Run: pip install pbixray"
            ) from exc

        metadata = DashboardMetadata(
            platform=BIPlatform.POWER_BI,
            source_file=file_path.name,
            model_name=file_path.stem,
        )

        try:
            model = PBIXRay(str(file_path))
        except Exception as exc:  # noqa: BLE001 - unreadable/encrypted package
            raise MetadataExtractionError(
                f"pbixray could not open '{file_path.name}': {exc}"
            ) from exc

        try:
            self._extract_model(model, metadata)
        except MetadataExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001
            metadata.extraction_warnings.append(f"Data-model parse failed: {exc}")
        finally:
            close = getattr(model, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

        self._merge_report_layout(file_path, metadata)

        counts = metadata.summary_counts()
        if all(v == 0 for v in counts.values()):
            raise MetadataExtractionError(
                f"No metadata could be extracted from '{file_path.name}'."
            )
        _logger.info("pbixray extraction complete: %s", counts)
        return metadata

    # --- data model -------------------------------------------------------
    def _extract_model(self, model, metadata: DashboardMetadata) -> None:
        schema = model.schema              # TableName, ColumnName, PandasDataType
        measures = model.dax_measures      # TableName, Name, Expression, DisplayFolder
        rels = model.relationships

        # Columns grouped per table.
        tables: dict[str, Table] = {}
        for name in list(model.tables):
            tables[name] = Table(name=str(name))

        for row in schema.itertuples(index=False):
            table = tables.setdefault(str(row.TableName), Table(name=str(row.TableName)))
            table.columns.append(Column(
                name=str(row.ColumnName),
                data_type=str(getattr(row, "PandasDataType", "") or ""),
            ))

        for row in measures.itertuples(index=False):
            table_name = str(row.TableName)
            table = tables.setdefault(table_name, Table(name=table_name))
            table.measures.append(Measure(
                name=str(row.Name),
                table=table_name,
                dax_expression=str(getattr(row, "Expression", "") or ""),
                display_folder=str(getattr(row, "DisplayFolder", "") or ""),
                description=str(getattr(row, "Description", "") or ""),
            ))

        # Calculated columns, when the model exposes them.
        try:
            for row in model.dax_columns.itertuples(index=False):
                table = tables.get(str(row.TableName))
                if table is None:
                    continue
                for col in table.columns:
                    if col.name == str(row.ColumnName):
                        col.is_calculated = True
                        col.dax_expression = str(getattr(row, "Expression", "") or "")
                        break
        except Exception:  # noqa: BLE001 - optional
            pass

        metadata.tables = list(tables.values())

        for row in rels.itertuples(index=False):
            cardinality = str(getattr(row, "Cardinality", "") or "")
            metadata.relationships.append(Relationship(
                from_table=str(row.FromTableName),
                from_column=str(row.FromColumnName),
                to_table=str(row.ToTableName),
                to_column=str(row.ToColumnName),
                cardinality=cardinality or "many-to-one",
                cross_filter_direction=_CROSSFILTER.get(
                    getattr(row, "CrossFilteringBehavior", None),
                    str(getattr(row, "CrossFilteringBehavior", "") or ""),
                ),
                is_active=bool(getattr(row, "IsActive", True)),
            ))

    # --- report layout ----------------------------------------------------
    def _merge_report_layout(self, file_path: Path, metadata: DashboardMetadata) -> None:
        """Pages/visuals/bookmarks still live in the ZIP's Report part."""
        if not zipfile.is_zipfile(file_path):
            return
        try:
            layout_only = PowerBIExtractor().extract(file_path)
        except MetadataExtractionError:
            metadata.extraction_warnings.append(
                "Report layout could not be parsed; model metadata only."
            )
            return
        metadata.pages = layout_only.pages
        metadata.bookmarks = layout_only.bookmarks
        metadata.report_level_filters = layout_only.report_level_filters
        # Keep only layout-related warnings. The stdlib parser always reports
        # "binary data model / unavailable" for a native .pbix, which is no
        # longer true once pbixray has read the model.
        stale = ("data model", "data-model", "datamodel")
        metadata.extraction_warnings.extend(
            w for w in layout_only.extraction_warnings
            if not any(s in w.lower() for s in stale)
        )
