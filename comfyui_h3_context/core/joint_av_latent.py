"""Typed identity for the joint audiovisual latent checkpoint artifact (M20-04).

This pure module owns descriptor and receipt truth for the one latent family the M19-08
requalification measured: the native H3 joint AV latent, a NestedTensor pair whose two dense
domains are video ``[B, 24, T, H/16, W/16]`` and audio ``[B, 32, 2, T40]`` at 40 latent fps.
Native ``SaveLatent`` persistence is unavailable on the qualified subject, so this repository
owns the serialized form; per-domain dense decomposition is sufficient because the pair is two
dense tensors.

The serialized payload is deliberately not a container format: one blob per checkpoint, the two
domains' raw C-contiguous little-endian bytes concatenated video-then-audio.  Offsets derive
from the validated descriptor, and ``byte_length == prod(shape) * itemsize`` is exact integer
arithmetic this module verifies without any tensor library.  A parser-bearing container inside
the private root would add untrusted-input surface for no consumer: the only reader is M20-05,
in-repo.

Nothing here may promote a qualification row.  Every descriptor and receipt is constructed
through an authority frozen from the accepted temporal capability profile, and the authority
builder refuses any status other than ``SUPPORTED``.  Filesystem paths, payload bytes, cleanup
and host storage remain behind the adapter boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import Enum

from .canonical import canonical_bytes, canonical_fingerprint
from .fingerprint_domain import IdentityDomain, domain_fingerprint
from .length import MAX_FRAME_COUNT
from .segment_artifacts import (
    ArtifactLifecycleState,
    redact_segment_artifact_fingerprint,
)
from .temporal_profile import CapabilityStatus, TemporalCapabilityProfile

JOINT_AV_LATENT_DESCRIPTOR_SCHEMA = "h3.context.joint_av_latent_descriptor.v1"
JOINT_AV_LATENT_RECEIPT_SCHEMA = "h3.context.joint_av_latent_receipt.v1"
JOINT_AV_LATENT_PUBLIC_SCHEMA = "h3.context.joint_av_latent_public.v1"
JOINT_AV_LATENT_AUTHORITY_SCHEMA = "h3.context.joint_av_latent_authority.v1"

#: The M19-08 row this artifact family is bound to; the only row it may consume.
JOINT_AV_LATENT_CAPABILITY = "joint_av_latent_descriptor"

MAX_JOINT_AV_LATENT_RECEIPT_BYTES = 32_768
MAX_JOINT_AV_LATENT_TTL_MILLISECONDS = 30 * 24 * 60 * 60 * 1_000
MAX_JOINT_AV_LATENT_PAYLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_JOINT_AV_LATENT_DIMENSION = 1_000_000

#: Measured layout pins (M19-08 `joint_av_latent_descriptor`, `descriptor_measured_live`).
#: The pinned source allocates video `[B, 24, T, H/16, W/16]` and audio `[B, 32, 2, T40]`;
#: the two domains do not share a rank.  A shape that does not carry these axes is not a
#: value this family measured, whatever produced it.
VIDEO_LATENT_RANK = 5
VIDEO_LATENT_CHANNELS = 24
AUDIO_LATENT_RANK = 4
AUDIO_LATENT_CHANNELS = 32
AUDIO_LATENT_PLANES = 2

#: Closed dtype vocabulary with fixed item sizes.  The qualified subject's latent dtype is not
#: observable over HTTP (recorded matrix limitation); the in-process producer declares it from
#: the live pair, and this module only accepts a declaration it can do exact byte arithmetic
#: for.  Widening the vocabulary is a contract change, not a convenience.
LATENT_DTYPE_ITEM_SIZES: dict[str, int] = {
    "float32": 4,
    "float16": 2,
    "bfloat16": 2,
}

_IDENTIFIER_MAX = 128
_FINGERPRINT_PREFIX = "sha256:"


class JointAVLatentError(ValueError):
    """Raised when a joint AV latent descriptor or receipt cannot preserve exact identity."""


class JointAVLatentKind(str, Enum):
    JOINT_AV_LATENT = "joint_av_latent"


class LatentDomain(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > _IDENTIFIER_MAX:
        raise JointAVLatentError(f"bounded_identifier:{field}")
    for char in value:
        if not (char.isascii() and (char.isalnum() or char in "_.:-")):
            raise JointAVLatentError(f"bounded_identifier:{field}")
    return value


def _fingerprint(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value.startswith(_FINGERPRINT_PREFIX)
        or len(value) != len(_FINGERPRINT_PREFIX) + 64
        or any(char not in "0123456789abcdef" for char in value[len(_FINGERPRINT_PREFIX) :])
    ):
        raise JointAVLatentError(f"sha256_fingerprint:{field}")
    return value


def _optional_fingerprint(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _fingerprint(value, field)


def _positive_integer(value: object, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise JointAVLatentError(f"positive_integer:{field}")
    return value


@dataclass(frozen=True, slots=True)
class JointAVLatentAuthority:
    """The frozen consumption of the accepted profile's supported descriptor row.

    Following the accepted `build_h3_timeline_profile` shape: a consumer never carries the live
    profile around, it carries a narrow fingerprinted projection.  The projection exists only in
    the ``SUPPORTED`` state -- an authority for an unqualified row is not a value with a flag,
    it is a construction refusal.
    """

    subject_identity: str
    reason_code: str
    profile_fingerprint: str
    schema: str = JOINT_AV_LATENT_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != JOINT_AV_LATENT_AUTHORITY_SCHEMA:
            raise JointAVLatentError("unsupported_authority_schema")
        _fingerprint(self.subject_identity, "subject_identity")
        _identifier(self.reason_code, "reason_code")
        _fingerprint(self.profile_fingerprint, "profile_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "capability": JOINT_AV_LATENT_CAPABILITY,
            "subject_identity": self.subject_identity,
            "reason_code": self.reason_code,
            "profile_fingerprint": self.profile_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return domain_fingerprint(IdentityDomain.SEMANTIC, self.to_wire()).digest


def build_joint_av_latent_authority(
    profile: TemporalCapabilityProfile,
) -> JointAVLatentAuthority:
    """Freeze the accepted profile's descriptor claim, refusing every unqualified state."""

    if not isinstance(profile, TemporalCapabilityProfile):
        raise JointAVLatentError("authority_requires_temporal_profile")
    claim = profile.capability_claim(JOINT_AV_LATENT_CAPABILITY)
    if claim.status is not CapabilityStatus.SUPPORTED:
        raise JointAVLatentError("descriptor_row_not_supported")
    if profile.subject_identity is None:
        raise JointAVLatentError("descriptor_row_without_subject_identity")
    return JointAVLatentAuthority(
        subject_identity=profile.subject_identity,
        reason_code=claim.reason_code,
        profile_fingerprint=canonical_fingerprint(profile.to_wire()),
    )


@dataclass(frozen=True, slots=True)
class LatentDomainDescriptor:
    """One dense domain of the pair: shape, declared dtype, and its exact byte extent."""

    domain: LatentDomain
    shape: tuple[int, ...]
    dtype: str
    byte_length: int

    def __post_init__(self) -> None:
        if type(self.domain) is not LatentDomain:
            raise JointAVLatentError("latent_domain")
        expected_rank = (
            VIDEO_LATENT_RANK if self.domain is LatentDomain.VIDEO else AUDIO_LATENT_RANK
        )
        if type(self.shape) is not tuple or len(self.shape) != expected_rank:
            raise JointAVLatentError(f"latent_rank:{self.domain.value}")
        for index, dimension in enumerate(self.shape):
            _positive_integer(
                dimension,
                f"{self.domain.value}_shape[{index}]",
                MAX_JOINT_AV_LATENT_DIMENSION,
            )
        if self.domain is LatentDomain.VIDEO:
            if self.shape[1] != VIDEO_LATENT_CHANNELS:
                raise JointAVLatentError("video_latent_channels")
        else:
            if self.shape[1] != AUDIO_LATENT_CHANNELS:
                raise JointAVLatentError("audio_latent_channels")
            if self.shape[2] != AUDIO_LATENT_PLANES:
                raise JointAVLatentError("audio_latent_planes")
        if self.dtype not in LATENT_DTYPE_ITEM_SIZES:
            raise JointAVLatentError(f"latent_dtype:{self.domain.value}")
        element_count = 1
        for dimension in self.shape:
            element_count *= dimension
        expected_bytes = element_count * LATENT_DTYPE_ITEM_SIZES[self.dtype]
        if expected_bytes > MAX_JOINT_AV_LATENT_PAYLOAD_BYTES:
            raise JointAVLatentError(f"latent_domain_bytes:{self.domain.value}")
        if self.byte_length != expected_bytes:
            raise JointAVLatentError(f"latent_byte_arithmetic:{self.domain.value}")

    def to_wire(self) -> dict[str, object]:
        return {
            "domain": self.domain.value,
            "shape": list(self.shape),
            "dtype": self.dtype,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True, slots=True)
class JointAVLatentDescriptor:
    """The exact serialized structure of one checkpoint payload.

    The payload is ``video.byte_length`` bytes of the video domain followed by
    ``audio.byte_length`` bytes of the audio domain; there is no header to parse and nothing
    else in the blob.  ``completed_frames`` records the completed boundary the checkpoint
    stands at, in decoded frames; the latent-step arithmetic between ``T`` and frames belongs
    to the resume item that will hold live tensors, not to storage identity.
    """

    video: LatentDomainDescriptor
    audio: LatentDomainDescriptor
    requested_frames: int
    completed_frames: int
    authority_fingerprint: str
    descriptor_fingerprint: str | None = None
    schema: str = JOINT_AV_LATENT_DESCRIPTOR_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != JOINT_AV_LATENT_DESCRIPTOR_SCHEMA:
            raise JointAVLatentError("unsupported_descriptor_schema")
        if type(self.video) is not LatentDomainDescriptor or self.video.domain is not (
            LatentDomain.VIDEO
        ):
            raise JointAVLatentError("descriptor_video_domain")
        if type(self.audio) is not LatentDomainDescriptor or self.audio.domain is not (
            LatentDomain.AUDIO
        ):
            raise JointAVLatentError("descriptor_audio_domain")
        if self.video.shape[0] != self.audio.shape[0]:
            raise JointAVLatentError("descriptor_batch_mismatch")
        _positive_integer(self.requested_frames, "requested_frames", MAX_FRAME_COUNT)
        _positive_integer(self.completed_frames, "completed_frames", self.requested_frames)
        _fingerprint(self.authority_fingerprint, "authority")
        if self.video.byte_length + self.audio.byte_length > MAX_JOINT_AV_LATENT_PAYLOAD_BYTES:
            raise JointAVLatentError("descriptor_payload_bytes")
        expected = domain_fingerprint(
            IdentityDomain.SEMANTIC, self._wire_without_fingerprint()
        ).digest
        if self.descriptor_fingerprint is None:
            object.__setattr__(self, "descriptor_fingerprint", expected)
        elif self.descriptor_fingerprint != expected:
            raise JointAVLatentError("descriptor_fingerprint_mismatch")

    @property
    def fingerprint(self) -> str:
        if self.descriptor_fingerprint is None:  # pragma: no cover - initialized above
            raise JointAVLatentError("descriptor_fingerprint_uninitialized")
        return self.descriptor_fingerprint

    @property
    def payload_byte_length(self) -> int:
        return self.video.byte_length + self.audio.byte_length

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "video": self.video.to_wire(),
            "audio": self.audio.to_wire(),
            "requested_frames": self.requested_frames,
            "completed_frames": self.completed_frames,
            "authority_fingerprint": self.authority_fingerprint,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["descriptor_fingerprint"] = self.fingerprint
        return value


def build_joint_av_latent_descriptor(
    *,
    authority: JointAVLatentAuthority,
    video_shape: tuple[int, ...],
    video_dtype: str,
    audio_shape: tuple[int, ...],
    audio_dtype: str,
    requested_frames: int,
    completed_frames: int,
) -> JointAVLatentDescriptor:
    """Build a descriptor through the authority; there is no authority-free path."""

    if type(authority) is not JointAVLatentAuthority:
        raise JointAVLatentError("descriptor_requires_authority")

    def _domain(domain: LatentDomain, shape: object, dtype: object) -> LatentDomainDescriptor:
        if type(shape) is not tuple:
            raise JointAVLatentError(f"latent_rank:{domain.value}")
        if dtype not in LATENT_DTYPE_ITEM_SIZES:
            raise JointAVLatentError(f"latent_dtype:{domain.value}")
        element_count = 1
        for dimension in shape:
            if type(dimension) is not int or dimension < 1:
                raise JointAVLatentError(f"latent_rank:{domain.value}")
            element_count *= dimension
        return LatentDomainDescriptor(
            domain=domain,
            shape=shape,
            dtype=dtype,
            byte_length=element_count * LATENT_DTYPE_ITEM_SIZES[dtype],
        )

    return JointAVLatentDescriptor(
        video=_domain(LatentDomain.VIDEO, video_shape, video_dtype),
        audio=_domain(LatentDomain.AUDIO, audio_shape, audio_dtype),
        requested_frames=requested_frames,
        completed_frames=completed_frames,
        authority_fingerprint=authority.fingerprint,
    )


@dataclass(frozen=True, slots=True)
class JointAVLatentReceipt:
    """One immutable locator-free checkpoint lifecycle receipt.

    The descriptor travels inside the receipt wire rather than as a third artifact member: the
    payload's structure truth and its lifecycle truth publish and tamper-fail together.
    """

    artifact_id: str
    state: ArtifactLifecycleState
    descriptor: JointAVLatentDescriptor
    transaction_fingerprint: str
    execution_fingerprint: str
    model_fingerprint: str
    runtime_fingerprint: str
    settings_fingerprint: str
    source_id: str
    predecessor_artifact_fingerprint: str | None
    created_at_ms: int
    expires_at_ms: int
    output_fingerprint: str | None = None
    byte_length: int = 0
    failure_code: str | None = None
    receipt_fingerprint: str | None = None
    artifact_kind: JointAVLatentKind = JointAVLatentKind.JOINT_AV_LATENT
    schema: str = JOINT_AV_LATENT_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != JOINT_AV_LATENT_RECEIPT_SCHEMA:
            raise JointAVLatentError("unsupported_receipt_schema")
        if type(self.artifact_kind) is not JointAVLatentKind:
            raise JointAVLatentError("artifact_kind")
        if type(self.state) is not ArtifactLifecycleState:
            raise JointAVLatentError("artifact_state")
        _identifier(self.artifact_id, "artifact_id")
        if type(self.descriptor) is not JointAVLatentDescriptor:
            raise JointAVLatentError("receipt_descriptor")
        _fingerprint(self.transaction_fingerprint, "transaction")
        _fingerprint(self.execution_fingerprint, "execution")
        _fingerprint(self.model_fingerprint, "model")
        _fingerprint(self.runtime_fingerprint, "runtime")
        _fingerprint(self.settings_fingerprint, "settings")
        _identifier(self.source_id, "source_id")
        _optional_fingerprint(self.predecessor_artifact_fingerprint, "predecessor_artifact")
        _positive_integer(self.created_at_ms, "created_at_ms", 9_999_999_999_999)
        _positive_integer(self.expires_at_ms, "expires_at_ms", 9_999_999_999_999)
        ttl = self.expires_at_ms - self.created_at_ms
        if not 0 < ttl <= MAX_JOINT_AV_LATENT_TTL_MILLISECONDS:
            raise JointAVLatentError("artifact_ttl")
        if (
            type(self.byte_length) is not int
            or not 0 <= self.byte_length <= MAX_JOINT_AV_LATENT_PAYLOAD_BYTES
        ):
            raise JointAVLatentError("artifact_byte_length")
        _optional_fingerprint(self.output_fingerprint, "output")
        if self.failure_code is not None:
            _identifier(self.failure_code, "failure_code")
        self._validate_lifecycle_shape()
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.receipt_fingerprint is None:
            object.__setattr__(self, "receipt_fingerprint", expected)
        elif self.receipt_fingerprint != expected:
            raise JointAVLatentError("receipt_fingerprint_mismatch")
        if len(self.to_wire_bytes()) > MAX_JOINT_AV_LATENT_RECEIPT_BYTES:
            raise JointAVLatentError("receipt_wire_limit")

    def _validate_lifecycle_shape(self) -> None:
        if self.state is ArtifactLifecycleState.COMPLETE:
            if (
                self.output_fingerprint is None
                or self.byte_length != self.descriptor.payload_byte_length
                or self.failure_code is not None
            ):
                raise JointAVLatentError("complete_receipt_shape")
        elif self.state is ArtifactLifecycleState.PARTIAL:
            if (
                self.output_fingerprint is not None
                or self.byte_length != 0
                or self.failure_code is not None
            ):
                raise JointAVLatentError("partial_receipt_shape")
        elif (
            self.output_fingerprint is not None
            or self.byte_length != 0
            or self.failure_code is None
        ):
            raise JointAVLatentError("failed_receipt_shape")

    @property
    def fingerprint(self) -> str:
        if self.receipt_fingerprint is None:  # pragma: no cover - initialized above
            raise JointAVLatentError("receipt_fingerprint_uninitialized")
        return self.receipt_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "artifact_kind": self.artifact_kind.value,
            "artifact_id": self.artifact_id,
            "state": self.state.value,
            "descriptor": self.descriptor.to_wire(),
            "transaction_fingerprint": self.transaction_fingerprint,
            "execution_fingerprint": self.execution_fingerprint,
            "model_fingerprint": self.model_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "settings_fingerprint": self.settings_fingerprint,
            "source_id": self.source_id,
            "predecessor_artifact_fingerprint": self.predecessor_artifact_fingerprint,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "output_fingerprint": self.output_fingerprint,
            "byte_length": self.byte_length,
            "failure_code": self.failure_code,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["receipt_fingerprint"] = self.fingerprint
        return value

    def to_wire_bytes(self) -> bytes:
        return canonical_bytes(self.to_wire())

    def to_public_dict(self) -> dict[str, object]:
        """Return an explicit locator-free allowlist with redacted opaque tokens.

        Shapes and frame counts are structure, not content, and the resume surface needs them;
        every fingerprint leaves as a redacted token, and no root, path, name, prompt or media
        value has a member to hide in.
        """

        return {
            "schema": JOINT_AV_LATENT_PUBLIC_SCHEMA,
            "artifact_kind": self.artifact_kind.value,
            "artifact_id": self.artifact_id,
            "state": self.state.value,
            "video_shape": list(self.descriptor.video.shape),
            "audio_shape": list(self.descriptor.audio.shape),
            "video_dtype": self.descriptor.video.dtype,
            "audio_dtype": self.descriptor.audio.dtype,
            "requested_frames": self.descriptor.requested_frames,
            "completed_frames": self.descriptor.completed_frames,
            "byte_length": self.byte_length,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "receipt_token": redact_segment_artifact_fingerprint(
                self.fingerprint,
                domain="latent_receipt",
            ),
            "output_token": redact_segment_artifact_fingerprint(
                self.output_fingerprint,
                domain="latent_output",
            ),
            "descriptor_token": redact_segment_artifact_fingerprint(
                self.descriptor.fingerprint,
                domain="latent_descriptor",
            ),
        }


def begin_joint_av_latent_receipt(
    *,
    descriptor: JointAVLatentDescriptor,
    authority: JointAVLatentAuthority,
    artifact_id: str,
    transaction_fingerprint: str,
    execution_fingerprint: str,
    model_fingerprint: str,
    runtime_fingerprint: str,
    settings_fingerprint: str,
    source_id: str,
    predecessor_artifact_fingerprint: str | None,
    created_at_ms: int,
    expires_at_ms: int,
) -> JointAVLatentReceipt:
    """Bind one authority-checked descriptor to one partial checkpoint receipt."""

    if type(descriptor) is not JointAVLatentDescriptor:
        raise JointAVLatentError("descriptor_type")
    if type(authority) is not JointAVLatentAuthority:
        raise JointAVLatentError("authority_type")
    if descriptor.authority_fingerprint != authority.fingerprint:
        raise JointAVLatentError("descriptor_authority_mismatch")
    return JointAVLatentReceipt(
        artifact_id=artifact_id,
        state=ArtifactLifecycleState.PARTIAL,
        descriptor=descriptor,
        transaction_fingerprint=transaction_fingerprint,
        execution_fingerprint=execution_fingerprint,
        model_fingerprint=model_fingerprint,
        runtime_fingerprint=runtime_fingerprint,
        settings_fingerprint=settings_fingerprint,
        source_id=source_id,
        predecessor_artifact_fingerprint=predecessor_artifact_fingerprint,
        created_at_ms=created_at_ms,
        expires_at_ms=expires_at_ms,
    )


def complete_joint_av_latent_receipt(
    receipt: JointAVLatentReceipt,
    *,
    output_fingerprint: str,
    byte_length: int,
) -> JointAVLatentReceipt:
    if type(receipt) is not JointAVLatentReceipt:
        raise JointAVLatentError("receipt_type")
    if receipt.state is not ArtifactLifecycleState.PARTIAL:
        raise JointAVLatentError("receipt_not_partial")
    if byte_length != receipt.descriptor.payload_byte_length:
        raise JointAVLatentError("payload_extent_mismatch")
    return replace(
        receipt,
        state=ArtifactLifecycleState.COMPLETE,
        output_fingerprint=output_fingerprint,
        byte_length=byte_length,
        receipt_fingerprint=None,
    )


def fail_joint_av_latent_receipt(
    receipt: JointAVLatentReceipt,
    *,
    failure_code: str,
) -> JointAVLatentReceipt:
    if type(receipt) is not JointAVLatentReceipt:
        raise JointAVLatentError("receipt_type")
    if receipt.state is not ArtifactLifecycleState.PARTIAL:
        raise JointAVLatentError("receipt_not_partial")
    _identifier(failure_code, "failure_code")
    return replace(
        receipt,
        state=ArtifactLifecycleState.FAILED,
        failure_code=failure_code,
        receipt_fingerprint=None,
    )


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise JointAVLatentError("duplicate_receipt_member")
        result[key] = value
    return result


_DESCRIPTOR_KEYS = {
    "schema",
    "video",
    "audio",
    "requested_frames",
    "completed_frames",
    "authority_fingerprint",
    "descriptor_fingerprint",
}

_DOMAIN_KEYS = {"domain", "shape", "dtype", "byte_length"}

_RECEIPT_KEYS = {
    "schema",
    "artifact_kind",
    "artifact_id",
    "state",
    "descriptor",
    "transaction_fingerprint",
    "execution_fingerprint",
    "model_fingerprint",
    "runtime_fingerprint",
    "settings_fingerprint",
    "source_id",
    "predecessor_artifact_fingerprint",
    "created_at_ms",
    "expires_at_ms",
    "output_fingerprint",
    "byte_length",
    "failure_code",
    "receipt_fingerprint",
}


def _decode_domain(value: object, field: str) -> LatentDomainDescriptor:
    if type(value) is not dict or set(value) != _DOMAIN_KEYS:
        raise JointAVLatentError(f"descriptor_wire_members:{field}")
    shape = value["shape"]
    if type(shape) is not list:
        raise JointAVLatentError(f"latent_rank:{field}")
    return LatentDomainDescriptor(
        domain=LatentDomain(value["domain"]),
        shape=tuple(shape),
        dtype=value["dtype"],
        byte_length=value["byte_length"],
    )


def _decode_descriptor(value: object) -> JointAVLatentDescriptor:
    if type(value) is not dict or set(value) != _DESCRIPTOR_KEYS:
        raise JointAVLatentError("descriptor_wire_members")
    return JointAVLatentDescriptor(
        schema=value["schema"],
        video=_decode_domain(value["video"], "video"),
        audio=_decode_domain(value["audio"], "audio"),
        requested_frames=value["requested_frames"],
        completed_frames=value["completed_frames"],
        authority_fingerprint=value["authority_fingerprint"],
        descriptor_fingerprint=value["descriptor_fingerprint"],
    )


def decode_joint_av_latent_receipt(payload: bytes) -> JointAVLatentReceipt:
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_JOINT_AV_LATENT_RECEIPT_BYTES:
        raise JointAVLatentError("receipt_wire_limit")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_members,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JointAVLatentError("invalid_receipt_json") from exc
    if type(value) is not dict:
        raise JointAVLatentError("receipt_wire_type")
    if set(value) != _RECEIPT_KEYS:
        raise JointAVLatentError("receipt_wire_members")
    try:
        return JointAVLatentReceipt(
            schema=value["schema"],
            artifact_kind=JointAVLatentKind(value["artifact_kind"]),
            artifact_id=value["artifact_id"],
            state=ArtifactLifecycleState(value["state"]),
            descriptor=_decode_descriptor(value["descriptor"]),
            transaction_fingerprint=value["transaction_fingerprint"],
            execution_fingerprint=value["execution_fingerprint"],
            model_fingerprint=value["model_fingerprint"],
            runtime_fingerprint=value["runtime_fingerprint"],
            settings_fingerprint=value["settings_fingerprint"],
            source_id=value["source_id"],
            predecessor_artifact_fingerprint=value["predecessor_artifact_fingerprint"],
            created_at_ms=value["created_at_ms"],
            expires_at_ms=value["expires_at_ms"],
            output_fingerprint=value["output_fingerprint"],
            byte_length=value["byte_length"],
            failure_code=value["failure_code"],
            receipt_fingerprint=value["receipt_fingerprint"],
        )
    except (ValueError, TypeError) as exc:
        if isinstance(exc, JointAVLatentError):
            raise
        raise JointAVLatentError("receipt_wire_value") from exc


__all__ = [
    "AUDIO_LATENT_CHANNELS",
    "AUDIO_LATENT_PLANES",
    "AUDIO_LATENT_RANK",
    "JOINT_AV_LATENT_AUTHORITY_SCHEMA",
    "JOINT_AV_LATENT_CAPABILITY",
    "JOINT_AV_LATENT_DESCRIPTOR_SCHEMA",
    "JOINT_AV_LATENT_PUBLIC_SCHEMA",
    "JOINT_AV_LATENT_RECEIPT_SCHEMA",
    "JointAVLatentAuthority",
    "JointAVLatentDescriptor",
    "JointAVLatentError",
    "JointAVLatentKind",
    "JointAVLatentReceipt",
    "LATENT_DTYPE_ITEM_SIZES",
    "LatentDomain",
    "LatentDomainDescriptor",
    "MAX_JOINT_AV_LATENT_DIMENSION",
    "MAX_JOINT_AV_LATENT_PAYLOAD_BYTES",
    "MAX_JOINT_AV_LATENT_RECEIPT_BYTES",
    "MAX_JOINT_AV_LATENT_TTL_MILLISECONDS",
    "VIDEO_LATENT_CHANNELS",
    "VIDEO_LATENT_RANK",
    "begin_joint_av_latent_receipt",
    "build_joint_av_latent_authority",
    "build_joint_av_latent_descriptor",
    "complete_joint_av_latent_receipt",
    "decode_joint_av_latent_receipt",
    "fail_joint_av_latent_receipt",
]
