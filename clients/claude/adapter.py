from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from memory_framework.errors import MemoryFrameworkError, ValidationError
from memory_framework.parsing import load_task_types
from memory_framework.rendering import render_auto_view, render_task_view
from memory_framework.tasking import BUILT_IN_TASKS, resolve_task
from memory_framework.workspace import Workspace, atomic_write_json, atomic_write_text


PREFERENCES_VERSION = 1


def _process_start_time(pid: int) -> str | None:
    try:
        source = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return source.rsplit(")", 1)[1].split()[19]
    except (IndexError, ValueError):
        return None


class ClaudeAdapter:
    """Render and inject task-bound Memory views into Claude Code."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        executable: str | None = None,
        launch_cwd: Path | None = None,
        effective_cwd: Path | None = None,
    ):
        self.workspace = workspace
        self.executable = executable or os.environ.get("MEMORY_CLAUDE_BIN", "claude")
        self.launch_cwd = (launch_cwd or Path.cwd()).expanduser().resolve()
        self.effective_cwd = (effective_cwd or self.launch_cwd).expanduser().resolve()
        for label, path in (
            ("Claude Code launch", self.launch_cwd),
            ("Claude Code effective", self.effective_cwd),
        ):
            if not path.is_dir():
                raise ValidationError(f"{label} directory does not exist: {path}")
        self._leases: dict[Path, str] = {}

    @property
    def launches_dir(self) -> Path:
        return self.workspace.generated_dir / "clients" / "claude" / "launches"

    @property
    def preferences_path(self) -> Path:
        return self.workspace.state_dir / "clients" / "claude" / "preferences.json"

    @property
    def preferences_lock_path(self) -> Path:
        return self.workspace.state_dir / "clients" / "claude" / "preferences.lock"

    @property
    def registrations_dir(self) -> Path:
        return self.workspace.state_dir / "clients" / "claude" / "registrations"

    def available_tasks(self) -> dict[str, object]:
        tasks: dict[str, object] = dict(BUILT_IN_TASKS)
        tasks.update(load_task_types(self.workspace.tasks_dir))
        return tasks

    def available_routing_tasks(self) -> dict[str, object]:
        return dict(load_task_types(self.workspace.tasks_dir))

    def validate_launch_arguments(self, arguments: list[str]) -> None:
        """Validate wrapper-owned launch constraints.

        Claude Code supports combining one appended prompt with either
        replacement form. The wrapper owns the append surface so a later user
        append option cannot replace or reorder the selected Memory artifact.
        """
        for argument in arguments:
            if argument in {
                "--append-system-prompt",
                "--append-system-prompt-file",
            } or any(
                argument.startswith(prefix)
                for prefix in (
                    "--append-system-prompt=",
                    "--append-system-prompt-file=",
                )
            ):
                raise ValidationError(
                    "memory-claude owns the appended system prompt; put additional persistent "
                    "instructions in CLAUDE.md or use --system-prompt/--system-prompt-file"
                )

    def render_task_artifact(self, task_id: str) -> Path:
        target = (
            self.workspace.generated_dir
            / "clients"
            / "claude"
            / task_id
            / "instructions.md"
        )
        render_task_view(self.workspace, task_id, output_path=target)
        return target

    def _new_launch_path(self) -> Path:
        launch_id = uuid.uuid4().hex
        return self.launches_dir / launch_id / "system-prompt.md"

    def _register_prompt(self, output: Path) -> None:
        self.workspace.assert_mutable("build Claude Code prompt")
        launch_id = output.parent.name
        if output != self.launches_dir / launch_id / "system-prompt.md":
            raise ValidationError(f"invalid Claude Code launch artifact path: {output}")
        try:
            uuid.UUID(hex=launch_id)
        except ValueError as exc:
            raise ValidationError(
                f"invalid Claude Code launch ID: {launch_id}"
            ) from exc
        self.workspace.ensure_private_runtime_directory(output.parent)
        self.workspace.ensure_private_runtime_directory(self.registrations_dir)
        start_time = _process_start_time(os.getpid())
        if start_time is None:
            raise ValidationError("cannot determine wrapper process start time")
        registry = self.registrations_dir / f"{launch_id}.json"
        atomic_write_json(
            registry,
            {
                "version": 1,
                "launch_id": launch_id,
                "uid": os.getuid(),
                "pid": os.getpid(),
                "process_start_time": start_time,
                "prompt": str(output.resolve()),
                "generated": str(output.parent.resolve()),
            },
            mode=0o600,
        )
        self._leases[output.resolve()] = launch_id

    def _registration_is_active(self, value: dict[str, object]) -> bool:
        pid = value.get("pid")
        start_time = value.get("process_start_time")
        return (
            isinstance(pid, int)
            and isinstance(start_time, str)
            and _process_start_time(pid) == start_time
        )

    def _remove_registration(
        self, registry: Path, value: dict[str, object], *, force: bool
    ) -> bool:
        launch_id = value.get("launch_id")
        if not isinstance(launch_id, str) or registry.name != f"{launch_id}.json":
            return False
        if value.get("version") != 1 or value.get("uid") != os.getuid():
            return False
        if not force and self._registration_is_active(value):
            return False
        launch_directory = (self.launches_dir / launch_id).resolve()
        prompt = launch_directory / "system-prompt.md"
        if value.get("generated") != str(launch_directory) or value.get(
            "prompt"
        ) != str(prompt):
            return False
        if launch_directory.parent != self.launches_dir.resolve():
            return False
        if launch_directory.exists():
            metadata = launch_directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                return False
            shutil.rmtree(launch_directory)
        registry.unlink(missing_ok=True)
        return True

    def cleanup_registrations(self) -> None:
        if not self.registrations_dir.exists():
            return
        for registry in sorted(self.registrations_dir.glob("*.json")):
            try:
                metadata = registry.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    continue
                value = json.loads(registry.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                self._remove_registration(registry, value, force=False)

    def build_task_prompt(self, task_id: str, *, session_note: str = "") -> Path:
        standard = load_task_types(self.workspace.tasks_dir)
        resolve_task(task_id, standard)
        output = self._new_launch_path()
        self._register_prompt(output)
        try:
            result = render_task_view(
                self.workspace,
                task_id,
                output_path=output,
                write_manifest=False,
            )
            if session_note.strip():
                content = str(result["content"]) + (
                    "\n---\n\n# Confirmed routing note for this session\n\n"
                    + session_note.strip()
                    + "\n"
                )
                atomic_write_text(output, content, mode=0o600)
        except BaseException:
            self.release_prompt(output)
            raise
        return output

    @contextmanager
    def _preferences_lock(self) -> Iterator[None]:
        self.workspace.ensure_private_runtime_directory(
            self.preferences_lock_path.parent
        )
        with self.preferences_lock_path.open("a+", encoding="utf-8") as handle:
            os.chmod(self.preferences_lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load_preferences(self) -> dict[str, object]:
        if not self.preferences_path.exists():
            return {"version": PREFERENCES_VERSION, "tasks": {}}
        try:
            value = json.loads(self.preferences_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"version": PREFERENCES_VERSION, "tasks": {}}
        if not isinstance(value, dict) or value.get("version") != PREFERENCES_VERSION:
            return {"version": PREFERENCES_VERSION, "tasks": {}}
        entries = value.get("tasks")
        if not isinstance(entries, dict):
            return {"version": PREFERENCES_VERSION, "tasks": {}}
        available = self.available_routing_tasks()
        filtered: dict[str, object] = {}
        for task_id, entry in entries.items():
            task = available.get(task_id)
            if (
                task is not None
                and isinstance(entry, dict)
                and entry.get("task_hash") == task.content_hash
            ):
                filtered[task_id] = entry
        return {"version": PREFERENCES_VERSION, "tasks": filtered}

    def remember_selection(
        self,
        task_id: str,
        *,
        directory_hint: str | None = None,
        normalized_intent: str | None = None,
        remember_similar: bool = False,
    ) -> None:
        self.workspace.assert_mutable("write routing preferences")
        task = self.available_routing_tasks().get(task_id)
        if task is None:
            raise ValidationError(f"cannot remember unknown standard task: {task_id}")
        with self._preferences_lock():
            preferences = self.load_preferences()
            entries = preferences["tasks"]
            assert isinstance(entries, dict)
            entry = dict(entries.get(task_id, {}))
            entry["task_hash"] = task.content_hash
            entry["selection_count"] = int(entry.get("selection_count", 0)) + 1
            entry["last_used"] = datetime.now(timezone.utc).isoformat()
            hints = [
                item
                for item in entry.get("directory_hints", [])
                if isinstance(item, str)
            ]
            if directory_hint and directory_hint not in hints:
                hints.append(directory_hint)
            entry["directory_hints"] = hints[-12:]
            summaries = [
                item
                for item in entry.get("intent_summaries", [])
                if isinstance(item, str)
            ]
            if (
                remember_similar
                and normalized_intent
                and normalized_intent not in summaries
            ):
                summaries.append(normalized_intent)
            entry["intent_summaries"] = summaries[-12:]
            entry["auto_similar"] = bool(remember_similar and normalized_intent)
            entries[task_id] = entry
            atomic_write_json(self.preferences_path, preferences, mode=0o600)

    def build_auto_prompt(
        self,
        handoff_path: Path,
        *,
        nonce: str,
        continuation: bool,
    ) -> Path:
        if continuation:
            launch_contract = (
                "Complete this continuation in Auto and do not create a task handoff. "
                "Start a new session if a different task is needed."
            )
        else:
            launch_contract = (
                "Only if this new request must continue under one standard task, write exactly "
                "one v2 handoff with fields `version: 2`, `nonce`, `mode: task`, `kind: new`, "
                "`task`, and the original user `request` preserved verbatim."
            )
        instructions = (
            "The handoff is an optional request to start a second Claude Code process, never a "
            "completion receipt. Create no file when Auto answers or completes the work. "
            + launch_contract
            + " The task must be a defined standard task, never `all-lazy` or `all-eager`. "
            "Optional fields are boolean `remember`, a user-confirmed `normalized_intent`, "
            "`directory_hint`, and a session-only `memory_preference`. Use the exact path and "
            "nonce below, create a regular owner-only file, and then tell the user to exit. "
            "Direct memory administration stays in Auto. Never write raw prompts to preferences.\n\n"
            f"HANDOFF_PATH={handoff_path} HANDOFF_NONCE={nonce}"
        )
        output = self._new_launch_path()
        self._register_prompt(output)
        try:
            render_auto_view(
                self.workspace,
                preferences=self.load_preferences(),
                extra_instructions=instructions,
                output_path=output,
                write_manifest=False,
            )
        except BaseException:
            self.release_prompt(output)
            raise
        return output

    def run_with_prompt(
        self,
        prompt_path: Path,
        arguments: list[str],
        *,
        handoff_contract: str | None = None,
    ) -> int:
        command = [
            self.executable,
            "--append-system-prompt-file",
            str(prompt_path),
            *arguments,
        ]
        environment = os.environ.copy()
        environment["MEMORY_WORKSPACE"] = str(self.workspace.root)
        environment["MEMORY_FRAMEWORK_ROOT"] = str(self.workspace.framework_root)
        if handoff_contract is None:
            environment.pop("MEMORY_FRAMEWORK_HANDOFF", None)
        else:
            environment["MEMORY_FRAMEWORK_HANDOFF"] = handoff_contract
        try:
            return subprocess.run(
                command,
                env=environment,
                cwd=self.launch_cwd,
                check=False,
            ).returncode
        except OSError as exc:
            raise MemoryFrameworkError(
                f"cannot launch Claude Code executable {self.executable!r}: {exc}"
            ) from exc

    def release_prompt(self, prompt_path: Path) -> None:
        resolved = prompt_path.resolve()
        launch_id = self._leases.pop(resolved, None)
        if launch_id is None:
            return
        registry = self.registrations_dir / f"{launch_id}.json"
        try:
            value = json.loads(registry.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(value, dict):
            self._remove_registration(registry, value, force=True)

    def launch_task(self, task_id: str, arguments: list[str]) -> None:
        prompt_path = self.build_task_prompt(task_id)
        try:
            status = self.run_with_prompt(prompt_path, arguments)
        finally:
            self.release_prompt(prompt_path)
        if status != 0:
            raise MemoryFrameworkError(f"Claude Code exited with status {status}")
