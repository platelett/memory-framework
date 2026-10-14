#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path


FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FRAMEWORK_ROOT / "lib"))
sys.path.insert(0, str(FRAMEWORK_ROOT / "clients"))

from base import load_handoff  # noqa: E402
from agy.adapter import AgyAdapter, default_executable  # noqa: E402
from agy.snapshot import export_snapshot  # noqa: E402
from codex.adapter import CodexAdapter  # noqa: E402
from memory_framework.errors import MemoryFrameworkError, ValidationError  # noqa: E402
from memory_framework.workspace import Workspace, discover_workspace  # noqa: E402


UTILITY_COMMANDS = {
    "agent", "agents", "changelog", "help", "install", "mcp", "mic-serve",
    "models", "plugin", "plugins", "remote-control", "update",
}
VALUE_OPTIONS = {
    "--add-dir", "--agent", "--conversation", "--effort", "--input-format",
    "--json-schema", "--log-file", "--mode", "--model", "--new-project",
    "--output-format", "--print", "-p", "--print-timeout", "--project",
    "--prompt", "--prompt-interactive", "-i",
}
PROMPT_OPTIONS = {"--print", "-p", "--prompt", "--prompt-interactive", "-i"}


def native_options(arguments):
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item == "--":
            return
        if item.startswith("-"):
            name, equal, value = item.partition("=")
            if not equal and name in VALUE_OPTIONS:
                index += 1
                if index >= len(arguments):
                    raise ValidationError(f"{name} requires a value")
                value = arguments[index]
            yield name, value
        index += 1


@dataclass(frozen=True)
class MemoryArguments:
    remaining: list[str]
    raw: bool = False
    auto: bool = False
    task: str | None = None
    list_tasks: bool = False
    snapshot: tuple[str, str] | None = None
    include_local: bool = False
    accept_limitations: bool = False


def _parse_memory_arguments(argv: list[str]) -> MemoryArguments:
    remaining: list[str] = []
    raw = auto = list_tasks = include_local = accept_limitations = False
    task: str | None = None
    snapshot: tuple[str, str] | None = None
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            remaining.extend(argv[index:])
            break
        if argument == "--accept-limitations":
            accept_limitations = True
        elif argument == "--raw":
            raw = True
        elif argument == "--auto":
            auto = True
        elif argument == "--list":
            list_tasks = True
        elif argument == "--include-local":
            include_local = True
        elif argument == "--task" or argument.startswith("--task="):
            if argument == "--task":
                index += 1
                if index >= len(argv):
                    raise ValidationError("--task requires a task ID")
                task = argv[index]
            else:
                task = argument.split("=", 1)[1]
            if not task:
                raise ValidationError("--task requires a task ID")
        elif argument == "--snapshot":
            if index + 2 >= len(argv):
                raise ValidationError("--snapshot requires TASK and OUTPUT_DIR")
            snapshot = (argv[index + 1], argv[index + 2])
            index += 2
        else:
            remaining.extend(argv[index:])
            break
        index += 1
    modes = sum(bool(item) for item in (raw, auto, task, list_tasks, snapshot))
    if modes > 1:
        raise ValidationError(
            "choose only one of --raw, --auto, --task, --list, or --snapshot"
        )
    if include_local and snapshot is None:
        raise ValidationError("--include-local is valid only with --snapshot")
    return MemoryArguments(
        remaining=remaining,
        raw=raw,
        auto=auto,
        task=task,
        list_tasks=list_tasks,
        snapshot=snapshot,
        include_local=include_local,
        accept_limitations=accept_limitations,
    )


def _command_index(arguments: list[str]) -> int | None:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return index + 1 if index + 1 < len(arguments) else None
        if argument in VALUE_OPTIONS:
            index += 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return index
    return None


def _is_utility(arguments: list[str]) -> bool:
    if arguments and arguments[0] in UTILITY_COMMANDS:
        return True
    return any(name in {"-h", "--help", "--version"} for name, _ in native_options(arguments))


def _unsupported_memory_operation(arguments: list[str]) -> str | None:
    if arguments and arguments[0] in UTILITY_COMMANDS:
        return arguments[0] + " is a utility; invoke it without --task/--auto"
    return None


def _is_continuation(arguments: list[str]) -> bool:
    return any(name in {"-c", "--continue", "--conversation"} for name, _ in native_options(arguments))


def _extract_new_request(arguments: list[str]) -> str | None:
    return next((value for name, value in native_options(arguments) if name in PROMPT_OPTIONS), None)


def _new_task_arguments(arguments: list[str], request: str) -> list[str]:
    if _extract_new_request(arguments) is not None:
        return list(arguments)
    return [*arguments, "--prompt-interactive", request] if request else list(arguments)


def _exec_passthrough(arguments: list[str]) -> None:
    executable = default_executable()
    command = [executable, *arguments]
    environment = os.environ.copy()
    environment["MEMORY_AGY_ACTIVE"] = "1"
    environment.pop("MEMORY_FRAMEWORK_HANDOFF", None)
    try:
        if os.sep not in executable:
            os.execvpe(executable, command, environment)
        os.execve(executable, command, environment)
    except OSError as exc:
        raise MemoryFrameworkError(
            f"cannot launch Antigravity executable {executable!r}: {exc}"
        ) from exc


def _list(adapter: AgyAdapter) -> None:
    for task_id, task in adapter.available_tasks().items():
        marker = "built-in" if task.built_in else "standard"
        print(f"{task_id}\t{marker}\t{task.title}")


def _interactive_choice(adapter: AgyAdapter) -> tuple[str, str]:
    tasks = list(adapter.available_tasks().items())
    opened_terminal = None
    try:
        if sys.stdin.isatty():
            input_stream = sys.stdin
            output_stream = sys.stdout if sys.stdout.isatty() else sys.stderr
        else:
            try:
                opened_terminal = open("/dev/tty", "r+", encoding="utf-8", buffering=1)
            except OSError as exc:
                raise ValidationError(
                    "memory task selection requires a terminal; use --task TASK or --auto"
                ) from exc
            input_stream = output_stream = opened_terminal
        print("Select a memory task type:", file=output_stream)
        for index, (task_id, task) in enumerate(tasks, start=1):
            marker = "built-in" if task.built_in else "standard"
            print(f"  {index}. {task_id} [{marker}] - {task.title}", file=output_stream)
        print(
            f"  {len(tasks) + 1}. auto - automatic routing and memory administration",
            file=output_stream,
        )
        output_stream.write("Task: ")
        output_stream.flush()
        choice = input_stream.readline().strip()
        if not choice:
            raise ValidationError("memory task selection ended without a choice")
    finally:
        if opened_terminal is not None:
            opened_terminal.close()
    if choice == "auto":
        return "auto", ""
    if choice.isdigit():
        number = int(choice)
        if number == len(tasks) + 1:
            return "auto", ""
        if 1 <= number <= len(tasks):
            return "task", tasks[number - 1][0]
    if choice in dict(tasks):
        return "task", choice
    raise ValidationError(f"unknown selection: {choice}")


def _run_prompt(
    adapter: AgyAdapter, prompt_path: Path, arguments: list[str]
) -> None:
    try:
        status = adapter.run_with_prompt(prompt_path, arguments)
    finally:
        adapter.release_prompt(prompt_path)
    if status != 0:
        raise MemoryFrameworkError(f"Antigravity exited with status {status}")


def _run_selected_task(
    adapter: AgyAdapter, task_id: str, arguments: list[str]
) -> None:
    if task_id not in adapter.available_tasks():
        raise ValidationError(f"unknown Memory task: {task_id}")
    unsupported = _unsupported_memory_operation(arguments)
    if unsupported is not None:
        raise ValidationError(
            f"cannot verify task-memory injection for {unsupported}; "
            "use --raw only for an explicit memory bypass"
        )
    adapter.confirm_launch(task_id, continuation=_is_continuation(arguments))
    CodexAdapter(
        adapter.workspace,
        launch_cwd=adapter.launch_cwd,
        effective_cwd=adapter.effective_cwd,
    ).ensure_task_annotations(task_id)
    prompt_path = adapter.build_task_prompt(task_id)
    if task_id in adapter.available_routing_tasks():
        adapter.remember_selection(task_id, directory_hint=str(adapter.effective_cwd))
    _run_prompt(adapter, prompt_path, arguments)


def _run_auto(adapter: AgyAdapter, arguments: list[str]) -> None:
    continuation = _is_continuation(arguments)
    adapter.confirm_launch("auto", continuation=continuation)
    if any(name == "--input-format" and value == "stream-json" for name, value in native_options(arguments)):
        raise ValidationError("Auto handoff does not support stream-json input; select --task instead")
    handoff_dir = adapter.workspace.state_dir / "handoffs"
    adapter.workspace.ensure_private_runtime_directory(handoff_dir)
    handoff = handoff_dir / f"{uuid.uuid4().hex}.json"
    nonce = uuid.uuid4().hex
    prompt_path = adapter.build_auto_prompt(
        handoff,
        nonce=nonce,
        continuation=continuation,
    )
    resume_contract = None
    if continuation:
        resume_contract = json.dumps(
            {
                "version": 1,
                "path": str(handoff),
                "nonce": nonce,
                "kind": "continue",
                "ephemeral": True,
            },
            separators=(",", ":"),
        )
    try:
        status = adapter.run_with_prompt(
            prompt_path,
            arguments,
            handoff_contract=resume_contract,
        )
    finally:
        adapter.release_prompt(prompt_path)
    if status != 0:
        raise MemoryFrameworkError(f"routing Antigravity exited with status {status}")
    if not os.path.lexists(handoff):
        return
    try:
        if continuation:
            raise ValidationError(
                "Antigravity Auto continuation cannot create a task handoff"
            )
        data = load_handoff(
            handoff,
            expected_nonce=nonce,
            expected_kind="new",
            expected_request=_extract_new_request(arguments),
        )
    finally:
        handoff.unlink(missing_ok=True)
    task_id = str(data["task"])
    if task_id not in adapter.available_routing_tasks():
        raise ValidationError(
            f"Auto handoff must select a standard task, not a built-in or unknown task: {task_id}"
        )
    adapter.confirm_launch(task_id, continuation=False)
    adapter.remember_selection(
        task_id,
        directory_hint=str(data.get("directory_hint") or adapter.effective_cwd),
        normalized_intent=str(data.get("normalized_intent") or "") or None,
        remember_similar=bool(data.get("remember", False)),
    )
    CodexAdapter(
        adapter.workspace,
        launch_cwd=adapter.launch_cwd,
        effective_cwd=adapter.effective_cwd,
    ).ensure_task_annotations(task_id)
    final_prompt = adapter.build_task_prompt(
        task_id,
        session_note=str(data.get("memory_preference") or ""),
    )
    final_arguments = _new_task_arguments(arguments, str(data["request"]))
    _run_prompt(adapter, final_prompt, final_arguments)


def main(argv: list[str] | None = None) -> int:
    try:
        memory = _parse_memory_arguments(list(sys.argv[1:] if argv is None else argv))
        if memory.raw or (
            not any((memory.auto, memory.task, memory.list_tasks, memory.snapshot))
            and _is_utility(memory.remaining)
        ):
            _exec_passthrough(memory.remaining)
            return 0

        os.umask(0o077)
        workspace = Workspace(discover_workspace())
        process_cwd = Path.cwd().resolve()
        adapter = AgyAdapter(
            workspace,
            launch_cwd=process_cwd,
            effective_cwd=process_cwd,
        )
        adapter.accept_limitations = memory.accept_limitations
        if memory.remaining and not memory.remaining[0].startswith("-") and memory.remaining[0] not in UTILITY_COMMANDS:
            memory.remaining[:1] = ["--prompt-interactive", memory.remaining[0]]
        adapter.validate_launch_arguments(memory.remaining)
        adapter.cleanup_registrations()
        if memory.list_tasks:
            if memory.remaining:
                raise ValidationError(
                    f"unexpected arguments for --list: {' '.join(memory.remaining)}"
                )
            _list(adapter)
            return 0
        if memory.snapshot:
            if memory.remaining:
                raise ValidationError(
                    f"unexpected arguments for --snapshot: {' '.join(memory.remaining)}"
                )
            task_id, output = memory.snapshot
            print(
                export_snapshot(
                    workspace, task_id, output, include_local=memory.include_local
                )
            )
            return 0
        if memory.task:
            _run_selected_task(adapter, memory.task, memory.remaining)
            return 0
        unsupported = _unsupported_memory_operation(memory.remaining)
        if not memory.auto:
            mode, selection = _interactive_choice(adapter)
            if mode == "task":
                _run_selected_task(adapter, selection, memory.remaining)
                return 0
        if unsupported is not None:
            raise ValidationError(
                f"cannot verify Auto-memory injection for {unsupported}; "
                "use --raw only for an explicit memory bypass"
            )
        _run_auto(adapter, memory.remaining)
    except MemoryFrameworkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
