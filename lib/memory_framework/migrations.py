"""Publish an explicitly reviewed record-ID migration without discarding labels."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .annotations import read_matrix, validate_matrix_schema, write_matrix, task_annotation_hash
from .errors import PublicationError
from .parsing import load_records, load_task_types
from .validation import validate_workspace
from .workspace import Workspace, atomic_write_json


def migrate_record_ids(workspace: Workspace, plan_path: Path | str) -> dict[str, object]:
    workspace.assert_mutable("migrate record identifiers")
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or set(plan) != {"version", "mapping", "before", "condition_aliases", "approved_name_change"} or plan["version"] != 1:
        raise PublicationError("invalid record migration plan")
    if plan["approved_name_change"] is not True:
        raise PublicationError("ID migrations require explicit approval of a naming-only change")
    mapping, before, conditions = plan["mapping"], plan["before"], plan["condition_aliases"]
    if not all(isinstance(value, dict) for value in (mapping, before, conditions)) or not before:
        raise PublicationError("migration mappings and prior record states must be objects")
    if any(not isinstance(old, str) or not isinstance(new, str) or old == new for old, new in mapping.items()):
        raise PublicationError("invalid record-ID mapping")
    if len(set(mapping.values())) != len(mapping) or set(mapping) & set(mapping.values()):
        raise PublicationError("record-ID mapping has a collision or chain")
    if not set(mapping) <= set(before):
        raise PublicationError("every renamed record requires a pinned prior state")

    with workspace.annotation_lock(mutable=True):
        records = load_records(workspace.records_dir)
        matrices = {sharing: read_matrix(workspace.matrix_path(sharing), missing_ok=sharing == "local") for sharing in ("shared", "local")}
        updated = copy.deepcopy(matrices)
        alias_path = workspace.memory_dir / "record-aliases.json"
        aliases = json.loads(alias_path.read_text()) if alias_path.exists() else {"version": 1, "ids": {}, "conditions": {}}
        if set(aliases) != {"version", "ids", "conditions"} or aliases["version"] != 1 or not isinstance(aliases["ids"], dict) or not isinstance(aliases["conditions"], dict):
            raise PublicationError("invalid record alias store")
        aliases = copy.deepcopy(aliases)
        version_path = workspace.memory_dir / "record-versions.json"
        versions = json.loads(version_path.read_text()) if version_path.exists() else {"version": 1, "records": {}}
        if set(versions) != {"version", "records"} or versions["version"] != 1 or not isinstance(versions["records"], dict):
            raise PublicationError("invalid approved record version transitions")
        versions = copy.deepcopy(versions)
        modified = set()
        for old, prior in before.items():
            if not isinstance(prior, dict) or set(prior) != {"sharing", "record_hash"}:
                raise PublicationError("invalid pinned prior record state")
            sharing = prior["sharing"]
            if sharing not in matrices:
                raise PublicationError("invalid record sharing")
            new = mapping.get(old, old)
            if new not in records or records[new].sharing != sharing:
                raise PublicationError(f"replacement record missing or sharing changed: {new}")
            if old != new and old in records:
                raise PublicationError(f"old record still exists: {old}")
            source = matrices[sharing]["always"].get(old)
            if not isinstance(source, dict) or source["record_hash"] != prior["record_hash"]:
                raise PublicationError(f"prior always decision changed or was not fresh: {old}")
            if old != new and new in matrices[sharing]["always"]:
                raise PublicationError(f"replacement record already has annotations: {new}")
            matrix = updated[sharing]
            matrix["always"].pop(old)
            matrix["always"][new] = {"label": source["label"], "record_hash": records[new].content_hash}
            known_hashes = {source["record_hash"]}
            for row in matrices[sharing]["tasks"].values():
                if old in row["records"]:
                    known_hashes.add(row["records"][old]["record_hash"])
            versions["records"].pop(old, None)
            versions["records"][new] = {"target_hash": records[new].content_hash, "prior_hashes": sorted(known_hashes)}
            for row in matrix["tasks"].values():
                cells = row["records"]
                if old in cells and old != new:
                    if new in cells:
                        raise PublicationError(f"replacement task cell already exists: {new}")
                    # Preserve decisions; selected-task launch binds the approved version automatically.
                    cells[new] = cells.pop(old)
            if old != new:
                key = hashlib.sha256(old.encode()).hexdigest()[:12]
                if key in aliases["ids"] and aliases["ids"][key] not in (old, new):
                    raise PublicationError("short ID alias collision")
                aliases["ids"][key] = new
            modified.add(sharing)
        for key, target in list(aliases["ids"].items()):
            aliases["ids"][key] = mapping.get(target, target)
        for key, targets in list(aliases["conditions"].items()):
            aliases["conditions"][key] = [mapping.get(target, target) for target in targets]
        for key, targets in conditions.items():
            if not isinstance(key, str) or len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key) or not isinstance(targets, list) or not targets or any(target not in records for target in targets):
                raise PublicationError("invalid legacy condition aliases")
            aliases["conditions"][key] = sorted(set(targets))
        for key, target in aliases["ids"].items():
            if target not in records:
                raise PublicationError("alias target is missing")
            for record_id in records:
                if hashlib.sha256(record_id.encode()).hexdigest()[:12] == key and record_id != target:
                    raise PublicationError("alias conflicts with a canonical short ID")
        for sharing in modified:
            validate_matrix_schema(updated[sharing], str(workspace.matrix_path(sharing)))
        for sharing in sorted(modified):
            write_matrix(workspace.matrix_path(sharing), updated[sharing])
        temporary = alias_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(aliases, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        temporary.replace(alias_path)
        atomic_write_json(version_path, versions)
    report = validate_workspace(workspace, require_complete=False)
    return {"renamed_records": len(mapping), "updated_records": len(before), "retained_task_cells": sum(len(row["records"]) for matrix in updated.values() for row in matrix["tasks"].values()), "remaining_coverage_issues": report["coverage_issues"]}


def bind_approved_task_versions(workspace: Workspace, task_id: str, *, include_local: bool = True) -> int:
    """Carry an approved naming-only decision to its selected task, without classification."""
    version_path = workspace.memory_dir / "record-versions.json"
    if not version_path.exists():
        return 0
    with workspace.annotation_lock(mutable=True):
        versions = json.loads(version_path.read_text())
        if set(versions) != {"version", "records"} or versions["version"] != 1:
            raise PublicationError("invalid approved version transitions")
        records = load_records(workspace.records_dir)
        tasks = load_task_types(workspace.tasks_dir)
        if task_id not in tasks:
            return 0
        count = 0
        for sharing in ("shared", "local"):
            if sharing == "local" and not include_local:
                continue
            matrix = read_matrix(workspace.matrix_path(sharing), missing_ok=sharing == "local")
            row = matrix["tasks"].get(task_id)
            if not row or row["task_hash"] != task_annotation_hash(tasks[task_id]):
                continue
            changed = False
            for record_id, cell in row["records"].items():
                record = records.get(record_id)
                transition = versions["records"].get(record_id)
                always = matrix["always"].get(record_id)
                if (record is None or record.sharing != sharing or not transition or
                        always != {"label": "not-always", "record_hash": record.content_hash} or
                        cell["always_label"] != "not-always" or
                        transition["target_hash"] != record.content_hash or
                        cell["record_hash"] == record.content_hash or
                        cell["record_hash"] not in transition["prior_hashes"]):
                    continue
                cell["record_hash"] = record.content_hash
                changed = True
                count += 1
            if changed:
                validate_matrix_schema(matrix, str(workspace.matrix_path(sharing)))
                write_matrix(workspace.matrix_path(sharing), matrix)
    return count
