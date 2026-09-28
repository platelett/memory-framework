from __future__ import annotations

import json
import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import complete_proposal, write_record
from memory_framework.parsing import load_records
from memory_framework.relabel import apply_relabel, prepare_relabel
from memory_framework.rendering import _record_block, _record_key, _render_lazy, render_task_view


LOADER = Path(__file__).resolve().parents[1] / "scripts/load.py"
CONDITION = "Read when checking `x` with $HOME, $(touch sentinel), quotes ' and \" or 中文."


def records_with_shared_condition(workspace):
    for layer, category, slug, marker in (
        ("policy", "shared", "guard", "SHARED_POLICY"),
        ("policy", "local", "machine", "LOCAL_POLICY"),
        ("playbook", "build", "recipe", "RECIPE_BODY"),
    ):
        path = write_record(workspace, layer, category, slug, marker)
        source = path.read_text()
        source = source.replace(
            f'load_when = "Read when work concerns {slug} behavior."',
            f"load_when = {json.dumps(CONDITION, ensure_ascii=False)}",
        )
        path.write_text(source)
    write_record(workspace, "reference", "docs", "other", "UNRELATED_BODY")


def run_loader(workspace, *args, input=None, script=LOADER):
    return subprocess.run(
        [sys.executable, str(script), "--workspace", str(workspace.root), *args],
        input=input, text=True, capture_output=True, cwd=workspace.root,
    )


def test_condition_catalog_deduplicates_and_load_returns_all_exact_matches(workspace):
    records_with_shared_condition(workspace)
    catalog = _render_lazy(list(load_records(workspace.records_dir).values()))
    assert catalog.count(CONDITION) == 1
    assert ".memory/records" not in catalog
    assert "RECIPE_BODY" not in catalog
    result = run_loader(workspace, CONDITION)
    assert result.returncode == 0, result.stderr
    for marker in ("SHARED_POLICY", "LOCAL_POLICY", "RECIPE_BODY"):
        assert result.stdout.count(marker) == 1
    assert "UNRELATED_BODY" not in result.stdout
    assert 'layer = "policy"' in result.stdout
    assert 'layer = "playbook"' in result.stdout
    assert not (workspace.root / "sentinel").exists()
    stdin = run_loader(workspace, "--stdin", input=CONDITION + "\n")
    assert stdin.returncode == 0
    assert stdin.stdout == result.stdout
    excluded = run_loader(workspace, "--exclude-local", CONDITION)
    assert excluded.returncode == 0
    assert "LOCAL_POLICY" not in excluded.stdout
    assert "SHARED_POLICY" in excluded.stdout and "RECIPE_BODY" in excluded.stdout


@pytest.mark.parametrize("query", [CONDITION.lower(), CONDITION[:-1], CONDITION + " "])
def test_loader_does_not_fuzzy_match_or_silently_succeed(workspace, query):
    records_with_shared_condition(workspace)
    result = run_loader(workspace, query)
    assert result.returncode == 2
    assert not result.stdout
    assert "no records match" in result.stderr


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_task_and_portable_snapshot_condition_roundtrip(workspace, tmp_path, client):
    records_with_shared_condition(workspace)
    for sharing in ("shared", "local"):
        proposal = prepare_relabel(workspace, scope="all", sharing=sharing)
        complete_proposal(proposal)
        apply_relabel(workspace, proposal)
    view = render_task_view(workspace, "all-lazy", include_local=False)
    assert "--exclude-local" in view["content"]
    assert view["content"].count(CONDITION) == 1
    if client == "codex":
        from codex.snapshot import export_snapshot
        entry = "AGENTS.md"
    else:
        from claude.snapshot import export_snapshot
        entry = "CLAUDE.md"
    target = tmp_path / client
    export_snapshot(workspace, "all-lazy", target)
    assert (target / entry).read_text().count(CONDITION) == 1
    # The exported loader resolves its own snapshot, regardless of launch cwd
    # or a live workspace environment setting. It needs no annotation state.
    result = subprocess.run(
        [sys.executable, str(target / "framework/scripts/load.py"), CONDITION],
        cwd=tmp_path, env={**os.environ, "MEMORY_WORKSPACE": str(workspace.root)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "SHARED_POLICY" in result.stdout and "RECIPE_BODY" in result.stdout
    assert "LOCAL_POLICY" not in result.stdout
    manifest = json.loads((target / "manifest.json").read_text())
    assert "framework/scripts/load.py" in {item["path"] for item in manifest["loader"]}


def test_short_id_preserves_semantics_and_loads_current_metadata_after_edit(workspace):
    path = write_record(workspace, "playbook", "build", "recipe", "COMPLETE BODY")
    record = next(iter(load_records(workspace.records_dir).values()))
    key = _record_key(record)
    block = _record_block(record)
    assert len(key) == 12 and f"[{key}]" in block
    assert record.title in block and record.body in block
    assert "Applies when: work concerns recipe behavior." in block
    assert record.id not in block and record.relative_path not in block
    assert record.content_hash not in block
    result = run_loader(workspace, "--id", key)
    assert result.returncode == 0, result.stderr
    assert record.id in result.stdout and record.relative_path in result.stdout
    assert record.content_hash in result.stdout and record.source.strip() in result.stdout

    path.write_text(path.read_text().replace("COMPLETE BODY", "REVISED BODY"))
    updated = next(iter(load_records(workspace.records_dir).values()))
    assert _record_key(updated) == key
    result = run_loader(workspace, "--id", key)
    assert result.returncode == 0
    assert "REVISED BODY" in result.stdout and "COMPLETE BODY" not in result.stdout
    assert hashlib.sha256(path.read_bytes()).hexdigest() in result.stdout
    missing = run_loader(workspace, "--id", key[:-1])
    assert missing.returncode == 2 and not missing.stdout
    assert "no record matches" in missing.stderr


def test_short_id_collisions_fail_closed(workspace, monkeypatch):
    records_with_shared_condition(workspace)
    import memory_framework.rendering as rendering
    from memory_framework.errors import ValidationError
    monkeypatch.setattr(rendering, "_record_key", lambda record: "0" * 12)
    with pytest.raises(ValidationError, match="collision"):
        rendering._render_loaded(list(load_records(workspace.records_dir).values()))
    spec = importlib.util.spec_from_file_location("memory_loader_test", LOADER)
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    monkeypatch.setattr(loader, "record_key", lambda record_id: "0" * 12)
    with pytest.raises(ValueError, match="collision"):
        loader.load_matches(workspace.root, short_id="0" * 12)


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_all_eager_snapshot_keeps_sources_for_short_id_lookup(workspace, tmp_path, client):
    path = write_record(workspace, "reference", "docs", "api", "EAGER BODY")
    proposal = prepare_relabel(workspace, scope="all", sharing="shared")
    complete_proposal(proposal)
    apply_relabel(workspace, proposal)
    record = next(iter(load_records(workspace.records_dir).values()))
    if client == "codex":
        from codex.snapshot import export_snapshot
        entry = "AGENTS.md"
    else:
        from claude.snapshot import export_snapshot
        entry = "CLAUDE.md"
    target = tmp_path / client
    export_snapshot(workspace, "all-eager", target)
    prompt = (target / entry).read_text()
    assert f"[{_record_key(record)}]" in prompt
    assert record.id not in prompt and record.content_hash not in prompt
    result = subprocess.run(
        [sys.executable, str(target / "framework/scripts/load.py"), "--id", _record_key(record)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert record.source.strip() in result.stdout
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["records"][0]["short_id"] == _record_key(record)
    assert manifest["records"][0]["path"] == str(path.relative_to(workspace.root))


def test_short_id_lookup_honors_exclude_local(workspace):
    path = write_record(workspace, "policy", "local", "private", "LOCAL BODY")
    record = next(iter(load_records(workspace.records_dir).values()))
    result = run_loader(workspace, "--id", _record_key(record), "--exclude-local")
    assert result.returncode == 2 and not result.stdout
    assert path.read_text()
