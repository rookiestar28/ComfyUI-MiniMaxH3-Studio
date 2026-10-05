"""Explicit subject, voice, object, scene, and source cross-reference contracts.

The pure core merges only caller-supplied stable candidate IDs. It does not infer identity from
prose, labels, filenames, media order, embeddings, or co-occurrence, and it never imports a
perception runtime or opens media.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, cast, runtime_checkable

from .contracts import (
    MediaKind,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .errors import CrossReferenceError
from .evidence import EvidenceRecord, EvidenceSourceKind, Uncertainty
from .local_adapters import (
    LocalAdapterDescriptor,
    LocalAdapterExecutionRequest,
    LocalAdapterResult,
    LocalAdapterRuntime,
    LocalBudgetGuard,
    LocalCancellationProbe,
    LocalDeviceKind,
    LocalDeviceSpec,
    run_local_adapter,
)
from .registry import ReferenceRegistry

CROSS_REFERENCE_GRAPH_SCHEMA = "h3.cross_reference.graph.v1"
MAX_CROSS_REFERENCE_OBSERVATIONS = 2048
MAX_CROSS_REFERENCE_PROPOSALS = 1024
MAX_CROSS_REFERENCE_ENTITIES = 1024
MAX_CROSS_REFERENCE_LINKS = 4096
MAX_CROSS_REFERENCE_CANDIDATES = 8
MAX_CROSS_REFERENCE_EVIDENCE = 32
MAX_CROSS_REFERENCE_UNCERTAINTIES = 32
MAX_CROSS_REFERENCE_TEXT_LENGTH = 4096
MAX_CROSS_REFERENCE_MEMORY_BYTES = 4 * 1024 * 1024 * 1024

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SENSITIVE_MARKERS = (
    "api_key",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "token=",
    "https://",
    "http://",
    "file://",
    "/",
    "\\",
)


class ReferenceEntityKind(str, Enum):
    """Entity families that may be linked across multimodal observations."""

    SUBJECT = "subject"
    VOICE = "voice"
    OBJECT = "object"
    SCENE = "scene"
    SOURCE_ASSET = "source_asset"


class CrossReferenceResolution(str, Enum):
    """Explicit resolution state; ambiguity is never silently promoted."""

    RESOLVED = "resolved"
    USER_SELECTED = "user_selected"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    CONFLICTING = "conflicting"


class CrossReferenceGraphStatus(str, Enum):
    """Graph-level state separating complete identity from unresolved evidence."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    EMPTY = "empty"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise CrossReferenceError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise CrossReferenceError(f"{field_name} contains sensitive material")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_CROSS_REFERENCE_TEXT_LENGTH:
        raise CrossReferenceError(f"{field_name} must be a bounded non-empty string")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CrossReferenceError(f"{field_name} contains an unsafe wire code point")
    return value


def _enum(value: object, expected: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected):
        raise CrossReferenceError(f"{field_name} must be a {expected.__name__}")


def _id_tuple(
    values: object, field_name: str, maximum: int, *, required: bool = False
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise CrossReferenceError(f"{field_name} must be a bounded tuple")
    if required and not values:
        raise CrossReferenceError(f"{field_name} must not be empty")
    result = tuple(_identifier(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise CrossReferenceError(f"{field_name} must not contain duplicates")
    return result


def _uncertainties(values: object, field_name: str) -> tuple[Uncertainty, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_CROSS_REFERENCE_UNCERTAINTIES:
        raise CrossReferenceError(f"{field_name} must be a bounded tuple")
    if not all(isinstance(value, Uncertainty) for value in values):
        raise CrossReferenceError(f"{field_name} must contain Uncertainty values")
    return values


def _evidence(values: object) -> tuple[EvidenceRecord, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_CROSS_REFERENCE_EVIDENCE:
        raise CrossReferenceError("proposal evidence must be a bounded tuple")
    if not all(isinstance(value, EvidenceRecord) for value in values):
        raise CrossReferenceError("proposal evidence must contain EvidenceRecord values")
    identifiers = tuple(value.evidence_id for value in values)
    if len(identifiers) != len(set(identifiers)):
        raise CrossReferenceError("proposal evidence IDs must not be duplicated")
    return values


@dataclass(frozen=True, slots=True)
class ObservationReference:
    """Stable observation identity and canonical source ownership without raw media."""

    observation_id: str
    asset_id: str
    source_id: str
    modality: MediaKind

    def __post_init__(self) -> None:
        _identifier(self.observation_id, "observation_id")
        _identifier(self.asset_id, "observation asset_id")
        _identifier(self.source_id, "observation source_id")
        _enum(self.modality, MediaKind, "observation modality")

    def to_wire(self) -> dict[str, str]:
        return {
            "observation_id": self.observation_id,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "modality": self.modality.value,
        }


@dataclass(frozen=True, slots=True)
class CrossReferenceRequest:
    """Canonical observation inventory and explicit Full-Reference graph request."""

    task_mode: TaskMode
    reference_registry: ReferenceRegistry
    observations: tuple[ObservationReference, ...]
    estimated_memory_bytes: int | None = None
    seed: int | None = None

    def __post_init__(self) -> None:
        if self.task_mode is not TaskMode.REF2VA:
            raise CrossReferenceError("cross-reference graph currently requires REF2VA")
        if not isinstance(self.reference_registry, ReferenceRegistry):
            raise CrossReferenceError("reference_registry must be a ReferenceRegistry")
        if (
            not isinstance(self.observations, tuple)
            or not self.observations
            or len(self.observations) > MAX_CROSS_REFERENCE_OBSERVATIONS
            or not all(isinstance(item, ObservationReference) for item in self.observations)
        ):
            raise CrossReferenceError("observations must be a bounded non-empty tuple")
        observation_ids = tuple(item.observation_id for item in self.observations)
        if len(observation_ids) != len(set(observation_ids)):
            raise CrossReferenceError("observation IDs must be unique")
        assets = {asset.asset_id: asset for asset in self.reference_registry.assets}
        for observation in self.observations:
            asset = assets.get(observation.asset_id)
            if asset is None or asset.kind is not observation.modality:
                raise CrossReferenceError("observation modality does not match canonical asset")
        if self.estimated_memory_bytes is not None and (
            isinstance(self.estimated_memory_bytes, bool)
            or not isinstance(self.estimated_memory_bytes, int)
            or not 0 < self.estimated_memory_bytes <= MAX_CROSS_REFERENCE_MEMORY_BYTES
        ):
            raise CrossReferenceError("estimated_memory_bytes is outside the bounded limit")
        if self.seed is not None and (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not -(2**63) <= self.seed <= 2**63 - 1
        ):
            raise CrossReferenceError("seed must be a signed 64-bit integer")

    @property
    def observation_ids(self) -> tuple[str, ...]:
        return tuple(item.observation_id for item in self.observations)

    @property
    def selected_asset_ids(self) -> tuple[str, ...]:
        observed = {item.asset_id for item in self.observations}
        return tuple(
            asset.asset_id for asset in self.reference_registry.assets if asset.asset_id in observed
        )

    @property
    def media_kinds(self) -> tuple[MediaKind, ...]:
        kinds = {item.modality for item in self.observations}
        return tuple(kind for kind in MediaKind if kind in kinds)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": CROSS_REFERENCE_GRAPH_SCHEMA,
            "task_mode": self.task_mode.value,
            "selected_asset_ids": list(self.selected_asset_ids),
            "observations": [item.to_wire() for item in self.observations],
            "estimated_memory_bytes": self.estimated_memory_bytes,
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class CrossReferenceProposal:
    """One explicit provider or caller identity proposal."""

    proposal_id: str
    entity_kind: ReferenceEntityKind
    candidate_ids: tuple[str, ...]
    label: str | None
    observation_ids: tuple[str, ...]
    source_asset_ids: tuple[str, ...]
    evidence: tuple[EvidenceRecord, ...]
    resolution: CrossReferenceResolution = CrossReferenceResolution.RESOLVED
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.proposal_id, "proposal_id")
        _enum(self.entity_kind, ReferenceEntityKind, "proposal entity_kind")
        if self.entity_kind is ReferenceEntityKind.SOURCE_ASSET:
            raise CrossReferenceError(
                "source asset identities are canonical and cannot be proposed"
            )
        candidates = _id_tuple(
            self.candidate_ids,
            "candidate_ids",
            MAX_CROSS_REFERENCE_CANDIDATES,
        )
        object.__setattr__(self, "candidate_ids", candidates)
        if self.label is not None:
            _text(self.label, "proposal label")
        object.__setattr__(
            self,
            "observation_ids",
            _id_tuple(
                self.observation_ids, "proposal observation_ids", MAX_CROSS_REFERENCE_OBSERVATIONS
            ),
        )
        object.__setattr__(
            self,
            "source_asset_ids",
            _id_tuple(
                self.source_asset_ids,
                "proposal source_asset_ids",
                MAX_CROSS_REFERENCE_OBSERVATIONS,
                required=True,
            ),
        )
        object.__setattr__(self, "evidence", _evidence(self.evidence))
        _enum(self.resolution, CrossReferenceResolution, "proposal resolution")
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "proposal uncertainties")
        )
        if (
            self.resolution
            in {
                CrossReferenceResolution.RESOLVED,
                CrossReferenceResolution.USER_SELECTED,
            }
            and len(candidates) != 1
        ):
            raise CrossReferenceError("resolved and user-selected proposals require one candidate")
        if self.resolution is CrossReferenceResolution.AMBIGUOUS and len(candidates) < 2:
            raise CrossReferenceError("ambiguous proposals require at least two candidates")
        if self.resolution is CrossReferenceResolution.UNRESOLVED and candidates:
            raise CrossReferenceError("unresolved proposals must not carry candidate IDs")
        if (
            self.resolution
            in {
                CrossReferenceResolution.RESOLVED,
                CrossReferenceResolution.USER_SELECTED,
            }
            and self.label is None
        ):
            raise CrossReferenceError("resolved proposals require a label")

    def to_wire(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "entity_kind": self.entity_kind.value,
            "candidate_ids": list(self.candidate_ids),
            "label": self.label,
            "observation_ids": list(self.observation_ids),
            "source_asset_ids": list(self.source_asset_ids),
            "evidence": [item.to_wire() for item in self.evidence],
            "resolution": self.resolution.value,
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class CrossReferenceEntity:
    """Stable identity node or explicit unresolved candidate set."""

    entity_id: str | None
    entity_kind: ReferenceEntityKind
    resolution: CrossReferenceResolution
    candidate_ids: tuple[str, ...]
    label: str | None
    observation_ids: tuple[str, ...]
    source_asset_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    uncertainties: tuple[Uncertainty, ...] = ()

    def __post_init__(self) -> None:
        if self.entity_id is not None:
            _identifier(self.entity_id, "entity_id")
        _enum(self.entity_kind, ReferenceEntityKind, "entity_kind")
        _enum(self.resolution, CrossReferenceResolution, "entity resolution")
        object.__setattr__(
            self,
            "candidate_ids",
            _id_tuple(self.candidate_ids, "entity candidate_ids", MAX_CROSS_REFERENCE_CANDIDATES),
        )
        if self.label is not None:
            _text(self.label, "entity label")
        object.__setattr__(
            self,
            "observation_ids",
            _id_tuple(
                self.observation_ids, "entity observation_ids", MAX_CROSS_REFERENCE_OBSERVATIONS
            ),
        )
        object.__setattr__(
            self,
            "source_asset_ids",
            _id_tuple(
                self.source_asset_ids,
                "entity source_asset_ids",
                MAX_CROSS_REFERENCE_OBSERVATIONS,
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "evidence_ids",
            _id_tuple(self.evidence_ids, "entity evidence_ids", MAX_CROSS_REFERENCE_EVIDENCE),
        )
        object.__setattr__(
            self, "uncertainties", _uncertainties(self.uncertainties, "entity uncertainties")
        )

    @property
    def kind(self) -> ReferenceEntityKind:
        """Compatibility alias for concise graph inspection."""

        return self.entity_kind

    def to_wire(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "entity_kind": self.entity_kind.value,
            "resolution": self.resolution.value,
            "candidate_ids": list(self.candidate_ids),
            "label": self.label,
            "observation_ids": list(self.observation_ids),
            "source_asset_ids": list(self.source_asset_ids),
            "evidence_ids": list(self.evidence_ids),
            "uncertainties": [item.to_wire() for item in self.uncertainties],
        }


@dataclass(frozen=True, slots=True)
class CrossReferenceLink:
    """One observation-to-identity link retaining unresolved candidates."""

    link_id: str
    proposal_id: str
    observation_id: str
    entity_kind: ReferenceEntityKind
    entity_id: str | None
    resolution: CrossReferenceResolution
    candidate_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.link_id, "link_id")
        _identifier(self.proposal_id, "link proposal_id")
        _identifier(self.observation_id, "link observation_id")
        _enum(self.entity_kind, ReferenceEntityKind, "link entity_kind")
        if self.entity_id is not None:
            _identifier(self.entity_id, "link entity_id")
        _enum(self.resolution, CrossReferenceResolution, "link resolution")
        object.__setattr__(
            self,
            "candidate_ids",
            _id_tuple(self.candidate_ids, "link candidate_ids", MAX_CROSS_REFERENCE_CANDIDATES),
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "link_id": self.link_id,
            "proposal_id": self.proposal_id,
            "observation_id": self.observation_id,
            "entity_kind": self.entity_kind.value,
            "entity_id": self.entity_id,
            "resolution": self.resolution.value,
            "candidate_ids": list(self.candidate_ids),
        }


@dataclass(frozen=True, slots=True)
class CrossReferenceGraph:
    """Inspectable graph with explicit non-complete identity states."""

    schema: str
    status: CrossReferenceGraphStatus
    selected_asset_ids: tuple[str, ...]
    entities: tuple[CrossReferenceEntity, ...]
    links: tuple[CrossReferenceLink, ...]
    diagnostics: tuple[ValidationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != CROSS_REFERENCE_GRAPH_SCHEMA:
            raise CrossReferenceError("unsupported cross-reference graph schema")
        _enum(self.status, CrossReferenceGraphStatus, "graph status")
        object.__setattr__(
            self,
            "selected_asset_ids",
            _id_tuple(
                self.selected_asset_ids,
                "selected_asset_ids",
                MAX_CROSS_REFERENCE_OBSERVATIONS,
                required=True,
            ),
        )
        if (
            not isinstance(self.entities, tuple)
            or len(self.entities) > MAX_CROSS_REFERENCE_ENTITIES
            or not all(isinstance(item, CrossReferenceEntity) for item in self.entities)
        ):
            raise CrossReferenceError(
                "entities must be a bounded tuple of CrossReferenceEntity values"
            )
        if (
            not isinstance(self.links, tuple)
            or len(self.links) > MAX_CROSS_REFERENCE_LINKS
            or not all(isinstance(item, CrossReferenceLink) for item in self.links)
        ):
            raise CrossReferenceError("links must be a bounded tuple of CrossReferenceLink values")
        if (
            not isinstance(self.diagnostics, tuple)
            or len(self.diagnostics) > 256
            or not all(isinstance(item, ValidationDiagnostic) for item in self.diagnostics)
        ):
            raise CrossReferenceError(
                "diagnostics must be a bounded tuple of ValidationDiagnostic values"
            )
        entity_ids = tuple(item.entity_id for item in self.entities if item.entity_id is not None)
        if len(entity_ids) != len(set(entity_ids)):
            raise CrossReferenceError("resolved entity IDs must be unique")
        link_ids = tuple(item.link_id for item in self.links)
        if len(link_ids) != len(set(link_ids)):
            raise CrossReferenceError("link IDs must be unique")

    @property
    def complete(self) -> bool:
        return self.status is CrossReferenceGraphStatus.COMPLETE

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status.value,
            "complete": self.complete,
            "selected_asset_ids": list(self.selected_asset_ids),
            "entities": [item.to_wire() for item in self.entities],
            "links": [item.to_wire() for item in self.links],
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


def _diagnostic(
    code: str, message: str, location: str, severity: ValidationSeverity
) -> ValidationDiagnostic:
    return ValidationDiagnostic(severity, code, message, location)


def build_cross_reference_graph(
    request: CrossReferenceRequest,
    proposals: tuple[CrossReferenceProposal, ...],
) -> CrossReferenceGraph:
    """Merge explicit proposals while retaining ambiguity and deterministic conflicts."""

    if not isinstance(request, CrossReferenceRequest):
        raise CrossReferenceError("request must be a CrossReferenceRequest")
    if (
        not isinstance(proposals, tuple)
        or len(proposals) > MAX_CROSS_REFERENCE_PROPOSALS
        or not all(isinstance(item, CrossReferenceProposal) for item in proposals)
    ):
        raise CrossReferenceError(
            "proposals must be a bounded tuple of CrossReferenceProposal values"
        )
    proposal_ids = tuple(item.proposal_id for item in proposals)
    if len(proposal_ids) != len(set(proposal_ids)):
        raise CrossReferenceError("proposal IDs must be unique")
    observation_map = {item.observation_id: item for item in request.observations}
    selected_assets = set(request.selected_asset_ids)
    diagnostics: list[ValidationDiagnostic] = []
    source_entities = tuple(
        CrossReferenceEntity(
            f"asset.{asset_id}",
            ReferenceEntityKind.SOURCE_ASSET,
            CrossReferenceResolution.USER_SELECTED,
            (asset_id,),
            asset_id,
            tuple(
                item.observation_id for item in request.observations if item.asset_id == asset_id
            ),
            (asset_id,),
            (),
        )
        for asset_id in request.selected_asset_ids
    )
    entity_data: dict[tuple[ReferenceEntityKind, str], dict[str, object]] = {}
    ambiguous_entities: list[CrossReferenceEntity] = []
    links: list[CrossReferenceLink] = []
    assignments: dict[str, list[tuple[ReferenceEntityKind, str, str]]] = {}
    candidate_kinds: dict[str, ReferenceEntityKind] = {}
    conflict_keys: set[tuple[ReferenceEntityKind, str]] = set()
    seen_evidence: dict[str, EvidenceRecord] = {}
    has_ambiguous = False
    has_partial = False

    for proposal in proposals:
        for observation_id in proposal.observation_ids:
            observation = observation_map.get(observation_id)
            if observation is None:
                raise CrossReferenceError(
                    f"proposal references unknown observation {observation_id!r}"
                )
            if observation.asset_id not in proposal.source_asset_ids:
                raise CrossReferenceError("proposal observation is outside its source asset set")
        if not set(proposal.source_asset_ids).issubset(selected_assets):
            raise CrossReferenceError("proposal references an unselected source asset")
        for evidence in proposal.evidence:
            prior = seen_evidence.get(evidence.evidence_id)
            if prior is not None and prior != evidence:
                raise CrossReferenceError("same evidence ID has conflicting proposal values")
            seen_evidence[evidence.evidence_id] = evidence
            source = evidence.provenance.source
            if (
                source.kind is EvidenceSourceKind.MEDIA_ASSET
                and source.asset_id not in selected_assets
            ):
                raise CrossReferenceError("proposal evidence references an unselected source asset")
        if (
            proposal.resolution
            in {
                CrossReferenceResolution.RESOLVED,
                CrossReferenceResolution.USER_SELECTED,
            }
            and not proposal.evidence
        ):
            raise CrossReferenceError("resolved proposal requires evidence")
        if not proposal.observation_ids or not proposal.evidence:
            diagnostics.append(
                _diagnostic(
                    "missing_evidence",
                    f"proposal {proposal.proposal_id!r} lacks complete observation evidence",
                    f"proposals.{proposal.proposal_id}",
                    ValidationSeverity.WARNING,
                )
            )
            has_partial = True
        if proposal.resolution is CrossReferenceResolution.AMBIGUOUS:
            has_ambiguous = True
            diagnostics.append(
                _diagnostic(
                    "ambiguous_identity",
                    f"proposal {proposal.proposal_id!r} retains multiple candidates",
                    f"proposals.{proposal.proposal_id}",
                    ValidationSeverity.WARNING,
                )
            )
        if proposal.resolution is CrossReferenceResolution.UNRESOLVED:
            has_partial = True
        if proposal.resolution in {
            CrossReferenceResolution.AMBIGUOUS,
            CrossReferenceResolution.UNRESOLVED,
        }:
            ambiguous_entities.append(
                CrossReferenceEntity(
                    None,
                    proposal.entity_kind,
                    proposal.resolution,
                    proposal.candidate_ids,
                    proposal.label,
                    proposal.observation_ids,
                    proposal.source_asset_ids,
                    tuple(item.evidence_id for item in proposal.evidence),
                    proposal.uncertainties,
                )
            )
            for index, observation_id in enumerate(proposal.observation_ids):
                links.append(
                    CrossReferenceLink(
                        f"link.{proposal.proposal_id}.{index}",
                        proposal.proposal_id,
                        observation_id,
                        proposal.entity_kind,
                        None,
                        proposal.resolution,
                        proposal.candidate_ids,
                    )
                )
            continue
        for candidate_id in proposal.candidate_ids:
            prior_kind = candidate_kinds.get(candidate_id)
            if prior_kind is not None and prior_kind is not proposal.entity_kind:
                diagnostics.append(
                    _diagnostic(
                        "duplicate_identity",
                        f"candidate {candidate_id!r} is used by multiple entity kinds",
                        f"proposals.{proposal.proposal_id}",
                        ValidationSeverity.ERROR,
                    )
                )
                conflict_keys.add((prior_kind, candidate_id))
                conflict_keys.add((proposal.entity_kind, candidate_id))
            candidate_kinds[candidate_id] = proposal.entity_kind
            key = (proposal.entity_kind, candidate_id)
            state = entity_data.setdefault(
                key,
                {
                    "label": proposal.label,
                    "observations": [],
                    "assets": [],
                    "evidence": [],
                    "uncertainties": [],
                    "resolution": proposal.resolution,
                },
            )
            if state["label"] != proposal.label and proposal.label is not None:
                diagnostics.append(
                    _diagnostic(
                        "duplicate_identity",
                        f"candidate {candidate_id!r} has conflicting labels",
                        f"proposals.{proposal.proposal_id}",
                        ValidationSeverity.ERROR,
                    )
                )
                conflict_keys.add(key)
            for field, values in (
                ("observations", proposal.observation_ids),
                ("assets", proposal.source_asset_ids),
                ("evidence", tuple(item.evidence_id for item in proposal.evidence)),
                ("uncertainties", proposal.uncertainties),
            ):
                target = cast(list[object], state[field])
                for value in values:
                    if value not in target:
                        target.append(value)
            if proposal.resolution is CrossReferenceResolution.USER_SELECTED:
                state["resolution"] = CrossReferenceResolution.USER_SELECTED
            for observation_id in proposal.observation_ids:
                assignments.setdefault(observation_id, []).append(
                    (proposal.entity_kind, candidate_id, proposal.proposal_id)
                )
                links.append(
                    CrossReferenceLink(
                        f"link.{proposal.proposal_id}.{len(links)}",
                        proposal.proposal_id,
                        observation_id,
                        proposal.entity_kind,
                        candidate_id,
                        proposal.resolution,
                        (candidate_id,),
                    )
                )

    for observation_id, assignment_values in assignments.items():
        distinct: set[tuple[ReferenceEntityKind, str]] = {
            (kind, candidate_id) for kind, candidate_id, _ in assignment_values
        }
        if len(distinct) > 1:
            diagnostics.append(
                _diagnostic(
                    "conflicting_provider_result",
                    f"observation {observation_id!r} has conflicting identity candidates",
                    f"observations.{observation_id}",
                    ValidationSeverity.ERROR,
                )
            )
            conflict_keys.update(distinct)

    entities: list[CrossReferenceEntity] = list(source_entities)
    for (kind, candidate_id), state in entity_data.items():
        resolution = cast(CrossReferenceResolution, state["resolution"])
        if (kind, candidate_id) in conflict_keys:
            resolution = CrossReferenceResolution.CONFLICTING
        entity_id = (
            f"{kind.value}.{candidate_id}"
            if (kind, candidate_id) in conflict_keys
            and any(
                other_kind is not kind and other_id == candidate_id
                for other_kind, other_id in conflict_keys
            )
            else candidate_id
        )
        entities.append(
            CrossReferenceEntity(
                entity_id,
                kind,
                resolution,
                (candidate_id,),
                cast(str | None, state["label"]),
                tuple(cast(list[str], state["observations"])),
                tuple(cast(list[str], state["assets"])),
                tuple(cast(list[str], state["evidence"])),
                tuple(cast(list[Uncertainty], state["uncertainties"])),
            )
        )
    entities.extend(ambiguous_entities)
    if not proposals:
        status = CrossReferenceGraphStatus.EMPTY
    elif conflict_keys:
        status = CrossReferenceGraphStatus.CONFLICTING
    elif has_ambiguous:
        status = CrossReferenceGraphStatus.AMBIGUOUS
    elif has_partial:
        status = CrossReferenceGraphStatus.PARTIAL
    else:
        status = CrossReferenceGraphStatus.COMPLETE
    return CrossReferenceGraph(
        CROSS_REFERENCE_GRAPH_SCHEMA,
        status,
        request.selected_asset_ids,
        tuple(entities),
        tuple(links),
        tuple(diagnostics),
    )


@runtime_checkable
class CrossReferenceAdapter(Protocol):
    """Explicit local adapter seam for proposal assembly."""

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        """Return a descriptor supporting the request's media modalities."""

    def analyze(
        self, request: CrossReferenceRequest, guard: LocalBudgetGuard
    ) -> CrossReferenceGraph:
        """Return an already-built graph; no hard constraints are mutated."""


class _CrossReferenceAdapterBridge:
    def __init__(self, adapter: CrossReferenceAdapter) -> None:
        self._adapter = adapter

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._adapter.descriptor

    def run(
        self,
        request: LocalAdapterExecutionRequest,
        guard: LocalBudgetGuard,
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, CrossReferenceRequest):
            raise CrossReferenceError("cross-reference adapter input is invalid")
        graph = self._adapter.analyze(request.input_value, guard)
        if not isinstance(graph, CrossReferenceGraph):
            raise CrossReferenceError("cross-reference adapter returned an invalid graph")
        if graph.schema != self.descriptor.output_schema:
            raise CrossReferenceError("cross-reference adapter returned an unexpected schema")
        output_bytes = sum(
            len((entity.label or "").encode("utf-8")) for entity in graph.entities
        ) + sum(len(item.message.encode("utf-8")) for item in graph.diagnostics)
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=graph,
            output_bytes=output_bytes,
            output_items=len(graph.entities) + len(graph.links),
        )


def _validate_graph(graph: CrossReferenceGraph, request: CrossReferenceRequest) -> None:
    if graph.selected_asset_ids != request.selected_asset_ids:
        raise CrossReferenceError("graph selected assets do not match the request")
    observation_ids = set(request.observation_ids)
    selected_assets = set(request.selected_asset_ids)
    for entity in graph.entities:
        if not set(entity.source_asset_ids).issubset(selected_assets):
            raise CrossReferenceError("graph entity references an unselected source asset")
        if not set(entity.observation_ids).issubset(observation_ids):
            raise CrossReferenceError("graph entity references an unknown observation")
    for link in graph.links:
        if link.observation_id not in observation_ids:
            raise CrossReferenceError("graph link references an unknown observation")


def execute_cross_reference_graph(
    adapter: CrossReferenceAdapter,
    request: CrossReferenceRequest,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    deterministic_required: bool = False,
    seed: int | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> CrossReferenceGraph:
    """Run one explicitly selected cross-reference adapter through M5-01 guards."""

    if not isinstance(adapter, CrossReferenceAdapter):
        raise CrossReferenceError("adapter must implement CrossReferenceAdapter")
    if not isinstance(request, CrossReferenceRequest):
        raise CrossReferenceError("request must be a CrossReferenceRequest")
    device_value = LocalDeviceSpec(LocalDeviceKind.AUTO) if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise CrossReferenceError("device must be a LocalDeviceSpec")
    execution_request = LocalAdapterExecutionRequest(
        adapter_id=adapter.descriptor.adapter_id,
        task_mode=request.task_mode,
        media_kinds=request.media_kinds,
        reference_count=len(request.selected_asset_ids),
        device=device_value,
        estimated_memory_bytes=request.estimated_memory_bytes,
        deterministic_required=deterministic_required,
        seed=request.seed if seed is None else seed,
        cancellation_required=cancellation_probe is not None,
        input_value=request,
    )
    bridge = _CrossReferenceAdapterBridge(adapter)
    result = run_local_adapter(
        bridge,
        execution_request,
        runtime=runtime,
        cancellation_probe=cancellation_probe,
        **({"clock": clock} if clock is not None else {}),
        memory_meter=memory_meter,
    )
    if not isinstance(result.value, CrossReferenceGraph):
        raise CrossReferenceError("adapter result does not contain a cross-reference graph")
    _validate_graph(result.value, request)
    return result.value


__all__ = [
    "CROSS_REFERENCE_GRAPH_SCHEMA",
    "MAX_CROSS_REFERENCE_CANDIDATES",
    "MAX_CROSS_REFERENCE_ENTITIES",
    "MAX_CROSS_REFERENCE_EVIDENCE",
    "MAX_CROSS_REFERENCE_LINKS",
    "MAX_CROSS_REFERENCE_MEMORY_BYTES",
    "MAX_CROSS_REFERENCE_OBSERVATIONS",
    "MAX_CROSS_REFERENCE_PROPOSALS",
    "MAX_CROSS_REFERENCE_TEXT_LENGTH",
    "MAX_CROSS_REFERENCE_UNCERTAINTIES",
    "CrossReferenceAdapter",
    "CrossReferenceEntity",
    "CrossReferenceGraph",
    "CrossReferenceGraphStatus",
    "CrossReferenceLink",
    "CrossReferenceProposal",
    "CrossReferenceRequest",
    "CrossReferenceResolution",
    "ObservationReference",
    "ReferenceEntityKind",
    "build_cross_reference_graph",
    "execute_cross_reference_graph",
]
