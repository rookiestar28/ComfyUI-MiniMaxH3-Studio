"""Execution-local join of native mapping, host observation and admitted audio.

This qualifies a structural composition, never a model result or a file codec. Audio must
already be a host-decoded AUDIO value. Existing generation and media producers own the facts;
the content-free projection is evidence, not a browser-replayable execution permission.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from .canonical import canonical_fingerprint, fingerprint_context_report
from .context_reporting import ContextReport
from .contracts import MediaKind, TaskMode
from .errors import ContractValidationError, NativeH3AdapterError
from .generation_profile import FamilyDisposition, HostObservation, qualify_generation_profile
from .native_asset_resolution import NativeAssetResolutionV1
from .native_h3 import (
    NATIVE_H3_HOST_REVISION,
    NATIVE_H3_SOURCE_BLOB,
    NativeH3Wiring,
    assert_native_h3_wiring_authority,
)
from .native_source_compatibility import NativeSourceCompatibilityV2, NativeSourceObservation
from .perception_producer import PerceptionProducerResult, ProducerDisposition, ProducerKind
from .registry import ReferenceAsset


class NativeCompositionDisposition(str, Enum):
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class NativeCompositionObservation:
    """One fresh producer observation: host facts and the loaded native source, if readable."""

    host: HostObservation | None
    source: NativeSourceObservation | None


@dataclass(frozen=True, slots=True)
class NativeCompositionQualification:
    """A current exact-subject join, retained only for this local execution."""

    disposition: NativeCompositionDisposition
    reason: str
    subject_fingerprint: str
    host_fingerprint: str | None
    media_fingerprints: tuple[str, ...]
    _report: ContextReport = field(repr=False, compare=False)
    _wiring: NativeH3Wiring = field(repr=False, compare=False)
    _host: HostObservation | None = field(repr=False, compare=False)
    _source: NativeSourceObservation | None = field(repr=False)
    _media: tuple[PerceptionProducerResult, ...] = field(repr=False, compare=False)
    _media_identity: tuple[tuple[int, int | None], ...] = field(repr=False)
    _observe_current: Callable[[], NativeCompositionObservation] | None = field(
        repr=False, compare=False
    )
    _asset_resolution: NativeAssetResolutionV1 | None = field(default=None, repr=False)
    _source_compatibility: NativeSourceCompatibilityV2 | None = field(default=None, repr=False)
    #: Closed internal diagnostic of the exercised refusal branch; never part of a wire or error.
    detail: str | None = field(default=None, repr=False)

    @property
    def qualified(self) -> bool:
        return self.disposition is NativeCompositionDisposition.QUALIFIED

    def observed_source(self) -> NativeSourceObservation | None:
        """The in-process source observation this join used; never serialized."""
        return self._source

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "schema": "h3.native_composition_qualification.v1",
            "disposition": self.disposition.value,
            "reason": self.reason,
            "subject_fingerprint": self.subject_fingerprint,
            "host_fingerprint": self.host_fingerprint,
            "media_fingerprints": list(self.media_fingerprints),
            "execution_fingerprint": canonical_fingerprint(
                [list(identity) for identity in self._media_identity]
            ),
        }
        if self._asset_resolution is not None:
            wire["schema"] = "h3.native_composition_qualification.v2"
            wire["asset_resolution_fingerprint"] = self._asset_resolution.inventory_fingerprint
        if self._source_compatibility is not None:
            # v4: the structural receipt binds classifier, manifest and raw-source identity.
            wire["schema"] = "h3.native_composition_qualification.v4"
            wire["source_compatibility_fingerprint"] = self._source_compatibility.fingerprint
        return wire

    def assert_current(self, report: ContextReport, wiring: NativeH3Wiring) -> None:
        # IMPORTANT: neither a serialized receipt nor another Ref2VA graph can authorize
        # this composition. Rejoin exact runtime values; hashes alone provide no authority.
        if (
            type(self) is not NativeCompositionQualification
            or self._report is not report
            or self._wiring is not wiring
        ):
            raise NativeH3AdapterError(
                "native_composition_receipt_stale", "composition owner changed"
            )
        current = qualify_native_composition(
            report,
            wiring,
            host=self._host,
            source=self._source,
            media=self._media,
            observe_current=self._observe_current,
            asset_resolution=self._asset_resolution,
            source_compatibility=self._source_compatibility,
        )
        if current != self:
            raise NativeH3AdapterError(
                "native_composition_receipt_stale", "composition facts changed"
            )


def is_audio_only_composition(wiring: NativeH3Wiring) -> bool:
    return bool(wiring.bindings) and all(
        binding.kind is MediaKind.AUDIO for binding in wiring.bindings
    )


def _host_fingerprint(host: HostObservation) -> str:
    return canonical_fingerprint(
        {
            "host_version": host.host_version,
            "anchors": sorted(host.anchor_node_types),
            "templates": [list(item) for item in host.template_digests],
            "slots": [[item.slot.value, item.disposition.value] for item in host.slots],
            "capabilities": [
                [item.template_name, item.anchor_node_type, sorted(item.input_names)]
                for item in host.template_capabilities
            ],
        }
    )


def _audio_current(media: PerceptionProducerResult, asset: ReferenceAsset) -> bool:
    if type(media) is not PerceptionProducerResult:
        return False
    try:
        PerceptionProducerResult.assert_current(media)
    except ContractValidationError:
        return False
    if (
        media.kind is not ProducerKind.MEDIA
        or media.disposition is not ProducerDisposition.COMPLETE
        or media.media_kind != "audio"
    ):
        return False
    evidence = media.admission_evidence
    expected_role = "paired_audio" if asset.paired_video_id is not None else "reference"
    if (
        media.asset_id != asset.asset_id
        or evidence.reference_order != asset.connection_order - 1
        or evidence.reference_role != expected_role
    ):
        return False
    metadata = asset.metadata
    if metadata is None or metadata.duration_seconds != Decimal(str(evidence.duration_seconds)):
        return False
    # Host AUDIO is decoded waveform data, not a container filename or a codec_ok flag.
    # Bind actual shape/rate to admitted declarations; do not open or resample media here.
    payload = media.runtime_payload
    if type(payload) is not dict or set(payload) != {"waveform", "sample_rate"}:
        return False
    shape = getattr(payload["waveform"], "shape", ())
    rate = payload["sample_rate"]
    return (
        type(rate) is int
        and rate == evidence.sample_rate_hz
        and isinstance(shape, tuple)
        and len(shape) == 3
        and tuple(shape[:2]) == (1, evidence.channel_count)
        and type(shape[2]) is int
        and Decimal(shape[2]) / Decimal(rate) == metadata.duration_seconds
    )


def _waveform_identity(media: PerceptionProducerResult) -> tuple[int, int | None]:
    # IMPORTANT: the AUDIO dict can retain its identity while its waveform is replaced.
    # Tensor version counters also catch ordinary in-place edits; no sample data enters wire.
    payload = media.runtime_payload
    if type(payload) is not dict:
        raise NativeH3AdapterError("native_audio_source_unqualified", "audio payload changed")
    waveform = payload["waveform"]
    try:
        version = getattr(waveform, "_version", None)
    except RuntimeError:
        version = None  # Host inference tensors do not expose a mutation counter.
    return id(waveform), version if type(version) is int else None


def qualify_native_composition(
    report: ContextReport,
    wiring: NativeH3Wiring,
    *,
    host: HostObservation | None = None,
    source: NativeSourceObservation | None = None,
    media: tuple[PerceptionProducerResult, ...] = (),
    observe_current: Callable[[], NativeCompositionObservation] | None = None,
    asset_resolution: NativeAssetResolutionV1 | None = None,
    source_compatibility: NativeSourceCompatibilityV2 | None = None,
) -> NativeCompositionQualification:
    """Join current producer observations; absent facts yield a named no-effect hold."""
    assert_native_h3_wiring_authority(wiring, report)
    if observe_current is not None:
        # IMPORTANT: reobserve before issuing as well as consuming a receipt. The supplied
        # snapshot can already be stale when host slots or loaded source changed concurrently.
        if not callable(observe_current):
            raise NativeH3AdapterError("native_host_currentness_unqualified", "observer invalid")
        observed = observe_current()
        if type(observed) is not NativeCompositionObservation:
            raise NativeH3AdapterError("native_host_currentness_unqualified", "observation invalid")
        host, source = observed.host, observed.source
    if host is not None and type(host) is not HostObservation:
        raise NativeH3AdapterError("native_host_unqualified", "host observation must be exact")
    if source is not None and type(source) is not NativeSourceObservation:
        raise NativeH3AdapterError("native_host_currentness_unqualified", "observation invalid")
    if asset_resolution is not None:
        if type(asset_resolution) is not NativeAssetResolutionV1:
            raise NativeH3AdapterError("native_asset_resolution_unqualified", "resolution invalid")
        asset_resolution.assert_current(host)
    if type(media) is not tuple or len(media) > 12:
        raise NativeH3AdapterError("native_audio_source_unqualified", "media inventory is invalid")
    if source_compatibility is not None and (
        type(source_compatibility) is not NativeSourceCompatibilityV2
        or not source_compatibility.admits(source, wiring)
    ):
        # IMPORTANT: a fresh observation is compared with the ORIGINAL receipt. Even a benign
        # byte edit invalidates it; never re-mint authority from newer bytes inside this check.
        raise NativeH3AdapterError("native_source_identity_mismatch", "compatibility changed")
    subject = canonical_fingerprint(
        {
            "report": fingerprint_context_report(report),
            "wiring": canonical_fingerprint(wiring.to_wire()),
            "registry": report.request.reference_registry.to_wire(),
        }
    )
    reason = "qualified_structural_composition"
    detail: str | None = None
    disposition = NativeCompositionDisposition.QUALIFIED
    media_fingerprints: tuple[str, ...] = ()
    if not wiring.queue_ready:
        reason = "native_duration_unqualified"
    elif host is None:
        reason = "native_host_unqualified"
    elif (
        wiring.native_source_blob != NATIVE_H3_SOURCE_BLOB
        or wiring.host_revision != NATIVE_H3_HOST_REVISION
    ):
        reason, detail = "native_source_identity_mismatch", "wiring_identity"
    elif source is None:
        reason, detail = "native_source_identity_mismatch", "source_unavailable"
    elif source.source_blob != NATIVE_H3_SOURCE_BLOB and source_compatibility is None:
        if (
            wiring.task_mode is TaskMode.T2VA
            and not wiring.bindings
            and not source.structure.admitted
        ):
            # A readable, nonmatching T2VA surface. `check` is a closed manifest identifier.
            reason, detail = "native_t2va_structure_changed", source.structure.check
        else:
            # Media modes and bindings stay pinned; a missing receipt is never promoted.
            reason, detail = "native_source_identity_mismatch", "receipt_absent"
    else:
        detail = (
            "baseline_blob" if source.source_blob == NATIVE_H3_SOURCE_BLOB else "structural_receipt"
        )
        family = qualify_generation_profile(host).for_task_mode(wiring.task_mode.value)
        reasons = {
            FamilyDisposition.UNSUPPORTED_HOST: "native_host_unsupported",
            FamilyDisposition.TEMPLATE_DRIFT: "native_template_drift",
        }
        reason = reasons.get(family.disposition, reason)
        # CRITICAL: missing/relocated weight names are advisory. Requiring official-name
        # resolution here would stop App Mode before ComfyUI can validate its queued loaders.
        if family.disposition is FamilyDisposition.UNSUPPORTED_HOST:
            disposition = NativeCompositionDisposition.UNSUPPORTED
        audio_assets = tuple(
            asset
            for asset in report.request.reference_registry.assets
            if asset.kind is MediaKind.AUDIO
        )
        if reason == "qualified_structural_composition":
            if len(media) != len(audio_assets) or not all(
                _audio_current(item, asset) for item, asset in zip(media, audio_assets, strict=True)
            ):
                reason = "native_audio_source_unqualified"
            else:
                media_fingerprints = tuple(
                    "sha256:" + item.receipt.execution_fingerprint for item in media
                )
                if observe_current is None:
                    reason = "native_host_currentness_unqualified"
    if (
        reason != "qualified_structural_composition"
        and disposition is NativeCompositionDisposition.QUALIFIED
    ):
        disposition = NativeCompositionDisposition.UNQUALIFIED
    return NativeCompositionQualification(
        disposition,
        reason,
        subject,
        None if host is None else _host_fingerprint(host),
        media_fingerprints,
        report,
        wiring,
        host,
        source,
        media,
        tuple(_waveform_identity(item) for item in media) if media_fingerprints else (),
        observe_current,
        asset_resolution,
        source_compatibility,
        detail,
    )
