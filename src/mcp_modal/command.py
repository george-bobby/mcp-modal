"""Building and running `modal` CLI commands.

Every tool ends up here: it assembles an argv list with the `_add_env` / `_uv_prefixed`
helpers and hands it to one of the two runners. `run_modal_command` is for commands that
terminate on their own; `run_modal_streaming_command` is for anything that streams or may
hang, and is bounded by a timeout rather than by output volume.

Per-call profile selection rides on the `MODAL_PROFILE` environment variable, which the
Modal CLI honors over the stored active profile — so targeting another profile never
rewrites `~/.modal.toml` and stays safe under concurrent calls.
"""
import os
import signal
import subprocess
from typing import Any, Dict, List, Optional

from .app import logger


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


# The Modal CLI resolves the profile from MODAL_PROFILE first, falling back to the
# stored active profile in ~/.modal.toml. Scoping the override to the child's
# environment (rather than `modal profile activate`, which rewrites the file) keeps
# per-call selection isolated — concurrent calls with different profiles can't race.
_MODAL_PROFILE_ENV = "MODAL_PROFILE"


def _profile_env(profile: Optional[str]) -> Optional[Dict[str, str]]:
    """Build the child environment selecting `profile`, or None to inherit.

    Returns None (plain inheritance) when no profile is given, so the default path —
    active profile from ~/.modal.toml — is byte-for-byte unchanged.
    """
    if not profile:
        return None
    return {**os.environ, _MODAL_PROFILE_ENV: profile}


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


def run_modal_command(
    command: List[str],
    uv_directory: Optional[str] = None,
    redact: Optional[List[str]] = None,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a Modal CLI command to completion and return the result.

    `redact`, if given, is a list of secret values scrubbed from the logged command and
    from every returned text field (command/stdout/stderr/error) — see _redact_text.

    `profile`, if given, selects the Modal profile for this call only (via
    MODAL_PROFILE); the stored active profile is never changed.

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
            env=_profile_env(profile),
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
    command: List[str],
    timeout_seconds: int,
    uv_directory: Optional[str] = None,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a Modal CLI command that may stream indefinitely (e.g. `modal app logs`, `modal serve`).

    Captures whatever output is produced within `timeout_seconds`. If the command is
    still running at the deadline (i.e. it was streaming), the whole process group is
    terminated and the partial output is returned with timed_out=True.

    `profile`, if given, selects the Modal profile for this call only (via
    MODAL_PROFILE); the stored active profile is never changed.
    """
    full_command = _uv_prefixed(command, uv_directory)
    proc = subprocess.Popen(
        full_command,
        stdin=subprocess.DEVNULL,  # never block on an unexpected interactive prompt
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_profile_env(profile),
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
