"""Finite projection for failure-safe automatic Production accumulation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .canonical import canonical_bytes, canonical_fingerprint
from .production_workbench import ProductionWorkbenchProjection

PRODUCTION_ACCUMULATED_PROJECT_SCHEMA = "h3.context.production_accumulated_project.v1"
PRODUCTION_ACCUMULATION_ACTION_VERSION = "h3.context.production_accumulation.v1"
MAX_ACCUMULATED_PROJECT_ATTEMPTS = 2
MAX_ACCUMULATED_PROJECT_BYTES = 1_000_000

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HANDLE = re.compile(r"pw_[A-Za-z0-9_-]{32,96}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_STATUSES = {
    "admitted",
    "planned",
    "projected",
    "submitted",
    "running",
    "executing",
    "verification_pending",
    "output_verification_failed",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "unknown_ownership",
}
_RECOVERY = {None, "start", "retry", "verify_output"}


@dataclass(frozen=True, slots=True)
class ProductionAccumulationAttemptProjection:
    """Content-free latest-attempt facts for one server-owned candidate member."""

    candidate_id: str
    attempt_id: str
    member_segment_id: str
    status: str
    recovery: str | None
    committed: bool = False

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or _IDENTIFIER.fullmatch(value) is None
            for value in (self.candidate_id, self.attempt_id, self.member_segment_id)
        ):
            raise ValueError("invalid accumulation attempt identity")
        if self.status not in _STATUSES or self.recovery not in _RECOVERY:
            raise ValueError("invalid accumulation attempt state")
        if type(self.committed) is not bool:
            raise TypeError("invalid accumulation commit disposition")
        if self.committed and self.status != "succeeded":
            raise ValueError("committed accumulation attempt is not succeeded")

    def to_wire(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "attempt_id": self.attempt_id,
            "member_segment_id": self.member_segment_id,
            "status": self.status,
            "recovery": self.recovery,
            "committed": self.committed,
        }


@dataclass(frozen=True, slots=True)
class ProductionAccumulatedProjectProjection:
    """A stable project identity whose committed nonempty workspace may not exist yet."""

    workspace_handle: str
    workspace_id: str
    project_revision: int
    workspace: ProductionWorkbenchProjection | None
    attempts: tuple[ProductionAccumulationAttemptProjection, ...] = ()
    project_fingerprint: str | None = None
    schema: str = PRODUCTION_ACCUMULATED_PROJECT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_ACCUMULATED_PROJECT_SCHEMA:
            raise ValueError("unsupported accumulated project schema")
        if (
            type(self.workspace_handle) is not str
            or _HANDLE.fullmatch(self.workspace_handle) is None
        ):
            raise ValueError("invalid accumulated project handle")
        if type(self.workspace_id) is not str or _IDENTIFIER.fullmatch(self.workspace_id) is None:
            raise ValueError("invalid accumulated project id")
        if type(self.project_revision) is not int or not 1 <= self.project_revision <= 1_000_000:
            raise ValueError("invalid accumulated project revision")
        if (
            type(self.attempts) is not tuple
            or len(self.attempts) > MAX_ACCUMULATED_PROJECT_ATTEMPTS
            or not all(
                type(item) is ProductionAccumulationAttemptProjection for item in self.attempts
            )
            or len({item.attempt_id for item in self.attempts}) != len(self.attempts)
        ):
            raise ValueError("invalid accumulated project attempts")
        if self.workspace is not None and (
            type(self.workspace) is not ProductionWorkbenchProjection
            or self.workspace.workspace_handle != self.workspace_handle
            or self.workspace.workspace_id != self.workspace_id
        ):
            raise ValueError("accumulated workspace identity mismatch")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.project_fingerprint is None:
            object.__setattr__(self, "project_fingerprint", expected)
        elif type(self.project_fingerprint) is not str or self.project_fingerprint != expected:
            raise ValueError("accumulated project fingerprint mismatch")
        if len(canonical_bytes(self.to_wire())) > MAX_ACCUMULATED_PROJECT_BYTES:
            raise ValueError("accumulated project projection too large")

    @property
    def fingerprint(self) -> str:
        if self.project_fingerprint is None:  # pragma: no cover - initialized above
            raise ValueError("accumulated project fingerprint unavailable")
        return self.project_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "workspace_handle": self.workspace_handle,
            "workspace_id": self.workspace_id,
            "project_revision": self.project_revision,
            "workspace": None if self.workspace is None else self.workspace.to_wire(),
            "attempts": [item.to_wire() for item in self.attempts],
            "capabilities": ["read", "admit_generation", "release_generation"],
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["project_fingerprint"] = self.fingerprint
        return value


__all__ = [
    "MAX_ACCUMULATED_PROJECT_ATTEMPTS",
    "PRODUCTION_ACCUMULATED_PROJECT_SCHEMA",
    "PRODUCTION_ACCUMULATION_ACTION_VERSION",
    "ProductionAccumulatedProjectProjection",
    "ProductionAccumulationAttemptProjection",
]
