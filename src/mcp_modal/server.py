"""MCP server for managing Modal apps, containers, volumes, and secrets.

All tools shell out to the local `modal` CLI, so they use whatever Modal profile /
credentials are configured on the host (`~/.modal.toml`). Account-scoped operations
(apps, containers, volumes, secrets, profiles, environments) run the plain `modal`
binary; operations that build/deploy/run a local project (`deploy`, `run`) wrap the
command in `uv run --directory=<project>` so the project's own virtualenv is used.

Three cross-cutting behaviors are worth knowing about:

* **Grouped tools.** Every tool schema is loaded into the client's context for the whole
  session, so related operations are grouped behind an `action`/`resource` argument
  rather than exposed as one tool per CLI subcommand. Read-only lookups live in
  `list_modal_resources`; mutations live in the `manage_*` tools.
* **Tool annotations.** Every tool declares MCP annotations (readOnlyHint /
  destructiveHint / idempotentHint), so a client can auto-approve safe lookups while
  still prompting for a volume delete or a container exec.
* **Output caps.** Log/exec/run output is capped (see MCP_MODAL_MAX_OUTPUT_CHARS) before
  it is returned, so a chatty app can't flood the caller's context window.
"""
import logging
import os
import re
import signal
from typing import Any, Optional, List, Dict, Tuple
from decimal import Decimal, InvalidOperation
import subprocess
import json

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

logger = logging.getLogger(__name__)

mcp = FastMCP("modal-deploy")

# Matches http(s) URLs in CLI output so we can surface deployment / web-endpoint links.
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


# ---------------------------------------------------------------------------
# Tool annotations
# ---------------------------------------------------------------------------
# MCP clients use these hints to decide what can be auto-approved and what needs a
# human in the loop. Only readOnlyHint is a safety-relevant promise we can actually
# keep (the tool runs no mutating CLI subcommand); the rest are advisory.

def _read_only(title: str) -> ToolAnnotations:
    """Annotations for a tool that only reads state (safe to auto-approve/retry)."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )


def _mutating(title: str, destructive: bool = True, idempotent: bool = False) -> ToolAnnotations:
    """Annotations for a tool that changes remote state.

    `destructive=True` means a call can remove or overwrite something a user would miss
    (delete a volume, stop a live app); `destructive=False` marks additive/no-op-on-repeat
    changes. Both are conservative defaults — when in doubt a tool is marked destructive
    so clients prompt rather than auto-run it.
    """
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=True,
    )


def _uv_prefixed(command: List[str], uv_directory: Optional[str]) -> List[str]:
    """Prefix a command with `uv run --directory=<dir>` when a project dir is given.

    Deploying/running a Modal app requires the app's own uv virtualenv, so those
    commands must run through `uv`. Account-scoped commands pass uv_directory=None.
    """
    if uv_directory:
        return ["uv", "run", f"--directory={uv_directory}"] + command
    return command


def _add_env(command: List[str], env: Optional[str]) -> List[str]:
    """Append `-e <env>` to target a specific Modal environment, if provided.

    Only call this for subcommands that actually accept `-e/--env`. Notably `modal
    container logs|exec|stop` do NOT (only `modal container list` does), because a
    container ID is already globally unique.
    """
    if env:
        command.extend(["-e", env])
    return command


def _redact_text(text: Optional[str], secrets: Optional[List[str]]) -> Optional[str]:
    """Replace every occurrence of each secret value in `text` with "***".

    Used so secret values passed to `modal secret create` never surface in the logged
    command, the echoed `command` field, or any captured stdout/stderr/error. Empty
    values are skipped (replacing "" would corrupt the whole string); all other values
    are redacted regardless of length — over-redaction is safe, under-redaction is not.
    """
    if not text or not secrets:
        return text
    # Longest first: if one value is a substring of another ("dummy" inside "dummy2"),
    # replacing the short one first leaves the remainder ("***2") visible in the output.
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


# Opt-in allowlist for the LOCAL paths that volume put/get may read from / write to.
# Unset (the default) means no restriction — fully backward compatible. When set to an
# os.pathsep-separated list of directories, a local path must resolve inside one of them
# or the operation is refused. This blunts the confused-deputy risk where a prompt-injected
# client uses put (to exfiltrate ~/.ssh/id_rsa) or get --force (to overwrite ~/.zshrc).
_ALLOWED_PATHS_ENV = "MCP_MODAL_ALLOWED_LOCAL_PATHS"


def _allowed_local_roots() -> Optional[List[str]]:
    """Parse the allowlist env var into resolved root dirs, or None if unset/empty."""
    raw = os.environ.get(_ALLOWED_PATHS_ENV)
    if not raw or not raw.strip():
        return None
    roots = [
        os.path.realpath(os.path.expanduser(p))
        for p in raw.split(os.pathsep)
        if p.strip()
    ]
    return roots or None


def _check_local_path(path: str) -> Optional[str]:
    """Return an error string if `path` is outside the allowlist, else None.

    No-op (returns None) when the allowlist is not configured, so behavior is unchanged
    unless an operator opts in via MCP_MODAL_ALLOWED_LOCAL_PATHS. realpath resolves both
    `..` traversal and symlinks before the prefix check.
    """
    roots = _allowed_local_roots()
    if roots is None:
        return None
    resolved = os.path.realpath(os.path.expanduser(path))
    for root in roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return None
    return (
        f"Local path {path!r} is outside the allowed roots configured in "
        f"{_ALLOWED_PATHS_ENV}. Allowed roots: {roots}"
    )


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


def run_modal_command(
    command: List[str],
    uv_directory: Optional[str] = None,
    redact: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run a Modal CLI command to completion and return the result.

    `redact`, if given, is a list of secret values scrubbed from the logged command and
    from every returned text field (command/stdout/stderr/error) — see _redact_text.

    Output is returned uncapped: callers that echo it to the client cap it via
    standardize_result / _add_capped, while callers that parse it (JSON listings) need
    the intact text.
    """
    try:
        command = _uv_prefixed(command, uv_directory)
        command_str = ' '.join(command)
        logger.info(f"Running command: {_redact_text(command_str, redact)}")
        # stdin is closed so a CLI that unexpectedly prompts (e.g. an auth flow on an
        # unconfigured host) fails fast on EOF instead of hanging this blocking call.
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        return {
            "success": True,
            "stdout": _redact_text(result.stdout, redact),
            "stderr": _redact_text(result.stderr, redact),
            "command": _redact_text(command_str, redact),
        }
    except subprocess.CalledProcessError as e:
        return {
            "success": False,
            "error": _redact_text(str(e), redact),
            "stdout": _redact_text(e.stdout, redact),
            "stderr": _redact_text(e.stderr, redact),
            "command": _redact_text(command_str, redact),
        }


def run_modal_streaming_command(
    command: List[str], timeout_seconds: int, uv_directory: Optional[str] = None
) -> Dict[str, Any]:
    """Run a Modal CLI command that may stream indefinitely (e.g. `modal app logs`, `modal serve`).

    Captures whatever output is produced within `timeout_seconds`. If the command is
    still running at the deadline (i.e. it was streaming), the whole process group is
    terminated and the partial output is returned with timed_out=True.
    """
    full_command = _uv_prefixed(command, uv_directory)
    proc = subprocess.Popen(
        full_command,
        stdin=subprocess.DEVNULL,  # never block on an unexpected interactive prompt
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # New session so `modal` (a possible grandchild under `uv run`) can be killed as a group.
        start_new_session=True,
    )
    logger.info(f"Running streaming command (timeout={timeout_seconds}s): {' '.join(full_command)}")
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()

    return {
        "stdout": stdout or "",
        "stderr": stderr or "",
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "command": ' '.join(full_command),
    }


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
    command: List[str], key: str, error_prefix: str, **extra: Any
) -> Dict[str, Any]:
    """Run a `--json` listing command and return {success, <key>: [...]}.

    Long listings are capped at _MAX_LIST_ITEMS entries, with `omitted_items` reporting
    how many were dropped so the caller knows the view is partial.
    """
    result = run_modal_command(command)
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


def _resolve_log_target(identifier: str, target: str) -> str:
    """Resolve target="auto" to "app" or "container" from the identifier's ID prefix."""
    if target != "auto":
        return target
    return "container" if identifier.startswith("ta-") else "app"


# ---------------------------------------------------------------------------
# Deploy & run (compute) — these wrap the command in the project's uv venv
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_mutating("Deploy a Modal app"))
async def deploy_modal_app(
    absolute_path_to_app: str,
    env: Optional[str] = None,
    name: Optional[str] = None,
    tag: Optional[str] = None,
    strategy: Optional[str] = None,
    stream_logs: bool = False,
) -> Dict[str, Any]:
    """
    Deploy a Modal app (`modal deploy`). Deployed endpoints persist after this returns,
    so any URLs in the result are live, shareable links.

    Args:
        absolute_path_to_app: Absolute path to the app file. Its directory must use `uv`
            and have `modal` installed in its virtualenv.
        env: Modal environment to deploy into.
        name: Deployment name (`--name`).
        tag: Version tag (`--tag`).
        strategy: Rollout strategy — "rolling" or "recreate".
        stream_logs: Stream the app's logs after deploying.

    Returns: {message, urls (live endpoints), stdout, stderr}.
    """
    uv_directory = os.path.dirname(absolute_path_to_app)
    app_name = os.path.basename(absolute_path_to_app)
    try:
        command = ["modal", "deploy"]
        if name:
            command.extend(["--name", name])
        if tag:
            command.extend(["--tag", tag])
        if strategy:
            command.extend(["--strategy", strategy])
        if stream_logs:
            command.append("--stream-logs")
        _add_env(command, env)
        # `--` ends option parsing so an app filename starting with `-` can't be
        # misread as a CLI flag (option injection).
        command.extend(["--", app_name])

        result = run_modal_command(command, uv_directory)
        # URLs are extracted from the full output before capping, so a link near the end
        # of a long deploy log is still surfaced even when the text itself is trimmed.
        urls = extract_urls(result.get("stdout"), result.get("stderr"))
        response = standardize_result(
            result, f"Successfully deployed {app_name}", "Failed to deploy app"
        )
        if urls:
            response["urls"] = urls
        return response
    except Exception as e:
        logger.error(f"Failed to deploy Modal app: {e}")
        raise


@mcp.tool(annotations=_mutating("Run a Modal function", destructive=False))
async def run_modal_app(
    absolute_path_to_app: str,
    function_name: Optional[str] = None,
    env: Optional[str] = None,
    detach: bool = False,
    timeout_seconds: int = 120,
) -> Dict[str, Any]:
    """
    Run a Modal function or local entrypoint once and collect its output (`modal run`).
    Use this to test on Modal compute; use deploy_modal_app to publish.

    Args:
        absolute_path_to_app: Absolute path to the app file. Its directory must use `uv`
            and have `modal` installed in its virtualenv.
        function_name: Function/entrypoint name, e.g. "main". Omit if the module has
            exactly one.
        env: Modal environment to target.
        detach: Keep the run alive on Modal past this call (`--detach`) — for long jobs.
        timeout_seconds: Max seconds to collect output. Default 120.

    Returns: {output, urls, truncated (still running at the timeout), output_capped}.
    """
    uv_directory = os.path.dirname(absolute_path_to_app)
    app_name = os.path.basename(absolute_path_to_app)
    func_ref = f"{app_name}::{function_name}" if function_name else app_name
    try:
        command = ["modal", "run"]
        if detach:
            command.append("--detach")
        _add_env(command, env)
        # `--` ends option parsing so a func ref starting with `-` can't be misread as a flag.
        command.extend(["--", func_ref])

        result = run_modal_streaming_command(command, timeout_seconds, uv_directory)
        failed = result["returncode"] not in (0, None) and not result["timed_out"]
        if failed:
            response = {
                "success": False,
                "error": f"Run failed for '{func_ref}' (exit {result['returncode']})",
                "command": result["command"],
            }
            _add_capped(response, "stdout", result["stdout"])
            _add_capped(response, "stderr", result["stderr"])
            return response

        response = {
            "success": True,
            "func_ref": func_ref,
            "truncated": result["timed_out"],
            "command": result["command"],
        }
        _add_capped(response, "output", result["stdout"])
        urls = extract_urls(result["stdout"], result["stderr"])
        if urls:
            response["urls"] = urls
        if result["timed_out"]:
            response["message"] = (
                f"Run still active after {timeout_seconds}s; returning a snapshot. "
                "Increase timeout_seconds, or pass detach=True to keep it running on Modal."
            )
        _add_capped(response, "stderr", result["stderr"])
        return response
    except Exception as e:
        logger.error(f"Failed to run Modal app '{func_ref}': {e}")
        raise


# ---------------------------------------------------------------------------
# Read-only lookups — one tool for every "what exists / what is this" question
# ---------------------------------------------------------------------------

_RESOURCES = (
    "apps",
    "app_history",
    "containers",
    "volumes",
    "volume_files",
    "secrets",
    "environments",
    "profile",
)


@mcp.tool(annotations=_read_only("List Modal resources"))
async def list_modal_resources(
    resource: str,
    name: Optional[str] = None,
    path: str = "/",
    env: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Read-only lookup of everything in the Modal account. Start here to find the app name,
    container ID or volume name that the other tools take.

    Args:
        resource: One of:
            "apps" — deployed/running/recently-stopped apps.
            "app_history" — one app's deployment versions (`name` = app name/ID); use it
                to pick a version for manage_modal_app(action="rollback").
            "containers" — running containers ("ta-..."); `name` = app ID to filter.
            "volumes" — named volumes.
            "volume_files" — files in a volume (`name` = volume, plus `path`).
            "secrets" — secret names only; values are never returned.
            "environments" — valid values for every `env` argument.
            "profile" — active profile + all profiles (which account am I?).
        name: App name/ID, app ID filter, or volume name — see `resource`.
        path: Path inside the volume for "volume_files". Default "/".
        env: Modal environment. Ignored for "environments"/"profile".

    Returns: {<resource key>: [...]} — e.g. "apps", "containers", "contents". Listings
    over 200 entries are capped, with `omitted_items` giving the count dropped.
    """
    if resource not in _RESOURCES:
        return {
            "success": False,
            "error": f"Unknown resource {resource!r}. Valid values: {', '.join(_RESOURCES)}",
        }
    if resource in ("app_history", "volume_files") and not name:
        target = "app name/ID" if resource == "app_history" else "volume name"
        return {"success": False, "error": f"resource={resource!r} requires `name` (the {target})"}

    try:
        if resource == "apps":
            command = ["modal", "app", "list", "--json"]
            _add_env(command, env)
            return json_listing(command, "apps", "Failed to list apps")

        if resource == "app_history":
            command = ["modal", "app", "history", "--json"]
            _add_env(command, env)
            command.extend(["--", name])
            return json_listing(command, "history", "Failed to get app history", app_identifier=name)

        if resource == "containers":
            command = ["modal", "container", "list", "--json"]
            if name:
                command.extend(["--app-id", name])
            _add_env(command, env)
            return json_listing(command, "containers", "Failed to list containers")

        if resource == "volumes":
            command = ["modal", "volume", "list", "--json"]
            _add_env(command, env)
            return json_listing(command, "volumes", "Failed to list volumes")

        if resource == "volume_files":
            command = ["modal", "volume", "ls", "--json"]
            _add_env(command, env)
            command.extend(["--", name, path])
            response = json_listing(
                command, "contents", "Failed to list volume contents", volume_name=name, path=path
            )
            if not response["success"]:
                return response
            # An empty list is a valid, successful result — flag it so the caller doesn't
            # mistake "genuinely empty" for "the listing failed" or "wrong path format".
            contents = response["contents"]
            if isinstance(contents, list) and not contents:
                response["empty"] = True
                response["message"] = (
                    f"{name!r} at path {path!r} is empty (the listing succeeded and "
                    "returned no entries). If you expected files, double-check the path "
                    "(e.g. a leading '/' or a subdirectory) and the volume name."
                )
            else:
                response["empty"] = False
            return response

        if resource == "secrets":
            command = ["modal", "secret", "list", "--json"]
            _add_env(command, env)
            return json_listing(command, "secrets", "Failed to list secrets")

        if resource == "environments":
            return json_listing(
                ["modal", "environment", "list", "--json"], "environments", "Failed to list environments"
            )

        # resource == "profile"
        current = run_modal_command(["modal", "profile", "current"])
        listing = run_modal_command(["modal", "profile", "list", "--json"])
        response = {"success": current["success"] and listing["success"]}
        if current["success"]:
            response["active_profile"] = (current["stdout"] or "").strip()
        profiles = handle_json_response(listing, "Failed to list profiles")
        if profiles["success"]:
            response["profiles"] = profiles["data"]
        elif "error" not in response:
            response["error"] = profiles.get("error")
        if not response["success"] and "error" not in response:
            response["error"] = current.get("error") or listing.get("error")
        return response
    except Exception as e:
        logger.error(f"Failed to list Modal {resource}: {e}")
        raise


# ---------------------------------------------------------------------------
# Logs (apps & containers)
# ---------------------------------------------------------------------------

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
        since / until: Time range, ISO 8601 or relative ("2h", "30m", "1d").
        tail: Only the last N entries.
        source: "stdout", "stderr", or "system".
        timestamps: Prefix each line with its wall-clock timestamp.
        follow: Live-stream until the app/container stops or the timeout hits.

    Returns: {logs, truncated (still streaming at the timeout), output_capped (text
    trimmed to fit context — narrow with tail/since/source)}.
    """
    if target not in ("auto", "app", "container"):
        return {"success": False, "error": "target must be 'auto', 'app', or 'container'"}
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
                "error": f"Failed to get logs for '{identifier}' (exit {result['returncode']})",
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
    tail: Optional[int] = None,
    source: Optional[str] = None,
    exclude: Optional[str] = None,
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
        since / tail: Search window — relative time ("2h") or last N entries. Defaults to
            the last 1000 entries.
        source: Search only "stdout", "stderr", or "system".
        exclude: Drop lines matching this BEFORE searching, to strip repeated noise.
        timestamps: Prefix lines with their timestamp. Default True.
        timeout_seconds: Max seconds spent fetching logs. Default 30.
        env: Modal environment (apps only).

    Returns: {match_count (exact, whole log searched), matches (context blocks, matched
    lines prefixed ">"), returned, excluded_lines, output_capped}.
    """
    if target not in ("auto", "app", "container"):
        return {"success": False, "error": "target must be 'auto', 'app', or 'container'"}
    if not pattern:
        return {"success": False, "error": "A non-empty search pattern is required"}
    if len(pattern) > _MAX_SCAN_WIDTH:
        return {"success": False, "error": f"Pattern too long (max {_MAX_SCAN_WIDTH} chars)"}
    if source is not None and source not in ("stdout", "stderr", "system"):
        return {"success": False, "error": "source must be 'stdout', 'stderr', or 'system'"}
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
        if since:
            command.extend(["--since", since])
        if tail is not None:
            command.extend(["--tail", str(tail)])
        if since is None and tail is None:
            # Search a generous window by default so debugging isn't limited to ~100 lines.
            command.extend(["--tail", "1000"])
        if resolved == "app":
            _add_env(command, env)
        command.extend(["--", identifier])

        result = run_modal_streaming_command(command, timeout_seconds)
        failed = result["returncode"] not in (0, None) and not result["timed_out"]
        if failed:
            response = {
                "success": False,
                "error": f"Failed to fetch logs for '{identifier}' (exit {result['returncode']})",
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
        response = {
            "success": True,
            "target": resolved,
            "identifier": identifier,
            "pattern": pattern,
            "match_count": total,
            "returned": len(blocks),
            "matches": blocks,
            "logs_truncated": result["timed_out"],
            "command": result["command"],
        }
        if dropped:
            response["output_capped"] = True
        if exclude:
            response["excluded_lines"] = excluded_lines
        if total == 0:
            response["message"] = (
                f"No matches for {pattern!r} in the fetched logs. Try a broader pattern, "
                "increase `tail`/`since`, or set regex=True. Note that some failures "
                "(e.g. crashes reported as '... exited with ...') are Modal dashboard "
                "events, not log lines, and will never match here — check the dashboard."
            )
        elif len(blocks) < total:
            hidden = total - len(blocks)
            reason = (
                f"{dropped} block(s) were dropped to fit the output budget"
                if dropped
                else "increase max_matches for more"
            )
            response["message"] = (
                f"Showing {len(blocks)} of {total} matches ({hidden} not shown); {reason}. "
                "Narrowing with `since`, `tail`, `source` or `exclude` gives sharper results."
            )
        if result["timed_out"]:
            response.setdefault("message", "")
            response["message"] = (
                (response["message"] + " ").lstrip()
                + f"Log fetch was cut off at {timeout_seconds}s, so older entries may be missing."
            )
        return response
    except Exception as e:
        logger.error(f"Failed to search logs for '{identifier}': {e}")
        raise


# ---------------------------------------------------------------------------
# Apps & containers — state changes
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_mutating("Stop or roll back a Modal app"))
async def manage_modal_app(
    action: str,
    app_identifier: str,
    version: Optional[str] = None,
    env: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Change a deployed app's state. Both actions affect live traffic.

    Args:
        action: "stop" — shut the app down, ending web endpoints (`modal app stop`).
            "rollback" — restore a previous deployment (`modal app rollback`).
        app_identifier: App name ("my-app") or ID ("ap-...").
        version: Rollback target; omit for the immediately preceding version. List valid
            versions with list_modal_resources(resource="app_history", name=...).
        env: Modal environment to target.

    Returns: {message, stdout, stderr} or {error}.
    """
    if action not in ("stop", "rollback"):
        return {"success": False, "error": "action must be 'stop' or 'rollback'"}
    if version and action != "rollback":
        return {"success": False, "error": "`version` is only valid with action='rollback'"}
    try:
        if action == "stop":
            command = ["modal", "app", "stop"]
            _add_env(command, env)
            command.extend(["--", app_identifier])
            result = run_modal_command(command)
            return standardize_result(
                result, f"Successfully stopped app {app_identifier}", "Failed to stop app"
            )

        command = ["modal", "app", "rollback"]
        _add_env(command, env)
        command.extend(["--", app_identifier])
        if version:
            command.append(str(version))
        result = run_modal_command(command)
        return standardize_result(
            result, f"Successfully rolled back app {app_identifier}", "Failed to roll back app"
        )
    except Exception as e:
        logger.error(f"Failed to {action} Modal app '{app_identifier}': {e}")
        raise


@mcp.tool(annotations=_mutating("Run a command in, or stop, a Modal container"))
async def manage_modal_container(
    action: str,
    container_id: str,
    command: Optional[List[str]] = None,
    timeout_seconds: int = 60,
) -> Dict[str, Any]:
    """
    Act on one running container. Find IDs with
    list_modal_resources(resource="containers").

    Args:
        action: "exec" — run a command inside the container (`modal container exec`).
            This is arbitrary remote code execution: treat it like SSH, not a lookup.
            "stop" — terminate it (`modal container stop`); in-flight inputs are
            cancelled and rescheduled elsewhere.
        container_id: Container ID ("ta-..."). Unique across environments, so no `env`
            argument is needed (the CLI accepts none for these subcommands).
        command: For "exec": argv list, e.g. ["python", "-c", "print('hi')"] or
            ["ls", "-la", "/"].
        timeout_seconds: For "exec": max seconds to wait. Default 60.

    Returns: exec → {output, returncode, truncated, output_capped}; stop → {message}.
    """
    if action not in ("exec", "stop"):
        return {"success": False, "error": "action must be 'exec' or 'stop'"}
    try:
        if action == "stop":
            if command:
                return {"success": False, "error": "`command` is only valid with action='exec'"}
            # `-y` avoids the interactive confirmation prompt.
            result = run_modal_command(["modal", "container", "stop", "-y", "--", container_id])
            return standardize_result(
                result, f"Successfully stopped container {container_id}", "Failed to stop container"
            )

        if not command:
            return {"success": False, "error": "action='exec' requires a non-empty `command` list"}
        # `--no-pty` avoids allocating a PTY, which isn't available in this subprocess.
        # `--` ends modal's own option parsing: it protects `container_id` from option
        # injection AND lets the user command carry its own flags (e.g. `ls -la`) without
        # modal trying to interpret them.
        full_command = ["modal", "container", "exec", "--no-pty", "--", container_id] + command
        result = run_modal_streaming_command(full_command, timeout_seconds)
        failed = result["returncode"] not in (0, None) and not result["timed_out"]
        response = {
            "success": not failed,
            "container_id": container_id,
            "returncode": result["returncode"],
            "truncated": result["timed_out"],
            "command": result["command"],
        }
        _add_capped(response, "output", result["stdout"])
        if failed:
            response["error"] = f"Command exited with code {result['returncode']}"
        _add_capped(response, "stderr", result["stderr"])
        return response
    except Exception as e:
        logger.error(f"Failed to {action} Modal container '{container_id}': {e}")
        raise


# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_mutating("Create, delete or rename a Modal volume"))
async def manage_modal_volume(
    action: str,
    volume_name: str,
    new_name: Optional[str] = None,
    env: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Volume lifecycle. For the files inside a volume use modal_volume_files (writes) or
    list_modal_resources(resource="volume_files") (reads).

    Args:
        action: "create", "delete" (removes the volume and ALL its data — irreversible),
            or "rename".
        volume_name: Volume name (the current name, for "rename").
        new_name: Required for "rename".
        env: Modal environment. Volumes are environment-scoped, so this must match the
            environment the volume lives in.

    Returns: {message, stdout, stderr} or {error}.
    """
    if action not in ("create", "delete", "rename"):
        return {"success": False, "error": "action must be 'create', 'delete', or 'rename'"}
    if action == "rename" and not new_name:
        return {"success": False, "error": "action='rename' requires `new_name`"}
    try:
        if action == "create":
            command = ["modal", "volume", "create"]
            _add_env(command, env)
            command.extend(["--", volume_name])
            result = run_modal_command(command)
            return standardize_result(
                result, f"Successfully created volume {volume_name}", "Failed to create volume"
            )

        if action == "delete":
            # `-y` avoids the interactive confirmation prompt.
            command = ["modal", "volume", "delete", "-y"]
            _add_env(command, env)
            command.extend(["--", volume_name])
            result = run_modal_command(command)
            return standardize_result(
                result, f"Successfully deleted volume {volume_name}", "Failed to delete volume"
            )

        # `-y` avoids the interactive confirmation prompt.
        command = ["modal", "volume", "rename", "-y"]
        _add_env(command, env)
        command.extend(["--", volume_name, new_name])
        result = run_modal_command(command)
        return standardize_result(
            result,
            f"Successfully renamed volume {volume_name} to {new_name}",
            "Failed to rename volume",
        )
    except Exception as e:
        logger.error(f"Failed to {action} Modal volume '{volume_name}': {e}")
        raise


@mcp.tool(annotations=_mutating("Move files in a Modal volume"))
async def modal_volume_files(
    action: str,
    volume_name: str,
    local_path: Optional[str] = None,
    remote_path: Optional[str] = None,
    paths: Optional[List[str]] = None,
    recursive: bool = False,
    force: bool = False,
    env: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Write operations on a volume's files. To LIST a volume's contents use
    list_modal_resources(resource="volume_files").

    Args:
        action: "put" (upload local_path → remote_path), "get" (download remote_path →
            local_path; "-" returns the contents instead of writing a file), "cp" (copy
            inside the volume, using `paths`), "rm" (delete remote_path).
        volume_name: Volume name.
        local_path: Local source ("put") or destination ("get", default ".").
        remote_path: In-volume destination ("put", default "/", trailing "/" keeps the
            filename), source ("get"), or target ("rm").
        paths: For "cp": sources followed by the destination, e.g. ["a.txt", "dest/"].
        recursive: Needed to "rm" or "cp" a directory.
        force: Overwrite existing files ("put"/"get").
        env: Modal environment the volume lives in.

    Returns: {message, stdout, stderr} or {error}. When MCP_MODAL_ALLOWED_LOCAL_PATHS is
    set, "put"/"get" are refused for local paths outside the allowlist.
    """
    if action not in ("put", "get", "cp", "rm"):
        return {"success": False, "error": "action must be 'put', 'get', 'cp', or 'rm'"}
    try:
        if action == "put":
            if not local_path:
                return {"success": False, "error": "action='put' requires `local_path`"}
            denied = _check_local_path(local_path)
            if denied:
                return {"success": False, "error": denied}
            destination = remote_path or "/"
            command = ["modal", "volume", "put"]
            if force:
                command.append("-f")
            _add_env(command, env)
            command.extend(["--", volume_name, local_path, destination])
            result = run_modal_command(command)
            return standardize_result(
                result,
                f"Successfully uploaded {local_path} to {volume_name}:{destination}",
                f"Failed to upload {local_path}",
            )

        if action == "get":
            if not remote_path:
                return {"success": False, "error": "action='get' requires `remote_path`"}
            destination = local_path or "."
            # "-" streams to stdout (no file is written), so it bypasses the path allowlist.
            if destination != "-":
                denied = _check_local_path(destination)
                if denied:
                    return {"success": False, "error": denied}
            command = ["modal", "volume", "get"]
            if force:
                command.append("--force")
            _add_env(command, env)
            command.extend(["--", volume_name, remote_path, destination])
            result = run_modal_command(command)
            return standardize_result(
                result,
                f"Successfully downloaded {remote_path} from volume {volume_name}",
                f"Failed to download {remote_path}",
            )

        if action == "cp":
            if not paths or len(paths) < 2:
                return {
                    "success": False,
                    "error": "action='cp' requires `paths` with at least one source and a destination",
                }
            command = ["modal", "volume", "cp"]
            if recursive:
                command.append("-r")
            _add_env(command, env)
            command.extend(["--", volume_name] + paths)
            result = run_modal_command(command)
            return standardize_result(
                result,
                f"Successfully copied files in volume {volume_name}",
                "Failed to copy files",
            )

        # action == "rm"
        if not remote_path:
            return {"success": False, "error": "action='rm' requires `remote_path`"}
        command = ["modal", "volume", "rm"]
        if recursive:
            command.append("-r")
        _add_env(command, env)
        command.extend(["--", volume_name, remote_path])
        result = run_modal_command(command)
        return standardize_result(
            result,
            f"Successfully deleted {remote_path} from volume {volume_name}",
            f"Failed to delete {remote_path}",
        )
    except Exception as e:
        logger.error(f"Failed to {action} files in Modal volume '{volume_name}': {e}")
        raise


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_mutating("Create or delete a Modal secret"))
async def manage_modal_secret(
    action: str,
    secret_name: str,
    key_values: Optional[Dict[str, str]] = None,
    from_dotenv: Optional[str] = None,
    from_json: Optional[str] = None,
    force: bool = False,
    env: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create or delete a secret. To list secret names use
    list_modal_resources(resource="secrets") — values are never readable.

    Values are redacted from every field returned (command, stdout, stderr, error), so
    they cannot leak back into the transcript on failure.

    Args:
        action: "create" or "delete".
        secret_name: Secret name.
        key_values: For "create": {"API_KEY": "abc", ...}.
        from_dotenv / from_json: For "create": load key/values from a local file instead.
        force: For "create": overwrite an existing secret.
        env: Modal environment to target.

    Returns: {message, stdout, stderr} or {error}, with values redacted.
    """
    if action not in ("create", "delete"):
        return {"success": False, "error": "action must be 'create' or 'delete'"}
    try:
        if action == "delete":
            # `-y` avoids the interactive confirmation prompt.
            command = ["modal", "secret", "delete", "-y"]
            _add_env(command, env)
            command.extend(["--", secret_name])
            result = run_modal_command(command)
            return standardize_result(
                result, f"Successfully deleted secret {secret_name}", "Failed to delete secret"
            )

        if not key_values and not from_dotenv and not from_json:
            return {
                "success": False,
                "error": "Provide key_values, from_dotenv, or from_json to create a secret",
            }
        command = ["modal", "secret", "create"]
        if from_dotenv:
            command.extend(["--from-dotenv", from_dotenv])
        if from_json:
            command.extend(["--from-json", from_json])
        if force:
            command.append("--force")
        _add_env(command, env)
        # `--` ends option parsing; the name and KEY=VALUE pairs follow as positionals.
        command.append("--")
        command.append(secret_name)
        if key_values:
            command.extend([f"{k}={v}" for k, v in key_values.items()])

        # Pass the secret values to the runner so they are scrubbed from the logged
        # command AND from every returned field (command/stdout/stderr/error) — not just
        # the happy-path command string. A failed create (e.g. secret exists, no --force)
        # would otherwise echo the plaintext values back in the error.
        secret_values = list(key_values.values()) if key_values else None
        result = run_modal_command(command, redact=secret_values)
        return standardize_result(
            result, f"Successfully created secret {secret_name}", "Failed to create secret"
        )
    except Exception as e:
        logger.error(f"Failed to {action} Modal secret '{secret_name}': {e}")
        raise


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Secret inspection
# ---------------------------------------------------------------------------

# Environment variables that come from the container image or the Modal runtime rather
# than from the mounted secret. Modal exposes no API for a secret's key names, so the only
# way to see them is to mount the secret and list the container's environment — which
# means subtracting the variables that would have been there anyway. The exact names below
# were captured from a real container in Modal's default image (which also carries
# MODAL_TOKEN_ID / MODAL_TOKEN_SECRET — filtered by the MODAL_ prefix, never returned).
_BASE_ENV_NAMES = {
    "BLIS_NUM_THREADS", "CFLAGS", "DEBIAN_FRONTEND", "GPG_KEY", "HOME", "HOSTNAME",
    "LANG", "LC_ALL", "MKL_NUM_THREADS", "OLDPWD", "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS", "ORT_INTER_OP_NUM_THREADS", "ORT_INTRA_OP_NUM_THREADS",
    "PATH", "PWD", "SHLVL", "SOURCE_DATE_EPOCH", "SSL_CERT_DIR", "SSL_CERT_FILE",
    "TERM", "UV_BREAK_SYSTEM_PACKAGES", "_",
}
_BASE_ENV_PREFIXES = ("MODAL_", "PYTHON", "PIP_", "NVIDIA_", "CUDA_", "LD_LIBRARY_PATH")

# The probe runs inside the container. `modal shell -c` shlex-splits the string, re-joins
# it with spaces and hands it to `bash -c`, so quoting does NOT survive: the command must
# contain no quotes and no shell metacharacters beyond `;`. `compgen -e` is a bash builtin
# that prints exported variable NAMES only — a value is never printed, not even into a
# pipe inside the container. The markers bracket the list so it can be picked out of the
# image-build chatter on the same stream.
_KEYS_START = "@@MCPKEYS-START@@"
_KEYS_END = "@@MCPKEYS-END@@"
_KEYS_PROBE = f"echo {_KEYS_START} ; compgen -e ; echo {_KEYS_END}"


def _secret_key_names(env_names: List[str]) -> List[str]:
    """Drop image/runtime variables, leaving the names that came from the secret."""
    return sorted(
        n for n in env_names
        if n not in _BASE_ENV_NAMES and not n.startswith(_BASE_ENV_PREFIXES)
    )


@mcp.tool(annotations=_mutating("Inspect a Modal secret's key names", destructive=False))
async def inspect_modal_secret(
    secret_name: str,
    env: Optional[str] = None,
    image: Optional[str] = None,
    timeout_seconds: int = 300,
) -> Dict[str, Any]:
    """
    List the KEY NAMES inside a Modal secret — never the values.

    Modal exposes no API for this: neither the CLI, the SDK, nor the gRPC layer can read
    a secret's contents, by design. The only way to see which keys a secret defines is to
    mount it in a container and look at the environment variable names. So this tool
    starts a short-lived container (`modal shell --secret ...`), prints the variable NAMES
    only, and subtracts the ones the image and the Modal runtime would have set anyway.

    That means, unlike every other read in this server, a call here **starts remote
    compute and costs a few cents** (and takes tens of seconds — longer on the first run
    for a given image, which has to be built). It is not a free lookup: use
    list_modal_resources(resource="secrets") to see which secrets exist, and reach for
    this only when you need to know what is inside one.

    Values never leave the container: the probe is `compgen -e`, a bash builtin that
    prints exported variable NAMES only, so no value is ever printed or read.

    Args:
        secret_name: Name of the secret, from list_modal_resources(resource="secrets").
        env: Modal environment the secret lives in.
        image: Optional container image. Omit it to use Modal's default image, which is
            built to match this server's Python — that is the most reliable choice. Pass one
            (e.g. "python:3.12-slim") if the workspace's image builder rejects that Python.
        timeout_seconds: Max seconds to wait, including image build. Default 300.

    Returns: {keys: [...names...], all_env_names: [...], filtered_out: n}. `all_env_names`
        is the unfiltered list, so a key that looks like a runtime variable (e.g. one
        literally named "PATH") is still visible rather than silently dropped.
    """
    if not secret_name:
        return {"success": False, "error": "secret_name is required"}
    try:
        command = ["modal", "shell", "--no-pty"]
        if image:
            command.extend(["--image", image])
        command.extend(["--secret", secret_name])
        _add_env(command, env)
        command.extend(["-c", _KEYS_PROBE])

        result = run_modal_streaming_command(command, timeout_seconds)
        combined = (result["stdout"] or "") + "\n" + (result["stderr"] or "")
        if _KEYS_START not in combined or _KEYS_END not in combined:
            hint = (
                "The container never reported its environment. "
                if not result["timed_out"]
                else f"Timed out after {timeout_seconds}s (an image build can be slow — retry, "
                "or raise timeout_seconds). "
            )
            if "Unsupported Python version" in combined:
                hint = (
                    "This workspace's image builder does not support the Python version this "
                    "server runs on. Pass an explicit `image` that it accepts (e.g. "
                    "\"python:3.12-slim\"), or upgrade the image builder at "
                    "modal.com/settings/image-config. "
                )
            elif "Image build" in combined and "failed" in combined:
                hint = (
                    "The container image failed to build. Try a different `image`, or check "
                    "`modal image logs` for the build. "
                )
            response = {
                "success": False,
                "error": (
                    f"Could not list the keys of {secret_name!r}. {hint}"
                    "Confirm the secret exists in this environment with "
                    "list_modal_resources(resource='secrets')."
                ),
                "timed_out": result["timed_out"],
                "command": result["command"],
            }
            _add_capped(response, "stderr", result["stderr"])
            return response

        body = combined.split(_KEYS_START, 1)[1].split(_KEYS_END, 1)[0]
        env_names = [line.strip() for line in body.splitlines() if line.strip()]

        keys = _secret_key_names(env_names)
        return {
            "success": True,
            "secret_name": secret_name,
            "keys": keys,
            "key_count": len(keys),
            "all_env_names": sorted(env_names),
            "filtered_out": len(env_names) - len(keys),
            "command": result["command"],
            "message": (
                f"{secret_name!r} defines {len(keys)} key(s). Values were never read. "
                "Names are inferred by subtracting the image/runtime variables listed in "
                "`all_env_names`, so double-check that list if a key looks missing."
            ),
        }
    except Exception as e:
        logger.error(f"Failed to inspect Modal secret '{secret_name}': {e}")
        raise


# ---------------------------------------------------------------------------
# Prompts — reusable workflows the user can invoke directly from the client
# ---------------------------------------------------------------------------
# Prompts cost nothing in per-session tool schema (clients fetch them on demand), so
# they're the cheap place to put the multi-step know-how that would otherwise have to
# be repeated in every tool description.

@mcp.prompt(title="Debug a Modal app")
def debug_modal_app(app_name: str, symptom: str = "") -> str:
    """Walk through diagnosing a failing or misbehaving Modal app."""
    focus = f"\nReported symptom: {symptom}\n" if symptom else "\n"
    return f"""Diagnose what is wrong with the Modal app "{app_name}".{focus}
Work in this order, stopping as soon as you have the root cause:

1. Confirm the app exists and its current state:
   list_modal_resources(resource="apps")
2. Look for failures in the logs, with context around each hit:
   search_modal_logs(identifier="{app_name}", pattern="Traceback|Error|Exception", regex=True, since="1h")
   If that is noisy, cut the noise with source="stderr" or exclude="<repeated line>".
3. If nothing matches, read the recent log tail directly:
   get_modal_logs(identifier="{app_name}", tail=200, timestamps=True)
4. If the app is running but wedged, inspect its containers:
   list_modal_resources(resource="containers"), then get_modal_logs on the container ID,
   and manage_modal_container(action="exec", ...) for a live look (e.g. ["ps", "aux"]).
5. If the failure started after a deploy, compare against history:
   list_modal_resources(resource="app_history", name="{app_name}")
   and propose manage_modal_app(action="rollback", ...) if a recent version is the cause.

Important: `modal app logs` only carries the stdout/stderr/system streams. Crash events
shown on the Modal dashboard (e.g. "... exited with ...") are NOT log lines and will never
appear in a search — if the logs look clean but the app is clearly failing, say so and
point at the dashboard rather than concluding nothing is wrong.

Finish with: the root cause, the evidence (quote the log lines), and the fix."""


@mcp.prompt(title="Deploy and verify a Modal app")
def deploy_and_verify(absolute_path_to_app: str, env: str = "") -> str:
    """Deploy a Modal app, then confirm it actually came up."""
    env_note = f' into the "{env}" environment' if env else ""
    env_arg = f', env="{env}"' if env else ""
    return f"""Deploy {absolute_path_to_app}{env_note} and verify it is genuinely live.

1. Check which account/workspace you are about to deploy to:
   list_modal_resources(resource="profile")
2. Deploy:
   deploy_modal_app(absolute_path_to_app="{absolute_path_to_app}"{env_arg})
   The app's directory must use `uv` and have `modal` installed in its virtualenv.
3. Report every URL from the `urls` field — those are live, shareable endpoints.
4. Verify rather than assume: confirm the app appears with
   list_modal_resources(resource="apps"{env_arg}), then check the first minutes of logs with
   search_modal_logs(identifier="<app name>", pattern="Error|Traceback", regex=True, since="5m").
5. If the deploy failed or the app is crash-looping, do NOT retry blindly — report the
   error, and offer manage_modal_app(action="rollback", ...) to restore the previous version.

Report: what was deployed, its URLs, and the evidence that it is healthy."""


@mcp.prompt(title="Investigate a Modal cost spike")
def investigate_modal_costs(period: str = "last week", app: str = "") -> str:
    """Trace a cost increase back to the app and resource that caused it."""
    focus = f'\nFocus on the app "{app}".\n' if app else "\n"
    app_arg = f', app="{app}"' if app else ""
    return f"""Work out where Modal spend went during {period} and what drove it.{focus}
1. Shape of the spend over time — this also names the peak interval and ranks which apps
   grew into it:
   analyze_modal_costs(view="timeline", period="{period}"{app_arg})
   Read `explanation.movers`: those are the apps that cost more in the peak interval than
   in the one before, biggest increase first.
2. Who spends the most overall:
   analyze_modal_costs(view="by_app", period="{period}"{app_arg})
3. Drill into the peak. Take the peak date from step 1 and go hourly:
   analyze_modal_costs(view="timeline", start="<peak date>", end="<next date>", resolution="h")
4. What kind of resource it was (a GPU class is usually the answer):
   analyze_modal_costs(view="by_resource", period="{period}"{app_arg})
5. Tie it to what actually ran: list_modal_resources(resource="apps") for the culprit, then
   list_modal_resources(resource="app_history", name="<app>") to see whether a deploy lines
   up with the increase, and search_modal_logs for retries/restarts around the peak.

Costs are US dollars. `total_cost` covers every row in range even when the group list is
truncated, so quote it rather than summing the visible rows. Modal reports whole intervals
only, so a partially elapsed day reads low — say so instead of calling it a drop.

Report: total for the period, the biggest spender, what changed at the peak and why, and —
if the cause is a still-running container or an over-provisioned GPU — the exact call that
would stop it, without running it."""


@mcp.prompt(title="Review Modal account usage")
def review_modal_account(env: str = "") -> str:
    """Inventory the Modal account and flag cleanup candidates."""
    env_arg = f'env="{env}"' if env else ""
    sep = ", " if env_arg else ""
    return f"""Produce an inventory of this Modal account and flag anything worth cleaning up.

Gather (read-only — do not stop, delete, or modify anything):
- list_modal_resources(resource="profile") — which workspace am I looking at?
- list_modal_resources(resource="environments") — repeat the sweep per environment if
  there is more than one.
- list_modal_resources(resource="apps"{sep}{env_arg}) — note stopped or stale apps.
- list_modal_resources(resource="containers"{sep}{env_arg}) — anything running that
  shouldn't be is burning money right now.
- list_modal_resources(resource="volumes"{sep}{env_arg}) — spot-check large or unused ones
  with resource="volume_files".
- list_modal_resources(resource="secrets"{sep}{env_arg}) — names only; never print values.

Then report: a short inventory table, running containers with no obvious owner, apps that
look abandoned, and volumes/secrets that appear orphaned. For each cleanup candidate name
the exact tool call that would remove it, but do not run it — deletions are the user's call."""


def main() -> None:
    """Console-script entry point for the mcp-modal package."""
    mcp.run()


if __name__ == "__main__":
    main()
