# Workspace memory operating rules

Memory prose may change only after showing the exact proposed change and destination to the
user and receiving confirmation, unless the user explicitly delegates prose and granularity
judgment for one bounded maintenance batch. Pure annotation changes need no confirmation.
Adding or changing a record in the active task uses one explicit `update submit`; unrelated
task annotations remain pending until that task's next launch preflight. A task session starts
only after its relevant annotations are fresh; pending bodies are never injected as a fallback.
Never edit generated views, task profiles, or live annotation matrices directly.

Record granularity is a mandatory maintenance invariant, not a loading annotation. Every record
must remain one self-contained semantic unit that can be independently retrieved, annotated,
maintained, and invalidated. Before changing memory prose or structure, read
`{{FRAMEWORK_ROOT}}/skills/maintain-memory/SKILL.md` in full and apply its granularity review. Before
changing loading labels independently of a routine record add/edit submission, read
`{{FRAMEWORK_ROOT}}/skills/relabel-memory/SKILL.md` in full. The routine workflow uses one explicit
`update submit` command instead of a separate relabel cycle.

Policy constraints are mandatory when their activation condition matches. Reference is the
authoritative factual layer. Playbook is default practice that yields to Policy, Reference, and
direct task evidence. Loading state never changes authority. Every record owns a required
`load_when`, independent of its loading label; full bodies display it as an activation condition
and lazy catalogs display the exact condition once. When a lazy condition matches, use the
catalog's loader command and read every matching record before relying on it. Conditions are
not unique identifiers; records with the same condition must be read together.

Direct-loaded records contain a stable short ID and semantic content. Before modifying one,
use the displayed loader command with `--id` to retrieve its current full ID, source path,
content hash and complete source. Short IDs are lookup keys, not content-version hashes.
