from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Protocol

from memory_framework.errors import ValidationError


class ClientAdapter(Protocol):
    def render_task_artifact(self, task_id: str) -> Path: ...

    def launch_task(self, task_id: str, arguments: list[str]) -> None: ...


HANDOFF_VERSION = 2


def load_handoff(
    path: Path,
    *,
    expected_nonce: str,
    expected_kind: str | None = None,
    expected_request: str | None = None,
) -> dict[str, object]:
    """Load and authenticate one client-routing handoff."""
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValidationError(
            f"routing session did not create handoff: {path}"
        ) from exc
    except OSError as exc:
        raise ValidationError(f"cannot inspect routing handoff {path}: {exc}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_nlink != 1
    ):
        raise ValidationError(
            "routing handoff must be one ordinary regular file, not a symlink"
        )
    if metadata.st_uid != os.getuid():
        raise ValidationError("routing handoff must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValidationError("routing handoff mode must be 0600")
    if metadata.st_size > 65536:
        raise ValidationError("routing handoff is unreasonably large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read routing handoff {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("routing handoff must be an object")
    common = {
        "version",
        "nonce",
        "mode",
        "kind",
        "task",
        "remember",
        "normalized_intent",
        "directory_hint",
        "memory_preference",
    }
    kind = value.get("kind")
    allowed = common | ({"request"} if kind == "new" else {"session_id"})
    if set(value) - allowed:
        raise ValidationError(
            f"routing handoff has unknown fields: {sorted(set(value) - allowed)}"
        )
    if value.get("version") != HANDOFF_VERSION:
        raise ValidationError(f"routing handoff version must be {HANDOFF_VERSION}")
    if value.get("nonce") != expected_nonce:
        raise ValidationError("routing handoff nonce does not match this launch")
    if value.get("mode") != "task":
        raise ValidationError("routing handoff mode must be task")
    if kind not in {"new", "continue"}:
        raise ValidationError("routing handoff kind must be new or continue")
    if expected_kind is not None and kind != expected_kind:
        raise ValidationError(f"routing handoff kind must be {expected_kind}")
    if not isinstance(value.get("task"), str):
        raise ValidationError("task routing handoff task must be a string")
    for key in ("normalized_intent", "directory_hint", "memory_preference"):
        if key in value and not isinstance(value[key], str):
            raise ValidationError(f"routing handoff {key} must be a string")
    if "remember" in value and not isinstance(value["remember"], bool):
        raise ValidationError("routing handoff remember must be a boolean")
    if kind == "new":
        if not isinstance(value.get("request"), str):
            raise ValidationError("new routing handoff request must be a string")
        if expected_request is not None and value["request"] != expected_request:
            raise ValidationError(
                "routing handoff did not preserve the original request verbatim"
            )
        if "session_id" in value:
            raise ValidationError("new routing handoff cannot contain session_id")
    else:
        session_id = value.get("session_id")
        if not isinstance(session_id, str):
            raise ValidationError(
                "continue routing handoff session_id must be a UUID string"
            )
        try:
            parsed = uuid.UUID(session_id)
        except ValueError as exc:
            raise ValidationError(
                "continue routing handoff session_id must be a canonical UUID"
            ) from exc
        if str(parsed) != session_id:
            raise ValidationError(
                "continue routing handoff session_id must be a canonical UUID"
            )
        if "request" in value:
            raise ValidationError("continue routing handoff cannot repeat the request")
    return value
