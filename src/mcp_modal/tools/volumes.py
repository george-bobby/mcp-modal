"""Volume lifecycle (`manage_modal_volume`) and file moves (`modal_volume_files`).

All `modal volume` subcommands take `-e/--env`: volumes are environment-scoped, so
omitting it silently targets the default environment.
"""
from typing import Any, Dict, List, Optional

from ..app import logger, mcp, _mutating
from ..command import _add_env, _check_local_path, run_modal_command
from ..output import standardize_result


@mcp.tool(annotations=_mutating("Create, delete or rename a Modal volume"))
async def manage_modal_volume(
    action: str,
    volume_name: str,
    new_name: Optional[str] = None,
    env: Optional[str] = None,
    profile: Optional[str] = None,
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
        profile: Modal profile for this call only. Defaults to the active profile.

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
            result = run_modal_command(command, profile=profile)
            return standardize_result(
                result, f"Successfully created volume {volume_name}", "Failed to create volume"
            )

        if action == "delete":
            # `-y` avoids the interactive confirmation prompt.
            command = ["modal", "volume", "delete", "-y"]
            _add_env(command, env)
            command.extend(["--", volume_name])
            result = run_modal_command(command, profile=profile)
            return standardize_result(
                result, f"Successfully deleted volume {volume_name}", "Failed to delete volume"
            )

        # `-y` avoids the interactive confirmation prompt.
        command = ["modal", "volume", "rename", "-y"]
        _add_env(command, env)
        command.extend(["--", volume_name, new_name])
        result = run_modal_command(command, profile=profile)
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
    profile: Optional[str] = None,
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
        profile: Modal profile for this call only. Defaults to the active profile.

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
            result = run_modal_command(command, profile=profile)
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
            result = run_modal_command(command, profile=profile)
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
            result = run_modal_command(command, profile=profile)
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
        result = run_modal_command(command, profile=profile)
        return standardize_result(
            result,
            f"Successfully deleted {remote_path} from volume {volume_name}",
            f"Failed to delete {remote_path}",
        )
    except Exception as e:
        logger.error(f"Failed to {action} files in Modal volume '{volume_name}': {e}")
        raise
