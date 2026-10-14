from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from claude.adapter import ClaudeAdapter
from agy.terminal import LaunchCancelled, terminal_streams
from memory_framework.errors import MemoryFrameworkError, ValidationError
from memory_framework.rendering import render_auto_view
from memory_framework.workspace import atomic_write_json, atomic_write_text


def default_executable() -> str:
    return os.environ.get("MEMORY_AGY_BIN") or shutil.which("agy") or str(Path.home() / ".local/bin/agy")


# Reuse the existing prompt leases, rendering and preference implementation.
# Launching and agent discovery are entirely specific to Antigravity.
class AgyAdapter(ClaudeAdapter):
    client_id = "agy"
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

    def confirm_launch(self, mode: str, *, continuation: bool) -> None:
        message = (
            f"Memory / Antigravity startup: {mode}\n"
            "  A generated main agent supplies Memory and basic coding/web tools.\n"
            "  Custom agents, MCP tools and subagent orchestration are not inherited.\n"
            "  AGENTS.md/GEMINI.md on the current directory's ancestry are included;\n"
            "  other native rules retain agy's discovery and size limits:\n"
            "  24,000 bytes per rule file / 20,000 tokens total (truncation or lazy demotion).\n"
            "  Long-context/compaction retention is not yet verified.\n"
            "  Pending/stale annotations use the existing Codex classifier before launch.\n"
        )
        if continuation:
            message += (
                "  RESUME: agy may keep the original agent prompt and old Memory.\n"
                "  The selection above cannot guarantee replacement of that context.\n"
                "  Use the original task; start a new session for revised Memory.\n"
            )
        if self.accept_limitations:
            print(message + "Accepted for this invocation via --accept-limitations.", file=sys.stderr)
            return
        try:
            with terminal_streams() as (input_stream, output_stream):
                output_stream.write(message + "Continue with these limitations? [y/N]: ")
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
                    output_stream.write("Please enter y or n (Enter cancels): ")
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

    def _install_agent(self, prompt_path: Path) -> str:
        launch_id = self._leases[prompt_path.resolve()]
        name = "memory-" + launch_id
        directory = self.agent_root / name
        path = directory / "agent.md"
        header = {
            "name": name, "description": "Task-bound Memory Framework session.",
            "mainAgent": True, "subagent": False, "tools": list(self.tools),
            "commandExecutionPolicy": "off",
        }
        # JSON mappings are valid YAML frontmatter; no escaping or implicit YAML values.
        body = self._workspace_instructions() + "\n\n" + prompt_path.read_text(encoding="utf-8")
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
