"""Pure migration contract for the transparent Reference assistant Subgraph UX."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .contracts import TaskMode
from .errors import ReferenceAssistantMigrationError
from .length import LengthError, resolve_frames, resolve_milliseconds
from .normalization import MAX_USER_INTENT_LENGTH

REFERENCE_ASSISTANT_UX_SCHEMA = "h3-context-reference-assistant/1"
REFERENCE_ASSISTANT_MIGRATION_SCHEMA = "h3-context-reference-assistant-migration/1"
LEGACY_REFERENCE_SUBGRAPH_SCHEMA = "h3-context-subgraph-fixture/1"
LEGACY_REFERENCE_SUBGRAPH_VERSION = 1


@dataclass(frozen=True, slots=True)
class ReferenceAssistantControls:
    """Visible request controls forwarded to the canonical REF2VA Request node."""

    task_mode: TaskMode
    user_intent: str
    duration_milliseconds: int

    def __post_init__(self) -> None:
        if self.task_mode is not TaskMode.REF2VA:
            raise ReferenceAssistantMigrationError("Reference assistant task_mode must be ref2va")
        if not isinstance(self.user_intent, str) or not self.user_intent.strip():
            raise ReferenceAssistantMigrationError("user_intent must be a non-empty string")
        if len(self.user_intent) > MAX_USER_INTENT_LENGTH:
            raise ReferenceAssistantMigrationError("user_intent exceeds the bounded control length")
        if any(
            (ord(char) < 0x20 and char not in "\n\r\t") or ord(char) == 0x7F
            for char in self.user_intent
        ):
            raise ReferenceAssistantMigrationError(
                "user_intent contains a forbidden control character"
            )
        if isinstance(self.duration_milliseconds, bool) or not isinstance(
            self.duration_milliseconds, int
        ):
            raise ReferenceAssistantMigrationError("duration_milliseconds must be an integer")
        try:
            resolve_milliseconds(self.duration_milliseconds)
        except LengthError as exc:
            raise ReferenceAssistantMigrationError(
                "duration_milliseconds is outside the supported bounds"
            ) from exc

    @property
    def frame_count(self) -> int:
        """Return the derived producible length; it is never authored here."""

        return resolve_milliseconds(self.duration_milliseconds).frame_count

    @property
    def snapped(self) -> bool:
        """Return whether the delivered duration differs from the authored one."""

        return resolve_milliseconds(self.duration_milliseconds).snapped

    def to_wire(self) -> dict[str, object]:
        """Return the exact values expected by the canonical Request node."""

        return {
            "task_mode": self.task_mode.value,
            "user_intent": self.user_intent,
            "duration_seconds": self.duration_milliseconds / 1000,
        }


def migrate_reference_assistant_widgets(
    legacy: Mapping[str, object],
) -> ReferenceAssistantControls:
    """Migrate the accepted no-input Reference Subgraph widget tuple strictly.

    The old Reference Subgraph had no external inputs; its canonical Request node carried exactly
    three widgets. Unknown fields, versions, modes, and values are rejected so a stale workflow
    cannot silently alter prompt semantics.
    """

    if not isinstance(legacy, Mapping):
        raise ReferenceAssistantMigrationError(
            "legacy Reference Subgraph envelope must be an object"
        )
    if set(legacy) != {"schema", "version", "widgets_values"}:
        raise ReferenceAssistantMigrationError(
            "legacy Reference Subgraph envelope contains unknown fields"
        )
    if legacy.get("schema") != LEGACY_REFERENCE_SUBGRAPH_SCHEMA:
        raise ReferenceAssistantMigrationError("unsupported legacy Reference Subgraph schema")
    if legacy.get("version") != LEGACY_REFERENCE_SUBGRAPH_VERSION:
        raise ReferenceAssistantMigrationError("unsupported legacy Reference Subgraph version")
    widgets = legacy.get("widgets_values")
    if not isinstance(widgets, list) or len(widgets) != 3:
        raise ReferenceAssistantMigrationError("legacy widgets_values must contain three controls")
    task_mode, user_intent, frame_count = widgets
    if not isinstance(task_mode, str):
        raise ReferenceAssistantMigrationError("legacy task_mode must be a string")
    try:
        mode = TaskMode(task_mode)
    except ValueError as exc:
        raise ReferenceAssistantMigrationError("legacy task_mode is unsupported") from exc
    if mode is not TaskMode.REF2VA:
        raise ReferenceAssistantMigrationError("legacy Reference task_mode must be ref2va")
    if not isinstance(user_intent, str):
        raise ReferenceAssistantMigrationError("legacy user_intent must be a string")
    if isinstance(frame_count, bool) or not isinstance(frame_count, int):
        raise ReferenceAssistantMigrationError("legacy frame_count must be an integer")
    # M17-25: the legacy widget authored a frame count. It is converted here into
    # the duration that produces exactly the length alignment would have given it,
    # and the conversion is reported through `snapped` rather than applied
    # silently. Values outside the accepted range fail rather than clamp.
    try:
        migrated = resolve_frames(frame_count)
    except LengthError as exc:
        raise ReferenceAssistantMigrationError(
            "legacy frame_count is outside the supported bounds"
        ) from exc
    return ReferenceAssistantControls(mode, user_intent, migrated.requested_milliseconds)


__all__ = [
    "REFERENCE_ASSISTANT_MIGRATION_SCHEMA",
    "REFERENCE_ASSISTANT_UX_SCHEMA",
    "LEGACY_REFERENCE_SUBGRAPH_SCHEMA",
    "LEGACY_REFERENCE_SUBGRAPH_VERSION",
    "ReferenceAssistantControls",
    "ReferenceAssistantMigrationError",
    "migrate_reference_assistant_widgets",
]
