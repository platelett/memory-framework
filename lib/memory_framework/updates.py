from __future__ import annotations

import copy
import json
from pathlib import Path

from .annotations import (
    ALWAYS_LABELS,
    TASK_LABELS,
    fresh_always_label,
    read_matrix,
    task_annotation_hash,
    validate_matrix_schema,
    write_matrix,
)
from .errors import PublicationError
from .parsing import load_records, load_task_types
from .tasking import resolve_task
from .validation import validate_workspace
from .workspace import Workspace


UPDATE_VERSION = 1


def _load_submission(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"cannot read incremental submission {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicationError("incremental submission must be a JSON object")
    expected = {"version", "task", "records"}
    if set(value) != expected or value.get("version") != UPDATE_VERSION:
        raise PublicationError("incremental submission schema or version is invalid")
    if value["task"] is not None and not isinstance(value["task"], str):
        raise PublicationError("incremental submission task must be a string or null")
    decisions = value["records"]
    if not isinstance(decisions, dict) or not decisions:
        raise PublicationError("incremental submission records must be a non-empty object")
    for record_id, decision in decisions.items():
        if not isinstance(record_id, str) or not isinstance(decision, dict):
            raise PublicationError("incremental submission has a malformed record decision")
        if not {"always"} <= set(decision) <= {"always", "task_label"}:
            raise PublicationError(f"incremental decision fields are invalid: {record_id}")
        if decision["always"] not in ALWAYS_LABELS:
            raise PublicationError(f"incremental always decision is invalid: {record_id}")
        if "task_label" in decision and decision["task_label"] not in TASK_LABELS:
            raise PublicationError(f"incremental task decision is invalid: {record_id}")
    return value


def submit_incremental_update(
    workspace: Workspace,
    submission_path: Path | str,
) -> dict[str, object]:
    workspace.assert_mutable("submit an incremental memory update")
    path = Path(submission_path).expanduser().resolve()
    submission = _load_submission(path)
    selected_task_id = submission["task"]
    decisions = submission["records"]
    assert isinstance(decisions, dict)

    with workspace.annotation_lock(mutable=True):
        records = load_records(workspace.records_dir)
        tasks = load_task_types(workspace.tasks_dir)
        selected_task = (
            resolve_task(str(selected_task_id), tasks)
            if selected_task_id is not None
            else None
        )
        matrices = {
            "shared": read_matrix(workspace.matrix_path("shared")),
            "local": read_matrix(workspace.matrix_path("local"), missing_ok=True),
        }
        updated = {sharing: copy.deepcopy(matrix) for sharing, matrix in matrices.items()}
        modified_sharings: set[str] = set()

        for record_id, raw_decision in decisions.items():
            if record_id not in records:
                raise PublicationError(f"incremental target record does not exist: {record_id}")
            record = records[record_id]
            matrix = matrices[record.sharing]
            if fresh_always_label(matrix, record) is not None:
                raise PublicationError(
                    f"incremental target is not pending or stale: {record_id}"
                )
            assert isinstance(raw_decision, dict)
            always_label = str(raw_decision["always"])
            task_label = raw_decision.get("task_label")
            task_annotation_required = (
                always_label == "not-always"
                and selected_task is not None
                and not selected_task.built_in
            )
            if task_annotation_required and task_label not in TASK_LABELS:
                raise PublicationError(
                    f"incremental task decision is required: {selected_task.id}/{record_id}"
                )
            if not task_annotation_required and task_label is not None:
                raise PublicationError(
                    f"incremental task decision is not applicable: {record_id}"
                )
            if task_annotation_required:
                live_rows = matrix["tasks"]
                assert isinstance(live_rows, dict)
                live_row = live_rows.get(selected_task.id)
                if (
                    isinstance(live_row, dict)
                    and live_row["task_hash"] != task_annotation_hash(selected_task)
                ):
                    raise PublicationError(
                        f"active task row is stale; run a task relabel first: {selected_task.id}"
                    )

        for record_id, raw_decision in decisions.items():
            record = records[record_id]
            matrix = updated[record.sharing]
            modified_sharings.add(record.sharing)
            assert isinstance(raw_decision, dict)
            always_label = str(raw_decision["always"])
            always = matrix["always"]
            rows = matrix["tasks"]
            assert isinstance(always, dict) and isinstance(rows, dict)
            always[record_id] = {
                "label": always_label,
                "record_hash": record.content_hash,
            }
            for row in rows.values():
                assert isinstance(row, dict)
                row_records = row["records"]
                assert isinstance(row_records, dict)
                row_records.pop(record_id, None)

            if (
                always_label == "not-always"
                and selected_task is not None
                and not selected_task.built_in
            ):
                row = rows.setdefault(
                    selected_task.id,
                    {"task_hash": task_annotation_hash(selected_task), "records": {}},
                )
                assert isinstance(row, dict)
                row["task_hash"] = task_annotation_hash(selected_task)
                row_records = row["records"]
                assert isinstance(row_records, dict)
                row_records[record_id] = {
                    "label": raw_decision["task_label"],
                    "record_hash": record.content_hash,
                    "always_label": "not-always",
                }

        for sharing in modified_sharings:
            validate_matrix_schema(updated[sharing], str(workspace.matrix_path(sharing)))
        for sharing in sorted(modified_sharings):
            write_matrix(workspace.matrix_path(sharing), updated[sharing])

    report = validate_workspace(workspace, require_complete=False)
    return {
        "submission": str(path),
        "task": selected_task_id,
        "records": sorted(decisions),
        "matrices": [str(workspace.matrix_path(item)) for item in sorted(modified_sharings)],
        "remaining_coverage_issues": report["coverage_issues"],
    }
