from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPOSITORY, write_json, write_record
from memory_framework.parsing import load_records
from memory_framework.workspace import Workspace


def clean_env(**values):
    result = {key: value for key, value in os.environ.items() if key not in {"MEMORY_WORKSPACE", "MEMORY_FRAMEWORK_ROOT"}}
    return {**result, "PYTHONDONTWRITEBYTECODE": "1", **values}


def run(framework, *args, cwd, env=None, script="memory.py"):
    return subprocess.run(
        [sys.executable, str(framework / "scripts" / script), *map(str, args)],
        cwd=cwd, env=clean_env(**(env or {})), text=True, capture_output=True,
    )


def install(destination):
    shutil.copytree(REPOSITORY, destination, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".ruff_cache", "tests", "tools", "packaging"))
    return destination


def bank(framework, root, marker):
    result = run(framework, "--workspace", root, "init", cwd=root.parent)
    assert result.returncode == 0, result.stderr
    workspace = Workspace(root, framework_root=framework)
    write_record(workspace, "reference", "docs", "example", marker)
    record = next(iter(load_records(workspace.records_dir).values()))
    write_json(workspace.matrix_path("shared"), {
        "version": 2, "always": {record.id: {"label": "always", "record_hash": record.content_hash}}, "tasks": {},
    })
    return workspace


def semantic_hashes(workspace):
    return {str(path.relative_to(workspace.memory_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
            for directory in ("records", "categories", "task-types", "annotations")
            for path in (workspace.memory_dir / directory).rglob("*") if path.is_file()}


def test_two_framework_releases_read_two_banks_without_binding_or_rewriting(tmp_path):
    first = install(tmp_path / "framework-a")
    second = install(tmp_path / "framework-b")
    declaration = json.loads((second / "framework.json").read_text())
    declaration["release"] = "3.1.0-test"
    write_json(second / "framework.json", declaration)
    for framework, marker in [(first, "FRAMEWORK_A"), (second, "FRAMEWORK_B")]:
        (framework / "instructions/common.md").write_text(marker)
    banks = [bank(first, tmp_path / "bank-a", "KNOWLEDGE_A"), bank(first, tmp_path / "bank-b", "KNOWLEDGE_B")]
    before = [semantic_hashes(workspace) for workspace in banks]
    for framework in (first, second):
        for index, workspace in enumerate(banks):
            result = run(framework, "--workspace", workspace.root, "render", "--task", "all-eager", cwd=tmp_path)
            assert result.returncode == 0, result.stderr
            content = Path(json.loads(result.stdout)["path"]).read_text()
            assert f"KNOWLEDGE_{'AB'[index]}" in content
            assert ("FRAMEWORK_A" if framework == first else "FRAMEWORK_B") in content
            assert str(framework / "scripts/load.py") in content
            assert f"--workspace {workspace.root}" in content
            assert str(first) not in (workspace.memory_dir / "protocol.json").read_text()
            assert not (workspace.memory_dir / "instructions").exists()
            assert semantic_hashes(workspace) == before[index]
    # Moving a bank does not require editing its declaration or records.
    moved = tmp_path / "moved-bank"
    shutil.move(banks[0].root, moved)
    result = run(second, "--workspace", moved, "validate", cwd=tmp_path)
    assert result.returncode == 0, result.stderr


def test_embedded_default_and_explicit_workspace_precedence(tmp_path):
    root = tmp_path / "embedded"
    framework = install(root / ".memory/framework")
    result = run(framework, "init", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (root / ".memory/protocol.json").is_file()
    other = bank(framework, tmp_path / "other", "OTHER_BANK")
    # Cwd can belong to a different bank: the embedded parent wins.
    result = run(framework, "validate", cwd=other.root)
    assert result.returncode == 0 and json.loads(result.stdout)["records"] == 0
    result = run(framework, "validate", cwd=tmp_path, env={"MEMORY_WORKSPACE": str(other.root)})
    assert result.returncode == 0 and json.loads(result.stdout)["records"] == 1
    result = run(framework, "--workspace", root, "validate", cwd=tmp_path, env={"MEMORY_WORKSPACE": str(other.root)})
    assert result.returncode == 0 and json.loads(result.stdout)["records"] == 0
    refused = run(framework, "init", cwd=tmp_path)
    assert refused.returncode == 2 and "existing memory bank" in refused.stderr


def test_external_framework_discovers_bank_from_cwd(tmp_path):
    framework = install(tmp_path / "external")
    workspace = bank(framework, tmp_path / "bank", "BODY")
    cwd = workspace.root / "project/subdir"
    cwd.mkdir(parents=True)
    result = run(framework, "validate", cwd=cwd)
    assert result.returncode == 0 and json.loads(result.stdout)["records"] == 1
    refused = run(framework, "init", cwd=cwd)
    assert refused.returncode == 2 and "existing memory bank" in refused.stderr
    assert not (cwd / ".memory").exists()


def test_embedded_commands_use_parent_bank_from_unrelated_cwd(tmp_path):
    root = tmp_path / "project"
    framework = install(root / ".memory/framework")
    workspace = bank(framework, root, "EMBEDDED_KNOWLEDGE")
    result = run(framework, "Read when work concerns example behavior.", cwd=tmp_path, script="load.py")
    assert result.returncode == 0 and "EMBEDDED_KNOWLEDGE" in result.stdout
    rendered = run(framework, "render", "--task", "all-eager", cwd=tmp_path)
    assert rendered.returncode == 0, rendered.stderr
    assert Path(json.loads(rendered.stdout)["path"]).is_relative_to(workspace.memory_dir)
    submission = tmp_path / "submission.json"
    record = next(iter(load_records(workspace.records_dir).values()))
    write_record(workspace, "reference", "docs", "example", "UPDATED_KNOWLEDGE")
    write_json(submission, {"version": 1, "task": None, "records": {record.id: {"always": "always"}}})
    updated = run(framework, "update", "submit", submission, cwd=tmp_path)
    assert updated.returncode == 0, updated.stderr
    prepared = run(framework, "relabel", "prepare", "--scope", "always", "--sharing", "shared", cwd=tmp_path)
    assert prepared.returncode == 0, prepared.stderr
    proposal_path = Path(prepared.stdout.strip())
    proposal = json.loads(proposal_path.read_text())
    proposal["decisions"]["always"][record.id] = "always"
    write_json(proposal_path, proposal)
    applied = run(framework, "relabel", "apply", prepared.stdout.strip(), cwd=tmp_path)
    assert applied.returncode == 0, applied.stderr
    for relative, args in [
        ("bin/memory-codex", ["--list"]),
        ("bin/memory-claude", ["--list"]),
        ("skills/maintain-memory/scripts/cleanup_artifacts.py", ["--dry-run"]),
    ]:
        result = subprocess.run([sys.executable, str(framework / relative), *args],
                                cwd=tmp_path, env=clean_env(), text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".memory").exists()


@pytest.mark.parametrize("embedded", [False, True])
def test_initialized_bank_tracks_shared_data_but_ignores_local_runtime(tmp_path, embedded):
    source = install(tmp_path / "source")
    root = tmp_path / "project"
    root.mkdir()

    def git(cwd, *args):
        return subprocess.check_output(["git", *args], cwd=cwd, env=clean_env(), text=True)

    git(root, "init", "-q")
    framework = source
    if embedded:
        git(source, "init", "-q")
        git(source, "add", ".")
        git(source, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-qm", "Framework fixture")
        git(root, "-c", "protocol.file.allow=always", "submodule", "add",
            str(source), ".memory/framework")
        framework = root / ".memory/framework"
    result = run(framework, "--workspace", root, "init", cwd=root)
    assert result.returncode == 0, result.stderr
    ignored = [
        "records/policy/local/private.md", "annotations/matrix.local.json",
        "generated/views/prompt.md", "state/clients/codex/preferences.json",
        "clients/codex/profiles/session.config.toml", "tools/__pycache__/tool.pyc",
        "tools/compiled.pyc",
    ]
    shared = [
        "records/policy/shared/rule.md", "records/reference/docs/fact.md",
        "categories/reference/docs.md", "task-types/task.md", "tools/helper.py",
    ]
    for relative in ignored + shared:
        path = root / ".memory" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    git(root, "add", ".memory")
    tracked = set(git(root, "ls-files").splitlines())
    assert all(f".memory/{path}" not in tracked for path in ignored)
    assert all(f".memory/{path}" in tracked for path in shared)
    assert {".memory/.gitignore", ".memory/protocol.json", ".memory/annotations/matrix.json"} <= tracked
    if embedded:
        assert git(root, "ls-files", "--stage", "--", ".memory/framework").startswith("160000 ")
        assert not any(path.startswith(".memory/framework/") for path in tracked)
    ignore = root / ".memory/.gitignore"
    ignore.write_text(ignore.read_text() + "custom-local-file\n")
    before = ignore.read_bytes()
    refused = run(framework, "--workspace", root, "init", cwd=root)
    assert refused.returncode == 2
    assert ignore.read_bytes() == before


@pytest.mark.parametrize("mutation", ["missing", "generation", "bool-generation", "snapshot", "extra"])
def test_bad_bank_protocol_is_rejected_before_writes_by_cli_and_loader(tmp_path, mutation):
    framework = install(tmp_path / "framework")
    workspace = bank(framework, tmp_path / "bank", "BODY")
    protocol = workspace.memory_dir / "protocol.json"
    value = json.loads(protocol.read_text())
    if mutation == "missing":
        protocol.unlink()
    else:
        if mutation == "generation": value["protocol"]["generation"] = 4
        if mutation == "bool-generation": value["protocol"]["generation"] = True
        if mutation == "snapshot": value["kind"] = "snapshot"
        if mutation == "extra": value["framework_path"] = str(framework)
        write_json(protocol, value)
    before = semantic_hashes(workspace)
    shutil.rmtree(workspace.state_dir, ignore_errors=True)
    result = run(framework, "--workspace", workspace.root, "render", "--task", "all-eager", cwd=tmp_path)
    assert result.returncode == 2, result.stdout
    assert "Traceback" not in result.stderr
    assert not workspace.generated_dir.exists() and not workspace.state_dir.exists()
    assert semantic_hashes(workspace) == before
    if mutation != "snapshot":
        lookup = run(framework, "--workspace", workspace.root, "Read when work concerns example behavior.", script="load.py", cwd=tmp_path)
        assert lookup.returncode == 2 and not lookup.stdout


def test_bad_framework_protocol_and_data_owned_instructions_are_not_used(tmp_path):
    framework = install(tmp_path / "framework")
    workspace = bank(framework, tmp_path / "bank", "BODY")
    fake = workspace.memory_dir / "instructions/common.md"
    fake.parent.mkdir()
    fake.write_text("BANK_MUST_NOT_SUPPLY_FRAMEWORK_BEHAVIOR")
    result = run(framework, "--workspace", workspace.root, "render", "--task", "all-eager", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "BANK_MUST_NOT_SUPPLY_FRAMEWORK_BEHAVIOR" not in Path(json.loads(result.stdout)["path"]).read_text()
    declaration = json.loads((framework / "framework.json").read_text())
    declaration["data_protocol"]["generation"] = 4
    write_json(framework / "framework.json", declaration)
    result = run(framework, "--workspace", workspace.root, "validate", cwd=tmp_path)
    assert result.returncode == 2 and "incompatible data protocol" in result.stderr
