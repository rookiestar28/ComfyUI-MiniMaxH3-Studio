"""Operational capability must agree with registered-node preflight without side effects."""

from __future__ import annotations

from typing import cast
from unittest.mock import Mock

import pytest

from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.official_context_ir import OfficialContextIRTransport
from comfyui_h3_context.core.perception_producer import (
    MediaAdmissionEvidence,
    ProducerDisposition,
    ProducerRoute,
)
from comfyui_h3_context.nodes import (
    NODE_CLASS_MAPPINGS,
    H3AudioPerceptionProducerNode,
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3FullReferenceTimelineProducerNode,
    H3MediaAdmissionProducerNode,
    H3OfficialContextIRNode,
    H3ReferenceRegistryNode,
    H3VisualPerceptionProducerNode,
)


@pytest.mark.parametrize(
    "node_type",
    [
        H3VisualPerceptionProducerNode,
        H3AudioPerceptionProducerNode,
        H3FullReferenceTimelineProducerNode,
        H3OfficialContextIRNode,
    ],
)
def test_registered_nodes_refuse_unavailable_default_before_execution(
    node_type: type[
        H3VisualPerceptionProducerNode
        | H3AudioPerceptionProducerNode
        | H3FullReferenceTimelineProducerNode
        | H3OfficialContextIRNode
    ],
) -> None:
    assert NODE_CLASS_MAPPINGS[node_type.NODE_ID] is node_type
    node = node_type()
    row = node.capability()
    assert row.node_id == node_type.NODE_ID
    assert row.ready is False
    assert row.remediation
    assert node_type.VALIDATE_INPUTS() == row.validation_result()
    assert node_type.DESCRIPTION


@pytest.mark.parametrize(
    "node_type", [H3VisualPerceptionProducerNode, H3AudioPerceptionProducerNode]
)
@pytest.mark.parametrize(
    "route", [ProducerRoute.COMFYUI_NATIVE, ProducerRoute.OLLAMA, ProducerRoute.SPECIALIST]
)
def test_profile_device_route_change_cannot_invent_qualification(
    node_type: type[H3VisualPerceptionProducerNode | H3AudioPerceptionProducerNode],
    route: ProducerRoute,
) -> None:
    audio = node_type is H3AudioPerceptionProducerNode
    media = H3MediaAdmissionProducerNode().admit(
        "audio" if audio else "image",
        "sample",
        admission_evidence=MediaAdmissionEvidence(
            declared_source_fingerprint="a" * 64,
            width_pixels=None if audio else 16,
            height_pixels=None if audio else 16,
            duration_seconds=1.0 if audio else 0.0,
            sample_rate_hz=8000 if audio else None,
            channel_count=1 if audio else None,
            reference_role="reference",
            reference_order=0,
        ),
        image=None if audio else object(),
        audio=object() if audio else None,
    )[0]
    for profile, device in (("unqualified", "auto"), ("private-profile-marker", "cpu")):
        row = node_type.capability(cancel_requested=False)
        assert row.ready is False
        assert "private-profile-marker" not in str(row.to_wire())
        assert node_type.VALIDATE_INPUTS(cancel_requested=False) is not True
        result = node_type().produce(media, route, profile, device, False)[0]
        assert result.disposition is ProducerDisposition.UNAVAILABLE
        assert result.reason_code.value == row.reason_code.value
        assert result.receipt.executed is False
    cancelled = node_type().produce(media, route, cancel_requested=True)[0]
    assert cancelled.disposition is ProducerDisposition.CANCELLED
    assert node_type.capability(cancel_requested=True).status.value == "cancelled"
    assert node_type.VALIDATE_INPUTS(cancel_requested=True) is True


def test_transport_configuration_is_observed_without_credentials_or_provider_call() -> None:
    resolver, transport = Mock(), Mock()
    node = H3OfficialContextIRNode(resolver=resolver, transport=transport)
    assert node.capability().ready is True
    resolver.assert_not_called()
    transport.assert_not_called()
    assert resolver.method_calls == transport.method_calls == []
    # Class validation reflects actual default composition, not an unrelated injected instance.
    assert H3OfficialContextIRNode.VALIDATE_INPUTS() is not True
    assert H3OfficialContextIRNode().capability().ready is False
    invalid = H3OfficialContextIRNode(transport=cast(OfficialContextIRTransport, object()))
    assert invalid.capability().ready is False


def test_manual_ref2va_plan_compiler_remains_available() -> None:
    registry = H3ReferenceRegistryNode().build_registry(images=[object()])[0]
    request = H3ContextRequestNode().build_request(
        TaskMode.REF2VA, "Keep the subject.", duration_seconds=5.0
    )[0]
    assert H3ContextPlanNode.VALIDATE_INPUTS(None) is True
    plan, _ = H3ContextPlanNode().build_plan(request, registry)
    assert H3ContextCompilerNode.VALIDATE_INPUTS(None) is True
    prompt, report, document = H3ContextCompilerNode().compile(plan)
    assert prompt == document.text
    assert prompt
    assert report.prompt_document == document
