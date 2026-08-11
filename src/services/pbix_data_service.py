"""Evaluate DAX measures against the data stored inside a PBIX.

``pbixray`` exposes not just the model definition but the **row data** of the
VertiPaq tables. That means the number a KPI card renders can be recomputed in
pandas directly from the file — no screenshot, no Power BI Desktop, no live
Analysis Services instance.

Scope, stated plainly: this is a *pragmatic* DAX subset, not a DAX engine.

Supported
    SUM, AVERAGE, MIN, MAX, COUNT, COUNTROWS, DISTINCTCOUNT over a column or
    table, plus measures derived from other measures via ``-``, ``+``, ``*``
    and ``DIVIDE(a, b)`` — resolved iteratively so chains chain correctly.

Not supported (reported as unevaluated, never guessed)
    CALCULATE with filter context, time intelligence (SAMEPERIODLASTYEAR, DATEADD),
    row-context iterators (SUMX/AVERAGEX), and anything else requiring a real
    evaluation engine.

Unsupported measures are simply absent from the result, so the validation
engine degrades to executability/consistency for them rather than comparing
against a fabricated number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.exceptions import MetadataExtractionError
from src.core.logger import get_logger
from src.domain.models import DashboardMetadata, Project
from src.storage import file_manager as fm
from src.storage.project_repository import ProjectRepository

_logger = get_logger()

# SUM(Table[Column]) / AVERAGE('My Table'[Col]) — table name may be quoted.
_AGG_COLUMN = re.compile(
    r"^(SUM|AVERAGE|AVG|MIN|MAX|COUNT|DISTINCTCOUNT)\s*\(\s*"
    r"'?([^'\[\]]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*\)$",
    re.IGNORECASE,
)
# COUNTROWS(Table)
_AGG_TABLE = re.compile(r"^(COUNTROWS)\s*\(\s*'?([^'()\[\]]+?)'?\s*\)$", re.IGNORECASE)

# Derived-from-other-measures shapes.
_DIVIDE = re.compile(
    r"^DIVIDE\s*\(\s*\[([^\]]+)\]\s*,\s*\[([^\]]+)\]\s*(?:,[^)]*)?\)$", re.IGNORECASE
)
_BINARY = re.compile(r"^\[([^\]]+)\]\s*([-+*])\s*\[([^\]]+)\]$")


@dataclass
class MeasureValue:
    name: str
    value: float
    method: str          # "aggregate" | "derived"
    expression: str = ""


@dataclass(frozen=True)
class Restriction:
    """One narrowing of a table: a slicer selection or an explicit row mask.

    Both forms reduce a table to a subset of rows, and both then propagate to
    related tables the same way, so they share a representation. ``predicate``
    takes that table's DataFrame and returns a boolean mask.
    """

    table: str
    predicate: object            # Callable[[DataFrame], Series[bool]]
    column: str = ""             # set for slicer filters; blank for row masks
    label: str = ""


class FilterContext:
    """The set of filters a measure is evaluated under.

    Power BI's ``CALCULATE`` does not just *add* filters — it replaces and
    removes them (``ALL``), and time intelligence swaps the date rows outright.
    A single ``(table, column, value)`` tuple cannot express any of that, so
    every such measure was previously unevaluable.

    Instances are immutable; the ``with_*``/``without_*`` methods return new
    contexts, which keeps a nested CALCULATE from corrupting its caller.
    """

    __slots__ = ("_restrictions",)

    def __init__(self, restrictions: tuple[Restriction, ...] = ()) -> None:
        self._restrictions = tuple(restrictions)

    # --- construction -----------------------------------------------------
    @classmethod
    def from_spec(cls, spec: tuple[str, str, str] | None) -> "FilterContext":
        """Build from the legacy ``(table, column, value)`` slicer tuple."""
        if not spec:
            return cls()
        table, column, value = spec
        return cls().with_column_filter(table, column, value)

    def with_column_filter(self, table: str, column: str, value) -> "FilterContext":
        def predicate(df, _column=column, _value=value):
            return df[_column].astype(str) == str(_value)

        return FilterContext(self._restrictions + (
            Restriction(table=table, predicate=predicate, column=column,
                        label=f"{table}[{column}]={value}"),
        ))

    def with_mask(self, table: str, predicate, label: str = "") -> "FilterContext":
        """Restrict *table* by an arbitrary row predicate (time intelligence)."""
        return FilterContext(self._restrictions + (
            Restriction(table=table, predicate=predicate, label=label or f"mask({table})"),
        ))

    def without_column(self, table: str, column: str) -> "FilterContext":
        """``ALL('Table'[Column])`` — drop any filter on that column."""
        return FilterContext(tuple(
            r for r in self._restrictions
            if not (r.table.casefold() == table.casefold()
                    and r.column.casefold() == column.casefold())
        ))

    def without_table(self, table: str) -> "FilterContext":
        """``ALL('Table')`` — drop every filter on that table."""
        return FilterContext(tuple(
            r for r in self._restrictions
            if r.table.casefold() != table.casefold()
        ))

    def remove_all(self) -> "FilterContext":
        """``ALL()`` — evaluate over the whole model."""
        return FilterContext()

    # --- inspection -------------------------------------------------------
    @property
    def restrictions(self) -> tuple[Restriction, ...]:
        return self._restrictions

    @property
    def key(self) -> tuple:
        """Stable cache key. ``id()`` would be reused after a context is freed."""
        return tuple(r.label for r in self._restrictions)

    def __bool__(self) -> bool:
        return bool(self._restrictions)

    def __repr__(self) -> str:
        return f"FilterContext({', '.join(r.label for r in self._restrictions) or 'no filters'})"


@dataclass
class FilterOption:
    """A slicer on the report and the distinct values it can take."""

    table: str
    column: str
    values: list[str]

    @property
    def field(self) -> str:
        return f"{self.table}[{self.column}]"


class PbixDataService:
    """Computes true measure values from the data inside a PBIX file."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository
        #: {(table, column): dax} for calculated columns of the model being
        #: evaluated. Set per run by :meth:`_evaluate_measures`.
        self._calc_columns: dict = {}

    # --- slicer discovery -------------------------------------------------
    def detect_filters(
        self,
        project: Project,
        metadata: DashboardMetadata | None = None,
        *,
        max_values: int = 8,
    ) -> list[FilterOption]:
        """Return the report's slicers together with their real distinct values.

        The values come from the PBIX data itself, so every condition a user
        could select on the dashboard becomes a testable scenario — without a
        single screenshot.
        """
        metadata = metadata or self._repo.load_metadata(project)
        if metadata is None:
            return []

        wanted: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for visual in metadata.all_visuals:
            if (visual.visual_type or "").casefold() != "slicer" or not visual.fields:
                continue
            table, _, column = visual.fields[0].rpartition(".")
            key = (table.casefold(), column.casefold())
            if table and column and key not in seen:
                seen.add(key)
                wanted.append((table, column))
        if not wanted:
            return []

        model = self._open(project)
        if model is None:
            return []
        try:
            cache: dict[str, object] = {}
            options: list[FilterOption] = []
            for table, column in wanted:
                df = self._table(model, table, cache)
                if df is None or column not in df.columns:
                    continue
                try:
                    distinct = sorted(
                        str(v) for v in df[column].dropna().unique()
                    )
                except Exception:  # noqa: BLE001 - unhashable/odd dtypes
                    continue
                # A slicer over thousands of customers is not a useful test
                # axis; only low-cardinality slicers expand into scenarios.
                if 1 < len(distinct) <= max_values:
                    options.append(FilterOption(table, column, distinct))
            return options
        finally:
            self._close(model)

    # --- entry point ------------------------------------------------------
    def evaluate(
        self,
        project: Project,
        metadata: DashboardMetadata | None = None,
        *,
        filter_spec: tuple[str, str, str] | None = None,
        context: "FilterContext | None" = None,
    ) -> dict[str, str]:
        """Return ``{measure_name: value}`` for every measure we can compute.

        ``filter_spec`` is ``(table, column, value)`` — the equivalent of
        selecting that value in a slicer. Measures are then evaluated over the
        filtered data, exactly as Power BI would render them.

        ``context`` is the richer form: it also expresses removed filters and
        explicit row masks. Passing one supersedes ``filter_spec``.
        """
        metadata = metadata or self._repo.load_metadata(project)
        if metadata is None or not metadata.all_measures:
            return {}

        context = context if context is not None else FilterContext.from_spec(filter_spec)

        model = self._open(project)
        if model is None:
            return {}
        try:
            values = self._evaluate_measures(model, metadata, context)
        finally:
            self._close(model)

        _logger.info(
            "Evaluated %d/%d measure(s) from PBIX data%s",
            len(values), len(metadata.all_measures),
            f" under {context}" if context else "",
        )
        return {name: self._format(v.value) for name, v in values.items()}

    # --- pbixray lifecycle ------------------------------------------------
    def _open(self, project: Project):
        path = self._pbix_path(project)
        if path is None:
            _logger.info("No .pbix available; skipping data-backed evaluation.")
            return None
        try:
            from pbixray import PBIXRay
        except ImportError:
            _logger.info("pbixray not installed; skipping data-backed evaluation.")
            return None
        try:
            return PBIXRay(str(path))
        except Exception as exc:  # noqa: BLE001
            raise MetadataExtractionError(f"Could not open '{path.name}': {exc}") from exc

    @staticmethod
    def _close(model) -> None:
        close = getattr(model, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    # --- evaluation -------------------------------------------------------
    # --- filter propagation ----------------------------------------------
    def _filtered_table(self, model, table_name: str, cache: dict, metadata, context):
        """Return *table_name* narrowed by every restriction in *context*.

        Mirrors how Power BI propagates a filter: if the restriction sits on
        this table, apply it directly; otherwise follow the relationship to the
        restricted table and keep only rows whose key survived there.

        Restrictions compose, so a slicer selection and a time-intelligence
        date mask narrow the same fact table together.
        """
        df = self._table(model, table_name, cache)
        if df is None or not context:
            return df

        for restriction in context.restrictions:
            df = self._apply_restriction(
                model, df, table_name, restriction, cache, metadata
            )
            if df is None or df.empty:
                return df
        return df

    def _apply_restriction(self, model, df, table_name, restriction, cache, metadata):
        """Narrow *df* by one restriction, propagating across relationships."""
        # The restriction is on this very table.
        if table_name.casefold() == restriction.table.casefold():
            try:
                return df[restriction.predicate(df)]
            except (KeyError, TypeError, ValueError):
                return df          # column absent — the filter cannot apply

        source = self._table(model, restriction.table, cache)
        if source is None:
            return df
        try:
            surviving = source[restriction.predicate(source)]
        except (KeyError, TypeError, ValueError):
            return df

        # Follow the relationship joining this table to the restricted one.
        for rel in (metadata.relationships if metadata else []):
            pairs = [
                (rel.from_table, rel.from_column, rel.to_table, rel.to_column),
                (rel.to_table, rel.to_column, rel.from_table, rel.from_column),
            ]
            for f_tbl, f_col, t_tbl, t_col in pairs:
                if (f_tbl.casefold() == table_name.casefold()
                        and t_tbl.casefold() == restriction.table.casefold()
                        and f_col in df.columns and t_col in surviving.columns):
                    return df[df[f_col].isin(set(surviving[t_col]))]

        # No path from this table to the restriction — it does not apply.
        return df

    def _evaluate_measures(
        self, model, metadata, context: "FilterContext | None" = None
    ) -> dict[str, MeasureValue]:
        measures = {m.name: m for m in metadata.all_measures if m.name}
        values: dict[str, MeasureValue] = {}
        table_cache: dict[str, object] = {}
        context = context if context is not None else FilterContext()
        # Filtered frames are cached per (context, table): a CALCULATE that
        # changes the context must not read the outer context's rows.
        filtered_cache: dict[tuple[int, str], object] = {}

        def table_in(ctx: "FilterContext", name: str):
            if not ctx:
                return self._table(model, name, table_cache)
            key = (ctx.key, name)
            if key not in filtered_cache:
                filtered_cache[key] = self._filtered_table(
                    model, name, table_cache, metadata, ctx
                )
            return filtered_cache[key]

        def get_table(name: str):
            return table_in(context, name)

        # Calculated columns are usually materialised in the VertiPaq store, so
        # they arrive as ordinary data. When a model stores only the formula,
        # derive the column rather than treating the measure as unevaluable.
        calc_columns = {
            (t.name.casefold(), c.name.casefold()): c.dax_expression
            for t in (metadata.tables if metadata else [])
            for c in t.columns
            if c.is_calculated and c.dax_expression
        }
        self._calc_columns = calc_columns

        lookup = {n.casefold(): n for n in measures}
        memo: dict[tuple, float | None] = {}
        #: Measures that needed CALCULATE / time intelligence to resolve. Their
        #: values come from an emulation of DAX semantics rather than a direct
        #: aggregation, so they are reported at lower confidence.
        emulated: set[str] = set()

        def eval_measure(name: str, ctx, stack: tuple) -> float | None:
            real = lookup.get(name.strip().casefold())
            if real is None or real.casefold() in stack:
                return None                      # unknown, or circular reference
            key = (ctx.key, real.casefold())
            if key in memo:
                return memo[key]
            expr = " ".join((measures[real].dax_expression or "").split())
            value = eval_expr(expr, ctx, stack + (real.casefold(),))
            memo[key] = value
            return value

        def eval_expr(expr: str, ctx, stack: tuple) -> float | None:
            expr = (expr or "").strip()
            if not expr:
                return None
            upper = expr.upper()

            # CALCULATE(<expression>, <modifier>, ...) — evaluate the inner
            # expression under a context the modifiers rewrite.
            if upper.startswith("CALCULATE(") and expr.endswith(")"):
                args = self._split_args(expr[len("CALCULATE("):-1])
                if args:
                    inner_ctx = ctx
                    for modifier in args[1:]:
                        inner_ctx = self._apply_modifier(
                            inner_ctx, modifier, model, metadata, table_cache, table_in
                        )
                        if inner_ctx is None:
                            return None          # modifier not understood
                    stack_name = stack[-1] if stack else ""
                    if stack_name:
                        emulated.add(stack_name)
                    return eval_expr(args[0], inner_ctx, stack)
                return None

            # TOTALYTD(<expression>, <dates>) — year-to-date of the last date
            # visible in the current context.
            if upper.startswith("TOTALYTD(") and expr.endswith(")"):
                args = self._split_args(expr[len("TOTALYTD("):-1])
                if len(args) >= 2:
                    ytd = self._ytd_context(ctx, args[1], model, metadata,
                                            table_cache, table_in)
                    if ytd is None:
                        return None
                    if stack:
                        emulated.add(stack[-1])
                    return eval_expr(args[0], ytd, stack)
                return None

            # Otherwise fall back to the arithmetic evaluator, resolving
            # measure references and aggregates under THIS context.
            def atom(text: str):
                # A CALCULATE nested inside DIVIDE (the "% of total" shape)
                # must go back through eval_expr, not be treated as a column
                # aggregate. Only these two recurse, so this cannot loop.
                head = text.strip().upper()
                if head.startswith(("CALCULATE(", "TOTALYTD(")):
                    return eval_expr(text, ctx, stack)
                return self._resolve_atom(
                    text, ctx, stack, eval_measure, model, table_cache, table_in
                )

            return self._eval_derived(expr, values={}, lookup={}, agg=atom)

        # Evaluate every measure under the requested context.
        for name, measure in measures.items():
            expr = " ".join((measure.dax_expression or "").split())
            if not expr:
                continue
            result = eval_measure(name, context, ())
            if result is None:
                continue
            method = ("emulated" if name.casefold() in emulated
                      else "aggregate" if self._eval_aggregate(
                          model, expr, table_cache, get_table,
                          calc_columns=calc_columns) is not None
                      else "derived")
            values[name] = MeasureValue(name, result, method, expr)
        return values

    def _eval_aggregate(self, model, expr: str, cache: dict, get_table=None,
                        calc_columns: dict | None = None) -> float | None:
        get_table = get_table or (lambda n: self._table(model, n, cache))
        match = _AGG_COLUMN.match(expr)
        if match:
            func, table_name, column = (g.strip() for g in match.groups())
            df = get_table(table_name)
            if df is not None and column not in df.columns:
                df = self._add_calculated_column(df, table_name, column, calc_columns or {})
            if df is None or column not in df.columns:
                return None
            if df.empty:
                # DAX returns BLANK, not 0, when the filter context selects no
                # rows. Reporting 0 as the expected value produced validations
                # like "dashboard $0 vs database $5.8M" that were pure noise.
                return None
            series = df[column]
            func = func.upper()
            try:
                if func == "SUM":
                    return float(series.sum())
                if func in ("AVERAGE", "AVG"):
                    return float(series.mean())
                if func == "MIN":
                    return float(series.min())
                if func == "MAX":
                    return float(series.max())
                if func == "COUNT":
                    return float(series.count())
                if func == "DISTINCTCOUNT":
                    return float(series.nunique())
            except (TypeError, ValueError):
                return None      # non-numeric column
            return None

        match = _AGG_TABLE.match(expr)
        if match:
            df = get_table(match.group(2).strip())
            return float(len(df)) if df is not None else None
        return None

    @staticmethod
    def _split_args(inner: str) -> list[str]:
        """Split a DAX argument list on top-level commas only."""
        args, depth, current = [], 0, []
        for char in inner:
            if char in "([":
                depth += 1
            elif char in ")]":
                depth -= 1
            if char == "," and depth == 0:
                args.append("".join(current).strip())
                current = []
                continue
            current.append(char)
        if current:
            args.append("".join(current).strip())
        return args

    def _add_calculated_column(self, df, table_name: str, column: str, calc_columns: dict):
        """Derive a calculated column that the stored data does not carry.

        Power BI normally materialises calculated columns into VertiPaq, so
        they arrive as ordinary data. When a model stores only the formula, a
        measure over that column would otherwise be silently unevaluable.
        Handles row-level arithmetic over sibling columns — the common shape,
        e.g. ``Sales[Profit] = Sales[Sales Amount] - Sales[Product Cost]``.
        """
        formula = calc_columns.get((table_name.casefold(), column.casefold()))
        if not formula:
            return df

        expression = " ".join(formula.split())
        tokens = re.findall(r"'?([^'\[\]()+\-*/]+)'?\s*\[\s*([^\]]+?)\s*\]", expression)
        if not tokens:
            return df

        # Only same-table references: a cross-table lookup needs RELATED().
        python_expr = expression
        for source_table, source_column in tokens:
            if source_table.strip().casefold() != table_name.casefold():
                return df
            if source_column not in df.columns:
                return df
            python_expr = python_expr.replace(
                f"{source_table}[{source_column}]", f"__c['{source_column}']"
            ).replace(
                f"'{source_table}'[{source_column}]", f"__c['{source_column}']"
            )

        if re.search(r"[A-Za-z_]{2,}\s*\(", python_expr):
            return df                      # a function call — out of scope
        try:
            derived = eval(python_expr, {"__builtins__": {}}, {"__c": df})  # noqa: S307
        except Exception:  # noqa: BLE001 - unparseable formula stays unevaluated
            return df

        out = df.copy()
        out[column] = derived
        _logger.info("Derived calculated column %s[%s]", table_name, column)
        return out

    # --- CALCULATE / time intelligence -----------------------------------
    @staticmethod
    def _parse_column_ref(text: str) -> tuple[str, str] | None:
        """``'Date'[Date]`` or ``Product[Category]`` -> (table, column)."""
        match = re.match(r"^\s*'?([^'\[\]]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*$", text or "")
        return (match.group(1).strip(), match.group(2).strip()) if match else None

    def _resolve_atom(self, text, ctx, stack, eval_measure, model, cache, table_in):
        """A measure reference or an aggregate, evaluated under *ctx*."""
        text = (text or "").strip()
        bare = text[1:-1] if text.startswith("[") and text.endswith("]") else text
        value = eval_measure(bare, ctx, stack)
        if value is not None:
            return value
        return self._eval_aggregate(
            model, text, cache, get_table=lambda n: table_in(ctx, n),
            calc_columns=self._calc_columns
        )

    def _apply_modifier(self, ctx, modifier, model, metadata, cache, table_in):
        """Rewrite a filter context per one CALCULATE modifier.

        Returns ``None`` for anything unrecognised, so an unsupported modifier
        makes the whole measure unevaluated rather than silently ignored — a
        wrong number is worse than no number.
        """
        text = (modifier or "").strip()
        upper = text.upper()

        if upper.startswith("ALL(") and text.endswith(")"):
            inner = text[len("ALL("):-1].strip()
            if not inner:
                return ctx.remove_all()
            column = self._parse_column_ref(inner)
            if column:
                return ctx.without_column(*column)
            return ctx.without_table(inner.strip("'"))

        if upper.startswith("SAMEPERIODLASTYEAR(") and text.endswith(")"):
            inner = text[len("SAMEPERIODLASTYEAR("):-1]
            return self._shift_year_context(ctx, inner, model, metadata, cache,
                                            table_in, years=1)

        if upper.startswith("FILTER(") and text.endswith(")"):
            return self._filter_modifier(ctx, text[len("FILTER("):-1],
                                         model, cache, table_in)

        # A boolean predicate: 'Product'[Category] = "Bikes"
        match = re.match(r"^(.+?)\s*=\s*\"?'?([^\"']*)'?\"?$", text)
        if match:
            column = self._parse_column_ref(match.group(1))
            if column:
                return ctx.with_column_filter(column[0], column[1], match.group(2))
        return None

    #: Comparison operators inside a FILTER predicate, longest first so that
    #: ``<=`` is matched before ``<``.
    _COMPARISONS = ("<=", ">=", "<>", "!=", "=", "<", ">")

    def _scalar_in_context(self, expr, ctx, model, cache, table_in):
        """Evaluate ``MAX('Date'[Date])`` etc., keeping the native dtype.

        ``_eval_aggregate`` coerces to float, which destroys a datetime — and a
        running-total predicate compares against exactly that.
        """
        import pandas as pd

        match = re.match(r"^\s*(MAX|MIN)\s*\((.+)\)\s*$", expr or "", re.IGNORECASE)
        if match:
            parsed = self._parse_column_ref(match.group(2))
            if not parsed:
                return None
            table, column = parsed
            df = table_in(ctx, table)
            if df is None or column not in df.columns or df.empty:
                return None
            series = df[column]
            if not pd.api.types.is_numeric_dtype(series):
                series = pd.to_datetime(series, errors="coerce").dropna()
                if series.empty:
                    return None
            return series.max() if match.group(1).upper() == "MAX" else series.min()

        text = (expr or "").strip().strip("\"'")
        try:
            return float(text)
        except ValueError:
            return text or None

    def _filter_modifier(self, ctx, inner, model, cache, table_in):
        """``FILTER(<table>, <predicate>)`` — the running-total building block.

        The scalar on the right of the predicate is evaluated in the *outer*
        context (before the table expression widens it), which is what makes
        ``'Date'[Date] <= MAX('Date'[Date])`` cumulative rather than a no-op.
        """
        import pandas as pd

        args = self._split_args(inner)
        if len(args) != 2:
            return None
        table_expr, predicate_text = args[0].strip(), args[1].strip()

        # Table expression: ALL('Date') widens first, or a bare table name.
        base = ctx
        if table_expr.upper().startswith("ALL(") and table_expr.endswith(")"):
            target = table_expr[len("ALL("):-1].strip().strip("'")
            column = self._parse_column_ref(target)
            table = column[0] if column else target
            base = ctx.without_table(table)
        else:
            table = table_expr.strip("'")

        # Predicate: <column> <op> <scalar>
        for operator in self._COMPARISONS:
            position = predicate_text.find(operator)
            if position <= 0:
                continue
            left = predicate_text[:position].strip()
            right = predicate_text[position + len(operator):].strip()
            parsed = self._parse_column_ref(left)
            if not parsed:
                continue
            _, column = parsed
            bound = self._scalar_in_context(right, ctx, model, cache, table_in)
            if bound is None:
                return None

            def predicate(df, _col=column, _op=operator, _bound=bound):
                series = df[_col]
                if isinstance(_bound, pd.Timestamp):
                    series = pd.to_datetime(series, errors="coerce")
                if _op == "<=":
                    return series <= _bound
                if _op == ">=":
                    return series >= _bound
                if _op == "<":
                    return series < _bound
                if _op == ">":
                    return series > _bound
                if _op in ("<>", "!="):
                    return series != _bound
                return series == _bound

            return base.with_mask(
                table, predicate, label=f"FILTER({table}[{column}]{operator}{bound})"
            )
        return None

    def _dates_in_context(self, ctx, column_ref, model, cache, table_in):
        """The date Series of *column_ref* as narrowed by the current context."""
        parsed = self._parse_column_ref(column_ref)
        if not parsed:
            return None, None
        table, column = parsed
        df = table_in(ctx, table)
        if df is None or column not in df.columns:
            return None, None
        import pandas as pd

        series = pd.to_datetime(df[column], errors="coerce").dropna()
        return (table, column), (series if not series.empty else None)

    def _ytd_context(self, ctx, column_ref, model, metadata, cache, table_in):
        """Context narrowed to Jan 1 .. the last date visible in *ctx*.

        Anchored to the dates surviving the current filter context, exactly as
        DAX does. Note this is the date *dimension*: when a calendar table runs
        past the facts, the year-to-date window can legitimately contain no
        rows, which is what Power BI itself would render.
        """
        parsed, series = self._dates_in_context(ctx, column_ref, model, cache, table_in)
        if not parsed or series is None:
            return None
        import pandas as pd

        table, column = parsed
        end = series.max()
        start = pd.Timestamp(end.year, 1, 1)

        def predicate(df, _col=column, _start=start, _end=end):
            dates = pd.to_datetime(df[_col], errors="coerce")
            return (dates >= _start) & (dates <= _end)

        return ctx.with_mask(
            table, predicate, label=f"YTD({table}[{column}]<={end.date()})"
        )

    def _shift_year_context(self, ctx, column_ref, model, metadata, cache,
                            table_in, years: int = 1):
        """Context moved back *years*, preserving the window width."""
        parsed, series = self._dates_in_context(ctx, column_ref, model, cache, table_in)
        if not parsed or series is None:
            return None
        import pandas as pd

        table, column = parsed
        start = series.min() - pd.DateOffset(years=years)
        end = series.max() - pd.DateOffset(years=years)

        def predicate(df, _col=column, _start=start, _end=end):
            dates = pd.to_datetime(df[_col], errors="coerce")
            return (dates >= _start) & (dates <= _end)

        # The prior-period window REPLACES any existing filter on this table,
        # which is what SAMEPERIODLASTYEAR does.
        return ctx.without_table(table).with_mask(
            table, predicate, label=f"SPLY({table}[{column}]:{start.date()}..{end.date()})"
        )

    @staticmethod
    def _split_operator(expr: str, operators: str) -> tuple[str, str, str] | None:
        """Rightmost top-level operator, so ``a - b - c`` groups left to right."""
        depth = 0
        for i in range(len(expr) - 1, -1, -1):
            char = expr[i]
            if char in ")]":
                depth += 1
            elif char in "([":
                depth -= 1
            elif depth == 0 and char in operators and i > 0:
                # Not a sign: the previous non-space char must end an operand.
                previous = expr[:i].rstrip()
                if previous and previous[-1] not in "(+-*/,":
                    return expr[:i], char, expr[i + 1:]
        return None

    def _eval_derived(
        self, expr: str, values: dict, lookup: dict, agg=None
    ) -> float | None:
        """Evaluate a measure expression built from other measures.

        Recursive rather than pattern-matched: real DAX nests operands inside
        DIVIDE, e.g. ``DIVIDE([Sales]-[Sales LY], [Sales LY], 0)``. Matching
        only ``[A] op [B]`` and ``DIVIDE([A],[B])`` left those unevaluated.
        """
        expr = (expr or "").strip()
        if not expr:
            return None

        # Strip redundant outer parentheses: "(a + b)" -> "a + b".
        while expr.startswith("(") and expr.endswith(")"):
            depth = 0
            for i, char in enumerate(expr):
                depth += (char == "(") - (char == ")")
                if depth == 0 and i < len(expr) - 1:
                    break
            else:
                expr = expr[1:-1].strip()
                continue
            break

        # Lowest precedence first, so it binds loosest.
        for operators in ("+-", "*/"):
            split = self._split_operator(expr, operators)
            if split:
                left_text, op, right_text = split
                left = self._eval_derived(left_text, values, lookup, agg)
                right = self._eval_derived(right_text, values, lookup, agg)
                if left is None or right is None:
                    return None
                if op == "+":
                    return left + right
                if op == "-":
                    return left - right
                if op == "*":
                    return left * right
                return None if right == 0 else left / right

        if expr.upper().startswith("DIVIDE(") and expr.endswith(")"):
            args = self._split_args(expr[len("DIVIDE("):-1])
            if len(args) >= 2:
                left = self._eval_derived(args[0], values, lookup, agg)
                right = self._eval_derived(args[1], values, lookup, agg)
                if left is None or right is None:
                    return None
                if right == 0:
                    # DIVIDE's third argument is the divide-by-zero result.
                    if len(args) > 2:
                        return self._eval_derived(args[2], values, lookup, agg)
                    return None
                return left / right

        # A measure reference: [Total Sales Amount]
        bare = expr[1:-1] if expr.startswith("[") and expr.endswith("]") else expr
        key = lookup.get(bare.strip().casefold())
        found = values.get(key) if key else None
        if found is not None:
            return found.value

        # A numeric literal (DIVIDE's alternate result, scaling factors).
        try:
            return float(expr)
        except ValueError:
            pass

        # An aggregate written inline: DISTINCTCOUNT('Date'[Date])
        return agg(expr) if agg else None

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _table(model, name: str, cache: dict):
        if name in cache:
            return cache[name]
        try:
            df = model.get_table(name)
        except Exception:  # noqa: BLE001 - table not in the model
            df = None
        cache[name] = df
        return df

    @staticmethod
    def _format(value: float) -> str:
        """Raw numeric string — the comparison engine handles display formats."""
        if value == int(value):
            return str(int(value))
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def _pbix_path(self, project: Project):
        paths = self._repo.paths_for(project)
        files = fm.list_dir(paths.dashboard_dir)
        for f in files:
            if f.suffix.lower() == ".pbix":
                return f
        return None
