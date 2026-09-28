from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "lib"))
sys.path.insert(0, str(REPOSITORY / "clients"))

from memory_framework.workspace import Workspace
from memory_protocol import DATA_PROTOCOL


INSTRUCTIONS = {
    "common.md": "# Common\n\nPropose durable memory when useful.",
    "normal.md": "# Normal\n\nMaintain labels before work.",
    "auto.md": "# Auto\n\nChoose one task.",
    "snapshot.md": "# Snapshot\n\nRead only.",
}


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    memory = root / ".memory"
    framework = tmp_path / "framework"
    shutil.copytree(REPOSITORY, framework, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".ruff_cache", "tests", "tools", "packaging"))
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
    for name, content in INSTRUCTIONS.items():
        (framework / "instructions" / name).write_text(content + "\n", encoding="utf-8")
    (memory / "protocol.json").write_text(json.dumps({"kind": "bank", "protocol": DATA_PROTOCOL}))
    (memory / "annotations" / "matrix.json").write_text(
        json.dumps({"version": 2, "always": {}, "tasks": {}}, indent=2) + "\n",
        encoding="utf-8",
    )
    (framework / "skills" / "maintain-memory" / "SKILL.md").write_text(
        "---\nname: maintain-memory\ndescription: Maintain records.\n---\n\n# Maintain\n",
        encoding="utf-8",
    )
    (framework / "skills" / "relabel-memory" / "SKILL.md").write_text(
        "---\nname: relabel-memory\ndescription: Relabel records.\n---\n\n# Relabel\n",
        encoding="utf-8",
    )
    for name, title in {
        "initialize.md": "Initialize fixture workspace",
        "categories.md": "Manage fixture categories",
        "task-types.md": "Manage fixture task types",
        "tools.md": "Manage fixture tools",
    }.items():
        (framework / "skills" / "maintain-memory" / "references" / name).write_text(
            f"# {title}\n\nFixture branch procedure.\n",
            encoding="utf-8",
        )
    return Workspace(root, framework_root=framework)


def write_record(
    workspace: Workspace,
    layer: str,
    category: str,
    slug: str,
    marker: str,
    *,
    title: str | None = None,
) -> Path:
    if layer in {"reference", "playbook"}:
        category_path = workspace.categories_dir / layer / f"{category}.md"
        if not category_path.exists():
            write_category(workspace, layer, category)
    path = workspace.records_dir / layer / category / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "+++",
                f'id = "{layer}.{category}.{slug}"',
                f'title = "{title or slug.replace("-", " ").title()}"',
                f'layer = "{layer}"',
                f'category = "{category}"',
                f'load_when = "Read when work concerns {slug} behavior."',
                "+++",
                "",
                f"## Knowledge\n\n{marker}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def write_category(
    workspace: Workspace,
    layer: str,
    category: str,
    *,
    title: str | None = None,
    notes: str | None = None,
) -> Path:
    path = workspace.categories_dir / layer / f"{category}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "+++",
        f'id = "{layer}.{category}"',
        f'title = "{title or category.replace("-", " ").title()}"',
        f'layer = "{layer}"',
        "+++",
        "",
        "## Scope",
        "",
        f"Durable {category} knowledge in the {layer} layer.",
        "",
        "## Boundary",
        "",
        "Exclude unrelated subjects and other authority layers.",
    ]
    if notes is not None:
        lines.extend(["", "## Notes", "", notes])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_task(workspace: Workspace, task_id: str = "build", marker: str = "Build artifacts.") -> Path:
    path = workspace.tasks_dir / f"{task_id}.md"
    path.write_text(
        "\n".join(
            [
                "+++",
                f'id = "{task_id}"',
                f'title = "{task_id.title()}"',
                'routing_hints = ["compile", "package"]',
                "+++",
                "",
                "## Scope",
                "",
                marker,
                "",
                "## Boundary",
                "",
                "Exclude unrelated documentation work.",
                "",
                "## Positive examples",
                "",
                "- Compile a release binary.",
                "",
                "## Negative examples",
                "",
                "- Rewrite a prose guide.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def complete_proposal(path: Path, *, always: str = "not-always", task: str = "lazy") -> dict:
    value = read_json(path)
    for record_id in value["decisions"]["always"]:
        value["decisions"]["always"][record_id] = always
    for task_cells in value["decisions"]["tasks"].values():
        for record_id in task_cells:
            task_cells[record_id] = None if always == "always" else task
    write_json(path, value)
    return value


def trim_pending_proposal(path: Path, record_id: str) -> None:
    value = read_json(path)
    value["record_manifest"] = {record_id: value["record_manifest"][record_id]}
    value["decisions"]["always"] = {
        key: item for key, item in value["decisions"]["always"].items() if key == record_id
    }
    value["base_fingerprints"]["always"] = {
        key: item for key, item in value["base_fingerprints"]["always"].items() if key == record_id
    }
    for task_id in list(value["decisions"]["tasks"]):
        for container in (
            value["decisions"]["tasks"],
            value["requirements"]["tasks"],
            value["base_fingerprints"]["tasks"],
        ):
            container[task_id] = {
                key: item for key, item in container[task_id].items() if key == record_id
            }
        if not value["decisions"]["tasks"][task_id]:
            for container in (
                value["decisions"]["tasks"],
                value["requirements"]["tasks"],
                value["base_fingerprints"]["tasks"],
            ):
                del container[task_id]
            value["task_manifest"] = {}
    write_json(path, value)
