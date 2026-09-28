# Workspace memory data protocol, generation 3

This is a program interface, not guidance for an agent writing prose. The runtime
enforces it through `lib/memory_protocol.py`, the parsers in
`lib/memory_framework/`, and the annotation/update validators. Knowledge
content, classification heuristics and client UI are not compatibility declarations.

## Installation and data locations

An installation root contains `framework.json`, `bin/`, `lib/`, `clients/`,
`scripts/`, `instructions/` and `skills/`. These are replaced as one software unit.
They are never imported or read from corresponding directories in the data bank.

The public `--workspace` argument and `MEMORY_WORKSPACE` environment variable
identify a workspace root; its data bank is the `.memory/` child. Resolution order:

1. Explicit script `--workspace`.
2. Explicit `MEMORY_WORKSPACE`.
3. If the executing installation is directly inside `.memory/` (normally
   `.memory/framework/`), that parent bank's workspace.
4. The nearest `.memory/` in the current working directory or its ancestors.

An explicit or nearer incompatible bank is rejected, not skipped. An external
framework can target any compatible bank. A bank never stores the framework path.
The software's own filesystem location selects its implementation and assets;
`MEMORY_FRAMEWORK_ROOT` is propagated to agents for commands, not used to silently
swap imports underneath an already selected executable.

## Machine declarations

The installation's `framework.json` has exactly these fields:

```json
{
  "kind": "memory-framework",
  "release": "3.0.0",
  "data_protocol": {"name": "workspace-memory", "generation": 3}
}
```

`release` identifies software, independently of data compatibility. Different
releases supporting the same generation can access the same bank. This
implementation supports exactly generation 3 and checks its own declaration.
Unknown names, generations, fields or types fail; no migration is inferred.

The bank's `.memory/protocol.json` has exactly these fields:

```json
{
  "kind": "bank",
  "protocol": {"name": "workspace-memory", "generation": 3}
}
```

Generation is an integer, not a boolean or string. A portable read-only export
uses `kind: "snapshot"`; its minimal reader installation uses
`kind: "memory-reader"`. The full runtime rejects snapshots. The standalone
loader accepts either bank kind, performs no writes, and a bundled snapshot
reader defaults to its sibling snapshot bank rather than the caller's live bank.

Both declarations are checked before workspace access. Mutation and annotation
lock entry recheck them so a changed declaration does not reuse a previously
constructed compatible workspace object unchecked.

## Knowledge files

| Path below `.memory/` | Machine format |
| --- | --- |
| `records/<layer>/<category>/<slug>.md` | UTF-8 Markdown with strict `+++` TOML frontmatter |
| `categories/<layer>/<category>.md` | Strict TOML identity fields and Scope/Boundary sections |
| `task-types/<id>.md` | Strict TOML identity/routing fields and prescribed task sections |
| `annotations/matrix.json` | Shared annotation matrix, component schema version 2 |
| `annotations/matrix.local.json` | Optional local annotation matrix, component schema version 2 |
| `tools/` | Opaque record-owned assets; framework does not import them |

Records have exactly five single-line, nonempty, trimmed string fields:
`id`, `title`, `layer`, `category`, `load_when`. The body is nonempty. Layer is
`policy`, `reference` or `playbook`. Category and filename use kebab-case; policy
categories are `shared` and `local`. Directory and frontmatter identities must
agree; `id` equals `<layer>.<category>.<slug>`. Reference/playbook categories
must be declared in a full bank. Snapshots retain only copied record sources,
not a mutable category/task/annotation database.

`load_when` is not unique. Exact text lookup returns every match without changing
case, spacing or punctuation. The short record key is the first 12 hexadecimal
digits of SHA-256 of the UTF-8 immutable full ID. It is independent of the content
version; collisions must fail explicitly. Metadata lookup returns current source
and full-byte SHA-256. IDs remain stable for a record; content hashes change on
every source-byte change, including frontmatter and whitespace.

Categories have exactly `id`, `title`, `layer`, with identity matching their path.
Required sections are Scope and Boundary, with optional Notes. Tasks have exactly
`id`, `title`, `routing_hints`; required sections are Scope, Boundary, Positive
examples, Negative examples, with optional Routing hints. Built-in task names are
reserved. The runtime's strict parsers enforce these schemas.

## Annotation state and publication

The component version in annotation files remains `2`; it denotes the matrix
encoding, not the encompassing protocol generation. A matrix has exactly
`version`, `always`, `tasks`. Always cells hold `label` and `record_hash`; task
rows hold `task_hash` and `records`. Each task cell holds `label`, `record_hash`,
`always_label`. The accepted labels and SHA-256 forms are checked by code.

Task annotation freshness includes the task source hash and the framework's
classification-policy fingerprint. Compatible frameworks may use different
classification policies: the records remain readable, but affected labels require
refresh before task launch. Same-generation compatibility is not a promise that
every cached classification remains fresh or that every runtime/client is identical.

Missing/stale labels are valid pending state, not a different data protocol. Launch
requires fresh coverage for its selected task. Publication holds the workspace's
annotation lock, verifies record/task hashes and proposal fingerprints, then uses
atomic replacement. It must not overwrite unrelated concurrent record changes.

## Local runtime and portability

`generated/`, client profiles under `clients/`, locks and active registrations are
workspace-local runtime state. Durable local routing preferences reside under
`state/clients/`. These are separate from the portable knowledge set above; moving
knowledge does not transfer process leases, live registrations or generated paths.
`.memory/FROZEN` prevents mutable framework operations.

The optional `.memory/framework/` is a software installation, not knowledge or
runtime state. Packaging/deployment carries only framework files. `init` creates
data only and refuses an existing bank; it also accepts a directory containing
only the executing embedded `framework/`. It does not repair or migrate old banks.
There is no compatibility branch for the previous mixed layout.
