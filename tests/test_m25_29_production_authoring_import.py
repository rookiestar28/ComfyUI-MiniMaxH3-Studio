"""M25-29 focused contract and atomic catalog/reference tests.

Fixtures are content-free.  Real encoded-source qualification is recorded separately by the
item's runtime row; these tests pin the pure transaction boundaries that must stay deterministic.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NoReturn, cast
from unittest.mock import patch

import pytest
from deployment_request_doubles import LOOPBACK_HOST, ListenerTransport
from deployment_request_doubles import LOOPBACK_ORIGIN as OWNED_ORIGIN

from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.authoring_generated_source import (
    CompositeAuthoringSourceBindingReceipt,
)
from comfyui_h3_context.adapters.authoring_render_leases import (
    RenderSourceLeaseError,
    acquire_render_job_sources,
)
from comfyui_h3_context.adapters.authoring_source_binding import (
    AuthoringSourceBindingError,
    AuthoringSourceBindingReceipt,
    RuntimeVideoCapability,
)
from comfyui_h3_context.adapters.av_reconstruction_media import (
    AuthoringVideoProbePayload,
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.comfyui_authoring_workspace import (
    AUTHORING_ACTION_SCHEMA,
    MAX_AUTHORING_IMPORT_LEDGER,
    AuthoringWorkbenchError,
    AuthoringWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    SidebarAuthoringSeed,
    SidebarAuthoringWorkspaceClaim,
    SidebarProductionSeed,
)
from comfyui_h3_context.adapters.production_authoring_import_service import (
    PRODUCTION_AUTHORING_IMPORT_ROUTE,
    ProductionAuthoringImportService,
    ensure_production_authoring_import_route_registered,
)
from comfyui_h3_context.adapters.segment_artifact_store import (
    ArtifactStoreError,
    PrivateSegmentArtifactStore,
)
from comfyui_h3_context.core import (
    AcceptedIntentAuthority,
    ExecutionCorrelation,
    FingerprintDomain,
    GenerationJobSpec,
    build_generation_sequence_plan,
    build_generation_sequence_projection,
    create_generation_sequence_state,
    create_workspace,
    derive_segment_manifests,
    plan_recompute,
    record_generation_projection,
    record_generation_running,
    record_generation_submission,
    record_generation_success,
    revise_workspace,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.composition_contract import (
    PublicAsset,
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.nle_authoring_contract import (
    NLE_AUTHORING_PROFILE_ID,
    NLE_AUTHORING_SCHEMA,
    NLE_OPERATION_PROFILE_ID,
    TIMELINE_TRANSACTION_SCHEMA_V2,
)
from comfyui_h3_context.core.production_authoring_import import (
    PRODUCTION_AUTHORING_IMPORT_ACTION,
    PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA,
    ProductionAuthoringImportEntry,
    ProductionAuthoringImportRequest,
    ProductionAuthoringImportRequestV2,
    ProductionAuthoringImportResponseV2,
    decode_production_authoring_import_json,
)
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.core.reference_set_authoring import (
    AdmittedSourceInput,
    TimedReferenceLimits,
    apply_video_source_batch,
    build_h3_base_capacity,
    create_reference_set,
)
from comfyui_h3_context.core.render_planner import (
    PrivateSourceFactsManifest,
    RenderPlannerError,
    _validate_source_manifest,
    private_source_manifest_fingerprint,
)
from comfyui_h3_context.core.segment_artifacts import begin_segment_artifact_receipt
from comfyui_h3_context.core.segment_workspace import SegmentDuration
from comfyui_h3_context.core.timeline_history import (
    TIMELINE_TRANSACTION_SCHEMA,
    TimelineHistoryState,
    apply_timeline_transaction,
    decode_timeline_transaction,
    refresh_timeline_asset_catalog,
)
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

FP = "sha256:" + "a" * 64
EXPECTED_512_LANDMARK_SNAPSHOT_FINGERPRINT = (
    "sha256:ab6b9b9e828724b8db792f89cb35b667b4e10ae851fcc29e48abbbd215bbfdd8"
)
FIXTURE = Path(__file__).parent / "fixtures" / "m25_10_composition_contract_v1.json"


def request_wire() -> dict[str, object]:
    return {
        "schema": PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA,
        "action": PRODUCTION_AUTHORING_IMPORT_ACTION,
        "request_id": "import.1",
        "production_workspace_handle": "pw_" + "p" * 32,
        "production_workspace_id": "workspace.1",
        "expected_production_workspace_revision": 7,
        "expected_production_workspace_fingerprint": FP,
        "authoring_workspace_handle": "authoring-" + "a" * 32,
        "expected_authoring_registry_fingerprint": FP,
        "expected_authoring_reference_revision": 3,
        "expected_authoring_timeline_revision": 4,
        "expected_authoring_timeline_content_fingerprint": FP,
        "expected_nle_workspace_revision": 3,
        "expected_nle_timeline_revision": 4,
        "expected_nle_timeline_fingerprint": FP,
        "expected_nle_public_fingerprint": FP,
        "entries": [
            {"segment_id": "segment.1", "output_handle": "out_" + "1" * 40},
            {"segment_id": "segment.2", "output_handle": "out_" + "2" * 40},
        ],
    }


def test_closed_import_request_round_trips_and_rejects_aliases() -> None:
    wire = request_wire()
    request = decode_production_authoring_import_json(
        json.dumps(wire, separators=(",", ":")).encode()
    )
    assert type(request) is ProductionAuthoringImportRequest
    assert request.to_wire() == wire
    assert request.digest == canonical_fingerprint(wire)

    mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
        lambda value: value.update({"path": "private.mp4"}),
        lambda value: value["entries"].append(value["entries"][0]),
        lambda value: value.update({"expected_nle_workspace_revision": True}),
        lambda value: value.update({"action": "preview_output"}),
    )
    for mutation in mutations:
        invalid = copy.deepcopy(wire)
        mutation(invalid)
        with pytest.raises(ValueError):
            decode_production_authoring_import_json(json.dumps(invalid).encode())


def test_video_batch_advances_reference_once_and_is_all_or_none() -> None:
    capacity = build_h3_base_capacity(
        authority="core.registry",
        fingerprint=FP,
        timed=TimedReferenceLimits(max_duration_milliseconds=150_000, max_frames=3_600),
    )
    state = create_reference_set(capacity)
    sources = tuple(
        AdmittedSourceInput(
            source_id=f"generated.{index}",
            kind=MediaKind.VIDEO,
            fingerprint=canonical_fingerprint({"generated": index}),
            duration_milliseconds=5_000,
        )
        for index in range(1, 5)
    )
    updated = apply_video_source_batch(state, sources[:2])
    assert updated.revision == state.revision + 1
    assert tuple(row.source_id for row in updated.videos) == tuple(
        row.source_id for row in sources[:2]
    )
    with pytest.raises(ContractValidationError):
        apply_video_source_batch(updated, sources[2:])
    assert len(updated.videos) == 2
    assert state.videos == ()


def _snapshot() -> PublicCompositionSnapshot:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return decode_public_snapshot(document["snapshot"])


def _composition_wire_with_video_landmarks(
    *, asset_count: int, frame_count: int
) -> dict[str, object]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot = cast(dict[str, object], document["snapshot"])
    assets = cast(list[dict[str, object]], snapshot["assets"])
    template = copy.deepcopy(next(asset for asset in assets if asset["kind"] == "video"))
    landmarks = [
        {
            "frame_index": index,
            "pts": index * 3_750,
            "dts": index * 3_750,
            "duration_ticks": 3_750,
        }
        for index in range(frame_count)
    ]
    retained_assets = copy.deepcopy(assets)
    for retained in retained_assets:
        if retained["kind"] == "video":
            retained["landmarks"] = []
    primary = next(asset for asset in retained_assets if asset["asset_id"] == template["asset_id"])
    primary["source_frame_count"] = frame_count
    primary["landmarks"] = copy.deepcopy(landmarks)
    timed_assets: list[dict[str, object]] = []
    for index in range(asset_count):
        if index:
            asset = copy.deepcopy(template)
            asset["asset_id"] = f"generated.boundary.{index}"
            asset["source_frame_count"] = frame_count
            asset["landmarks"] = copy.deepcopy(landmarks)
            timed_assets.append(asset)
    first_font = next(
        (index for index, asset in enumerate(retained_assets) if asset["kind"] == "font"),
        len(retained_assets),
    )
    snapshot["assets"] = retained_assets[:first_font] + timed_assets + retained_assets[first_font:]
    snapshot["public_fingerprint"] = public_snapshot_fingerprint(snapshot)
    return snapshot


@pytest.mark.parametrize("frame_count", (192, 512))
def test_imported_production_landmark_profile_admits_normal_and_ceiling_outputs(
    frame_count: int,
) -> None:
    snapshot = decode_public_snapshot(
        _composition_wire_with_video_landmarks(asset_count=1, frame_count=frame_count)
    )
    assert len(snapshot.assets[0].landmarks) == frame_count
    if frame_count == 512:
        assert snapshot.public_fingerprint == EXPECTED_512_LANDMARK_SNAPSHOT_FINGERPRINT


def test_imported_production_landmark_profile_preserves_closed_resource_boundaries() -> None:
    with pytest.raises(ContractValidationError, match="resource_limit"):
        decode_public_snapshot(
            _composition_wire_with_video_landmarks(asset_count=1, frame_count=513)
        )
    exact_aggregate = decode_public_snapshot(
        _composition_wire_with_video_landmarks(asset_count=4, frame_count=512)
    )
    assert sum(len(asset.landmarks) for asset in exact_aggregate.assets) == 2_048
    with pytest.raises(ContractValidationError, match="resource_limit"):
        decode_public_snapshot(
            _composition_wire_with_video_landmarks(asset_count=5, frame_count=512)
        )


def test_catalog_refresh_changes_only_workspace_identity_and_preserves_history() -> None:
    before = TimelineHistoryState.initialize(_snapshot())
    asset = PublicAsset(
        "generated.asset",
        "video",
        before.snapshot.assets[0].source_time_base,
        before.snapshot.assets[0].source_frame_count,
        before.snapshot.assets[0].source_sample_count,
        before.snapshot.assets[0].embedded_audio,
        before.snapshot.assets[0].timestamp_policy,
        before.snapshot.assets[0].landmarks,
    )
    after = refresh_timeline_asset_catalog(before, (asset,))
    assert after.snapshot.workspace_revision == before.snapshot.workspace_revision + 1
    assert after.snapshot.timeline_revision == before.snapshot.timeline_revision
    assert after.snapshot.timeline_fingerprint == before.snapshot.timeline_fingerprint
    assert after.snapshot.public_fingerprint != before.snapshot.public_fingerprint
    assert asset in after.snapshot.assets
    assert after.snapshot.assets.index(asset) < next(
        index for index, row in enumerate(after.snapshot.assets) if row.kind == "font"
    )
    assert after.selection == before.selection
    assert after.undo_entries == before.undo_entries
    assert after.redo_entries == before.redo_entries
    assert after.idempotency_records == before.idempotency_records
    assert after.next_sequence == before.next_sequence


def _timeline_transaction(
    state: TimelineHistoryState,
    *,
    request_id: str,
    command: dict[str, object],
) -> object:
    snapshot = state.snapshot
    return decode_timeline_transaction(
        {
            "schema": TIMELINE_TRANSACTION_SCHEMA,
            "request_id": request_id,
            "transaction_id": "tx." + request_id,
            "workspace_handle": snapshot.workspace_handle,
            "expected_workspace_revision": snapshot.workspace_revision,
            "expected_timeline_revision": snapshot.timeline_revision,
            "expected_timeline_fingerprint": snapshot.timeline_fingerprint,
            "commands": [command],
        }
    )


def test_catalog_refresh_survives_undo_and_redo_without_mutating_retained_entries() -> None:
    state = TimelineHistoryState.initialize(_snapshot())
    changed, accepted = apply_timeline_transaction(
        state,
        _timeline_transaction(
            state,
            request_id="catalog.select",
            command={"kind": "select_clips", "payload": {"clip_ids": ["clip-main"]}},
        ),
    )
    retained = copy.deepcopy(changed.undo_entries[0])
    template = changed.snapshot.assets[0]
    imported = PublicAsset(
        "generated.undo-safe",
        "video",
        template.source_time_base,
        template.source_frame_count,
        template.source_sample_count,
        template.embedded_audio,
        template.timestamp_policy,
        template.landmarks,
    )
    refreshed = refresh_timeline_asset_catalog(changed, (imported,))
    undone, undo_receipt = apply_timeline_transaction(
        refreshed,
        _timeline_transaction(
            refreshed,
            request_id="catalog.undo",
            command={"kind": "undo", "payload": {"history_cursor": accepted.history_cursor}},
        ),
    )
    redone, _ = apply_timeline_transaction(
        undone,
        _timeline_transaction(
            undone,
            request_id="catalog.redo",
            command={"kind": "redo", "payload": {"history_cursor": undo_receipt.history_cursor}},
        ),
    )

    assert imported in undone.snapshot.assets
    assert imported in redone.snapshot.assets
    assert changed.undo_entries[0] == retained
    assert imported not in retained.before_snapshot.assets
    assert imported not in retained.after_snapshot.assets


class _EmptyBinding(AuthoringSourceBindingReceipt):
    def __init__(self) -> None:
        from comfyui_h3_context.core.registry import ReferenceRegistry

        super().__init__(exact_registry=ReferenceRegistry.empty(), generation=1)

    def _capability_for(self, _source_id: str) -> RuntimeVideoCapability:
        return RuntimeVideoCapability.UNSUPPORTED

    def _claim_source(self, _source_id: str) -> NoReturn:
        raise AuthoringSourceBindingError("source_not_found")

    def _release_sources(self) -> None:
        return None


def _authoring_action(request_id: str, action: str, **payload: object) -> dict[str, object]:
    return {
        "schema": AUTHORING_ACTION_SCHEMA,
        "request_id": request_id,
        "action": action,
        "payload": payload,
    }


def _production_action(request_id: str, context_handle: str) -> dict[str, object]:
    return {
        "schema": PRODUCTION_ACTION_SCHEMA,
        "request_id": request_id,
        "action": "create_workspace_from_context",
        "payload": {"context_workspace_handle": context_handle},
    }


def _video_probe_wire(*, frame_count: int = 3, width: int = 320, height: int = 240) -> bytes:
    return json.dumps(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": width,
                    "height": height,
                    "pix_fmt": "yuv420p",
                    "sample_aspect_ratio": "1:1",
                    "color_range": "tv",
                    "color_space": "bt709",
                    "color_primaries": "bt709",
                    "color_transfer": "bt709",
                    "time_base": "1/90000",
                    "start_pts": 0,
                }
            ],
            "frames": [
                {
                    "media_type": "video",
                    "stream_index": 0,
                    "pts": pts,
                    "pkt_dts": pts,
                    "duration": 3000,
                }
                for pts in range(0, frame_count * 3000, 3000)
            ],
            "programs": [],
            "stream_groups": [],
        },
        separators=(",", ":"),
    ).encode()


def _qualified_test_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    frame_count: int = 3,
    width: int = 320,
    height: int = 240,
) -> QualifiedAVMediaAdapter:
    scratch = (tmp_path / "authoring-scratch").resolve()
    scratch.mkdir()
    adapter = object.__new__(QualifiedAVMediaAdapter)
    adapter._scratch_root = scratch

    def probe(
        _self: QualifiedAVMediaAdapter,
        *,
        source_path: Path,
        deadline: float,
        cancellation: object | None = None,
    ) -> AuthoringVideoProbePayload:
        assert cancellation is not None and not cancellation.is_cancelled()  # type: ignore[attr-defined]
        assert deadline > time.monotonic()
        body = source_path.read_bytes()
        return AuthoringVideoProbePayload(
            _video_probe_wire(frame_count=frame_count, width=width, height=height),
            "sha256:" + hashlib.sha256(body).hexdigest(),
            len(body),
        )

    monkeypatch.setattr(QualifiedAVMediaAdapter, "probe_authoring_video_source", probe)
    return adapter


def _registries_with_ready_output(
    tmp_path: Path,
    *,
    segment_count: int = 1,
    artifact_bodies: tuple[bytes, ...] | None = None,
    frame_count: int = 3,
    width: int = 320,
    height: int = 240,
    initial_source_binding: bool = True,
) -> tuple[
    ProductionWorkspaceRegistry,
    ProductionWorkbenchProjection,
    AuthoringWorkspaceRegistry,
    dict[str, Any],
    dict[str, Any],
    PrivateSegmentArtifactStore,
]:
    lineage = object()
    context_handle = "ws_" + "c" * 32
    source_fingerprint = canonical_fingerprint({"source": "content-free"})
    production_seed = SidebarProductionSeed(
        task_mode=TaskMode.T2VA,
        source_id="source.context",
        reference_ids=(),
        duration=SegmentDuration.from_frame_count(124),
        accepted_intent_fingerprint=canonical_fingerprint({"intent": 1}),
        profile_fingerprint=canonical_fingerprint({"profile": 1}),
        reference_registry_fingerprint=source_fingerprint,
        native_binding_fingerprint=canonical_fingerprint({"native": 1}),
        producer_settings_fingerprint=canonical_fingerprint({"settings": 1}),
        seed_fingerprint=canonical_fingerprint({"seed": 1}),
        lineage_token=lineage,
    )
    now_ms = [2_000]
    production = ProductionWorkspaceRegistry(
        seed_claim=lambda handle: (
            production_seed if handle == context_handle else (_ for _ in ()).throw(KeyError(handle))
        ),
        clock=lambda: 1_000.0,
        clock_ms=lambda: now_ms[0],
    )
    created = production.dispatch(_production_action("production.create", context_handle))
    projection = created.projection
    assert isinstance(projection, ProductionWorkbenchProjection)
    for index in range(2, segment_count + 1):
        projection = production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": f"production.add.{index}",
                "action": "add_segment_from_context",
                "payload": {
                    "workspace_handle": projection.workspace_handle,
                    "expected_workspace_revision": projection.workspace_revision,
                    "expected_workspace_fingerprint": projection.workspace_fingerprint,
                    "context_workspace_handle": context_handle,
                    "relation": "reset",
                    "predecessor_segment_id": None,
                },
            }
        ).projection
        assert isinstance(projection, ProductionWorkbenchProjection)
    if segment_count > 1:
        projection = production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "production.select.all",
                "action": "set_selection",
                "payload": {
                    "workspace_handle": projection.workspace_handle,
                    "expected_workspace_revision": projection.workspace_revision,
                    "expected_workspace_fingerprint": projection.workspace_fingerprint,
                    "segment_ids": [row.segment_id for row in projection.segments],
                },
            }
        ).projection
        assert isinstance(projection, ProductionWorkbenchProjection)
    workspace = production._entries[projection.workspace_handle].workspace
    manifests = derive_segment_manifests(workspace)
    previous_declarations = tuple(
        replace(
            segment,
            producer_settings_fingerprint=canonical_fingerprint(
                {"settings": "previous", "index": index}
            ),
        )
        for index, segment in enumerate(workspace.segments, start=1)
    )
    previous = create_workspace(
        workspace.workspace_id,
        previous_declarations,
        accepted_intent_authorities=tuple(
            AcceptedIntentAuthority(
                segment.segment_id,
                segment.accepted_intent_fingerprint,
            )
            for segment in previous_declarations
        ),
        selected_segment_ids=tuple(segment.segment_id for segment in previous_declarations),
    )
    jobs = tuple(
        GenerationJobSpec(
            segment_id=manifest.segment_id,
            job_id=f"job.import.{index}",
            graph_fingerprint=canonical_fingerprint({"graph": index}),
            compiled_prompt_fingerprint=canonical_fingerprint({"compiled": index}),
            model_fingerprint=canonical_fingerprint({"model": index}),
            runtime_fingerprint=canonical_fingerprint({"runtime": index}),
            expected_format="mp4",
            expected_shape=(frame_count, height, width, 3),
            timeout_ms=60_000,
            fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
        )
        for index, manifest in enumerate(manifests, start=1)
    )
    plan = build_generation_sequence_plan(
        workspace,
        manifests,
        plan_recompute(derive_segment_manifests(previous), manifests),
        jobs,
        sequence_id="sequence.import.1",
    )
    state = create_generation_sequence_state(plan)
    store = PrivateSegmentArtifactStore(tmp_path / "production-store", clock_ms=lambda: now_ms[0])
    receipts = []
    for index, (manifest, job) in enumerate(zip(manifests, jobs, strict=True), start=1):
        state = record_generation_projection(
            state,
            job.job_id,
            transaction_id=f"transaction.import.{index}",
            graph_fingerprint=job.graph_fingerprint,
            compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
            fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
        )
        state = record_generation_submission(
            state, job.job_id, queue_prompt_id=f"prompt.import.{index}"
        )
        state = record_generation_running(state, job.job_id, host_owner_id=f"host.import.{index}")
        transaction = state.runtime_for(job.job_id).transaction
        assert transaction is not None
        partial = begin_segment_artifact_receipt(
            manifest=manifest,
            transaction=transaction,
            artifact_id=f"artifact.import.{index}",
            model_fingerprint=job.model_fingerprint,
            runtime_fingerprint=job.runtime_fingerprint,
            execution_fingerprint=canonical_fingerprint({"execution": index}),
            predecessor_artifact_fingerprint=None,
            format_label=job.expected_format,
            shape=job.expected_shape,
            created_at_ms=1_900,
            expires_at_ms=600_000,
        )
        body = (
            f"bounded-generated-video-{index}".encode()
            if artifact_bodies is None
            else artifact_bodies[index - 1]
        )
        receipt = store.commit(store.begin(partial), body)
        receipts.append(receipt)
        state = record_generation_success(
            state,
            job.job_id,
            result_fingerprint=canonical_fingerprint({"result": index}),
            receipt=receipt,
        )
    sequence = build_generation_sequence_projection(
        state, ExecutionCorrelation("prompt.sequence", "node.sequence")
    )
    projection = production.replace_generation_authority(
        projection.workspace_handle,
        expected_workspace_revision=projection.workspace_revision,
        expected_workspace_fingerprint=projection.workspace_fingerprint,
        expected_sequence_state_fingerprint=None,
        generation_sequence=sequence,
        artifact_receipts=tuple(receipts),
        artifact_store=store,
    )
    assert len(projection.outputs) == segment_count

    binding = _EmptyBinding() if initial_source_binding else None
    authoring_seed = SidebarAuthoringSeed(
        source_id=production_seed.source_id,
        task_mode=production_seed.task_mode,
        registry_fingerprint=production_seed.reference_registry_fingerprint,
        sources=(),
        lineage_token=lineage,
    )
    authoring = AuthoringWorkspaceRegistry(
        workspace_claim=lambda handle: (
            SidebarAuthoringWorkspaceClaim(authoring_seed, binding)
            if handle == context_handle
            else (_ for _ in ()).throw(KeyError(handle))
        ),
        clock=lambda: 1_000.0,
    )
    authoring_created = authoring.dispatch(
        _authoring_action(
            "authoring.create",
            "create_authoring_workspace",
            context_workspace_handle=context_handle,
        )
    )
    assert authoring_created.body is not None
    authoring_projection = cast(dict[str, Any], authoring_created.body)
    initialized = authoring.dispatch(
        _authoring_action(
            "authoring.initialize",
            "initialize_timeline_history",
            workspace_handle=authoring_projection["workspace_handle"],
            expected_reference_revision=authoring_projection["reference"]["revision"],
            expected_timeline_revision=authoring_projection["timeline"]["revision"],
            authoring_schema=NLE_AUTHORING_SCHEMA,
            profile_id=NLE_AUTHORING_PROFILE_ID,
            operation_profile_id=NLE_OPERATION_PROFILE_ID,
        )
    )
    assert initialized.body is not None
    return (
        production,
        projection,
        authoring,
        authoring_projection,
        cast(dict[str, Any], initialized.body),
        store,
    )


def _import_request(
    production_projection: ProductionWorkbenchProjection,
    authoring_projection: dict[str, Any],
    history_projection: dict[str, Any],
    *,
    request_id: str = "import.integration.1",
    output_indexes: tuple[int, ...] | None = None,
) -> ProductionAuthoringImportRequestV2:
    authoring_state = history_projection["authoring"]
    reference = authoring_projection["reference"]
    timeline = authoring_projection["timeline"]
    indexes = (
        tuple(range(len(production_projection.outputs)))
        if output_indexes is None
        else output_indexes
    )
    outputs = tuple(production_projection.outputs[index] for index in indexes)
    assert all(output.segment_id is not None for output in outputs)
    return ProductionAuthoringImportRequestV2(
        request_id=request_id,
        production_workspace_handle=production_projection.workspace_handle,
        production_workspace_id=production_projection.workspace_id,
        expected_production_workspace_revision=production_projection.workspace_revision,
        expected_production_workspace_fingerprint=production_projection.workspace_fingerprint,
        authoring_workspace_handle=authoring_projection["workspace_handle"],
        expected_authoring_registry_fingerprint=authoring_projection["registry_fingerprint"],
        expected_authoring_reference_revision=reference["revision"],
        expected_authoring_timeline_revision=timeline["revision"],
        expected_authoring_timeline_content_fingerprint=timeline["content_fingerprint"],
        expected_nle_workspace_revision=authoring_state["workspace_revision"],
        expected_nle_timeline_revision=authoring_state["timeline_revision"],
        expected_nle_timeline_fingerprint=authoring_state["timeline_fingerprint"],
        expected_nle_authoring_fingerprint=authoring_state["authoring_fingerprint"],
        authoring_schema=authoring_state["schema"],
        profile_id=authoring_state["profile_id"],
        entries=tuple(
            ProductionAuthoringImportEntry(
                cast(str, output.segment_id),
                output.output_handle,
            )
            for output in outputs
        ),
    )


def _insert_asset_transaction(
    *,
    authoring_state: dict[str, Any],
    asset_id: str,
    track_id: str,
    request_id: str,
    duration_frames: int = 2,
) -> dict[str, object]:
    return {
        "schema": TIMELINE_TRANSACTION_SCHEMA_V2,
        "authoring_schema": NLE_AUTHORING_SCHEMA,
        "profile_id": NLE_AUTHORING_PROFILE_ID,
        "operation_profile_id": NLE_OPERATION_PROFILE_ID,
        "request_id": request_id,
        "transaction_id": "tx." + request_id,
        "workspace_handle": authoring_state["workspace_handle"],
        "expected_workspace_revision": authoring_state["workspace_revision"],
        "expected_timeline_revision": authoring_state["timeline_revision"],
        "expected_timeline_fingerprint": authoring_state["timeline_fingerprint"],
        "expected_authoring_fingerprint": authoring_state["authoring_fingerprint"],
        "commands": [
            {
                "kind": "insert_asset_clip",
                "payload": {
                    "clip": {
                        "clip_id": "clip." + request_id,
                        "asset_id": asset_id,
                        "track_id": track_id,
                        "start_frame": 0,
                        "duration_frames": duration_frames,
                        "source_start_frame": 0,
                        "enabled": True,
                        "transform": {
                            "anchor_x_bp": 5000,
                            "anchor_y_bp": 5000,
                            "position_x_bp": 0,
                            "position_y_bp": 0,
                            "scale_x_bp": 10000,
                            "scale_y_bp": 10000,
                            "rotation_mdeg": 0,
                        },
                        "crop": {
                            "left_bp": 0,
                            "top_bp": 0,
                            "right_bp": 0,
                            "bottom_bp": 0,
                        },
                        "opacity_bp": 10000,
                        "blend": "normal",
                        "text": None,
                        "transition": {"kind": "none", "duration_frames": 0},
                        "effect": {
                            "kind": "none",
                            "brightness_permille": 0,
                            "contrast_permille": 1000,
                            "saturation_permille": 1000,
                        },
                    }
                },
            }
        ],
    }


def _place_imported_asset_for_render(
    authoring: AuthoringWorkspaceRegistry,
    *,
    workspace_handle: str,
    asset_id: str,
    request_id: str,
) -> dict[str, Any]:
    history = authoring.dispatch(
        _authoring_action(
            request_id + ".read-empty-history",
            "read_timeline_history",
            workspace_handle=workspace_handle,
        )
    )
    assert history.body is not None
    assert history.body["render_snapshot"] is None
    authoring_state = cast(dict[str, Any], history.body["authoring"])
    assert authoring_state["content_end_exclusive"] == 0
    assert authoring_state["clips"] == []
    track = next(track for track in authoring_state["tracks"] if track["kind"] == "primary_video")
    inserted = authoring.dispatch(
        {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": request_id,
            "action": "apply_timeline_transaction",
            "payload": _insert_asset_transaction(
                authoring_state=authoring_state,
                asset_id=asset_id,
                track_id=track["track_id"],
                request_id=request_id,
            ),
        }
    )
    assert inserted.status == 200 and inserted.body is not None
    placed_state = cast(dict[str, Any], inserted.body["authoring"])
    assert placed_state["content_end_exclusive"] == 2
    assert inserted.body["render_snapshot"] is not None
    return placed_state


def test_live_production_retains_source_free_authoring_seed_after_context_expires(
    tmp_path: Path,
) -> None:
    production, projection, _authoring, _before, _history, _store = _registries_with_ready_output(
        tmp_path
    )

    def expired_context(_handle: str) -> NoReturn:
        raise KeyError("original Context expired")

    production._seed_claim = expired_context
    seed = production.claim_authoring_seed_for_project(
        projection.workspace_handle, projection.workspace_id
    )
    assert seed.source_id == "source.context"
    assert seed.task_mode is TaskMode.T2VA
    assert seed.sources == ()
    assert seed.lineage_token is production._entries[projection.workspace_handle].lineage_token
    with pytest.raises(ProductionWorkbenchError) as foreign:
        production.claim_authoring_seed_for_project(
            projection.workspace_handle, "workspace.foreign"
        )
    assert (foreign.value.status, foreign.value.code) == (409, "destination_mismatch")


def test_cold_production_ensure_reuses_one_editor_after_context_expires(tmp_path: Path) -> None:
    production, projection, _authoring, _before, _history, _store = _registries_with_ready_output(
        tmp_path
    )

    def expired_context(_handle: str) -> NoReturn:
        raise KeyError("original Context expired")

    production._seed_claim = expired_context
    authoring = AuthoringWorkspaceRegistry(seed_claim=expired_context)
    first = authoring.ensure_from_production(
        production,
        workspace_handle=projection.workspace_handle,
        workspace_id=projection.workspace_id,
        preferred_authoring_handle=None,
    )
    second = authoring.ensure_from_production(
        production,
        workspace_handle=projection.workspace_handle,
        workspace_id=projection.workspace_id,
        preferred_authoring_handle=None,
    )
    assert (first.status, second.status) == (201, 200)
    assert first.body == second.body
    assert len(authoring._entries) == 1

    wire: dict[str, object] = {
        "schema": AUTHORING_ACTION_SCHEMA,
        "request_id": "ensure.first",
        "action": "ensure_authoring_from_production",
        "payload": {
            "production_workspace_handle": projection.workspace_handle,
            "production_workspace_id": projection.workspace_id,
            "preferred_authoring_handle": None,
        },
    }
    routed = authoring.dispatch(wire, production_registry=production)
    assert routed.status == 200
    assert routed.body == first.body
    assert authoring.dispatch(wire, production_registry=production).body == first.body
    wire_payload = cast(dict[str, object], wire["payload"])
    with pytest.raises(AuthoringWorkbenchError) as altered_replay:
        authoring.dispatch(
            {
                **wire,
                "payload": {**wire_payload, "preferred_authoring_handle": "authoring.other"},
            },
            production_registry=production,
        )
    assert (altered_replay.value.status, altered_replay.value.code) == (
        422,
        "request_replay_mismatch",
    )
    with pytest.raises(AuthoringWorkbenchError) as foreign_project:
        authoring.dispatch(
            {
                **wire,
                "request_id": "ensure.foreign",
                "payload": {**wire_payload, "production_workspace_id": "workspace.foreign"},
            },
            production_registry=production,
        )
    assert (foreign_project.value.status, foreign_project.value.code) == (
        409,
        "destination_mismatch",
    )


def test_visible_owner_reads_keep_exact_project_and_editor_across_inactivity_boundary(
    tmp_path: Path,
) -> None:
    production, project, authoring, editor, _history, _store = _registries_with_ready_output(
        tmp_path
    )
    editor_handle = editor["workspace_handle"]
    initial = max(
        production._entries[project.workspace_handle].touched_at,
        authoring._entries[editor_handle].touched_at,
    )
    now = [initial]
    production._clock = lambda: now[0]
    authoring._clock = lambda: now[0]

    def read_project(request_id: str) -> int:
        return production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": request_id,
                "action": "read_projection",
                "payload": {"workspace_handle": project.workspace_handle},
            }
        ).status

    now[0] = initial + 840
    assert read_project("renew.project") == 200
    assert (
        authoring.dispatch(
            _authoring_action("renew.editor", "read_projection", workspace_handle=editor_handle)
        ).status
        == 200
    )
    now[0] = initial + 901
    assert read_project("read.project.after.boundary") == 200
    assert (
        authoring.dispatch(
            _authoring_action(
                "read.editor.after.boundary", "read_projection", workspace_handle=editor_handle
            )
        ).status
        == 200
    )


def test_cold_production_ensure_initializes_and_imports_original_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, _old_authoring, _before, _history, _store = (
        _registries_with_ready_output(tmp_path)
    )

    def expired_context(_handle: str) -> NoReturn:
        raise KeyError("original Context expired")

    production._seed_claim = expired_context
    authoring = AuthoringWorkspaceRegistry(seed_claim=expired_context)
    created = authoring.ensure_from_production(
        production,
        workspace_handle=projection.workspace_handle,
        workspace_id=projection.workspace_id,
        preferred_authoring_handle=None,
    )
    assert created.body is not None
    target = created.body
    target_reference = cast(dict[str, object], target["reference"])
    target_timeline = cast(dict[str, object], target["timeline"])
    initialized = authoring.dispatch(
        _authoring_action(
            "cold.initialize",
            "initialize_timeline_history",
            workspace_handle=target["workspace_handle"],
            expected_reference_revision=target_reference["revision"],
            expected_timeline_revision=target_timeline["revision"],
            authoring_schema=NLE_AUTHORING_SCHEMA,
            profile_id=NLE_AUTHORING_PROFILE_ID,
            operation_profile_id=NLE_OPERATION_PROFILE_ID,
        )
    )
    assert initialized.body is not None
    request = _import_request(projection, target, initialized.body, request_id="import.cold.1")
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    accepted = ProductionAuthoringImportService(
        production_registry=production,
        authoring_registry=authoring,
        media_runtime=lambda: adapter,
    ).dispatch(request, deadline=time.monotonic() + 5.0)
    assert len(accepted.receipt.rows) == 1
    assert accepted.receipt.rows[0].segment_id == projection.outputs[0].segment_id
    assert accepted.authoring_projection["workspace_handle"] == target["workspace_handle"]
    retained = authoring.ensure_from_production(
        production,
        workspace_handle=projection.workspace_handle,
        workspace_id=projection.workspace_id,
        preferred_authoring_handle=cast(str, target["workspace_handle"]),
    )
    assert retained.status == 200
    assert retained.body == accepted.authoring_projection
    assert authoring._entries[cast(str, target["workspace_handle"])].production_owner == (
        projection.workspace_handle,
        projection.workspace_id,
    )


def test_ensure_preserves_existing_initialized_target_and_refuses_lost_association(
    tmp_path: Path,
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    existing_handle = before["workspace_handle"]
    attached = authoring.ensure_from_production(
        production,
        workspace_handle=projection.workspace_handle,
        workspace_id=projection.workspace_id,
        preferred_authoring_handle=existing_handle,
    )
    assert attached.status == 200
    assert attached.body is not None
    assert attached.body["workspace_handle"] == existing_handle
    assert (
        authoring.dispatch(
            _authoring_action(
                "history.retained",
                "read_timeline_history",
                workspace_handle=existing_handle,
            )
        ).body
        == history
    )
    authoring.dispatch(
        _authoring_action(
            "editor.release",
            "release_workspace",
            workspace_handle=existing_handle,
        )
    )
    with pytest.raises(AuthoringWorkbenchError) as lost:
        authoring.ensure_from_production(
            production,
            workspace_handle=projection.workspace_handle,
            workspace_id=projection.workspace_id,
            preferred_authoring_handle=None,
        )
    assert (lost.value.status, lost.value.code) == (410, "associated_editor_gone")
    assert not authoring._entries


def test_project_bound_editor_refuses_another_project_with_same_context_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    second = production.dispatch(
        _production_action("production.second", "ws_" + "c" * 32)
    ).projection
    assert isinstance(second, ProductionWorkbenchProjection)
    authoring.ensure_from_production(
        production,
        workspace_handle=projection.workspace_handle,
        workspace_id=projection.workspace_id,
        preferred_authoring_handle=before["workspace_handle"],
    )
    request = _import_request(projection, before, history)
    foreign = replace(
        request,
        request_id="import.same_lineage.foreign",
        production_workspace_handle=second.workspace_handle,
        production_workspace_id=second.workspace_id,
        expected_production_workspace_revision=second.workspace_revision,
        expected_production_workspace_fingerprint=second.workspace_fingerprint,
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    with pytest.raises(AuthoringWorkbenchError) as rejected:
        authoring.import_production_outputs(
            foreign,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )
    assert (rejected.value.status, rejected.value.code) == (409, "production_owner_mismatch")
    assert not authoring._entries[before["workspace_handle"]].imported_outputs


def test_service_refuses_missing_runtime_without_touching_either_workspace(
    tmp_path: Path,
) -> None:
    production, production_projection, authoring, before, history, _store = (
        _registries_with_ready_output(tmp_path)
    )
    request = _import_request(production_projection, before, history)
    service = ProductionAuthoringImportService(
        production_registry=production,
        authoring_registry=authoring,
        media_runtime=lambda: None,
    )

    with pytest.raises(AuthoringWorkbenchError) as unavailable:
        service.dispatch(request, deadline=time.monotonic() + 5.0)

    assert (unavailable.value.status, unavailable.value.code) == (503, "source_unavailable")
    assert authoring._entries[request.authoring_workspace_handle].reference.revision == (
        request.expected_authoring_reference_revision
    )
    current = production._entries[request.production_workspace_handle].workspace
    assert (current.revision, current.fingerprint) == (
        request.expected_production_workspace_revision,
        request.expected_production_workspace_fingerprint,
    )


def test_exact_ready_output_import_is_atomic_replayable_and_releasable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, production_projection, authoring, before, history, _store = (
        _registries_with_ready_output(tmp_path)
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(production_projection, before, history)
    before_state = copy.deepcopy(history["authoring"])

    response = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 5.0,
    )
    assert isinstance(response, ProductionAuthoringImportResponseV2)

    assert response.receipt.disposition == "created"
    assert response.receipt.next_reference_revision == (
        response.receipt.prior_reference_revision + 1
    )
    assert response.receipt.next_timeline_revision == response.receipt.prior_timeline_revision
    assert response.receipt.next_nle_workspace_revision == (
        response.receipt.prior_nle_workspace_revision + 1
    )
    assert response.receipt.next_nle_timeline_revision == (
        response.receipt.prior_nle_timeline_revision
    )
    row = response.receipt.rows[0]
    assert row.asset_id.startswith("generated.") and row.disposition == "created"
    public = json.dumps(response.to_wire(), sort_keys=True).lower()
    assert all(token not in public for token in ('"path"', '"url"', "filename"))

    imported_history = authoring.dispatch(
        _authoring_action(
            "history.after.import",
            "read_timeline_history",
            workspace_handle=request.authoring_workspace_handle,
        )
    )
    assert imported_history.body is not None
    imported_state = cast(dict[str, Any], imported_history.body["authoring"])
    for key in (
        "timeline_revision",
        "timeline_fingerprint",
        "tracks",
        "clips",
        "content_end_exclusive",
    ):
        assert imported_state[key] == before_state[key]
    assert imported_history.body["selection"] == history["selection"]
    assert imported_history.body["render_snapshot"] is None
    assert imported_state["content_end_exclusive"] == 0
    assert row.asset_id in {asset["asset_id"] for asset in imported_state["assets"]}
    assert response.history_projection == imported_history.body

    primary_track = next(
        track for track in imported_state["tracks"] if track["kind"] == "primary_video"
    )
    inserted = authoring.dispatch(
        {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": "history.insert.first",
            "action": "apply_timeline_transaction",
            "payload": _insert_asset_transaction(
                authoring_state=imported_state,
                asset_id=row.asset_id,
                track_id=primary_track["track_id"],
                request_id="history.insert.first",
            ),
        }
    )
    assert inserted.status == 200 and inserted.body is not None
    inserted_state = cast(dict[str, Any], inserted.body["authoring"])
    assert inserted_state["content_end_exclusive"] == 2
    inserted_render = cast(dict[str, Any], inserted.body["render_snapshot"])
    inserted_output = cast(dict[str, Any], inserted_render["output"])
    assert inserted_output["duration_frames"] == 2
    after_history = authoring.dispatch(
        _authoring_action(
            "history.after.insert",
            "read_timeline_history",
            workspace_handle=request.authoring_workspace_handle,
        )
    )
    assert after_history.body is not None and after_history.body["render_snapshot"] is not None
    after_snapshot = cast(dict[str, Any], after_history.body["render_snapshot"])

    replay = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 5.0,
    )
    assert replay is response
    reimport_request = _import_request(
        production_projection,
        response.authoring_projection,
        after_history.body,
        request_id="import.integration.2",
    )
    reimport = authoring.import_production_outputs(
        reimport_request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 5.0,
    )
    assert reimport.receipt.disposition == "already_imported"
    assert reimport.receipt.rows[0].asset_id == row.asset_id
    assert reimport.receipt.next_reference_revision == reimport.receipt.prior_reference_revision
    assert reimport.receipt.next_nle_workspace_revision == (
        reimport.receipt.prior_nle_workspace_revision
    )

    with pytest.raises(AuthoringWorkbenchError) as mismatch:
        authoring.import_production_outputs(
            replace(
                request,
                expected_authoring_reference_revision=(
                    request.expected_authoring_reference_revision + 1
                ),
            ),
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )
    assert (mismatch.value.status, mismatch.value.code) == (
        409,
        "request_replay_mismatch",
    )

    production_after = production.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.read.after.import",
            "action": "read_projection",
            "payload": {"workspace_handle": production_projection.workspace_handle},
        }
    ).projection
    assert isinstance(production_after, ProductionWorkbenchProjection)
    assert (
        production_after.workspace_revision,
        production_after.workspace_fingerprint,
        production_after.selected_segment_ids,
    ) == (
        production_projection.workspace_revision,
        production_projection.workspace_fingerprint,
        production_projection.selected_segment_ids,
    )

    source = next(
        iter(authoring._entries[request.authoring_workspace_handle].imported_outputs.values())
    ).source
    assert source.current() and not source.lease.released
    bound = authoring.prepare_render_plan(request.authoring_workspace_handle)
    accepted_snapshot = decode_public_snapshot(after_snapshot)
    generated_manifest = PrivateSourceFactsManifest(
        schema_version="h3.context.private_source_facts_manifest.v1",
        workspace_handle=accepted_snapshot.workspace_handle,
        workspace_revision=accepted_snapshot.workspace_revision,
        timeline_revision=accepted_snapshot.timeline_revision,
        public_fingerprint=accepted_snapshot.public_fingerprint,
        generation=bound.plan.source_currentness_claim.generation,
        facts=bound.plan.source_bindings,
        manifest_fingerprint=bound.plan.source_manifest_fingerprint,
    )
    assert _validate_source_manifest(accepted_snapshot, generated_manifest)[
        row.asset_id
    ].origin == ("generated")

    image_snapshot = replace(
        accepted_snapshot,
        assets=tuple(
            PublicAsset(
                asset.asset_id,
                "image",
                None,
                None,
                None,
                "absent",
                "not_applicable",
                (),
            )
            if asset.asset_id == row.asset_id
            else asset
            for asset in accepted_snapshot.assets
        ),
    )
    with pytest.raises(RenderPlannerError) as generated_image:
        _validate_source_manifest(image_snapshot, generated_manifest)
    assert generated_image.value.code == "source_origin_mismatch"

    unknown_origin = replace(
        generated_manifest,
        facts=(replace(generated_manifest.facts[0], origin="unknown_origin"),),
    )
    unknown_origin = replace(
        unknown_origin,
        manifest_fingerprint=private_source_manifest_fingerprint(unknown_origin),
    )
    with pytest.raises(RenderPlannerError) as unknown:
        _validate_source_manifest(accepted_snapshot, unknown_origin)
    assert unknown.value.code == "invalid_contract"

    job = acquire_render_job_sources(bound, deadline=time.monotonic() + 5.0)
    authoring.dispatch(
        _authoring_action(
            "authoring.release",
            "release_workspace",
            workspace_handle=request.authoring_workspace_handle,
        )
    )
    assert not source.current() and not source.lease.released
    assert job.read_source(row.asset_id) == b"bounded-generated-video-1"

    production.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.release.after.import",
            "action": "release_workspace",
            "payload": {
                "workspace_handle": production_projection.workspace_handle,
                "expected_workspace_revision": production_projection.workspace_revision,
                "expected_workspace_fingerprint": production_projection.workspace_fingerprint,
            },
        }
    )
    with pytest.raises(RenderSourceLeaseError) as revoked:
        job.read_source(row.asset_id)
    assert revoked.value.code == "source_replaced"
    job.release()
    assert source.lease.released


def test_import_replay_ledger_is_bounded_and_eviction_preserves_idempotent_reimport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    created_request = _import_request(projection, before, history)
    created = authoring.import_production_outputs(
        created_request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 5.0,
    )
    current_history = authoring.dispatch(
        _authoring_action(
            "history.for.bounded.ledger",
            "read_timeline_history",
            workspace_handle=created_request.authoring_workspace_handle,
        )
    )
    assert current_history.body is not None

    requests = tuple(
        _import_request(
            projection,
            created.authoring_projection,
            current_history.body,
            request_id=f"import.bounded.ledger.{index}",
        )
        for index in range(MAX_AUTHORING_IMPORT_LEDGER + 1)
    )
    for request in requests:
        response = authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )
        assert response.receipt.disposition == "already_imported"

    assert len(authoring._import_ledger) == MAX_AUTHORING_IMPORT_LEDGER
    assert created_request.request_id not in authoring._import_ledger
    assert requests[0].request_id not in authoring._import_ledger
    replay_after_eviction = authoring.import_production_outputs(
        requests[0],
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 5.0,
    )
    assert replay_after_eviction.receipt.disposition == "already_imported"
    assert len(authoring._import_ledger) == MAX_AUTHORING_IMPORT_LEDGER
    assert replay_after_eviction.receipt.rows[0].asset_id == created.receipt.rows[0].asset_id

    authoring.dispatch(
        _authoring_action(
            "authoring.release.bounded.ledger",
            "release_workspace",
            workspace_handle=created_request.authoring_workspace_handle,
        )
    )


def test_import_rejects_stale_authoring_and_equal_looking_foreign_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)

    with pytest.raises(AuthoringWorkbenchError) as stale:
        authoring.import_production_outputs(
            replace(request, expected_authoring_reference_revision=99),
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )
    assert (stale.value.status, stale.value.code) == (409, "authoring_stale")

    authoring._entries[request.authoring_workspace_handle].lineage_token = object()
    with pytest.raises(AuthoringWorkbenchError) as foreign:
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )
    assert (foreign.value.status, foreign.value.code) == (409, "lineage_mismatch")

    with pytest.raises(AuthoringWorkbenchError) as timeout:
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() - 1.0,
        )
    assert (timeout.value.status, timeout.value.code) == (408, "timed_out")


def test_import_rejects_stale_gone_mismatched_and_noncanonical_production_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path, segment_count=2
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)

    failures = (
        (
            replace(
                request,
                expected_production_workspace_revision=(
                    request.expected_production_workspace_revision + 1
                ),
            ),
            (409, "production_stale"),
        ),
        (replace(request, entries=tuple(reversed(request.entries))), (404, "output_unknown")),
        (
            replace(
                request,
                entries=(
                    replace(request.entries[0], output_handle=request.entries[1].output_handle),
                    replace(request.entries[1], output_handle=request.entries[0].output_handle),
                ),
            ),
            (404, "output_unknown"),
        ),
    )
    for candidate, expected in failures:
        with pytest.raises(AuthoringWorkbenchError) as rejected:
            authoring.import_production_outputs(
                candidate,
                production_registry=production,
                media_adapter=adapter,
                deadline=time.monotonic() + 5.0,
            )
        assert (rejected.value.status, rejected.value.code) == expected

    entry = authoring._entries[request.authoring_workspace_handle]
    assert entry.imported_outputs == {}
    assert entry.reference.revision == request.expected_authoring_reference_revision
    production.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.release.before.import",
            "action": "release_workspace",
            "payload": {
                "workspace_handle": projection.workspace_handle,
                "expected_workspace_revision": projection.workspace_revision,
                "expected_workspace_fingerprint": projection.workspace_fingerprint,
            },
        }
    )
    with pytest.raises(AuthoringWorkbenchError) as gone:
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )
    assert (gone.value.status, gone.value.code) == (410, "production_gone")


def test_import_capacity_failure_releases_every_staged_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path, segment_count=3
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)
    entry = authoring._entries[request.authoring_workspace_handle]
    entry.reference = replace(
        entry.reference,
        capacity=replace(entry.reference.capacity, aggregate_max=2, video_max=2),
    )

    with pytest.raises(AuthoringWorkbenchError) as capacity:
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )

    assert (capacity.value.status, capacity.value.code) == (422, "capacity")
    assert entry.imported_outputs == {}
    assert entry.reference.revision == request.expected_authoring_reference_revision
    assert entry.importing is None
    assert list(adapter._scratch_root.iterdir()) == []


def test_import_cancellation_during_artifact_stream_is_typed_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)
    cancelled = [False]

    def interrupt_stream(
        _self: PrivateSegmentArtifactStore,
        _expected: object,
        _destination: Path,
        *,
        should_cancel: object = None,
    ) -> tuple[int, str]:
        cancelled[0] = True
        assert callable(should_cancel) and should_cancel()
        raise ArtifactStoreError("stream_cancelled")

    monkeypatch.setattr(PrivateSegmentArtifactStore, "stream_artifact_into", interrupt_stream)
    with pytest.raises(AuthoringWorkbenchError) as interrupted:
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
            cancelled=lambda: cancelled[0],
        )

    assert (interrupted.value.status, interrupted.value.code) == (408, "cancelled")
    entry = authoring._entries[request.authoring_workspace_handle]
    assert entry.importing is None
    assert entry.reference.revision == request.expected_authoring_reference_revision
    assert entry.imported_outputs == {}
    assert list(adapter._scratch_root.iterdir()) == []


def test_import_cancellation_after_probe_but_before_commit_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)
    cancelled = [False]
    original_probe = QualifiedAVMediaAdapter.probe_authoring_video_source

    def cancel_after_probe(
        self: QualifiedAVMediaAdapter,
        *,
        source_path: Path,
        deadline: float,
        cancellation: object | None = None,
    ) -> AuthoringVideoProbePayload:
        result = original_probe(
            self,
            source_path=source_path,
            deadline=deadline,
            cancellation=cancellation,  # type: ignore[arg-type]
        )
        cancelled[0] = True
        return result

    monkeypatch.setattr(QualifiedAVMediaAdapter, "probe_authoring_video_source", cancel_after_probe)
    with pytest.raises(AuthoringWorkbenchError) as interrupted:
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
            cancelled=lambda: cancelled[0],
        )

    assert (interrupted.value.status, interrupted.value.code) == (408, "cancelled")
    entry = authoring._entries[request.authoring_workspace_handle]
    assert entry.importing is None and entry.imported_outputs == {}
    assert entry.reference.revision == request.expected_authoring_reference_revision
    assert list(adapter._scratch_root.iterdir()) == []


def test_import_late_production_currentness_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)
    original_probe = QualifiedAVMediaAdapter.probe_authoring_video_source

    def revise_after_probe(
        self: QualifiedAVMediaAdapter,
        *,
        source_path: Path,
        deadline: float,
        cancellation: object | None = None,
    ) -> AuthoringVideoProbePayload:
        result = original_probe(
            self,
            source_path=source_path,
            deadline=deadline,
            cancellation=cancellation,  # type: ignore[arg-type]
        )
        production_entry = production._entries[request.production_workspace_handle]
        workspace = production_entry.workspace
        production_entry.workspace = revise_workspace(
            workspace,
            expected_workspace_fingerprint=workspace.fingerprint,
            accepted_intent_authorities=tuple(
                AcceptedIntentAuthority(
                    segment.segment_id,
                    segment.accepted_intent_fingerprint,
                )
                for segment in workspace.segments
            ),
        )
        return result

    monkeypatch.setattr(QualifiedAVMediaAdapter, "probe_authoring_video_source", revise_after_probe)
    with pytest.raises(AuthoringWorkbenchError) as stale:
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )

    assert (stale.value.status, stale.value.code) == (409, "source_stale")
    entry = authoring._entries[request.authoring_workspace_handle]
    assert entry.importing is None and entry.imported_outputs == {}
    assert entry.reference.revision == request.expected_authoring_reference_revision
    assert list(adapter._scratch_root.iterdir()) == []


@pytest.mark.parametrize("segment_count", [1, 2])
def test_production_release_cannot_complete_between_import_validation_and_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, segment_count: int
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path, segment_count=segment_count
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)
    original = authoring._require_import_deadline
    calls = 0
    started = threading.Event()
    released = threading.Event()
    errors: list[BaseException] = []
    released_before_publication: list[bool] = []

    def release() -> None:
        started.set()
        try:
            production.dispatch(
                {
                    "schema": PRODUCTION_ACTION_SCHEMA,
                    "request_id": "production.release.publication.race",
                    "action": "release_workspace",
                    "payload": {
                        "workspace_handle": projection.workspace_handle,
                        "expected_workspace_revision": projection.workspace_revision,
                        "expected_workspace_fingerprint": projection.workspace_fingerprint,
                    },
                }
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            released.set()

    worker = threading.Thread(target=release, daemon=True)

    def final_check(deadline: float, cancelled: Callable[[], bool]) -> None:
        nonlocal calls
        calls += 1
        original(deadline, cancelled)
        if calls == segment_count + 3:
            worker.start()
            assert started.wait(2.0)
            released_before_publication.append(released.wait(0.25))

    monkeypatch.setattr(authoring, "_require_import_deadline", final_check)
    try:
        response = authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 10.0,
        )
    finally:
        if worker.ident is not None:
            worker.join(3.0)
    assert released_before_publication == [False]
    assert released.is_set() and not worker.is_alive() and errors == []
    assert response.receipt.disposition == "created"
    imported = authoring._entries[request.authoring_workspace_handle].imported_outputs
    assert len(imported) == segment_count
    assert all(not row.source.current() for row in imported.values())


def test_import_projection_exception_releases_staged_source_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)

    def fail_projection(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("synthetic projection failure")

    monkeypatch.setattr(authoring, "_projection", fail_projection)
    with pytest.raises(RuntimeError, match="synthetic projection failure"):
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )

    entry = authoring._entries[request.authoring_workspace_handle]
    assert entry.importing is None and entry.imported_outputs == {}
    assert entry.reference.revision == request.expected_authoring_reference_revision
    assert list(adapter._scratch_root.iterdir()) == []


def test_import_rejects_media_facts_that_do_not_match_the_production_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)

    def mismatched_probe(
        _self: QualifiedAVMediaAdapter,
        *,
        source_path: Path,
        deadline: float,
        cancellation: object | None = None,
    ) -> AuthoringVideoProbePayload:
        assert cancellation is not None and not cancellation.is_cancelled()  # type: ignore[attr-defined]
        assert deadline > time.monotonic()
        body = source_path.read_bytes()
        return AuthoringVideoProbePayload(
            _video_probe_wire(frame_count=3, width=319, height=240),
            "sha256:" + hashlib.sha256(body).hexdigest(),
            len(body),
        )

    monkeypatch.setattr(QualifiedAVMediaAdapter, "probe_authoring_video_source", mismatched_probe)
    with pytest.raises(AuthoringWorkbenchError) as mismatch:
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )

    assert (mismatch.value.status, mismatch.value.code) == (422, "source_unavailable")
    entry = authoring._entries[request.authoring_workspace_handle]
    assert entry.imported_outputs == {}
    assert entry.reference.revision == request.expected_authoring_reference_revision
    assert list(adapter._scratch_root.iterdir()) == []


def test_imported_source_is_revoked_by_production_revision_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)
    response = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 5.0,
    )
    source = next(
        iter(authoring._entries[request.authoring_workspace_handle].imported_outputs.values())
    ).source
    assert response.receipt.disposition == "created" and source.current()

    production_entry = production._entries[request.production_workspace_handle]
    workspace = production_entry.workspace
    production_entry.workspace = revise_workspace(
        workspace,
        expected_workspace_fingerprint=workspace.fingerprint,
        accepted_intent_authorities=tuple(
            AcceptedIntentAuthority(
                segment.segment_id,
                segment.accepted_intent_fingerprint,
            )
            for segment in workspace.segments
        ),
    )

    assert not source.current()


@pytest.mark.parametrize(
    ("assembly_timing", "revocation"),
    [
        ("before_import", "release"),
        ("after_import", "release"),
        ("after_import", "store_tamper"),
        ("after_import", "expiry"),
        ("during_probe", "release"),
    ],
)
def test_automatic_assembly_preserves_imported_owner_and_render_borrower(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assembly_timing: str,
    revocation: str,
) -> None:
    from test_production_import import (
        _m26_assemble_action,
        _m26_read,
        _m26_store_backed_publication,
    )

    production, ready, callbacks, receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    production_entry = production._entries[ready.workspace_handle]
    segment = production_entry.workspace.segments[0]
    context_handle = "ws_" + "a" * 32
    seed = SidebarAuthoringSeed(
        source_id=segment.source_id,
        task_mode=segment.task_mode,
        registry_fingerprint=segment.reference_registry_fingerprint,
        sources=(),
        lineage_token=production_entry.lineage_token,
    )
    authoring = AuthoringWorkspaceRegistry(
        workspace_claim=lambda _handle: SidebarAuthoringWorkspaceClaim(seed, None),
        clock=lambda: 1_000.0,
    )
    created = authoring.dispatch(
        _authoring_action(
            "authoring.automatic.create",
            "create_authoring_workspace",
            context_workspace_handle=context_handle,
        )
    )
    assert created.body is not None
    before = cast(dict[str, Any], created.body)
    initialized = authoring.dispatch(
        _authoring_action(
            "authoring.automatic.initialize",
            "initialize_timeline_history",
            workspace_handle=before["workspace_handle"],
            expected_reference_revision=before["reference"]["revision"],
            expected_timeline_revision=before["timeline"]["revision"],
            authoring_schema=NLE_AUTHORING_SCHEMA,
            profile_id=NLE_AUTHORING_PROFILE_ID,
            operation_profile_id=NLE_OPERATION_PROFILE_ID,
        )
    )
    assert initialized.body is not None
    history = cast(dict[str, Any], initialized.body)
    adapter = _qualified_test_adapter(
        tmp_path,
        monkeypatch,
        frame_count=receipts[0].shape[0],
        height=receipts[0].shape[1],
        width=receipts[0].shape[2],
    )
    production.dispatch(_m26_assemble_action(ready, "request.m26.import_lifetime.assemble"))

    if assembly_timing == "before_import":
        callbacks.pop()()
        ready = _m26_read(production, ready)
        assert ready.assembly.state == "succeeded"
    elif assembly_timing == "during_probe":
        original_probe = QualifiedAVMediaAdapter.probe_authoring_video_source

        def assemble_after_probe(
            self: QualifiedAVMediaAdapter,
            *,
            source_path: Path,
            deadline: float,
            cancellation: object | None = None,
        ) -> AuthoringVideoProbePayload:
            result = original_probe(
                self,
                source_path=source_path,
                deadline=deadline,
                cancellation=cancellation,  # type: ignore[arg-type]
            )
            callbacks.pop()()
            return result

        monkeypatch.setattr(
            QualifiedAVMediaAdapter, "probe_authoring_video_source", assemble_after_probe
        )

    request = _import_request(ready, before, history, output_indexes=(0,))
    response = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 10.0,
    )
    imported = authoring._entries[request.authoring_workspace_handle].imported_outputs
    assert response.receipt.disposition == "created" and len(imported) == 1
    source = next(iter(imported.values())).source
    assert source.expected_receipt is receipts[0]
    assert source.store is production_entry.artifact_store
    borrower = source.borrow_for_render()
    try:
        assert source.current() and borrower.current()
        if assembly_timing == "after_import":
            callbacks.pop()()
        succeeded = _m26_read(production, ready)
        assert succeeded.assembly.state == "succeeded"
        assert source.current() and borrower.current()
        assert borrower.path == source.lease.path
        assert len(imported) == 1  # The second original and aggregate were not imported.
        assert (
            authoring.import_production_outputs(
                request,
                production_registry=production,
                media_adapter=adapter,
                deadline=time.monotonic() + 5.0,
            )
            is response
        )
        after_history = authoring.dispatch(
            _authoring_action(
                "authoring.automatic.history",
                "read_timeline_history",
                workspace_handle=request.authoring_workspace_handle,
            )
        )
        assert after_history.body is not None
        authoring_state = cast(dict[str, Any], after_history.body)["authoring"]
        assert (
            authoring_state["timeline_fingerprint"] == history["authoring"]["timeline_fingerprint"]
        )
        assert authoring_state["timeline_revision"] == history["authoring"]["timeline_revision"]

        if revocation == "release":
            production.dispatch(
                {
                    "schema": PRODUCTION_ACTION_SCHEMA,
                    "request_id": "production.automatic.release",
                    "action": "release_workspace",
                    "payload": {
                        "workspace_handle": succeeded.workspace_handle,
                        "expected_workspace_revision": succeeded.workspace_revision,
                        "expected_workspace_fingerprint": succeeded.workspace_fingerprint,
                    },
                }
            )
        elif revocation == "store_tamper":
            artifact_path = source.store._artifact_path(receipts[0].artifact_id)
            artifact_path.write_bytes(b"x" * receipts[0].byte_length)
        else:
            production._clock_ms = lambda: receipts[0].expires_at_ms
        assert not source.current() and not borrower.current()
        assert borrower.path is None
        assert not source.lease.released  # Copied bytes do not override revoked authority.
    finally:
        borrower.release()
        source.release()
    assert source.lease.released


def test_bounded_multi_import_and_mixed_reimport_advance_once_per_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path, segment_count=3
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    first_request = _import_request(projection, before, history, output_indexes=(0, 1))
    first = authoring.import_production_outputs(
        first_request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 5.0,
    )
    assert [row.disposition for row in first.receipt.rows] == ["created", "created"]
    assert first.receipt.next_reference_revision == first.receipt.prior_reference_revision + 1
    assert (
        first.receipt.next_nle_workspace_revision == first.receipt.prior_nle_workspace_revision + 1
    )

    first_history = authoring.dispatch(
        _authoring_action(
            "history.after.first.batch",
            "read_timeline_history",
            workspace_handle=first_request.authoring_workspace_handle,
        )
    )
    assert first_history.body is not None
    mixed_request = _import_request(
        projection,
        first.authoring_projection,
        first_history.body,
        request_id="import.integration.mixed",
    )
    mixed = authoring.import_production_outputs(
        mixed_request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 5.0,
    )

    assert [row.disposition for row in mixed.receipt.rows] == [
        "already_imported",
        "already_imported",
        "created",
    ]
    assert [row.asset_id for row in mixed.receipt.rows[:2]] == [
        row.asset_id for row in first.receipt.rows
    ]
    assert len({row.asset_id for row in mixed.receipt.rows}) == 3
    assert mixed.receipt.next_reference_revision == mixed.receipt.prior_reference_revision + 1
    assert (
        mixed.receipt.next_nle_workspace_revision == mixed.receipt.prior_nle_workspace_revision + 1
    )
    mixed_history = authoring.dispatch(
        _authoring_action(
            "history.after.mixed.batch",
            "read_timeline_history",
            workspace_handle=first_request.authoring_workspace_handle,
        )
    )
    assert mixed_history.body is not None
    snapshot = cast(dict[str, Any], mixed_history.body["authoring"])
    primary_track = next(track for track in snapshot["tracks"] if track["kind"] == "primary_video")
    inserted = authoring.dispatch(
        {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": "history.insert.imported",
            "action": "apply_timeline_transaction",
            "payload": {
                "schema": TIMELINE_TRANSACTION_SCHEMA_V2,
                "authoring_schema": NLE_AUTHORING_SCHEMA,
                "profile_id": NLE_AUTHORING_PROFILE_ID,
                "operation_profile_id": NLE_OPERATION_PROFILE_ID,
                "request_id": "history.insert.imported",
                "transaction_id": "tx.insert.imported",
                "workspace_handle": first_request.authoring_workspace_handle,
                "expected_workspace_revision": snapshot["workspace_revision"],
                "expected_timeline_revision": snapshot["timeline_revision"],
                "expected_timeline_fingerprint": snapshot["timeline_fingerprint"],
                "expected_authoring_fingerprint": snapshot["authoring_fingerprint"],
                "commands": [
                    {
                        "kind": "insert_asset_clip",
                        "payload": {
                            "clip": {
                                "clip_id": "clip.imported",
                                "asset_id": mixed.receipt.rows[2].asset_id,
                                "track_id": primary_track["track_id"],
                                "start_frame": 0,
                                "duration_frames": 2,
                                "source_start_frame": 0,
                                "enabled": True,
                                "transform": {
                                    "anchor_x_bp": 5000,
                                    "anchor_y_bp": 5000,
                                    "position_x_bp": 0,
                                    "position_y_bp": 0,
                                    "scale_x_bp": 10000,
                                    "scale_y_bp": 10000,
                                    "rotation_mdeg": 0,
                                },
                                "crop": {
                                    "left_bp": 0,
                                    "top_bp": 0,
                                    "right_bp": 0,
                                    "bottom_bp": 0,
                                },
                                "opacity_bp": 10000,
                                "blend": "normal",
                                "text": None,
                                "transition": {"kind": "none", "duration_frames": 0},
                                "effect": {
                                    "kind": "none",
                                    "brightness_permille": 0,
                                    "contrast_permille": 1000,
                                    "saturation_permille": 1000,
                                },
                            },
                        },
                    },
                ],
            },
        }
    )
    assert inserted.status == 200 and inserted.body is not None
    inserted_state = cast(dict[str, Any], inserted.body["authoring"])
    assert inserted_state["clips"][0]["asset_id"] == mixed.receipt.rows[2].asset_id
    assert inserted_state["content_end_exclusive"] == 2
    inserted_render = cast(dict[str, Any], inserted.body["render_snapshot"])
    inserted_output = cast(dict[str, Any], inserted_render["output"])
    assert inserted_output["duration_frames"] == 2
    historical_replay = authoring.import_production_outputs(
        first_request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 5.0,
    )
    assert historical_replay is first
    after_replay = authoring.dispatch(
        _authoring_action(
            "history.after.historical.replay",
            "read_timeline_history",
            workspace_handle=first_request.authoring_workspace_handle,
        )
    )
    assert after_replay.body is not None
    assert after_replay.body["authoring"] == inserted_state
    entry = authoring._entries[first_request.authoring_workspace_handle]
    assert len(entry.imported_outputs) == 3
    sources = tuple(imported.source for imported in entry.imported_outputs.values())
    authoring.dispatch(
        _authoring_action(
            "authoring.release.multi",
            "release_workspace",
            workspace_handle=first_request.authoring_workspace_handle,
        )
    )
    assert all(source.lease.released for source in sources)


def test_second_staging_failure_releases_prefix_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path, segment_count=2
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)
    original = PrivateSegmentArtifactStore.stream_artifact_into
    calls = [0]

    def fail_second(
        self: PrivateSegmentArtifactStore,
        expected: object,
        destination: Path,
        *,
        should_cancel: object = None,
    ) -> tuple[int, str]:
        calls[0] += 1
        if calls[0] == 2:
            raise ArtifactStoreError("store_entry_unavailable")
        return original(
            self,
            expected,  # type: ignore[arg-type]
            destination,
            should_cancel=should_cancel,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(PrivateSegmentArtifactStore, "stream_artifact_into", fail_second)
    with pytest.raises(AuthoringWorkbenchError) as failed:
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )

    assert (failed.value.status, failed.value.code) == (422, "source_unavailable")
    entry = authoring._entries[request.authoring_workspace_handle]
    assert entry.reference.revision == request.expected_authoring_reference_revision
    assert entry.imported_outputs == {}
    assert entry.importing is None
    assert list(adapter._scratch_root.iterdir()) == []


def test_expired_production_output_and_released_authoring_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)
    production._clock_ms = lambda: 600_001
    with pytest.raises(AuthoringWorkbenchError) as expired:
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )
    assert (expired.value.status, expired.value.code) == (422, "artifact_not_ready")

    authoring.dispatch(
        _authoring_action(
            "authoring.release.before.import",
            "release_workspace",
            workspace_handle=request.authoring_workspace_handle,
        )
    )
    production._clock_ms = lambda: 2_000
    with pytest.raises(AuthoringWorkbenchError) as gone:
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )
    assert (gone.value.status, gone.value.code) == (410, "authoring_gone")


def test_concurrent_same_request_is_busy_then_replays_the_single_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    request = _import_request(projection, before, history)
    original = PrivateSegmentArtifactStore.stream_artifact_into
    entered = threading.Event()
    resume = threading.Event()
    outcome: list[object] = []

    def hold_stream(
        self: PrivateSegmentArtifactStore,
        expected: object,
        destination: Path,
        *,
        should_cancel: object = None,
    ) -> tuple[int, str]:
        entered.set()
        assert resume.wait(timeout=2.0)
        return original(
            self,
            expected,  # type: ignore[arg-type]
            destination,
            should_cancel=should_cancel,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(PrivateSegmentArtifactStore, "stream_artifact_into", hold_stream)

    def import_once() -> None:
        try:
            outcome.append(
                authoring.import_production_outputs(
                    request,
                    production_registry=production,
                    media_adapter=adapter,
                    deadline=time.monotonic() + 5.0,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            outcome.append(exc)

    worker = threading.Thread(target=import_once)
    worker.start()
    assert entered.wait(timeout=2.0)
    with pytest.raises(AuthoringWorkbenchError) as busy:
        authoring.import_production_outputs(
            request,
            production_registry=production,
            media_adapter=adapter,
            deadline=time.monotonic() + 5.0,
        )
    assert (busy.value.status, busy.value.code) == (423, "workspace_busy")
    resume.set()
    worker.join(timeout=3.0)
    assert not worker.is_alive() and len(outcome) == 1
    assert not isinstance(outcome[0], BaseException)
    committed = outcome[0]
    replay = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 5.0,
    )
    assert replay is committed
    assert len(authoring._entries[request.authoring_workspace_handle].imported_outputs) == 1


class _ImportRoutes(list[SimpleNamespace]):
    def post(self, path: str):  # type: ignore[no-untyped-def]
        def decorate(handler):  # type: ignore[no-untyped-def]
            self.append(SimpleNamespace(method="POST", path=path, handler=handler))
            return handler

        return decorate


class _ImportHeaders:
    def __init__(self, values: dict[str, list[str]]) -> None:
        self._values = values

    def getall(self, name: str, default: list[str]) -> list[str]:
        return self._values.get(name.lower(), default)


class _ImportContent:
    def __init__(self, payload: bytes) -> None:
        self._chunks = [payload, b""]

    async def read(self, _limit: int) -> bytes:
        return self._chunks.pop(0)


def _import_http_request(payload: bytes, origin: str) -> SimpleNamespace:
    return SimpleNamespace(
        content_type="application/json",
        content_length=len(payload),
        content=_ImportContent(payload),
        headers=_ImportHeaders({"origin": [origin], "host": [LOOPBACK_HOST]}),
        transport=ListenerTransport(),
    )


async def _drive_owned_import_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    adapter = _qualified_test_adapter(tmp_path, monkeypatch)
    service = ProductionAuthoringImportService(
        production_registry=production,
        authoring_registry=authoring,
        media_runtime=lambda: adapter,
    )
    request = _import_request(projection, before, history, request_id="import.route.1")
    payload = json.dumps(request.to_wire(), separators=(",", ":")).encode()
    routes = _ImportRoutes()
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(
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

    with (
        composition_root.substituted(composition_root.PRODUCTION_AUTHORING_IMPORT, service),
        patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}),
    ):
        assert ensure_production_authoring_import_route_registered()
        assert ensure_production_authoring_import_route_registered()
        assert [(row.method, row.path) for row in routes] == [
            ("POST", PRODUCTION_AUTHORING_IMPORT_ROUTE)
        ]
        handler = routes[0].handler
        foreign = await handler(_import_http_request(payload, "https://example.invalid"))
        assert foreign.status == 403 and foreign.body is None
        accepted = await handler(_import_http_request(payload, OWNED_ORIGIN))
        assert accepted.status == 200
        serialized = json.dumps(accepted.body, sort_keys=True).lower()
        assert all(token not in serialized for token in ('"path"', '"url"', "filename"))

        invalid_wire = request.to_wire()
        invalid_wire["path"] = "private.mp4"
        invalid_payload = json.dumps(invalid_wire, separators=(",", ":")).encode()
        invalid = await handler(_import_http_request(invalid_payload, OWNED_ORIGIN))
        assert invalid.status == 400 and invalid.body is None


def test_owned_import_route_is_idempotent_same_origin_typed_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_drive_owned_import_route(tmp_path, monkeypatch))


def test_normal_192_frame_production_output_imports_without_contract_narrowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"bounded-normal-production-video"
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path,
        artifact_bodies=(body,),
        frame_count=192,
        width=512,
        height=512,
    )
    adapter = _qualified_test_adapter(
        tmp_path,
        monkeypatch,
        frame_count=192,
        width=512,
        height=512,
    )
    request = _import_request(projection, before, history)
    response = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 30.0,
    )
    assert isinstance(response, ProductionAuthoringImportResponseV2)
    row = response.receipt.rows[0]
    imported = authoring._entries[request.authoring_workspace_handle].imported_outputs
    source = next(iter(imported.values())).source
    assert (source.facts.frame_count, source.facts.width, source.facts.height) == (192, 512, 512)
    authoring_state = cast(dict[str, Any], response.history_projection["authoring"])
    primary_track = next(
        track for track in authoring_state["tracks"] if track["kind"] == "primary_video"
    )
    inserted = authoring.dispatch(
        {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": "history.insert.normal.192",
            "action": "apply_timeline_transaction",
            "payload": _insert_asset_transaction(
                authoring_state=authoring_state,
                asset_id=row.asset_id,
                track_id=primary_track["track_id"],
                request_id="history.insert.normal.192",
            ),
        }
    )
    assert inserted.status == 200
    bound = authoring.prepare_render_plan(request.authoring_workspace_handle)
    job = acquire_render_job_sources(bound, deadline=time.monotonic() + 30.0)
    assert job.read_source(row.asset_id) == body
    job.release()
    authoring.dispatch(
        _authoring_action(
            "authoring.release.normal",
            "release_workspace",
            workspace_handle=request.authoring_workspace_handle,
        )
    )
    assert source.lease.released


def test_source_free_t2v_workspace_creates_generated_only_binding_on_first_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path,
        initial_source_binding=False,
    )
    request = _import_request(projection, before, history)
    entry_before = authoring._entries[request.authoring_workspace_handle]
    assert entry_before.source_binding is None
    assert entry_before.initialized_sources is not None
    assert entry_before.initialized_sources.generation == 0

    response = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=_qualified_test_adapter(tmp_path, monkeypatch),
        deadline=time.monotonic() + 30.0,
    )

    entry_after = authoring._entries[request.authoring_workspace_handle]
    assert response.receipt.disposition == "created"
    assert type(entry_after.source_binding) is CompositeAuthoringSourceBindingReceipt
    assert entry_after.source_binding.generation == 1
    assert entry_after.initialized_sources is not None
    assert entry_after.initialized_sources.generation == 1


@pytest.mark.parametrize("origin", ["generated", "unknown_origin"])
def test_imported_generated_source_reaches_executor_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    from comfyui_h3_context.adapters.authoring_render_executor import (
        RenderPreparationError,
        prepare_render_assets,
    )
    from comfyui_h3_context.adapters.authoring_render_source import prepare_bound_render_plan
    from comfyui_h3_context.adapters.authoring_render_store import RenderOutputStore

    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path
    )
    request = _import_request(projection, before, history)
    response = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=_qualified_test_adapter(tmp_path, monkeypatch),
        deadline=time.monotonic() + 5,
    )
    assert isinstance(response, ProductionAuthoringImportResponseV2)
    asset_id = response.receipt.rows[0].asset_id
    authoring_state = cast(dict[str, Any], response.history_projection["authoring"])
    primary_track = next(
        track for track in authoring_state["tracks"] if track["kind"] == "primary_video"
    )
    inserted = authoring.dispatch(
        {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": "history.insert.executor",
            "action": "apply_timeline_transaction",
            "payload": _insert_asset_transaction(
                authoring_state=authoring_state,
                asset_id=asset_id,
                track_id=primary_track["track_id"],
                request_id="history.insert.executor",
                duration_frames=1,
            ),
        }
    )
    assert inserted.status == 200
    original = authoring.prepare_render_plan(request.authoring_workspace_handle)
    assert original._history.snapshot is not None
    wire = cast(dict[str, Any], original._history.snapshot.to_wire())
    wire["output"].update(width=64, height=64, duration_frames=24)
    wire["tracks"] = [
        dict(track_id="primary", kind="primary_video", order=0, enabled=True, locked=False)
    ]
    clip = dict(wire["clips"][0])
    clip.update(
        clip_id="generated",
        asset_id=asset_id,
        track_id="primary",
        start_frame=0,
        duration_frames=1,
        source_start_frame=0,
    )
    wire["clips"] = [clip]
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    bound = prepare_bound_render_plan(original._history, decode_public_snapshot(wire), lambda: True)
    sources = acquire_render_job_sources(bound, deadline=time.monotonic() + 5)
    output = tmp_path / "renders"
    output.mkdir()
    store = RenderOutputStore(output)
    try:
        stage = store.begin("render-" + "e" * 32, FP)
        plan = replace(
            bound.plan,
            source_bindings=tuple(
                replace(row, origin=origin) for row in bound.plan.source_bindings
            ),
        )
        control = SimpleNamespace(deadline=time.monotonic() + 5, is_cancelled=lambda: False)
        if origin == "unknown_origin":
            with pytest.raises(RenderPreparationError, match="plan_mismatch"):
                prepare_render_assets(plan=plan, sources=sources, stage=stage, control=control)
        else:
            prepared = prepare_render_assets(
                plan=plan, sources=sources, stage=stage, control=control
            )
            assert len(prepared.media) == 1
            assert prepared.media[0].origin == "generated"
            assert prepared.media[0].path.suffix == ".mp4"
            assert prepared.media[0].path.read_bytes() == b"bounded-generated-video-1"
    finally:
        sources.release()
        store.close()


def test_real_encoded_production_artifact_imports_through_the_qualified_runtime(
    tmp_path: Path,
) -> None:
    ffmpeg_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    ffprobe_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
    if not ffmpeg_value or not ffprobe_value:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    ffmpeg = Path(ffmpeg_value).resolve(strict=True)
    ffprobe = Path(ffprobe_value).resolve(strict=True)
    encoded = (tmp_path / "production-segment.mp4").resolve()
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=512x512:rate=24:duration=8",
            "-c:v",
            "libx264",
            "-bf",
            "0",
            "-vf",
            "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-movflags",
            "+faststart",
            "-y",
            str(encoded),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    body = encoded.read_bytes()
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path,
        artifact_bodies=(body,),
        frame_count=192,
        width=512,
        height=512,
    )
    scratch = (tmp_path / "qualified-scratch").resolve()
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=scratch,
        clock_ms=lambda: 1,
    )
    request = _import_request(projection, before, history)
    response = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 30.0,
    )
    row = response.receipt.rows[0]
    imported = authoring._entries[request.authoring_workspace_handle].imported_outputs
    source = next(iter(imported.values())).source
    assert (source.facts.frame_count, source.facts.width, source.facts.height) == (192, 512, 512)
    _place_imported_asset_for_render(
        authoring,
        workspace_handle=request.authoring_workspace_handle,
        asset_id=row.asset_id,
        request_id="real-encoded.place-before-render",
    )
    bound = authoring.prepare_render_plan(request.authoring_workspace_handle)
    job = acquire_render_job_sources(bound, deadline=time.monotonic() + 30.0)
    assert job.read_source(row.asset_id) == body
    job.release()
    authoring.dispatch(
        _authoring_action(
            "authoring.release.real",
            "release_workspace",
            workspace_handle=request.authoring_workspace_handle,
        )
    )
    assert source.lease.released and list(scratch.iterdir()) == []


def test_native_srgb_b_frame_artifact_is_converted_without_changing_its_receipt(
    tmp_path: Path,
) -> None:
    ffmpeg_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    ffprobe_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
    if not ffmpeg_value or not ffprobe_value:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    ffmpeg = Path(ffmpeg_value).resolve(strict=True)
    ffprobe = Path(ffprobe_value).resolve(strict=True)
    encoded = (tmp_path / "native-output.mp4").resolve()
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=512x512:rate=24:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=32000:duration=2",
            "-vf",
            "setparams=range=tv:color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "iec61966-2-1",
            "-c:a",
            "aac",
            "-ar",
            "32000",
            "-movflags",
            "+faststart",
            "-y",
            str(encoded),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    original = encoded.read_bytes()
    production, projection, authoring, before, history, store = _registries_with_ready_output(
        tmp_path,
        artifact_bodies=(original,),
        frame_count=48,
        width=512,
        height=512,
    )
    scratch = (tmp_path / "qualified-scratch").resolve()
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=scratch,
        clock_ms=lambda: 1,
    )
    request = _import_request(projection, before, history)
    response = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 30.0,
    )
    source = next(
        iter(authoring._entries[request.authoring_workspace_handle].imported_outputs.values())
    ).source
    assert source.facts.frame_count == 48
    assert source.facts.color_transfer == "bt709"
    assert source.current()
    assert source.lease.path.read_bytes() != original
    assert (
        store.inspect(source.expected_receipt, maximum_bytes=len(original)).status.value
        == "reusable"
    )
    _place_imported_asset_for_render(
        authoring,
        workspace_handle=request.authoring_workspace_handle,
        asset_id=response.receipt.rows[0].asset_id,
        request_id="native-srgb.place-before-render",
    )
    bound = authoring.prepare_render_plan(request.authoring_workspace_handle)
    job = acquire_render_job_sources(bound, deadline=time.monotonic() + 30.0)
    assert job.read_source(response.receipt.rows[0].asset_id) == source.lease.path.read_bytes()
    job.release()
    authoring.dispatch(
        _authoring_action(
            "authoring.release.converted",
            "release_workspace",
            workspace_handle=request.authoring_workspace_handle,
        )
    )
    assert source.lease.released and list(scratch.iterdir()) == []
