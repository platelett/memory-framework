from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from agy.adapter import AgyAdapter
from agy.cli import _is_continuation, _is_utility
from conftest import REPOSITORY, write_record, write_task
from memory_framework.errors import ValidationError
from test_claude_client import _fake_codex_classifier


WRAPPER = REPOSITORY / "bin/memory-agy"


def fake_agy(tmp_path):
    executable = tmp_path / "fake-agy"
    log = tmp_path / "agy-calls.jsonl"
    executable.write_text('''#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
entry = {"argv": sys.argv[1:], "cwd": os.getcwd()}
if "--agent" in sys.argv:
    name = sys.argv[sys.argv.index("--agent") + 1]
    path = Path(os.environ["MEMORY_AGY_AGENT_ROOT"]) / name / "agent.md"
    if name.startswith("memory-"):
        entry.update(agent=str(path), body=path.read_text(), mode=path.stat().st_mode & 0o777)
with open(os.environ["FAKE_AGY_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\\n")
body = entry.get("body", "")
path = re.search(r"HANDOFF_PATH=([^\\s]+)", body)
nonce = re.search(r"HANDOFF_NONCE=([0-9a-f]+)", body)
if path and nonce and os.environ.get("FAKE_HANDOFF_TASK"):
    value = {"version":2,"nonce":nonce[1],"mode":"task","kind":"new",
             "task":os.environ["FAKE_HANDOFF_TASK"],"request":os.environ["FAKE_REQUEST"]}
    p=Path(path[1]);p.write_text(json.dumps(value));p.chmod(0o600)
''')
    executable.chmod(0o700)
    return executable, log


def env_for(workspace, tmp_path):
    executable, log = fake_agy(tmp_path)
    env = dict(os.environ, MEMORY_WORKSPACE=str(workspace.root),
               MEMORY_AGY_BIN=str(executable), FAKE_AGY_LOG=str(log),
               MEMORY_AGY_AGENT_ROOT=str(tmp_path / "agents"),
               MEMORY_CODEX_BIN=str(_fake_codex_classifier(tmp_path)),
               CODEX_HOME=str(tmp_path / "codex-home"))
    return env, log


def invoke(workspace, env, *args):
    # A new session deliberately has no controlling terminal.
    return subprocess.run([str(WRAPPER), *args], cwd=workspace.root, env=env,
                          capture_output=True, text=True, start_new_session=True)


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.parametrize("args", [["--version"], ["models"], ["mcp", "list"], ["--raw", "--agent", "user"]])
def test_utility_passthrough_has_no_confirmation_or_runtime(workspace, tmp_path, args):
    env, log = env_for(workspace, tmp_path)
    result = invoke(workspace, env, *args)
    assert result.returncode == 0, result.stderr
    assert calls(log)[0]["argv"] == (args[1:] if args[0] == "--raw" else args)
    assert not workspace.generated_dir.exists()


def test_noninteractive_confirmation_fails_before_classifier_and_launch(workspace, tmp_path):
    write_record(workspace, "reference", "docs", "api", "BODY")
    write_task(workspace)
    env, log = env_for(workspace, tmp_path)
    result = invoke(workspace, env, "--task", "build", "-p", "inspect")
    assert result.returncode == 2
    assert "--accept-limitations" in result.stderr
    assert not log.exists()
    assert not workspace.generated_dir.exists()


@pytest.mark.parametrize("answer,accepted", [("\n", False), ("n\n", False), ("yes\n", True)])
def test_interactive_confirmation_is_default_no_and_uses_tty(workspace, monkeypatch, answer, accepted):
    class Terminal(io.StringIO):
        def readline(self, *args): return answer
        def close(self): pass
    terminal = Terminal()
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: terminal)
    adapter = AgyAdapter(workspace)
    if accepted:
        adapter.confirm_launch("all-lazy", continuation=True)
    else:
        with pytest.raises(ValidationError, match="cancelled"):
            adapter.confirm_launch("all-lazy", continuation=True)
    assert "RESUME" in terminal.getvalue()
    assert "[y/N]" in terminal.getvalue()


def test_confirmed_launch_composes_rules_and_cleans_private_artifacts(workspace, tmp_path):
    (workspace.root / "AGENTS.md").write_text("WORKSPACE_SENTINEL")
    env, log = env_for(workspace, tmp_path)
    result = invoke(workspace, env, "--task", "all-lazy", "--accept-limitations", "-p", "probe")
    assert result.returncode == 0, result.stderr
    entry = calls(log)[0]
    assert "WORKSPACE_SENTINEL" in entry["body"]
    assert "# Active task type" in entry["body"]
    assert entry["mode"] == 0o600
    assert entry["argv"][2:] == ["-p", "probe"]
    assert not Path(entry["agent"]).exists()
    assert not list(workspace.state_dir.glob("clients/agy/registrations/*.json"))
    assert not (workspace.root / ".agents").exists()


def test_standard_task_preflights_before_render_after_consent(workspace, tmp_path):
    write_record(workspace, "reference", "docs", "api", "API_BODY")
    write_task(workspace)
    env, log = env_for(workspace, tmp_path)
    result = invoke(workspace, env, "--accept-limitations", "--task", "build", "-p", "inspect")
    assert result.returncode == 0, result.stderr
    assert "Read when work concerns api behavior." in calls(log)[0]["body"]
    assert "Codex classifier" in result.stderr


def test_auto_handoff_confirms_both_phases_and_preserves_request(workspace, tmp_path):
    write_task(workspace)
    env, log = env_for(workspace, tmp_path)
    env.update(FAKE_HANDOFF_TASK="build", FAKE_REQUEST="inspect this")
    result = invoke(workspace, env, "--auto", "--accept-limitations", "-p", "inspect this")
    assert result.returncode == 0, result.stderr
    entries = calls(log)
    assert len(entries) == 2
    assert entries[1]["argv"][2:] == ["-p", "inspect this"]
    assert result.stderr.count("Accepted for this invocation") == 2
    assert "# Active task type" in entries[1]["body"]


def test_resume_warns_and_keeps_native_arguments(workspace, tmp_path):
    env, log = env_for(workspace, tmp_path)
    args = ["--conversation", "2797a912-0fb9-4dd7-8cdf-833a89e8e7a4", "-p", "continue"]
    result = invoke(workspace, env, "--task", "all-lazy", "--accept-limitations", *args)
    assert result.returncode == 0, result.stderr
    assert "original agent prompt" in result.stderr
    assert calls(log)[0]["argv"][2:] == args


def test_prompt_values_are_not_parsed_as_options(workspace):
    adapter = AgyAdapter(workspace)
    adapter.validate_launch_arguments(["-p", "--agent"])
    assert not _is_continuation(["-p", "--continue"])
    assert not _is_utility(["-p", "--help"])
    with pytest.raises(ValidationError, match="owns --agent"):
        adapter.validate_launch_arguments(["--agent=user"])


def test_overlapping_launches_keep_independent_agent_files(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_AGY_AGENT_ROOT", str(tmp_path / "agents"))
    first, second = AgyAdapter(workspace), AgyAdapter(workspace)
    a, b = first.build_task_prompt("all-lazy"), second.build_task_prompt("all-eager")
    one, two = first._install_agent(a), second._install_agent(b)
    second.cleanup_registrations()
    first.release_prompt(a)
    assert not (first.agent_root / one).exists()
    assert (second.agent_root / two / "agent.md").exists()
    second.release_prompt(b)
    assert not (second.agent_root / two).exists()


def test_stale_registration_cleans_only_unchanged_owned_agent(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_AGY_AGENT_ROOT", str(tmp_path / "agents"))
    adapter = AgyAdapter(workspace)
    prompt = adapter.build_task_prompt("all-lazy")
    name = adapter._install_agent(prompt)
    registry = next(adapter.registrations_dir.glob("*.json"))
    value = json.loads(registry.read_text())
    value["process_start_time"] = "not-the-current-process"
    registry.write_text(json.dumps(value))
    path = adapter.agent_root / name / "agent.md"
    original = path.read_text()
    path.write_text(original + "\nUser modification\n")
    AgyAdapter(workspace).cleanup_registrations()
    assert path.exists() and registry.exists()
    path.write_text(original)
    AgyAdapter(workspace).cleanup_registrations()
    assert not path.exists() and not registry.exists() and not prompt.exists()


def test_snapshot_exports_agy_entry_without_launch_or_consent(workspace, tmp_path):
    env, log = env_for(workspace, tmp_path)
    target = tmp_path / "snapshot"
    result = invoke(workspace, env, "--snapshot", "all-lazy", str(target))
    assert result.returncode == 0, result.stderr
    assert not log.exists()
    assert (target / "AGENTS.md").is_file()
    assert not (target / "CLAUDE.md").exists()
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["client"] == "agy"
    assert manifest["entry"]["path"] == "AGENTS.md"
