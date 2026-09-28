---
name: maintain-memory
description: Maintain protocol-3 workspace memory prose and structure with confirmation, semantic-granularity review, single-submit incremental add/edit publication, smart merges, validation, required reclassification, and safe runtime-artifact cleanup. Use when adding, editing, splitting, merging, migrating, promoting, or removing durable memory; initializing a workspace; safely clearing disposable memory artifacts; or explicitly managing task types, record categories, or record-bound tools.
---

# Maintain Workspace Memory

Treat the directory containing `.memory/` as one workspace. Keep current task state out of
memory. Never edit generated views, profiles, or annotation matrices.

## Semantic record granularity

Treat granularity as a mandatory invariant for every record create, update, split, merge,
promotion, or migration, regardless of its loading label. One record is the smallest
self-contained semantic unit that can be independently retrieved, annotated, maintained, and
invalidated.

Before proposing record prose, verify all of these conditions:

1. The body answers one primary reusable question, decision, procedure, or failure mode.
2. One task-independent `load_when` truthfully describes when the whole body is useful.
3. Every part shares the same global always/not-always decision and, when not always, could
   receive the same eager/lazy decision for every standard task.
4. The body has one maintenance, validation, and invalidation boundary; no part can become
   independently obsolete or incorrect while the rest remains current.

Split the record when any condition fails. Keep material together only when separating it would
make a required causal chain, safety condition, procedure, or worked example incomplete or
misleading. Under recall-first uncertainty, prefer multiple self-contained records over one
coarse record; repeat essential safety preconditions where needed, and use cross-references for
navigation rather than as a substitute for self-containment. Never split mechanically by heading
count or byte target; size is only a review signal.

## Practical writing standard

Optimize memory prose for practical future reuse, not completeness, record count, or KiB. Before
writing, identify the future decision, action, or avoidable error the record should improve, then
choose the smallest self-contained form that does so.

Write maintained memory source prose in English, including record titles, bodies and `load_when`
text, category descriptions, task-type definitions, and record-bound tool usage contracts. The
user-facing conversation may use any appropriate language. Translate confirmed content without
changing its authority, meaning, provenance, or evidence scope.

Universality is not a qualification requirement. Preserve a non-universal method as Playbook
guidance when evidence makes it a useful first candidate for a future decision or experiment.
Separate the supporting observation, inference, search recommendation, and adopted rule. State the
scope, transfer conditions, and the evidence or measurement that decides adoption. A search prior
orders what to try; it does not predict the winner, override direct task evidence, or become a
Reference fact merely because it worked once.

When evidence changes, revise the record at its source. Distinguish false, unsupported, unresolved,
conditionally useful, and adopted claims. If only the strongest interpretation fails, narrow or
reclassify it and preserve independently supported premises, conditional deductions, and useful
search guidance. Remove the invalidated portion instead of deleting the whole lesson or stacking
qualifications around stale prose. Net growth should correspond to new reusable behavior or a new
decision branch.

Use progressive disclosure: lead with the reusable conclusion or procedure, normally followed by
only the two or three decisive facts or causal links and the important scope boundary. Use the
shortest causal chain that makes the record actionable. Add detail only when it changes correct
application, safety, trust, maintenance, or invalidation.

Treat memory as a decision layer, not a raw-evidence archive or a copy of canonical documentation.
Keep the decisive facts and provenance needed to judge the guidance, and link durable experiments,
raw data, public contracts, and canonical examples instead of reproducing them. For mutable facts,
state the observed environment or identity and the condition that requires rechecking them.

Do not assume future readers know investigation-only terminology, identifiers, or context. Define
what is needed in plain language, but compress or omit investigation chronology, redundant
examples, raw measurements, and supported facts that do not affect future use. Prefer the shorter
formulation when utility is equal, without shortening away a necessary safety chain, causal
explanation, functional worked example, or failure boundary. When authoritative sources cannot be
reconciled, preserve both provenances explicitly instead of silently deleting a claim or promoting
one source above another.

## Routine incremental add/edit workflow

1. Inspect existing records and authoritative sources. Route each durable fact to exactly one
   authority layer: workspace constraints and preferences to Policy, docs/source/design facts to
   Reference, and practice-derived lessons or search priors to Playbook. Use `policy/local` only
   for machine-local or personal facts; all other categories are shared. Before routing a
   Reference or Playbook record, read its matching
   `.memory/categories/<layer>/<category>.md` Scope and Boundary.
2. Smart-merge overlaps only when the result passes the semantic granularity review. Rewrite the
   existing semantic record so every distinct actionable detail survives, contradictions are
   resolved, and obsolete claims are corrected at their source. Keep separate files whenever the
   knowledge has a different retrieval, annotation, maintenance, or invalidation boundary.
3. Before editing, show the user the exact proposed prose and destination path. Wait for explicit
   confirmation. Annotation-only changes are always exempt. A user may also explicitly delegate
   prose and granularity judgment for one bounded maintenance batch; that delegation is the batch
   confirmation, so record its scope and apply the full granularity review without interrupting for
   per-record approval. Never infer or generalize this exception from an ordinary action request.
4. After confirmation, create or edit the Markdown record directly. Use strict TOML frontmatter
   with exactly these fields:

   ```markdown
   +++
   id = "playbook.build.release-check"
   title = "Check release artifacts before publishing"
   layer = "playbook"
   category = "build"
   load_when = "Read before assembling or publishing a release artifact."
   +++

   <one self-contained semantic record body>
   ```

   Store it as `.memory/records/<layer>/<category>/<slug>.md`. The ID must equal
   `<layer>.<category>.<slug>` and the immutable final ID segment must equal the filename. Titles
   may change; IDs and filenames do not. Policy categories are only `shared` and `local` and have
   no category files. Every Reference or Playbook category must already be defined under
   `.memory/categories/<layer>/`. `load_when` is mandatory for every record, including Policy.
   Write a useful, task-independent retrieval condition even when the record is currently always
   or eager. The field belongs to the record and must not be omitted, moved into annotations, or
   rewritten merely because a loading label changes. Direct-loaded views render it as the
   activation condition; lazy catalogs render it as the retrieval cue.
5. Prepare one explicit semantic submission containing only the records changed for this user
   request. Do not include another agent's pending records. Store it under
   `.memory/generated/updates/<update-id>/submission.json`:

   ```json
   {
     "version": 1,
     "task": "build",
     "records": {
       "playbook.build.release-check": {
         "always": "not-always",
         "task_label": "eager"
       }
     }
   }
   ```

   Decide `always` only when every task must know the complete record before acting; otherwise use
   `not-always`. For each `not-always` record in an active standard task, use a recall-biased
   balance: consider applicability frequency, miss cost, trigger recognizability, and attention
   cost. Choose `eager` when the record is frequently applicable or a miss is costly and its trigger
   is not reliably self-identifying. Choose `lazy` for narrow, recognizable conditions or cheap
   delayed reads. Non-obvious content alone does not justify `eager`; choose `eager` on a genuine
   tie and never target a numerical split. Omit `task_label` for an `always` record. Set top-level
   `task` to null and omit every `task_label` when there is no active standard task or the active
   task is built in.
6. Submit exactly once:

   ```bash
   python3 "$MEMORY_FRAMEWORK_ROOT/scripts/memory.py" --workspace "$MEMORY_WORKSPACE" update submit \
     .memory/generated/updates/<update-id>/submission.json
   ```

   Do not separately validate, prepare a relabel proposal, apply annotations, or render task views
   for this routine path; the command validates and updates only the listed record IDs. Generated
   task views and profiles are launch-time disposable artifacts. Unlisted concurrent edits remain
   visible as pending and are not a submission failure; the affected task's next launch performs
   one selected-task annotation preflight before creating its profile. If submission rejects a
   stale task row or the request removes records, changes task or category structure, or requires
   full reconsideration, switch to the relevant administrative branch and the relabel skill.

Promote a proven Playbook lesson to Reference by proposing the destination record and deletion or
rewrite of the source lesson as one confirmed change. Preserve provenance in the resulting prose;
because promotion removes or structurally replaces authority, do not use the routine add/edit
submission path.

## Safe runtime-artifact cleanup

Only on an explicit cleanup request, and after the user has finished any uncommitted memory update
or relabel transaction, run this one-command cleanup from the workspace root:

```bash
python3 "$MEMORY_FRAMEWORK_ROOT/skills/maintain-memory/scripts/cleanup_artifacts.py" --workspace "$MEMORY_WORKSPACE"
```

Use `--dry-run` to inspect without deleting, or `--workspace <path>` when invoking the deployed
script for another workspace. The cleaner removes generated views, update and relabel workspaces,
test or acceptance output under `.memory/generated`, stale client profiles, prompts,
registrations, and inactive handoffs. It preserves every launch artifact covered by a live Codex
or Claude Code lease, and it never touches records, categories, task types, annotations,
maintained framework files or tools, client routing preferences, or exported snapshots outside
the workspace cache. If a client registry cannot be authenticated and bounded, the cleaner
refuses broad deletion rather than guessing ownership.

## Administrative branches

Read only the support file matching an explicit request:

- Workspace initialization: [references/initialize.md](references/initialize.md)
- Task-type management: [references/task-types.md](references/task-types.md)
- Category management: [references/categories.md](references/categories.md)
- Record-bound tool management: [references/tools.md](references/tools.md)
