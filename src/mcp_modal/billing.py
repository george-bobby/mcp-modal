"""Local aggregation of `modal billing report --json` rows.

The report is one flat row per (app, interval) — 1500+ rows for a week on a busy
workspace — so grouping, ranking and diffing happen here rather than in the caller.
Costs are Decimal throughout: summing hundreds of 8-decimal strings as floats drifts.
"""
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Cost aggregation
# ---------------------------------------------------------------------------
# `modal billing report --json` returns one flat row per (app, interval) — a busy
# workspace easily produces hundreds. Summing and ranking them locally is the whole
# point of the cost tool: the caller gets "which app cost what", not 400 raw rows to
# add up itself. Costs are strings of decimal dollars; Decimal keeps the arithmetic
# exact (floats drift once you sum hundreds of 8-decimal values).

_COST_QUANTUM = Decimal("0.0001")


def _to_cost(value: Any) -> Decimal:
    """Parse a cost field into a Decimal, treating anything unparseable as zero."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _fmt_cost(value: Decimal) -> str:
    """Render a Decimal cost as a fixed 4-decimal string (dollars)."""
    return str(value.quantize(_COST_QUANTUM))


# `modal billing report --json` renames its columns between client versions: 1.4.x emits
# Title Case with spaces ("Object ID", "Interval Start", "Cost"), 1.5+ emits snake_case
# ("object_id", "interval_start", "cost"). Reading only one spelling silently produces a
# report full of zeros, so every field is looked up through this alias table.
_COST_FIELD_ALIASES = {
    "object_id": ("object_id", "Object ID"),
    "description": ("description", "Description"),
    "environment": ("environment", "Environment"),
    "interval_start": ("interval_start", "Interval Start"),
    "resource": ("resource", "Resource"),
    "cost": ("cost", "Cost"),
}


def _row_field(row: Dict[str, Any], field: str) -> Any:
    """Read a billing-row field by its canonical name, whatever the client version calls it."""
    for alias in _COST_FIELD_ALIASES.get(field, (field,)):
        if alias in row:
            return row[alias]
    return None


def _row_label(row: Dict[str, Any]) -> str:
    """Human-facing name for a billing row: the app description, else its object id."""
    return _row_field(row, "description") or _row_field(row, "object_id") or "(unknown)"


def group_costs(
    rows: List[Dict[str, Any]], key: str, top_n: int
) -> Tuple[List[Dict[str, Any]], Decimal, int]:
    """Sum costs by `key` ("app", "environment", "resource" or "interval_start").

    Returns (groups, total, omitted). Groups are sorted most-expensive first, carry a
    share of the total, and are cut to `top_n` — the total still reflects every row, so
    the caller can see how much the visible rows account for.
    """
    totals: Dict[str, Decimal] = {}
    for row in rows:
        if key == "app":
            label = _row_label(row)
        else:
            label = str(_row_field(row, key) or "(none)")
        totals[label] = totals.get(label, Decimal(0)) + _to_cost(_row_field(row, "cost"))

    total = sum(totals.values(), Decimal(0))
    ordered = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    shown = ordered[:top_n] if top_n > 0 else ordered
    groups = [
        {
            "name": name,
            "cost": _fmt_cost(cost),
            "share_pct": (
                float((cost / total * 100).quantize(Decimal("0.1"))) if total else 0.0
            ),
        }
        for name, cost in shown
    ]
    return groups, total, len(ordered) - len(shown)


def cost_movers(
    rows: List[Dict[str, Any]], top_n: int
) -> Optional[Dict[str, Any]]:
    """Explain the most expensive interval by diffing it against the one before.

    This is what answers "why was Monday expensive?": find the peak interval, then rank
    apps by how much MORE they cost then than in the preceding interval. Returns None
    when there are fewer than two intervals to compare.
    """
    per_interval: Dict[str, Dict[str, Decimal]] = {}
    for row in rows:
        interval = str(_row_field(row, "interval_start") or "")
        if not interval:
            continue
        per_interval.setdefault(interval, {})
        label = _row_label(row)
        bucket = per_interval[interval]
        bucket[label] = bucket.get(label, Decimal(0)) + _to_cost(_row_field(row, "cost"))

    intervals = sorted(per_interval)
    if len(intervals) < 2:
        return None

    totals = {i: sum(per_interval[i].values(), Decimal(0)) for i in intervals}
    peak = max(intervals, key=lambda i: totals[i])
    peak_index = intervals.index(peak)
    if peak_index == 0:
        # The peak is the first interval, so there is nothing earlier to compare it
        # with; fall back to the second interval so the diff is still meaningful.
        peak, peak_index = intervals[1], 1
    previous = intervals[peak_index - 1]

    deltas = []
    names = set(per_interval[peak]) | set(per_interval[previous])
    for name in names:
        now = per_interval[peak].get(name, Decimal(0))
        before = per_interval[previous].get(name, Decimal(0))
        deltas.append((name, now, before, now - before))
    deltas.sort(key=lambda d: d[3], reverse=True)

    return {
        "peak_interval": peak,
        "peak_cost": _fmt_cost(totals[peak]),
        "compared_with": previous,
        "compared_cost": _fmt_cost(totals[previous]),
        "change": _fmt_cost(totals[peak] - totals[previous]),
        "movers": [
            {
                "name": name,
                "cost": _fmt_cost(now),
                "previous_cost": _fmt_cost(before),
                "change": _fmt_cost(delta),
            }
            for name, now, before, delta in deltas[:top_n]
            if delta != 0
        ],
    }
