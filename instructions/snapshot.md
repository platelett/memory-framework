# Read-only workspace memory snapshot

This directory is a portable, query-only snapshot for one task type. Treat embedded Policy as
higher authority than embedded Reference, and Reference as higher authority than Playbook.
Every record owns a required `load_when`. Direct-loaded records display it as an activation
condition; lazy entries display the exact condition once. Use the catalog's read-only loader to
read every matching snapshot record when that condition applies. Tools are ordinary files and may be used only
when loaded record prose directs you to them.

Do not attempt to update, relabel, initialize, or reconstruct memory from this snapshot. It has
no authoritative annotation state or maintenance framework. Ask the user to make durable
changes in the source workspace and export a new snapshot.
