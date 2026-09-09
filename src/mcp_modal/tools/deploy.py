"""Deploy and run tools — the two project-scoped commands.

These wrap the CLI in `uv run --directory=<project>` so Modal executes inside the target
project's virtualenv, which is why both require an absolute path to the app.
"""
import os
from typing import Any, Dict, Optional

from ..app import logger, mcp, _mutating
from ..command import _add_env, run_modal_command, run_modal_streaming_command
from ..output import _add_capped, extract_urls, standardize_result


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
