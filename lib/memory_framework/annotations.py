from __future__ import annotations

import copy
import json
from pathlib import Path

from .errors import ValidationError
from .models import CoverageIssue, MemoryRecord, TaskType
from .parsing import load_records, load_task_types
from .workspace import Workspace, atomic_write_json, json_digest


MATRIX_VERSION = 2
ALWAYS_LABELS = {"always", "not-always"}
TASK_LABELS = {"eager", "lazy"}
TASK_CLASSIFICATION_POLICY = """Use a recall-biased balance. Consider applicability frequency,
miss cost, whether ordinary task input reliably reveals the trigger, and the attention cost of
preloading the body. Choose eager when the record is frequently applicable, or when a miss is
costly and the trigger is not reliably self-identifying. Choose lazy for narrow shape-, API-,
file-, experiment-, or failure-specific knowledge when the trigger is recognizable or delayed
reading is inexpensive. Non-obvious content alone does not justify eager. On a genuine tie,
choose eager; do not target a numerical split. Use context metrics as evidence of attention cost,
not as a quota. Classify from the label-free corpus only; never consult previous labels or generated
annotation artifacts."""


def empty_matrix() -> dict[str, object]:
    return {"version": MATRIX_VERSION, "always": {}, "tasks": {}}


def _expect_exact_keys(value: dict[str, object], keys: set[str], where: str) -> None:
    if set(value) != keys:
        raise ValidationError(
            f"{where}: fields mismatch; missing={sorted(keys - set(value))}, "
            f"extra={sorted(set(value) - keys)}"
        )


def validate_matrix_schema(matrix: object, where: str) -> dict[str, object]:
    if not isinstance(matrix, dict):
        raise ValidationError(f"{where}: matrix must be a JSON object")
    _expect_exact_keys(matrix, {"version", "always", "tasks"}, where)
    if matrix["version"] != MATRIX_VERSION:
        raise ValidationError(f"{where}: unsupported matrix version {matrix['version']!r}")
    always = matrix["always"]
    tasks = matrix["tasks"]
    if not isinstance(always, dict) or not isinstance(tasks, dict):
        raise ValidationError(f"{where}: always and tasks must be objects")
    for record_id, cell in always.items():
        if not isinstance(record_id, str) or not isinstance(cell, dict):
            raise ValidationError(f"{where}: invalid always cell")
        _expect_exact_keys(cell, {"label", "record_hash"}, f"{where}: always/{record_id}")
        if cell["label"] not in ALWAYS_LABELS or not _is_hash(cell["record_hash"]):
            raise ValidationError(f"{where}: invalid always cell for {record_id}")
    for task_id, row in tasks.items():
        if not isinstance(task_id, str) or not isinstance(row, dict):
            raise ValidationError(f"{where}: invalid task row")
        _expect_exact_keys(row, {"task_hash", "records"}, f"{where}: tasks/{task_id}")
        if not _is_hash(row["task_hash"]) or not isinstance(row["records"], dict):
            raise ValidationError(f"{where}: invalid task row for {task_id}")
        for record_id, cell in row["records"].items():
            if not isinstance(record_id, str) or not isinstance(cell, dict):
                raise ValidationError(f"{where}: invalid task cell")
            _expect_exact_keys(
                cell,
                {"label", "record_hash", "always_label"},
                f"{where}: tasks/{task_id}/{record_id}",
            )
            if (
                cell["label"] not in TASK_LABELS
                or not _is_hash(cell["record_hash"])
                or cell["always_label"] != "not-always"
            ):
                raise ValidationError(f"{where}: invalid task cell for {task_id}/{record_id}")
    return matrix


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def read_matrix(path: Path, *, missing_ok: bool = False) -> dict[str, object]:
    if not path.exists():
        if missing_ok:
            return empty_matrix()
        raise ValidationError(f"annotation matrix is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read annotation matrix {path}: {exc}") from exc
    return validate_matrix_schema(value, str(path))


def write_matrix(path: Path, matrix: dict[str, object]) -> None:
    validate_matrix_schema(matrix, str(path))
    atomic_write_json(path, matrix, mode=0o600)


def matrix_digest(matrix: dict[str, object]) -> str:
    return json_digest(matrix)


def task_annotation_hash(task: TaskType) -> str:
    return json_digest(
        {
            "task_description": task.content_hash,
            "classification_policy": TASK_CLASSIFICATION_POLICY,
        }
    )


def cell_fingerprint(cell: object) -> str:
    return json_digest(cell)


def fresh_always_label(
    matrix: dict[str, object], record: MemoryRecord
) -> str | None:
    cell = matrix["always"].get(record.id)  # type: ignore[union-attr]
    if not isinstance(cell, dict) or cell.get("record_hash") != record.content_hash:
        return None
    label = cell.get("label")
    return label if isinstance(label, str) else None


def fresh_task_label(
    matrix: dict[str, object], task: TaskType, record: MemoryRecord
) -> str | None:
    row = matrix["tasks"].get(task.id)  # type: ignore[union-attr]
    if not isinstance(row, dict) or row.get("task_hash") != task_annotation_hash(task):
        return None
    cells = row.get("records")
    if not isinstance(cells, dict):
        return None
    cell = cells.get(record.id)
    if (
        not isinstance(cell, dict)
        or cell.get("record_hash") != record.content_hash
        or cell.get("always_label") != "not-always"
    ):
        return None
    label = cell.get("label")
    return label if isinstance(label, str) else None


def coverage_issues(
    records: dict[str, MemoryRecord],
    tasks: dict[str, TaskType],
    matrices: dict[str, dict[str, object]],
) -> list[CoverageIssue]:
    issues: list[CoverageIssue] = []
    for sharing in ("shared", "local"):
        scoped_records = {
            record_id: record
            for record_id, record in records.items()
            if record.sharing == sharing
        }
        matrix = matrices.get(sharing, empty_matrix())
        always = matrix["always"]
        rows = matrix["tasks"]
        assert isinstance(always, dict) and isinstance(rows, dict)

        for record_id in sorted(set(always) - set(scoped_records)):
            issues.append(
                CoverageIssue(sharing, "orphan-always", record_id=record_id, detail="record no longer exists")
            )
        for task_id in sorted(set(rows) - set(tasks)):
            issues.append(
                CoverageIssue(sharing, "orphan-task", task_id=task_id, detail="task no longer exists")
            )

        for record in scoped_records.values():
            cell = always.get(record.id)
            if cell is None:
                issues.append(
                    CoverageIssue(sharing, "pending-always", record_id=record.id, detail="decision is missing")
                )
                continue
            assert isinstance(cell, dict)
            if cell["record_hash"] != record.content_hash:
                issues.append(
                    CoverageIssue(sharing, "stale-always", record_id=record.id, detail="record hash changed")
                )
                continue
            if cell["label"] == "always":
                continue
            for task in tasks.values():
                row = rows.get(task.id)
                if row is None:
                    issues.append(
                        CoverageIssue(
                            sharing,
                            "pending-task",
                            record_id=record.id,
                            task_id=task.id,
                            detail="task row is missing",
                        )
                    )
                    continue
                assert isinstance(row, dict)
                if row["task_hash"] != task_annotation_hash(task):
                    issues.append(
                        CoverageIssue(
                            sharing,
                            "stale-task",
                            record_id=record.id,
                            task_id=task.id,
                            detail="task description hash changed",
                        )
                    )
                    continue
                cells = row["records"]
                assert isinstance(cells, dict)
                task_cell = cells.get(record.id)
                if task_cell is None:
                    issues.append(
                        CoverageIssue(
                            sharing,
                            "pending-task",
                            record_id=record.id,
                            task_id=task.id,
                            detail="decision is missing",
                        )
                    )
                elif (
                    task_cell["record_hash"] != record.content_hash
                    or task_cell["always_label"] != "not-always"
                ):
                    issues.append(
                        CoverageIssue(
                            sharing,
                            "stale-task",
                            record_id=record.id,
                            task_id=task.id,
                            detail="record or always state changed",
                        )
                    )

        for task_id, row in rows.items():
            if task_id not in tasks:
                continue
            cells = row["records"]
            assert isinstance(cells, dict)
            for record_id in sorted(set(cells) - set(scoped_records)):
                issues.append(
                    CoverageIssue(
                        sharing,
                        "orphan-task-cell",
                        record_id=record_id,
                        task_id=task_id,
                        detail="record no longer exists in this sharing scope",
                    )
                )
    return issues


def load_annotation_snapshot(
    workspace: Workspace,
) -> tuple[dict[str, MemoryRecord], dict[str, TaskType], dict[str, dict[str, object]]]:
    with workspace.annotation_lock():
        records = load_records(workspace.records_dir)
        tasks = load_task_types(workspace.tasks_dir)
        matrices = {
            "shared": copy.deepcopy(read_matrix(workspace.matrix_path("shared"))),
            "local": copy.deepcopy(read_matrix(workspace.matrix_path("local"), missing_ok=True)),
        }
    return records, tasks, matrices


def validate_annotations(workspace: Workspace) -> list[CoverageIssue]:
    records, tasks, matrices = load_annotation_snapshot(workspace)
    return coverage_issues(records, tasks, matrices)
