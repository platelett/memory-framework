from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from base import load_handoff as load_handoff
from memory_framework.errors import MemoryFrameworkError, PublicationError, ValidationError
from memory_framework.parsing import load_task_types
from memory_framework.preflight import (
    apply_annotation_preflight,
    prepare_annotation_preflight,
    record_classifier_result,
    relevant_task_issues,
)
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


class CodexAdapter:
    def __init__(
        self,
        workspace: Workspace,
        *,
        codex_home: Path | None = None,
        executable: str | None = None,
        launch_cwd: Path | None = None,
        effective_cwd: Path | None = None,
    ):
        self.workspace = workspace
        configured_home = os.environ.get("CODEX_HOME")
        self.codex_home = (
            codex_home
            or (Path(configured_home) if configured_home else Path.home() / ".codex")
        ).expanduser().resolve()
        self.executable = executable or os.environ.get("MEMORY_CODEX_BIN", "codex")
        self.launch_cwd = (launch_cwd or Path.cwd()).expanduser().resolve()
        self.effective_cwd = (effective_cwd or self.launch_cwd).expanduser().resolve()
        for label, path in (
            ("Codex launch", self.launch_cwd),
            ("Codex effective", self.effective_cwd),
        ):
            if not path.is_dir():
                raise ValidationError(f"{label} directory does not exist: {path}")
        digest = hashlib.sha256(str(workspace.root).encode("utf-8")).hexdigest()[:12]
        self.namespace = f"memory-{digest}"
        self._leases: dict[str, str] = {}

    @property
    def profiles_dir(self) -> Path:
        return self.workspace.memory_dir / "clients" / "codex" / "profiles"

    @property
    def preferences_path(self) -> Path:
        return self.workspace.state_dir / "clients" / "codex" / "preferences.json"

    @property
    def registrations_dir(self) -> Path:
        return self.workspace.state_dir / "clients" / "codex" / "registrations"

    @property
    def preferences_lock_path(self) -> Path:
        return self.workspace.state_dir / "clients" / "codex" / "preferences.lock"

    def available_tasks(self) -> dict[str, object]:
        tasks: dict[str, object] = dict(BUILT_IN_TASKS)
        tasks.update(load_task_types(self.workspace.tasks_dir))
        return tasks

    def available_routing_tasks(self) -> dict[str, object]:
        return dict(load_task_types(self.workspace.tasks_dir))

    def _read_user_instructions(self) -> str:
        path = self.codex_home / "config.toml"
        if not path.exists():
            return ""
        try:
            config = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ValidationError(f"cannot read user Codex config {path}: {exc}") from exc
        value = config.get("developer_instructions", "")
        if not isinstance(value, str):
            raise ValidationError(f"developer_instructions in {path} must be a string")
        return value.strip()

    def _project_config_paths(self) -> list[Path]:
        root = self.workspace.root
        cwd = self.effective_cwd
        try:
            relative = cwd.relative_to(root)
        except ValueError:
            return [cwd / ".codex" / "config.toml"]
        directories = [root]
        current = root
        for part in relative.parts:
            current = current / part
            directories.append(current)
        return [directory / ".codex" / "config.toml" for directory in directories]

    def ensure_no_project_override(self) -> None:
        for path in self._project_config_paths():
            if not path.exists():
                continue
            try:
                value = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise ValidationError(
                    f"cannot inspect higher-precedence project config {path}: {exc}"
                ) from exc
            if "developer_instructions" in value:
                raise ValidationError(
                    "project config would override the task-bound developer_instructions: "
                    f"{path}; remove that key or launch from a scope where it does not apply"
                )

    def validate_launch_arguments(self, arguments: list[str]) -> None:
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument == "--maintain" or argument.startswith("--maintain="):
                raise ValidationError(
                    "--maintain was removed; describe the maintenance request to --auto"
                )
            if argument in {"--profile", "-p"} or argument.startswith("--profile="):
                raise ValidationError("memory-codex owns --profile; a second profile is not supported")
            if argument in {"--config", "-c"}:
                if index + 1 >= len(arguments):
                    raise ValidationError(f"{argument} requires a value")
                if arguments[index + 1].split("=", 1)[0] == "developer_instructions":
                    raise ValidationError("launch arguments cannot override developer_instructions")
                index += 1
            elif (
                argument.startswith("--config=")
                and argument[9:].split("=", 1)[0] == "developer_instructions"
            ):
                raise ValidationError("launch arguments cannot override developer_instructions")
            index += 1

    def _compose(self, framework_instructions: str) -> str:
        user = self._read_user_instructions()
        if not user:
            return framework_instructions.strip() + "\n"
        return user + "\n\n---\n\n" + framework_instructions.strip() + "\n"

    def _profile_name(self, key: str, launch_id: str | None = None) -> str:
        safe = "".join(
            character if character.isalnum() or character in "-_" else "-"
            for character in key
        )
        suffix = launch_id or safe
        return f"{self.namespace}-{suffix}"

    def _ensure_private_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.stat().st_uid != os.getuid():
            raise ValidationError(f"refusing to use directory owned by another user: {path}")
        path.chmod(0o700)

    def _register_profile(
        self,
        key: str,
        instructions: str,
        output: Path,
        *,
        include_user_instructions: bool = True,
    ) -> tuple[str, Path]:
        self.workspace.assert_mutable("build Codex profile")
        self.ensure_no_project_override()
        launch_id = uuid.uuid4().hex
        profile_name = self._profile_name(key, launch_id)
        profile_path = self.profiles_dir / f"{launch_id}.config.toml"
        composed = (
            self._compose(instructions)
            if include_user_instructions
            else instructions.strip() + "\n"
        )
        content = "developer_instructions = " + json.dumps(
            composed, ensure_ascii=False
        ) + (
            "\nsuppress_unstable_features_warning = true"
            "\n\n[features]\nterminal_visualization_instructions = true\n"
        )

        self._ensure_private_directory(self.profiles_dir)
        self._ensure_private_directory(self.registrations_dir)
        self._ensure_private_directory(output.parent)
        atomic_write_text(profile_path, content, mode=0o600)

        self._ensure_private_directory(self.codex_home)
        registration = self.codex_home / f"{profile_name}.config.toml"
        if os.path.lexists(registration):
            raise ValidationError(f"unique Codex profile registration already exists: {registration}")
        registration.symlink_to(profile_path.resolve())

        start_time = _process_start_time(os.getpid())
        if start_time is None:
            registration.unlink(missing_ok=True)
            profile_path.unlink(missing_ok=True)
            raise ValidationError("cannot determine wrapper process start time")
        registry = self.registrations_dir / f"{launch_id}.json"
        atomic_write_json(
            registry,
            {
                "version": 1,
                "launch_id": launch_id,
                "key": key,
                "uid": os.getuid(),
                "pid": os.getpid(),
                "process_start_time": start_time,
                "profile": str(profile_path),
                "registration": str(registration),
                "generated": str(output.parent),
            },
            mode=0o600,
        )
        self._leases[profile_name] = launch_id
        return profile_name, profile_path

    def _registration_is_active(self, value: dict[str, object]) -> bool:
        pid = value.get("pid")
        start_time = value.get("process_start_time")
        return (
            isinstance(pid, int)
            and isinstance(start_time, str)
            and _process_start_time(pid) == start_time
        )

    def _remove_registration(self, registry: Path, value: dict[str, object], *, force: bool) -> bool:
        launch_id = value.get("launch_id")
        if not isinstance(launch_id, str) or registry.name != f"{launch_id}.json":
            return False
        if value.get("uid") != os.getuid() or (not force and self._registration_is_active(value)):
            return False
        expected_profile = self.profiles_dir / f"{launch_id}.config.toml"
        registration_value = value.get("registration")
        if value.get("profile") != str(expected_profile) or not isinstance(
            registration_value, str
        ):
            return False
        expected_registration = Path(registration_value)
        if (
            not expected_registration.is_absolute()
            or expected_registration.name != f"{self.namespace}-{launch_id}.config.toml"
        ):
            return False
        try:
            registration_parent = expected_registration.parent.lstat()
        except OSError:
            return False
        if not stat.S_ISDIR(registration_parent.st_mode) or registration_parent.st_uid != os.getuid():
            return False
        if os.path.lexists(expected_registration):
            if not expected_registration.is_symlink():
                return False
            if expected_registration.resolve(strict=False) != expected_profile.resolve():
                return False
            expected_registration.unlink()
        expected_profile.unlink(missing_ok=True)
        generated = Path(str(value.get("generated", ""))).resolve()
        generated_root = (
            self.workspace.generated_dir / "clients" / "codex" / "launches"
        ).resolve()
        try:
            generated.relative_to(generated_root)
        except ValueError:
            return False
        if generated.exists():
            shutil.rmtree(generated)
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

    def release_profile(self, profile_name: str) -> None:
        launch_id = self._leases.pop(profile_name, None)
        if launch_id is None:
            return
        registry = self.registrations_dir / f"{launch_id}.json"
        try:
            value = json.loads(registry.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(value, dict):
            self._remove_registration(registry, value, force=True)

    def build_task_profile(self, task_id: str, *, session_note: str = "") -> tuple[str, Path]:
        standard = load_task_types(self.workspace.tasks_dir)
        resolve_task(task_id, standard)
        launch_id = uuid.uuid4().hex
        output = (
            self.workspace.generated_dir
            / "clients"
            / "codex"
            / "launches"
            / launch_id
            / "developer-instructions.md"
        )
        result = render_task_view(
            self.workspace,
            task_id,
            output_path=output,
            write_manifest=False,
        )
        content = str(result["content"])
        if session_note.strip():
            content += (
                "\n---\n\n# Confirmed routing note for this session\n\n"
                + session_note.strip()
                + "\n"
            )
        return self._register_profile(task_id, content, output)

    def _build_annotation_profile(self) -> tuple[str, Path]:
        launch_id = uuid.uuid4().hex
        output = (
            self.workspace.generated_dir
            / "clients"
            / "codex"
            / "launches"
            / launch_id
            / "developer-instructions.md"
        )
        instructions = """# Annotation preflight classifier

Perform only the semantic classification supplied in the user prompt. Use the prompt as the sole
source of evidence. Do not inspect files, generated artifacts, old labels, records outside the
prompt, or live annotation matrices. Do not call tools or shell commands and do not edit files.
Return only the JSON required by the supplied output schema. The parent launcher owns proposal
construction, validation, and atomic application.
"""
        self.workspace.ensure_private_runtime_directory(output.parent)
        atomic_write_text(output, instructions, mode=0o600)
        return self._register_profile(
            "annotation-preflight",
            instructions,
            output,
            include_user_instructions=False,
        )

    def _run_annotation_classifier(
        self,
        profile_name: str,
        corpus: str,
        output_schema: dict[str, object],
    ) -> tuple[int, dict[str, object] | None]:
        transaction = self.workspace.generated_dir / "annotation-preflight" / uuid.uuid4().hex
        self.workspace.ensure_private_runtime_directory(transaction)
        schema_path = transaction / "output-schema.json"
        result_path = transaction / "result.json"
        log_path = transaction / "codex.log"
        atomic_write_json(schema_path, output_schema, mode=0o600)
        command = [
            self.executable,
            "--profile",
            profile_name,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "-C",
            str(self.workspace.root),
            "-s",
            "workspace-write",
            "--output-schema",
            str(schema_path),
            "-o",
            str(result_path),
            "--color",
            "never",
            "-",
        ]
        environment = os.environ.copy()
        environment["MEMORY_WORKSPACE"] = str(self.workspace.root)
        environment["MEMORY_FRAMEWORK_ROOT"] = str(self.workspace.framework_root)
        environment.pop("MEMORY_FRAMEWORK_HANDOFF", None)
        try:
            with log_path.open("w", encoding="utf-8") as log:
                status = subprocess.run(
                    command,
                    input=corpus,
                    text=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    cwd=self.launch_cwd,
                    check=False,
                ).returncode
        except OSError as exc:
            raise MemoryFrameworkError(
                f"cannot launch annotation Codex executable {self.executable!r}: {exc}"
            ) from exc
        if status != 0 or not result_path.is_file():
            return status, None
        try:
            value = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MemoryFrameworkError(f"annotation Codex returned invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise MemoryFrameworkError("annotation Codex result must be a JSON object")
        return status, value

    def ensure_task_annotations(self, task_id: str, *, max_attempts: int = 3) -> None:
        for attempt in range(1, max_attempts + 1):
            try:
                preflight = prepare_annotation_preflight(self.workspace, task_id)
            except ValidationError:
                if attempt == max_attempts:
                    raise
                continue
            if not preflight.issues:
                return
            print(
                f"Memory annotation preflight for {task_id}: "
                f"{len(preflight.issues)} pending/stale cells (attempt {attempt}/{max_attempts}).",
                file=sys.stderr,
                flush=True,
            )
            if preflight.requires_classification:
                profile_name, _ = self._build_annotation_profile()
                try:
                    status, result = self._run_annotation_classifier(
                        profile_name,
                        preflight.corpus,
                        preflight.output_schema,
                    )
                finally:
                    self.release_profile(profile_name)
                if status != 0:
                    raise MemoryFrameworkError(
                        f"annotation Codex exited with status {status}; task session was not started"
                    )
                if result is None:
                    raise MemoryFrameworkError(
                        "annotation Codex produced no structured result; task session was not started"
                    )
                try:
                    record_classifier_result(preflight, result)
                except (ValidationError, TypeError, KeyError) as exc:
                    raise MemoryFrameworkError(
                        f"annotation Codex result failed validation: {exc}"
                    ) from exc
            try:
                apply_annotation_preflight(self.workspace, preflight)
            except PublicationError:
                if attempt == max_attempts:
                    raise
                continue
            _, remaining = relevant_task_issues(self.workspace, task_id)
            if not remaining:
                return
        raise MemoryFrameworkError(
            f"annotation preflight did not converge for {task_id}; task session was not started"
        )

    @contextmanager
    def _preferences_lock(self) -> Iterator[None]:
        self._ensure_private_directory(self.preferences_lock_path.parent)
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
            hints = [item for item in entry.get("directory_hints", []) if isinstance(item, str)]
            if directory_hint and directory_hint not in hints:
                hints.append(directory_hint)
            entry["directory_hints"] = hints[-12:]
            summaries = [
                item for item in entry.get("intent_summaries", []) if isinstance(item, str)
            ]
            if remember_similar and normalized_intent and normalized_intent not in summaries:
                summaries.append(normalized_intent)
            entry["intent_summaries"] = summaries[-12:]
            entry["auto_similar"] = bool(remember_similar and normalized_intent)
            entries[task_id] = entry
            atomic_write_json(self.preferences_path, preferences, mode=0o600)

    def build_auto_profile(
        self,
        handoff_path: Path,
        *,
        nonce: str,
        continuation: bool,
        ephemeral: bool = False,
    ) -> tuple[str, Path]:
        if ephemeral:
            handoff_contract = (
                "This is an ephemeral resume. Complete it in Auto and do not create a task "
                "handoff under any circumstance."
            )
        elif continuation:
            handoff_contract = (
                "Only if this continuation must proceed under one standard task, write exactly "
                "one v2 handoff with fields `version: 2`, `nonce`, `mode: task`, `kind: continue`, "
                "`task`, and the current canonical `session_id`. Do not repeat the request."
            )
        else:
            handoff_contract = (
                "Only if this new request must continue under one standard task, write exactly "
                "one v2 handoff with fields `version: 2`, `nonce`, `mode: task`, `kind: new`, "
                "`task`, and the original user `request` preserved verbatim."
            )
        instructions = (
            "The handoff is an optional request to start a second Codex process, never a "
            "completion receipt. Create no file when Auto answers or completes the work. "
            + handoff_contract
            + " On any future resumed or forked turn, the developer instructions retained by "
            "Codex may still contain an older embedded path and nonce. In that case, ignore the "
            "embedded values and run `printenv MEMORY_FRAMEWORK_HANDOFF` to read the current "
            "JSON contract from the process environment; its `path`, `nonce`, `kind`, and "
            "`ephemeral` fields supersede every earlier handoff value. If that variable is absent, "
            "do not invent a continuation contract. "
            + " The task must be a defined standard task, never `all-lazy` or `all-eager`. "
            "Optional fields are boolean `remember`, a user-confirmed `normalized_intent`, "
            "`directory_hint`, and a session-only `memory_preference`. Use the exact path and "
            "nonce below, create a regular owner-only file, and then tell the user to exit. "
            "Direct memory administration stays in Auto. Never write raw prompts to preferences.\n\n"
            f"HANDOFF_PATH={handoff_path} HANDOFF_NONCE={nonce}"
        )
        launch_id = uuid.uuid4().hex
        output = (
            self.workspace.generated_dir
            / "clients"
            / "codex"
            / "launches"
            / launch_id
            / "developer-instructions.md"
        )
        result = render_auto_view(
            self.workspace,
            preferences=self.load_preferences(),
            extra_instructions=instructions,
            output_path=output,
            write_manifest=False,
        )
        return self._register_profile("auto", str(result["content"]), output)

    def run_routing(
        self,
        profile_name: str,
        arguments: list[str],
        *,
        handoff_contract: str | None = None,
    ) -> int:
        command = [self.executable, "--profile", profile_name, *arguments]
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
                f"cannot launch Codex executable {self.executable!r}: {exc}"
            ) from exc

    def replace_with_task(self, profile_name: str, arguments: list[str]) -> None:
        command = [self.executable, "--profile", profile_name, *arguments]
        executable = self.executable
        environment = os.environ.copy()
        environment["MEMORY_WORKSPACE"] = str(self.workspace.root)
        environment["MEMORY_FRAMEWORK_ROOT"] = str(self.workspace.framework_root)
        environment.pop("MEMORY_FRAMEWORK_HANDOFF", None)
        os.chdir(self.launch_cwd)
        try:
            if os.sep not in executable:
                os.execvpe(executable, command, environment)
            os.execve(executable, command, environment)
        except OSError as exc:
            raise MemoryFrameworkError(
                f"cannot launch Codex executable {self.executable!r}: {exc}"
            ) from exc
