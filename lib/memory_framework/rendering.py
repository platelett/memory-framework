from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from .annotations import (
    coverage_issues,
    fresh_always_label,
    fresh_task_label,
    load_annotation_snapshot,
)
from .errors import ValidationError
from .models import CoverageIssue, MemoryRecord, TaskType
from .parsing import load_task_types
from .tasking import resolve_task
from .workspace import (
    Workspace,
    atomic_write_json,
    atomic_write_text,
    json_digest,
    sha256_bytes,
)


AUTHORITY = ("policy", "reference", "playbook")
AUTHORITY_TITLES = {
    "policy": "Policy constraints",
    "reference": "Authoritative reference",
    "playbook": "Playbook guidance",
}
AUTHORITY_BEHAVIOR = {
    "policy": "These requirements are mandatory whenever their activation condition matches.",
    "reference": "Use these records as authoritative facts whenever their activation condition matches.",
    "playbook": (
        "Use these practices as defaults when their activation condition matches; Policy, "
        "Reference, and direct task evidence take precedence."
    ),
}


def _read_instruction(workspace: Workspace, name: str) -> str:
    path = workspace.instructions_dir / name
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValidationError(f"cannot read instruction file {path}: {exc}") from exc
    if not value:
        raise ValidationError(f"instruction file is empty: {path}")
    return value.replace("{{FRAMEWORK_ROOT}}", str(workspace.framework_root)).replace(
        "{{WORKSPACE_ROOT}}", str(workspace.root)
    )


def _activation(record: MemoryRecord) -> tuple[str, str]:
    condition = record.load_when.strip()
    if condition.startswith("Read when working in this context: "):
        condition = condition.removeprefix("Read when working in this context: ")
    elif condition.startswith("Read when "):
        condition = condition.removeprefix("Read when ")
    elif condition.startswith("Read before "):
        condition = condition.removeprefix("Read before ")
        return ("Mandatory before" if record.layer == "policy" else "Read before", condition)
    return ("Mandatory when" if record.layer == "policy" else "Applies when", condition)


def _record_key(record: MemoryRecord) -> str:
    # Shared wire format with scripts/load.py: hash the immutable ID, not content.
    return sha256_bytes(record.id.encode("utf-8"))[:12]


def _check_record_keys(records: list[MemoryRecord]) -> None:
    seen: dict[str, str] = {}
    for record in records:
        key = _record_key(record)
        if key in seen and seen[key] != record.id:
            raise ValidationError(f"short record ID collision {key}: {seen[key]} / {record.id}")
        seen[key] = record.id


def _record_block(record: MemoryRecord) -> str:
    activation_label, activation_condition = _activation(record)
    return (
        f"### [{_record_key(record)}] {record.title}\n\n"
        f"{activation_label}: {activation_condition}\n\n"
        f"{record.body.strip()}"
    )


def _render_loaded(records: list[MemoryRecord]) -> tuple[str, dict[str, int]]:
    _check_record_keys(records)
    sections: list[str] = []
    bytes_by_layer: dict[str, int] = {}
    for layer in AUTHORITY:
        selected = sorted((record for record in records if record.layer == layer), key=lambda item: item.id)
        if not selected:
            continue
        body = (
            f"## {AUTHORITY_TITLES[layer]}\n\n"
            f"{AUTHORITY_BEHAVIOR[layer]}\n\n"
        ) + "\n\n".join(
            _record_block(record) for record in selected
        )
        sections.append(body)
        bytes_by_layer[layer] = len(body.encode("utf-8"))
    if not sections:
        return "(no loaded memory records)", bytes_by_layer
    return "\n\n".join(sections), bytes_by_layer


def _render_lazy(records: list[MemoryRecord]) -> str:
    if not records:
        return "(none)"
    # Conditions are retrieval keys, not unique record identifiers. Keep the
    # original text and list a shared condition once, even across authority layers.
    conditions = dict.fromkeys(record.load_when for record in sorted(records, key=lambda item: item.id))
    return "\n".join(f"- {condition}" for condition in conditions)


def _lazy_loader_hint(script: Path | str, *, include_local: bool = True, workspace_root: Path | str | None = None) -> str:
    command = f"python3 {shlex.quote(str(script))}"
    if workspace_root is not None:
        command += f" --workspace {shlex.quote(str(workspace_root))}"
    if not include_local:
        command += " --exclude-local"
    return (
        "Each bullet below is an exact `load_when`. When it applies, load and read all matches:\n\n"
        f"`{command} 'EXACT LOAD_WHEN TEXT'`\n\n"
        "Copy the complete condition without the bullet; preserve case, punctuation and spacing. "
        "For shell-sensitive text, use `--stdin` with a quoted heredoc. "
        "Identical conditions load every matching record; no match is an error.\n\n"
    )


def _record_loader_hint(script: Path | str, *, include_local: bool = True, workspace_root: Path | str | None = None) -> str:
    command = f"python3 {shlex.quote(str(script))}"
    if workspace_root is not None:
        command += f" --workspace {shlex.quote(str(workspace_root))}"
    if not include_local:
        command += " --exclude-local"
    return (
        "Bracketed record IDs are stable short keys. Retrieve full source and metadata on demand: "
        f"`{command} --id SHORT_ID`.\n\n"
    )


def _matching_issues(
    issues: list[CoverageIssue], sharing: str, task: TaskType
) -> list[CoverageIssue]:
    selected: list[CoverageIssue] = []
    for issue in issues:
        if issue.sharing != sharing:
            continue
        if issue.kind in {"pending-always", "stale-always", "orphan-always"}:
            selected.append(issue)
        elif not task.built_in and issue.task_id == task.id:
            selected.append(issue)
    return selected


def render_task_view(
    workspace: Workspace,
    task_id: str,
    *,
    include_local: bool = True,
    output_path: Path | None = None,
    write_manifest: bool = True,
) -> dict[str, object]:
    workspace.assert_mutable("render a task view")
    records, standard_tasks, matrices = load_annotation_snapshot(workspace)
    task = resolve_task(task_id, standard_tasks)
    issues = coverage_issues(records, standard_tasks, matrices)
    relevant_issues: list[CoverageIssue] = []
    for sharing in ("shared", "local"):
        if sharing == "local" and not include_local:
            continue
        relevant_issues.extend(_matching_issues(issues, sharing, task))
    if relevant_issues:
        details = "\n".join(f"- {issue.describe()}" for issue in relevant_issues)
        raise ValidationError(
            "task rendering requires complete fresh annotations; run the launch preflight:\n"
            + details
        )

    loaded: list[MemoryRecord] = []
    lazy: list[MemoryRecord] = []
    always_loaded: list[MemoryRecord] = []
    eager_loaded: list[MemoryRecord] = []
    for record in sorted(records.values(), key=lambda item: item.id):
        if record.sharing == "local" and not include_local:
            continue
        matrix = matrices[record.sharing]
        always_label = fresh_always_label(matrix, record)
        if always_label is None:
            raise ValidationError(f"unexpected unresolved always annotation: {record.id}")
        if always_label == "always":
            loaded.append(record)
            always_loaded.append(record)
            continue
        if task.id == "all-eager":
            loaded.append(record)
            eager_loaded.append(record)
        elif task.id == "all-lazy":
            lazy.append(record)
        else:
            task_label = fresh_task_label(matrix, task, record)
            if task_label == "eager":
                loaded.append(record)
                eager_loaded.append(record)
            elif task_label == "lazy":
                lazy.append(record)
            else:
                raise ValidationError(f"unexpected unresolved task annotation: {task.id}/{record.id}")

    _check_record_keys(loaded + lazy)
    loaded_markdown, _ = _render_loaded(loaded)
    lazy_markdown = _render_lazy(lazy)
    parts = [
        _read_instruction(workspace, "common.md"),
        _read_instruction(workspace, "normal.md"),
        "# Active task type\n\n" + task.description_markdown().strip(),
        "# Materialized memory\n\n"
        + (_record_loader_hint(workspace.framework_root / "scripts" / "load.py", include_local=include_local, workspace_root=workspace.root) if loaded else "")
        + loaded_markdown,
        "# Lazy memory catalog\n\n"
        + (_lazy_loader_hint(workspace.framework_root / "scripts" / "load.py", include_local=include_local, workspace_root=workspace.root) if lazy else "")
        + lazy_markdown,
    ]
    content = "\n\n---\n\n".join(parts).rstrip() + "\n"

    target = output_path or workspace.generated_dir / "views" / task.id / "normal.md"
    metrics_path = target.with_name("metrics.json")
    previous: dict[str, object] = {}
    if metrics_path.exists():
        try:
            previous = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    metrics = {
        "task": task.id,
        "always_bytes": sum(len(_record_block(record).encode("utf-8")) for record in always_loaded),
        "eager_bytes": sum(len(_record_block(record).encode("utf-8")) for record in eager_loaded),
        "lazy_bytes": len(lazy_markdown.encode("utf-8")),
        "total_bytes": len(content.encode("utf-8")),
        "estimated_tokens": (len(content.encode("utf-8")) + 3) // 4,
        "pending_count": 0,
    }
    metrics["change_from_previous"] = {
        key: metrics[key] - int(previous.get(key, metrics[key]))
        for key in ("always_bytes", "eager_bytes", "lazy_bytes", "total_bytes")
    }
    workspace.ensure_private_runtime_directory(target.parent)
    atomic_write_text(target, content, mode=0o600)
    atomic_write_json(metrics_path, metrics, mode=0o600)
    manifest_path: Path | None = None
    if write_manifest:
        manifest_path = target.with_name("manifest.json")
        manifest = _task_view_manifest(
            workspace,
            task,
            records,
            matrices,
            include_local=include_local,
            content=content,
            target=target,
        )
        atomic_write_json(manifest_path, manifest, mode=0o600)
    return {
        "task": task,
        "content": content,
        "path": target,
        "metrics": metrics,
        "issues": relevant_issues,
        "loaded": loaded,
        "lazy": lazy,
        "maintenance": [],
        "manifest": manifest_path,
    }


def _task_view_source_state(
    workspace: Workspace,
    task: TaskType,
    records: dict[str, MemoryRecord],
    matrices: dict[str, dict[str, object]],
    *,
    include_local: bool,
) -> dict[str, object]:
    selected = {
        record_id: record.content_hash
        for record_id, record in sorted(records.items())
        if include_local or record.sharing == "shared"
    }
    selected_matrices = {
        sharing: json_digest(matrix)
        for sharing, matrix in sorted(matrices.items())
        if include_local or sharing == "shared"
    }
    return {
        "framework": workspace.framework_manifest,
        "protocol": workspace.bank_manifest,
        "task": {"id": task.id, "hash": task.content_hash},
        "instructions": {
            name: sha256_bytes((workspace.instructions_dir / name).read_bytes())
            for name in ("common.md", "normal.md")
        },
        "include_local": include_local,
        "records": selected,
        "matrices": selected_matrices,
    }


def _task_view_manifest(
    workspace: Workspace,
    task: TaskType,
    records: dict[str, MemoryRecord],
    matrices: dict[str, dict[str, object]],
    *,
    include_local: bool,
    content: str,
    target: Path,
) -> dict[str, object]:
    state = _task_view_source_state(
        workspace, task, records, matrices, include_local=include_local
    )
    return {
        "version": 1,
        "kind": "task-view",
        "output": target.name,
        "content_sha256": sha256_bytes(content.encode("utf-8")),
        "source_digest": json_digest(state),
        "source": state,
    }


def validate_generated_task_views(workspace: Workspace) -> list[str]:
    root = workspace.generated_dir / "views"
    if not root.exists():
        return []
    issues: list[str] = []
    records, standard_tasks, matrices = load_annotation_snapshot(workspace)
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        markdown_files = sorted(directory.glob("*.md"))
        manifest_path = directory / "manifest.json"
        if directory.name == "auto":
            continue
        if markdown_files and not manifest_path.is_file():
            issues.append(f"stale-generated-view: {directory}: manifest is missing")
            continue
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            task = resolve_task(directory.name, standard_tasks)
            include_local = bool(manifest["source"]["include_local"])
            state = _task_view_source_state(
                workspace, task, records, matrices, include_local=include_local
            )
            output = directory / str(manifest["output"])
            content_hash = sha256_bytes(output.read_bytes())
        except (OSError, KeyError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            issues.append(f"stale-generated-view: {directory}: invalid manifest: {exc}")
            continue
        if manifest.get("version") != 1 or manifest.get("kind") != "task-view":
            issues.append(f"stale-generated-view: {directory}: unsupported manifest")
        elif manifest.get("source_digest") != json_digest(state):
            issues.append(f"stale-generated-view: {directory}: source state changed")
        elif manifest.get("content_sha256") != content_hash:
            issues.append(f"stale-generated-view: {directory}: output content changed")
    return issues


def _skill_metadata(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"cannot read skill metadata {path}: {exc}") from exc
    if not text.startswith("---\n"):
        raise ValidationError(f"skill has no frontmatter: {path}")
    closing = text.find("\n---\n", 4)
    if closing < 0:
        raise ValidationError(f"skill frontmatter is not closed: {path}")
    fields: dict[str, str] = {}
    for line in text[4:closing].splitlines():
        match = re.fullmatch(r"(name|description):\s*[\"']?(.*?)[\"']?", line)
        if match:
            fields[match.group(1)] = match.group(2)
    if set(fields) != {"name", "description"}:
        raise ValidationError(f"skill metadata must contain name and description: {path}")
    return fields["name"], fields["description"]


def render_auto_view(
    workspace: Workspace,
    *,
    preferences: dict[str, object] | None = None,
    extra_instructions: str = "",
    output_path: Path | None = None,
    write_manifest: bool = True,
) -> dict[str, object]:
    workspace.assert_mutable("render an Auto view")
    standard = load_task_types(workspace.tasks_dir)
    lines = [
        _read_instruction(workspace, "common.md"),
        "",
        "---",
        "",
        _read_instruction(workspace, "auto.md"),
        "",
        "# Available standard task types",
    ]
    for task in [standard[key] for key in sorted(standard)]:
        lines.extend(
            [
                "",
                f"Task-description SHA-256: `{task.content_hash}`",
                "",
                task.description_markdown().strip(),
            ]
        )
    if not standard:
        lines.extend(["", "No standard task types are defined. Complete requests in Auto."])
    lines.extend(["", "# Maintenance skills"])
    for skill_path in sorted(workspace.skills_dir.glob("*/SKILL.md")):
        name, description = _skill_metadata(skill_path)
        relative = skill_path.as_posix()
        lines.extend(
            [
                "",
                f"- `{name}` — {description}",
                f"  - Body: `{relative}` (read only when the request triggers it)",
            ]
        )
    if preferences:
        lines.extend(
            [
                "",
                "# Confirmed local routing preferences",
                "",
                "```json",
                json.dumps(preferences, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ]
        )
    if extra_instructions.strip():
        lines.extend(["", "# Routing handoff", "", extra_instructions.strip()])
    content = "\n".join(lines).rstrip() + "\n"
    target = output_path or workspace.generated_dir / "views" / "auto" / "auto.md"
    workspace.ensure_private_runtime_directory(target.parent)
    atomic_write_text(target, content, mode=0o600)
    if write_manifest:
        atomic_write_json(
            target.with_name("manifest.json"),
            {
                "version": 1,
                "kind": "auto-view",
                "output": target.name,
                "content_sha256": sha256_bytes(content.encode("utf-8")),
                "task_manifest": {
                    task_id: task.content_hash for task_id, task in sorted(standard.items())
                },
            },
            mode=0o600,
        )
    return {"content": content, "path": target, "tasks": standard}
