# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`mcp-modal` is an MCP (Model Context Protocol) server that exposes 12 tools and 4 prompts for managing Modal (apps, containers, volumes, secrets) and for deploying/running Modal apps. It is published to PyPI as `mcp-modal` and is meant to be launched by MCP clients via `uvx mcp-modal`.

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

### Module layout

```
src/mcp_modal/
  server.py      assembly point: imports the pieces, re-exports the public names, main()
  app.py         the FastMCP instance (`mcp`), `logger`, and _read_only/_mutating
  command.py     argv helpers (_add_env, _uv_prefixed, path allowlist) + the two runners
  output.py      output caps and the response envelope (handle_json_response, json_listing…)
  logsearch.py   pure log-text logic: grep_lines, filter_log_lines, cap_blocks, window checks
  billing.py     pure cost math: Decimal helpers, _row_field aliases, group_costs, cost_movers
  prompts.py     the 4 @mcp.prompt() workflows
  tools/         the 12 @mcp.tool() functions, one module per group
    deploy.py resources.py logs.py apps.py volumes.py secrets.py costs.py
```

The dependency graph runs strictly one way: `app` → `command` → `output` → `logsearch` → `tools`/`prompts` → `server`, with `billing` depending on nothing but the stdlib. (`output` imports `command` because `json_listing` runs the command itself, and `logsearch` imports `output` for the character budget `cap_blocks` spends.) Nothing under `tools/` is imported by a support module, and `app.py` imports nothing from the package — keep it that way and there are no cycles to reason about.

Tools and prompts register by *decorator side effect*, so a module that is never imported silently disappears from the server. `tools/__init__.py` imports all seven tool modules and `server.py` imports `tools` and `prompts` — **a new tool module must be added to `tools/__init__.py` or its tools won't exist.** After any change here, check the inventory is still 12 tools and 4 prompts (`mcp.list_tools()` / `mcp.list_prompts()`).

`server.py` re-exports every tool, prompt and helper and lists them in `__all__`, so `from mcp_modal.server import search_modal_logs` (and `group_costs`, `grep_lines`, …) keeps working — that's the import path the README and any ad-hoc script uses, and the console script `mcp_modal.server:main` depends on it too. Add new public names to those re-exports.

`logger` is `logging.getLogger("mcp_modal")` in `app.py` rather than `__name__` per module, so log lines keep one stable name no matter which module emits them.

### Tool grouping (why there are 12 tools, not 28)

Every tool schema is loaded into the client's context for the entire session, so the tool surface is a standing token cost and a standing "which of these near-identical tools do I want?" problem for the model. Related CLI subcommands are therefore grouped behind an `action`/`resource` argument:

- `list_modal_resources(resource=...)` — every read-only lookup (apps, app_history, containers, volumes, volume_files, secrets, environments, profile).
- `manage_modal_app` (stop/rollback), `manage_modal_container` (exec/stop), `manage_modal_volume` (create/delete/rename), `modal_volume_files` (put/get/cp/rm), `manage_modal_secret` (create/delete).
- `deploy_modal_app`, `run_modal_app`, `get_modal_logs`, `search_modal_logs`, `analyze_modal_costs`, `inspect_modal_secret` keep their own tools — distinct enough that folding them in would only make the schemas harder to read.

Prefer adding an `action` to an existing group over adding a 13th tool (new group → new module under `tools/`, registered in `tools/__init__.py`). Every grouped tool validates its `action` up front and returns a `{"success": False, "error": ...}` naming the valid values; keep that pattern, and keep docstrings terse — the docstring *is* the schema description the model pays for.

### Tool annotations (`app.py`)

Each `@mcp.tool()` passes `annotations=` built by `_read_only()` or `_mutating()` so clients can auto-approve safe lookups and prompt on the rest. `readOnlyHint=True` is a real promise: only put it on a tool that runs no mutating subcommand. `_mutating(destructive=False)` is for calls that don't remove or overwrite anything (currently only `run_modal_app`).

### The core idea: shell out to `modal`

The server holds no Modal SDK state. Every tool builds an argv list, runs the local `modal` CLI as a subprocess, and shapes the stdout/stderr into a standard response. Credentials, profiles, and environments come from the host's `~/.modal.toml`. Every tool also takes an optional `profile` argument that scopes that call to another profile via the CLI's `MODAL_PROFILE` override — the stored active profile is never rewritten, so concurrent calls with different profiles are safe.

There are two execution modes for account-scoped vs project-scoped commands, controlled by a single helper:

- **Account-scoped** (apps, containers, volumes, secrets, profiles, environments) — run the plain `modal` binary on the host.
- **Project-scoped** (`deploy_modal_app`, `run_modal_app`) — wrap the command in `uv run --directory=<project_dir>` so Modal executes inside the *target project's* virtualenv. This is why those tools require `absolute_path_to_app` and why the target project must use `uv` with `modal` installed in its venv. `uv_directory` is the only difference; `_uv_prefixed` adds the `uv run --directory=...` prefix when set, otherwise leaves the command untouched.

### Two subprocess runners (`command.py`)

Tools call exactly one of these:

- `run_modal_command` — blocking `subprocess.run(check=True)`. Use for commands that terminate on their own (list, create, delete, history, rollback, …).
- `run_modal_streaming_command` — `Popen` with `start_new_session=True`, bounded by `timeout_seconds`. On timeout it sends `SIGTERM` to the whole process group (then `SIGKILL` after 5s), captures partial output, and sets `timed_out=True`. The new session is required because under `uv run` the real `modal` process is a grandchild; killing only the direct child would orphan it. Use this for anything that streams or may hang: `modal app logs --follow`, `modal run`, `modal container logs`, `modal container exec`.

A tool that uses the streaming runner returns `truncated: true` when output was cut off at the deadline — callers are expected to interpret that as "still running, ask again" rather than failure.

### Response shape (`output.py`)

Three helpers produce the standard envelope so every tool returns the same shape:

- `handle_json_response` — for `modal … --json` commands; parses stdout into `{"success": True, "data": …}`.
- `json_listing` — wraps the above for `list_modal_resources`: runs the command, renames `data` → `apps`/`volumes`/`contents`/etc, and caps the list at `_MAX_LIST_ITEMS` (200) with an `omitted_items` count.
- `standardize_result` — for action commands (deploy/stop/create/rm/rename); produces `{success, message, command, stdout?, stderr?}` or the error variant.
- Streaming tools build their own response directly because they need the `truncated` / `output` / `urls` fields.

### Output caps (`output.py`)

The streaming runner is bounded by *time*, not volume, so a chatty app can emit megabytes inside a 30s window. Text that goes back to the client is therefore capped:

- `_cap_text` keeps a head and a tail slice (snapped to line boundaries) with a marker naming how much was dropped; `_add_capped(response, key, text)` is how tools set a text field and flag `output_capped: true`.
- The budget is `MCP_MODAL_MAX_OUTPUT_CHARS` (default 40000, floor 1000, `0` disables capping).
- Cap at the *response* boundary, never before parsing or searching: `handle_json_response` must see intact JSON, and `search_modal_logs` greps the full log and then caps only the returned blocks (`cap_blocks`) so `match_count` stays exact.

`extract_urls` scrubs http(s) links from stdout+stderr so deploy/run tools can surface live web-endpoint URLs in a dedicated `urls` field — this is the main way clients discover what got deployed.

### Cost analysis (`tools/costs.py`, math in `billing.py`)

Same shape as log search: fetch once, compute locally. `modal billing report --json` returns one flat row per (app, interval) — 1500+ rows for a week on a busy workspace — so the tool sums and ranks them itself rather than handing the caller arithmetic. `group_costs` aggregates by app/environment/resource/interval; `cost_movers` finds the most expensive interval and diffs it against the preceding one, which is what actually answers "why was Monday expensive". Costs are `Decimal` throughout: summing hundreds of 8-decimal strings as floats drifts.

Two version landmines, both handled and both worth knowing before you touch this code:

- **Column names change between clients.** modal 1.4.x emits Title Case with spaces (`"Object ID"`, `"Interval Start"`, `"Cost"`); 1.5+ emits snake_case. Reading one spelling silently yields a report full of zeros — the failure is invisible, not loud. Every field goes through `_row_field()` / `_COST_FIELD_ALIASES`; add both spellings when you add a field.
- **`billing summary` and `billing rates` only exist in modal >= 1.5.** Older clients fail with a bare `Error: No such command`, which the tool turns into an upgrade hint. `pyproject.toml` pins `modal>=1.5` for this reason.

Billing is workspace-wide: `modal billing report` accepts no `-e/--env`, so never route it through `_add_env` — the environment arrives as a row field and is filtered locally.

### Secret key inspection (`inspect_modal_secret`, in `tools/secrets.py`)

The one tool that spends money. Modal exposes secret key names nowhere — not the CLI, not `Secret.info()` (name/created_at/created_by only), not even the gRPC `SecretMetadata` message. The only route is to mount the secret in a container and list the environment, so this runs `modal shell --secret <name>` and subtracts `_BASE_ENV_NAMES` / `_BASE_ENV_PREFIXES` (captured from a real container, and covering the `MODAL_TOKEN_*` credentials that live there too).

The probe string is fragile in a specific way: **`modal shell -c` shlex-splits the string, re-joins it with spaces, and runs it under `bash -c`.** Quotes do not survive, and unquoted parentheses are a bash syntax error — so a `python -c "..."` probe cannot work. Hence `compgen -e`, a bash builtin that prints exported variable *names* only, which also guarantees no value is printed even inside the container. If you change `_KEYS_PROBE`, it must contain no quotes and no shell metacharacters beyond `;`.

Because it starts compute, it is annotated `_mutating(destructive=False)` and kept out of `list_modal_resources` — putting it there would break that tool's `readOnlyHint` promise.

### Log search (`tools/logs.py`, text logic in `logsearch.py`)

This is the only non-trivial bit of logic beyond shelling out. It fetches logs once (via the same streaming runner against `modal app logs` / `modal container logs`), then runs everything locally:

- `filter_log_lines` drops `exclude`-matching lines first (used to strip known noise before grepping for signal).
- `grep_lines` does `grep -C`-style context: builds `[i-ctx, i+ctx]` windows around each match, merges overlapping/adjacent windows into single blocks, formats each line as `> N: …` (match) or `  N: …` (context) with 1-based line numbers.

Volume, not matching, is the practical failure mode. `modal app|container logs` has two modes: `--since` **without** `--tail` is *range mode* and fetches every entry in the range (measured: 664KB for 6h on one moderately busy app, vs 0 bytes for a bounded `--since 6h --until 5h`), while any `--tail` is *tail mode* anchored at `--until` or now. So a one-sided `since` is what makes a search blow the 30s fetch deadline and the output budget — which is why both log tools take `until`, why their messages tell the caller to bound the window rather than raise `timeout_seconds`, and why `search_modal_logs` has `prefilter` (passes `--search <pattern>`, a server-side substring filter added in modal 1.5, so non-matching lines are never fetched; requires `regex=False`, and it destroys real context, hence opt-in and flagged with `prefiltered`/`note` in the response).

Window errors are UsageErrors — exit 2 with `Error: ...` on stderr (reversed range, range over 35 days, `tail` over 20000 = `modal._logs._FETCH_LIMIT`). `_check_log_window` catches what can be checked without parsing relative times, and `_log_failure_error` lifts the CLI's own `Error:` line into the `error` field so the caller sees "range cannot exceed 35 days" rather than "exit 2".

In `search_modal_logs` responses, `returned` counts matched *lines* shown and `returned_blocks` counts blocks: `grep_lines` merges adjacent windows, so with `context_lines=0` 150 consecutive matches collapse into one block, and counting blocks would report that as "showing 1 of 150".

If you change log behavior, note: stdout/stderr/system are the only streams Modal exposes via `app logs` — failure events shown on the Modal dashboard (e.g. "… exited with …") are *not* log lines and will not appear in `search_modal_logs` results. This is called out in tool docstrings and the README and should stay accurate.

### Argument plumbing (`command.py`)

A few tiny helpers keep argv construction consistent across the tools:

- `_add_env(command, env)` — appends `-e <env>` when the caller passed an environment. Only call it for subcommands that actually accept `-e/--env`. **All `modal volume` subcommands do** (create, delete, rename, list, ls, put, get, cp, rm — volumes are environment-scoped, so omitting it silently targets the default environment). **`modal container logs|exec|stop` do not** — only `modal container list` takes `-e`, because a container ID is already globally unique. Check the CLI reference before adding it to a new command.
- `_profile_env(profile)` — returns the child `env=` mapping selecting a profile via `MODAL_PROFILE`, or `None` (plain inheritance) when omitted. Both runners take `profile=` and pass it through; `json_listing` takes it too, since it runs the command itself. Never implement profile selection as `modal profile activate` — that rewrites `~/.modal.toml` and races under concurrent calls.
- `_uv_prefixed(command, uv_directory)` — see above.
- `_resolve_log_target(identifier, target)` — maps `target="auto"` to app vs container from the `ta-` ID prefix, so the log tools take either kind of identifier.

When adding a new action, follow the existing pattern: validate the action, build the argv with these helpers, pick the right runner, route through the matching response helper, and document it in the docstring (the README's tool list mirrors those docstrings — keep them in sync).

### Prompts (`prompts.py`)

Four `@mcp.prompt()` functions (`debug_modal_app`, `deploy_and_verify`, `review_modal_account`, `investigate_modal_costs`) return workflow text. Clients fetch prompts on demand, so they cost nothing in per-session tool schema — that makes them the right home for multi-step guidance (and for caveats like "dashboard crash events are not log lines") instead of repeating it in every tool description. Keep the tool names inside prompt text in sync when tools change.

## Packaging notes

- `pyproject.toml` declares the `mcp-modal` console script → `mcp_modal.server:main`, which just calls `mcp.run()` (FastMCP's stdio loop).
- The `mcp` dependency is pinned `>=1.9.2,<2` on purpose. `mcp` 2.x renamed `FastMCP` to `MCPServer` and moved the module, so `from mcp.server.fastmcp import FastMCP` raises `ModuleNotFoundError` there. `uvx mcp-modal` resolves dependencies fresh from PyPI metadata (it does not read `uv.lock`), so an unbounded specifier would break every new install the day 2.x is picked up.
- `server.json` is the MCP registry manifest. Its `version` must match `pyproject.toml`'s `version` on every release — bump both together. Same for `packages[0].version` inside `server.json`.
- `uv.lock` is committed. Update it via `uv sync` or `uv lock` when dependencies change.
- `[tool.hatch.build.targets.wheel] packages = ["src/mcp_modal"]` ships the whole package directory, so `tools/` is included with no extra config. Worth a `uv build` + a peek inside the wheel after adding a subpackage.
