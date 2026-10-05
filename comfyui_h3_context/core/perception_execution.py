"""Module-issued qualified analysis; portable receipts carry no reconstruction authority."""

from __future__ import annotations

import hashlib
import json
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, cast

from .audio_analysis import AudioAnalysisBatch, AudioAnalysisDiagnostic, AudioObservation
from .constraints import TimePoint
from .errors import ContractValidationError
from .evidence import EvidenceRecord, EvidenceSource, Provenance, Uncertainty
from .perception_producer import PerceptionProducerResult
from .reference_window import ReferenceConditioningWindow
from .video_analysis import (
    VideoAnalysisBatch,
    VideoAnalysisDiagnostic,
    VideoKeyframe,
    VideoObservation,
    VideoShot,
)

VISUAL_PROFILE = "qwen38_27b_q4_visual_v2"
AUDIO_PROFILE = "whisper_large_v3_cpu_en_v1"
PROFILE_MODELS = MappingProxyType(
    {
        VISUAL_PROFILE: "25b843619e944cd0ae6069f94ff4e5e26a16e109ccbc0a66a0f05979ed70098e",
        AUDIO_PROFILE: "a8e94b85976e5864ba3e9525c7e6c83b2a1eca42d4b797a0c7c24d778e40fd95",
    }
)
_CONTRACTS = frozenset(
    {
        AudioAnalysisBatch,
        AudioAnalysisDiagnostic,
        AudioObservation,
        VideoAnalysisBatch,
        VideoAnalysisDiagnostic,
        VideoKeyframe,
        VideoObservation,
        VideoShot,
        ReferenceConditioningWindow,
        TimePoint,
        EvidenceRecord,
        EvidenceSource,
        Provenance,
        Uncertainty,
    }
)


def _batch_digest(batch: VideoAnalysisBatch | AudioAnalysisBatch) -> str:
    def validate(value: object) -> None:
        if value is None or type(value) in {str, int, float, bool, Decimal}:
            return
        if isinstance(value, Enum):
            # Only enum values owned by these exact, imported contract modules are admitted.
            from .audio_analysis import AudioAnalysisStatus, AudioObservationKind
            from .contracts import EvidenceLevel, ProviderIdentity, ValidationSeverity
            from .evidence import EvidenceOrigin, EvidenceSourceKind, SupportStatus, UncertaintyKind
            from .video_analysis import VideoAnalysisStatus, VideoObservationKind, VideoOrientation

            if type(value) not in {
                AudioAnalysisStatus,
                AudioObservationKind,
                VideoAnalysisStatus,
                VideoObservationKind,
                VideoOrientation,
                EvidenceLevel,
                ProviderIdentity,
                ValidationSeverity,
                EvidenceOrigin,
                EvidenceSourceKind,
                SupportStatus,
                UncertaintyKind,
            }:
                raise ContractValidationError("analysis enum is not owned")
            return
        if type(value) is tuple:
            for item in value:
                validate(item)
            return
        if type(value) not in _CONTRACTS:
            raise ContractValidationError("analysis contains a non-owned contract")
        for entry in fields(cast(Any, value)):
            validate(getattr(value, entry.name))
        type(cast(Any, value)).__post_init__(value)

    validate(batch)
    encoded = json.dumps(batch.to_wire(), sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 65536:
        raise ContractValidationError("analysis exceeds its byte envelope")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, eq=False)
class QualifiedPerceptionResult:
    """A successful execution may still produce explicitly partial uncertain analysis."""

    profile_id: str
    source_sha256: str
    parent: PerceptionProducerResult = field(repr=False)
    analysis: VideoAnalysisBatch | AudioAnalysisBatch = field(repr=False)

    def assert_current(self, parent: PerceptionProducerResult | None = None) -> None:
        # CRITICAL: equality/copies and serialized receipts cannot grant execution authority.
        authority = _AUTHORITY.get(self)
        if authority is None or (parent is not None and parent is not self.parent):
            raise ContractValidationError("qualified result does not authorize this source")
        original_parent, original_analysis, digest, current = authority
        if self.parent is not original_parent or self.analysis is not original_analysis:
            raise ContractValidationError("qualified result identity changed")
        self.parent.assert_current()
        if (
            self.profile_id not in PROFILE_MODELS
            or (self.profile_id + ":" + self.source_sha256 + ":" + _batch_digest(self.analysis))
            != digest
        ):
            raise ContractValidationError("qualified analysis is stale")
        if current() != self.source_sha256:
            raise ContractValidationError("qualified source or runtime authority is stale")

    def to_wire(self) -> dict[str, str]:
        self.assert_current()
        return {
            "schema": "h3.perception_execution.v1",
            "status": "partial",
            "profile_id": self.profile_id,
            "model_sha256": PROFILE_MODELS[self.profile_id],
            "asset_id": self.parent.asset_id,
            "source_sha256": self.source_sha256,
            "analysis_sha256": _batch_digest(self.analysis),
        }


_AUTHORITY: weakref.WeakKeyDictionary[
    QualifiedPerceptionResult,
    tuple[PerceptionProducerResult, object, str, Callable[[], str]],
] = weakref.WeakKeyDictionary()


def _issue_execution(
    profile_id: str,
    source_sha256: str,
    parent: PerceptionProducerResult,
    analysis: VideoAnalysisBatch | AudioAnalysisBatch,
    current: Callable[[], str],
) -> QualifiedPerceptionResult:
    if profile_id not in PROFILE_MODELS or type(parent) is not PerceptionProducerResult:
        raise ContractValidationError("execution profile/source is not supported")
    expected = VideoAnalysisBatch if profile_id == VISUAL_PROFILE else AudioAnalysisBatch
    if type(analysis) is not expected or analysis.selected_asset_ids != (parent.asset_id,):
        raise ContractValidationError("execution analysis/source join is invalid")
    if profile_id == VISUAL_PROFILE and (
        type(analysis) is not VideoAnalysisBatch
        or analysis.admitted_frame_count is None
        or analysis.frame_rate is None
        or (parent.media_kind == "video" and analysis.frame_rate <= 0)
        or (
            parent.media_kind == "image"
            and (
                analysis.frame_rate != 0
                or analysis.admitted_frame_count != 1
                or analysis.conditioning_window is not None
            )
        )
    ):
        raise ContractValidationError("qualified visual analysis lacks admitted frame metadata")
    if not analysis.observations:
        raise ContractValidationError("empty analysis cannot authorize success")
    result = QualifiedPerceptionResult(profile_id, source_sha256, parent, analysis)
    _AUTHORITY[result] = (
        parent,
        analysis,
        profile_id + ":" + source_sha256 + ":" + _batch_digest(analysis),
        current,
    )
    result.assert_current(parent)
    return result
