"""Unit tests for the pure logic: log search, output caps, cost math, argv helpers."""
from decimal import Decimal

from mcp_modal.billing import cost_movers, group_costs
from mcp_modal.command import _add_env, _check_local_path, _redact_text, _uv_prefixed
from mcp_modal.logsearch import (
    _check_log_window,
    _log_failure_error,
    _resolve_log_target,
    cap_blocks,
    filter_log_lines,
    grep_lines,
)
from mcp_modal.output import _cap_items, _cap_text, extract_urls
from mcp_modal.tools.secrets import _KEYS_PROBE, _secret_key_names

LOG = "\n".join(f"line {i}" + (" ERROR boom" if i in (3, 4, 10) else "") for i in range(1, 13))


# --- log search -------------------------------------------------------------------------

def test_grep_merges_adjacent_windows_and_marks_matches():
    total, blocks = grep_lines(LOG, "error", regex=False, case_sensitive=False,
                               context_lines=1, max_matches=50)
    assert total == 3
    assert blocks == [
        "  2: line 2\n> 3: line 3 ERROR boom\n> 4: line 4 ERROR boom\n  5: line 5",
        "  9: line 9\n> 10: line 10 ERROR boom\n  11: line 11",
    ]


def test_grep_total_stays_exact_when_shown_matches_are_limited():
    total, blocks = grep_lines(LOG, "ERROR", regex=False, case_sensitive=True,
                               context_lines=0, max_matches=1)
    assert total == 3
    assert blocks == ["> 3: line 3 ERROR boom"]


def test_grep_literal_mode_escapes_regex_characters():
    total, _ = grep_lines("a.b\naxb", "a.b", regex=False, case_sensitive=True,
                          context_lines=0, max_matches=10)
    assert total == 1


def test_grep_reports_a_bad_regex():
    total, error = grep_lines(LOG, "(", regex=True, case_sensitive=True,
                              context_lines=0, max_matches=10)
    assert total is None and error.startswith("Invalid regex pattern")


def test_filter_log_lines_drops_noise():
    text, removed = filter_log_lines(LOG, "error", regex=False, case_sensitive=False)
    assert removed == 3 and "ERROR" not in text


def test_cap_blocks_drops_whole_blocks_from_the_end(monkeypatch):
    monkeypatch.setenv("MCP_MODAL_MAX_OUTPUT_CHARS", "1000")
    blocks = ["a" * 600, "b" * 600, "c" * 10]
    kept, dropped = cap_blocks(blocks)
    assert kept == ["a" * 600] and dropped == 2


def test_log_window_and_target_helpers():
    assert _check_log_window(None) is None
    assert _check_log_window(20000) is None
    assert "exceed" in _check_log_window(20001)
    assert "positive" in _check_log_window(0)
    assert _resolve_log_target("ta-123", "auto") == "container"
    assert _resolve_log_target("my-app", "auto") == "app"
    assert _resolve_log_target("ta-123", "app") == "app"


def test_log_failure_error_prefers_the_cli_message():
    result = {"stderr": "Usage: ...\nError: Log fetch time range cannot exceed 35 days\n",
              "returncode": 2}
    assert _log_failure_error("app", result, "Failed to fetch logs") == (
        "Failed to fetch logs for 'app': Log fetch time range cannot exceed 35 days"
    )
    assert _log_failure_error("app", {"stderr": "", "returncode": 1}, "Failed") == (
        "Failed for 'app' (exit 1)"
    )


# --- output caps ------------------------------------------------------------------------

def test_cap_text_keeps_head_and_tail(monkeypatch):
    monkeypatch.setenv("MCP_MODAL_MAX_OUTPUT_CHARS", "1000")
    text = "\n".join(f"{i:05d}" for i in range(2000))
    capped, was_capped = _cap_text(text)
    assert was_capped and len(capped) < len(text)
    assert capped.startswith("00000") and capped.endswith("01999")
    assert "omitted" in capped


def test_cap_text_can_be_disabled(monkeypatch):
    monkeypatch.setenv("MCP_MODAL_MAX_OUTPUT_CHARS", "0")
    text = "x" * 100000
    assert _cap_text(text) == (text, False)


def test_cap_items_reports_omitted_count():
    data, omitted = _cap_items(list(range(250)))
    assert len(data) == 200 and omitted == 50


def test_extract_urls_dedupes_and_strips_punctuation():
    out = "Deployed: https://x--app.modal.run). See https://x--app.modal.run, done"
    assert extract_urls(out, None, "http://other.example/path") == [
        "https://x--app.modal.run",
        "http://other.example/path",
    ]


# --- cost math --------------------------------------------------------------------------

def _row(app, interval, cost, env="main", title_case=False):
    row = {"description": app, "object_id": f"ap-{app}", "environment": env,
           "interval_start": interval, "cost": cost}
    if title_case:  # modal 1.4.x column names
        return {"Description": app, "Object ID": f"ap-{app}", "Environment": env,
                "Interval Start": interval, "Cost": cost}
    return row


def test_group_costs_reads_both_column_spellings():
    rows = [_row("a", "d1", "1.10"), _row("a", "d2", "0.90", title_case=True),
            _row("b", "d1", "0.50")]
    groups, total, omitted = group_costs(rows, "app", top_n=1)
    assert total == Decimal("2.50")
    assert groups == [{"name": "a", "cost": "2.0000", "share_pct": 80.0}]
    assert omitted == 1


def test_group_costs_has_no_float_drift():
    rows = [_row("a", "d1", "0.1")] * 3
    _, total, _ = group_costs(rows, "app", top_n=10)
    assert total == Decimal("0.3")


def test_cost_movers_explains_the_peak_interval():
    rows = [_row("a", "d1", "1"), _row("b", "d1", "1"),
            _row("a", "d2", "1"), _row("b", "d2", "5")]
    movers = cost_movers(rows, top_n=5)
    assert movers["peak_interval"] == "d2"
    assert movers["compared_with"] == "d1"
    assert movers["peak_cost"] == "6.0000"


def test_cost_movers_needs_two_intervals():
    assert cost_movers([_row("a", "d1", "1")], top_n=5) is None


# --- argv helpers and safety ------------------------------------------------------------

def test_add_env_and_uv_prefix():
    assert _add_env(["modal", "app", "list"], "dev") == ["modal", "app", "list", "-e", "dev"]
    assert _add_env(["modal", "app", "list"], None) == ["modal", "app", "list"]
    assert _uv_prefixed(["modal", "deploy"], "/p") == ["uv", "run", "--directory=/p", "modal", "deploy"]
    assert _uv_prefixed(["modal", "deploy"], None) == ["modal", "deploy"]


def test_redact_text_replaces_longest_secret_first():
    assert _redact_text("k=dummy2 j=dummy", ["dummy", "dummy2", ""]) == "k=*** j=***"


def test_local_path_allowlist(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_MODAL_ALLOWED_LOCAL_PATHS", raising=False)
    assert _check_local_path("/etc/passwd") is None  # unset means unrestricted
    monkeypatch.setenv("MCP_MODAL_ALLOWED_LOCAL_PATHS", str(tmp_path))
    assert _check_local_path(str(tmp_path / "file.txt")) is None
    assert _check_local_path(str(tmp_path / ".." / "escape.txt")) is not None
    assert _check_local_path("/etc/passwd") is not None


def test_secret_probe_is_safe_for_modal_shell():
    # `modal shell -c` re-joins the string and runs it under bash -c: quotes and
    # parentheses would not survive (see CLAUDE.md).
    assert not set(_KEYS_PROBE) & set("'\"()`$|&<>")
    assert _secret_key_names(["PATH", "MODAL_TOKEN_ID", "PYTHONPATH", "API_KEY", "DB_URL"]) == [
        "API_KEY", "DB_URL",
    ]
