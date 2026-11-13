"""Guards the public contract that 1.0 promises (see the README's Versioning section).

`contract.json` is a snapshot of every tool's name, arguments, defaults and safety
annotations, and every prompt's arguments. Two checks run against it:

- test_no_breaking_changes fails when a release would break existing callers (a tool,
  argument or prompt removed, an argument made required, a default or annotation
  changed) unless the major version in pyproject.toml has been bumped past the
  snapshot's.
- test_snapshot_is_current fails on any difference at all, so additions get recorded
  too. After an intended change, regenerate with:

      UPDATE_CONTRACT=1 uv run pytest tests/test_contract.py
"""
import asyncio
import json
import os
import tomllib
from pathlib import Path

from mcp_modal.server import mcp

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = Path(__file__).resolve().parent / "contract.json"


def _major_version() -> int:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return int(tomllib.load(f)["project"]["version"].split(".")[0])


def current_contract() -> dict:
    tools = asyncio.run(mcp.list_tools())
    prompts = asyncio.run(mcp.list_prompts())
    contract = {"major_version": _major_version(), "tools": {}, "prompts": {}}
    for tool in tools:
        schema = tool.inputSchema
        contract["tools"][tool.name] = {
            "required": sorted(schema.get("required", [])),
            "arguments": {
                name: {"default": prop.get("default")}
                for name, prop in sorted(schema["properties"].items())
            },
            "read_only": tool.annotations.readOnlyHint,
            "destructive": tool.annotations.destructiveHint,
        }
    for prompt in prompts:
        contract["prompts"][prompt.name] = {
            "required": sorted(a.name for a in prompt.arguments or [] if a.required),
            "arguments": sorted(a.name for a in prompt.arguments or []),
        }
    contract["tools"] = dict(sorted(contract["tools"].items()))
    contract["prompts"] = dict(sorted(contract["prompts"].items()))
    return contract


def breaking_changes(old: dict, new: dict) -> list:
    problems = []
    for name, old_tool in old["tools"].items():
        new_tool = new["tools"].get(name)
        if new_tool is None:
            problems.append(f"tool {name!r} was removed")
            continue
        for arg, spec in old_tool["arguments"].items():
            if arg not in new_tool["arguments"]:
                problems.append(f"{name}: argument {arg!r} was removed")
            elif new_tool["arguments"][arg]["default"] != spec["default"]:
                problems.append(
                    f"{name}: default of {arg!r} changed from {spec['default']!r} "
                    f"to {new_tool['arguments'][arg]['default']!r}"
                )
        for arg in set(new_tool["required"]) - set(old_tool["required"]):
            problems.append(f"{name}: argument {arg!r} is now required")
        if old_tool["read_only"] and not new_tool["read_only"]:
            problems.append(f"{name}: is no longer read-only")
        if new_tool["destructive"] and not old_tool["destructive"]:
            problems.append(f"{name}: is now destructive")
    for name, old_prompt in old["prompts"].items():
        new_prompt = new["prompts"].get(name)
        if new_prompt is None:
            problems.append(f"prompt {name!r} was removed")
            continue
        for arg in set(old_prompt["arguments"]) - set(new_prompt["arguments"]):
            problems.append(f"prompt {name}: argument {arg!r} was removed")
        for arg in set(new_prompt["required"]) - set(old_prompt["required"]):
            problems.append(f"prompt {name}: argument {arg!r} is now required")
    return problems


def _write_snapshot(contract: dict) -> None:
    SNAPSHOT.write_text(json.dumps(contract, indent=2) + "\n")


if os.environ.get("UPDATE_CONTRACT"):
    _write_snapshot(current_contract())


def test_no_breaking_changes():
    old = json.loads(SNAPSHOT.read_text())
    new = current_contract()
    problems = breaking_changes(old, new)
    if problems and new["major_version"] <= old["major_version"]:
        raise AssertionError(
            "Breaking change to the public contract without a major version bump:\n  "
            + "\n  ".join(problems)
            + f"\nEither keep the old behaviour, or bump pyproject.toml to "
            f"{old['major_version'] + 1}.0.0 and regenerate the snapshot."
        )


def test_snapshot_is_current():
    old = json.loads(SNAPSHOT.read_text())
    assert current_contract() == old, (
        "The tool/prompt surface differs from tests/contract.json. If the change is "
        "intended, regenerate it: UPDATE_CONTRACT=1 uv run pytest tests/test_contract.py"
    )


def test_inventory():
    contract = current_contract()
    assert len(contract["tools"]) == 12
    assert len(contract["prompts"]) == 4


def test_every_tool_takes_profile_and_it_is_optional():
    for name, tool in current_contract()["tools"].items():
        assert "profile" in tool["arguments"], name
        assert "profile" not in tool["required"], name


def test_detects_a_removed_argument():
    old = current_contract()
    new = json.loads(json.dumps(old))
    del new["tools"]["get_modal_logs"]["arguments"]["since"]
    new["tools"]["deploy_modal_app"]["required"].append("name")
    assert sorted(breaking_changes(old, new)) == [
        "deploy_modal_app: argument 'name' is now required",
        "get_modal_logs: argument 'since' was removed",
    ]
