from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from conftest import REPOSITORY, read_json, write_json, write_record, write_task
from memory_framework.annotations import fresh_task_label, task_annotation_hash
from memory_framework.errors import PublicationError
from memory_framework.migrations import migrate_record_ids
from memory_framework.parsing import load_records, load_task_types
from memory_framework.preflight import prepare_annotation_preflight


def fixture(workspace, tmp_path):
    path = write_record(workspace, "reference", "docs", "old", "ONE")
    write_task(workspace, "build")
    write_task(workspace, "check")
    old = load_records(workspace.records_dir)["reference.docs.old"]
    tasks = load_task_types(workspace.tasks_dir)
    matrix = {
        "version": 2,
        "always": {old.id: {"label": "not-always", "record_hash": old.content_hash}},
        "tasks": {name: {"task_hash": task_annotation_hash(tasks[name]), "records": {
            old.id: {"label": label, "record_hash": old.content_hash, "always_label": "not-always"}
        }} for name, label in (("build", "eager"), ("check", "lazy"))},
    }
    write_json(workspace.matrix_path("shared"), matrix)
    replacement = path.with_name("new.md")
    replacement.write_text(path.read_text().replace('id = "reference.docs.old"', 'id = "reference.docs.new"'))
    path.unlink()
    new = load_records(workspace.records_dir)["reference.docs.new"]
    plan = tmp_path / "migration.json"
    write_json(plan, {"version": 1, "mapping": {old.id: new.id}, "before": {
        old.id: {"sharing": "shared", "record_hash": old.content_hash}
    }, "condition_aliases": {}, "approved_name_change": True})
    return old, new, matrix, plan


def test_id_migration_preserves_decisions_and_launch_needs_no_classifier(workspace, tmp_path):
    old, new, before, plan = fixture(workspace, tmp_path)
    result = migrate_record_ids(workspace, plan)
    assert result["renamed_records"] == 1
    after = read_json(workspace.matrix_path("shared"))
    assert after["always"][new.id]["label"] == "not-always"
    assert old.id not in after["always"]
    for task in ("build", "check"):
        assert after["tasks"][task]["records"][new.id] == before["tasks"][task]["records"][old.id]
    preflight = prepare_annotation_preflight(workspace, "build")
    assert not preflight.requires_classification and not preflight.issues
    bound = read_json(workspace.matrix_path("shared"))
    assert bound["tasks"]["build"]["records"][new.id]["label"] == "eager"
    assert bound["tasks"]["build"]["records"][new.id]["record_hash"] == new.content_hash
    assert bound["tasks"]["check"] == after["tasks"]["check"]
    assert not prepare_annotation_preflight(workspace, "check").requires_classification
    final = read_json(workspace.matrix_path("shared"))
    assert final["tasks"]["check"]["records"][new.id]["label"] == "lazy"


def test_legacy_short_id_loads_canonical_replacement(workspace, tmp_path):
    old, new, _, plan = fixture(workspace, tmp_path)
    migrate_record_ids(workspace, plan)
    spec = importlib.util.spec_from_file_location("migration_loader", REPOSITORY / "scripts/load.py")
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    output = loader.load_matches(workspace.root, short_id=hashlib.sha256(old.id.encode()).hexdigest()[:12])
    assert new.id in output and 'id = "reference.docs.old"' not in output


def test_unapproved_or_changed_versions_do_not_inherit(workspace, tmp_path):
    _, new, _, plan = fixture(workspace, tmp_path)
    migrate_record_ids(workspace, plan)
    path = workspace.records_dir / "reference/docs/new.md"
    path.write_text(path.read_text().replace("ONE", "DIFFERENT FACT"))
    before = read_json(workspace.matrix_path("shared"))
    preflight = prepare_annotation_preflight(workspace, "build")
    assert preflight.requires_classification
    assert read_json(workspace.matrix_path("shared")) == before


def test_migration_rejection_does_not_mutate_annotations(workspace, tmp_path):
    _, _, before, plan = fixture(workspace, tmp_path)
    value = read_json(plan)
    value["before"]["reference.docs.old"]["record_hash"] = "0" * 64
    write_json(plan, value)
    with pytest.raises(PublicationError, match="prior always decision changed"):
        migrate_record_ids(workspace, plan)
    assert read_json(workspace.matrix_path("shared")) == before
    assert not (workspace.memory_dir / "record-aliases.json").exists()


def test_migration_requires_explicit_naming_approval(workspace, tmp_path):
    _, _, before, plan = fixture(workspace, tmp_path)
    value = read_json(plan)
    value["approved_name_change"] = False
    write_json(plan, value)
    with pytest.raises(PublicationError, match="explicit approval"):
        migrate_record_ids(workspace, plan)
    assert read_json(workspace.matrix_path("shared")) == before


def test_approved_same_id_name_change_binds_without_reclassification(workspace, tmp_path):
    old, new, _, plan = fixture(workspace, tmp_path)
    migrate_record_ids(workspace, plan)
    path = workspace.records_dir / "reference/docs/new.md"
    path.write_text(path.read_text().replace("ONE", "Renamed environment interface"))
    second = tmp_path / "second.json"
    write_json(second, {"version": 1, "mapping": {}, "before": {
        new.id: {"sharing": "shared", "record_hash": new.content_hash}
    }, "condition_aliases": {}, "approved_name_change": True})
    migrate_record_ids(workspace, second)
    preflight = prepare_annotation_preflight(workspace, "build")
    assert not preflight.requires_classification and not preflight.issues
    matrix = read_json(workspace.matrix_path("shared"))
    assert matrix["tasks"]["build"]["records"][new.id]["label"] == "eager"


def test_old_condition_alias_uses_new_record(workspace, tmp_path):
    old, new, _, plan = fixture(workspace, tmp_path)
    value = read_json(plan)
    cue = "Old renamed environment condition"
    value["condition_aliases"] = {hashlib.sha256(cue.encode()).hexdigest(): [new.id]}
    write_json(plan, value)
    migrate_record_ids(workspace, plan)
    spec = importlib.util.spec_from_file_location("condition_loader", REPOSITORY / "scripts/load.py")
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    output = loader.load_matches(workspace.root, cue)
    assert new.id in output
