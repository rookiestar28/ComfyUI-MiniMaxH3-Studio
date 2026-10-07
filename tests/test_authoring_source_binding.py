from __future__ import annotations

import copy
import gc
import io
import pickle
import weakref
from abc import ABCMeta
from collections.abc import Callable
from pathlib import Path

import pytest

from comfyui_h3_context.adapters.authoring_source_binding import (
    AuthoringSourceBindingError,
    AuthoringSourceBindingReceipt,
    ComfyVideoInputTypeAuthority,
    PathBackedComfyVideoFromFileV1Factory,
    ProcessLocalAuthoringSourceBindingStore,
    RuntimeVideoCapability,
    authoring_source_duration_milliseconds,
    authoring_source_preview_capability,
    claim_exact_authoring_source,
    detect_comfy_video_input_capability,
    execute_transferred_authoring_preview,
)
from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.core.authoring_preview_protocol import (
    AuthoringPreviewCapabilityReason,
)
from comfyui_h3_context.core.contracts import AssetRole, MediaKind
from comfyui_h3_context.core.registry import (
    ReferenceAsset,
    ReferenceRegistry,
    build_reference_registry,
)


def registry() -> ReferenceRegistry:
    return build_reference_registry(
        (ReferenceAsset("video-1", MediaKind.VIDEO, AssetRole.REFERENCE, 1),)
    )


VideoInput = ABCMeta(
    "VideoInput",
    (),
    {"__module__": "comfy_api.latest._input.video_types"},
)
VideoFromFile = type(
    "VideoFromFile",
    (VideoInput,),
    {
        "__module__": "comfy_api.latest._input_impl.video_types",
        "get_stream_source": lambda _self: (_ for _ in ()).throw(
            AssertionError("media methods must not be called")
        ),
        "get_components": lambda _self: (_ for _ in ()).throw(
            AssertionError("media methods must not be called")
        ),
        "get_duration": lambda _self: (_ for _ in ()).throw(
            AssertionError("media methods must not be called")
        ),
        "get_dimensions": lambda _self: (_ for _ in ()).throw(
            AssertionError("media methods must not be called")
        ),
    },
)
VideoFromComponents = type(
    "VideoFromComponents",
    (VideoInput,),
    {"__module__": "comfy_api.latest._input_impl.video_types"},
)


CustomVideo = type("CustomVideo", (VideoInput,), {"__module__": __name__})
TYPE_AUTHORITY = ComfyVideoInputTypeAuthority(
    video_input=VideoInput,
    video_from_file=VideoFromFile,
    video_from_components=VideoFromComponents,
)


def file_video(value: object) -> object:
    video = VideoFromFile()
    object.__setattr__(video, "_VideoFromFile__file", value)
    return video


def file_factory(
    root: Path,
    *,
    max_source_bytes: int = 64 * 1024 * 1024,
    duration_probe: Callable[[object], int | None] | None = None,
) -> PathBackedComfyVideoFromFileV1Factory:
    return PathBackedComfyVideoFromFileV1Factory(
        type_authority=TYPE_AUTHORITY,
        input_root_factory=lambda: root,
        max_source_bytes=max_source_bytes,
        duration_probe=duration_probe,
    )


def test_type_authority_accepts_the_host_abc_metaclass_shape() -> None:
    assert type(VideoInput) is ABCMeta
    assert TYPE_AUTHORITY.video_input is VideoInput


class FakeReceipt(AuthoringSourceBindingReceipt):
    def __init__(
        self,
        exact_registry: ReferenceRegistry,
        generation: int,
        sources: dict[str, tuple[RuntimeVideoCapability, object]],
    ) -> None:
        super().__init__(exact_registry=exact_registry, generation=generation)
        self.sources = sources

    def _capability_for(self, source_id: str) -> RuntimeVideoCapability:
        try:
            return self.sources[source_id][0]
        except KeyError as exc:
            raise AuthoringSourceBindingError("source_not_found") from exc

    def _claim_source(self, source_id: str) -> object:
        return self.sources[source_id][1]

    def _release_sources(self) -> None:
        self.sources.clear()


def test_capability_detection_is_method_free_and_fail_closed() -> None:
    assert (
        detect_comfy_video_input_capability(VideoFromFile(), type_authority=TYPE_AUTHORITY)
        is RuntimeVideoCapability.AVAILABLE
    )
    assert (
        detect_comfy_video_input_capability(VideoFromComponents(), type_authority=TYPE_AUTHORITY)
        is RuntimeVideoCapability.COMPONENT_BACKED
    )
    assert (
        detect_comfy_video_input_capability(CustomVideo(), type_authority=TYPE_AUTHORITY)
        is RuntimeVideoCapability.CUSTOM
    )
    assert (
        detect_comfy_video_input_capability(object(), type_authority=TYPE_AUTHORITY)
        is RuntimeVideoCapability.UNSUPPORTED
    )

    spoof = type(
        "VideoFromFile",
        (),
        {"__module__": "comfy_api.latest._input_impl.video_types"},
    )()
    assert (
        detect_comfy_video_input_capability(spoof, type_authority=TYPE_AUTHORITY)
        is RuntimeVideoCapability.UNSUPPORTED
    )


def test_exact_claim_rejects_equal_registry_substitution_generation_and_release() -> None:
    exact_registry = registry()
    runtime_source = object()
    receipt = FakeReceipt(
        exact_registry,
        3,
        {"video-1": (RuntimeVideoCapability.AVAILABLE, runtime_source)},
    )

    assert (
        claim_exact_authoring_source(
            receipt,
            exact_registry=exact_registry,
            expected_generation=3,
            source_id="video-1",
        )
        is runtime_source
    )

    equal_but_distinct = ReferenceRegistry(
        assets=exact_registry.assets,
        labels=exact_registry.labels,
    )
    assert equal_but_distinct == exact_registry and equal_but_distinct is not exact_registry
    with pytest.raises(AuthoringSourceBindingError, match="registry_mismatch"):
        claim_exact_authoring_source(
            receipt,
            exact_registry=equal_but_distinct,
            expected_generation=3,
            source_id="video-1",
        )
    with pytest.raises(AuthoringSourceBindingError, match="generation_mismatch"):
        claim_exact_authoring_source(
            receipt,
            exact_registry=exact_registry,
            expected_generation=4,
            source_id="video-1",
        )

    receipt.release()
    receipt.release()
    assert receipt.released is True
    assert receipt.sources == {}
    with pytest.raises(AuthoringSourceBindingError, match="receipt_released"):
        claim_exact_authoring_source(
            receipt,
            exact_registry=exact_registry,
            expected_generation=3,
            source_id="video-1",
        )


@pytest.mark.parametrize(
    "capability",
    [
        RuntimeVideoCapability.COMPONENT_BACKED,
        RuntimeVideoCapability.CUSTOM,
        RuntimeVideoCapability.UNSUPPORTED,
    ],
)
def test_claim_rejects_every_unsupported_capability(capability: RuntimeVideoCapability) -> None:
    exact_registry = registry()
    receipt = FakeReceipt(exact_registry, 1, {"video-1": (capability, object())})
    with pytest.raises(AuthoringSourceBindingError, match="source_unsupported"):
        claim_exact_authoring_source(
            receipt,
            exact_registry=exact_registry,
            expected_generation=1,
            source_id="video-1",
        )


def test_receipt_repr_copy_and_serialization_never_expose_runtime_values() -> None:
    exact_registry = registry()
    receipt = FakeReceipt(
        exact_registry,
        1,
        {"video-1": (RuntimeVideoCapability.AVAILABLE, "synthetic-private-source")},
    )

    assert repr(receipt) == "<AuthoringSourceBindingReceipt opaque>"
    for operation in (
        lambda: copy.copy(receipt),
        lambda: copy.deepcopy(receipt),
        lambda: pickle.dumps(receipt),
    ):
        with pytest.raises(TypeError, match="not (copyable|serializable)"):
            operation()


def test_file_backed_factory_admits_only_exact_str_under_trusted_root_without_methods(
    tmp_path: Path,
) -> None:
    root = tmp_path / "input"
    root.mkdir()
    source = root / "nested" / "clip.mp4"
    source.parent.mkdir()
    source.write_bytes(b"synthetic-video")
    exact_registry = registry()

    receipt = file_factory(root).capture(
        exact_registry=exact_registry,
        generation=7,
        sources=(("video-1", MediaKind.VIDEO, file_video(str(source))),),
    )

    capability = authoring_source_preview_capability(receipt, "video-1")
    assert capability.available is True and capability.reason is None
    authority = claim_exact_authoring_source(
        receipt,
        exact_registry=exact_registry,
        expected_generation=7,
        source_id="video-1",
    )
    assert repr(authority) == "<PathBackedAuthoringVideoSource opaque>"
    with pytest.raises(TypeError, match="not serializable"):
        pickle.dumps(authority)
    assert str(source) not in repr(receipt)


def test_file_backed_receipt_keeps_only_bounded_duration_and_clears_it_on_release(
    tmp_path: Path,
) -> None:
    root = tmp_path / "input"
    root.mkdir()
    source = root / "clip.mp4"
    source.write_bytes(b"synthetic-video")
    exact_registry = registry()
    observed: list[str] = []

    def probe(authority: object) -> int:
        observed.append(repr(authority))
        return 4_000

    receipt = file_factory(root, duration_probe=probe).capture(
        exact_registry=exact_registry,
        generation=1,
        sources=(("video-1", MediaKind.VIDEO, file_video(str(source))),),
    )

    assert observed == ["<PathBackedAuthoringVideoSource opaque>"]
    assert authoring_source_duration_milliseconds(receipt, "video-1") == 4_000
    assert str(source) not in repr(receipt)
    receipt.release()
    assert authoring_source_duration_milliseconds(receipt, "video-1") is None


def test_duration_probe_cannot_authorize_a_source_changed_during_probe(tmp_path: Path) -> None:
    root = tmp_path / "input"
    root.mkdir()
    source = root / "clip.mp4"
    source.write_bytes(b"synthetic-video")
    exact_registry = registry()

    def replace_during_probe(_authority: object) -> int:
        source.write_bytes(b"replacement-video")
        return 4_000

    receipt = file_factory(root, duration_probe=replace_during_probe).capture(
        exact_registry=exact_registry,
        generation=1,
        sources=(("video-1", MediaKind.VIDEO, file_video(str(source))),),
    )

    assert authoring_source_duration_milliseconds(receipt, "video-1") is None
    capability = authoring_source_preview_capability(receipt, "video-1")
    assert capability.available is False
    assert capability.reason is AuthoringPreviewCapabilityReason.STALE


def test_private_authority_alone_hands_path_to_exact_qualified_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "input"
    root.mkdir()
    source = root / "clip.mp4"
    source.write_bytes(b"synthetic-video")
    exact_registry = registry()
    receipt = file_factory(root).capture(
        exact_registry=exact_registry,
        generation=1,
        sources=(("video-1", MediaKind.VIDEO, file_video(str(source))),),
    )
    authority = claim_exact_authoring_source(
        receipt,
        exact_registry=exact_registry,
        expected_generation=1,
        source_id="video-1",
    )
    adapter = object.__new__(QualifiedAVMediaAdapter)
    captured: dict[str, object] = {}

    def execute(_self: QualifiedAVMediaAdapter, **kwargs: object) -> tuple[bytearray, str]:
        captured.update(kwargs)
        return bytearray(b"normalized"), "absent"

    monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_authoring_preview", execute)
    body, audio = execute_transferred_authoring_preview(
        authority,
        adapter,
        source_start_frame=12,
        frames=24,
        source_fps=24,
        deadline=10.0,
    )

    assert (body, audio) == (bytearray(b"normalized"), "absent")
    assert captured["source_path"] == source
    assert repr(authority) == "<PathBackedAuthoringVideoSource opaque>"


@pytest.mark.parametrize(
    ("value", "expected_reason"),
    [
        (io.BytesIO(b"synthetic"), AuthoringPreviewCapabilityReason.UNSUPPORTED),
        (Path("synthetic.mp4"), AuthoringPreviewCapabilityReason.UNSUPPORTED),
        (None, AuthoringPreviewCapabilityReason.UNSUPPORTED),
    ],
)
def test_file_backed_factory_rejects_non_str_backing_without_retention(
    tmp_path: Path,
    value: object,
    expected_reason: AuthoringPreviewCapabilityReason,
) -> None:
    root = tmp_path / "input"
    root.mkdir()
    exact_registry = registry()
    receipt = file_factory(root).capture(
        exact_registry=exact_registry,
        generation=1,
        sources=(("video-1", MediaKind.VIDEO, file_video(value)),),
    )
    capability = authoring_source_preview_capability(receipt, "video-1")
    assert capability.available is False and capability.reason is expected_reason


def test_file_backed_factory_rejects_component_custom_subclass_and_field_drift(
    tmp_path: Path,
) -> None:
    root = tmp_path / "input"
    root.mkdir()
    source = root / "clip.mp4"
    source.write_bytes(b"synthetic")
    exact_registry = registry()
    subclass = type("SubclassVideo", (VideoFromFile,), {})()
    object.__setattr__(subclass, "_VideoFromFile__file", str(source))
    values = (
        VideoFromComponents(),
        CustomVideo(),
        subclass,
        VideoFromFile(),
    )
    for value in values:
        receipt = file_factory(root).capture(
            exact_registry=exact_registry,
            generation=1,
            sources=(("video-1", MediaKind.VIDEO, value),),
        )
        capability = authoring_source_preview_capability(receipt, "video-1")
        assert capability.available is False
        assert capability.reason is AuthoringPreviewCapabilityReason.UNSUPPORTED


def test_file_backed_factory_rejects_outside_link_and_oversized_sources(
    tmp_path: Path,
) -> None:
    root = tmp_path / "input"
    root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    oversized = root / "oversized.mp4"
    oversized.write_bytes(b"12345")
    exact_registry = registry()

    for source in (outside, oversized):
        receipt = file_factory(root, max_source_bytes=4).capture(
            exact_registry=exact_registry,
            generation=1,
            sources=(("video-1", MediaKind.VIDEO, file_video(str(source))),),
        )
        capability = authoring_source_preview_capability(receipt, "video-1")
        assert capability.available is False
        assert capability.reason is AuthoringPreviewCapabilityReason.UNAVAILABLE

    link = root / "linked.mp4"
    try:
        link.symlink_to(outside)
    except OSError:
        return
    receipt = file_factory(root).capture(
        exact_registry=exact_registry,
        generation=1,
        sources=(("video-1", MediaKind.VIDEO, file_video(str(link))),),
    )
    capability = authoring_source_preview_capability(receipt, "video-1")
    assert capability.available is False
    assert capability.reason is AuthoringPreviewCapabilityReason.UNAVAILABLE


def test_late_identity_revalidation_marks_replaced_file_stale(tmp_path: Path) -> None:
    root = tmp_path / "input"
    root.mkdir()
    source = root / "clip.mp4"
    source.write_bytes(b"first")
    exact_registry = registry()
    receipt = file_factory(root).capture(
        exact_registry=exact_registry,
        generation=2,
        sources=(("video-1", MediaKind.VIDEO, file_video(str(source))),),
    )
    source.unlink()
    source.write_bytes(b"replacement-with-different-identity")

    capability = authoring_source_preview_capability(receipt, "video-1")
    assert capability.available is False
    assert capability.reason is AuthoringPreviewCapabilityReason.STALE
    with pytest.raises(AuthoringSourceBindingError, match="source_stale"):
        claim_exact_authoring_source(
            receipt,
            exact_registry=exact_registry,
            expected_generation=2,
            source_id="video-1",
        )


def test_factory_never_retains_audio_runtime_objects(tmp_path: Path) -> None:
    class AudioRuntime:
        pass

    root = tmp_path / "input"
    root.mkdir()
    source = root / "clip.mp4"
    source.write_bytes(b"synthetic")
    exact_registry = build_reference_registry(
        (
            ReferenceAsset("video-1", MediaKind.VIDEO, AssetRole.REFERENCE, 1),
            ReferenceAsset("audio-1", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 2),
        )
    )
    audio = AudioRuntime()
    audio_ref = weakref.ref(audio)
    receipt = file_factory(root).capture(
        exact_registry=exact_registry,
        generation=1,
        sources=(
            ("video-1", MediaKind.VIDEO, file_video(str(source))),
            ("audio-1", MediaKind.AUDIO, audio),
        ),
    )
    del audio
    gc.collect()

    assert audio_ref() is None
    audio_capability = authoring_source_preview_capability(receipt, "audio-1")
    assert audio_capability.available is False
    assert audio_capability.reason is AuthoringPreviewCapabilityReason.UNSUPPORTED


def test_process_store_binds_exact_registry_generation_ttl_overflow_and_close(
    tmp_path: Path,
) -> None:
    root = tmp_path / "input"
    root.mkdir()
    source = root / "clip.mp4"
    source.write_bytes(b"synthetic")
    now = [0.0]
    store = ProcessLocalAuthoringSourceBindingStore(
        factory=file_factory(root),
        max_entries=1,
        ttl_seconds=5,
        clock=lambda: now[0],
    )
    first_registry = registry()
    first = store.capture(
        exact_registry=first_registry,
        sources=(("video-1", MediaKind.VIDEO, file_video(str(source))),),
    )
    assert first.generation == 1
    equal_but_distinct = ReferenceRegistry(
        first_registry.assets,
        first_registry.labels,
    )
    assert store.claim(equal_but_distinct) is None
    assert store.claim(first_registry) is first
    assert store.claim(first_registry) is None

    second_registry = registry()
    second = store.capture(
        exact_registry=second_registry,
        sources=(("video-1", MediaKind.VIDEO, file_video(str(source))),),
    )
    third_registry = registry()
    third = store.capture(
        exact_registry=third_registry,
        sources=(("video-1", MediaKind.VIDEO, file_video(str(source))),),
    )
    assert second.released is True
    assert third.released is False

    now[0] = 6.0
    assert store.claim(third_registry) is None
    assert third.released is True

    fourth_registry = registry()
    fourth = store.capture(
        exact_registry=fourth_registry,
        sources=(("video-1", MediaKind.VIDEO, file_video(str(source))),),
    )
    store.close()
    store.close()
    assert fourth.released is True
