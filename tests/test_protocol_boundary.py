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
