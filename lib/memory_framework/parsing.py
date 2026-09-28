from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

from memory_protocol import validate_record_metadata

from .errors import ValidationError
from .models import MemoryRecord, RecordCategory, TaskType


SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
LAYERS = ("policy", "reference", "playbook")
RECORD_FIELDS = {"id", "title", "layer", "category", "load_when"}
CATEGORY_FIELDS = {"id", "title", "layer"}
CATEGORY_LAYERS = ("reference", "playbook")
CATEGORY_SECTIONS = ("Scope", "Boundary", "Notes")
TASK_FIELDS = {"id", "title", "routing_hints"}
TASK_SECTIONS = ("Scope", "Boundary", "Positive examples", "Negative examples")


def _read_markdown(path: Path) -> tuple[dict[str, object], str, str, str]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(f"cannot read UTF-8 Markdown {path}: {exc}") from exc
    lines = text.splitlines()
    if not lines or lines[0] != "+++":
        raise ValidationError(f"{path}: TOML frontmatter must start with +++")
    try:
        closing = lines.index("+++", 1)
    except ValueError as exc:
        raise ValidationError(f"{path}: TOML frontmatter has no closing +++") from exc
    frontmatter_text = "\n".join(lines[1:closing])
    try:
        frontmatter = tomllib.loads(frontmatter_text)
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"{path}: invalid TOML frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise ValidationError(f"{path}: frontmatter must be a table")
    body = "\n".join(lines[closing + 1 :]).strip()
    if not body:
        raise ValidationError(f"{path}: Markdown body is empty")
    return frontmatter, body, hashlib.sha256(raw).hexdigest(), text


def _require_string(metadata: dict[str, object], key: str, path: Path) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValidationError(f"{path}: {key} must be a non-empty trimmed string")
    if "\n" in value or "\r" in value:
        raise ValidationError(f"{path}: {key} must be a single line")
    return value


def parse_record(path: Path, records_dir: Path) -> MemoryRecord:
    metadata, body, content_hash, source = _read_markdown(path)
    try:
        validate_record_metadata(metadata, path, records_dir)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    record_id = metadata["id"]
    title = metadata["title"]
    layer = metadata["layer"]
    category = metadata["category"]
    load_when = metadata["load_when"]
    sharing = "local" if layer == "policy" and category == "local" else "shared"
    return MemoryRecord(
        id=record_id,
        title=title,
        layer=layer,
        category=category,
        load_when=load_when,
        body=body,
        path=path.resolve(),
        relative_path=path.resolve().relative_to(records_dir.parent.parent.resolve()).as_posix(),
        content_hash=content_hash,
        sharing=sharing,
        source=source,
    )


def _parse_category_sections(body: str, path: Path) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    seen_order: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            if heading not in CATEGORY_SECTIONS:
                raise ValidationError(f"{path}: unknown category section {heading!r}")
            if heading in sections:
                raise ValidationError(f"{path}: duplicate category section {heading!r}")
            current = heading
            seen_order.append(heading)
            sections[heading] = []
        elif current is None:
            if line.strip():
                raise ValidationError(f"{path}: content before first category section")
        else:
            sections[current].append(line)
    allowed_orders = [CATEGORY_SECTIONS[:2], CATEGORY_SECTIONS]
    if tuple(seen_order) not in allowed_orders:
        raise ValidationError(
            f"{path}: category sections must be Scope, Boundary, and optional Notes in that order"
        )
    cleaned = {key: "\n".join(value).strip() for key, value in sections.items()}
    for key in ("Scope", "Boundary"):
        if not cleaned.get(key):
            raise ValidationError(f"{path}: category section {key!r} is empty")
    if "Notes" in cleaned and not cleaned["Notes"]:
        raise ValidationError(f"{path}: category section 'Notes' is empty")
    return cleaned


def parse_category(path: Path, categories_dir: Path) -> RecordCategory:
    metadata, body, _content_hash, source = _read_markdown(path)
    if set(metadata) != CATEGORY_FIELDS:
        missing = sorted(CATEGORY_FIELDS - set(metadata))
        extra = sorted(set(metadata) - CATEGORY_FIELDS)
        raise ValidationError(
            f"{path}: category fields mismatch; missing={missing}, extra={extra}"
        )
    category_id = _require_string(metadata, "id", path)
    title = _require_string(metadata, "title", path)
    layer = _require_string(metadata, "layer", path)
    if layer not in CATEGORY_LAYERS:
        raise ValidationError(
            f"{path}: category layer must be one of {', '.join(CATEGORY_LAYERS)}"
        )
    try:
        relative = path.resolve().relative_to(categories_dir.resolve())
    except ValueError as exc:
        raise ValidationError(f"{path}: category is outside {categories_dir}") from exc
    if len(relative.parts) != 2:
        raise ValidationError(f"{path}: expected categories/<layer>/<category>.md")
    path_layer, filename = relative.parts
    if path_layer != layer:
        raise ValidationError(f"{path}: frontmatter layer does not match its directory")
    if path.suffix != ".md" or not SLUG_RE.fullmatch(path.stem):
        raise ValidationError(f"{path}: category filename must be a kebab-case .md slug")
    expected_id = f"{layer}.{path.stem}"
    if category_id != expected_id:
        raise ValidationError(f"{path}: id must be {expected_id!r}")
    sections = _parse_category_sections(body, path)
    return RecordCategory(
        id=category_id,
        title=title,
        layer=layer,
        scope=sections["Scope"],
        boundary=sections["Boundary"],
        notes=sections.get("Notes"),
        path=path.resolve(),
        source=source,
    )


def load_categories(categories_dir: Path) -> dict[str, RecordCategory]:
    categories: dict[str, RecordCategory] = {}
    if not categories_dir.exists():
        return categories
    for path in sorted(categories_dir.rglob("*.md")):
        category = parse_category(path, categories_dir)
        if category.id in categories:
            raise ValidationError(
                f"duplicate category id {category.id!r}: {categories[category.id].path} and {path}"
            )
        categories[category.id] = category
    return categories


def load_records(
    records_dir: Path, categories_dir: Path | None = None
) -> dict[str, MemoryRecord]:
    records: dict[str, MemoryRecord] = {}
    if not records_dir.exists():
        return records
    for path in sorted(records_dir.rglob("*.md")):
        record = parse_record(path, records_dir)
        if record.id in records:
            raise ValidationError(
                f"duplicate record id {record.id!r}: {records[record.id].path} and {path}"
            )
        records[record.id] = record
    category_root = categories_dir or records_dir.parent / "categories"
    categories = load_categories(category_root)
    for record in records.values():
        if record.layer in CATEGORY_LAYERS:
            category_id = f"{record.layer}.{record.category}"
            if category_id not in categories:
                raise ValidationError(
                    f"{record.path}: record references undefined category {category_id!r}"
                )
    return records


def _parse_task_sections(body: str, path: Path) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    seen_order: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            if heading not in TASK_SECTIONS:
                raise ValidationError(f"{path}: unknown task section {heading!r}")
            if heading in sections:
                raise ValidationError(f"{path}: duplicate task section {heading!r}")
            current = heading
            seen_order.append(heading)
            sections[heading] = []
        elif current is None:
            if line.strip():
                raise ValidationError(f"{path}: content before first task section")
        else:
            sections[current].append(line)
    if tuple(seen_order) != TASK_SECTIONS:
        raise ValidationError(
            f"{path}: task sections must appear exactly in this order: {', '.join(TASK_SECTIONS)}"
        )
    cleaned = {key: "\n".join(value).strip() for key, value in sections.items()}
    for key, value in cleaned.items():
        if not value:
            raise ValidationError(f"{path}: task section {key!r} is empty")
    return cleaned


def _parse_examples(value: str, path: Path, section: str) -> tuple[str, ...]:
    examples: list[str] = []
    for line in value.splitlines():
        if not line.strip():
            continue
        if not line.startswith("- ") or not line[2:].strip():
            raise ValidationError(f"{path}: {section} must contain only non-empty bullet items")
        examples.append(line[2:].strip())
    if not examples:
        raise ValidationError(f"{path}: {section} needs at least one example")
    return tuple(examples)


def parse_task_type(path: Path, tasks_dir: Path) -> TaskType:
    metadata, body, content_hash, source = _read_markdown(path)
    unknown = set(metadata) - TASK_FIELDS
    missing = {"id", "title"} - set(metadata)
    if unknown or missing:
        raise ValidationError(f"{path}: task fields mismatch; missing={sorted(missing)}, extra={sorted(unknown)}")
    task_id = _require_string(metadata, "id", path)
    title = _require_string(metadata, "title", path)
    if not SLUG_RE.fullmatch(task_id):
        raise ValidationError(f"{path}: task id must be kebab-case")
    if task_id in {"all-lazy", "all-eager"}:
        raise ValidationError(f"{path}: {task_id} is reserved for a built-in task")
    try:
        relative = path.resolve().relative_to(tasks_dir.resolve())
    except ValueError as exc:
        raise ValidationError(f"{path}: task is outside {tasks_dir}") from exc
    if len(relative.parts) != 1 or path.suffix != ".md" or path.stem != task_id:
        raise ValidationError(f"{path}: expected task-types/<id>.md")
    raw_hints = metadata.get("routing_hints", [])
    if not isinstance(raw_hints, list) or any(
        not isinstance(item, str) or not item.strip() or item != item.strip()
        for item in raw_hints
    ):
        raise ValidationError(f"{path}: routing_hints must be an array of trimmed strings")
    sections = _parse_task_sections(body, path)
    return TaskType(
        id=task_id,
        title=title,
        scope=sections["Scope"],
        boundary=sections["Boundary"],
        positive_examples=_parse_examples(sections["Positive examples"], path, "Positive examples"),
        negative_examples=_parse_examples(sections["Negative examples"], path, "Negative examples"),
        routing_hints=tuple(raw_hints),
        content_hash=content_hash,
        path=path.resolve(),
        source=source,
    )


def load_task_types(tasks_dir: Path) -> dict[str, TaskType]:
    tasks: dict[str, TaskType] = {}
    if not tasks_dir.exists():
        return tasks
    for path in sorted(tasks_dir.rglob("*.md")):
        task = parse_task_type(path, tasks_dir)
        if task.id in tasks:
            raise ValidationError(f"duplicate task id {task.id!r}")
        tasks[task.id] = task
    return tasks
