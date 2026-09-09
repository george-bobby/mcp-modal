"""Tool modules. Importing this package registers all 12 tools on the shared server.

Each module decorates its functions with `@mcp.tool(...)`, so registration is a side
effect of import — that is why these names are imported here but not otherwise used.
"""
from . import apps, costs, deploy, logs, resources, secrets, volumes  # noqa: F401

__all__ = ["apps", "costs", "deploy", "logs", "resources", "secrets", "volumes"]
