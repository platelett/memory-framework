# Install Memory Framework 3

The archive contains only framework software. It has no records, categories, standard tasks,
annotations, credentials or runtime state. `framework.json` declares the supported data protocol.

After extraction, install the entire framework into a new directory:

```sh
./install.sh /path/to/workspace/.memory/framework
```

Or choose a standalone location:

```sh
./install.sh /opt/memory-framework
```

The installer verifies the archive manifest, refuses an existing target, and writes only the
selected framework directory. It never installs or overwrites a bank's data files.

For a new embedded bank:

```sh
python3 /path/to/workspace/.memory/framework/scripts/memory.py init
```

For a new bank with a standalone framework:

```sh
python3 /opt/memory-framework/scripts/memory.py --workspace /path/to/workspace init
```

Then launch `bin/memory-codex --auto` or `bin/memory-claude --auto`. Embedded installations infer
the parent bank; standalone installations can use `MEMORY_WORKSPACE=/path/to/workspace`.
A pre-existing compatible bank needs no initialization. Unknown protocol generations are rejected.
No prior layout is supported; upgrading old data is a separate, explicit migration operation.
