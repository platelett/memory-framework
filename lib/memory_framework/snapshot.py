from __future__ import annotations

import shutil
from pathlib import Path

from memory_protocol import DATA_PROTOCOL

from .annotations import coverage_issues, fresh_always_label, fresh_task_label, load_annotation_snapshot
from .errors import ValidationError
from .models import MemoryRecord
from .tasking import resolve_task
from .workspace import Workspace, atomic_write_json


def write_snapshot_reader(workspace: Workspace, target: Path) -> None:
    """A portable reader is a separate, minimal framework next to snapshot data."""
    reader = target / "framework"
    for relative in ("scripts/load.py", "lib/memory_protocol.py"):
        destination = reader / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(workspace.framework_root / relative, destination)
    declaration = dict(workspace.framework_manifest)
    declaration["kind"] = "memory-reader"
    atomic_write_json(reader / "framework.json", declaration)
    atomic_write_json(target / ".memory" / "protocol.json", {"kind": "snapshot", "protocol": DATA_PROTOCOL})


def build_snapshot_plan(
    workspace: Workspace,
    task_id: str,
    *,
    include_local: bool,
) -> dict[str, object]:
    records, standard_tasks, matrices = load_annotation_snapshot(workspace)
    task = resolve_task(task_id, standard_tasks)
    all_issues = coverage_issues(records, standard_tasks, matrices)
    relevant = []
    for issue in all_issues:
        if issue.sharing == "local" and not include_local:
            continue
        if issue.kind in {"pending-always", "stale-always", "orphan-always", "orphan-task"}:
            relevant.append(issue)
        elif not task.built_in and issue.task_id == task.id:
            relevant.append(issue)
    if relevant:
        details = "\n".join(f"- {issue.describe()}" for issue in relevant)
        raise ValidationError(f"snapshot requires complete fresh annotations:\n{details}")

    loaded: list[MemoryRecord] = []
    lazy: list[MemoryRecord] = []
    load_states: dict[str, str] = {}
    for record in sorted(records.values(), key=lambda item: item.id):
        if record.sharing == "local" and not include_local:
            continue
        matrix = matrices[record.sharing]
        always_label = fresh_always_label(matrix, record)
        if always_label == "always":
            loaded.append(record)
            load_states[record.id] = "always"
        elif task.id == "all-eager":
            loaded.append(record)
            load_states[record.id] = "eager"
        elif task.id == "all-lazy":
            lazy.append(record)
            load_states[record.id] = "lazy"
        else:
            label = fresh_task_label(matrix, task, record)
            if label == "eager":
                loaded.append(record)
            elif label == "lazy":
                lazy.append(record)
            else:
                raise ValidationError(f"unexpected unresolved annotation: {task.id}/{record.id}")
            load_states[record.id] = str(label)
    return {
        "task": task,
        "loaded": loaded,
        "lazy": lazy,
        "load_states": load_states,
        "include_local": include_local,
    }
