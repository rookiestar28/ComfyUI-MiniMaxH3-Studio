"""M23-18 bounded ComfyUI input-identity preflight regressions."""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import os
import struct
import sys
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from deployment_request_doubles import LOOPBACK_HOST, ListenerTransport

import comfyui_h3_context
import comfyui_h3_context.adapters.comfyui_input_geometry as adapter
from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.comfyui_input_geometry import (
    GEOMETRY_RECEIPT_TTL_SECONDS,
    INPUT_GEOMETRY_ERROR_SCHEMA,
    INPUT_GEOMETRY_RECEIPT_SCHEMA,
    INPUT_GEOMETRY_REQUEST_SCHEMA,
    INPUT_GEOMETRY_ROUTE,
    MAX_INPUT_GEOMETRY_REQUEST_BYTES,
    GeometryPreflightError,
    InputGeometryRegistry,
    decode_input_geometry_request_json,
)
from comfyui_h3_context.adapters.segment_artifact_store import ArtifactStoreError
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "comfyui_h3_context" / "adapters" / "comfyui_input_geometry.py"


def test_package_lifecycle_invokes_the_lazy_geometry_registration() -> None:
    with patch.object(
        adapter, "ensure_input_geometry_route_registered", return_value=False
    ) as ensure:
        importlib.reload(comfyui_h3_context)
        ensure.assert_called_once_with()
    importlib.reload(comfyui_h3_context)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def _png(width: int, height: int) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\0" + b"\0" * (width * 3) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(rows, level=9))
        + _png_chunk(b"IEND", b"")
    )


def _jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"\0" * 14
    frame = (
        b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    return b"\xff\xd8" + app0 + b"\xff\xc0" + struct.pack(">H", 17) + frame + b"\xff\xd9"


def _gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\0" * 6


def _webp_vp8x(width: int, height: int) -> bytes:
    payload = b"\0" * 4 + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return (
        b"RIFF"
        + struct.pack("<I", len(payload) + 12)
        + b"WEBPVP8X"
        + struct.pack("<I", len(payload))
        + payload
    )


def _request(locator: str = "synthetic/source.png") -> dict[str, object]:
    return {
        "schema": INPUT_GEOMETRY_REQUEST_SCHEMA,
        "locator": locator,
    }


def _registry(root: Path, clock: list[float] | None = None) -> InputGeometryRegistry:
    now = clock if clock is not None else [100.0]
    return InputGeometryRegistry(
        input_root_factory=lambda: root,
        clock=lambda: now[0],
        token_factory=lambda: "r" * 40,
        fingerprint_secret=b"s" * 32,
    )


def test_real_header_preflight_returns_only_safe_identity_and_one_claim(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    source = input_root / "synthetic" / "source.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(_png(120, 160))
    registry = _registry(input_root)

    receipt = registry.observe(_request())

    assert receipt.to_wire() == {
        "schema": INPUT_GEOMETRY_RECEIPT_SCHEMA,
        "receipt_handle": "ig_" + "r" * 40,
        "source_fingerprint": receipt.source_fingerprint,
    }
    assert receipt.source_fingerprint.startswith("sha256:")
    public = json.dumps(receipt.to_wire(), sort_keys=True)
    for private in (str(input_root), "synthetic", "source.png", _png(1, 1).hex()):
        assert private not in public

    claimed = registry.claim(receipt.to_wire())
    assert claimed == receipt
    with pytest.raises(GeometryPreflightError, match="^geometry_receipt_stale$"):
        registry.claim(receipt.to_wire())


def test_jpeg_header_used_by_the_corrected_operator_graph_is_supported(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "portrait.jpg").write_bytes(_jpeg(120, 160))

    receipt = _registry(input_root).observe(_request("portrait.jpg"))

    assert receipt.source_fingerprint.startswith("sha256:")
    assert "portrait.jpg" not in repr(receipt)


def test_jpeg_segment_without_a_marker_prefix_fails_closed(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    frame = b"\x08" + struct.pack(">HH", 160, 120)
    (input_root / "malformed.jpg").write_bytes(
        b"\xff\xd8" + b"\xc0" + struct.pack(">H", 7) + frame + b"\xff\xd9\0\0\0\0"
    )

    with pytest.raises(GeometryPreflightError, match="^image_header_unsupported$"):
        _registry(input_root).observe(_request("malformed.jpg"))


@pytest.mark.parametrize(
    ("name", "payload"),
    [("source.gif", _gif(120, 160)), ("source.webp", _webp_vp8x(120, 160))],
)
def test_other_admitted_comfyui_header_formats_stay_bounded(
    tmp_path: Path, name: str, payload: bytes
) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / name).write_bytes(payload)

    receipt = _registry(input_root).observe(_request(name))

    assert receipt.source_fingerprint.startswith("sha256:")


def test_claim_revalidates_the_same_input_and_refuses_changed_or_expired_receipts(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source.png"
    input_root.mkdir()
    source.write_bytes(_png(120, 160))
    clock = [100.0]
    registry = _registry(input_root, clock)

    changed = registry.observe(_request("source.png"))
    source.write_bytes(_png(160, 120))
    with pytest.raises(GeometryPreflightError, match="^input_changed$"):
        registry.claim(changed.to_wire())

    source.write_bytes(_png(120, 160))
    expired = registry.observe(_request("source.png"))
    clock[0] += GEOMETRY_RECEIPT_TTL_SECONDS + 1.0
    with pytest.raises(GeometryPreflightError, match="^geometry_receipt_stale$"):
        registry.claim(expired.to_wire())


def test_claim_preserves_a_safe_reprobe_category_when_the_input_becomes_unsupported(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source.png"
    input_root.mkdir()
    source.write_bytes(_png(120, 160))
    registry = _registry(input_root)
    receipt = registry.observe(_request("source.png"))
    source.write_bytes(b"not an admitted image format")

    with pytest.raises(GeometryPreflightError, match="^image_format_unsupported$") as failure:
        registry.claim(receipt.to_wire())

    assert failure.value.status == 415


def test_receipt_window_covers_the_bounded_model_free_bootstrap(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source.png"
    input_root.mkdir()
    source.write_bytes(_png(120, 160))
    clock = [100.0]
    registry = _registry(input_root, clock)
    receipt = registry.observe(_request("source.png"))

    clock[0] += 121.0

    assert registry.claim(receipt.to_wire()) == receipt


@pytest.mark.parametrize(
    "locator",
    [
        "../outside.png",
        "/absolute.png",
        r"C:\\absolute.png",
        "https://example.invalid/source.png",
        "source.png:stream",
        "folder//source.png",
        "./source.png",
        "folder/source.png.",
        "folder/source.png ",
        "CON.png",
        "folder/source?.png",
    ],
)
def test_locator_admission_refuses_traversal_schemes_ads_and_ambiguity(
    tmp_path: Path, locator: str
) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    with pytest.raises(GeometryPreflightError, match="^input_locator_unsafe$"):
        _registry(input_root).observe(_request(locator))


def test_non_file_unsupported_oversized_and_symlink_inputs_fail_closed(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "folder.png").mkdir()
    (input_root / "unsupported.png").write_bytes(b"not an image")
    with (input_root / "oversized.png").open("wb") as stream:
        stream.truncate(adapter.MAX_INPUT_IMAGE_BYTES + 1)

    cases = (
        ("missing.png", "input_unavailable"),
        ("folder.png", "input_unsafe"),
        ("unsupported.png", "image_format_unsupported"),
        ("oversized.png", "input_too_large"),
    )
    registry = _registry(input_root)
    for locator, code in cases:
        with pytest.raises(GeometryPreflightError, match=f"^{code}$"):
            registry.observe(_request(locator))

    target = input_root / "target.png"
    target.write_bytes(_png(120, 160))
    linked = input_root / "linked.png"
    try:
        linked.symlink_to(target)
    except OSError:
        pytest.skip("file symlink creation is unavailable")
    with pytest.raises(GeometryPreflightError, match="^input_unsafe$"):
        registry.observe(_request("linked.png"))


def test_hardlinked_input_is_not_admitted_as_one_private_owner(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    source = input_root / "source.png"
    alias = input_root / "alias.png"
    source.write_bytes(_png(120, 160))
    try:
        os.link(source, alias)
    except OSError:
        pytest.skip("hardlink creation is unavailable")

    with pytest.raises(GeometryPreflightError, match="^input_unsafe$"):
        _registry(input_root).observe(_request("alias.png"))


def test_receipt_registry_capacity_fails_before_retaining_an_extra_owner(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "source.png").write_bytes(_png(120, 160))
    tokens = iter(("a" * 40, "b" * 40))
    registry = InputGeometryRegistry(
        input_root_factory=lambda: input_root,
        clock=lambda: 100.0,
        token_factory=lambda: next(tokens),
        fingerprint_secret=b"s" * 32,
        max_entries=1,
    )
    registry.observe(_request("source.png"))

    with pytest.raises(GeometryPreflightError, match="^geometry_capacity$"):
        registry.observe(_request("source.png"))


def test_reparse_refusal_is_pinned_even_when_windows_cannot_create_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    source = input_root / "source.png"
    source.write_bytes(_png(120, 160))
    monkeypatch.setattr(
        adapter,
        "_is_link_or_reparse",
        lambda path, _metadata: path == source,
    )

    with pytest.raises(GeometryPreflightError, match="^input_unsafe$"):
        _registry(input_root).observe(_request("source.png"))


def test_intermediate_directory_guard_failure_stays_a_typed_unsafe_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_root = tmp_path / "input"
    source = input_root / "nested" / "source.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(_png(120, 160))

    @contextmanager
    def refuse_directory_identity(*_directories: Path) -> Iterator[None]:
        raise ArtifactStoreError("unsafe_store_entry")
        yield

    monkeypatch.setattr(adapter, "_validated_directories", refuse_directory_identity)

    with pytest.raises(GeometryPreflightError, match="^input_unsafe$") as failure:
        _registry(input_root).observe(_request("nested/source.png"))

    assert failure.value.status == 422

    async def exercise_route() -> None:
        handler = _registered_handler(_Routes())
        body = json.dumps(_request("nested/source.png"), separators=(",", ":")).encode()
        with composition_root.substituted(composition_root.INPUT_GEOMETRY, _registry(input_root)):
            response = await handler(_route_request(body))
        assert response.status == 422
        assert response.body == {
            "schema": INPUT_GEOMETRY_ERROR_SCHEMA,
            "category": "input_unsafe",
        }

    asyncio.run(exercise_route())


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"[]",
        b"{",
        b"\xff",
        b'{"schema":"x","schema":"y"}',
    ],
)
def test_decoder_rejects_malformed_or_ambiguous_json(payload: bytes) -> None:
    with pytest.raises(GeometryPreflightError, match="^invalid_request$"):
        decode_input_geometry_request_json(payload)


class _Routes(list[SimpleNamespace]):
    def post(self, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.append(SimpleNamespace(method="POST", path=path, handler=handler))
            return handler

        return decorate


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._done = False

    async def read(self, _limit: int) -> bytes:
        if self._done:
            return b""
        self._done = True
        return self._body


class _Headers:
    def __init__(self, origins: list[str]) -> None:
        self._origins = origins

    def getall(self, name: str, default: list[str]) -> list[str]:
        if name == "Host":
            return [LOOPBACK_HOST]
        return self._origins if name == "Origin" else default


def _route_request(
    body: bytes,
    *,
    origins: list[str] | None = None,
    content_type: str = "application/json",
    content_length: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content_type=content_type,
        content_length=len(body) if content_length is None else content_length,
        content=_Content(body),
        headers=_Headers(["http://127.0.0.1:8188"] if origins is None else origins),
        transport=ListenerTransport(),
    )


def _registered_handler(routes: _Routes) -> Callable[..., Any]:
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(
        json_response=lambda value, status=200: SimpleNamespace(status=status, body=value)
    )
    with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
        with patch.object(adapter, "_ROUTE_REGISTERED", False):
            assert adapter.ensure_input_geometry_route_registered()
            assert adapter.ensure_input_geometry_route_registered()
    assert len(routes) == 1
    assert routes[0].path == INPUT_GEOMETRY_ROUTE
    return cast(Callable[..., Any], routes[0].handler)


def test_route_is_lazy_same_origin_bounded_and_returns_safe_categories(tmp_path: Path) -> None:
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"), filename=str(ADAPTER))
    top_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_imports.add(node.module.split(".", 1)[0])
    assert {"aiohttp", "server", "PIL", "comfy"}.isdisjoint(top_imports)

    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "source.png").write_bytes(_png(120, 160))
    body = json.dumps(_request("source.png"), separators=(",", ":")).encode()

    async def exercise() -> None:
        handler = _registered_handler(_Routes())
        with composition_root.substituted(composition_root.INPUT_GEOMETRY, _registry(input_root)):
            rejected = await handler(_route_request(body, origins=[]))
            assert rejected.status == 403
            assert rejected.body == {
                "schema": INPUT_GEOMETRY_ERROR_SCHEMA,
                "category": "origin_rejected",
            }

            oversized = await handler(
                _route_request(body, content_length=MAX_INPUT_GEOMETRY_REQUEST_BYTES + 1)
            )
            assert oversized.status == 413
            assert oversized.body["category"] == "request_too_large"

            accepted = await handler(_route_request(body))
            assert accepted.status == 200
            assert set(accepted.body) == {
                "schema",
                "receipt_handle",
                "source_fingerprint",
            }
            assert "source.png" not in json.dumps(accepted.body)

    asyncio.run(exercise())
