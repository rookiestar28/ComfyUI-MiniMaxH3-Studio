"""Workspace-minted derivative claims over the existing original-source authority."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, SupportsIndex, cast

from ..core.authoring_asset_manifest import validate_nle_authoring_asset_lease_request
from ..core.authoring_media import (
    ASSET_PREPARATION_KINDS,
    DERIVATIVE_PROFILE_ID,
    NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
    AudioDisposition,
    CreateLeaseRequest,
    MediaGeometry,
    MediaLeaseError,
    VerifiedDerivative,
    derivative_byte_limit,
    public_runtime_asset_wire,
    validate_lease_snapshot,
)
from ..core.canonical import canonical_fingerprint
from ..core.composition_contract import (
    DERIVATIVE_MANIFEST_SCHEMA,
    PRIVATE_SOURCE_MANIFEST_SCHEMA,
    DerivativeManifest,
    PrivateSourceManifest,
    PublicAsset,
    PublicCompositionSnapshot,
    composition_contract_fingerprint,
)
from ..core.nle_authoring_contract import NleAuthoringState
from .authoring_derivative_generator import (
    AuthoringDerivativeGeneratorError,
    GeneratedAudioPeaksBody,
    GeneratedAudioPreviewBody,
    GeneratedDerivativeBody,
)
from .authoring_fonts import AuthoringFontError, load_packaged_font_manifest
from .authoring_generated_source import GeneratedAuthoringVideoSource
from .authoring_image_source import OwnedImageSource
from .authoring_render_source import PreparedAuthoringHistory
from .authoring_source_binding import (
    AuthoringSourceBindingError,
    AuthoringSourceBindingReceipt,
    _PathBackedAuthoringVideoSource,
    claim_transferred_authoring_source,
)
from .authoring_video_facts import AuthoringVideoFactsError

if TYPE_CHECKING:
    from .authoring_derivative_generator import AuthoringDerivativeGenerator
    from .authoring_video_facts import AuthoringVideoFacts
    from .av_reconstruction_media import QualifiedAVMediaAdapter
    from .media_subprocess import CancellationProbe


def _packaged_font_is_resident() -> bool:
    # A packaged face has no owner to end: its file is part of the installation, and its cache key
    # already names the manifest, the package and the face it was read from.
    return True


class AuthoringDerivativeClaim:
    """Only the Authoring application service supplies the exact history and binding objects."""

    def __init__(
        self,
        request: CreateLeaseRequest,
        snapshot: PublicCompositionSnapshot | None,
        history: PreparedAuthoringHistory,
        binding: AuthoringSourceBindingReceipt | None,
        workspace_is_current: Callable[[], bool],
        generator: AuthoringDerivativeGenerator | None,
        media_adapter: QualifiedAVMediaAdapter | None,
        *,
        authoring_state: NleAuthoringState | None = None,
    ) -> None:
        if type(history) is not PreparedAuthoringHistory:
            raise MediaLeaseError("authority_mismatch")
        self._asset: PublicAsset
        if request.request_schema == NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA:
            if snapshot is not None or type(authoring_state) is not NleAuthoringState:
                raise MediaLeaseError("authority_mismatch")
            self._asset = validate_nle_authoring_asset_lease_request(request, authoring_state)
        else:
            if type(snapshot) is not PublicCompositionSnapshot or authoring_state is not None:
                raise MediaLeaseError("authority_mismatch")
            self._asset = validate_lease_snapshot(request, snapshot)
        self._request = request
        self._snapshot = snapshot
        self._history = history
        self._binding = binding
        self._workspace_is_current = workspace_is_current
        self._generator = generator
        self._media_adapter = media_adapter
        self._invalid = False
        self._content_disproved = False
        self._original = next(
            (item for item in history.sources if item.asset.asset_id == request.asset_id), None
        )
        self._source: object | None = None
        self._font = None
        self._audio: AudioDisposition = "absent"
        clip = (
            None
            if snapshot is None
            else next((item for item in snapshot.clips if item.clip_id == request.clip_id), None)
        )
        track = (
            None
            if snapshot is None or clip is None
            else next(item for item in snapshot.tracks if item.track_id == clip.track_id)
        )
        if self._asset.kind == "font":
            if clip is None or clip.text is None:
                raise MediaLeaseError("authority_mismatch")
            self._font = next(
                (
                    face
                    for face in history.fonts.faces
                    if face.font_asset_id == self._asset.asset_id
                    and face.weight == clip.text.weight
                    and face.style == clip.text.style
                ),
                None,
            )
            if self._font is None or not self._font.covers(clip.text.content):
                raise MediaLeaseError("unsupported")
            source_fingerprint = self._font.file_fingerprint
            generation = history.generation or 1
            generator_fingerprint = canonical_fingerprint(
                {
                    "profile": "h3.authoring.packaged_ttf_identity.v1",
                    "fontManifest": history.fonts.manifest_fingerprint,
                    "fontPackage": history.fonts.package_fingerprint,
                    "fontFace": self._font.file_fingerprint,
                }
            )
        else:
            if self._original is None or binding is None or self._original.asset != self._asset:
                raise MediaLeaseError("authority_mismatch")
            try:
                self._source = claim_transferred_authoring_source(binding, request.asset_id)
            except AuthoringSourceBindingError as exc:
                # IMPORTANT (B-M2522-REVOKE-02): a released or replaced source (a Production
                # original after its workspace release) is refused here, before the currentness
                # check below. Letting it escape turns the route answer into `generation_failed`,
                # which the browser treats as a generation fault instead of a revoked source.
                if exc.code in {
                    "source_stale",
                    "source_replaced",
                    "receipt_released",
                    "generation_mismatch",
                }:
                    raise MediaLeaseError("stale") from None
                raise
            # CRITICAL: only exact transferred factory types carry source authority. Generated
            # originals retain Production revocation; arbitrary path-shaped wrappers must fail.
            if type(self._source) not in (
                OwnedImageSource,
                _PathBackedAuthoringVideoSource,
                GeneratedAuthoringVideoSource,
            ):
                raise MediaLeaseError("unsupported")
            if self._original.generation != binding.generation:
                raise MediaLeaseError("stale")
            source_fingerprint = self._original.source_fingerprint
            generation = self._original.generation
            if request.derivative_kind == "frame_timing_index":
                generator_fingerprint = canonical_fingerprint(
                    {"profile": "h3.authoring.frame_timing_index.v1"}
                )
            else:
                if generator is None:
                    raise MediaLeaseError("unsupported")
                generator_fingerprint = generator.profile_fingerprint
            if request.derivative_kind in ASSET_PREPARATION_KINDS:
                if clip is None or track is None:
                    # IMPORTANT: a playback body without a clip is a preparation, and only the
                    # authoring-bound request may ask for one. It is generated with the asset's
                    # own audio disposition, which is what an enabled clip on the enabled primary
                    # track gets; an overlay policy needs a clip and is never assumed here.
                    if request.request_schema != NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA:
                        raise MediaLeaseError("authority_mismatch")
                    self._audio = cast(AudioDisposition, self._asset.embedded_audio)
                elif self._asset.embedded_audio == "present_bound":
                    self._audio = (
                        "present_bound"
                        if track.kind == "primary_video" and track.enabled and clip.enabled
                        else "excluded_overlay_policy"
                    )
                else:
                    self._audio = cast(AudioDisposition, self._asset.embedded_audio)
                if request.derivative_kind == "audio_preview" and self._audio != "present_bound":
                    raise MediaLeaseError("unsupported")
            elif request.derivative_kind == "audio_peaks":
                # IMPORTANT: peaks are an asset decoration for already-bound embedded audio.
                # Do not reuse clip overlay policy or turn absent audio into generated silence.
                if self._asset.embedded_audio != "present_bound":
                    raise MediaLeaseError("unsupported")
                self._audio = "present_bound"
        self._source_manifest = PrivateSourceManifest(
            PRIVATE_SOURCE_MANIFEST_SCHEMA,
            self._asset.asset_id,
            generation,
            source_fingerprint,
            "derivative-source-" + source_fingerprint[-24:],
        )
        self._generator_fingerprint = generator_fingerprint
        # The full asset can contain 512 timing rows; the generic 256-row hash rejects it.
        self._asset_fingerprint = composition_contract_fingerprint(
            public_runtime_asset_wire(self._asset)
        )
        key: dict[str, object] = {
            "source": source_fingerprint,
            "asset": self._asset_fingerprint,
            "kind": request.derivative_kind,
            "profile": generator_fingerprint,
            "span": [request.source_start_frame, request.source_end_frame],
            "audio": self._audio,
        }
        # CRITICAL: a playback body is made the same way whether it was asked for with a clip
        # or prepared for the asset, so one body serves both and its key must not name the
        # scope. With the scope in the key a prepared body is never found by the clip that
        # needs it and preparation only adds work. The other kinds keep the scope: an asset
        # thumbnail and a clip thumbnail differ.
        # The key names how a body is made, not its bytes: a proxy generated again under the
        # same key can differ from the one before, so the key is never a body's identity.
        if request.derivative_kind not in ASSET_PREPARATION_KINDS:
            key["scope"] = request.scope
        self._cache_key = canonical_fingerprint(key)
        if not self.current():
            raise MediaLeaseError("stale")

    @property
    def cache_key(self) -> str:
        return self._cache_key

    def retention_probe(self) -> Callable[[], bool] | None:
        """How long a body generated for this claim's cache key may outlive its leases.

        CRITICAL: the probe is the exact source object's own liveness and nothing else. It must
        not close over this claim, its snapshot or its workspace check: those end with every
        accepted edit, which is exactly when the retained body is needed, and a probe that held
        them would also keep a whole superseded snapshot alive for the retention period. A source
        ends when its workspace or its Production authority releases it or when its file identity
        changes; a content mismatch found by `confirm` is reported here as "do not retain".
        """

        if self._content_disproved:
            return None
        source = self._source
        if type(source) is OwnedImageSource:
            return source.current
        if type(source) is _PathBackedAuthoringVideoSource:
            return source.current
        if type(source) is GeneratedAuthoringVideoSource:
            return source.current
        return _packaged_font_is_resident if self._font is not None else None

    def __repr__(self) -> str:
        return "<AuthoringDerivativeClaim opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("derivative source claims are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("derivative source claims are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("derivative source claims are not serializable")

    def current(self) -> bool:
        try:
            if self._invalid or not self._workspace_is_current() or not self._history.current():
                return False
            if self._source is not None:
                if self._binding is None or self._original is None or not self._original.current():
                    return False
                return (
                    claim_transferred_authoring_source(self._binding, self._asset.asset_id)
                    is self._source
                )
            return self._font is not None
        except Exception:
            return False

    def confirm(self, deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise MediaLeaseError("timeout")
        try:
            if not self.current():
                raise MediaLeaseError("stale")
            if self._original is not None:
                self._original.verify_currentness(deadline)
            elif self._font is not None:
                current_fonts = load_packaged_font_manifest()
                if current_fonts != self._history.fonts:
                    raise MediaLeaseError("stale")
                self._font.read_verified_bytes()
            # CRITICAL: workspace/history and exact factory identity must still agree after
            # hashing. A valid old source digest cannot authorize a new semantic snapshot.
            if not self.current():
                raise MediaLeaseError("stale")
            if time.monotonic() >= deadline:
                raise MediaLeaseError("timeout")
        except Exception as exc:
            if isinstance(exc, MediaLeaseError):
                self._invalid = exc.reason not in {"timeout", "cancelled", "busy"}
                raise
            if isinstance(exc, AuthoringSourceBindingError):
                if exc.code in {"source_timeout", "source_deadline_invalid"}:
                    raise MediaLeaseError("timeout") from None
                if exc.code == "source_cancelled":
                    raise MediaLeaseError("cancelled") from None
            # A workspace that moved on answers `stale` above and leaves the source's content
            # proven. Only a failed content proof -- a hash that differs, a font that no longer
            # matches its manifest -- withdraws retention of the bytes derived from it.
            if isinstance(exc, AuthoringFontError) or (
                isinstance(exc, AuthoringSourceBindingError) and exc.code == "source_replaced"
            ):
                self._content_disproved = True
            self._invalid = True
            raise MediaLeaseError("stale") from None

    def _video_facts(
        self, deadline: float, cancellation: object | None
    ) -> tuple[Path, AuthoringVideoFacts]:
        from .authoring_video_facts import AuthoringVideoFacts, probe_authoring_video_facts

        source = self._source
        path = (
            source._path
            if type(source) is _PathBackedAuthoringVideoSource
            else source.lease.path
            if type(source) is GeneratedAuthoringVideoSource
            else None
        )
        if path is None or self._media_adapter is None:
            raise MediaLeaseError("unsupported")
        # The facts were probed for exactly the content this claim's `confirm` re-hashes before
        # and after generation, and the generator hashes the bytes it stages against the same
        # fingerprint, so they are reused instead of probing the unchanged source for every
        # derivative. Do not reuse facts that are not tied to `source_fingerprint`.
        held = None if self._original is None else self._original._video_facts
        if type(held) is AuthoringVideoFacts:
            facts = held
        else:
            # This private adapter bridge receives only the exact workspace-transferred factory
            # object, never a route path. Preserve that identity gate when adding source origins.
            facts = probe_authoring_video_facts(
                path,
                self._media_adapter,
                deadline,
                cast("CancellationProbe | None", cancellation),
            )
        if (
            facts.content_fingerprint != self._source_manifest.source_fingerprint
            or self._asset.source_time_base is None
            or facts.source_time_base.to_wire() != self._asset.source_time_base.to_wire()
            or facts.frame_count != self._asset.source_frame_count
            or [row.to_wire() for row in facts.landmarks]
            != [row.to_wire() for row in self._asset.landmarks]
        ):
            raise MediaLeaseError("stale")
        return path, facts

    def generate(
        self, deadline: float, cancellation: object | None
    ) -> tuple[bytearray, VerifiedDerivative]:
        self.confirm(deadline)
        body: bytearray | None = None
        geometry: MediaGeometry | None = None
        generator_fingerprint = self._generator_fingerprint
        try:
            kind = self._request.derivative_kind
            if kind == "packaged_font_face":
                if self._font is None:
                    raise MediaLeaseError("unsupported")
                body = bytearray(self._font.read_verified_bytes())
            elif kind == "frame_timing_index":
                # Complete typed timing can exceed 256 rows. Keep every row and apply the
                # independent 16 KiB body limit below; generic canonicalization masks that refusal.
                body = bytearray(
                    json.dumps(
                        {
                            "schema": "h3.authoring.frame_timing_index.v1",
                            "assetId": self._asset.asset_id,
                            "assetFingerprint": self._asset_fingerprint,
                            "timing": public_runtime_asset_wire(self._asset),
                            "sourceToProxy": "rational_pts_duration_identity_required_v1",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                )
            else:
                generator = self._generator
                if generator is None:
                    raise MediaLeaseError("unsupported")
                result: (
                    GeneratedDerivativeBody | GeneratedAudioPeaksBody | GeneratedAudioPreviewBody
                )
                source = self._source
                if type(source) is OwnedImageSource:
                    source_width = source.width
                    source_height = source.height
                    method = (
                        generator.generate_image_proxy
                        if kind == "image_proxy"
                        else generator.generate_image_thumbnail
                    )
                    result = method(
                        source,
                        expected_source_fingerprint=self._source_manifest.source_fingerprint,
                        deadline=deadline,
                        cancellation=cast("CancellationProbe | None", cancellation),
                    )
                else:
                    video_path, facts = self._video_facts(deadline, cancellation)
                    source_width = facts.width
                    source_height = facts.height
                    if kind == "video_proxy":
                        result = generator.generate_video_proxy(
                            video_path,
                            facts,
                            expected_source_fingerprint=self._source_manifest.source_fingerprint,
                            include_embedded_audio=self._audio == "present_bound",
                            deadline=deadline,
                            cancellation=cast("CancellationProbe | None", cancellation),
                        )
                    elif kind == "audio_preview":
                        result = generator.generate_video_audio_preview(
                            video_path,
                            facts,
                            expected_source_fingerprint=self._source_manifest.source_fingerprint,
                            deadline=deadline,
                            cancellation=cast("CancellationProbe | None", cancellation),
                        )
                    elif kind == "thumbnail":
                        result = generator.generate_video_thumbnail(
                            video_path,
                            facts,
                            expected_source_fingerprint=self._source_manifest.source_fingerprint,
                            source_frame=self._request.source_start_frame,
                            deadline=deadline,
                            cancellation=cast("CancellationProbe | None", cancellation),
                        )
                    elif kind == "filmstrip":
                        result = generator.generate_video_filmstrip(
                            video_path,
                            facts,
                            expected_source_fingerprint=self._source_manifest.source_fingerprint,
                            deadline=deadline,
                            cancellation=cast("CancellationProbe | None", cancellation),
                        )
                    elif kind == "audio_peaks":
                        result = generator.generate_video_audio_peaks(
                            video_path,
                            facts,
                            expected_source_fingerprint=self._source_manifest.source_fingerprint,
                            deadline=deadline,
                            cancellation=cast("CancellationProbe | None", cancellation),
                        )
                    else:
                        raise MediaLeaseError("unsupported")
                try:
                    expected_audio = (
                        "not_applicable"
                        if type(source) is OwnedImageSource
                        else "present_bound"
                        if self._audio == "present_bound"
                        else "absent"
                    )
                    if result.audio_disposition != expected_audio:
                        raise MediaLeaseError("generation_failed")
                    if kind == "audio_peaks":
                        if not isinstance(result, GeneratedAudioPeaksBody):
                            raise MediaLeaseError("generation_failed")
                    elif kind == "audio_preview":
                        if not isinstance(result, GeneratedAudioPreviewBody):
                            raise MediaLeaseError("generation_failed")
                    else:
                        if not isinstance(result, GeneratedDerivativeBody):
                            raise MediaLeaseError("generation_failed")
                        # IMPORTANT: capture verified dimensions before take/clear transfers and
                        # invalidates the generated result; preview geometry describes visual bytes.
                        geometry = MediaGeometry(
                            source_width,
                            source_height,
                            result.width,
                            result.height,
                        )
                    generator_fingerprint = result.generator_fingerprint
                    body = result.take()
                finally:
                    result.clear()
            if not 1 <= len(body) <= derivative_byte_limit(kind):
                raise MediaLeaseError("resource_limit")
            self.confirm(deadline)
            derivative = DerivativeManifest(
                DERIVATIVE_MANIFEST_SCHEMA,
                self._asset.asset_id,
                self._source_manifest.source_fingerprint,
                DERIVATIVE_PROFILE_ID,
                "sha256:" + hashlib.sha256(body).hexdigest(),
                self._source_manifest.generation,
            )
            verified = VerifiedDerivative(
                self._source_manifest,
                derivative,
                kind,
                self._asset_fingerprint,
                generator_fingerprint,
                len(body),
                self._audio,
                geometry,
            )
            completed, body = body, None
            return completed, verified
        except AuthoringVideoFactsError as exc:
            if exc.code in {"cancelled", "source_cancelled"}:
                raise MediaLeaseError("cancelled") from None
            if exc.code in {"preview_deadline", "process_timeout", "source_timeout"}:
                raise MediaLeaseError("timeout") from None
            if exc.code in {"source_stale", "source_replaced"}:
                raise MediaLeaseError("stale") from None
            if exc.code == "resource_limit":
                raise MediaLeaseError("resource_limit") from None
            raise MediaLeaseError("unsupported") from None
        except AuthoringDerivativeGeneratorError as exc:
            # CRITICAL: preserve closed native outcomes across this adapter boundary. Collapsing
            # cancellation/timeout/busy into generation failure breaks owner cleanup and retry.
            raise MediaLeaseError(exc.code) from None
        except MediaLeaseError:
            raise
        except Exception:
            raise MediaLeaseError("generation_failed") from None
        finally:
            if body is not None:
                body.clear()
