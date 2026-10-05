"""Owned automatic planning actions; source facts and materializers never come from a client."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from ..core.canonical import canonical_bytes, canonical_fingerprint
from ..core.errors import SidebarWorkspaceError
from ..core.production_duration import ProductionDurationIntentV1, SegmentationPolicyV1
from ..core.production_import import ProductionImportRequestV1
from ..core.production_semantics import ProductionSemanticError
from ..core.production_storyboard import (
    ProductionPlanningContextV2,
    ProductionStoryboardAdmissionRequestV1,
    ProductionStoryboardAdmissionService,
    ProductionStoryboardAdmissionV2,
    SegmentationProposalV2,
    StoryboardShotV1,
    StoryboardSourceKindV1,
    build_segmentation_proposal,
)
from ..core.segment_workspace import MultiSegmentWorkspace
from .comfyui_production_workspace import ProductionWorkbenchError, ProductionWorkspaceRegistry
from .comfyui_route_seam import (
    RoutePolicy,
    RouteResult,
    offload_route_handler,
    register_owned_route,
)
from .comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from .production_planning_source import (
    ProductionPlanningSourceError,
    ProductionPlanningSourceSnapshot,
    build_production_planning_context,
)

PRODUCTION_PLANNING_ACTION_SCHEMA = "h3.context.production_planning.action.v1"
PRODUCTION_PLANNING_PROJECTION_SCHEMA = "h3.context.production_planning.projection.v1"
PRODUCTION_PLANNING_ROUTE = "/h3-context/v1/production/planning/action"
MAX_PLANNING_ACTION_BYTES = 262_144
MAX_PLANNING_RESPONSE_BYTES = 1_048_576
MAX_PLANNING_RETAINED_BYTES = 16_777_216
MAX_PLANNING_ENTRIES = 16
MAX_PLANNING_REQUESTS = 256
PLANNING_TTL_SECONDS = 900

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WORKSPACE = re.compile(r"pw_[A-Za-z0-9_-]{32,96}\Z")
_SOURCE = re.compile(r"ws_[A-Za-z0-9_-]{32,96}\Z")
_CAS_KEYS = {"workspace_handle", "expected_workspace_revision", "expected_workspace_fingerprint"}
_PLAN_KEYS = _CAS_KEYS | {"planning_context_id", "expected_planning_revision"}
_KEYS = {
    "prepare_context": _CAS_KEYS
    | {
        "context_workspace_handle",
        "expected_report_revision",
        "expected_report_fingerprint",
        "expected_planning_revision",
        "target_seconds",
        "policy",
    },
    "admit_storyboard": _PLAN_KEYS | {"source_kind", "typed_rows", "user_reviewed"},
    "propose": _PLAN_KEYS | {"admission_id"},
    "import_plan": _PLAN_KEYS | {"proposal_id"},
    "read_plan": _PLAN_KEYS,
    "prepare_managed_readiness": _CAS_KEYS | {"expected_plan_fingerprint"},
    "read_managed_readiness": _CAS_KEYS
    | {"expected_plan_fingerprint", "qualification_fingerprint"},
}


def _reject(code: str = "invalid_planning_action", status: int = 400) -> ProductionWorkbenchError:
    return ProductionWorkbenchError(code, status)


def _text(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise _reject()
    return value


def _integer(value: object, minimum: int = 1, maximum: int = 1_000_000) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _reject()
    return value


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise _reject()
        result[key] = value
    return result


def _shape(value: object, depth: int = 0) -> int:
    if depth > 6:
        raise _reject()
    if type(value) is dict:
        if len(value) > 24:
            raise _reject()
        return 1 + sum(_shape(item, depth + 1) for item in value.values())
    if type(value) is list:
        if len(value) > 64:
            raise _reject()
        return 1 + sum(_shape(item, depth + 1) for item in value)
    if value is None or type(value) in (str, int, bool):
        return 1
    raise _reject()


def decode_production_planning_action(value: object) -> dict[str, object]:
    """Validate the same closed contract for HTTP and internal application callers."""
    if _shape(value) > 12_000 or len(canonical_bytes(value)) > MAX_PLANNING_ACTION_BYTES:
        raise _reject("planning_action_capacity", 413)
    if type(value) is not dict or set(value) != {"schema", "request_id", "action", "payload"}:
        raise _reject()
    action = cast(dict[str, object], value)
    if action["schema"] != PRODUCTION_PLANNING_ACTION_SCHEMA:
        raise _reject()
    _text(action["request_id"], _IDENTIFIER)
    kind = action["action"]
    if type(kind) is not str or kind not in _KEYS:
        raise _reject()
    payload = action["payload"]
    if type(payload) is not dict or set(payload) != _KEYS[kind]:
        raise _reject()
    _text(payload["workspace_handle"], _WORKSPACE)
    _integer(payload["expected_workspace_revision"])
    _text(payload["expected_workspace_fingerprint"], _HASH)
    if kind in ("prepare_managed_readiness", "read_managed_readiness"):
        _text(payload["expected_plan_fingerprint"], _HASH)
        if kind == "read_managed_readiness":
            _text(payload["qualification_fingerprint"], _HASH)
        return cast(dict[str, object], json.loads(canonical_bytes(action)))
    _integer(payload["expected_planning_revision"], 0 if kind == "prepare_context" else 1)
    if kind == "prepare_context":
        _text(payload["context_workspace_handle"], _SOURCE)
        _integer(payload["expected_report_revision"], 0)
        _text(payload["expected_report_fingerprint"], _HASH)
        _integer(payload["target_seconds"], 4, 60)
        if type(payload["policy"]) is not str:
            raise _reject()
        try:
            SegmentationPolicyV1(payload["policy"])
        except ValueError:
            raise _reject() from None
    else:
        _text(payload["planning_context_id"], _IDENTIFIER)
    if kind == "admit_storyboard":
        if type(payload["source_kind"]) is not str or type(payload["user_reviewed"]) is not bool:
            raise _reject()
        try:
            source_kind = StoryboardSourceKindV1(payload["source_kind"])
            rows = payload["typed_rows"]
            if type(rows) is not list:
                raise ValueError()
            for row in rows:
                StoryboardShotV1.from_wire(row)
            canonical = source_kind is StoryboardSourceKindV1.CANONICAL_OPTIMIZED_PROMPT
            if canonical and (rows or payload["user_reviewed"]):
                raise ValueError()
            if not canonical and (not rows or payload["user_reviewed"] is not True):
                raise ValueError()
        except ValueError:
            raise _reject() from None
    for key in ("admission_id", "proposal_id"):
        if key in payload:
            _text(payload[key], _IDENTIFIER)
    # Never retain an alias to caller-mutable dictionaries or reviewed rows.
    return cast(dict[str, object], json.loads(canonical_bytes(action)))


def decode_production_planning_json(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes or len(payload) > MAX_PLANNING_ACTION_BYTES:
        raise _reject("planning_action_capacity", 413)
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs)
        return decode_production_planning_action(value)
    except (UnicodeError, ValueError, RecursionError):
        raise _reject() from None


@dataclass(frozen=True, slots=True)
class PlanningActionResult:
    status: int
    body: bytes

    def to_wire(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.body))


@dataclass(frozen=True, slots=True)
class _PlanningEntry:
    source: ProductionPlanningSourceSnapshot
    context: ProductionPlanningContextV2
    revision: int
    expires_at: float
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    admission: ProductionStoryboardAdmissionV2 | None = None
    proposal: SegmentationProposalV2 | None = None


@dataclass(frozen=True, slots=True)
class _Replay:
    digest: str
    expires_at: float
    result: PlanningActionResult | None
    context_id: str
    workspace_handle: str
    import_request: ProductionImportRequestV1 | None = None


class ProductionPlanningService:
    """Bounded fixed-lifetime owner above the existing atomic Production import transaction."""

    def __init__(
        self,
        *,
        sidebar_registry: SidebarWorkspaceRegistry,
        production_registry: ProductionWorkspaceRegistry,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = MAX_PLANNING_ENTRIES,
        max_requests: int = MAX_PLANNING_REQUESTS,
    ) -> None:
        if (
            not 1 <= max_entries <= MAX_PLANNING_ENTRIES
            or not 1 <= max_requests <= MAX_PLANNING_REQUESTS
        ):
            raise ValueError("planning capacity is invalid")
        self._sidebar = sidebar_registry
        self._production = production_registry
        self._clock = clock
        self._max_entries = max_entries
        self._max_requests = max_requests
        self._entries: dict[str, _PlanningEntry] = {}
        self._requests: dict[str, _Replay] = {}
        self._lock = threading.RLock()

    def _prune(self) -> None:
        now = self._clock()
        self._entries = {key: row for key, row in self._entries.items() if row.expires_at > now}
        self._requests = {key: row for key, row in self._requests.items() if row.expires_at > now}

    def _workspace(self, payload: dict[str, object]) -> MultiSegmentWorkspace:
        return self._production.claim_workspace_authority(
            str(payload["workspace_handle"]),
            expected_workspace_revision=cast(int, payload["expected_workspace_revision"]),
            expected_workspace_fingerprint=str(payload["expected_workspace_fingerprint"]),
        )

    def _current(self, payload: dict[str, object]) -> _PlanningEntry:
        entry = self._entries.get(str(payload["workspace_handle"]))
        if entry is None:
            raise _reject("planning_unavailable", 410)
        if (
            entry.context.planning_context_id != payload["planning_context_id"]
            or entry.revision != payload["expected_planning_revision"]
            or entry.workspace_revision != payload["expected_workspace_revision"]
            or entry.workspace_fingerprint != payload["expected_workspace_fingerprint"]
        ):
            raise _reject("planning_stale", 409)
        self._workspace(payload)
        with self._sidebar.guard_production_planning_source(entry.source):
            pass
        return entry

    @staticmethod
    def _result(request_id: str, handle: str, entry: _PlanningEntry) -> PlanningActionResult:
        proposal = entry.proposal
        wire: dict[str, object] = {
            "schema": PRODUCTION_PLANNING_PROJECTION_SCHEMA,
            "request_id": request_id,
            "workspace_handle": handle,
            "workspace_id": entry.workspace_id,
            "workspace_revision": entry.workspace_revision,
            "workspace_fingerprint": entry.workspace_fingerprint,
            "planning_context_id": entry.context.planning_context_id,
            "planning_revision": entry.revision,
            "source_duration_seconds": entry.context.source_context_duration_seconds,
            "target_seconds": entry.context.production_target_duration_seconds,
            "policy": entry.context.production_duration_intent.policy.value,
            "admission_id": entry.admission.admission_id if entry.admission else None,
            "proposal": None
            if proposal is None
            else {
                "proposal_id": proposal.proposal_id,
                "revision": proposal.revision,
                "fingerprint": proposal.fingerprint,
                "importable": proposal.importable,
                "blocker_codes": [code.value for code in proposal.blocker_codes],
                "start_hold_codes": [code.value for code in proposal.start_hold_codes],
                "segments": [
                    {
                        "segment_id": segment.segment_id,
                        "ordinal": segment.ordinal,
                        "task_mode": segment.task_mode.value,
                        "duration_seconds": segment.duration.requested_seconds,
                        "local_prompt": segment.local_prompt,
                    }
                    for segment in proposal.segments
                ],
            },
        }
        body = canonical_bytes(wire)
        if len(body) > MAX_PLANNING_RESPONSE_BYTES:
            raise _reject("planning_response_capacity", 429)
        return PlanningActionResult(200, body)

    def _capacity(
        self,
        result: PlanningActionResult,
        handle: str,
        entry: _PlanningEntry,
        *,
        importing: bool,
    ) -> None:
        entries = {**self._entries, handle: entry}
        retained_bytes = sum(
            len(
                json.dumps(
                    {
                        "source": row.source.report.to_wire(),
                        "context": row.context.to_wire(),
                        "admission": row.admission.to_wire() if row.admission else None,
                        "proposal": row.proposal.to_wire() if row.proposal else None,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            )
            for row in entries.values()
        )
        # Reserve the bounded import response before its transaction commits. A post-commit
        # response budget failure would strand the caller without a retained replay key.
        response_bytes = MAX_PLANNING_RESPONSE_BYTES if importing else len(result.body)
        if len(self._requests) >= self._max_requests or (
            retained_bytes
            + sum(
                len(row.result.body) if row.result else MAX_PLANNING_RESPONSE_BYTES
                for row in self._requests.values()
            )
            + response_bytes
            > MAX_PLANNING_RETAINED_BYTES
        ):
            raise _reject("planning_request_capacity", 429)

    def _prepare(self, payload: dict[str, object]) -> _PlanningEntry:
        handle = str(payload["workspace_handle"])
        previous = self._entries.get(handle)
        if payload["expected_planning_revision"] != (previous.revision if previous else 0):
            raise _reject("planning_stale", 409)
        if previous is None and len(self._entries) >= self._max_entries:
            raise _reject("planning_capacity", 429)
        workspace = self._workspace(payload)
        source = self._sidebar.snapshot_production_planning_source(
            str(payload["context_workspace_handle"]),
            expected_report_revision=cast(int, payload["expected_report_revision"]),
            expected_report_fingerprint=str(payload["expected_report_fingerprint"]),
        )
        self._production.assert_planning_context_source(workspace, source.workspace_id)
        try:
            context = build_production_planning_context(
                source,
                planning_context_id="planning_" + secrets.token_hex(16),
                revision=1,
                duration_intent=ProductionDurationIntentV1(
                    target_seconds=cast(int, payload["target_seconds"]),
                    policy=SegmentationPolicyV1(payload["policy"]),
                ),
            )
        except (ProductionSemanticError, ProductionPlanningSourceError):
            # GUARD: these are `ValueError`s, which the route policy answers as a malformed
            # request (400). The request is well formed; the retained Context is one whole-video
            # planning cannot use (a clip length that is not a whole 4-15 seconds, a timing
            # constraint without an end). Name that, so the client does not blame its rows.
            raise _reject("planning_source_unsupported", 422) from None
        return _PlanningEntry(
            source,
            context,
            (previous.revision + 1) if previous else 1,
            min(source.expires_at_monotonic, self._clock() + PLANNING_TTL_SECONDS),
            workspace.workspace_id,
            workspace.revision,
            workspace.fingerprint,
        )

    def _admit(
        self, entry: _PlanningEntry, payload: dict[str, object], request_id: str
    ) -> _PlanningEntry:
        from dataclasses import replace

        context = entry.context
        source_kind = StoryboardSourceKindV1(payload["source_kind"])
        request = ProductionStoryboardAdmissionRequestV1(
            request_id=request_id,
            planning_context_id=context.planning_context_id,
            planning_context_revision=context.revision,
            planning_context_fingerprint=context.fingerprint,
            optimized_candidate_id=context.optimized_candidate_id,
            optimized_candidate_text_fingerprint=context.optimized_candidate_text_fingerprint,
            production_duration_intent_fingerprint=context.duration_intent_fingerprint,
            source_kind=source_kind,
            candidate_text=(
                entry.source.report.prompt_document.text
                if source_kind is StoryboardSourceKindV1.CANONICAL_OPTIMIZED_PROMPT
                else None
            ),
            typed_rows=tuple(
                StoryboardShotV1.from_wire(row) for row in cast(list[object], payload["typed_rows"])
            ),
            user_reviewed=cast(bool, payload["user_reviewed"]),
        )
        admission = ProductionStoryboardAdmissionService().admit(context, request)
        if type(admission) is not ProductionStoryboardAdmissionV2:
            raise _reject("storyboard_review_required", 422)
        return replace(entry, revision=entry.revision + 1, admission=admission, proposal=None)

    def dispatch(self, action_value: object) -> PlanningActionResult:
        try:
            return self._dispatch(action_value)
        except SidebarWorkspaceError as error:
            code = (
                "planning_source_stale"
                if error.code == "planning_source_stale"
                else "planning_source_unavailable"
            )
            raise _reject(code, 409 if code == "planning_source_stale" else 404) from None
        except KeyError:
            raise _reject("planning_source_unavailable", 404) from None

    def _dispatch(self, action_value: object) -> PlanningActionResult:
        from dataclasses import replace

        action = decode_production_planning_action(action_value)
        payload = cast(dict[str, object], action["payload"])
        request_id, kind = str(action["request_id"]), str(action["action"])
        if kind in ("prepare_managed_readiness", "read_managed_readiness"):
            from .composition_root import MANAGED_MODE_QUALIFICATION, get

            result = get(MANAGED_MODE_QUALIFICATION).readiness(action)
            return PlanningActionResult(200, canonical_bytes(result))
        handle, digest = str(payload["workspace_handle"]), canonical_fingerprint(action)
        with self._lock:
            self._prune()
            previous = self._requests.get(request_id)
            if previous is not None:
                if previous.digest != digest:
                    raise _reject("request_id_conflict", 409)
                # CRITICAL: a committed import survives source expiry, but only the Production
                # ledger can prove its current replay. Do not return a cached success blindly.
                if previous.import_request is not None:
                    replay = self._production.replay_automatic_plan_import(previous.import_request)
                    if replay is None:
                        raise _reject("planning_import_unavailable", 410)
                    return PlanningActionResult(200, canonical_bytes(replay.projection.to_wire()))
                current = self._entries.get(handle)
                if current is None or current.context.planning_context_id != previous.context_id:
                    raise _reject("planning_stale", 409)
                with self._sidebar.guard_production_planning_source(current.source):
                    pass
                if previous.result is None:
                    raise _reject("planning_unavailable", 410)
                return previous.result
            if kind == "prepare_context":
                entry = self._prepare(payload)
            else:
                entry = self._current(payload)
                if kind == "admit_storyboard":
                    entry = self._admit(entry, payload, request_id)
                elif kind == "propose":
                    if (
                        entry.admission is None
                        or entry.admission.admission_id != payload["admission_id"]
                    ):
                        raise _reject("planning_admission_stale", 409)
                    candidate = build_segmentation_proposal(entry.context, entry.admission)
                    if type(candidate) is not SegmentationProposalV2:
                        raise _reject("planning_semantic_review_required", 422)
                    entry = replace(entry, revision=entry.revision + 1, proposal=candidate)
            result = self._result(request_id, handle, entry)
            if kind == "read_plan":
                return result
            self._capacity(result, handle, entry, importing=kind == "import_plan")
            import_request = None
            if kind == "import_plan":
                proposal = entry.proposal
                if proposal is None or proposal.proposal_id != payload["proposal_id"]:
                    raise _reject("planning_proposal_stale", 409)
                from .production_context_materializer import CanonicalProductionMaterializer

                import_request = ProductionImportRequestV1(
                    request_id=request_id,
                    workspace_handle=handle,
                    workspace_id=entry.workspace_id,
                    expected_workspace_revision=entry.workspace_revision,
                    expected_workspace_fingerprint=entry.workspace_fingerprint,
                    proposal_id=proposal.proposal_id,
                    proposal_revision=proposal.revision,
                    proposal_fingerprint=proposal.fingerprint,
                    planning_context_fingerprint=entry.context.fingerprint,
                    source_request_fingerprint=entry.context.source_request_fingerprint,
                    source_profile_fingerprint=entry.context.source_profile_fingerprint,
                )
                # CRITICAL: retain the exact request before invoking the transaction. A lost
                # return after commit must consult its ledger before stale source/CAS checks.
                self._requests[request_id] = _Replay(
                    digest,
                    self._clock() + PLANNING_TTL_SECONDS,
                    None,
                    entry.context.planning_context_id,
                    handle,
                    import_request,
                )
                try:
                    imported = self._production.import_automatic_plan(
                        import_request,
                        planning_context=entry.context,
                        proposal=proposal,
                        materialize=CanonicalProductionMaterializer(
                            entry.source, workspace_registry=self._sidebar
                        ),
                        source_guard=lambda: self._sidebar.guard_production_planning_source(
                            entry.source
                        ),
                    )
                except Exception:
                    if self._production.replay_automatic_plan_import(import_request) is None:
                        del self._requests[request_id]
                    raise
                result = PlanningActionResult(200, canonical_bytes(imported.projection.to_wire()))
            else:
                # Publish only after the response is bounded and source/Production CAS are still
                # current. Reads never refresh this fixed expiry or create execution authority.
                with self._sidebar.guard_production_planning_source(entry.source):
                    self._workspace(payload)
                    self._entries[handle] = entry
            self._requests[request_id] = _Replay(
                digest,
                (self._clock() + PLANNING_TTL_SECONDS) if import_request else entry.expires_at,
                result,
                entry.context.planning_context_id,
                handle,
                import_request,
            )
            return result


def build_production_planning_service(
    *,
    sidebar_registry: SidebarWorkspaceRegistry,
    production_registry: ProductionWorkspaceRegistry,
) -> ProductionPlanningService:
    return ProductionPlanningService(
        sidebar_registry=sidebar_registry, production_registry=production_registry
    )


def dispatch_production_planning_action(action: object) -> PlanningActionResult:
    from .composition_root import PRODUCTION_PLANNING, get

    return cast(ProductionPlanningService, get(PRODUCTION_PLANNING)).dispatch(action)


def _refusal_body(_status: int, _reason: str) -> None:
    return None


def _refuse(error: BaseException) -> RouteResult:
    return RouteResult(error.status if isinstance(error, ProductionWorkbenchError) else 400)


def _dispatch_body(payload: bytes) -> RouteResult:
    result = dispatch_production_planning_action(decode_production_planning_json(payload))
    return RouteResult(result.status, result.to_wire())


async def _act(payload: bytes, _context: object) -> RouteResult:
    return await offload_route_handler("planning", lambda: _dispatch_body(payload))


_ROUTE_POLICY = RoutePolicy(
    path=PRODUCTION_PLANNING_ROUTE,
    owner=PRODUCTION_PLANNING_ACTION_SCHEMA,
    owner_attribute="__h3_context_production_planning_action_v1__",
    max_bytes=MAX_PLANNING_ACTION_BYTES,
    refusals=(ProductionWorkbenchError, ValueError, KeyError),
)


def ensure_production_planning_route_registered() -> bool:
    return register_owned_route(
        _ROUTE_POLICY,
        __name__,
        _act,
        _refusal_body,
        refusal_mapper=_refuse,
    )
