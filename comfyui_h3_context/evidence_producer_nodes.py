"""Node adapters that produce evidence: admitted media, perception, constraints and intent.

The perception producers degrade to a declared unavailable state rather than inventing an
observation, which is why the unavailable base class sits with them rather than in shared support.
"""

from __future__ import annotations

from typing import Any

from .core.constraints import DialogueDelivery, HardConstraintSet
from .core.cross_reference_graph import CrossReferenceGraph
from .core.dialogue_language import OFFICIAL_STABLE_DIALOGUE_LANGUAGES
from .core.downstream_producer import (
    DirectiveAuthorityBundle,
    DownstreamProducerReport,
    DownstreamStage,
    ManualKeyframeBinding,
    build_admission_evidence_graph,
    build_manual_directive_authority,
    build_manual_hard_constraints,
    build_manual_intent,
    build_source_cross_reference,
    issue_manual_component_report,
)
from .core.errors import (
    ContractValidationError,
)
from .core.intent_graph import IntentGraph, SegmentDevelopment
from .core.normalization import (
    RawContextRequest,
)
from .core.perception_producer import (
    MediaAdmissionEvidence,
    PerceptionProducerResult,
    ProducerKind,
    ProducerRoute,
    admit_host_media,
    declare_unqualified_perception,
)
from .core.registry import (
    ReferenceRegistry,
)
from .core.unified_evidence_graph import UnifiedEvidenceGraph
from .node_capability import NodeCapability, NodeCapabilityReason
from .node_support import (
    CROSS_REFERENCE_GRAPH_SOCKET_TYPE,
    DIRECTIVE_AUTHORITY_SOCKET_TYPE,
    DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE,
    INTENT_GRAPH_SOCKET_TYPE,
    MEDIA_PRODUCER_SOCKET_TYPE,
    REFERENCE_SOCKET_TYPE,
    REQUEST_SOCKET_TYPE,
    UNIFIED_EVIDENCE_GRAPH_SOCKET_TYPE,
)

MEDIA_ADMISSION_NODE_ID = "comfyui_h3_context.H3Context.MediaAdmissionProducer"

MEDIA_ADMISSION_DISPLAY_NAME = "H3 Media Admission Producer"

VISUAL_PERCEPTION_NODE_ID = "comfyui_h3_context.H3Context.VisualPerceptionProducer"

VISUAL_PERCEPTION_DISPLAY_NAME = "H3 Visual Perception Producer"

VISUAL_PRODUCER_SOCKET_TYPE = "H3_VISUAL_PRODUCER_RESULT"

AUDIO_PERCEPTION_NODE_ID = "comfyui_h3_context.H3Context.AudioPerceptionProducer"

AUDIO_PERCEPTION_DISPLAY_NAME = "H3 Audio Perception Producer"

AUDIO_PRODUCER_SOCKET_TYPE = "H3_AUDIO_PRODUCER_RESULT"

HARD_CONSTRAINT_PRODUCER_NODE_ID = "comfyui_h3_context.H3Context.HardConstraintProducer"

HARD_CONSTRAINT_PRODUCER_DISPLAY_NAME = "H3 Hard Constraint Producer"

INTENT_GRAPH_PRODUCER_NODE_ID = "comfyui_h3_context.H3Context.IntentGraphProducer"

INTENT_GRAPH_PRODUCER_DISPLAY_NAME = "H3 Intent Graph Producer"

EVIDENCE_FUSION_PRODUCER_NODE_ID = "comfyui_h3_context.H3Context.EvidenceFusionProducer"

EVIDENCE_FUSION_PRODUCER_DISPLAY_NAME = "H3 Evidence Fusion Producer"

CROSS_REFERENCE_PRODUCER_NODE_ID = "comfyui_h3_context.H3Context.CrossReferenceProducer"

CROSS_REFERENCE_PRODUCER_DISPLAY_NAME = "H3 Cross Reference Producer"

DIRECTIVE_AUTHORITY_PRODUCER_NODE_ID = "comfyui_h3_context.H3Context.DirectiveAuthorityProducer"

DIRECTIVE_AUTHORITY_PRODUCER_DISPLAY_NAME = "H3 Directive Authority Producer"

PERCEPTION_ROUTE_CHOICES = (
    ProducerRoute.COMFYUI_NATIVE.value,
    ProducerRoute.OLLAMA.value,
    ProducerRoute.SPECIALIST.value,
)


class H3MediaAdmissionProducerNode:
    """Admit one host-owned media value without serializing its private runtime payload."""

    NODE_ID = MEDIA_ADMISSION_NODE_ID
    __h3_context_node_id__ = MEDIA_ADMISSION_NODE_ID
    RETURN_TYPES = (MEDIA_PRODUCER_SOCKET_TYPE,)
    RETURN_NAMES = ("media",)
    FUNCTION = "admit"
    CATEGORY = "h3_context/perception"
    DESCRIPTION = "Admits one host-owned media value and emits a locator-free typed envelope."
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "media_kind": (
                    "COMBO",
                    {"default": "image", "options": ["image", "video", "audio"]},
                ),
                "asset_id": ("STRING", {"default": "asset-1"}),
                "declared_source_fingerprint": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Unverified caller-declared lowercase SHA-256; not trusted-loader "
                            "provenance."
                        ),
                    },
                ),
                "width_pixels": ("INT", {"default": 64, "min": 0, "max": 32768}),
                "height_pixels": ("INT", {"default": 64, "min": 0, "max": 32768}),
                "duration_seconds": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 86400.0}),
                "sample_rate_hz": ("INT", {"default": 0, "min": 0, "max": 768000}),
                "channel_count": ("INT", {"default": 0, "min": 0, "max": 64}),
                "reference_role": (
                    "COMBO",
                    {
                        "default": "reference",
                        "options": [
                            "input",
                            "reference",
                            "first_frame",
                            "last_frame",
                            "paired_audio",
                        ],
                    },
                ),
                "reference_order": ("INT", {"default": 0, "min": 0, "max": 63}),
            },
            "optional": {
                "image": ("IMAGE", {"tooltip": "Host-owned image value."}),
                "video": ("VIDEO", {"tooltip": "Host-owned video value."}),
                "audio": ("AUDIO", {"tooltip": "Host-owned audio value."}),
            },
        }

    def admit(
        self,
        media_kind: str,
        asset_id: str,
        declared_source_fingerprint: str = "",
        width_pixels: int = 64,
        height_pixels: int = 64,
        duration_seconds: float = 0.0,
        sample_rate_hz: int = 0,
        channel_count: int = 0,
        reference_role: str = "reference",
        reference_order: int = 0,
        image: object | None = None,
        video: object | None = None,
        audio: object | None = None,
        admission_evidence: MediaAdmissionEvidence | None = None,
    ) -> tuple[PerceptionProducerResult]:
        evidence = (
            admission_evidence
            if admission_evidence is not None
            else MediaAdmissionEvidence(
                declared_source_fingerprint=declared_source_fingerprint,
                width_pixels=width_pixels or None,
                height_pixels=height_pixels or None,
                duration_seconds=duration_seconds,
                sample_rate_hz=sample_rate_hz or None,
                channel_count=channel_count or None,
                reference_role=reference_role,
                reference_order=reference_order,
            )
        )
        return (
            admit_host_media(
                media_kind=media_kind,
                asset_id=asset_id,
                admission_evidence=evidence,
                image=image,
                video=video,
                audio=audio,
            ),
        )


class _UnavailablePerceptionProducerNode:
    """Preserve legacy refusal while exposing finite host-configured perception profiles."""

    KIND: ProducerKind
    NODE_ID: str
    RETURN_TYPES: tuple[str, ...]
    RETURN_NAMES = ("result",)
    FUNCTION = "produce"
    CATEGORY = "h3_context/perception"
    OUTPUT_NODE = False

    @classmethod
    def capability(
        cls, *, cancel_requested: bool = False, profile_id: str = "unqualified"
    ) -> NodeCapability:
        from .adapters.perception_host import perception_configured
        from .core.perception_execution import AUDIO_PROFILE, VISUAL_PROFILE

        expected = VISUAL_PROFILE if cls.KIND is ProducerKind.VISUAL else AUDIO_PROFILE
        reason = (
            NodeCapabilityReason.CANCELLED_BEFORE_EXECUTION
            if cancel_requested is True
            else (
                NodeCapabilityReason.PERCEPTION_CONFIGURED
                if perception_configured(profile_id)
                else NodeCapabilityReason.PERCEPTION_UNCONFIGURED
            )
            if profile_id == expected
            else NodeCapabilityReason.PROFILE_UNAVAILABLE
        )
        return NodeCapability(cls.NODE_ID, reason)

    @classmethod
    def VALIDATE_INPUTS(
        cls,
        cancel_requested: bool = False,
        profile_id: str = "unqualified",
        route: str = "comfyui_native",
        device: str = "auto",
        local_service_consent: bool = False,
        request: RawContextRequest | None = None,
    ) -> bool | str:
        # CRITICAL: registration/profile names do not prove qualification. Refuse before queueing,
        # but preserve an explicit cancellation's typed non-execution result.
        if type(cancel_requested) is not bool:
            return "invalid_cancel_requested: Cancellation must be a boolean."
        if request is not None and (
            cls.KIND is not ProducerKind.VISUAL or type(request) is not RawContextRequest
        ):
            return "invalid_request: Visual perception needs a typed context request."
        if cancel_requested:
            return True
        from .core.perception_execution import AUDIO_PROFILE, VISUAL_PROFILE

        if profile_id in {VISUAL_PROFILE, AUDIO_PROFILE}:
            expected_route = "ollama" if cls.KIND is ProducerKind.VISUAL else "comfyui_native"
            if (
                route != expected_route
                or device not in ("auto", "cpu")
                or (cls.KIND is ProducerKind.VISUAL and device != "auto")
            ):
                return "unsupported_route_or_device: Selected profile has a fixed runtime route."
            if cls.KIND is ProducerKind.VISUAL and local_service_consent is not True:
                return "local_service_consent_required: Allow transfer to the local Ollama service."
        return cls.capability(
            cancel_requested=cancel_requested, profile_id=profile_id
        ).validation_result()

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        inputs: dict[str, dict[str, tuple[object, dict[str, object]]]] = {
            "required": {
                "media": (MEDIA_PRODUCER_SOCKET_TYPE, {}),
                "route": (
                    "COMBO",
                    {
                        "default": ProducerRoute.COMFYUI_NATIVE.value,
                        "options": list(PERCEPTION_ROUTE_CHOICES),
                    },
                ),
                "profile_id": ("STRING", {"default": "unqualified"}),
                "device": (
                    "COMBO",
                    {"default": "auto", "options": ["auto", "cpu", "cuda", "mps"]},
                ),
                "cancel_requested": ("BOOLEAN", {"default": False}),
            },
            "optional": {"local_service_consent": ("BOOLEAN", {"default": False})},
        }
        if cls.KIND is ProducerKind.VISUAL:
            inputs["optional"]["request"] = (REQUEST_SOCKET_TYPE, {})
        return inputs

    def produce(
        self,
        media: PerceptionProducerResult,
        route: ProducerRoute | str,
        profile_id: str = "unqualified",
        device: str = "auto",
        cancel_requested: bool = False,
        local_service_consent: bool = False,
        request: RawContextRequest | None = None,
    ) -> tuple[Any]:
        from .adapters.perception_host import PerceptionExecutionError, execute_perception
        from .core.perception_execution import AUDIO_PROFILE, VISUAL_PROFILE

        expected = VISUAL_PROFILE if self.KIND is ProducerKind.VISUAL else AUDIO_PROFILE
        if profile_id == expected and not cancel_requested:
            validation = self.VALIDATE_INPUTS(
                cancel_requested,
                profile_id,
                str(route.value if isinstance(route, ProducerRoute) else route),
                device,
                local_service_consent,
                request,
            )
            if validation is not True:
                raise PerceptionExecutionError(str(validation).split(":", 1)[0])
            return (
                execute_perception(
                    media,
                    profile_id=profile_id,
                    local_service_consent=local_service_consent,
                    request=request,
                ),
            )
        return (
            declare_unqualified_perception(
                media,
                kind=self.KIND,
                route=route,
                profile_id=profile_id,
                device=device,
                cancel_requested=cancel_requested,
            ),
        )


class H3VisualPerceptionProducerNode(_UnavailablePerceptionProducerNode):
    NODE_ID = VISUAL_PERCEPTION_NODE_ID
    __h3_context_node_id__ = VISUAL_PERCEPTION_NODE_ID
    RETURN_TYPES = (VISUAL_PRODUCER_SOCKET_TYPE,)
    DESCRIPTION = (
        "Host-configured Qwen frame observations for decoded CPU IMAGE/CFR VIDEO. "
        "Choose qwen38_27b_q4_visual_v2 and ollama; local transfer requires consent. "
        "Connect request to sample only the frames kept by reference conditioning."
    )
    KIND = ProducerKind.VISUAL


class H3AudioPerceptionProducerNode(_UnavailablePerceptionProducerNode):
    NODE_ID = AUDIO_PERCEPTION_NODE_ID
    __h3_context_node_id__ = AUDIO_PERCEPTION_NODE_ID
    RETURN_TYPES = (AUDIO_PRODUCER_SOCKET_TYPE,)
    DESCRIPTION = (
        "Host-configured short English CPU ASR. Choose whisper_large_v3_cpu_en_v1 "
        "and comfyui_native; transcript candidates remain uncertain."
    )
    KIND = ProducerKind.AUDIO


class H3HardConstraintProducerNode:
    """Create exact manual constraints without rewriting caller-owned text."""

    NODE_ID = HARD_CONSTRAINT_PRODUCER_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = ("H3_HARD_CONSTRAINTS", DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE)
    RETURN_NAMES = ("hard_constraints", "producer_report")
    FUNCTION = "produce"
    CATEGORY = "h3_context/assembly"
    DESCRIPTION = "Builds exact caller-authored hard constraints without inference or rewriting."
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "dialogue": ("STRING", {"default": "", "multiline": True}),
                "visible_text": ("STRING", {"default": "", "multiline": True}),
                "required_content": ("STRING", {"default": "", "multiline": True}),
                "forbidden_content": ("STRING", {"default": "", "multiline": True}),
                "timing_start": ("STRING", {"default": ""}),
                "timing_end": ("STRING", {"default": ""}),
                "keep_target": (
                    "COMBO",
                    {
                        "default": "subject",
                        "options": [
                            "subject",
                            "scene",
                            "action",
                            "camera",
                            "style",
                            "audio",
                            "dialogue",
                            "lyrics",
                            "visible_text",
                            "asset",
                        ],
                    },
                ),
                "keep_value": ("STRING", {"default": ""}),
                "change_replacement": ("STRING", {"default": ""}),
            },
            "optional": {
                "dialogue_language": (
                    "COMBO",
                    {"default": "auto", "options": ["auto", *OFFICIAL_STABLE_DIALOGUE_LANGUAGES]},
                ),
                "dialogue_speaker": ("STRING", {"default": ""}),
                "dialogue_delivery": (
                    "COMBO",
                    {
                        "default": "on_screen",
                        "options": [value.value for value in DialogueDelivery],
                    },
                ),
            },
        }

    def produce(
        self,
        dialogue: str = "",
        visible_text: str = "",
        required_content: str = "",
        forbidden_content: str = "",
        timing_start: str = "",
        timing_end: str = "",
        keep_target: str = "subject",
        keep_value: str = "",
        change_replacement: str = "",
        dialogue_language: str = "auto",
        dialogue_speaker: str = "",
        dialogue_delivery: str = "on_screen",
    ) -> tuple[HardConstraintSet, DownstreamProducerReport]:
        component = build_manual_hard_constraints(
            dialogue=dialogue,
            visible_text=visible_text,
            required_content=required_content,
            forbidden_content=forbidden_content,
            timing_start=timing_start,
            timing_end=timing_end,
            keep_target=keep_target,
            keep_value=keep_value,
            change_replacement=change_replacement,
            dialogue_language=dialogue_language,
            dialogue_speaker=dialogue_speaker,
            dialogue_delivery=dialogue_delivery,
        )
        return (
            component,
            issue_manual_component_report(DownstreamStage.HARD_CONSTRAINT, component),
        )


class H3IntentGraphProducerNode:
    """Build the deterministic manual intent skeleton through the accepted graph builder."""

    NODE_ID = INTENT_GRAPH_PRODUCER_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = (INTENT_GRAPH_SOCKET_TYPE, DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE)
    RETURN_NAMES = ("intent_graph", "producer_report")
    FUNCTION = "produce"
    CATEGORY = "h3_context/assembly"
    DESCRIPTION = "Builds a deterministic manual intent graph from request and registry."
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "request": (REQUEST_SOCKET_TYPE, {}),
                "reference_registry": (REFERENCE_SOCKET_TYPE, {}),
                "subject_label": ("STRING", {"default": ""}),
                "action_description": ("STRING", {"default": "", "multiline": True}),
            },
            "optional": {
                "secondary_subject_label": ("STRING", {"default": ""}),
                "secondary_action_description": (
                    "STRING",
                    {"default": "", "multiline": True},
                ),
                "complete_silence": ("BOOLEAN", {"default": False}),
                "keyframe_binding": (
                    "COMBO",
                    {
                        "default": ManualKeyframeBinding.UNBOUND.value,
                        "options": [item.value for item in ManualKeyframeBinding],
                    },
                ),
                "segment_development": (
                    "COMBO",
                    {
                        "default": SegmentDevelopment.UNSPECIFIED.value,
                        "options": [item.value for item in SegmentDevelopment],
                    },
                ),
            },
        }

    def produce(
        self,
        request: RawContextRequest,
        reference_registry: ReferenceRegistry,
        subject_label: str = "",
        action_description: str = "",
        secondary_subject_label: str = "",
        secondary_action_description: str = "",
        complete_silence: bool = False,
        keyframe_binding: str = ManualKeyframeBinding.UNBOUND.value,
        segment_development: str = SegmentDevelopment.UNSPECIFIED.value,
    ) -> tuple[IntentGraph, DownstreamProducerReport]:
        if type(keyframe_binding) is not str or type(segment_development) is not str:
            raise ContractValidationError("manual declaration widgets must be exact strings")
        try:
            binding = ManualKeyframeBinding(keyframe_binding)
            development = SegmentDevelopment(segment_development)
        except ValueError as exc:
            raise ContractValidationError("manual declaration widget value is unsupported") from exc
        component = build_manual_intent(
            request,
            reference_registry,
            subject_label=subject_label,
            action_description=action_description,
            secondary_subject_label=secondary_subject_label,
            secondary_action_description=secondary_action_description,
            complete_silence=complete_silence,
            keyframe_binding=binding,
            segment_development=development,
        )
        return (
            component,
            issue_manual_component_report(
                DownstreamStage.INTENT_GRAPH, component, (request, reference_registry)
            ),
        )


class H3EvidenceFusionProducerNode:
    """Project admitted source metadata into an uncertainty-capped evidence graph."""

    NODE_ID = EVIDENCE_FUSION_PRODUCER_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = (
        UNIFIED_EVIDENCE_GRAPH_SOCKET_TYPE,
        DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE,
    )
    RETURN_NAMES = ("evidence_graph", "producer_report")
    FUNCTION = "produce"
    CATEGORY = "h3_context/assembly"
    DESCRIPTION = "Projects admitted source metadata without claiming semantic perception."
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {"required": {"media": (MEDIA_PRODUCER_SOCKET_TYPE, {})}}

    def produce(
        self, media: PerceptionProducerResult
    ) -> tuple[UnifiedEvidenceGraph, DownstreamProducerReport]:
        return build_admission_evidence_graph(media)


class H3CrossReferenceProducerNode:
    """Build canonical source-identity nodes without inferring semantic identities."""

    NODE_ID = CROSS_REFERENCE_PRODUCER_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = (CROSS_REFERENCE_GRAPH_SOCKET_TYPE, DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE)
    RETURN_NAMES = ("cross_reference_graph", "producer_report")
    FUNCTION = "produce"
    CATEGORY = "h3_context/assembly"
    DESCRIPTION = "Builds source-owned cross-reference identities from admitted media."
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "reference_registry": (REFERENCE_SOCKET_TYPE, {}),
                "media": (MEDIA_PRODUCER_SOCKET_TYPE, {}),
            },
            "optional": {
                "resolution": (
                    "COMBO",
                    {"default": "source_only", "options": ["source_only", "ambiguous"]},
                ),
                "entity_kind": (
                    "COMBO",
                    {"default": "subject", "options": ["subject", "voice", "object", "scene"]},
                ),
                "candidate_a": ("STRING", {"default": ""}),
                "candidate_b": ("STRING", {"default": ""}),
            },
        }

    def produce(
        self,
        reference_registry: ReferenceRegistry,
        media: PerceptionProducerResult,
        resolution: str = "source_only",
        entity_kind: str = "subject",
        candidate_a: str = "",
        candidate_b: str = "",
    ) -> tuple[CrossReferenceGraph, DownstreamProducerReport]:
        return build_source_cross_reference(
            reference_registry,
            media,
            resolution=resolution,
            entity_kind=entity_kind,
            candidate_a=candidate_a,
            candidate_b=candidate_b,
        )


class H3DirectiveAuthorityProducerNode:
    """Project explicit directives through the accepted deterministic authority engine."""

    NODE_ID = DIRECTIVE_AUTHORITY_PRODUCER_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = (DIRECTIVE_AUTHORITY_SOCKET_TYPE, DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE)
    RETURN_NAMES = ("directive_authority", "producer_report")
    FUNCTION = "produce"
    CATEGORY = "h3_context/assembly"
    DESCRIPTION = "Builds deterministic directive authority without hidden conflict choices."
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "request": (REQUEST_SOCKET_TYPE, {}),
                "reference_registry": (REFERENCE_SOCKET_TYPE, {}),
                "hard_constraints": ("H3_HARD_CONSTRAINTS", {}),
                "hard_constraints_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
                "intent_graph": (INTENT_GRAPH_SOCKET_TYPE, {}),
                "intent_report": (DOWNSTREAM_PRODUCER_REPORT_SOCKET_TYPE, {}),
                "action": (
                    "COMBO",
                    {"default": "none", "options": ["none", "retain", "adapt", "exclude"]},
                ),
                "target_kind": (
                    "COMBO",
                    {
                        "default": "subject",
                        "options": [
                            "subject",
                            "scene",
                            "action",
                            "asset",
                        ],
                    },
                ),
                "target_id": ("STRING", {"default": ""}),
                "source_asset_id": ("STRING", {"default": ""}),
                "retention_aspect": (
                    "COMBO",
                    {
                        "default": "identity",
                        "options": [
                            "identity",
                            "style",
                            "camera",
                            "audio",
                            "voice",
                            "scene",
                            "action",
                            "object",
                        ],
                    },
                ),
                "adaptation": ("STRING", {"default": "", "multiline": True}),
                "exclusion_reason": ("STRING", {"default": "", "multiline": True}),
                "authority": (
                    "COMBO",
                    {
                        "default": "user_preference",
                        "options": [
                            "user_hard",
                            "user_preference",
                            "reference_only",
                            "assisted_proposal",
                        ],
                    },
                ),
                "priority": ("INT", {"default": 0, "min": 0, "max": 1000}),
            }
        }

    def produce(
        self,
        request: RawContextRequest,
        reference_registry: ReferenceRegistry,
        hard_constraints: HardConstraintSet,
        hard_constraints_report: DownstreamProducerReport,
        intent_graph: IntentGraph,
        intent_report: DownstreamProducerReport,
        action: str = "none",
        target_kind: str = "subject",
        target_id: str = "",
        source_asset_id: str = "",
        retention_aspect: str = "identity",
        adaptation: str = "",
        exclusion_reason: str = "",
        authority: str = "user_preference",
        priority: int = 0,
    ) -> tuple[DirectiveAuthorityBundle, DownstreamProducerReport]:
        if type(action) is not str:
            raise ContractValidationError("directive action must be an exact string")
        return build_manual_directive_authority(
            request,
            reference_registry,
            hard_constraints,
            hard_constraints_report,
            intent_graph,
            intent_report,
            action="" if action == "none" else action,
            target_kind=target_kind,
            target_id=target_id,
            source_asset_id=source_asset_id,
            retention_aspect=retention_aspect,
            adaptation=adaptation,
            exclusion_reason=exclusion_reason,
            authority=authority,
            priority=priority,
        )
