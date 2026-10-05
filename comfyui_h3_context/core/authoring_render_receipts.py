"""Content-free output facts and independently recomputed Authoring receipt joins.

The adapter must obtain observations from the complete private artifact using the pinned probe
and a fresh bounded content hash. A pure value here is neither executable qualification nor proof
that a file exists. The verifier accepts observations separately; it never trusts the receipt's
copy of those observations as its measurement.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields
from typing import Final

from .authoring_render_jobs import (
    AuthoringRenderJobRequestV1,
    RenderJobError,
    RenderJobLimits,
    require_request_matches_plan,
    semantic_render_fingerprint,
)
from .canonical import canonical_bytes, canonical_fingerprint
from .composition_contract import OUTPUT_PROFILE_ID
from .render_planner import (
    RenderPlannerError,
    RenderPlanV1,
    SourceCurrentnessConfirmation,
    revalidate_render_plan_currentness,
)

RENDER_RECEIPT_SCHEMA: Final = "h3.authoring.render_receipt.v1"
RENDER_OUTPUT_FACTS_SCHEMA: Final = "h3.authoring.render_output_facts.v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_]{0,31}\Z")


class RenderReceiptError(ValueError):
    def __init__(self, code: str) -> None:
        if code not in {
            "invalid_receipt",
            "invalid_output",
            "output_mismatch",
            "receipt_mismatch",
            "currentness_mismatch",
        }:
            code = "invalid_receipt"
        self.code = code
        super().__init__(code)


def _fingerprint(value: object) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def render_video_timing_fingerprint(
    pts: tuple[int, ...], dts: tuple[int, ...], duration_ticks: tuple[int, ...]
) -> str:
    if any(type(values) is not tuple for values in (pts, dts, duration_ticks)):
        raise RenderReceiptError("invalid_output")
    if not 1 <= len(pts) <= 3600 or len(dts) != len(pts) or len(duration_ticks) != len(pts):
        raise RenderReceiptError("invalid_output")
    if any(
        type(value) is not int or not -3600 <= value <= 3600
        for values in (pts, dts, duration_ticks)
        for value in values
    ):
        raise RenderReceiptError("invalid_output")
    # IMPORTANT: chunk the full sequence, not a sample or first/last summary. Flat3600item
    # arrays violate the global256item canonical guard; weakening that guard is not required.
    return canonical_fingerprint(
        {
            "schema": "h3.authoring.render_video_timing.v1",
            "count": len(pts),
            "chunks": [
                canonical_fingerprint(
                    {
                        "pts": pts[start : start + 256],
                        "dts": dts[start : start + 256],
                        "duration_ticks": duration_ticks[start : start + 256],
                    }
                )
                for start in range(0, len(pts), 256)
            ],
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MeasuredRenderOutput:
    output_fingerprint: str
    byte_length: int
    container: str
    video_codec: str
    video_streams: int
    other_streams: int
    width: int
    height: int
    frame_count: int
    frame_rate_num: int
    frame_rate_den: int
    time_base_num: int
    time_base_den: int
    pixel_format: str
    pixel_aspect_num: int
    pixel_aspect_den: int
    color_range: str
    color_space: str
    color_primaries: str
    color_transfer: str
    video_timing_fingerprint: str
    audio_streams: int
    audio_codec: str | None
    audio_sample_rate: int | None
    audio_channels: int | None
    audio_effective_samples: int | None

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name.endswith("fingerprint"):
                valid = _fingerprint(value)
            elif field.name in {
                "container",
                "video_codec",
                "pixel_format",
                "color_range",
                "color_space",
                "color_primaries",
                "color_transfer",
                "audio_codec",
            }:
                valid = (value is None and field.name == "audio_codec") or (
                    type(value) is str and _TOKEN.fullmatch(value) is not None
                )
            elif field.name in {"audio_sample_rate", "audio_channels", "audio_effective_samples"}:
                valid = value is None or (type(value) is int and 0 <= value <= 1_000_000_000)
            else:
                valid = type(value) is int and 0 <= value <= 1_000_000_000
            if not valid:
                raise RenderReceiptError("invalid_output")
        if self.byte_length == 0:
            raise RenderReceiptError("invalid_output")

    def to_wire(self) -> dict[str, object]:
        return {"schema": RENDER_OUTPUT_FACTS_SCHEMA, **asdict(self)}


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthoringRenderReceiptV1:
    schema: str
    request: AuthoringRenderJobRequestV1
    semantic_fingerprint: str
    renderer_artifact_fingerprint: str
    probe_artifact_fingerprint: str
    qualification_fingerprint: str
    publication_currentness_fingerprint: str
    observed: MeasuredRenderOutput

    def __post_init__(self) -> None:
        if (
            type(self.schema) is not str
            or self.schema != RENDER_RECEIPT_SCHEMA
            or type(self.request) is not AuthoringRenderJobRequestV1
            or type(self.observed) is not MeasuredRenderOutput
        ):
            raise RenderReceiptError("invalid_receipt")
        for field in fields(self):
            if field.name.endswith("fingerprint") and not _fingerprint(getattr(self, field.name)):
                raise RenderReceiptError("invalid_receipt")
        if len(canonical_bytes(self.to_wire())) > RenderJobLimits().max_receipt_bytes:
            raise RenderReceiptError("invalid_receipt")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request": self.request.to_wire(),
            "semantic_fingerprint": self.semantic_fingerprint,
            "renderer_artifact_fingerprint": self.renderer_artifact_fingerprint,
            "probe_artifact_fingerprint": self.probe_artifact_fingerprint,
            "qualification_fingerprint": self.qualification_fingerprint,
            "publication_currentness_fingerprint": self.publication_currentness_fingerprint,
            "observed": self.observed.to_wire(),
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def validate_render_output(plan: RenderPlanV1, observed: MeasuredRenderOutput) -> None:
    if type(plan) is not RenderPlanV1 or type(observed) is not MeasuredRenderOutput:
        raise RenderReceiptError("invalid_output")
    output = plan.output_profile
    if not 1 <= output.duration_frames <= 3600:
        raise RenderReceiptError("output_mismatch")
    # CRITICAL: inspect all streams and the entire timing sequence. A first-frame/fps check
    # accepts a truncated or reordered output, and a first-stream check hides overlay audio.
    expected = {
        "container": output.container,
        "video_codec": output.video_codec,
        "video_streams": 1,
        "other_streams": 0,
        "width": output.width,
        "height": output.height,
        "frame_count": output.duration_frames,
        "frame_rate_num": output.frame_rate.num,
        "frame_rate_den": output.frame_rate.den,
        "time_base_num": output.time_base.num,
        "time_base_den": output.time_base.den,
        "pixel_format": output.pixel_format,
        "pixel_aspect_num": output.pixel_aspect.num,
        "pixel_aspect_den": output.pixel_aspect.den,
        "color_range": "tv",
        "color_space": "bt709",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "video_timing_fingerprint": render_video_timing_fingerprint(
            tuple(range(output.duration_frames)),
            tuple(range(output.duration_frames)),
            (1,) * output.duration_frames,
        ),
        "audio_streams": int(plan.emits_audio_stream),
        "audio_codec": output.audio_codec if plan.emits_audio_stream else None,
        "audio_sample_rate": output.sample_rate if plan.emits_audio_stream else None,
        "audio_channels": output.channels if plan.emits_audio_stream else None,
        "audio_effective_samples": output.duration_frames * 2000
        if plan.emits_audio_stream
        else None,
    }
    if (
        output.profile_id != OUTPUT_PROFILE_ID
        or output.container != "mp4"
        or output.video_codec != "h264"
        or output.pixel_format != "yuv420p"
        or output.audio_codec != "aac"
        or output.sample_rate != 48000
        or output.channels != 1
        or output.pixel_aspect.num != 1
        or output.pixel_aspect.den != 1
        or not 2 <= output.width <= 1920
        or not 2 <= output.height <= 1080
        or output.width % 2
        or output.height % 2
        # IMPORTANT: silence is derived from resolved owner spans, never a replaceable flag.
        # Otherwise a matching forged observation can certify dropped primary audio.
        or type(plan.emits_audio_stream) is not bool
        or plan.emits_audio_stream
        != any(
            frame.audio_span is not None
            for chunk in plan.resolved_operations
            for frame in chunk.frames
        )
        or output.frame_rate.num != 24
        or output.frame_rate.den != 1
        or output.time_base.num != 1
        or output.time_base.den != 24
        or output.color_policy != "bt709_sdr_limited_v1"
        or output.audio_policy != "primary_embedded_follow_video_v1"
        or observed.byte_length > RenderJobLimits().max_output_bytes
        or any(getattr(observed, name) != value for name, value in expected.items())
    ):
        raise RenderReceiptError("output_mismatch")


def _validate_publication_currentness(
    plan: RenderPlanV1, publication_currentness: SourceCurrentnessConfirmation
) -> None:
    try:
        revalidate_render_plan_currentness(plan=plan, confirmation=publication_currentness)
    except (RenderPlannerError, TypeError, ValueError):
        raise RenderReceiptError("currentness_mismatch") from None


def make_render_receipt(
    *,
    request: AuthoringRenderJobRequestV1,
    plan: RenderPlanV1,
    observed: MeasuredRenderOutput,
    renderer_artifact_fingerprint: str,
    probe_artifact_fingerprint: str,
    qualification_fingerprint: str,
    publication_currentness: SourceCurrentnessConfirmation,
) -> AuthoringRenderReceiptV1:
    try:
        require_request_matches_plan(request, plan)
    except RenderJobError:
        raise RenderReceiptError("receipt_mismatch") from None
    validate_render_output(plan, observed)
    _validate_publication_currentness(plan, publication_currentness)
    return AuthoringRenderReceiptV1(
        schema=RENDER_RECEIPT_SCHEMA,
        request=request,
        semantic_fingerprint=semantic_render_fingerprint(plan),
        renderer_artifact_fingerprint=renderer_artifact_fingerprint,
        probe_artifact_fingerprint=probe_artifact_fingerprint,
        qualification_fingerprint=qualification_fingerprint,
        publication_currentness_fingerprint=canonical_fingerprint(
            publication_currentness.to_wire()
        ),
        observed=observed,
    )


def verify_render_receipt(
    receipt: AuthoringRenderReceiptV1,
    *,
    request: AuthoringRenderJobRequestV1,
    plan: RenderPlanV1,
    observed: MeasuredRenderOutput,
    renderer_artifact_fingerprint: str,
    probe_artifact_fingerprint: str,
    qualification_fingerprint: str,
    publication_currentness: SourceCurrentnessConfirmation,
) -> None:
    """Verify using independently supplied artifact facts and execution authorities.

    This does not call the receipt issuer or infer observation from the receipt. The adapter
    must additionally bind the request to its live service entry and these facts to actual bytes.
    A later edit is irrelevant: verification uses the retained immutable plan, not current UI state.
    """

    if type(receipt) is not AuthoringRenderReceiptV1:
        raise RenderReceiptError("invalid_receipt")
    if receipt.request != request:
        raise RenderReceiptError("receipt_mismatch")
    try:
        require_request_matches_plan(request, plan)
    except RenderJobError:
        raise RenderReceiptError("receipt_mismatch") from None
    validate_render_output(plan, observed)
    _validate_publication_currentness(plan, publication_currentness)
    if (
        receipt.request != request
        or receipt.observed != observed
        or receipt.semantic_fingerprint != semantic_render_fingerprint(plan)
        or receipt.renderer_artifact_fingerprint != renderer_artifact_fingerprint
        or receipt.probe_artifact_fingerprint != probe_artifact_fingerprint
        or receipt.qualification_fingerprint != qualification_fingerprint
        or receipt.publication_currentness_fingerprint
        != canonical_fingerprint(publication_currentness.to_wire())
    ):
        raise RenderReceiptError("receipt_mismatch")
