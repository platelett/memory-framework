# Initialize a protocol-3 memory workspace

Use this branch only when the user explicitly asks to initialize a blank protocol-3 workspace. Verify the selected framework separately from the data bank. Create an absent bank with
`python3 "$MEMORY_FRAMEWORK_ROOT/scripts/memory.py" --workspace "$MEMORY_WORKSPACE" init`.
This writes data directories and the machine protocol declaration only. Do not reconstruct a
damaged or previously used bank as a reset; initialization refuses an existing `.memory`.

Inspect the workspace read-only and propose only durable records supported by the evidence or the
user's stated preferences. Separate shared Policy from local Policy by sharing scope. Ask which
standard task types the user wants; creating none is valid because `all-lazy` and `all-eager` are
built in. Define every proposed Reference and Playbook category before creating records in it.
Show every proposed category, record, and task file before writing, then follow the incremental
workflow and fully classify the initial shared and local matrices before declaring initialization
complete.
