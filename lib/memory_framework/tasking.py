from __future__ import annotations

from .models import TaskType
from .workspace import sha256_bytes


def _built_in(
    task_id: str,
    title: str,
    scope: str,
    boundary: str,
    positive: str,
    negative: str,
) -> TaskType:
    source = (
        f"id={task_id}\ntitle={title}\nscope={scope}\nboundary={boundary}\n"
        f"positive={positive}\nnegative={negative}\n"
    )
    return TaskType(
        id=task_id,
        title=title,
        scope=scope,
        boundary=boundary,
        positive_examples=(positive,),
        negative_examples=(negative,),
        routing_hints=(),
        content_hash=sha256_bytes(source.encode("utf-8")),
        path=None,
        source=source,
        built_in=True,
    )


BUILT_IN_TASKS = {
    "all-lazy": _built_in(
        "all-lazy",
        "All records lazy",
        "Use for a concrete task when only globally mandatory memory should be preloaded.",
        "Do not use when the task has a maintained standard type or needs every record body up front.",
        "Explore an unusual one-off request with direct lazy reads as conditions match.",
        "Load the entire bank for broad analysis; use all-eager instead.",
    ),
    "all-eager": _built_in(
        "all-eager",
        "All records eager",
        "Use for a concrete task that genuinely needs every non-global record body initially.",
        "Do not use as a default merely to avoid choosing or maintaining a standard task type.",
        "Audit interactions across all durable workspace knowledge.",
        "Handle a narrow implementation request with a small relevant memory subset.",
    ),
}


def resolve_task(task_id: str, standard: dict[str, TaskType]) -> TaskType:
    if task_id in BUILT_IN_TASKS:
        return BUILT_IN_TASKS[task_id]
    try:
        return standard[task_id]
    except KeyError as exc:
        from .errors import ValidationError

        raise ValidationError(f"unknown task type: {task_id}") from exc
