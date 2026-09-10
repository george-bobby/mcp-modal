"""Secret tools: create/delete (`manage_modal_secret`) and key-name inspection.

`inspect_modal_secret` is the one tool that spends money. Modal exposes secret key names
nowhere in the CLI or SDK, so the only route is to mount the secret in a container and
list the environment — see the probe notes below.
"""
from typing import Any, Dict, List, Optional

from ..app import logger, mcp, _mutating
from ..command import _add_env, run_modal_command, run_modal_streaming_command
from ..output import _add_capped, standardize_result


@mcp.tool(annotations=_mutating("Create or delete a Modal secret"))
async def manage_modal_secret(
    action: str,
    secret_name: str,
    key_values: Optional[Dict[str, str]] = None,
    from_dotenv: Optional[str] = None,
    from_json: Optional[str] = None,
    force: bool = False,
    env: Optional[str] = None,
    profile: Optional[str] = None,
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
        profile: Modal profile for this call only. Defaults to the active profile.

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
            result = run_modal_command(command, profile=profile)
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
        result = run_modal_command(command, redact=secret_values, profile=profile)
        return standardize_result(
            result, f"Successfully created secret {secret_name}", "Failed to create secret"
        )
    except Exception as e:
        logger.error(f"Failed to {action} Modal secret '{secret_name}': {e}")
        raise


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
    profile: Optional[str] = None,
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
        profile: Modal profile for this call only. Defaults to the active profile.
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

        result = run_modal_streaming_command(command, timeout_seconds, profile=profile)
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
