# Manage record-bound tools

Add or remove a tool only at explicit human request. Put deterministic reusable implementations
under `.memory/tools/<tool>/`; keep the conceptual rule and exact invocation in the owning memory
record. There is no independent tool annotation or discovery harness.

Propose the record prose and tool files together. Preserve external originals when importing.
Keep implementation details out of injected prose, but document when to run the tool, command,
working directory when relevant, inputs, outputs, invariants, and verification. Test a safe
representative invocation. Update the owning record whenever the interface or behavior changes,
then validate and reclassify that record.
