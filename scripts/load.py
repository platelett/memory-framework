#!/usr/bin/env python3
"""Read full memory records by exact load_when or a stable short ID."""
from __future__ import annotations

import argparse
import hashlib
import sys
import tomllib
from pathlib import Path


FRAMEWORK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRAMEWORK_ROOT / "lib"))
from memory_protocol import (  # noqa: E402
    bank_manifest, discover_workspace, framework_manifest, validate_record_metadata,
)


def record_key(record_id: str) -> str:
    # Shared wire format with rendering.py: hash the immutable ID, not content.
    return hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:12]


def load_matches(
    workspace: Path, condition: str | None = None, *,
    short_id: str | None = None, exclude_local: bool = False,
) -> str:
    if (condition is None) == (short_id is None):
        raise ValueError("provide exactly one condition or short ID")
    bank_manifest(workspace, snapshot_ok=True)
    records = workspace / ".memory" / "records"
    if not (workspace / ".memory").is_dir():
        raise ValueError(f"workspace has no .memory directory: {workspace}")
    matches: list[str] = []
    for path in sorted(records.glob("*/*/*.md")):
        if exclude_local and path.parent == records / "policy" / "local":
            continue
        raw = path.read_bytes()
        source = raw.decode("utf-8")
        lines = source.splitlines()
        if not lines or lines[0] != "+++":
            raise ValueError(f"{path}: missing TOML frontmatter")
        try:
            end = lines.index("+++", 1)
        except ValueError as exc:
            raise ValueError(f"{path}: unclosed TOML frontmatter") from exc
        metadata = tomllib.loads("\n".join(lines[1:end]))
        validate_record_metadata(metadata, path, records)
        if not "\n".join(lines[end + 1:]).strip():
            raise ValueError(f"{path}: Markdown body is empty")
        record_id = metadata.get("id")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError(f"{path}: missing record ID")
        key = record_key(record_id)
        matched = key == short_id if short_id is not None else metadata.get("load_when") == condition
        if matched:
            matches.append(
                f"Short ID: {key}\n"
                f"Source: {path}\n"
                f"Content SHA-256: {hashlib.sha256(raw).hexdigest()}\n\n{source.rstrip()}"
            )
    if short_id is not None and len(matches) > 1:
        raise ValueError(f"short record ID collision {short_id}; refusing ambiguous lookup")
    if not matches:
        if short_id is not None:
            raise ValueError(f"no record matches short ID {short_id!r}; use the complete key from its heading")
        raise ValueError(
            "no records match this exact load_when; copy the complete condition from "
            "the current catalog (without its bullet), including punctuation"
        )
    return "\n\n---\n\n".join(matches) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("condition", nargs="?", help="exact load_when text")
    parser.add_argument("--stdin", action="store_true", help="read the condition from stdin")
    parser.add_argument("--id", dest="short_id", help="stable short ID from a loaded record heading")
    parser.add_argument("--workspace", type=Path, help="data workspace root")
    parser.add_argument("--exclude-local", action="store_true")
    args = parser.parse_args(argv)
    if sum((args.stdin, args.condition is not None, args.short_id is not None)) != 1:
        parser.error("provide exactly one of condition, --stdin or --id")
    condition = sys.stdin.read().removesuffix("\n").removesuffix("\r") if args.stdin else args.condition
    if args.short_id is None and (not condition or "\n" in condition or "\r" in condition):
        parser.error("condition must be one non-empty line")
    try:
        framework = framework_manifest(FRAMEWORK_ROOT, reader_ok=True)
        root = args.workspace
        if root is None and framework["kind"] == "memory-reader":
            root = FRAMEWORK_ROOT.parent
        root = discover_workspace(root, framework_root=FRAMEWORK_ROOT)
        output = load_matches(
            root, condition,
            short_id=args.short_id, exclude_local=args.exclude_local,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
