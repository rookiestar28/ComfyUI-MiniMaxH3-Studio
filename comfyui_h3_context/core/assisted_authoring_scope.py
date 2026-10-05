"""Closed product truth for optional prompt-model assisted authoring.

Availability is package capability data. Selection and readiness belong to one provider session,
and authorization belongs to one explicit future action. Keeping the five facts in one validated
value prevents a presentation layer from collapsing any of those boundaries into an "enabled"
flag.
"""

from __future__ import annotations

from dataclasses import dataclass


class AssistedAuthoringScopeError(ValueError):
    """The projected assistance facts contradict one another or imply a default."""


@dataclass(frozen=True, slots=True)
class AssistedAuthoringState:
    available: bool
    selected: bool
    ready: bool
    authorized_for_this_action: bool
    defaulted: bool

    def __post_init__(self) -> None:
        values = (
            self.available,
            self.selected,
            self.ready,
            self.authorized_for_this_action,
            self.defaulted,
        )
        if any(type(value) is not bool for value in values):
            raise AssistedAuthoringScopeError("assisted-authoring facts must be exact booleans")
        # CRITICAL: catalog membership is never selection or execution authority.
        if self.defaulted:
            raise AssistedAuthoringScopeError("assisted authoring must never be defaulted")
        if self.selected and not self.available:
            raise AssistedAuthoringScopeError("selection requires an available catalog profile")
        if self.ready and not self.selected:
            raise AssistedAuthoringScopeError("readiness requires an explicit selection")
        if self.authorized_for_this_action and not self.ready:
            raise AssistedAuthoringScopeError("action authorization requires readiness")

    def to_wire(self) -> dict[str, bool]:
        return {
            "available": self.available,
            "selected": self.selected,
            "ready": self.ready,
            "authorized_for_this_action": self.authorized_for_this_action,
            "defaulted": self.defaulted,
        }


__all__ = ["AssistedAuthoringScopeError", "AssistedAuthoringState"]
