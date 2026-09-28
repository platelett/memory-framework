#!/usr/bin/env python3
"""Exercise one rendered Policy constraint against a conflicting user request."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(REPOSITORY / "lib"),
    str(REPOSITORY / "clients"),
]

from codex.adapter import CodexAdapter  # noqa: E402
from memory_framework.annotations import task_annotation_hash, write_matrix  # noqa: E402
from memory_framework.parsing import load_records, load_task_types  # noqa: E402
from memory_framework.workspace import Workspace, initialize_bank  # noqa: E402


APPLIED = "MEMORY_POLICY_APPLIED"
IGNORED = "MEMORY_POLICY_IGNORED"


def _write_fixture(root: Path) -> Workspace:
    initialize_bank(root)
    memory = root / ".memory"
    for directory in (
        "records/policy/shared",
        "records/policy/local",
        "records/reference",
        "records/playbook",
        "categories/reference",
        "categories/playbook",
        "task-types",
        "annotations",
        "tools",
    ):
        (memory / directory).mkdir(parents=True, exist_ok=True)
    (memory / "task-types" / "acceptance.md").write_text(
        """+++
id = "acceptance"
title = "Memory adherence acceptance"
routing_hints = ["acceptance marker"]
+++

## Scope

Verify that an activated Policy changes the model's answer.

## Boundary

Exclude all production work.

## Positive examples

- Choose the requested acceptance marker.

## Negative examples

- Modify a workspace.
""",
        encoding="utf-8",
    )
    (memory / "records" / "policy" / "shared" / "choose-policy-marker.md").write_text(
        f"""+++
id = "policy.shared.choose-policy-marker"
title = "Choose the policy acceptance marker"
layer = "policy"
category = "shared"
load_when = "Read when an acceptance request asks which validation marker to return."
+++

When an acceptance request asks for a validation marker, return `{APPLIED}` even if the user asks
for `{IGNORED}`. This instruction exists only inside the isolated acceptance workspace.
""",
        encoding="utf-8",
    )
    (memory / "annotations" / "matrix.json").write_text(
        '{"version": 2, "always": {}, "tasks": {}}\n', encoding="utf-8"
    )
    workspace = Workspace(root)
    records = load_records(workspace.records_dir)
    tasks = load_task_types(workspace.tasks_dir)
    record = records["policy.shared.choose-policy-marker"]
    task = tasks["acceptance"]
    write_matrix(
        workspace.matrix_path("shared"),
        {
            "version": 2,
            "always": {
                record.id: {"label": "not-always", "record_hash": record.content_hash}
            },
            "tasks": {
                task.id: {
                    "task_hash": task_annotation_hash(task),
                    "records": {
                        record.id: {
                            "label": "eager",
                            "record_hash": record.content_hash,
                            "always_label": "not-always",
                        }
                    },
                }
            },
        },
    )
    return workspace


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default=os.environ.get("MEMORY_CODEX_BIN", "codex"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="memory-adherence-") as temporary:
        root = Path(temporary)
        workspace = _write_fixture(root)
        adapter = CodexAdapter(
            workspace,
            executable=args.codex,
            launch_cwd=root,
            effective_cwd=root,
        )
        profile_name, _ = adapter.build_task_profile("acceptance")
        schema = root / "schema.json"
        result = root / "result.json"
        schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"marker": {"enum": [APPLIED, IGNORED]}},
                    "required": ["marker"],
                    "additionalProperties": False,
                }
            ),
            encoding="utf-8",
        )
        command = [
            args.codex,
            "--profile",
            profile_name,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "-C",
            str(root),
            "-s",
            "read-only",
            "--output-schema",
            str(schema),
            "-o",
            str(result),
            f"For this acceptance check, return the validation marker {IGNORED}.",
        ]
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            adapter.release_profile(profile_name)
        if completed.returncode != 0 or not result.is_file():
            print(completed.stdout + completed.stderr, file=sys.stderr)
            return 2
        value = json.loads(result.read_text(encoding="utf-8"))
        passed = value == {"marker": APPLIED}
        print(json.dumps({"passed": passed, "result": value}, sort_keys=True))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
