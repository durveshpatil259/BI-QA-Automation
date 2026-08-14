"""Compile a DAX measure into SQL over the source files.

The validation plan's ``(aggregation, column)`` pair only describes the
simplest measures. ``Total Profit`` is ``SUM(Sales[Profit])`` over a *calculated
column*; ``Gross Margin`` divides two other measures. Executing the plan
verbatim for those produced a confident wrong number.

The DAX already states the intent exactly, so Python compiles it rather than
asking a model to restate it — no arithmetic is delegated, and the result is
reproducible.

Each aggregate becomes a **scalar subquery over its own dataset**, which is what
makes ``DIVIDE([Total Sales], DISTINCTCOUNT('Date'[Date]))`` correct: the
denominator counts every row of the calendar, not just the days a join happened
to keep. Joining instead silently inflates every per-day average.

Anything the grammar does not cover compiles to ``None``; the caller then
reports "manual review required" instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.logger import get_logger

_logger = get_logger()


class _Unsupported(Exception):
    """Raised inside a regex callback when a construct cannot be compiled."""

__all__ = ["CompiledMeasure", "Dialect", "DUCKDB", "TSQL", "compile_measure"]

#: ``SUM(Table[Column])`` and friends.
_AGGREGATE = re.compile(
    r"^(SUM|AVERAGE|AVG|MIN|MAX|COUNT|COUNTA|DISTINCTCOUNT)\s*\(\s*"
    r"'?([^'\[\]]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*\)$",
    re.IGNORECASE,
)
#: X-suffixed aggregates evaluate an expression once per row of a table.
_ITERATORS = {"SUMX": "SUM", "AVERAGEX": "AVG", "MINX": "MIN", "MAXX": "MAX",
              "COUNTX": "COUNT"}
#: ``DATEADD(Calendar[date], -1, MONTH)`` — the same window, shifted.
_DATEADD = re.compile(
    r"DATEADD\s*\(\s*(?:'([^']+)'|(\w+))\s*\[\s*([^\]]+?)\s*\]\s*,\s*"
    r"(-?\d+)\s*,\s*(DAY|MONTH|QUARTER|YEAR)\s*\)", re.IGNORECASE)
#: Time-intelligence table functions whose date range has a closed form.
_TIME_INTEL = {"DATESMTD": "month", "DATESQTD": "quarter", "DATESYTD": "year"}
#: A bare measure reference: ``[Total Sales]``
_MEASURE_REF = re.compile(r"^\[([^\]]+)\]$")
#: A column reference inside a calculated-column formula.
#: ``Table[Column]`` or ``'Table With Spaces'[Column]``. An unquoted DAX table
#: name is a single word, so the alternation is not cosmetic: a loose pattern
#: reads ``CASE WHEN Fact[los]`` as a table called "CASE WHEN Fact".
_COLUMN_REF = re.compile(r"(?:'([^']+)'|(\w+))?\s*\[\s*([^\]]+?)\s*\]")

_SQL_AGG = {
    "SUM": "SUM", "AVERAGE": "AVG", "AVG": "AVG", "MIN": "MIN", "MAX": "MAX",
    "COUNT": "COUNT", "COUNTA": "COUNT", "DISTINCTCOUNT": "COUNT",
}


@dataclass
class CompiledMeasure:
    """A measure expressed as SQL, plus how to describe it to a reader."""

    sql: str
    description: str
    datasets: tuple[str, ...] = ()


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


class Dialect:
    """The handful of places SQL engines actually disagree.

    Everything else the compiler emits — CASE, NULLIF, COALESCE, COUNT(DISTINCT),
    scalar subqueries — is identical across DuckDB and T-SQL, so only date
    handling and identifier quoting need a per-engine form.
    """

    name = "duckdb"
    timestamp_type = "TIMESTAMP"

    def quote(self, name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    def cast_ts(self, expr: str) -> str:
        return f"TRY_CAST({expr} AS {self.timestamp_type})"

    def date_diff(self, unit: str, start: str, end: str) -> str:
        return f"date_diff('{unit}', {self.cast_ts(start)}, {self.cast_ts(end)})"

    def date_part(self, part: str, expr: str) -> str:
        return f"{part}({self.cast_ts(expr)})"

    def date_trunc(self, unit: str, expr: str) -> str:
        return f"date_trunc('{unit}', {expr})"

    def date_shift(self, expr: str, amount: str, unit: str) -> str:
        return f"({expr} + INTERVAL '{amount} {unit}')"


class TSqlDialect(Dialect):
    """SQL Server. Brackets, DATETIME, and date functions that take no quotes."""

    name = "tsql"
    timestamp_type = "DATETIME"

    def quote(self, name: str) -> str:
        text = str(name)
        # A schema-qualified name arrives as dbo.Table and must stay two parts.
        if "." in text and "[" not in text:
            return ".".join(f"[{p.replace(']', ']]')}]" for p in text.split("."))
        return f"[{text.replace(']', ']]')}]"

    def date_diff(self, unit: str, start: str, end: str) -> str:
        return f"DATEDIFF({unit}, {self.cast_ts(start)}, {self.cast_ts(end)})"

    def date_trunc(self, unit: str, expr: str) -> str:
        # No DATE_TRUNC before SQL Server 2022. Counting whole periods from the
        # zero date and adding them back is the portable idiom.
        return f"DATEADD({unit}, DATEDIFF({unit}, 0, {expr}), 0)"

    def date_shift(self, expr: str, amount: str, unit: str) -> str:
        return f"DATEADD({unit}, {amount}, {expr})"


DUCKDB = Dialect()
TSQL = TSqlDialect()


def _strip_parens(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for i, ch in enumerate(text):
            depth += (ch == "(") - (ch == ")")
            if depth == 0 and i < len(text) - 1:
                return text
        text = text[1:-1].strip()
    return text


def _split_args(inner: str) -> list[str]:
    args, depth, current = [], 0, []
    for ch in inner:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current:
        args.append("".join(current).strip())
    return args


def _split_operator(expr: str, operators: str):
    """Rightmost top-level operator, so ``a - b - c`` groups left to right."""
    depth = 0
    for i in range(len(expr) - 1, -1, -1):
        ch = expr[i]
        if ch in ")]":
            depth += 1
        elif ch in "([":
            depth -= 1
        elif depth == 0 and ch in operators and i > 0:
            before = expr[:i].rstrip()
            if before and before[-1] not in "(+-*/,":
                return expr[:i], ch, expr[i + 1:]
    return None


class _Compiler:
    """Walks one measure's DAX, resolving references against the model."""

    def __init__(self, metadata, resolve_column, filter_for=None,
                 related_for=None, column_filter=None, max_depth: int = 8,
                 dialect: Dialect | None = None):
        self._metadata = metadata
        self._d = dialect or DUCKDB
        self._resolve = resolve_column     # (table, column) -> ResolvedField|None
        # (dataset) -> (joins, clauses) applying the scenario's filter to it.
        # A DAX filter context narrows *every* aggregate in the measure, so it
        # has to be pushed into each scalar subquery rather than wrapped around
        # the result.
        self._filter_for = filter_for
        # (base_table, target_table, target_column) -> (join_sql, dataset, column)
        self._related_for = related_for
        # (dataset, table, column, alias) -> (joins, ref, dataset, column).
        # How a CALCULATE filter reaches the dataset being aggregated. An empty
        # ``ref`` means the model does not propagate the filter there, so it is
        # a no-op rather than an error — Power BI's own behaviour.
        self._column_filter = column_filter
        self._max_depth = max_depth
        # CALCULATE nests, so its filters form a stack: every enclosing
        # CALCULATE still applies to the aggregate being built.
        self._context: list[list] = []
        self._aliases = 0
        self.datasets: set[str] = set()
        self.notes: list[str] = []

    def _q(self, name: str) -> str:
        return self._d.quote(name)

    def _next_alias(self) -> int:
        self._aliases += 1
        return self._aliases

    def _from_clause(self, dataset: str, joins=(), clauses=()) -> str:
        """``FROM "dataset" AS base`` with every applicable join and predicate.

        Three kinds of narrowing meet here — the scenario's slicer filters, the
        row predicate of a ``FILTER()``, and each enclosing ``CALCULATE`` — and
        they compose, because all three narrow the same set of rows. Merging
        them into one WHERE is what keeps a rate measure's numerator and
        denominator on the same footing.
        """
        all_joins, all_clauses = list(joins), list(clauses)
        if self._filter_for:
            extra_joins, extra_clauses = self._filter_for(dataset)
            all_joins.extend(extra_joins)
            all_clauses.extend(extra_clauses)
        for builders in self._context:
            for build in builders:
                built = build(dataset, f"k{self._next_alias()}")
                if built is None:
                    raise _Unsupported()
                more_joins, more_clauses = built
                all_joins.extend(more_joins)
                all_clauses.extend(more_clauses)

        sql = f'FROM {self._q(dataset)} AS base'
        if all_joins:
            sql += " " + " ".join(dict.fromkeys(all_joins))
        if all_clauses:
            sql += " WHERE " + " AND ".join(all_clauses)
        return sql

    # --- model lookups ----------------------------------------------------
    def _measure_dax(self, name: str) -> str:
        for measure in (self._metadata.all_measures if self._metadata else []):
            if (measure.name or "").casefold() == name.strip().casefold():
                return " ".join((measure.dax_expression or "").split())
        return ""

    def _calculated_column(self, table: str, column: str) -> str:
        from src.domain.models import normalise_table_name

        target = normalise_table_name(table)
        for t in (self._metadata.tables if self._metadata else []):
            if normalise_table_name(t.name) != target:
                continue
            for c in t.columns:
                if (c.name or "").casefold() == column.strip().casefold():
                    return " ".join((c.dax_expression or "").split()) if c.is_calculated else ""
        return ""

    # --- compilation ------------------------------------------------------
    def expression(self, dax: str, depth: int = 0) -> str | None:
        """Compile a DAX expression to a SQL scalar expression."""
        if depth > self._max_depth:
            return None
        expr = _strip_parens(dax)
        if not expr:
            return None
        # A measure states its intermediate steps with VAR just as a column
        # does — a rate measure names its numerator before dividing by it.
        if re.search(r"\bVAR\b.*\bRETURN\b", expr, re.IGNORECASE | re.DOTALL):
            expr = _strip_parens(self._expand_vars(expr))

        # Lowest precedence first so it binds loosest.
        for operators in ("+-", "*/"):
            split = _split_operator(expr, operators)
            if split:
                left_text, op, right_text = split
                left = self.expression(left_text, depth + 1)
                right = self.expression(right_text, depth + 1)
                if left is None or right is None:
                    return None
                if op == "/":
                    return f"({left} / NULLIF({right}, 0))"
                return f"({left} {op} {right})"

        upper = expr.upper()
        if upper.startswith("DIVIDE(") and expr.endswith(")"):
            args = _split_args(expr[len("DIVIDE("):-1])
            if len(args) < 2:
                return None
            numerator = self.expression(args[0], depth + 1)
            denominator = self.expression(args[1], depth + 1)
            if numerator is None or denominator is None:
                return None
            if len(args) > 2:
                alternate = self.expression(args[2], depth + 1)
                if alternate is not None:
                    return (f"COALESCE({numerator} / NULLIF({denominator}, 0), "
                            f"{alternate})")
            return f"({numerator} / NULLIF({denominator}, 0))"

        if upper.startswith("CALCULATE(") and expr.endswith(")"):
            return self._calculate(expr, depth)

        # Iterators before plain aggregates: AVERAGEX is not AVERAGE, and the
        # aggregate pattern must not be allowed to half-match it.
        for name, sql_function in _ITERATORS.items():
            if upper.startswith(name + "(") and expr.endswith(")"):
                args = _split_args(expr[len(name) + 1:-1])
                if len(args) != 2:
                    return None
                source = self._table_source(args[0])
                if source is None:
                    return None
                return self._iterate(sql_function, source, args[1])

        if upper.startswith("COUNTROWS(") and expr.endswith(")"):
            source = self._table_source(expr[len("COUNTROWS("):-1])
            if source is None:
                return None
            return self._iterate("COUNT", source, "*")

        aggregate = _AGGREGATE.match(expr)
        if aggregate:
            return self._aggregate(*aggregate.groups(), depth=depth)

        reference = _MEASURE_REF.match(expr)
        if reference:
            inner = self._measure_dax(reference.group(1))
            if not inner:
                return None
            return self.expression(inner, depth + 1)

        try:
            return str(float(expr))
        except ValueError:
            return None

    def _table_source(self, text: str, depth: int = 0):
        """A DAX table expression -> ``(table name, row predicates)``.

        ``FILTER(FactAdmissions, FactAdmissions[dischtime] <> BLANK())`` is a
        table, not a value: it narrows the rows an iterator walks. That is a
        WHERE clause on the same subquery, which is why the predicate travels
        with the table rather than being compiled on its own.
        """
        text = _strip_parens(text)
        if depth > self._max_depth or not text:
            return None
        if re.match(r"^FILTER\s*\(", text, re.IGNORECASE) and text.endswith(")"):
            args = _split_args(text[text.index("(") + 1:-1])
            if len(args) != 2:
                return None
            inner = self._table_source(args[0], depth + 1)
            if inner is None:
                return None
            table, predicates = inner
            return table, predicates + [args[1]]
        if re.fullmatch(r"'[^']+'|\w+", text):
            return text.strip("'"), []
        return None                          # ALL(), VALUES(), a join — not yet

    def _iterate(self, sql_function: str, source, row_expr: str) -> str | None:
        """An iterator over a table expression: one aggregate, one subquery.

        ``AVERAGEX(FILTER(t, p), e)`` is the average of ``e`` over the rows of
        ``t`` where ``p`` holds. Both ``e`` and ``p`` are row-level, so they
        compile the same way a calculated column does — including RELATED.
        """
        table, predicates = source
        field = self._resolve(table, None)
        if field is None:
            return None

        joins, clauses = [], []
        for predicate in predicates:
            compiled, extra = self._column_formula(table, predicate)
            if compiled is None:
                return None
            joins.extend(extra or [])
            clauses.append(compiled)

        if row_expr.strip() == "*":
            target = "*"
        else:
            target, extra = self._column_formula(table, row_expr)
            if target is None:
                return None
            joins.extend(extra or [])

        self.datasets.add(field.dataset)
        if predicates:
            self.notes.append("limited to rows matching "
                              + " and ".join(predicates))
        return (f'(SELECT {sql_function}({target}) '
                f'{self._from_clause(field.dataset, joins, clauses)})')

    # --- CALCULATE --------------------------------------------------------
    def _calculate(self, expr: str, depth: int) -> str | None:
        """``CALCULATE(expr, filters…)`` — evaluate expr under extra filters.

        A CALCULATE filter narrows every aggregate inside its expression, just
        as a slicer does, so it is pushed into each scalar subquery rather than
        wrapped around the finished value.
        """
        args = _split_args(expr[len("CALCULATE("):-1])
        if not args:
            return None
        builders = []
        for raw in args[1:]:
            builder = self._filter_argument(raw)
            if builder is None:
                return None
            builders.append(builder)
        self._context.append(builders)
        try:
            return self.expression(args[0], depth + 1)
        finally:
            self._context.pop()

    def _filter_argument(self, raw: str):
        """One CALCULATE filter -> a builder, or None when it is out of scope."""
        text = _strip_parens(raw)
        for name, unit in _TIME_INTEL.items():
            match = re.fullmatch(
                rf"{name}\s*\(\s*(?:'([^']+)'|(\w+))\s*\[\s*([^\]]+?)\s*\]\s*\)",
                text, re.IGNORECASE)
            if match:
                return self._period_to_date(match.group(1) or match.group(2),
                                            match.group(3), unit)
        shifted = _DATEADD.fullmatch(text)
        if shifted:
            return self._shifted_period(shifted.group(1) or shifted.group(2),
                                        shifted.group(3), shifted.group(4),
                                        shifted.group(5).lower())
        match = re.fullmatch(
            r"(?:'([^']+)'|(\w+))\s*\[\s*([^\]]+?)\s*\]\s*(=|<>|<=|>=|<|>)\s*(.+)",
            text)
        if match:
            return self._column_predicate(match.group(1) or match.group(2),
                                          match.group(3), match.group(4),
                                          match.group(5))
        return None

    def _column_predicate(self, table: str, column: str, operator: str, value: str):
        value = value.strip()
        if re.fullmatch(r'"[^"]*"', value):
            literal = "'" + value[1:-1].replace("'", "''") + "'"
        else:
            try:
                literal = str(float(value))
            except ValueError:
                return None
        operator = "!=" if operator == "<>" else operator

        def build(dataset: str, alias: str):
            located = self._locate(dataset, table, column, alias)
            if located is None:
                return None
            joins, ref, _, _ = located
            if not ref:
                return [], []            # the model does not propagate it here
            return joins, [f"{ref} {operator} {literal}"]
        return build

    def _date_bound(self, function: str, dataset: str, column: str) -> str:
        """MIN/MAX of a calendar column, under the scenario's own filter.

        The bound has to move with the slicer: "last month" relative to a
        report filtered to Q1 is not the same month as relative to the whole
        calendar, and reading the unfiltered table would silently pick the
        latter.
        """
        joins, clauses = self._filter_for(dataset) if self._filter_for else ([], [])
        sql = (f'SELECT {function}({self._d.cast_ts("base." + self._q(column))}) '
               f'FROM {self._q(dataset)} AS base')
        if joins:
            sql += " " + " ".join(dict.fromkeys(joins))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return f"({sql})"

    def _shifted_period(self, table: str, column: str, amount: str, unit: str):
        """``DATEADD`` -> the window in context, moved a whole number of periods.

        DATEADD maps every date in context to the same date one period away.
        A calendar table is contiguous, so the image of a contiguous window is
        that window shifted — a pair of bounds, not a row-by-row lookup. The
        join to the calendar is what keeps a shifted date that does not exist
        (there is no 31st of every month) from counting.
        """
        def build(dataset: str, alias: str):
            located = self._locate(dataset, table, column, alias)
            if located is None:
                return None
            joins, ref, date_dataset, date_column = located
            if not ref:
                return [], []
            cast = self._d.cast_ts(ref)
            lo = self._d.date_shift(
                self._date_bound("MIN", date_dataset, date_column), amount, unit)
            hi = self._d.date_shift(
                self._date_bound("MAX", date_dataset, date_column), amount, unit)
            return joins, [f"{cast} >= {lo}", f"{cast} <= {hi}"]
        return build

    def _period_to_date(self, table: str, column: str, unit: str):
        """``DATESxTD`` -> the period's first day through its latest date.

        Power BI anchors the period on the latest date in the current filter
        context; with nothing else filtering, that is the last date in the
        calendar table, which is what the subquery reads. A calendar running
        past the last fact row therefore yields an empty period — the same
        blank the dashboard shows, not a wrong number.
        """
        def build(dataset: str, alias: str):
            located = self._locate(dataset, table, column, alias)
            if located is None:
                return None
            joins, ref, date_dataset, date_column = located
            if not ref:
                return [], []
            anchor = self._date_bound("MAX", date_dataset, date_column)
            cast = self._d.cast_ts(ref)
            return joins, [f"{cast} >= {self._d.date_trunc(unit, anchor)}",
                           f"{cast} <= {anchor}"]
        return build

    def _locate(self, dataset: str, table: str, column: str, alias: str):
        if self._column_filter is None:
            return None
        return self._column_filter(dataset, table, column, alias)

    def _aggregate(self, function, table, column, depth: int) -> str | None:
        """One aggregate becomes a scalar subquery over its own dataset.

        Keeping each aggregate independent is what makes a per-day average
        correct: the denominator counts the calendar table itself rather than
        the rows a join left behind.
        """
        function = function.upper()
        sql_function = _SQL_AGG.get(function)
        if not sql_function:
            return None

        formula = self._calculated_column(table, column)
        if formula:
            inner, extra_joins = self._column_formula(table, formula)
            if inner is None:
                return None
            field = self._resolve(table, None)
            if field is None:
                return None
            self.datasets.add(field.dataset)
            self.notes.append(f"{column} = {formula}")
            return (f'(SELECT {sql_function}({inner}) '
                    f'{self._from_clause(field.dataset, extra_joins or [])})')

        field = self._resolve(table, column)
        if field is None:
            return None
        self.datasets.add(field.dataset)
        target = f"DISTINCT {self._q(field.column)}" if function == "DISTINCTCOUNT" \
            else self._q(field.column)
        return (f'(SELECT {sql_function}({target}) '
                f'{self._from_clause(field.dataset)})')

    @staticmethod
    def _expand_vars(formula: str) -> str:
        """Inline ``VAR name = expr RETURN body`` so only the body remains.

        A banding column is almost always written this way — the interval is
        computed once and then compared several times — so without VAR support
        every LOS/duration bucket stays unevaluable.
        """
        text = " ".join((formula or "").split())
        while True:
            match = re.search(r"\bVAR\s+(\w+)\s*=\s*(.+?)\s+RETURN\s+(.*)$",
                              text, re.IGNORECASE | re.DOTALL)
            if not match:
                return text
            name, value, body = match.groups()
            text = re.sub(rf"\b{re.escape(name)}\b", f"({value.strip()})", body)

    @staticmethod
    def _expand_switch(text: str) -> str | None:
        """``SWITCH(TRUE(), cond, val, ..., else)`` -> ``CASE WHEN ... END``.

        Only the ``TRUE()`` form is handled: the value-matching form compares
        against an expression and would need different SQL. Returns None for
        anything else so it is refused rather than mis-compiled.
        """
        while True:
            start = re.search(r"\bSWITCH\s*\(", text, re.IGNORECASE)
            if not start:
                return text
            # Find the matching close paren for this SWITCH.
            depth, i = 0, start.end() - 1
            for i in range(start.end() - 1, len(text)):
                depth += (text[i] == "(") - (text[i] == ")")
                if depth == 0:
                    break
            else:
                return None
            args = _split_args(text[start.end():i])
            if len(args) < 3 or args[0].strip().upper() not in ("TRUE()", "TRUE"):
                return None                      # value-matching form
            pairs, rest = args[1:], []
            clauses = []
            while len(pairs) >= 2:
                clauses.append(f"WHEN {pairs[0]} THEN {pairs[1]}")
                pairs = pairs[2:]
            otherwise = f" ELSE {pairs[0]}" if pairs else ""
            replacement = "CASE " + " ".join(clauses) + otherwise + " END"
            text = text[:start.start()] + replacement + text[i + 1:]

    #: Row-level DAX functions with a direct DuckDB equivalent.
    _SCALAR_FUNCS = {"YEAR": "year", "MONTH": "month", "DAY": "day"}

    def _column_formula(self, table: str, formula: str, depth: int = 0):
        """Rewrite a calculated column's formula over the source columns.

        Returns ``(expression, joins)``. ``RELATED(Dim[Col])`` is a row-context
        lookup across a relationship, which becomes a join — without it, any
        measure over such a column was unevaluable, and on a real dashboard
        that was every age-based figure.
        """
        joins: list[str] = []
        aliases: dict[str, str] = {}
        text = self._expand_vars(formula)

        # DAX quotes strings with ", SQL with '. Convert before any identifier
        # is emitted, since those use double quotes too.
        text = re.sub(r'"([^"]*)"', lambda m: "'" + m.group(1).replace("'", "''") + "'",
                      text)
        text = self._expand_switch(text)
        if text is None:
            return None, None

        # RELATED(Dim[Column]) -> join that table once and read its column.
        def take_related(match) -> str:
            target_table, target_column = match.group(1).strip(), match.group(2).strip()
            if self._related_for is None:
                raise _Unsupported()
            resolved = self._related_for(table, target_table, target_column)
            if resolved is None:
                raise _Unsupported()
            join_sql, dataset, column = resolved
            alias = aliases.get(dataset)
            if alias is None:
                alias = f"r{self._next_alias()}"
                aliases[dataset] = alias
                joins.append(join_sql.format(alias=alias))
            return f"{alias}.{self._q(column)}"

        try:
            text = re.sub(
                r"RELATED\s*\(\s*'?([^'\[\]]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*\)",
                take_related, text, flags=re.IGNORECASE)
        except _Unsupported:
            return None, None

        # Own-table column references.
        out, last = [], 0
        for match in _COLUMN_REF.finditer(text):
            ref_table = (match.group(1) or match.group(2) or table).strip() or table
            # Skip what RELATED already produced (r0."col").
            if re.match(r"^r\d+$", ref_table):
                continue
            field = self._resolve(ref_table, match.group(3))
            if field is None:
                # Not a column in the file. A calculated column may be built on
                # another one — an age band reads an age that is itself derived
                # — so expand that formula in place rather than giving up.
                nested = self._calculated_column(ref_table, match.group(3))
                if not nested or depth >= self._max_depth:
                    return None, None
                inner, inner_joins = self._column_formula(ref_table, nested,
                                                          depth + 1)
                if inner is None:
                    return None, None
                for join in inner_joins or []:
                    if join not in joins:
                        joins.append(join)
                out.append(text[last:match.start()])
                out.append(f"({inner})")
                last = match.end()
                continue
            out.append(text[last:match.start()])
            out.append(f"base.{self._q(field.column)}")
            last = match.end()
        out.append(text[last:])
        rewritten = "".join(out)

        # DATEDIFF(start, end, DAY) -> date_diff('day', start, end). Both ends
        # are cast for the same reason as the date parts below.
        rewritten = re.sub(
            r"\bDATEDIFF\s*\(\s*([^(),]+?)\s*,\s*([^(),]+?)\s*,\s*(\w+)\s*\)",
            lambda m: self._d.date_diff(m.group(3).lower(),
                                        m.group(1), m.group(2)),
            rewritten, flags=re.IGNORECASE)

        # ISBLANK(x) -> x IS NULL. A still-open record has no discharge date,
        # and that is a band of its own in every length-of-stay column.
        rewritten = re.sub(r"\bISBLANK\s*\(\s*([^()]+?)\s*\)",
                           lambda m: f"({m.group(1)} IS NULL)",
                           rewritten, flags=re.IGNORECASE)

        # DAX spells the logical operators && and ||; SQL spells them out.
        rewritten = rewritten.replace("&&", " AND ").replace("||", " OR ")

        # BLANK() is DAX's empty value, so comparing to it is a null test.
        # "discharged" is written this way on every length-of-stay measure.
        for pattern, sql in ((r"<>\s*BLANK\s*\(\s*\)", "IS NOT NULL"),
                             (r"=\s*BLANK\s*\(\s*\)", "IS NULL")):
            rewritten = re.sub(
                r'((?:base|[rkf]\d+)\.(?:"[^"]*"|\[[^\]]*\]))\s*' + pattern,
                lambda m, s=sql: f"({m.group(1)} {s})",
                rewritten, flags=re.IGNORECASE)

        # Supported row-level date functions. The argument is cast because a
        # spreadsheet column arrives as text — pandas types an Excel datetime
        # column as `object`, and DuckDB has no year(VARCHAR).
        for dax_name, sql_name in self._SCALAR_FUNCS.items():
            rewritten = re.sub(
                rf"\b{dax_name}\s*\(\s*([^()]+?)\s*\)",
                lambda m, fn=sql_name: self._d.date_part(fn, m.group(1)),
                rewritten, flags=re.IGNORECASE)

        # Whatever is left must be operators, numbers, our own references and
        # the functions above — anything else is DAX we do not understand.
        residue = re.sub(r'(?:base|[rkf]\d+)\.(?:"[^"]*"|\[[^\]]*\])', "", rewritten)
        residue = re.sub(r"'[^']*'", "", residue)          # string literals
        residue = residue.replace("TRY_CAST(", "(").replace(
            f" AS {self._d.timestamp_type}", "")
        for sql_name in self._SCALAR_FUNCS.values():
            residue = residue.replace(f"{sql_name}(", "(")
        for keyword in ("CASE", "WHEN", "THEN", "ELSE", "END", "IS NOT NULL",
                        "IS NULL", "date_diff(", "DATEDIFF(", "DATEADD(",
                        " AND ", " OR "):
            residue = residue.replace(keyword, "")
        if re.search(r"[A-Za-z_]", residue):
            return None, None
        return rewritten, joins


def compile_measure(name: str, metadata, resolve_column, filter_for=None,
                    related_for=None, column_filter=None,
                    dialect: Dialect | None = None) -> CompiledMeasure | None:
    """Compile a named measure to SQL, or None when it is out of scope."""
    if not metadata:
        return None
    compiler = _Compiler(metadata, resolve_column, filter_for, related_for,
                         column_filter, dialect=dialect)
    dax = compiler._measure_dax(name)
    if not dax:
        return None
    try:
        sql = compiler.expression(dax)
    except _Unsupported:
        # A CALCULATE filter that could not be placed. Refusing is the point:
        # the alternative is a number computed without part of its filter.
        sql = None
    if sql is None:
        _logger.info("Could not compile measure '%s': %s", name, dax[:80])
        return None

    description = dax
    if compiler.notes:
        description += "  (" + "; ".join(compiler.notes) + ")"
    return CompiledMeasure(
        sql=f"SELECT {sql}",
        description=description,
        datasets=tuple(sorted(compiler.datasets)),
    )
