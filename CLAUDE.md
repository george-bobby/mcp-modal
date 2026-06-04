# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`mcp-modal` is an MCP (Model Context Protocol) server that exposes 26 tools for managing Modal (apps, containers, volumes, secrets) and for deploying/running Modal apps. It is published to PyPI as `mcp-modal` and is meant to be launched by MCP clients via `uvx mcp-modal`.

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

The whole server is one file: `src/mcp_modal/server.py` (~1350 lines, 26 `@mcp.tool()` functions). Everything else is packaging.

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

- `handle_json_response` — for `modal … --json` commands; parses stdout into `{"success": True, "data": …}` (the tool then renames `data` → `apps`/`volumes`/etc).
- `standardize_result` — for action commands (deploy/stop/create/rm/rename); produces `{success, message, command, stdout?, stderr?}` or the error variant.
- Streaming tools build their own response directly because they need the `truncated` / `output` / `urls` fields.

`extract_urls` scrubs http(s) links from stdout+stderr so deploy/run tools can surface live web-endpoint URLs in a dedicated `urls` field — this is the main way clients discover what got deployed.

### Log search (`search_modal_logs`)

This is the only non-trivial bit of logic beyond shelling out. It fetches logs once (via the same streaming runner against `modal app logs` / `modal container logs`), then runs everything locally:

- `filter_log_lines` drops `exclude`-matching lines first (used to strip known noise before grepping for signal).
- `grep_lines` does `grep -C`-style context: builds `[i-ctx, i+ctx]` windows around each match, merges overlapping/adjacent windows into single blocks, formats each line as `> N: …` (match) or `  N: …` (context) with 1-based line numbers.

If you change log behavior, note: stdout/stderr/system are the only streams Modal exposes via `app logs` — failure events shown on the Modal dashboard (e.g. "… exited with …") are *not* log lines and will not appear in `search_modal_logs` results. This is called out in tool docstrings and the README and should stay accurate.

### Argument plumbing

Two tiny helpers keep argv construction consistent across all 26 tools:

- `_add_env(command, env)` — appends `-e <env>` when the caller passed an environment. Every account-scoped tool accepts an optional `env` and threads it through this.
- `_uv_prefixed(command, uv_directory)` — see above.

When adding a new tool, follow the existing pattern: build the argv with these helpers, pick the right runner, route through the matching response helper, and document the params in the docstring (the README's tool list mirrors those docstrings — keep them in sync).

## Packaging notes

- `pyproject.toml` declares the `mcp-modal` console script → `mcp_modal.server:main`, which just calls `mcp.run()` (FastMCP's stdio loop).
- `server.json` is the MCP registry manifest. Its `version` must match `pyproject.toml`'s `version` on every release — bump both together. Same for `packages[0].version` inside `server.json`.
- `uv.lock` is committed. Update it via `uv sync` or `uv lock` when dependencies change.
