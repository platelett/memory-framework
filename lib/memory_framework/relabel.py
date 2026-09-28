from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from .annotations import (
    ALWAYS_LABELS,
    TASK_LABELS,
    TASK_CLASSIFICATION_POLICY,
    cell_fingerprint,
    empty_matrix,
    fresh_always_label,
    fresh_task_label,
    read_matrix,
    task_annotation_hash,
    validate_matrix_schema,
    write_matrix,
)
from .errors import PublicationError, ValidationError
from .models import MemoryRecord, TaskType
from .parsing import load_records, load_task_types
from .workspace import Workspace, atomic_write_json, atomic_write_text, json_digest


PROPOSAL_VERSION = 2
SCOPES = {"pending", "always", "task", "all"}
SHARING_SCOPES = {"shared", "local"}
CLASSIFICATION_GUIDANCE = (
    "Classify only loading behavior. Do not edit record prose, task descriptions, or authority.",
    "Every record owns a required, task-independent `load_when`. Relabeling may use it as",
    "evidence but never creates, deletes, or rewrites it; lazy only exposes it in the catalog.",
    "Use `always` only when every task must know a record before acting. Otherwise use",
    "`not-always`. The task policy below is part of annotation freshness:",
    *TASK_CLASSIFICATION_POLICY.splitlines(),
    "The corpus intentionally contains no previous loading labels.",
)


def _manifest(records: dict[str, MemoryRecord]) -> dict[str, str]:
    return {record_id: record.content_hash for record_id, record in sorted(records.items())}


def _task_manifest(tasks: dict[str, TaskType]) -> dict[str, str]:
    return {task_id: task_annotation_hash(task) for task_id, task in sorted(tasks.items())}


def _always_state_digest(
    matrix: dict[str, object], records: dict[str, MemoryRecord]
) -> str:
    state = {
        record_id: fresh_always_label(matrix, record)
        for record_id, record in sorted(records.items())
    }
    return json_digest(state)


def _fingerprint(value: object) -> str:
    return cell_fingerprint(value)


def _live_cell(matrix: dict[str, object], kind: str, record_id: str, task_id: str | None = None) -> object:
    if kind == "always":
        always = matrix["always"]
        assert isinstance(always, dict)
        return always.get(record_id)
    if task_id is None:
        raise AssertionError("task cell requires task id")
    rows = matrix["tasks"]
    assert isinstance(rows, dict)
    row = rows.get(task_id)
    if not isinstance(row, dict):
        return None
    cells = row["records"]
    assert isinstance(cells, dict)
    return cells.get(record_id)


def _selected_task(task_id: str | None, tasks: dict[str, TaskType], scope: str) -> TaskType | None:
    if scope in {"task"} and task_id is None:
        raise ValidationError(f"--task is required for relabel scope {scope}")
    if task_id is None:
        return None
    if task_id in {"all-lazy", "all-eager"}:
        raise ValidationError(f"built-in task {task_id!r} has no annotation row")
    try:
        return tasks[task_id]
    except KeyError as exc:
        raise ValidationError(f"unknown standard task: {task_id}") from exc


def prepare_relabel(
    workspace: Workspace,
    *,
    scope: str,
    sharing: str,
    task_id: str | None = None,
) -> Path:
    workspace.assert_mutable("prepare a relabel proposal")
    if scope not in SCOPES:
        raise ValidationError(f"scope must be one of {', '.join(sorted(SCOPES))}")
    if sharing not in SHARING_SCOPES:
        raise ValidationError("sharing must be shared or local")
    if scope in {"always", "all"} and task_id is not None:
        raise ValidationError(f"--task is not valid with relabel scope {scope}")

    with workspace.annotation_lock(mutable=True):
        all_records = load_records(workspace.records_dir)
        tasks = load_task_types(workspace.tasks_dir)
        matrix = read_matrix(workspace.matrix_path(sharing), missing_ok=sharing == "local")
        records = {
            record_id: record
            for record_id, record in all_records.items()
            if record.sharing == sharing
        }
        task = _selected_task(task_id, tasks, scope)
        proposal = _build_proposal(scope, sharing, records, tasks, matrix, task)

    proposal_id = str(proposal["proposal_id"])
    directory = workspace.generated_dir / "relabel" / proposal_id
    workspace.ensure_private_runtime_directory(directory.parent)
    directory.mkdir(mode=0o700, exist_ok=False)
    corpus = _build_corpus(proposal, records, tasks)
    atomic_write_text(directory / "corpus.md", corpus, mode=0o600)
    atomic_write_json(directory / "proposal.json", proposal, mode=0o600)
    return directory / "proposal.json"


def _build_proposal(
    scope: str,
    sharing: str,
    records: dict[str, MemoryRecord],
    tasks: dict[str, TaskType],
    matrix: dict[str, object],
    task: TaskType | None,
) -> dict[str, object]:
    decisions_always: dict[str, str | None] = {}
    decisions_tasks: dict[str, dict[str, str | None]] = {}
    base_always: dict[str, str] = {}
    base_tasks: dict[str, dict[str, str]] = {}
    requirement_modes: dict[str, dict[str, str]] = {}

    if scope == "pending":
        pending_always = {
            record_id
            for record_id, record in records.items()
            if fresh_always_label(matrix, record) is None
        }
        decisions_always = {record_id: None for record_id in sorted(pending_always)}
        for record_id in decisions_always:
            base_always[record_id] = _fingerprint(
                _live_cell(matrix, "always", record_id)
            )
        if task is not None:
            target_modes: dict[str, str] = {}
            for record_id, record in records.items():
                always_label = fresh_always_label(matrix, record)
                if record_id in pending_always:
                    target_modes[record_id] = "if-not-always"
                elif always_label == "not-always" and fresh_task_label(matrix, task, record) is None:
                    target_modes[record_id] = "required"
            if target_modes:
                decisions_tasks[task.id] = {
                    record_id: None for record_id in sorted(target_modes)
                }
                requirement_modes[task.id] = dict(sorted(target_modes.items()))
                base_tasks[task.id] = {
                    record_id: _fingerprint(_live_cell(matrix, "task", record_id, task.id))
                    for record_id in sorted(target_modes)
                }
        always_cells = matrix["always"]
        task_rows = matrix["tasks"]
        assert isinstance(always_cells, dict) and isinstance(task_rows, dict)
        has_orphans = any(record_id not in records for record_id in always_cells)
        for task_id, row in task_rows.items():
            assert isinstance(row, dict)
            row_records = row["records"]
            assert isinstance(row_records, dict)
            has_orphans = has_orphans or task_id not in tasks or any(
                record_id not in records for record_id in row_records
            )
        if not decisions_always and not decisions_tasks and not has_orphans:
            raise ValidationError("no pending or stale decisions match this relabel request")
        selected_ids = set(decisions_always)
        for task_decisions in decisions_tasks.values():
            selected_ids.update(task_decisions)
        record_manifest = _manifest(
            {record_id: records[record_id] for record_id in selected_ids}
        )
        task_manifest = {task.id: task_annotation_hash(task)} if task and decisions_tasks else {}
    elif scope == "always":
        decisions_always = {record_id: None for record_id in sorted(records)}
        record_manifest = _manifest(records)
        task_manifest = {}
    elif scope == "task":
        assert task is not None
        unresolved = [
            record.id for record in records.values() if fresh_always_label(matrix, record) is None
        ]
        if unresolved:
            raise ValidationError(
                "full task relabel requires fresh always decisions first: " + ", ".join(sorted(unresolved))
            )
        target_ids = [
            record.id
            for record in records.values()
            if fresh_always_label(matrix, record) == "not-always"
        ]
        decisions_tasks[task.id] = {record_id: None for record_id in sorted(target_ids)}
        requirement_modes[task.id] = {record_id: "required" for record_id in sorted(target_ids)}
        record_manifest = _manifest(records)
        task_manifest = {task.id: task_annotation_hash(task)}
    else:
        decisions_always = {record_id: None for record_id in sorted(records)}
        for task_id in sorted(tasks):
            decisions_tasks[task_id] = {record_id: None for record_id in sorted(records)}
            requirement_modes[task_id] = {
                record_id: "if-not-always" for record_id in sorted(records)
            }
        record_manifest = _manifest(records)
        task_manifest = _task_manifest(tasks)

    return {
        "version": PROPOSAL_VERSION,
        "proposal_id": uuid.uuid4().hex,
        "scope": scope,
        "sharing": sharing,
        "task": task.id if task else None,
        "record_manifest": record_manifest,
        "task_manifest": task_manifest,
        "always_state_digest": _always_state_digest(matrix, records),
        "base_fingerprints": {"always": base_always, "tasks": base_tasks},
        "requirements": {"tasks": requirement_modes},
        "decisions": {"always": decisions_always, "tasks": decisions_tasks},
    }


def _build_corpus(
    proposal: dict[str, object],
    records: dict[str, MemoryRecord],
    tasks: dict[str, TaskType],
) -> str:
    scope = str(proposal["scope"])
    record_manifest = proposal["record_manifest"]
    task_manifest = proposal["task_manifest"]
    decisions = proposal["decisions"]
    assert isinstance(record_manifest, dict)
    assert isinstance(task_manifest, dict)
    assert isinstance(decisions, dict)
    task_decisions = decisions["tasks"]
    assert isinstance(task_decisions, dict)

    lines = [
        "# Memory relabel corpus",
        "",
        f"Scope: `{scope}`",
        f"Sharing: `{proposal['sharing']}`",
        "",
        *CLASSIFICATION_GUIDANCE,
        "",
        "Edit only the null values under `decisions` in the sibling `proposal.json`.",
        "Leave conditional task decisions null only when that record is decided `always`.",
        "Self-review every decision. The transaction owner validates and atomically applies it.",
    ]

    if task_manifest:
        lines.extend(["", "# Task descriptions"])
        for task_id in sorted(task_manifest):
            task = tasks[task_id]
            lines.extend(
                [
                    "",
                    f"<!-- TASK {task.id} hash={task.content_hash} -->",
                    task.description_markdown().rstrip(),
                    f"<!-- END TASK {task.id} -->",
                ]
            )

    if scope == "task":
        decisions_always = proposal["decisions"]
        assert isinstance(decisions_always, dict)
        selected_task = str(proposal["task"])
        selected_ids = set(task_decisions[selected_task])  # type: ignore[arg-type]
        always_ids = sorted(set(record_manifest) - selected_ids)
        lines.extend(["", "# Current always manifest", ""])
        if always_ids:
            for record_id in always_ids:
                lines.append(f"- `{record_id}` — {records[record_id].title}")
        else:
            lines.append("(none)")

    lines.extend(["", "# Records"])
    for record_id in sorted(record_manifest):
        record = records[record_id]
        lines.extend(
            [
                "",
                f"<!-- RECORD {record.id} path={record.relative_path} hash={record.content_hash} -->",
                f"## {record.title}",
                "",
                f"Layer: `{record.layer}`  ",
                f"Load condition: {record.load_when}",
                "",
                record.body,
                f"<!-- END RECORD {record.id} -->",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _load_proposal(path: Path) -> dict[str, object]:
    try:
        proposal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"cannot read proposal {path}: {exc}") from exc
    if not isinstance(proposal, dict):
        raise PublicationError("proposal must be a JSON object")
    required = {
        "version",
        "proposal_id",
        "scope",
        "sharing",
        "task",
        "record_manifest",
        "task_manifest",
        "always_state_digest",
        "base_fingerprints",
        "requirements",
        "decisions",
    }
    if set(proposal) != required or proposal.get("version") != PROPOSAL_VERSION:
        raise PublicationError("proposal schema or version is invalid")
    if proposal.get("scope") not in SCOPES or proposal.get("sharing") not in SHARING_SCOPES:
        raise PublicationError("proposal scope or sharing value is invalid")
    return proposal


def apply_relabel(workspace: Workspace, proposal_path: Path | str) -> dict[str, object]:
    workspace.assert_mutable("apply a relabel proposal")
    path = Path(proposal_path).expanduser().resolve()
    proposal = _load_proposal(path)
    sharing = str(proposal["sharing"])
    with workspace.annotation_lock(mutable=True):
        all_records = load_records(workspace.records_dir)
        tasks = load_task_types(workspace.tasks_dir)
        records = {
            record_id: record
            for record_id, record in all_records.items()
            if record.sharing == sharing
        }
        matrix_path = workspace.matrix_path(sharing)
        matrix = read_matrix(matrix_path, missing_ok=sharing == "local")
        _validate_proposal_against_live(proposal, records, tasks, matrix)
        updated = _apply_proposal(proposal, records, tasks, matrix)
        validate_matrix_schema(updated, str(matrix_path))
        write_matrix(matrix_path, updated)
    return {
        "proposal_id": proposal["proposal_id"],
        "sharing": sharing,
        "scope": proposal["scope"],
        "matrix": str(matrix_path),
    }


# Compatibility for callers created before relabel publication was renamed to an atomic apply.
publish_relabel = apply_relabel


def _validate_proposal_against_live(
    proposal: dict[str, object],
    records: dict[str, MemoryRecord],
    tasks: dict[str, TaskType],
    matrix: dict[str, object],
) -> None:
    scope = str(proposal["scope"])
    record_manifest = proposal["record_manifest"]
    task_manifest = proposal["task_manifest"]
    decisions = proposal["decisions"]
    requirements = proposal["requirements"]
    if not isinstance(record_manifest, dict) or not isinstance(task_manifest, dict):
        raise PublicationError("proposal manifests must be objects")
    if not isinstance(decisions, dict) or set(decisions) != {"always", "tasks"}:
        raise PublicationError("proposal decisions schema is invalid")
    if not isinstance(requirements, dict) or set(requirements) != {"tasks"}:
        raise PublicationError("proposal requirements schema is invalid")

    current_hashes = _manifest(records)
    if scope == "pending":
        for record_id, pinned_hash in record_manifest.items():
            if current_hashes.get(record_id) != pinned_hash:
                raise PublicationError(f"record changed or disappeared: {record_id}")
    elif current_hashes != record_manifest:
        raise PublicationError("full relabel record manifest is stale")

    for task_id, pinned_hash in task_manifest.items():
        if task_id not in tasks or task_annotation_hash(tasks[task_id]) != pinned_hash:
            raise PublicationError(
                f"task description or classification policy changed or disappeared: {task_id}"
            )
    if scope == "all" and _task_manifest(tasks) != task_manifest:
        raise PublicationError("full relabel task manifest is stale")
    if scope == "task" and _always_state_digest(matrix, records) != proposal["always_state_digest"]:
        raise PublicationError("always decisions changed during full task relabel")

    _validate_decision_shape_and_completeness(proposal, records, tasks, matrix)

    if scope == "pending":
        fingerprints = proposal["base_fingerprints"]
        if not isinstance(fingerprints, dict) or set(fingerprints) != {"always", "tasks"}:
            raise PublicationError("base fingerprints schema is invalid")
        always_fingerprints = fingerprints["always"]
        task_fingerprints = fingerprints["tasks"]
        if not isinstance(always_fingerprints, dict) or not isinstance(task_fingerprints, dict):
            raise PublicationError("base fingerprints must be objects")
        for record_id, expected in always_fingerprints.items():
            current = _fingerprint(_live_cell(matrix, "always", record_id))
            if current != expected:
                raise PublicationError(f"same always cell changed concurrently: {record_id}")
        for task_id, cells in task_fingerprints.items():
            if not isinstance(cells, dict):
                raise PublicationError("task fingerprints must be objects")
            for record_id, expected in cells.items():
                current = _fingerprint(_live_cell(matrix, "task", record_id, task_id))
                if current != expected:
                    raise PublicationError(f"same task cell changed concurrently: {task_id}/{record_id}")


def _validate_decision_shape_and_completeness(
    proposal: dict[str, object],
    records: dict[str, MemoryRecord],
    tasks: dict[str, TaskType],
    matrix: dict[str, object],
) -> None:
    scope = str(proposal["scope"])
    decisions = proposal["decisions"]
    requirements = proposal["requirements"]
    assert isinstance(decisions, dict) and isinstance(requirements, dict)
    always_decisions = decisions["always"]
    task_decisions = decisions["tasks"]
    task_requirements = requirements["tasks"]
    if not isinstance(always_decisions, dict) or not isinstance(task_decisions, dict):
        raise PublicationError("decision maps must be objects")
    if not isinstance(task_requirements, dict) or set(task_requirements) != set(task_decisions):
        raise PublicationError("task decision requirements do not match task decisions")

    record_ids = set(records)
    if scope == "always":
        if task_decisions or proposal["task"] is not None or proposal["task_manifest"]:
            raise PublicationError("always-only proposal cannot contain task decisions")
    elif scope == "task":
        selected_task = proposal["task"]
        if not isinstance(selected_task, str) or set(task_decisions) != {selected_task}:
            raise PublicationError("task relabel must contain exactly its selected task row")
        expected = {
            record_id
            for record_id, record in records.items()
            if fresh_always_label(matrix, record) == "not-always"
        }
        if set(task_decisions[selected_task]) != expected:
            raise PublicationError("full task decision set is incomplete")
        if set(task_requirements[selected_task]) != expected or any(
            mode != "required" for mode in task_requirements[selected_task].values()
        ):
            raise PublicationError("full task requirements are incomplete")
    elif scope == "all":
        if proposal["task"] is not None:
            raise PublicationError("combined relabel cannot select one task")
        for task_id, cells in task_decisions.items():
            if set(cells) != record_ids:
                raise PublicationError(f"combined relabel task row is incomplete: {task_id}")
            if set(task_requirements[task_id]) != record_ids or any(
                mode != "if-not-always" for mode in task_requirements[task_id].values()
            ):
                raise PublicationError(f"combined relabel task requirements are incomplete: {task_id}")

    for record_id, label in always_decisions.items():
        if record_id not in records or label not in ALWAYS_LABELS:
            raise PublicationError(f"incomplete or invalid always decision: {record_id}")
    if scope in {"always", "all"} and set(always_decisions) != set(records):
        raise PublicationError("full always decision set is incomplete")
    if scope in {"task", "all"} and scope == "task" and always_decisions:
        raise PublicationError("task-only proposal cannot change always decisions")

    for task_id, cells in task_decisions.items():
        if task_id not in tasks or not isinstance(cells, dict):
            raise PublicationError(f"unknown or malformed task decision row: {task_id}")
        modes = task_requirements[task_id]
        if not isinstance(modes, dict) or set(modes) != set(cells):
            raise PublicationError(f"task requirements mismatch: {task_id}")
        for record_id, label in cells.items():
            if record_id not in records or modes[record_id] not in {"required", "if-not-always"}:
                raise PublicationError(f"invalid task target: {task_id}/{record_id}")
            final_always = always_decisions.get(record_id)
            required = modes[record_id] == "required" or final_always == "not-always"
            if required and label not in TASK_LABELS:
                raise PublicationError(f"incomplete task decision: {task_id}/{record_id}")
            if not required and label is not None:
                raise PublicationError(
                    f"task decision must stay null when record is always: {task_id}/{record_id}"
                )
    if scope == "all" and set(task_decisions) != set(tasks):
        raise PublicationError("combined relabel is missing task rows")

    if scope == "pending":
        manifest = proposal["record_manifest"]
        fingerprints = proposal["base_fingerprints"]
        task_manifest = proposal["task_manifest"]
        assert isinstance(manifest, dict) and isinstance(fingerprints, dict) and isinstance(task_manifest, dict)
        targeted = set(always_decisions)
        for cells in task_decisions.values():
            targeted.update(cells)
        if targeted != set(manifest):
            raise PublicationError("incremental proposal target manifest is incomplete")
        if set(fingerprints.get("always", {})) != set(always_decisions):
            raise PublicationError("incremental always fingerprints do not match targets")
        fingerprint_tasks = fingerprints.get("tasks", {})
        if not isinstance(fingerprint_tasks, dict) or set(fingerprint_tasks) != set(task_decisions):
            raise PublicationError("incremental task fingerprints do not match targets")
        for task_id, cells in task_decisions.items():
            if set(fingerprint_tasks[task_id]) != set(cells):
                raise PublicationError(f"incremental task fingerprints are incomplete: {task_id}")
        if set(task_manifest) != set(task_decisions):
            raise PublicationError("incremental task manifest does not match task targets")
        expected_task = next(iter(task_decisions), None)
        if proposal["task"] != expected_task:
            raise PublicationError("incremental selected task does not match task targets")


def _apply_proposal(
    proposal: dict[str, object],
    records: dict[str, MemoryRecord],
    tasks: dict[str, TaskType],
    matrix: dict[str, object],
) -> dict[str, object]:
    scope = str(proposal["scope"])
    decisions = proposal["decisions"]
    assert isinstance(decisions, dict)
    always_decisions = decisions["always"]
    task_decisions = decisions["tasks"]
    assert isinstance(always_decisions, dict) and isinstance(task_decisions, dict)

    if scope == "all":
        updated = empty_matrix()
    else:
        updated = copy.deepcopy(matrix)
        updated["always"] = {
            record_id: cell
            for record_id, cell in updated["always"].items()  # type: ignore[union-attr]
            if record_id in records
        }
        updated["tasks"] = {
            task_id: row
            for task_id, row in updated["tasks"].items()  # type: ignore[union-attr]
            if task_id in tasks
        }
        for row in updated["tasks"].values():  # type: ignore[union-attr]
            assert isinstance(row, dict)
            row_records = row["records"]
            assert isinstance(row_records, dict)
            row["records"] = {
                record_id: cell
                for record_id, cell in row_records.items()
                if record_id in records
            }

    always = updated["always"]
    rows = updated["tasks"]
    assert isinstance(always, dict) and isinstance(rows, dict)

    for record_id, label in always_decisions.items():
        record = records[record_id]
        always[record_id] = {"label": label, "record_hash": record.content_hash}
        if label == "always":
            for row in rows.values():
                assert isinstance(row, dict)
                row["records"].pop(record_id, None)  # type: ignore[union-attr]

    if scope == "always":
        current_non_always = {
            record_id for record_id, cell in always.items() if cell["label"] == "not-always"
        }
        for task_id, row in rows.items():
            if task_id not in tasks:
                continue
            row["records"] = {  # type: ignore[index]
                record_id: cell
                for record_id, cell in row["records"].items()  # type: ignore[union-attr]
                if record_id in current_non_always
                and cell["record_hash"] == records[record_id].content_hash
            }

    if scope == "task":
        task_id = str(proposal["task"])
        rows[task_id] = {"task_hash": task_annotation_hash(tasks[task_id]), "records": {}}

    for task_id, cells in task_decisions.items():
        if task_id not in rows or scope == "all":
            rows[task_id] = {"task_hash": task_annotation_hash(tasks[task_id]), "records": {}}
        row = rows[task_id]
        assert isinstance(row, dict)
        row["task_hash"] = task_annotation_hash(tasks[task_id])
        row_cells = row["records"]
        assert isinstance(row_cells, dict)
        for record_id, label in cells.items():
            always_cell = always[record_id]
            if always_cell["label"] == "always":
                row_cells.pop(record_id, None)
                continue
            row_cells[record_id] = {
                "label": label,
                "record_hash": records[record_id].content_hash,
                "always_label": "not-always",
            }

    return updated
