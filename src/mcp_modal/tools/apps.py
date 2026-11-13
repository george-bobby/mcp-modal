"""State changes for apps and containers (stop, rollback, exec)."""
from typing import Any, Dict, List, Optional

from ..app import logger, mcp, _mutating
from ..command import _add_env, run_modal_command, run_modal_streaming_command
from ..output import _add_capped, standardize_result


@mcp.tool(annotations=_mutating("Stop or roll back a Modal app"))
async def manage_modal_app(
    action: str,
    app_identifier: str,
    version: Optional[str] = None,
    env: Optional[str] = None,
    profile: Optional[str] = None,
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
        profile: Modal profile for this call only. Defaults to the active profile.

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
            result = run_modal_command(command, profile=profile)
            return standardize_result(
                result, f"Successfully stopped app {app_identifier}", "Failed to stop app"
            )

        command = ["modal", "app", "rollback"]
        _add_env(command, env)
        command.extend(["--", app_identifier])
        if version:
            command.append(str(version))
        result = run_modal_command(command, profile=profile)
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
    profile: Optional[str] = None,
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
        profile: Modal profile for this call only. Defaults to the active profile.

    Returns: exec → {output, returncode, truncated, output_capped}; stop → {message}.
    """
    if action not in ("exec", "stop"):
        return {"success": False, "error": "action must be 'exec' or 'stop'"}
    try:
        if action == "stop":
            if command:
                return {"success": False, "error": "`command` is only valid with action='exec'"}
            # `-y` avoids the interactive confirmation prompt.
            result = run_modal_command(
                ["modal", "container", "stop", "-y", "--", container_id], profile=profile
            )
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
        result = run_modal_streaming_command(full_command, timeout_seconds, profile=profile)
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
