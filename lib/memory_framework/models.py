from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RecordCategory:
    id: str
    title: str
    layer: str
    scope: str
    boundary: str
    notes: str | None
    path: Path
    source: str


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    title: str
    layer: str
    category: str
    load_when: str
    body: str
    path: Path
    relative_path: str
    content_hash: str
    sharing: str
    source: str


@dataclass(frozen=True, slots=True)
class TaskType:
    id: str
    title: str
    scope: str
    boundary: str
    positive_examples: tuple[str, ...]
    negative_examples: tuple[str, ...]
    routing_hints: tuple[str, ...]
    content_hash: str
    path: Path | None
    source: str
    built_in: bool = False

    def description_markdown(self) -> str:
        positive = "\n".join(f"- {item}" for item in self.positive_examples)
        negative = "\n".join(f"- {item}" for item in self.negative_examples)
        hints = ""
        if self.routing_hints:
            hints = "\n\n## Routing hints\n\n" + "\n".join(
                f"- {item}" for item in self.routing_hints
            )
        return (
            f"# {self.title}\n\n"
            f"ID: `{self.id}`\n\n"
            f"## Scope\n\n{self.scope}\n\n"
            f"## Boundary\n\n{self.boundary}\n\n"
            f"## Positive examples\n\n{positive}\n\n"
            f"## Negative examples\n\n{negative}{hints}\n"
        )


@dataclass(frozen=True, slots=True)
class CoverageIssue:
    sharing: str
    kind: str
    record_id: str | None = None
    task_id: str | None = None
    detail: str = ""

    def describe(self) -> str:
        target = "/".join(
            part for part in (self.sharing, self.task_id, self.record_id) if part
        )
        return f"{self.kind}: {target}: {self.detail}".rstrip(": ")
