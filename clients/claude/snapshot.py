from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from memory_framework.errors import ValidationError
from memory_framework.rendering import (
    _check_record_keys, _lazy_loader_hint, _record_key, _record_loader_hint,
    _render_lazy, _render_loaded,
)
from memory_framework.snapshot import build_snapshot_plan, write_snapshot_reader
from memory_framework.workspace import Workspace


def _file_manifest(root: Path, directory: Path) -> list[dict[str, object]]:
    if not directory.exists():
        return []
    result = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        raw = path.read_bytes()
        result.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return result


def export_snapshot(
    workspace: Workspace,
    task_id: str,
    output_dir: Path | str,
    *,
    include_local: bool = False,
) -> Path:
    """Export a framework-free Claude Code snapshot for one task."""
    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise ValidationError(f"snapshot target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    plan = build_snapshot_plan(workspace, task_id, include_local=include_local)
    task = plan["task"]
    loaded = plan["loaded"]
    lazy = plan["lazy"]
    load_states = plan["load_states"]
    assert (
        isinstance(loaded, list)
        and isinstance(lazy, list)
        and isinstance(load_states, dict)
    )
    instruction_path = workspace.instructions_dir / "snapshot.md"
    snapshot_instruction = instruction_path.read_text(encoding="utf-8").strip()
    _check_record_keys(loaded + lazy)
    loaded_markdown, _ = _render_loaded(loaded)
    lazy_markdown = _render_lazy(lazy)
    entry = (
        snapshot_instruction
        + "\n\n---\n\n# Active task type\n\n"
        + task.description_markdown().strip()
        + "\n\n---\n\n# Materialized memory\n\n"
        + (_record_loader_hint("framework/scripts/load.py") if loaded else "")
        + loaded_markdown
        + "\n\n---\n\n# Lazy memory catalog\n\n"
        + (_lazy_loader_hint("framework/scripts/load.py") if lazy else "")
        + lazy_markdown
        + "\n"
    )
    entry_bytes = entry.encode("utf-8")

    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        (temporary / "CLAUDE.md").write_bytes(entry_bytes)
        write_snapshot_reader(workspace, temporary)
        for record in loaded + lazy:
            destination = temporary / record.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(record.source.encode("utf-8"))
        tools_destination = temporary / ".memory" / "tools"
        if workspace.tools_dir.exists():
            shutil.copytree(
                workspace.tools_dir,
                tools_destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

        records = []
        for record in sorted(loaded + lazy, key=lambda item: item.id):
            records.append(
                {
                    "id": record.id,
                    "short_id": _record_key(record),
                    "hash": record.content_hash,
                    "loading": load_states[record.id],
                    "path": record.relative_path,
                    "sharing": record.sharing,
                }
            )
        manifest = {
            "version": 2,
            "client": "claude",
            "task": {"id": task.id, "hash": task.content_hash},
            "include_local": include_local,
            "entry": {
                "path": "CLAUDE.md",
                "bytes": len(entry_bytes),
                "sha256": hashlib.sha256(entry_bytes).hexdigest(),
            },
            "records": records,
            "tools": _file_manifest(temporary, tools_destination),
            "loader": _file_manifest(temporary, temporary / "framework"),
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        directory_mode = 0o500 if include_local else 0o555
        file_mode = 0o400 if include_local else 0o444
        for path in sorted(
            temporary.rglob("*"), key=lambda item: len(item.parts), reverse=True
        ):
            if path.is_symlink():
                raise ValidationError(f"snapshot cannot contain symlinks: {path}")
            path.chmod(directory_mode if path.is_dir() else file_mode)
        temporary.chmod(directory_mode)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target
