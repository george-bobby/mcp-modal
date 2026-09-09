"""The shared FastMCP server object and the tool-annotation policy.

Everything else in the package registers against the `mcp` instance defined here, so
this module must not import any of them — it is the bottom of the dependency graph.
"""
import logging

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

logger = logging.getLogger("mcp_modal")

mcp = FastMCP("modal-deploy")


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
