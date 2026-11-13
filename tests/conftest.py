import subprocess

import pytest


@pytest.fixture
def no_subprocess(monkeypatch):
    """Fail the test if anything tries to start a process (i.e. would call `modal`)."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"unexpected subprocess call: {args[0] if args else kwargs}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


@pytest.fixture
def captured(monkeypatch):
    """Record the argv and profile of every `run_modal_command` call a tool makes.

    Patched in each tool module's namespace (where it was imported to), returning a
    successful empty result so the tool runs its normal response path.
    """
    import mcp_modal.tools.apps as apps
    import mcp_modal.tools.secrets as secrets
    import mcp_modal.tools.volumes as volumes

    calls = []

    def fake(command, uv_directory=None, redact=None, profile=None):
        calls.append({"command": command, "profile": profile, "redact": redact})
        return {"success": True, "stdout": "", "stderr": "", "command": " ".join(command)}

    for module in (apps, secrets, volumes):
        monkeypatch.setattr(module, "run_modal_command", fake)
    return calls
