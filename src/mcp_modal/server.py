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

The implementation is split by concern; this module is the assembly point that imports
the pieces (registering every tool and prompt on the shared server as a side effect) and
exposes the console-script entry point. Public names are re-exported below so
`from mcp_modal.server import ...` keeps working.

* `app` — the FastMCP instance and the tool-annotation policy.
* `command` — argv construction and the two subprocess runners.
* `output` — output caps and the standard response envelope.
* `logsearch` / `billing` — the pure local logic behind log search and cost aggregation.
* `tools/` — the 12 tools, one module per group; `prompts` — the 4 prompt workflows.
"""
from . import prompts, tools  # noqa: F401  (imported for registration side effects)
from .app import logger, mcp, _mutating, _read_only  # noqa: F401
from .billing import cost_movers, group_costs
from .command import (  # noqa: F401
    _add_env,
    _check_local_path,
    _uv_prefixed,
    run_modal_command,
    run_modal_streaming_command,
)
from .logsearch import (  # noqa: F401
    _check_log_window,
    _log_failure_error,
    _resolve_log_target,
    cap_blocks,
    filter_log_lines,
    grep_lines,
)
from .output import (  # noqa: F401
    _add_capped,
    _cap_text,
    _max_output_chars,
    extract_urls,
    handle_json_response,
    json_listing,
    standardize_result,
)
from .prompts import (
    debug_modal_app,
    deploy_and_verify,
    investigate_modal_costs,
    review_modal_account,
)
from .tools.apps import manage_modal_app, manage_modal_container
from .tools.costs import analyze_modal_costs
from .tools.deploy import deploy_modal_app, run_modal_app
from .tools.logs import get_modal_logs, search_modal_logs
from .tools.resources import list_modal_resources
from .tools.secrets import inspect_modal_secret, manage_modal_secret
from .tools.volumes import manage_modal_volume, modal_volume_files

__all__ = [
    "main",
    "mcp",
    # tools
    "analyze_modal_costs",
    "deploy_modal_app",
    "get_modal_logs",
    "inspect_modal_secret",
    "list_modal_resources",
    "manage_modal_app",
    "manage_modal_container",
    "manage_modal_secret",
    "manage_modal_volume",
    "modal_volume_files",
    "run_modal_app",
    "search_modal_logs",
    # prompts
    "debug_modal_app",
    "deploy_and_verify",
    "investigate_modal_costs",
    "review_modal_account",
    # helpers used by the tools (kept importable from here for compatibility)
    "cap_blocks",
    "cost_movers",
    "extract_urls",
    "filter_log_lines",
    "grep_lines",
    "group_costs",
    "handle_json_response",
    "json_listing",
    "run_modal_command",
    "run_modal_streaming_command",
    "standardize_result",
]


def main() -> None:
    """Console-script entry point for the mcp-modal package."""
    mcp.run()


if __name__ == "__main__":
    main()
