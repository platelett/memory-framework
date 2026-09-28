#!/bin/sh
set -eu

umask 077

package_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target_dir=${1:-$(pwd)/.memory/framework}

python3 - "$package_dir" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1]).resolve()
manifest_path = root / "manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("version") != 1 or manifest.get("root") != "memory-framework" or manifest.get("framework_only") is not True:
    raise SystemExit("package manifest schema is invalid")

expected = set()
for entry in manifest.get("entries", []):
    relative = entry.get("path")
    if not isinstance(relative, str) or not relative or relative.startswith("/"):
        raise SystemExit(f"unsafe manifest path: {relative!r}")
    parts = Path(relative).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise SystemExit(f"unsafe manifest path: {relative!r}")
    path = root / relative
    expected.add(relative)
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != int(entry["mode"], 8):
        raise SystemExit(f"mode mismatch: {relative}")
    if entry["type"] == "directory":
        if not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit(f"type mismatch: {relative}")
    elif entry["type"] == "file":
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != entry["size"]:
            raise SystemExit(f"file metadata mismatch: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise SystemExit(f"SHA-256 mismatch: {relative}")
    else:
        raise SystemExit(f"unknown entry type: {relative}")

actual = {
    str(path.relative_to(root))
    for path in root.rglob("*")
    if path.name != "manifest.json"
}
if actual != expected:
    raise SystemExit(
        "package contents differ from manifest: "
        f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
    )
PY

if [ -e "$target_dir" ] || [ -L "$target_dir" ]; then
    echo "refusing to overwrite existing framework installation: $target_dir" >&2
    exit 2
fi
mkdir -p "$(dirname -- "$target_dir")"
cp -R "$package_dir/framework" "$target_dir"
chmod 0700 "$target_dir"
echo "Memory framework installed at $target_dir"
echo "For a new bank: python3 $target_dir/scripts/memory.py --workspace /path/to/workspace init"
echo "For an existing bank: MEMORY_WORKSPACE=/path/to/workspace $target_dir/bin/memory-codex"
