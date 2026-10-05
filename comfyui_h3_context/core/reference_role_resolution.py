"""Explicit cross-asset identity and reference-role resolution contracts.

This module accepts caller-supplied proposals over the M13-01 graph.  It never matches faces,
voices, embeddings, labels, filenames, or media; it preserves alternatives and conflicts instead
of selecting a winner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from .canonical import canonical_fingerprint
from .contracts import MediaKind
from .errors import ReferenceRoleResolutionError
from .registry import BackendLabel, ReferenceRegistry
from .unified_evidence_graph import UnifiedEvidenceGraph

REFERENCE_ROLE_RESOLUTION_SCHEMA = "h3.reference_role_resolution.v1"
MAX_ROLE_ASSETS = 256
MAX_ROLE_OBSERVATIONS = 256
MAX_ROLE_PROPOSALS = 512
MAX_ROLE_ASSIGNMENTS = 1024
MAX_ROLE_LINKS = 1024
MAX_ROLE_ALTERNATIVES = 256
MAX_ROLE_CONFLICTS = 256
MAX_ROLE_IDS = 256
MAX_ROLE_TEXT = 4096
MAX_ROLE_OUTPUT_BYTES = 65_536

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE = (
    "http://",
    "https://",
    "file://",
    "/mnt/",
    "c:\\",
    "api_key",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "token=",
)


class ReferenceRole(str, Enum):
    """Closed role vocabulary for independent asset and semantic references."""

    PICTURE = "picture"
    VIDEO = "video"
    AUDIO = "audio"
    SUBJECT = "subject"
    VOICE = "voice"
    OBJECT = "object"
    SCENE = "scene"
    STYLE = "style"
    MOTION = "motion"
    CAMERA = "camera"
    EDIT = "edit"
    KEYFRAME = "keyframe"


class RoleResolution(str, Enum):
    """Explicit proposal/assignment disposition."""

    RESOLVED = "resolved"
    USER_SELECTED = "user_selected"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    CONFLICTING = "conflicting"


class ReferenceRoleGraphStatus(str, Enum):
    """Graph-level claim ceiling."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    EMPTY = "empty"


_ASSET_ROLES = {
    ReferenceRole.PICTURE: MediaKind.IMAGE,
    ReferenceRole.VIDEO: MediaKind.VIDEO,
    ReferenceRole.AUDIO: MediaKind.AUDIO,
}


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ReferenceRoleResolutionError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise ReferenceRoleResolutionError(f"{field_name} contains sensitive or locator material")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ROLE_TEXT:
        raise ReferenceRoleResolutionError(f"{field_name} must be bounded non-empty text")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise ReferenceRoleResolutionError(f"{field_name} contains sensitive or locator material")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ReferenceRoleResolutionError(f"{field_name} contains an unsafe wire code point")
    return value


def _ids(
    values: object, field_name: str, maximum: int = MAX_ROLE_IDS, *, required: bool = False
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise ReferenceRoleResolutionError(f"{field_name} must be a bounded tuple")
    if required and not values:
        raise ReferenceRoleResolutionError(f"{field_name} must not be empty")
    result = tuple(_identifier(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise ReferenceRoleResolutionError(f"{field_name} must not contain duplicates")
    return result


def _confidence(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or not Decimal("0") <= value <= Decimal("1")
    ):
        raise ReferenceRoleResolutionError(f"{field_name} must be a finite Decimal between 0 and 1")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ReferenceRoleResolutionError(f"{field_name} must be a SHA-256 fingerprint")
    return value


def _enum(value: object, expected: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected):
        raise ReferenceRoleResolutionError(f"{field_name} must be a {expected.__name__}")


@dataclass(frozen=True, slots=True)
class ReferenceRoleProposal:
    """One explicit role/candidate proposal supplied by a caller or provider adapter."""

    proposal_id: str
    role: ReferenceRole
    candidate_ids: tuple[str, ...]
    label: str | None
    observation_ids: tuple[str, ...]
    source_asset_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    resolution: RoleResolution = RoleResolution.RESOLVED
    confidence: Decimal | None = None
    uncertainty_ids: tuple[str, ...] = ()
    schema: str = REFERENCE_ROLE_RESOLUTION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.proposal_id, "proposal_id")
        _enum(self.role, ReferenceRole, "proposal role")
        object.__setattr__(self, "candidate_ids", _ids(self.candidate_ids, "candidate_ids"))
        object.__setattr__(
            self,
            "observation_ids",
            _ids(self.observation_ids, "observation_ids", MAX_ROLE_OBSERVATIONS),
        )
        object.__setattr__(
            self,
            "source_asset_ids",
            _ids(self.source_asset_ids, "source_asset_ids", MAX_ROLE_ASSETS, required=True),
        )
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids, "evidence_ids"))
        object.__setattr__(self, "uncertainty_ids", _ids(self.uncertainty_ids, "uncertainty_ids"))
        _enum(self.resolution, RoleResolution, "proposal resolution")
        _confidence(self.confidence, "proposal confidence")
        if self.label is not None:
            _text(self.label, "proposal label")
        if self.resolution in {RoleResolution.RESOLVED, RoleResolution.USER_SELECTED}:
            if len(self.candidate_ids) != 1 or self.label is None:
                raise ReferenceRoleResolutionError(
                    "resolved and user-selected proposals require one candidate and a label"
                )
        elif self.resolution in {RoleResolution.AMBIGUOUS, RoleResolution.CONFLICTING}:
            if len(self.candidate_ids) < 2:
                raise ReferenceRoleResolutionError(
                    "ambiguous and conflicting proposals require at least two candidates"
                )
        elif self.candidate_ids:
            raise ReferenceRoleResolutionError("unresolved proposals must not carry candidates")
        if self.schema != REFERENCE_ROLE_RESOLUTION_SCHEMA:
            raise ReferenceRoleResolutionError("unsupported reference-role resolution schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "proposal_id": self.proposal_id,
            "role": self.role.value,
            "candidate_ids": list(self.candidate_ids),
            "label": self.label,
            "observation_ids": list(self.observation_ids),
            "source_asset_ids": list(self.source_asset_ids),
            "evidence_ids": list(self.evidence_ids),
            "resolution": self.resolution.value,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "uncertainty_ids": list(self.uncertainty_ids),
        }


@dataclass(frozen=True, slots=True)
class ReferenceRoleAssignment:
    """One role-scoped candidate assignment; candidate IDs are never merged across roles."""

    assignment_id: str
    role: ReferenceRole
    candidate_id: str
    label: str
    observation_ids: tuple[str, ...]
    source_asset_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    resolution: RoleResolution
    confidence: Decimal | None = None
    uncertainty_ids: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    schema: str = REFERENCE_ROLE_RESOLUTION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.assignment_id, "assignment_id")
        _enum(self.role, ReferenceRole, "assignment role")
        _identifier(self.candidate_id, "assignment candidate_id")
        _text(self.label, "assignment label")
        _ids(self.observation_ids, "assignment observation_ids", MAX_ROLE_OBSERVATIONS)
        _ids(self.source_asset_ids, "assignment source_asset_ids", MAX_ROLE_ASSETS, required=True)
        _ids(self.evidence_ids, "assignment evidence_ids")
        _enum(self.resolution, RoleResolution, "assignment resolution")
        _confidence(self.confidence, "assignment confidence")
        _ids(self.uncertainty_ids, "assignment uncertainty_ids")
        _ids(self.proposal_ids, "assignment proposal_ids")
        if self.schema != REFERENCE_ROLE_RESOLUTION_SCHEMA:
            raise ReferenceRoleResolutionError("unsupported reference-role resolution schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "assignment_id": self.assignment_id,
            "role": self.role.value,
            "candidate_id": self.candidate_id,
            "label": self.label,
            "observation_ids": list(self.observation_ids),
            "source_asset_ids": list(self.source_asset_ids),
            "evidence_ids": list(self.evidence_ids),
            "resolution": self.resolution.value,
            "confidence": None if self.confidence is None else format(self.confidence, "f"),
            "uncertainty_ids": list(self.uncertainty_ids),
            "proposal_ids": list(self.proposal_ids),
        }


@dataclass(frozen=True, slots=True)
class ReferenceRoleLink:
    """Observation link retaining every candidate from one role proposal."""

    link_id: str
    proposal_id: str
    observation_id: str
    role: ReferenceRole
    candidate_ids: tuple[str, ...]
    resolution: RoleResolution
    schema: str = REFERENCE_ROLE_RESOLUTION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.link_id, "link_id")
        _identifier(self.proposal_id, "link proposal_id")
        _identifier(self.observation_id, "link observation_id")
        _enum(self.role, ReferenceRole, "link role")
        _ids(self.candidate_ids, "link candidate_ids")
        _enum(self.resolution, RoleResolution, "link resolution")
        if self.schema != REFERENCE_ROLE_RESOLUTION_SCHEMA:
            raise ReferenceRoleResolutionError("unsupported reference-role resolution schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "link_id": self.link_id,
            "proposal_id": self.proposal_id,
            "observation_id": self.observation_id,
            "role": self.role.value,
            "candidate_ids": list(self.candidate_ids),
            "resolution": self.resolution.value,
        }


@dataclass(frozen=True, slots=True)
class ReferenceRoleAlternative:
    """Explicit competing candidates retained without a winner."""

    alternative_id: str
    role: ReferenceRole
    candidate_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    source_asset_ids: tuple[str, ...]
    proposal_ids: tuple[str, ...]
    reason: str
    uncertainty_ids: tuple[str, ...] = ()
    schema: str = REFERENCE_ROLE_RESOLUTION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.alternative_id, "alternative_id")
        _enum(self.role, ReferenceRole, "alternative role")
        if len(_ids(self.candidate_ids, "alternative candidate_ids")) < 2:
            raise ReferenceRoleResolutionError("alternative requires at least two candidates")
        _ids(self.observation_ids, "alternative observation_ids", MAX_ROLE_OBSERVATIONS)
        _ids(self.source_asset_ids, "alternative source_asset_ids", MAX_ROLE_ASSETS, required=True)
        _ids(self.proposal_ids, "alternative proposal_ids")
        _text(self.reason, "alternative reason")
        _ids(self.uncertainty_ids, "alternative uncertainty_ids")
        if self.schema != REFERENCE_ROLE_RESOLUTION_SCHEMA:
            raise ReferenceRoleResolutionError("unsupported reference-role resolution schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "alternative_id": self.alternative_id,
            "role": self.role.value,
            "candidate_ids": list(self.candidate_ids),
            "observation_ids": list(self.observation_ids),
            "source_asset_ids": list(self.source_asset_ids),
            "proposal_ids": list(self.proposal_ids),
            "reason": self.reason,
            "uncertainty_ids": list(self.uncertainty_ids),
        }


@dataclass(frozen=True, slots=True)
class ReferenceRoleConflict:
    """Explicit disagreement that prevents a silent false merge."""

    conflict_id: str
    role: ReferenceRole
    candidate_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    proposal_ids: tuple[str, ...]
    reason: str
    schema: str = REFERENCE_ROLE_RESOLUTION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.conflict_id, "conflict_id")
        _enum(self.role, ReferenceRole, "conflict role")
        if len(_ids(self.candidate_ids, "conflict candidate_ids")) < 2:
            raise ReferenceRoleResolutionError("conflict requires at least two candidates")
        _ids(self.observation_ids, "conflict observation_ids", MAX_ROLE_OBSERVATIONS)
        _ids(self.proposal_ids, "conflict proposal_ids")
        _text(self.reason, "conflict reason")
        if self.schema != REFERENCE_ROLE_RESOLUTION_SCHEMA:
            raise ReferenceRoleResolutionError("unsupported reference-role resolution schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "conflict_id": self.conflict_id,
            "role": self.role.value,
            "candidate_ids": list(self.candidate_ids),
            "observation_ids": list(self.observation_ids),
            "proposal_ids": list(self.proposal_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ReferenceRoleMetrics:
    """Structural counts; these are not perception accuracy metrics."""

    proposal_count: int
    resolved_count: int
    ambiguous_count: int
    unresolved_count: int
    conflicting_count: int
    false_merge_count: int
    prevented_false_merge_count: int
    label_order_errors: int

    def __post_init__(self) -> None:
        values = (
            self.proposal_count,
            self.resolved_count,
            self.ambiguous_count,
            self.unresolved_count,
            self.conflicting_count,
            self.false_merge_count,
            self.prevented_false_merge_count,
            self.label_order_errors,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise ReferenceRoleResolutionError("role metrics must be non-negative integers")

    def to_wire(self) -> dict[str, int | bool]:
        return {
            "proposal_count": self.proposal_count,
            "resolved_count": self.resolved_count,
            "ambiguous_count": self.ambiguous_count,
            "unresolved_count": self.unresolved_count,
            "conflicting_count": self.conflicting_count,
            "false_merge_count": self.false_merge_count,
            "prevented_false_merge_count": self.prevented_false_merge_count,
            "label_order_errors": self.label_order_errors,
            "label_order_pass": self.label_order_errors == 0,
        }


@dataclass(frozen=True, slots=True)
class ReferenceRoleResolutionGraph:
    """Inspectable role-scoped resolution projection over one evidence graph."""

    status: ReferenceRoleGraphStatus
    source_graph_fingerprint: str
    labels: tuple[BackendLabel, ...]
    input_proposals: tuple[ReferenceRoleProposal, ...]
    assignments: tuple[ReferenceRoleAssignment, ...]
    links: tuple[ReferenceRoleLink, ...]
    alternatives: tuple[ReferenceRoleAlternative, ...]
    conflicts: tuple[ReferenceRoleConflict, ...]
    diagnostics: tuple[str, ...]
    metrics: ReferenceRoleMetrics
    schema: str = REFERENCE_ROLE_RESOLUTION_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.status, ReferenceRoleGraphStatus, "graph status")
        _fingerprint(self.source_graph_fingerprint, "source_graph_fingerprint")
        if self.schema != REFERENCE_ROLE_RESOLUTION_SCHEMA:
            raise ReferenceRoleResolutionError("unsupported reference-role resolution schema")
        for values, maximum, expected, field_name, identifier_field in (
            (self.labels, MAX_ROLE_ASSETS, BackendLabel, "labels", "asset_id"),
            (
                self.input_proposals,
                MAX_ROLE_PROPOSALS,
                ReferenceRoleProposal,
                "input_proposals",
                "proposal_id",
            ),
            (
                self.assignments,
                MAX_ROLE_ASSIGNMENTS,
                ReferenceRoleAssignment,
                "assignments",
                "assignment_id",
            ),
            (self.links, MAX_ROLE_LINKS, ReferenceRoleLink, "links", "link_id"),
            (
                self.alternatives,
                MAX_ROLE_ALTERNATIVES,
                ReferenceRoleAlternative,
                "alternatives",
                "alternative_id",
            ),
            (self.conflicts, MAX_ROLE_CONFLICTS, ReferenceRoleConflict, "conflicts", "conflict_id"),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > maximum
                or not all(isinstance(value, expected) for value in values)
            ):
                raise ReferenceRoleResolutionError(f"{field_name} are outside the bounded envelope")
            identifiers = tuple(getattr(value, identifier_field) for value in values)
            if len(identifiers) != len(set(identifiers)):
                raise ReferenceRoleResolutionError(f"{field_name} IDs must be unique")
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > MAX_ROLE_IDS:
            raise ReferenceRoleResolutionError("diagnostics are outside the bounded envelope")
        for diagnostic in self.diagnostics:
            _identifier(diagnostic, "diagnostic")
        if not isinstance(self.metrics, ReferenceRoleMetrics):
            raise ReferenceRoleResolutionError("metrics must be ReferenceRoleMetrics")
        if len(self.to_wire_bytes()) > MAX_ROLE_OUTPUT_BYTES:
            raise ReferenceRoleResolutionError("reference-role resolution exceeds output limit")

    @property
    def complete(self) -> bool:
        return self.status is ReferenceRoleGraphStatus.COMPLETE

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status.value,
            "complete": self.complete,
            "source_graph_fingerprint": self.source_graph_fingerprint,
            "labels": [label.to_wire() for label in self.labels],
            "input_proposals": [item.to_wire() for item in self.input_proposals],
            "assignments": [item.to_wire() for item in self.assignments],
            "links": [item.to_wire() for item in self.links],
            "alternatives": [item.to_wire() for item in self.alternatives],
            "conflicts": [item.to_wire() for item in self.conflicts],
            "diagnostics": list(self.diagnostics),
            "metrics": self.metrics.to_wire(),
        }

    def to_wire_bytes(self) -> bytes:
        import json

        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_public_dict(self) -> dict[str, object]:
        result = self.to_wire()
        result["fingerprint"] = self.fingerprint
        return result


@dataclass
class _AssignmentState:
    role: ReferenceRole
    candidate_id: str
    label: str
    confidence: Decimal | None
    observations: set[str] = field(default_factory=set)
    assets: set[str] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)
    uncertainties: set[str] = field(default_factory=set)
    proposals: set[str] = field(default_factory=set)
    resolutions: set[RoleResolution] = field(default_factory=set)


def _asset_label(role: ReferenceRole, candidate_id: str, registry: ReferenceRegistry) -> str:
    kind = _ASSET_ROLES[role]
    asset = next((item for item in registry.assets if item.asset_id == candidate_id), None)
    if asset is None or asset.kind is not kind:
        raise ReferenceRoleResolutionError("asset role candidate has an incompatible media kind")
    return registry.label_for(candidate_id).label


def _label_order_errors(registry: ReferenceRegistry) -> int:
    counters = {MediaKind.IMAGE: 0, MediaKind.VIDEO: 0, MediaKind.AUDIO: 0}
    errors = 0
    if len(registry.labels) != len(registry.assets):
        return 1
    for asset, label in zip(registry.assets, registry.labels, strict=True):
        counters[asset.kind] += 1
        if label.asset_id != asset.asset_id or label.ordinal != counters[asset.kind]:
            errors += 1
    return errors


def build_reference_role_resolution(
    graph: UnifiedEvidenceGraph,
    registry: ReferenceRegistry,
    proposals: tuple[ReferenceRoleProposal, ...] = (),
) -> ReferenceRoleResolutionGraph:
    """Validate explicit role proposals and retain all ambiguity/conflict states."""

    if not isinstance(graph, UnifiedEvidenceGraph):
        raise ReferenceRoleResolutionError("graph must be a UnifiedEvidenceGraph")
    if not isinstance(registry, ReferenceRegistry):
        raise ReferenceRoleResolutionError("registry must be a ReferenceRegistry")
    if (
        not isinstance(proposals, tuple)
        or len(proposals) > MAX_ROLE_PROPOSALS
        or not all(isinstance(item, ReferenceRoleProposal) for item in proposals)
    ):
        raise ReferenceRoleResolutionError(
            "proposals must be a bounded tuple of ReferenceRoleProposal values"
        )
    proposal_ids = tuple(item.proposal_id for item in proposals)
    if len(proposal_ids) != len(set(proposal_ids)):
        raise ReferenceRoleResolutionError("proposal IDs must be unique")
    asset_map = {asset.asset_id: asset for asset in registry.assets}
    observation_map = {item.observation_id: item for item in graph.observations}
    evidence_map = {item.evidence_id: item for item in graph.evidence}
    uncertainty_ids = {item.uncertainty_id for item in graph.uncertainties}
    graph_assets = {item.asset_id for item in graph.observations}
    if not graph_assets.issubset(asset_map):
        raise ReferenceRoleResolutionError("registry does not own every graph observation asset")
    order_map = {asset.asset_id: asset.connection_order for asset in registry.assets}
    ordered_proposals = tuple(
        sorted(
            proposals,
            key=lambda item: (
                item.role.value,
                min(order_map[asset_id] for asset_id in item.source_asset_ids),
                item.observation_ids[0] if item.observation_ids else "",
                item.proposal_id,
            ),
        )
    )
    states: dict[tuple[ReferenceRole, str], _AssignmentState] = {}
    links: list[ReferenceRoleLink] = []
    alternatives: list[ReferenceRoleAlternative] = []
    conflicts: list[ReferenceRoleConflict] = []
    diagnostics: set[str] = set()
    assignments_by_observation: dict[tuple[ReferenceRole, str], list[tuple[str, str]]] = {}
    label_candidates: dict[tuple[ReferenceRole, str], set[str]] = {}
    resolved_count = 0
    ambiguous_count = 0
    unresolved_count = 0
    conflicting_count = 0

    for proposal in ordered_proposals:
        source_assets = set(proposal.source_asset_ids)
        if not source_assets.issubset(asset_map):
            raise ReferenceRoleResolutionError("proposal references an unknown source asset")
        for observation_id in proposal.observation_ids:
            observation = observation_map.get(observation_id)
            if observation is None:
                raise ReferenceRoleResolutionError("proposal references an unknown observation")
            if observation.asset_id not in source_assets:
                raise ReferenceRoleResolutionError(
                    "proposal observation is outside its source assets"
                )
        for evidence_id in proposal.evidence_ids:
            record = evidence_map.get(evidence_id)
            if record is None:
                raise ReferenceRoleResolutionError("proposal references unknown evidence")
            source_asset = record.provenance.source.asset_id
            if source_asset is not None and source_asset not in source_assets:
                raise ReferenceRoleResolutionError("proposal evidence is outside its source assets")
        if not set(proposal.uncertainty_ids).issubset(uncertainty_ids):
            raise ReferenceRoleResolutionError("proposal references unknown uncertainty")
        if proposal.role in _ASSET_ROLES:
            expected_kind = _ASSET_ROLES[proposal.role]
            if not set(proposal.candidate_ids).issubset(source_assets):
                raise ReferenceRoleResolutionError("asset-role candidates must be source assets")
            for candidate_id in proposal.candidate_ids:
                candidate = asset_map[candidate_id]
                if candidate.kind is not expected_kind:
                    raise ReferenceRoleResolutionError("asset-role candidate has wrong modality")
            if proposal.resolution in {RoleResolution.RESOLVED, RoleResolution.USER_SELECTED}:
                expected_label = _asset_label(proposal.role, proposal.candidate_ids[0], registry)
                if proposal.label != expected_label:
                    raise ReferenceRoleResolutionError(
                        "asset-role label does not match registry order"
                    )
        elif proposal.role is ReferenceRole.VOICE and not any(
            observation_map[item].modality is MediaKind.AUDIO for item in proposal.observation_ids
        ):
            raise ReferenceRoleResolutionError("voice proposals require an audio observation")

        links.extend(
            ReferenceRoleLink(
                f"link.{proposal.proposal_id}.{index}",
                proposal.proposal_id,
                observation_id,
                proposal.role,
                proposal.candidate_ids,
                proposal.resolution,
            )
            for index, observation_id in enumerate(proposal.observation_ids)
        )
        if proposal.resolution is RoleResolution.AMBIGUOUS:
            ambiguous_count += 1
            alternatives.append(
                ReferenceRoleAlternative(
                    f"alternative.{proposal.proposal_id}",
                    proposal.role,
                    proposal.candidate_ids,
                    proposal.observation_ids,
                    proposal.source_asset_ids,
                    (proposal.proposal_id,),
                    "explicit competing identity or role candidates",
                    proposal.uncertainty_ids,
                )
            )
            diagnostics.add(f"ambiguous_identity:{proposal.proposal_id}")
            continue
        if proposal.resolution is RoleResolution.UNRESOLVED:
            unresolved_count += 1
            diagnostics.add(f"unresolved_identity:{proposal.proposal_id}")
            continue
        if proposal.resolution is RoleResolution.CONFLICTING:
            conflicting_count += 1
            conflicts.append(
                ReferenceRoleConflict(
                    f"conflict.{proposal.proposal_id}",
                    proposal.role,
                    proposal.candidate_ids,
                    proposal.observation_ids,
                    (proposal.proposal_id,),
                    "proposal explicitly marks candidates as conflicting",
                )
            )
        else:
            resolved_count += 1
        for candidate_id in proposal.candidate_ids:
            label = (
                _asset_label(proposal.role, candidate_id, registry)
                if proposal.role in _ASSET_ROLES
                else proposal.label
            )
            if label is None:
                raise ReferenceRoleResolutionError("resolved semantic proposal requires a label")
            key = (proposal.role, candidate_id)
            state = states.get(key)
            if state is None:
                state = _AssignmentState(proposal.role, candidate_id, label, proposal.confidence)
                states[key] = state
            elif state.label != label:
                raise ReferenceRoleResolutionError(
                    "one role candidate cannot change its explicit label"
                )
            state.observations.update(proposal.observation_ids)
            state.assets.update(proposal.source_asset_ids)
            state.evidence.update(proposal.evidence_ids)
            state.uncertainties.update(proposal.uncertainty_ids)
            state.proposals.add(proposal.proposal_id)
            state.resolutions.add(proposal.resolution)
            if state.confidence is None or (
                proposal.confidence is not None and proposal.confidence > state.confidence
            ):
                state.confidence = proposal.confidence
            for observation_id in proposal.observation_ids:
                assignments_by_observation.setdefault((proposal.role, observation_id), []).append(
                    (candidate_id, proposal.proposal_id)
                )
            label_candidates.setdefault((proposal.role, label), set()).add(candidate_id)

    conflict_candidates: set[tuple[ReferenceRole, str]] = set()
    prevented_false_merges = 0
    for (role, observation_id), values in sorted(
        assignments_by_observation.items(), key=lambda item: (item[0][0].value, item[0][1])
    ):
        candidates = tuple(sorted({candidate for candidate, _ in values}))
        proposal_values = tuple(sorted({proposal_id for _, proposal_id in values}))
        if len(candidates) > 1:
            prevented_false_merges += 1
            conflict_candidates.update((role, candidate) for candidate in candidates)
            conflicts.append(
                ReferenceRoleConflict(
                    f"conflict.{role.value}.{observation_id}",
                    role,
                    candidates,
                    (observation_id,),
                    proposal_values,
                    "one observation has competing resolved candidates",
                )
            )
            diagnostics.add(f"conflicting_assignment:{role.value}:{observation_id}")
    for (role, _label), candidate_set in sorted(
        label_candidates.items(), key=lambda item: (item[0][0].value, item[0][1])
    ):
        if len(candidate_set) > 1:
            sorted_candidates = tuple(sorted(candidate_set))
            conflict_candidates.update((role, candidate) for candidate in sorted_candidates)
            proposal_values = tuple(
                sorted(
                    {
                        proposal_id
                        for key, state in states.items()
                        if key[0] is role and key[1] in candidate_set
                        for proposal_id in state.proposals
                    }
                )
            )
            observation_values = tuple(
                sorted(
                    {
                        observation_id
                        for (
                            observation_role,
                            observation_id,
                        ), values in assignments_by_observation.items()
                        if observation_role is role
                        and any(candidate in candidate_set for candidate, _ in values)
                    }
                )
            )
            conflicts.append(
                ReferenceRoleConflict(
                    f"conflict.{role.value}.label.{sorted_candidates[0]}",
                    role,
                    sorted_candidates,
                    observation_values,
                    proposal_values,
                    "distinct candidates share one role label",
                )
            )
    assignments = tuple(
        ReferenceRoleAssignment(
            f"assignment.{state.role.value}.{state.candidate_id}",
            state.role,
            state.candidate_id,
            state.label,
            tuple(sorted(state.observations)),
            tuple(sorted(state.assets, key=lambda asset_id: order_map[asset_id])),
            tuple(sorted(state.evidence)),
            RoleResolution.CONFLICTING
            if (state.role, state.candidate_id) in conflict_candidates
            else RoleResolution.USER_SELECTED
            if RoleResolution.USER_SELECTED in state.resolutions
            else RoleResolution.RESOLVED,
            state.confidence,
            tuple(sorted(state.uncertainties)),
            tuple(sorted(state.proposals)),
        )
        for state in sorted(
            states.values(),
            key=lambda item: (
                item.role.value,
                min(order_map[asset_id] for asset_id in item.assets),
                item.candidate_id,
            ),
        )
    )
    conflicts_tuple = tuple(
        sorted(
            {item.conflict_id: item for item in conflicts}.values(),
            key=lambda item: item.conflict_id,
        )
    )
    alternatives_tuple = tuple(sorted(alternatives, key=lambda item: item.alternative_id))
    status = (
        ReferenceRoleGraphStatus.EMPTY
        if not ordered_proposals
        else ReferenceRoleGraphStatus.CONFLICTING
        if conflicts_tuple
        else ReferenceRoleGraphStatus.AMBIGUOUS
        if alternatives_tuple
        else ReferenceRoleGraphStatus.PARTIAL
        if unresolved_count
        else ReferenceRoleGraphStatus.COMPLETE
    )
    label_errors = _label_order_errors(registry)
    metrics = ReferenceRoleMetrics(
        len(ordered_proposals),
        resolved_count,
        ambiguous_count,
        unresolved_count,
        conflicting_count + len(conflicts_tuple),
        0,
        prevented_false_merges,
        label_errors,
    )
    return ReferenceRoleResolutionGraph(
        status,
        graph.fingerprint,
        registry.labels,
        ordered_proposals,
        assignments,
        tuple(sorted(links, key=lambda item: item.link_id)),
        alternatives_tuple,
        conflicts_tuple,
        tuple(sorted(diagnostics)),
        metrics,
    )


__all__ = [
    "REFERENCE_ROLE_RESOLUTION_SCHEMA",
    "MAX_ROLE_ALTERNATIVES",
    "MAX_ROLE_ASSIGNMENTS",
    "MAX_ROLE_CONFLICTS",
    "MAX_ROLE_LINKS",
    "MAX_ROLE_PROPOSALS",
    "ReferenceRole",
    "ReferenceRoleAlternative",
    "ReferenceRoleAssignment",
    "ReferenceRoleConflict",
    "ReferenceRoleGraphStatus",
    "ReferenceRoleLink",
    "ReferenceRoleMetrics",
    "ReferenceRoleProposal",
    "ReferenceRoleResolutionError",
    "ReferenceRoleResolutionGraph",
    "RoleResolution",
    "build_reference_role_resolution",
]
