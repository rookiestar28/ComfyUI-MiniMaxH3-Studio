"""Node adapters that build a request and own its references.

The entry point of the pipeline.  The request node turns what a user asked for into a normalized
context request; the reference nodes own asset identity, which is the thing that must stay stable
across every later stage.  The reference helpers live here because the registry node is their only
caller.
"""

from __future__ import annotations

from typing import cast

from .adapters.authoring_source_binding import (
    AuthoringSourceBindingError,
    capture_process_authoring_sources,
)
from .core.canonical_context_pipeline import (
    CanonicalContextPipelineError,
    build_canonical_context_request,
)
from .core.constraints import HardConstraintSet
from .core.context_reporting import (
    ContextPlan,
    ContextReport,
)
from .core.contracts import (
    AssetRole,
    MediaKind,
    PromptProfile,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .core.errors import (
    ContextReportError,
)
from .core.full_reference_timeline import FullReferenceTimelineResult
from .core.length import MAX_ACCEPTED_MILLISECONDS, MILLISECONDS_PER_SECOND
from .core.normalization import RawContextRequest
from .core.registry import (
    MAX_PAIRED_VIDEO_AUDIO,
    MAX_REFERENCE_IMAGES,
    MAX_REFERENCE_VIDEOS,
    MAX_STANDALONE_AUDIO,
    ReferenceAsset,
    ReferenceRegistry,
    ReferenceRegistryError,
    build_reference_registry,
)
from .node_support import (
    FULL_REFERENCE_TIMELINE_SOCKET_TYPE,
    PLAN_SOCKET_TYPE,
    REFERENCE_SOCKET_TYPE,
    REQUEST_SOCKET_TYPE,
    PipelineNodeError,
    ReferenceRegistryNodeError,
    RequestNodeError,
    _draft_prompt_document,
    _not_run_validation,
    _pipeline_error,
    _pipeline_report,
)
from .product_shell_node import (
    REPORT_SOCKET_TYPE,
)

REQUEST_NODE_ID = "comfyui_h3_context.H3Context.Request"

REQUEST_DISPLAY_NAME = "H3 Context Request"

TASK_MODE_CHOICES = tuple(mode.value for mode in TaskMode)

REFERENCE_NODE_ID = "comfyui_h3_context.H3Context.ReferenceRegistry"

REFERENCE_DISPLAY_NAME = "H3 Reference Registry"

FULL_REFERENCE_NODE_ID = "comfyui_h3_context.H3Context.FullReference"

FULL_REFERENCE_DISPLAY_NAME = "H3 Full-Reference Plan"


class H3ContextRequestNode:
    """Map visible request controls to an immutable ``RawContextRequest`` envelope."""

    NODE_ID = REQUEST_NODE_ID
    __h3_context_node_id__ = REQUEST_NODE_ID
    RETURN_TYPES = (REQUEST_SOCKET_TYPE,)
    RETURN_NAMES = ("request",)
    FUNCTION = "build_request"
    CATEGORY = "h3_context/contracts"
    DESCRIPTION = (
        "Builds a typed H3 request envelope. Reference binding and prompt compilation are later "
        "nodes."
    )
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        """Expose the frozen M3-01 socket surface with host-neutral V1 metadata."""

        return {
            "required": {
                "task_mode": (
                    "COMBO",
                    {
                        "default": TaskMode.T2VA.value,
                        "options": list(TASK_MODE_CHOICES),
                        "tooltip": "Explicit H3 task mode.",
                    },
                ),
                "user_intent": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": True,
                        "dynamicPrompts": False,
                        "tooltip": "User-declared intent; exact hard text is preserved.",
                    },
                ),
            },
            "optional": {
                "duration_seconds": (
                    "FLOAT",
                    {
                        # `MAX_FRAME_COUNT / FPS` is 150.0 s and was the bound here
                        # until M17-25 distinct review: it admits about 313 ms of
                        # durations that always fail, because 3593..3600 frames sit
                        # inside the host range yet align above it. The bound is now
                        # the largest duration that actually resolves. Zero is kept
                        # as the low end because it is how this optional control
                        # reads as unset; it is rejected by validation, not offered.
                        "min": 0.0,
                        "max": MAX_ACCEPTED_MILLISECONDS / MILLISECONDS_PER_SECOND,
                        "step": 0.01,
                        "tooltip": (
                            "Optional expected duration in seconds; the H3 frame count is "
                            "derived and snapped to the 17k+5 lattice."
                        ),
                    },
                ),
                "hard_constraints": (
                    "H3_HARD_CONSTRAINTS",
                    {
                        "tooltip": "Optional immutable hard-constraint set from a typed node.",
                    },
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(
        cls,
        task_mode: TaskMode | str | None = None,
        user_intent: str | None = None,
        duration_seconds: int | float | None = None,
        hard_constraints: HardConstraintSet | None = None,
    ) -> bool | str:
        """Return a host-readable error string without producing a fallback request."""

        try:
            cls().build_request(
                cast(TaskMode | str, task_mode),
                cast(str, user_intent),
                duration_seconds,
                hard_constraints,
            )
        except RequestNodeError as exc:
            return str(exc)
        return True

    def build_request(
        self,
        task_mode: TaskMode | str,
        user_intent: str,
        duration_seconds: int | float | None = None,
        hard_constraints: HardConstraintSet | None = None,
    ) -> tuple[RawContextRequest]:
        """Validate controls and return the unchanged typed request envelope."""

        try:
            request = build_canonical_context_request(
                task_mode,
                user_intent,
                duration_seconds,
                hard_constraints,
            )
        except CanonicalContextPipelineError as exc:
            raise RequestNodeError(exc.diagnostics) from exc
        return (request,)


def _reference_error(code: str, message: str) -> ReferenceRegistryNodeError:
    return ReferenceRegistryNodeError(
        (ValidationDiagnostic(ValidationSeverity.ERROR, code, message),)
    )


def _expand_reference_input(value: object, field: str, maximum: int) -> tuple[object, ...]:
    """Expand only explicit list/tuple containers; opaque media values are never iterated."""

    if value is None:
        return ()
    values = tuple(value) if isinstance(value, (list, tuple)) else (value,)
    if len(values) > maximum:
        code = {
            "first_frame": "too_many_first_frames",
            "last_frame": "too_many_last_frames",
            "images": "too_many_images",
            "videos": "too_many_videos",
            "paired_audios": "too_many_paired_audio",
            "audios": "too_many_audio",
        }[field]
        raise _reference_error(code, f"{field} accepts at most {maximum} items")
    for item in values:
        if item is None:
            raise _reference_error(
                "missing_reference_media",
                f"{field} contains a missing media item; remove the empty connection",
            )
        if isinstance(item, (list, tuple)):
            raise _reference_error(
                "ambiguous_nested_reference_input",
                f"{field} must contain media items, not nested list or tuple values",
            )
    return values


def _is_unresolved_reference_placeholder(value: object) -> bool:
    """Recognize only ComfyUI's pre-validation sentinel for an unresolved list input."""

    return type(value) is tuple and value == (None,)


def _build_reference_assets(
    values: tuple[object, ...],
    *,
    prefix: str,
    kind: MediaKind,
    role: AssetRole,
    connection_start: int,
) -> tuple[ReferenceAsset, ...]:
    """Create metadata-free canonical slot assets without retaining the opaque media values."""

    return tuple(
        ReferenceAsset(
            asset_id=f"{prefix}_{index}",
            kind=kind,
            role=role,
            connection_order=connection_start + index - 1,
        )
        for index in range(1, len(values) + 1)
    )


class H3ReferenceRegistryNode:
    """Map grouped V1 media sockets to the immutable pure-core reference registry."""

    NODE_ID = REFERENCE_NODE_ID
    __h3_context_node_id__ = REFERENCE_NODE_ID
    RETURN_TYPES = (REFERENCE_SOCKET_TYPE,)
    RETURN_NAMES = ("reference_registry",)
    FUNCTION = "build_registry"
    CATEGORY = "h3_context/contracts"
    DESCRIPTION = (
        "Assigns deterministic reference slots and explicit frame/pair roles while preserving "
        "the order of grouped media."
    )
    INPUT_IS_LIST = True
    OUTPUT_NODE = False

    @classmethod
    def IS_CHANGED(cls, **_: object) -> float:
        # CRITICAL: execution also captures a one-time process-local source receipt. Caching only
        # the pure registry lets a rerun ProductShell lose duration/source authority.
        return float("nan")

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        """Expose grouped optional V1 sockets; list order is preserved by the adapter."""

        return {
            "required": {},
            "optional": {
                "first_frame": (
                    "IMAGE",
                    {
                        "max_items": 1,
                        "tooltip": "Optional explicit first-frame image anchor; at most one.",
                    },
                ),
                "last_frame": (
                    "IMAGE",
                    {
                        "max_items": 1,
                        "tooltip": "Optional explicit last-frame image anchor; at most one.",
                    },
                ),
                "images": (
                    "IMAGE",
                    {
                        "max_items": MAX_REFERENCE_IMAGES,
                        "tooltip": "Ordered image references; up to nine items.",
                    },
                ),
                "videos": (
                    "VIDEO",
                    {
                        "max_items": MAX_REFERENCE_VIDEOS,
                        "tooltip": "Ordered video references; up to three items.",
                    },
                ),
                "paired_audios": (
                    "AUDIO",
                    {
                        "max_items": MAX_PAIRED_VIDEO_AUDIO,
                        "tooltip": (
                            "Explicit paired soundtrack items; item N pairs only with video N "
                            "and must precede it in canonical order."
                        ),
                    },
                ),
                "audios": (
                    "AUDIO",
                    {
                        "max_items": MAX_STANDALONE_AUDIO,
                        "tooltip": "Ordered standalone audio references; up to three items.",
                    },
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(
        cls,
        first_frame: object | None = None,
        last_frame: object | None = None,
        images: object | None = None,
        videos: object | None = None,
        paired_audios: object | None = None,
        audios: object | None = None,
    ) -> bool | str:
        """Return a host-readable error without producing a fallback registry."""

        # ComfyUI validates this INPUT_IS_LIST node before linked media executes. In that phase
        # an unresolved link is represented as ``(None,)``; defer only that protocol sentinel.
        if any(
            _is_unresolved_reference_placeholder(value)
            for value in (first_frame, last_frame, images, videos, paired_audios, audios)
        ):
            return True
        try:
            cls()._build_registry(
                first_frame,
                last_frame,
                images,
                videos,
                paired_audios,
                audios,
                capture_runtime_sources=False,
            )
        except ReferenceRegistryNodeError as exc:
            return str(exc)
        return True

    def build_registry(
        self,
        first_frame: object | None = None,
        last_frame: object | None = None,
        images: object | None = None,
        videos: object | None = None,
        paired_audios: object | None = None,
        audios: object | None = None,
    ) -> tuple[ReferenceRegistry]:
        """Build roles in frame, reference, paired-video, then standalone-audio order."""

        return self._build_registry(
            first_frame,
            last_frame,
            images,
            videos,
            paired_audios,
            audios,
            capture_runtime_sources=True,
        )

    def _build_registry(
        self,
        first_frame: object | None,
        last_frame: object | None,
        images: object | None,
        videos: object | None,
        paired_audios: object | None,
        audios: object | None,
        *,
        capture_runtime_sources: bool,
    ) -> tuple[ReferenceRegistry]:
        """Build the registry, optionally capturing execution-only VIDEO authority."""

        first_values = _expand_reference_input(first_frame, "first_frame", 1)
        last_values = _expand_reference_input(last_frame, "last_frame", 1)
        image_values = _expand_reference_input(images, "images", MAX_REFERENCE_IMAGES)
        video_values = _expand_reference_input(videos, "videos", MAX_REFERENCE_VIDEOS)
        paired_audio_values = _expand_reference_input(
            paired_audios, "paired_audios", MAX_PAIRED_VIDEO_AUDIO
        )
        audio_values = _expand_reference_input(audios, "audios", MAX_STANDALONE_AUDIO)
        if len(paired_audio_values) > len(video_values):
            raise _reference_error(
                "paired_audio_without_video",
                "paired_audios item count cannot exceed the ordered videos item count",
            )
        connection_order = 1
        assets = (
            _build_reference_assets(
                first_values,
                prefix="first_frame",
                kind=MediaKind.IMAGE,
                role=AssetRole.FIRST_FRAME,
                connection_start=connection_order,
            )
            + _build_reference_assets(
                last_values,
                prefix="last_frame",
                kind=MediaKind.IMAGE,
                role=AssetRole.LAST_FRAME,
                connection_start=connection_order + len(first_values),
            )
            + _build_reference_assets(
                image_values,
                prefix="image",
                kind=MediaKind.IMAGE,
                role=AssetRole.REFERENCE,
                connection_start=connection_order + len(first_values) + len(last_values),
            )
        )
        connection_order += len(first_values) + len(last_values) + len(image_values)
        video_assets: list[ReferenceAsset] = []
        for index, _video in enumerate(video_values, start=1):
            if index <= len(paired_audio_values):
                video_assets.append(
                    ReferenceAsset(
                        asset_id=f"audio_pair_{index}",
                        kind=MediaKind.AUDIO,
                        role=AssetRole.AUDIO_SOURCE,
                        connection_order=connection_order,
                        paired_video_id=f"video_{index}",
                    )
                )
                connection_order += 1
            video_assets.append(
                ReferenceAsset(
                    asset_id=f"video_{index}",
                    kind=MediaKind.VIDEO,
                    role=AssetRole.REFERENCE,
                    connection_order=connection_order,
                )
            )
            connection_order += 1
        standalone_assets = _build_reference_assets(
            audio_values,
            prefix="audio",
            kind=MediaKind.AUDIO,
            role=AssetRole.AUDIO_SOURCE,
            connection_start=connection_order,
        )
        assets += tuple(video_assets) + standalone_assets
        try:
            registry = build_reference_registry(assets)
        except ReferenceRegistryError as exc:
            raise _reference_error("invalid_reference_registry", str(exc)) from exc
        if capture_runtime_sources:
            try:
                # IMPORTANT: capture belongs after successful construction only. Validation must
                # remain side-effect free, and an unavailable preview must not invalidate registry.
                capture_process_authoring_sources(
                    exact_registry=registry,
                    sources=tuple(
                        (f"video_{index}", MediaKind.VIDEO, value)
                        for index, value in enumerate(video_values, start=1)
                    )
                    + tuple(
                        (f"{prefix}_{index}", MediaKind.IMAGE, value)
                        for prefix, values in (
                            ("first_frame", first_values),
                            ("last_frame", last_values),
                            ("image", image_values),
                        )
                        for index, value in enumerate(values, start=1)
                    ),
                )
            except AuthoringSourceBindingError:
                pass
        return (registry,)


class H3ContextFullReferenceNode:
    """Hand an accepted M6 timeline result to the existing M2/M3 pipeline stages."""

    NODE_ID = FULL_REFERENCE_NODE_ID
    __h3_context_node_id__ = FULL_REFERENCE_NODE_ID
    RETURN_TYPES = (PLAN_SOCKET_TYPE, REPORT_SOCKET_TYPE)
    RETURN_NAMES = ("plan", "report")
    FUNCTION = "build_plan"
    CATEGORY = "h3_context/compiler"
    DESCRIPTION = (
        "Adopts a validated Full-Reference timeline plan; compilation, validation, and native "
        "media wiring remain explicit downstream nodes."
    )
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "timeline": (
                    FULL_REFERENCE_TIMELINE_SOCKET_TYPE,
                    {"tooltip": "Validated M6 Full-Reference timeline result."},
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, timeline: object) -> bool | str:
        # Linked custom values are unavailable during ComfyUI's pre-execution validation pass.
        if timeline is None:
            return True
        try:
            cls().build_plan(timeline)
        except PipelineNodeError as exc:
            return str(exc)
        return True

    def build_plan(self, timeline: object) -> tuple[ContextPlan, ContextReport]:
        if not isinstance(timeline, FullReferenceTimelineResult):
            raise _pipeline_error(
                "invalid_timeline", "timeline must be a FullReferenceTimelineResult"
            )
        if not timeline.is_valid or timeline.plan is None or timeline.timeline is None:
            diagnostics = (
                ValidationDiagnostic(
                    ValidationSeverity.ERROR,
                    "invalid_timeline",
                    "Full-Reference timeline did not produce a renderable plan",
                ),
            ) + timeline.diagnostics
            raise PipelineNodeError(diagnostics)
        plan = timeline.plan
        # M6-05 owns this plan; the adapter only verifies its profile boundary and creates the
        # same draft report shape used by H3ContextPlanNode before the compiler runs.
        if (
            plan.request.profile.name is not PromptProfile.FULL_REFERENCE
            or plan.request.task_mode is not TaskMode.REF2VA
        ):
            raise _pipeline_error(
                "invalid_timeline", "Full-Reference timeline plan has an incompatible profile"
            )
        diagnostics = tuple(timeline.diagnostics) + tuple(timeline.timeline.diagnostics)
        document = _draft_prompt_document(plan.request, plan.plan_id)
        try:
            report = _pipeline_report(
                plan,
                document,
                _not_run_validation(plan.schema_version, document.document_id),
                diagnostics=diagnostics,
            )
        except (ContextReportError, TypeError, ValueError) as exc:
            raise _pipeline_error(
                "timeline_report_failed", "Full-Reference timeline report construction failed"
            ) from exc
        return plan, report
