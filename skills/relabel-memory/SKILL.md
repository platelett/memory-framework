---
name: relabel-memory
description: Classify and atomically apply v2 always/not-always and task eager/lazy annotations from label-free corpora. Use for explicit incremental or full relabeling; ordinary task launches automatically classify only their selected pending/stale row before rendering.
---

# Relabel Workspace Memory

Change annotation state only. Do not edit record prose, task descriptions, categories, tools, or
authority. Pure annotation changes require no user confirmation and may block the user's request.
Never edit `.memory/annotations/matrix*.json` or generated views directly.
Bank commands automatically use the embedded framework's parent bank; `MEMORY_WORKSPACE` or
an explicit `--workspace <path>` can select a different workspace.

## Choose a scope

- `pending`: classify missing/stale always cells and, with `--task <id>`, missing/stale cells for
  the active standard task. Omit `--task` for a built-in task. This is the incremental path after a
  record add or edit and supports non-conflicting concurrent proposals.
- `always`: fully reconsider every record's task-independent `always` or `not-always` decision.
- `task`: fully reconsider every non-always record for one standard task. Always decisions must be
  fresh first.
- `all`: fully decide always first, then every standard task for every resulting non-always record.

Run shared and local scopes separately. If no local records exist, no local proposal is needed.

## Prepare, classify, apply

1. Create an immutable input corpus and unpublished proposal:

   ```bash
   python3 "$MEMORY_FRAMEWORK_ROOT/scripts/memory.py" relabel prepare \
     --scope <pending|always|task|all> [--task <id>] --sharing <shared|local>
   ```

2. Read the generated `corpus.md` completely. It contains no previous labels. Decide `always`
   only when every task must know the record before acting; otherwise decide `not-always`.
   Every record already owns a required, task-independent `load_when`; relabeling may use it as
   evidence but never creates, deletes, or rewrites it. Lazy is only the loading state that exposes
   that condition in the catalog.
   For a task, use a recall-biased balance. Consider applicability frequency, miss cost, whether
   ordinary task input reliably reveals the trigger, and the attention cost of preloading the
   body. Decide `eager` when the record is frequently applicable, or when a miss is costly and the
   trigger is not reliably self-identifying. Decide `lazy` for narrow shape-, API-, file-,
   experiment-, or failure-specific knowledge when the trigger is recognizable or delayed reading
   is inexpensive. Non-obvious content alone does not justify `eager`. On a genuine tie, choose
   `eager`; do not target a numerical split. Use render metrics as evidence of attention cost, not
   as a quota.
3. Edit only null values under `decisions` in the sibling `proposal.json`. For conditional task
   cells, leave the value null only when the same proposal decides that record `always`. Work may
   span turns; the live matrix remains unchanged.
4. Self-review completeness and semantic consistency without consulting old labels. Then apply:

   ```bash
   python3 "$MEMORY_FRAMEWORK_ROOT/scripts/memory.py" relabel apply \
     .memory/generated/relabel/<proposal-id>/proposal.json
   ```

5. If application reports a changed hash or same-cell conflict, discard the stale semantic
   judgment, prepare a new proposal for the affected scope, and reclassify. Never copy decisions
   blindly into a refreshed corpus. After success, rerender and resume normal work only when the
   active task's required shared and local coverage is fresh.

Full proposals reject any change to their pinned complete manifest. Incremental proposals merge
only when every targeted cell and pinned record/task hash still match.

## Automatic selected-task launch preflight

For new, resumed, and forked task-bound Codex launches, the wrapper handles pending/stale coverage
outside the main session. It prepares one label-free bundle containing the selected task and every
required shared/local record, runs one ephemeral non-interactive Codex classifier under an exact
JSON output schema, records the returned decisions into proposal nulls, validates the completed
proposal files, atomically applies each sharing matrix, and only then renders the latest task
profile. The classifier uses the bundle as its sole evidence, invokes no tools, sees no old labels
or proposal paths, and never owns application.

This preflight never classifies another task row and never falls back to direct-loading pending
bodies. Normally `always` was already decided by the record add/edit transaction; exceptional
pending/stale always cells are repaired first because task labels are defined only for
`not-always` records. Full always or all-task reconsideration remains explicit Auto maintenance.
