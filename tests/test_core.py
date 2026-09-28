from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import (
    complete_proposal,
    read_json,
    trim_pending_proposal,
    write_category,
    write_json,
    write_record,
    write_task,
)
from memory_framework.annotations import (
    coverage_issues,
    load_annotation_snapshot,
    task_annotation_hash,
)
import memory_framework.annotations as annotation_module
from memory_framework.errors import PublicationError, ValidationError
from memory_framework.parsing import (
    load_categories,
    load_records,
    load_task_types,
    parse_category,
    parse_record,
)
from memory_framework.relabel import apply_relabel, prepare_relabel
from memory_framework.rendering import render_auto_view, render_task_view
from memory_framework.updates import submit_incremental_update
from memory_framework.validation import validate_workspace


def test_builtin_stewardship_is_visible_and_full_writing_review_is_on_demand():
    repository = Path(__file__).resolve().parents[1]
    instructions = repository / "instructions"
    common = (instructions / "common.md").read_text(encoding="utf-8")
    normal = (instructions / "normal.md").read_text(encoding="utf-8")
    auto = (instructions / "auto.md").read_text(encoding="utf-8")
    snapshot = (instructions / "snapshot.md").read_text(encoding="utf-8")
    maintain = (
        repository / "skills" / "maintain-memory" / "SKILL.md"
    ).read_text(encoding="utf-8")

    common_flat = " ".join(common.split())
    normal_flat = " ".join(normal.split())
    auto_flat = " ".join(auto.split())
    snapshot_flat = " ".join(snapshot.split())
    maintain_flat = " ".join(maintain.split())
    short_invariant = "independently retrieved, annotated, maintained, and invalidated"
    assert short_invariant in normal_flat
    assert short_invariant in auto_flat
    assert short_invariant not in snapshot_flat
    assert "proactively initiate the memory-maintenance workflow" in common_flat
    assert "Do not silently write memory" in common_flat
    assert "ritual memory suggestion" in common_flat
    assert "Universality is not a memory qualification gate" in common_flat
    assert "scoped search prior" in common_flat
    assert "forcing a keep-or-delete choice" in common_flat
    assert "## Apply retrieved guidance" in common
    assert "revise the work itself" in common_flat
    assert "summarizing the rule is not compliance" in common_flat
    assert "apply its granularity review" in normal_flat
    assert "## Semantic record granularity" in maintain
    assert "One task-independent `load_when`" in maintain_flat
    assert "same eager/lazy decision for every standard task" in maintain_flat
    assert "Under recall-first uncertainty" in maintain_flat
    assert "Never split mechanically by heading count or byte target" in maintain_flat
    assert "explicitly delegate" in maintain_flat
    assert "per-record approval" in maintain_flat
    assert "## Practical writing standard" in maintain
    assert "practical future reuse, not completeness, record count, or KiB" in maintain_flat
    assert "two or three decisive facts or causal links" in maintain_flat
    assert "shortest causal chain" in maintain_flat
    assert "investigation chronology" in maintain_flat
    assert "Prefer the shorter formulation when utility is equal" in maintain_flat
    assert "Write maintained memory source prose in English" in maintain_flat
    assert "search prior orders what to try" in maintain_flat
    assert "false, unsupported, unresolved, conditionally useful, and adopted" in maintain_flat
    assert "decision layer, not a raw-evidence archive" in maintain_flat
    assert "condition that requires rechecking" in maintain_flat
    assert "Smart-merge overlaps only" in maintain_flat
    assert "every distinct actionable detail survives" in maintain_flat
    assert "different retrieval, annotation, maintenance, or invalidation boundary" in maintain_flat


def test_builtin_stewardship_is_rendered_for_auto_and_every_task(workspace):
    write_task(workspace)
    auto = render_auto_view(workspace)["content"]
    normal = render_task_view(workspace, "build")["content"]

    assert "Propose durable memory when useful." in auto
    assert "Propose durable memory when useful." in normal


def test_builtin_stewardship_is_part_of_clean_deployment_payload():
    repository = Path(__file__).resolve().parents[1]
    whitelist = (repository / "tools" / "live-deploy-whitelist.txt").read_text(
        encoding="utf-8"
    )

    assert "instructions/common.md" in whitelist.splitlines()
    assert "lib/memory_framework/preflight.py" in whitelist.splitlines()


def test_strict_record_and_task_parsing(workspace):
    record_path = write_record(workspace, "playbook", "build", "release-check", "PLAYBOOK MARKER")
    task_path = write_task(workspace)
    records = load_records(workspace.records_dir)
    tasks = load_task_types(workspace.tasks_dir)
    assert records["playbook.build.release-check"].content_hash
    assert tasks["build"].positive_examples == ("Compile a release binary.",)

    malformed = record_path.read_text(encoding="utf-8").replace(
        'category = "build"', 'category = "other"'
    )
    record_path.write_text(malformed, encoding="utf-8")
    with pytest.raises(ValidationError, match="do not match"):
        parse_record(record_path, workspace.records_dir)

    task_path.write_text(task_path.read_text(encoding="utf-8").replace("## Boundary", "## Unknown"), encoding="utf-8")
    with pytest.raises(ValidationError, match="unknown task section"):
        load_task_types(workspace.tasks_dir)


@pytest.mark.parametrize(
    ("layer", "category"),
    [("policy", "shared"), ("reference", "docs"), ("playbook", "build")],
)
def test_every_record_layer_requires_load_when(workspace, layer, category):
    path = write_record(workspace, layer, category, "required-condition", "BODY")
    source = path.read_text(encoding="utf-8")
    without_load_when = "\n".join(
        line for line in source.splitlines() if not line.startswith("load_when = ")
    ) + "\n"
    path.write_text(without_load_when, encoding="utf-8")

    with pytest.raises(ValidationError, match=r"missing=\['load_when'\]"):
        parse_record(path, workspace.records_dir)


def test_duplicate_or_path_inconsistent_ids_are_rejected(workspace):
    first = write_record(workspace, "reference", "docs", "one", "ONE")
    second = write_record(workspace, "reference", "docs", "two", "TWO")
    second.write_text(second.read_text(encoding="utf-8").replace("reference.docs.two", "reference.docs.one"), encoding="utf-8")
    with pytest.raises(ValidationError, match="id must be"):
        load_records(workspace.records_dir)
    assert first.exists()


def test_strict_category_parsing_path_and_duplicate_ids(workspace):
    category = write_category(workspace, "reference", "docs", notes="Stable public docs.")
    parsed = parse_category(category, workspace.categories_dir)
    assert parsed.id == "reference.docs"
    assert parsed.notes == "Stable public docs."

    malformed = category.read_text(encoding="utf-8").replace(
        'id = "reference.docs"', 'id = "reference.other"'
    )
    category.write_text(malformed, encoding="utf-8")
    with pytest.raises(ValidationError, match="id must be"):
        load_categories(workspace.categories_dir)

    category.write_text(malformed.replace("reference.other", "reference.docs"), encoding="utf-8")
    duplicate = workspace.categories_dir / "reference" / "nested" / "docs.md"
    duplicate.parent.mkdir()
    duplicate.write_text(category.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValidationError, match="expected categories"):
        load_categories(workspace.categories_dir)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("## Scope", "## Unknown", "unknown category section"),
        ("## Boundary", "## Scope", "duplicate category section"),
        ("Durable docs knowledge in the playbook layer.", "", "Scope.*empty"),
    ],
)
def test_category_requires_unique_nonempty_scope_and_boundary(workspace, old, new, message):
    path = write_category(workspace, "playbook", "docs")
    path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    with pytest.raises(ValidationError, match=message):
        load_categories(workspace.categories_dir)


def test_records_require_defined_category_but_empty_categories_are_valid(workspace):
    empty = write_category(workspace, "playbook", "empty")
    assert validate_workspace(workspace, require_complete=False)["categories"] == 1
    empty.unlink()

    record = write_record(workspace, "reference", "docs", "api", "API")
    (workspace.categories_dir / "reference" / "docs.md").unlink()
    with pytest.raises(ValidationError, match="undefined category 'reference.docs'"):
        load_records(workspace.records_dir)
    assert record.exists()


def _classified_matrix(workspace):
    records = load_records(workspace.records_dir)
    tasks = load_task_types(workspace.tasks_dir)
    matrix = {"version": 2, "always": {}, "tasks": {}}
    for record in records.values():
        matrix["always"][record.id] = {
            "label": "always" if record.layer == "policy" else "not-always",
            "record_hash": record.content_hash,
        }
    task = tasks["build"]
    matrix["tasks"]["build"] = {"task_hash": task_annotation_hash(task), "records": {}}
    for record in records.values():
        if record.layer != "policy":
            matrix["tasks"]["build"]["records"][record.id] = {
                "label": "eager" if record.layer == "reference" else "lazy",
                "record_hash": record.content_hash,
                "always_label": "not-always",
            }
    write_json(workspace.matrix_path("shared"), matrix)
    return records, tasks, matrix


def test_rendering_order_catalog_builtins_and_auto_body_exclusion(workspace):
    write_record(workspace, "policy", "shared", "guardrail", "POLICY MARKER")
    write_record(workspace, "reference", "docs", "api", "REFERENCE MARKER")
    write_record(workspace, "playbook", "build", "release", "PLAYBOOK SECRET BODY")
    write_task(workspace)
    _classified_matrix(workspace)

    result = render_task_view(workspace, "build")
    content = result["content"]
    assert content.index("POLICY MARKER") < content.index("REFERENCE MARKER")
    assert content.count("POLICY MARKER") == 1
    assert "PLAYBOOK SECRET BODY" not in content
    assert ".memory/records/playbook/build/release.md" not in content
    assert "Mandatory when: work concerns guardrail behavior." in content
    assert "Applies when: work concerns api behavior." in content
    assert "- Read when work concerns release behavior." in content
    assert "Policy constraints" in content
    assert "Authoritative reference" in content
    assert "Playbook guidance" not in content  # Lazy entries have only conditions.

    eager = render_task_view(workspace, "all-eager")["content"]
    assert "PLAYBOOK SECRET BODY" in eager
    assert "Applies when: work concerns release behavior." in eager
    lazy = render_task_view(workspace, "all-lazy")["content"]
    assert "REFERENCE MARKER" not in lazy
    assert "[Api](.memory/records/reference/docs/api.md)" not in lazy
    assert "- Read when work concerns api behavior." in lazy
    assert "SHA-256" not in lazy.split("# Lazy memory catalog", 1)[1]

    auto = render_auto_view(workspace, preferences={"tasks": {}})["content"]
    assert "POLICY MARKER" not in auto
    assert "REFERENCE MARKER" not in auto
    assert "PLAYBOOK SECRET BODY" not in auto
    assert "build" in auto
    assert "all-lazy" not in auto and "all-eager" not in auto


def test_missing_and_changed_hashes_are_explicitly_stale(workspace):
    path = write_record(workspace, "reference", "docs", "api", "V1")
    write_task(workspace)
    records, tasks, matrices = load_annotation_snapshot(workspace)
    issues = coverage_issues(records, tasks, matrices)
    assert [issue.kind for issue in issues] == ["pending-always"]

    proposal = prepare_relabel(workspace, scope="pending", sharing="shared", task_id="build")
    complete_proposal(proposal, always="not-always", task="eager")
    apply_relabel(workspace, proposal)
    assert validate_workspace(workspace)["coverage_issues"] == []

    path.write_text(path.read_text(encoding="utf-8").replace("V1", "V2"), encoding="utf-8")
    with pytest.raises(ValidationError, match="stale-always"):
        validate_workspace(workspace)


def test_task_classification_policy_change_invalidates_existing_task_rows(
    workspace, monkeypatch
):
    write_record(workspace, "reference", "docs", "api", "API")
    write_task(workspace)
    _classified_matrix(workspace)
    records, tasks, matrices = load_annotation_snapshot(workspace)
    assert coverage_issues(records, tasks, matrices) == []

    monkeypatch.setattr(
        annotation_module,
        "TASK_CLASSIFICATION_POLICY",
        annotation_module.TASK_CLASSIFICATION_POLICY + " Changed.",
    )
    issues = coverage_issues(records, tasks, matrices)
    assert [issue.kind for issue in issues] == ["stale-task"]
    assert issues[0].task_id == "build"


def test_relabel_corpus_declares_recall_biased_balance(workspace):
    write_record(workspace, "reference", "docs", "api", "API")
    write_task(workspace)

    proposal = prepare_relabel(workspace, scope="pending", sharing="shared", task_id="build")
    corpus = proposal.with_name("corpus.md").read_text(encoding="utf-8")

    assert "recall-biased balance" in corpus
    assert "applicability frequency" in corpus
    assert "Non-obvious content alone does not justify eager" in corpus
    assert "genuine tie" in corpus and "choose eager" in corpus


def test_nonconflicting_incremental_proposals_merge(workspace):
    write_record(workspace, "reference", "docs", "one", "ONE")
    write_record(workspace, "reference", "docs", "two", "TWO")
    write_task(workspace)
    first = prepare_relabel(workspace, scope="pending", sharing="shared", task_id="build")
    second = prepare_relabel(workspace, scope="pending", sharing="shared", task_id="build")
    trim_pending_proposal(first, "reference.docs.one")
    trim_pending_proposal(second, "reference.docs.two")
    complete_proposal(first)
    complete_proposal(second)
    apply_relabel(workspace, first)
    apply_relabel(workspace, second)
    matrix = read_json(workspace.matrix_path("shared"))
    assert set(matrix["always"]) == {"reference.docs.one", "reference.docs.two"}


def test_incremental_publish_cleans_orphan_task_cells(workspace):
    obsolete = write_record(workspace, "reference", "docs", "obsolete", "OLD")
    write_task(workspace)
    initial = prepare_relabel(workspace, scope="pending", sharing="shared", task_id="build")
    complete_proposal(initial)
    apply_relabel(workspace, initial)

    obsolete.unlink()
    cleanup = prepare_relabel(workspace, scope="pending", sharing="shared")
    cleanup_value = read_json(cleanup)
    assert cleanup_value["decisions"] == {"always": {}, "tasks": {}}
    apply_relabel(workspace, cleanup)

    matrix = read_json(workspace.matrix_path("shared"))
    assert "reference.docs.obsolete" not in matrix["always"]
    assert "reference.docs.obsolete" not in matrix["tasks"]["build"]["records"]


def test_same_cell_conflict_is_rejected(workspace):
    write_record(workspace, "reference", "docs", "one", "ONE")
    write_task(workspace)
    first = prepare_relabel(workspace, scope="pending", sharing="shared", task_id="build")
    second = prepare_relabel(workspace, scope="pending", sharing="shared", task_id="build")
    complete_proposal(first)
    complete_proposal(second)
    apply_relabel(workspace, first)
    with pytest.raises(PublicationError, match="same always cell changed"):
        apply_relabel(workspace, second)


def test_incremental_submit_updates_only_explicit_records_and_reports_other_pending(
    workspace, tmp_path
):
    write_record(workspace, "reference", "docs", "one", "ONE")
    write_record(workspace, "reference", "docs", "two", "TWO")
    write_task(workspace)
    submission = tmp_path / "submission.json"
    second_submission = tmp_path / "second-submission.json"
    write_json(
        submission,
        {
            "version": 1,
            "task": "build",
            "records": {
                "reference.docs.one": {
                    "always": "not-always",
                    "task_label": "eager",
                }
            },
        },
    )
    write_json(
        second_submission,
        {
            "version": 1,
            "task": "build",
            "records": {"reference.docs.two": {"always": "always"}},
        },
    )

    result = submit_incremental_update(workspace, submission)
    matrix = read_json(workspace.matrix_path("shared"))
    assert set(matrix["always"]) == {"reference.docs.one"}
    assert set(matrix["tasks"]["build"]["records"]) == {"reference.docs.one"}
    assert result["records"] == ["reference.docs.one"]
    assert any(
        "pending-always: shared/reference.docs.two" in issue
        for issue in result["remaining_coverage_issues"]
    )
    assert "render" not in result and "renders" not in result

    completed = submit_incremental_update(workspace, second_submission)
    assert completed["remaining_coverage_issues"] == []
    assert validate_workspace(workspace)["coverage_issues"] == []

    with pytest.raises(PublicationError, match="not pending or stale"):
        submit_incremental_update(workspace, second_submission)


def test_incremental_submit_validates_semantics_before_changing_matrix(workspace, tmp_path):
    write_record(workspace, "reference", "docs", "one", "ONE")
    task = write_task(workspace)
    parsed_task = load_task_types(workspace.tasks_dir)["build"]
    matrix = {
        "version": 2,
        "always": {},
        "tasks": {
            "build": {
                "task_hash": "0" * 64,
                "records": {},
            }
        },
    }
    write_json(workspace.matrix_path("shared"), matrix)
    submission = tmp_path / "submission.json"
    write_json(
        submission,
        {
            "version": 1,
            "task": "build",
            "records": {
                "reference.docs.one": {
                    "always": "not-always",
                    "task_label": "lazy",
                }
            },
        },
    )
    with pytest.raises(PublicationError, match="task row is stale"):
        submit_incremental_update(workspace, submission)
    assert read_json(workspace.matrix_path("shared")) == matrix

    write_json(
        submission,
        {
            "version": 1,
            "task": None,
            "records": {
                "reference.docs.one": {
                    "always": "not-always",
                    "task_label": "lazy",
                }
            },
        },
    )
    with pytest.raises(PublicationError, match="not applicable"):
        submit_incremental_update(workspace, submission)
    assert task.exists() and parsed_task.id == "build"
    assert read_json(workspace.matrix_path("shared")) == matrix


def test_auto_incremental_submit_leaves_disposable_generated_views_untouched(workspace, tmp_path):
    record = write_record(workspace, "reference", "docs", "one", "ONE")
    write_task(workspace)
    proposal = prepare_relabel(workspace, scope="pending", sharing="shared", task_id="build")
    complete_proposal(proposal, always="not-always", task="eager")
    apply_relabel(workspace, proposal)
    for task in ("build", "all-eager", "all-lazy"):
        render_task_view(workspace, task)
    old_view = workspace.generated_dir / "views" / "build" / "normal.md"
    assert "ONE" in old_view.read_text(encoding="utf-8")

    record.write_text(record.read_text(encoding="utf-8").replace("ONE", "TWO"), encoding="utf-8")
    submission = tmp_path / "auto-submission.json"
    write_json(
        submission,
        {
            "version": 1,
            "task": None,
            "records": {"reference.docs.one": {"always": "not-always"}},
        },
    )

    result = submit_incremental_update(workspace, submission)

    assert "render" not in result and "renders" not in result
    assert any("pending-task: shared/build/reference.docs.one" in issue for issue in result["remaining_coverage_issues"])
    report = validate_workspace(workspace, require_complete=False)
    assert not any("stale-generated-view" in issue for issue in report["coverage_issues"])
    assert "ONE" in old_view.read_text(encoding="utf-8")


def test_record_task_and_full_manifest_staleness(workspace):
    record = write_record(workspace, "reference", "docs", "one", "ONE")
    task = write_task(workspace)
    pending = prepare_relabel(workspace, scope="pending", sharing="shared", task_id="build")
    complete_proposal(pending)
    record.write_text(record.read_text(encoding="utf-8").replace("ONE", "CHANGED"), encoding="utf-8")
    with pytest.raises(PublicationError, match="record changed"):
        apply_relabel(workspace, pending)

    record.write_text(record.read_text(encoding="utf-8").replace("CHANGED", "ONE"), encoding="utf-8")
    pending = prepare_relabel(workspace, scope="pending", sharing="shared", task_id="build")
    complete_proposal(pending)
    task.write_text(task.read_text(encoding="utf-8").replace("Build artifacts.", "Build packages."), encoding="utf-8")
    with pytest.raises(PublicationError, match="task description or classification policy changed"):
        apply_relabel(workspace, pending)

    full = prepare_relabel(workspace, scope="always", sharing="shared")
    complete_proposal(full, always="always")
    write_record(workspace, "playbook", "build", "new", "NEW")
    with pytest.raises(PublicationError, match="full relabel record manifest is stale"):
        apply_relabel(workspace, full)


def test_shared_and_local_matrices_do_not_leak(workspace):
    write_record(workspace, "reference", "docs", "shared", "SHARED")
    write_record(workspace, "policy", "local", "personal", "LOCAL")
    shared = prepare_relabel(workspace, scope="pending", sharing="shared")
    complete_proposal(shared, always="always")
    apply_relabel(workspace, shared)
    assert "reference.docs.shared" in read_json(workspace.matrix_path("shared"))["always"]
    assert not workspace.matrix_path("local").exists()

    local = prepare_relabel(workspace, scope="pending", sharing="local")
    complete_proposal(local, always="always")
    apply_relabel(workspace, local)
    assert set(read_json(workspace.matrix_path("local"))["always"]) == {"policy.local.personal"}
    assert "policy.local.personal" not in read_json(workspace.matrix_path("shared"))["always"]
