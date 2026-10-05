"""Pure migration contract for the transparent Base assistant Subgraph UX."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .contracts import TaskMode
from .errors import BaseAssistantMigrationError
from .length import LengthError, resolve_frames, resolve_milliseconds
from .normalization import MAX_USER_INTENT_LENGTH

BASE_ASSISTANT_UX_SCHEMA = "h3-context-base-assistant/1"
BASE_ASSISTANT_MIGRATION_SCHEMA = "h3-context-base-assistant-migration/1"
LEGACY_BASE_SUBGRAPH_SCHEMA = "h3-context-subgraph-fixture/1"
LEGACY_BASE_SUBGRAPH_VERSION = 1


@dataclass(frozen=True, slots=True)
class BaseAssistantControls:
    """The three visible controls forwarded to the canonical Request node."""

    task_mode: TaskMode
    user_intent: str
    duration_milliseconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.task_mode, TaskMode):
            raise BaseAssistantMigrationError("task_mode must be a supported TaskMode")
        if not isinstance(self.user_intent, str) or not self.user_intent.strip():
            raise BaseAssistantMigrationError("user_intent must be a non-empty string")
        if len(self.user_intent) > MAX_USER_INTENT_LENGTH:
            raise BaseAssistantMigrationError("user_intent exceeds the bounded control length")
        if any(
            (ord(char) < 0x20 and char not in "\n\r\t") or ord(char) == 0x7F
            for char in self.user_intent
        ):
            raise BaseAssistantMigrationError("user_intent contains a forbidden control character")
        if isinstance(self.duration_milliseconds, bool) or not isinstance(
            self.duration_milliseconds, int
        ):
            raise BaseAssistantMigrationError("duration_milliseconds must be an integer")
        try:
            resolve_milliseconds(self.duration_milliseconds)
        except LengthError as exc:
            raise BaseAssistantMigrationError(
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


def migrate_base_assistant_widgets(legacy: Mapping[str, object]) -> BaseAssistantControls:
    """Migrate the accepted no-input Base Subgraph widget tuple into named controls.

    The old shape is deliberately narrow: unknown keys, schema versions, or widget positions are
    rejected so a stale workflow cannot silently change prompt semantics.
    """

    if not isinstance(legacy, Mapping):
        raise BaseAssistantMigrationError("legacy Base Subgraph envelope must be an object")
    if set(legacy) != {"schema", "version", "widgets_values"}:
        raise BaseAssistantMigrationError("legacy Base Subgraph envelope contains unknown fields")
    if legacy.get("schema") != LEGACY_BASE_SUBGRAPH_SCHEMA:
        raise BaseAssistantMigrationError("unsupported legacy Base Subgraph schema")
    if legacy.get("version") != LEGACY_BASE_SUBGRAPH_VERSION:
        raise BaseAssistantMigrationError("unsupported legacy Base Subgraph version")
    widgets = legacy.get("widgets_values")
    if not isinstance(widgets, list) or len(widgets) != 3:
        raise BaseAssistantMigrationError("legacy widgets_values must contain three controls")
    task_mode, user_intent, frame_count = widgets
    if not isinstance(task_mode, str):
        raise BaseAssistantMigrationError("legacy task_mode must be a string")
    try:
        mode = TaskMode(task_mode)
    except ValueError as exc:
        raise BaseAssistantMigrationError("legacy task_mode is unsupported") from exc
    if not isinstance(user_intent, str):
        raise BaseAssistantMigrationError("legacy user_intent must be a string")
    if isinstance(frame_count, bool) or not isinstance(frame_count, int):
        raise BaseAssistantMigrationError("legacy frame_count must be an integer")
    # M17-25: the legacy widget authored a frame count. It is converted here into
    # the duration that produces exactly the length alignment would have given it,
    # and the conversion is reported through `snapped` rather than applied
    # silently. Values outside the accepted range fail rather than clamp.
    try:
        migrated = resolve_frames(frame_count)
    except LengthError as exc:
        raise BaseAssistantMigrationError(
            "legacy frame_count is outside the supported bounds"
        ) from exc
    return BaseAssistantControls(mode, user_intent, migrated.requested_milliseconds)


__all__ = [
    "BASE_ASSISTANT_MIGRATION_SCHEMA",
    "BASE_ASSISTANT_UX_SCHEMA",
    "BaseAssistantControls",
    "LEGACY_BASE_SUBGRAPH_SCHEMA",
    "LEGACY_BASE_SUBGRAPH_VERSION",
    "migrate_base_assistant_widgets",
]
