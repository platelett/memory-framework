from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from claude.adapter import ClaudeAdapter
from codex.adapter import CodexAdapter
from conftest import (
    REPOSITORY,
    complete_proposal,
    read_json,
    write_json,
    write_record,
    write_task,
)
from memory_framework.relabel import apply_relabel, prepare_relabel
from memory_framework.rendering import render_task_view
from memory_framework.validation import validate_workspace


CLEANER = (
    REPOSITORY
    / "skills"
    / "maintain-memory"
    / "scripts"
    / "cleanup_artifacts.py"
)


def _run_cleaner(workspace, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(CLEANER), "--workspace", str(workspace.root), *arguments],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _make_stale(registry: Path) -> None:
    value = read_json(registry)
    value["pid"] = 2_000_000_000
    value["process_start_time"] = "not-running"
    write_json(registry, value)


def test_cleanup_removes_caches_and_preserves_active_client_leases(workspace, tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    active_codex = CodexAdapter(workspace, codex_home=codex_home)
    active_name, active_profile = active_codex.build_task_profile("all-lazy")
    active_registration = codex_home / f"{active_name}.config.toml"
    active_codex_registry = (
        active_codex.registrations_dir
        / f"{active_profile.stem.removesuffix('.config')}.json"
    )
    active_codex_generated = Path(read_json(active_codex_registry)["generated"])

    stale_codex = CodexAdapter(workspace, codex_home=codex_home)
    stale_name, stale_profile = stale_codex.build_task_profile("all-lazy")
    stale_registration = codex_home / f"{stale_name}.config.toml"
    stale_codex_registry = (
        stale_codex.registrations_dir
        / f"{stale_profile.stem.removesuffix('.config')}.json"
    )
    _make_stale(stale_codex_registry)

    active_claude = ClaudeAdapter(workspace, launch_cwd=workspace.root)
    active_prompt = active_claude.build_task_prompt("all-lazy")
    active_claude_registry = (
        active_claude.registrations_dir / f"{active_prompt.parent.name}.json"
    )
    stale_claude = ClaudeAdapter(workspace, launch_cwd=workspace.root)
    stale_prompt = stale_claude.build_task_prompt("all-lazy")
    stale_claude_registry = (
        stale_claude.registrations_dir / f"{stale_prompt.parent.name}.json"
    )
    _make_stale(stale_claude_registry)

    junk = workspace.generated_dir / "acceptance-old" / "report.json"
    junk.parent.mkdir(parents=True)
    junk.write_text("{}\n", encoding="utf-8")
    orphan_profile = (
        workspace.memory_dir / "clients" / "codex" / "profiles" / "orphan.config.toml"
    )
    orphan_profile.write_text("developer_instructions = 'old'\n", encoding="utf-8")
    handoff = workspace.state_dir / "handoffs" / "in-use.json"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("{}\n", encoding="utf-8")
    preferences = workspace.state_dir / "clients" / "codex" / "preferences.json"
    write_json(preferences, {"version": 1, "tasks": {}})

    result = _run_cleaner(workspace)

    assert result.returncode == 0, result.stderr
    assert not junk.exists()
    assert not orphan_profile.exists()
    assert not stale_profile.exists()
    assert not os.path.lexists(stale_registration)
    assert not stale_codex_registry.exists()
    assert not stale_prompt.exists()
    assert not stale_claude_registry.exists()
    assert active_profile.exists()
    assert active_registration.is_symlink()
    assert active_codex_generated.is_dir()
    assert active_codex_registry.exists()
    assert active_prompt.exists()
    assert active_claude_registry.exists()
    assert handoff.exists()
    assert json.loads(preferences.read_text(encoding="utf-8")) == {
        "tasks": {},
        "version": 1,
    }

    active_codex.release_profile(active_name)
    active_claude.release_prompt(active_prompt)


def test_cleanup_dry_run_is_non_mutating_and_cold_state_self_bootstraps(workspace):
    record = write_record(workspace, "reference", "docs", "api", "API BODY")
    write_task(workspace)
    proposal = prepare_relabel(
        workspace,
        scope="pending",
        sharing="shared",
        task_id="build",
    )
    complete_proposal(proposal, always="not-always", task="eager")
    apply_relabel(workspace, proposal)
    render_task_view(workspace, "build")
    generated = workspace.generated_dir / "updates" / "abandoned" / "submission.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("{}\n", encoding="utf-8")
    profile = workspace.memory_dir / "clients" / "codex" / "profiles" / "orphan.config.toml"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("developer_instructions = 'old'\n", encoding="utf-8")
    handoff = workspace.state_dir / "handoffs" / "abandoned.json"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("{}\n", encoding="utf-8")
    preferences = workspace.state_dir / "clients" / "claude" / "preferences.json"
    write_json(preferences, {"version": 1, "tasks": {}})

    before = {
        str(path.relative_to(workspace.memory_dir)): path.read_bytes()
        for path in workspace.memory_dir.rglob("*")
        if path.is_file()
    }
    dry_run = _run_cleaner(workspace, "--dry-run")
    assert dry_run.returncode == 0, dry_run.stderr
    assert generated.exists() and profile.exists() and handoff.exists()
    after = {
        str(path.relative_to(workspace.memory_dir)): path.read_bytes()
        for path in workspace.memory_dir.rglob("*")
        if path.is_file()
    }
    assert after == before

    result = _run_cleaner(workspace)
    assert result.returncode == 0, result.stderr
    assert not workspace.generated_dir.exists()
    assert not profile.parent.exists()
    assert not handoff.parent.exists()
    assert preferences.exists()
    assert record.exists()

    report = validate_workspace(workspace, require_complete=False)
    assert report["records"] == 1 and report["tasks"] == 1
    rebuilt = render_task_view(workspace, "build")
    assert "API BODY" in rebuilt["content"]


def test_cleanup_refuses_broad_deletion_for_untrusted_registry(workspace):
    junk = workspace.generated_dir / "views" / "keep-on-refusal.md"
    junk.parent.mkdir(parents=True)
    junk.write_text("keep\n", encoding="utf-8")
    registry = (
        workspace.state_dir / "clients" / "codex" / "registrations" / "broken.json"
    )
    registry.parent.mkdir(parents=True)
    registry.write_text("not json\n", encoding="utf-8")

    result = _run_cleaner(workspace)

    assert result.returncode == 2
    assert "cleanup refused" in result.stderr
    assert junk.exists()
    assert registry.exists()
