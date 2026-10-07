from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest
from test_m25_authoring_render_jobs import _plan
from test_m25_render_planner import (
    _currentness,
    _font_facts,
    _snapshot_wire,
    _source_manifest,
)

from comfyui_h3_context.core.authoring_render_jobs import request_for_render_plan
from comfyui_h3_context.core.authoring_render_receipts import (
    AuthoringRenderReceiptV1,
    MeasuredRenderOutput,
    RenderReceiptError,
    make_render_receipt,
    render_video_timing_fingerprint,
    validate_render_output,
    verify_render_receipt,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.composition_contract import (
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.render_planner import RenderPlanV1, plan_render


def _facts(plan: RenderPlanV1) -> MeasuredRenderOutput:
    output = plan.output_profile
    # These are unit-test observations, never runtime qualification evidence.
    return MeasuredRenderOutput(
        output_fingerprint=canonical_fingerprint({"synthetic-output": 1}),
        byte_length=100_000,
        container="mp4",
        video_codec="h264",
        video_streams=1,
        other_streams=0,
        width=output.width,
        height=output.height,
        frame_count=output.duration_frames,
        frame_rate_num=24,
        frame_rate_den=1,
        time_base_num=1,
        time_base_den=24,
        pixel_format="yuv420p",
        pixel_aspect_num=1,
        pixel_aspect_den=1,
        color_range="tv",
        color_space="bt709",
        color_primaries="bt709",
        color_transfer="bt709",
        video_timing_fingerprint=render_video_timing_fingerprint(
            tuple(range(output.duration_frames)),
            tuple(range(output.duration_frames)),
            (1,) * output.duration_frames,
        ),
        audio_streams=int(plan.emits_audio_stream),
        audio_codec="aac" if plan.emits_audio_stream else None,
        audio_sample_rate=48000 if plan.emits_audio_stream else None,
        audio_channels=1 if plan.emits_audio_stream else None,
        audio_effective_samples=output.duration_frames * 2000 if plan.emits_audio_stream else None,
    )


def test_full_extent_receipt_timing_join_includes_last_frame() -> None:
    from test_m25_authoring_render_jobs import long_plan

    plan = long_plan()
    facts = _facts(plan)
    validate_render_output(plan, facts)
    positions = tuple(range(3600))
    changed = render_video_timing_fingerprint(positions[:-1] + (3598,), positions, (1,) * 3600)
    assert changed != facts.video_timing_fingerprint
    with pytest.raises(RenderReceiptError, match="output_mismatch"):
        validate_render_output(plan, replace(facts, video_timing_fingerprint=changed))


def _receipt(plan: RenderPlanV1 | None = None) -> AuthoringRenderReceiptV1:
    subject = _plan() if plan is None else plan
    return make_render_receipt(
        request=request_for_render_plan(
            subject, idempotency_key="request-00000001", timeout_ms=30_000
        ),
        plan=subject,
        observed=_facts(subject),
        renderer_artifact_fingerprint=canonical_fingerprint({"unit-renderer": 1}),
        probe_artifact_fingerprint=canonical_fingerprint({"unit-probe": 1}),
        qualification_fingerprint=canonical_fingerprint({"unit-only-not-qualified": 1}),
        publication_currentness=subject.source_currentness_claim,
    )


def _verify(
    receipt: AuthoringRenderReceiptV1,
    observed: MeasuredRenderOutput | None = None,
    plan: RenderPlanV1 | None = None,
) -> None:
    subject = _plan() if plan is None else plan
    verify_render_receipt(
        receipt,
        request=request_for_render_plan(
            subject, idempotency_key="request-00000001", timeout_ms=30_000
        ),
        plan=subject,
        observed=_facts(subject) if observed is None else observed,
        renderer_artifact_fingerprint=canonical_fingerprint({"unit-renderer": 1}),
        probe_artifact_fingerprint=canonical_fingerprint({"unit-probe": 1}),
        qualification_fingerprint=canonical_fingerprint({"unit-only-not-qualified": 1}),
        publication_currentness=subject.source_currentness_claim,
    )


def test_receipt_tooling_schema_preserves_version_and_closed_nested_fields() -> None:
    from jsonschema import Draft202012Validator

    from scripts.authoring_render_schemas import render_wire_schemas

    validator = Draft202012Validator(
        render_wire_schemas()["authoring_render_receipt_v1.schema.json"]
    )
    receipt = _receipt()
    wire = receipt.to_wire()
    validator.validate(wire)
    for field in ("request", "observed"):
        nested = wire[field]
        assert isinstance(nested, dict)
        mutated = {**wire, field: {**nested, "private_path": "not-admitted"}}
        assert not validator.is_valid(mutated)
    for version in ("h3.authoring.render_receipt.v0", "h3.authoring.render_receipt.v2"):
        assert not validator.is_valid({**wire, "schema": version})
        with pytest.raises(RenderReceiptError, match="invalid_receipt"):
            replace(receipt, schema=version)
    assert receipt.to_wire() == wire


def test_receipt_recomputes_joins_and_matches_separately_supplied_observation() -> None:
    receipt = _receipt()
    _verify(receipt)
    assert receipt.fingerprint == canonical_fingerprint(receipt.to_wire())
    assert receipt.observed.audio_streams == 1
    assert receipt.request.timeline_revision == _plan().snapshot_revision


@pytest.mark.parametrize(
    "field,value",
    [
        ("video_streams", 2),
        ("other_streams", 1),
        ("width", 640),
        ("height", 480),
        ("frame_count", 1),
        ("frame_rate_num", 25),
        ("time_base_den", 12288),
        ("pixel_aspect_num", 2),
        ("pixel_format", "yuv444p"),
        ("color_range", "pc"),
        ("color_space", "bt2020nc"),
        ("color_primaries", "bt2020"),
        ("color_transfer", "smpte2084"),
        ("video_codec", "hevc"),
        ("container", "matroska"),
        ("audio_streams", 2),
        ("audio_codec", "pcm_s16le"),
        ("audio_sample_rate", 44100),
        ("audio_channels", 2),
        ("audio_effective_samples", 1),
    ],
)
def test_self_consistent_wrong_output_facts_cannot_issue_receipt(field: str, value: object) -> None:
    subject = _plan()
    with pytest.raises(RenderReceiptError, match="output_mismatch"):
        make_render_receipt(
            request=request_for_render_plan(
                subject, idempotency_key="request-00000001", timeout_ms=30_000
            ),
            plan=subject,
            observed=replace(_facts(subject), **cast(dict[str, Any], {field: value})),
            renderer_artifact_fingerprint=canonical_fingerprint({"renderer": 1}),
            probe_artifact_fingerprint=canonical_fingerprint({"probe": 1}),
            qualification_fingerprint=canonical_fingerprint({"qualification": 1}),
            publication_currentness=subject.source_currentness_claim,
        )


@pytest.mark.parametrize(
    "field",
    [
        "renderer_artifact_fingerprint",
        "probe_artifact_fingerprint",
        "qualification_fingerprint",
        "semantic_fingerprint",
        "publication_currentness_fingerprint",
    ],
)
def test_receipt_runtime_and_publication_joins_are_not_self_asserted(field: str) -> None:
    forged = replace(
        _receipt(), **cast(dict[str, Any], {field: canonical_fingerprint({"foreign": field})})
    )
    with pytest.raises(RenderReceiptError, match="receipt_mismatch"):
        _verify(forged)


def test_changed_output_bytes_or_timestamp_sequence_invalidates_receipt() -> None:
    receipt = _receipt()
    for field in ("output_fingerprint", "video_timing_fingerprint"):
        observed = replace(
            receipt.observed,
            **cast(dict[str, Any], {field: canonical_fingerprint({"changed": field})}),
        )
        with pytest.raises(RenderReceiptError):
            _verify(receipt, observed)


def test_no_primary_audio_has_zero_streams_not_an_empty_aac_stream() -> None:
    wire = _snapshot_wire()
    for asset in wire["assets"]:
        if asset["embedded_audio"] == "present_bound":
            asset["embedded_audio"] = "absent"
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    snapshot = decode_public_snapshot(wire)
    manifest = _source_manifest(snapshot)
    plan = plan_render(
        snapshot=snapshot,
        source_manifest=manifest,
        currentness=_currentness(manifest),
        font_facts=_font_facts(snapshot),
    )
    assert not plan.emits_audio_stream
    receipt = _receipt(plan)
    _verify(receipt, plan=plan)
    assert receipt.observed.audio_streams == 0
    with pytest.raises(RenderReceiptError):
        _verify(receipt, replace(receipt.observed, audio_codec="aac"), plan)


def test_silent_output_cannot_hide_resolved_primary_audio() -> None:
    plan = replace(_plan(), emits_audio_stream=False)
    assert any(
        frame.audio_span is not None for chunk in plan.resolved_operations for frame in chunk.frames
    )
    with pytest.raises(RenderReceiptError, match="output_mismatch"):
        _receipt(plan)


@pytest.mark.parametrize(
    ("field", "value", "observed_field"),
    [
        ("container", "matroska", "container"),
        ("video_codec", "hevc", "video_codec"),
        ("pixel_format", "yuv444p", "pixel_format"),
        ("audio_codec", "opus", "audio_codec"),
        ("sample_rate", 44100, "audio_sample_rate"),
        ("channels", 2, "audio_channels"),
    ],
)
def test_receipt_cannot_certify_a_forged_noncanonical_output_profile(
    field: str, value: Any, observed_field: str
) -> None:
    original = _plan()
    forged = replace(original, output_profile=replace(original.output_profile, **{field: value}))
    observed = replace(_facts(forged), **{observed_field: value})
    from comfyui_h3_context.core.authoring_render_receipts import validate_render_output

    with pytest.raises(RenderReceiptError, match="output_mismatch"):
        validate_render_output(forged, observed)


def test_immutable_old_revision_stays_verifiable_only_against_its_own_plan() -> None:
    old = _plan()
    receipt = _receipt(old)
    newer = replace(old, snapshot_revision=old.snapshot_revision + 1)
    _verify(receipt, plan=old)
    with pytest.raises(RenderReceiptError, match="receipt_mismatch"):
        _verify(receipt, plan=newer)


def test_replaced_publication_currentness_cannot_borrow_the_plan_receipt() -> None:
    subject = _plan()
    stale = replace(subject.source_currentness_claim, generation=999)
    with pytest.raises(RenderReceiptError, match="currentness_mismatch"):
        make_render_receipt(
            request=_receipt().request,
            plan=subject,
            observed=_facts(subject),
            renderer_artifact_fingerprint=canonical_fingerprint({"renderer": 1}),
            probe_artifact_fingerprint=canonical_fingerprint({"probe": 1}),
            qualification_fingerprint=canonical_fingerprint({"qualification": 1}),
            publication_currentness=stale,
        )
