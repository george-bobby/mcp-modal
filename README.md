<!-- mcp-name: io.github.george-bobby/mcp-modal -->

# MCP Modal Server

[![mcp-modal MCP server](https://glama.ai/mcp/servers/george-bobby/mcp-modal/badges/card.svg)](https://glama.ai/mcp/servers/george-bobby/mcp-modal)

[![PyPI](https://img.shields.io/pypi/v/mcp-modal.svg)](https://pypi.org/project/mcp-modal/)

An MCP server for managing [Modal](https://modal.com) — apps, containers, volumes, and secrets — and for deploying & running Modal apps directly from [Claude Code](https://docs.claude.com/en/docs/claude-code) and other MCP clients.


Every tool shells out to your local `modal` CLI, so it operates against whatever Modal profile and credentials are configured on your machine. There are no extra tokens to manage.

## Installation

The server is published on PyPI as [`mcp-modal`](https://pypi.org/project/mcp-modal/). No manual install is needed — the recommended way to run it is with [`uvx`](https://docs.astral.sh/uv/), which fetches and launches it on demand. Just point your MCP client at the command below (see [Configuration](#configuration)).

## Logging in to Modal

This server uses your local Modal credentials. If you haven't authenticated yet, run:

```bash
modal setup
```

This opens a browser to log in and stores a token in `~/.modal.toml`. Already logged in elsewhere? Check with `modal profile current`.

## Configuration

Add the server to Claude Code with the `claude mcp` CLI:

```bash
claude mcp add mcp-modal -- uvx mcp-modal
```

Or add it to a `.mcp.json` file in your project root:

```json
{
  "mcpServers": {
    "mcp-modal": {
      "command": "uvx",
      "args": ["mcp-modal"]
    }
  }
}
```

To pin a specific release, use `uvx mcp-modal@0.2.0`.

## Requirements

- Python 3.11 or higher
- [`uv`](https://docs.astral.sh/uv/) (provides `uvx`)
- Modal CLI 1.x configured with valid credentials (`modal setup`)
- For Modal **deploy** and **run** support:
  - The project being deployed/run must use `uv` for dependency management
  - `modal` must be installed in that project's virtual environment

## Supported Tools

26 tools, grouped by area. Account-scoped tools accept an optional `env` argument to
target a specific [Modal environment](https://modal.com/docs/guide/environments); if
omitted, they use the profile's default (or `MODAL_ENVIRONMENT`).

### Deploy & Run

1. **Deploy Modal App** (`deploy_modal_app`)
   - Deploys a Modal app (`modal deploy`). Deployed web endpoints persist, so any links
     in the output are live and shareable (returned in `urls`).
   - Parameters: `absolute_path_to_app` (required), `env`, `name`, `tag`,
     `strategy` (`rolling`/`recreate`), `stream_logs`
   - The app's directory must use `uv` with `modal` installed in its virtualenv.

2. **Run Modal App** (`run_modal_app`)
   - Runs a function or local entrypoint once and streams its output (`modal run`).
   - Parameters: `absolute_path_to_app` (required), `function_name`, `env`,
     `detach`, `timeout_seconds` (default 120)
   - Returns a snapshot with `truncated: true` if the run is still going at the timeout.
     Pass `detach=True` to keep long jobs alive on Modal past the timeout.

> **Why no `modal serve` tool?** `modal serve` only keeps its endpoints alive while the
> blocking process runs — an MCP tool that returns would tear them down immediately,
> handing back a dead URL. Use `deploy_modal_app` for a persistent, shareable endpoint.

### Apps

3. **List Modal Apps** (`list_modal_apps`)
   - Lists apps currently deployed/running or recently stopped. Use this to find the
     app name/ID for the other app tools.
   - Parameters: `env`

4. **Get Modal App Logs** (`get_modal_app_logs`)
   - Fetches or streams logs for an app by name or ID (`modal app logs`).
   - Parameters: `app_identifier` (required), `timeout_seconds` (default 30), `env`,
     `since`, `until`, `tail`, `search`, `source` (`stdout`/`stderr`/`system`),
     `timestamps` (prefix each line with its wall-clock time), `follow`
   - With `follow=True`, logs stream until the app stops or `timeout_seconds` is reached,
     returning a snapshot with `truncated: true`.
   - Only covers the stdout/stderr/system streams; some failures (e.g. a crash reported as
     "... exited with ...") are Modal dashboard events, not log lines, and won't appear here.

5. **Stop Modal App** (`stop_modal_app`)
   - Permanently stops an app and terminates its containers (`modal app stop`).
   - Parameters: `app_identifier` (required), `env`

6. **Roll Back Modal App** (`rollback_modal_app`)
   - Redeploys a previous version of an app (`modal app rollback`).
   - Parameters: `app_identifier` (required), `version` (optional — defaults to the
     previous version), `env`

7. **Get Modal App History** (`get_modal_app_history`)
   - Returns an app's deployment history (`modal app history`). Use it to find a
     `version` for rollback.
   - Parameters: `app_identifier` (required), `env`

### Containers

8. **List Modal Containers** (`list_modal_containers`)
   - Lists currently running containers (`modal container list`).
   - Parameters: `app_id` (optional filter), `env`

9. **Get Modal Container Logs** (`get_modal_container_logs`)
   - Fetches or streams logs for a container ID (`modal container logs`).
   - Parameters: `container_id` (required), `timeout_seconds` (default 30),
     `since`, `until`, `tail`, `search`, `source`, `timestamps`, `follow`
   - Same stdout/stderr/system-only caveat as the app-logs tool above.

10. **Exec in Modal Container** (`exec_modal_container`)
    - Runs a command inside a running container (`modal container exec --no-pty`).
    - Parameters: `container_id` (required), `command` (list of args, e.g.
      `["python", "-c", "print('hi')"]`), `timeout_seconds` (default 60)

11. **Stop Modal Container** (`stop_modal_container`)
    - Terminates a running container (`modal container stop`).
    - Parameters: `container_id` (required)

### Log Search

12. **Search Modal Logs** (`search_modal_logs`)
    - Greps an app's or container's logs for a pattern and returns each hit **with the
      surrounding lines** — built for "where did it go wrong?" debugging. Logs are fetched
      once and searched locally, so (unlike the `search` argument on the log tools) you get
      context, regex, case control, and match counts, not just the bare matching line.
    - Parameters: `identifier` (required — app name/ID or container ID), `pattern` (required),
      `target` (`app`/`container`, default `app`), `regex`, `case_sensitive`,
      `context_lines` (default 3), `max_matches` (default 50), `since`, `tail`
      (defaults to the last 1000 entries), `source` (`stdout`/`stderr`/`system`),
      `exclude` (drop noise lines before searching, e.g. `"queue put failed"`),
      `timestamps` (default `true` — carry each line's wall-clock time into the result),
      `timeout_seconds`, `env`
    - Returns `match_count` and `matches`: timestamped, line-numbered context blocks where
      matched lines are prefixed with `>`, e.g. `> 8: 2026-06-04T... ValueError: bad input`.
      Reports `excluded_lines` when `exclude` is used.
    - Only searches the stdout/stderr/system streams; failures emitted as Modal dashboard
      events (e.g. "... exited with ...") return 0 matches even when the failure is real.

### Volumes — Files

13. **List Modal Volumes** (`list_modal_volumes`) — lists all volumes. Parameters: none.
14. **List Volume Contents** (`list_modal_volume_contents`) — `volume_name`, `path` (default `/`). Sets `empty: true` with a message when the listing genuinely returns nothing, so an empty directory is distinguishable from an error or a wrong path.
15. **Copy Files** (`copy_modal_volume_files`) — `volume_name`, `paths` (last is destination).
16. **Remove File** (`remove_modal_volume_file`) — `volume_name`, `remote_path`, `recursive`.
17. **Upload File** (`put_modal_volume_file`) — `volume_name`, `local_path`, `remote_path`, `force`.
18. **Download File** (`get_modal_volume_file`) — `volume_name`, `remote_path`, `local_destination`, `force`. Use `-` as the destination to stream contents to stdout.

### Volumes — Lifecycle

19. **Create Volume** (`create_modal_volume`) — creates a named persistent volume. Parameters: `volume_name`, `env`.
20. **Delete Volume** (`delete_modal_volume`) — deletes a volume **and all its data** (irreversible). Parameters: `volume_name`, `env`.
21. **Rename Volume** (`rename_modal_volume`) — Parameters: `old_name`, `new_name`, `env`.

### Secrets

22. **List Secrets** (`list_modal_secrets`)
    - Lists published secrets (names and timestamps only — values are never exposed).
    - Parameters: `env`

23. **Create Secret** (`create_modal_secret`)
    - Creates a secret from inline key/values or a local file (`modal secret create`).
      Secret values are **redacted** from the returned `command`.
    - Parameters: `secret_name` (required), `key_values` (dict), `from_dotenv` (path),
      `from_json` (path), `force`, `env`. Provide at least one of `key_values`,
      `from_dotenv`, or `from_json`.

24. **Delete Secret** (`delete_modal_secret`) — Parameters: `secret_name`, `env`.

### Discovery

25. **Get Modal Profile** (`get_modal_profile`)
    - Shows the active profile and all configured profiles. Use it to confirm which
      workspace/account the server is authenticated as. Parameters: none.

26. **List Modal Environments** (`list_modal_environments`)
    - Lists the environments in the current workspace; the names are valid `env`
      arguments for the other tools. Parameters: none.

## Response Format

All tools return responses in a standardized format, with slight variations depending on the operation type:

```python
# JSON / list operations (apps, containers, volumes, secrets, history, ...):
{
    "success": True,
    "apps": [...]   # or "containers", "volumes", "secrets", "history", "environments"
}

# Action operations (deploy, stop, create, delete, rename, copy, put, get, rm):
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
