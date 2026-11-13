"""Tool-level behaviour that needs no Modal account: validation and argv construction."""
import asyncio

import pytest

from mcp_modal.server import (
    analyze_modal_costs,
    get_modal_logs,
    list_modal_resources,
    manage_modal_app,
    manage_modal_container,
    manage_modal_secret,
    manage_modal_volume,
    modal_volume_files,
    search_modal_logs,
)


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("call", [
    lambda: list_modal_resources(resource="nope"),
    lambda: manage_modal_app(action="nope", app_identifier="a"),
    lambda: manage_modal_container(action="nope", container_id="ta-1"),
    lambda: manage_modal_volume(action="nope", volume_name="v"),
    lambda: modal_volume_files(action="nope", volume_name="v"),
    lambda: manage_modal_secret(action="nope", secret_name="s"),
    lambda: analyze_modal_costs(view="nope"),
    lambda: get_modal_logs(identifier="a", target="nope"),
    lambda: get_modal_logs(identifier="a", tail=20001),
    lambda: search_modal_logs(identifier="a", pattern="(", regex=True, prefilter=True),
])
def test_bad_arguments_fail_before_running_modal(no_subprocess, call):
    result = run(call())
    assert result["success"] is False and result["error"]


def test_volume_commands_carry_env_and_end_options(captured):
    run(manage_modal_volume(action="create", volume_name="-weird", env="dev", profile="work"))
    assert captured[-1] == {
        "command": ["modal", "volume", "create", "-e", "dev", "--", "-weird"],
        "profile": "work",
        "redact": None,
    }


def test_container_stop_takes_no_env(captured):
    run(manage_modal_container(action="stop", container_id="ta-1"))
    assert captured[-1]["command"] == ["modal", "container", "stop", "-y", "--", "ta-1"]
    assert captured[-1]["profile"] is None


def test_app_rollback_passes_version_after_the_separator(captured):
    run(manage_modal_app(action="rollback", app_identifier="app", version="v3", env="prod"))
    assert captured[-1]["command"] == ["modal", "app", "rollback", "-e", "prod", "--", "app", "v3"]


def test_secret_create_redacts_values(captured):
    result = run(manage_modal_secret(action="create", secret_name="s",
                                     key_values={"API_KEY": "hunter2"}))
    assert result["success"] is True
    assert captured[-1]["redact"] == ["hunter2"]


def test_secret_files_respect_the_local_path_allowlist(captured, monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_MODAL_ALLOWED_LOCAL_PATHS", str(tmp_path))
    for option in ("from_dotenv", "from_json"):
        result = run(manage_modal_secret(action="create", secret_name="s",
                                         **{option: "/etc/passwd"}))
        assert result["success"] is False and "outside the allowed roots" in result["error"]
    assert captured == []  # refused before any command was built or run

    allowed = str(tmp_path / ".env")
    assert run(manage_modal_secret(action="create", secret_name="s", from_dotenv=allowed))["success"]
    assert f"--from-dotenv={allowed}" in captured[-1]["command"]


def test_option_values_are_bound_with_equals(captured):
    run(manage_modal_secret(action="create", secret_name="s", from_dotenv="-x.env",
                            from_json="-y.json"))
    command = captured[-1]["command"]
    assert "--from-dotenv=-x.env" in command and "--from-json=-y.json" in command
    assert command.index("--from-json=-y.json") < command.index("--")


def test_inspect_secret_binds_the_name_with_equals(monkeypatch):
    import mcp_modal.tools.secrets as secrets

    seen = {}

    def fake(command, timeout_seconds, uv_directory=None, profile=None):
        seen["command"] = command
        return {"stdout": "", "stderr": "", "returncode": 1, "timed_out": False,
                "command": " ".join(command)}

    monkeypatch.setattr(secrets, "run_modal_streaming_command", fake)
    run(secrets.inspect_modal_secret(secret_name="-weird", image="-img"))
    assert "--secret=-weird" in seen["command"] and "--image=-img" in seen["command"]
