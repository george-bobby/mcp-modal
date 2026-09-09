"""`analyze_modal_costs` — fetch the billing report once, aggregate it locally.

Billing is workspace-wide: `modal billing report` accepts no `-e/--env`, so the
environment arrives as a row field and is filtered here rather than passed to the CLI.
"""
from typing import Any, Dict, Optional

from ..app import logger, mcp, _read_only
from ..billing import _fmt_cost, _row_field, cost_movers, group_costs
from ..command import run_modal_command
from ..output import handle_json_response


_COST_VIEWS = ("by_app", "timeline", "by_environment", "by_resource", "summary", "rates")


@mcp.tool(annotations=_read_only("Analyze Modal costs"))
async def analyze_modal_costs(
    view: str = "by_app",
    period: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    resolution: str = "d",
    timezone: Optional[str] = None,
    app: Optional[str] = None,
    environment: Optional[str] = None,
    top_n: int = 10,
    tag_names: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Break down what the workspace is spending (`modal billing`). Costs are fetched once
    and aggregated locally, so you get ranked totals and period-over-period changes
    rather than hundreds of raw rows.

    Answering common questions:
      "what is my costliest app?"     -> view="by_app", period="this month"
      "why was Monday expensive?"     -> view="timeline", period="last week" (the
                                         `explanation` field diffs the peak day against
                                         the day before and ranks which apps grew)
      "what did that day cost hourly?" -> view="timeline", start="2026-08-31",
                                          end="2026-09-01", resolution="h"
      "where does the money go?"      -> view="by_resource" (CPU / GPU / memory / ...)
      "what is the bill this cycle?"  -> view="summary"

    Billing is workspace-wide, so this reports across every environment; use
    `environment` to narrow it after the fact.

    Args:
        view: "by_app" (default), "timeline" (per interval, with an explanation of the
            peak), "by_environment", "by_resource", "summary" (billed vs metered for a
            month cycle), or "rates" (current unit prices).
        period: Convenience range — "today", "yesterday", "this week", "last week",
            "this month", "last month". For "summary" also accepts "YYYY-MM".
        start / end: Explicit range instead of `period` — ISO dates ("2026-08-31") or
            relative ("3 days ago"). Start is inclusive, end exclusive; end defaults to now.
        resolution: "d" (daily, default) or "h" (hourly). Hourly is what you want when
            drilling into a single day.
        timezone: Timezone for interpreting dates — "local", an offset ("+05:30"), or an
            IANA name. Requires resolution="h".
        app: Only include apps whose name or ID contains this string (case-insensitive).
        environment: Only include rows from this Modal environment.
        top_n: How many groups/movers to return. Default 10.
        tag_names: Comma-separated cost-attribution tag names to include.

    Returns: {total_cost, groups | intervals, explanation (for timeline), row_count}.
        Costs are strings of US dollars with 4 decimals. `total_cost` always covers every
        row in range, even when `groups` is cut to top_n.
    """
    if view not in _COST_VIEWS:
        return {
            "success": False,
            "error": f"Unknown view {view!r}. Valid values: {', '.join(_COST_VIEWS)}",
        }
    if resolution not in ("d", "h"):
        return {"success": False, "error": "resolution must be 'd' (daily) or 'h' (hourly)"}
    if timezone and resolution != "h":
        return {"success": False, "error": "timezone requires resolution='h' (Modal's own constraint)"}
    top_n = max(1, min(top_n, 200))

    try:
        if view == "rates":
            rates = run_modal_command(["modal", "billing", "rates", "--json"])
            if not rates["success"] and "No such command" in (rates.get("stderr") or ""):
                return {
                    "success": False,
                    "error": (
                        "This Modal client is too old for `billing rates` (it needs modal "
                        ">= 1.5). Upgrade modal, or use view='by_app'/'timeline'."
                    ),
                    "command": rates["command"],
                }
            parsed = handle_json_response(rates, "Failed to get rates")
            if not parsed["success"]:
                return parsed
            return {"success": True, "view": view, "rates": parsed["data"]}

        if view == "summary":
            command = ["modal", "billing", "summary", "--json"]
            if period:
                command.extend(["--for", period])
            result = run_modal_command(command)
            if not result["success"] and "No such command" in (result.get("stderr") or ""):
                # `modal billing summary` and `rates` arrived in client 1.5; older clients
                # only have `report`, and fail with a bare usage error.
                return {
                    "success": False,
                    "error": (
                        "This Modal client is too old for `billing summary` (it needs modal "
                        ">= 1.5). Upgrade modal, or use view='by_app'/'timeline', which work "
                        "on every supported client."
                    ),
                    "command": result["command"],
                }
            response = handle_json_response(result, "Failed to get billing summary")
            if not response["success"]:
                return response
            return {"success": True, "view": view, "period": period or "this month", "summary": response["data"]}

        command = ["modal", "billing", "report", "--json", "-r", resolution]
        if period:
            command.extend(["--for", period])
        if start:
            command.extend(["--start", start])
        if end:
            command.extend(["--end", end])
        if timezone:
            command.extend(["--tz", timezone])
        if tag_names:
            command.extend(["--tag-names", tag_names])
        if view == "by_resource":
            # The resource column (CPU / GPU type / memory / ...) only exists with this flag.
            command.append("--show-resources")

        result = run_modal_command(command)
        response = handle_json_response(result, "Failed to get billing report")
        if not response["success"]:
            return response

        rows = response["data"]
        if not isinstance(rows, list):
            return {"success": False, "error": "Unexpected billing report shape (expected a list of rows)"}

        matched = len(rows)
        if app:
            needle = app.lower()
            rows = [
                r for r in rows
                if needle in str(_row_field(r, "description") or "").lower()
                or needle in str(_row_field(r, "object_id") or "").lower()
            ]
        if environment:
            rows = [r for r in rows if str(_row_field(r, "environment") or "") == environment]

        out: Dict[str, Any] = {
            "success": True,
            "view": view,
            "range": period or {"start": start, "end": end},
            "resolution": resolution,
            "row_count": len(rows),
            "command": result["command"],
        }
        if (app or environment) and len(rows) != matched:
            out["rows_before_filter"] = matched
        if not rows:
            out["total_cost"] = "0.0000"
            out["groups"] = []
            out["message"] = (
                "No billing rows in this range"
                + (" after filtering" if app or environment else "")
                + ". Widen the range with `period`/`start`, or drop the filters. Note that "
                "Modal reports full intervals only, so a range shorter than one interval is empty."
            )
            return out

        if view == "timeline":
            groups, total, omitted = group_costs(rows, "interval_start", top_n=0)
            # Timeline reads chronologically, not by size.
            groups.sort(key=lambda g: g["name"])
            out["intervals"] = groups
            out["total_cost"] = _fmt_cost(total)
            explanation = cost_movers(rows, top_n)
            if explanation:
                out["explanation"] = explanation
                out["message"] = (
                    f"Most expensive interval: {explanation['peak_interval']} at "
                    f"${explanation['peak_cost']} ({explanation['change']} vs "
                    f"{explanation['compared_with']}). `explanation.movers` ranks the apps "
                    "by how much they grew between those two intervals."
                )
            else:
                out["message"] = (
                    "Only one interval in range, so there is nothing to compare it against. "
                    "Widen `period`, or use resolution='h' to break a single day into hours."
                )
            return out

        key = {"by_app": "app", "by_environment": "environment", "by_resource": "resource"}[view]
        groups, total, omitted = group_costs(rows, key, top_n)
        out["groups"] = groups
        out["total_cost"] = _fmt_cost(total)
        if omitted:
            out["omitted_groups"] = omitted
            out["message"] = (
                f"Showing the top {len(groups)} of {len(groups) + omitted}; total_cost covers all of them."
            )
        return out
    except Exception as e:
        logger.error(f"Failed to analyze Modal costs: {e}")
        raise
