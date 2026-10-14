from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from claude.adapter import ClaudeAdapter
from agy.terminal import LaunchCancelled, terminal_streams
from memory_framework.errors import MemoryFrameworkError, ValidationError
from memory_framework.preflight import ANNOTATION_CLASSIFIER_INSTRUCTIONS
from memory_framework.rendering import render_auto_view
from memory_framework.workspace import atomic_write_json, atomic_write_text


def default_executable() -> str:
    return os.environ.get("MEMORY_AGY_BIN") or shutil.which("agy") or str(Path.home() / ".local/bin/agy")


# Reuse the existing prompt leases, rendering and preference implementation.
# Launching and agent discovery are entirely specific to Antigravity.
class AgyAdapter(ClaudeAdapter):
    client_id = "agy"
    client_name = "agy"
    accept_limitations = False
    tools = (
        "view_file", "list_dir", "find_by_name", "grep_search",
        "run_command", "manage_task", "write_to_file", "replace_file_content",
        "multi_replace_file_content", "search_web", "read_url_content",
    )

    def __init__(self, workspace, *, executable=None, **kwargs):
        super().__init__(
            workspace, executable=executable or default_executable(),
            **kwargs,
        )
        self.agent_root = Path(os.environ.get(
            "MEMORY_AGY_AGENT_ROOT", str(Path.home() / ".gemini/config/agents")
        )).expanduser().resolve()

    def validate_launch_arguments(self, arguments: list[str]) -> None:
        # Parse option values: a prompt whose text is '--agent' is still a prompt.
        from agy.cli import native_options
        for name, _ in native_options(arguments):
            if name == "--agent":
                raise ValidationError("memory-agy owns --agent; use --raw for a custom agent")
            if name == "--remote-control":
                raise ValidationError("remote-control Memory lifetime is unverified; use --raw explicitly")

    def task_label(self, mode: str) -> str:
        labels = {
            "all-lazy": "All memories, read when needed",
            "all-eager": "All memories, loaded at startup",
            "auto": "Choose memories automatically",
        }
        if mode in labels:
            return labels[mode]
        return self.available_tasks()[mode].title

    def confirm_launch(self, mode: str, *, continuation: bool) -> None:
        message = (
            f"\nStart agy with saved memory: {self.task_label(mode)}\n"
            "  This mode does not include your connected services or other AI helpers.\n"
        )
        if continuation:
            message += (
                "  Resuming a conversation may keep its old memories despite your selection.\n"
                "  Start a new conversation to use changed memories or a different task.\n"
            )
        details = (
            "\nHow this mode works:\n"
            "  A temporary custom main agent receives the full rendered Memory view and\n"
            "  standalone global/ancestor AGENTS.md and GEMINI.md instructions.\n"
            "  It provides file, shell and web tools, with agy's normal tool approval.\n"
            "  Custom-agent tools, MCP integrations and subagent orchestration are not inherited.\n"
            "  Native rule-file loading limits are separate from this injected prompt;\n"
            "  the adapter does not truncate Memory to fit those limits.\n"
            "  Memory instructions stay in the system prompt when conversation history is compacted.\n"
            "  Lazy record bodies can be read again with the Memory loader after compaction.\n"
            "  agy prepares any pending/stale memory annotations before starting your task.\n"
        )
        if continuation:
            details += "  On resume, agy may reuse the original agent prompt rather than this launch's prompt.\n"
        if self.accept_limitations:
            print(message + "Accepted for this invocation via --accept-limitations.", file=sys.stderr)
            return
        try:
            with terminal_streams() as (input_stream, output_stream):
                prompt = "Start session? [y/N] (? for details): "
                output_stream.write(message + prompt)
                output_stream.flush()
                while True:
                    answer = input_stream.readline()
                    if not answer:
                        raise LaunchCancelled("No confirmation received. Launch cancelled.")
                    answer = answer.strip().lower()
                    if answer in {"y", "yes"}:
                        return
                    if answer in {"", "n", "no"}:
                        raise LaunchCancelled("Launch cancelled.")
                    if answer == "?":
                        output_stream.write(details + "\n" + prompt)
                    else:
                        output_stream.write("Please enter y to start, n to cancel, or ? for details: ")
                    output_stream.flush()
        except OSError as exc:
            print(message, file=sys.stderr)
            command = "agy --auto" if mode == "auto" else f"agy --task {mode}"
            raise ValidationError(
                "Could not read the startup confirmation from a terminal.\n"
                "Run agy in an interactive terminal to answer the question.\n"
                "For a script, explicitly accept the listed limitations with:\n"
                f"  {command} --accept-limitations\n"
                "The model has not been started for this launch."
            ) from exc

    def _workspace_instructions(self) -> str:
        parts = []
        files = [
            Path.home() / ".gemini" / name
            for name in ("AGENTS.md", "GEMINI.md", "config/AGENTS.md", "config/GEMINI.md")
        ]
        paths = [self.effective_cwd, *self.effective_cwd.parents]
        for directory in reversed(paths):
            for name in ("AGENTS.md", "GEMINI.md"):
                files.append(directory / name)
        for path in dict.fromkeys(files):
            if path.is_file():
                parts.append(f"## Instructions from {path}\n\n" + path.read_text(encoding="utf-8"))
        return "\n\n".join(parts)

    def build_auto_prompt(self, handoff_path: Path, *, nonce: str, continuation: bool) -> Path:
        contract = (
            "Complete this continuation in Auto; do not create a handoff."
            if continuation else
            "Only if a standard task is needed, create one owner-only v2 handoff: "
            "version=2, nonce, mode=task, kind=new, task, and the original request verbatim. "
            "Optional fields: remember, normalized_intent, directory_hint, memory_preference. "
            "Do not select all-lazy or all-eager. Then ask the user to exit this CLI."
        )
        output = self._new_launch_path()
        self._register_prompt(output)
        try:
            render_auto_view(
                self.workspace, preferences=self.load_preferences(),
                extra_instructions=(
                    "A handoff requests a second Antigravity session, not a completion receipt. "
                    "Create no handoff when Auto completes the request. Memory administration stays in Auto. "
                    + contract + f"\nHANDOFF_PATH={handoff_path} HANDOFF_NONCE={nonce}"
                ), output_path=output, write_manifest=False,
            )
        except BaseException:
            self.release_prompt(output)
            raise
        return output

    def _install_agent(self, prompt_path: Path, *, classifier: bool = False) -> str:
        launch_id = self._leases[prompt_path.resolve()]
        name = "memory-" + launch_id
        directory = self.agent_root / name
        path = directory / "agent.md"
        header = {
            "name": name, "description": "Task-bound Memory Framework session.",
            "mainAgent": True, "subagent": False,
            "tools": [] if classifier else list(self.tools), "inheritMcp": False,
            "commandExecutionPolicy": "off",
        }
        # JSON mappings are valid YAML frontmatter; no escaping or implicit YAML values.
        body = prompt_path.read_text(encoding="utf-8")
        if not classifier:
            body = self._workspace_instructions() + "\n\n" + body
            body += (
                "\n\nBefore working in a subdirectory, read its applicable AGENTS.md/GEMINI.md. "
                "Use the configured Memory loader for lazy records; do not infer their bodies.\n"
            )
        text = "---\n" + json.dumps(header, ensure_ascii=False) + "\n---\n\n" + body
        registry = self.registrations_dir / f"{launch_id}.json"
        value = json.loads(registry.read_text(encoding="utf-8"))
        value.update(agy_agent=str(path), agy_sha256=hashlib.sha256(text.encode()).hexdigest())
        # Register before publishing so an interrupted launch can be reclaimed.
        atomic_write_json(registry, value, mode=0o600)
        self.agent_root.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=0o700)
        atomic_write_text(path, text, mode=0o600)
        return name

    def _classify_annotations(self, corpus, output_schema):
        prompt = self._new_launch_path()
        self._register_prompt(prompt)
        try:
            atomic_write_text(
                prompt,
                ANNOTATION_CLASSIFIER_INSTRUCTIONS
                + "\n# Required output schema\n```json\n"
                + json.dumps(output_schema) + "\n```\n\n" + corpus,
                mode=0o600,
            )
            name = self._install_agent(prompt, classifier=True)
            with tempfile.TemporaryDirectory(prefix="memory-agy-annotations-") as directory:
                command = [
                    self.executable, "--agent", name, "--output-format", "json",
                    "--disable-slash-commands", "--print",
                    "Classify the supplied annotation corpus against the required output schema. "
                    "Return exactly one JSON object, with no other text.",
                ]
                try:
                    completed = subprocess.run(
                        command, text=True, capture_output=True, cwd=directory,
                        env=self._annotation_environment(), check=False,
                    )
                except OSError as exc:
                    raise MemoryFrameworkError(f"cannot launch annotation agy: {exc}") from exc
                return self._annotation_result(completed)
        finally:
            self.release_prompt(prompt)

    def _remove_registration(self, registry, value, *, force):
        path = None
        if value.get("agy_agent"):
            expected = self.agent_root / ("memory-" + str(value.get("launch_id"))) / "agent.md"
            if value["agy_agent"] != str(expected) or expected.parent.is_symlink():
                return False
            path = expected
            if path.exists() or path.is_symlink():
                metadata = path.lstat()
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                        or metadata.st_nlink != 1
                        or hashlib.sha256(path.read_bytes()).hexdigest() != value.get("agy_sha256")):
                    return False
        if not super()._remove_registration(registry, value, force=force):
            return False
        if path is not None:
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass  # Never remove someone else's additional files.
        return True

    def run_with_prompt(self, prompt_path, arguments, *, handoff_contract=None) -> int:
        try:
            name = self._install_agent(prompt_path)
        except (OSError, UnicodeDecodeError) as exc:
            raise MemoryFrameworkError(f"cannot prepare Antigravity agent: {exc}") from exc
        environment = os.environ.copy()
        environment["MEMORY_AGY_ACTIVE"] = "1"
        environment["MEMORY_WORKSPACE"] = str(self.workspace.root)
        environment["MEMORY_FRAMEWORK_ROOT"] = str(self.workspace.framework_root)
        if handoff_contract is None:
            environment.pop("MEMORY_FRAMEWORK_HANDOFF", None)
        else:
            environment["MEMORY_FRAMEWORK_HANDOFF"] = handoff_contract
        try:
            return subprocess.run(
                [self.executable, "--agent", name, *arguments],
                cwd=self.launch_cwd, env=environment, check=False,
            ).returncode
        except OSError as exc:
            raise MemoryFrameworkError(f"cannot launch Antigravity: {exc}") from exc
