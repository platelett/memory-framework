# Memory Framework 3 — Design Specification

Status: protocol-generation-3 architecture. This file remains the design source of truth
for the single maintained framework installation; it does not authorize changes to another
memory bank. The implementation uses
Python 3.12 standard-library code and ships without predefined standard task
types.

The programmatic data boundary and root-resolution rules are defined in [PROTOCOL.md](PROTOCOL.md).
Framework software resides in this directory; it may be installed inside a bank as `.memory/framework/`
or independently. Installed framework instructions and skills are never resolved from data directories.

## 1. Goals

- Avoid loading the complete memory bank into every agent context.
- Decide loading at the granularity of one memory record and one task type.
- Define one record as an independently retrievable, annotatable, maintainable,
  and invalidatable self-contained semantic unit.
- Guarantee complete annotation coverage for every standard task type, even
  when doing so delays the user's requested work.
- Keep memory prose easy for humans and agents to read and edit.
- Keep classification, validation, rendering, and concurrency independent of
  any agent client.
- Make client integrations replaceable adapters.
- Support portable, read-only, framework-free snapshots for one task type.

## 2. Non-goals and deliberate omissions

- No stateful memory loader. A read-only exact-condition lookup returns all matching records.
- No `requires`, `related`, dependency graph, or recursive loading in v2.
- No hard token quota that changes an eager/lazy semantic decision.
- No root-level bootstrap/router `AGENTS.md` or `CLAUDE.md` in a deployed v2
  workspace. LLM-capable client commands select one task type (or explicit Auto)
  through the client adapter before launch.
- No assumption that different clients accept the same instruction-discovery
  or launch mechanism.

## 3. Architecture and client boundary

### 3.1 Client-independent core

The core owns:

- Markdown/TOML-frontmatter record parsing;
- task-type parsing;
- `always` / `eager` / `lazy` semantics;
- record and task hashes;
- annotation coverage and stale detection;
- neutral task-view construction;
- classification-corpus construction;
- selected-task annotation preflight planning;
- snapshot contents before client rendering;
- validation, atomic annotation application, and concurrency control.

The core must not mention `AGENTS.md`, `CLAUDE.md`, `developer_instructions`,
`codex`, or another client command.

### 3.2 Client adapters

An adapter owns:

- rendering the neutral task view into the client's instruction artifact;
- injecting that artifact at launch;
- the client's interactive selection UI;
- invoking a client agent for automatic routing;
- invoking one ephemeral non-interactive classifier when the selected task has pending or stale
  annotations, then asking the core to apply its proposals;
- client-local learned selection preferences;
- rendering a client-specific read-only snapshot entry file.

Codex and Claude Code are full local adapters. Each uses the client's documented
per-invocation instruction surface; unsupported launch behavior is explicit rather than guessed.

### 3.3 Instruction environments

Framework operating rules are built-in behavior rather than memory records. A common stewardship
instruction is injected into both writable agent environments, while detailed writing guidance
lives in the maintenance skill and is loaded only when maintenance is triggered. The common
instruction also requires retrieved guidance to be applied to the active answer or artifact; a
request to reread a rule must trigger reassessment rather than a summary of the rule.

The framework has three instruction environments:

1. **Normal task:** the common stewardship instruction, one concrete task type, and its materialized memory view.
   It also contains a very short, always-present granularity invariant and
   maintenance trigger with direct paths to the maintenance skills. Memory
   routine incremental record maintenance is triggered naturally inside this
   same session by reading the relevant skill. Pending/stale selected-task
   classification is completed by the launcher before this session exists;
   the full skill catalog is not loaded.
2. **Automatic routing:** the common stewardship instruction plus a selectable or explicitly requested environment containing routing instructions, task descriptions, every skill's
   compact metadata, the same short granularity invariant, and client-local
   preferences only; no memory record bodies and no concrete task instructions.
   It hands a request to a stable task session only when one concrete memory
   task is needed. Otherwise it fulfills the request itself without a second
   launch. For explicit memory administration it remains the working session
   and reads the matching authoritative skill and support procedure on demand;
   those files can be re-read after compaction. There is no separate maintainer
   role or command.
3. **Snapshot export:** deterministic code only; no agent is required.

### 3.4 Disposable runtime artifacts and cold bootstrap

The deployed workspace must be able to cold-start from authoritative inputs alone. Records,
categories, task types, annotations, maintained framework instructions and tools, and explicitly
durable client preferences are authoritative inputs. Everything materialized from them or created
while one memory transaction is in progress is a disposable runtime artifact.

Disposable artifacts include generated views, update submissions, relabel corpora and proposals,
client launch instructions, profiles, registrations, handoffs, and framework test or acceptance
reports placed under `.memory/generated`. With no transaction or client launch actively consuming
them, deleting all such artifacts must not change validation semantics or prevent the next render,
task selection, profile construction, or memory-maintenance operation from rebuilding what it
needs. Implementations must create missing runtime directories on demand and must never read a
completed transaction artifact as canonical state.

Deleting an in-progress update or relabel artifact may discard that transaction's uncommitted
work, but it must not corrupt published records or annotations. A snapshot explicitly exported to
a user-selected destination is a deliverable rather than a workspace cache and is outside this
cold-deletion contract.

The built-in `maintain-memory` cleanup command implements this boundary. It reclaims stale client
leases and disposable artifacts, preserves artifacts covered by live Codex or Claude Code leases,
and refuses broad deletion when a registry cannot be safely authenticated or bounded.

## 4. Memory records

### 4.0 Category definitions

Reference and Playbook category boundaries are first-class source metadata at
`.memory/categories/<layer>/<category>.md`. Their strict frontmatter contains `id`, `title`, and
`layer`; their bodies contain one non-empty Scope and Boundary plus optional Notes. Records in
those layers must reference a defined category, while an empty category is valid. Policy keeps the
built-in `shared` and `local` namespaces and has no category files.

Category metadata is consulted only while routing or maintaining records. It has no loading state,
does not participate in annotation freshness or relabel corpora, and is absent from Normal, Auto,
and Snapshot artifacts.

### 4.1 One semantic record per Markdown file

Every memory record is one Markdown file with strict TOML frontmatter. The body
remains prose rather than JSON.

Example:

```markdown
+++
id = "playbook.build.release-check"
title = "Verify release artifact hashes"
layer = "playbook"
category = "build"
load_when = "Read before publishing a release artifact."
+++

## Context
...

## Lesson
...
```

Every record, including Policy, must have exactly the five frontmatter fields
shown above: `id`, `title`, `layer`, `category`, and `load_when`. `load_when` is
a required, useful, task-independent retrieval condition owned by the record;
it is never annotation metadata. Always and eager bodies display the condition
as an activation cue (`Mandatory when` for Policy and `Applies when` for the
other layers). A lazy catalog exposes the same record-owned condition as the
retrieval cue.
Relabeling never creates, deletes, or rewrites `load_when`.

Granularity is a mandatory maintenance invariant, not an annotation state. A
record is the smallest self-contained unit that can be independently retrieved,
annotated, maintained, and invalidated. Its body answers one primary reusable
question, decision, procedure, or failure mode; one task-independent
`load_when` covers the whole body; all parts share one always/not-always and
per-task eager/lazy decision boundary; and all parts share one maintenance,
validation, and invalidation boundary. Failure of any condition requires a
split. Material remains together only when splitting a causal chain, safety
condition, procedure, or worked example would make the resulting records
incomplete or misleading.

Under recall-first uncertainty, maintenance prefers multiple self-contained
records over one coarse record. Essential safety preconditions may be repeated
so each record remains safe in isolation. Heading count and byte size never
mechanically decide a split; size only prompts review. These are semantic
judgments that the parser cannot prove, so the short invariant is always
visible while the complete checklist is loaded on demand from the maintenance
skill before any record prose or structure change.

Universality is not a record-admission requirement. An evidence-backed method
may be retained as a scoped Playbook search prior when it improves a future
decision or experiment order. The prose must separate observations, inferences,
search recommendations, and adopted rules, and must state transfer conditions
and the evidence that decides adoption. New evidence narrows or reclassifies
only the claims it invalidates; independently supported premises and useful
conditional guidance survive. Memory remains a decision layer that links raw
evidence and canonical documentation rather than copying them.

All maintained semantic prose is English: record titles, bodies and `load_when`
text, category descriptions, task-type definitions, and record-bound tool usage
contracts. User-facing conversation remains language-independent. Mutable facts
identify their observed environment and their recheck condition.

### 4.2 Stable IDs and filenames

- The immutable ID is generated from the initial semantic title and namespace.
- The filename uses the final slug component, for example
  `lock-buffer-ownership.md`.
- The title remains human-facing and may change without changing the ID or
  filename.
- Sequential numeric filenames are not used: they are unreadable and require a
  contended allocator.
- A collision is detected at creation time and resolved before publication.

### 4.3 Authority is independent of loading

The authority hierarchy remains:

```text
Policy > Reference (Canon) > Playbook
```

Loading state never changes authority. Reference and Playbook use the same
record and annotation machinery but retain different provenance, maintenance,
and promotion rules.

Playbook includes both adopted default practice and explicitly scoped search
priors. A search prior orders which candidate to try first; it does not predict
the winner, override direct task evidence, or acquire Reference authority.

Policy also uses one-record-per-file storage. Shared/local is a sharing scope,
not a loading mode. A genuinely workspace-wide policy may be `always`; a
task-scoped policy participates in the standard eager/lazy matrix. Framework
operating rules are not Policy records: they live in the mode instructions.

### 4.4 Exact-condition lazy reads

A lazy catalog contains only distinct, verbatim `load_when` conditions. The
catalog provides one command for `framework/scripts/load.py`, which returns all
records whose condition exactly equals the supplied text. Conditions are not
unique identifiers: equal conditions intentionally load records together,
including across authority layers. The response includes source paths and full
record frontmatter and bodies. Missing matches fail explicitly; case, spacing
and punctuation are not normalized and there is no fuzzy fallback. `--stdin`
accepts shell-sensitive text without interpolation. The loader scans current
record sources without a persistent index, caller identity, seen-set or recursive
expansion. Repeated reads are valid, including after compaction. It defaults to
the workspace containing the script; `--workspace` overrides it and
`--exclude-local` preserves a shared-only view's retrieval scope.

Direct-loaded records contain only a short key, title, activation condition and
body. The key is the first 12 hexadecimal digits of SHA-256 of the immutable full
record ID, and remains stable across content edits. Rendering checks the included
record set for collisions; `load.py --id SHORT_ID` rejects ambiguous or missing
matches. This read-only lookup returns the current full ID, path, content hash
and complete record source. Maintenance must retrieve this metadata before edits;
the short key identifies a record, not a content version.

Stable IDs, paths and content hashes remain in authoritative sources and
manifests rather than every catalog entry. Lazy is merely the loading state that
renders `load_when`; it does not own or rewrite the field. Equal conditions do
not merge records or their independent maintenance and annotation boundaries.

### 4.5 Tools are record-bound prose

When a record has a reusable executable tool, the record body explains when and
how to invoke it and points to its implementation under `.memory/tools/`.
There is no tool dependency graph, automatic tool loader, usage harness, or
separate tool annotation. The agent reads the record and invokes the documented
tool directly. Implementations are never injected merely because the record is
loaded.

## 5. Loading states

### 5.1 `always`

`always` is a global annotation, not record-authored metadata and not a
per-task annotation. Its full body appears in every normal task view, including
the two built-in task types. The annotation matrix records an explicit
always/not-always decision for every record.

Use it only for knowledge that every task must know before acting. A new record
must first receive an always/not-always decision. Promoting a record to always
makes task-row cells inactive; demoting one makes every standard task cell
pending until it is classified.

### 5.2 `eager`

For a standard task type, the complete record body appears in the materialized
task view together with its activation condition. Classification uses a
recall-biased balance. It considers applicability frequency, miss cost, trigger
recognizability from ordinary task input, and the attention cost of preloading
the body. A record is eager when it is frequently applicable, or when missing
it is costly and its trigger is not reliably self-identifying. Non-obvious
content alone does not make a record eager. A genuine tie resolves to eager;
there is no numerical eager/lazy target.

### 5.3 `lazy`

For a standard task type, the view lists each distinct lazy `load_when` once.
The agent must use the exact-condition loader and read every match before
relying on that knowledge.

Use lazy for narrow shape-, API-, file-, experiment-, or failure-specific
knowledge when the catalog condition is recognizable or delayed reading is
inexpensive. Context metrics are evidence of attention cost but never impose a
quota or mechanically decide a label.

The eager/lazy decision is task-specific. `load_when` is task-independent in
v2. If experience later proves that a small number of records require
task-specific wording, an optional override can be added then; it is not part
of the initial schema.

### 5.4 Context-size reporting

Rendering reports bytes and estimated tokens for always bodies, eager bodies,
and lazy descriptions, plus change from the previous build. These metrics do
not automatically demote records. Persistent growth prompts task-type or
record-granularity review. Only a real client or filesystem limit is a hard
failure.

## 6. Task types

### 6.1 Standard task types

A standard task type has:

- immutable ID and human title;
- Scope and Boundary;
- positive and negative examples;
- optional routing hints;
- a content hash used with the canonical task-classification policy to derive
  the annotation-row hash. Changing either the task description or that policy
  invalidates the row.

Every non-always record must have a fresh explicit eager/lazy annotation for
every standard task type.

### 6.2 Built-in virtual task types

Two built-ins are always available and behave like standard types at launch and
snapshot time, but have no annotation rows and cannot be relabeled:

- `all-lazy`: every non-always record is lazy;
- `all-eager`: every non-always record is eager.

### 6.3 Automatic routing is not a task type

Automatic routing is a client-independent routing module hosted by a client
adapter. It loads:

- task descriptions and hashes;
- built-in task descriptions;
- compact metadata for every maintenance skill (name, description, and body
  path), but not the skill bodies;
- client-local learned preferences;
- its own minimal routing instructions.

It initially loads no memory record bodies and has no eager/lazy annotation. It
may find an exact match, rank similar types, ask the user to choose, or offer to
remember a task choice. It routes only when the request needs a concrete memory
task; requests it can fulfill without task-bound memory remain in Auto. For
explicit memory administration it selects one branch, reads the authoritative
maintenance skill and matching support file, and performs that work in the
current Auto session.

## 7. Annotation lifecycle

### 7.1 Completeness has priority

Annotation-only changes are a narrowly defined exception to the normal memory
confirmation rule:

- they require no user approval;
- they may delay task launch but run outside the main user session;
- they may change only annotation state and derived artifacts;
- they must never change record bodies, task descriptions, categories, or
  authority.

Coverage accounting can be guaranteed completely: every required cell is
either a fresh label or visibly pending/stale, never silently absent. Semantic
correctness is improved by periodic full relabeling but cannot be
mathematically guaranteed by an agent judgment.

### 7.2 Incremental changes

Adding or changing a record changes its content hash:

- after confirmation, the active agent edits the record and prepares one
  explicit submission listing only records changed for that request;
- the submission contains each record's always/not-always decision and, when
  applicable, its active standard task eager/lazy decision;
- one `update submit` command validates the sources, updates only those listed
  annotation cells, and reports remaining coverage without rendering views;
- if it is not always and the active type is standard, that task type must
  annotate it immediately;
- every other standard task type sees that cell as pending/stale;
- before a later session of one of those types handles the user's request, its
  pending cells are classified in one automatic selected-task launch preflight;
- when the active type is a built-in, every standard task cell for a new
  non-always record remains pending;
- built-in types require no annotation.

Changing a task description invalidates only that task's annotation row. The
automatic launch preflight reclassifies that row before the type can handle
normal work; it does not
invalidate the task-independent always decisions. Built-in loading policies
themselves remain annotation-free.

### 7.3 Full relabel

Relabel is one natural-language-triggered skill. It owns both the global always
decision and standard task eager/lazy decisions. It supports these conceptual
scopes without requiring a separate CLI mode or TUI:

- pending records for the active task;
- the complete always classification;
- one standard task's complete eager/lazy classification;
- always first and then every standard task.

For a full task relabel, the skill asks the script to create one deterministic
Markdown corpus containing:

- neutral relabel instructions;
- the selected task description;
- a compact ID/title manifest of the current always set, not duplicate bodies;
- every non-always memory record, delimited by ID, path, and hash, with its
  task label removed;
- a strict output schema.

It contains no previous task labels. For explicit maintenance, the agent reads
one generated corpus rather than opening many record files, edits one proposal
file over as many turns as needed, self-reviews it, and applies it only when
complete. There is no progress harness, per-batch staging protocol, or audit
agent. The live annotation remains unchanged until final application.

The always scope uses a label-free corpus containing every record and asks the
task-independent question: must every task know this record before acting? A
combined full relabel classifies always first, uses that result while relabeling
every affected standard task, and applies each sharing-scope matrix
atomically. Normal work remains blocked until every required sharing scope is
current.

### 7.4 Automatic selected-task launch preflight

Every new, resumed, or forked task-bound launch checks fresh coverage before it
renders a task profile. If selected-task cells are pending or stale, the launcher
prepares one label-free bundle containing the selected task and every affected
shared/local record. One ephemeral non-interactive Codex process receives the
complete bundle and a dynamically exact JSON output schema. It uses no tools or
filesystem evidence and returns decisions only. The parent launcher records
those decisions in proposal nulls, validates pinned hashes and completeness, atomically applies the sharing
matrices, reruns coverage, and only then renders the task profile.

The preflight never classifies another task row. It may mechanically remove
orphan cells, but unrelated semantic labels remain untouched. Normally every
record's global always/not-always decision was already made by its add/edit
submission. Exceptional pending/stale always cells are included because a task
label is meaningful only after the record is known to be `not-always`. Full
always or all-task reconsideration remains explicit administration.

The classifier never sees old labels, proposal paths, or live state. It never
applies state, and normal rendering never creates a
proposal or direct-loads pending bodies. A failed classifier, incomplete output,
or non-converging concurrent conflict fails the task launch before the main
session exists.

### 7.5 Incremental-update instruction placement

Incremental-update rules are split by function rather than loaded all at once.
Every normal task view directly includes only these short trigger/invariant
rules:

- memory prose is changed only after the required user confirmation;
- pure annotation changes need no confirmation and may block the user request;
- adding or changing a record requires reclassification before normal work
  continues;
- generated views and the live annotation matrix are never edited directly;
- read `skills/maintain-memory/SKILL.md` before changing memory prose or
  structure; read `skills/relabel-memory/SKILL.md` before independent or full
  label maintenance, but not for the routine single-submit add/edit path.

The detailed record-routing, merge, semantic-submission, and write procedure
lives in the `maintain-memory` skill. The independent/full corpus, labeling,
and application procedure lives in the `relabel-memory` skill. Each body is
lazy-loaded only when its trigger fires. The short trigger itself lives in
`instructions/normal.md`; it is static normal-task instruction, not a
memory record and not a lazy catalog item. Making it lazy would prevent the
agent from knowing that it must load the procedure.

`maintain-memory/SKILL.md` is optimized for the common incremental add/edit
path and keeps that path short and self-contained. Rare administration such as
workspace initialization, task-type/category changes, or adding/removing tools
lives in referenced supporting files and is read only when that specific branch
is requested. Thus normal incremental editing can share the Maintain skill name
without loading every administrative procedure; no separate skill is created
for each small maintenance operation.

## 8. Safe concurrent annotation application

Live label state is kept in one small structured file per sharing scope:

```text
.memory/annotations/matrix.json
.memory/annotations/matrix.local.json
```

`matrix.json` covers shared records and may be tracked;
`matrix.local.json` covers local records and is untracked. Each has the same
schema: the global always/not-always row, one eager/lazy row per standard task
type, and the record and combined task-description/classification-policy hashes
on which those decisions were made. Built-in task types have no rows. If a workspace has no local records,
the local matrix is absent.

Agents never edit this live file. Routine record add/edit decisions live in an
explicit submission; independent and full relabel decisions live in an
unapplied proposal. Classification happens outside the lock; transaction
commands alone own matrix validation and mutation.

### 8.1 Incremental application

1. After confirmation, edit the authoritative record with an ordinary
   context-checked patch and prepare a submission listing exactly the changed
   record IDs and their semantic loading decisions.
2. Call `update submit` once. It acquires the annotation lock, parses every
   authoritative source, and requires each listed record to be pending/stale.
3. Update only listed always cells and the active standard task cells. Remove
   obsolete cells for those records from other task rows so they remain visibly
   pending rather than silently inherited.
4. Validate and atomically replace each affected sharing matrix and return every
   remaining coverage issue. Do not render task views. Pending cells stay out of
   main-session prompts and are classified once when that task is next launched.

Concurrent agents may patch different record files. A submission never scans
or claims all pending records, so another agent's unlisted edit remains pending
until its own submission. This is an eventual-coverage condition rather than
record corruption. Same-record source editing remains governed by the patch
tool's context checks; independent/full label concurrency continues to use the
pinned relabel proposal protocol below.

### 8.2 Full relabel application

1. Capture the task hash and complete record manifest.
2. Build and classify the label-free corpus outside the lock.
3. Acquire the annotation lock.
4. Apply only if the relevant task hashes, record manifest, and always state
   still match exactly; otherwise reject the proposal for refresh.
5. Atomically replace the target sharing-scope matrix. Within that scope, a
   combined relabel updates the always row and every affected task row in this
   single replacement, so no reader can observe a half-published
   classification.

Shared and local scopes apply as separate transactions because one is tracked
and the other must remain private. A normal renderer takes the same short lock
while capturing both files and refuses to run normal work if either required
scope is stale, so an interrupted two-scope relabel is recoverable rather than
silently inconsistent.

Independent agents can classify concurrently; only a short final matrix
replacement or coherent read snapshot is serialized. This deliberately trades
a tiny amount of write parallelism for much simpler state. Generated task views
and profiles are disposable caches, not memory state: writers may atomically
overwrite them, and a later launch may regenerate them from the records and
matrix. Concurrent stale-cache replacement is acceptable because it cannot
lose record or annotation content. Snapshot outputs are built in temporary
directories and atomically renamed. Locks are never held during model work.

Each relabel attempt uses only one immutable input and one unapplied output:

```text
.memory/generated/relabel/<proposal-id>/
├── corpus.md
└── proposal.json
```

The agent edits `proposal.json` directly and may resume that work over multiple
turns. A final transaction command validates its schema, completeness, pinned
hashes, and conflicts, then applies it. Separate agents use separate proposal
directories; they never share a mutable progress file and never directly edit
a live matrix. There is no generation tree, `HEAD`, audit agent, derived-view
publication protocol, or garbage collection protocol.

## 9. Codex and Claude Code adapters

### 9.1 Normal launch

There is no root router document. The wrapper's client-side selector precedes
every local Codex command capable of starting an LLM turn unless a concrete task
or Auto is explicit. Before any task-bound new, resume, or fork invocation, the
wrapper completes the selected task's annotation preflight. A normal task view
therefore renders only fresh always and task labels into Codex
`developer_instructions`; rendering fails closed on unresolved cells and never
creates a proposal or direct-loads pending bodies. A concrete task remains
responsible for its work and for listed routine `update submit` operations;
other task rows wait for their own next launch.

Normal and automatic framework launches do not generate or select an
`AGENTS.md`; they load framework instructions only through
`developer_instructions`. The only v2 feature that generates an `AGENTS.md` is
the explicitly requested framework-free read-only snapshot.

When Auto requests a concrete standard task, automatic routing and the final task are separate
interactive Codex processes orchestrated by the wrapper. The routing TUI uses
an isolated Auto profile. A handoff is optional and exclusively requests a
second process. After the user confirms a standard task, the routing agent
writes that handoff containing the selected task and original request. The
user exits that TUI; the waiting wrapper preflights the selected task and
replaces itself with a fresh Codex process using the latest task profile. The
same handoff can continue a resumed or forked Auto session by resuming its
canonical session ID under the fresh task profile. New developer instructions
remain the current task memory through later context compaction. Explicit memory
administration stays in the current Auto process and reads its skill files on
demand. Directly completed work creates no handoff; an absent handoff makes the
waiting wrapper exit normally.
The built-in `all-lazy` and `all-eager` loading modes remain available through
explicit `--task` selection but are not Auto routing targets.

### 9.2 Wrapper dispatch and continuation

Running the wrapper without a Memory mode shows the task selector for the
interactive TUI, prompt launches, `exec`/`e`, `review`, `resume`, `exec resume`,
and `fork`. `--auto` is a pure explicit routing flag, `--task` is the single-launch
direct-task fast path, and `--raw` is unconditional Codex
passthrough. Non-agent utility commands bypass Memory before it creates any
state. Task selection uses the controlling terminal instead of redirected stdin;
without a terminal the wrapper fails before launch and requires explicit `--task`
or `--auto`. The Codex adapter exposes at least:

```text
memory-codex
memory-codex --task <task-id>
memory-codex --auto [<initial request>]
memory-codex --list
memory-codex --snapshot <task-id> <output-dir>
```

All resume and fork transports follow the same selector rule. Selecting a
standard or built-in task preflights and freshly renders that task profile with
the original target; a bare resume then proceeds to Codex's own session picker.
Selecting `auto`, or using explicit `--auto`, resumes or forks under a fresh Auto
profile. A non-ephemeral Auto continuation may hand off its canonical session ID
to a standard task, after which the wrapper preflights that task and resumes the
same thread again under the latest task profile. Ephemeral continuations cannot
handoff because they have no durable session to resume.

Current Codex forwards the newly selected profile's developer instructions on
resume and fork, and later compaction retains that refreshed instruction set.
The wrapper therefore has no legacy same-task warning or prohibition. Retasking
an old conversation may still carry historical task content, but that is a
semantic choice rather than an instruction-transport limitation.

Codex cloud submission is guarded separately. Bare `cloud` and `cloud exec`
can create LLM tasks, but upstream rejects `--profile` for cloud and its cloud task
implementation does not apply local instruction overrides. They therefore show
the selector and fail closed before submission. Read-only cloud subcommands remain
transparent; `--raw` is the explicit opt-out from Memory enforcement. Codex's
internal `debug app-server send-message-v2` command is likewise guarded because it
makes an LLM call but rejects the selected profile. Merely starting `app-server`,
`mcp-server`, `exec-server`, a proxy, or remote-control does not itself call an LLM
and remains utility passthrough; a later protocol client is responsible for task
selection at the point where it creates an LLM turn.

The adapter separately captures the shell process directory and the effective
Codex `-C/--cd` directory. Processes stay launched from the former; project
override checks and learned directory hints use the latter.

There is no maintainer task type, command, profile class, memory view, or
annotation row. A normal task session may still perform routine incremental
updates. Explicit administration enters through Auto.

### 9.3 Task-bound and Auto profile injection

The adapter must not put potentially large `developer_instructions` in a shell
argument. It:

1. resolves `CODEX_HOME` and the user's normal configuration;
2. for a task-bound launch, checks selected-task coverage and, when needed,
   runs one minimal ephemeral `codex exec` over the complete label-free
   shared/local bundle; the parent validates and atomically applies its proposal
   files;
3. renders the selected task memory view or the compact Auto entry instructions
   under `.memory/generated/clients/codex/`;
4. composes that artifact with the user's existing `developer_instructions`, which
   is the same Codex configuration surface;
5. writes one unique launch profile under
   `.memory/clients/codex/profiles/<launch-id>.config.toml`;
6. stores the composed value in the profile's `developer_instructions` key;
7. derives a short namespace from the canonical workspace-root path and
   registers a unique launch-named symlink in `CODEX_HOME`,
   because current Codex resolves `--profile <name>` only from `CODEX_HOME`;
8. launches Codex with that registered profile name;
9. records the owner UID, wrapper PID, and `/proc` process-start time in a
   private registry. Cleanup removes only a dead, identity-matching owner and
   its framework-owned generated directory, so concurrent launches never
   delete one another's profile.

Codex profiles are configuration overlays; string fields are replaced, not
automatically concatenated. The adapter therefore composes only the two values
that share the `developer_instructions` key. It rejects conflicting
user-supplied `--profile` arguments unless a future explicit base-profile merge
feature handles them. It also detects a
higher-precedence project configuration that would override the injected
`developer_instructions` and fails clearly rather than starting with the wrong
memory view.

### 9.4 Learned selection preferences

Each client's selection preferences are separate local, untracked adapter state. Learning uses
explicit choices and corrections, not an unconfirmed guess. It may retain:

- task selection frequency and recency;
- normalized intent summaries rather than raw prompts;
- directory/project hints;
- explicit “automatically use this type for similar requests” choices.

Preferences are versioned against task-description hashes, updated atomically,
and ignored when their referenced task changes. Ambiguous routing still asks.

### 9.5 Claude Code launch and injection

`memory-claude` exposes the same Memory modes, selector, client-local preferences, and optional
two-phase Auto handoff as `memory-codex`. Utility commands and `--raw` are exact passthroughs.
Claude Code `ultrareview` is guarded because its cloud-hosted agent path is not verified to carry
the local task prompt. Background-agent manager, attachment, and respawn transports are guarded
for the same reason.

Normal and Auto launches render a unique owner-only artifact under
`.memory/generated/clients/claude/launches/<launch-id>/system-prompt.md` and invoke Claude Code
with `--append-system-prompt-file`. The documented append surface works in interactive and print
modes and composes with either replacement system-prompt form. The wrapper rejects a second append
flag so it cannot shadow or reorder selected memory. It leaves Claude Code's native user/project
`CLAUDE.md`, settings, rules, and auto-memory discovery intact. The wrapper waits for the client
and removes its launch artifact afterward, so concurrent launches cannot overwrite one another
and normal operation creates no repository entry document. Owner UID, wrapper PID, and process
start time are recorded privately; later launches remove only stale identity-matching artifacts.

Before a task-bound Claude launch renders that artifact, it uses the same selected-task annotation
preflight, implemented by the minimal ephemeral Codex classifier and parent-owned atomic apply.
This keeps annotation semantics and the no-pending-render invariant client-independent while
retaining one classifier implementation.

Resume and continue invocations receive the selected prompt again. Because their retained
conversation can still contain prior task context, the wrapper warns that selection must match
the session's original Memory mode; changing tasks or loading revised memory requires a new
session. Auto continuation cannot create a second task handoff.

## 10. Read-only task snapshots

The core can export one standard or built-in task type. A client adapter renders
the entry instruction file. A Codex snapshot is:

```text
snapshot/
├── AGENTS.md
├── .codex/
│   └── config.toml
├── .memory/
│   ├── records/<included record paths and files>
│   └── tools/
└── manifest.json
```

A Claude Code snapshot has the same copied records, loader and tools, but uses a root `CLAUDE.md`
entry and needs no `.codex/config.toml`:

```text
snapshot/
├── CLAUDE.md
├── .memory/
│   ├── records/<included record paths and files>
│   └── tools/
└── manifest.json
```

- Export refuses a task whose required shared or included-local annotations are
  pending or stale. Local records are excluded by default and require an
  explicit `--include-local` choice to avoid accidental disclosure.
- Always and eager bodies are embedded in the client's entry document (`AGENTS.md` or
  `CLAUDE.md`).
- Every included record is copied under `.memory/`, including direct-loaded
  records for metadata lookup by short ID. A separate portable reader installation
  under `framework/` with `framework/scripts/load.py` resolves conditions against those copied
  records; its path and hash are included in the snapshot manifest.
- Domain tools are files, not injected context. To avoid a tool-discovery
  harness, the exporter may copy the whole record-tool directory; agents still
  invoke a tool only when record prose tells them to do so.
- The snapshot contains no framework maintenance scripts, annotations, task
  definitions, locks, update protocol, or relabel capability.
- It is portable and query-only. A manifest records IDs and hashes; filesystem
  read-only permissions may be applied when supported but are not the security
  boundary.
- The Codex adapter records the exact `AGENTS.md` byte count and writes a
  snapshot-local `project_doc_max_bytes` large enough for that artifact. If the
  selected client still imposes a hard instruction or context limit, export
  fails rather than producing a silently truncated snapshot.

Both `all-eager` and `all-lazy` copy all included record sources; their entry
documents differ in loading format. The optional tools directory is
independent of that distinction.

### 10.1 Mutation boundary

Agents edit confirmed Markdown record prose directly. Record creation and edits
remain subject to normal schema and ID validation; the framework adds no
record-editing protocol.

Annotation mutation is different: agents edit one unapplied proposal and a
minimal transaction script validates, merges, and atomically applies it.
Direct edits to `annotations/matrix*.json` are forbidden. This keeps model work
efficient without adding a workflow harness around the model.

### 10.2 Behavioral acceptance

Renderer tests prove structure and transaction tests prove state safety, but neither proves that a
model changes its action. `tools/adherence_acceptance.py` creates an isolated task with one eager
Policy whose activated requirement conflicts with the user request, renders the real task profile,
runs an ephemeral Codex call under a two-choice output schema, and passes only when the Policy wins.
It never touches deployed records or annotations. This real-model check complements, rather than
replaces, deterministic unit tests.

## 11. Single maintained framework and data layout

```text
workspace/.memory/
  protocol.json
  records/
  categories/
  task-types/
  annotations/
  tools/                          # record-owned assets
  generated/
  state/
  clients/                        # runtime profiles only
  framework/                      # complete, single maintained framework
    framework.json
    lib/
    clients/                      # adapter implementation
    instructions/
    skills/
    scripts/
    bin/
    tests/
    tools/                        # framework development utilities
    packaging/
    README.md
    DESIGN.md
    PROTOCOL.md
```

There is no separately synchronized source checkout. The local authoritative framework is
`WORKSPACE/.memory/framework/`; tests and packaging run there. It remains independently installable
at another location. Its parent directory provides a default data location only; the data
protocol never records a framework path. Entry points use their own installation for imports,
instructions, skills and loader commands. Every launcher propagates the selected framework and
workspace roots to child processes. Historical audit backups are not maintained framework copies.
