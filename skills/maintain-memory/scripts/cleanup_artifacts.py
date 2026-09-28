#!/usr/bin/env python3
"""Safely remove disposable Memory v2 runtime artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
FRAMEWORK_ROOT = SCRIPT_PATH.parents[3]
sys.path.insert(0, str(FRAMEWORK_ROOT / "lib"))
sys.path.insert(0, str(FRAMEWORK_ROOT / "clients"))

from claude.adapter import ClaudeAdapter  # noqa: E402
from codex.adapter import CodexAdapter  # noqa: E402
from memory_framework.errors import MemoryFrameworkError  # noqa: E402
from memory_framework.workspace import Workspace, discover_workspace  # noqa: E402


class CleanupRefusal(RuntimeError):
    """Raised when safe ownership or lease state cannot be established."""


@dataclass(frozen=True)
class Lease:
    client: str
    registry: Path
    active: bool
    generated: Path
    profile: Path | None = None
    prompt: Path | None = None
    registration: Path | None = None


@dataclass
class CleanupReport:
    removed: int = 0
    active_leases: int = 0
    preserved_paths: int = 0
    stale_leases: int = 0
    skipped: list[str] = field(default_factory=list)


def _process_start_time(pid: int) -> str | None:
    try:
        source = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return source.rsplit(")", 1)[1].split()[19]
    except (IndexError, ValueError):
        return None


def _is_active(value: dict[str, object]) -> bool:
    pid = value.get("pid")
    start_time = value.get("process_start_time")
    return (
        isinstance(pid, int)
        and isinstance(start_time, str)
        and _process_start_time(pid) == start_time
    )


def _launch_id(value: dict[str, object], registry: Path) -> str:
    launch_id = value.get("launch_id")
    if not isinstance(launch_id, str) or registry.name != f"{launch_id}.json":
        raise CleanupRefusal(f"invalid launch ID in client registry: {registry}")
    try:
        parsed = uuid.UUID(hex=launch_id)
    except ValueError as exc:
        raise CleanupRefusal(f"invalid launch UUID in client registry: {registry}") from exc
    if parsed.hex != launch_id:
        raise CleanupRefusal(f"non-canonical launch UUID in client registry: {registry}")
    return launch_id


def _absolute_path(value: object, field_name: str, registry: Path) -> Path:
    if not isinstance(value, str):
        raise CleanupRefusal(f"invalid {field_name} path in client registry: {registry}")
    path = Path(value)
    if not path.is_absolute():
        raise CleanupRefusal(f"non-absolute {field_name} path in client registry: {registry}")
    return path


def _direct_launch_directory(path: Path, root: Path, registry: Path) -> Path:
    resolved = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise CleanupRefusal(f"generated path escapes its client cache: {registry}") from exc
    if len(relative.parts) != 1:
        raise CleanupRefusal(f"generated path is not one launch directory: {registry}")
    try:
        parsed = uuid.UUID(hex=relative.name)
    except ValueError as exc:
        raise CleanupRefusal(f"generated path has an invalid launch UUID: {registry}") from exc
    if parsed.hex != relative.name:
        raise CleanupRefusal(f"generated path has a non-canonical launch UUID: {registry}")
    return resolved


def _registry_values(directory: Path) -> list[tuple[Path, dict[str, object]]]:
    if not os.path.lexists(directory):
        return []
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise CleanupRefusal(f"unsafe client registry directory: {directory}")
    values: list[tuple[Path, dict[str, object]]] = []
    for registry in sorted(directory.iterdir()):
        metadata = registry.lstat()
        if (
            registry.suffix != ".json"
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise CleanupRefusal(f"unexpected client registry entry: {registry}")
        try:
            value = json.loads(registry.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CleanupRefusal(f"cannot read client registry safely: {registry}") from exc
        if not isinstance(value, dict):
            raise CleanupRefusal(f"client registry must be a JSON object: {registry}")
        values.append((registry, value))
    return values


def _inspect_codex(workspace: Workspace) -> list[Lease]:
    profiles = workspace.memory_dir / "clients" / "codex" / "profiles"
    registrations = workspace.state_dir / "clients" / "codex" / "registrations"
    launches = workspace.generated_dir / "clients" / "codex" / "launches"
    namespace = "memory-" + hashlib.sha256(
        str(workspace.root).encode("utf-8")
    ).hexdigest()[:12]
    leases: list[Lease] = []
    for registry, value in _registry_values(registrations):
        launch_id = _launch_id(value, registry)
        if value.get("version") != 1 or value.get("uid") != os.getuid():
            raise CleanupRefusal(f"unsupported or foreign Codex registry: {registry}")
        profile = _absolute_path(value.get("profile"), "profile", registry)
        expected_profile = profiles / f"{launch_id}.config.toml"
        if profile != expected_profile:
            raise CleanupRefusal(f"Codex profile path does not match its registry: {registry}")
        generated = _direct_launch_directory(
            _absolute_path(value.get("generated"), "generated", registry),
            launches,
            registry,
        )
        registration = _absolute_path(
            value.get("registration"), "external registration", registry
        )
        if registration.name != f"{namespace}-{launch_id}.config.toml":
            raise CleanupRefusal(f"unexpected external Codex registration name: {registry}")
        try:
            parent_metadata = registration.parent.lstat()
        except OSError as exc:
            raise CleanupRefusal(
                f"cannot verify external Codex registration directory: {registry}"
            ) from exc
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.getuid()
        ):
            raise CleanupRefusal(f"unsafe external Codex registration directory: {registry}")
        if os.path.lexists(registration):
            metadata = registration.lstat()
            if (
                not stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or registration.resolve(strict=False) != profile.resolve(strict=False)
            ):
                raise CleanupRefusal(f"unsafe external Codex registration: {registration}")
        leases.append(
            Lease(
                client="codex",
                registry=registry,
                active=_is_active(value),
                generated=generated,
                profile=profile,
                registration=registration,
            )
        )
    return leases


def _inspect_claude(workspace: Workspace) -> list[Lease]:
    registrations = workspace.state_dir / "clients" / "claude" / "registrations"
    launches = workspace.generated_dir / "clients" / "claude" / "launches"
    leases: list[Lease] = []
    for registry, value in _registry_values(registrations):
        launch_id = _launch_id(value, registry)
        if value.get("version") != 1 or value.get("uid") != os.getuid():
            raise CleanupRefusal(f"unsupported or foreign Claude registry: {registry}")
        generated = _direct_launch_directory(
            _absolute_path(value.get("generated"), "generated", registry),
            launches,
            registry,
        )
        expected_generated = launches.resolve(strict=False) / launch_id
        if generated != expected_generated:
            raise CleanupRefusal(f"Claude launch path does not match its registry: {registry}")
        prompt = _absolute_path(value.get("prompt"), "prompt", registry)
        if prompt != generated / "system-prompt.md":
            raise CleanupRefusal(f"Claude prompt path does not match its registry: {registry}")
        leases.append(
            Lease(
                client="claude",
                registry=registry,
                active=_is_active(value),
                generated=generated,
                prompt=prompt,
            )
        )
    return leases


def _inspect_leases(workspace: Workspace) -> list[Lease]:
    return [*_inspect_codex(workspace), *_inspect_claude(workspace)]


def _remove_stale_leases(workspace: Workspace, leases: list[Lease]) -> None:
    CodexAdapter(workspace, launch_cwd=workspace.root).cleanup_registrations()
    ClaudeAdapter(workspace, launch_cwd=workspace.root).cleanup_registrations()
    for lease in leases:
        if not lease.active and os.path.lexists(lease.registry):
            raise CleanupRefusal(
                f"stale {lease.client} lease could not be removed safely: {lease.registry}"
            )


def _has_protected_descendant(path: Path, protected: set[Path]) -> bool:
    return any(path == item or path in item.parents for item in protected)


def _clean_path(
    path: Path,
    *,
    protected: set[Path],
    dry_run: bool,
    report: CleanupReport,
) -> None:
    if not os.path.lexists(path):
        return
    if path in protected:
        report.preserved_paths += 1
        return
    metadata = path.lstat()
    if metadata.st_uid != os.getuid():
        report.skipped.append(f"foreign-owned artifact: {path}")
        return
    if stat.S_ISLNK(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
        if _has_protected_descendant(path, protected):
            report.skipped.append(f"protected path has an unsafe parent: {path}")
            return
        if not dry_run:
            path.unlink()
        report.removed += 1
        return
    if not stat.S_ISDIR(metadata.st_mode):
        report.skipped.append(f"special artifact entry: {path}")
        return
    if os.path.ismount(path):
        report.skipped.append(f"mounted artifact directory: {path}")
        return
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        _clean_path(child, protected=protected, dry_run=dry_run, report=report)
    if _has_protected_descendant(path, protected):
        return
    if dry_run:
        report.removed += 1
        return
    try:
        path.rmdir()
    except OSError:
        return
    report.removed += 1


def _clean_artifacts(workspace: Workspace, *, dry_run: bool) -> CleanupReport:
    report = CleanupReport()
    leases = _inspect_leases(workspace)
    report.stale_leases = sum(not lease.active for lease in leases)
    if not dry_run:
        _remove_stale_leases(workspace, leases)
        leases = _inspect_leases(workspace)
        if any(not lease.active for lease in leases):
            raise CleanupRefusal("a stale client lease remained after safe cleanup")

    active = [lease for lease in leases if lease.active]
    report.active_leases = len(active)
    generated_protected = {lease.generated for lease in active}
    profile_protected = {
        lease.profile for lease in active if lease.profile is not None
    }
    _clean_path(
        workspace.generated_dir,
        protected=generated_protected,
        dry_run=dry_run,
        report=report,
    )
    _clean_path(
        workspace.memory_dir / "clients" / "codex" / "profiles",
        protected=profile_protected,
        dry_run=dry_run,
        report=report,
    )

    handoffs = workspace.state_dir / "handoffs"
    _clean_path(
        handoffs,
        protected={handoffs} if active else set(),
        dry_run=dry_run,
        report=report,
    )

    if not dry_run:
        for directory in (
            workspace.state_dir / "clients" / "codex" / "registrations",
            workspace.state_dir / "clients" / "claude" / "registrations",
        ):
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
                report.removed += 1
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Remove disposable Memory v2 artifacts while preserving active client leases "
            "and all authoritative or durable state."
        )
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="workspace containing .memory (default: MEMORY_WORKSPACE or cwd discovery)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect and report without deleting anything",
    )
    args = parser.parse_args()

    try:
        workspace = Workspace(discover_workspace(args.workspace))
        if not args.dry_run:
            workspace.assert_mutable("clean disposable runtime artifacts")
        lock = nullcontext() if args.dry_run else workspace.annotation_lock(mutable=True)
        with lock:
            report = _clean_artifacts(workspace, dry_run=args.dry_run)
    except (CleanupRefusal, MemoryFrameworkError, OSError) as exc:
        print(f"artifact cleanup refused: {exc}", file=sys.stderr)
        return 2

    action = "would remove" if args.dry_run else "removed"
    print(
        f"artifact cleanup: {action} {report.removed} paths; "
        f"stale leases={report.stale_leases}; "
        f"preserved active leases={report.active_leases}"
    )
    if report.skipped:
        for message in report.skipped:
            print(f"artifact cleanup skipped: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
