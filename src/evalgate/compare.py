"""Row comparison, carried over from the M0 benchmark."""

import re
from collections import Counter
from decimal import Decimal

ORDERED_LIMIT = re.compile(r"order\s+by.*\blimit\b", re.IGNORECASE | re.DOTALL)


def _norm(value):
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return round(float(value), 2)
    return str(value) if value is not None else None


def normalize(rows, ordered: bool):
    out = [tuple(_norm(v) for v in row) for row in rows]
    return out if ordered else Counter(out)


def is_ordered(gold_sql: str) -> bool:
    """Row order only matters when the gold query cuts a sorted list (top N)."""
    return bool(ORDERED_LIMIT.search(gold_sql))


def is_correct(model_rows, gold_rows, gold_sql: str) -> bool:
    ordered = is_ordered(gold_sql)
    return normalize(model_rows, ordered) == normalize(gold_rows, ordered)
