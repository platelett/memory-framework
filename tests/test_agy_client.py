from __future__ import annotations

import io
import json
import os
import pty
import select
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

from agy.adapter import AgyAdapter
from agy.cli import _is_continuation, _is_utility
from agy.terminal import LaunchCancelled
from conftest import REPOSITORY, write_record, write_task
from memory_framework.errors import ValidationError


WRAPPER = REPOSITORY / "bin/memory-agy"


def fake_agy(tmp_path):
    executable = tmp_path / "fake-agy"
    log = tmp_path / "agy-calls.jsonl"
    executable.write_text('''#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
entry = {"argv": sys.argv[1:], "cwd": os.getcwd()}
if os.environ.get("FAKE_AGY_READ_STDIN"):
    entry["stdin"] = sys.stdin.read()
if "--agent" in sys.argv:
    name = sys.argv[sys.argv.index("--agent") + 1]
    path = Path(os.environ["MEMORY_AGY_AGENT_ROOT"]) / name / "agent.md"
    if name.startswith("memory-"):
        entry.update(agent=str(path), body=path.read_text(), mode=path.stat().st_mode & 0o777)
with open(os.environ["FAKE_AGY_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\\n")
if "# Annotation preflight classifier" in entry.get("body", ""):
    assert "# Automated task annotation preflight" in entry["body"]
    assert '"tools": []' in entry["body"]
    assert "WORKSPACE_SENTINEL" not in entry["body"]
    schema = json.loads(re.search(r"# Required output schema\\n```json\\n(.*?)\\n```", entry["body"], re.S)[1])
    def synthesize(value):
        if "enum" in value:
            return "not-always" if "not-always" in value["enum"] else "lazy"
        return {key: synthesize(item) for key, item in value.get("properties", {}).items()}
    output = synthesize(schema)
    if os.environ.get("FAKE_ANNOTATION_INVALID"):
        output = {}
    print(json.dumps({"status":"SUCCESS","response":json.dumps(output)}))
    raise SystemExit(int(os.environ.get("FAKE_ANNOTATION_EXIT", "0")))
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
               MEMORY_CODEX_BIN="/must-not-run-codex",
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


@pytest.mark.parametrize("answer,accepted", [("\n", False), ("n\n", False), ("", False), ("yes\n", True)])
def test_interactive_confirmation_is_default_no_and_uses_tty(workspace, monkeypatch, answer, accepted):
    terminal = io.StringIO()
    monkeypatch.setattr("agy.adapter.terminal_streams", lambda: nullcontext((io.StringIO(answer), terminal)))
    adapter = AgyAdapter(workspace)
    if accepted:
        adapter.confirm_launch("all-lazy", continuation=True)
    else:
        with pytest.raises(LaunchCancelled, match="cancelled"):
            adapter.confirm_launch("all-lazy", continuation=True)
    assert "Resuming a conversation may keep its old memories" in terminal.getvalue()
    assert "[y/N]" in terminal.getvalue()


def pty_invoke(workspace, env, args, answers, *, redirected_input=None):
    """Exercise real tty streams, including tty stdin without a controlling tty."""
    master, slave = pty.openpty()
    kwargs = {"start_new_session": True}
    if redirected_input is not None:
        def controlling_terminal():
            import fcntl
            import termios
            os.setsid()
            fcntl.ioctl(1, termios.TIOCSCTTY, 0)
        kwargs = {"preexec_fn": controlling_terminal}
    process = subprocess.Popen(
        [str(WRAPPER), *args], cwd=workspace.root, env=env,
        stdin=slave if redirected_input is None else subprocess.PIPE,
        stdout=slave, stderr=slave, **kwargs,
    )
    os.close(slave)
    transcript = bytearray()
    try:
        if redirected_input is not None:
            process.stdin.write(redirected_input)
            process.stdin.close()
        os.write(master, answers)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                transcript.extend(data)
            elif process.poll() is not None:
                break
        return process.wait(timeout=2), transcript.decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def test_real_tty_task_selection_then_confirmation_without_dev_tty(workspace, tmp_path):
    env, log = env_for(workspace, tmp_path)
    status, transcript = pty_invoke(workspace, env, [], b"1\nyes\n")
    assert status == 0, transcript
    assert "Task:" in transcript and "Start session?" in transcript
    assert "All memories, read when needed" in transcript
    assert "[built-in]" not in transcript
    assert len(calls(log)) == 1


def test_real_tty_decline_is_a_normal_exit(workspace, tmp_path):
    env, log = env_for(workspace, tmp_path)
    status, transcript = pty_invoke(workspace, env, [], b"1\nn\n")
    assert status == 0, transcript
    assert "Launch cancelled." in transcript and "error:" not in transcript
    assert not log.exists()
    assert not workspace.generated_dir.exists()


def test_redirected_prompt_uses_controlling_tty_for_both_questions(workspace, tmp_path):
    env, log = env_for(workspace, tmp_path)
    env["FAKE_AGY_READ_STDIN"] = "1"
    prompt = b"Original model input, not a task selection or consent.\n"
    status, transcript = pty_invoke(workspace, env, [], b"1\ny\n", redirected_input=prompt)
    assert status == 0, transcript
    assert calls(log)[0]["stdin"] == prompt.decode()


def test_invalid_confirmation_answer_reprompts(workspace, monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr("agy.adapter.terminal_streams", lambda: nullcontext((io.StringIO("maybe\ny\n"), output)))
    AgyAdapter(workspace).confirm_launch("all-lazy", continuation=False)
    assert "Please enter y to start, n to cancel, or ? for details" in output.getvalue()


@pytest.mark.parametrize("answer,accepted", [("?\ny\n", True), ("?\nn\n", False), ("?\n", False)])
def test_details_do_not_accept_launch_or_claim_rule_caps(workspace, monkeypatch, answer, accepted):
    output = io.StringIO()
    monkeypatch.setattr("agy.adapter.terminal_streams", lambda: nullcontext((io.StringIO(answer), output)))
    adapter = AgyAdapter(workspace)
    if accepted:
        adapter.confirm_launch("all-lazy", continuation=False)
    else:
        with pytest.raises(LaunchCancelled):
            adapter.confirm_launch("all-lazy", continuation=False)
    summary, details = output.getvalue().split("How this mode works:")
    assert "Custom-agent tools" not in summary and "Codex" not in output.getvalue()
    assert "unverified" not in output.getvalue()
    assert "Resuming" not in summary
    assert "Native rule-file loading limits are separate" in details
    assert "does not truncate Memory" in details
    assert "24,000" not in output.getvalue() and "20,000" not in output.getvalue()
    assert output.getvalue().count("Start session?") == 2


def test_keyboard_interrupt_cancels_without_traceback(workspace, monkeypatch, capsys):
    from agy.cli import main
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setenv("MEMORY_WORKSPACE", str(workspace.root))
    monkeypatch.setattr(AgyAdapter, "confirm_launch", interrupted)
    assert main(["--task", "all-lazy"]) == 130
    assert capsys.readouterr().err == "\nLaunch cancelled.\n"


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
    (workspace.root / "AGENTS.md").write_text("WORKSPACE_SENTINEL")
    write_record(workspace, "reference", "docs", "api", "API_BODY")
    write_task(workspace)
    env, log = env_for(workspace, tmp_path)
    result = invoke(workspace, env, "--accept-limitations", "--task", "build", "-p", "inspect")
    assert result.returncode == 0, result.stderr
    entries = calls(log)
    assert len(entries) == 2
    assert "# Annotation preflight classifier" in entries[0]["body"]
    assert "Read when work concerns api behavior." in entries[1]["body"]
    assert "Preparing memories for build with agy" in result.stderr
    assert "Codex" not in result.stderr


@pytest.mark.parametrize("setting,value", [("FAKE_ANNOTATION_INVALID", "1"), ("FAKE_ANNOTATION_EXIT", "7")])
def test_own_annotation_failure_preserves_matrix_and_never_starts_task(workspace, tmp_path, setting, value):
    write_record(workspace, "reference", "docs", "api", "API_BODY")
    write_task(workspace)
    original = workspace.matrix_path("shared").read_bytes()
    env, log = env_for(workspace, tmp_path)
    env[setting] = value
    result = invoke(workspace, env, "--task", "build", "--accept-limitations", "-p", "inspect")
    assert result.returncode == 2
    assert "annotation agy" in result.stderr
    assert len(calls(log)) == 1
    assert workspace.matrix_path("shared").read_bytes() == original
    assert not list(workspace.state_dir.glob("clients/agy/registrations/*.json"))


def test_auto_handoff_confirms_both_phases_and_preserves_request(workspace, tmp_path):
    write_record(workspace, "reference", "docs", "api", "API_BODY")
    write_task(workspace)
    env, log = env_for(workspace, tmp_path)
    env.update(FAKE_HANDOFF_TASK="build", FAKE_REQUEST="inspect this")
    result = invoke(workspace, env, "--auto", "--accept-limitations", "-p", "inspect this")
    assert result.returncode == 0, result.stderr
    entries = calls(log)
    assert len(entries) == 3
    assert "# Annotation preflight classifier" in entries[1]["body"]
    assert entries[2]["argv"][2:] == ["-p", "inspect this"]
    assert result.stderr.count("Accepted for this invocation") == 2
    assert "# Active task type" in entries[2]["body"]


def test_resume_warns_and_keeps_native_arguments(workspace, tmp_path):
    env, log = env_for(workspace, tmp_path)
    args = ["--conversation", "2797a912-0fb9-4dd7-8cdf-833a89e8e7a4", "-p", "continue"]
    result = invoke(workspace, env, "--task", "all-lazy", "--accept-limitations", *args)
    assert result.returncode == 0, result.stderr
    assert "Resuming a conversation may keep its old memories" in result.stderr
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
