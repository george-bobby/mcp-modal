"""Shaping CLI output into the standard response envelope, within a budget.

Two concerns live here. **Caps**: the streaming runner is bounded by time, not volume, so
text fields are trimmed to a character budget before they reach the client. **Envelopes**:
`handle_json_response`, `standardize_result` and `json_listing` give every tool the same
response shape. Cap at the response boundary only — never before parsing or searching.
"""
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .app import logger
from .command import run_modal_command

# Matches http(s) URLs in CLI output so we can surface deployment / web-endpoint links.
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


# ---------------------------------------------------------------------------
# Output caps
# ---------------------------------------------------------------------------
# The streaming runner is bounded by *time*, not by volume: a chatty app can emit
# megabytes of logs inside a 30s window, and every byte would otherwise land in the
# client's context in one tool result. Text fields are therefore capped at a character
# budget (head + tail, so both the start of a run and the traceback at the end survive),
# and JSON listings are capped by item count.

_MAX_OUTPUT_ENV = "MCP_MODAL_MAX_OUTPUT_CHARS"
_DEFAULT_MAX_OUTPUT_CHARS = 40000  # ~10k tokens: big enough to debug, small enough to fit
_MIN_MAX_OUTPUT_CHARS = 1000  # a smaller budget can't hold the marker plus useful context
_MAX_LIST_ITEMS = 200  # per JSON listing (apps, containers, volume entries, ...)


def _max_output_chars() -> int:
    """Character budget for a single text field. 0 means uncapped.

    Operators can raise/lower it with MCP_MODAL_MAX_OUTPUT_CHARS, or set it to 0 to opt
    out entirely (the pre-0.3 behavior). A garbage value falls back to the default rather
    than failing the call.
    """
    raw = os.environ.get(_MAX_OUTPUT_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_MAX_OUTPUT_CHARS
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r", _MAX_OUTPUT_ENV, raw)
        return _DEFAULT_MAX_OUTPUT_CHARS
    if value <= 0:
        return 0
    return max(value, _MIN_MAX_OUTPUT_CHARS)


def _cap_text(text: Optional[str]) -> Tuple[Optional[str], bool]:
    """Cap `text` to the output budget, returning (text, was_capped).

    Keeps a head and a tail slice with an explanatory marker between them, snapping both
    cuts to line boundaries where possible so log lines aren't sliced mid-line.
    """
    limit = _max_output_chars()
    if not text or limit == 0 or len(text) <= limit:
        return text, False

    head_budget = int(limit * 0.6)
    tail_budget = limit - head_budget
    head = text[:head_budget]
    tail = text[-tail_budget:]
    # Snap to line boundaries, but only if that doesn't throw away most of the slice.
    cut = head.rfind("\n")
    if cut > head_budget // 2:
        head = head[:cut]
    cut = tail.find("\n")
    if 0 <= cut < tail_budget // 2:
        tail = tail[cut + 1:]

    omitted = len(text) - len(head) - len(tail)
    marker = (
        f"\n\n... [mcp-modal omitted {omitted} of {len(text)} characters to protect the "
        f"client's context. Narrow the query (tail / since / source / search), or raise "
        f"{_MAX_OUTPUT_ENV} (0 disables the cap).] ...\n\n"
    )
    return head + marker + tail, True


def _cap_items(data: Any) -> Tuple[Any, int]:
    """Cap a JSON listing to _MAX_LIST_ITEMS entries, returning (data, omitted_count)."""
    if isinstance(data, list) and len(data) > _MAX_LIST_ITEMS:
        return data[:_MAX_LIST_ITEMS], len(data) - _MAX_LIST_ITEMS
    return data, 0


def _add_capped(response: Dict[str, Any], key: str, text: Optional[str]) -> Dict[str, Any]:
    """Set `response[key]` to the capped `text` (when non-empty) and flag any capping."""
    capped, was_capped = _cap_text(text)
    if capped:
        response[key] = capped
    if was_capped:
        response["output_capped"] = True
    return response


def extract_urls(*texts: Optional[str]) -> List[str]:
    """Collect unique http(s) URLs from CLI output (deployment / web-endpoint links)."""
    urls: List[str] = []
    for text in texts:
        if not text:
            continue
        for match in _URL_RE.findall(text):
            cleaned = match.rstrip(").,")
            if cleaned not in urls:
                urls.append(cleaned)
    return urls


def handle_json_response(result: Dict[str, Any], error_prefix: str) -> Dict[str, Any]:
    """Parse JSON CLI output into a standardized success/error response."""
    if not result["success"]:
        response = {"success": False, "error": f"{error_prefix}: {result.get('error', 'Unknown error')}"}
        _add_capped(response, "stdout", result.get("stdout"))
        _add_capped(response, "stderr", result.get("stderr"))
        return response

    try:
        data = json.loads(result["stdout"])
        return {"success": True, "data": data}
    except json.JSONDecodeError as e:
        response = {"success": False, "error": f"Failed to parse JSON output: {str(e)}"}
        _add_capped(response, "stdout", result.get("stdout"))
        _add_capped(response, "stderr", result.get("stderr"))
        return response


def standardize_result(
    result: Dict[str, Any], success_message: str, error_prefix: str
) -> Dict[str, Any]:
    """Build a uniform response for non-JSON action commands (stop, create, rm, ...)."""
    response: Dict[str, Any] = {"success": result["success"], "command": result["command"]}
    if not result["success"]:
        response["error"] = f"{error_prefix}: {result.get('error', 'Unknown error')}"
    else:
        response["message"] = success_message
    _add_capped(response, "stdout", result.get("stdout"))
    _add_capped(response, "stderr", result.get("stderr"))
    return response


def json_listing(
    command: List[str], key: str, error_prefix: str, profile: Optional[str] = None, **extra: Any
) -> Dict[str, Any]:
    """Run a `--json` listing command and return {success, <key>: [...]}.

    `profile` selects the Modal profile for this call only (via MODAL_PROFILE); the
    stored active profile is never changed.

    Long listings are capped at _MAX_LIST_ITEMS entries, with `omitted_items` reporting
    how many were dropped so the caller knows the view is partial.
    """
    result = run_modal_command(command, profile=profile)
    response = handle_json_response(result, error_prefix)
    if not response["success"]:
        return response
    data, omitted = _cap_items(response["data"])
    out: Dict[str, Any] = {"success": True, key: data, **extra}
    if omitted:
        out["omitted_items"] = omitted
        out["message"] = (
            f"Showing the first {_MAX_LIST_ITEMS} entries; {omitted} more were omitted."
        )
    return out
