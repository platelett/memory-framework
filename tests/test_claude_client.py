from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from claude.adapter import ClaudeAdapter
from claude.snapshot import export_snapshot
from conftest import REPOSITORY, read_json, write_json, write_record, write_task
from memory_framework.parsing import load_records


WRAPPER = REPOSITORY / "bin" / "memory-claude"


def _fake_claude(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "fake-claude"
    log = tmp_path / "calls.jsonl"
    executable.write_text(
        """#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path

entry = {"argv": sys.argv[1:], "cwd": os.getcwd()}
if "--append-system-prompt-file" in sys.argv:
    prompt_path = Path(sys.argv[sys.argv.index("--append-system-prompt-file") + 1])
    entry["prompt_path"] = str(prompt_path)
    entry["prompt_mode"] = oct(prompt_path.stat().st_mode & 0o777)
    entry["prompt"] = prompt_path.read_text(encoding="utf-8")
if os.environ.get("MEMORY_FRAMEWORK_HANDOFF"):
    entry["handoff_contract"] = os.environ["MEMORY_FRAMEWORK_HANDOFF"]
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(entry) + "\\n")

text = entry.get("prompt", "")
path_match = re.search(r"HANDOFF_PATH=([^\\s]+)", text)
nonce_match = re.search(r"HANDOFF_NONCE=([0-9a-f]+)", text)
if path_match and nonce_match and os.environ.get("FAKE_HANDOFF_TASK"):
    handoff = Path(path_match.group(1))
    value = {"version": 2, "nonce": nonce_match.group(1), "mode": "task",
             "kind": "new", "task": os.environ["FAKE_HANDOFF_TASK"],
             "request": os.environ.get("FAKE_REQUEST", "")}
    if os.environ.get("FAKE_MEMORY_PREFERENCE"):
        value["memory_preference"] = os.environ["FAKE_MEMORY_PREFERENCE"]
    handoff.write_text(json.dumps(value), encoding="utf-8")
    handoff.chmod(0o600)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable, log


def _fake_codex_classifier(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-codex-classifier"
    executable.write_text(
        """#!/usr/bin/env python3
import json, sys
from pathlib import Path
prompt = sys.stdin.read()
if "# Automated task annotation preflight" not in prompt:
    raise SystemExit(3)
schema = json.loads(Path(sys.argv[sys.argv.index("--output-schema") + 1]).read_text())
def synthesize(value):
    if "enum" in value:
        return "not-always" if "not-always" in value["enum"] else "lazy"
    return {key: synthesize(item) for key, item in value.get("properties", {}).items()}
Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps(synthesize(schema)))
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def _environment(workspace, executable: Path, log: Path) -> dict[str, str]:
    value = os.environ.copy()
    value.update(
        {
            "MEMORY_WORKSPACE": str(workspace.root),
            "MEMORY_CLAUDE_BIN": str(executable),
            "FAKE_CLAUDE_LOG": str(log),
        }
    )
    return value


def _calls(log: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--version"],
        ["--help"],
        ["auth", "status"],
        ["mcp", "list"],
        ["plugin", "list"],
        ["doctor"],
        ["update"],
        ["project", "list"],
    ],
)
def test_claude_utilities_are_exact_passthrough(workspace, tmp_path, arguments):
    executable, log = _fake_claude(tmp_path)
    environment = _environment(workspace, executable, log)

    result = subprocess.run(
        [str(WRAPPER), *arguments],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert _calls(log) == [{"argv": arguments, "cwd": str(Path.cwd())}]
    assert not workspace.generated_dir.exists()
    assert not workspace.state_dir.joinpath("clients").exists()


def test_claude_task_uses_unique_prompt_file_and_preserves_user_layers(
    workspace, tmp_path
):
    executable, log = _fake_claude(tmp_path)
    environment = _environment(workspace, executable, log)
    arguments = ["--system-prompt", "USER SYSTEM", "inspect this"]

    result = subprocess.run(
        [str(WRAPPER), "--task", "all-lazy", *arguments],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = _calls(log)
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[:2] == ["--append-system-prompt-file", calls[0]["prompt_path"]]
    assert argv[2:] == arguments
    assert calls[0]["prompt_mode"] == "0o600"
    assert "# Active task type" in calls[0]["prompt"]
    assert not Path(str(calls[0]["prompt_path"])).exists()
    assert not (workspace.root / "CLAUDE.md").exists()
    assert not list(
        (workspace.state_dir / "clients" / "claude" / "registrations").glob("*.json")
    )


def test_claude_standard_task_uses_codex_annotation_preflight_before_render(
    workspace, tmp_path
):
    write_record(workspace, "reference", "docs", "api", "API BODY")
    write_task(workspace)
    executable, log = _fake_claude(tmp_path)
    environment = _environment(workspace, executable, log)
    environment.update(
        {
            "MEMORY_CODEX_BIN": str(_fake_codex_classifier(tmp_path)),
            "CODEX_HOME": str(tmp_path / "codex-home"),
        }
    )

    result = subprocess.run(
        [str(WRAPPER), "--task", "build", "inspect"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    prompt = _calls(log)[0]["prompt"]
    assert "Recall-first pending annotation" not in prompt
    assert "- Read when work concerns api behavior." in prompt
    matrix = read_json(workspace.matrix_path("shared"))
    assert matrix["tasks"]["build"]["records"]["reference.docs.api"]["label"] == "lazy"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--append-system-prompt", "USER APPEND", "inspect this"],
        ["--append-system-prompt-file=extra.md", "inspect this"],
    ],
)
def test_claude_rejects_a_second_append_surface(workspace, tmp_path, arguments):
    executable, log = _fake_claude(tmp_path)
    environment = _environment(workspace, executable, log)

    result = subprocess.run(
        [str(WRAPPER), "--task", "all-lazy", *arguments],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "owns the appended system prompt" in result.stderr
    assert not log.exists()


def test_claude_auto_handoff_runs_twice_and_uses_separate_preferences(
    workspace, tmp_path
):
    write_task(workspace)
    executable, log = _fake_claude(tmp_path)
    environment = _environment(workspace, executable, log)
    environment.update(
        {
            "FAKE_HANDOFF_TASK": "build",
            "FAKE_REQUEST": "inspect this",
            "FAKE_MEMORY_PREFERENCE": "prefer direct reads",
        }
    )

    result = subprocess.run(
        [str(WRAPPER), "--auto", "inspect this"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = _calls(log)
    assert len(calls) == 2
    assert calls[0]["argv"][-1] == calls[1]["argv"][-1] == "inspect this"
    assert calls[0]["prompt_path"] != calls[1]["prompt_path"]
    assert "# Auto" in calls[0]["prompt"]
    assert "prefer direct reads" in calls[1]["prompt"]
    assert not Path(str(calls[0]["prompt_path"])).exists()
    assert not Path(str(calls[1]["prompt_path"])).exists()
    preferences = read_json(
        workspace.state_dir / "clients" / "claude" / "preferences.json"
    )
    assert preferences["tasks"]["build"]["selection_count"] == 1
    assert not (workspace.state_dir / "clients" / "codex" / "preferences.json").exists()
    assert not list((workspace.state_dir / "handoffs").glob("*.json"))
    assert not list(
        (workspace.state_dir / "clients" / "claude" / "registrations").glob("*.json")
    )


def test_claude_auto_without_handoff_stays_one_process(workspace, tmp_path):
    executable, log = _fake_claude(tmp_path)
    environment = _environment(workspace, executable, log)

    result = subprocess.run(
        [str(WRAPPER), "--auto", "answer directly"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert len(_calls(log)) == 1
    assert not (
        workspace.state_dir / "clients" / "claude" / "preferences.json"
    ).exists()
    assert not list((workspace.state_dir / "handoffs").glob("*.json"))


def test_claude_stale_prompt_cleanup_preserves_active_launches(workspace):
    active = ClaudeAdapter(workspace, launch_cwd=workspace.root)
    active_prompt = active.build_task_prompt("all-lazy")
    active_registry = active.registrations_dir / f"{active_prompt.parent.name}.json"

    ClaudeAdapter(workspace, launch_cwd=workspace.root).cleanup_registrations()

    assert active_prompt.exists()
    assert active_registry.exists()

    stale = ClaudeAdapter(workspace, launch_cwd=workspace.root)
    stale_prompt = stale.build_task_prompt("all-lazy")
    stale_registry = stale.registrations_dir / f"{stale_prompt.parent.name}.json"
    stale_value = read_json(stale_registry)
    stale_value["pid"] = 2_000_000_000
    stale_value["process_start_time"] = "not-running"
    write_json(stale_registry, stale_value)

    ClaudeAdapter(workspace, launch_cwd=workspace.root).cleanup_registrations()

    assert not stale_prompt.exists()
    assert not stale_registry.exists()
    assert active_prompt.exists()
    assert active_registry.exists()
    active.release_prompt(active_prompt)


def test_claude_raw_is_exact_passthrough(workspace, tmp_path):
    executable, log = _fake_claude(tmp_path)
    environment = _environment(workspace, executable, log)
    arguments = ["--print", "do work"]

    result = subprocess.run(
        [str(WRAPPER), "--raw", *arguments],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert _calls(log)[0]["argv"] == arguments
    assert not workspace.generated_dir.exists()


def test_claude_resume_is_injected_but_warns_to_keep_the_same_task(workspace, tmp_path):
    executable, log = _fake_claude(tmp_path)
    environment = _environment(workspace, executable, log)

    result = subprocess.run(
        [str(WRAPPER), "--task", "all-lazy", "--continue"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "may contain earlier task context" in result.stderr
    assert _calls(log)[0]["argv"][-1] == "--continue"


@pytest.mark.parametrize("command", ["agents", "attach", "respawn", "ultrareview"])
def test_claude_unverified_agent_transports_fail_closed_without_raw(
    workspace, tmp_path, command
):
    executable, log = _fake_claude(tmp_path)
    environment = _environment(workspace, executable, log)

    result = subprocess.run(
        [str(WRAPPER), "--task", "all-lazy", command, "123"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "cannot verify task-memory injection" in result.stderr
    assert not log.exists()


def test_claude_snapshot_uses_claude_md_and_keeps_lazy_sources(workspace, tmp_path):
    record = write_record(workspace, "policy", "shared", "guard", "GUARD BODY")
    records = load_records(workspace.records_dir)
    write_json(
        workspace.annotations_dir / "matrix.json",
        {
            "version": 2,
            "always": {
                "policy.shared.guard": {
                    "label": "not-always",
                    "record_hash": records["policy.shared.guard"].content_hash,
                }
            },
            "tasks": {},
        },
    )
    target = tmp_path / "claude-snapshot"

    export_snapshot(workspace, "all-lazy", target)

    assert (target / "CLAUDE.md").is_file()
    assert (target / record.relative_to(workspace.root)).is_file()
    assert not (target / "AGENTS.md").exists()
    manifest = read_json(target / "manifest.json")
    assert manifest["client"] == "claude"
    assert manifest["entry"]["path"] == "CLAUDE.md"
    assert {path.name for path in target.iterdir()} == {
        "CLAUDE.md",
        "framework",
        ".memory",
        "manifest.json",
    }
