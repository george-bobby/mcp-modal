"""MCP server for managing Modal (modal.com) apps, containers, volumes, and secrets.

All tools shell out to the local `modal` CLI, so they use whatever Modal profile /
credentials are configured on the host (`~/.modal.toml`). Account-scoped operations
(apps, containers, volumes, secrets, profiles, environments) run the plain `modal`
binary; operations that build/deploy/run a local project (`deploy`, `run`) wrap the
command in `uv run --directory=<project>` so the project's own virtualenv is used.
"""
import logging
import os
import re
import signal
from typing import Any, Optional, List, Dict
import subprocess
import json

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("modal-deploy")

# Matches http(s) URLs in CLI output so we can surface deployment / web-endpoint links.
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
# Matches a `KEY=VALUE` secret pair (but not CLI flags like `--force`).
_KEYVALUE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _uv_prefixed(command: List[str], uv_directory: Optional[str]) -> List[str]:
    """Prefix a command with `uv run --directory=<dir>` when a project dir is given.

    Deploying/running a Modal app requires the app's own uv virtualenv, so those
    commands must run through `uv`. Account-scoped commands pass uv_directory=None.
    """
    if uv_directory:
        return ["uv", "run", f"--directory={uv_directory}"] + command
    return command


def _add_env(command: List[str], env: Optional[str]) -> List[str]:
    """Append `-e <env>` to target a specific Modal environment, if provided."""
    if env:
        command.extend(["-e", env])
    return command


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


def run_modal_command(command: List[str], uv_directory: Optional[str] = None) -> Dict[str, Any]:
    """Run a Modal CLI command to completion and return the result."""
    try:
        command = _uv_prefixed(command, uv_directory)
        logger.info(f"Running command: {' '.join(command)}")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True
        )
        return {
            "success": True,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "command": ' '.join(command)
        }
    except subprocess.CalledProcessError as e:
        return {
            "success": False,
            "error": str(e),
            "stdout": e.stdout,
            "stderr": e.stderr,
            "command": ' '.join(command)
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
        if result.get("stdout"):
            response["stdout"] = result["stdout"]
        if result.get("stderr"):
            response["stderr"] = result["stderr"]
        return response

    try:
        data = json.loads(result["stdout"])
        return {"success": True, "data": data}
    except json.JSONDecodeError as e:
        response = {"success": False, "error": f"Failed to parse JSON output: {str(e)}"}
        if result.get("stdout"):
            response["stdout"] = result["stdout"]
        if result.get("stderr"):
            response["stderr"] = result["stderr"]
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
    if result.get("stdout"):
        response["stdout"] = result["stdout"]
    if result.get("stderr"):
        response["stderr"] = result["stderr"]
    return response


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
    match_indices = [i for i, line in enumerate(lines) if compiled.search(line)]
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


# ---------------------------------------------------------------------------
# Deploy & run (compute) — these wrap the command in the project's uv venv
# ---------------------------------------------------------------------------

@mcp.tool()
async def deploy_modal_app(
    absolute_path_to_app: str,
    env: Optional[str] = None,
    name: Optional[str] = None,
    tag: Optional[str] = None,
    strategy: Optional[str] = None,
    stream_logs: bool = False,
) -> Dict[str, Any]:
    """
    Deploy a Modal application (`modal deploy`). Deployed web endpoints persist after
    this call returns, so any URLs in the output are live, shareable links.

    Args:
        absolute_path_to_app: Absolute path to the Modal app file to deploy. Its
            directory must use `uv` and have `modal` installed in its virtualenv.
        env: Optional Modal environment to deploy into.
        name: Optional deployment name (`--name`).
        tag: Optional version tag for the deployment (`--tag`).
        strategy: Optional rollout strategy: "rolling" or "recreate" (`--strategy`).
        stream_logs: If True, stream logs from the app after deploy (`--stream-logs`).

    Returns:
        A dictionary with deployment results. `urls` lists any web-endpoint/dashboard
        links found in the output.
    """
    uv_directory = os.path.dirname(absolute_path_to_app)
    app_name = os.path.basename(absolute_path_to_app)
    try:
        command = ["modal", "deploy", app_name]
        if name:
            command.extend(["--name", name])
        if tag:
            command.extend(["--tag", tag])
        if strategy:
            command.extend(["--strategy", strategy])
        if stream_logs:
            command.append("--stream-logs")
        _add_env(command, env)

        result = run_modal_command(command, uv_directory)
        urls = extract_urls(result.get("stdout"), result.get("stderr"))
        if urls:
            result["urls"] = urls
        return result
    except Exception as e:
        logger.error(f"Failed to deploy Modal app: {e}")
        raise


@mcp.tool()
async def run_modal_app(
    absolute_path_to_app: str,
    function_name: Optional[str] = None,
    env: Optional[str] = None,
    detach: bool = False,
    timeout_seconds: int = 120,
) -> Dict[str, Any]:
    """
    Run a Modal function or local entrypoint (`modal run`). Unlike deploy, this executes
    the app once and streams its logs; use it to test a function on Modal compute.

    Args:
        absolute_path_to_app: Absolute path to the Modal app file. Its directory must
            use `uv` and have `modal` installed in its virtualenv.
        function_name: Optional function / entrypoint name, e.g. "main". When omitted,
            Modal runs the single entrypoint/function if the module has exactly one.
        env: Optional Modal environment to target.
        detach: If True, keep the app running on Modal even if this process disconnects
            (`--detach`). Useful for long jobs you don't want cut off at the timeout.
        timeout_seconds: Max seconds to collect output before returning. Defaults to 120.

    Returns:
        A dictionary with collected output. `truncated` is True when the run was still
        going at the timeout. `urls` lists any links found in the output.
    """
    uv_directory = os.path.dirname(absolute_path_to_app)
    app_name = os.path.basename(absolute_path_to_app)
    func_ref = f"{app_name}::{function_name}" if function_name else app_name
    try:
        command = ["modal", "run"]
        if detach:
            command.append("--detach")
        command.append(func_ref)
        _add_env(command, env)

        result = run_modal_streaming_command(command, timeout_seconds, uv_directory)
        failed = result["returncode"] not in (0, None) and not result["timed_out"]
        if failed:
            response = {
                "success": False,
                "error": f"Run failed for '{func_ref}' (exit {result['returncode']})",
                "command": result["command"],
            }
            if result["stdout"]:
                response["stdout"] = result["stdout"]
            if result["stderr"]:
                response["stderr"] = result["stderr"]
            return response

        response = {
            "success": True,
            "func_ref": func_ref,
            "output": result["stdout"],
            "truncated": result["timed_out"],
            "command": result["command"],
        }
        urls = extract_urls(result["stdout"], result["stderr"])
        if urls:
            response["urls"] = urls
        if result["timed_out"]:
            response["message"] = (
                f"Run still active after {timeout_seconds}s; returning a snapshot. "
                "Increase timeout_seconds, or pass detach=True to keep it running on Modal."
            )
        if result["stderr"]:
            response["stderr"] = result["stderr"]
        return response
    except Exception as e:
        logger.error(f"Failed to run Modal app '{func_ref}': {e}")
        raise


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_modal_apps(env: Optional[str] = None) -> Dict[str, Any]:
    """
    List Modal apps that are currently deployed/running or recently stopped.

    Useful for discovering the app name or ID to pass to other app tools.

    Args:
        env: Optional Modal environment to target. If omitted, uses the profile's
             default environment (or the MODAL_ENVIRONMENT variable).

    Returns:
        A dictionary containing the parsed JSON list of apps.
    """
    try:
        command = ["modal", "app", "list", "--json"]
        _add_env(command, env)
        result = run_modal_command(command)
        response = handle_json_response(result, "Failed to list apps")
        if response["success"]:
            return {"success": True, "apps": response["data"]}
        return response
    except Exception as e:
        logger.error(f"Failed to list Modal apps: {e}")
        raise


@mcp.tool()
async def get_modal_app_logs(
    app_identifier: str,
    timeout_seconds: int = 30,
    env: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tail: Optional[int] = None,
    search: Optional[str] = None,
    source: Optional[str] = None,
    follow: bool = False,
) -> Dict[str, Any]:
    """
    Fetch logs for a Modal app by name or app ID (`modal app logs`).

    By default the CLI fetches recent entries and exits. Pass `follow=True` to live-stream
    (collected for up to `timeout_seconds`, then cut off as a snapshot). Use list_modal_apps
    to discover the app name/ID.

    Args:
        app_identifier: App name (e.g. "my-app") or app ID (e.g. "ap-123456").
        timeout_seconds: Max seconds to collect logs before returning. Defaults to 30.
        env: Optional Modal environment to target.
        since: Start of time range — ISO 8601 datetime or relative time like "2h", "30m", "1d".
        until: End of time range (same formats as `since`).
        tail: Show only the last N log entries.
        search: Only include log lines matching this search text.
        source: Filter by source: "stdout", "stderr", or "system".
        follow: If True, live-stream logs until the app stops or the timeout is reached.

    Returns:
        A dictionary with the collected logs. `truncated` is True when the stream was still
        active at the timeout (i.e. logs are a partial snapshot).
    """
    try:
        command = ["modal", "app", "logs", app_identifier]
        if follow:
            command.append("-f")
        if since:
            command.extend(["--since", since])
        if until:
            command.extend(["--until", until])
        if tail is not None:
            command.extend(["--tail", str(tail)])
        if search:
            command.extend(["--search", search])
        if source:
            command.extend(["--source", source])
        _add_env(command, env)

        result = run_modal_streaming_command(command, timeout_seconds)

        # A non-zero, non-timeout exit means a genuine failure (unknown app, auth error).
        # A SIGTERM/SIGKILL from our timeout produces a negative return code, which is
        # expected when we cut off a live stream.
        failed = result["returncode"] not in (0, None) and not result["timed_out"]
        if failed:
            response = {
                "success": False,
                "error": f"Failed to get logs for '{app_identifier}' (exit {result['returncode']})",
                "command": result["command"],
            }
            if result["stdout"]:
                response["stdout"] = result["stdout"]
            if result["stderr"]:
                response["stderr"] = result["stderr"]
            return response

        response = {
            "success": True,
            "app_identifier": app_identifier,
            "logs": result["stdout"],
            "truncated": result["timed_out"],
            "command": result["command"],
        }
        if result["timed_out"]:
            response["message"] = (
                f"App is still active and streaming; returning a {timeout_seconds}s snapshot. "
                "Increase timeout_seconds for more, or stop the app for the full log."
            )
        if result["stderr"]:
            response["stderr"] = result["stderr"]
        return response
    except Exception as e:
        logger.error(f"Failed to get logs for Modal app '{app_identifier}': {e}")
        raise


@mcp.tool()
async def stop_modal_app(app_identifier: str, env: Optional[str] = None) -> Dict[str, Any]:
    """
    Permanently stop a Modal app and terminate its running containers (`modal app stop`).

    Args:
        app_identifier: App name (e.g. "my-app") or app ID (e.g. "ap-123456").
        env: Optional Modal environment to target.

    Returns:
        A dictionary containing the result of the stop operation.
    """
    try:
        # `-y` avoids the interactive confirmation prompt, which would hang with no TTY.
        command = ["modal", "app", "stop", "-y", app_identifier]
        _add_env(command, env)
        result = run_modal_command(command)
        return standardize_result(
            result, f"Successfully stopped app {app_identifier}", "Failed to stop app"
        )
    except Exception as e:
        logger.error(f"Failed to stop Modal app '{app_identifier}': {e}")
        raise


@mcp.tool()
async def rollback_modal_app(
    app_identifier: str, version: Optional[str] = None, env: Optional[str] = None
) -> Dict[str, Any]:
    """
    Roll a Modal app back to a previous deployment version (`modal app rollback`).

    Args:
        app_identifier: App name or app ID.
        version: Optional specific version to roll back to. If omitted, Modal rolls back
            to the immediately preceding version. Use get_modal_app_history to list versions.
        env: Optional Modal environment to target.

    Returns:
        A dictionary containing the result of the rollback operation.
    """
    try:
        command = ["modal", "app", "rollback", app_identifier]
        if version:
            command.append(str(version))
        _add_env(command, env)
        result = run_modal_command(command)
        return standardize_result(
            result, f"Successfully rolled back app {app_identifier}", "Failed to roll back app"
        )
    except Exception as e:
        logger.error(f"Failed to roll back Modal app '{app_identifier}': {e}")
        raise


@mcp.tool()
async def get_modal_app_history(app_identifier: str, env: Optional[str] = None) -> Dict[str, Any]:
    """
    Show a Modal app's deployment history (`modal app history`).

    Useful for finding a version to pass to rollback_modal_app.

    Args:
        app_identifier: App name or app ID.
        env: Optional Modal environment to target.

    Returns:
        A dictionary containing the parsed JSON deployment history.
    """
    try:
        command = ["modal", "app", "history", "--json", app_identifier]
        _add_env(command, env)
        result = run_modal_command(command)
        response = handle_json_response(result, "Failed to get app history")
        if response["success"]:
            return {"success": True, "history": response["data"]}
        return response
    except Exception as e:
        logger.error(f"Failed to get history for Modal app '{app_identifier}': {e}")
        raise


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_modal_containers(app_id: Optional[str] = None, env: Optional[str] = None) -> Dict[str, Any]:
    """
    List all Modal containers that are currently running (`modal container list`).

    Args:
        app_id: Optional app ID to only list containers for that app.
        env: Optional Modal environment to target.

    Returns:
        A dictionary containing the parsed JSON list of containers (IDs like "ta-...").
    """
    try:
        command = ["modal", "container", "list", "--json"]
        if app_id:
            command.extend(["--app-id", app_id])
        _add_env(command, env)
        result = run_modal_command(command)
        response = handle_json_response(result, "Failed to list containers")
        if response["success"]:
            return {"success": True, "containers": response["data"]}
        return response
    except Exception as e:
        logger.error(f"Failed to list Modal containers: {e}")
        raise


@mcp.tool()
async def get_modal_container_logs(
    container_id: str,
    timeout_seconds: int = 30,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tail: Optional[int] = None,
    search: Optional[str] = None,
    source: Optional[str] = None,
    follow: bool = False,
) -> Dict[str, Any]:
    """
    Fetch or stream logs for a specific Modal container (`modal container logs`).

    Args:
        container_id: Container ID (e.g. "ta-123456"), from list_modal_containers.
        timeout_seconds: Max seconds to collect logs before returning. Defaults to 30.
        since: Start of time range — ISO 8601 or relative like "2h", "30m", "1d".
        until: End of time range (same formats as `since`).
        tail: Show only the last N log entries.
        search: Only include log lines matching this search text.
        source: Filter by source: "stdout", "stderr", or "system".
        follow: If True, live-stream logs until the container stops or the timeout hits.

    Returns:
        A dictionary with the collected logs. `truncated` is True when the stream was cut
        off at the timeout.
    """
    try:
        command = ["modal", "container", "logs", container_id]
        if follow:
            command.append("-f")
        if since:
            command.extend(["--since", since])
        if until:
            command.extend(["--until", until])
        if tail is not None:
            command.extend(["--tail", str(tail)])
        if search:
            command.extend(["--search", search])
        if source:
            command.extend(["--source", source])

        result = run_modal_streaming_command(command, timeout_seconds)
        failed = result["returncode"] not in (0, None) and not result["timed_out"]
        if failed:
            response = {
                "success": False,
                "error": f"Failed to get logs for container '{container_id}' (exit {result['returncode']})",
                "command": result["command"],
            }
            if result["stdout"]:
                response["stdout"] = result["stdout"]
            if result["stderr"]:
                response["stderr"] = result["stderr"]
            return response

        response = {
            "success": True,
            "container_id": container_id,
            "logs": result["stdout"],
            "truncated": result["timed_out"],
            "command": result["command"],
        }
        if result["timed_out"]:
            response["message"] = (
                f"Container is still active and streaming; returning a {timeout_seconds}s snapshot."
            )
        if result["stderr"]:
            response["stderr"] = result["stderr"]
        return response
    except Exception as e:
        logger.error(f"Failed to get logs for Modal container '{container_id}': {e}")
        raise


@mcp.tool()
async def exec_modal_container(
    container_id: str, command: List[str], timeout_seconds: int = 60
) -> Dict[str, Any]:
    """
    Execute a command inside a running Modal container (`modal container exec`).

    Args:
        container_id: Container ID (e.g. "ta-123456"), from list_modal_containers.
        command: The command to run as a list of arguments,
            e.g. ["python", "-c", "print('hi')"] or ["ls", "-la", "/"].
        timeout_seconds: Max seconds to wait for the command before returning. Defaults to 60.

    Returns:
        A dictionary with the command's captured output. `truncated` is True if the
        command was still running at the timeout.
    """
    if not command:
        return {"success": False, "error": "A non-empty command list is required"}
    try:
        # `--no-pty` avoids allocating a PTY, which isn't available in this subprocess.
        full_command = ["modal", "container", "exec", "--no-pty", container_id] + command
        result = run_modal_streaming_command(full_command, timeout_seconds)
        failed = result["returncode"] not in (0, None) and not result["timed_out"]
        response = {
            "success": not failed,
            "container_id": container_id,
            "output": result["stdout"],
            "returncode": result["returncode"],
            "truncated": result["timed_out"],
            "command": result["command"],
        }
        if failed:
            response["error"] = f"Command exited with code {result['returncode']}"
        if result["stderr"]:
            response["stderr"] = result["stderr"]
        return response
    except Exception as e:
        logger.error(f"Failed to exec in Modal container '{container_id}': {e}")
        raise


@mcp.tool()
async def stop_modal_container(container_id: str) -> Dict[str, Any]:
    """
    Terminate a running Modal container (`modal container stop`).

    Sends SIGINT to the container; in-flight inputs are cancelled and rescheduled elsewhere.

    Args:
        container_id: Container ID (e.g. "ta-123456"), from list_modal_containers.

    Returns:
        A dictionary containing the result of the stop operation.
    """
    try:
        # `-y` avoids the interactive confirmation prompt.
        result = run_modal_command(["modal", "container", "stop", "-y", container_id])
        return standardize_result(
            result, f"Successfully stopped container {container_id}", "Failed to stop container"
        )
    except Exception as e:
        logger.error(f"Failed to stop Modal container '{container_id}': {e}")
        raise


# ---------------------------------------------------------------------------
# Log search (apps & containers)
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_modal_logs(
    identifier: str,
    pattern: str,
    target: str = "app",
    regex: bool = False,
    case_sensitive: bool = False,
    context_lines: int = 3,
    max_matches: int = 50,
    since: Optional[str] = None,
    tail: Optional[int] = None,
    timeout_seconds: int = 30,
    env: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Search an app's or container's logs for a pattern and return matches WITH surrounding
    context — useful for finding where something went wrong (errors, tracebacks, a request
    ID, etc.). Logs are fetched once and grepped locally, so unlike the `search` argument
    on the log tools you get the lines around each hit, not just the matching line.

    Args:
        identifier: App name/ID (e.g. "my-app", "ap-123456") or container ID ("ta-123456").
        pattern: Text (or regex, if `regex=True`) to search for, e.g. "Traceback", "Error",
            "timeout", or a request/job ID.
        target: What `identifier` refers to: "app" (default) or "container".
        regex: If True, treat `pattern` as a Python regular expression instead of literal text.
        case_sensitive: If True, match case-sensitively. Defaults to case-insensitive.
        context_lines: Number of lines to include before and after each match. Defaults to 3.
        max_matches: Cap on the number of match blocks returned. Defaults to 50.
        since: Only search logs newer than this — ISO 8601 or relative like "2h", "1d".
        tail: Only search the last N log entries. If neither `since` nor `tail` is given,
            the last 1000 entries are searched.
        timeout_seconds: Max seconds to spend fetching logs before searching. Defaults to 30.
        env: Optional Modal environment (apps only).

    Returns:
        A dictionary with `match_count` (total hits), `matches` (a list of context blocks,
        each a string with line numbers; matched lines are prefixed with ">"), and
        `returned` (how many blocks are included after `max_matches`).
    """
    if target not in ("app", "container"):
        return {"success": False, "error": "target must be 'app' or 'container'"}
    if not pattern:
        return {"success": False, "error": "A non-empty search pattern is required"}
    try:
        subcommand = "app" if target == "app" else "container"
        command = ["modal", subcommand, "logs", identifier]
        if since:
            command.extend(["--since", since])
        if tail is not None:
            command.extend(["--tail", str(tail)])
        if since is None and tail is None:
            # Search a generous window by default so debugging isn't limited to ~100 lines.
            command.extend(["--tail", "1000"])
        if target == "app":
            _add_env(command, env)

        result = run_modal_streaming_command(command, timeout_seconds)
        failed = result["returncode"] not in (0, None) and not result["timed_out"]
        if failed:
            response = {
                "success": False,
                "error": f"Failed to fetch logs for '{identifier}' (exit {result['returncode']})",
                "command": result["command"],
            }
            if result["stderr"]:
                response["stderr"] = result["stderr"]
            return response

        # Modal writes log lines to stdout; some builds emit them on stderr — search both.
        log_text = result["stdout"] or result["stderr"] or ""
        total, blocks = grep_lines(
            log_text, pattern, regex, case_sensitive, context_lines, max_matches
        )
        if total is None:
            # grep_lines returned an error message (e.g. bad regex) in `blocks`.
            return {"success": False, "error": blocks, "command": result["command"]}

        response = {
            "success": True,
            "target": target,
            "identifier": identifier,
            "pattern": pattern,
            "match_count": total,
            "returned": len(blocks),
            "matches": blocks,
            "logs_truncated": result["timed_out"],
            "command": result["command"],
        }
        if total == 0:
            response["message"] = (
                f"No matches for {pattern!r} in the fetched logs. Try a broader pattern, "
                "increase `tail`/`since`, or set regex=True."
            )
        elif len(blocks) < total:
            response["message"] = (
                f"Showing the first {len(blocks)} of {total} matches; increase max_matches for more."
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
# Volumes — file operations
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_modal_volumes() -> Dict[str, Any]:
    """
    List all Modal volumes using the Modal CLI with JSON output.

    Returns:
        A dictionary containing the parsed JSON output of the Modal volumes list.
    """
    try:
        result = run_modal_command(["modal", "volume", "list", "--json"])
        response = handle_json_response(result, "Failed to list volumes")
        if response["success"]:
            return {"success": True, "volumes": response["data"]}
        return response
    except Exception as e:
        logger.error(f"Failed to list Modal volumes: {e}")
        raise


@mcp.tool()
async def list_modal_volume_contents(volume_name: str, path: str = "/") -> Dict[str, Any]:
    """
    List files and directories in a Modal volume.

    Args:
        volume_name: Name of the Modal volume to list contents from.
        path: Path within the volume to list contents from. Defaults to root ("/").

    Returns:
        A dictionary containing the parsed JSON output of the volume contents.
    """
    try:
        result = run_modal_command(["modal", "volume", "ls", "--json", volume_name, path])
        response = handle_json_response(result, "Failed to list volume contents")
        if response["success"]:
            return {"success": True, "contents": response["data"]}
        return response
    except Exception as e:
        logger.error(f"Failed to list Modal volume contents: {e}")
        raise


@mcp.tool()
async def copy_modal_volume_files(volume_name: str, paths: List[str]) -> Dict[str, Any]:
    """
    Copy files within a Modal volume. Can copy a source file to a destination file
    or multiple source files to a destination directory.

    Args:
        volume_name: Name of the Modal volume to perform copy operation in.
        paths: List of paths for the copy operation. The last path is the destination,
              all others are sources. For example: ["source1.txt", "source2.txt", "dest_dir/"]

    Returns:
        A dictionary containing the result of the copy operation.
    """
    if len(paths) < 2:
        return {
            "success": False,
            "error": "At least one source and one destination path are required"
        }

    try:
        result = run_modal_command(["modal", "volume", "cp", volume_name] + paths)
        return standardize_result(
            result, f"Successfully copied files in volume {volume_name}", "Failed to copy files"
        )
    except Exception as e:
        logger.error(f"Failed to copy files in Modal volume: {e}")
        raise


@mcp.tool()
async def remove_modal_volume_file(volume_name: str, remote_path: str, recursive: bool = False) -> Dict[str, Any]:
    """
    Delete a file or directory from a Modal volume.

    Args:
        volume_name: Name of the Modal volume to delete from.
        remote_path: Path to the file or directory to delete.
        recursive: If True, delete directories recursively. Required for deleting directories.

    Returns:
        A dictionary containing the result of the delete operation.
    """
    try:
        command = ["modal", "volume", "rm"]
        if recursive:
            command.append("-r")
        command.extend([volume_name, remote_path])

        result = run_modal_command(command)
        return standardize_result(
            result,
            f"Successfully deleted {remote_path} from volume {volume_name}",
            f"Failed to delete {remote_path}",
        )
    except Exception as e:
        logger.error(f"Failed to delete from Modal volume: {e}")
        raise


@mcp.tool()
async def put_modal_volume_file(volume_name: str, local_path: str, remote_path: str = "/", force: bool = False) -> Dict[str, Any]:
    """
    Upload a file or directory to a Modal volume.

    Args:
        volume_name: Name of the Modal volume to upload to.
        local_path: Path to the local file or directory to upload.
        remote_path: Path in the volume to upload to. Defaults to root ("/").
                    If ending with "/", it's treated as a directory and the file keeps its name.
        force: If True, overwrite existing files. Defaults to False.

    Returns:
        A dictionary containing the result of the upload operation.
    """
    try:
        command = ["modal", "volume", "put"]
        if force:
            command.append("-f")
        command.extend([volume_name, local_path, remote_path])

        result = run_modal_command(command)
        return standardize_result(
            result,
            f"Successfully uploaded {local_path} to {volume_name}:{remote_path}",
            f"Failed to upload {local_path}",
        )
    except Exception as e:
        logger.error(f"Failed to upload to Modal volume: {e}")
        raise


@mcp.tool()
async def get_modal_volume_file(volume_name: str, remote_path: str, local_destination: str = ".", force: bool = False) -> Dict[str, Any]:
    """
    Download files from a Modal volume.

    Args:
        volume_name: Name of the Modal volume to download from.
        remote_path: Path to the file or directory in the volume to download.
        local_destination: Local path to save the downloaded file(s). Defaults to current directory.
                         Use "-" to write file contents to stdout.
        force: If True, overwrite existing files. Defaults to False.

    Returns:
        A dictionary containing the result of the download operation.
    """
    try:
        command = ["modal", "volume", "get"]
        if force:
            command.append("--force")
        command.extend([volume_name, remote_path, local_destination])

        result = run_modal_command(command)
        return standardize_result(
            result,
            f"Successfully downloaded {remote_path} from volume {volume_name}",
            f"Failed to download {remote_path}",
        )
    except Exception as e:
        logger.error(f"Failed to download from Modal volume: {e}")
        raise


# ---------------------------------------------------------------------------
# Volumes — lifecycle
# ---------------------------------------------------------------------------

@mcp.tool()
async def create_modal_volume(volume_name: str, env: Optional[str] = None) -> Dict[str, Any]:
    """
    Create a named, persistent Modal volume (`modal volume create`).

    Args:
        volume_name: Name for the new volume.
        env: Optional Modal environment to create the volume in.

    Returns:
        A dictionary containing the result of the create operation.
    """
    try:
        command = ["modal", "volume", "create", volume_name]
        _add_env(command, env)
        result = run_modal_command(command)
        return standardize_result(
            result, f"Successfully created volume {volume_name}", "Failed to create volume"
        )
    except Exception as e:
        logger.error(f"Failed to create Modal volume '{volume_name}': {e}")
        raise


@mcp.tool()
async def delete_modal_volume(volume_name: str, env: Optional[str] = None) -> Dict[str, Any]:
    """
    Delete a named Modal volume and ALL of its data (`modal volume delete`).

    This is irreversible — the entire volume and its contents are removed. To delete
    individual files instead, use remove_modal_volume_file.

    Args:
        volume_name: Name of the volume to delete.
        env: Optional Modal environment to target.

    Returns:
        A dictionary containing the result of the delete operation.
    """
    try:
        # `-y` avoids the interactive confirmation prompt.
        command = ["modal", "volume", "delete", "-y", volume_name]
        _add_env(command, env)
        result = run_modal_command(command)
        return standardize_result(
            result, f"Successfully deleted volume {volume_name}", "Failed to delete volume"
        )
    except Exception as e:
        logger.error(f"Failed to delete Modal volume '{volume_name}': {e}")
        raise


@mcp.tool()
async def rename_modal_volume(old_name: str, new_name: str, env: Optional[str] = None) -> Dict[str, Any]:
    """
    Rename a Modal volume (`modal volume rename`).

    Args:
        old_name: Current volume name.
        new_name: New volume name.
        env: Optional Modal environment to target.

    Returns:
        A dictionary containing the result of the rename operation.
    """
    try:
        # `-y` avoids the interactive confirmation prompt.
        command = ["modal", "volume", "rename", "-y", old_name, new_name]
        _add_env(command, env)
        result = run_modal_command(command)
        return standardize_result(
            result, f"Successfully renamed volume {old_name} to {new_name}", "Failed to rename volume"
        )
    except Exception as e:
        logger.error(f"Failed to rename Modal volume '{old_name}': {e}")
        raise


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_modal_secrets(env: Optional[str] = None) -> Dict[str, Any]:
    """
    List your published Modal secrets (`modal secret list`). Only names and timestamps
    are returned — secret values are never exposed by the CLI.

    Args:
        env: Optional Modal environment to target.

    Returns:
        A dictionary containing the parsed JSON list of secrets.
    """
    try:
        command = ["modal", "secret", "list", "--json"]
        _add_env(command, env)
        result = run_modal_command(command)
        response = handle_json_response(result, "Failed to list secrets")
        if response["success"]:
            return {"success": True, "secrets": response["data"]}
        return response
    except Exception as e:
        logger.error(f"Failed to list Modal secrets: {e}")
        raise


@mcp.tool()
async def create_modal_secret(
    secret_name: str,
    key_values: Optional[Dict[str, str]] = None,
    from_dotenv: Optional[str] = None,
    from_json: Optional[str] = None,
    force: bool = False,
    env: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a Modal secret (`modal secret create`). Provide the key/value pairs inline,
    or load them from a local .env or JSON file. Secret values are redacted from the
    returned `command` field.

    Args:
        secret_name: Name for the secret.
        key_values: Mapping of secret keys to values, e.g. {"API_KEY": "abc", "DB_URL": "..."}.
        from_dotenv: Path to a local .env file to load key/values from (`--from-dotenv`).
        from_json: Path to a local JSON file to load key/values from (`--from-json`).
        force: If True, overwrite the secret if it already exists (`--force`).
        env: Optional Modal environment to create the secret in.

    Returns:
        A dictionary containing the result of the create operation, with values redacted.
    """
    if not key_values and not from_dotenv and not from_json:
        return {
            "success": False,
            "error": "Provide key_values, from_dotenv, or from_json to create a secret",
        }
    try:
        command = ["modal", "secret", "create", secret_name]
        if key_values:
            command.extend([f"{k}={v}" for k, v in key_values.items()])
        if from_dotenv:
            command.extend(["--from-dotenv", from_dotenv])
        if from_json:
            command.extend(["--from-json", from_json])
        if force:
            command.append("--force")
        _add_env(command, env)

        result = run_modal_command(command)
        # Redact KEY=VALUE pairs so secret values never appear in the returned command.
        result["command"] = ' '.join(
            re.sub(r"=.*", "=***", part) if _KEYVALUE_RE.match(part) else part
            for part in result["command"].split(' ')
        )
        return standardize_result(
            result, f"Successfully created secret {secret_name}", "Failed to create secret"
        )
    except Exception as e:
        logger.error(f"Failed to create Modal secret '{secret_name}': {e}")
        raise


@mcp.tool()
async def delete_modal_secret(secret_name: str, env: Optional[str] = None) -> Dict[str, Any]:
    """
    Delete a named Modal secret (`modal secret delete`).

    Args:
        secret_name: Name of the secret to delete.
        env: Optional Modal environment to target.

    Returns:
        A dictionary containing the result of the delete operation.
    """
    try:
        # `-y` avoids the interactive confirmation prompt.
        command = ["modal", "secret", "delete", "-y", secret_name]
        _add_env(command, env)
        result = run_modal_command(command)
        return standardize_result(
            result, f"Successfully deleted secret {secret_name}", "Failed to delete secret"
        )
    except Exception as e:
        logger.error(f"Failed to delete Modal secret '{secret_name}': {e}")
        raise


# ---------------------------------------------------------------------------
# Discovery — who am I, what environments exist
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_modal_profile() -> Dict[str, Any]:
    """
    Show the active Modal profile and all configured profiles (`modal profile current`
    + `modal profile list`). Use this to confirm which workspace/account the server is
    authenticated as before running account-scoped operations.

    Returns:
        A dictionary with the active profile name and the parsed JSON list of profiles.
    """
    try:
        current = run_modal_command(["modal", "profile", "current"])
        listing = run_modal_command(["modal", "profile", "list", "--json"])

        response: Dict[str, Any] = {"success": current["success"] and listing["success"]}
        if current["success"]:
            response["active_profile"] = current["stdout"].strip()
        profiles = handle_json_response(listing, "Failed to list profiles")
        if profiles["success"]:
            response["profiles"] = profiles["data"]
        elif "error" not in response:
            response["error"] = profiles.get("error")
        if not response["success"] and "error" not in response:
            response["error"] = current.get("error") or listing.get("error")
        return response
    except Exception as e:
        logger.error(f"Failed to get Modal profile: {e}")
        raise


@mcp.tool()
async def list_modal_environments() -> Dict[str, Any]:
    """
    List all environments in the current Modal workspace (`modal environment list`).

    Environments are sub-divisions of a workspace (e.g. "dev" vs "production"), each with
    its own apps and secrets. The names returned here are valid `env` arguments for the
    other tools.

    Returns:
        A dictionary containing the parsed JSON list of environments.
    """
    try:
        result = run_modal_command(["modal", "environment", "list", "--json"])
        response = handle_json_response(result, "Failed to list environments")
        if response["success"]:
            return {"success": True, "environments": response["data"]}
        return response
    except Exception as e:
        logger.error(f"Failed to list Modal environments: {e}")
        raise


def main() -> None:
    """Console-script entry point for the mcp-modal package."""
    mcp.run()


if __name__ == "__main__":
    main()
