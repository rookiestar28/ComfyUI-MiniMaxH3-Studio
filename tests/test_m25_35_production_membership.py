"""M25-35: independent managed generations accumulate as members of one stable project."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_m23_15_sequence_coordinator import _action, _fp, _observation, _required_response
from test_m26_03_context_materializer import _source

import comfyui_h3_context.adapters.comfyui_production_workspace as production_module
import comfyui_h3_context.adapters.comfyui_sequence_coordinator as coordinator_module
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    MAX_PRODUCTION_REGISTRY_BYTES,
    PRODUCTION_ACTION_SCHEMA,
    PRODUCTION_DESTINATION_ADMISSION_TTL_SECONDS,
    ProductionAuthoringOutputClaim,
    ProductionDispatchResult,
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
    decode_production_action_json,
)
from comfyui_h3_context.adapters.comfyui_sequence_coordinator import (
    COORDINATOR_PRODUCTION_MEMBER_AUTHORITY_SCHEMA,
    MANAGED_MEMBER_RESPONSE_SCHEMA,
    MAX_COORDINATOR_RESPONSE_BYTES,
    RELEASE_REQUEST_V2_SCHEMA,
    ObservedVideoArtifact,
    SequenceCoordinatorError,
    SequenceCoordinatorRegistry,
    SequenceCoordinatorResponse,
    SequenceProductionMemberAuthorityV1,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.core import ExecutionCorrelation, TaskMode, build_native_h3_wiring
from comfyui_h3_context.core.canonical import canonical_bytes
from comfyui_h3_context.core.generation_sequence import GenerationJobState
from comfyui_h3_context.core.production_accumulation import (
    PRODUCTION_ACCUMULATION_ACTION_VERSION,
    ProductionAccumulatedProjectProjection,
)
from comfyui_h3_context.core.production_membership import (
    ProductionMemberAttemptV1,
    ProductionMembershipError,
)
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)

_MEMBER_KEYS = {
    "schema",
    "run_handle",
    "disposition",
    "artifact_authority",
    "terminal_fingerprint",
    "sequence",
    "production_member_authority",
}


def _report(seconds: float, intent: str) -> Any:
    request = H3ContextRequestNode().build_request(TaskMode.T2VA, intent, duration_seconds=seconds)[
        0
    ]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    return H3ContextValidatorNode().validate(plan, document)[1]


class _Harness:
    """Real Sidebar, Production and coordinator registries on one virtual clock."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        max_entries: int = 8,
        ttl_seconds: int = 60,
        preview: bool = True,
    ) -> None:
        monkeypatch.setattr(
            "comfyui_h3_context.adapters.comfyui_media_preview.ensure_media_preview_route_registered",
            lambda: preview,
        )
        self.now = [100.0]
        self.sidebar = SidebarWorkspaceRegistry(max_entries=16, ttl_seconds=86_400)
        self.production = ProductionWorkspaceRegistry(
            seed_claim=self.sidebar.claim_production_seed,
            max_entries=max_entries,
            ttl_seconds=ttl_seconds,
            terminal_ttl_seconds=60,
            clock=lambda: self.now[0],
            clock_ms=lambda: 100_000,
        )
        self.output = tmp_path / "output"
        self.output.mkdir()
        self.tokens: Iterator[int] = iter(range(1, 10_000))
        self.coordinator = SequenceCoordinatorRegistry(
            production_registry=self.production,
            output_root_factory=lambda: self.output,
            private_root_factory=lambda: tmp_path / "private",
            artifact_inspector=lambda _payload, **expected: ObservedVideoArtifact(
                "mp4", (expected["expected_frames"], 512, 512, 3)
            ),
            clock=lambda: self.now[0],
            clock_ms=lambda: 100_000,
            token_factory=lambda: f"{next(self.tokens):040d}",
        )
        self.contexts: dict[str, str] = {}

    def context(self, name: str, seconds: float = 8.0) -> str:
        if name not in self.contexts:
            report = _report(seconds, f"Synthetic M25-35 member {name}.")
            published = self.sidebar.publish(
                report,
                build_native_h3_wiring(report),
                ExecutionCorrelation(f"prompt.context.{name}", "node.product.shell"),
            )
            self.contexts[name] = published.workspace_id
        return self.contexts[name]

    def frames(self, context_handle: str) -> int:
        return self.sidebar.claim_production_seed(context_handle).duration.resolved.frame_count

    def production_action(self, request_id: str, action: str, payload: dict[str, object]) -> Any:
        return self.production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": request_id,
                "action": action,
                "payload": payload,
            }
        )

    def admit(
        self,
        request_id: str,
        project: ProductionWorkbenchProjection | None = None,
        segment_id: str | None = None,
    ) -> ProductionDispatchResult:
        result: ProductionDispatchResult = self.production_action(
            request_id,
            "admit_generation_destination",
            {
                "workspace_handle": None if project is None else project.workspace_handle,
                "workspace_id": None if project is None else project.workspace_id,
                "segment_id": segment_id,
            },
        )
        return result

    def admit_v2(
        self,
        request_id: str,
        project: ProductionAccumulatedProjectProjection | None = None,
        segment_id: str | None = None,
    ) -> ProductionDispatchResult:
        result: ProductionDispatchResult = self.production_action(
            request_id,
            "admit_generation_destination_v2",
            {
                "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
                "workspace_handle": None if project is None else project.workspace_handle,
                "workspace_id": None if project is None else project.workspace_id,
                "segment_id": segment_id,
            },
        )
        return result

    def read_v2(self, handle: str, workspace_id: str) -> ProductionAccumulatedProjectProjection:
        projection = self.production_action(
            f"read.v2.{self.now[0]}",
            "read_accumulated_project",
            {
                "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
                "workspace_handle": handle,
                "workspace_id": workspace_id,
            },
        ).projection
        assert isinstance(projection, ProductionAccumulatedProjectProjection)
        return projection

    def read(self, handle: str) -> ProductionWorkbenchProjection:
        result = self.production_action(
            f"read.{self.now[0]}", "read_projection", {"workspace_handle": handle}
        )
        projection: ProductionWorkbenchProjection | None = result.projection
        assert projection is not None
        return projection

    def prepare(self, name: str, context: str, admission_id: str) -> SequenceCoordinatorResponse:
        return _required_response(
            self.coordinator.dispatch(
                _action(
                    f"prepare.{name}",
                    "prepare_managed_run",
                    {
                        "context_workspace_handle": context,
                        "correlation": {
                            "prompt_id": f"prompt.pending.{name}",
                            "execution_node_id": "node.product.shell",
                        },
                        "observation": {
                            **_observation(),
                            "expected_frames": self.frames(context),
                            "graph_fingerprint": _fp(f"graph.{name}"),
                            "compiled_prompt_fingerprint": _fp(f"prompt.{name}"),
                        },
                        "production_admission_request_id": admission_id,
                    },
                )
            )
        )

    def submit(
        self, name: str, prepared: SequenceCoordinatorResponse
    ) -> SequenceCoordinatorResponse:
        command = prepared.sequence.eligible_commands[0]
        return _required_response(
            self.coordinator.dispatch(
                _action(
                    f"submit.{name}",
                    "submit_managed_run",
                    {
                        "run_handle": prepared.run_handle,
                        "expected_state_fingerprint": prepared.sequence.state.fingerprint,
                        "job_id": command.job_id,
                        "transaction_id": command.transaction_id,
                        "graph_fingerprint": command.job.graph_fingerprint,
                        "compiled_prompt_fingerprint": command.job.compiled_prompt_fingerprint,
                        "queue_prompt_id": f"prompt.model.{name}",
                    },
                )
            )
        )

    def close(
        self,
        name: str,
        submitted: SequenceCoordinatorResponse,
        kind: str = "success",
    ) -> SequenceCoordinatorResponse:
        artifact: dict[str, object] | None = None
        if kind == "success":
            media = self.output / f"{name}.mp4"
            media.write_bytes(f"synthetic-member-{name}".encode())
            artifact = {
                "output_node_id": "node.save.video",
                "locator": {"filename": media.name, "subfolder": "", "type": "output"},
            }
        return _required_response(
            self.coordinator.dispatch(
                _action(
                    f"close.{name}",
                    "close_managed_run",
                    {
                        "run_handle": submitted.run_handle,
                        "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                        "queue_prompt_id": f"prompt.model.{name}",
                        "kind": kind,
                        "artifact": artifact,
                    },
                )
            )
        )

    def release(
        self, name: str, response: SequenceCoordinatorResponse, intent: str
    ) -> SequenceCoordinatorResponse:
        payload: dict[str, object] = {
            "schema": RELEASE_REQUEST_V2_SCHEMA,
            "run_handle": response.run_handle,
            "intent": intent,
            "expected_state_fingerprint": response.sequence.state.fingerprint,
        }
        if intent == "cleanup_terminal":
            payload["observed_terminal_fingerprint"] = response.terminal_fingerprint
        return _required_response(
            self.coordinator.dispatch(_action(f"release.{name}", "release_sequence", payload))
        )

    def start(
        self,
        name: str,
        *,
        project: ProductionWorkbenchProjection | None = None,
        segment_id: str | None = None,
        seconds: float = 8.0,
        kind: str = "success",
    ) -> tuple[ProductionWorkbenchProjection, SequenceCoordinatorResponse]:
        admitted = self.admit(f"admit.{name}", project, segment_id)
        assert admitted.status == (202 if project is None else 200)
        prepared = self.prepare(name, self.context(name, seconds), f"admit.{name}")
        closed = self.close(name, self.submit(name, prepared), kind)
        authority = closed.production_member_authority
        assert authority is not None
        return self.read(authority.workspace_handle), closed

    def start_v2(
        self,
        name: str,
        *,
        project: ProductionAccumulatedProjectProjection | None = None,
        segment_id: str | None = None,
        kind: str = "success",
    ) -> tuple[ProductionAccumulatedProjectProjection, SequenceCoordinatorResponse]:
        admission_id = f"admit.v2.{name}"
        admitted = self.admit_v2(admission_id, project, segment_id)
        assert admitted.status == (201 if project is None else 200)
        prepared = self.prepare(name, self.context(name), admission_id)
        closed = self.close(name, self.submit(name, prepared), kind)
        authority = closed.production_member_authority
        assert authority is not None
        return self.read_v2(authority.workspace_handle, authority.workspace_id), closed

    def row(self, handle: str, segment_id: str) -> Any:
        members = self.production._entries[handle].members
        assert members is not None
        return members[segment_id]


def _member_wire(response: SequenceCoordinatorResponse) -> dict[str, object]:
    wire = response.to_wire()
    assert set(wire) == _MEMBER_KEYS
    assert len(canonical_bytes(wire)) <= MAX_COORDINATOR_RESPONSE_BYTES
    return wire


def _outputs(projection: ProductionWorkbenchProjection) -> dict[str, str]:
    return {str(row.segment_id): row.output_handle for row in projection.outputs}


def test_member_attempt_contract_refuses_foreign_or_relabelled_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    project, _closed = harness.start("contract")
    segment_id = project.segments[0].segment_id
    attempt = harness.row(project.workspace_handle, segment_id).completed
    receipt = attempt.artifact_receipt
    assert attempt.verified and receipt is not None

    rebuilt = ProductionMemberAttemptV1(
        attempt.authority_workspace, segment_id, attempt.generation_sequence, receipt
    )
    assert rebuilt.verified
    # An equal but distinct workspace object is a foreign authority: identity, not equality, joins.
    foreign = replace(attempt.authority_workspace)
    assert foreign == attempt.authority_workspace and foreign is not attempt.authority_workspace
    refusals: tuple[tuple[str, dict[str, Any]], ...] = (
        ("member_attempt_plan_authority", {"authority_workspace": foreign}),
        ("member_attempt_segment", {"segment_id": "segment_not_in_this_workspace"}),
        (
            "member_attempt_receipt_identity",
            {
                "artifact_receipt": replace(
                    receipt, model_fingerprint=_fp("foreign.model"), receipt_fingerprint=None
                )
            },
        ),
        ("member_attempt_completion", {"artifact_receipt": None}),
    )
    for code, change in refusals:
        values: dict[str, Any] = {
            "authority_workspace": attempt.authority_workspace,
            "segment_id": segment_id,
            "generation_sequence": attempt.generation_sequence,
            "artifact_receipt": receipt,
            **change,
        }
        with pytest.raises(ProductionMembershipError, match=code):
            ProductionMemberAttemptV1(**values)


def test_action_wire_closes_the_nullable_destination_members() -> None:
    def encoded(payload: dict[str, object], action: str = "admit_generation_destination") -> bytes:
        return json.dumps(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "admit.wire",
                "action": action,
                "payload": payload,
            }
        ).encode()

    handle = "pw_" + "a" * 43
    decode_production_action_json(
        encoded({"workspace_handle": None, "workspace_id": None, "segment_id": None})
    )
    decode_production_action_json(
        encoded({"workspace_handle": handle, "workspace_id": "workspace_a", "segment_id": None})
    )
    decode_production_action_json(
        encoded({"admission_request_id": "admit.wire"}, "release_generation_destination")
    )
    decode_production_action_json(
        encoded(
            {
                "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
                "workspace_handle": None,
                "workspace_id": None,
                "segment_id": None,
            },
            "admit_generation_destination_v2",
        )
    )
    decode_production_action_json(
        encoded(
            {
                "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
                "workspace_handle": handle,
                "workspace_id": "workspace_a",
            },
            "read_accumulated_project",
        )
    )
    refused: tuple[dict[str, object], ...] = (
        {"workspace_handle": None, "workspace_id": "workspace_a", "segment_id": None},
        {"workspace_handle": None, "workspace_id": None, "segment_id": "segment_a"},
        {"workspace_handle": handle, "workspace_id": None, "segment_id": None},
        {"workspace_handle": "ws_" + "a" * 43, "workspace_id": "workspace_a", "segment_id": None},
        {"workspace_handle": None, "workspace_id": None},
    )
    for payload in refused:
        with pytest.raises(ValueError):
            decode_production_action_json(encoded(payload))
    for version in ("h3.context.production_accumulation.v0", None):
        with pytest.raises(ValueError, match="version"):
            decode_production_action_json(
                encoded(
                    {
                        "version": version,
                        "workspace_handle": None,
                        "workspace_id": None,
                        "segment_id": None,
                    },
                    "admit_generation_destination_v2",
                )
            )
    with pytest.raises(ValueError, match="closed"):
        decode_production_action_json(
            encoded(
                {
                    "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
                    "workspace_handle": handle,
                    "workspace_id": "workspace_a",
                    "unexpected": True,
                },
                "read_accumulated_project",
            )
        )


def test_versioned_empty_project_survives_failure_and_first_success_commits_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    admitted = harness.production_action(
        "admit.v2.failed",
        "admit_generation_destination_v2",
        {
            "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
            "workspace_handle": None,
            "workspace_id": None,
            "segment_id": None,
        },
    )
    assert admitted.status == 201
    empty = admitted.projection
    assert isinstance(empty, ProductionAccumulatedProjectProjection)
    assert empty.workspace is None
    assert len(empty.attempts) == 1 and empty.attempts[0].status == "admitted"
    replayed = harness.production_action(
        "admit.v2.failed",
        "admit_generation_destination_v2",
        {
            "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
            "workspace_handle": None,
            "workspace_id": None,
            "segment_id": None,
        },
    )
    assert replayed.status == 201
    assert replayed.projection == empty
    assert len(harness.production._empty_projects) == 1

    prepared = harness.prepare("v2-failed", harness.context("v2-failed"), "admit.v2.failed")
    failed = harness.close("v2-failed", harness.submit("v2-failed", prepared), "error")
    authority = failed.production_member_authority
    assert authority is not None
    after_failure = harness.production_action(
        "read.v2.failed",
        "read_accumulated_project",
        {
            "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
            "workspace_handle": authority.workspace_handle,
            "workspace_id": authority.workspace_id,
        },
    ).projection
    assert isinstance(after_failure, ProductionAccumulatedProjectProjection)
    assert after_failure.workspace is None
    assert len(after_failure.attempts) == 1
    assert after_failure.attempts[0].status == "failed"

    next_admission = harness.production_action(
        "admit.v2.success",
        "admit_generation_destination_v2",
        {
            "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
            "workspace_handle": authority.workspace_handle,
            "workspace_id": authority.workspace_id,
            "segment_id": None,
        },
    )
    assert next_admission.status == 200
    prepared = harness.prepare("v2-success", harness.context("v2-success"), "admit.v2.success")
    succeeded = harness.close("v2-success", harness.submit("v2-success", prepared))
    assert succeeded.disposition == "succeeded"
    committed = harness.production_action(
        "read.v2.success",
        "read_accumulated_project",
        {
            "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
            "workspace_handle": authority.workspace_handle,
            "workspace_id": authority.workspace_id,
        },
    ).projection
    assert isinstance(committed, ProductionAccumulatedProjectProjection)
    assert committed.workspace is not None
    assert len(committed.workspace.segments) == 1
    assert committed.workspace.selected_segment_ids == (committed.workspace.segments[0].segment_id,)
    assert len(committed.workspace.outputs) == 1


def test_versioned_open_admission_is_readable_replayed_exclusive_and_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    admitted = harness.admit_v2("admit.v2.visible")
    project = admitted.projection
    assert admitted.status == 201
    assert isinstance(project, ProductionAccumulatedProjectProjection)

    assert harness.read_v2(project.workspace_handle, project.workspace_id) == project
    assert harness.admit_v2("admit.v2.visible").projection == project
    with pytest.raises(ProductionWorkbenchError) as overlapping:
        harness.admit_v2("admit.v2.overlapping", project)
    assert overlapping.value.code == "destination_busy"
    overlap_projection: Any = overlapping.value.projection
    assert overlap_projection == project

    released = harness.production_action(
        "release.v2.visible",
        "release_generation_destination_v2",
        {
            "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
            "admission_request_id": "admit.v2.visible",
        },
    )
    assert released.status == 204
    after_release = harness.read_v2(project.workspace_handle, project.workspace_id)
    assert after_release.attempts == ()
    assert after_release.project_revision == project.project_revision + 1


def test_versioned_projects_refuse_cross_protocol_admission_read_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    legacy, _ = harness.start("legacy-protocol")
    with pytest.raises(ProductionWorkbenchError) as v2_targets_v1:
        harness.production_action(
            "admit.v2.legacy",
            "admit_generation_destination_v2",
            {
                "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
                "workspace_handle": legacy.workspace_handle,
                "workspace_id": legacy.workspace_id,
                "segment_id": None,
            },
        )
    assert v2_targets_v1.value.code == "accumulation_version_mismatch"
    with pytest.raises(ProductionWorkbenchError) as v2_reads_v1:
        harness.read_v2(legacy.workspace_handle, legacy.workspace_id)
    assert v2_reads_v1.value.code == "accumulation_version_mismatch"

    admitted = harness.admit_v2("admit.v2.protocol")
    project = admitted.projection
    assert isinstance(project, ProductionAccumulatedProjectProjection)
    with pytest.raises(ProductionWorkbenchError) as v1_targets_v2:
        harness.production_action(
            "admit.v1.versioned",
            "admit_generation_destination",
            {
                "workspace_handle": project.workspace_handle,
                "workspace_id": project.workspace_id,
                "segment_id": None,
            },
        )
    assert v1_targets_v2.value.code == "accumulation_version_mismatch"
    with pytest.raises(ProductionWorkbenchError) as wrong_release:
        harness.production_action(
            "release.v1.versioned",
            "release_generation_destination",
            {"admission_request_id": "admit.v2.protocol"},
        )
    assert wrong_release.value.code == "accumulation_version_mismatch"
    assert (
        harness.production_action(
            "release.v2.versioned",
            "release_generation_destination_v2",
            {
                "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
                "admission_request_id": "admit.v2.protocol",
            },
        ).status
        == 204
    )


def test_versioned_preprepare_terminal_settles_without_phantom_content_and_retries_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch, ttl_seconds=600)
    admitted = harness.admit_v2("admit.v2.fast-terminal")
    project = admitted.projection
    assert isinstance(project, ProductionAccumulatedProjectProjection)
    assert project.workspace is None
    assert len(project.attempts) == 1
    original = project.attempts[0]

    settled = harness.production_action(
        "settle.v2.fast-terminal",
        "settle_generation_destination_v2",
        {
            "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
            "admission_request_id": "admit.v2.fast-terminal",
            "terminal": "failed",
        },
    )
    assert settled.status == 200
    terminal_project = settled.projection
    assert isinstance(terminal_project, ProductionAccumulatedProjectProjection)
    assert terminal_project.workspace is None
    assert [
        (
            item.candidate_id,
            item.attempt_id,
            item.status,
            item.recovery,
            item.committed,
        )
        for item in terminal_project.attempts
    ] == [
        (
            original.candidate_id,
            original.attempt_id,
            "failed",
            "retry",
            False,
        )
    ]
    assert harness.read_v2(project.workspace_handle, project.workspace_id) == terminal_project

    replay = harness.production_action(
        "settle.v2.fast-terminal",
        "settle_generation_destination_v2",
        {
            "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
            "admission_request_id": "admit.v2.fast-terminal",
            "terminal": "failed",
        },
    )
    assert replay.projection == terminal_project

    retry = harness.admit_v2(
        "admit.v2.fast-terminal.retry",
        terminal_project,
        original.member_segment_id,
    )
    assert retry.status == 200
    retry_project = retry.projection
    assert isinstance(retry_project, ProductionAccumulatedProjectProjection)
    assert retry_project.attempts[0].candidate_id == original.candidate_id
    assert retry_project.attempts[0].attempt_id != original.attempt_id
    assert retry_project.attempts[0].status == "admitted"

    replayed_original = harness.admit_v2("admit.v2.fast-terminal")
    assert replayed_original.projection == retry_project
    assert harness.read_v2(project.workspace_handle, project.workspace_id) == retry_project


def test_versioned_admission_expiry_advances_project_revision_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch, ttl_seconds=600)
    admitted = harness.admit_v2("admit.v2.expiry-transition")
    project = admitted.projection
    assert isinstance(project, ProductionAccumulatedProjectProjection)
    original = project.attempts[0]
    terminal_project = harness.production_action(
        "settle.v2.expiry-transition",
        "settle_generation_destination_v2",
        {
            "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
            "admission_request_id": "admit.v2.expiry-transition",
            "terminal": "failed",
        },
    ).projection
    assert isinstance(terminal_project, ProductionAccumulatedProjectProjection)
    retry = harness.admit_v2(
        "admit.v2.expiry-transition.retry",
        terminal_project,
        original.member_segment_id,
    ).projection
    assert isinstance(retry, ProductionAccumulatedProjectProjection)

    harness.now[0] += PRODUCTION_DESTINATION_ADMISSION_TTL_SECONDS
    after_expiry = harness.read_v2(project.workspace_handle, project.workspace_id)
    assert after_expiry.project_revision == retry.project_revision + 1
    assert len(after_expiry.attempts) == 1
    assert after_expiry.attempts[0].attempt_id == original.attempt_id
    assert after_expiry.attempts[0].status == "failed"
    harness.now[0] += 1
    assert harness.read_v2(project.workspace_handle, project.workspace_id) == after_expiry


def test_versioned_fast_fail_start_fast_fail_retry_preserves_only_verified_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    first_admission = harness.admit_v2("admit.v2.host-sequence.1")
    project = first_admission.projection
    assert isinstance(project, ProductionAccumulatedProjectProjection)
    first_candidate = project.attempts[0].candidate_id
    project = harness.production_action(
        "settle.v2.host-sequence.1",
        "settle_generation_destination_v2",
        {
            "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
            "admission_request_id": "admit.v2.host-sequence.1",
            "terminal": "failed",
        },
    ).projection
    assert isinstance(project, ProductionAccumulatedProjectProjection)
    assert project.workspace is None

    project, first_success = harness.start_v2("host-sequence-success", project=project)
    assert first_success.disposition == "succeeded"
    assert project.workspace is not None
    first_segment = project.workspace.segments[0].segment_id
    assert first_segment != first_candidate

    second_admission_id = "admit.v2.host-sequence.2"
    admitted = harness.admit_v2(second_admission_id, project)
    admitted_project = admitted.projection
    assert isinstance(admitted_project, ProductionAccumulatedProjectProjection)
    second_candidate = admitted_project.attempts[0].candidate_id
    project = harness.production_action(
        "settle.v2.host-sequence.2",
        "settle_generation_destination_v2",
        {
            "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
            "admission_request_id": second_admission_id,
            "terminal": "cancelled",
        },
    ).projection
    assert isinstance(project, ProductionAccumulatedProjectProjection)
    assert project.workspace is not None
    assert tuple(item.segment_id for item in project.workspace.segments) == (first_segment,)
    assert project.workspace.selected_segment_ids == (first_segment,)
    assert project.attempts[0].candidate_id == second_candidate
    assert (project.attempts[0].status, project.attempts[0].recovery) == (
        "cancelled",
        "retry",
    )

    project, retried = harness.start_v2(
        "host-sequence-retry",
        project=project,
        segment_id=second_candidate,
    )
    assert retried.disposition == "succeeded"
    assert project.workspace is not None
    assert tuple(item.segment_id for item in project.workspace.segments) == (
        first_segment,
        second_candidate,
    )
    assert project.workspace.selected_segment_ids == (first_segment, second_candidate)
    assert all(
        item.candidate_id != second_candidate or (item.status == "succeeded" and item.committed)
        for item in project.attempts
    )


def test_versioned_project_expiry_and_release_keep_gone_semantics_without_revision_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_harness = _Harness(tmp_path, monkeypatch, ttl_seconds=60)
    admitted = empty_harness.admit_v2("admit.v2.expiring")
    empty = admitted.projection
    assert isinstance(empty, ProductionAccumulatedProjectProjection)
    assert (
        empty_harness.production_action(
            "release.v2.expiring",
            "release_generation_destination_v2",
            {
                "version": PRODUCTION_ACCUMULATION_ACTION_VERSION,
                "admission_request_id": "admit.v2.expiring",
            },
        ).status
        == 204
    )
    empty_harness.now[0] += 61
    with pytest.raises(ProductionWorkbenchError) as expired:
        empty_harness.read_v2(empty.workspace_handle, empty.workspace_id)
    assert (expired.value.code, expired.value.status) == ("workspace_gone", 410)
    assert empty.workspace_handle not in empty_harness.production._project_revisions

    released_root = tmp_path / "released"
    released_root.mkdir()
    released_harness = _Harness(released_root, monkeypatch)
    project, _ = released_harness.start_v2("release-versioned")
    assert project.workspace is not None
    released = released_harness.production_action(
        "release.v2.project",
        "release_workspace",
        {
            "workspace_handle": project.workspace_handle,
            "expected_workspace_revision": project.workspace.workspace_revision,
            "expected_workspace_fingerprint": project.workspace.workspace_fingerprint,
        },
    )
    assert released.status == 204
    with pytest.raises(ProductionWorkbenchError) as gone:
        released_harness.read_v2(project.workspace_handle, project.workspace_id)
    assert (gone.value.code, gone.value.status) == ("workspace_gone", 410)
    assert project.workspace_handle not in released_harness.production._project_revisions


def test_versioned_failure_matrix_keeps_one_candidate_and_commits_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch, max_entries=1)
    project, first_failure = harness.start_v2("matrix-failure-1", kind="error")
    assert first_failure.disposition == "failed"
    assert project.workspace is None
    first_candidate = project.attempts[0].candidate_id

    project, second_failure = harness.start_v2("matrix-failure-2", project=project, kind="error")
    assert second_failure.disposition == "failed"
    assert project.workspace is None
    assert len(project.attempts) == 1
    assert project.attempts[0].candidate_id != first_candidate
    retry_candidate = project.attempts[0].candidate_id

    project, retried = harness.start_v2("matrix-retry", project=project, segment_id=retry_candidate)
    assert retried.disposition == "succeeded"
    assert project.workspace is not None
    assert tuple(item.segment_id for item in project.workspace.segments) == (retry_candidate,)
    assert project.workspace.selected_segment_ids == (retry_candidate,)
    assert len(project.workspace.outputs) == 1
    assert harness.production._reserved_registry_bytes() < MAX_PRODUCTION_REGISTRY_BYTES

    with pytest.raises(ProductionWorkbenchError) as full:
        harness.admit_v2("admit.v2.second-project")
    assert (full.value.code, full.value.status) == ("workspace_capacity", 429)


def test_versioned_terminal_and_verification_states_require_verified_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    project, interrupted = harness.start_v2("v2-interrupted", kind="interrupted")
    assert interrupted.disposition == "interrupted"
    assert project.workspace is None
    assert [(row.status, row.recovery, row.committed) for row in project.attempts] == [
        ("cancelled", "retry", False)
    ]

    admitted = harness.admit_v2("admit.v2.verification", project)
    assert admitted.status == 200
    prepared = harness.prepare(
        "v2-verification", harness.context("v2-verification"), "admit.v2.verification"
    )
    submitted = harness.submit("v2-verification", prepared)
    terminal = _required_response(
        harness.coordinator.dispatch(
            _action(
                "terminal.v2.verification",
                "record_terminal",
                {
                    "run_handle": submitted.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                    "queue_prompt_id": "prompt.model.v2-verification",
                    "kind": "success",
                },
            )
        )
    )
    media = harness.output / "v2-verification.mp4"
    media.write_bytes(b"synthetic-v2-verification")
    inspections = 0

    def inspect(_payload: bytes, **expected: object) -> ObservedVideoArtifact:
        nonlocal inspections
        inspections += 1
        if inspections == 1:
            raise SequenceCoordinatorError("artifact_inspector_unavailable", 422)
        frames = expected["expected_frames"]
        assert isinstance(frames, int)
        return ObservedVideoArtifact("mp4", (frames, 512, 512, 3))

    monkeypatch.setattr(harness.coordinator, "_artifact_inspector", inspect)
    artifact_payload: dict[str, object] = {
        "run_handle": submitted.run_handle,
        "expected_state_fingerprint": terminal.sequence.state.fingerprint,
        "queue_prompt_id": "prompt.model.v2-verification",
        "output_node_id": "node.save.video",
        "locator": {"filename": media.name, "subfolder": "", "type": "output"},
    }
    with pytest.raises(SequenceCoordinatorError) as first_verification:
        harness.coordinator.dispatch(
            _action("artifact.v2.verification.failed", "record_artifact", artifact_payload)
        )
    assert first_verification.value.retry_disposition == "retry_output_verification"
    failed = harness.read_v2(project.workspace_handle, project.workspace_id)
    assert failed.workspace is None
    assert [(row.status, row.recovery, row.committed) for row in failed.attempts] == [
        ("output_verification_failed", "verify_output", False)
    ]

    current = _required_response(
        harness.coordinator.dispatch(
            _action(
                "read.v2.verification.failed",
                "read_managed_run",
                {"run_handle": submitted.run_handle},
            )
        )
    )
    artifact_payload["expected_state_fingerprint"] = current.sequence.state.fingerprint
    completed = _required_response(
        harness.coordinator.dispatch(
            _action("artifact.v2.verification.retry", "record_artifact", artifact_payload)
        )
    )
    assert completed.disposition == "succeeded"
    assert completed.sequence.progress[0].attempt == 1
    assert completed.sequence.progress[0].queue_prompt_id == "prompt.model.v2-verification"
    committed = harness.read_v2(project.workspace_handle, project.workspace_id)
    assert committed.workspace is not None
    assert len(committed.workspace.segments) == 1
    assert len(committed.workspace.outputs) == 1
    assert committed.attempts[0].committed is True
    replay = _required_response(
        harness.coordinator.dispatch(
            _action("artifact.v2.verification.retry", "record_artifact", artifact_payload)
        )
    )
    assert replay.to_wire() == completed.to_wire()
    assert inspections == 2

    admitted = harness.admit_v2("admit.v2.unknown", committed)
    assert admitted.status == 200
    prepared = harness.prepare("v2-unknown", harness.context("v2-unknown"), "admit.v2.unknown")
    submitted = harness.submit("v2-unknown", prepared)
    harness.coordinator._managed.release(submitted.run_handle)
    unknown = harness.read_v2(project.workspace_handle, project.workspace_id)
    assert unknown.workspace is not None and committed.workspace is not None
    assert (
        unknown.workspace.workspace_revision,
        unknown.workspace.workspace_fingerprint,
        unknown.workspace.segments,
        unknown.workspace.outputs,
        unknown.workspace.selected_segment_ids,
    ) == (
        committed.workspace.workspace_revision,
        committed.workspace.workspace_fingerprint,
        committed.workspace.segments,
        committed.workspace.outputs,
        committed.workspace.selected_segment_ids,
    )
    assert [(row.status, row.recovery, row.committed) for row in unknown.attempts] == [
        ("unknown_ownership", None, False)
    ]


def test_first_commit_validation_failure_is_atomic_and_same_close_can_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    admitted = harness.admit_v2("admit.v2.atomic")
    project = admitted.projection
    assert isinstance(project, ProductionAccumulatedProjectProjection)
    prepared = harness.prepare("atomic", harness.context("atomic"), "admit.v2.atomic")
    submitted = harness.submit("atomic", prepared)
    original_projection = harness.production._projection

    def fail_first_commit(handle: str, entry: Any) -> Any:
        if handle in harness.production._empty_projects:
            raise ProductionWorkbenchError("workspace_capacity", 429)
        return original_projection(handle, entry)

    monkeypatch.setattr(harness.production, "_projection", fail_first_commit)
    with pytest.raises(SequenceCoordinatorError) as refused:
        harness.close("atomic", submitted)
    assert (refused.value.code, refused.value.status) == ("workspace_capacity", 429)
    unchanged = harness.read_v2(project.workspace_handle, project.workspace_id)
    assert unchanged.workspace is None
    assert len(unchanged.attempts) == 1

    monkeypatch.setattr(harness.production, "_projection", original_projection)
    current = _required_response(
        harness.coordinator.dispatch(
            _action(
                "read.atomic.after-refusal",
                "read_managed_run",
                {"run_handle": submitted.run_handle},
            )
        )
    )
    reconciled = harness.close("atomic", current)
    assert reconciled.disposition == "succeeded"
    committed = harness.read_v2(project.workspace_handle, project.workspace_id)
    assert committed.workspace is not None
    assert len(committed.workspace.segments) == 1
    assert len(committed.workspace.outputs) == 1


def test_two_and_three_independent_starts_accumulate_one_stable_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    first, first_closed = harness.start("first", seconds=8.0)
    assert first_closed.disposition == "succeeded"
    wire = _member_wire(first_closed)
    authority = first_closed.production_member_authority
    assert authority is not None
    assert wire["production_member_authority"] == {
        "schema": COORDINATOR_PRODUCTION_MEMBER_AUTHORITY_SCHEMA,
        "workspace_handle": first.workspace_handle,
        "workspace_id": first.workspace_id,
        "member_segment_id": first.segments[0].segment_id,
    }
    first_receipt = harness.row(first.workspace_handle, first.segments[0].segment_id).completed
    first_preview = harness.production.admit_media_preview_source(
        workspace_handle=first.workspace_handle,
        expected_workspace_revision=first.workspace_revision,
        expected_workspace_fingerprint=first.workspace_fingerprint,
        output_handle=first.outputs[0].output_handle,
    )

    second, second_closed = harness.start("second", project=first, seconds=4.0)
    _member_wire(second_closed)
    third, third_closed = harness.start("third", project=second, seconds=6.0)
    _member_wire(third_closed)

    assert first.workspace_handle == second.workspace_handle == third.workspace_handle
    assert first.workspace_id == second.workspace_id == third.workspace_id
    assert [len(item.segments) for item in (first, second, third)] == [1, 2, 3]
    assert tuple(row.segment_id for row in second.segments) == (
        first.segments[0].segment_id,
        second.segments[1].segment_id,
    )
    assert tuple(row.segment_id for row in third.segments)[:2] == tuple(
        row.segment_id for row in second.segments
    )
    # Every earlier output keeps its handle, receipt object and preview across later appends.
    assert _outputs(third) == {**_outputs(second), **_outputs(third)}
    assert _outputs(second).items() <= _outputs(third).items()
    assert _outputs(first).items() <= _outputs(third).items()
    assert all(row.preview for row in third.outputs)
    assert (
        harness.row(third.workspace_handle, first.segments[0].segment_id).completed is first_receipt
    )
    assert harness.production.media_preview_source_is_current(
        workspace_handle=first.workspace_handle,
        expected_workspace_revision=first.workspace_revision,
        expected_workspace_fingerprint=first.workspace_fingerprint,
        source=first_preview,
    )
    expected_frames = [
        harness.frames(harness.contexts[name]) for name in ("first", "second", "third")
    ]
    assert [row.frame_count for row in third.segments] == expected_frames
    assert sum(row.delivered_milliseconds for row in third.segments) == sum(
        harness.sidebar.claim_production_seed(
            harness.contexts[name]
        ).duration.resolved.delivered_milliseconds
        for name in ("first", "second", "third")
    )
    assert (third.run_state, third.run_completed, third.run_total) == ("succeeded", 3, 3)
    # One member, one run and one single-job plan per Start; no aggregate plan or receipt exists.
    members = harness.production._entries[third.workspace_handle].members
    assert members is not None and len(members) == 3
    assert all(len(row.latest.generation_sequence.state.plan.jobs) == 1 for row in members.values())
    assert (
        len({row.latest.generation_sequence.state.plan.fingerprint for row in members.values()})
        == 3
    )
    assert len(harness.coordinator._runs) == 3
    assert third.generation_sequence is None
    assert (
        harness.production._entries[third.workspace_handle].accepted_authorities.generation_sequence
        is None
    )


def test_replayed_admission_prepare_and_publication_deliver_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    project, _ = harness.start("seed")
    admitted = harness.admit("admit.replay", project)
    assert admitted.status == 200
    replayed = harness.admit("admit.replay", project)
    assert (replayed.status, replayed.projection) == (200, admitted.projection)
    with pytest.raises(ProductionWorkbenchError) as conflict:
        harness.admit("admit.replay", project, project.segments[0].segment_id)
    assert (conflict.value.code, conflict.value.status) == ("request_id_conflict", 409)

    context = harness.context("replay")
    prepared = harness.prepare("replay", context, "admit.replay")
    again = harness.prepare("replay", context, "admit.replay")
    assert again is prepared
    submitted = harness.submit("replay", prepared)
    assert harness.submit("replay", prepared) is submitted
    closed = harness.close("replay", submitted)
    assert harness.close("replay", submitted) is closed
    current = harness.read(project.workspace_handle)
    assert len(current.segments) == 2
    assert len(harness.coordinator._runs) == 2
    assert len(harness.coordinator._managed.live_handles()) == 0
    # A consumed admission replays its original result but cannot prepare a second member.
    assert harness.admit("admit.replay", project).status == 200
    with pytest.raises(SequenceCoordinatorError) as consumed:
        harness.coordinator.dispatch(
            _action(
                "prepare.replay.second-delivery",
                "prepare_managed_run",
                {
                    "context_workspace_handle": context,
                    "correlation": {"prompt_id": "prompt.x", "execution_node_id": "node.x"},
                    "observation": {**_observation(), "expected_frames": harness.frames(context)},
                    "production_admission_request_id": "admit.replay",
                },
            )
        )
    assert (consumed.value.code, consumed.value.status) == ("admission_unavailable", 409)
    assert len(harness.read(project.workspace_handle).segments) == 2
    with pytest.raises(SequenceCoordinatorError) as prepare_conflict:
        harness.coordinator.dispatch(
            _action(
                "prepare.replay",
                "prepare_managed_run",
                {
                    "context_workspace_handle": context,
                    "correlation": {"prompt_id": "prompt.other", "execution_node_id": "node.x"},
                    "observation": _observation(),
                    "production_admission_request_id": "admit.replay",
                },
            )
        )
    assert prepare_conflict.value.code == "request_id_conflict"


def test_admission_refuses_busy_mismatched_and_exhausted_destinations_before_any_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch, max_entries=3)
    created = harness.admit("admit.create")
    assert (created.status, created.projection) == (202, None)
    assert harness.admit("admit.create").status == 202
    manual = harness.production_action(
        "manual.create",
        "create_workspace_from_context",
        {"context_workspace_handle": harness.context("manual")},
    ).projection
    assert manual is not None
    before = (dict(harness.production._ledger), harness.read(manual.workspace_handle))

    def refused(code: str, status: int, *args: Any) -> None:
        with pytest.raises(ProductionWorkbenchError) as error:
            harness.admit(*args)
        assert (error.value.code, error.value.status) == (code, status)

    wrong_id = replace(manual, workspace_id="workspace_foreign")
    refused("destination_mismatch", 409, "admit.mismatch", wrong_id)
    refused("destination_mismatch", 409, "admit.unknown-member", manual, "segment_unknown")
    assert harness.admit("admit.append", manual).status == 200
    refused("destination_busy", 409, "admit.busy", manual)
    refused("destination_busy", 409, "admit.busy-member", manual, manual.segments[0].segment_id)
    assert harness.admit("admit.create.second").status == 202
    # One manual entry plus two create admissions occupy all three entry slots, for every writer.
    refused("workspace_capacity", 429, "admit.create.over")
    with pytest.raises(ProductionWorkbenchError) as manual_over:
        harness.production_action(
            "manual.create.over",
            "create_workspace_from_context",
            {"context_workspace_handle": harness.context("manual")},
        )
    assert manual_over.value.code == "workspace_capacity"
    assert (dict(harness.production._ledger), harness.read(manual.workspace_handle)) == before
    assert (
        harness.production_action(
            "release.append",
            "release_generation_destination",
            {"admission_request_id": "admit.append"},
        ).status
        == 204
    )
    assert (
        harness.production_action(
            "release.append.again",
            "release_generation_destination",
            {"admission_request_id": "admit.append"},
        ).status
        == 204
    )
    with pytest.raises(ProductionWorkbenchError) as closed:
        harness.admit("admit.append", manual)
    assert closed.value.code == "admission_closed"
    assert harness.admit("admit.append.after-release", manual).status == 200

    harness.production_action(
        "release.create.second",
        "release_generation_destination",
        {"admission_request_id": "admit.create.second"},
    )
    monkeypatch.setattr(production_module, "MAX_PRODUCTION_DESTINATION_ADMISSIONS", 2)
    other = harness.production_action(
        "manual.create.table",
        "create_workspace_from_context",
        {"context_workspace_handle": harness.context("table")},
    )
    assert other.status == 201
    refused("admission_capacity", 429, "admit.table", other.projection)

    missing = replace(manual, workspace_handle="pw_" + "z" * 43)
    with pytest.raises(ProductionWorkbenchError) as unknown:
        harness.admit("admit.missing", missing)
    assert unknown.value.status == 404


def test_the_sixty_four_member_boundary_refuses_append_but_admits_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    projection = harness.production_action(
        "boundary.create",
        "create_workspace_from_context",
        {"context_workspace_handle": harness.context("boundary")},
    ).projection
    assert projection is not None
    for index in range(63):
        projection = harness.production_action(
            f"boundary.add.{index}",
            "add_segment_from_context",
            {
                "workspace_handle": projection.workspace_handle,
                "expected_workspace_revision": projection.workspace_revision,
                "expected_workspace_fingerprint": projection.workspace_fingerprint,
                "context_workspace_handle": harness.context("boundary"),
                "relation": "independent",
                "predecessor_segment_id": None,
            },
        ).projection
        assert projection is not None
    assert len(projection.segments) == 64
    with pytest.raises(ProductionWorkbenchError) as capacity:
        harness.admit("boundary.append", projection)
    assert (capacity.value.code, capacity.value.status) == ("project_capacity", 429)
    target = projection.segments[40].segment_id
    assert harness.admit("boundary.regenerate", projection, target).status == 200
    prepared = harness.prepare("boundary", harness.context("boundary"), "boundary.regenerate")
    closed = harness.close("boundary", harness.submit("boundary", prepared))
    current = harness.read(projection.workspace_handle)
    assert len(current.segments) == 64
    assert tuple(row.segment_id for row in current.outputs) == (target,)
    assert closed.production_member_authority is not None
    assert closed.production_member_authority.member_segment_id == target


def test_failed_automatic_append_stays_provisional_until_verified_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    project, _ = harness.start_v2("kept-before-failure")
    assert project.workspace is not None
    original_segment_ids = tuple(row.segment_id for row in project.workspace.segments)
    original_selection = project.workspace.selected_segment_ids
    original_outputs = _outputs(project.workspace)

    failed, failed_closed = harness.start_v2("provisional-failure", project=project, kind="error")

    assert failed_closed.disposition == "failed"
    assert failed.workspace is not None
    assert tuple(row.segment_id for row in failed.workspace.segments) == original_segment_ids
    assert failed.workspace.selected_segment_ids == original_selection
    assert _outputs(failed.workspace) == original_outputs

    succeeded, succeeded_closed = harness.start_v2("after-failure", project=failed)

    assert succeeded_closed.disposition == "succeeded"
    assert succeeded.workspace is not None
    assert len(succeeded.workspace.segments) == len(project.workspace.segments) + 1
    assert tuple(
        row.segment_id for row in succeeded.workspace.segments[: len(project.workspace.segments)]
    ) == (original_segment_ids)
    assert set(_outputs(succeeded.workspace)).issuperset(original_outputs)


def test_failure_cancellation_cleanup_lost_runs_and_rollback_never_remove_earlier_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    project, _ = harness.start("kept")
    kept = _outputs(project)

    failed, failed_closed = harness.start("failed", project=project, kind="error")
    assert failed_closed.disposition == "failed"
    failed_segment = failed.segments[1].segment_id
    assert failed.segments[1].job_state == "failed"
    assert failed.run_state == "failed"
    assert _outputs(failed) == kept

    assert harness.admit("admit.pre-submit", failed).status == 200
    prepared = harness.prepare("pre-submit", harness.context("pre-submit"), "admit.pre-submit")
    released = harness.release("pre-submit", prepared, "cleanup_pre_submit")
    assert released.disposition == "released"
    cleaned = harness.read(project.workspace_handle)
    assert cleaned.segments[2].job_state == "cancelled"
    assert cleaned.run_state == "cancelled"
    assert _outputs(cleaned) == kept

    assert harness.admit("admit.cancel", cleaned).status == 200
    prepared = harness.prepare("cancel", harness.context("cancel"), "admit.cancel")
    submitted = harness.submit("cancel", prepared)
    cancelled = _required_response(
        harness.coordinator.dispatch(
            _action(
                "cancel.cancel",
                "cancel_sequence",
                {
                    "run_handle": submitted.run_handle,
                    "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                },
            )
        )
    )
    assert cancelled.disposition == "unknown_ownership"
    after_cancel = harness.read(project.workspace_handle)
    assert after_cancel.segments[3].job_state == "unknown_ownership"
    assert _outputs(after_cancel) == kept

    # A run lost without a terminal leaves an unknown member; the project is not busy.
    assert harness.admit("admit.lost", after_cancel).status == 200
    prepared = harness.prepare("lost", harness.context("lost"), "admit.lost")
    submitted = harness.submit("lost", prepared)
    lost_projection = harness.read(project.workspace_handle)
    assert lost_projection.segments[4].job_state == "submitted"
    assert lost_projection.run_state == "running"
    with pytest.raises(ProductionWorkbenchError) as busy:
        harness.admit("admit.while-live", lost_projection)
    assert busy.value.code == "destination_busy"
    harness.coordinator._managed.release(submitted.run_handle)
    lost = harness.read(project.workspace_handle)
    assert lost.segments[4].job_state == "unknown_ownership"
    assert lost.run_state == "failed"
    lost_segment = lost.segments[4].segment_id
    assert harness.admit("admit.lost.retry", lost, lost_segment).status == 200
    assert (
        harness.production_action(
            "release.lost.retry",
            "release_generation_destination",
            {"admission_request_id": "admit.lost.retry"},
        ).status
        == 204
    )

    # Prepare failure after publication rolls back only the new member.
    before_failure = harness.read(project.workspace_handle)
    assert harness.admit("admit.rollback", before_failure).status == 200
    original_confirm = harness.production.confirm_member_run

    def failing_confirm(publication: Any) -> None:
        raise ProductionWorkbenchError("member_publication_authority", 409)

    monkeypatch.setattr(harness.production, "confirm_member_run", failing_confirm)
    with pytest.raises(SequenceCoordinatorError) as rollback:
        harness.prepare("rollback", harness.context("rollback"), "admit.rollback")
    assert rollback.value.code == "member_publication_authority"
    assert harness.production._admissions["admit.rollback"].state == "consumed"
    assert harness.coordinator._managed.live_handles() == ()
    monkeypatch.setattr(harness.production, "confirm_member_run", original_confirm)
    assert harness.read(project.workspace_handle).segments == before_failure.segments
    assert _outputs(harness.read(project.workspace_handle)) == kept
    assert not any(run == "prepare.rollback" for run in harness.coordinator._ledger), (
        "a refused prepare must not leave a replay row"
    )

    # A refusal before publication releases the admission, so the project is admissible at once.
    assert harness.admit("admit.refused", before_failure).status == 200
    with pytest.raises(SequenceCoordinatorError) as refused_prepare:
        harness.coordinator.dispatch(
            _action(
                "prepare.refused",
                "prepare_managed_run",
                {
                    "context_workspace_handle": "ws_unknown_context_handle_for_member",
                    "correlation": {"prompt_id": "prompt.x", "execution_node_id": "node.x"},
                    "observation": _observation(),
                    "production_admission_request_id": "admit.refused",
                },
            )
        )
    assert refused_prepare.value.status in {400, 404}
    assert harness.production._admissions["admit.refused"].state == "released"
    assert harness.read(project.workspace_handle).segments == before_failure.segments

    # Retry keeps the segment and replaces output only after verification.
    retry_prepared_admission = harness.admit("admit.retry.failed", before_failure, failed_segment)
    assert retry_prepared_admission.status == 200
    prepared = harness.prepare("retry", harness.context("retry"), "admit.retry.failed")
    running = harness.read(project.workspace_handle)
    assert running.segments[1].segment_id == failed_segment
    assert running.segments[1].job_state == "planned"
    closed = harness.close("retry", harness.submit("retry", prepared))
    assert closed.disposition == "succeeded"
    retried = harness.read(project.workspace_handle)
    assert retried.segments[1].segment_id == failed_segment
    assert set(_outputs(retried)) == {project.segments[0].segment_id, failed_segment}

    # Regenerating a completed member keeps its output through failure and replaces it on success.
    kept_segment = project.segments[0].segment_id
    assert harness.admit("admit.regenerate.fail", retried, kept_segment).status == 200
    prepared = harness.prepare(
        "regenerate-fail", harness.context("regenerate-fail"), "admit.regenerate.fail"
    )
    assert _outputs(harness.read(project.workspace_handle))[kept_segment] == kept[kept_segment]
    harness.close("regenerate-fail", harness.submit("regenerate-fail", prepared), "error")
    after_failed_regeneration = harness.read(project.workspace_handle)
    assert _outputs(after_failed_regeneration)[kept_segment] == kept[kept_segment]
    _, regenerated_closed = harness.start(
        "regenerate-ok", project=after_failed_regeneration, segment_id=kept_segment
    )
    regenerated = harness.read(project.workspace_handle)
    assert regenerated.segments[0].segment_id == kept_segment
    assert _outputs(regenerated)[kept_segment] != kept[kept_segment]
    assert regenerated_closed.production_member_authority is not None
    assert regenerated_closed.production_member_authority.member_segment_id == kept_segment


def test_create_intent_rollback_removes_only_the_unpublished_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    assert harness.admit("admit.create.rollback").status == 202

    original_advance = harness.coordinator._managed.advance

    def failing_advance(run_handle: str, trigger: Any, **values: Any) -> Any:
        if trigger.name == "PREPARE_SEQUENCE":
            raise RuntimeError("synthetic aggregate failure")
        return original_advance(run_handle, trigger, **values)

    monkeypatch.setattr(harness.coordinator._managed, "advance", failing_advance)
    with pytest.raises(SequenceCoordinatorError) as error:
        harness.prepare(
            "create-rollback", harness.context("create-rollback"), "admit.create.rollback"
        )
    assert error.value.code == "managed_prepare_failed"
    assert harness.production._entries == {}
    assert harness.production._tombstones == {}
    assert harness.coordinator._managed.live_handles() == ()
    assert harness.coordinator._runs == {}


def test_member_claims_and_previews_survive_appends_selection_and_reorder_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    project, _ = harness.start("claimed")
    segment_id = project.segments[0].segment_id
    output_handle = project.outputs[0].output_handle

    def claim(current: ProductionWorkbenchProjection, pairs: tuple[tuple[str, str], ...]) -> Any:
        return harness.production.claim_authoring_output_batch(
            workspace_handle=current.workspace_handle,
            workspace_id=current.workspace_id,
            expected_workspace_revision=current.workspace_revision,
            expected_workspace_fingerprint=current.workspace_fingerprint,
            pairs=pairs,
        )

    (raw,) = claim(project, ((segment_id, output_handle),))
    assert raw.current()
    preview = harness.production.admit_media_preview_source(
        workspace_handle=project.workspace_handle,
        expected_workspace_revision=project.workspace_revision,
        expected_workspace_fingerprint=project.workspace_fingerprint,
        output_handle=output_handle,
    )
    appended, _ = harness.start("appended", project=project)
    assert raw.current()
    selected = harness.production_action(
        "claims.select",
        "set_selection",
        {
            "workspace_handle": appended.workspace_handle,
            "expected_workspace_revision": appended.workspace_revision,
            "expected_workspace_fingerprint": appended.workspace_fingerprint,
            "segment_ids": [appended.segments[1].segment_id],
        },
    ).projection
    assert raw.current()
    reordered = harness.production_action(
        "claims.reorder",
        "reorder_segments",
        {
            "workspace_handle": selected.workspace_handle,
            "expected_workspace_revision": selected.workspace_revision,
            "expected_workspace_fingerprint": selected.workspace_fingerprint,
            "segment_ids": [selected.segments[1].segment_id, segment_id],
        },
    ).projection
    assert reordered is not None
    assert raw.current()
    assert harness.production.media_preview_source_is_current(
        workspace_handle=project.workspace_handle,
        expected_workspace_revision=project.workspace_revision,
        expected_workspace_fingerprint=project.workspace_fingerprint,
        source=preview,
    )
    assert _outputs(reordered)[segment_id] == output_handle
    # A selection that no longer includes the member refuses a new claim for it.
    with pytest.raises(ProductionWorkbenchError) as unselected:
        claim(reordered, ((segment_id, output_handle),))
    assert unselected.value.code == "output_unknown"

    # A stale segment join fails: another member's output never answers for this segment.
    other_segment = reordered.segments[0].segment_id
    with pytest.raises(ProductionWorkbenchError) as stale_segment:
        claim(reordered, ((other_segment, output_handle),))
    assert stale_segment.value.code == "output_unknown"

    foreign_project, _ = harness.start("foreign")
    with pytest.raises(ProductionWorkbenchError) as foreign:
        claim(
            reordered,
            ((reordered.segments[0].segment_id, foreign_project.outputs[0].output_handle),),
        )
    assert foreign.value.code == "output_unknown"
    foreign_receipt = harness.row(
        foreign_project.workspace_handle, foreign_project.segments[0].segment_id
    ).completed.artifact_receipt
    assert not replace(raw, receipt=foreign_receipt).current()
    assert not replace(raw, lineage_token=object()).current()
    assert not replace(raw, workspace_handle=foreign_project.workspace_handle).current()

    deleted = harness.production_action(
        "claims.delete",
        "delete_segment",
        {
            "workspace_handle": reordered.workspace_handle,
            "expected_workspace_revision": reordered.workspace_revision,
            "expected_workspace_fingerprint": reordered.workspace_fingerprint,
            "segment_id": segment_id,
        },
    ).projection
    assert deleted is not None
    assert not raw.current()
    assert not harness.production.media_preview_source_is_current(
        workspace_handle=project.workspace_handle,
        expected_workspace_revision=project.workspace_revision,
        expected_workspace_fingerprint=project.workspace_fingerprint,
        source=preview,
    )
    remaining_segment = deleted.segments[0].segment_id
    (second_claim,) = claim(
        harness.production_action(
            "claims.select.remaining",
            "set_selection",
            {
                "workspace_handle": deleted.workspace_handle,
                "expected_workspace_revision": deleted.workspace_revision,
                "expected_workspace_fingerprint": deleted.workspace_fingerprint,
                "segment_ids": [remaining_segment],
            },
        ).projection,
        ((remaining_segment, _outputs(deleted)[remaining_segment]),),
    )
    assert second_claim.current()
    current = harness.read(deleted.workspace_handle)
    replaced = harness.production_action(
        "claims.replace",
        "replace_segment_from_context",
        {
            "workspace_handle": current.workspace_handle,
            "expected_workspace_revision": current.workspace_revision,
            "expected_workspace_fingerprint": current.workspace_fingerprint,
            "segment_id": remaining_segment,
            "context_workspace_handle": harness.context("replacement", 5.0),
        },
    ).projection
    assert replaced is not None and replaced.outputs == ()
    assert not second_claim.current()

    (foreign_claim,) = claim(
        foreign_project,
        ((foreign_project.segments[0].segment_id, foreign_project.outputs[0].output_handle),),
    )
    assert foreign_claim.current()
    harness.now[0] += 61
    assert not foreign_claim.current(), "an unleased project expires and revokes its claims"
    assert type(foreign_claim) is ProductionAuthoringOutputClaim


def test_release_and_regeneration_revoke_member_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    project, _ = harness.start("release")
    segment_id = project.segments[0].segment_id
    (raw,) = harness.production.claim_authoring_output_batch(
        workspace_handle=project.workspace_handle,
        workspace_id=project.workspace_id,
        expected_workspace_revision=project.workspace_revision,
        expected_workspace_fingerprint=project.workspace_fingerprint,
        pairs=((segment_id, project.outputs[0].output_handle),),
    )
    regenerated, _ = harness.start("regenerated", project=project, segment_id=segment_id)
    assert not raw.current()
    (fresh,) = harness.production.claim_authoring_output_batch(
        workspace_handle=regenerated.workspace_handle,
        workspace_id=regenerated.workspace_id,
        expected_workspace_revision=regenerated.workspace_revision,
        expected_workspace_fingerprint=regenerated.workspace_fingerprint,
        pairs=((segment_id, regenerated.outputs[0].output_handle),),
    )
    assert fresh.current()
    released = harness.production_action(
        "release.project",
        "release_workspace",
        {
            "workspace_handle": regenerated.workspace_handle,
            "expected_workspace_revision": regenerated.workspace_revision,
            "expected_workspace_fingerprint": regenerated.workspace_fingerprint,
        },
    )
    assert released.status == 204
    assert not fresh.current()


def test_admissions_and_live_runs_lease_the_project_across_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch, ttl_seconds=60)
    project, closed = harness.start("leased")
    harness.release("leased", closed, "cleanup_terminal")

    def retained() -> bool:
        with harness.production._lock:
            harness.production._prune(harness.now[0])
            return project.workspace_handle in harness.production._entries

    assert harness.admit("admit.lease", project).status == 200
    harness.now[0] += 250
    assert retained(), "an open admission holds its project"
    harness.now[0] += PRODUCTION_DESTINATION_ADMISSION_TTL_SECONDS
    assert not retained(), "an expired admission releases its slot and lease"
    with pytest.raises(ProductionWorkbenchError) as gone:
        harness.read(project.workspace_handle)
    assert (gone.value.code, gone.value.status) == ("workspace_gone", 410)

    second, second_closed = harness.start("second-project")
    harness.release("second-project", second_closed, "cleanup_terminal")
    assert harness.admit("admit.live", second).status == 200
    prepared = harness.prepare("live", harness.context("live"), "admit.live")

    def second_retained() -> bool:
        with harness.production._lock:
            harness.production._prune(harness.now[0])
            return second.workspace_handle in harness.production._entries

    harness.now[0] += 600
    assert second_retained(), "a live run holds its project past the project TTL"
    harness.release("live", prepared, "cleanup_pre_submit")
    assert second_retained(), "the cancellation publication touches the project"
    harness.now[0] += 61
    assert not second_retained(), "the project expires once no run holds it"

    # A run lease is bounded by the ManagedRun registry's own retention, never indefinite.
    third, third_closed = harness.start("third-project")
    harness.release("third-project", third_closed, "cleanup_terminal")
    assert harness.admit("admit.abandoned", third).status == 200
    harness.prepare("abandoned", harness.context("abandoned"), "admit.abandoned")
    harness.now[0] += 901
    with harness.production._lock:
        harness.production._prune(harness.now[0])
    assert third.workspace_handle not in harness.production._entries


def test_versioned_empty_project_live_run_lease_survives_project_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch, ttl_seconds=60)
    admitted = harness.admit_v2("admit.v2.empty-live")
    project = admitted.projection
    assert isinstance(project, ProductionAccumulatedProjectProjection)
    prepared = harness.prepare("empty-live", harness.context("empty-live"), "admit.v2.empty-live")

    def retained() -> bool:
        with harness.production._lock:
            harness.production._prune(harness.now[0])
            return project.workspace_handle in harness.production._empty_projects

    harness.now[0] += 600
    assert retained(), "a live first generation holds its empty project past the project TTL"
    harness.release("empty-live", prepared, "cleanup_pre_submit")
    assert retained(), "cancellation publication renews the empty project"
    harness.now[0] += 61
    assert not retained(), "the empty project expires after its run lease closes"


def test_admission_expiry_releases_the_slot_on_a_virtual_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch, max_entries=1)
    assert harness.admit("admit.expiring").status == 202
    with pytest.raises(ProductionWorkbenchError) as full:
        harness.admit("admit.waiting")
    assert full.value.code == "workspace_capacity"
    harness.now[0] += PRODUCTION_DESTINATION_ADMISSION_TTL_SECONDS
    assert harness.admit("admit.waiting").status == 202
    with pytest.raises(SequenceCoordinatorError) as expired:
        harness.prepare("expired", harness.context("expired"), "admit.expiring")
    assert expired.value.code == "admission_unavailable"


def test_legacy_single_clip_project_converts_without_changing_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    context = harness.context("legacy")
    prepared = _required_response(
        harness.coordinator.dispatch(
            _action(
                "legacy.prepare",
                "prepare_managed_run",
                {
                    "context_workspace_handle": context,
                    "correlation": {"prompt_id": "prompt.legacy", "execution_node_id": "node.x"},
                    "observation": {**_observation(), "expected_frames": harness.frames(context)},
                },
            )
        )
    )
    assert prepared.schema == "h3.context.generation_coordinator.response.v1"
    closed = harness.close("legacy", harness.submit("legacy", prepared))
    legacy = closed.production
    receipt = harness.production._entries[
        legacy.workspace_handle
    ].accepted_authorities.artifact_receipts[0]
    (legacy_claim,) = harness.production.claim_authoring_output_batch(
        workspace_handle=legacy.workspace_handle,
        workspace_id=legacy.workspace_id,
        expected_workspace_revision=legacy.workspace_revision,
        expected_workspace_fingerprint=legacy.workspace_fingerprint,
        pairs=((legacy.segments[0].segment_id, legacy.outputs[0].output_handle),),
    )
    harness.release("legacy", closed, "cleanup_terminal")

    converted, _ = harness.start("after-legacy", project=legacy)
    assert converted.workspace_handle == legacy.workspace_handle
    assert converted.segments[0].segment_id == legacy.segments[0].segment_id
    assert _outputs(legacy).items() <= _outputs(converted).items()
    row = harness.row(converted.workspace_handle, legacy.segments[0].segment_id)
    assert row.completed.artifact_receipt is receipt
    assert row.completed.authority_workspace is receipt_workspace(harness, receipt)
    assert legacy_claim.current()
    assert converted.outputs[0].preview


def receipt_workspace(harness: _Harness, receipt: Any) -> Any:
    for entry in harness.production._entries.values():
        for row in (entry.members or {}).values():
            if row.completed is not None and row.completed.artifact_receipt is receipt:
                return row.completed.generation_sequence.state.plan.source_workspace_authority
    raise AssertionError("receipt is not retained")


def test_completed_serial_project_converts_without_changing_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_managed_generation_publication_bridge import _PublicationBridge

    bridge = _PublicationBridge(tmp_path, monkeypatch)
    bridge.complete_child(0)
    bridge.complete_child(1)
    serial = bridge.read()
    receipts = tuple(bridge.receipts)
    assert len(serial.outputs) == 2
    source = _source(bridge.sidebar, user_intent="An appended independent member.")
    frames = bridge.sidebar.claim_production_seed(source.workspace_id).duration.resolved.frame_count
    monkeypatch.setattr(
        bridge.coordinator,
        "_artifact_inspector",
        lambda _payload, **_expected: ObservedVideoArtifact("mp4", (frames, 512, 512, 3)),
    )
    admitted = bridge.production.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "serial.admit",
            "action": "admit_generation_destination",
            "payload": {
                "workspace_handle": serial.workspace_handle,
                "workspace_id": serial.workspace_id,
                "segment_id": None,
            },
        }
    )
    assert admitted.status == 200
    prepared = _required_response(
        bridge.coordinator.dispatch(
            _action(
                "serial.member.prepare",
                "prepare_managed_run",
                {
                    "context_workspace_handle": source.workspace_id,
                    "correlation": {"prompt_id": "prompt.serial", "execution_node_id": "node.x"},
                    "observation": {**_observation(), "expected_frames": frames},
                    "production_admission_request_id": "serial.admit",
                },
            )
        )
    )
    assert prepared.schema == MANAGED_MEMBER_RESPONSE_SCHEMA
    converted = bridge.read()
    assert converted.workspace_handle == serial.workspace_handle
    assert tuple(row.segment_id for row in converted.segments)[:2] == tuple(
        row.segment_id for row in serial.segments
    )
    assert _outputs(serial).items() <= _outputs(converted).items()
    members = bridge.production._entries[serial.workspace_handle].members
    assert members is not None
    rows = members

    def completed(segment_id: str) -> ProductionMemberAttemptV1:
        attempt = rows[segment_id].completed
        assert attempt is not None
        return attempt

    assert tuple(completed(row.segment_id).artifact_receipt for row in serial.segments) == receipts
    assert all(
        completed(row.segment_id).artifact_receipt is receipt
        for row, receipt in zip(serial.segments, receipts, strict=True)
    )
    assert bridge.production._entries[serial.workspace_handle].automatic_plan is None

    # A converted serial member keeps its two-job sequence, which no member response may carry.
    first = completed(serial.segments[0].segment_id)
    assert len(first.generation_sequence.state.plan.jobs) == 2
    private = bridge.production.project_managed_child_generation(
        first.authority_workspace, serial.workspace_handle, first.generation_sequence
    )
    with pytest.raises(SequenceCoordinatorError) as second_job:
        SequenceCoordinatorResponse(
            prepared.run_handle,
            "current",
            first.generation_sequence,
            private,
            None,
            None,
            MANAGED_MEMBER_RESPONSE_SCHEMA,
            production_member_authority=SequenceProductionMemberAuthorityV1(
                serial.workspace_handle, serial.workspace_id, serial.segments[0].segment_id
            ),
        )
    assert second_job.value.code == "cross_authority_response"


def test_whole_project_writers_and_plan_import_refuse_member_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_production_import import _authorities, _Materializer, _request

    harness = _Harness(tmp_path, monkeypatch)
    project, closed = harness.start("writers")
    with pytest.raises(ProductionWorkbenchError) as replace_error:
        harness.production.replace_generation_authority(
            project.workspace_handle,
            expected_workspace_revision=project.workspace_revision,
            expected_workspace_fingerprint=project.workspace_fingerprint,
            expected_sequence_state_fingerprint=None,
            generation_sequence=closed.sequence,
        )
    assert (replace_error.value.code, replace_error.value.status) == ("member_mode_authority", 409)
    with pytest.raises(ProductionWorkbenchError) as attach_error:
        harness.production.attach_accepted_authorities(
            project.workspace_handle,
            expected_workspace_revision=project.workspace_revision,
            expected_workspace_fingerprint=project.workspace_fingerprint,
            authorities=production_module.ProductionAcceptedAuthorities(),
        )
    assert attach_error.value.code == "member_mode_authority"
    with pytest.raises(ProductionWorkbenchError) as assembly_error:
        harness.production_action(
            "writers.assemble",
            "cancel_assembly",
            {
                "workspace_handle": project.workspace_handle,
                "workspace_id": project.workspace_id,
                "expected_workspace_revision": project.workspace_revision,
                "expected_workspace_fingerprint": project.workspace_fingerprint,
                "expected_assembly_fingerprint": _fp("assembly"),
            },
        )
    assert assembly_error.value.code == "member_mode_authority"
    # The legacy single-clip prepare path cannot overwrite a member project either.
    with pytest.raises(SequenceCoordinatorError) as legacy_prepare:
        harness.coordinator.dispatch(
            _action(
                "writers.legacy.prepare",
                "prepare_sequence",
                {
                    "workspace_handle": project.workspace_handle,
                    "expected_workspace_revision": project.workspace_revision,
                    "expected_workspace_fingerprint": project.workspace_fingerprint,
                    "correlation": {"prompt_id": "prompt.x", "execution_node_id": "node.x"},
                    "observation": {
                        **_observation(),
                        "expected_frames": project.segments[0].frame_count,
                    },
                },
            )
        )
    assert legacy_prepare.value.code == "member_mode_authority"

    context, _admission, proposal = _authorities(parts=(15, 15))
    with pytest.raises(ProductionWorkbenchError) as import_error:
        harness.production.import_automatic_plan(
            _request(project, context, proposal),
            planning_context=context,
            proposal=proposal,
            materialize=_Materializer(context),
        )
    assert (import_error.value.code, import_error.value.status) == ("planned_project_required", 409)
    assert len(harness.read(project.workspace_handle).outputs) == 1


def test_conversion_drops_an_unaccepted_automatic_plan_only_when_a_member_is_published(
    tmp_path: Path,
) -> None:
    from test_production_import import _authorities, _Materializer, _production, _request

    context, _admission, proposal = _authorities()
    sidebar, registry, created = _production(context)
    imported = registry.import_automatic_plan(
        _request(created, context, proposal),
        planning_context=context,
        proposal=proposal,
        materialize=_Materializer(context),
    )
    assert imported.status == 200
    planned = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "planned.read",
            "action": "read_projection",
            "payload": {"workspace_handle": created.workspace_handle},
        }
    ).projection
    assert isinstance(planned, ProductionWorkbenchProjection)
    handle = planned.workspace_handle
    planned_segments = tuple(row.segment_id for row in planned.segments)
    assert len(planned_segments) == len(proposal.segments)
    assert registry._entries[handle].automatic_plan is not None
    assert registry._entries[handle].members is None

    admitted = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "planned.admit",
            "action": "admit_generation_destination",
            "payload": {
                "workspace_handle": handle,
                "workspace_id": planned.workspace_id,
                "segment_id": None,
            },
        }
    )
    assert admitted.status == 200
    # Admission converts nothing: the imported plan and its revision are untouched until a Start
    # actually publishes a member.
    assert registry._entries[handle].automatic_plan is not None
    assert registry._entries[handle].members is None
    assert registry._entries[handle].workspace.revision == planned.workspace_revision

    report = _report(8.0, "Synthetic member appended after an imported plan.")
    source = sidebar.publish(
        report,
        build_native_h3_wiring(report),
        ExecutionCorrelation("prompt.context.after-plan", "node.product.shell"),
    )
    frames = sidebar.claim_production_seed(source.workspace_id).duration.resolved.frame_count
    tokens = iter(range(1, 100))
    coordinator = SequenceCoordinatorRegistry(
        production_registry=registry,
        output_root_factory=lambda: tmp_path,
        private_root_factory=lambda: tmp_path / "private",
        artifact_inspector=lambda _payload, **expected: ObservedVideoArtifact(
            "mp4", (expected["expected_frames"], 512, 512, 3)
        ),
        token_factory=lambda: f"{next(tokens):040d}",
    )
    prepared = _required_response(
        coordinator.dispatch(
            _action(
                "planned.member.prepare",
                "prepare_managed_run",
                {
                    "context_workspace_handle": source.workspace_id,
                    "correlation": {
                        "prompt_id": "prompt.after-plan",
                        "execution_node_id": "node.x",
                    },
                    "observation": {**_observation(), "expected_frames": frames},
                    "production_admission_request_id": "planned.admit",
                },
            )
        )
    )
    authority = prepared.production_member_authority
    assert authority is not None and authority.workspace_handle == handle
    entry = registry._entries[handle]
    # Plan section 4.6: the unaccepted plan is dropped as a structural change drops it; every
    # planned declaration stays, and only the one published Start becomes a member.
    assert entry.automatic_plan is None
    assert entry.members is not None and set(entry.members) == {authority.member_segment_id}
    segments = tuple(row.segment_id for row in entry.workspace.segments)
    assert segments == (*planned_segments, authority.member_segment_id)

    current = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "planned.read.after",
            "action": "read_projection",
            "payload": {"workspace_handle": handle},
        }
    ).projection
    assert isinstance(current, ProductionWorkbenchProjection)
    with pytest.raises(ProductionWorkbenchError) as reimport:
        registry.import_automatic_plan(
            replace(_request(current, context, proposal), request_id="planned.import.again"),
            planning_context=context,
            proposal=proposal,
            materialize=_Materializer(context),
        )
    assert (reimport.value.code, reimport.value.status) == ("planned_project_required", 409)
    assert registry._entries[handle].members is entry.members


def test_member_mode_mutations_retain_unchanged_members_and_refuse_live_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    project, _ = harness.start("mutation-a")
    project, _ = harness.start("mutation-b", project=project)
    kept = _outputs(project)
    added = harness.production_action(
        "mutation.manual-add",
        "add_segment_from_context",
        {
            "workspace_handle": project.workspace_handle,
            "expected_workspace_revision": project.workspace_revision,
            "expected_workspace_fingerprint": project.workspace_fingerprint,
            "context_workspace_handle": harness.context("mutation-a"),
            "relation": "independent",
            "predecessor_segment_id": None,
        },
    ).projection
    assert added is not None and _outputs(added) == kept
    relation = harness.production_action(
        "mutation.relation",
        "set_segment_relation",
        {
            "workspace_handle": added.workspace_handle,
            "expected_workspace_revision": added.workspace_revision,
            "expected_workspace_fingerprint": added.workspace_fingerprint,
            "segment_id": project.segments[1].segment_id,
            "relation": "cut",
            "predecessor_segment_id": None,
        },
    ).projection
    assert relation is not None
    assert _outputs(relation) == {
        project.segments[0].segment_id: kept[project.segments[0].segment_id]
    }
    assert relation.segments[1].job_state == "unavailable"

    assert (
        harness.admit("admit.mutation.live", relation, relation.segments[2].segment_id).status
        == 200
    )
    prepared = harness.prepare(
        "mutation-live", harness.context("mutation-live"), "admit.mutation.live"
    )
    live = harness.read(project.workspace_handle)
    for action, payload in (
        ("delete_segment", {"segment_id": live.segments[2].segment_id}),
        (
            "replace_segment_from_context",
            {
                "segment_id": live.segments[2].segment_id,
                "context_workspace_handle": harness.context("mutation-other", 5.0),
            },
        ),
    ):
        with pytest.raises(ProductionWorkbenchError) as refused:
            harness.production_action(
                f"mutation.live.{action}",
                action,
                {
                    "workspace_handle": live.workspace_handle,
                    "expected_workspace_revision": live.workspace_revision,
                    "expected_workspace_fingerprint": live.workspace_fingerprint,
                    **payload,
                },
            )
        assert (refused.value.code, refused.value.status) == ("member_live", 409)
    with pytest.raises(ProductionWorkbenchError) as release_live:
        harness.production_action(
            "mutation.release.live",
            "release_workspace",
            {
                "workspace_handle": live.workspace_handle,
                "expected_workspace_revision": live.workspace_revision,
                "expected_workspace_fingerprint": live.workspace_fingerprint,
            },
        )
    assert release_live.value.code == "workspace_sequence_live"
    reordered = harness.production_action(
        "mutation.reorder.live",
        "reorder_segments",
        {
            "workspace_handle": live.workspace_handle,
            "expected_workspace_revision": live.workspace_revision,
            "expected_workspace_fingerprint": live.workspace_fingerprint,
            "segment_ids": [row.segment_id for row in reversed(live.segments)],
        },
    ).projection
    assert reordered is not None
    closed = harness.close("mutation-live", harness.submit("mutation-live", prepared))
    assert closed.disposition == "succeeded", "a reorder never breaks a running member"
    final = harness.read(project.workspace_handle)
    assert set(_outputs(final)) == {project.segments[0].segment_id, live.segments[2].segment_id}


def test_member_response_is_closed_bounded_and_joined_to_one_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    assert harness.admit("admit.bound").status == 202
    prepared = harness.prepare("bound", harness.context("bound"), "admit.bound")
    wire = _member_wire(prepared)
    assert wire["schema"] == MANAGED_MEMBER_RESPONSE_SCHEMA
    assert "production" not in wire
    authority = prepared.production_member_authority
    assert authority is not None
    with pytest.raises(SequenceCoordinatorError) as foreign_segment:
        SequenceCoordinatorResponse(
            prepared.run_handle,
            prepared.disposition,
            prepared.sequence,
            prepared.production,
            None,
            None,
            MANAGED_MEMBER_RESPONSE_SCHEMA,
            production_member_authority=SequenceProductionMemberAuthorityV1(
                authority.workspace_handle, authority.workspace_id, "segment_foreign"
            ),
        )
    assert foreign_segment.value.code == "cross_authority_response"
    with pytest.raises(SequenceCoordinatorError) as wrong_schema:
        replace(prepared, schema="h3.context.generation_coordinator.response.v1")
    assert wrong_schema.value.code == "invalid_response"

    monkeypatch.setattr(coordinator_module, "MAX_COORDINATOR_RESPONSE_BYTES", 512)
    assert harness.admit("admit.oversized").status == 202
    with pytest.raises(SequenceCoordinatorError) as oversized:
        harness.prepare("oversized", harness.context("oversized"), "admit.oversized")
    assert (oversized.value.code, oversized.value.status) == ("member_response_capacity", 503)
    admission = harness.production._admissions["admit.oversized"]
    assert admission.state == "released", "a refusal before publication releases, never consumes"
    assert admission.published_handle is None
    assert len(harness.production._entries) == 1


def test_member_run_state_projects_live_and_terminal_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    assert harness.admit("admit.states").status == 202
    prepared = harness.prepare("states", harness.context("states"), "admit.states")
    authority = prepared.production_member_authority
    assert authority is not None
    planned = harness.read(authority.workspace_handle)
    assert (planned.run_state, planned.run_completed, planned.run_total) == ("running", 0, 1)
    assert planned.segments[0].job_state == GenerationJobState.PLANNED.value
    assert planned.outputs == ()
    closed = harness.close("states", harness.submit("states", prepared))
    finished = harness.read(authority.workspace_handle)
    assert (finished.run_state, finished.run_completed, finished.run_total) == ("succeeded", 1, 1)
    assert finished.segments[0].artifact_state == "complete"
    assert finished.segments[0].delivered_geometry is not None
    assert "import_production_outputs_to_authoring" in finished.allowed_actions
    assert "preview_output" in finished.allowed_actions
    assert closed.artifact_authority is not None
