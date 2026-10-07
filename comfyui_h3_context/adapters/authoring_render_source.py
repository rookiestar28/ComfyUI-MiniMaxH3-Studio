"""Private source-origin claims and source-authoritative Authoring initialization."""

from __future__ import annotations

import hashlib
import math
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, NoReturn, SupportsIndex, cast

from ..core.canonical import canonical_fingerprint
from ..core.composition_contract import (
    PublicAsset,
    PublicCompositionSnapshot,
    Rational,
    TimingLandmark,
    composition_contract_fingerprint,
)
from ..core.nle_authoring_contract import (
    NleAuthoringState,
    adapt_legacy_authoring_projection_to_nle_state,
    materialize_render_snapshot,
)
from ..core.render_planner import (
    FONT_FACTS_SCHEMA,
    PRIVATE_SOURCE_FACTS_MANIFEST_SCHEMA,
    SOURCE_CURRENTNESS_SCHEMA,
    FontTitleBindingFact,
    PackagedFontFacts,
    PrivateSourceFact,
    PrivateSourceFactsManifest,
    RenderPlanV1,
    SourceCurrentnessConfirmation,
    SourceCurrentnessToken,
    packaged_font_facts_fingerprint,
    plan_render,
    private_source_manifest_fingerprint,
    revalidate_render_plan_currentness,
    source_facts_fingerprint,
)
from .authoring_fonts import (
    AuthoringFontError,
    PackagedFontManifest,
    load_packaged_font_manifest,
    require_text_coverage,
)
from .authoring_image_source import IMAGE_SOURCE_PROFILE, OwnedImageSource
from .authoring_source_binding import (
    AuthoringSourceBindingError,
    AuthoringSourceBindingReceipt,
    _PathBackedAuthoringVideoSource,
    claim_transferred_authoring_source,
)

if TYPE_CHECKING:
    from .authoring_generated_source import GeneratedAuthoringVideoSource
    from .authoring_video_facts import AuthoringVideoFacts
    from .retained_asset_use import RetainedVideoSource


class SourceOrigin(str, Enum):
    CONTEXT_VIDEO = "context_video"
    RUNTIME_IMAGE = "runtime_image"
    GENERATED = "generated"


class _OpaqueClaim:
    __slots__ = ()

    def __copy__(self) -> NoReturn:
        raise TypeError("private source claims are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("private source claims are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("private source claims are not serializable")


def _require_verification_deadline(deadline: float) -> None:
    if type(deadline) is not float or not math.isfinite(deadline):
        raise AuthoringSourceBindingError("source_deadline_invalid")
    if time.monotonic() >= deadline:
        raise AuthoringSourceBindingError("source_timeout")


def _read_image_source_fingerprint(source: OwnedImageSource, deadline: float) -> str:
    _require_verification_deadline(deadline)
    pixels = source.read_bytes()
    digest = hashlib.sha256()
    digest.update(f"{IMAGE_SOURCE_PROFILE}:{source.width}:{source.height}:".encode("ascii"))
    digest.update(pixels)
    _require_verification_deadline(deadline)
    return "sha256:" + digest.hexdigest()


def _read_path_source_fingerprint(
    source: _PathBackedAuthoringVideoSource,
    deadline: float,
) -> str:
    from .av_reconstruction_media import AVMediaAdapterError, _read_regular_media_body

    _require_verification_deadline(deadline)
    if not source.current() or source._path is None:
        raise AuthoringSourceBindingError("source_stale")
    body: bytearray | None = None
    try:
        body, fingerprint = _read_regular_media_body(
            source._path,
            source._maximum_bytes,
            deadline=deadline,
        )
        _require_verification_deadline(deadline)
        return fingerprint
    except AVMediaAdapterError as exc:
        if exc.code in {"preview_deadline", "preview_timeout", "process_timeout"}:
            raise AuthoringSourceBindingError("source_timeout") from None
        raise AuthoringSourceBindingError("source_stale") from None
    finally:
        if body is not None:
            body.clear()


def _read_generated_source_fingerprint(
    source: GeneratedAuthoringVideoSource | RetainedVideoSource, deadline: float
) -> str:
    from .authoring_generated_source import GeneratedAuthoringVideoSource
    from .av_reconstruction_media import AVMediaAdapterError, _read_regular_media_body

    _require_verification_deadline(deadline)
    # A confirmation proves the stored artifact's content here, exactly once; every other
    # currentness question about this source is answered from the identity this records.
    from .retained_asset_use import RetainedVideoSource

    if (
        type(source) not in {GeneratedAuthoringVideoSource, RetainedVideoSource}
        or not source.verify_artifact()
    ):
        raise AuthoringSourceBindingError("source_stale")
    body: bytearray | None = None
    try:
        body, fingerprint = _read_regular_media_body(
            source.lease.path,
            # The admitted lease may be a color/timestamp-normalized copy; its own
            # probed byte bound guards the read while Production retains the raw receipt.
            source.facts.byte_length,
            deadline=deadline,
        )
        _require_verification_deadline(deadline)
        return fingerprint
    except AVMediaAdapterError as exc:
        if exc.code in {"preview_deadline", "preview_timeout", "process_timeout"}:
            raise AuthoringSourceBindingError("source_timeout") from None
        raise AuthoringSourceBindingError("source_stale") from None
    finally:
        if body is not None:
            body.clear()


@dataclass(frozen=True, slots=True, repr=False)
class _AuthoringSourceVerification(_OpaqueClaim):
    """One factory-minted byte verifier with no origin visible to downstream consumers."""

    expected_fingerprint: str
    _current: Callable[[], bool] = field(repr=False)
    _read_fingerprint: Callable[[float], str] = field(repr=False)

    def current(self) -> bool:
        return self._current()

    def confirm(self, deadline: float) -> None:
        _require_verification_deadline(deadline)
        if not self.current():
            raise AuthoringSourceBindingError("source_stale")
        observed = self._read_fingerprint(deadline)
        # CRITICAL: stat identity alone cannot prove current bytes. Recheck the factory authority
        # after the bounded read, then require its captured content hash before plan handoff.
        if not self.current():
            raise AuthoringSourceBindingError("source_stale")
        _require_verification_deadline(deadline)
        if observed != self.expected_fingerprint:
            raise AuthoringSourceBindingError("source_replaced")


def _mint_source_verification(
    source: object,
    expected_fingerprint: str,
) -> _AuthoringSourceVerification:
    if type(source) is OwnedImageSource:
        return _AuthoringSourceVerification(
            expected_fingerprint,
            source.current,
            lambda deadline: _read_image_source_fingerprint(source, deadline),
        )
    if type(source) is _PathBackedAuthoringVideoSource:
        return _AuthoringSourceVerification(
            expected_fingerprint,
            source.current,
            lambda deadline: _read_path_source_fingerprint(source, deadline),
        )
    from .authoring_generated_source import GeneratedAuthoringVideoSource
    from .retained_asset_use import RetainedVideoSource

    if type(source) in {GeneratedAuthoringVideoSource, RetainedVideoSource}:
        owned = cast("GeneratedAuthoringVideoSource | RetainedVideoSource", source)
        return _AuthoringSourceVerification(
            expected_fingerprint,
            owned.current,
            lambda deadline: _read_generated_source_fingerprint(owned, deadline),
        )
    raise AuthoringSourceBindingError("source_origin_unregistered")


@dataclass(frozen=True, slots=True, repr=False)
class AuthoringRenderSourceClaim(_OpaqueClaim):
    """An origin-neutral consumer contract minted only by the registered factories below."""

    asset: PublicAsset
    origin: SourceOrigin
    generation: int
    source_fingerprint: str
    source_facts_fingerprint: str
    source_profile_fingerprint: str
    color_facts_fingerprint: str
    currentness_token: str
    byte_count: int
    width: int
    height: int
    duration_milliseconds: int | None
    _verification: _AuthoringSourceVerification = field(repr=False)
    _receipt: AuthoringSourceBindingReceipt = field(repr=False)
    # The facts probed for exactly `source_fingerprint`; a VIDEO origin keeps them so that a
    # derivative of the same verified content does not probe the source again.
    _video_facts: AuthoringVideoFacts | None = field(default=None, repr=False)

    def current(self) -> bool:
        return (
            not self._receipt.released
            and self._receipt.generation == self.generation
            and self._verification.current()
        )

    def verify_currentness(self, deadline: float) -> None:
        if not self.current():
            raise AuthoringSourceBindingError("source_stale")
        self._verification.confirm(deadline)
        if not self.current():
            raise AuthoringSourceBindingError("source_stale")

    def __repr__(self) -> str:
        return "<AuthoringRenderSourceClaim opaque>"


def claim_render_source(
    receipt: AuthoringSourceBindingReceipt,
    source_id: str,
) -> AuthoringRenderSourceClaim:
    source = claim_transferred_authoring_source(receipt, source_id)
    token = "source-" + secrets.token_hex(16)
    if type(source) is OwnedImageSource:
        asset = PublicAsset(source_id, "image", None, None, None, "absent", "not_applicable", ())
        profile_fp = canonical_fingerprint(
            {"profile": IMAGE_SOURCE_PROFILE, "asset": asset.to_wire()}
        )
        color_fp = canonical_fingerprint({"color": "unspecified_rgb_float32_v1"})
        byte_count = source.width * source.height * 12
        source_fingerprint = "sha256:" + source.fingerprint
        facts = source_facts_fingerprint(
            asset_id=source_id,
            source_id=source_id,
            origin=SourceOrigin.RUNTIME_IMAGE.value,
            width=source.width,
            height=source.height,
            size_bytes=byte_count,
            duration_milliseconds=None,
            source_profile_fingerprint=profile_fp,
            color_facts_fingerprint=color_fp,
        )
        result = AuthoringRenderSourceClaim(
            asset,
            SourceOrigin.RUNTIME_IMAGE,
            receipt.generation,
            source_fingerprint,
            facts,
            profile_fp,
            color_fp,
            token,
            byte_count,
            source.width,
            source.height,
            None,
            _mint_source_verification(source, source_fingerprint),
            receipt,
        )
    elif type(source) is _PathBackedAuthoringVideoSource:
        from .authoring_video_facts import AuthoringVideoFactsError, probe_authoring_video_facts
        from .av_reconstruction_media import QualifiedAVMediaAdapter
        from .comfyui_authoring_media_preview import current_authoring_media_preview_adapter
        from .comfyui_media_runtime import media_runtime_lease
        from .media_runtime_manager import MediaRuntimeBusy

        try:
            with media_runtime_lease():
                adapter = current_authoring_media_preview_adapter()
                if type(adapter) is not QualifiedAVMediaAdapter:
                    raise AuthoringSourceBindingError("adapter_unavailable")
                if not source.current() or source._path is None:
                    raise AuthoringSourceBindingError("source_stale")
                facts_value = probe_authoring_video_facts(
                    source._path, adapter, time.monotonic() + 30
                )
        except AuthoringVideoFactsError as exc:
            raise AuthoringSourceBindingError(exc.code) from None
        except MediaRuntimeBusy:
            raise AuthoringSourceBindingError("adapter_unavailable") from None
        audio = facts_value.embedded_audio
        asset = PublicAsset(
            source_id,
            "video",
            Rational(facts_value.source_time_base.num, facts_value.source_time_base.den),
            facts_value.frame_count,
            audio.canonical_sample_count,
            audio.disposition,
            "nonnegative_monotonic_v1",
            tuple(
                TimingLandmark(row.frame_index, row.pts, row.dts, row.duration_ticks)
                for row in facts_value.landmarks
            ),
        )
        last = asset.landmarks[-1]
        time_base = facts_value.source_time_base
        duration_ms = (
            (last.pts + last.duration_ticks) * time_base.num * 1000 + time_base.den - 1
        ) // time_base.den
        # CRITICAL: path-backed and generated videos share the admitted 512 timing rows.
        # A generic 256-row hash rejects the former before preview/render can claim it.
        profile_fp = composition_contract_fingerprint(
            {
                "profile": facts_value.schema_version,
                "asset": asset.to_wire(),
                "pixel_format": facts_value.pixel_format,
                "pixel_aspect": (
                    None
                    if facts_value.pixel_aspect_ratio is None
                    else facts_value.pixel_aspect_ratio.to_wire()
                ),
            }
        )
        color_fp = canonical_fingerprint(
            {
                "range": facts_value.color_range,
                "space": facts_value.color_space,
                "primaries": facts_value.color_primaries,
                "transfer": facts_value.color_transfer,
            }
        )
        facts = source_facts_fingerprint(
            asset_id=source_id,
            source_id=source_id,
            origin=SourceOrigin.CONTEXT_VIDEO.value,
            width=facts_value.width,
            height=facts_value.height,
            size_bytes=facts_value.byte_length,
            duration_milliseconds=duration_ms,
            source_profile_fingerprint=profile_fp,
            color_facts_fingerprint=color_fp,
        )
        result = AuthoringRenderSourceClaim(
            asset,
            SourceOrigin.CONTEXT_VIDEO,
            receipt.generation,
            facts_value.content_fingerprint,
            facts,
            profile_fp,
            color_fp,
            token,
            facts_value.byte_length,
            facts_value.width,
            facts_value.height,
            duration_ms,
            _mint_source_verification(source, facts_value.content_fingerprint),
            receipt,
            facts_value,
        )
    else:
        from .authoring_generated_source import GeneratedAuthoringVideoSource
        from .retained_asset_use import RetainedVideoSource

        if type(source) not in {GeneratedAuthoringVideoSource, RetainedVideoSource}:
            # CRITICAL: only registered generated/retained factories can claim managed video.
            # A file's PublicAsset or a path-shaped wrapper is never a source authority.
            raise AuthoringSourceBindingError("source_origin_unregistered")
        owned = cast("GeneratedAuthoringVideoSource | RetainedVideoSource", source)
        facts_value = owned.facts
        asset = owned.public_asset(source_id)
        duration_ms = owned.duration_milliseconds
        profile_fp = owned.source_profile_fingerprint(source_id)
        color_fp = owned.color_facts_fingerprint()
        facts = source_facts_fingerprint(
            asset_id=source_id,
            source_id=source_id,
            origin=SourceOrigin.GENERATED.value,
            width=facts_value.width,
            height=facts_value.height,
            size_bytes=facts_value.byte_length,
            duration_milliseconds=duration_ms,
            source_profile_fingerprint=profile_fp,
            color_facts_fingerprint=color_fp,
        )
        result = AuthoringRenderSourceClaim(
            asset,
            SourceOrigin.GENERATED,
            receipt.generation,
            facts_value.content_fingerprint,
            facts,
            profile_fp,
            color_fp,
            token,
            facts_value.byte_length,
            facts_value.width,
            facts_value.height,
            duration_ms,
            _mint_source_verification(source, facts_value.content_fingerprint),
            receipt,
            facts_value,
        )
    if not result.current():
        raise AuthoringSourceBindingError("source_stale")
    return result


@dataclass(slots=True, repr=False)
class PreparedAuthoringHistory(_OpaqueClaim):
    snapshot: PublicCompositionSnapshot | None
    generation: int
    sources: tuple[AuthoringRenderSourceClaim, ...]
    fonts: PackagedFontManifest
    authoring_state: NleAuthoringState | None = None
    source_reference_revision: int | None = None
    source_timeline_revision: int | None = None
    _discarded: bool = False

    def current(self) -> bool:
        return not self._discarded and all(source.current() for source in self.sources)

    def discard(self) -> None:
        # Preparation borrows workspace-owned leases. A failed CAS drops only provisional
        # references; releasing their receipt here would destroy the still-current workspace.
        self.sources = ()
        self._discarded = True


@dataclass(frozen=True, slots=True, repr=False)
class BoundAuthoringRenderPlan(_OpaqueClaim):
    plan: RenderPlanV1
    _history: PreparedAuthoringHistory
    _workspace_is_current: Callable[[], bool]
    _issued_snapshot: PublicCompositionSnapshot | None = field(default=None, repr=False)

    def confirm_currentness(
        self,
        *,
        deadline: float | None = None,
    ) -> SourceCurrentnessConfirmation:
        cutoff = time.monotonic() + 30.0 if deadline is None else deadline
        _require_verification_deadline(cutoff)
        if not self._workspace_is_current() or not self._history.current():
            raise AuthoringSourceBindingError("source_stale")
        fonts = load_packaged_font_manifest()
        if fonts != self._history.fonts:
            raise AuthoringSourceBindingError("font_manifest_changed")
        # Currentness is checked again after font I/O. Release/replacement or a timeline edit
        # during that window cannot return the confirmation carried by an older accepted plan.
        if not self._workspace_is_current() or not self._history.current():
            raise AuthoringSourceBindingError("source_stale")
        for source in self._history.sources:
            source.verify_currentness(cutoff)
            if not self._workspace_is_current() or not self._history.current():
                raise AuthoringSourceBindingError("source_stale")
        confirmation = self.plan.source_currentness_claim
        revalidate_render_plan_currentness(plan=self.plan, confirmation=confirmation)
        _require_verification_deadline(cutoff)
        if not self._workspace_is_current() or not self._history.current():
            raise AuthoringSourceBindingError("source_stale")
        return confirmation


def prepare_bound_render_plan(
    history: PreparedAuthoringHistory,
    snapshot: PublicCompositionSnapshot,
    workspace_is_current: Callable[[], bool],
    *,
    deadline: float | None = None,
) -> BoundAuthoringRenderPlan:
    if deadline is not None:
        _require_verification_deadline(deadline)
    if not history.current() or not workspace_is_current():
        raise AuthoringSourceBindingError("source_stale")
    expected = {source.asset.asset_id: source.asset for source in history.sources}
    actual = {asset.asset_id: asset for asset in snapshot.assets if asset.kind != "font"}
    # CRITICAL: compare complete measured assets, including audio. Overlay muting belongs to the
    # track resolver; omitting or relabelling facts here lets forged source claims reach rendering.
    history_workspace_handle = (
        history.authoring_state.workspace_handle
        if history.authoring_state is not None
        else history.snapshot.workspace_handle
        if history.snapshot is not None
        else None
    )
    if actual != expected or snapshot.workspace_handle != history_workspace_handle:
        raise AuthoringSourceBindingError("source_facts_mismatch")
    facts: list[PrivateSourceFact] = []
    for source in sorted(history.sources, key=lambda item: item.asset.asset_id):
        asset_id = source.asset.asset_id
        lease_fp = canonical_fingerprint(
            {
                "generation": source.generation,
                "token": source.currentness_token,
                "source": source.source_fingerprint,
                "facts": source.source_facts_fingerprint,
            }
        )
        facts.append(
            PrivateSourceFact(
                asset_id=asset_id,
                source_id=asset_id,
                origin=source.origin.value,
                disposition="enabled",
                generation=source.generation,
                source_fingerprint=source.source_fingerprint,
                source_facts_fingerprint=source.source_facts_fingerprint,
                source_profile_fingerprint=source.source_profile_fingerprint,
                color_facts_fingerprint=source.color_facts_fingerprint,
                currentness_token=source.currentness_token,
                lease_fingerprint=lease_fp,
                width=source.width,
                height=source.height,
                size_bytes=source.byte_count,
                duration_milliseconds=source.duration_milliseconds,
            )
        )
    manifest = PrivateSourceFactsManifest(
        schema_version=PRIVATE_SOURCE_FACTS_MANIFEST_SCHEMA,
        workspace_handle=snapshot.workspace_handle,
        workspace_revision=snapshot.workspace_revision,
        timeline_revision=snapshot.timeline_revision,
        public_fingerprint=snapshot.public_fingerprint,
        generation=history.generation if facts else 0,
        facts=tuple(facts),
        manifest_fingerprint="sha256:" + "0" * 64,
    )
    manifest = replace(manifest, manifest_fingerprint=private_source_manifest_fingerprint(manifest))
    confirmation = SourceCurrentnessConfirmation(
        schema_version=SOURCE_CURRENTNESS_SCHEMA,
        manifest_fingerprint=manifest.manifest_fingerprint,
        workspace_handle=snapshot.workspace_handle,
        workspace_revision=snapshot.workspace_revision,
        timeline_revision=snapshot.timeline_revision,
        public_fingerprint=snapshot.public_fingerprint,
        generation=manifest.generation,
        tokens=tuple(
            SourceCurrentnessToken(
                asset_id=fact.asset_id,
                source_fingerprint=str(fact.source_fingerprint),
                source_facts_fingerprint=str(fact.source_facts_fingerprint),
                currentness_token=str(fact.currentness_token),
                lease_fingerprint=str(fact.lease_fingerprint),
            )
            for fact in facts
        ),
    )
    fonts = load_packaged_font_manifest()
    if fonts != history.fonts:
        raise AuthoringSourceBindingError("font_manifest_changed")
    bindings: list[FontTitleBindingFact] = []
    tracks = {track.track_id: track for track in snapshot.tracks}
    for clip in sorted(snapshot.clips, key=lambda item: item.clip_id):
        if not clip.enabled or not tracks[clip.track_id].enabled or clip.text is None:
            continue
        text = clip.text
        font = require_text_coverage(
            fonts, text.font_asset_id, text.weight, text.style, text.content
        )
        bindings.append(
            FontTitleBindingFact(
                clip_id=clip.clip_id,
                font_asset_id=font.font_asset_id,
                artifact_id=font.artifact_id,
                weight=font.weight,
                style=font.style,
                text_fingerprint=canonical_fingerprint({"content": text.content}),
                file_fingerprint=font.file_fingerprint,
                cmap_fingerprint=font.cmap_fingerprint,
                build_version=font.build_version,
            )
        )
    font_facts = PackagedFontFacts(
        schema_version=FONT_FACTS_SCHEMA,
        manifest_schema_version=fonts.schema_version,
        profile_id=fonts.profile_id,
        fallback_order=fonts.fallback_order,
        license_spdx_id=fonts.license_spdx_id,
        manifest_fingerprint=fonts.manifest_fingerprint,
        package_fingerprint=fonts.package_fingerprint,
        license_file_fingerprint=fonts.license_file_fingerprint,
        title_bindings=tuple(bindings),
        facts_fingerprint="sha256:" + "0" * 64,
    )
    font_facts = replace(font_facts, facts_fingerprint=packaged_font_facts_fingerprint(font_facts))
    plan = plan_render(
        snapshot=snapshot, source_manifest=manifest, currentness=confirmation, font_facts=font_facts
    )
    bound = BoundAuthoringRenderPlan(plan, history, workspace_is_current, snapshot)
    bound.confirm_currentness(deadline=deadline)
    return bound


def prepare_authoring_history(
    projection: Mapping[str, object],
    receipt: AuthoringSourceBindingReceipt | None,
) -> PreparedAuthoringHistory:
    reference = projection["reference"]
    timeline = projection["timeline"]
    if not isinstance(reference, Mapping) or not isinstance(timeline, Mapping):
        raise AuthoringSourceBindingError("initialization_projection_invalid")
    rows = reference["sources"]
    clips = timeline["clips"]
    if not isinstance(rows, list) or not isinstance(clips, list):
        raise AuthoringSourceBindingError("initialization_projection_invalid")
    if timeline["links"] or any(
        not isinstance(clip, Mapping)
        or clip.get("kind") != "video"
        or clip.get("lane") != 0
        or clip.get("envelope") != []
        for clip in clips
    ):
        raise AuthoringSourceBindingError("audio_editing_deferred")
    sources: list[AuthoringRenderSourceClaim] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise AuthoringSourceBindingError("initialization_projection_invalid")
        if row["admitted"] is not True or row["kind"] == "audio":
            continue
        if receipt is None:
            raise AuthoringSourceBindingError("source_not_bound")
        sources.append(claim_render_source(receipt, str(row["source_id"])))
    try:
        fonts = load_packaged_font_manifest()
    except AuthoringFontError as exc:
        raise AuthoringSourceBindingError(exc.code) from None
    assets = {source.asset.asset_id: source.asset.to_wire() for source in sources}
    for font_id in fonts.fallback_order:
        assets[font_id] = PublicAsset(
            font_id, "font", None, None, None, "absent", "not_applicable", ()
        ).to_wire()
    authoring_state = adapt_legacy_authoring_projection_to_nle_state(
        projection,
        project_id=str(projection["workspace_handle"]),
        workspace_revision=int(str(reference["revision"])),
        asset_timings=assets,
    )
    snapshot = materialize_render_snapshot(authoring_state)
    prepared = PreparedAuthoringHistory(
        snapshot,
        receipt.generation if receipt else 0,
        tuple(sources),
        fonts,
        authoring_state,
        int(str(reference["revision"])),
        int(str(timeline["revision"])),
    )
    if not prepared.current():
        prepared.discard()
        raise AuthoringSourceBindingError("source_stale")
    return prepared
