# Initialize a protocol-3 memory workspace

Use this branch only when the user explicitly asks to initialize a blank protocol-3 workspace. Verify the selected framework separately from the data bank. Create an absent bank with
`python3 "$MEMORY_FRAMEWORK_ROOT/scripts/memory.py" init`.
The embedded framework automatically selects the workspace containing its parent `.memory`.
Use `--workspace <path>` only when selecting a different workspace explicitly.
This writes empty data directories, the shared annotation matrix, the machine protocol declaration,
and `.memory/.gitignore` for local policies, local annotations, generated files, runtime state,
client profiles and Python caches. In a Git-managed workspace, prefer installing the framework
as a submodule at `.memory/framework/`; its commit remains tracked by the parent repository.
Initialization accepts an absent `.memory` or one containing only the executing embedded
`framework/`. Do not reconstruct a damaged or previously used bank as a reset; other existing
banks are refused. Cloning an existing bank needs submodule initialization, not bank initialization.

Inspect the workspace read-only and propose only durable records supported by the evidence or the
user's stated preferences. Separate shared Policy from local Policy by sharing scope. Ask which
standard task types the user wants; creating none is valid because `all-lazy` and `all-eager` are
built in. Define every proposed Reference and Playbook category before creating records in it.
Show every proposed category, record, and task file before writing, then follow the incremental
workflow and fully classify the initial shared and local matrices before declaring initialization
complete.
