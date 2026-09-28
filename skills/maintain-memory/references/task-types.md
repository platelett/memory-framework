# Manage standard task types

Create, remove, rename, or redefine a standard task only at explicit human request. Store one task
as `.memory/task-types/<id>.md` with an immutable kebab-case ID and matching filename. Reserve
`all-lazy` and `all-eager` for built-ins.

Use TOML frontmatter containing `id`, `title`, and optional `routing_hints = ["..."]`. The Markdown
body must contain these non-empty H2 sections in this exact order: `Scope`, `Boundary`, `Positive
examples`, and `Negative examples`. Each examples section contains only bullet items. Make Scope
and Boundary jointly route ambiguous requests, and include examples close enough to expose
overlap with neighboring types.

Show the exact file before writing. Run validation afterward. A new or changed task description
makes its whole shared and local row pending/stale. Its next new, resumed, or forked launch performs
one automatic selected-task preflight before rendering; use the relabel skill with `--scope task`
only when the user requests immediate or full reconsideration. Removing a task requires matrix
cleanup through a relabel proposal; never delete its live row by hand.
