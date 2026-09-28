from __future__ import annotations

import json
import os
import pty
import shutil
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codex.adapter import CodexAdapter, load_handoff
from codex.snapshot import export_snapshot
from conftest import (
    REPOSITORY,
    complete_proposal,
    read_json,
    write_json,
    write_record,
    write_task,
)
from memory_framework.errors import ValidationError
from memory_framework.relabel import apply_relabel, prepare_relabel
from memory_framework.rendering import render_task_view
from memory_framework.validation import validate_workspace


WRAPPER = REPOSITORY / "bin" / "memory-codex"
SESSION_ID = "018f5f95-6d5a-7f7f-9000-111111111111"


def _fake_codex(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "fake-codex"
    log = tmp_path / "calls.jsonl"
    executable.write_text(
        """#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path

entry = {"argv": sys.argv[1:], "cwd": os.getcwd()}
if os.environ.get("MEMORY_FRAMEWORK_HANDOFF"):
    entry["resume_contract"] = os.environ["MEMORY_FRAMEWORK_HANDOFF"]
stdin_text = ""
if os.environ.get("FAKE_READ_STDIN") or (sys.argv and sys.argv[-1] == "-"):
    stdin_text = sys.stdin.read()
    entry["stdin"] = stdin_text
with open(os.environ["FAKE_CODEX_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(entry) + "\\n")

if "# Automated task annotation preflight" in stdin_text:
    if os.environ.get("FAKE_RELABEL_EXIT"):
        raise SystemExit(int(os.environ["FAKE_RELABEL_EXIT"]))
    if os.environ.get("FAKE_SKIP_RELABEL"):
        raise SystemExit(0)
    schema_path = Path(sys.argv[sys.argv.index("--output-schema") + 1])
    result_path = Path(sys.argv[sys.argv.index("-o") + 1])
    always_label = os.environ.get("FAKE_RELABEL_ALWAYS", "not-always")
    task_label = os.environ.get("FAKE_RELABEL_TASK", "lazy")
    def synthesize(schema):
        if "enum" in schema:
            values = schema["enum"]
            if "always" in values:
                return always_label
            if task_label in values:
                return task_label
            return None
        return {key: synthesize(item) for key, item in schema.get("properties", {}).items()}
    result_path.write_text(
        json.dumps(synthesize(json.loads(schema_path.read_text(encoding="utf-8"))), sort_keys=True),
        encoding="utf-8",
    )
    raise SystemExit(0)

if "--profile" not in sys.argv:
    raise SystemExit(0)
profile_name = sys.argv[sys.argv.index("--profile") + 1]
profile = Path(os.environ["CODEX_HOME"]) / f"{profile_name}.config.toml"
text = profile.read_text(encoding="utf-8")
path_match = re.search(r"HANDOFF_PATH=([^\\s]+)", text)
nonce_match = re.search(r"HANDOFF_NONCE=([0-9a-f]+)", text)
if not path_match or not os.environ.get("FAKE_HANDOFF_TASK"):
    raise SystemExit(0)
contract = json.loads(os.environ["MEMORY_FRAMEWORK_HANDOFF"]) if os.environ.get("MEMORY_FRAMEWORK_HANDOFF") else {}
handoff = Path(contract.get("path", path_match.group(1)))
nonce = contract.get("nonce", nonce_match.group(1))
kind = contract.get("kind", os.environ.get("FAKE_HANDOFF_KIND", "new"))
value = {"version": 2, "nonce": nonce, "mode": "task", "kind": kind,
         "task": os.environ["FAKE_HANDOFF_TASK"]}
if kind == "new":
    value["request"] = os.environ.get("FAKE_REQUEST", "")
else:
    value["session_id"] = os.environ.get("FAKE_SESSION_ID")
if os.environ.get("FAKE_MEMORY_PREFERENCE"):
    value["memory_preference"] = os.environ["FAKE_MEMORY_PREFERENCE"]
handoff.write_text(json.dumps(value), encoding="utf-8")
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable, log


def _environment(workspace, executable: Path, log: Path, tmp_path: Path) -> dict[str, str]:
    value = os.environ.copy()
    value.update(
        {
            "MEMORY_WORKSPACE": str(workspace.root),
            "MEMORY_CODEX_BIN": str(executable),
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "FAKE_CODEX_LOG": str(log),
        }
    )
    return value


def _calls(log: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _run_with_picker(
    arguments: list[str], choice: str, *, environment: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    master_fd, slave_fd = pty.openpty()
    command = [str(WRAPPER), *arguments]
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        os.close(slave_fd)
    try:
        os.write(master_fd, f"{choice}\n".encode())
        stdout, stderr = process.communicate(timeout=10)
    finally:
        os.close(master_fd)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--version"],
        ["help"],
        ["login", "status"],
        ["mcp", "list"],
        ["plugin", "list"],
        ["doctor"],
        ["completion", "zsh"],
        ["archive", SESSION_ID],
        ["delete", SESSION_ID],
        ["cloud", "list", "--json"],
        ["cloud", "status", "task_123"],
        ["cloud", "diff", "task_123"],
        ["cloud", "apply", "task_123"],
    ],
)
def test_utility_commands_are_transparent_and_create_no_memory_state(
    workspace, tmp_path, arguments
):
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)

    result = subprocess.run(
        [str(WRAPPER), *arguments], env=environment, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert _calls(log) == [{"argv": arguments, "cwd": str(Path.cwd())}]
    assert not workspace.generated_dir.exists()
    assert not workspace.state_dir.joinpath("clients").exists()


def test_auto_flag_enters_auto_without_consuming_prompt(workspace, tmp_path):
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "--auto", "keep", "both words"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    calls = _calls(log)
    assert len(calls) == 1
    assert calls[0]["argv"][0] == "--profile"
    assert calls[0]["argv"][-2:] == ["keep", "both words"]


def test_raw_is_an_unconditional_exact_passthrough(workspace, tmp_path):
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)
    arguments = ["exec", "--ephemeral", "do work"]

    result = subprocess.run(
        [str(WRAPPER), "--raw", *arguments],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert _calls(log)[0]["argv"] == arguments
    assert not workspace.generated_dir.exists()


def test_new_handoff_is_v2_nonce_bound_and_runs_exactly_twice(workspace, tmp_path):
    write_task(workspace)
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)
    environment.update({"FAKE_HANDOFF_TASK": "build", "FAKE_REQUEST": "inspect this"})

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
    assert calls[0]["argv"][1] != calls[1]["argv"][1]
    assert not list((workspace.state_dir / "handoffs").glob("*.json"))


@pytest.mark.parametrize(
    "arguments",
    [
        ["fork", SESSION_ID],
        ["exec", "resume", SESSION_ID, "do not repeat"],
    ],
)
def test_auto_continuation_handoff_relaunches_with_fresh_task_profile(
    workspace, tmp_path, arguments
):
    write_task(workspace)
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)
    environment.update(
        {
            "FAKE_HANDOFF_TASK": "build",
            "FAKE_HANDOFF_KIND": "continue",
            "FAKE_SESSION_ID": SESSION_ID,
        }
    )

    result = subprocess.run(
        [str(WRAPPER), "--auto", *arguments],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = _calls(log)
    assert len(calls) == 2
    assert calls[0]["argv"][2:] == arguments
    contract = json.loads(calls[0]["resume_contract"])
    assert contract["version"] == 1
    assert contract["kind"] == "continue"
    assert contract["ephemeral"] is False
    assert contract["path"].endswith(".json")
    if arguments[0] == "fork":
        assert calls[1]["argv"][2:] == ["resume", SESSION_ID]
    else:
        assert calls[1]["argv"][2:5] == ["exec", "resume", SESSION_ID]
        assert "freshly selected task memory" in calls[1]["argv"][-1]
    assert not list((workspace.state_dir / "handoffs").glob("*.json"))


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["inspect this"],
        ["exec", "inspect this"],
        ["e", "inspect this"],
        ["review", "--uncommitted"],
        ["exec", "review", "--uncommitted"],
        ["resume", SESSION_ID],
        ["resume", "--last"],
        ["resume"],
        ["exec", "resume", SESSION_ID, "continue"],
        ["e", "resume", SESSION_ID, "continue"],
        ["fork", SESSION_ID],
    ],
)
def test_every_llm_operation_enters_memory_task_picker_before_codex(
    workspace, tmp_path, arguments
):
    write_task(workspace)
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)

    result = _run_with_picker(arguments, "build", environment=environment)

    assert result.returncode == 0, result.stderr
    interface = result.stdout + result.stderr
    assert "Select a memory task type:" in interface
    assert "build" in interface
    assert "auto" in interface
    calls = _calls(log)
    assert len(calls) == 1
    assert calls[0]["argv"][2:] == arguments
    assert "resume_contract" not in calls[0]
    assert "keeps the resumed thread's original developer instructions" not in result.stderr
    preferences = read_json(workspace.state_dir / "clients" / "codex" / "preferences.json")
    assert preferences["tasks"]["build"]["selection_count"] == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ["cloud", "exec", "--env", "env_123", "inspect this"],
        ["cloud"],
    ],
)
def test_cloud_llm_operations_show_picker_then_refuse_unsupported_task_injection(
    workspace, tmp_path, arguments
):
    write_task(workspace)
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)

    result = _run_with_picker(arguments, "build", environment=environment)

    assert result.returncode == 2
    assert "Select a memory task type:" in result.stdout + result.stderr
    assert "cannot inject task memory into Codex cloud" in result.stderr
    assert not log.exists()


def test_explicit_auto_cloud_llm_operation_is_rejected_before_launch(workspace, tmp_path):
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "--auto", "cloud", "exec", "--env", "env_123", "inspect"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "cannot inject Auto memory into Codex cloud" in result.stderr
    assert not log.exists()


def test_debug_send_message_enters_picker_then_refuses_uninjectable_llm_call(
    workspace, tmp_path
):
    write_task(workspace)
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)

    result = _run_with_picker(
        ["debug", "app-server", "send-message-v2", "inspect"],
        "build",
        environment=environment,
    )

    assert result.returncode == 2
    assert "Select a memory task type:" in result.stdout + result.stderr
    assert "cannot inject task memory into Codex debug app-server send-message-v2" in result.stderr
    assert not log.exists()


def test_picker_requires_terminal_instead_of_consuming_piped_prompt(workspace, tmp_path):
    write_task(workspace)
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "exec", "-"],
        input="this is the model prompt\n",
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        start_new_session=True,
    )

    assert result.returncode == 2
    assert "memory task selection requires a terminal" in result.stderr
    assert "use --task TASK or --auto" in result.stderr
    assert not log.exists()


def test_explicit_task_preserves_piped_model_prompt(workspace, tmp_path):
    write_task(workspace)
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)
    environment["FAKE_READ_STDIN"] = "1"

    result = subprocess.run(
        [str(WRAPPER), "--task", "build", "exec", "-"],
        input="this is the model prompt\n",
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert _calls(log)[0]["stdin"] == "this is the model prompt\n"


@pytest.mark.parametrize(
    "arguments",
    [
        ["inspect"],
        ["resume", SESSION_ID],
        ["fork", SESSION_ID],
    ],
)
def test_task_launch_preflight_classifies_selected_task_once_before_new_resume_or_fork(
    workspace, tmp_path, arguments
):
    write_record(workspace, "reference", "docs", "shared", "SHARED BODY")
    write_record(workspace, "policy", "local", "personal", "LOCAL BODY")
    write_task(workspace, "build")
    write_task(workspace, "docs", marker="Documentation artifacts.")
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "--task", "build", *arguments],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = _calls(log)
    assert len(calls) == 2
    classifier, task_call = calls
    assert classifier["argv"][2:5] == ["exec", "--ephemeral", "--skip-git-repo-check"]
    assert "# Automated task annotation preflight" in classifier["stdin"]
    assert "SHARED BODY" in classifier["stdin"]
    assert "LOCAL BODY" in classifier["stdin"]
    assert "Documentation artifacts." not in classifier["stdin"]
    assert task_call["argv"][2:] == arguments

    shared = read_json(workspace.matrix_path("shared"))
    local = read_json(workspace.matrix_path("local"))
    assert set(shared["tasks"]) == {"build"}
    assert set(local["tasks"]) == {"build"}
    assert "docs" not in shared["tasks"] and "docs" not in local["tasks"]
    remaining = validate_workspace(workspace, require_complete=False)["coverage_issues"]
    assert any("pending-task: shared/docs/reference.docs.shared" in item for item in remaining)
    assert any("pending-task: local/docs/policy.local.personal" in item for item in remaining)

    launch_files = list(
        workspace.generated_dir.glob("clients/codex/launches/*/developer-instructions.md")
    )
    assert len(launch_files) == 1
    task_prompt = launch_files[0].read_text(encoding="utf-8")
    assert "Recall-first pending annotation" not in task_prompt
    assert "- Read when work concerns shared behavior." in task_prompt
    assert "- Read when work concerns personal behavior." in task_prompt


def test_auto_handoff_runs_selected_task_preflight_before_final_profile(workspace, tmp_path):
    write_record(workspace, "reference", "docs", "api", "API BODY")
    write_task(workspace)
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)
    environment.update({"FAKE_HANDOFF_TASK": "build", "FAKE_REQUEST": "inspect"})

    result = subprocess.run(
        [str(WRAPPER), "--auto", "inspect"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = _calls(log)
    assert len(calls) == 3
    assert "# Automated task annotation preflight" in calls[1]["stdin"]
    assert calls[2]["argv"][-1] == "inspect"
    assert validate_workspace(workspace)["coverage_issues"] == []


def test_failed_annotation_classifier_fails_closed_before_task_session(workspace, tmp_path):
    write_record(workspace, "reference", "docs", "api", "API BODY")
    write_task(workspace)
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)
    environment["FAKE_RELABEL_EXIT"] = "7"

    result = subprocess.run(
        [str(WRAPPER), "--task", "build", "inspect"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "annotation Codex exited with status 7" in result.stderr
    calls = _calls(log)
    assert len(calls) == 1
    assert "# Automated task annotation preflight" in calls[0]["stdin"]
    assert read_json(workspace.matrix_path("shared"))["always"] == {}
    assert not list(
        workspace.generated_dir.glob("clients/codex/launches/*/developer-instructions.md")
    )


def test_resume_picker_can_still_choose_auto(workspace, tmp_path):
    write_task(workspace)
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)

    result = _run_with_picker(["resume", SESSION_ID], "auto", environment=environment)

    assert result.returncode == 0, result.stderr
    calls = _calls(log)
    assert len(calls) == 1
    assert calls[0]["argv"][2:] == ["resume", SESSION_ID]
    contract = json.loads(calls[0]["resume_contract"])
    assert contract["kind"] == "continue"
    assert contract["ephemeral"] is False
    assert "keeps the resumed thread's original developer instructions" not in result.stderr


def test_ephemeral_resume_rejects_task_handoff(workspace, tmp_path):
    write_task(workspace)
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)
    environment.update(
        {
            "FAKE_HANDOFF_TASK": "build",
            "FAKE_HANDOFF_KIND": "continue",
            "FAKE_SESSION_ID": SESSION_ID,
        }
    )

    result = subprocess.run(
        [str(WRAPPER), "--auto", "exec", "resume", "--ephemeral", SESSION_ID],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "ephemeral" in result.stderr
    assert len(_calls(log)) == 1


def test_handoff_rejects_wrong_nonce_symlink_mode_owner_and_invalid_uuid(tmp_path):
    path = tmp_path / "handoff.json"
    base = {
        "version": 2,
        "nonce": "a" * 32,
        "mode": "task",
        "kind": "continue",
        "task": "build",
        "session_id": SESSION_ID,
    }
    write_json(path, base)
    path.chmod(0o600)
    assert load_handoff(path, expected_nonce="a" * 32)["session_id"] == SESSION_ID

    with pytest.raises(ValidationError, match="nonce"):
        load_handoff(path, expected_nonce="b" * 32)
    write_json(path, {**base, "session_id": "not-a-uuid"})
    path.chmod(0o600)
    with pytest.raises(ValidationError, match="UUID"):
        load_handoff(path, expected_nonce="a" * 32)
    path.unlink()
    target = tmp_path / "target.json"
    write_json(target, base)
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(ValidationError, match="regular file"):
        load_handoff(path, expected_nonce="a" * 32)


def test_effective_cd_controls_override_checks_and_preferences(workspace, tmp_path):
    write_task(workspace)
    process_cwd = workspace.root / "process"
    effective_cwd = workspace.root / "effective"
    process_cwd.mkdir()
    effective_cwd.mkdir()
    executable, log = _fake_codex(tmp_path)
    environment = _environment(workspace, executable, log, tmp_path)
    environment.update({"FAKE_HANDOFF_TASK": "build", "FAKE_REQUEST": "inspect"})

    result = _run_with_picker(
        ["-C", str(effective_cwd), "inspect"],
        "build",
        cwd=process_cwd,
        environment=environment,
    )

    assert result.returncode == 0, result.stderr
    assert {call["cwd"] for call in _calls(log)} == {str(process_cwd)}
    preferences = read_json(workspace.state_dir / "clients" / "codex" / "preferences.json")
    assert preferences["tasks"]["build"]["directory_hints"] == [str(effective_cwd)]

    (effective_cwd / ".codex").mkdir()
    (effective_cwd / ".codex" / "config.toml").write_text(
        'developer_instructions = "override"\n', encoding="utf-8"
    )
    rejected = subprocess.run(
        [str(WRAPPER), "--task", "build", "-C", str(effective_cwd), "inspect"],
        cwd=process_cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert "would override" in rejected.stderr


def test_normal_render_fails_closed_on_pending_without_preparing_relabel(workspace):
    write_record(workspace, "reference", "docs", "pending", "PENDING BODY")
    write_task(workspace)

    with pytest.raises(ValidationError, match="requires complete fresh annotations"):
        render_task_view(workspace, "build")
    assert not workspace.generated_dir.joinpath("relabel").exists()


def test_generated_view_manifest_detects_stale_matrix(workspace):
    record = write_record(workspace, "reference", "docs", "api", "API")
    write_task(workspace)
    write_json(
        workspace.matrix_path("shared"),
        {
            "version": 2,
            "always": {
                record.stem.replace("api", "reference.docs.api"): {
                    "label": "always",
                    "record_hash": __import__("memory_framework.parsing", fromlist=["load_records"])
                    .load_records(workspace.records_dir)["reference.docs.api"]
                    .content_hash,
                }
            },
            "tasks": {},
        },
    )
    render_task_view(workspace, "build")
    validate_workspace(workspace)
    matrix = read_json(workspace.matrix_path("shared"))
    matrix["always"]["reference.docs.api"]["label"] = "not-always"
    write_json(workspace.matrix_path("shared"), matrix)

    report = validate_workspace(workspace, require_complete=False)
    assert any("pending-task: shared/build/reference.docs.api" in issue for issue in report["coverage_issues"])
    assert not any("stale-generated-view" in issue for issue in report["coverage_issues"])


def test_generated_view_manifest_detects_stale_builtin_instruction(workspace):
    write_task(workspace)
    render_task_view(workspace, "build")
    validate_workspace(workspace)

    common = workspace.instructions_dir / "common.md"
    common.write_text(common.read_text(encoding="utf-8") + "Changed.\n", encoding="utf-8")

    report = validate_workspace(workspace)
    assert report["coverage_issues"] == []


def test_cold_delete_of_all_runtime_artifacts_self_bootstraps(workspace, tmp_path):
    record = write_record(workspace, "reference", "docs", "api", "API BODY")
    task = write_task(workspace)
    record_source = record.read_text(encoding="utf-8")
    task_source = task.read_text(encoding="utf-8")
    matrix_source = workspace.matrix_path("shared").read_text(encoding="utf-8")

    proposal = prepare_relabel(
        workspace,
        scope="pending",
        sharing="shared",
        task_id="build",
    )
    complete_proposal(proposal, always="not-always", task="eager")
    apply_relabel(workspace, proposal)
    matrix_source = workspace.matrix_path("shared").read_text(encoding="utf-8")
    render_task_view(workspace, "build")
    update = workspace.generated_dir / "updates" / "in-flight" / "submission.json"
    write_json(
        update,
        {
            "version": 1,
            "task": "build",
            "records": {
                "reference.docs.api": {
                    "always": "not-always",
                    "task_label": "lazy",
                }
            },
        },
    )
    relabel = workspace.generated_dir / "relabel" / "in-flight" / "proposal.json"
    write_json(relabel, {"transaction": "scratch"})

    codex_home = tmp_path / "codex-home"
    first = CodexAdapter(
        workspace,
        codex_home=codex_home,
        executable="true",
        launch_cwd=workspace.root,
        effective_cwd=workspace.root,
    )
    first_name, first_profile = first.build_task_profile("build")
    first_registration = codex_home / f"{first_name}.config.toml"
    assert first_profile.exists() and first_registration.is_symlink()

    # Simulate a cold cache deletion: no transaction or client process remains
    # active, and every derived runtime artifact disappears at once.
    first_registration.unlink()
    shutil.rmtree(workspace.generated_dir)
    shutil.rmtree(workspace.memory_dir / "clients" / "codex" / "profiles")
    shutil.rmtree(workspace.state_dir / "clients" / "codex" / "registrations")
    shutil.rmtree(workspace.state_dir / "handoffs", ignore_errors=True)

    report = validate_workspace(workspace, require_complete=False)
    assert report["records"] == 1 and report["tasks"] == 1
    assert record.read_text(encoding="utf-8") == record_source
    assert task.read_text(encoding="utf-8") == task_source
    assert workspace.matrix_path("shared").read_text(encoding="utf-8") == matrix_source

    rebuilt_view = render_task_view(workspace, "build")
    assert "API BODY" in rebuilt_view["content"]
    second = CodexAdapter(
        workspace,
        codex_home=codex_home,
        executable="true",
        launch_cwd=workspace.root,
        effective_cwd=workspace.root,
    )
    second_name, second_profile = second.build_task_profile("build")
    assert second_profile.exists()
    assert (codex_home / f"{second_name}.config.toml").resolve() == second_profile.resolve()
    second.release_profile(second_name)


def test_frozen_sentinel_blocks_mutating_cli_and_adapter(workspace, tmp_path):
    workspace.memory_dir.joinpath("FROZEN").write_text("frozen\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="frozen"):
        render_task_view(workspace, "all-lazy")
    with pytest.raises(ValidationError, match="frozen"):
        CodexAdapter(workspace, codex_home=tmp_path / "codex-home").build_task_profile("all-lazy")


@pytest.mark.parametrize("include_local", [False, True])
def test_snapshot_permissions_are_immutable(workspace, tmp_path, include_local):
    target = tmp_path / ("local" if include_local else "shared")
    export_snapshot(workspace, "all-lazy", target, include_local=include_local)
    directory_mode = 0o500 if include_local else 0o555
    file_mode = 0o400 if include_local else 0o444
    assert stat.S_IMODE(target.stat().st_mode) == directory_mode
    for path in target.rglob("*"):
        expected = directory_mode if path.is_dir() else file_mode
        assert stat.S_IMODE(path.stat().st_mode) == expected


def test_two_same_task_profiles_with_different_notes_do_not_cross_talk(workspace, tmp_path):
    write_task(workspace)
    codex_home = tmp_path / "codex-home"
    barrier = threading.Barrier(2)

    def build(note: str) -> tuple[str, Path]:
        adapter = CodexAdapter(
            workspace,
            codex_home=codex_home,
            executable="true",
            launch_cwd=workspace.root,
            effective_cwd=workspace.root,
        )
        barrier.wait()
        return adapter.build_task_profile("build", session_note=note)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(build, "SESSION NOTE ALPHA")
        second = executor.submit(build, "SESSION NOTE BETA")
        name_a, path_a = first.result()
        name_b, path_b = second.result()

    assert name_a != name_b and path_a != path_b
    text_a = path_a.read_text(encoding="utf-8")
    text_b = path_b.read_text(encoding="utf-8")
    assert "SESSION NOTE ALPHA" in text_a and "SESSION NOTE BETA" not in text_a
    assert "SESSION NOTE BETA" in text_b and "SESSION NOTE ALPHA" not in text_b
    assert (codex_home / f"{name_a}.config.toml").resolve() == path_a.resolve()
    assert (codex_home / f"{name_b}.config.toml").resolve() == path_b.resolve()


def test_two_concurrent_auto_launches_use_distinct_profiles_and_leave_no_handoff(
    workspace, tmp_path
):
    executable = tmp_path / "barrier-codex"
    log = tmp_path / "barrier-calls.jsonl"
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    executable.write_text(
        """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
marker = Path(os.environ["FAKE_BARRIER_DIR"]) / sys.argv[2]
marker.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 5
while len(list(marker.parent.iterdir())) < 2 and time.monotonic() < deadline:
    time.sleep(0.01)
with open(os.environ["FAKE_CODEX_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    environment = _environment(workspace, executable, log, tmp_path)
    environment["FAKE_BARRIER_DIR"] = str(barrier_dir)

    first = subprocess.Popen(
        [str(WRAPPER), "--auto", "alpha"],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second = subprocess.Popen(
        [str(WRAPPER), "--auto", "beta"],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first_output = first.communicate(timeout=10)
    second_output = second.communicate(timeout=10)

    assert first.returncode == 0, first_output
    assert second.returncode == 0, second_output
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    assert calls[0][1] != calls[1][1]
    assert {call[-1] for call in calls} == {"alpha", "beta"}
    assert not list((workspace.state_dir / "handoffs").glob("*.json"))
    assert not list(
        (workspace.memory_dir / "clients" / "codex" / "profiles").glob("*.config.toml")
    )
