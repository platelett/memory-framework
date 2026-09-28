"""Machine interface shared by the runtime and portable read-only loader.

This module has no framework-package dependencies and performs no writes.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


DATA_PROTOCOL = {"name": "workspace-memory", "generation": 3}
FRAMEWORK_FORMAT = "memory-framework"
READER_FORMAT = "memory-reader"
RECORD_FIELDS = {"id", "title", "layer", "category", "load_when"}
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def validate_record_metadata(metadata: dict, path: Path, records_dir: Path) -> None:
    """Shared machine schema for the full runtime and minimal snapshot reader."""
    if set(metadata) != RECORD_FIELDS:
        raise ValueError(
            f"{path}: record fields mismatch; missing={sorted(RECORD_FIELDS - set(metadata))}, "
            f"extra={sorted(set(metadata) - RECORD_FIELDS)}"
        )
    for key in RECORD_FIELDS:
        value = metadata[key]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"{path}: {key} must be a non-empty trimmed string")
        if "\n" in value or "\r" in value:
            raise ValueError(f"{path}: {key} must be a single line")
    layer, category = metadata["layer"], metadata["category"]
    if layer not in {"policy", "reference", "playbook"}:
        raise ValueError(f"{path}: layer must be policy, reference or playbook")
    if not SLUG.fullmatch(category):
        raise ValueError(f"{path}: category must be kebab-case")
    if len(metadata["load_when"]) < 12:
        raise ValueError(f"{path}: load_when is too vague; use a useful condition")
    try:
        relative = path.resolve().relative_to(records_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"{path}: record is outside {records_dir}") from exc
    if len(relative.parts) != 3:
        raise ValueError(f"{path}: expected records/<layer>/<category>/<slug>.md")
    if relative.parts[:2] != (layer, category):
        raise ValueError(f"{path}: frontmatter layer/category do not match its directory")
    if path.suffix != ".md" or not SLUG.fullmatch(path.stem):
        raise ValueError(f"{path}: record filename must be a kebab-case .md slug")
    expected_id = f"{layer}.{category}.{path.stem}"
    if metadata["id"] != expected_id:
        raise ValueError(f"{path}: id must be {expected_id!r}")
    if layer == "policy" and category not in {"shared", "local"}:
        raise ValueError(f"{path}: policy category must be shared or local")


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"cannot read protocol declaration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"protocol declaration must be an object: {path}")
    return value


def check_protocol(value: object, path: Path) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "generation"}
        or type(value.get("generation")) is not int
        or value != DATA_PROTOCOL
    ):
        raise ValueError(f"incompatible data protocol in {path}: expected {DATA_PROTOCOL!r}, got {value!r}")


def framework_manifest(root: Path, *, reader_ok: bool = False) -> dict:
    path = root / "framework.json"
    value = read_object(path)
    kinds = {FRAMEWORK_FORMAT, READER_FORMAT} if reader_ok else {FRAMEWORK_FORMAT}
    if (
        set(value) != {"kind", "release", "data_protocol"}
        or value.get("kind") not in kinds
        or not isinstance(value.get("release"), str)
        or not value["release"].strip()
    ):
        raise ValueError(f"invalid framework declaration: {path}")
    check_protocol(value["data_protocol"], path)
    return value


def bank_manifest(root: Path, *, snapshot_ok: bool = False) -> dict:
    path = root / ".memory" / "protocol.json"
    value = read_object(path)
    kinds = {"bank", "snapshot"} if snapshot_ok else {"bank"}
    if set(value) != {"kind", "protocol"} or value.get("kind") not in kinds:
        raise ValueError(f"invalid bank declaration or read-only snapshot: {path}")
    check_protocol(value["protocol"], path)
    return value


def discover_workspace(
    explicit: Path | str | None = None, *, framework_root: Path | None = None,
    allow_missing: bool = False,
) -> Path:
    selected = explicit if explicit is not None else os.environ.get("MEMORY_WORKSPACE")
    if selected is not None:
        return Path(selected).expanduser().resolve()
    if framework_root is not None and framework_root.resolve().parent.name == ".memory":
        return framework_root.resolve().parent.parent
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".memory").exists():
            # Do not silently skip an incompatible or undeclared nearer bank.
            return candidate
    if allow_missing:
        return current
    raise ValueError("no memory workspace found; supply --workspace or MEMORY_WORKSPACE")
