from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .annotations import coverage_issues, load_annotation_snapshot
from .errors import ValidationError
from .models import CoverageIssue, TaskType
from .relabel import CLASSIFICATION_GUIDANCE, apply_relabel, prepare_relabel
from .tasking import resolve_task
from .workspace import Workspace, atomic_write_json


GLOBAL_ISSUE_KINDS = {"pending-always", "stale-always", "orphan-always"}


@dataclass(frozen=True, slots=True)
class AnnotationPreflight:
    task: TaskType
    issues: tuple[CoverageIssue, ...]
    proposals: tuple[Path, ...]
    corpus: str
    output_schema: dict[str, object]
    requires_classification: bool


def relevant_task_issues(
    workspace: Workspace,
    task_id: str,
    *,
    include_local: bool = True,
) -> tuple[TaskType, tuple[CoverageIssue, ...]]:
    records, standard_tasks, matrices = load_annotation_snapshot(workspace)
    task = resolve_task(task_id, standard_tasks)
    selected: list[CoverageIssue] = []
    for issue in coverage_issues(records, standard_tasks, matrices):
        if issue.sharing == "local" and not include_local:
            continue
        if issue.kind in GLOBAL_ISSUE_KINDS:
            selected.append(issue)
        elif not task.built_in and issue.task_id == task.id:
            selected.append(issue)
    return task, tuple(selected)


def _proposal_requires_classification(path: Path) -> bool:
    value = json.loads(path.read_text(encoding="utf-8"))
    decisions = value["decisions"]
    values = list(decisions["always"].values())
    for cells in decisions["tasks"].values():
        values.extend(cells.values())
    return any(item is None for item in values)


def _enum_schema(values: list[object]) -> dict[str, object]:
    return {"enum": values}


def _object_schema(properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _proposal_output_schema(value: dict[str, object]) -> dict[str, object]:
    decisions = value["decisions"]
    requirements = value["requirements"]
    assert isinstance(decisions, dict) and isinstance(requirements, dict)
    always = decisions["always"]
    tasks = decisions["tasks"]
    task_requirements = requirements["tasks"]
    assert isinstance(always, dict) and isinstance(tasks, dict)
    assert isinstance(task_requirements, dict)
    task_properties: dict[str, object] = {}
    for task_id, cells in tasks.items():
        assert isinstance(cells, dict)
        modes = task_requirements[task_id]
        assert isinstance(modes, dict)
        cell_properties = {}
        for record_id in cells:
            labels: list[object] = ["eager", "lazy"]
            if modes[record_id] == "if-not-always":
                labels.append(None)
            cell_properties[record_id] = _enum_schema(labels)
        task_properties[task_id] = _object_schema(cell_properties)
    return _object_schema(
        {
            "always": _object_schema(
                {record_id: _enum_schema(["always", "not-always"]) for record_id in always}
            ),
            "tasks": _object_schema(task_properties),
        }
    )


def _decision_targets(value: dict[str, object]) -> dict[str, object]:
    decisions = value["decisions"]
    requirements = value["requirements"]
    assert isinstance(decisions, dict) and isinstance(requirements, dict)
    task_requirements = requirements["tasks"]
    assert isinstance(task_requirements, dict)
    return {
        "always": sorted(decisions["always"]),
        "tasks": {
            task_id: {
                "required": sorted(
                    record_id for record_id, mode in modes.items() if mode == "required"
                ),
                "if_not_always": sorted(
                    record_id
                    for record_id, mode in modes.items()
                    if mode == "if-not-always"
                ),
            }
            for task_id, modes in task_requirements.items()
        },
    }


def prepare_annotation_preflight(
    workspace: Workspace,
    task_id: str,
    *,
    include_local: bool = True,
) -> AnnotationPreflight:
    task, issues = relevant_task_issues(
        workspace,
        task_id,
        include_local=include_local,
    )
    if not issues:
        return AnnotationPreflight(task, (), (), "", _object_schema({}), False)

    proposals: list[Path] = []
    sections = [
        "# Automated task annotation preflight",
        "",
        "Classify every decision target below in this one run and return only the requested JSON.",
        "Use only this corpus as evidence. Do not inspect files, old labels, generated artifacts,",
        "records outside this corpus, or live annotation matrices. Do not use tools or shell commands.",
        "The parent launcher validates the structured result and atomically applies it.",
        "",
        *CLASSIFICATION_GUIDANCE,
        "",
        "# Selected task",
        "",
        task.description_markdown().strip(),
    ]
    selected_task = None if task.built_in else task.id
    schema_properties: dict[str, object] = {}
    for sharing in ("shared", "local"):
        if sharing == "local" and not include_local:
            continue
        scoped = [issue for issue in issues if issue.sharing == sharing]
        if not scoped:
            continue
        proposal = prepare_relabel(
            workspace,
            scope="pending",
            sharing=sharing,
            task_id=selected_task,
        )
        proposals.append(proposal)
        proposal_value = json.loads(proposal.read_text(encoding="utf-8"))
        corpus = proposal.with_name("corpus.md").read_text(encoding="utf-8").strip()
        _, record_section = corpus.split("# Records", 1)
        needs_classification = _proposal_requires_classification(proposal)
        if needs_classification:
            schema_properties[sharing] = _proposal_output_schema(proposal_value)
        sections.extend(
            [
                "",
                "---",
                "",
                f"# {sharing.title()} decisions",
                "",
                "Decision targets (not previous labels):",
                "",
                "```json",
                json.dumps(_decision_targets(proposal_value), ensure_ascii=False, indent=2),
                "```",
                "",
                "# Records",
                record_section.lstrip(),
            ]
        )
    return AnnotationPreflight(
        task,
        issues,
        tuple(proposals),
        "\n".join(sections).rstrip() + "\n",
        _object_schema(schema_properties),
        bool(schema_properties),
    )


def record_classifier_result(
    preflight: AnnotationPreflight,
    result: dict[str, object],
) -> None:
    expected = {
        json.loads(path.read_text(encoding="utf-8"))["sharing"]
        for path in preflight.proposals
        if _proposal_requires_classification(path)
    }
    if set(result) != expected:
        raise ValidationError(
            f"classifier result sharing mismatch: expected={sorted(expected)} actual={sorted(result)}"
        )
    for path in preflight.proposals:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not _proposal_requires_classification(path):
            continue
        sharing = value["sharing"]
        supplied = result[sharing]
        if not isinstance(supplied, dict) or set(supplied) != {"always", "tasks"}:
            raise ValidationError(f"classifier result is malformed for {sharing}")
        decisions = value["decisions"]
        assert isinstance(decisions, dict)
        if set(supplied["always"]) != set(decisions["always"]):
            raise ValidationError(f"classifier always targets differ for {sharing}")
        if set(supplied["tasks"]) != set(decisions["tasks"]):
            raise ValidationError(f"classifier task targets differ for {sharing}")
        for record_id, label in supplied["always"].items():
            if decisions["always"][record_id] is not None:
                raise ValidationError(
                    f"classifier attempted to replace a non-null always cell: {record_id}"
                )
            decisions["always"][record_id] = label
        for task_id, cells in supplied["tasks"].items():
            if set(cells) != set(decisions["tasks"][task_id]):
                raise ValidationError(
                    f"classifier task targets differ for {sharing}/{task_id}"
                )
            for record_id, label in cells.items():
                if decisions["tasks"][task_id][record_id] is not None:
                    raise ValidationError(
                        f"classifier attempted to replace a non-null task cell: {task_id}/{record_id}"
                    )
                decisions["tasks"][task_id][record_id] = label
        atomic_write_json(path, value, mode=0o600)


def apply_annotation_preflight(
    workspace: Workspace,
    preflight: AnnotationPreflight,
) -> list[dict[str, object]]:
    return [apply_relabel(workspace, path) for path in preflight.proposals]
