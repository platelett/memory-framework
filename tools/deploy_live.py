#!/usr/bin/env python3
"""Install a complete framework into a new directory; never publish knowledge data."""
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="framework maintenance root")
    parser.add_argument("installation", type=Path, help="new installation, e.g. WORKSPACE/.memory/framework")
    parser.add_argument("whitelist", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    target = args.installation.absolute()
    if target.exists() or target.is_symlink():
        raise ValueError(f"refusing to overwrite existing installation: {target}")
    entries = [line.strip() for line in args.whitelist.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(entries) != len(set(entries)):
        raise ValueError("deployment whitelist contains duplicates")
    for relative in entries:
        if Path(relative).parts[0] not in {"framework.json", "bin", "lib", "clients", "scripts", "instructions", "skills"} or ".." in Path(relative).parts:
            raise ValueError(f"non-framework deployment entry: {relative}")
        path = source / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"deployment source is not a regular file: {path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".framework-install-", dir=target.parent))
    try:
        for relative in entries:
            origin = source / relative
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(origin, destination)
            destination.chmod(0o700 if origin.stat().st_mode & 0o100 else 0o600)
        os.rename(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print(f"installed {len(entries)} framework files at {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
