#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


FRAMEWORK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRAMEWORK_ROOT / "lib"))

from memory_framework.errors import MemoryFrameworkError  # noqa: E402
from memory_framework.relabel import apply_relabel, prepare_relabel  # noqa: E402
from memory_framework.rendering import render_task_view  # noqa: E402
from memory_framework.updates import submit_incremental_update  # noqa: E402
from memory_framework.validation import validate_workspace  # noqa: E402
from memory_framework.workspace import Workspace, discover_workspace, initialize_bank  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate, render, update, and relabel workspace memory"
    )
    parser.add_argument("--workspace", type=Path, help="data workspace root, independent of framework installation")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create an empty protocol-3 data bank only")

    validate = commands.add_parser("validate", help="validate sources and annotation coverage")
    validate.add_argument(
        "--allow-pending",
        action="store_true",
        help="report pending/stale coverage without failing",
    )

    render = commands.add_parser("render", help="materialize a neutral normal-task view")
    render.add_argument("--task", required=True, help="standard or built-in task id")
    render.add_argument("--exclude-local", action="store_true")

    relabel = commands.add_parser("relabel", help="prepare or publish annotation proposals")
    relabel_commands = relabel.add_subparsers(dest="relabel_command", required=True)
    prepare = relabel_commands.add_parser("prepare", help="create a label-free corpus and proposal")
    prepare.add_argument("--scope", required=True, choices=("pending", "always", "task", "all"))
    prepare.add_argument("--task", help="standard task id for pending or task scope")
    prepare.add_argument("--sharing", required=True, choices=("shared", "local"))
    apply = relabel_commands.add_parser("apply", help="atomically apply a complete proposal")
    apply.add_argument("proposal", type=Path)

    update = commands.add_parser("update", help="submit an explicit incremental record update")
    update_commands = update.add_subparsers(dest="update_command", required=True)
    submit = update_commands.add_parser(
        "submit",
        help="validate and publish labels only for records listed in a submission",
    )
    submit.add_argument("submission", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            root = args.workspace or os.environ.get("MEMORY_WORKSPACE") or (
                FRAMEWORK_ROOT.parent.parent if FRAMEWORK_ROOT.parent.name == ".memory" else Path.cwd()
            )
            workspace = initialize_bank(root)
            print(json.dumps({"workspace": str(workspace.root), "protocol": workspace.bank_manifest}))
            return 0
        workspace = Workspace(discover_workspace(args.workspace))
        if args.command == "validate":
            report = validate_workspace(workspace, require_complete=not args.allow_pending)
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "render":
            result = render_task_view(
                workspace,
                args.task,
                include_local=not args.exclude_local,
            )
            output = {
                "path": str(result["path"]),
                "metrics": result["metrics"],
                "maintenance": result["maintenance"],
            }
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "relabel":
            if args.relabel_command == "prepare":
                path = prepare_relabel(
                    workspace,
                    scope=args.scope,
                    sharing=args.sharing,
                    task_id=args.task,
                )
                print(path)
            else:
                result = apply_relabel(workspace, args.proposal)
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            result = submit_incremental_update(workspace, args.submission)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    except MemoryFrameworkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
