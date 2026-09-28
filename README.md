# Memory Framework 3

This repository is a client-neutral framework for data protocol generation 3. Memory records and
annotation matrices are authoritative; generated views and client profiles are disposable caches.
It ships with only the virtual task types `all-lazy` and `all-eager`.

Requirements: Python 3.12+, POSIX file locking, and (for launching) a current Codex CLI or
Claude Code CLI.

## Framework and data boundary

This directory is the single maintained framework, including implementation, tests, packaging
and design documentation. Maintain the selected installation directly; there is no separately
synchronized source checkout. The framework is independently installable.
A knowledge bank is a workspace's `.memory/` data directory. Its `records/`, `categories/`,
`task-types/`, `annotations/` and record-bound `tools/` do not depend on an installation path.
Generated views and client runtime files are local to the bank and are not portable knowledge.

The recommended installation is `WORKSPACE/.memory/framework/`; a standalone installation such
as `/opt/memory-framework` also works. Explicit `--workspace` (scripts) or `MEMORY_WORKSPACE`
(clients and scripts) wins. Without either, an embedded framework uses the parent `.memory`;
otherwise the nearest `.memory` in cwd's ancestors is selected. An incompatible nearer bank is
an error, not a reason to silently fall through to another bank.

`framework.json` declares the software release and supported data protocol separately.
The bank's `.memory/protocol.json` declares protocol name/generation and bank versus snapshot.
Programs validate declarations before data operations; there is no legacy-layout fallback.
The machine contract is specified in [PROTOCOL.md](PROTOCOL.md) and enforced by
`lib/memory_protocol.py` plus the record, task, category and matrix parsers.

From this framework directory, create a data-only bank with:

```bash
python3 scripts/memory.py --workspace /path/to/workspace init
MEMORY_WORKSPACE=/path/to/workspace bin/memory-codex --auto
```

For an embedded installation, `python3 .memory/framework/scripts/memory.py init` infers the
parent workspace. Initialization accepts an absent bank or a directory containing only this
embedded `framework/`; it refuses to reset an existing bank. Upgrades replace the framework
installation as a unit and retain data. They do not copy instructions or skills into the bank.

The following examples run from this framework directory. An embedded installation infers its
parent bank; an external installation can set `MEMORY_WORKSPACE`.

## Commands

```bash
python3 scripts/memory.py validate
python3 scripts/memory.py render --task all-lazy
python3 scripts/load.py 'EXACT LOAD_WHEN TEXT'
python3 scripts/load.py --id SHORT_ID
python3 scripts/memory.py update submit "$MEMORY_WORKSPACE/.memory/generated/updates/<id>/submission.json"
python3 scripts/memory.py relabel prepare --scope pending --sharing shared
python3 scripts/memory.py relabel apply "$MEMORY_WORKSPACE/.memory/generated/relabel/<id>/proposal.json"
python3 tools/adherence_acceptance.py # real-model Policy behavior check

bin/memory-codex                 # select a Memory task, then start Codex
bin/memory-codex --task all-lazy
bin/memory-codex --auto "request" # --auto is a pure flag
bin/memory-codex --raw exec "request" # exact Codex passthrough
bin/memory-codex resume            # choose Memory task, then Codex session
bin/memory-codex --list
bin/memory-codex --snapshot all-lazy /path/to/new-snapshot

bin/memory-claude                # same task selector for Claude Code
bin/memory-claude --task all-lazy
bin/memory-claude --auto "request"
bin/memory-claude --raw --print "request"
bin/memory-claude --version       # automatic utility passthrough; no --raw needed
bin/memory-claude --list
bin/memory-claude --snapshot all-lazy /path/to/new-claude-snapshot
```

The Claude Code wrapper has the same Memory modes and two-phase Auto behavior. For normal and
Auto launches it creates one owner-only, launch-unique Markdown artifact under
`.memory/generated/clients/claude/launches/`, passes it with
`--append-system-prompt-file`, waits for Claude Code, and removes the artifact. It never creates
or rewrites a workspace `CLAUDE.md`. Claude Code therefore continues to load the user's existing
`~/.claude/CLAUDE.md`, project `CLAUDE.md`, settings, and either replacement system-prompt form in
their native order, while Memory is appended at system-prompt level for that invocation. The
wrapper owns Claude Code's append flags so a second append cannot shadow or reorder selected
memory. Client routing preferences are isolated under `.memory/state/clients/claude/`.
Each launch also has an owner-only PID/start-time registry; a later invocation removes only stale,
identity-matching prompt artifacts left by an interrupted wrapper.

Claude Code utility commands such as `auth`, `mcp`, `plugin`, `doctor`, `update`, and
`--version` are exact passthroughs and create no Memory runtime state. `ultrareview` fails closed
because its cloud review path has not been verified to carry the local appended system prompt.
The `agents`, `attach`, and `respawn` background-agent transports are guarded for the same reason;
`--raw` is their explicit bypass. Resume/continue launches receive the selected prompt, but the
wrapper warns to keep the task that created the conversation because earlier task context can
remain in session history. A Claude snapshot uses `CLAUDE.md` as its read-only entry document;
normal launches do not.

Without an explicit Memory mode, every local Codex operation that can start an LLM turn first
shows `Select a memory task type`: the interactive TUI, prompts, `exec`/`e`, `review`,
`resume`, `exec resume`, and `fork`. `--task` starts one task-bound profile directly; choosing
`auto` from the selector or using `--auto` starts automatic routing. Auto's handoff is
optional: if Auto creates no handoff, the wrapper exits after that one session; only an explicit
handoff to a standard task starts a second session whose memory must remain stable in
`developer_instructions`. Built-in `all-lazy` and `all-eager` modes are explicit `--task` choices,
not Auto routing targets. Requests Auto can fulfill itself stay in the first session.
This includes workspace initialization, category and task-type management, record and
record-bound tool maintenance, and relabeling, which read authoritative skill files on demand.
There is no separate maintainer role or command. These launches do not generate repository entry
documents. Snapshot export is the only operation that creates an `AGENTS.md`, inside a new
read-only snapshot directory.

The selector reads from the controlling terminal, not redirected stdin, so `codex exec -` cannot
consume the model prompt as a task choice. A launch with no controlling terminal fails before
Codex starts and must name `--task TASK` or `--auto` explicitly.

The directory containing `.memory/` is the memory workspace, not an automatic Codex working
directory. The wrapper preserves the shell process directory while honoring Codex `-C/--cd` as
the effective project directory for override checks and learned hints. After task selection, a
bare `resume` continues to Codex's session picker, while an ID or `--last` keeps that target.
Explicit `--auto resume ...` continues in Auto. Utility commands
(`--version`, help, login/logout, mcp/plugin, doctor, completion, archive/delete, and peers) are
transparent and create no Memory runtime state.

Before every standard or built-in task launch—including new, `resume`, and `fork`—the wrapper
checks the selected task's annotation coverage. Pending/stale cells are classified outside the
main session by one ephemeral non-interactive Codex call over a complete label-free shared/local
bundle and an exact JSON output schema. That classifier uses no tools or filesystem evidence and
returns decisions only; the parent records proposal nulls, validates pinned hashes, atomically
applies each matrix, and then renders the latest task profile. It never classifies another task
row, and normal rendering fails closed instead of direct-loading pending bodies.

Current Codex accepts the freshly selected profile on resume/fork and carries the refreshed
developer instructions through later compaction. Auto continuations may therefore hand their
canonical session ID to a standard task; the wrapper preflights that task and resumes the same
thread under the newest profile. Ephemeral continuations cannot hand off because they have no
durable session.

Codex cloud task submission is the one guarded incompatibility: upstream `cloud` rejects
profiles and discards local instruction overrides. The wrapper still shows the task selector for
bare `cloud` and `cloud exec`, but refuses to submit after selection rather than run an LLM task
without the selected memory. `--raw cloud ...` is the explicit bypass; read-only cloud
`list`/`status`/`diff`/`apply` remain transparent utilities.
The internal `debug app-server send-message-v2` path is guarded for the same reason: it sends an
LLM message but current Codex does not accept a profile for that subcommand. Server startup
commands remain transparent because starting a protocol server does not itself make an LLM call.

Handoffs are v2 nonce-bound owner-only regular files. New-request handoffs preserve the original
request; continuation handoffs carry the canonical session ID without repeating the request.
Every launch uses a freshly rendered unique profile and registration guarded by PID plus
process-start-time ownership metadata.

Records use `.memory/records/<layer>/<category>/<slug>.md`; standard task definitions use
`.memory/task-types/<id>.md`. Reference and Playbook categories are declared independently at
`.memory/categories/<layer>/<category>.md`; category metadata is routing-only and is never loaded
or annotated as a memory record. Read the maintenance skills before changing these sources.

For a routine confirmed record add/edit, the agent prepares only the record prose and an explicit
submission containing its always decision plus the active standard task's eager/lazy decision.
One `update submit` call validates and updates only those listed record IDs; it does not render
task views. Other task cells remain pending until their own next launch preflight. Generated
views, relabel corpora/proposals, launch instructions, profiles, and metrics are disposable;
records, task definitions, and annotation matrices are the persistent semantic state.

Task classification uses a recall-biased balance rather than an unconditional recall-first
default: applicability frequency, miss cost, trigger recognizability, and attention cost all
matter; a genuine tie goes to eager. Full bodies always display their `load_when` as an activation
condition. Direct-loaded records show only a 12-character short ID, title, activation condition
and body; the ID is the first 12 hexadecimal digits of SHA-256 of the immutable full record ID.
Content edits do not change it. Renderers check for collisions and ID lookup fails on ambiguity.
Before maintaining a record, use `load.py --id SHORT_ID` to retrieve its current full ID, source
path, content hash and complete source. This key is not a content-version hash. Portable snapshots
copy all included records so eager provenance lookup also works without the source workspace.
Rendered sections state their behavior explicitly—Policy is mandatory when activated,
Reference is authoritative fact, and Playbook is default practice. Lazy catalogs contain only
distinct, verbatim `load_when` conditions. The read-only `scripts/load.py` accepts an
exact condition and returns every matching record with its source and full frontmatter/body;
identical conditions intentionally group records, including across authority layers. No match
is an error, and there is no fuzzy fallback or seen-set. Use `--stdin` with a quoted heredoc for
shell-sensitive text. Explicit `--workspace` or `MEMORY_WORKSPACE` selects the data root. Otherwise an embedded
`.memory/framework/` uses its parent bank, and an external installation searches cwd ancestors. `--exclude-local` omits local policies.
Portable snapshots include the
same standalone loader for their copied records. Record IDs, paths and hashes remain available
in authoritative files and manifests instead of being repeated in every lazy catalog entry.

Memory qualification is not limited to universal conclusions. An evidence-backed, scoped search
prior may remain in Playbook when it improves the order of future trials, provided the record keeps
observations, inferences, recommendations, adoption decisions, transfer conditions, and decision
evidence distinct. Maintained semantic prose is written in English; user-facing conversation may
use any language.

## Developing and packaging

Maintain this directory directly; no deployment back to a second source tree is required.

```bash
python3 -m pytest -q
python3 tools/build_clean_package.py . /path/to/memory-framework-3.0.0.tar.gz
```

The package whitelist contains runtime software only. Knowledge and local runtime state are
never included. Source backups in the audit directory are historical snapshots, not maintained copies.
