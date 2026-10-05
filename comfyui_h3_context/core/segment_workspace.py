"""Canonical multi-segment workspace revisions and producer manifests.

This pure module owns content-free authoring identity only. It does not compile prompts, inspect
media, decide artifact reuse, queue host work, or implement continuity execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .canonical import canonical_bytes, canonical_fingerprint
from .contracts import TaskMode
from .length import LengthError, ResolvedLength, resolve_frames, resolve_milliseconds

MULTI_SEGMENT_WORKSPACE_SCHEMA = "h3.context.multi_segment_workspace.v1"
SEGMENT_CONTEXT_MANIFEST_SCHEMA = "h3.context.segment_manifest.v1"
MAX_WORKSPACE_SEGMENTS = 64
MAX_WORKSPACE_REFERENCES = 64
MAX_WORKSPACE_SELECTIONS = 64
MAX_WORKSPACE_ANCESTRY_DEPTH = 63
MAX_WORKSPACE_REVISION = 1_000_000
# M17-25: there is no separate workspace frame-count bound. The frame count is
# derived from the authored duration by `.length`, the single alignment
# authority, so the only bound that can apply is that authority's own.
MAX_WORKSPACE_DURATION_MILLISECONDS = 86_400_000
MAX_WORKSPACE_WIRE_BYTES = 262_144

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class SegmentWorkspaceError(ValueError):
    """Raised when a workspace revision or derived manifest is not canonical."""


class SegmentRelationKind(str, Enum):
    """Declared producer dependency or explicit propagation boundary."""

    INDEPENDENT = "independent"
    PREDECESSOR = "predecessor"
    ADJACENT_PAIR = "adjacent_pair"
    CUT = "cut"
    RESET = "reset"


@dataclass(frozen=True, slots=True)
class AcceptedIntentAuthority:
    """Exact accepted IntentGraph/semantic authority presented for one segment edit."""

    segment_id: str
    accepted_intent_fingerprint: str

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "authority.segment_id")
        _fingerprint(
            self.accepted_intent_fingerprint,
            "authority.accepted_intent_fingerprint",
        )


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise SegmentWorkspaceError(f"bounded_identifier:{field}")
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise SegmentWorkspaceError(f"sha256_fingerprint:{field}")
    return value


def _optional_fingerprint(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _fingerprint(value, field)


def _positive_integer(value: object, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise SegmentWorkspaceError(f"positive_integer:{field}")
    return value


def _identifier_tuple(values: object, field: str, maximum: int) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > maximum:
        raise SegmentWorkspaceError(f"identifier_tuple:{field}")
    result = tuple(_identifier(value, f"{field}[{index}]") for index, value in enumerate(values))
    if len(result) != len(set(result)):
        raise SegmentWorkspaceError(f"duplicate_identifier:{field}")
    return result


@dataclass(frozen=True, slots=True)
class SegmentDuration:
    """One authored duration in integer milliseconds, and the length it delivers.

    M17-25: the authored member is a duration and nothing else. The frame count is
    derived through `.length`, so a segment cannot represent a length the model
    cannot produce -- the previous contract accepted any positive frame count and
    the repository's own fixtures held 100, 300 and 180, none of them producible.

    The derived members are properties rather than stored fields so that a wire
    payload cannot assert a frame count its duration does not imply. Reading a
    workspace recomputes them; forging them is not possible.
    """

    duration_milliseconds: int

    def __post_init__(self) -> None:
        _positive_integer(
            self.duration_milliseconds,
            "duration.duration_milliseconds",
            MAX_WORKSPACE_DURATION_MILLISECONDS,
        )
        try:
            resolve_milliseconds(self.duration_milliseconds)
        except LengthError as error:
            raise SegmentWorkspaceError(f"duration_not_producible:{error.code}") from error

    @classmethod
    def from_frame_count(cls, frame_count: int) -> SegmentDuration:
        """Return the duration a frame count represents, failing closed off-range.

        Used both by the producers, whose frame count already came from the
        Context normalization, and by workspace migration. Over every lattice
        value the round trip is exact, and off the lattice the result is exactly
        what alignment would have given -- the conversion never invents a third
        answer. Values outside the accepted range fail rather than clamp.
        """

        try:
            resolved = resolve_frames(frame_count)
        except LengthError as error:
            raise SegmentWorkspaceError(f"duration_not_producible:{error.code}") from error
        return cls(duration_milliseconds=resolved.requested_milliseconds)

    @property
    def resolved(self) -> ResolvedLength:
        return resolve_milliseconds(self.duration_milliseconds)

    @property
    def frame_count(self) -> int:
        return self.resolved.frame_count

    @property
    def delivered_milliseconds(self) -> int:
        return self.resolved.delivered_milliseconds

    @property
    def snapped(self) -> bool:
        return self.resolved.snapped

    def to_wire(self) -> dict[str, int | bool]:
        resolved = self.resolved
        return {
            "duration_milliseconds": self.duration_milliseconds,
            "frame_count": resolved.frame_count,
            "delivered_milliseconds": resolved.delivered_milliseconds,
            "snapped": resolved.snapped,
        }


@dataclass(frozen=True, slots=True)
class SegmentDeclaration:
    """Content-free producer declaration owned by one workspace segment."""

    segment_id: str
    task_mode: TaskMode
    source_id: str
    reference_ids: tuple[str, ...]
    duration: SegmentDuration
    relation: SegmentRelationKind
    predecessor_segment_id: str | None
    accepted_intent_fingerprint: str
    semantic_receipt_fingerprint: str | None
    profile_fingerprint: str
    reference_registry_fingerprint: str
    native_binding_fingerprint: str
    producer_settings_fingerprint: str

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "segment_id")
        if type(self.task_mode) is not TaskMode:
            raise SegmentWorkspaceError("task_mode")
        _identifier(self.source_id, "source_id")
        _identifier_tuple(self.reference_ids, "reference_ids", MAX_WORKSPACE_REFERENCES)
        if type(self.duration) is not SegmentDuration:
            raise SegmentWorkspaceError("segment_duration")
        if type(self.relation) is not SegmentRelationKind:
            raise SegmentWorkspaceError("segment_relation")
        if self.predecessor_segment_id is not None:
            _identifier(self.predecessor_segment_id, "predecessor_segment_id")
        requires_predecessor = self.relation in {
            SegmentRelationKind.PREDECESSOR,
            SegmentRelationKind.ADJACENT_PAIR,
        }
        if requires_predecessor and self.predecessor_segment_id is None:
            raise SegmentWorkspaceError("missing_predecessor")
        if not requires_predecessor and self.predecessor_segment_id is not None:
            raise SegmentWorkspaceError("unexpected_predecessor")
        _fingerprint(self.accepted_intent_fingerprint, "accepted_intent_fingerprint")
        _optional_fingerprint(self.semantic_receipt_fingerprint, "semantic_receipt_fingerprint")
        _fingerprint(self.profile_fingerprint, "profile_fingerprint")
        _fingerprint(
            self.reference_registry_fingerprint,
            "reference_registry_fingerprint",
        )
        _fingerprint(self.native_binding_fingerprint, "native_binding_fingerprint")
        _fingerprint(self.producer_settings_fingerprint, "producer_settings_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "task_mode": self.task_mode.value,
            "source_id": self.source_id,
            "reference_ids": list(self.reference_ids),
            "duration": self.duration.to_wire(),
            "relation": self.relation.value,
            "predecessor_segment_id": self.predecessor_segment_id,
            "accepted_intent_fingerprint": self.accepted_intent_fingerprint,
            "semantic_receipt_fingerprint": self.semantic_receipt_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "reference_registry_fingerprint": self.reference_registry_fingerprint,
            "native_binding_fingerprint": self.native_binding_fingerprint,
            "producer_settings_fingerprint": self.producer_settings_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class MultiSegmentWorkspace:
    """One immutable, fully validated canonical workspace revision."""

    workspace_id: str
    revision: int
    segments: tuple[SegmentDeclaration, ...]
    selected_segment_ids: tuple[str, ...] = ()
    parent_workspace_fingerprint: str | None = None
    workspace_fingerprint: str | None = None
    schema: str = MULTI_SEGMENT_WORKSPACE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MULTI_SEGMENT_WORKSPACE_SCHEMA:
            raise SegmentWorkspaceError("unsupported_workspace_schema")
        _identifier(self.workspace_id, "workspace_id")
        _positive_integer(self.revision, "workspace.revision", MAX_WORKSPACE_REVISION)
        if (
            type(self.segments) is not tuple
            or not 1 <= len(self.segments) <= MAX_WORKSPACE_SEGMENTS
        ):
            raise SegmentWorkspaceError("segment_limit")
        if not all(type(segment) is SegmentDeclaration for segment in self.segments):
            raise SegmentWorkspaceError("segment_type")
        segment_ids = tuple(segment.segment_id for segment in self.segments)
        if len(segment_ids) != len(set(segment_ids)):
            raise SegmentWorkspaceError("duplicate_segment_id")
        prior: set[str] = set()
        for segment in self.segments:
            predecessor = segment.predecessor_segment_id
            if predecessor is not None and predecessor not in prior:
                raise SegmentWorkspaceError("unknown_or_forward_predecessor")
            prior.add(segment.segment_id)
        selected = _identifier_tuple(
            self.selected_segment_ids,
            "selected_segment_ids",
            MAX_WORKSPACE_SELECTIONS,
        )
        if not set(selected).issubset(segment_ids):
            raise SegmentWorkspaceError("unknown_selected_segment")
        if self.revision == 1:
            if self.parent_workspace_fingerprint is not None:
                raise SegmentWorkspaceError("initial_revision_parent")
        else:
            _fingerprint(self.parent_workspace_fingerprint, "parent_workspace_fingerprint")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.workspace_fingerprint is None:
            object.__setattr__(self, "workspace_fingerprint", expected)
        elif self.workspace_fingerprint != expected:
            raise SegmentWorkspaceError("workspace_fingerprint_mismatch")
        if len(self.to_wire_bytes()) > MAX_WORKSPACE_WIRE_BYTES:
            raise SegmentWorkspaceError("workspace_wire_limit")

    @property
    def fingerprint(self) -> str:
        if self.workspace_fingerprint is None:  # pragma: no cover - initialized above
            raise SegmentWorkspaceError("workspace_fingerprint_uninitialized")
        return self.workspace_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "workspace_id": self.workspace_id,
            "revision": self.revision,
            "parent_workspace_fingerprint": self.parent_workspace_fingerprint,
            "segments": [segment.to_wire() for segment in self.segments],
            "selected_segment_ids": list(self.selected_segment_ids),
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["workspace_fingerprint"] = self.fingerprint
        return value

    def to_public_dict(self) -> dict[str, object]:
        """Return the bounded content-free workspace projection."""

        return self.to_wire()

    def to_wire_bytes(self) -> bytes:
        return canonical_bytes(self.to_wire())


@dataclass(frozen=True, slots=True)
class SegmentContextManifest:
    """Derived producer and dependency identity for one canonical segment."""

    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    segment_id: str
    ordinal: int
    task_mode: TaskMode
    source_id: str
    reference_ids: tuple[str, ...]
    duration: SegmentDuration
    relation: SegmentRelationKind
    dependency_segment_ids: tuple[str, ...]
    accepted_intent_fingerprint: str
    semantic_receipt_fingerprint: str | None
    profile_fingerprint: str
    reference_registry_fingerprint: str
    native_binding_fingerprint: str
    producer_settings_fingerprint: str
    producer_fingerprint: str
    ancestry_root_segment_id: str
    ancestry_depth: int
    reset_boundary: bool
    manifest_fingerprint: str | None = None
    schema: str = SEGMENT_CONTEXT_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SEGMENT_CONTEXT_MANIFEST_SCHEMA:
            raise SegmentWorkspaceError("unsupported_manifest_schema")
        _identifier(self.workspace_id, "manifest.workspace_id")
        _positive_integer(
            self.workspace_revision, "manifest.workspace_revision", MAX_WORKSPACE_REVISION
        )
        _fingerprint(self.workspace_fingerprint, "manifest.workspace_fingerprint")
        _identifier(self.segment_id, "manifest.segment_id")
        _positive_integer(self.ordinal, "manifest.ordinal", MAX_WORKSPACE_SEGMENTS)
        if type(self.task_mode) is not TaskMode:
            raise SegmentWorkspaceError("manifest.task_mode")
        _identifier(self.source_id, "manifest.source_id")
        _identifier_tuple(self.reference_ids, "manifest.reference_ids", MAX_WORKSPACE_REFERENCES)
        if type(self.duration) is not SegmentDuration:
            raise SegmentWorkspaceError("manifest.duration")
        if type(self.relation) is not SegmentRelationKind:
            raise SegmentWorkspaceError("manifest.relation")
        _identifier_tuple(
            self.dependency_segment_ids,
            "manifest.dependency_segment_ids",
            1,
        )
        _fingerprint(self.accepted_intent_fingerprint, "manifest.accepted_intent_fingerprint")
        _optional_fingerprint(
            self.semantic_receipt_fingerprint,
            "manifest.semantic_receipt_fingerprint",
        )
        _fingerprint(self.profile_fingerprint, "manifest.profile_fingerprint")
        _fingerprint(
            self.reference_registry_fingerprint,
            "manifest.reference_registry_fingerprint",
        )
        _fingerprint(self.native_binding_fingerprint, "manifest.native_binding_fingerprint")
        _fingerprint(
            self.producer_settings_fingerprint,
            "manifest.producer_settings_fingerprint",
        )
        _fingerprint(self.producer_fingerprint, "manifest.producer_fingerprint")
        _identifier(self.ancestry_root_segment_id, "manifest.ancestry_root_segment_id")
        if type(self.ancestry_depth) is not int or not (
            0 <= self.ancestry_depth <= MAX_WORKSPACE_ANCESTRY_DEPTH
        ):
            raise SegmentWorkspaceError("manifest.ancestry_depth")
        if type(self.reset_boundary) is not bool:
            raise SegmentWorkspaceError("manifest.reset_boundary")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.manifest_fingerprint is None:
            object.__setattr__(self, "manifest_fingerprint", expected)
        elif self.manifest_fingerprint != expected:
            raise SegmentWorkspaceError("manifest_fingerprint_mismatch")

    @property
    def fingerprint(self) -> str:
        if self.manifest_fingerprint is None:  # pragma: no cover - initialized above
            raise SegmentWorkspaceError("manifest_fingerprint_uninitialized")
        return self.manifest_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "segment_id": self.segment_id,
            "ordinal": self.ordinal,
            "task_mode": self.task_mode.value,
            "source_id": self.source_id,
            "reference_ids": list(self.reference_ids),
            "duration": self.duration.to_wire(),
            "relation": self.relation.value,
            "dependency_segment_ids": list(self.dependency_segment_ids),
            "accepted_intent_fingerprint": self.accepted_intent_fingerprint,
            "semantic_receipt_fingerprint": self.semantic_receipt_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "reference_registry_fingerprint": self.reference_registry_fingerprint,
            "native_binding_fingerprint": self.native_binding_fingerprint,
            "producer_settings_fingerprint": self.producer_settings_fingerprint,
            "producer_fingerprint": self.producer_fingerprint,
            "ancestry_root_segment_id": self.ancestry_root_segment_id,
            "ancestry_depth": self.ancestry_depth,
            "reset_boundary": self.reset_boundary,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["manifest_fingerprint"] = self.fingerprint
        return value

    def to_public_dict(self) -> dict[str, object]:
        return self.to_wire()


def create_workspace(
    workspace_id: str,
    segments: tuple[SegmentDeclaration, ...],
    *,
    accepted_intent_authorities: tuple[AcceptedIntentAuthority, ...],
    selected_segment_ids: tuple[str, ...] = (),
) -> MultiSegmentWorkspace:
    """Create the first validated revision of a canonical workspace."""

    workspace = MultiSegmentWorkspace(
        workspace_id=workspace_id,
        revision=1,
        segments=segments,
        selected_segment_ids=selected_segment_ids,
    )
    _validate_accepted_intent_authorities(segments, accepted_intent_authorities)
    return workspace


def revise_workspace(
    workspace: MultiSegmentWorkspace,
    *,
    expected_workspace_fingerprint: str,
    accepted_intent_authorities: tuple[AcceptedIntentAuthority, ...],
    segments: tuple[SegmentDeclaration, ...] | None = None,
    selected_segment_ids: tuple[str, ...] | None = None,
) -> MultiSegmentWorkspace:
    """Atomically derive a successor revision after an exact stale-authority check."""

    if type(workspace) is not MultiSegmentWorkspace:
        raise SegmentWorkspaceError("workspace_type")
    _fingerprint(expected_workspace_fingerprint, "expected_workspace_fingerprint")
    if expected_workspace_fingerprint != workspace.fingerprint:
        raise SegmentWorkspaceError("stale_workspace")
    next_segments = workspace.segments if segments is None else segments
    revised = MultiSegmentWorkspace(
        workspace_id=workspace.workspace_id,
        revision=workspace.revision + 1,
        parent_workspace_fingerprint=workspace.fingerprint,
        segments=next_segments,
        selected_segment_ids=(
            workspace.selected_segment_ids if selected_segment_ids is None else selected_segment_ids
        ),
    )
    _validate_accepted_intent_authorities(next_segments, accepted_intent_authorities)
    return revised


def derive_segment_manifests(
    workspace: MultiSegmentWorkspace,
) -> tuple[SegmentContextManifest, ...]:
    """Derive ordered producer manifests without executing or inspecting any producer."""

    if type(workspace) is not MultiSegmentWorkspace:
        raise SegmentWorkspaceError("workspace_type")
    ancestry: dict[str, tuple[str, int]] = {}
    manifests: list[SegmentContextManifest] = []
    for ordinal, segment in enumerate(workspace.segments, start=1):
        predecessor = segment.predecessor_segment_id
        dependency_ids = () if predecessor is None else (predecessor,)
        reset = segment.relation in {
            SegmentRelationKind.CUT,
            SegmentRelationKind.RESET,
        }
        if predecessor is None:
            root, depth = segment.segment_id, 0
        else:
            root, predecessor_depth = ancestry[predecessor]
            depth = predecessor_depth + 1
            if depth > MAX_WORKSPACE_ANCESTRY_DEPTH:
                raise SegmentWorkspaceError("ancestry_depth_limit")
        ancestry[segment.segment_id] = (root, depth)
        manifests.append(
            SegmentContextManifest(
                workspace_id=workspace.workspace_id,
                workspace_revision=workspace.revision,
                workspace_fingerprint=workspace.fingerprint,
                segment_id=segment.segment_id,
                ordinal=ordinal,
                task_mode=segment.task_mode,
                source_id=segment.source_id,
                reference_ids=segment.reference_ids,
                duration=segment.duration,
                relation=segment.relation,
                dependency_segment_ids=dependency_ids,
                accepted_intent_fingerprint=segment.accepted_intent_fingerprint,
                semantic_receipt_fingerprint=segment.semantic_receipt_fingerprint,
                profile_fingerprint=segment.profile_fingerprint,
                reference_registry_fingerprint=segment.reference_registry_fingerprint,
                native_binding_fingerprint=segment.native_binding_fingerprint,
                producer_settings_fingerprint=segment.producer_settings_fingerprint,
                producer_fingerprint=canonical_fingerprint(segment.to_wire()),
                ancestry_root_segment_id=root,
                ancestry_depth=depth,
                reset_boundary=reset,
            )
        )
    return tuple(manifests)


def _validate_accepted_intent_authorities(
    segments: tuple[SegmentDeclaration, ...],
    authorities: tuple[AcceptedIntentAuthority, ...],
) -> None:
    if type(authorities) is not tuple or not all(
        type(authority) is AcceptedIntentAuthority for authority in authorities
    ):
        raise SegmentWorkspaceError("accepted_intent_authority_type")
    by_segment = {authority.segment_id: authority for authority in authorities}
    if len(by_segment) != len(authorities):
        raise SegmentWorkspaceError("duplicate_accepted_intent_authority")
    segment_ids = {segment.segment_id for segment in segments}
    if set(by_segment) != segment_ids:
        raise SegmentWorkspaceError("accepted_intent_authority_coverage")
    for segment in segments:
        if (
            by_segment[segment.segment_id].accepted_intent_fingerprint
            != segment.accepted_intent_fingerprint
        ):
            raise SegmentWorkspaceError("stale_accepted_intent")


__all__ = [
    "MAX_WORKSPACE_ANCESTRY_DEPTH",
    "MAX_WORKSPACE_DURATION_MILLISECONDS",
    "MAX_WORKSPACE_REFERENCES",
    "MAX_WORKSPACE_REVISION",
    "MAX_WORKSPACE_SEGMENTS",
    "MAX_WORKSPACE_SELECTIONS",
    "MAX_WORKSPACE_WIRE_BYTES",
    "MULTI_SEGMENT_WORKSPACE_SCHEMA",
    "SEGMENT_CONTEXT_MANIFEST_SCHEMA",
    "AcceptedIntentAuthority",
    "MultiSegmentWorkspace",
    "SegmentContextManifest",
    "SegmentDeclaration",
    "SegmentDuration",
    "SegmentRelationKind",
    "SegmentWorkspaceError",
    "create_workspace",
    "derive_segment_manifests",
    "revise_workspace",
]
