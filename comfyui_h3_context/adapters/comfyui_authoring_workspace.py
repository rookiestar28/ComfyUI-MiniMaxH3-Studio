"""M20-03: the authoring-workspace adapter — one bounded door for reference and timeline commands.

The Production editor needs the accepted M20-00 reference-set domain and M20-02 timeline domain
reachable from the sidebar.  This adapter is that single door, and three rules shape it:

* **The domains stay canonical.**  Every mutation is decoded, bounded, and handed verbatim to
  `core.reference_set_authoring` / `core.timeline_authoring`; the adapter never re-implements a
  label, pairing, capacity or geometry rule.  A domain rejection returns HTTP 409 *with* the
  current projection and the machine-readable code, so the UI can discard its draft, announce
  the reason and re-render accepted truth — no partial state, no silent drop.

* **Seeding is a claim, not a copy of content.**  A workspace is created from one exact live
  Context workspace through `claim_sidebar_authoring_seed`; each accepted registry asset maps to
  an M20-00 admitted source with a canonical identity fingerprint and a duration fact.  A timed
  source without a duration fact — or one the H3-Base aggregate cannot hold — is recorded as
  not-admissible with its exact reason and shown, never guessed at and never dropped.

* **Availability has one producer.**  Only the frozen producer identity
  ``frontend.host.graphReferenceQualification`` may post availability facts, with a monotonic
  producer revision, through `apply_availability_facts`.  No UI command can author availability,
  and `unknown` keeps blocking the queue instead of collapsing either way.

The route follows the accepted adapter pattern: lazy idempotent registration against an already
imported host, strict origin/content-type/size guards, a closed action vocabulary, a replaying
request ledger, TTL-pruned entries and tombstones, and a content-free wire in both directions.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..core import length
from ..core.authoring_media import CreateLeaseRequest
from ..core.authoring_preview_protocol import AuthoringPreviewRequest
from ..core.canonical import canonical_fingerprint
from ..core.composition_contract import PublicCompositionSnapshot
from ..core.contracts import MediaKind
from ..core.errors import ContractValidationError
from ..core.nle_authoring_contract import (
    NLE_AUTHORING_PROFILE_ID,
    NLE_AUTHORING_SCHEMA,
    NLE_OPERATION_PROFILE_ID,
    TIMELINE_TRANSACTION_SCHEMA_V2,
    materialize_render_snapshot,
)
from ..core.production_authoring_import import (
    PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA,
    PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2,
    PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA,
    PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA_V2,
    ProductionAuthoringImportReceipt,
    ProductionAuthoringImportReceiptV2,
    ProductionAuthoringImportRequest,
    ProductionAuthoringImportRequestV2,
    ProductionAuthoringImportResponse,
    ProductionAuthoringImportResponseV2,
    ProductionAuthoringImportRow,
)
from ..core.reference_set_authoring import (
    AddSource,
    AdmittedSourceInput,
    AvailabilityFact,
    AvailabilityFactSet,
    ExcludeSoundtrack,
    IncludeSoundtrack,
    ReferenceSetState,
    RemoveSource,
    ReorderSource,
    SoundtrackAvailability,
    TimedReferenceLimits,
    apply_availability_facts,
    apply_video_source_batch,
    build_h3_base_capacity,
    canonical_projection,
    capacity_projection,
    create_reference_set,
    derived_soundtrack_state,
)
from ..core.reference_set_authoring import (
    apply_command as apply_reference_command,
)
from ..core.temporal_profile import build_temporal_profile
from ..core.timeline_authoring import (
    AddClip,
    EnvelopePoint,
    LinkClips,
    MergeClips,
    MoveClip,
    MoveGroup,
    RemoveClip,
    SelectClips,
    SetEnvelope,
    SplitClip,
    TimelineState,
    TrimClip,
    TrimEdge,
    UnlinkClips,
    build_h3_timeline_limits,
    build_h3_timeline_profile,
    create_timeline,
    reference_view,
    snap_candidates,
    timeline_blockers,
)
from ..core.timeline_authoring import (
    apply_command as apply_timeline_command,
)
from ..core.timeline_history import (
    MAX_TRANSACTION_BYTES,
    MAX_TRANSACTION_COMMANDS,
    TIMELINE_TRANSACTION_SCHEMA,
    TimelineHistoryState,
    apply_timeline_transaction,
    decode_timeline_transaction,
    refresh_timeline_asset_catalog,
    release_timeline_history,
)
from ..core.timeline_history_v2 import (
    TimelineHistoryStateV2,
    apply_timeline_transaction_v2,
    decode_timeline_transaction_v2,
    refresh_authoring_asset_catalog_v2,
    release_timeline_history_v2,
    timeline_history_projection_v2,
)
from .authoring_generated_source import (
    GENERATED_AUTHORING_SOURCE_SCHEMA,
    CompositeAuthoringSourceBindingReceipt,
    GeneratedAuthoringVideoSource,
    stage_generated_authoring_source,
)
from .authoring_render_source import (
    BoundAuthoringRenderPlan,
    PreparedAuthoringHistory,
    claim_render_source,
    prepare_authoring_history,
    prepare_bound_render_plan,
)
from .authoring_source_binding import (
    AuthoringSourceBindingError,
    AuthoringSourceBindingReceipt,
    authoring_source_preview_capability,
    claim_transferred_authoring_source,
)
from .comfyui_route_seam import (
    RoutePolicy,
    RouteResult,
    offload_route_handler,
    register_owned_route,
)
from .comfyui_sidebar_workspace import (
    SidebarAuthoringSeed,
    SidebarAuthoringWorkspaceClaim,
    claim_sidebar_authoring_workspace,
)
from .composition_root import AUTHORING_WORKSPACE, component

AUTHORING_ACTION_SCHEMA = "h3.context.authoring_workbench.action.v1"
AUTHORING_PROJECTION_SCHEMA = "h3.context.authoring_workbench.projection.v1"
AUTHORING_SNAP_SCHEMA = "h3.context.authoring_workbench.snap.v1"
if TYPE_CHECKING:
    from .authoring_derivative_generator import AuthoringDerivativeGenerator
    from .authoring_derivative_source import AuthoringDerivativeClaim
    from .av_reconstruction_media import QualifiedAVMediaAdapter
    from .comfyui_production_workspace import (
        ProductionAuthoringOutputClaim,
        ProductionWorkspaceRegistry,
    )

AUTHORING_ACTION_ROUTE = "/h3-context/v1/authoring/action"
AUTHORING_AVAILABILITY_PRODUCER = "frontend.host.graphReferenceQualification"
TIMELINE_HISTORY_PROJECTION_SCHEMA = "h3.context.timeline_history_projection.v1"

# IMPORTANT: the outer route repeats action/request metadata around the bounded transaction.
# Removing this fixed framing headroom refuses a valid near-ceiling core envelope at transport.
MAX_AUTHORING_ACTION_BYTES = MAX_TRANSACTION_BYTES + 1_024
MAX_AUTHORING_ACTION_DEPTH = 36
MAX_AUTHORING_ACTION_NODES = 8_192
MAX_AUTHORING_ACTION_ARRAY = 128
MAX_LEGACY_AUTHORING_ACTION_ARRAY = 64
MAX_AUTHORING_WORKSPACES = 16
AUTHORING_WORKSPACE_TTL_SECONDS = 900
MAX_AUTHORING_REQUEST_LEDGER = 256
MAX_AUTHORING_IMPORT_LEDGER = 64
MAX_AUTHORING_TOMBSTONES = 256
AUTHORING_TOMBSTONE_TTL_SECONDS = 900
MAX_AUTHORING_PREVIEW_CLIP_FRAMES = 900

_ROUTE_OWNER_ATTRIBUTE = "__h3_context_authoring_action_v1__"
_ROUTE_REGISTERED = False

_ROOT_KEYS = {"schema", "request_id", "action", "payload"}
_LEGACY_TIMELINE_TRANSACTION_KEYS = {
    "schema",
    "request_id",
    "transaction_id",
    "workspace_handle",
    "expected_workspace_revision",
    "expected_timeline_revision",
    "expected_timeline_fingerprint",
    "commands",
}
_TIMELINE_TRANSACTION_KEYS = {
    "schema",
    "authoring_schema",
    "profile_id",
    "operation_profile_id",
    "request_id",
    "transaction_id",
    "workspace_handle",
    "expected_workspace_revision",
    "expected_timeline_revision",
    "expected_timeline_fingerprint",
    "expected_authoring_fingerprint",
    "commands",
}

_REFERENCE_ACTIONS = {
    "add_source": {"workspace_handle", "expected_reference_revision", "source_id"},
    "remove_source": {"workspace_handle", "expected_reference_revision", "source_id"},
    "reorder_source": {
        "workspace_handle",
        "expected_reference_revision",
        "source_id",
        "new_index",
    },
    "include_soundtrack": {
        "workspace_handle",
        "expected_reference_revision",
        "video_id",
        "audio_id",
    },
    "exclude_soundtrack": {"workspace_handle", "expected_reference_revision", "video_id"},
}
_TIMELINE_ACTIONS = {
    "add_clip": {
        "workspace_handle",
        "expected_timeline_revision",
        "asset_id",
        "lane",
        "start_frame",
        "frames",
        "source_start_frame",
    },
    "remove_clip": {"workspace_handle", "expected_timeline_revision", "clip_id"},
    "move_clip": {
        "workspace_handle",
        "expected_timeline_revision",
        "clip_id",
        "delta_frames",
        "delta_lanes",
    },
    "move_group": {
        "workspace_handle",
        "expected_timeline_revision",
        "clip_ids",
        "delta_frames",
    },
    "trim_clip": {
        "workspace_handle",
        "expected_timeline_revision",
        "clip_id",
        "edge",
        "delta_frames",
    },
    "split_clip": {
        "workspace_handle",
        "expected_timeline_revision",
        "clip_id",
        "at_offset_frames",
    },
    "merge_clips": {
        "workspace_handle",
        "expected_timeline_revision",
        "first_clip_id",
        "second_clip_id",
    },
    "link_clips": {
        "workspace_handle",
        "expected_timeline_revision",
        "video_clip_id",
        "audio_clip_id",
    },
    "unlink_clips": {"workspace_handle", "expected_timeline_revision", "video_clip_id"},
    "set_envelope": {"workspace_handle", "expected_timeline_revision", "clip_id", "points"},
    "select_clips": {"workspace_handle", "expected_timeline_revision", "clip_ids"},
}
_ACTIONS: dict[str, set[str]] = {
    "create_authoring_workspace": {"context_workspace_handle"},
    "ensure_authoring_from_production": {
        "production_workspace_handle",
        "production_workspace_id",
        "preferred_authoring_handle",
    },
    "read_projection": {"workspace_handle"},
    "read_snap": {"workspace_handle", "frame", "playhead_frame", "exclude_clip_id"},
    "read_timeline_history": {"workspace_handle"},
    "initialize_timeline_history": {
        "workspace_handle",
        "expected_reference_revision",
        "expected_timeline_revision",
        "authoring_schema",
        "profile_id",
        "operation_profile_id",
    },
    "apply_timeline_transaction": _TIMELINE_TRANSACTION_KEYS,
    "release_workspace": {"workspace_handle"},
    "set_availability": {
        "workspace_handle",
        "producer",
        "producer_revision",
        "fingerprint",
        "facts",
    },
    **_REFERENCE_ACTIONS,
    **_TIMELINE_ACTIONS,
}
_MUTATIONS = (
    set(_REFERENCE_ACTIONS)
    | set(_TIMELINE_ACTIONS)
    | {"set_availability", "apply_timeline_transaction", "release_workspace"}
)


class AuthoringWorkbenchError(Exception):
    """One typed transport outcome; ``projection`` rides along only on 409 conflicts."""

    def __init__(self, status: int, code: str, projection: dict[str, object] | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.projection = projection


def _expect_dict(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("authoring payload shape drifted after validation")
    return value


def _expect_list(value: object) -> list[object]:
    if type(value) is not list:
        raise ValueError("authoring payload shape drifted after validation")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("authoring action repeats an object key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("authoring action contains a non-finite constant")


def _shape(value: object, *, depth: int = 0) -> int:
    if depth > MAX_AUTHORING_ACTION_DEPTH:
        raise ValueError("authoring action exceeds the depth bound")
    if type(value) is dict:
        return 1 + sum(_shape(item, depth=depth + 1) for item in value.values())
    if type(value) is list:
        if len(value) > MAX_AUTHORING_ACTION_ARRAY:
            raise ValueError("authoring action exceeds the array bound")
        return 1 + sum(_shape(item, depth=depth + 1) for item in value)
    if type(value) in (str, int, float, bool) or value is None:
        return 1
    raise ValueError("authoring action holds an unsupported value type")


# The same character class the core domains and the frontend codec enforce; checking it
# here keeps every layer's identifier rule identical instead of merely compatible.
_WIRE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _bounded_identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _WIRE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _bounded_int(value: object, field_name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} is invalid")
    return value


def _validate_action(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _ROOT_KEYS:
        raise ValueError("authoring action must be one closed object")
    if value["schema"] != AUTHORING_ACTION_SCHEMA:
        raise ValueError("authoring action schema is unsupported")
    _bounded_identifier(value["request_id"], "request_id")
    action = value["action"]
    if type(action) is not str or action not in _ACTIONS:
        raise ValueError("authoring action is unsupported")
    payload = value["payload"]
    if type(payload) is not dict:
        raise ValueError("authoring action payload is not closed")
    if action == "apply_timeline_transaction":
        transaction_schema = payload.get("schema")
        if transaction_schema == TIMELINE_TRANSACTION_SCHEMA:
            if set(payload) != _LEGACY_TIMELINE_TRANSACTION_KEYS:
                raise ValueError("legacy timeline transaction payload is not closed")
        elif transaction_schema == TIMELINE_TRANSACTION_SCHEMA_V2:
            if set(payload) != _TIMELINE_TRANSACTION_KEYS:
                raise ValueError("versioned timeline transaction payload is not closed")
            if (
                payload["authoring_schema"] != NLE_AUTHORING_SCHEMA
                or payload["profile_id"] != NLE_AUTHORING_PROFILE_ID
                or payload["operation_profile_id"] != NLE_OPERATION_PROFILE_ID
            ):
                raise ValueError("authoring profile negotiation is unsupported")
        else:
            raise ValueError("timeline transaction schema is unsupported")
        if payload["request_id"] != value["request_id"]:
            raise ValueError("timeline transaction request_id differs from the outer action")
        _bounded_identifier(payload["transaction_id"], "transaction_id")
        _bounded_int(
            payload["expected_workspace_revision"],
            "expected_workspace_revision",
            0,
            1_000_000,
        )
        _bounded_int(
            payload["expected_timeline_revision"],
            "expected_timeline_revision",
            0,
            1_000_000,
        )
        timeline_fingerprint = payload["expected_timeline_fingerprint"]
        if (
            type(timeline_fingerprint) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", timeline_fingerprint) is None
        ):
            raise ValueError("expected_timeline_fingerprint is invalid")
        if transaction_schema == TIMELINE_TRANSACTION_SCHEMA_V2:
            authoring_fingerprint = payload["expected_authoring_fingerprint"]
            if (
                type(authoring_fingerprint) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", authoring_fingerprint) is None
            ):
                raise ValueError("expected_authoring_fingerprint is invalid")
        commands = payload["commands"]
        if type(commands) is not list or not 1 <= len(commands) <= MAX_TRANSACTION_COMMANDS:
            raise ValueError("timeline transaction commands are invalid")
    elif set(payload) != _ACTIONS[action]:
        raise ValueError("authoring action payload is not closed")
    if action == "initialize_timeline_history" and (
        payload["authoring_schema"] != NLE_AUTHORING_SCHEMA
        or payload["profile_id"] != NLE_AUTHORING_PROFILE_ID
        or payload["operation_profile_id"] != NLE_OPERATION_PROFILE_ID
    ):
        raise ValueError("authoring profile negotiation is unsupported")
    for key in ("workspace_handle", "context_workspace_handle"):
        if key in payload:
            _bounded_identifier(payload[key], key)
    for key in ("production_workspace_handle", "production_workspace_id"):
        if key in payload:
            _bounded_identifier(payload[key], key)
    if (
        "preferred_authoring_handle" in payload
        and payload["preferred_authoring_handle"] is not None
    ):
        _bounded_identifier(payload["preferred_authoring_handle"], "preferred_authoring_handle")
    for key in ("expected_reference_revision", "expected_timeline_revision", "producer_revision"):
        if key in payload:
            minimum = 0 if action == "apply_timeline_transaction" else 1
            _bounded_int(payload[key], key, minimum, 1_000_000)
    for key in (
        "source_id",
        "video_id",
        "audio_id",
        "clip_id",
        "asset_id",
        "first_clip_id",
        "second_clip_id",
        "video_clip_id",
        "audio_clip_id",
    ):
        if key in payload:
            _bounded_identifier(payload[key], key)
    for key in ("new_index", "lane", "start_frame", "frames", "source_start_frame"):
        if key in payload:
            _bounded_int(payload[key], key, 0 if key != "frames" else 1, 1_000_000)
    for key in ("delta_frames", "delta_lanes", "at_offset_frames"):
        if key in payload:
            _bounded_int(payload[key], key, -1_000_000, 1_000_000)
    if "frame" in payload:
        _bounded_int(payload["frame"], "frame", 0, 1_000_000)
    if "playhead_frame" in payload and payload["playhead_frame"] is not None:
        _bounded_int(payload["playhead_frame"], "playhead_frame", 0, 1_000_000)
    if "exclude_clip_id" in payload and payload["exclude_clip_id"] is not None:
        _bounded_identifier(payload["exclude_clip_id"], "exclude_clip_id")
    if "edge" in payload and payload["edge"] not in ("start", "end"):
        raise ValueError("edge is invalid")
    if "clip_ids" in payload:
        clip_ids = payload["clip_ids"]
        if type(clip_ids) is not list or len(clip_ids) > MAX_LEGACY_AUTHORING_ACTION_ARRAY:
            raise ValueError("clip_ids is invalid")
        for index, item in enumerate(clip_ids):
            _bounded_identifier(item, f"clip_ids[{index}]")
    if "points" in payload:
        points = payload["points"]
        if type(points) is not list or len(points) > MAX_LEGACY_AUTHORING_ACTION_ARRAY:
            raise ValueError("points is invalid")
        for index, item in enumerate(points):
            if type(item) is not dict or set(item) != {"offset_frames", "strength_per_mille"}:
                raise ValueError(f"points[{index}] is not closed")
            _bounded_int(item["offset_frames"], f"points[{index}].offset_frames", 0, 1_000_000)
            _bounded_int(
                item["strength_per_mille"], f"points[{index}].strength_per_mille", 0, 1_000
            )
    if "producer" in payload and payload["producer"] != AUTHORING_AVAILABILITY_PRODUCER:
        raise ValueError("producer is not the qualified availability producer")
    if "fingerprint" in payload:
        fingerprint = payload["fingerprint"]
        if type(fingerprint) is not str or not fingerprint.startswith("sha256:"):
            raise ValueError("fingerprint is invalid")
    if "facts" in payload:
        facts = payload["facts"]
        if type(facts) is not list or len(facts) > MAX_LEGACY_AUTHORING_ACTION_ARRAY:
            raise ValueError("facts is invalid")
        for index, item in enumerate(facts):
            if type(item) is not dict or set(item) != {"video_id", "availability"}:
                raise ValueError(f"facts[{index}] is not closed")
            _bounded_identifier(item["video_id"], f"facts[{index}].video_id")
            if item["availability"] not in ("available", "unavailable", "unknown"):
                raise ValueError(f"facts[{index}].availability is invalid")
    return value


def decode_authoring_action_json(data: bytes) -> dict[str, object]:
    """Decode the only action wire with strict bytes, UTF-8, JSON, depth and node bounds."""

    if type(data) is not bytes or not data or len(data) > MAX_AUTHORING_ACTION_BYTES:
        raise ValueError("authoring action bytes are invalid")
    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("authoring action JSON is invalid") from exc
    if _shape(value) > MAX_AUTHORING_ACTION_NODES:
        raise ValueError("authoring action exceeds the node bound")
    return _validate_action(value)


@dataclass(slots=True)
class _AuthoringEntry:
    seed: SidebarAuthoringSeed
    universe: OrderedDict[str, AdmittedSourceInput]
    not_admissible: dict[str, str]
    reference: ReferenceSetState
    timeline: TimelineState
    clip_counter: int
    touched_at: float
    source_binding: AuthoringSourceBindingReceipt | None
    timeline_history: TimelineHistoryState | None
    timeline_history_v2: TimelineHistoryStateV2 | None = None
    initialized_sources: PreparedAuthoringHistory | None = None
    initialization_fingerprint: str | None = None
    initializing: object | None = None
    lineage_token: object | None = field(default=None, repr=False)
    imported_outputs: dict[tuple[str, str], _ImportedOutput] = field(
        default_factory=dict, repr=False
    )
    importing: object | None = field(default=None, repr=False)
    production_owner: tuple[str, str] | None = field(default=None, repr=False)
    project_created: bool = field(default=False, repr=False)


@dataclass(frozen=True, slots=True)
class _Tombstone:
    reason: str
    terminated_at: float
    recoverable_empty: bool = False


@dataclass(frozen=True, slots=True)
class _LedgerEntry:
    workspace_handle: str
    status: int
    body: dict[str, object] | None
    # A replay is only a replay when it is the same logical request; the entry binds the
    # cached outcome to the action+payload that produced it so a reused id with different
    # content is refused instead of silently answered with the old result.
    request_fingerprint: str


@dataclass(frozen=True, slots=True, repr=False)
class _ImportedOutput:
    asset_id: str
    segment_id: str
    claim: ProductionAuthoringOutputClaim = field(repr=False, compare=False)
    source: GeneratedAuthoringVideoSource = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True, repr=False)
class _ImportLedgerEntry:
    workspace_handle: str
    request_digest: str
    response: ProductionAuthoringImportResponse | ProductionAuthoringImportResponseV2
    output_keys: tuple[tuple[str, str], ...]


def _request_fingerprint(name: str, payload: dict[str, object]) -> str:
    if name == "apply_timeline_transaction":
        try:
            transaction = decode_timeline_transaction_v2(payload)
        except ContractValidationError:
            try:
                legacy = decode_timeline_transaction(payload)
            except ContractValidationError:
                pass
            else:
                return f"timeline:{legacy.fingerprint}"
        else:
            # CRITICAL: the outer ledger and pure history owner must identify the same canonical
            # transaction. Hashing raw list order or pre-NFC text here would reject a core-safe
            # replay before the idempotency authority can return its original receipt.
            return f"timeline:{transaction.fingerprint}"
    return hashlib.sha256(
        json.dumps(
            {"action": name, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthoringDispatchResult:
    status: int
    body: dict[str, object] | None


@dataclass(frozen=True, slots=True, repr=False)
class AuthoringMediaPreviewClaim:
    """Opaque currentness claim; only content-free clip geometry is public."""

    request: AuthoringPreviewRequest
    source_id: str
    source_start_frame: int
    frames: int
    video_fps: int
    _entry: _AuthoringEntry
    _clip: object
    _source_binding: AuthoringSourceBindingReceipt
    _source: object

    def __repr__(self) -> str:
        return "<AuthoringMediaPreviewClaim opaque>"

    def render(
        self,
        adapter: object,
        *,
        deadline: float,
        cancellation: object | None = None,
    ) -> tuple[bytearray, str]:
        from .authoring_source_binding import execute_transferred_authoring_preview

        return execute_transferred_authoring_preview(
            self._source,
            adapter,
            source_start_frame=self.source_start_frame,
            frames=self.frames,
            source_fps=self.video_fps,
            deadline=deadline,
            cancellation=cancellation,
        )


def _seed_reference_state(
    seed: SidebarAuthoringSeed,
    *,
    source_binding_available: bool = True,
) -> tuple[OrderedDict[str, AdmittedSourceInput], dict[str, str], ReferenceSetState]:
    capacity = build_h3_base_capacity(
        authority="core.registry",
        fingerprint=seed.registry_fingerprint,
        timed=TimedReferenceLimits(
            max_duration_milliseconds=length.MAX_ACCEPTED_MILLISECONDS,
            max_frames=length.MAX_FRAME_COUNT,
        ),
    )
    state = create_reference_set(capacity)
    universe: OrderedDict[str, AdmittedSourceInput] = OrderedDict()
    not_admissible: dict[str, str] = {}
    pairings: list[tuple[str, str]] = []
    for row in seed.sources:
        source = AdmittedSourceInput(
            source_id=row.asset_id,
            kind=row.kind,
            fingerprint=row.identity_fingerprint,
            duration_milliseconds=row.duration_milliseconds,
        )
        universe[row.asset_id] = source
        if not source_binding_available:
            # IMPORTANT: a project retains the original source identities, not the Context's
            # one-owner media receipt. Admit no original media without that exact binding.
            not_admissible[row.asset_id] = "source_not_bound"
            continue
        try:
            state, _receipt = apply_reference_command(state, AddSource(state.revision, source))
        except ContractValidationError as exc:
            not_admissible[row.asset_id] = str(exc).split(":", 1)[0]
            continue
        if row.kind is MediaKind.AUDIO and row.paired_video_id is not None:
            pairings.append((row.paired_video_id, row.asset_id))
    for video_id, audio_id in pairings:
        if video_id in not_admissible or audio_id in not_admissible:
            continue
        try:
            state, _receipt = apply_reference_command(
                state, IncludeSoundtrack(state.revision, video_id, audio_id)
            )
        except ContractValidationError as exc:
            not_admissible[audio_id] = str(exc).split(":", 1)[0]
    return universe, not_admissible, state


class AuthoringWorkspaceRegistry:
    """Finite process-local holder for authoring state; never persisted or logged."""

    def __init__(
        self,
        *,
        seed_claim: Callable[[str], SidebarAuthoringSeed] | None = None,
        workspace_claim: Callable[
            [str], SidebarAuthoringWorkspaceClaim
        ] = claim_sidebar_authoring_workspace,
        max_entries: int = MAX_AUTHORING_WORKSPACES,
        ttl_seconds: int = AUTHORING_WORKSPACE_TTL_SECONDS,
        max_ledger_entries: int = MAX_AUTHORING_REQUEST_LEDGER,
        max_tombstones: int = MAX_AUTHORING_TOMBSTONES,
        terminal_ttl_seconds: int = AUTHORING_TOMBSTONE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        history_preparer: Callable[
            [dict[str, object], AuthoringSourceBindingReceipt | None], PreparedAuthoringHistory
        ] = prepare_authoring_history,
    ) -> None:
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_AUTHORING_WORKSPACES:
            raise ValueError("authoring entry limit is invalid")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86_400:
            raise ValueError("authoring TTL is invalid")
        self._seed_claim = seed_claim
        self._workspace_claim = workspace_claim
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._max_ledger_entries = max_ledger_entries
        self._max_tombstones = max_tombstones
        self._terminal_ttl_seconds = terminal_ttl_seconds
        self._clock = clock
        self._history_preparer = history_preparer
        self._entries: OrderedDict[str, _AuthoringEntry] = OrderedDict()
        self._ledger: OrderedDict[str, _LedgerEntry] = OrderedDict()
        self._import_ledger: OrderedDict[str, _ImportLedgerEntry] = OrderedDict()
        self._tombstones: OrderedDict[str, _Tombstone] = OrderedDict()
        self._lock = threading.RLock()

    def _record_tombstone(self, handle: str, tombstone: _Tombstone) -> None:
        self._tombstones[handle] = tombstone
        self._tombstones.move_to_end(handle)
        while len(self._tombstones) > self._max_tombstones:
            self._tombstones.popitem(last=False)

    def _prune(self, now: float) -> None:
        expired = [
            handle
            for handle, entry in self._entries.items()
            if now - entry.touched_at >= self._ttl_seconds
        ]
        for handle in expired:
            entry = self._entries.pop(handle, None)
            recoverable_empty = False
            if entry is not None and entry.project_created:
                _universe, _unavailable, initial_reference = _seed_reference_state(
                    entry.seed, source_binding_available=False
                )
                initial_timeline = create_timeline(
                    build_h3_timeline_profile(build_temporal_profile()),
                    build_h3_timeline_limits(),
                    reference_view(initial_reference),
                )
                history = entry.timeline_history_v2 or entry.timeline_history
                history_clips = (
                    history.authoring.clips
                    if isinstance(history, TimelineHistoryStateV2)
                    else history.snapshot.clips
                    if history is not None
                    else ()
                )
                recoverable_empty = (
                    entry.reference == initial_reference
                    and entry.timeline == initial_timeline
                    and entry.clip_counter == 0
                    and not entry.imported_outputs
                    and entry.importing is None
                    and entry.initializing is None
                    and (
                        history is None
                        or (
                            not history.undo_entries
                            and not history.redo_entries
                            and not history_clips
                        )
                    )
                )
            if entry is not None and entry.source_binding is not None:
                entry.source_binding.release()
            if entry is not None and entry.timeline_history is not None:
                entry.timeline_history = release_timeline_history(entry.timeline_history)
            if entry is not None and entry.timeline_history_v2 is not None:
                entry.timeline_history_v2 = release_timeline_history_v2(entry.timeline_history_v2)
            if entry is not None and entry.initialized_sources is not None:
                entry.initialized_sources.discard()
            self._record_tombstone(handle, _Tombstone("expired", now, recoverable_empty))
        dead = [
            handle
            for handle, item in self._tombstones.items()
            if now - item.terminated_at >= self._terminal_ttl_seconds
        ]
        for handle in dead:
            self._tombstones.pop(handle, None)
        retained = set(self._entries) | set(self._tombstones)
        for request_id in tuple(self._ledger):
            if self._ledger[request_id].workspace_handle not in retained:
                self._ledger.pop(request_id, None)
        for request_id in tuple(self._import_ledger):
            if self._import_ledger[request_id].workspace_handle not in retained:
                self._import_ledger.pop(request_id, None)

    def _entry(self, handle: str) -> _AuthoringEntry:
        entry = self._entries.get(handle)
        if entry is None:
            if handle in self._tombstones:
                raise AuthoringWorkbenchError(410, "workspace_gone")
            raise AuthoringWorkbenchError(404, "workspace_unavailable")
        return entry

    def ensure_from_production(
        self,
        production_registry: ProductionWorkspaceRegistry,
        *,
        workspace_handle: str,
        workspace_id: str,
        preferred_authoring_handle: str | None,
    ) -> AuthoringDispatchResult:
        """Reuse or create one target under the live Production owner's original seed."""

        from .comfyui_production_workspace import (
            ProductionWorkbenchError,
            ProductionWorkspaceRegistry,
        )

        if type(production_registry) is not ProductionWorkspaceRegistry:
            raise AuthoringWorkbenchError(400, "request_invalid")
        owner = (workspace_handle, workspace_id)
        now = self._clock()
        try:
            # CRITICAL: Authoring then Production is the import lock order. Reversing it can
            # deadlock generated-source currentness checks; releasing Production before binding
            # permits a released project to acquire a newly created editor.
            with self._lock:
                self._prune(now)
                with production_registry.authoring_anchor_for_project(*owner) as anchor:
                    if anchor.target_handle is not None:
                        target = self._entries.get(anchor.target_handle)
                        if target is None:
                            tombstone = self._tombstones.get(anchor.target_handle)
                            if tombstone is None or not tombstone.recoverable_empty:
                                raise AuthoringWorkbenchError(410, "associated_editor_gone")
                            # IMPORTANT: only an expired, provably empty project-created editor
                            # may be replaced. A lost edited target must never become a blank one.
                            anchor.target_handle = None
                        else:
                            if target.production_owner != owner:
                                raise AuthoringWorkbenchError(409, "associated_editor_mismatch")
                            target.touched_at = now
                            self._entries.move_to_end(anchor.target_handle)
                            return AuthoringDispatchResult(
                                200, self._projection(anchor.target_handle, target)
                            )
                    seed = anchor.seed
                    if preferred_authoring_handle is not None:
                        preferred = self._entries.get(preferred_authoring_handle)
                        if (
                            preferred is not None
                            and preferred.production_owner is None
                            and preferred.lineage_token is seed.lineage_token
                            and preferred.seed.source_id == seed.source_id
                            and preferred.seed.registry_fingerprint == seed.registry_fingerprint
                            and all(
                                output_key[0] == workspace_handle
                                for output_key in preferred.imported_outputs
                            )
                        ):
                            preferred.production_owner = owner
                            preferred.touched_at = now
                            anchor.target_handle = preferred_authoring_handle
                            self._entries.move_to_end(preferred_authoring_handle)
                            return AuthoringDispatchResult(
                                200, self._projection(preferred_authoring_handle, preferred)
                            )
                    if len(self._entries) >= self._max_entries:
                        raise AuthoringWorkbenchError(429, "workspace_capacity")
                    universe, not_admissible, reference = _seed_reference_state(
                        seed, source_binding_available=False
                    )
                    timeline = create_timeline(
                        build_h3_timeline_profile(build_temporal_profile()),
                        build_h3_timeline_limits(),
                        reference_view(reference),
                    )
                    handle = "authoring-" + secrets.token_hex(16)
                    target = _AuthoringEntry(
                        seed=seed,
                        universe=universe,
                        not_admissible=not_admissible,
                        reference=reference,
                        timeline=timeline,
                        clip_counter=0,
                        touched_at=now,
                        source_binding=None,
                        timeline_history=None,
                        lineage_token=seed.lineage_token,
                        production_owner=owner,
                        project_created=True,
                    )
                    projection = self._projection(handle, target)
                    self._entries[handle] = target
                    anchor.target_handle = handle
                    return AuthoringDispatchResult(201, projection)
        except ProductionWorkbenchError as exc:
            raise AuthoringWorkbenchError(exc.status, exc.code) from None

    @staticmethod
    def _history_projection(
        handle: str,
        history: TimelineHistoryState | TimelineHistoryStateV2,
        rejection: str | None = None,
    ) -> dict[str, object]:
        if isinstance(history, TimelineHistoryStateV2):
            return timeline_history_projection_v2(handle, history, rejection)
        undo_cursor = history.undo_entries[-1].cursor if history.undo_entries else None
        redo_cursor = history.redo_entries[-1].cursor if history.redo_entries else None
        return {
            "schema": TIMELINE_HISTORY_PROJECTION_SCHEMA,
            "workspace_handle": handle,
            "snapshot": history.snapshot.to_wire(),
            "selection": list(history.selection),
            # CRITICAL: expose only the backend-owned branch heads. A browser-retained receipt may
            # be stale after reload, undo, or quota eviction and must never reconstruct a cursor.
            "undo_cursor": undo_cursor,
            "redo_cursor": redo_cursor,
            "rejection": None if rejection is None else {"code": rejection},
        }

    @staticmethod
    def _history(entry: _AuthoringEntry) -> TimelineHistoryState | TimelineHistoryStateV2:
        history = entry.timeline_history_v2 or entry.timeline_history
        if history is None:
            raise AuthoringWorkbenchError(422, "timeline_history_unavailable")
        return history

    @staticmethod
    def _owns_history(
        entry: _AuthoringEntry,
        history: TimelineHistoryState | TimelineHistoryStateV2,
    ) -> bool:
        if isinstance(history, TimelineHistoryStateV2):
            return entry.timeline_history_v2 is history
        return entry.timeline_history is history

    @staticmethod
    def _render_snapshot(
        history: TimelineHistoryState | TimelineHistoryStateV2,
    ) -> PublicCompositionSnapshot | None:
        if isinstance(history, TimelineHistoryStateV2):
            return materialize_render_snapshot(history.authoring)
        return history.snapshot

    def bind_timeline_history_snapshot(self, snapshot: PublicCompositionSnapshot) -> None:
        """Bind one backend-accepted M25 snapshot to its existing authoring workspace."""

        if not isinstance(snapshot, PublicCompositionSnapshot):
            raise AuthoringWorkbenchError(422, "timeline_snapshot_invalid")
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entry(snapshot.workspace_handle)
            current = entry.timeline_history_v2 or entry.timeline_history
            # CRITICAL: browser input must never initialize or replace this state. Only an exact
            # backend-accepted snapshot may bind once; replacement here would bypass M25-11 CAS and
            # let a later integration silently erase history.
            if current is not None:
                current_snapshot = self._render_snapshot(current)
                if current_snapshot == snapshot and not current.released:
                    return
                raise AuthoringWorkbenchError(409, "timeline_history_already_bound")
            entry.timeline_history = TimelineHistoryState.initialize(snapshot)
            entry.touched_at = now
            self._entries.move_to_end(snapshot.workspace_handle)

    def _projection(
        self, handle: str, entry: _AuthoringEntry, rejection: str | None = None
    ) -> dict[str, object]:
        reference = entry.reference
        projected = canonical_projection(reference)
        capacity = capacity_projection(reference)
        labels: dict[str, dict[str, object]] = {
            str(asset.to_wire()["source_id"]): asset.to_wire() for asset in projected.assets
        }
        sources: list[dict[str, object]] = []
        for source_id, source in entry.universe.items():
            admitted = reference.entry(source_id) is not None
            wire_label = labels.get(source_id)
            derived: str | None = None
            if admitted and source.kind is MediaKind.VIDEO:
                derived = derived_soundtrack_state(reference, source_id).value
            sources.append(
                {
                    "source_id": source_id,
                    "kind": source.kind.value,
                    "admitted": admitted,
                    "admissible": source_id not in entry.not_admissible,
                    "reason": entry.not_admissible.get(source_id),
                    "duration_milliseconds": source.duration_milliseconds,
                    "label": None if wire_label is None else wire_label["label"],
                    "paired_with": None if wire_label is None else wire_label["paired_with"],
                    "derived_soundtrack": derived,
                    "preview": authoring_source_preview_capability(
                        entry.source_binding, source_id
                    ).to_wire(),
                }
            )
        view = reference_view(reference)
        return {
            "schema": AUTHORING_PROJECTION_SCHEMA,
            "workspace_handle": handle,
            "context_source_id": entry.seed.source_id,
            "task_mode": entry.seed.task_mode.value,
            "registry_fingerprint": entry.seed.registry_fingerprint,
            "reference": {
                "revision": reference.revision,
                "sources": sources,
                "canonical": [asset.to_wire() for asset in projected.assets],
                "soundtracks": [row.to_wire() for row in projected.soundtracks],
                "queue_blockers": [row.to_wire() for row in projected.queue_blockers],
                "capacity": capacity.to_wire(),
            },
            "availability": {
                "producer": reference.availability_producer,
                "revision": reference.availability_revision,
            },
            "timeline": {
                "revision": entry.timeline.revision,
                "content_fingerprint": entry.timeline.content_fingerprint(),
                "profile": {
                    "video_fps": entry.timeline.profile.video_fps,
                    "frame_grid": entry.timeline.profile.frame_grid,
                    "audio_period_frames": entry.timeline.profile.audio_period_frames,
                    "max_extent_frames": entry.timeline.profile.max_extent_frames,
                },
                "clips": [clip.to_wire() for clip in entry.timeline.clips],
                "links": [link.to_wire() for link in entry.timeline.links],
                "selection": list(entry.timeline.selection),
                "blockers": [row.to_wire() for row in timeline_blockers(entry.timeline, view)],
            },
            "rejection": None if rejection is None else {"code": rejection},
        }

    @staticmethod
    def _require_import_deadline(deadline: float, cancelled: Callable[[], bool]) -> None:
        if cancelled():
            raise AuthoringWorkbenchError(408, "cancelled")
        if type(deadline) is not float or deadline <= time.monotonic():
            raise AuthoringWorkbenchError(408, "timed_out")

    @staticmethod
    def _prepared_with_catalog(
        previous: PreparedAuthoringHistory,
        binding: AuthoringSourceBindingReceipt,
        history: TimelineHistoryState | TimelineHistoryStateV2,
        *,
        source_reference_revision: int,
        source_timeline_revision: int,
    ) -> PreparedAuthoringHistory:
        if isinstance(history, TimelineHistoryStateV2):
            authoring_state = history.authoring
            snapshot = materialize_render_snapshot(authoring_state)
            catalog = authoring_state.assets
        else:
            snapshot = history.snapshot
            authoring_state = None
            catalog = snapshot.assets
        claims = tuple(
            claim_render_source(binding, asset.asset_id)
            for asset in catalog
            if asset.kind != "font"
        )
        if {claim.asset.asset_id: claim.asset for claim in claims} != {
            asset.asset_id: asset for asset in catalog if asset.kind != "font"
        }:
            raise AuthoringSourceBindingError("source_facts_mismatch")
        prepared = PreparedAuthoringHistory(
            snapshot,
            binding.generation,
            claims,
            previous.fonts,
            authoring_state,
            source_reference_revision,
            source_timeline_revision,
        )
        if not prepared.current():
            prepared.discard()
            raise AuthoringSourceBindingError("source_stale")
        return prepared

    def import_production_outputs(
        self,
        request: ProductionAuthoringImportRequest | ProductionAuthoringImportRequestV2,
        *,
        production_registry: ProductionWorkspaceRegistry,
        media_adapter: QualifiedAVMediaAdapter,
        deadline: float,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> ProductionAuthoringImportResponse | ProductionAuthoringImportResponseV2:
        """Atomically add current same-lineage raw Production artifacts to Authoring."""

        from .av_reconstruction_media import QualifiedAVMediaAdapter
        from .comfyui_production_workspace import (
            ProductionWorkbenchError,
            ProductionWorkspaceRegistry,
        )

        if (
            type(request)
            not in {ProductionAuthoringImportRequest, ProductionAuthoringImportRequestV2}
            or type(production_registry) is not ProductionWorkspaceRegistry
            or type(media_adapter) is not QualifiedAVMediaAdapter
            or not callable(cancelled)
        ):
            raise AuthoringWorkbenchError(400, "request_invalid")
        self._require_import_deadline(deadline, cancelled)
        token = object()
        entry: _AuthoringEntry | None = None
        captured_reference: ReferenceSetState | None = None
        captured_timeline: TimelineState | None = None
        captured_history: TimelineHistoryState | TimelineHistoryStateV2 | None = None
        captured_prepared: PreparedAuthoringHistory | None = None
        captured_binding: AuthoringSourceBindingReceipt | None = None
        captured_universe: OrderedDict[str, AdmittedSourceInput] | None = None
        captured_not_admissible: dict[str, str] | None = None
        claims: tuple[ProductionAuthoringOutputClaim, ...] = ()
        existing_by_output: dict[str, _ImportedOutput] = {}
        try:
            with self._lock:
                self._prune(self._clock())
                entry = self._entry(request.authoring_workspace_handle)
                replay = self._import_ledger.get(request.request_id)
                if replay is not None:
                    if (
                        replay.request_digest != request.digest
                        or replay.workspace_handle != request.authoring_workspace_handle
                    ):
                        raise AuthoringWorkbenchError(409, "request_replay_mismatch")
                    for key in replay.output_keys:
                        imported = entry.imported_outputs.get(key)
                        if (
                            imported is None
                            or not imported.source.owner_retained()
                            or not imported.claim.current()
                        ):
                            raise AuthoringWorkbenchError(409, "source_stale")
                    self._import_ledger.move_to_end(request.request_id)
                    return replay.response
                history = self._history(entry)
                if isinstance(request, ProductionAuthoringImportRequestV2) != isinstance(
                    history, TimelineHistoryStateV2
                ):
                    raise AuthoringWorkbenchError(409, "unsupported_profile")
                prepared = entry.initialized_sources
                if prepared is None:
                    raise AuthoringWorkbenchError(422, "render_sources_unavailable")
                if entry.initializing is not None or entry.importing is not None:
                    raise AuthoringWorkbenchError(423, "workspace_busy")
                timeline_fingerprint = entry.timeline.content_fingerprint()
                if isinstance(history, TimelineHistoryStateV2):
                    nle_authoring = history.authoring
                    snapshot = None
                else:
                    nle_authoring = None
                    snapshot = self._render_snapshot(history)
                identity_is_stale = (
                    entry.seed.registry_fingerprint
                    != request.expected_authoring_registry_fingerprint
                    or entry.reference.revision != request.expected_authoring_reference_revision
                    or entry.timeline.revision != request.expected_authoring_timeline_revision
                    or timeline_fingerprint
                    != request.expected_authoring_timeline_content_fingerprint
                )
                if isinstance(request, ProductionAuthoringImportRequestV2):
                    identity_is_stale = identity_is_stale or (
                        request.authoring_schema != NLE_AUTHORING_SCHEMA
                        or request.profile_id != NLE_AUTHORING_PROFILE_ID
                        or nle_authoring is None
                        or nle_authoring.workspace_revision
                        != request.expected_nle_workspace_revision
                        or nle_authoring.timeline_revision != request.expected_nle_timeline_revision
                        or nle_authoring.timeline_fingerprint
                        != request.expected_nle_timeline_fingerprint
                        or nle_authoring.authoring_fingerprint
                        != request.expected_nle_authoring_fingerprint
                    )
                else:
                    identity_is_stale = identity_is_stale or (
                        snapshot is None
                        or snapshot.workspace_revision != request.expected_nle_workspace_revision
                        or snapshot.timeline_revision != request.expected_nle_timeline_revision
                        or snapshot.timeline_fingerprint
                        != request.expected_nle_timeline_fingerprint
                        or snapshot.public_fingerprint != request.expected_nle_public_fingerprint
                    )
                if identity_is_stale:
                    raise AuthoringWorkbenchError(409, "authoring_stale")
                if entry.lineage_token is None:
                    raise AuthoringWorkbenchError(409, "lineage_mismatch")
                if entry.production_owner is not None and entry.production_owner != (
                    request.production_workspace_handle,
                    request.production_workspace_id,
                ):
                    raise AuthoringWorkbenchError(409, "production_owner_mismatch")
                # IMPORTANT: every dual-registry section takes Authoring first and Production
                # second. Reversing this order deadlocks generated-source currentness callbacks.
                claims = production_registry.claim_authoring_output_batch(
                    workspace_handle=request.production_workspace_handle,
                    workspace_id=request.production_workspace_id,
                    expected_workspace_revision=request.expected_production_workspace_revision,
                    expected_workspace_fingerprint=(
                        request.expected_production_workspace_fingerprint
                    ),
                    pairs=tuple((row.segment_id, row.output_handle) for row in request.entries),
                )
                if any(claim.lineage_token is not entry.lineage_token for claim in claims):
                    raise AuthoringWorkbenchError(409, "lineage_mismatch")
                for claim in claims:
                    imported = entry.imported_outputs.get(
                        (claim.workspace_handle, claim.output_handle)
                    )
                    if imported is None:
                        continue
                    if (
                        imported.segment_id != claim.segment_id
                        or imported.claim.receipt is not claim.receipt
                        or imported.claim.store is not claim.store
                        or not imported.claim.current()
                        or not imported.source.owner_retained()
                    ):
                        raise AuthoringWorkbenchError(409, "source_stale")
                    existing_by_output[claim.output_handle] = imported
                entry.importing = token
                captured_reference = entry.reference
                captured_timeline = entry.timeline
                captured_history = history
                captured_prepared = prepared
                captured_binding = entry.source_binding
                captured_universe = OrderedDict(entry.universe)
                captured_not_admissible = dict(entry.not_admissible)
        except ProductionWorkbenchError as exc:
            code = {
                "workspace_unavailable": "production_gone",
                "workspace_gone": "production_gone",
            }.get(exc.code, exc.code)
            raise AuthoringWorkbenchError(exc.status, code) from None
        except AuthoringWorkbenchError as exc:
            code = {
                "workspace_unavailable": "authoring_gone",
                "workspace_gone": "authoring_gone",
            }.get(exc.code, exc.code)
            if code == exc.code:
                raise
            raise AuthoringWorkbenchError(exc.status, code) from None

        if (
            entry is None
            or captured_reference is None
            or captured_timeline is None
            or captured_history is None
            or captured_prepared is None
            or captured_universe is None
            or captured_not_admissible is None
        ):  # pragma: no cover - assigned atomically above
            raise AuthoringWorkbenchError(500, "internal_invariant")

        staged_sources: dict[str, GeneratedAuthoringVideoSource] = {}
        assigned: dict[str, tuple[str, GeneratedAuthoringVideoSource]] = {}
        composite: CompositeAuthoringSourceBindingReceipt | None = None
        candidate_prepared: PreparedAuthoringHistory | None = None
        committed = False
        try:
            reserved_ids = set(captured_universe)
            for claim in claims:
                existing = existing_by_output.get(claim.output_handle)
                if existing is not None:
                    assigned[claim.output_handle] = (existing.asset_id, existing.source)
                    continue
                self._require_import_deadline(deadline, cancelled)
                for _ in range(8):
                    asset_id = "generated." + secrets.token_hex(16)
                    if asset_id not in reserved_ids:
                        break
                else:
                    raise AuthoringWorkbenchError(429, "capacity")
                source = stage_generated_authoring_source(
                    receipt=claim.receipt,
                    store=claim.store,
                    media_adapter=media_adapter,
                    scratch_root=media_adapter._scratch_root,
                    production_is_current=claim.current,
                    deadline=deadline,
                    cancelled=cancelled,
                )
                reserved_ids.add(asset_id)
                staged_sources[asset_id] = source
                assigned[claim.output_handle] = (asset_id, source)

            new_claims = tuple(
                claim for claim in claims if claim.output_handle not in existing_by_output
            )
            candidate_reference = captured_reference
            candidate_history = captured_history
            candidate_binding: AuthoringSourceBindingReceipt | None = captured_binding
            candidate_universe = OrderedDict(captured_universe)
            candidate_imported = dict(entry.imported_outputs)
            if new_claims:
                composite = CompositeAuthoringSourceBindingReceipt(captured_binding, staged_sources)
                candidate_binding = composite
                batch: list[AdmittedSourceInput] = []
                additions: list[GeneratedAuthoringVideoSource] = []
                asset_ids: list[str] = []
                for claim in new_claims:
                    asset_id, source = assigned[claim.output_handle]
                    batch.append(
                        AdmittedSourceInput(
                            source_id=asset_id,
                            kind=MediaKind.VIDEO,
                            fingerprint=canonical_fingerprint(
                                {
                                    "schema": GENERATED_AUTHORING_SOURCE_SCHEMA,
                                    "production_workspace_id": claim.workspace_id,
                                    "segment_id": claim.segment_id,
                                    "output_handle": claim.output_handle,
                                }
                            ),
                            duration_milliseconds=source.duration_milliseconds,
                        )
                    )
                    candidate_universe[asset_id] = batch[-1]
                    additions.append(source)
                    asset_ids.append(asset_id)
                    candidate_imported[(claim.workspace_handle, claim.output_handle)] = (
                        _ImportedOutput(asset_id, claim.segment_id, claim, source)
                    )
                candidate_reference = apply_video_source_batch(captured_reference, tuple(batch))
                public_assets = tuple(
                    source.public_asset(asset_id)
                    for source, asset_id in zip(additions, asset_ids, strict=True)
                )
                candidate_history = (
                    refresh_authoring_asset_catalog_v2(captured_history, public_assets)
                    if isinstance(captured_history, TimelineHistoryStateV2)
                    else refresh_timeline_asset_catalog(captured_history, public_assets)
                )
                candidate_prepared = self._prepared_with_catalog(
                    captured_prepared,
                    composite,
                    candidate_history,
                    source_reference_revision=candidate_reference.revision,
                    source_timeline_revision=captured_timeline.revision,
                )
            else:
                candidate_prepared = captured_prepared

            if candidate_binding is None:
                # IMPORTANT: a source-free T2V workspace has no base receipt until its first
                # generated import. Reaching this guard means no generated receipt was created.
                raise AuthoringWorkbenchError(500, "internal_invariant")

            if new_claims:
                candidate_entry = _AuthoringEntry(
                    seed=entry.seed,
                    universe=candidate_universe,
                    not_admissible=captured_not_admissible,
                    reference=candidate_reference,
                    timeline=captured_timeline,
                    clip_counter=entry.clip_counter,
                    touched_at=entry.touched_at,
                    source_binding=candidate_binding,
                    timeline_history=(
                        candidate_history
                        if isinstance(candidate_history, TimelineHistoryState)
                        else entry.timeline_history
                    ),
                    timeline_history_v2=(
                        candidate_history
                        if isinstance(candidate_history, TimelineHistoryStateV2)
                        else None
                    ),
                    initialized_sources=candidate_prepared,
                    initialization_fingerprint=entry.initialization_fingerprint,
                    lineage_token=entry.lineage_token,
                    imported_outputs=candidate_imported,
                    # IMPORTANT: an import swaps this entry; dropping the Production owner makes
                    # the next ensure reject the live editor and strands existing user edits.
                    production_owner=entry.production_owner,
                    project_created=entry.project_created,
                )
            else:
                # CRITICAL: an already-imported request is a no-op on workspace authority. Keep
                # object identity or in-flight derivative claims become stale despite an unchanged
                # public snapshot and the media bin loses its thumbnail during idempotent import.
                candidate_entry = entry
            candidate_projection = self._projection(
                request.authoring_workspace_handle, candidate_entry
            )
            prior_reference_fingerprint = canonical_projection(captured_reference).fingerprint
            next_reference_fingerprint = canonical_projection(candidate_reference).fingerprint
            timeline_fingerprint = captured_timeline.content_fingerprint()
            rows = tuple(
                ProductionAuthoringImportRow(
                    segment_id=claim.segment_id,
                    output_handle=claim.output_handle,
                    asset_id=assigned[claim.output_handle][0],
                    source_kind="video",
                    disposition=(
                        "already_imported"
                        if claim.output_handle in existing_by_output
                        else "created"
                    ),
                )
                for claim in claims
            )
            if isinstance(request, ProductionAuthoringImportRequestV2):
                if not isinstance(captured_history, TimelineHistoryStateV2) or not isinstance(
                    candidate_history, TimelineHistoryStateV2
                ):
                    raise AuthoringWorkbenchError(409, "unsupported_profile")
                receipt_v2 = ProductionAuthoringImportReceiptV2(
                    request_id=request.request_id,
                    disposition="created" if new_claims else "already_imported",
                    production_workspace_id=claims[0].workspace_id,
                    production_workspace_revision=claims[0].workspace_revision,
                    production_workspace_fingerprint=claims[0].workspace_fingerprint,
                    authoring_workspace_handle=request.authoring_workspace_handle,
                    authoring_registry_fingerprint=entry.seed.registry_fingerprint,
                    prior_reference_revision=captured_reference.revision,
                    next_reference_revision=candidate_reference.revision,
                    prior_reference_fingerprint=prior_reference_fingerprint,
                    next_reference_fingerprint=next_reference_fingerprint,
                    prior_timeline_revision=captured_timeline.revision,
                    next_timeline_revision=captured_timeline.revision,
                    prior_timeline_content_fingerprint=timeline_fingerprint,
                    next_timeline_content_fingerprint=timeline_fingerprint,
                    authoring_schema=NLE_AUTHORING_SCHEMA,
                    profile_id=NLE_AUTHORING_PROFILE_ID,
                    prior_nle_workspace_revision=captured_history.authoring.workspace_revision,
                    next_nle_workspace_revision=candidate_history.authoring.workspace_revision,
                    prior_nle_workspace_fingerprint=captured_history.authoring.workspace_fingerprint,
                    next_nle_workspace_fingerprint=candidate_history.authoring.workspace_fingerprint,
                    prior_nle_timeline_revision=captured_history.authoring.timeline_revision,
                    next_nle_timeline_revision=candidate_history.authoring.timeline_revision,
                    prior_nle_timeline_fingerprint=captured_history.authoring.timeline_fingerprint,
                    next_nle_timeline_fingerprint=candidate_history.authoring.timeline_fingerprint,
                    prior_nle_authoring_fingerprint=captured_history.authoring.authoring_fingerprint,
                    next_nle_authoring_fingerprint=candidate_history.authoring.authoring_fingerprint,
                    rows=rows,
                    authority_versions=(
                        PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA_V2,
                        PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2,
                        "h3.context.segment_artifact_receipt.v1",
                        GENERATED_AUTHORING_SOURCE_SCHEMA,
                        NLE_AUTHORING_SCHEMA,
                        NLE_AUTHORING_PROFILE_ID,
                    ),
                )
                history_projection = self._history_projection(
                    request.authoring_workspace_handle, candidate_history
                )
                response: (
                    ProductionAuthoringImportResponse | ProductionAuthoringImportResponseV2
                ) = ProductionAuthoringImportResponseV2(
                    receipt_v2, candidate_projection, history_projection
                )
            else:
                if not isinstance(captured_history, TimelineHistoryState) or not isinstance(
                    candidate_history, TimelineHistoryState
                ):
                    raise AuthoringWorkbenchError(409, "unsupported_profile")
                prior_nle = captured_history.snapshot
                next_nle = candidate_history.snapshot
                receipt = ProductionAuthoringImportReceipt(
                    request_id=request.request_id,
                    disposition="created" if new_claims else "already_imported",
                    production_workspace_id=claims[0].workspace_id,
                    production_workspace_revision=claims[0].workspace_revision,
                    production_workspace_fingerprint=claims[0].workspace_fingerprint,
                    authoring_workspace_handle=request.authoring_workspace_handle,
                    authoring_registry_fingerprint=entry.seed.registry_fingerprint,
                    prior_reference_revision=captured_reference.revision,
                    next_reference_revision=candidate_reference.revision,
                    prior_reference_fingerprint=prior_reference_fingerprint,
                    next_reference_fingerprint=next_reference_fingerprint,
                    prior_timeline_revision=captured_timeline.revision,
                    next_timeline_revision=captured_timeline.revision,
                    prior_timeline_content_fingerprint=timeline_fingerprint,
                    next_timeline_content_fingerprint=timeline_fingerprint,
                    prior_nle_workspace_revision=prior_nle.workspace_revision,
                    next_nle_workspace_revision=next_nle.workspace_revision,
                    prior_nle_workspace_fingerprint=prior_nle.workspace_fingerprint,
                    next_nle_workspace_fingerprint=next_nle.workspace_fingerprint,
                    prior_nle_timeline_revision=prior_nle.timeline_revision,
                    next_nle_timeline_revision=next_nle.timeline_revision,
                    prior_nle_timeline_fingerprint=prior_nle.timeline_fingerprint,
                    next_nle_timeline_fingerprint=next_nle.timeline_fingerprint,
                    prior_nle_public_fingerprint=prior_nle.public_fingerprint,
                    next_nle_public_fingerprint=next_nle.public_fingerprint,
                    rows=rows,
                    authority_versions=(
                        PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA,
                        PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA,
                        "h3.context.segment_artifact_receipt.v1",
                        GENERATED_AUTHORING_SOURCE_SCHEMA,
                    ),
                )
                response = ProductionAuthoringImportResponse(receipt, candidate_projection)
            self._require_import_deadline(deadline, cancelled)
            if any(not source.current() for source in staged_sources.values()):
                raise AuthoringWorkbenchError(409, "source_stale")

            with self._lock:
                self._prune(self._clock())
                current = self._entries.get(request.authoring_workspace_handle)
                if (
                    current is not entry
                    or current.importing is not token
                    or current.reference is not captured_reference
                    or current.timeline is not captured_timeline
                    or not self._owns_history(current, captured_history)
                    or current.initialized_sources is not captured_prepared
                    or current.source_binding is not captured_binding
                    or current.lineage_token is None
                    or any(claim.lineage_token is not current.lineage_token for claim in claims)
                ):
                    raise AuthoringWorkbenchError(409, "authoring_stale")
                replay = self._import_ledger.get(request.request_id)
                if replay is not None:
                    if replay.request_digest != request.digest:
                        raise AuthoringWorkbenchError(409, "request_replay_mismatch")
                    return replay.response
                # CRITICAL: check private staged owners before retaining Production authority.
                # Generated borrowers acquire source -> Production; reversing that order here
                # can deadlock. These unpublished sources remain owned by this import attempt.
                if any(not source.owner_retained() for source in staged_sources.values()):
                    raise AuthoringWorkbenchError(409, "source_stale")
                with production_registry.authoring_output_commit_guard(claims) as current_batch:
                    if not current_batch:
                        raise AuthoringWorkbenchError(409, "source_stale")
                    self._require_import_deadline(deadline, cancelled)
                    candidate_entry.importing = None
                    candidate_entry.touched_at = self._clock()
                    candidate_entries = OrderedDict(self._entries)
                    candidate_entries[request.authoring_workspace_handle] = candidate_entry
                    candidate_entries.move_to_end(request.authoring_workspace_handle)
                    candidate_ledger = OrderedDict(self._import_ledger)
                    ledger = _ImportLedgerEntry(
                        request.authoring_workspace_handle,
                        request.digest,
                        response,
                        tuple(
                            (request.production_workspace_handle, row.output_handle)
                            for row in request.entries
                        ),
                    )
                    candidate_ledger[request.request_id] = ledger
                    candidate_ledger.move_to_end(request.request_id)
                    while len(candidate_ledger) > MAX_AUTHORING_IMPORT_LEDGER:
                        candidate_ledger.popitem(last=False)
                    if composite is not None:
                        composite.activate()
                    # CRITICAL: ownership activation is followed only by the prepared state-table
                    # swap. Adding fallible work here can release the still-live base receipt on
                    # rollback or publish a workspace without its replay receipt.
                    self._entries, self._import_ledger = candidate_entries, candidate_ledger
                    committed = True
            if candidate_prepared is not captured_prepared:
                captured_prepared.discard()
            return response
        except ContractValidationError as exc:
            code = str(exc).split(":", 1)[0]
            raise AuthoringWorkbenchError(422, "capacity" if "capacity" in code else code) from None
        except AuthoringSourceBindingError as exc:
            if exc.code in {"timed_out", "cancelled"}:
                raise AuthoringWorkbenchError(408, exc.code) from None
            if exc.code == "source_stale":
                raise AuthoringWorkbenchError(409, exc.code) from None
            raise AuthoringWorkbenchError(422, exc.code) from None
        finally:
            if not committed:
                if candidate_prepared is not None and candidate_prepared is not captured_prepared:
                    candidate_prepared.discard()
                if composite is not None:
                    composite.release()
                else:
                    for source in staged_sources.values():
                        source.release()
                with self._lock:
                    if entry is not None and entry.importing is token:
                        entry.importing = None

    def admit_media_preview(self, request: AuthoringPreviewRequest) -> AuthoringMediaPreviewClaim:
        """Atomically bind one closed request to the current exact clip and private source."""

        if type(request) is not AuthoringPreviewRequest:
            raise AuthoringWorkbenchError(400, "preview_request_invalid")
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entry(request.workspace_handle)
            fingerprint = entry.timeline.content_fingerprint()
            if (
                entry.reference.revision != request.reference_revision
                or entry.timeline.revision != request.timeline_revision
                or fingerprint != request.timeline_content_fingerprint
            ):
                raise AuthoringWorkbenchError(409, "preview_stale")
            clip = next(
                (item for item in entry.timeline.clips if item.clip_id == request.clip_id),
                None,
            )
            if clip is None:
                raise AuthoringWorkbenchError(404, "preview_clip_unknown")
            if clip.kind is not MediaKind.VIDEO or entry.reference.entry(clip.asset_id) is None:
                raise AuthoringWorkbenchError(422, "preview_source_unsupported")
            fps = entry.timeline.profile.video_fps
            if clip.frames > MAX_AUTHORING_PREVIEW_CLIP_FRAMES or clip.frames * 1000 > 30_000 * fps:
                raise AuthoringWorkbenchError(422, "preview_source_too_long")
            binding = entry.source_binding
            if binding is None:
                raise AuthoringWorkbenchError(422, "preview_source_unsupported")
            try:
                source = claim_transferred_authoring_source(binding, clip.asset_id)
            except AuthoringSourceBindingError as exc:
                if exc.code in {"source_stale", "receipt_released"}:
                    raise AuthoringWorkbenchError(409, "preview_stale") from None
                if exc.code in {"source_unsupported", "invalid_receipt"}:
                    raise AuthoringWorkbenchError(422, "preview_source_unsupported") from None
                raise AuthoringWorkbenchError(404, "preview_authority_mismatch") from None
            entry.touched_at = now
            self._entries.move_to_end(request.workspace_handle)
            return AuthoringMediaPreviewClaim(
                request=request,
                source_id=clip.asset_id,
                source_start_frame=clip.source_start_frame,
                frames=clip.frames,
                video_fps=fps,
                _entry=entry,
                _clip=clip,
                _source_binding=binding,
                _source=source,
            )

    def media_preview_is_current(self, claim: AuthoringMediaPreviewClaim) -> bool:
        """Late CAS across workspace, revisions, clip relation, receipt and file identity."""

        if type(claim) is not AuthoringMediaPreviewClaim:
            return False
        with self._lock:
            entry = self._entries.get(claim.request.workspace_handle)
            if (
                entry is not claim._entry
                or entry.source_binding is not claim._source_binding
                or entry.reference.revision != claim.request.reference_revision
                or entry.timeline.revision != claim.request.timeline_revision
                or entry.timeline.content_fingerprint()
                != claim.request.timeline_content_fingerprint
                or not any(item is claim._clip for item in entry.timeline.clips)
            ):
                return False
            try:
                return (
                    claim_transferred_authoring_source(claim._source_binding, claim.source_id)
                    is claim._source
                )
            except AuthoringSourceBindingError:
                return False

    def dispatch(
        self,
        action: dict[str, object],
        *,
        production_registry: ProductionWorkspaceRegistry | None = None,
    ) -> AuthoringDispatchResult:
        request_id = str(action["request_id"])
        name = str(action["action"])
        payload: dict[str, object] = _expect_dict(action["payload"])
        if name == "ensure_authoring_from_production":
            _validate_action(action)
            if production_registry is None:
                from .composition_root import PRODUCTION_WORKSPACE, get

                production_registry = get(PRODUCTION_WORKSPACE)
            fingerprint = _request_fingerprint(name, payload)
            with self._lock:
                self._prune(self._clock())
                replay = self._ledger.get(request_id)
                if replay is not None and replay.request_fingerprint != fingerprint:
                    raise AuthoringWorkbenchError(422, "request_replay_mismatch")
                # A replay re-resolves instead of returning the ledger entry: the Production
                # owner's anchor, not this ledger, deduplicates targets, so a replay may answer a
                # different current status but can never create a second editor.
                result = self.ensure_from_production(
                    production_registry,
                    workspace_handle=str(payload["production_workspace_handle"]),
                    workspace_id=str(payload["production_workspace_id"]),
                    preferred_authoring_handle=(
                        None
                        if payload["preferred_authoring_handle"] is None
                        else str(payload["preferred_authoring_handle"])
                    ),
                )
                if replay is None:
                    if result.body is None:
                        raise AuthoringWorkbenchError(500, "internal_invariant")
                    self._ledger[request_id] = _LedgerEntry(
                        str(result.body["workspace_handle"]),
                        result.status,
                        None,
                        fingerprint,
                    )
                    while len(self._ledger) > self._max_ledger_entries:
                        self._ledger.popitem(last=False)
                return result
        if name == "initialize_timeline_history":
            _validate_action(action)
            return self._initialize_history(request_id, payload)
        if name == "apply_timeline_transaction" and payload.get("request_id") != request_id:
            raise AuthoringWorkbenchError(422, "timeline_request_mismatch")
        now = self._clock()
        request_fingerprint = _request_fingerprint(name, payload)
        with self._lock:
            self._prune(now)
            replay = self._ledger.get(request_id)
            if replay is not None:
                if replay.request_fingerprint != request_fingerprint:
                    raise AuthoringWorkbenchError(422, "request_replay_mismatch")
                # CRITICAL: release/expiry clears timeline history before the workspace becomes a
                # tombstone. Never let the outer request ledger resurrect an old receipt or
                # projection; only the bodiless terminal release acknowledgement stays replayable.
                if replay.workspace_handle in self._tombstones and name != "release_workspace":
                    raise AuthoringWorkbenchError(410, "workspace_gone")
                # CRITICAL: timeline receipts and cursor material belong only to the bounded core
                # history ledger. The outer ledger reserves the request ID but must delegate every
                # replay so core eviction cannot be bypassed by an older cached response.
                if name != "apply_timeline_transaction":
                    return AuthoringDispatchResult(replay.status, replay.body)
            status, body, handle = self._dispatch_locked(name, payload, now)
            # A timeline rejection has no core idempotency receipt and carries the then-current
            # accepted projection. Caching it here would replay stale state after another request
            # advances the workspace, so only accepted timeline mutations enter the outer ledger.
            should_record = name == "create_authoring_workspace" or (
                name in _MUTATIONS and not (name == "apply_timeline_transaction" and status != 200)
            )
            if should_record:
                # Timeline bodies can contain snapshots, inverses and cursors. Retaining another
                # copy here would outlive the core's 128-entry / 8 MiB eviction boundary.
                ledger_body = None if name == "apply_timeline_transaction" else body
                self._ledger[request_id] = _LedgerEntry(
                    handle, status, ledger_body, request_fingerprint
                )
                self._ledger.move_to_end(request_id)
                while len(self._ledger) > self._max_ledger_entries:
                    self._ledger.popitem(last=False)
            return AuthoringDispatchResult(status, body)

    def _initialize_history(
        self,
        request_id: str,
        payload: dict[str, object],
    ) -> AuthoringDispatchResult:
        handle = str(payload["workspace_handle"])
        fingerprint = _request_fingerprint("initialize_timeline_history", payload)
        with self._lock:
            self._prune(self._clock())
            replay = self._ledger.get(request_id)
            if replay is not None and replay.request_fingerprint != fingerprint:
                raise AuthoringWorkbenchError(422, "request_replay_mismatch")
            entry = self._entry(handle)
            if entry.timeline_history_v2 is not None:
                if entry.initialization_fingerprint != fingerprint:
                    raise AuthoringWorkbenchError(409, "timeline_history_already_bound")
                # CRITICAL: an init retry returns the current history, never its old seed snapshot.
                # Caching the first response here can roll the browser back after accepted edits.
                return AuthoringDispatchResult(
                    200, self._history_projection(handle, entry.timeline_history_v2)
                )
            if entry.timeline_history is not None:
                raise AuthoringWorkbenchError(409, "timeline_history_profile_mismatch")
            if entry.initializing is not None:
                raise AuthoringWorkbenchError(409, "timeline_initialization_busy")
            if (
                payload["expected_reference_revision"] != entry.reference.revision
                or payload["expected_timeline_revision"] != entry.timeline.revision
            ):
                raise AuthoringWorkbenchError(409, "initialization_revision_conflict")
            projection = self._projection(handle, entry)
            source_binding = entry.source_binding
            generation = source_binding.generation if source_binding else 0
            token = object()
            entry.initializing = token
        candidate: PreparedAuthoringHistory | None = None
        try:
            # CRITICAL: probing/copying/fonts run outside the registry lock. A release or reference
            # edit must remain executable during preparation and win the final compare-and-bind.
            candidate = self._history_preparer(projection, source_binding)
            with self._lock:
                self._prune(self._clock())
                current = self._entry(handle)
                if (
                    current is not entry
                    or current.initializing is not token
                    or current.source_binding is not source_binding
                    or current.timeline_history is not None
                    or current.timeline_history_v2 is not None
                    or payload["expected_reference_revision"] != current.reference.revision
                    or payload["expected_timeline_revision"] != current.timeline.revision
                    or generation != candidate.generation
                    or candidate.authoring_state is None
                    or candidate.authoring_state.workspace_handle != handle
                    or candidate.authoring_state.workspace_revision != current.reference.revision
                    or candidate.authoring_state.timeline_revision != current.timeline.revision
                    or (source_binding is not None and source_binding.released)
                    or not candidate.current()
                ):
                    raise AuthoringWorkbenchError(409, "initialization_currentness_conflict")
                # Construct the validated history and response before the one atomic state bind.
                history = TimelineHistoryStateV2.initialize(candidate.authoring_state)
                body = self._history_projection(handle, history)
                current.timeline_history_v2 = history
                current.initialized_sources = candidate
                current.initialization_fingerprint = fingerprint
                current.initializing = None
                current.touched_at = self._clock()
                self._entries.move_to_end(handle)
                self._ledger[request_id] = _LedgerEntry(handle, 200, None, fingerprint)
                self._ledger.move_to_end(request_id)
                while len(self._ledger) > self._max_ledger_entries:
                    self._ledger.popitem(last=False)
                candidate = None
                return AuthoringDispatchResult(200, body)
        except (AuthoringSourceBindingError, ContractValidationError) as exc:
            raise AuthoringWorkbenchError(
                422, getattr(exc, "code", "initialization_unavailable")
            ) from None
        finally:
            if candidate is not None:
                candidate.discard()
            with self._lock:
                if entry.initializing is token:
                    entry.initializing = None

    def admit_media_derivative(
        self,
        request: CreateLeaseRequest,
        *,
        generator: AuthoringDerivativeGenerator | None,
        media_adapter: QualifiedAVMediaAdapter | None,
    ) -> AuthoringDerivativeClaim:
        """Mint a derivative consumer claim without disclosing the private registry or source."""
        from ..core.authoring_media import NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA
        from .authoring_derivative_source import AuthoringDerivativeClaim

        with self._lock:
            self._prune(self._clock())
            entry = self._entry(request.workspace_handle)
            history = self._history(entry)
            sources = entry.initialized_sources
            if sources is None:
                raise AuthoringWorkbenchError(422, "render_sources_unavailable")
            authoring_state = None
            if request.request_schema == NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA:
                if not isinstance(history, TimelineHistoryStateV2):
                    raise AuthoringWorkbenchError(409, "authoring_profile_mismatch")
                authoring_state = history.authoring
                snapshot = None
            else:
                snapshot = self._render_snapshot(history)
                if snapshot is None:
                    raise AuthoringWorkbenchError(409, "render_snapshot_unavailable")
            binding = entry.source_binding

        def current() -> bool:
            with self._lock:
                self._prune(self._clock())
                active = self._entries.get(request.workspace_handle)
                if active is None or active is not entry:
                    return False
                active_history = self._history(active)
                common = (
                    active.initialized_sources is sources
                    and active.source_binding is binding
                    and active.reference.revision == sources.source_reference_revision
                    and active.timeline.revision == sources.source_timeline_revision
                )
                if not common:
                    return False
                if authoring_state is not None:
                    return (
                        isinstance(active_history, TimelineHistoryStateV2)
                        and active_history.authoring.authoring_fingerprint
                        == authoring_state.authoring_fingerprint
                    )
                active_snapshot = self._render_snapshot(active_history)
                return (
                    snapshot is not None
                    and active_snapshot is not None
                    and active_snapshot.public_fingerprint == snapshot.public_fingerprint
                )

        # Source hashing/generation belongs outside the workspace lock. The claim repeats this
        # identity check after each I/O boundary, so a released or superseded authoring state
        # cannot return media.
        return AuthoringDerivativeClaim(
            request,
            snapshot,
            sources,
            binding,
            current,
            generator,
            media_adapter,
            authoring_state=authoring_state,
        )

    def prepare_render_plan(self, handle: str) -> BoundAuthoringRenderPlan:
        """Internal M25-18 handoff; no browser route or renderer execution is introduced."""
        with self._lock:
            self._prune(self._clock())
            entry = self._entry(handle)
            history = self._history(entry)
            source_history = entry.initialized_sources
            if source_history is None:
                raise AuthoringWorkbenchError(422, "render_sources_unavailable")
            snapshot = self._render_snapshot(history)
            if snapshot is None:
                raise AuthoringWorkbenchError(409, "render_snapshot_unavailable")

        def current() -> bool:
            with self._lock:
                self._prune(self._clock())
                active = self._entries.get(handle)
                if active is None or active is not entry:
                    return False
                active_history = self._history(active)
                active_snapshot = self._render_snapshot(active_history)
                return (
                    active.initialized_sources is source_history
                    and active.reference.revision == source_history.source_reference_revision
                    and active.timeline.revision == source_history.source_timeline_revision
                    and active_snapshot is not None
                    and active_snapshot.public_fingerprint == snapshot.public_fingerprint
                )

        return prepare_bound_render_plan(source_history, snapshot, current)

    def _dispatch_locked(
        self, name: str, payload: dict[str, object], now: float
    ) -> tuple[int, dict[str, object] | None, str]:
        if name == "create_authoring_workspace":
            if len(self._entries) >= self._max_entries:
                raise AuthoringWorkbenchError(429, "workspace_capacity")
            try:
                context_handle = str(payload["context_workspace_handle"])
                if self._seed_claim is None:
                    workspace_claim = self._workspace_claim(context_handle)
                else:
                    workspace_claim = SidebarAuthoringWorkspaceClaim(
                        seed=self._seed_claim(context_handle), source_binding=None
                    )
            except KeyError:
                raise AuthoringWorkbenchError(404, "context_unavailable") from None
            source_binding = workspace_claim.source_binding
            try:
                seed = workspace_claim.seed
                universe, not_admissible, reference = _seed_reference_state(seed)
                timeline = create_timeline(
                    build_h3_timeline_profile(build_temporal_profile()),
                    build_h3_timeline_limits(),
                    reference_view(reference),
                )
                handle = "authoring-" + secrets.token_hex(16)
                entry = _AuthoringEntry(
                    seed=seed,
                    universe=universe,
                    not_admissible=not_admissible,
                    reference=reference,
                    timeline=timeline,
                    clip_counter=0,
                    touched_at=now,
                    source_binding=source_binding,
                    timeline_history=None,
                    lineage_token=seed.lineage_token,
                )
                projection = self._projection(handle, entry)
            except Exception:
                if source_binding is not None:
                    source_binding.release()
                raise
            self._entries[handle] = entry
            return 201, projection, handle
        handle = str(payload["workspace_handle"])
        if name == "release_workspace":
            entry_or_none = self._entries.pop(handle, None)
            if entry_or_none is None and handle not in self._tombstones:
                raise AuthoringWorkbenchError(404, "workspace_unavailable")
            if entry_or_none is not None and entry_or_none.source_binding is not None:
                entry_or_none.source_binding.release()
            if entry_or_none is not None and entry_or_none.timeline_history is not None:
                entry_or_none.timeline_history = release_timeline_history(
                    entry_or_none.timeline_history
                )
            if entry_or_none is not None and entry_or_none.timeline_history_v2 is not None:
                entry_or_none.timeline_history_v2 = release_timeline_history_v2(
                    entry_or_none.timeline_history_v2
                )
            if entry_or_none is not None and entry_or_none.initialized_sources is not None:
                entry_or_none.initialized_sources.discard()
            self._record_tombstone(handle, _Tombstone("released", now))
            return 204, None, handle
        entry = self._entry(handle)
        entry.touched_at = now
        self._entries.move_to_end(handle)
        if name == "read_projection":
            return 200, self._projection(handle, entry), handle
        if name == "read_timeline_history":
            history = self._history(entry)
            return 200, self._history_projection(handle, history), handle
        if name == "read_snap":
            playhead = payload["playhead_frame"]
            exclude = payload["exclude_clip_id"]
            frame = min(int(str(payload["frame"])), entry.timeline.profile.max_extent_frames)
            candidates = snap_candidates(
                entry.timeline,
                frame=frame,
                playhead_frame=None if playhead is None else int(str(playhead)),
                exclude_clip_id=None if exclude is None else str(exclude),
            )
            return (
                200,
                {
                    "schema": AUTHORING_SNAP_SCHEMA,
                    "workspace_handle": handle,
                    "timeline_revision": entry.timeline.revision,
                    "candidates": [item.to_wire() for item in candidates],
                },
                handle,
            )
        if name == "apply_timeline_transaction":
            history = self._history(entry)
            try:
                if isinstance(history, TimelineHistoryStateV2):
                    updated_v2, receipt_v2 = apply_timeline_transaction_v2(history, payload)
                    entry.timeline_history_v2 = updated_v2
                    if entry.initialized_sources is not None:
                        entry.initialized_sources.authoring_state = updated_v2.authoring
                        entry.initialized_sources.snapshot = materialize_render_snapshot(
                            updated_v2.authoring
                        )
                    return 200, receipt_v2.to_wire(), handle
                updated_v1, receipt_v1 = apply_timeline_transaction(history, payload)
                entry.timeline_history = updated_v1
                return 200, receipt_v1.to_wire(), handle
            except ContractValidationError as exc:
                code = str(exc).split(":", 1)[0]
                return 409, self._history_projection(handle, history, rejection=code), handle
        try:
            if name == "set_availability":
                self._apply_availability(entry, payload)
            elif name in _REFERENCE_ACTIONS:
                self._apply_reference(entry, name, payload)
            else:
                self._apply_timeline(entry, name, payload)
        except ContractValidationError as exc:
            code = str(exc).split(":", 1)[0]
            return 409, self._projection(handle, entry, rejection=code), handle
        return 200, self._projection(handle, entry), handle

    def _apply_availability(self, entry: _AuthoringEntry, payload: dict[str, object]) -> None:
        facts_raw = _expect_list(payload["facts"])
        fact_set = AvailabilityFactSet(
            producer=AUTHORING_AVAILABILITY_PRODUCER,
            producer_revision=int(str(payload["producer_revision"])),
            fingerprint=str(payload["fingerprint"]),
            facts=tuple(
                AvailabilityFact(
                    video_id=str(item["video_id"]),
                    availability=SoundtrackAvailability(str(item["availability"])),
                )
                for item in map(_expect_dict, facts_raw)
            ),
        )
        entry.reference, _receipt = apply_availability_facts(entry.reference, fact_set)

    def _apply_reference(
        self, entry: _AuthoringEntry, name: str, payload: dict[str, object]
    ) -> None:
        expected = int(str(payload["expected_reference_revision"]))
        state = entry.reference
        if name == "add_source":
            source_id = str(payload["source_id"])
            source = entry.universe.get(source_id)
            if source is None:
                raise AuthoringWorkbenchError(422, "unknown_source")
            if (
                entry.project_created
                and entry.source_binding is None
                and any(row.asset_id == source_id for row in entry.seed.sources)
            ):
                # CRITICAL: a retained project seed names original media but owns no Context
                # receipt. Re-admitting that media would bypass the exact-source guard.
                raise AuthoringWorkbenchError(422, "source_not_bound")
            state, _receipt = apply_reference_command(state, AddSource(expected, source))
            entry.not_admissible.pop(source_id, None)
        elif name == "remove_source":
            state, _receipt = apply_reference_command(
                state, RemoveSource(expected, str(payload["source_id"]))
            )
        elif name == "reorder_source":
            state, _receipt = apply_reference_command(
                state,
                ReorderSource(expected, str(payload["source_id"]), int(str(payload["new_index"]))),
            )
        elif name == "include_soundtrack":
            state, _receipt = apply_reference_command(
                state,
                IncludeSoundtrack(expected, str(payload["video_id"]), str(payload["audio_id"])),
            )
        else:
            state, _receipt = apply_reference_command(
                state, ExcludeSoundtrack(expected, str(payload["video_id"]))
            )
        entry.reference = state

    def _apply_timeline(
        self, entry: _AuthoringEntry, name: str, payload: dict[str, object]
    ) -> None:
        expected = int(str(payload["expected_timeline_revision"]))
        view = reference_view(entry.reference)
        state: TimelineState = entry.timeline
        if name == "add_clip":
            entry.clip_counter += 1
            command: object = AddClip(
                expected_revision=expected,
                clip_id=f"clip-{entry.clip_counter}",
                asset_id=str(payload["asset_id"]),
                lane=int(str(payload["lane"])),
                start_frame=int(str(payload["start_frame"])),
                frames=int(str(payload["frames"])),
                source_start_frame=int(str(payload["source_start_frame"])),
            )
        elif name == "remove_clip":
            command = RemoveClip(expected_revision=expected, clip_id=str(payload["clip_id"]))
        elif name == "move_clip":
            command = MoveClip(
                expected_revision=expected,
                clip_id=str(payload["clip_id"]),
                delta_frames=int(str(payload["delta_frames"])),
                delta_lanes=int(str(payload["delta_lanes"])),
            )
        elif name == "move_group":
            clip_ids_raw = _expect_list(payload["clip_ids"])
            command = MoveGroup(
                expected_revision=expected,
                clip_ids=tuple(str(item) for item in clip_ids_raw),
                delta_frames=int(str(payload["delta_frames"])),
            )
        elif name == "trim_clip":
            command = TrimClip(
                expected_revision=expected,
                clip_id=str(payload["clip_id"]),
                edge=TrimEdge(str(payload["edge"])),
                delta_frames=int(str(payload["delta_frames"])),
            )
        elif name == "split_clip":
            entry.clip_counter += 1
            command = SplitClip(
                expected_revision=expected,
                clip_id=str(payload["clip_id"]),
                at_offset_frames=int(str(payload["at_offset_frames"])),
                new_clip_id=f"clip-{entry.clip_counter}",
            )
        elif name == "merge_clips":
            command = MergeClips(
                expected_revision=expected,
                first_clip_id=str(payload["first_clip_id"]),
                second_clip_id=str(payload["second_clip_id"]),
            )
        elif name == "link_clips":
            command = LinkClips(
                expected_revision=expected,
                video_clip_id=str(payload["video_clip_id"]),
                audio_clip_id=str(payload["audio_clip_id"]),
            )
        elif name == "unlink_clips":
            command = UnlinkClips(
                expected_revision=expected, video_clip_id=str(payload["video_clip_id"])
            )
        elif name == "set_envelope":
            points_raw = _expect_list(payload["points"])
            command = SetEnvelope(
                expected_revision=expected,
                clip_id=str(payload["clip_id"]),
                points=tuple(
                    EnvelopePoint(
                        offset_frames=int(str(item["offset_frames"])),
                        strength_per_mille=int(str(item["strength_per_mille"])),
                    )
                    for item in map(_expect_dict, points_raw)
                ),
            )
        else:
            clip_ids_raw = _expect_list(payload["clip_ids"])
            command = SelectClips(
                expected_revision=expected,
                clip_ids=tuple(str(item) for item in clip_ids_raw),
            )
        entry.timeline, _receipt = apply_timeline_command(state, command, view)  # type: ignore[arg-type]


def build_registry() -> AuthoringWorkspaceRegistry:
    """Construct this adapter's process registry. Called only by the composition root."""

    return AuthoringWorkspaceRegistry()


_registry = component(AUTHORING_WORKSPACE, AuthoringWorkspaceRegistry)


def dispatch_authoring_action(action: dict[str, object]) -> AuthoringDispatchResult:
    return _registry().dispatch(action)


def bind_authoring_timeline_history_snapshot(snapshot: PublicCompositionSnapshot) -> None:
    """Bind backend-accepted composition truth into the process-local authoring registry."""

    _registry().bind_timeline_history_snapshot(snapshot)


def admit_authoring_media_preview(
    request: AuthoringPreviewRequest,
) -> AuthoringMediaPreviewClaim:
    return _registry().admit_media_preview(request)


def authoring_media_preview_is_current(claim: AuthoringMediaPreviewClaim) -> bool:
    return _registry().media_preview_is_current(claim)


def admit_authoring_media_derivative(
    request: CreateLeaseRequest,
    *,
    generator: AuthoringDerivativeGenerator | None,
    media_adapter: QualifiedAVMediaAdapter | None,
) -> AuthoringDerivativeClaim:
    """One additive application-service boundary for the private derivative route."""
    return _registry().admit_media_derivative(
        request, generator=generator, media_adapter=media_adapter
    )


_ROUTE_POLICY = RoutePolicy(
    path=AUTHORING_ACTION_ROUTE,
    owner=AUTHORING_ACTION_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    max_bytes=MAX_AUTHORING_ACTION_BYTES,
    refusals=(AuthoringWorkbenchError,),
)


def _refusal_body(_status: int, _reason: str) -> None:
    """This route has always answered every seam-level refusal with a bodiless response."""

    return None


def _refuse(error: BaseException) -> RouteResult:
    status = getattr(error, "status", 400)
    projection = getattr(error, "projection", None)
    # CRITICAL: a projection rides along only on a 409 conflict, because that is the one refusal the
    # client must reconcile against. Widening this to every status would publish workspace state on
    # refusals that deliberately answer with nothing at all.
    if status == 409 and projection is not None:
        return RouteResult(409, projection)
    return RouteResult(status if isinstance(status, int) else 400)


def _dispatch_body(payload: bytes) -> RouteResult:
    result = dispatch_authoring_action(decode_authoring_action_json(payload))
    return RouteResult(result.status, result.body)


async def _act(payload: bytes, _context: object) -> RouteResult:
    return await offload_route_handler("authoring", lambda: _dispatch_body(payload))


def ensure_authoring_route_registered() -> bool:
    """Register the strict action route only against modules already owned by the active host."""

    global _ROUTE_REGISTERED
    _ROUTE_REGISTERED = register_owned_route(
        _ROUTE_POLICY,
        __name__,
        _act,
        _refusal_body,
        refusal_mapper=_refuse,
    )
    return _ROUTE_REGISTERED


__all__ = [
    "AUTHORING_ACTION_ROUTE",
    "AUTHORING_ACTION_SCHEMA",
    "AUTHORING_AVAILABILITY_PRODUCER",
    "AUTHORING_PROJECTION_SCHEMA",
    "AUTHORING_SNAP_SCHEMA",
    "AUTHORING_TOMBSTONE_TTL_SECONDS",
    "AUTHORING_WORKSPACE_TTL_SECONDS",
    "AuthoringDispatchResult",
    "AuthoringMediaPreviewClaim",
    "AuthoringWorkbenchError",
    "AuthoringWorkspaceRegistry",
    "MAX_AUTHORING_ACTION_ARRAY",
    "MAX_AUTHORING_ACTION_BYTES",
    "MAX_AUTHORING_ACTION_DEPTH",
    "MAX_AUTHORING_ACTION_NODES",
    "MAX_AUTHORING_REQUEST_LEDGER",
    "MAX_AUTHORING_TOMBSTONES",
    "MAX_AUTHORING_PREVIEW_CLIP_FRAMES",
    "MAX_AUTHORING_WORKSPACES",
    "admit_authoring_media_preview",
    "authoring_media_preview_is_current",
    "bind_authoring_timeline_history_snapshot",
    "decode_authoring_action_json",
    "dispatch_authoring_action",
    "ensure_authoring_route_registered",
]
