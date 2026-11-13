<!-- mcp-name: io.github.george-bobby/mcp-modal -->

# Modal MCP Server

[![PyPI](https://img.shields.io/pypi/v/mcp-modal)](https://pypi.org/project/mcp-modal/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-modal)](https://pypi.org/project/mcp-modal/)
[![Tests](https://img.shields.io/github/actions/workflow/status/george-bobby/mcp-modal/tests.yml?branch=main&label=tests)](https://github.com/george-bobby/mcp-modal/actions/workflows/tests.yml)
[![License](https://img.shields.io/pypi/l/mcp-modal)](LICENSE)

Manage [Modal](https://modal.com) from Claude Code or any MCP client: apps, containers,
logs, volumes, secrets and costs, plus deploying and running your own Modal apps.

It runs your local `modal` CLI, so it uses the profile and credentials already on your
machine. There are no extra tokens to manage.

[![Modal MCP server](https://mcprush.com/badge/mcp-modal-server/card.svg)](https://mcprush.com/to/mcp-modal-server?ref=badge)
[![mcp-modal MCP server](https://glama.ai/mcp/servers/george-bobby/mcp-modal/badges/card.svg)](https://glama.ai/mcp/servers/george-bobby/mcp-modal)

## Quick start

1. Log in to Modal once (this writes `~/.modal.toml`):

   ```bash
   modal setup
   ```

2. Add the server to Claude Code:

   ```bash
   claude mcp add mcp-modal -- uvx mcp-modal@latest
   ```

   Or share it with your team through a `.mcp.json` in the project root:

   ```json
   {
     "mcpServers": {
       "mcp-modal": { "command": "uvx", "args": ["mcp-modal@latest"] }
     }
   }
   ```

`uvx` caches a tool after its first run, so a plain `uvx mcp-modal` never updates.
`@latest` checks PyPI on every launch. Pin an exact version (`mcp-modal@1.0.0`) when every
machine must run the same build.

## Requirements

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- Modal CLI 1.5+, logged in with `modal setup`
- To deploy or run an app, that app's project must use `uv` and have `modal` installed in
  its virtualenv

## Tools

| Tool | What it does |
| --- | --- |
| `list_modal_resources` | Read-only lookup of `apps`, `app_history`, `containers`, `volumes`, `volume_files`, `secrets`, `environments` or `profile` |
| `get_modal_logs` | Fetch or follow logs for an app or a container |
| `search_modal_logs` | Grep logs and return each match with surrounding lines |
| `analyze_modal_costs` | Rank spend by app, environment or resource, explain a cost spike, or show the bill and rates |
| `deploy_modal_app` | `modal deploy`, returning the live endpoint URLs |
| `run_modal_app` | `modal run` a function or entrypoint and collect its output |
| `manage_modal_app` | Stop an app, or roll it back to an earlier version |
| `manage_modal_container` | Run a command in a container, or stop it |
| `manage_modal_volume` | Create, delete or rename a volume |
| `modal_volume_files` | Upload, download, copy or remove files in a volume |
| `manage_modal_secret` | Create or delete a secret (values are redacted from all output) |
| `inspect_modal_secret` | List the key names inside a secret, never its values (starts a short container) |

Each tool's full arguments are in its MCP schema, so your client shows them. Common ones:

- **`env`** targets a [Modal environment](https://modal.com/docs/guide/environments).
  Leave it out to use the profile's default.
- **`profile`** runs a single call against another profile in `~/.modal.toml`, for example
  `list_modal_resources(resource="apps", profile="work")`. Your default profile is not
  changed.

Tips:

- On a busy app, give log tools both `since` and `until`. `since` on its own fetches
  everything from then until now. `search_modal_logs(prefilter=True)` filters on Modal's
  side when the logs are too large to fetch.
- Logs only contain stdout, stderr and system output. Crash events shown on the Modal
  dashboard (such as "… exited with …") are not log lines, so they won't appear here.
- There is no `modal serve` tool, because its endpoints stop when the command returns. Use
  `deploy_modal_app` to get an endpoint that stays up.

## Prompts

Four ready-made workflows. In Claude Code they appear as `/mcp__mcp-modal__<name>`:

- `debug_modal_app`: work out why an app is failing, from logs, containers and deploy
  history
- `deploy_and_verify`: deploy an app, then check it is actually healthy
- `review_modal_account`: find idle apps, stray containers and orphaned volumes or secrets
- `investigate_modal_costs`: trace a cost increase down to the app, resource and hour

## Security

The server can do anything your Modal login can. Let your client auto-approve the four
read-only tools (`list_modal_resources`, `get_modal_logs`, `search_modal_logs`,
`analyze_modal_costs`), and keep the rest behind an approval prompt. In particular:

- `deploy_modal_app` and `run_modal_app` execute local Python code.
- `modal_volume_files` can upload any local file (`put`) or overwrite one (`get` with
  `force`).
- `manage_modal_secret` with `from_dotenv` or `from_json` reads a local file and stores
  its contents in a Modal secret.
- `manage_modal_container` with `exec` runs arbitrary commands in a container.

Every tool declares MCP read-only and destructive hints so clients can tell these apart.
Names and paths are passed after `--` or as `--option=value`, so a value starting with `-`
can never act as a CLI flag.

## Configuration

Set these in the `env` block of your MCP client config:

| Variable | Default | Effect |
| --- | --- | --- |
| `MCP_MODAL_ALLOWED_LOCAL_PATHS` | unset (no limit) | `:`-separated directories that `modal_volume_files` and `manage_modal_secret` may read from or write to |
| `MCP_MODAL_MAX_OUTPUT_CHARS` | `40000` | Character limit per text field in a response. `0` turns the limit off |

When output is cut, the response sets `output_capped: true` and keeps both the start and
the end of the text.

## Responses

Every tool returns JSON with `success`. On failure it also has `error`, plus the `command`
that ran and its `stdout`/`stderr` when available. On success:

- `list_modal_resources` returns its items under `apps`, `history`, `containers`,
  `volumes`, `contents`, `secrets` or `environments`. Lists longer than 200 items are cut,
  and `omitted_items` says how many were left out. `resource="profile"` returns
  `active_profile` and `profiles` instead.
- Actions return `message`, `command`, `stdout` and `stderr`.
- Logs and runs return `logs` or `output`. `truncated: true` means the command was still
  running when the timeout hit.
- Log search returns `match_count` (an exact count over the whole fetched log) and
  `matches`, which are line-numbered context blocks with each matching line marked `>`.
