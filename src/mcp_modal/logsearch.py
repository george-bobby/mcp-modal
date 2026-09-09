"""Pure log-text logic: grep with context, noise filtering, and window checks.

The log tools fetch once and then do everything locally in here, so this module never
touches the network or the CLI — it takes text and arguments and returns text.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from .output import _cap_text, _max_output_chars


# Longest slice of any single line actually fed to the regex engine. Caller-supplied
# regexes can backtrack catastrophically on long inputs; matching line-by-line against a
# bounded slice keeps worst-case work finite. The full line is still shown in output.
_MAX_SCAN_WIDTH = 16384


def grep_lines(
    text: str,
    pattern: str,
    regex: bool,
    case_sensitive: bool,
    context_lines: int,
    max_matches: int,
) -> Any:
    """Grep `text` line-by-line, returning (total_matches, blocks) or (None, error_message).

    Each block is a chunk of log text covering one or more matches and `context_lines`
    of surrounding context. Matched lines are prefixed with ">", context lines with " ",
    and every line is given its 1-based line number — grep `-C` style. Overlapping or
    adjacent match windows are merged into a single block to avoid repeating lines.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error as e:
        return None, f"Invalid regex pattern: {e}"

    lines = text.splitlines()
    match_indices = [
        i for i, line in enumerate(lines) if compiled.search(line[:_MAX_SCAN_WIDTH])
    ]
    total = len(match_indices)
    shown = match_indices[:max_matches]
    matched = set(match_indices)  # mark every real match, even inside another's window

    # Merge each shown match's [i-ctx, i+ctx] window into non-overlapping intervals.
    intervals: List[List[int]] = []
    for i in shown:
        lo = max(0, i - context_lines)
        hi = min(len(lines) - 1, i + context_lines)
        if intervals and lo <= intervals[-1][1] + 1:
            intervals[-1][1] = max(intervals[-1][1], hi)
        else:
            intervals.append([lo, hi])

    blocks: List[str] = []
    for lo, hi in intervals:
        block = [
            f"{'>' if n in matched else ' '} {n + 1}: {lines[n]}"
            for n in range(lo, hi + 1)
        ]
        blocks.append("\n".join(block))
    return total, blocks


def filter_log_lines(
    text: str, exclude: str, regex: bool, case_sensitive: bool
) -> Any:
    """Drop lines matching `exclude` from `text`, returning (filtered_text, removed_count).

    Used to strip high-volume noise (e.g. repeated "queue put failed" spam) before
    grepping for the real signal. Returns (None, error_message) on a bad regex.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(exclude if regex else re.escape(exclude), flags)
    except re.error as e:
        return None, f"Invalid exclude pattern: {e}"

    lines = text.splitlines()
    kept = [line for line in lines if not compiled.search(line[:_MAX_SCAN_WIDTH])]
    return "\n".join(kept), len(lines) - len(kept)


def cap_blocks(blocks: List[str]) -> Tuple[List[str], int]:
    """Cap a list of grep context blocks to the output budget.

    Whole blocks are dropped from the end rather than sliced, so every returned block
    stays readable. Returns (kept_blocks, dropped_count).
    """
    limit = _max_output_chars()
    if limit == 0:
        return blocks, 0
    kept: List[str] = []
    used = 0
    for block in blocks:
        cost = len(block) + 1
        if kept and used + cost > limit:
            break
        # Always keep at least one block, capping it if it alone busts the budget.
        if not kept and cost > limit:
            block = _cap_text(block)[0] or block
            cost = len(block) + 1
        kept.append(block)
        used += cost
    return kept, len(blocks) - len(kept)


# The log CLI enforces these itself, but as UsageErrors (exit 2) — checking locally turns
# "Failed to fetch logs (exit 2)" into an answer the caller can act on, without a round trip.
_MAX_LOG_TAIL = 20000  # modal._logs._FETCH_LIMIT
_MAX_LOG_RANGE_DAYS = 35  # modal._logs._MAX_FETCH_RANGE


def _check_log_window(tail: Optional[int]) -> Optional[str]:
    """Return an error message if the log window args will be rejected by the CLI."""
    if tail is not None and tail <= 0:
        return "tail must be a positive number of entries"
    if tail is not None and tail > _MAX_LOG_TAIL:
        return f"tail must not exceed {_MAX_LOG_TAIL} entries (Modal's per-fetch limit)"
    return None


def _log_failure_error(identifier: str, result: Dict[str, Any], verb: str) -> str:
    """Build the error string for a failed log fetch, preferring the CLI's own message.

    `modal ... logs` rejects a bad window with a UsageError on stderr ("--since must be
    before --until", "Log fetch time range cannot exceed 35 days") and exit 2. Surfacing
    that beats reporting the exit code, which tells the caller nothing about the fix.
    """
    stderr = (result.get("stderr") or "").strip()
    for line in reversed(stderr.splitlines()):
        line = line.strip()
        if line.startswith("Error: "):
            return f"{verb} for '{identifier}': {line[len('Error: '):]}"
    return f"{verb} for '{identifier}' (exit {result['returncode']})"


def _resolve_log_target(identifier: str, target: str) -> str:
    """Resolve target="auto" to "app" or "container" from the identifier's ID prefix."""
    if target != "auto":
        return target
    return "container" if identifier.startswith("ta-") else "app"
