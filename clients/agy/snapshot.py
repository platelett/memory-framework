from claude.snapshot import export_snapshot as _export_snapshot


def export_snapshot(workspace, task_id, output_dir, *, include_local=False):
    return _export_snapshot(
        workspace, task_id, output_dir, include_local=include_local,
        entry_filename="AGENTS.md", client_name="agy",
    )
