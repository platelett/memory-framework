#!/usr/bin/env python3
"""Build a deterministic framework-only archive without knowledge or runtime state."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_entries(root: Path) -> list[dict[str, object]]:
    entries = []
    for path in sorted(root.rglob("*")):
        if path.name == "manifest.json":
            continue
        relative = str(path.relative_to(root))
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": relative, "type": "directory", "mode": "0700"})
        elif stat.S_ISREG(metadata.st_mode):
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                    "size": metadata.st_size,
                    "sha256": sha256(path),
                }
            )
        else:
            raise ValueError(f"package payload must not contain links or special files: {path}")
    return entries


def add_deterministic(tar: tarfile.TarFile, root: Path, path: Path) -> None:
    relative = path.relative_to(root)
    info = tar.gettarinfo(str(path), arcname=str(relative))
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    if path.is_file():
        with path.open("rb") as stream:
            tar.addfile(info, stream)
    else:
        tar.addfile(info)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--whitelist",
        type=Path,
        default=Path("tools/live-deploy-whitelist.txt"),
    )
    args = parser.parse_args()
    source = args.source.resolve()
    archive = args.archive.resolve()
    whitelist = args.whitelist.resolve()
    relative_files = [
        line.strip()
        for line in whitelist.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(relative_files) != len(set(relative_files)):
        raise ValueError("package input list contains duplicates")

    with tempfile.TemporaryDirectory(prefix="memory-framework-package-") as temporary:
        root = Path(temporary) / "memory-framework"
        root.mkdir(mode=0o700)
        for relative in relative_files:
            if Path(relative).parts[0] not in {"framework.json", "bin", "lib", "clients", "scripts", "instructions", "skills"} or ".." in Path(relative).parts:
                raise ValueError(f"non-framework package input: {relative}")
            src = source / relative
            if not src.is_file() or src.is_symlink():
                raise ValueError(f"package input is not a regular file: {src}")
            dst = root / "framework" / relative
            dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o700 if src.stat().st_mode & 0o100 else 0o600)

        for name in ("install.sh", "INSTALL.md"):
            src = source / "packaging" / name
            dst = root / name
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o700 if name.endswith(".sh") else 0o600)

        for directory in (root, *root.rglob("*")):
            if directory.is_dir():
                os.chmod(directory, 0o700)

        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        bank = Path(temporary) / "independent-bank"
        subprocess.run(
            ["python3", str(root / "framework" / "scripts" / "memory.py"), "--workspace", str(bank), "init"],
            check=True,
            env=environment,
            text=True,
            capture_output=True,
        )
        subprocess.run(
            ["python3", str(root / "framework" / "scripts" / "memory.py"), "--workspace", str(bank), "validate"],
            check=True, env=environment, text=True, capture_output=True,
        )
        for cache in sorted(root.rglob("__pycache__"), reverse=True):
            shutil.rmtree(cache)

        forbidden = (
            "records/",
            "categories/",
            "task-types/",
            "generated/",
            "state/",
            "profiles/",
            "auth.json",
            "__pycache__",
            ".pytest_cache",
        )
        payload_names = [str(path.relative_to(root)) for path in root.rglob("*")]
        for name in payload_names:
            if any(token in name for token in forbidden):
                raise ValueError(f"forbidden package payload: {name}")
        for path in root.rglob("*"):
            if path.is_file() and b"/root/" in path.read_bytes():
                raise ValueError(f"machine-specific absolute path in package: {path}")

        manifest = {
            "version": 1,
            "root": "memory-framework",
            "framework_only": True,
            "entries": package_entries(root),
        }
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(manifest_path, 0o600)

        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as tar:
                    add_deterministic(tar, root.parent, root)
                    for path in sorted(root.rglob("*")):
                        add_deterministic(tar, root.parent, path)
        os.chmod(archive, 0o600)

    sidecar = archive.with_name(archive.name + ".sha256")
    sidecar.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="utf-8")
    os.chmod(sidecar, 0o600)
    print(archive)
    print(sidecar)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
