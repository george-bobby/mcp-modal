"""Prompt workflows the user can invoke directly from the client.

Clients fetch prompts on demand, so unlike tool schemas they cost nothing per session.
That makes this the right home for multi-step guidance and for caveats (e.g. "dashboard
crash events are not log lines") instead of repeating them in every tool description.
Keep the tool names used in these texts in sync when tools change.
"""
from .app import mcp


@mcp.prompt(title="Debug a Modal app")
def debug_modal_app(app_name: str, symptom: str = "") -> str:
    """Walk through diagnosing a failing or misbehaving Modal app."""
    focus = f"\nReported symptom: {symptom}\n" if symptom else "\n"
    return f"""Diagnose what is wrong with the Modal app "{app_name}".{focus}
Work in this order, stopping as soon as you have the root cause:

1. Confirm the app exists and its current state:
   list_modal_resources(resource="apps")
2. Look for failures in the logs, with context around each hit:
   search_modal_logs(identifier="{app_name}", pattern="Traceback|Error|Exception", regex=True, since="1h")
   If that is noisy, cut the noise with source="stderr" or exclude="<repeated line>".

   If the result comes back `logs_truncated` or `output_capped`, the fetch hit the volume
   ceiling — narrow it, never widen it, and never just raise timeout_seconds:
   - If you know roughly WHEN it happened, bound BOTH ends. A one-sided since= fetches
     every entry from then until now (megabytes on a busy app); since= plus until= around
     the incident is typically kilobytes:
       search_modal_logs(identifier="{app_name}", pattern="<error>",
                         since="2026-03-01T17:33:00", until="2026-03-01T17:36:00")
   - If you do NOT know when, find the time first with a server-side filter, then re-query
     that minute for real context:
       search_modal_logs(identifier="{app_name}", pattern="<literal>", prefilter=True,
                         timestamps=True, context_lines=0)
     prefilter drops non-matching lines inside Modal, so it survives huge logs — but its
     context lines are other matches, not the app's surrounding output.
   - Also cheap: source="stderr", a stricter pattern, a smaller tail.
   A time range cannot exceed 35 days, and since must be before until.
3. If nothing matches, read the recent log tail directly:
   get_modal_logs(identifier="{app_name}", tail=200, timestamps=True)
4. When an incident is pinned to a window, check whether it hit MORE than the one app you
   started with — sibling workers sharing a database or queue usually fail together:
   list_modal_resources(resource="apps"), then run the same bounded since/until search
   against each plausible sibling. Report which apps you checked and which you could not,
   so nobody reads "confirmed on one app" as "only one app was affected".
5. If the app is running but wedged, inspect its containers:
   list_modal_resources(resource="containers"), then get_modal_logs on the container ID,
   and manage_modal_container(action="exec", ...) for a live look (e.g. ["ps", "aux"]).
6. If the failure started after a deploy, compare against history:
   list_modal_resources(resource="app_history", name="{app_name}")
   and propose manage_modal_app(action="rollback", ...) if a recent version is the cause.

Important: `modal app logs` only carries the stdout/stderr/system streams. Crash events
shown on the Modal dashboard (e.g. "... exited with ...") are NOT log lines and will never
appear in a search — if the logs look clean but the app is clearly failing, say so and
point at the dashboard rather than concluding nothing is wrong.

Finish with: the root cause, the evidence (quote the log lines), and the fix."""


@mcp.prompt(title="Deploy and verify a Modal app")
def deploy_and_verify(absolute_path_to_app: str, env: str = "") -> str:
    """Deploy a Modal app, then confirm it actually came up."""
    env_note = f' into the "{env}" environment' if env else ""
    env_arg = f', env="{env}"' if env else ""
    return f"""Deploy {absolute_path_to_app}{env_note} and verify it is genuinely live.

1. Check which account/workspace you are about to deploy to:
   list_modal_resources(resource="profile")
2. Deploy:
   deploy_modal_app(absolute_path_to_app="{absolute_path_to_app}"{env_arg})
   The app's directory must use `uv` and have `modal` installed in its virtualenv.
3. Report every URL from the `urls` field — those are live, shareable endpoints.
4. Verify rather than assume: confirm the app appears with
   list_modal_resources(resource="apps"{env_arg}), then check the first minutes of logs with
   search_modal_logs(identifier="<app name>", pattern="Error|Traceback", regex=True, since="5m").
5. If the deploy failed or the app is crash-looping, do NOT retry blindly — report the
   error, and offer manage_modal_app(action="rollback", ...) to restore the previous version.

Report: what was deployed, its URLs, and the evidence that it is healthy."""


@mcp.prompt(title="Investigate a Modal cost spike")
def investigate_modal_costs(period: str = "last week", app: str = "") -> str:
    """Trace a cost increase back to the app and resource that caused it."""
    focus = f'\nFocus on the app "{app}".\n' if app else "\n"
    app_arg = f', app="{app}"' if app else ""
    return f"""Work out where Modal spend went during {period} and what drove it.{focus}
1. Shape of the spend over time — this also names the peak interval and ranks which apps
   grew into it:
   analyze_modal_costs(view="timeline", period="{period}"{app_arg})
   Read `explanation.movers`: those are the apps that cost more in the peak interval than
   in the one before, biggest increase first.
2. Who spends the most overall:
   analyze_modal_costs(view="by_app", period="{period}"{app_arg})
3. Drill into the peak. Take the peak date from step 1 and go hourly:
   analyze_modal_costs(view="timeline", start="<peak date>", end="<next date>", resolution="h")
4. What kind of resource it was (a GPU class is usually the answer):
   analyze_modal_costs(view="by_resource", period="{period}"{app_arg})
5. Tie it to what actually ran: list_modal_resources(resource="apps") for the culprit, then
   list_modal_resources(resource="app_history", name="<app>") to see whether a deploy lines
   up with the increase, and search_modal_logs for retries/restarts around the peak.

Costs are US dollars. `total_cost` covers every row in range even when the group list is
truncated, so quote it rather than summing the visible rows. Modal reports whole intervals
only, so a partially elapsed day reads low — say so instead of calling it a drop.

Report: total for the period, the biggest spender, what changed at the peak and why, and —
if the cause is a still-running container or an over-provisioned GPU — the exact call that
would stop it, without running it."""


@mcp.prompt(title="Review Modal account usage")
def review_modal_account(env: str = "") -> str:
    """Inventory the Modal account and flag cleanup candidates."""
    env_arg = f'env="{env}"' if env else ""
    sep = ", " if env_arg else ""
    return f"""Produce an inventory of this Modal account and flag anything worth cleaning up.

Gather (read-only — do not stop, delete, or modify anything):
- list_modal_resources(resource="profile") — which workspace am I looking at?
- list_modal_resources(resource="environments") — repeat the sweep per environment if
  there is more than one.
- list_modal_resources(resource="apps"{sep}{env_arg}) — note stopped or stale apps.
- list_modal_resources(resource="containers"{sep}{env_arg}) — anything running that
  shouldn't be is burning money right now.
- list_modal_resources(resource="volumes"{sep}{env_arg}) — spot-check large or unused ones
  with resource="volume_files".
- list_modal_resources(resource="secrets"{sep}{env_arg}) — names only; never print values.

Then report: a short inventory table, running containers with no obvious owner, apps that
look abandoned, and volumes/secrets that appear orphaned. For each cleanup candidate name
the exact tool call that would remove it, but do not run it — deletions are the user's call."""
