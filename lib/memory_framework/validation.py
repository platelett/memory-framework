from __future__ import annotations

from .annotations import coverage_issues, load_annotation_snapshot
from .errors import ValidationError
from .parsing import load_categories
from .workspace import Workspace


def validate_workspace(workspace: Workspace, *, require_complete: bool = True) -> dict[str, object]:
    workspace.assert_compatible()
    categories = load_categories(workspace.categories_dir)
    records, tasks, matrices = load_annotation_snapshot(workspace)
    for name in ("normal.md", "auto.md", "snapshot.md"):
        path = workspace.instructions_dir / name
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            raise ValidationError(f"required instruction file is missing or empty: {path}")
    issues = coverage_issues(records, tasks, matrices)
    if require_complete and issues:
        details = "\n".join(f"- {issue.describe()}" for issue in issues)
        raise ValidationError(f"annotation coverage is incomplete:\n{details}")
    return {
        "protocol": workspace.bank_manifest["protocol"],
        "framework_release": workspace.framework_manifest["release"],
        "categories": len(categories),
        "records": len(records),
        "tasks": len(tasks),
        "shared_records": sum(record.sharing == "shared" for record in records.values()),
        "local_records": sum(record.sharing == "local" for record in records.values()),
        "coverage_issues": [issue.describe() for issue in issues],
    }
