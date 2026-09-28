from __future__ import annotations

import json
import os
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

from codex.adapter import CodexAdapter, load_handoff
from codex.snapshot import export_snapshot
from conftest import REPOSITORY, read_json, write_json, write_record, write_task
from memory_framework.errors import ValidationError
from memory_framework.parsing import load_records


def _always_matrix(workspace, labels):
    records = load_records(workspace.records_dir)
    matrix = {"version": 2, "always": {}, "tasks": {}}
    for record_id, record in records.items():
        if record_id in labels:
            matrix["always"][record_id] = {
                "label": labels[record_id],
                "record_hash": record.content_hash,
            }
    return matrix


def test_profile_composition_stability_symlink_and_project_override(workspace, tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'developer_instructions = "USER PREFIX"\nmodel = "example"\n', encoding="utf-8"
    )
    adapter = CodexAdapter(
        workspace,
        codex_home=codex_home,
        executable="true",
        launch_cwd=workspace.root,
        effective_cwd=workspace.root,
    )
    name, profile = adapter.build_task_profile("all-lazy")
    first_inode = profile.stat().st_ino
    profile_text = profile.read_text(encoding="utf-8")
    assert "USER PREFIX" in profile_text and "Workspace memory" not in profile_text
    profile_config = tomllib.loads(profile_text)
    assert profile_config["features"]["terminal_visualization_instructions"] is True
    assert profile_config["suppress_unstable_features_warning"] is True
    registration = codex_home / f"{name}.config.toml"
    assert registration.is_symlink() and registration.resolve() == profile.resolve()

    name_again, profile_again = adapter.build_task_profile("all-lazy")
    assert name_again != name and profile_again != profile
    assert profile.stat().st_ino == first_inode
    assert (codex_home / f"{name_again}.config.toml").resolve() == profile_again.resolve()

    project_config = workspace.root / ".codex" / "config.toml"
    project_config.parent.mkdir()
    project_config.write_text('developer_instructions = "OVERRIDE"\n', encoding="utf-8")
    with pytest.raises(ValidationError, match="would override"):
        adapter.build_task_profile("all-eager")


def test_profile_registration_ownership_and_argument_rejection(workspace, tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    adapter = CodexAdapter(workspace, codex_home=codex_home)
    name = adapter._profile_name("all-lazy")
    collision = codex_home / f"{name}.config.toml"
    collision.write_text("model = 'other'\n", encoding="utf-8")
    adapter.build_task_profile("all-lazy")
    assert collision.read_text(encoding="utf-8") == "model = 'other'\n"
    with pytest.raises(ValidationError, match="owns --profile"):
        adapter.validate_launch_arguments(["--profile", "other"])
    with pytest.raises(ValidationError, match="cannot override"):
        adapter.validate_launch_arguments(["-c", "developer_instructions='x'"])
    with pytest.raises(ValidationError, match="was removed"):
        adapter.validate_launch_arguments(["--maintain", "init"])


def test_stale_registration_cleanup_uses_its_recorded_codex_home(workspace, tmp_path):
    first_home = tmp_path / "first-home"
    second_home = tmp_path / "second-home"
    first_home.mkdir()
    second_home.mkdir()
    first = CodexAdapter(workspace, codex_home=first_home)
    profile_name, profile = first.build_task_profile("all-lazy")
    launch_id = profile.stem.removesuffix(".config")
    registry = first.registrations_dir / f"{launch_id}.json"
    registration = first_home / f"{profile_name}.config.toml"
    value = read_json(registry)
    value["pid"] = 2_000_000_000
    value["process_start_time"] = "not-running"
    write_json(registry, value)

    CodexAdapter(workspace, codex_home=second_home).cleanup_registrations()

    assert not os.path.lexists(registration)
    assert not profile.exists()
    assert not registry.exists()


def test_auto_two_phase_handoff_and_preferences(workspace, tmp_path):
    write_task(workspace)
    fake = tmp_path / "fake-codex"
    log = tmp_path / "calls.jsonl"
    fake.write_text(
        """#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
with open(os.environ['FAKE_CODEX_LOG'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
profile_name = sys.argv[sys.argv.index('--profile') + 1]
profile = Path(os.environ['CODEX_HOME']) / f'{profile_name}.config.toml'
text = profile.read_text(encoding='utf-8')
match = re.search(r'HANDOFF_PATH=([^\\s]+)', text)
nonce = re.search(r'HANDOFF_NONCE=([0-9a-f]+)', text)
handoff = match.group(1) if match else None
if handoff:
    request = sys.argv[-1] if len(sys.argv) > 3 else ''
    with open(handoff, 'w', encoding='utf-8') as handle:
        json.dump({'version':2,'nonce':nonce.group(1),'mode':'task','kind':'new',
                   'task':'build','request':request,'remember':True,
                   'normalized_intent':'inspect unusual work','directory_hint':'project-a',
                   'memory_preference':'prefer direct reads'}, handle)
""",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    codex_home = tmp_path / "codex-home"
    environment = os.environ.copy()
    environment.update(
        {
            "MEMORY_WORKSPACE": str(workspace.root),
            "MEMORY_CODEX_BIN": str(fake),
            "CODEX_HOME": str(codex_home),
            "FAKE_CODEX_LOG": str(log),
        }
    )
    result = subprocess.run(
        [str(REPOSITORY / "bin" / "memory-codex"), "--auto", "inspect this"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    assert calls[0][-1] == "inspect this" and calls[1][-1] == "inspect this"
    preferences = read_json(workspace.state_dir / "clients" / "codex" / "preferences.json")
    serialized = json.dumps(preferences)
    assert "inspect this" not in serialized
    assert "inspect unusual work" in serialized
    assert not list((workspace.state_dir / "handoffs").glob("*.json"))
    task_profiles = list((workspace.memory_dir / "clients" / "codex" / "profiles").glob("*.config.toml"))
    assert len(task_profiles) == 1
    assert "prefer direct reads" in task_profiles[0].read_text(encoding="utf-8")


def test_wrapper_preserves_launch_directory_for_auto_and_direct_task(workspace, tmp_path):
    write_task(workspace)
    launch_directory = workspace.root / "projects" / "kernel-a"
    launch_directory.mkdir(parents=True)
    fake = tmp_path / "fake-codex"
    log = tmp_path / "cwd-calls.jsonl"
    fake.write_text(
        """#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
with open(os.environ['FAKE_CODEX_LOG'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),
                             'handoff_env': 'MEMORY_FRAMEWORK_HANDOFF' in os.environ}) + '\\n')
profile_name = sys.argv[sys.argv.index('--profile') + 1]
profile = Path(os.environ['CODEX_HOME']) / f'{profile_name}.config.toml'
text = profile.read_text(encoding='utf-8')
match = re.search(r'HANDOFF_PATH=([^\\s]+)', text)
nonce = re.search(r'HANDOFF_NONCE=([0-9a-f]+)', text)
handoff = match.group(1) if match else None
if handoff:
    request = sys.argv[-1] if len(sys.argv) > 3 else ''
    with open(handoff, 'w', encoding='utf-8') as handle:
        json.dump({'version':2,'nonce':nonce.group(1),'mode':'task','kind':'new',
                   'task':'build','request':request}, handle)
""",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    environment = os.environ.copy()
    environment.update(
        {
            "MEMORY_WORKSPACE": str(workspace.root),
            "MEMORY_CODEX_BIN": str(fake),
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "FAKE_CODEX_LOG": str(log),
            "MEMORY_FRAMEWORK_HANDOFF": str(tmp_path / "outer-handoff.json"),
        }
    )

    result = subprocess.run(
        [str(REPOSITORY / "bin" / "memory-codex"), "--auto", "inspect kernel"],
        cwd=launch_directory,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    assert {call["cwd"] for call in calls} == {str(launch_directory)}
    assert not any(call["handoff_env"] for call in calls)
    preferences = read_json(workspace.state_dir / "clients" / "codex" / "preferences.json")
    assert preferences["tasks"]["build"]["directory_hints"] == [str(launch_directory)]

    log.write_text("", encoding="utf-8")
    result = subprocess.run(
        [str(REPOSITORY / "bin" / "memory-codex"), "--task", "all-lazy"],
        cwd=launch_directory,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    direct_calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(direct_calls) == 1
    assert direct_calls[0]["cwd"] == str(launch_directory)
    assert direct_calls[0]["handoff_env"] is False
    assert not (tmp_path / "outer-handoff.json").exists()


def test_auto_performs_maintenance_without_second_launch(workspace, tmp_path):
    fake = tmp_path / "fake-codex"
    log = tmp_path / "calls.jsonl"
    fake.write_text(
        """#!/usr/bin/env python3
import json, os, sys
with open(os.environ['FAKE_CODEX_LOG'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
""",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    codex_home = tmp_path / "codex-home"
    environment = os.environ.copy()
    environment.update(
        {
            "MEMORY_WORKSPACE": str(workspace.root),
            "MEMORY_CODEX_BIN": str(fake),
            "CODEX_HOME": str(codex_home),
            "FAKE_CODEX_LOG": str(log),
        }
    )
    result = subprocess.run(
        [str(REPOSITORY / "bin" / "memory-codex"), "--auto", "initialize memory"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 1
    assert calls[0][0] == "--profile"
    assert not list((workspace.memory_dir / "clients" / "codex" / "profiles").glob("maintain-*.config.toml"))
    assert not (workspace.state_dir / "clients" / "codex" / "preferences.json").exists()


def test_auto_can_complete_a_request_without_task_handoff(workspace, tmp_path):
    fake = tmp_path / "fake-codex"
    log = tmp_path / "calls.jsonl"
    fake.write_text(
        """#!/usr/bin/env python3
import json, os, sys
with open(os.environ['FAKE_CODEX_LOG'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
""",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    environment = os.environ.copy()
    environment.update(
        {
            "MEMORY_WORKSPACE": str(workspace.root),
            "MEMORY_CODEX_BIN": str(fake),
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "FAKE_CODEX_LOG": str(log),
        }
    )
    result = subprocess.run(
        [str(REPOSITORY / "bin" / "memory-codex"), "--auto", "answer directly"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 1
    assert calls[0][0] == "--profile"
    profiles = workspace.memory_dir / "clients" / "codex" / "profiles"
    assert not list(profiles.glob("*.config.toml"))
    assert not (workspace.state_dir / "clients" / "codex" / "preferences.json").exists()
    assert not list((workspace.state_dir / "handoffs").glob("*.json"))


def test_auto_rejects_built_in_as_second_launch_request(workspace, tmp_path):
    fake = tmp_path / "fake-codex"
    log = tmp_path / "calls.jsonl"
    fake.write_text(
        """#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
with open(os.environ['FAKE_CODEX_LOG'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
profile_name = sys.argv[sys.argv.index('--profile') + 1]
profile = Path(os.environ['CODEX_HOME']) / f'{profile_name}.config.toml'
text = profile.read_text(encoding='utf-8')
match = re.search(r'HANDOFF_PATH=([^\\s]+)', text)
nonce = re.search(r'HANDOFF_NONCE=([0-9a-f]+)', text)
handoff = match.group(1) if match else None
if handoff:
    request = sys.argv[-1] if len(sys.argv) > 3 else ''
    with open(handoff, 'w', encoding='utf-8') as handle:
        json.dump({'version':2,'nonce':nonce.group(1),'mode':'task','kind':'new',
                   'task':'all-lazy','request':request}, handle)
""",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    environment = os.environ.copy()
    environment.update(
        {
            "MEMORY_WORKSPACE": str(workspace.root),
            "MEMORY_CODEX_BIN": str(fake),
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "FAKE_CODEX_LOG": str(log),
        }
    )
    result = subprocess.run(
        [str(REPOSITORY / "bin" / "memory-codex"), "--auto", "answer directly"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "must select a standard task" in result.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 1
    assert not (workspace.memory_dir / "clients" / "codex" / "profiles" / "all-lazy.config.toml").exists()
    assert not list((workspace.state_dir / "handoffs").glob("*.json"))


def test_completion_receipts_are_rejected_by_v2_handoff_schema(tmp_path):
    invalid = tmp_path / "invalid-handoff.json"
    write_json(
        invalid,
        {"version": 2, "nonce": "a" * 32, "mode": "complete", "kind": "new",
         "task": "build", "request": "x"},
    )
    invalid.chmod(0o600)
    with pytest.raises(ValidationError, match="mode must be task"):
        load_handoff(invalid, expected_nonce="a" * 32)


def test_snapshot_layout_local_boundary_stale_and_existing_target(workspace, tmp_path):
    shared = write_record(workspace, "reference", "docs", "shared", "SHARED BODY")
    local = write_record(workspace, "policy", "local", "secret", "LOCAL SECRET")
    (workspace.tools_dir / "checker").mkdir()
    (workspace.tools_dir / "checker" / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shared_matrix = _always_matrix(workspace, {"reference.docs.shared": "not-always"})
    local_matrix = _always_matrix(workspace, {"policy.local.secret": "not-always"})
    write_json(workspace.matrix_path("shared"), shared_matrix)
    write_json(workspace.matrix_path("local"), local_matrix)

    target = tmp_path / "snapshot"
    export_snapshot(workspace, "all-lazy", target)
    assert (target / "AGENTS.md").is_file()
    assert (target / ".codex" / "config.toml").is_file()
    assert (target / shared.relative_to(workspace.root)).is_file()
    assert not (target / local.relative_to(workspace.root)).exists()
    assert (target / ".memory" / "tools" / "checker" / "run.sh").is_file()
    assert not (target / ".memory" / "categories").exists()
    manifest = read_json(target / "manifest.json")
    assert manifest["entry"]["project_doc_max_bytes"] > manifest["entry"]["bytes"]
    assert all(record["sharing"] == "shared" for record in manifest["records"])
    top_level = {path.name for path in target.iterdir()}
    assert top_level == {"AGENTS.md", ".codex", ".memory", "framework", "manifest.json"}
    with pytest.raises(ValidationError, match="already exists"):
        export_snapshot(workspace, "all-lazy", target)

    included = tmp_path / "snapshot-local"
    export_snapshot(workspace, "all-lazy", included, include_local=True)
    assert (included / local.relative_to(workspace.root)).is_file()

    shared.write_text(shared.read_text(encoding="utf-8").replace("SHARED BODY", "CHANGED"), encoding="utf-8")
    with pytest.raises(ValidationError, match="stale-always"):
        export_snapshot(workspace, "all-lazy", tmp_path / "stale")


def test_core_has_no_client_specific_terms_and_normal_launch_does_not_create_entry(workspace, tmp_path):
    forbidden = ("codex", "agents.md", "claude.md", "developer_instructions")
    core = REPOSITORY / "lib"
    for path in core.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert not any(term in text for term in forbidden), path
    adapter = CodexAdapter(workspace, codex_home=tmp_path / "codex-home", executable="true")
    adapter.build_task_profile("all-lazy")
    assert not (workspace.root / "AGENTS.md").exists()
    assert not (workspace.root / "CLAUDE.md").exists()
