"""The subprocess runners, exercised with real child processes (no `modal` needed)."""
import sys

from mcp_modal.command import _profile_env, run_modal_command, run_modal_streaming_command

PRINT_ENV = [sys.executable, "-c",
             "import os; print(os.environ.get('MODAL_PROFILE'), os.environ.get('MODAL_TOKEN_ID'))"]


def test_no_profile_inherits_the_environment_unchanged(monkeypatch):
    monkeypatch.delenv("MODAL_PROFILE", raising=False)
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-from-env")
    assert _profile_env(None) is None
    assert run_modal_command(PRINT_ENV)["stdout"].split() == ["None", "ak-from-env"]


def test_profile_is_set_for_the_child_and_overriding_settings_are_dropped(monkeypatch):
    for var in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "MODAL_SERVER_URL", "MODAL_ENVIRONMENT"):
        monkeypatch.setenv(var, "inherited")
    monkeypatch.setenv("UNRELATED", "kept")
    env = _profile_env("work")
    assert env["MODAL_PROFILE"] == "work" and env["UNRELATED"] == "kept"
    assert not {"MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "MODAL_SERVER_URL",
                "MODAL_ENVIRONMENT"} & set(env)
    assert run_modal_command(PRINT_ENV, profile="work")["stdout"].split() == ["work", "None"]
    streamed = run_modal_streaming_command(PRINT_ENV, timeout_seconds=30, profile="work")
    assert streamed["stdout"].split() == ["work", "None"]


def test_profile_never_leaks_into_the_server_process(monkeypatch):
    monkeypatch.delenv("MODAL_PROFILE", raising=False)
    run_modal_command(PRINT_ENV, profile="work")
    import os
    assert "MODAL_PROFILE" not in os.environ


def test_failed_command_is_reported_not_raised():
    result = run_modal_command([sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"])
    assert result["success"] is False
    assert "bad" in result["stderr"]


def test_redact_scrubs_command_and_output():
    result = run_modal_command([sys.executable, "-c", "print('value=hunter2')"], redact=["hunter2"])
    assert "hunter2" not in result["stdout"] and "hunter2" not in result["command"]


def test_streaming_runner_stops_at_the_deadline_with_partial_output():
    cmd = [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(60)"]
    result = run_modal_streaming_command(cmd, timeout_seconds=2)
    assert result["timed_out"] is True
    assert "started" in result["stdout"]
