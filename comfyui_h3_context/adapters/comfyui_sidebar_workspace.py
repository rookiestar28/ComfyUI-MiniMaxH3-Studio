"""Optional ComfyUI route and bounded in-memory authority for sidebar actions."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

from ..core.assisted_draft import DraftGuarantee, reference_inventory
from ..core.assisted_draft_orchestration import (
    AssistedDraftExecutionResult,
    AssistedDraftReceipt,
    run_assisted_draft_orchestration,
)
from ..core.audit_override import stage_audit_override
from ..core.canonical import canonical_fingerprint, fingerprint_context_report
from ..core.context_reporting import ContextReport
from ..core.contracts import MediaKind, TaskMode
from ..core.errors import NativeH3AdapterError, SidebarWorkspaceError
from ..core.native_h3 import (
    NativeH3Wiring,
    assert_native_h3_wiring_authority,
)
from ..core.prompt_fidelity import PromptFidelityAuditResult, audit_prompt_fidelity
from ..core.registry import ReferenceRegistry
from ..core.segment_workspace import (
    MultiSegmentWorkspace,
    SegmentDuration,
)
from ..core.semantic_proposal_producer import SemanticProposalReviewBundle
from ..core.semantic_proposal_review import (
    SemanticProposalReviewError,
    SemanticProposalReviewHandle,
    build_semantic_proposal_action_result,
    build_semantic_proposal_review_handle,
    build_semantic_proposal_review_projection,
)
from ..core.semantic_proposal_transaction import (
    SemanticProposalTransaction,
    SemanticProposalTransactionError,
    accept_semantic_proposal_transaction,
    cancel_semantic_proposal_transaction,
    reject_semantic_proposal_transaction,
    resolve_semantic_proposal_clarification,
)
from ..core.sidebar_workspace import (
    SIDEBAR_TRANSFER_SCHEMA,
    SidebarWorkspaceProjection,
    build_sidebar_transfer,
    build_sidebar_workspace_projection,
    validate_sidebar_revision,
)
from ..core.ui_projection import ExecutionCorrelation
from .assisted_draft_execution import (
    build_assisted_draft_binding,
    build_assisted_guarantee,
    build_assisted_instruction,
)
from .authoring_source_binding import (
    AuthoringSourceBindingReceipt,
    authoring_source_duration_milliseconds,
    claim_process_authoring_sources,
)
from .comfyui_provider_settings import (
    ProviderAuthorityClaim,
    ProviderSessionExecutionLease,
    ProviderSettingsSessionError,
    ProviderSettingsSessionRegistry,
    provider_session_id_from_request,
    provider_settings_registry,
)
from .comfyui_route_seam import (
    RoutePolicy,
    RouteResult,
    offload_route_handler,
    register_owned_route,
)
from .composition_root import SIDEBAR_WORKSPACE, component
from .production_planning_source import (
    ProductionPlanningSourceSnapshot,
    make_production_planning_source_snapshot,
)

SIDEBAR_ACTION_SCHEMA = "h3.context.sidebar.action.v2"
SIDEBAR_ACTION_ROUTE = "/h3-context/v1/sidebar/action"
ASSISTED_SIDEBAR_ACTION_ROUTE = "/h3-context/v1/sidebar/assisted"
MAX_SIDEBAR_ACTION_BYTES = 70_000
MAX_SIDEBAR_TRANSFER_BYTES = 70_000
MAX_SIDEBAR_ACTION_DEPTH = 8
MAX_SIDEBAR_ACTION_ITEMS = 64
MAX_SIDEBAR_WORKSPACES = 64
SIDEBAR_WORKSPACE_TTL_SECONDS = 15 * 60
_ROOT_KEYS = {
    "schema",
    "workspace_id",
    "expected_revision",
    "expected_report_fingerprint",
    "action",
    "payload",
}
_ACTIONS = {"stage_prompt", "import_prompt", "validate", "export"}
_PROPOSAL_ACTIONS = {
    "proposal_read",
    "proposal_resolve",
    "proposal_accept",
    "proposal_reject",
    "proposal_cancel",
}
_ASSISTED_ACTIONS = {
    "optimize_prompt",
    "refine_prompt",
    "edit_assisted_proposal",
    "accept_assisted_proposal",
    "reject_assisted_proposal",
    "cancel_assisted_execution",
}
_ALL_ACTIONS = _ACTIONS | _PROPOSAL_ACTIONS | _ASSISTED_ACTIONS
_PROPOSAL_PAYLOAD_KEYS = {
    "review_id",
    "expected_transaction_fingerprint",
    "expected_workspace_fingerprint",
}
MAX_SEMANTIC_REVIEW_LINEAGES = 64
SEMANTIC_REVIEW_TTL_SECONDS = 15 * 60
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TASK_MODES = {"t2va", "i2va", "fl2va", "l2va", "ref2va"}
_PROFILES = {"h3_base", "h3_full_reference"}
_ROUTE_REGISTERED = False
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_sidebar_action_v1__"


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _shape(value: object, *, depth: int = 0) -> int:
    if depth > MAX_SIDEBAR_ACTION_DEPTH:
        raise ValueError("action JSON exceeds the depth bound")
    if isinstance(value, dict):
        if type(value) is not dict or len(value) > MAX_SIDEBAR_ACTION_ITEMS:
            raise ValueError("action object exceeds its bound")
        return 1 + sum(
            _shape(key, depth=depth + 1) + _shape(item, depth=depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, list):
        if type(value) is not list or len(value) > MAX_SIDEBAR_ACTION_ITEMS:
            raise ValueError("action array exceeds its bound")
        return 1 + sum(_shape(item, depth=depth + 1) for item in value)
    if value is None or type(value) in {str, int, bool}:
        if type(value) is str and len(value) > 65_536:
            raise ValueError("action text exceeds its bound")
        return 1
    raise ValueError("action JSON contains an unsupported value")


def _validate_action(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _ROOT_KEYS:
        raise ValueError("sidebar action must be one closed object")
    if value["schema"] != SIDEBAR_ACTION_SCHEMA:
        raise ValueError("sidebar action schema is unsupported")
    workspace_id = value["workspace_id"]
    if (
        type(workspace_id) is not str
        or not workspace_id.startswith("ws_")
        or not 35 <= len(workspace_id) <= 131
    ):
        raise ValueError("workspace identity is malformed")
    revision = value["expected_revision"]
    if type(revision) is not int or not 0 <= revision <= 1_000_000:
        raise ValueError("expected revision is invalid")
    fingerprint = value["expected_report_fingerprint"]
    if (
        type(fingerprint) is not str
        or len(fingerprint) != 71
        or not fingerprint.startswith("sha256:")
    ):
        raise ValueError("expected fingerprint is invalid")
    try:
        int(fingerprint[7:], 16)
    except ValueError as exc:
        raise ValueError("expected fingerprint is invalid") from exc
    action = value["action"]
    if type(action) is not str or action not in _ALL_ACTIONS:
        raise ValueError("sidebar action is unsupported")
    payload = value["payload"]
    if type(payload) is not dict:
        raise ValueError("sidebar action payload must be an object")
    if action in {"validate", "export", "optimize_prompt", "cancel_assisted_execution"}:
        if payload:
            raise ValueError("this action accepts no payload members")
    elif action == "refine_prompt":
        from ..core.assisted_refinement import validate_revision_instruction

        if set(payload) != {"instruction"}:
            raise ValueError("refine_prompt payload is not closed")
        validate_revision_instruction(payload["instruction"])
    elif action == "stage_prompt":
        if set(payload) != {"reason", "prompt_text"}:
            raise ValueError("stage_prompt payload is not closed")
        if type(payload["reason"]) is not str or type(payload["prompt_text"]) is not str:
            raise ValueError("stage_prompt values must use exact text")
    elif action == "import_prompt":
        if set(payload) != {"transfer_json"} or type(payload["transfer_json"]) is not str:
            raise ValueError("import_prompt payload is not closed")
    elif action in _PROPOSAL_ACTIONS:
        expected_keys = set(_PROPOSAL_PAYLOAD_KEYS)
        if action == "proposal_resolve":
            expected_keys.add("resolutions")
        if set(payload) != expected_keys:
            raise ValueError("proposal action payload is not closed")
        review_id = payload["review_id"]
        if (
            type(review_id) is not str
            or not review_id.startswith("review_")
            or not 39 <= len(review_id) <= 103
        ):
            raise ValueError("proposal review identity is malformed")
        for key in (
            "expected_transaction_fingerprint",
            "expected_workspace_fingerprint",
        ):
            if type(payload[key]) is not str or _FINGERPRINT.fullmatch(payload[key]) is None:
                raise ValueError("proposal action fingerprint is invalid")
        if action == "proposal_resolve":
            resolutions = payload["resolutions"]
            if (
                type(resolutions) is not list
                or not 1 <= len(resolutions) <= 32
                or any(
                    type(item) is not str or not item or len(item) > 4_096 for item in resolutions
                )
            ):
                raise ValueError("proposal clarification resolutions are invalid")
    else:
        expected = {"proposal_id", "expected_proposal_revision"}
        if action == "edit_assisted_proposal":
            expected.add("prompt_text")
        if set(payload) != expected:
            raise ValueError("assisted proposal payload is not closed")
        proposal_id = payload["proposal_id"]
        proposal_revision = payload["expected_proposal_revision"]
        if (
            type(proposal_id) is not str
            or not proposal_id.startswith("assist_")
            or not 39 <= len(proposal_id) <= 103
            or type(proposal_revision) is not int
            or not 1 <= proposal_revision <= 1_000_000
        ):
            raise ValueError("assisted proposal identity is invalid")
        if action == "edit_assisted_proposal" and (
            type(payload["prompt_text"]) is not str
            or not payload["prompt_text"]
            or len(payload["prompt_text"]) > 65_536
        ):
            raise ValueError("assisted proposal prompt is invalid")
    _shape(value)
    return value


def decode_sidebar_action_json(payload: bytes) -> dict[str, object]:
    """Decode one exact bytes body with strict UTF-8 and recursive duplicate rejection."""

    # CRITICAL: exact bytes admission prevents subclass methods from bypassing the wire-size gate.
    if type(payload) is not bytes or not payload or len(payload) > MAX_SIDEBAR_ACTION_BYTES:
        raise ValueError("sidebar action body must be bounded exact bytes")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("sidebar action body is invalid") from exc
    return _validate_action(value)


def decode_sidebar_transfer_json(payload: bytes) -> dict[str, object]:
    """Decode a raw imported transfer before duplicate member information can be lost."""

    if type(payload) is not bytes or not payload or len(payload) > MAX_SIDEBAR_TRANSFER_BYTES:
        raise ValueError("sidebar transfer must be bounded exact bytes")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("sidebar transfer is invalid") from exc
    keys = {
        "schema",
        "report_id",
        "report_revision",
        "report_fingerprint",
        "prompt_fingerprint",
        "task_mode",
        "profile",
        "prompt_text",
    }
    if type(value) is not dict or set(value) != keys or value["schema"] != SIDEBAR_TRANSFER_SCHEMA:
        raise ValueError("sidebar transfer is incompatible")
    for key in (
        "report_id",
        "report_fingerprint",
        "prompt_fingerprint",
        "task_mode",
        "profile",
        "prompt_text",
    ):
        if type(value[key]) is not str:
            raise ValueError("sidebar transfer contains an invalid field")
    if type(value["report_revision"]) is not int or not 0 <= value["report_revision"] <= 1_000_000:
        raise ValueError("sidebar transfer revision is invalid")
    if _IDENTIFIER.fullmatch(value["report_id"]) is None:
        raise ValueError("sidebar transfer report identity is invalid")
    if (
        _FINGERPRINT.fullmatch(value["report_fingerprint"]) is None
        or _FINGERPRINT.fullmatch(value["prompt_fingerprint"]) is None
    ):
        raise ValueError("sidebar transfer fingerprint is invalid")
    if value["task_mode"] not in _TASK_MODES or value["profile"] not in _PROFILES:
        raise ValueError("sidebar transfer mode or profile is unsupported")
    # CRITICAL: authenticate the exact portable prompt text before any registry mutation.
    if value["prompt_fingerprint"] != canonical_fingerprint(value["prompt_text"]):
        raise ValueError("sidebar transfer prompt fingerprint does not match prompt text")
    _shape(value)
    return value


@dataclass(slots=True)
class _ProposalReviewEntry:
    anchor: str
    handle: SemanticProposalReviewHandle
    report: ContextReport | None
    workspace: MultiSegmentWorkspace | None
    transaction: SemanticProposalTransaction | None
    correlation: ExecutionCorrelation
    sidebar_workspace_id: str
    sidebar_revision: int
    sidebar_report_fingerprint: str
    transaction_aliases: set[str]
    workspace_aliases: set[str]
    touched_at: float
    state: str = "active"
    publication_identity: object | None = None
    publication_claim: object | None = None
    active_claim: object | None = None
    terminal_action: str | None = None
    terminal_result: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _SemanticProposalReviewPublication:
    handle: SemanticProposalReviewHandle
    identity: object | None


class _ProposalReviewCoordination:
    """Process-wide logical-key ledger and mutation CAS for private proposal review entries."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_lineages: int = MAX_SEMANTIC_REVIEW_LINEAGES,
        ttl_seconds: int = SEMANTIC_REVIEW_TTL_SECONDS,
    ) -> None:
        if type(max_lineages) is not int or not 1 <= max_lineages <= MAX_SEMANTIC_REVIEW_LINEAGES:
            raise ValueError("proposal lineage limit is invalid")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86_400:
            raise ValueError("proposal review TTL is invalid")
        self._clock = clock
        self._max_lineages = max_lineages
        self._ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._entries: dict[str, _ProposalReviewEntry] = {}
        self._review_ids: dict[str, str] = {}
        self._transaction_aliases: dict[str, str] = {}
        self._workspace_aliases: dict[str, str] = {}

    @staticmethod
    def _anchor(
        workspace: MultiSegmentWorkspace,
        transaction: SemanticProposalTransaction,
    ) -> str:
        return canonical_fingerprint(
            {
                "schema": "h3.context.semantic_proposal_review_lineage.v1",
                "transaction_id": transaction.transaction_id,
                "attempt": transaction.attempt,
                "workspace_id": workspace.workspace_id,
                "workspace_revision": workspace.revision,
                "initial_workspace_fingerprint": workspace.fingerprint,
                "segment_id": transaction.segment_id,
                "baseline_graph_fingerprint": transaction.baseline_graph_fingerprint,
            }
        )

    @staticmethod
    def _validate_private_bundle(
        bundle: SemanticProposalReviewBundle,
        wiring: NativeH3Wiring,
        correlation: ExecutionCorrelation,
        sidebar: SidebarWorkspaceProjection,
    ) -> None:
        if (
            type(bundle) is not SemanticProposalReviewBundle
            or type(bundle.report) is not ContextReport
            or type(bundle.workspace) is not MultiSegmentWorkspace
            or type(bundle.transaction) is not SemanticProposalTransaction
            or type(wiring) is not NativeH3Wiring
            or type(correlation) is not ExecutionCorrelation
            or type(sidebar) is not SidebarWorkspaceProjection
        ):
            raise SidebarWorkspaceError("proposal_authority", "proposal authority is invalid")
        try:
            # CRITICAL: value-equal report/wiring cannot replace runtime-issued authority.
            assert_native_h3_wiring_authority(wiring, bundle.report)
            verified_transaction = replace(bundle.transaction)
        except Exception:
            raise SidebarWorkspaceError(
                "proposal_authority", "proposal authority is invalid"
            ) from None
        if (
            verified_transaction.fingerprint != bundle.transaction.fingerprint
            or bundle.transaction.workspace_id != bundle.workspace.workspace_id
            or bundle.transaction.workspace_revision != bundle.workspace.revision
            or bundle.transaction.workspace_fingerprint != bundle.workspace.fingerprint
            or sidebar.report_id != bundle.report.report_id
            or sidebar.report_revision != bundle.report.revision
            or sidebar.report_fingerprint != fingerprint_context_report(bundle.report)
            or sidebar.correlation != correlation
        ):
            raise SidebarWorkspaceError("proposal_authority", "proposal authority drifted")

    def _expire(self, now: float) -> None:
        for entry in self._entries.values():
            if (
                entry.active_claim is None
                and entry.publication_claim is None
                and entry.state != "expired"
                and now - entry.touched_at >= self._ttl_seconds
            ):
                entry.state = "expired"
                entry.report = None
                entry.workspace = None
                entry.transaction = None
                entry.terminal_result = None

    def _publish(
        self,
        bundle: SemanticProposalReviewBundle,
        wiring: NativeH3Wiring,
        correlation: ExecutionCorrelation,
        sidebar: SidebarWorkspaceProjection,
        *,
        staged: bool,
    ) -> tuple[SemanticProposalReviewHandle, object | None]:
        self._validate_private_bundle(bundle, wiring, correlation, sidebar)
        now = float(self._clock())
        anchor = self._anchor(bundle.workspace, bundle.transaction)
        with self._lock:
            self._expire(now)
            existing = self._entries.get(anchor)
            if existing is not None:
                if existing.state == "expired":
                    raise SidebarWorkspaceError("proposal_expired", "proposal review expired")
                if existing.publication_claim is not None:
                    raise SidebarWorkspaceError("proposal_busy", "proposal publication is busy")
                if (
                    existing.correlation != correlation
                    or existing.sidebar_report_fingerprint != sidebar.report_fingerprint
                    or existing.transaction is None
                    or existing.workspace is None
                    or existing.transaction.fingerprint != bundle.transaction.fingerprint
                    or existing.workspace.fingerprint != bundle.workspace.fingerprint
                ):
                    raise SidebarWorkspaceError("proposal_identity", "proposal lineage drifted")
                existing.touched_at = now
                return existing.handle, None
            if len(self._entries) >= self._max_lineages:
                raise SidebarWorkspaceError("proposal_capacity", "proposal capacity is exhausted")
            for alias, aliases in (
                (bundle.transaction.fingerprint, self._transaction_aliases),
                (bundle.workspace.fingerprint, self._workspace_aliases),
            ):
                previous = aliases.get(alias)
                if previous is not None and previous != anchor:
                    raise SidebarWorkspaceError("proposal_identity", "proposal alias conflicts")
            review_id = "review_" + secrets.token_urlsafe(32)
            publication_claim = object() if staged else None
            handle = build_semantic_proposal_review_handle(
                review_id,
                bundle.report,
                bundle.workspace,
                bundle.transaction,
                correlation,
            )
            entry = _ProposalReviewEntry(
                anchor=anchor,
                handle=handle,
                report=bundle.report,
                workspace=bundle.workspace,
                transaction=bundle.transaction,
                correlation=correlation,
                sidebar_workspace_id=sidebar.workspace_id,
                sidebar_revision=sidebar.report_revision,
                sidebar_report_fingerprint=sidebar.report_fingerprint,
                transaction_aliases={bundle.transaction.fingerprint},
                workspace_aliases={bundle.workspace.fingerprint},
                touched_at=now,
                publication_identity=publication_claim,
                publication_claim=publication_claim,
            )
            self._entries[anchor] = entry
            self._review_ids[review_id] = anchor
            self._transaction_aliases[bundle.transaction.fingerprint] = anchor
            self._workspace_aliases[bundle.workspace.fingerprint] = anchor
            return handle, publication_claim

    def publish(
        self,
        bundle: SemanticProposalReviewBundle,
        wiring: NativeH3Wiring,
        correlation: ExecutionCorrelation,
        sidebar: SidebarWorkspaceProjection,
    ) -> SemanticProposalReviewHandle:
        handle, _identity = self._publish(
            bundle,
            wiring,
            correlation,
            sidebar,
            staged=False,
        )
        return handle

    def stage(
        self,
        bundle: SemanticProposalReviewBundle,
        wiring: NativeH3Wiring,
        correlation: ExecutionCorrelation,
        sidebar: SidebarWorkspaceProjection,
    ) -> _SemanticProposalReviewPublication:
        handle, identity = self._publish(
            bundle,
            wiring,
            correlation,
            sidebar,
            staged=True,
        )
        return _SemanticProposalReviewPublication(handle, identity)

    def commit_publication(self, publication: object) -> None:
        if type(publication) is not _SemanticProposalReviewPublication:
            raise SidebarWorkspaceError("proposal_authority", "proposal publication is invalid")
        if publication.identity is None:
            return
        with self._lock:
            anchor = self._review_ids.get(publication.handle.review_id)
            entry = self._entries.get(anchor) if anchor is not None else None
            if (
                entry is None
                or entry.handle is not publication.handle
                or entry.publication_identity is not publication.identity
                or entry.publication_claim is not publication.identity
            ):
                raise SidebarWorkspaceError(
                    "proposal_authority", "proposal publication is inactive"
                )
            entry.publication_claim = None

    def rollback(self, publication: object) -> None:
        if type(publication) is not _SemanticProposalReviewPublication:
            raise SidebarWorkspaceError("proposal_authority", "proposal publication is invalid")
        if publication.identity is None:
            return
        with self._lock:
            anchor = self._review_ids.get(publication.handle.review_id)
            entry = self._entries.get(anchor) if anchor is not None else None
            if (
                entry is None
                or entry.handle is not publication.handle
                or entry.publication_identity is not publication.identity
                or entry.active_claim is not None
                or entry.terminal_result is not None
            ):
                raise SidebarWorkspaceError(
                    "proposal_authority", "proposal publication is inactive"
                )
            self._entries.pop(entry.anchor, None)
            self._review_ids.pop(publication.handle.review_id, None)
            for alias in entry.transaction_aliases:
                if self._transaction_aliases.get(alias) == entry.anchor:
                    self._transaction_aliases.pop(alias, None)
            for alias in entry.workspace_aliases:
                if self._workspace_aliases.get(alias) == entry.anchor:
                    self._workspace_aliases.pop(alias, None)

    def _entry(
        self,
        action: dict[str, object],
        sidebar: SidebarWorkspaceProjection,
        now: float,
    ) -> tuple[_ProposalReviewEntry, dict[str, object], str]:
        payload = action["payload"]
        kind = action["action"]
        if type(payload) is not dict or type(kind) is not str:
            raise ValueError("proposal action is invalid")
        review_id = payload.get("review_id")
        if type(review_id) is not str:
            raise ValueError("proposal review identity is invalid")
        anchor = self._review_ids.get(review_id)
        entry = self._entries.get(anchor) if anchor is not None else None
        if entry is None:
            raise KeyError("proposal review is unavailable")
        if entry.publication_claim is not None:
            raise KeyError("proposal review is unavailable")
        if (
            entry.state == "expired"
            or entry.report is None
            or entry.workspace is None
            or entry.transaction is None
        ):
            raise SidebarWorkspaceError("proposal_expired", "proposal review expired")
        if (
            entry.sidebar_workspace_id != sidebar.workspace_id
            or entry.sidebar_revision != sidebar.report_revision
            or entry.sidebar_report_fingerprint != sidebar.report_fingerprint
            or entry.correlation != sidebar.correlation
        ):
            raise SidebarWorkspaceError("proposal_stale", "sidebar authority changed")
        expected_transaction = payload.get("expected_transaction_fingerprint")
        expected_workspace = payload.get("expected_workspace_fingerprint")
        if (
            expected_transaction not in entry.transaction_aliases
            or expected_workspace not in entry.workspace_aliases
        ):
            raise SidebarWorkspaceError("proposal_stale", "proposal authority changed")
        entry.touched_at = now
        return entry, payload, kind

    def dispatch(
        self,
        action: dict[str, object],
        sidebar: SidebarWorkspaceProjection,
    ) -> dict[str, object]:
        now = float(self._clock())
        with self._lock:
            self._expire(now)
            entry, payload, kind = self._entry(action, sidebar, now)
            report = entry.report
            current_workspace = entry.workspace
            current_transaction = entry.transaction
            if report is None or current_workspace is None or current_transaction is None:
                raise SidebarWorkspaceError("proposal_expired", "proposal review expired")
            if kind == "proposal_read":
                review = build_semantic_proposal_review_projection(
                    entry.handle.review_id,
                    report,
                    current_workspace,
                    current_transaction,
                    entry.correlation,
                )
                return build_semantic_proposal_action_result("read", "review_current", review)
            if entry.terminal_result is not None:
                expected_terminal = {
                    "proposal_accept": "accepted",
                    "proposal_reject": "rejected",
                    "proposal_cancel": "cancelled",
                }.get(kind)
                if expected_terminal is not None and entry.terminal_action == kind:
                    return entry.terminal_result
                raise SidebarWorkspaceError("proposal_terminal", "proposal review is terminal")
            if (
                payload.get("expected_transaction_fingerprint") != current_transaction.fingerprint
                or payload.get("expected_workspace_fingerprint") != current_workspace.fingerprint
            ):
                # CRITICAL: historical aliases admit reads/terminal replay only,
                # never a new mutation.
                raise SidebarWorkspaceError("proposal_stale", "proposal authority changed")
            if entry.active_claim is not None:
                raise SidebarWorkspaceError("proposal_busy", "proposal review is busy")
            identity = object()
            entry.active_claim = identity

        try:
            if kind == "proposal_resolve":
                resolutions = payload.get("resolutions")
                if type(resolutions) is not list or len(resolutions) != len(
                    current_transaction.clarification_ids
                ):
                    raise SidebarWorkspaceError(
                        "proposal_resolution", "clarification resolution is incomplete"
                    )
                fingerprints = tuple(
                    canonical_fingerprint(
                        {"clarification_id": clarification_id, "text": resolution}
                    )
                    for clarification_id, resolution in zip(
                        current_transaction.clarification_ids, resolutions, strict=True
                    )
                )
                next_transaction = resolve_semantic_proposal_clarification(
                    current_transaction,
                    expected_transaction_fingerprint=current_transaction.fingerprint,
                    resolution_fingerprints=fingerprints,
                )
                next_workspace = current_workspace
                outcome = "resolved"
                reason = "clarification_resolved"
            elif kind == "proposal_accept":
                applied = accept_semantic_proposal_transaction(
                    current_transaction,
                    expected_transaction_fingerprint=current_transaction.fingerprint,
                    workspace=current_workspace,
                    expected_workspace_fingerprint=current_workspace.fingerprint,
                )
                next_transaction = applied.transaction
                next_workspace = applied.workspace
                outcome = "accepted"
                reason = "proposal_applied"
            elif kind == "proposal_reject":
                next_transaction = reject_semantic_proposal_transaction(
                    current_transaction,
                    expected_transaction_fingerprint=current_transaction.fingerprint,
                )
                next_workspace = current_workspace
                outcome = "rejected"
                reason = "proposal_rejected"
            elif kind == "proposal_cancel":
                next_transaction = cancel_semantic_proposal_transaction(
                    current_transaction,
                    expected_transaction_fingerprint=current_transaction.fingerprint,
                )
                next_workspace = current_workspace
                outcome = "cancelled"
                reason = "proposal_cancelled"
            else:
                raise ValueError("proposal action is unsupported")
            review = build_semantic_proposal_review_projection(
                entry.handle.review_id,
                report,
                next_workspace,
                next_transaction,
                entry.correlation,
            )
            result = build_semantic_proposal_action_result(outcome, reason, review)
        except SidebarWorkspaceError:
            with self._lock:
                if entry.active_claim is identity:
                    entry.active_claim = None
            raise
        except (
            SemanticProposalTransactionError,
            SemanticProposalReviewError,
            TypeError,
            ValueError,
        ):
            with self._lock:
                if entry.active_claim is identity:
                    entry.active_claim = None
            raise SidebarWorkspaceError(
                "proposal_transition", "proposal transition failed"
            ) from None
        except Exception:
            with self._lock:
                if entry.active_claim is identity:
                    entry.active_claim = None
            raise

        with self._lock:
            if (
                entry.active_claim is not identity
                or entry.transaction is not current_transaction
                or entry.workspace is not current_workspace
            ):
                if entry.active_claim is identity:
                    entry.active_claim = None
                raise SidebarWorkspaceError("proposal_stale", "proposal result is stale")
            entry.transaction = next_transaction
            entry.workspace = next_workspace
            entry.active_claim = None
            entry.transaction_aliases.add(next_transaction.fingerprint)
            entry.workspace_aliases.add(next_workspace.fingerprint)
            self._transaction_aliases[next_transaction.fingerprint] = entry.anchor
            self._workspace_aliases[next_workspace.fingerprint] = entry.anchor
            if outcome in {"accepted", "rejected", "cancelled"}:
                entry.state = "terminal"
                entry.terminal_action = kind
                entry.terminal_result = result
            return result


_PROCESS_PROPOSAL_COORDINATION = _ProposalReviewCoordination()


@dataclass(slots=True)
class _WorkspaceEntry:
    report: ContextReport
    base_report: ContextReport
    wiring: NativeH3Wiring | None
    correlation: ExecutionCorrelation
    base_prompt_fingerprint: str
    proposal_reason: str | None
    touched_at: float
    authoring_source_binding: AuthoringSourceBindingReceipt | None
    production_ephemeral: bool = False
    lineage_token: object | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SidebarProductionSeed:
    """Reusable content-free authority derived from one exact live Context workspace."""

    task_mode: TaskMode
    source_id: str
    reference_ids: tuple[str, ...]
    duration: SegmentDuration
    accepted_intent_fingerprint: str
    profile_fingerprint: str
    reference_registry_fingerprint: str
    native_binding_fingerprint: str
    producer_settings_fingerprint: str
    seed_fingerprint: str
    lineage_token: object | None = field(default=None, repr=False, compare=False)
    authoring_seed: SidebarAuthoringSeed | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SidebarAuthoringSource:
    """One content-free admitted source row for the authoring-workspace seed (M20-03)."""

    asset_id: str
    kind: MediaKind
    duration_milliseconds: int | None
    paired_video_id: str | None
    connection_order: int
    identity_fingerprint: str


@dataclass(frozen=True, slots=True)
class SidebarAuthoringSeed:
    """Content-free reference-authoring seed claimed from one exact live Context workspace."""

    source_id: str
    task_mode: TaskMode
    registry_fingerprint: str
    sources: tuple[SidebarAuthoringSource, ...]
    lineage_token: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SidebarAuthoringWorkspaceClaim:
    """Atomic content-free seed plus one opaque process-local source authority."""

    seed: SidebarAuthoringSeed
    source_binding: AuthoringSourceBindingReceipt | None


@dataclass(frozen=True, slots=True)
class AssistedPromptProposalProjection:
    """Content-bounded transient proposal shown to the existing Context audit editor."""

    proposal_id: str
    proposal_revision: int
    state: str
    workspace_id: str
    report_revision: int
    report_fingerprint: str
    prompt_fingerprint: str
    candidate_text: str
    audit: PromptFidelityAuditResult
    receipt: AssistedDraftReceipt

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": "h3.context.assisted_prompt_proposal.v1",
            "proposal_id": self.proposal_id,
            "proposal_revision": self.proposal_revision,
            "state": self.state,
            "workspace_id": self.workspace_id,
            "report_revision": self.report_revision,
            "report_fingerprint": self.report_fingerprint,
            "prompt_fingerprint": self.prompt_fingerprint,
            "candidate_text": self.candidate_text,
            "audit": self.audit.to_wire(),
            "receipt": self.receipt.to_wire(),
        }


@dataclass(slots=True)
class _AssistedPromptProposalEntry:
    proposal_id: str
    proposal_revision: int
    state: str
    workspace_id: str
    report_revision: int
    report_fingerprint: str
    evidence_fingerprint: str
    candidate_text: str
    guarantee: DraftGuarantee
    audit: PromptFidelityAuditResult
    receipt: AssistedDraftReceipt
    session_id: str
    session_generation: str
    authority_epoch: int
    provider_fingerprint: str
    touched_at: float


class SidebarWorkspaceRegistry:
    """Finite process-local holder for exact report/wiring authority; never persisted or logged."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_SIDEBAR_WORKSPACES,
        ttl_seconds: int = SIDEBAR_WORKSPACE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        proposal_coordination: _ProposalReviewCoordination | None = None,
        source_binding_claim: Callable[
            [ReferenceRegistry], AuthoringSourceBindingReceipt | None
        ] = claim_process_authoring_sources,
    ) -> None:
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_SIDEBAR_WORKSPACES:
            raise ValueError("workspace entry limit is invalid")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86_400:
            raise ValueError("workspace TTL is invalid")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, _WorkspaceEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._proposal_coordination = (
            _PROCESS_PROPOSAL_COORDINATION
            if proposal_coordination is None
            else proposal_coordination
        )
        self._assisted_proposals: OrderedDict[str, _AssistedPromptProposalEntry] = OrderedDict()
        self._active_assisted: dict[tuple[str, str], str] = {}
        self._source_binding_claim = source_binding_claim

    @staticmethod
    def _release_entry(entry: _WorkspaceEntry | None) -> None:
        if entry is not None and entry.authoring_source_binding is not None:
            entry.authoring_source_binding.release()
            entry.authoring_source_binding = None

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, value in self._entries.items()
            if now - value.touched_at >= self._ttl_seconds
        ]
        for key in expired:
            self._release_entry(self._entries.pop(key, None))
        while len(self._entries) > self._max_entries:
            _key, entry = self._entries.popitem(last=False)
            self._release_entry(entry)
        expired_proposals = [
            key
            for key, value in self._assisted_proposals.items()
            if now - value.touched_at >= self._ttl_seconds
            or value.workspace_id not in self._entries
        ]
        for key in expired_proposals:
            proposal = self._assisted_proposals.pop(key, None)
            if proposal is not None:
                active_key = (proposal.session_id, proposal.workspace_id)
                if self._active_assisted.get(active_key) == key:
                    self._active_assisted.pop(active_key, None)
        while len(self._assisted_proposals) > self._max_entries:
            key, proposal = self._assisted_proposals.popitem(last=False)
            active_key = (proposal.session_id, proposal.workspace_id)
            if self._active_assisted.get(active_key) == key:
                self._active_assisted.pop(active_key, None)

    def _project(self, workspace_id: str, entry: _WorkspaceEntry) -> SidebarWorkspaceProjection:
        return build_sidebar_workspace_projection(
            entry.report,
            entry.wiring,
            entry.correlation,
            workspace_id=workspace_id,
            base_prompt_fingerprint=entry.base_prompt_fingerprint,
            proposal_reason=entry.proposal_reason,
            base_report=entry.base_report,
        )

    def publish(
        self,
        report: ContextReport,
        wiring: NativeH3Wiring,
        correlation: ExecutionCorrelation,
    ) -> SidebarWorkspaceProjection:
        now = self._clock()
        workspace_id = "ws_" + secrets.token_urlsafe(32)
        source_binding = self._source_binding_claim(report.request.reference_registry)
        if source_binding is not None and not isinstance(
            source_binding, AuthoringSourceBindingReceipt
        ):
            source_binding = None
        entry = _WorkspaceEntry(
            report=report,
            base_report=report,
            wiring=wiring,
            correlation=correlation,
            base_prompt_fingerprint=wiring.prompt_fingerprint,
            proposal_reason=None,
            touched_at=now,
            authoring_source_binding=source_binding,
            lineage_token=object(),
        )
        try:
            projection = self._project(workspace_id, entry)
        except Exception:
            self._release_entry(entry)
            raise
        with self._lock:
            self._prune(now)
            self._entries[workspace_id] = entry
            self._entries.move_to_end(workspace_id)
            self._prune(now)
        return projection

    def publish_production_materialization(
        self,
        report: ContextReport,
        wiring: NativeH3Wiring,
        correlation: ExecutionCorrelation,
    ) -> SidebarWorkspaceProjection:
        """Publish one owned scratch Context without evicting a retained user workspace."""

        now = self._clock()
        with self._lock:
            self._prune(now)
            if len(self._entries) >= self._max_entries:
                raise SidebarWorkspaceError(
                    "production_materialization_capacity",
                    "sidebar workspace capacity is unavailable",
                )
            workspace_id = "ws_" + secrets.token_urlsafe(32)
            entry = _WorkspaceEntry(
                report=report,
                base_report=report,
                wiring=wiring,
                correlation=correlation,
                base_prompt_fingerprint=wiring.prompt_fingerprint,
                proposal_reason=None,
                touched_at=now,
                authoring_source_binding=None,
                production_ephemeral=True,
                lineage_token=object(),
            )
            projection = self._project(workspace_id, entry)
            self._entries[workspace_id] = entry
            self._entries.move_to_end(workspace_id)
            return projection

    def release_production_materialization(
        self,
        workspace_id: str,
        *,
        expected_report_revision: int,
        expected_report_fingerprint: str,
    ) -> None:
        """Release only the exact scratch workspace allocated by the materializer."""

        if type(workspace_id) is not str:
            raise KeyError("workspace is unavailable")
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(workspace_id)
            if entry is None or not entry.production_ephemeral:
                raise KeyError("workspace is unavailable")
            projection = self._project(workspace_id, entry)
            if (
                projection.report_revision != expected_report_revision
                or projection.report_fingerprint != expected_report_fingerprint
            ):
                raise SidebarWorkspaceError(
                    "production_materialization_stale",
                    "materialized workspace changed",
                )
            self._release_entry(self._entries.pop(workspace_id))

    def snapshot_production_planning_source(
        self,
        workspace_id: str,
        *,
        expected_report_revision: int,
        expected_report_fingerprint: str,
    ) -> ProductionPlanningSourceSnapshot:
        """Snapshot an exact validated user workspace without extending its lifetime."""

        if type(workspace_id) is not str:
            raise KeyError("workspace is unavailable")
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(workspace_id)
            if entry is None or entry.production_ephemeral or entry.wiring is None:
                raise KeyError("workspace is unavailable")
            report = entry.report
            report_fingerprint = fingerprint_context_report(report)
            if (
                report.revision != expected_report_revision
                or report_fingerprint != expected_report_fingerprint
            ):
                raise SidebarWorkspaceError("planning_source_stale", "workspace changed")
            return make_production_planning_source_snapshot(
                workspace_id=workspace_id,
                report=report,
                wiring=entry.wiring,
                expires_at_monotonic=entry.touched_at + self._ttl_seconds,
            )

    @contextmanager
    def guard_production_planning_source(
        self,
        snapshot: ProductionPlanningSourceSnapshot,
    ) -> Iterator[None]:
        """Hold exact source currentness across the caller's final publication CAS."""

        if type(snapshot) is not ProductionPlanningSourceSnapshot:
            raise SidebarWorkspaceError("planning_source_invalid", "source snapshot is invalid")
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(snapshot.workspace_id)
            current_expiry = None if entry is None else entry.touched_at + self._ttl_seconds
            if (
                entry is None
                or entry.production_ephemeral
                or entry.wiring is None
                or now >= snapshot.expires_at_monotonic
                or current_expiry is None
                or snapshot.expires_at_monotonic > current_expiry
                or entry.report is not snapshot.report
                or entry.wiring is not snapshot.wiring
            ):
                raise SidebarWorkspaceError("planning_source_stale", "workspace changed")
            current = make_production_planning_source_snapshot(
                workspace_id=snapshot.workspace_id,
                report=entry.report,
                wiring=entry.wiring,
                expires_at_monotonic=snapshot.expires_at_monotonic,
            )
            if current.source_fingerprint != snapshot.source_fingerprint:
                raise SidebarWorkspaceError("planning_source_stale", "workspace changed")
            # CRITICAL: keep this lock held through the caller's final Production CAS. A
            # source edit between this check and publication would authorize stale content.
            yield

    def get(self, workspace_id: str) -> SidebarWorkspaceProjection:
        if type(workspace_id) is not str:
            raise KeyError("workspace is unavailable")
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(workspace_id)
            if entry is None or entry.production_ephemeral:
                raise KeyError("workspace is unavailable")
            entry.touched_at = now
            self._entries.move_to_end(workspace_id)
            return self._project(workspace_id, entry)

    def claim_assisted_source(
        self,
        workspace_id: str,
        expected_revision: int,
        expected_report_fingerprint: str,
        *,
        require_resolved_session: str | None = None,
    ) -> ContextReport:
        """Read the exact current report for one explicit Optimize action."""

        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(workspace_id)
            if entry is None or entry.production_ephemeral:
                raise KeyError("workspace is unavailable")
            projection = self._project(workspace_id, entry)
            if (
                projection.report_revision != expected_revision
                or projection.report_fingerprint != expected_report_fingerprint
            ):
                raise SidebarWorkspaceError("assisted_proposal_stale", "workspace changed")
            if require_resolved_session is not None:
                active_id = self._active_assisted.get((require_resolved_session, workspace_id))
                previous = self._assisted_proposals.get(active_id or "")
                if (
                    previous is not None
                    and previous.state == "active"
                    and previous.report_revision == projection.report_revision
                    and previous.report_fingerprint == projection.report_fingerprint
                ):
                    # IMPORTANT: Refine starts from the current report, never an unresolved
                    # candidate. Check under the report lock before acquiring a provider lease.
                    raise SidebarWorkspaceError("assisted_proposal_unresolved", "resolve proposal")
            return entry.report

    def claim_production_seed(self, workspace_id: str) -> SidebarProductionSeed:
        """Validate and snapshot one exact Context seed without consuming its Sidebar authority."""

        if type(workspace_id) is not str:
            raise KeyError("workspace is unavailable")
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(workspace_id)
            # CRITICAL: the random handle is the Production seed bearer. Tagged scratch rows are
            # valid only through this non-refreshing internal claim; planning snapshots and every
            # generic Sidebar read/action still reject them.
            if entry is None or entry.wiring is None:
                raise KeyError("workspace is unavailable")
            report = entry.report
            if not report.is_successful or not report.validation.is_valid:
                raise KeyError("workspace is unavailable")
            try:
                wiring = assert_native_h3_wiring_authority(entry.wiring, report)
            except NativeH3AdapterError as exc:
                raise KeyError("workspace is unavailable") from exc

            graph_fingerprint = canonical_fingerprint(report.plan.intent_graph.to_wire())
            profile_fingerprint = canonical_fingerprint(report.request.profile.to_wire())
            registry_fingerprint = canonical_fingerprint(
                report.request.reference_registry.to_wire()
            )
            # CRITICAL: never fingerprint NativeH3Wiring.to_wire() here; it contains the prompt.
            native_fingerprint = canonical_fingerprint(
                {
                    "schema": "h3.context.production_seed.native_binding.v1",
                    "report_id": wiring.report_id,
                    "host_version": wiring.host_version,
                    "host_revision": wiring.host_revision,
                    "native_source": wiring.native_source,
                    "native_source_blob": wiring.native_source_blob,
                    "native_node_id": wiring.native_node_id,
                    "task_mode": wiring.task_mode.value,
                    "profile": wiring.profile.to_wire(),
                    "prompt_fingerprint": wiring.prompt_fingerprint,
                    "native_length_frames": wiring.native_length_frames,
                    "native_input_names": list(wiring.native_input_names),
                    "bindings": [binding.to_wire() for binding in wiring.bindings],
                    "validation_status": wiring.validation_status.value,
                    "limitations": list(wiring.limitations),
                }
            )
            settings_fingerprint = canonical_fingerprint(
                {
                    "schema": "h3.context.production_seed.settings.v1",
                    "source": "accepted_context",
                    "duration_locked": True,
                    "raw_media": False,
                }
            )
            safe_seed = {
                "schema": "h3.context.production_seed.v1",
                "task_mode": report.request.task_mode.value,
                "source_id": report.report_id,
                "reference_ids": [
                    asset.asset_id for asset in report.request.reference_registry.assets
                ],
                "frame_count": report.request.effective_frame_count,
                "accepted_intent_fingerprint": graph_fingerprint,
                "profile_fingerprint": profile_fingerprint,
                "reference_registry_fingerprint": registry_fingerprint,
                "native_binding_fingerprint": native_fingerprint,
                "producer_settings_fingerprint": settings_fingerprint,
            }
            # CRITICAL: a Production attempt must not mutate Sidebar lifecycle state.  The reusable
            # snapshot neither consumes the seed nor refreshes its independent Context TTL.
            return SidebarProductionSeed(
                task_mode=report.request.task_mode,
                source_id=report.report_id,
                reference_ids=tuple(
                    asset.asset_id for asset in report.request.reference_registry.assets
                ),
                duration=SegmentDuration.from_frame_count(report.request.effective_frame_count),
                accepted_intent_fingerprint=graph_fingerprint,
                profile_fingerprint=profile_fingerprint,
                reference_registry_fingerprint=registry_fingerprint,
                native_binding_fingerprint=native_fingerprint,
                producer_settings_fingerprint=settings_fingerprint,
                seed_fingerprint=canonical_fingerprint(safe_seed),
                lineage_token=entry.lineage_token,
                authoring_seed=self._build_authoring_seed(entry),
            )

    def claim_production_materialization_seed(
        self,
        workspace_id: str,
        *,
        expected_report_revision: int,
        expected_report_fingerprint: str,
    ) -> SidebarProductionSeed:
        """Claim one exact tagged scratch seed without refreshing its lifetime."""

        if type(workspace_id) is not str:
            raise KeyError("workspace is unavailable")
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(workspace_id)
            if entry is None or not entry.production_ephemeral:
                raise KeyError("workspace is unavailable")
            projection = self._project(workspace_id, entry)
            if (
                projection.report_revision != expected_report_revision
                or projection.report_fingerprint != expected_report_fingerprint
            ):
                raise KeyError("workspace is unavailable")
            # CRITICAL: keep the exact check and seed derivation under one lock. Releasing the
            # lock between them permits scratch release/replacement to authorize the wrong child.
            return self.claim_production_seed(workspace_id)

    def claim_authoring_seed(self, workspace_id: str) -> SidebarAuthoringSeed:
        """Snapshot the accepted reference registry for authoring, content-free, non-consuming.

        Unlike the Production seed this does not require compiled native wiring: reference
        authoring happens before a queue exists.  It still requires the exact successful,
        valid report, and it neither consumes the workspace nor refreshes its TTL.
        """

        if type(workspace_id) is not str:
            raise KeyError("workspace is unavailable")
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(workspace_id)
            if entry is None or entry.production_ephemeral:
                raise KeyError("workspace is unavailable")
            return self._build_authoring_seed(entry)

    @staticmethod
    def _build_authoring_seed(entry: _WorkspaceEntry) -> SidebarAuthoringSeed:
        report = entry.report
        if not report.is_successful or not report.validation.is_valid:
            raise KeyError("workspace is unavailable")
        registry = report.request.reference_registry
        rows: list[SidebarAuthoringSource] = []
        for asset in registry.assets:
            duration_milliseconds: int | None = None
            metadata = asset.metadata
            if metadata is not None and metadata.duration_seconds is not None:
                duration_milliseconds = int(metadata.duration_seconds * 1000)
            elif entry.authoring_source_binding is not None:
                # IMPORTANT: metadata-light live VIDEO may use only the exact receipt's bounded
                # probe. Reconstructing duration from a registry fingerprint can admit a wrong file.
                duration_milliseconds = authoring_source_duration_milliseconds(
                    entry.authoring_source_binding,
                    asset.asset_id,
                )
            rows.append(
                SidebarAuthoringSource(
                    asset_id=asset.asset_id,
                    kind=asset.kind,
                    duration_milliseconds=duration_milliseconds,
                    paired_video_id=asset.paired_video_id,
                    connection_order=asset.connection_order,
                    identity_fingerprint=canonical_fingerprint(asset.to_wire()),
                )
            )
        rows.sort(key=lambda row: row.connection_order)
        return SidebarAuthoringSeed(
            source_id=report.report_id,
            task_mode=report.request.task_mode,
            registry_fingerprint=canonical_fingerprint(registry.to_wire()),
            sources=tuple(rows),
            lineage_token=getattr(entry, "lineage_token", None),
        )

    def claim_authoring_workspace(self, workspace_id: str) -> SidebarAuthoringWorkspaceClaim:
        """Atomically transfer the exact pending source authority with its content-free seed."""

        if type(workspace_id) is not str:
            raise KeyError("workspace is unavailable")
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(workspace_id)
            if entry is None or entry.production_ephemeral:
                raise KeyError("workspace is unavailable")
            seed = self._build_authoring_seed(entry)
            binding = entry.authoring_source_binding
            # IMPORTANT: transfer is one-time and atomic with the seed snapshot. Copying this
            # reference would leave two lifecycle owners and can expose stale source authority.
            entry.authoring_source_binding = None
            return SidebarAuthoringWorkspaceClaim(seed=seed, source_binding=binding)

    def publish_semantic_proposal_review(
        self,
        bundle: SemanticProposalReviewBundle,
        wiring: NativeH3Wiring,
        correlation: ExecutionCorrelation,
        sidebar: SidebarWorkspaceProjection,
    ) -> SemanticProposalReviewHandle:
        """Stage one private review entry before its predecessor claim is consumed."""

        return self._proposal_coordination.publish(bundle, wiring, correlation, sidebar)

    @staticmethod
    def _assisted_authority_valid(
        proposal: _AssistedPromptProposalEntry,
        authority: ProviderAuthorityClaim,
    ) -> bool:
        return authority.matches(
            proposal.session_id,
            proposal.session_generation,
            proposal.authority_epoch,
        )

    @staticmethod
    def _validate_assisted_candidate(
        report: ContextReport,
        prompt_text: str,
        guarantee: DraftGuarantee,
    ) -> tuple[PromptFidelityAuditResult, ContextReport]:
        if type(prompt_text) is not str:
            raise SidebarWorkspaceError("assisted_proposal_invalid", "candidate is invalid")
        if reference_inventory(prompt_text) != reference_inventory(report.prompt_document.text):
            raise SidebarWorkspaceError(
                "assisted_proposal_preservation", "reference inventory changed"
            )
        # SECURITY: revision instructions cannot relax server-owned exact spans, even if the
        # prose audit passes. Keep this deterministic check before candidate staging.
        if any(span not in prompt_text for span in guarantee.preserved_spans):
            raise SidebarWorkspaceError("assisted_proposal_preservation", "exact user text changed")
        audit = audit_prompt_fidelity(
            report.plan,
            replace(report.prompt_document, text=prompt_text),
        )
        if audit.prose_blocking:
            raise SidebarWorkspaceError("assisted_proposal_audit", "candidate audit failed")
        staged = stage_audit_override(
            report,
            expected_revision=report.revision,
            expected_report_fingerprint=fingerprint_context_report(report),
            reason="Accept assisted prompt proposal",
            prompt_text=prompt_text,
        )
        return audit, staged

    def publish_assisted_proposal(
        self,
        *,
        workspace_id: str,
        expected_revision: int,
        expected_report_fingerprint: str,
        execution: AssistedDraftExecutionResult,
        guarantee: DraftGuarantee,
        lease: ProviderSessionExecutionLease,
        authority: ProviderAuthorityClaim,
    ) -> AssistedPromptProposalProjection:
        if (
            not isinstance(execution, AssistedDraftExecutionResult)
            or not execution.usable
            or execution.draft is None
            or execution.receipt is None
            or not isinstance(guarantee, DraftGuarantee)
            or not isinstance(lease, ProviderSessionExecutionLease)
            or not isinstance(authority, ProviderAuthorityClaim)
        ):
            raise SidebarWorkspaceError("assisted_proposal_invalid", "proposal is unavailable")
        now = self._clock()
        with self._lock:
            self._prune(now)
            workspace = self._entries.get(workspace_id)
            if workspace is None:
                raise KeyError("workspace is unavailable")
            current = self._project(workspace_id, workspace)
            if (
                current.report_revision != expected_revision
                or current.report_fingerprint != expected_report_fingerprint
            ):
                raise SidebarWorkspaceError("assisted_proposal_stale", "workspace changed")
            receipt = execution.receipt
            evidence_fingerprint = canonical_fingerprint(workspace.report.evidence.to_wire())
            if (
                receipt.evidence_fingerprint != evidence_fingerprint
                or receipt.provider_revision != lease.snapshot.provider_revision
                or receipt.profile_id != lease.snapshot.profile.profile_id
                or receipt.provider_family != lease.snapshot.profile.family.value
                or receipt.model_id != lease.snapshot.selected_model_id
            ):
                # SECURITY: a usable draft is not publication evidence unless its receipt binds
                # the exact report and provider snapshot held by this transaction.
                raise SidebarWorkspaceError(
                    "assisted_proposal_receipt", "execution receipt does not match authority"
                )
            if not authority.matches_lease(lease):
                raise SidebarWorkspaceError("assisted_proposal_authority", "provider changed")
            audit, _staged = self._validate_assisted_candidate(
                workspace.report,
                execution.draft.candidate_text,
                guarantee,
            )
            active_key = (lease.session_id, workspace_id)
            previous_id = self._active_assisted.get(active_key)
            previous = self._assisted_proposals.get(previous_id or "")
            if previous is not None and previous.state == "active":
                previous.state = "superseded"
                previous.proposal_revision += 1
                previous.touched_at = now
            proposal_id = "assist_" + secrets.token_urlsafe(24)
            entry = _AssistedPromptProposalEntry(
                proposal_id=proposal_id,
                proposal_revision=1,
                state="active",
                workspace_id=workspace_id,
                report_revision=current.report_revision,
                report_fingerprint=current.report_fingerprint,
                evidence_fingerprint=evidence_fingerprint,
                candidate_text=execution.draft.candidate_text,
                guarantee=guarantee,
                audit=audit,
                receipt=execution.receipt,
                session_id=lease.session_id,
                session_generation=lease.session_generation,
                authority_epoch=lease.snapshot.authority_epoch,
                provider_fingerprint=canonical_fingerprint(
                    {
                        "profile_id": lease.snapshot.profile.profile_id,
                        "model_id": lease.snapshot.selected_model_id,
                        "provider_revision": lease.snapshot.provider_revision,
                    }
                ),
                touched_at=now,
            )
            self._assisted_proposals[proposal_id] = entry
            self._active_assisted[active_key] = proposal_id
            self._assisted_proposals.move_to_end(proposal_id)
            self._prune(now)
            return self._project_assisted(entry)

    @staticmethod
    def _project_assisted(
        entry: _AssistedPromptProposalEntry,
    ) -> AssistedPromptProposalProjection:
        return AssistedPromptProposalProjection(
            proposal_id=entry.proposal_id,
            proposal_revision=entry.proposal_revision,
            state=entry.state,
            workspace_id=entry.workspace_id,
            report_revision=entry.report_revision,
            report_fingerprint=entry.report_fingerprint,
            prompt_fingerprint=canonical_fingerprint(entry.candidate_text),
            candidate_text=entry.candidate_text,
            audit=entry.audit,
            receipt=entry.receipt,
        )

    def _active_assisted_entry(
        self,
        proposal_id: str,
        expected_workspace_id: str,
        expected_proposal_revision: int,
        authority: ProviderAuthorityClaim,
    ) -> tuple[_AssistedPromptProposalEntry, _WorkspaceEntry]:
        proposal = self._assisted_proposals.get(proposal_id)
        if proposal is None or proposal.state != "active":
            raise SidebarWorkspaceError("assisted_proposal_terminal", "proposal is unavailable")
        if proposal.workspace_id != expected_workspace_id:
            # SECURITY: a proposal handle cannot borrow a different current workspace root.
            raise SidebarWorkspaceError(
                "assisted_proposal_identity", "proposal belongs to another workspace"
            )
        if proposal.proposal_revision != expected_proposal_revision:
            raise SidebarWorkspaceError("assisted_proposal_stale", "proposal changed")
        if not self._assisted_authority_valid(proposal, authority):
            raise SidebarWorkspaceError("assisted_proposal_authority", "provider changed")
        workspace = self._entries.get(proposal.workspace_id)
        if workspace is None:
            raise SidebarWorkspaceError("assisted_proposal_stale", "workspace is unavailable")
        current = self._project(proposal.workspace_id, workspace)
        if (
            current.report_revision != proposal.report_revision
            or current.report_fingerprint != proposal.report_fingerprint
            or canonical_fingerprint(workspace.report.evidence.to_wire())
            != proposal.evidence_fingerprint
        ):
            raise SidebarWorkspaceError("assisted_proposal_stale", "workspace changed")
        return proposal, workspace

    def assisted_proposal_authority(
        self,
        proposal_id: str,
        *,
        expected_workspace_id: str,
        expected_proposal_revision: int,
        session_id: str,
    ) -> tuple[str, int]:
        """Snapshot generation/epoch before the caller acquires provider authority."""

        now = self._clock()
        with self._lock:
            self._prune(now)
            proposal = self._assisted_proposals.get(proposal_id)
            if proposal is None or proposal.state != "active":
                raise SidebarWorkspaceError("assisted_proposal_terminal", "proposal is unavailable")
            if proposal.workspace_id != expected_workspace_id or proposal.session_id != session_id:
                raise SidebarWorkspaceError(
                    "assisted_proposal_identity", "proposal authority is unrelated"
                )
            if proposal.proposal_revision != expected_proposal_revision:
                raise SidebarWorkspaceError("assisted_proposal_stale", "proposal changed")
            workspace = self._entries.get(proposal.workspace_id)
            if workspace is None:
                raise SidebarWorkspaceError("assisted_proposal_stale", "workspace is unavailable")
            current = self._project(proposal.workspace_id, workspace)
            if (
                current.report_revision != proposal.report_revision
                or current.report_fingerprint != proposal.report_fingerprint
                or canonical_fingerprint(workspace.report.evidence.to_wire())
                != proposal.evidence_fingerprint
            ):
                raise SidebarWorkspaceError("assisted_proposal_stale", "workspace changed")
            return proposal.session_generation, proposal.authority_epoch

    def edit_assisted_proposal(
        self,
        proposal_id: str,
        *,
        expected_workspace_id: str,
        expected_proposal_revision: int,
        prompt_text: str,
        authority: ProviderAuthorityClaim,
    ) -> AssistedPromptProposalProjection:
        now = self._clock()
        with self._lock:
            self._prune(now)
            proposal, workspace = self._active_assisted_entry(
                proposal_id,
                expected_workspace_id,
                expected_proposal_revision,
                authority,
            )
            audit, _staged = self._validate_assisted_candidate(
                workspace.report, prompt_text, proposal.guarantee
            )
            proposal.candidate_text = prompt_text
            proposal.audit = audit
            proposal.proposal_revision += 1
            proposal.touched_at = now
            self._assisted_proposals.move_to_end(proposal_id)
            return self._project_assisted(proposal)

    def reject_assisted_proposal(
        self,
        proposal_id: str,
        *,
        expected_workspace_id: str,
        expected_proposal_revision: int,
        authority: ProviderAuthorityClaim,
    ) -> AssistedPromptProposalProjection:
        now = self._clock()
        with self._lock:
            self._prune(now)
            proposal, _workspace = self._active_assisted_entry(
                proposal_id,
                expected_workspace_id,
                expected_proposal_revision,
                authority,
            )
            proposal.state = "rejected"
            proposal.proposal_revision += 1
            proposal.touched_at = now
            self._active_assisted.pop((proposal.session_id, proposal.workspace_id), None)
            return self._project_assisted(proposal)

    def cancel_assisted_proposal(
        self,
        proposal_id: str,
        *,
        expected_workspace_id: str,
        expected_proposal_revision: int,
        authority: ProviderAuthorityClaim,
    ) -> AssistedPromptProposalProjection:
        now = self._clock()
        with self._lock:
            self._prune(now)
            proposal, _workspace = self._active_assisted_entry(
                proposal_id,
                expected_workspace_id,
                expected_proposal_revision,
                authority,
            )
            proposal.state = "cancelled"
            proposal.proposal_revision += 1
            proposal.touched_at = now
            self._active_assisted.pop((proposal.session_id, proposal.workspace_id), None)
            return self._project_assisted(proposal)

    def accept_assisted_proposal(
        self,
        proposal_id: str,
        *,
        expected_workspace_id: str,
        expected_proposal_revision: int,
        authority: ProviderAuthorityClaim,
    ) -> SidebarWorkspaceProjection:
        now = self._clock()
        with self._lock:
            self._prune(now)
            proposal, workspace = self._active_assisted_entry(
                proposal_id,
                expected_workspace_id,
                expected_proposal_revision,
                authority,
            )
            # CRITICAL: validate and stage from the exact report held under the workspace lock.
            _audit, staged = self._validate_assisted_candidate(
                workspace.report,
                proposal.candidate_text,
                proposal.guarantee,
            )
            candidate = _WorkspaceEntry(
                report=staged,
                base_report=workspace.base_report,
                wiring=None,
                correlation=workspace.correlation,
                base_prompt_fingerprint=workspace.base_prompt_fingerprint,
                proposal_reason="Accept assisted prompt proposal",
                touched_at=now,
                authoring_source_binding=workspace.authoring_source_binding,
                production_ephemeral=workspace.production_ephemeral,
                lineage_token=workspace.lineage_token,
            )
            result = self._project(proposal.workspace_id, candidate)
            self._entries[proposal.workspace_id] = candidate
            self._entries.move_to_end(proposal.workspace_id)
            proposal.state = "accepted"
            proposal.proposal_revision += 1
            proposal.touched_at = now
            self._active_assisted.pop((proposal.session_id, proposal.workspace_id), None)
            return result

    def stage_semantic_proposal_review(
        self,
        bundle: SemanticProposalReviewBundle,
        wiring: NativeH3Wiring,
        correlation: ExecutionCorrelation,
        sidebar: SidebarWorkspaceProjection,
    ) -> _SemanticProposalReviewPublication:
        """Reserve a rollback-capable entry before the predecessor claim commits."""

        return self._proposal_coordination.stage(bundle, wiring, correlation, sidebar)

    def commit_semantic_proposal_review(self, publication: object) -> None:
        self._proposal_coordination.commit_publication(publication)

    def rollback_semantic_proposal_review(self, publication: object) -> None:
        """Remove only the exact entry staged by this publication."""

        self._proposal_coordination.rollback(publication)

    def dispatch(self, action_value: object) -> SidebarWorkspaceProjection | dict[str, object]:
        action = _validate_action(action_value)
        workspace_id = action["workspace_id"]
        if type(workspace_id) is not str:
            raise ValueError("workspace identity must be an exact string")
        now = self._clock()
        if action["action"] in _PROPOSAL_ACTIONS:
            with self._lock:
                self._prune(now)
                entry = self._entries.get(workspace_id)
                if entry is None:
                    raise KeyError("workspace is unavailable")
                if entry.production_ephemeral:
                    # CRITICAL: Production materializations are immutable bearer-scoped scratch
                    # authority. Generic Sidebar reads/actions would refresh or rewrite the exact
                    # Context consumed by a held managed prepare.
                    raise KeyError("workspace is unavailable")
                projection = self._project(workspace_id, entry)
                if (
                    action["expected_revision"] != projection.report_revision
                    or action["expected_report_fingerprint"] != projection.report_fingerprint
                ):
                    raise SidebarWorkspaceError("stale_action", "workspace identity changed")
            # IMPORTANT: do not retain the workspace lock across the proposal transition;
            # the process-wide opaque claim must be observable by concurrent callers.
            return self._proposal_coordination.dispatch(action, projection)
        with self._lock:
            self._prune(now)
            entry = self._entries.get(workspace_id)
            if entry is None:
                raise KeyError("workspace is unavailable")
            if entry.production_ephemeral:
                raise KeyError("workspace is unavailable")
            projection = self._project(workspace_id, entry)
            if (
                action["expected_revision"] != projection.report_revision
                or action["expected_report_fingerprint"] != projection.report_fingerprint
            ):
                raise SidebarWorkspaceError("stale_action", "workspace identity changed")
            kind = action["action"]
            payload = action["payload"]
            if type(payload) is not dict:
                raise ValueError("action payload must be an exact object")
            if kind == "export":
                entry.touched_at = now
                self._entries.move_to_end(workspace_id)
                return build_sidebar_transfer(projection)
            if kind == "validate":
                report, wiring = validate_sidebar_revision(
                    entry.report,
                    expected_revision=projection.report_revision,
                    expected_report_fingerprint=projection.report_fingerprint,
                )
                candidate = _WorkspaceEntry(
                    report=report,
                    base_report=entry.base_report,
                    wiring=wiring,
                    correlation=entry.correlation,
                    base_prompt_fingerprint=entry.base_prompt_fingerprint,
                    proposal_reason=entry.proposal_reason,
                    touched_at=now,
                    authoring_source_binding=entry.authoring_source_binding,
                    production_ephemeral=entry.production_ephemeral,
                    lineage_token=entry.lineage_token,
                )
            else:
                if kind == "stage_prompt":
                    reason = payload["reason"]
                    prompt_text = payload["prompt_text"]
                else:
                    transfer_json = payload["transfer_json"]
                    if type(transfer_json) is not str:
                        raise ValueError("import transfer must be an exact string")
                    try:
                        transfer_bytes = transfer_json.encode("utf-8", errors="strict")
                    except UnicodeEncodeError as exc:
                        raise ValueError("import transfer is not valid UTF-8") from exc
                    transfer = decode_sidebar_transfer_json(transfer_bytes)
                    if (
                        transfer["task_mode"] != projection.task_mode
                        or transfer["profile"] != projection.profile
                    ):
                        raise SidebarWorkspaceError(
                            "import_conflict", "transfer mode/profile conflicts"
                        )
                    reason = "Import validated prompt transfer"
                    prompt_text = transfer["prompt_text"]
                if type(reason) is not str or type(prompt_text) is not str:
                    raise ValueError("staged prompt fields must be exact strings")
                report = stage_audit_override(
                    entry.report,
                    expected_revision=projection.report_revision,
                    expected_report_fingerprint=projection.report_fingerprint,
                    reason=reason,
                    prompt_text=prompt_text,
                )
                candidate = _WorkspaceEntry(
                    report=report,
                    base_report=entry.base_report,
                    wiring=None,
                    correlation=entry.correlation,
                    base_prompt_fingerprint=entry.base_prompt_fingerprint,
                    proposal_reason=reason,
                    touched_at=now,
                    authoring_source_binding=entry.authoring_source_binding,
                    production_ephemeral=entry.production_ephemeral,
                    lineage_token=entry.lineage_token,
                )
            result = self._project(workspace_id, candidate)
            # Commit only after the complete next projection validates and serializes.
            self._entries[workspace_id] = candidate
            self._entries.move_to_end(workspace_id)
            return result


def build_registry() -> SidebarWorkspaceRegistry:
    """Construct this adapter's process registry. Called only by the composition root."""

    return SidebarWorkspaceRegistry()


_registry = component(SIDEBAR_WORKSPACE, SidebarWorkspaceRegistry)


def publish_sidebar_workspace(
    report: ContextReport,
    wiring: NativeH3Wiring,
    correlation: ExecutionCorrelation,
) -> SidebarWorkspaceProjection:
    return _registry().publish(report, wiring, correlation)


def publish_production_materialization(
    report: ContextReport,
    wiring: NativeH3Wiring,
    correlation: ExecutionCorrelation,
) -> SidebarWorkspaceProjection:
    return _registry().publish_production_materialization(report, wiring, correlation)


def release_production_materialization(
    workspace_id: str,
    *,
    expected_report_revision: int,
    expected_report_fingerprint: str,
) -> None:
    _registry().release_production_materialization(
        workspace_id,
        expected_report_revision=expected_report_revision,
        expected_report_fingerprint=expected_report_fingerprint,
    )


def snapshot_production_planning_source(
    workspace_id: str,
    *,
    expected_report_revision: int,
    expected_report_fingerprint: str,
) -> ProductionPlanningSourceSnapshot:
    return _registry().snapshot_production_planning_source(
        workspace_id,
        expected_report_revision=expected_report_revision,
        expected_report_fingerprint=expected_report_fingerprint,
    )


@contextmanager
def guard_production_planning_source(
    snapshot: ProductionPlanningSourceSnapshot,
) -> Iterator[None]:
    with _registry().guard_production_planning_source(snapshot):
        yield


def stage_semantic_proposal_review(
    bundle: SemanticProposalReviewBundle,
    wiring: NativeH3Wiring,
    correlation: ExecutionCorrelation,
    sidebar: SidebarWorkspaceProjection,
) -> _SemanticProposalReviewPublication:
    return _registry().stage_semantic_proposal_review(bundle, wiring, correlation, sidebar)


def commit_semantic_proposal_review(publication: object) -> None:
    _registry().commit_semantic_proposal_review(publication)


def rollback_semantic_proposal_review(publication: object) -> None:
    _registry().rollback_semantic_proposal_review(publication)


def dispatch_sidebar_action(action: object) -> SidebarWorkspaceProjection | dict[str, object]:
    return _registry().dispatch(action)


async def dispatch_assisted_sidebar_action(
    action_value: object,
    session_id: str,
    *,
    workspace_registry: SidebarWorkspaceRegistry | None = None,
    provider_registry: ProviderSettingsSessionRegistry | None = None,
    exchange_factory: Callable[[object], object] | None = None,
) -> dict[str, object] | SidebarWorkspaceProjection:
    """Execute only the closed assisted actions under exact provider/workspace authority."""

    workspace_registry = _registry() if workspace_registry is None else workspace_registry
    action = _validate_action(action_value)
    kind = action["action"]
    if kind not in _ASSISTED_ACTIONS:
        raise ValueError("action is not assisted")
    providers = provider_settings_registry() if provider_registry is None else provider_registry
    workspace_id = action["workspace_id"]
    revision = action["expected_revision"]
    report_fingerprint = action["expected_report_fingerprint"]
    if (
        type(workspace_id) is not str
        or type(revision) is not int
        or type(report_fingerprint) is not str
    ):
        raise ValueError("assisted action identity is invalid")
    if kind == "cancel_assisted_execution":
        # SECURITY: a valid provider session cannot be signalled through a foreign or stale
        # workspace root.
        workspace_registry.claim_assisted_source(workspace_id, revision, report_fingerprint)
        cancelled = providers.cancel_assisted_execution(session_id)
        return {
            "schema": "h3.context.assisted_sidebar_result.v1",
            "state": "cancelled" if cancelled else "idle",
            "proposal": None,
        }

    payload = action["payload"]
    if type(payload) is not dict:
        raise ValueError("assisted action payload is invalid")
    if kind in {"optimize_prompt", "refine_prompt"}:
        report = workspace_registry.claim_assisted_source(
            workspace_id,
            revision,
            report_fingerprint,
            require_resolved_session=session_id if kind == "refine_prompt" else None,
        )
        decision = providers.begin_assisted_execution(session_id)
        if decision.lease is None:
            return {
                "schema": "h3.context.assisted_sidebar_result.v1",
                "state": "failed",
                "execution": AssistedDraftExecutionResult(outcome=decision.outcome).to_wire(),
                "proposal": None,
            }
        lease = decision.lease
        guarantee = build_assisted_guarantee(report)
        try:
            execution = await asyncio.to_thread(
                run_assisted_draft_orchestration,
                profile=lease.snapshot.profile,
                model_choice=lease.snapshot.model,
                plan=report.plan,
                template=report.prompt_document,
                guarantee=guarantee,
                instruction=build_assisted_instruction(
                    report,
                    guarantee,
                    revision_instruction=payload["instruction"]
                    if kind == "refine_prompt"
                    else None,
                ),
                evidence_fingerprint=canonical_fingerprint(report.evidence.to_wire()),
                provider_revision=lease.snapshot.provider_revision,
                session_generation=lease.session_generation,
                authority_epoch=lease.snapshot.authority_epoch,
                model_factory=lambda: build_assisted_draft_binding(
                    lease,
                    report,
                    exchange_factory=exchange_factory,
                ),
                cancellation=lease.cancelled,
            )
            proposal = None
            if execution.usable:
                try:
                    proposal = providers.run_under_assisted_authority(
                        lease.session_id,
                        lease.session_generation,
                        lease.snapshot.authority_epoch,
                        lambda authority: workspace_registry.publish_assisted_proposal(
                            workspace_id=workspace_id,
                            expected_revision=revision,
                            expected_report_fingerprint=report_fingerprint,
                            execution=execution,
                            guarantee=guarantee,
                            lease=lease,
                            authority=authority,
                        ),
                        lease=lease,
                    )
                except ProviderSettingsSessionError:
                    raise SidebarWorkspaceError(
                        "assisted_proposal_authority", "provider changed"
                    ) from None
            return {
                "schema": "h3.context.assisted_sidebar_result.v1",
                "state": "proposal" if proposal is not None else "failed",
                "execution": execution.to_wire(),
                "proposal": None if proposal is None else proposal.to_wire(),
            }
        finally:
            providers.finish_assisted_execution(lease)

    # The root identity must still name the current workspace before proposal mutation.
    workspace_registry.claim_assisted_source(workspace_id, revision, report_fingerprint)
    proposal_id = payload["proposal_id"]
    proposal_revision = payload["expected_proposal_revision"]
    if type(proposal_id) is not str or type(proposal_revision) is not int:
        raise ValueError("assisted proposal identity is invalid")
    generation, authority_epoch = workspace_registry.assisted_proposal_authority(
        proposal_id,
        expected_workspace_id=workspace_id,
        expected_proposal_revision=proposal_revision,
        session_id=session_id,
    )

    def under_authority(
        operation: Callable[[ProviderAuthorityClaim], object],
    ) -> object:
        try:
            return providers.run_under_assisted_authority(
                session_id,
                generation,
                authority_epoch,
                operation,
            )
        except ProviderSettingsSessionError:
            raise SidebarWorkspaceError("assisted_proposal_authority", "provider changed") from None

    if kind == "edit_assisted_proposal":
        result = under_authority(
            lambda authority: workspace_registry.edit_assisted_proposal(
                proposal_id,
                expected_workspace_id=workspace_id,
                expected_proposal_revision=proposal_revision,
                prompt_text=payload["prompt_text"],
                authority=authority,
            )
        )
        if not isinstance(result, AssistedPromptProposalProjection):
            raise RuntimeError("assisted proposal projection is invalid")
        return result.to_wire()
    if kind == "accept_assisted_proposal":
        accepted = under_authority(
            lambda authority: workspace_registry.accept_assisted_proposal(
                proposal_id,
                expected_workspace_id=workspace_id,
                expected_proposal_revision=proposal_revision,
                authority=authority,
            )
        )
        if not isinstance(accepted, SidebarWorkspaceProjection):
            raise RuntimeError("assisted workspace projection is invalid")
        return accepted
    if kind == "reject_assisted_proposal":
        rejected = under_authority(
            lambda authority: workspace_registry.reject_assisted_proposal(
                proposal_id,
                expected_workspace_id=workspace_id,
                expected_proposal_revision=proposal_revision,
                authority=authority,
            )
        )
        if not isinstance(rejected, AssistedPromptProposalProjection):
            raise RuntimeError("assisted proposal projection is invalid")
        return rejected.to_wire()
    raise ValueError("assisted action is unsupported")


def claim_sidebar_production_seed(workspace_id: str) -> SidebarProductionSeed:
    return _registry().claim_production_seed(workspace_id)


def claim_sidebar_production_materialization_seed(
    workspace_id: str,
    *,
    expected_report_revision: int,
    expected_report_fingerprint: str,
) -> SidebarProductionSeed:
    return _registry().claim_production_materialization_seed(
        workspace_id,
        expected_report_revision=expected_report_revision,
        expected_report_fingerprint=expected_report_fingerprint,
    )


def claim_sidebar_authoring_seed(workspace_id: str) -> SidebarAuthoringSeed:
    return _registry().claim_authoring_seed(workspace_id)


def claim_sidebar_authoring_workspace(workspace_id: str) -> SidebarAuthoringWorkspaceClaim:
    return _registry().claim_authoring_workspace(workspace_id)


_ACTION_POLICY = RoutePolicy(
    path=SIDEBAR_ACTION_ROUTE,
    owner=SIDEBAR_ACTION_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    max_bytes=MAX_SIDEBAR_ACTION_BYTES,
    # CRITICAL: `ProviderSettingsSessionError` is deliberately absent here and present on the
    # assisted policy below. Only the assisted route carries provider session authority; on this
    # route a session refusal has always reached the generic decode branch and answered 400
    # `invalid_request`, and naming it here would silently turn that into a 410.
    refusals=(KeyError, SidebarWorkspaceError),
)

_ASSISTED_POLICY = RoutePolicy(
    path=ASSISTED_SIDEBAR_ACTION_ROUTE,
    owner=SIDEBAR_ACTION_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    max_bytes=MAX_SIDEBAR_ACTION_BYTES,
    refusals=(KeyError, ProviderSettingsSessionError, SidebarWorkspaceError),
)

_ACTION_CONFLICT_CODES = frozenset(
    {
        "proposal_stale",
        "proposal_busy",
        "proposal_terminal",
        "proposal_capacity",
        "proposal_identity",
    }
)


def _refusal_body(status: int, reason: str) -> dict[str, str]:
    # Both routes have always answered an unsupported media type and a generic decode failure with
    # the same opaque code, which says nothing about the body that produced it.
    return {"error": "invalid_request" if status in (400, 415) else reason}


def _session_prelude(request: object) -> object:
    """Settle session ownership before the assisted route materializes a private body."""

    try:
        return provider_session_id_from_request(request)
    except ProviderSettingsSessionError as exc:
        return RouteResult(400, {"error": exc.code})


def _projection_wire(result: object) -> object:
    return result.to_wire() if isinstance(result, SidebarWorkspaceProjection) else result


def _refuse_action(error: BaseException) -> RouteResult:
    if isinstance(error, KeyError):
        return RouteResult(404, {"error": "workspace_unavailable"})
    code = getattr(error, "code", "invalid_request")
    if code == "proposal_expired":
        status = 404
    elif code in _ACTION_CONFLICT_CODES or code.startswith("stale"):
        status = 409
    else:
        status = 400
    return RouteResult(status, {"error": code})


def _refuse_assisted(error: BaseException) -> RouteResult:
    if isinstance(error, KeyError):
        return RouteResult(404, {"error": "workspace_unavailable"})
    if isinstance(error, ProviderSettingsSessionError):
        return RouteResult(409 if error.code == "action_in_flight" else 410, {"error": error.code})
    code = getattr(error, "code", "invalid_request")
    return RouteResult(409 if "stale" in code or "terminal" in code else 400, {"error": code})


def _dispatch_body(payload: bytes) -> RouteResult:
    action = decode_sidebar_action_json(payload)
    if action["action"] in _ASSISTED_ACTIONS:
        return RouteResult(400, {"error": "assisted_route_required"})
    return RouteResult(200, _projection_wire(dispatch_sidebar_action(action)))


async def _act(payload: bytes, _context: object) -> RouteResult:
    return await offload_route_handler("sidebar", lambda: _dispatch_body(payload))


async def _act_assisted(payload: bytes, context: object) -> RouteResult:
    action = decode_sidebar_action_json(payload)
    if action["action"] not in _ASSISTED_ACTIONS:
        return RouteResult(400, {"error": "invalid_request"})
    result = await dispatch_assisted_sidebar_action(action, str(context))
    return RouteResult(200, _projection_wire(result))


def ensure_sidebar_route_registered() -> bool:
    """Register the route while keeping host imports out of package requirements."""

    global _ROUTE_REGISTERED
    _ROUTE_REGISTERED = register_owned_route(
        _ACTION_POLICY,
        __name__,
        _act,
        _refusal_body,
        refusal_mapper=_refuse_action,
    )
    return _ROUTE_REGISTERED


def ensure_assisted_sidebar_route_registered() -> bool:
    """Register the private assisted route with pre-body origin/session checks."""

    return register_owned_route(
        _ASSISTED_POLICY,
        __name__,
        _act_assisted,
        _refusal_body,
        refusal_mapper=_refuse_assisted,
        prelude=_session_prelude,
    )


__all__ = [
    "MAX_SIDEBAR_ACTION_BYTES",
    "ASSISTED_SIDEBAR_ACTION_ROUTE",
    "MAX_SIDEBAR_ACTION_DEPTH",
    "MAX_SIDEBAR_ACTION_ITEMS",
    "MAX_SIDEBAR_TRANSFER_BYTES",
    "MAX_SIDEBAR_WORKSPACES",
    "MAX_SEMANTIC_REVIEW_LINEAGES",
    "SEMANTIC_REVIEW_TTL_SECONDS",
    "SIDEBAR_ACTION_ROUTE",
    "SIDEBAR_ACTION_SCHEMA",
    "SIDEBAR_WORKSPACE_TTL_SECONDS",
    "SidebarProductionSeed",
    "SidebarAuthoringSeed",
    "SidebarAuthoringSource",
    "SidebarAuthoringWorkspaceClaim",
    "SidebarWorkspaceRegistry",
    "ProductionPlanningSourceSnapshot",
    "decode_sidebar_action_json",
    "decode_sidebar_transfer_json",
    "claim_sidebar_production_materialization_seed",
    "claim_sidebar_production_seed",
    "claim_sidebar_authoring_seed",
    "claim_sidebar_authoring_workspace",
    "guard_production_planning_source",
    "snapshot_production_planning_source",
    "publish_production_materialization",
    "release_production_materialization",
    "dispatch_sidebar_action",
    "dispatch_assisted_sidebar_action",
    "ensure_assisted_sidebar_route_registered",
    "ensure_sidebar_route_registered",
    "commit_semantic_proposal_review",
    "publish_sidebar_workspace",
    "rollback_semantic_proposal_review",
    "stage_semantic_proposal_review",
]
