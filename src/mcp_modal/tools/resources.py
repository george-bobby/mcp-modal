"""`list_modal_resources` — every read-only "what exists / what is this" lookup."""
from typing import Any, Dict, Optional

from ..app import logger, mcp, _read_only
from ..command import _add_env, run_modal_command
from ..output import handle_json_response, json_listing


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
    profile: Optional[str] = None,
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
        profile: Modal profile for this call only. Defaults to the active profile.

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
            return json_listing(command, "apps", "Failed to list apps", profile=profile)

        if resource == "app_history":
            command = ["modal", "app", "history", "--json"]
            _add_env(command, env)
            command.extend(["--", name])
            return json_listing(
                command, "history", "Failed to get app history", profile=profile, app_identifier=name
            )

        if resource == "containers":
            command = ["modal", "container", "list", "--json"]
            if name:
                command.extend(["--app-id", name])
            _add_env(command, env)
            return json_listing(command, "containers", "Failed to list containers", profile=profile)

        if resource == "volumes":
            command = ["modal", "volume", "list", "--json"]
            _add_env(command, env)
            return json_listing(command, "volumes", "Failed to list volumes", profile=profile)

        if resource == "volume_files":
            command = ["modal", "volume", "ls", "--json"]
            _add_env(command, env)
            command.extend(["--", name, path])
            response = json_listing(
                command,
                "contents",
                "Failed to list volume contents",
                profile=profile,
                volume_name=name,
                path=path,
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
            return json_listing(command, "secrets", "Failed to list secrets", profile=profile)

        if resource == "environments":
            return json_listing(
                ["modal", "environment", "list", "--json"],
                "environments",
                "Failed to list environments",
                profile=profile,
            )

        # resource == "profile"
        # `profile` scopes these two calls too, so the response reflects the effective
        # profile (the override), not just the stored default.
        current = run_modal_command(["modal", "profile", "current"], profile=profile)
        listing = run_modal_command(["modal", "profile", "list", "--json"], profile=profile)
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
