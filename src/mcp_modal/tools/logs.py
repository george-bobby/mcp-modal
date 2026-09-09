"""The log tools: fetch a window (`get_modal_logs`) or grep one (`search_modal_logs`).

Both take either an app or a container identifier. Volume, not matching, is the practical
failure mode here — see the `since`/`until`/`prefilter` notes in the docstrings.
"""
from typing import Any, Dict, Optional

from ..app import logger, mcp, _read_only
from ..command import _add_env, run_modal_streaming_command
from ..logsearch import (
    _MAX_SCAN_WIDTH,
    _check_log_window,
    _log_failure_error,
    _resolve_log_target,
    cap_blocks,
    filter_log_lines,
    grep_lines,
)
from ..output import _add_capped


@mcp.tool(annotations=_read_only("Read Modal logs"))
async def get_modal_logs(
    identifier: str,
    target: str = "auto",
    timeout_seconds: int = 30,
    env: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tail: Optional[int] = None,
    source: Optional[str] = None,
    timestamps: bool = False,
    follow: bool = False,
) -> Dict[str, Any]:
    """
    Fetch logs for an app or container (`modal app logs` / `modal container logs`).
    To find where something went wrong, prefer search_modal_logs — it returns matches
    with surrounding context instead of a raw tail.

    Covers the stdout/stderr/system streams ONLY. Crash events shown on the Modal
    dashboard (e.g. "... exited with ...") are not log lines and never appear here.

    Args:
        identifier: App name/ID ("my-app", "ap-...") or container ID ("ta-...").
        target: "auto" (default — "ta-..." is a container), "app", or "container".
        timeout_seconds: Max seconds to collect. Default 30.
        env: Modal environment. Apps only — container logs take no environment.
        since / until: Time range, ISO 8601 or relative ("2h", "30m", "1d"). Max 35 days.
            `since` without `tail` fetches EVERY entry in the range — pass `until` too
            (or a `tail`) to bound the volume on a busy app.
        tail: Only the last N entries (max 20000).
        source: "stdout", "stderr", or "system".
        timestamps: Prefix each line with its wall-clock timestamp.
        follow: Live-stream until the app/container stops or the timeout hits.

    Returns: {logs, truncated (still streaming at the timeout), output_capped (text
    trimmed to fit context — narrow with tail/since/source)}.
    """
    if target not in ("auto", "app", "container"):
        return {"success": False, "error": "target must be 'auto', 'app', or 'container'"}
    window_error = _check_log_window(tail)
    if window_error:
        return {"success": False, "error": window_error}
    if follow and (since or until or tail):
        return {
            "success": False,
            "error": "follow=True cannot be combined with since, until, or tail — "
                     "follow streams live output, the others fetch a past window",
        }
    resolved = _resolve_log_target(identifier, target)
    try:
        command = ["modal", "app" if resolved == "app" else "container", "logs"]
        if follow:
            command.append("-f")
        if timestamps:
            command.append("--timestamps")
        if since:
            command.extend(["--since", since])
        if until:
            command.extend(["--until", until])
        if tail is not None:
            command.extend(["--tail", str(tail)])
        if source:
            command.extend(["--source", source])
        if resolved == "app":
            _add_env(command, env)
        command.extend(["--", identifier])

        result = run_modal_streaming_command(command, timeout_seconds)

        # A non-zero, non-timeout exit means a genuine failure (unknown app, auth error).
        # A SIGTERM/SIGKILL from our timeout produces a negative return code, which is
        # expected when we cut off a live stream.
        failed = result["returncode"] not in (0, None) and not result["timed_out"]
        if failed:
            response = {
                "success": False,
                "error": _log_failure_error(identifier, result, "Failed to get logs"),
                "target": resolved,
                "command": result["command"],
            }
            _add_capped(response, "stdout", result["stdout"])
            _add_capped(response, "stderr", result["stderr"])
            return response

        response = {
            "success": True,
            "target": resolved,
            "identifier": identifier,
            "truncated": result["timed_out"],
            "command": result["command"],
        }
        _add_capped(response, "logs", result["stdout"])
        if result["timed_out"]:
            response["message"] = (
                f"{resolved.capitalize()} is still active and streaming; returning a "
                f"{timeout_seconds}s snapshot. Increase timeout_seconds for more."
            )
        _add_capped(response, "stderr", result["stderr"])
        return response
    except Exception as e:
        logger.error(f"Failed to get logs for '{identifier}': {e}")
        raise


@mcp.tool(annotations=_read_only("Search Modal logs"))
async def search_modal_logs(
    identifier: str,
    pattern: str,
    target: str = "auto",
    regex: bool = False,
    case_sensitive: bool = False,
    context_lines: int = 3,
    max_matches: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tail: Optional[int] = None,
    source: Optional[str] = None,
    exclude: Optional[str] = None,
    prefilter: bool = False,
    timestamps: bool = True,
    timeout_seconds: int = 30,
    env: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Search an app's or container's logs and return each hit WITH surrounding context —
    the fastest way to find a traceback, an error or a request ID. Logs are fetched once
    and grepped locally, so you get the lines around each match, not just the match.

    Covers the stdout/stderr/system streams ONLY. Crash events shown on the Modal
    dashboard (e.g. "... exited with ...") are not log lines, so a search for them
    returns 0 matches even though the failure is real — check the dashboard instead.

    Args:
        identifier: App name/ID ("my-app", "ap-...") or container ID ("ta-...").
        pattern: Text to find, or a Python regex when regex=True.
        target: "auto" (default — "ta-..." is a container), "app", or "container".
        regex / case_sensitive: Match mode. Both default False.
        context_lines: Lines of context each side of a match. Default 3.
        max_matches: Cap on match blocks returned. Default 50.
        since / until: Time range, ISO 8601 or relative ("2h", "30m", "1d"). PREFER a
            bounded range (both ends) when you know roughly when something happened —
            `since` alone fetches every entry from then until now, which on a busy app
            is megabytes and gets cut off at the timeout. Range must be <= 35 days.
        tail: Search only the last N entries (max 20000) instead of a whole range.
            With no since/until/tail, defaults to the last 1000 entries.
        source: Search only "stdout", "stderr", or "system".
        exclude: Drop lines matching this BEFORE searching, to strip repeated noise.
        prefilter: Push `pattern` down to Modal as a server-side substring filter, so
            non-matching lines are never fetched. The big lever for huge logs, but it
            requires regex=False and leaves `context_lines` showing only other matching
            lines — use it to find *where* something is, then re-query that window.
        timestamps: Prefix lines with their timestamp. Default True.
        timeout_seconds: Max seconds spent fetching logs. Default 30.
        env: Modal environment (apps only).

    Returns: {match_count (exact, whole log searched), returned (matches actually shown),
    returned_blocks, matches (context blocks, matched lines prefixed ">"), excluded_lines,
    output_capped}.
    """
    if target not in ("auto", "app", "container"):
        return {"success": False, "error": "target must be 'auto', 'app', or 'container'"}
    if not pattern:
        return {"success": False, "error": "A non-empty search pattern is required"}
    if len(pattern) > _MAX_SCAN_WIDTH:
        return {"success": False, "error": f"Pattern too long (max {_MAX_SCAN_WIDTH} chars)"}
    if source is not None and source not in ("stdout", "stderr", "system"):
        return {"success": False, "error": "source must be 'stdout', 'stderr', or 'system'"}
    if prefilter and regex:
        return {
            "success": False,
            "error": "prefilter=True needs a literal pattern (regex=False) — Modal's "
                     "server-side --search filter does substring matching, not regex",
        }
    window_error = _check_log_window(tail)
    if window_error:
        return {"success": False, "error": window_error}
    # Clamp to sane bounds so a huge value can't blow up memory or output size.
    context_lines = max(0, min(context_lines, 100))
    max_matches = max(1, min(max_matches, 1000))
    resolved = _resolve_log_target(identifier, target)
    try:
        command = ["modal", "app" if resolved == "app" else "container", "logs"]
        if timestamps:
            command.append("--timestamps")
        if source:
            command.extend(["--source", source])
        if prefilter:
            # Server-side substring filter: the lines never leave Modal, so a chatty app
            # stops blowing the fetch timeout and the output budget.
            command.extend(["--search", pattern])
        if since:
            command.extend(["--since", since])
        if until:
            command.extend(["--until", until])
        if tail is not None:
            command.extend(["--tail", str(tail)])
        if since is None and tail is None:
            # Search a generous window by default so debugging isn't limited to ~100 lines.
            # (`until` alone still means tail mode, anchored at that instant.)
            command.extend(["--tail", "1000"])
        if resolved == "app":
            _add_env(command, env)
        command.extend(["--", identifier])

        result = run_modal_streaming_command(command, timeout_seconds)
        failed = result["returncode"] not in (0, None) and not result["timed_out"]
        if failed:
            response = {
                "success": False,
                "error": _log_failure_error(identifier, result, "Failed to fetch logs"),
                "command": result["command"],
            }
            _add_capped(response, "stderr", result["stderr"])
            return response

        # Modal writes log lines to stdout; some builds emit them on stderr — search both.
        # The fetched text is searched in full (uncapped): capping before the grep would
        # silently hide matches, so only the returned blocks are budget-limited.
        log_text = result["stdout"] or result["stderr"] or ""
        excluded_lines = 0
        if exclude:
            log_text, excluded_lines = filter_log_lines(
                log_text, exclude, regex, case_sensitive
            )
            if log_text is None:
                # filter_log_lines returned an error message (e.g. bad regex) in the count slot.
                return {"success": False, "error": excluded_lines, "command": result["command"]}

        total, blocks = grep_lines(
            log_text, pattern, regex, case_sensitive, context_lines, max_matches
        )
        if total is None:
            # grep_lines returned an error message (e.g. bad regex) in `blocks`.
            return {"success": False, "error": blocks, "command": result["command"]}

        blocks, dropped = cap_blocks(blocks)
        # Adjacent match windows are merged, so one block can carry many matches — count the
        # matched lines actually returned instead of the blocks, or a single merged block
        # reads as "showing 1 of 156" and invites a pointless wider re-query.
        shown = sum(
            1 for block in blocks for line in block.splitlines() if line.startswith(">")
        )
        response = {
            "success": True,
            "target": resolved,
            "identifier": identifier,
            "pattern": pattern,
            "match_count": total,
            "returned": shown,
            "returned_blocks": len(blocks),
            "matches": blocks,
            "logs_truncated": result["timed_out"],
            "command": result["command"],
        }
        if prefilter:
            # Context lines came from an already-filtered stream, so say so rather than
            # letting the caller read them as the app's surrounding output.
            response["prefiltered"] = True
            response["note"] = (
                "Fetched with Modal's server-side --search filter: non-matching lines were "
                "never fetched, so context lines show only other matches. Re-query the "
                "window you care about with prefilter=False to see real context."
            )
        if dropped:
            response["output_capped"] = True
        if exclude:
            response["excluded_lines"] = excluded_lines
        if total == 0:
            response["message"] = (
                f"No matches for {pattern!r} in the fetched logs. Try a broader pattern, "
                "set regex=True, or move the window (`since` + `until`, or a larger `tail`) "
                "— widen the window, don't unbound it. Note that some failures "
                "(e.g. crashes reported as '... exited with ...') are Modal dashboard "
                "events, not log lines, and will never match here — check the dashboard."
            )
        elif shown < total:
            hidden = total - shown
            reason = (
                f"{dropped} block(s) were dropped to fit the output budget"
                if dropped
                else "increase max_matches for more"
            )
            response["message"] = (
                f"Showing {shown} of {total} matches in {len(blocks)} block(s) "
                f"({hidden} not shown); {reason}. "
                "Narrowing gives sharper results: bound the window with `since` AND `until`, "
                "or use `source` / `exclude` / a stricter `pattern`. Do NOT retry wider."
            )
        if result["timed_out"]:
            response.setdefault("message", "")
            response["message"] = (
                (response["message"] + " ").lstrip()
                + f"Log fetch was cut off at {timeout_seconds}s, so older entries may be missing. "
                + (
                    "This app produces more logs than a "
                    f"{timeout_seconds}s fetch can drain: bound the window with `since` AND "
                    "`until` around the time you care about, or set prefilter=True to filter "
                    "server-side, rather than raising timeout_seconds."
                )
            )
        return response
    except Exception as e:
        logger.error(f"Failed to search logs for '{identifier}': {e}")
        raise
