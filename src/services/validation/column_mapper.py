"""Map PBIX model fields to source columns, with a confidence score.

A model column and the spreadsheet column that feeds it rarely share a name:
``Sales[Sales Amount]`` arrives as ``SalesAmount``, ``Sales_Amount`` or
``AMOUNT_SOLD``. Most of that is lexical and can be settled deterministically —
which is preferable, because a deterministic match is reproducible and testable.

Three tiers, cheapest first:

1. **Lexical** — exact, case-folded, punctuation-stripped, token-set.
2. **Synonym** — a small curated BI vocabulary (revenue/sales, region/territory).
3. **AI** — only for what survives, via an injected resolver.

Nothing here fabricates a mapping. Anything below the review threshold is
returned as *unresolved* so the caller can skip it or ask the user, rather than
validating against a column that was guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

__all__ = [
    "ColumnMatch",
    "AUTO_ACCEPT",
    "REVIEW_FLOOR",
    "normalise",
    "score_pair",
    "is_match",
    "map_columns",
    "map_table_to_dataset",
]

#: >= this, the mapping is used without comment.
AUTO_ACCEPT = 0.90
#: >= this (but below AUTO_ACCEPT), used but flagged for review.
REVIEW_FLOOR = 0.70

#: Interchangeable BI vocabulary. Each row is one concept; any two members of
#: a row are treated as the same idea. Deliberately small — a large guessed
#: thesaurus would trade a visible failure for a silent wrong mapping.
_SYNONYMS: tuple[frozenset[str], ...] = (
    frozenset({"amount", "value", "total"}),
    frozenset({"sales", "revenue", "turnover"}),
    frozenset({"region", "territory", "area", "zone"}),
    frozenset({"customer", "client", "account"}),
    frozenset({"product", "item", "sku"}),
    frozenset({"category", "class", "segment"}),
    frozenset({"quantity", "qty", "units", "volume"}),
    frozenset({"cost", "expense"}),
    frozenset({"profit", "margin"}),
    frozenset({"date", "day"}),
    frozenset({"country", "nation"}),
    frozenset({"city", "town"}),
    frozenset({"id", "key", "code"}),
)

#: Warehouse noise that carries no meaning when comparing names.
_NOISE = {"data", "tbl", "table", "dim", "fact", "col", "field"}


def normalise(name: str) -> str:
    """Case-folded, punctuation-free form: ``Sales Amount`` -> ``salesamount``."""
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())


def _tokens(name: str) -> list[str]:
    """Split on separators and camelCase: ``SalesAmount`` -> [sales, amount]."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    parts = re.split(r"[^A-Za-z0-9]+", spaced)
    return [p.casefold() for p in parts if p and p.casefold() not in _NOISE]


def _concept(token: str) -> frozenset[str] | None:
    for group in _SYNONYMS:
        if token in group:
            return group
    return None


def _tokens_equivalent(a: str, b: str) -> bool:
    """Same token, or two names for the same concept."""
    if a == b:
        return True
    ca, cb = _concept(a), _concept(b)
    return ca is not None and ca is cb


def score_pair(pbix_column: str, source_column: str) -> tuple[float, str]:
    """Confidence that these two names denote the same field, and why."""
    if not pbix_column or not source_column:
        return 0.0, "unresolved"

    if pbix_column == source_column:
        return 1.0, "exact"
    if pbix_column.casefold() == source_column.casefold():
        return 0.99, "case"

    left, right = normalise(pbix_column), normalise(source_column)
    if left and left == right:
        return 0.97, "normalised"          # Sales Amount == SalesAmount

    lt, rt = _tokens(pbix_column), _tokens(source_column)
    if lt and rt:
        if lt == rt:
            return 0.96, "tokens"
        if sorted(lt) == sorted(rt):
            return 0.93, "tokens-reordered"  # Amount Sales == Sales Amount
        # Every token has an equivalent on the other side (Region -> Territory).
        if len(lt) == len(rt) and all(
            any(_tokens_equivalent(x, y) for y in rt) for x in lt
        ):
            return 0.91, "synonym"

    # Lexical similarity as the last deterministic signal. Capped below the
    # review floor: a fuzzy string match alone is not evidence of meaning.
    ratio = SequenceMatcher(None, left, right).ratio() if left and right else 0.0
    if ratio >= 0.86:
        return round(min(0.88, ratio), 3), "fuzzy"
    return round(ratio, 3), "unresolved"


def is_match(left: str, right: str) -> bool:
    """Do these two names denote the same field?

    Callers should prefer this over comparing ``score_pair``'s number to a
    threshold. The score is reported honestly even when no tier recognised the
    pair, so a bare similarity ratio can sit above the review floor —
    ``"Net Charge"`` scores 0.73 against ``"Freight Charge"``. Three separate
    call sites made that mistake before this helper existed.
    """
    confidence, method = score_pair(left, right)
    return method != "unresolved" and confidence >= REVIEW_FLOOR


@dataclass
class ColumnMatch:
    """One PBIX field and the source column it maps to."""

    pbix_field: str                  # "Sales[Sales Amount]"
    pbix_table: str = ""
    pbix_column: str = ""
    source_field: str = ""
    confidence: float = 0.0
    method: str = "unresolved"
    alternatives: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        # ``method == "unresolved"`` means no tier recognised the pair; a bare
        # string-similarity ratio can still sit above the review floor, and
        # using it would be exactly the silent wrong mapping this module exists
        # to avoid. The score is reported honestly and the match is not used.
        if (not self.source_field
                or self.method == "unresolved"
                or self.confidence < REVIEW_FLOOR):
            return "unresolved"
        return "accepted" if self.confidence >= AUTO_ACCEPT else "review"

    @property
    def usable(self) -> bool:
        """Accepted or review — anything below is not validated against."""
        return self.status in ("accepted", "review")

    def to_dict(self) -> dict:
        return {
            "pbix_field": self.pbix_field,
            "source_field": self.source_field,
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "status": self.status,
        }


def map_columns(
    pbix_columns: list[str],
    source_columns: list[str],
    *,
    table: str = "",
    resolver=None,
) -> list[ColumnMatch]:
    """Best source column for each PBIX column.

    Assignment is greedy over the strongest pairs first, and each source column
    is claimed once — otherwise ``Sales Amount`` and ``Total Amount`` both grab
    ``Amount`` and one of them is silently wrong.

    ``resolver`` is an optional callable ``(pbix_column, candidates) -> (name,
    confidence)`` used only for columns lexical scoring could not settle.
    """
    scored: list[tuple[float, str, str, str]] = []
    for pbix in pbix_columns:
        for source in source_columns:
            confidence, method = score_pair(pbix, source)
            if confidence > 0:
                scored.append((confidence, method, pbix, source))
    scored.sort(key=lambda row: (-row[0], row[2], row[3]))

    matches: dict[str, ColumnMatch] = {}
    claimed: set[str] = set()
    for confidence, method, pbix, source in scored:
        if pbix in matches or source in claimed:
            continue
        if confidence < REVIEW_FLOOR or method == "unresolved":
            continue
        matches[pbix] = ColumnMatch(
            pbix_field=f"{table}[{pbix}]" if table else pbix,
            pbix_table=table, pbix_column=pbix,
            source_field=source, confidence=confidence, method=method,
        )
        claimed.add(source)

    # Whatever is left is genuinely ambiguous — the only place AI is warranted.
    for pbix in pbix_columns:
        if pbix in matches:
            continue
        remaining = [c for c in source_columns if c not in claimed]
        resolved = resolver(pbix, remaining) if resolver and remaining else None
        if resolved and resolved[0] and resolved[1] >= REVIEW_FLOOR:
            matches[pbix] = ColumnMatch(
                pbix_field=f"{table}[{pbix}]" if table else pbix,
                pbix_table=table, pbix_column=pbix,
                source_field=resolved[0], confidence=float(resolved[1]), method="ai",
            )
            claimed.add(resolved[0])
        else:
            matches[pbix] = ColumnMatch(
                pbix_field=f"{table}[{pbix}]" if table else pbix,
                pbix_table=table, pbix_column=pbix,
                alternatives=remaining[:5],
            )
    return [matches[c] for c in pbix_columns]


def map_table_to_dataset(
    model_columns: list[str], datasets: dict[str, list[str]]
) -> tuple[str, float]:
    """Pick the sheet/file whose columns best cover a model table.

    Scored by how much of the model table the dataset explains, using the same
    tiered matching — so a sheet named nothing like the table still wins if its
    columns line up. Name similarity alone picked the wrong table before.
    """
    if not datasets:
        return "", 0.0
    best_name, best_key = "", (0.0, 0.0)
    for name, columns in datasets.items():
        if not columns or not model_columns:
            continue
        # Score only *recognised* pairs. A bare similarity ratio can clear the
        # review floor without any tier recognising it — "Despatch Date" scores
        # 0.78 against "despatch_ref" — and counting those made an unrelated
        # sheet tie with the right one.
        confidences = []
        for model_column in model_columns:
            best = 0.0
            for source_column in columns:
                confidence, method = score_pair(model_column, source_column)
                if method != "unresolved" and confidence >= REVIEW_FLOOR:
                    best = max(best, confidence)   # see is_match()
            confidences.append(best)

        matched = sum(1 for c in confidences if c > 0)
        coverage = matched / len(model_columns)
        # Total confidence breaks ties: two sheets can both cover every column
        # while one matches them far better.
        key = (coverage, sum(confidences))
        if key > best_key:
            best_name, best_key = name, key
    return best_name, round(best_key[0], 3)
