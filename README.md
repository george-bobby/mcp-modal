<!-- mcp-name: io.github.george-bobby/mcp-modal -->

# MCP Modal Server

[![mcp-modal MCP server](https://glama.ai/mcp/servers/george-bobby/mcp-modal/badges/card.svg)](https://glama.ai/mcp/servers/george-bobby/mcp-modal)

[![PyPI](https://img.shields.io/pypi/v/mcp-modal.svg)](https://pypi.org/project/mcp-modal/)

An MCP server for managing [Modal](https://modal.com) — apps, containers, volumes, and secrets — and for deploying & running Modal apps directly from [Claude Code](https://docs.claude.com/en/docs/claude-code) and other MCP clients.


Every tool shells out to your local `modal` CLI, so it operates against whatever Modal profile and credentials are configured on your machine. There are no extra tokens to manage.

## Installation

The server is published on PyPI as [`mcp-modal`](https://pypi.org/project/mcp-modal/). No manual install is needed — the recommended way to run it is with [`uvx`](https://docs.astral.sh/uv/), which fetches and launches it on demand. Just point your MCP client at the command below (see [Configuration](#configuration)).

Every version is also tagged and published on the
[Releases page](https://github.com/george-bobby/mcp-modal/releases), with release notes and
the same `.whl` / `.tar.gz` that PyPI serves attached — useful for pinning, air-gapped
installs, or reading what changed between two versions.

## Logging in to Modal

This server uses your local Modal credentials. If you haven't authenticated yet, run:

```bash
modal setup
```

This opens a browser to log in and stores a token in `~/.modal.toml`. Already logged in elsewhere? Check with `modal profile current`.

## Configuration

Add the server to Claude Code with the `claude mcp` CLI:

```bash
claude mcp add mcp-modal -- uvx mcp-modal@latest
```

Or add it to a `.mcp.json` file in your project root, which is the better option for a team
— everyone who opens the repo gets the same configuration:

```json
{
  "mcpServers": {
    "mcp-modal": {
      "command": "uvx",
      "args": ["mcp-modal@latest"]
    }
  }
}
```

### Why `@latest`, and when to pin instead

`uvx` caches the environment it builds on the first run and **does not check PyPI again**:

> "uvx will use the latest available version of the requested tool on the first invocation.
> After that, uvx will use the cached version of the tool unless a different version is
> requested, the cache is pruned, or the cache is refreshed."
> — [uv docs](https://docs.astral.sh/uv/concepts/tools/)

So a plain `uvx mcp-modal` means *latest at install time, frozen forever after* — restarting
the client or rebooting changes nothing, because the cache lives on disk. Different people
end up on different versions depending on when they first ran it, with no warning.

- **`mcp-modal@latest`** re-resolves on every launch, so a restart picks up new releases.
  Costs one network round-trip at startup. Use it while the tool surface is still moving.
- **`mcp-modal@0.4.0`** (an explicit version) is reproducible and upgrades become a
  deliberate one-line change. Use it once you want stability, or for a wider audience.

To move a machine that is already stuck on an old cached build, switching it to either form
above is enough — requesting a version invalidates the cache. Otherwise
`uv cache clean mcp-modal` forces a refresh.

## Requirements

- Python 3.11 or higher
- [`uv`](https://docs.astral.sh/uv/) (provides `uvx`)
- Modal CLI 1.5 or newer, configured with valid credentials (`modal setup`) — 1.5 is
  where `modal billing summary`/`rates` landed and where the billing report switched to
  snake_case columns; the cost tool reads both spellings but needs 1.5 for those two views
- For Modal **deploy** and **run** support:
  - The project being deployed/run must use `uv` for dependency management
  - `modal` must be installed in that project's virtual environment

## Security

This server shells out to your local `modal` CLI using whatever credentials are in
`~/.modal.toml`. A few tools are powerful by design — if the MCP client driving the server
is ever prompt-injected (for example by malicious text inside logs it fetched), these are
the escalation paths and should stay **behind your client's tool-approval prompts** rather
than being auto-approved:

- **`deploy_modal_app` / `run_modal_app`** — execute arbitrary local Python on the host
  (`modal deploy` imports the app file; `uv run` resolves and installs the target project's
  dependencies).
- **`modal_volume_files`** with `action="put"` — can read any local file (e.g. `~/.ssh/id_rsa`,
  `~/.modal.toml`) and upload it to a cloud volume (a data-exfiltration primitive).
- **`modal_volume_files`** with `action="get"` and `force=True` — can overwrite any local path
  (e.g. `~/.zshrc` or a shell profile, a persistence primitive).
- **`manage_modal_container`** with `action="exec"` — runs arbitrary commands inside a
  container, by design.

Every tool declares [MCP tool annotations](https://modelcontextprotocol.io/specification/server/tools#tool-annotations),
so a client can distinguish the four read-only tools (`list_modal_resources`,
`get_modal_logs`, `search_modal_logs`, `analyze_modal_costs` — all `readOnlyHint: true`)
from the eight that change remote state or start compute. Six of those eight are
`destructiveHint: true`; the exceptions are `run_modal_app` and `inspect_modal_secret`,
which start compute without removing or overwriting anything. Auto-approve the reads; keep
the rest behind a prompt.

### Optional local-path allowlist

To contain the two filesystem-touching volume tools, set the
`MCP_MODAL_ALLOWED_LOCAL_PATHS` environment variable to an
[`os.pathsep`](https://docs.python.org/3/library/os.html#os.pathsep)-separated list of
directories (`:` on macOS/Linux). When it is set, `modal_volume_files` is refused for any
local path — `local_path` on `action="put"`, the destination on `action="get"` — unless the
resolved path, after expanding `~` and collapsing `..`/symlinks, falls inside one of those
roots. The download target `"-"` (return contents instead of writing a file) is exempt
because nothing is written to disk.

When the variable is **unset (the default) there is no restriction**, so existing setups are
unaffected. Configure it in your MCP client, e.g.:

```json
{
  "mcpServers": {
    "mcp-modal": {
      "command": "uvx",
      "args": ["mcp-modal"],
      "env": { "MCP_MODAL_ALLOWED_LOCAL_PATHS": "/Users/me/modal-workspace:/tmp/modal" }
    }
  }
}
```

All tools also pass user-supplied names/paths after a `--` end-of-options separator, so a
value beginning with `-` is always treated as data, never as a `modal` CLI flag. Secret
values handed to `manage_modal_secret` are redacted from the echoed command, logs, and any
error output.

## Supported Tools

12 tools. Related operations are grouped behind an `action`/`resource` argument rather than
split one-per-CLI-subcommand: every tool schema is loaded into the model's context for the
whole session, so a smaller surface leaves more room for your actual work (and gives the
model fewer near-identical tools to choose between).

Tools that talk to environment-scoped resources take an optional `env` argument to target a
specific [Modal environment](https://modal.com/docs/guide/environments); if omitted, they
use the profile's default (or `MODAL_ENVIRONMENT`). The exception is `manage_modal_container`
and container logs — a container ID is globally unique and the CLI accepts no environment
there.

Every tool also takes an optional `profile` argument to run that call against a different
Modal profile (account) without switching your stored default: `list_modal_resources`
with `resource="apps", profile="work"` lists that profile's apps, while `modal profile
current` on your machine still reports the same default afterwards. Selection is scoped
to the single call (via the CLI's `MODAL_PROFILE` override), so concurrent calls with
different profiles can't interfere. To change the stored default itself, use
`modal profile activate <name>` in a terminal; to see all profiles, use
`list_modal_resources(resource="profile")`.

### Read-only

1. **List Modal Resources** (`list_modal_resources`) — one lookup for the whole account.
   - Parameters: `resource` (required), `name`, `path` (default `/`), `env`, `profile`
   - `resource` values:
     | value | returns | `name` means |
     | --- | --- | --- |
     | `apps` | deployed/running/recently-stopped apps | — |
     | `app_history` | one app's deployment versions (for rollback) | app name/ID |
     | `containers` | running containers (`ta-...`) | app ID to filter by |
     | `volumes` | named volumes | — |
     | `volume_files` | files inside a volume (with `path`) | volume name |
     | `secrets` | secret names (values are never exposed) | — |
     | `environments` | valid `env` values for this workspace | — |
     | `profile` | active profile + all profiles | — |
   - `volume_files` sets `empty: true` with a message when a listing genuinely returns
     nothing, so an empty directory is distinguishable from a wrong path.
   - Listings over 200 entries are capped, with `omitted_items` giving the number dropped.

2. **Get Modal Logs** (`get_modal_logs`) — fetch or stream logs for an app *or* a container.
   - Parameters: `identifier` (required), `target` (`auto`/`app`/`container`, default
     `auto` — anything starting `ta-` is a container), `timeout_seconds` (default 30),
     `env`, `profile`, `since`, `until`, `tail`, `source` (`stdout`/`stderr`/`system`),
     `timestamps`, `follow`
   - `since` without `tail` fetches *every* entry in the range; pass `until` as well (max
     range 35 days, `tail` max 20,000) to keep a busy app's output bounded.
   - With `follow=True`, logs stream until the app/container stops or `timeout_seconds` is
     reached, returning a snapshot with `truncated: true`.
   - Only covers the stdout/stderr/system streams; some failures (e.g. a crash reported as
     "... exited with ...") are Modal dashboard events, not log lines, and won't appear here.

3. **Search Modal Logs** (`search_modal_logs`) — grep logs and get each hit **with the
   surrounding lines**, built for "where did it go wrong?" debugging. Logs are fetched once
   and searched locally, so you get context, regex, case control, and exact match counts.
   - Parameters: `identifier` (required), `pattern` (required), `target` (default `auto`),
     `regex`, `case_sensitive`, `context_lines` (default 3), `max_matches` (default 50),
     `since`, `until`, `tail` (defaults to the last 1000 entries), `source`,
     `exclude` (drop noise lines before searching, e.g. `"queue put failed"`),
     `prefilter`, `timestamps` (default `true`), `timeout_seconds`, `env`, `profile`
   - **Bound the window on a busy app.** `since` on its own fetches everything from then
     until now — hundreds of KB per hour on a chatty app, which the 30s fetch cuts off
     (`logs_truncated: true`) and the output budget trims. `since` *and* `until` around the
     minute you care about is the fix, and is usually kilobytes.
   - `prefilter=True` pushes `pattern` down to Modal as a server-side substring filter
     (`modal app logs --search`), so non-matching lines are never fetched — the lever for
     logs too large to drain. Requires `regex=False`, and context lines then show only
     other matches, so use it to locate the window and re-query it with `prefilter=False`.
   - Returns `match_count` and `matches`: timestamped, line-numbered context blocks where
     matched lines are prefixed with `>`, e.g. `> 8: 2026-06-04T... ValueError: bad input`.
     The whole fetched log is always searched, so `match_count` stays exact even when fewer
     blocks are returned. `returned` is how many matches came back (adjacent matches merge
     into one block, counted by `returned_blocks`). Reports `excluded_lines` when `exclude`
     is used.
   - A window the CLI rejects (reversed range, over 35 days, `tail` over 20,000) comes back
     as `success: false` with Modal's own message, not a bare exit code.
   - Same stdout/stderr/system-only caveat as `get_modal_logs`.

### Deploy & run

4. **Deploy Modal App** (`deploy_modal_app`)
   - Deploys a Modal app (`modal deploy`). Deployed web endpoints persist, so any links in
     the output are live and shareable (returned in `urls`).
   - Parameters: `absolute_path_to_app` (required), `env`, `profile`, `name`, `tag`,
     `strategy` (`rolling`/`recreate`), `stream_logs`
   - The app's directory must use `uv` with `modal` installed in its virtualenv.

5. **Run Modal App** (`run_modal_app`)
   - Runs a function or local entrypoint once and collects its output (`modal run`).
   - Parameters: `absolute_path_to_app` (required), `function_name`, `env`, `profile`,
     `detach`, `timeout_seconds` (default 120)
   - Returns a snapshot with `truncated: true` if the run is still going at the timeout.
     Pass `detach=True` to keep long jobs alive on Modal past the timeout.

> **Why no `modal serve` tool?** `modal serve` only keeps its endpoints alive while the
> blocking process runs — an MCP tool that returns would tear them down immediately,
> handing back a dead URL. Use `deploy_modal_app` for a persistent, shareable endpoint.

### State changes

6. **Manage Modal App** (`manage_modal_app`) — `action` is `stop` (shut the app down and
   terminate its containers) or `rollback` (redeploy a previous version).
   - Parameters: `action` (required), `app_identifier` (required), `version` (rollback
     only — defaults to the immediately preceding version), `env`, `profile`

7. **Manage Modal Container** (`manage_modal_container`) — `action` is `exec` (run a command
   inside a running container, `modal container exec --no-pty`) or `stop` (terminate it).
   - Parameters: `action` (required), `container_id` (required), `command` (exec only —
     a list of args, e.g. `["python", "-c", "print('hi')"]`), `timeout_seconds` (default 60),
     `profile`

8. **Manage Modal Volume** (`manage_modal_volume`) — `action` is `create`, `delete`
   (the volume **and all its data**, irreversible), or `rename`.
   - Parameters: `action` (required), `volume_name` (required), `new_name` (rename only), `env`,
     `profile`

9. **Modal Volume Files** (`modal_volume_files`) — write operations on a volume's files:
   `action` is `put` (upload), `get` (download), `cp` (copy inside the volume), or `rm`.
   - Parameters: `action` (required), `volume_name` (required), `local_path`, `remote_path`,
     `paths` (for `cp`: sources then destination), `recursive`, `force`, `env`, `profile`
   - `action="get"` with `local_path="-"` returns the file contents instead of writing a file.
   - To *list* a volume's contents use `list_modal_resources(resource="volume_files")`.

10. **Manage Modal Secret** (`manage_modal_secret`) — `action` is `create` or `delete`.
    - Parameters: `action` (required), `secret_name` (required), `key_values` (dict),
      `from_dotenv` (path), `from_json` (path), `force`, `env`, `profile`. Creating requires at least
      one of `key_values`, `from_dotenv`, or `from_json`.
    - Secret values are redacted from every field returned, including error output.
    - To list secret names use `list_modal_resources(resource="secrets")`.

### Costs

11. **Analyze Modal Costs** (`analyze_modal_costs`) — read-only. Fetches
    `modal billing` once and aggregates locally, so you get ranked totals and
    period-over-period changes instead of hundreds of raw rows.
    - Parameters: `view` (default `by_app`), `period`, `start`, `end`, `resolution`
      (`d`/`h`), `timezone`, `app`, `environment`, `top_n` (default 10), `tag_names`, `profile`
    - `view` values:
      | value | answers |
      | --- | --- |
      | `by_app` | "what is my costliest app?" — apps ranked by spend, with % share |
      | `timeline` | "why was Monday expensive?" — cost per interval, plus an `explanation` that diffs the peak interval against the one before and ranks which apps grew |
      | `by_environment` | which environment the money goes to |
      | `by_resource` | CPU vs GPU class vs memory vs storage |
      | `summary` | billed vs metered cost for a month cycle, with credits/plan adjustments |
      | `rates` | current unit prices |
    - `total_cost` always covers every row in range, even when `groups` is cut to
      `top_n` — quote it rather than summing the visible rows.
    - Billing is workspace-wide (the CLI takes no `-e`), so this reports across all
      environments; `environment` filters the rows afterwards.
    - Modal reports **whole intervals only**, so a partially elapsed day reads low.

### Secrets — inspection

12. **Inspect Modal Secret** (`inspect_modal_secret`) — lists the **key names** inside a
    secret, never the values.
    - Parameters: `secret_name` (required), `env`, `profile`, `image`, `timeout_seconds` (default 300)
    - Modal exposes no API for this by design: not the CLI, not the SDK, not the gRPC
      layer. The only way to see which keys a secret defines is to mount it in a container
      and list the environment. So this tool runs `modal shell --secret <name>` with
      `compgen -e` (a bash builtin that prints exported variable *names* only — no value is
      ever printed, even inside the container), then subtracts the variables the image and
      Modal runtime set anyway — 23 known names plus anything under six prefixes
      (`MODAL_`, `PYTHON`, `PIP_`, `NVIDIA_`, `CUDA_`, `LD_LIBRARY_PATH`), which also
      covers the `MODAL_TOKEN_*` credentials that live in every container.
    - **This one call starts remote compute**, so it costs a few cents and takes tens of
      seconds (longer when the image has to build). Every other read in this server is
      free; use `list_modal_resources(resource="secrets")` to see *which* secrets exist and
      reach for this only when you need to know what is *inside* one.
    - Returns `keys`, plus the unfiltered `all_env_names` so a key that looks like a
      runtime variable is still visible rather than silently dropped.
    - Omit `image` to use Modal's default (built to match the server's Python — the most
      reliable choice). Pass one, e.g. `python:3.12-slim`, if your workspace's image
      builder rejects that Python version.

## Prompts

The server also ships four MCP prompts — multi-step workflows your client can invoke
directly (in Claude Code they appear as `/mcp__mcp-modal__<name>`). Prompts are fetched on
demand, so unlike tools they cost nothing in per-session context:

- **`debug_modal_app`** (`app_name`, optional `symptom`) — an ordered triage routine: check
  the app is up, search logs for tracebacks with context, narrow the window instead of
  widening it when a log fetch comes back truncated, fall back to the log tail, check
  whether sibling apps were hit in the same window, inspect containers, then compare
  against deployment history and consider a rollback.
- **`deploy_and_verify`** (`absolute_path_to_app`, optional `env`) — confirm the target
  workspace, deploy, report the live URLs, then *verify* the app is healthy instead of
  assuming it.
- **`review_modal_account`** (optional `env`) — a read-only inventory that flags idle apps,
  unexplained running containers, and orphaned volumes/secrets, naming the exact call that
  would clean each one up without running it.
- **`investigate_modal_costs`** (optional `period`, `app`) — traces a spend increase from
  the daily timeline down to the peak hour, the resource class, and the deploy or
  still-running container behind it.

## Output caps

Log, run, and exec output is capped before it is returned, so one chatty app can't flood
your context window. The default budget is 40,000 characters per text field (roughly 10k
tokens); when a field is trimmed the result sets `output_capped: true` and the text carries
a marker naming how much was dropped. A capped field keeps its head *and* its tail, so a
startup banner and the traceback at the end both survive.

Searching is never capped before the fact: `search_modal_logs` greps the whole fetched log
and only limits how many context blocks come back, so `match_count` is always exact.

Raising `timeout_seconds` or the budget is rarely the right answer to a truncated log
search — fetching less is. Bound the window with `since` **and** `until`, filter with
`source`/`exclude`, or set `prefilter=True` to drop non-matching lines inside Modal.

Set `MCP_MODAL_MAX_OUTPUT_CHARS` to raise or lower the budget, or to `0` to disable capping
entirely:

```json
{
  "mcpServers": {
    "mcp-modal": {
      "command": "uvx",
      "args": ["mcp-modal"],
      "env": { "MCP_MODAL_MAX_OUTPUT_CHARS": "80000" }
    }
  }
}
```

## Response Format

All tools return responses in a standardized format, with slight variations depending on the operation type:

```python
# Lookups (list_modal_resources):
{
    "success": True,
    "apps": [...],          # or "containers", "volumes", "contents", "secrets", ...
    "omitted_items": 0      # present when the listing was capped at 200 entries
}

# Action operations (deploy, stop, rollback, create, delete, rename, cp, put, get, rm):
{
    "success": True,
    "message": "Operation successful message",
    "command": "executed command string",
    "stdout": "command output",  # if any
    "stderr": "error output"     # if any
}

# Log / run / exec operations (snapshot-based):
{
    "success": True,
    "logs": "...",          # or "output" for run/exec
    "truncated": False,     # True when cut off at timeout_seconds
    "output_capped": False, # True when text was trimmed to fit MCP_MODAL_MAX_OUTPUT_CHARS
    "command": "executed command string"
}

# Log search (search_modal_logs):
{
    "success": True,
    "match_count": 12,      # exact: the whole fetched log is searched
    "returned": 5,          # matches actually shown
    "returned_blocks": 2,   # adjacent matches merge into one context block
    "matches": ["> 8: ...", ...],
    "logs_truncated": False,  # True when the log fetch hit timeout_seconds
    "output_capped": False,
    "command": "executed command string"
}

# Error case (all operations):
{
    "success": False,
    "error": "Error message describing what went wrong",
    "command": "executed command string",
    "stdout": "command output",  # if available
    "stderr": "error output"     # if available
}
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
