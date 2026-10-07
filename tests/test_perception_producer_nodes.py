"""M15-01 public media/perception producer contract and node tests."""

from __future__ import annotations

import json
import pickle
from copy import copy, deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from jsonschema import Draft202012Validator

from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.node_contracts import (
    NodeSocketType,
    default_node_contract_registry,
)
from comfyui_h3_context.core.perception_producer import (
    PERCEPTION_PRODUCER_SCHEMA,
    FingerprintClaim,
    MediaAdmissionEvidence,
    PerceptionProducerResult,
    ProducerDisposition,
    ProducerKind,
    ProducerReasonCode,
    ProducerRoute,
    validate_perception_producer_wire,
)
from comfyui_h3_context.nodes import (
    AUDIO_PERCEPTION_NODE_ID,
    MEDIA_ADMISSION_NODE_ID,
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    VISUAL_PERCEPTION_NODE_ID,
    H3AudioPerceptionProducerNode,
    H3MediaAdmissionProducerNode,
    H3VisualPerceptionProducerNode,
)
from comfyui_h3_context.public_api import get_public_manifest

FINGERPRINT = "a" * 64
ROOT = Path(__file__).resolve().parents[1]


def test_visual_request_appends_without_rebinding_legacy_workflow_widgets() -> None:
    saved = json.loads('{"nodes":[{"widgets_values":["ollama","unqualified","auto",false,true]}]}')
    node = H3VisualPerceptionProducerNode.INPUT_TYPES()
    assert list(node["required"]) == ["media", "route", "profile_id", "device", "cancel_requested"]
    assert list(node["optional"]) == ["local_service_consent", "request"]
    assert node["optional"]["request"] == ("H3_CONTEXT_REQUEST", {})
    widgets = [
        name
        for section in node.values()
        for name, (kind, _) in section.items()
        if kind in {"COMBO", "STRING", "BOOLEAN", "INT", "FLOAT"}
    ]
    assert dict(zip(widgets, saved["nodes"][0]["widgets_values"], strict=True)) == {
        "route": "ollama",
        "profile_id": "unqualified",
        "device": "auto",
        "cancel_requested": False,
        "local_service_consent": True,
    }
    assert list(H3AudioPerceptionProducerNode.INPUT_TYPES()["optional"]) == [
        "local_service_consent"
    ]
    contract = default_node_contract_registry().get(VISUAL_PERCEPTION_NODE_ID)
    assert tuple(item.name for item in contract.inputs) == (
        "media",
        "route",
        "profile_id",
        "device",
        "cancel_requested",
        "local_service_consent",
        "request",
    )
    assert contract.inputs[-1].socket_type is NodeSocketType.H3_CONTEXT_REQUEST
    assert not contract.inputs[-1].required


def test_retired_visual_profile_has_no_queueing_or_execution_authority() -> None:
    from comfyui_h3_context.core.perception_execution import PROFILE_MODELS, VISUAL_PROFILE

    assert VISUAL_PROFILE == "qwen38_27b_q4_visual_v2"
    assert "qwen38_27b_q4_visual_v1" not in PROFILE_MODELS
    assert (
        H3VisualPerceptionProducerNode.VALIDATE_INPUTS(
            profile_id="qwen38_27b_q4_visual_v1", route="ollama", local_service_consent=True
        )
        is not True
    )


def test_visual_request_validation_refuses_a_non_exact_carrier() -> None:
    assert str(
        H3VisualPerceptionProducerNode.VALIDATE_INPUTS(
            request={"duration_seconds": 5},  # type: ignore[arg-type]
        )
    ).startswith("invalid_request:")


def _image_evidence() -> MediaAdmissionEvidence:
    return MediaAdmissionEvidence(
        declared_source_fingerprint=FINGERPRINT,
        width_pixels=64,
        height_pixels=48,
        duration_seconds=0.0,
        sample_rate_hz=None,
        channel_count=None,
        reference_role="reference",
        reference_order=0,
    )


def _evidence_for(kind: str) -> MediaAdmissionEvidence:
    if kind == "image":
        return _image_evidence()
    if kind == "video":
        return replace(_image_evidence(), duration_seconds=1.0)
    return MediaAdmissionEvidence(
        declared_source_fingerprint=FINGERPRINT,
        width_pixels=None,
        height_pixels=None,
        duration_seconds=1.0,
        sample_rate_hz=8000,
        channel_count=1,
        reference_role="reference",
        reference_order=0,
    )


def test_media_node_completes_only_with_the_selected_host_payload() -> None:
    payload = object()
    (result,) = H3MediaAdmissionProducerNode().admit(
        media_kind="image",
        asset_id="image-1",
        admission_evidence=_image_evidence(),
        image=payload,
    )

    assert result.schema == PERCEPTION_PRODUCER_SCHEMA
    assert result.kind is ProducerKind.MEDIA
    assert result.disposition is ProducerDisposition.COMPLETE
    assert result.route is ProducerRoute.MANUAL_HOST
    assert result.runtime_payload is payload
    assert result.receipt.executed is True
    assert result.receipt.cleanup_succeeded is True
    assert result.receipt.progress_fraction == 1.0
    assert result.receipt.model_id == "not_applicable"
    assert result.component_types == ("admitted_image",)
    assert (
        result.admission_evidence.fingerprint_claim is FingerprintClaim.CALLER_DECLARED_UNVERIFIED
    )
    assert result.admission_evidence.width_pixels == 64
    assert result.admission_evidence.reference_role == "reference"


@pytest.mark.parametrize(  # type: ignore[untyped-decorator, unused-ignore]
    "kind", ["image", "video", "audio"]
)
def test_media_node_rejects_missing_or_mismatched_payload_without_evidence(kind: str) -> None:
    kwargs: dict[str, object | None] = {"image": None, "video": None, "audio": None}
    kwargs[{"image": "video", "video": "audio", "audio": "image"}[kind]] = object()
    (result,) = H3MediaAdmissionProducerNode().admit(
        media_kind=kind,
        asset_id="asset-1",
        admission_evidence=_evidence_for(kind),
        image=kwargs["image"],
        video=kwargs["video"],
        audio=kwargs["audio"],
    )

    assert result.disposition is ProducerDisposition.REJECTED
    assert result.runtime_payload is None
    assert result.components == ()
    assert result.receipt.executed is False
    assert result.receipt.progress_fraction == 0.0


def test_public_wire_is_bounded_redacted_and_json_portable() -> None:
    private_value = r"C:\private\signed-media.png?token=secret"
    (result,) = H3MediaAdmissionProducerNode().admit(
        media_kind="image",
        asset_id="image-1",
        admission_evidence=_image_evidence(),
        image=private_value,
    )

    wire = result.to_wire()
    encoded = json.dumps(wire, sort_keys=True)
    assert "private" not in encoded
    assert "token=" not in encoded
    assert private_value not in encoded
    assert "runtime_payload" not in wire
    evidence_wire = cast(dict[str, object], wire["admission_evidence"])
    assert evidence_wire["declared_source_fingerprint"] == FINGERPRINT
    assert evidence_wire["fingerprint_claim"] == "caller_declared_unverified"
    schema = json.loads(
        (ROOT / "governance/contracts/perception_producer_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(Draft202012Validator(schema).iter_errors(wire)) == []


@pytest.mark.parametrize(  # type: ignore[untyped-decorator, unused-ignore]
    ("node", "kind"),
    [
        (H3VisualPerceptionProducerNode(), ProducerKind.VISUAL),
        (H3AudioPerceptionProducerNode(), ProducerKind.AUDIO),
    ],
)
@pytest.mark.parametrize(  # type: ignore[untyped-decorator, unused-ignore]
    "route",
    [
        ProducerRoute.COMFYUI_NATIVE.value,
        ProducerRoute.OLLAMA.value,
        ProducerRoute.SPECIALIST.value,
    ],
)
def test_unqualified_perception_routes_are_explicitly_unavailable_without_fallback(
    node: H3VisualPerceptionProducerNode | H3AudioPerceptionProducerNode,
    kind: ProducerKind,
    route: str,
) -> None:
    (media,) = H3MediaAdmissionProducerNode().admit(
        media_kind="audio" if kind is ProducerKind.AUDIO else "image",
        asset_id="asset-1",
        admission_evidence=(
            MediaAdmissionEvidence(
                declared_source_fingerprint=FINGERPRINT,
                width_pixels=None,
                height_pixels=None,
                duration_seconds=1.0,
                sample_rate_hz=8000,
                channel_count=1,
                reference_role="reference",
                reference_order=0,
            )
            if kind is ProducerKind.AUDIO
            else _image_evidence()
        ),
        audio=object() if kind is ProducerKind.AUDIO else None,
        image=object() if kind is ProducerKind.VISUAL else None,
    )

    (result,) = node.produce(media, route=route, profile_id="not-qualified")

    assert result.kind is kind
    assert result.disposition is ProducerDisposition.UNAVAILABLE
    assert result.route.value == route
    assert result.profile_id == "not-qualified"
    assert result.runtime_payload is None
    assert result.components == ()
    assert result.receipt.executed is False
    assert result.receipt.backend_id == route
    assert result.receipt.model_id == "not_resolved"
    assert result.receipt.device == "auto"
    assert "fallback" not in result.reason.lower()


def test_pre_execution_cancellation_is_terminal_and_produces_no_evidence() -> None:
    (media,) = H3MediaAdmissionProducerNode().admit(
        media_kind="image",
        asset_id="image-1",
        admission_evidence=_image_evidence(),
        image=object(),
    )
    (result,) = H3VisualPerceptionProducerNode().produce(
        media,
        route="comfyui_native",
        profile_id="not-qualified",
        device="cpu",
        cancel_requested=True,
    )
    assert result.disposition is ProducerDisposition.CANCELLED
    assert result.components == ()
    assert result.runtime_payload is None
    assert result.receipt.executed is False
    assert result.receipt.progress_fraction == 0.0
    assert result.receipt.device == "cpu"


def test_result_contract_rejects_forged_complete_and_polymorphic_evidence() -> None:
    (complete,) = H3MediaAdmissionProducerNode().admit(
        media_kind="image",
        asset_id="image-1",
        admission_evidence=_image_evidence(),
        image=object(),
    )
    with pytest.raises(ContractValidationError):
        replace(complete, runtime_payload=None)
    with pytest.raises(ContractValidationError):
        replace(complete, disposition=ProducerDisposition.UNAVAILABLE)
    with pytest.raises(ContractValidationError):
        H3VisualPerceptionProducerNode().produce(Mock(spec=type(complete)), route="comfyui_native")
    for forged in (
        replace(complete),
        copy(complete),
        deepcopy(complete),
        pickle.loads(pickle.dumps(complete)),  # noqa: S301 - trusted local regression object
    ):
        with pytest.raises(ContractValidationError):
            forged.to_wire()
        with pytest.raises(ContractValidationError):
            _ = forged.component_types


def test_complete_authority_detects_runtime_payload_reassignment() -> None:
    (result,) = H3MediaAdmissionProducerNode().admit(
        media_kind="image",
        asset_id="image-1",
        admission_evidence=_image_evidence(),
        image=object(),
    )
    object.__setattr__(result, "runtime_payload", object())
    with pytest.raises(ContractValidationError):
        result.to_wire()
    with pytest.raises(ContractValidationError):
        H3VisualPerceptionProducerNode().produce(result, route="comfyui_native")

    (metadata_result,) = H3MediaAdmissionProducerNode().admit(
        media_kind="image",
        asset_id="image-1",
        admission_evidence=_image_evidence(),
        image=object(),
    )
    metadata_result.__dict__["asset_id"] = "image-2"
    with pytest.raises(ContractValidationError):
        metadata_result.to_wire()


@pytest.mark.parametrize(  # type: ignore[untyped-decorator, unused-ignore]
    "kwargs",
    [
        {"asset_id": "x" * 129},
        {"admission_evidence": Mock(spec=MediaAdmissionEvidence)},
    ],
)
def test_media_boundary_rejects_hostile_identity_values_with_typed_errors(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "media_kind": "image",
        "asset_id": "image-1",
        "admission_evidence": _image_evidence(),
        "image": object(),
    }
    values.update(kwargs)
    with pytest.raises(ContractValidationError):
        H3MediaAdmissionProducerNode().admit(**values)  # type: ignore[arg-type]


def test_admission_evidence_rejects_non_digest_caller_declaration() -> None:
    with pytest.raises(ContractValidationError):
        replace(_image_evidence(), declared_source_fingerprint="A" * 64)


def test_three_nodes_and_sockets_are_registered_in_the_declarative_contract() -> None:
    expected = {
        MEDIA_ADMISSION_NODE_ID,
        VISUAL_PERCEPTION_NODE_ID,
        AUDIO_PERCEPTION_NODE_ID,
    }
    assert expected <= set(NODE_CLASS_MAPPINGS)
    assert expected <= set(NODE_DISPLAY_NAME_MAPPINGS)

    registry = default_node_contract_registry()
    definitions = {item.node_id: item for item in registry.definitions}
    assert expected <= set(definitions)
    assert NodeSocketType.H3_MEDIA_PRODUCER_RESULT.value == "H3_MEDIA_PRODUCER_RESULT"
    assert NodeSocketType.H3_VISUAL_PRODUCER_RESULT.value == "H3_VISUAL_PRODUCER_RESULT"
    assert NodeSocketType.H3_AUDIO_PRODUCER_RESULT.value == "H3_AUDIO_PRODUCER_RESULT"
    assert (
        definitions[MEDIA_ADMISSION_NODE_ID].outputs[0].socket_type
        is NodeSocketType.H3_MEDIA_PRODUCER_RESULT
    )
    assert (
        definitions[VISUAL_PERCEPTION_NODE_ID].inputs[0].socket_type
        is NodeSocketType.H3_MEDIA_PRODUCER_RESULT
    )
    assert (
        definitions[AUDIO_PERCEPTION_NODE_ID].inputs[0].socket_type
        is NodeSocketType.H3_MEDIA_PRODUCER_RESULT
    )


def test_node_metadata_uses_visible_explicit_route_and_optional_host_media_inputs() -> None:
    media_inputs = H3MediaAdmissionProducerNode.INPUT_TYPES()
    assert set(media_inputs["optional"]) == {"image", "video", "audio"}
    assert media_inputs["optional"]["image"][0] == "IMAGE"
    assert "declared_source_fingerprint" in media_inputs["required"]
    assert "source_fingerprint" not in media_inputs["required"]
    assert (
        "unverified"
        in str(media_inputs["required"]["declared_source_fingerprint"][1]["tooltip"]).lower()
    )
    for field in (
        "width_pixels",
        "height_pixels",
        "duration_seconds",
        "sample_rate_hz",
        "channel_count",
        "reference_role",
        "reference_order",
    ):
        assert field in media_inputs["required"]
    visual_inputs = H3VisualPerceptionProducerNode.INPUT_TYPES()
    assert visual_inputs["required"]["media"][0] == "H3_MEDIA_PRODUCER_RESULT"
    assert set(cast(list[str], visual_inputs["required"]["route"][1]["options"])) == {
        "comfyui_native",
        "ollama",
        "specialist",
    }
    assert set(cast(list[str], visual_inputs["required"]["device"][1]["options"])) == {
        "auto",
        "cpu",
        "cuda",
        "mps",
    }
    assert visual_inputs["required"]["cancel_requested"][0] == "BOOLEAN"


def test_static_workflow_fixture_wires_host_media_to_explicit_nonqualified_routes() -> None:
    path = ROOT / "workflows/m15_01_perception_producers.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    classes = [value["class_type"] for value in fixture["prompt"].values()]
    assert classes.count(MEDIA_ADMISSION_NODE_ID) == 2
    assert VISUAL_PERCEPTION_NODE_ID in classes
    assert AUDIO_PERCEPTION_NODE_ID in classes
    assert fixture["expected"]["automatic_fallback"] is False
    assert fixture["expected"]["qualified_visual_profiles"] == 0
    manifest_ref = next(
        item
        for item in get_public_manifest().workflow_fixtures
        if item.fixture_id == "workflow.m15_01.perception_producers"
    )
    assert manifest_ref.path == "workflows/m15_01_perception_producers.json"


def test_complete_component_surface_requires_current_module_authority() -> None:
    (valid,) = H3MediaAdmissionProducerNode().admit(
        media_kind="image",
        asset_id="image-1",
        admission_evidence=_image_evidence(),
        image=object(),
    )
    for forged in (
        replace(valid),
        copy(valid),
        deepcopy(valid),
        pickle.loads(pickle.dumps(valid)),  # noqa: S301 - trusted local regression object
    ):
        with pytest.raises(ContractValidationError):
            _ = forged.component_types
    valid.__dict__["asset_id"] = "changed"
    with pytest.raises(ContractValidationError):
        _ = valid.component_types


def test_complete_surfaces_do_not_dispatch_caller_overridden_authority_methods() -> None:
    (valid,) = H3MediaAdmissionProducerNode().admit(
        media_kind="image",
        asset_id="image-1",
        admission_evidence=_image_evidence(),
        image=object(),
    )

    class ForgedBinding(PerceptionProducerResult):
        def assert_current(self) -> None:
            return None

    forged = object.__new__(ForgedBinding)
    forged.__dict__.update(valid.__dict__)
    for operation in (
        lambda: forged.component_types,
        lambda: forged.reason,
        forged.to_wire,
    ):
        with pytest.raises(ContractValidationError):
            operation()
    with pytest.raises(ContractValidationError):
        H3VisualPerceptionProducerNode().produce(forged, route="comfyui_native")


def test_semantic_wire_validator_rejects_cross_field_and_fingerprint_contradictions() -> None:
    (result,) = H3MediaAdmissionProducerNode().admit(
        media_kind="image",
        asset_id="image-1",
        admission_evidence=_image_evidence(),
        image=object(),
    )
    wire = result.to_wire()
    validate_perception_producer_wire(wire)
    mutations: list[dict[str, Any]] = []
    for path, field, replacement in (
        ("receipt", "route", "ollama"),
        ("receipt", "profile_id", "different"),
        ("root", "route", "ollama"),
        ("root", "kind", "audio"),
        ("receipt", "execution_fingerprint", "f" * 64),
        ("receipt", "backend_id", "caller_backend"),
        ("receipt", "cleanup_succeeded", False),
        ("admission_evidence", "width_pixels", 65),
        ("root", "reason", "selected profile execution failed"),
    ):
        changed = cast(dict[str, Any], json.loads(json.dumps(wire)))
        target = changed if path == "root" else cast(dict[str, Any], changed[path])
        target[field] = replacement
        mutations.append(changed)
    for changed in mutations:
        with pytest.raises(ContractValidationError):
            validate_perception_producer_wire(changed)
    malformed_values: tuple[object, ...] = (
        None,
        [],
        Mock(spec=dict),
        {"schema": PERCEPTION_PRODUCER_SCHEMA},
    )
    for malformed in malformed_values:
        with pytest.raises(ContractValidationError):
            validate_perception_producer_wire(malformed)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator, unused-ignore]
    "reason",
    [
        "C:users:alice:media",
        "localhost:8080",
        "file:private-media",
        "s3:private-bucket",
        "https://private.example/media?token=x",
        "/private/media",
    ],
)
def test_reason_is_a_closed_module_owned_code_not_caller_text(reason: str) -> None:
    (result,) = H3MediaAdmissionProducerNode().admit(
        media_kind="image",
        asset_id="image-1",
        admission_evidence=_image_evidence(),
    )
    with pytest.raises(ContractValidationError):
        replace(result, reason_code=cast(ProducerReasonCode, reason))


def test_admission_evidence_enforces_media_specific_metadata_and_reference_bounds() -> None:
    media_invalid = (
        replace(_image_evidence(), width_pixels=None),
        replace(_image_evidence(), duration_seconds=1.0),
    )
    for evidence in media_invalid:
        with pytest.raises(ContractValidationError):
            H3MediaAdmissionProducerNode().admit(
                media_kind="image",
                asset_id="image-1",
                admission_evidence=evidence,
                image=object(),
            )
    with pytest.raises(ContractValidationError):
        replace(_image_evidence(), reference_order=64)
    with pytest.raises(ContractValidationError):
        replace(_image_evidence(), reference_role="private:path")
