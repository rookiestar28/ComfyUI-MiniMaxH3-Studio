from __future__ import annotations

import copy
import threading
from typing import Any

import pytest

from comfyui_h3_context.adapters import authoring_source_binding as binding
from comfyui_h3_context.adapters.authoring_image_source import OwnedImageSource
from comfyui_h3_context.context_request_nodes import H3ReferenceRegistryNode
from comfyui_h3_context.core.contracts import AssetRole, MediaKind
from comfyui_h3_context.core.registry import (
    ReferenceAsset,
    ReferenceRegistry,
    build_reference_registry,
)


def image_registry() -> ReferenceRegistry:
    return build_reference_registry(
        (ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1),)
    )


def test_exact_runtime_image_is_owned_and_survives_caller_mutation() -> None:
    torch = pytest.importorskip("torch")
    from comfyui_h3_context.adapters.authoring_image_source import ImageSourcePool

    pool = ImageSourcePool()
    factory = binding.RuntimeComfySourceFactory(image_pool=pool)
    registry = image_registry()
    image = torch.full((1, 2, 3, 3), 0.25, dtype=torch.float32)
    receipt = factory.capture(
        exact_registry=registry,
        generation=1,
        sources=(("image_1", MediaKind.IMAGE, image),),
    )
    source = binding.claim_exact_authoring_source(
        receipt,
        exact_registry=registry,
        expected_generation=1,
        source_id="image_1",
    )
    assert isinstance(source, OwnedImageSource)
    before = source.fingerprint
    owned = source.read_bytes()
    image.fill_(0.75)
    assert source.read_bytes() == owned
    assert source.fingerprint == before
    assert source.width == 3 and source.height == 2
    assert pool.live_bytes == 72 and pool.reserved_bytes == 0
    with pytest.raises(binding.AuthoringSourceBindingError, match="registry_mismatch"):
        binding.claim_exact_authoring_source(
            receipt,
            exact_registry=copy.deepcopy(registry),
            expected_generation=1,
            source_id="image_1",
        )
    receipt.release()
    assert pool.live_bytes == 0
    with pytest.raises(binding.AuthoringSourceBindingError, match="source_stale"):
        source.read_bytes()


@pytest.mark.parametrize(
    "case", ["batch", "channels", "dtype", "nan", "range", "empty", "meta", "subclass", "array"]
)
def test_unsupported_images_never_acquire_owned_storage(case: str) -> None:
    torch = pytest.importorskip("torch")
    from comfyui_h3_context.adapters.authoring_image_source import ImageSourcePool

    image = torch.zeros((1, 2, 2, 3))
    if case == "batch":
        image = torch.zeros((2, 2, 2, 3))
    elif case == "channels":
        image = torch.zeros((1, 2, 2, 4))
    elif case == "dtype":
        image = image.to(torch.float64)
    elif case == "nan":
        image[0, 0, 0, 0] = float("nan")
    elif case == "range":
        image[0, 0, 0, 0] = 1.01
    elif case == "empty":
        image = torch.zeros((1, 0, 2, 3))
    elif case == "meta":
        image = torch.zeros((1, 2, 2, 3), device="meta")
    elif case == "subclass":
        tensor_subclass = type("TensorSubclass", (torch.Tensor,), {})
        image = image.as_subclass(tensor_subclass)
    elif case == "array":
        image = image.numpy()
    pool = ImageSourcePool()
    registry = image_registry()
    receipt = binding.RuntimeComfySourceFactory(image_pool=pool).capture(
        exact_registry=registry,
        generation=1,
        sources=(("image_1", MediaKind.IMAGE, image),),
    )
    with pytest.raises(binding.AuthoringSourceBindingError, match="source_unsupported"):
        binding.claim_exact_authoring_source(
            receipt,
            exact_registry=registry,
            expected_generation=1,
            source_id="image_1",
        )
    assert pool.live_bytes == pool.reserved_bytes == 0


def test_source_capacity_includes_copy_scratch_and_releases_on_expiry() -> None:
    torch = pytest.importorskip("torch")
    from comfyui_h3_context.adapters.authoring_image_source import ImageSourcePool

    now = [1.0]
    pool = ImageSourcePool(max_bytes=160, clock=lambda: now[0], ttl_seconds=10)
    image = torch.zeros((1, 2, 2, 3))
    source = pool.capture(image)
    assert pool.live_bytes == 48
    with pytest.raises(binding.AuthoringSourceBindingError, match="image_capacity"):
        pool.capture(image)
    assert pool.reserved_bytes == 0
    now[0] = 11.0
    assert not source.current()
    assert pool.live_bytes == 0
    assert pool.capture(image).current()
    pool.close()
    assert pool.live_bytes == pool.reserved_bytes == 0


def test_node_captures_exact_grouped_image_return_and_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")
    from comfyui_h3_context.adapters.authoring_image_source import ImageSourcePool

    pool = ImageSourcePool()
    store = binding.ProcessLocalAuthoringSourceBindingStore(
        factory=binding.RuntimeComfySourceFactory(image_pool=pool),
    )
    monkeypatch.setattr(binding, "_PROCESS_BINDINGS", store)
    image = torch.zeros((1, 2, 2, 3))
    registry = H3ReferenceRegistryNode().build_registry(images=image)[0]
    receipt = store.claim(registry)
    assert receipt is not None
    source = binding.claim_transferred_authoring_source(receipt, "image_1")
    assert isinstance(source, OwnedImageSource)
    assert source.current()
    replacement = store.capture(
        exact_registry=registry,
        sources=(("image_1", MediaKind.IMAGE, image),),
    )
    assert replacement.generation != receipt.generation
    assert not source.current()
    replacement.release()
    store.close()
    assert pool.live_bytes == 0


def test_noncontiguous_image_copy_preserves_exact_pixels_and_closed_metadata() -> None:
    torch = pytest.importorskip("torch")
    from comfyui_h3_context.adapters.authoring_image_source import ImageSourcePool

    value = torch.arange(18, dtype=torch.float32).reshape(1, 2, 3, 3) / 18
    view = value.transpose(1, 2)
    pool = ImageSourcePool()
    source = pool.capture(view)
    assert source.read_bytes() == view.contiguous().numpy().tobytes()
    assert (source.width, source.height) == (2, 3)
    for attribute in ("width", "height", "fingerprint"):
        with pytest.raises(AttributeError):
            setattr(source, attribute, 999)
    pool.close()


def test_cancelled_copy_releases_provisional_reservation() -> None:
    torch = pytest.importorskip("torch")
    from comfyui_h3_context.adapters.authoring_image_source import ImageSourcePool

    pool = ImageSourcePool()
    with pytest.raises(binding.AuthoringSourceBindingError, match="source_cancelled"):
        pool.capture(torch.zeros((1, 2, 2, 3)), cancelled=lambda: True)
    assert pool.live_bytes == pool.reserved_bytes == 0


def test_copy_has_no_wait_queue_and_close_wins_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")
    numpy = pytest.importorskip("numpy")
    from comfyui_h3_context.adapters.authoring_image_source import ImageSourcePool

    pool = ImageSourcePool()
    value = torch.zeros((1, 2, 2, 3))
    entered, resume = threading.Event(), threading.Event()
    original = numpy.array
    errors: list[binding.AuthoringSourceBindingError] = []

    def delayed(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert resume.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(numpy, "array", delayed)

    def capture() -> None:
        try:
            pool.capture(value)
        except binding.AuthoringSourceBindingError as exc:
            errors.append(exc)

    worker = threading.Thread(target=capture)
    worker.start()
    assert entered.wait(5)
    try:
        assert pool.reserved_bytes == 144
        with pytest.raises(binding.AuthoringSourceBindingError, match="image_copy_busy"):
            pool.capture(value)
        pool.close()
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1 and errors[0].code == "store_closed"
    assert pool.live_bytes == pool.reserved_bytes == 0
