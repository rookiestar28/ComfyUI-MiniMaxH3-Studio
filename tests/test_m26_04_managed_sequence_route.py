from __future__ import annotations

import asyncio
import json
import sys
from hashlib import sha256
from types import ModuleType, SimpleNamespace
from typing import Protocol, cast
from unittest.mock import patch

import pytest
from deployment_request_doubles import LOOPBACK_HOST, ListenerTransport
from deployment_request_doubles import LOOPBACK_ORIGIN as OWNED_ORIGIN

from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.managed_sequence_service import (
    MANAGED_SEQUENCE_ACTION_ROUTE,
    MANAGED_SEQUENCE_ACTION_SCHEMA,
    MANAGED_SEQUENCE_PROJECTION_ROUTE,
    FailBoundManagedSequenceChildRequestV1,
    FailPreparedManagedSequenceChildRequestV1,
    ManagedModeQualificationRegistry,
    ManagedSequenceMutationResultV1,
    ManagedSequenceSegmentMutationRequestV1,
    ManagedSequenceService,
    ManagedSequenceServiceError,
    ManagedSequenceStartResourceClaimV1,
    MarkManagedSequenceInvocationUnknownRequestV1,
    RecordManagedSequenceCanvasRollbackRequestV1,
    decode_managed_sequence_action_json,
    dispatch_decoded_managed_sequence_action,
    ensure_managed_sequence_route_registered,
)
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.managed_sequence import (
    ManagedModeQualificationV1,
    ManagedSequenceAuthorizationV1,
    ManagedSequenceSegmentAuthorizationV1,
    ManagedSequenceStateV1,
)
from scripts.hc_09_host_seam_test_double import host_prompt_server_module


def _fp(label: str) -> str:
    return f"sha256:{sha256(label.encode('ascii')).hexdigest()}"


def _authorization(sequence_id: str) -> ManagedSequenceAuthorizationV1:
    segment = ManagedSequenceSegmentAuthorizationV1(
        segment_id="segment.1",
        ordinal=1,
        task_mode=TaskMode.T2VA,
        duration_milliseconds=4_000,
        frame_count=100,
        manifest_fingerprint=_fp("manifest.1"),
        materialization_receipt_fingerprint=_fp("materialization.1"),
        local_prompt_fingerprint=_fp("prompt.1"),
        intent_graph_fingerprint=_fp("intent.1"),
        profile_fingerprint=_fp("profile.1"),
        capability_fingerprint=_fp("capability.current"),
        predecessor_segment_id=None,
        predecessor_cut_receipt_fingerprint=None,
    )
    return ManagedSequenceAuthorizationV1(
        sequence_id=sequence_id,
        workspace_id="production.workspace.1",
        workspace_revision=7,
        workspace_fingerprint=_fp("workspace.current"),
        production_plan_fingerprint=_fp("production.plan"),
        proposal_fingerprint=_fp("proposal.approved"),
        generation_plan_fingerprint=_fp("generation.plan"),
        compiler_fingerprint=_fp("compiler.current"),
        host_capability_fingerprint=_fp("capability.current"),
        segments=(segment,),
    )


class _Production:
    def claim_automatic_plan_authority(
        self,
        handle: str,
        *,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        expected_plan_fingerprint: str,
    ) -> object:
        del (
            handle,
            expected_workspace_revision,
            expected_workspace_fingerprint,
            expected_plan_fingerprint,
        )
        return object()


class _WireRequest(Protocol):
    def to_wire(self) -> dict[str, object]: ...


def _service() -> ManagedSequenceService:
    tokens = iter(("a" * 40, "b" * 40))
    return ManagedSequenceService(
        production_registry=_Production(),
        authorization_builder=lambda _source, **values: _authorization(str(values["sequence_id"])),
        reserve_start_resources=lambda request: ManagedSequenceStartResourceClaimV1(
            request_fingerprint=request.fingerprint,
            coordinator_reservation_fingerprint=_fp("coordinator.reservation"),
            child_slot_reservation_fingerprint=_fp("child.slot.reservation"),
            artifact_reservation_fingerprint=_fp("artifact.reservation"),
            consume=lambda _request_id, _rows: None,
            release=lambda: None,
        ),
        token_factory=lambda: next(tokens),
        clock=lambda: 100.0,
    )


def _wire(action: str, payload: dict[str, object], request_id: str) -> bytes:
    return json.dumps(
        {
            "schema": MANAGED_SEQUENCE_ACTION_SCHEMA,
            "request_id": request_id,
            "action": action,
            "payload": payload,
        }
    ).encode()


def test_closed_decoder_rejects_duplicate_unknown_and_oversized_bodies() -> None:
    payload = b'{"schema":"h3.context.managed_sequence_action.v1","schema":"x"}'
    with pytest.raises(ValueError, match="duplicate"):
        decode_managed_sequence_action_json(payload)
    with pytest.raises(ManagedSequenceServiceError, match="action_envelope"):
        decode_managed_sequence_action_json(
            _wire("cancel_sequence", {"unexpected": True}, "cancel.1")
        )
    with pytest.raises(ValueError, match="body bound"):
        decode_managed_sequence_action_json(b"x" * 32_769)


def test_canvas_rollback_decoder_requires_the_exact_explicit_write_evidence() -> None:
    parent = {
        "parent_sequence_id": "managed.sequence.1",
        "expected_revision": 9,
        "authorization_fingerprint": _fp("authorization.current"),
        "segment_id": "segment.2",
        "active_workflow_fingerprint": _fp("workflow.current"),
        "drifted_owned_projection_fingerprint": _fp("owned.foreign"),
        "restored_owned_projection_fingerprint": _fp("owned.restored"),
    }
    decoded = decode_managed_sequence_action_json(
        _wire("record_canvas_rollback", parent, "rollback.segment.2")
    )

    assert decoded.action == "record_canvas_rollback"
    assert type(decoded.request) is RecordManagedSequenceCanvasRollbackRequestV1
    assert decoded.request.to_wire()["segment_id"] == "segment.2"
    with pytest.raises(ManagedSequenceServiceError, match="action_envelope"):
        decode_managed_sequence_action_json(
            _wire(
                "record_canvas_rollback",
                {**parent, "read_authority_fingerprint": _fp("read.only")},
                "rollback.segment.2.extra",
            )
        )


def test_retry_decoder_requires_current_workflow_and_owned_projection_authority() -> None:
    payload = {
        "parent_sequence_id": "managed.sequence.1",
        "expected_revision": 9,
        "authorization_fingerprint": _fp("authorization.current"),
        "segment_id": "segment.1",
        "active_workflow_fingerprint": _fp("workflow.current"),
        "owned_projection_fingerprint": _fp("owned.current"),
    }

    decoded = decode_managed_sequence_action_json(
        _wire("retry_segment", payload, "retry.segment.1")
    )

    assert type(decoded.request) is ManagedSequenceSegmentMutationRequestV1
    with pytest.raises(ManagedSequenceServiceError, match="action_envelope"):
        decode_managed_sequence_action_json(
            _wire(
                "retry_segment",
                {
                    key: value
                    for key, value in payload.items()
                    if key != "owned_projection_fingerprint"
                },
                "retry.segment.1.missing-canvas",
            )
        )


def test_fail_prepared_decoder_requires_exact_execution_and_failure_evidence() -> None:
    parent = {
        "parent_sequence_id": "managed.sequence.1",
        "expected_revision": 3,
        "authorization_fingerprint": _fp("authorization.current"),
        "segment_id": "segment.1",
        "eligible_execution_fingerprint": _fp("eligible.execution.1"),
        "failure_fingerprint": _fp("compile.refused"),
    }
    decoded = decode_managed_sequence_action_json(
        _wire("fail_prepared_child", parent, "fail.prepared.segment.1")
    )

    assert decoded.action == "fail_prepared_child"
    assert type(decoded.request) is FailPreparedManagedSequenceChildRequestV1
    assert decoded.request.to_wire()["failure_fingerprint"] == _fp("compile.refused")
    with pytest.raises(ManagedSequenceServiceError, match="action_envelope"):
        decode_managed_sequence_action_json(
            _wire(
                "fail_prepared_child",
                {**parent, "queue_prompt_id": "prompt.not.created"},
                "fail.prepared.segment.1.extra",
            )
        )


@pytest.mark.parametrize(
    ("action", "extra", "request_type"),
    (
        (
            "fail_bound_child",
            {
                "child_run_handle": "mc_" + "d" * 40,
                "failure_fingerprint": _fp("queue.rejected"),
            },
            FailBoundManagedSequenceChildRequestV1,
        ),
        (
            "mark_invocation_unknown",
            {
                "child_run_handle": "mc_" + "d" * 40,
                "timeout_ms": 60_000,
                "active_workflow_fingerprint": _fp("workflow.current"),
                "previous_owned_projection_fingerprint": _fp("owned.before"),
                "written_owned_projection_fingerprint": _fp("owned.written"),
            },
            MarkManagedSequenceInvocationUnknownRequestV1,
        ),
    ),
)
def test_pre_ack_queue_outcomes_have_closed_prompt_free_decoders(
    action: str,
    extra: dict[str, object],
    request_type: type,
) -> None:
    decoded = decode_managed_sequence_action_json(
        _wire(
            action,
            {
                "parent_sequence_id": "managed.sequence.1",
                "expected_revision": 4,
                "authorization_fingerprint": _fp("authorization.current"),
                "segment_id": "segment.1",
                **extra,
            },
            f"{action}.segment.1",
        )
    )

    assert type(decoded.request) is request_type
    assert "queue_prompt_id" not in cast(_WireRequest, decoded.request).to_wire()


def test_qualification_registry_exposes_only_one_current_exact_claim() -> None:
    now = 100.0
    registry = ManagedModeQualificationRegistry(clock=lambda: now)
    first = ManagedModeQualificationV1(
        host_capability_fingerprint=_fp("capability.current"),
        compiler_fingerprint=_fp("compiler.current"),
        qualified_global_modes=tuple(TaskMode),
        qualified_materialization_receipts=(_fp("materialization.1"),),
        observed_at=90.0,
        expires_at=200.0,
    )
    second = ManagedModeQualificationV1(
        host_capability_fingerprint=_fp("capability.next"),
        compiler_fingerprint=_fp("compiler.next"),
        qualified_global_modes=tuple(TaskMode),
        qualified_materialization_receipts=(_fp("materialization.next"),),
        observed_at=95.0,
        expires_at=250.0,
    )

    assert registry.publish(first) == first.fingerprint
    assert registry.claim(first.fingerprint) is first
    assert registry.publish(second) == second.fingerprint
    assert registry.claim(first.fingerprint) is None
    assert registry.claim(second.fingerprint) is second

    now = 251.0
    assert registry.claim(second.fingerprint) is None
    with pytest.raises(ManagedSequenceServiceError, match="qualification_expired"):
        registry.publish(first)


def test_authorize_and_start_dispatch_claim_only_exact_backend_qualification() -> None:
    service = _service()
    authorize = decode_managed_sequence_action_json(
        _wire(
            "authorize_sequence",
            {
                "workspace_handle": "pw_" + "a" * 40,
                "expected_workspace_revision": 7,
                "expected_workspace_fingerprint": _fp("workspace.current"),
                "expected_plan_fingerprint": _fp("production.plan"),
                "generation_plan_fingerprint": _fp("generation.plan"),
                "compiler_fingerprint": _fp("compiler.current"),
                "host_capability_fingerprint": _fp("capability.current"),
                "explicit_intent": "generate_approved_sequence",
            },
            "authorize.1",
        )
    )
    authorized = dispatch_decoded_managed_sequence_action(
        service,
        authorize,
        qualification_claimant=lambda _fingerprint: None,
    )
    assert isinstance(authorized, ManagedSequenceMutationResultV1)
    authority = _authorization(authorized.parent_sequence_id)
    qualification = ManagedModeQualificationV1(
        host_capability_fingerprint=authority.host_capability_fingerprint,
        compiler_fingerprint=authority.compiler_fingerprint,
        qualified_global_modes=tuple(TaskMode),
        qualified_materialization_receipts=(
            authority.segments[0].materialization_receipt_fingerprint,
        ),
        observed_at=90.0,
        expires_at=1_000.0,
    )
    start = decode_managed_sequence_action_json(
        _wire(
            "start_sequence",
            {
                "parent_sequence_id": authorized.parent_sequence_id,
                "expected_revision": authorized.revision,
                "authorization_fingerprint": authorized.authorization_fingerprint,
                "qualification_fingerprint": qualification.fingerprint,
            },
            "start.1",
        )
    )
    claims: list[str] = []

    def claim_qualification(fingerprint: str) -> ManagedModeQualificationV1:
        claims.append(fingerprint)
        return qualification

    started = dispatch_decoded_managed_sequence_action(
        service,
        start,
        qualification_claimant=claim_qualification,
    )

    assert isinstance(started, ManagedSequenceMutationResultV1)
    assert started.state is ManagedSequenceStateV1.ACTIVE
    assert claims == [qualification.fingerprint]
    with pytest.raises(ManagedSequenceServiceError, match="qualification_claim_drift"):
        dispatch_decoded_managed_sequence_action(
            _service(),
            decode_managed_sequence_action_json(
                _wire(
                    "start_sequence",
                    {
                        "parent_sequence_id": authorized.parent_sequence_id,
                        "expected_revision": authorized.revision,
                        "authorization_fingerprint": authorized.authorization_fingerprint,
                        "qualification_fingerprint": qualification.fingerprint,
                    },
                    "start.2",
                )
            ),
            qualification_claimant=lambda _fingerprint: None,
        )


class _Routes(list[SimpleNamespace]):
    def _decorate(self, method: str, path: str):  # type: ignore[no-untyped-def]
        def decorate(handler):  # type: ignore[no-untyped-def]
            self.append(SimpleNamespace(method=method, path=path, handler=handler))
            return handler

        return decorate

    def get(self, path: str):  # type: ignore[no-untyped-def]
        return self._decorate("GET", path)

    def post(self, path: str):  # type: ignore[no-untyped-def]
        return self._decorate("POST", path)


class _Headers:
    def __init__(self, values: dict[str, list[str]]) -> None:
        self._values = values

    def getall(self, name: str, default: list[str]) -> list[str]:
        return self._values.get(name.lower(), default)


class _Content:
    def __init__(self, payload: bytes) -> None:
        self._chunks = [payload, b""]

    async def read(self, _limit: int) -> bytes:
        return self._chunks.pop(0)


def _host(routes: _Routes) -> tuple[ModuleType, ModuleType]:
    server = host_prompt_server_module(routes)
    web = SimpleNamespace(
        json_response=lambda value, status=200, headers=None: SimpleNamespace(
            status=status,
            body=value,
            headers={} if headers is None else headers,
        ),
        Response=lambda status=200, body=None, headers=None: SimpleNamespace(
            status=status,
            body=body,
            headers={} if headers is None else headers,
        ),
    )
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = web
    return server, aiohttp


async def _drive_owned_post_and_side_effect_free_get() -> None:
    routes = _Routes()
    server, aiohttp = _host(routes)
    service = _service()
    qualifications = ManagedModeQualificationRegistry(clock=lambda: 100.0)
    with (
        composition_root.substituted(composition_root.MANAGED_SEQUENCE, service),
        composition_root.substituted(
            composition_root.MANAGED_MODE_QUALIFICATION,
            qualifications,
        ),
        patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}),
    ):
        assert ensure_managed_sequence_route_registered()
        assert {(row.method, row.path) for row in routes} == {
            ("POST", MANAGED_SEQUENCE_ACTION_ROUTE),
            ("GET", MANAGED_SEQUENCE_PROJECTION_ROUTE),
        }
        post = next(row for row in routes if row.method == "POST")
        payload = _wire(
            "authorize_sequence",
            {
                "workspace_handle": "pw_" + "a" * 40,
                "expected_workspace_revision": 7,
                "expected_workspace_fingerprint": _fp("workspace.current"),
                "expected_plan_fingerprint": _fp("production.plan"),
                "generation_plan_fingerprint": _fp("generation.plan"),
                "compiler_fingerprint": _fp("compiler.current"),
                "host_capability_fingerprint": _fp("capability.current"),
                "explicit_intent": "generate_approved_sequence",
            },
            "authorize.route.1",
        )
        post_response = await post.handler(
            SimpleNamespace(
                content_type="application/json",
                content_length=len(payload),
                content=_Content(payload),
                headers=_Headers({"origin": [OWNED_ORIGIN], "host": [LOOPBACK_HOST]}),
                transport=ListenerTransport(),
            )
        )
        assert post_response.status == 200
        parent_id = post_response.body["parent_sequence_id"]
        read_authority = post_response.body["read_authority_fingerprint"]
        authority = _authorization(parent_id)
        qualification = ManagedModeQualificationV1(
            host_capability_fingerprint=authority.host_capability_fingerprint,
            compiler_fingerprint=authority.compiler_fingerprint,
            qualified_global_modes=tuple(TaskMode),
            qualified_materialization_receipts=(
                authority.segments[0].materialization_receipt_fingerprint,
            ),
            observed_at=90.0,
            expires_at=1_000.0,
        )
        qualifications.publish(qualification)
        start_payload = _wire(
            "start_sequence",
            {
                "parent_sequence_id": parent_id,
                "expected_revision": post_response.body["revision"],
                "authorization_fingerprint": post_response.body["authorization_fingerprint"],
                "qualification_fingerprint": qualification.fingerprint,
            },
            "start.route.1",
        )
        start_response = await post.handler(
            SimpleNamespace(
                content_type="application/json",
                content_length=len(start_payload),
                content=_Content(start_payload),
                headers=_Headers({"origin": [OWNED_ORIGIN], "host": [LOOPBACK_HOST]}),
                transport=ListenerTransport(),
            )
        )
        assert start_response.status == 200

        get = next(row for row in routes if row.method == "GET")

        def read_request(if_none_match: str | None = None) -> SimpleNamespace:
            headers: dict[str, list[str]] = {"host": [LOOPBACK_HOST]}
            if if_none_match is not None:
                headers["if-none-match"] = [if_none_match]
            return SimpleNamespace(
                content_type="application/json",
                content_length=None,
                content=_Content(b""),
                headers=_Headers(headers),
                transport=ListenerTransport(),
                match_info={"parent_sequence_id": parent_id},
                query=SimpleNamespace(
                    getall=lambda name, default: (
                        [read_authority] if name == "read_authority_fingerprint" else default
                    )
                ),
            )

        first = await get.handler(read_request())
        assert first.status == 200
        assert first.headers == {"ETag": first.body["etag"]}
        second = await get.handler(read_request(first.body["etag"]))
        assert second.status == 304
        assert second.body is None
        assert second.headers == first.headers


def test_owned_post_and_side_effect_free_get_publish_exact_etag() -> None:
    asyncio.run(_drive_owned_post_and_side_effect_free_get())
