from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import ValidationError
from memory_protocol import DATA_PROTOCOL, bank_manifest, framework_manifest
from memory_protocol import discover_workspace as _discover_workspace


FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]


def discover_workspace(explicit: Path | str | None = None) -> Path:
    try:
        return _discover_workspace(explicit, framework_root=FRAMEWORK_ROOT)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


class Workspace:
    def __init__(self, root: Path | str, *, framework_root: Path | str | None = None):
        self.root = Path(root).expanduser().resolve()
        self.memory_dir = self.root / ".memory"
        self.framework_root = Path(framework_root or FRAMEWORK_ROOT).expanduser().resolve()
        self.assert_compatible()

    def assert_compatible(self) -> None:
        try:
            self.framework_manifest = framework_manifest(self.framework_root)
            self.bank_manifest = bank_manifest(self.root)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    @property
    def records_dir(self) -> Path:
        return self.memory_dir / "records"

    @property
    def categories_dir(self) -> Path:
        return self.memory_dir / "categories"

    @property
    def tasks_dir(self) -> Path:
        return self.memory_dir / "task-types"

    @property
    def annotations_dir(self) -> Path:
        return self.memory_dir / "annotations"

    @property
    def generated_dir(self) -> Path:
        return self.memory_dir / "generated"

    @property
    def skills_dir(self) -> Path:
        return self.framework_root / "skills"

    @property
    def instructions_dir(self) -> Path:
        return self.framework_root / "instructions"

    @property
    def tools_dir(self) -> Path:
        return self.memory_dir / "tools"

    @property
    def state_dir(self) -> Path:
        return self.memory_dir / "state"

    def matrix_path(self, sharing: str) -> Path:
        if sharing == "shared":
            return self.annotations_dir / "matrix.json"
        if sharing == "local":
            return self.annotations_dir / "matrix.local.json"
        raise ValidationError(f"unknown sharing scope: {sharing}")

    @property
    def freeze_sentinels(self) -> tuple[Path, Path]:
        return (self.memory_dir / "FROZEN",)

    def assert_mutable(self, operation: str) -> None:
        self.assert_compatible()
        for sentinel in self.freeze_sentinels:
            if sentinel.exists() or sentinel.is_symlink():
                raise ValidationError(
                    f"workspace is frozen; refusing to {operation}: {sentinel}"
                )

    def ensure_private_runtime_directory(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        permitted = (
            self.generated_dir.resolve(),
            self.state_dir.resolve(),
            (self.memory_dir / "clients").resolve(),
        )
        roots = [root for root in permitted if resolved == root or root in resolved.parents]
        if not roots:
            raise ValidationError(f"not a framework runtime directory: {resolved}")
        root = max(roots, key=lambda item: len(item.parts))
        current = root
        current.mkdir(parents=True, exist_ok=True, mode=0o700)
        if current.stat().st_uid != os.getuid():
            raise ValidationError(f"runtime directory is owned by another user: {current}")
        current.chmod(0o700)
        for part in resolved.relative_to(root).parts:
            current = current / part
            current.mkdir(exist_ok=True, mode=0o700)
            if current.stat().st_uid != os.getuid():
                raise ValidationError(f"runtime directory is owned by another user: {current}")
            current.chmod(0o700)

    @contextmanager
    def annotation_lock(self, *, mutable: bool = False) -> Iterator[None]:
        self.assert_compatible()
        if mutable:
            self.assert_mutable("lock annotations for mutation")
        lock_path = self.state_dir / "annotations.lock"
        if not lock_path.exists():
            if any(path.exists() or path.is_symlink() for path in self.freeze_sentinels):
                yield
                return
            self.state_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.state_dir, 0o700)
            lock_path.touch(mode=0o600)
        mode = "r+" if mutable else "r"
        with lock_path.open(mode, encoding="utf-8") as handle:
            if mutable:
                os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if mutable else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def initialize_bank(root: Path | str) -> Workspace:
    """Create data only. Never replace or infer the format of an existing bank."""
    target = Path(root).expanduser().resolve()
    try:
        framework_manifest(FRAMEWORK_ROOT)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    memory = target / ".memory"
    embedded_only = (
        memory.is_dir() and not memory.is_symlink()
        and set(path.name for path in memory.iterdir()) == {"framework"}
        and (memory / "framework").resolve() == FRAMEWORK_ROOT
    )
    if (memory.exists() or memory.is_symlink()) and not embedded_only:
        raise ValidationError(f"refusing to initialize existing memory bank: {memory}")
    target.mkdir(parents=True, exist_ok=True)
    if not embedded_only:
        memory.mkdir(mode=0o700)
    for name in ("records", "categories", "task-types", "annotations", "tools"):
        (memory / name).mkdir(mode=0o700)
    atomic_write_json(memory / "annotations" / "matrix.json", {"version": 2, "always": {}, "tasks": {}}, mode=0o600)
    atomic_write_json(memory / "protocol.json", {"kind": "bank", "protocol": DATA_PROTOCOL}, mode=0o600)
    return Workspace(target)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_digest(value: object) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def atomic_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def atomic_write_json(path: Path, value: object, mode: int = 0o644) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode)
