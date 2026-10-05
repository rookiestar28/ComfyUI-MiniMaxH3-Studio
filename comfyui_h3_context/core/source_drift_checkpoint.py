"""Bounded pre-productization source, model, and host-API drift checkpoint.

The checkpoint is a frozen, content-free observation record.  It never performs network access,
imports ComfyUI/Ollama/model runtimes, opens media, or upgrades a supported revision.  Mutable
upstream observations remain separate from the exact supported pair and material drift blocks the
affected seam until a later item supplies requalification evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import cast

from .assisted_authoring_scope import AssistedAuthoringState
from .canonical import canonical_fingerprint
from .fidelity_scorecard import FROZEN_FIDELITY_SCORECARD_FINGERPRINT
from .prompt_model_provider import load_prompt_model_catalog

SOURCE_DRIFT_CHECKPOINT_SCHEMA = "h3.source_drift_checkpoint.v2"
SOURCE_DRIFT_CHECKPOINT_VERSION = "2.0.0"
SOURCE_DRIFT_CHECKPOINT_RETRIEVED_AT = "2026-08-10T00:39:00+08:00"
MAX_SOURCE_DRIFT_WIRE_BYTES = 65_536
MAX_SOURCE_DRIFT_SURFACES = 64
MAX_SOURCE_DRIFT_STRING_LENGTH = 512

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_IDENTITY = re.compile(r"(?:git|gitblob):[0-9a-f]{40}|sha256:[0-9a-f]{64}\Z")
_OFFICIAL_URL_PREFIXES = (
    "https://github.com/MiniMax-AI/",
    "https://huggingface.co/MiniMaxAI/",
    "https://github.com/Comfy-Org/",
    "https://docs.comfy.org/",
    "https://github.com/ollama/",
    "https://docs.ollama.com/",
)


class SourceDriftCheckpointError(ValueError):
    """Raised when a checkpoint is open, drift-forgiving, or claim-escalating."""


class SourceSurfaceGroup(str, Enum):
    MINIMAX_REPOSITORY = "minimax_repository"
    MINIMAX_MODEL = "minimax_model"
    MINIMAX_GUIDES = "minimax_guides"
    MINIMAX_API = "minimax_api"
    COMFYUI_CORE = "comfyui_core"
    COMFYUI_FRONTEND = "comfyui_frontend"
    COMFYUI_NATIVE_H3 = "comfyui_native_h3"
    OLLAMA_DOCS = "ollama_docs"
    OLLAMA_SERVER = "ollama_server"
    OLLAMA_PYTHON_CLIENT = "ollama_python_client"
    M14_EVIDENCE = "m14_evidence"


class DriftDisposition(str, Enum):
    EXACT_MATCH = "EXACT_MATCH"
    ADDITIVE_NON_SUBSTITUTING = "ADDITIVE_NON_SUBSTITUTING"
    BLOCKED_PENDING_REQUALIFICATION = "BLOCKED_PENDING_REQUALIFICATION"


class CheckpointDisposition(str, Enum):
    PASS_WITH_BLOCKED_DRIFT = "PASS_WITH_BLOCKED_DRIFT"  # noqa: S105


def _bounded_identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise SourceDriftCheckpointError(f"{field_name} is not a bounded identifier")
    return value


def _bounded_string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > 32:
        raise SourceDriftCheckpointError(f"{field_name} is not a bounded tuple")
    for item in value:
        if (
            type(item) is not str
            or not item
            or len(item) > MAX_SOURCE_DRIFT_STRING_LENGTH
            or _TOKEN.fullmatch(item) is None
        ):
            raise SourceDriftCheckpointError(f"{field_name} contains a non-identifier value")
    if len(value) != len(set(value)):
        raise SourceDriftCheckpointError(f"{field_name} contains duplicates")
    return value


@dataclass(frozen=True, slots=True)
class SourceSurface:
    """One immutable official-source observation and its drift disposition."""

    surface_id: str
    group: SourceSurfaceGroup
    official_url: str
    retrieved_at: str
    expected_identity: str
    observed_identity: str
    material: bool
    disposition: DriftDisposition
    affected_seams: tuple[str, ...]
    requalification_owners: tuple[str, ...]

    def __post_init__(self) -> None:
        _bounded_identifier(self.surface_id, "surface_id")
        if type(self.group) is not SourceSurfaceGroup:
            raise SourceDriftCheckpointError("surface group must be an exact enum member")
        if (
            type(self.official_url) is not str
            or len(self.official_url) > MAX_SOURCE_DRIFT_STRING_LENGTH
            or not self.official_url.startswith(_OFFICIAL_URL_PREFIXES)
            or any(marker in self.official_url for marker in ("?", "#", "@", "\\"))
        ):
            raise SourceDriftCheckpointError("surface official_url is not allowlisted")
        if type(self.retrieved_at) is not str or self.retrieved_at != (
            SOURCE_DRIFT_CHECKPOINT_RETRIEVED_AT
        ):
            raise SourceDriftCheckpointError("surface retrieval time is not frozen")
        for value, field_name in (
            (self.expected_identity, "expected_identity"),
            (self.observed_identity, "observed_identity"),
        ):
            if type(value) is not str or _IDENTITY.fullmatch(value) is None:
                raise SourceDriftCheckpointError(f"{field_name} is malformed")
        if type(self.material) is not bool:
            raise SourceDriftCheckpointError("surface material flag must be exact bool")
        if type(self.disposition) is not DriftDisposition:
            raise SourceDriftCheckpointError("surface disposition must be an exact enum member")
        _bounded_string_tuple(self.affected_seams, "affected_seams")
        _bounded_string_tuple(self.requalification_owners, "requalification_owners")
        if self.disposition is DriftDisposition.EXACT_MATCH:
            if self.expected_identity != self.observed_identity:
                raise SourceDriftCheckpointError("exact-match surface identity drifted")
            if self.requalification_owners:
                raise SourceDriftCheckpointError(
                    "exact-match surface must not name requalification owners"
                )
        elif self.expected_identity == self.observed_identity:
            raise SourceDriftCheckpointError("non-match surface must preserve distinct identities")
        if self.disposition is DriftDisposition.BLOCKED_PENDING_REQUALIFICATION:
            if not self.material or not self.affected_seams or not self.requalification_owners:
                raise SourceDriftCheckpointError("blocked material drift requires seams and owners")

    def to_wire(self) -> dict[str, object]:
        return {
            "surface_id": self.surface_id,
            "group": self.group.value,
            "official_url": self.official_url,
            "retrieved_at": self.retrieved_at,
            "expected_identity": self.expected_identity,
            "observed_identity": self.observed_identity,
            "material": self.material,
            "disposition": self.disposition.value,
            "affected_seams": list(self.affected_seams),
            "requalification_owners": list(self.requalification_owners),
        }


def _surface(
    surface_id: str,
    group: SourceSurfaceGroup,
    official_url: str,
    expected_identity: str,
    observed_identity: str | None = None,
    *,
    material: bool = False,
    disposition: DriftDisposition = DriftDisposition.EXACT_MATCH,
    affected_seams: tuple[str, ...] = (),
    owners: tuple[str, ...] = (),
) -> SourceSurface:
    return SourceSurface(
        surface_id=surface_id,
        group=group,
        official_url=official_url,
        retrieved_at=SOURCE_DRIFT_CHECKPOINT_RETRIEVED_AT,
        expected_identity=expected_identity,
        observed_identity=expected_identity if observed_identity is None else observed_identity,
        material=material,
        disposition=disposition,
        affected_seams=affected_seams,
        requalification_owners=owners,
    )


def _surface_inventory() -> tuple[SourceSurface, ...]:
    minimax_repo = (
        "https://github.com/MiniMax-AI/MiniMax-H3/tree/b7227fa6a6206e9fb30562383d39e53cf3866a48"
    )
    comfy_supported = (
        "https://github.com/Comfy-Org/ComfyUI/tree/c44dea18809e3ca0e12e25cbe6938c2c45a29c9d"
    )
    frontend_supported = (
        "https://github.com/Comfy-Org/ComfyUI_frontend/tree/"
        "f339c6b2ec0cc90e1c4fa717d9f7d6d565a8f6da"
    )
    return (
        _surface(
            "minimax.repository",
            SourceSurfaceGroup.MINIMAX_REPOSITORY,
            minimax_repo,
            "git:b7227fa6a6206e9fb30562383d39e53cf3866a48",
        ),
        _surface(
            "minimax.readme",
            SourceSurfaceGroup.MINIMAX_REPOSITORY,
            minimax_repo,
            "sha256:6e4cfcfd85a796cf3c965a2dcc0cc328b59dd680d9718d1e0fef0add7aca8ada",
        ),
        _surface(
            "minimax.guide.base",
            SourceSurfaceGroup.MINIMAX_GUIDES,
            minimax_repo,
            "sha256:2cfebc096a6e08370f288d468d90b60f7f9bcb938f94bf090816e910e48e75fc",
        ),
        _surface(
            "minimax.guide.reference",
            SourceSurfaceGroup.MINIMAX_GUIDES,
            minimax_repo,
            "sha256:1e574f356716ad55612247ffb7bbccbcdb484ad96599d63c7dca1af186b1fab7",
        ),
        _surface(
            "minimax.model.revision",
            SourceSurfaceGroup.MINIMAX_MODEL,
            "https://huggingface.co/MiniMaxAI/MiniMax-H3/tree/"
            "6818f6c32d12b210915e44ad56a4228c2608f160",  # pragma: allowlist secret
            "git:6818f6c32d12b210915e44ad56a4228c2608f160",
        ),
        _surface(
            "minimax.model.license",
            SourceSurfaceGroup.MINIMAX_MODEL,
            "https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/"
            "6818f6c32d12b210915e44ad56a4228c2608f160/LICENSE",
            "sha256:59b99642b95ea21630e311198ddbfffbfe05aadba0c2f5d884cbdf4efcc90f44",
        ),
        _surface(
            "minimax.context_ir.examples",
            SourceSurfaceGroup.MINIMAX_API,
            minimax_repo,
            "git:b7227fa6a6206e9fb30562383d39e53cf3866a48",
        ),
        _surface(
            "comfyui.supported.core",
            SourceSurfaceGroup.COMFYUI_CORE,
            comfy_supported,
            "git:c44dea18809e3ca0e12e25cbe6938c2c45a29c9d",
        ),
        _surface(
            "comfyui.supported.text_generate",
            SourceSurfaceGroup.COMFYUI_CORE,
            comfy_supported,
            "gitblob:5a947d5c57b32e7e8d2ed1ef364064959d841cc4",
        ),
        _surface(
            "comfyui.supported.clip_loader",
            SourceSurfaceGroup.COMFYUI_CORE,
            comfy_supported,
            "gitblob:08529d8dc414138e56d0cc6e68367f95da70f038",
        ),
        _surface(
            "comfyui.supported.native_h3",
            SourceSurfaceGroup.COMFYUI_NATIVE_H3,
            comfy_supported,
            "gitblob:22bc91cd4570a071c664ec4848a5be748909e1e7",
        ),
        _surface(
            "comfyui.supported.frontend",
            SourceSurfaceGroup.COMFYUI_FRONTEND,
            frontend_supported,
            "git:f339c6b2ec0cc90e1c4fa717d9f7d6d565a8f6da",
        ),
        _surface(
            "comfyui.docs.v3",
            SourceSurfaceGroup.COMFYUI_CORE,
            "https://docs.comfy.org/custom-nodes/v3_migration",
            "sha256:bc6c4a7bdafba5e0fcad23186ea0478a841b708451c924e0337302f8b5e677a5",
        ),
        _surface(
            "comfyui.docs.sidebar",
            SourceSurfaceGroup.COMFYUI_FRONTEND,
            "https://docs.comfy.org/custom-nodes/js/javascript_sidebar_tabs",
            "sha256:da8f294db0ef5ccfd278e615495f7b2202b9a8d7e9067b22a9ba1b0c37c184f1",
        ),
        _surface(
            "comfyui.latest.core",
            SourceSurfaceGroup.COMFYUI_CORE,
            "https://github.com/Comfy-Org/ComfyUI/tree/cbbc9dab1f03d0d9a6caa8a8be7d77a7e37e1e44",
            "git:c44dea18809e3ca0e12e25cbe6938c2c45a29c9d",
            "git:cbbc9dab1f03d0d9a6caa8a8be7d77a7e37e1e44",
            disposition=DriftDisposition.ADDITIVE_NON_SUBSTITUTING,
            affected_seams=("latest_host_compatibility",),
            owners=("M16-01",),
        ),
        _surface(
            "comfyui.latest.native_h3",
            SourceSurfaceGroup.COMFYUI_NATIVE_H3,
            "https://github.com/Comfy-Org/ComfyUI/blob/"
            "cbbc9dab1f03d0d9a6caa8a8be7d77a7e37e1e44/"
            "comfy_extras/nodes_minimax_h3.py",
            "gitblob:22bc91cd4570a071c664ec4848a5be748909e1e7",
            "gitblob:0b1840e851c248f89e9920159c3c8237fa2e7186",
            material=True,
            disposition=DriftDisposition.BLOCKED_PENDING_REQUALIFICATION,
            affected_seams=("native_sigma_sampler", "latest_native_workflow"),
            owners=("M15-05", "M16-01"),
        ),
        _surface(
            "comfyui.latest.frontend",
            SourceSurfaceGroup.COMFYUI_FRONTEND,
            "https://github.com/Comfy-Org/ComfyUI_frontend/tree/"
            "a8f73de1f78396562503fa96a282f23f6e6a7e6b",  # pragma: allowlist secret
            "git:f339c6b2ec0cc90e1c4fa717d9f7d6d565a8f6da",
            "git:a8f73de1f78396562503fa96a282f23f6e6a7e6b",
            disposition=DriftDisposition.ADDITIVE_NON_SUBSTITUTING,
            affected_seams=("latest_frontend_compatibility",),
            owners=("M16-01",),
        ),
        _surface(
            "ollama.docs.introduction",
            SourceSurfaceGroup.OLLAMA_DOCS,
            "https://docs.ollama.com/api/introduction",
            "sha256:a4f4f1d59067ebff92da446d1c92c80a2a4b6066e566d9c9dc0cba96ba877394",
        ),
        _surface(
            "ollama.docs.chat",
            SourceSurfaceGroup.OLLAMA_DOCS,
            "https://docs.ollama.com/api/chat",
            "sha256:309ef5fbdc781525eb04e091478c51e4e17be3f6baafb7bd4b6e3b23e726cb49",
        ),
        _surface(
            "ollama.docs.structured_outputs",
            SourceSurfaceGroup.OLLAMA_DOCS,
            "https://docs.ollama.com/capabilities/structured-outputs",
            "sha256:4e21cd3eca241db09adb7775c98ca8b2e1878d76fde1684ef34531345df7aa6f",
        ),
        _surface(
            "ollama.docs.tags",
            SourceSurfaceGroup.OLLAMA_DOCS,
            "https://docs.ollama.com/api/tags",
            "sha256:70b873546ce4cb19113f8de8ce3cd8fb22e36d130c80d2a753254f2a45e1b20f",
        ),
        _surface(
            "ollama.docs.show",
            SourceSurfaceGroup.OLLAMA_DOCS,
            "https://docs.ollama.com/api-reference/show-model-details",
            "sha256:75e200fa9ec92986579c8fcc7319c38fd85dc1310e853ee20eeba1d1fb02d2ad",
        ),
        _surface(
            "ollama.docs.ps",
            SourceSurfaceGroup.OLLAMA_DOCS,
            "https://docs.ollama.com/api/ps",
            "sha256:c7d9eb1ee9ecae4a275aa5b6a5ff209e4a303b32b3fc994d60aec853ef5fd8ee",
        ),
        _surface(
            "ollama.latest.server",
            SourceSurfaceGroup.OLLAMA_SERVER,
            "https://github.com/ollama/ollama/tree/5a173edb63dbe5d1f555ae258a7930135d7c9336",
            "git:144893850fa778c8c81ff931f26614d62e6689c1",
            "git:5a173edb63dbe5d1f555ae258a7930135d7c9336",
            material=True,
            disposition=DriftDisposition.BLOCKED_PENDING_REQUALIFICATION,
            affected_seams=("ollama_server_api", "ollama_runtime_qualification"),
            owners=("M15-05", "M15-06"),
        ),
        _surface(
            "ollama.python_client",
            SourceSurfaceGroup.OLLAMA_PYTHON_CLIENT,
            "https://github.com/ollama/ollama-python/tree/25b93290d8cd07b0d00732641f812ee34fd4c989",
            "git:25b93290d8cd07b0d00732641f812ee34fd4c989",
        ),
        _surface(
            "m14.fidelity_scorecard",
            SourceSurfaceGroup.M14_EVIDENCE,
            minimax_repo,
            FROZEN_FIDELITY_SCORECARD_FINGERPRINT,
        ),
    )


_LIMITATIONS = (
    "latest_comfyui_native_sigma_sampler_requires_requalification",
    "latest_comfyui_frontend_is_non_substituting",
    "latest_ollama_server_requires_requalification",
    "no_weight_backed_h3_execution",
    "no_official_oracle_execution",
    "no_assisted_profile_qualification",
    "not_legal_advice",
)


@dataclass(frozen=True, slots=True)
class SourceDriftCheckpoint:
    """Exact accepted observation set and non-escalating product disposition."""

    surfaces: tuple[SourceSurface, ...]
    schema: str = SOURCE_DRIFT_CHECKPOINT_SCHEMA
    version: str = SOURCE_DRIFT_CHECKPOINT_VERSION
    retrieved_at: str = SOURCE_DRIFT_CHECKPOINT_RETRIEVED_AT
    disposition: CheckpointDisposition = CheckpointDisposition.PASS_WITH_BLOCKED_DRIFT
    product_scope: str = "MANUAL_ONLY_SCOPED"
    non_equivalence_preserved: bool = True
    assisted_defaults_authorized: bool = False
    assisted_profiles: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            profile.profile_id for profile in load_prompt_model_catalog().profiles
        )
    )
    assisted_authoring: AssistedAuthoringState = field(
        default_factory=lambda: AssistedAuthoringState(True, False, False, False, False)
    )
    supported_host_qualified: bool = True
    latest_host_qualified: bool = False
    native_prompt_boundary: str = "STRING"
    autogrow_path_basis: str = "fully_qualified_zero_based"
    presentation_label_basis: str = "one_based"
    fidelity_scorecard_fingerprint: str = FROZEN_FIDELITY_SCORECARD_FINGERPRINT
    paid_calls: tuple[str, ...] = ()
    model_executions: tuple[str, ...] = ()
    media_executions: tuple[str, ...] = ()
    provider_executions: tuple[str, ...] = ()
    spend_microusd: int = 0
    limitations: tuple[str, ...] = _LIMITATIONS

    def __post_init__(self) -> None:
        if type(self) is not SourceDriftCheckpoint:
            raise SourceDriftCheckpointError("checkpoint must be an exact contract value")
        expected_surfaces = _surface_inventory()
        if type(self.surfaces) is not tuple or self.surfaces != expected_surfaces:
            raise SourceDriftCheckpointError("checkpoint surface inventory drifted")
        exact_values = (
            (self.schema, SOURCE_DRIFT_CHECKPOINT_SCHEMA, "schema"),
            (self.version, SOURCE_DRIFT_CHECKPOINT_VERSION, "version"),
            (self.retrieved_at, SOURCE_DRIFT_CHECKPOINT_RETRIEVED_AT, "retrieved_at"),
            (
                self.disposition,
                CheckpointDisposition.PASS_WITH_BLOCKED_DRIFT,
                "disposition",
            ),
            (self.product_scope, "MANUAL_ONLY_SCOPED", "product_scope"),
            (self.non_equivalence_preserved, True, "non_equivalence_preserved"),
            (self.assisted_defaults_authorized, False, "assisted_defaults_authorized"),
            (
                self.assisted_profiles,
                tuple(profile.profile_id for profile in load_prompt_model_catalog().profiles),
                "assisted_profiles",
            ),
            (
                self.assisted_authoring,
                AssistedAuthoringState(True, False, False, False, False),
                "assisted_authoring",
            ),
            (self.supported_host_qualified, True, "supported_host_qualified"),
            (self.latest_host_qualified, False, "latest_host_qualified"),
            (self.native_prompt_boundary, "STRING", "native_prompt_boundary"),
            (
                self.autogrow_path_basis,
                "fully_qualified_zero_based",
                "autogrow_path_basis",
            ),
            (self.presentation_label_basis, "one_based", "presentation_label_basis"),
            (
                self.fidelity_scorecard_fingerprint,
                FROZEN_FIDELITY_SCORECARD_FINGERPRINT,
                "fidelity_scorecard_fingerprint",
            ),
            (self.paid_calls, (), "paid_calls"),
            (self.model_executions, (), "model_executions"),
            (self.media_executions, (), "media_executions"),
            (self.provider_executions, (), "provider_executions"),
            (self.spend_microusd, 0, "spend_microusd"),
            (self.limitations, _LIMITATIONS, "limitations"),
        )
        for actual, expected, field_name in exact_values:
            if type(actual) is not type(expected) or actual != expected:
                raise SourceDriftCheckpointError(
                    f"checkpoint {field_name} is not the admitted value"
                )
        if len(self.surfaces) > MAX_SOURCE_DRIFT_SURFACES:
            raise SourceDriftCheckpointError("checkpoint has too many source surfaces")
        if {surface.group for surface in self.surfaces} != set(SourceSurfaceGroup):
            raise SourceDriftCheckpointError("checkpoint source groups are incomplete")
        if len({surface.surface_id for surface in self.surfaces}) != len(self.surfaces):
            raise SourceDriftCheckpointError("checkpoint source IDs are not unique")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version": self.version,
            "retrieved_at": self.retrieved_at,
            "disposition": self.disposition.value,
            "product_scope": self.product_scope,
            "non_equivalence_preserved": self.non_equivalence_preserved,
            "assisted_defaults_authorized": self.assisted_defaults_authorized,
            "assisted_profiles": list(self.assisted_profiles),
            "assisted_authoring": self.assisted_authoring.to_wire(),
            "supported_host_qualified": self.supported_host_qualified,
            "latest_host_qualified": self.latest_host_qualified,
            "native_prompt_boundary": self.native_prompt_boundary,
            "autogrow_path_basis": self.autogrow_path_basis,
            "presentation_label_basis": self.presentation_label_basis,
            "fidelity_scorecard_fingerprint": self.fidelity_scorecard_fingerprint,
            "surfaces": [surface.to_wire() for surface in self.surfaces],
            "paid_calls": list(self.paid_calls),
            "model_executions": list(self.model_executions),
            "media_executions": list(self.media_executions),
            "provider_executions": list(self.provider_executions),
            "spend_microusd": self.spend_microusd,
            "limitations": list(self.limitations),
        }


def build_default_source_drift_checkpoint() -> SourceDriftCheckpoint:
    """Return a fresh exact checkpoint without consulting mutable external state."""

    return SourceDriftCheckpoint(surfaces=_surface_inventory())


def _reject_non_json_types(value: object, *, depth: int = 0) -> None:
    if depth > 8:
        raise SourceDriftCheckpointError("checkpoint wire exceeds the depth limit")
    if value is None or type(value) in {str, int, bool}:
        if type(value) is str and len(value) > MAX_SOURCE_DRIFT_STRING_LENGTH:
            raise SourceDriftCheckpointError("checkpoint wire string exceeds the limit")
        return
    if type(value) is list:
        if len(value) > MAX_SOURCE_DRIFT_SURFACES * 12:
            raise SourceDriftCheckpointError("checkpoint wire list exceeds the limit")
        for item in value:
            _reject_non_json_types(item, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > 64:
            raise SourceDriftCheckpointError("checkpoint wire mapping exceeds the limit")
        for key, item in value.items():
            if type(key) is not str:
                raise SourceDriftCheckpointError("checkpoint wire key must be a string")
            _reject_non_json_types(item, depth=depth + 1)
        return
    raise SourceDriftCheckpointError("checkpoint wire contains a non-JSON exact type")


def validate_source_drift_checkpoint_wire(value: object) -> SourceDriftCheckpoint:
    """Validate a portable wire value against the exact frozen checkpoint."""

    _reject_non_json_types(value)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SourceDriftCheckpointError("checkpoint wire is not canonical JSON") from exc
    if len(encoded) > MAX_SOURCE_DRIFT_WIRE_BYTES:
        raise SourceDriftCheckpointError("checkpoint wire exceeds the byte limit")
    expected = build_default_source_drift_checkpoint()
    expected_bytes = json.dumps(
        expected.to_wire(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if encoded != expected_bytes:
        raise SourceDriftCheckpointError("checkpoint wire does not match the frozen observation")
    return expected


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SourceDriftCheckpointError(f"checkpoint JSON contains duplicate member {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SourceDriftCheckpointError(f"checkpoint JSON contains invalid constant {value!r}")


def decode_source_drift_checkpoint_json(
    payload: str | bytes | bytearray,
) -> SourceDriftCheckpoint:
    """Decode strict UTF-8 JSON and reject duplicate members before exact validation."""

    # CRITICAL: exact built-in types prevent hostile subclasses from spoofing encode/bytes length.
    if type(payload) is str:
        try:
            encoded = payload.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise SourceDriftCheckpointError("checkpoint JSON is not strict UTF-8") from exc
        text = payload
    elif type(payload) in (bytes, bytearray):
        encoded = bytes(cast(bytes | bytearray, payload))
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise SourceDriftCheckpointError("checkpoint JSON is not strict UTF-8") from exc
    else:
        raise SourceDriftCheckpointError("checkpoint JSON must be text or bytes")
    if len(encoded) > MAX_SOURCE_DRIFT_WIRE_BYTES:
        raise SourceDriftCheckpointError("checkpoint JSON exceeds the byte limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except SourceDriftCheckpointError:
        raise
    except json.JSONDecodeError as exc:
        raise SourceDriftCheckpointError("checkpoint JSON is malformed") from exc
    return validate_source_drift_checkpoint_wire(value)


__all__ = [
    "CheckpointDisposition",
    "DriftDisposition",
    "MAX_SOURCE_DRIFT_WIRE_BYTES",
    "SOURCE_DRIFT_CHECKPOINT_RETRIEVED_AT",
    "SOURCE_DRIFT_CHECKPOINT_SCHEMA",
    "SOURCE_DRIFT_CHECKPOINT_VERSION",
    "SourceDriftCheckpoint",
    "SourceDriftCheckpointError",
    "SourceSurface",
    "SourceSurfaceGroup",
    "build_default_source_drift_checkpoint",
    "decode_source_drift_checkpoint_json",
    "validate_source_drift_checkpoint_wire",
]
