# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`mcp-modal` is an MCP (Model Context Protocol) server that exposes 10 tools and 3 prompts for managing Modal (apps, containers, volumes, secrets) and for deploying/running Modal apps. It is published to PyPI as `mcp-modal` and is meant to be launched by MCP clients via `uvx mcp-modal`.

## Common commands

```bash
# Install/sync deps (creates .venv/)
uv sync

# Run the server locally (stdio transport — for testing it speaks MCP)
uv run mcp-modal

# Build the wheel/sdist (hatchling backend)
uv build

# Smoke-test against a real Modal account — the server shells out to the local
# `modal` CLI, so credentials must already be configured:
modal setup            # one-time login → ~/.modal.toml
modal profile current  # verify which workspace you're using
```

There is **no test suite, linter config, or CI** in this repo. Don't claim a change is verified by tests — exercise it by running the server against a real Modal account, or by inspecting the constructed CLI commands in the returned `command` field.

## Architecture

The whole server is one file: `src/mcp_modal/server.py` (~1300 lines, 10 `@mcp.tool()` functions plus 3 `@mcp.prompt()` functions). Everything else is packaging.

### Tool grouping (why there are 10 tools, not 26)

Every tool schema is loaded into the client's context for the entire session, so the tool surface is a standing token cost and a standing "which of these near-identical tools do I want?" problem for the model. Related CLI subcommands are therefore grouped behind an `action`/`resource` argument:

- `list_modal_resources(resource=...)` — every read-only lookup (apps, app_history, containers, volumes, volume_files, secrets, environments, profile).
- `manage_modal_app` (stop/rollback), `manage_modal_container` (exec/stop), `manage_modal_volume` (create/delete/rename), `modal_volume_files` (put/get/cp/rm), `manage_modal_secret` (create/delete).
- `deploy_modal_app`, `run_modal_app`, `get_modal_logs`, `search_modal_logs` keep their own tools — distinct enough that folding them in would only make the schemas harder to read.

Prefer adding an `action` to an existing group over adding an 11th tool. Every grouped tool validates its `action` up front and returns a `{"success": False, "error": ...}` naming the valid values; keep that pattern, and keep docstrings terse — the docstring *is* the schema description the model pays for.

### Tool annotations

Each `@mcp.tool()` passes `annotations=` built by `_read_only()` or `_mutating()` so clients can auto-approve safe lookups and prompt on the rest. `readOnlyHint=True` is a real promise: only put it on a tool that runs no mutating subcommand. `_mutating(destructive=False)` is for calls that don't remove or overwrite anything (currently only `run_modal_app`).

### The core idea: shell out to `modal`

The server holds no Modal SDK state. Every tool builds an argv list, runs the local `modal` CLI as a subprocess, and shapes the stdout/stderr into a standard response. Credentials, profiles, and environments come from the host's `~/.modal.toml`.

There are two execution modes for account-scoped vs project-scoped commands, controlled by a single helper:

- **Account-scoped** (apps, containers, volumes, secrets, profiles, environments) — run the plain `modal` binary on the host.
- **Project-scoped** (`deploy_modal_app`, `run_modal_app`) — wrap the command in `uv run --directory=<project_dir>` so Modal executes inside the *target project's* virtualenv. This is why those tools require `absolute_path_to_app` and why the target project must use `uv` with `modal` installed in its venv. `uv_directory` is the only difference; `_uv_prefixed` adds the `uv run --directory=...` prefix when set, otherwise leaves the command untouched.

### Two subprocess runners

Tools call exactly one of these:

- `run_modal_command` — blocking `subprocess.run(check=True)`. Use for commands that terminate on their own (list, create, delete, history, rollback, …).
- `run_modal_streaming_command` — `Popen` with `start_new_session=True`, bounded by `timeout_seconds`. On timeout it sends `SIGTERM` to the whole process group (then `SIGKILL` after 5s), captures partial output, and sets `timed_out=True`. The new session is required because under `uv run` the real `modal` process is a grandchild; killing only the direct child would orphan it. Use this for anything that streams or may hang: `modal app logs --follow`, `modal run`, `modal container logs`, `modal container exec`.

A tool that uses the streaming runner returns `truncated: true` when output was cut off at the deadline — callers are expected to interpret that as "still running, ask again" rather than failure.

### Response shape

Three helpers produce the standard envelope so every tool returns the same shape:

- `handle_json_response` — for `modal … --json` commands; parses stdout into `{"success": True, "data": …}`.
- `json_listing` — wraps the above for `list_modal_resources`: runs the command, renames `data` → `apps`/`volumes`/`contents`/etc, and caps the list at `_MAX_LIST_ITEMS` (200) with an `omitted_items` count.
- `standardize_result` — for action commands (deploy/stop/create/rm/rename); produces `{success, message, command, stdout?, stderr?}` or the error variant.
- Streaming tools build their own response directly because they need the `truncated` / `output` / `urls` fields.

### Output caps

The streaming runner is bounded by *time*, not volume, so a chatty app can emit megabytes inside a 30s window. Text that goes back to the client is therefore capped:

- `_cap_text` keeps a head and a tail slice (snapped to line boundaries) with a marker naming how much was dropped; `_add_capped(response, key, text)` is how tools set a text field and flag `output_capped: true`.
- The budget is `MCP_MODAL_MAX_OUTPUT_CHARS` (default 40000, floor 1000, `0` disables capping).
- Cap at the *response* boundary, never before parsing or searching: `handle_json_response` must see intact JSON, and `search_modal_logs` greps the full log and then caps only the returned blocks (`cap_blocks`) so `match_count` stays exact.

`extract_urls` scrubs http(s) links from stdout+stderr so deploy/run tools can surface live web-endpoint URLs in a dedicated `urls` field — this is the main way clients discover what got deployed.

### Log search (`search_modal_logs`)

This is the only non-trivial bit of logic beyond shelling out. It fetches logs once (via the same streaming runner against `modal app logs` / `modal container logs`), then runs everything locally:

- `filter_log_lines` drops `exclude`-matching lines first (used to strip known noise before grepping for signal).
- `grep_lines` does `grep -C`-style context: builds `[i-ctx, i+ctx]` windows around each match, merges overlapping/adjacent windows into single blocks, formats each line as `> N: …` (match) or `  N: …` (context) with 1-based line numbers.

If you change log behavior, note: stdout/stderr/system are the only streams Modal exposes via `app logs` — failure events shown on the Modal dashboard (e.g. "… exited with …") are *not* log lines and will not appear in `search_modal_logs` results. This is called out in tool docstrings and the README and should stay accurate.

### Argument plumbing

A few tiny helpers keep argv construction consistent across the tools:

- `_add_env(command, env)` — appends `-e <env>` when the caller passed an environment. Only call it for subcommands that actually accept `-e/--env`. **All `modal volume` subcommands do** (create, delete, rename, list, ls, put, get, cp, rm — volumes are environment-scoped, so omitting it silently targets the default environment). **`modal container logs|exec|stop` do not** — only `modal container list` takes `-e`, because a container ID is already globally unique. Check the CLI reference before adding it to a new command.
- `_uv_prefixed(command, uv_directory)` — see above.
- `_resolve_log_target(identifier, target)` — maps `target="auto"` to app vs container from the `ta-` ID prefix, so the log tools take either kind of identifier.

When adding a new action, follow the existing pattern: validate the action, build the argv with these helpers, pick the right runner, route through the matching response helper, and document it in the docstring (the README's tool list mirrors those docstrings — keep them in sync).

### Prompts

Three `@mcp.prompt()` functions (`debug_modal_app`, `deploy_and_verify`, `review_modal_account`) return workflow text. Clients fetch prompts on demand, so they cost nothing in per-session tool schema — that makes them the right home for multi-step guidance (and for caveats like "dashboard crash events are not log lines") instead of repeating it in every tool description. Keep the tool names inside prompt text in sync when tools change.

## Packaging notes

- `pyproject.toml` declares the `mcp-modal` console script → `mcp_modal.server:main`, which just calls `mcp.run()` (FastMCP's stdio loop).
- The `mcp` dependency is pinned `>=1.9.2,<2` on purpose. `mcp` 2.x renamed `FastMCP` to `MCPServer` and moved the module, so `from mcp.server.fastmcp import FastMCP` raises `ModuleNotFoundError` there. `uvx mcp-modal` resolves dependencies fresh from PyPI metadata (it does not read `uv.lock`), so an unbounded specifier would break every new install the day 2.x is picked up.
- `server.json` is the MCP registry manifest. Its `version` must match `pyproject.toml`'s `version` on every release — bump both together. Same for `packages[0].version` inside `server.json`.
- `uv.lock` is committed. Update it via `uv sync` or `uv lock` when dependencies change.
