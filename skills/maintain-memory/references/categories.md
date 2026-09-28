# Manage record categories

Categories are the middle path and ID segment in
`.memory/records/<layer>/<category>/<slug>.md`. Reference and Playbook categories are registered
as `.memory/categories/<layer>/<category>.md`; Policy uses only the built-in `shared` and `local`
categories and has no category files.

Use strict TOML frontmatter containing exactly `id`, `title`, and `layer`. The ID is
`<layer>.<category>`, and the filename is the kebab-case category. The body contains one non-empty
`## Scope`, then one non-empty `## Boundary`, and may end with a non-empty `## Notes`. Empty
categories are valid and preserve a routing boundary without inventing records.

Add, remove, rename, or redefine a category only at explicit human request. Explain which records
move and how adjacent categories remain unambiguous. A rename changes record paths and IDs, so
treat it as replacement records: show the complete proposed move/rewrite, obtain confirmation,
validate, and reclassify. A category definition itself has no always/eager/lazy annotation, does
not stale a matrix, and is excluded from Normal, Auto, relabel corpora, and snapshots. Do not use a
category to encode ephemeral task state.
