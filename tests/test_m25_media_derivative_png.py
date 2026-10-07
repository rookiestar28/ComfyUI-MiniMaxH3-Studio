from __future__ import annotations

import struct
import zlib

import pytest

import comfyui_h3_context.adapters.authoring_derivative_png as png_module
from comfyui_h3_context.adapters.authoring_derivative_png import (
    PNG_IMAGE_MAX_BYTES,
    PNG_MEDIA_TYPE,
    PNG_PROFILE_ID,
    PNG_THUMBNAIL_MAX_BYTES,
    RGB8PNGError,
    encode_rgb8_png,
    probe_rgb8_png,
)


def _chunks(payload: bytes | bytearray) -> list[tuple[bytes, bytes]]:
    offset = 8
    result: list[tuple[bytes, bytes]] = []
    while offset < len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        name = bytes(payload[offset + 4 : offset + 8])
        body = bytes(payload[offset + 8 : offset + 8 + length])
        result.append((name, body))
        offset += 12 + length
    assert offset == len(payload)
    return result


def test_png_encoder_is_standard_closed_and_deterministic() -> None:
    rgb = bytes((0, 1, 2, 127, 128, 129, 253, 254, 255, 20, 40, 60))

    first = encode_rgb8_png(
        rgb,
        width=2,
        height=2,
        maximum_bytes=PNG_IMAGE_MAX_BYTES,
    )
    second = encode_rgb8_png(
        rgb,
        width=2,
        height=2,
        maximum_bytes=PNG_IMAGE_MAX_BYTES,
    )

    assert isinstance(first, bytearray)
    assert first == second
    assert bytes(first[:8]) == b"\x89PNG\r\n\x1a\n"
    chunks = _chunks(first)
    assert [name for name, _body in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    assert chunks[0][1] == struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    assert zlib.decompress(chunks[1][1]) == bytes(
        (0, 0, 1, 2, 127, 128, 129, 0, 253, 254, 255, 20, 40, 60)
    )

    probe = probe_rgb8_png(
        first,
        maximum_width=8192,
        maximum_height=8192,
        maximum_pixels=4_194_304,
        maximum_bytes=PNG_IMAGE_MAX_BYTES,
    )
    assert (probe.width, probe.height, probe.byte_count) == (2, 2, len(first))
    assert probe.payload_fingerprint.startswith("sha256:")
    assert probe.rgb_fingerprint.startswith("sha256:")
    assert PNG_PROFILE_ID == "h3.authoring.png.rgb8.v1"
    assert PNG_MEDIA_TYPE == "image/png"


def test_png_encoder_guard_and_byte_limits_fail_closed() -> None:
    observations = 0

    def cancelled() -> None:
        nonlocal observations
        observations += 1
        if observations == 2:
            raise RGB8PNGError("cancelled")

    with pytest.raises(RGB8PNGError, match="^cancelled$"):
        encode_rgb8_png(
            bytes(4 * 4 * 3),
            width=4,
            height=4,
            maximum_bytes=PNG_THUMBNAIL_MAX_BYTES,
            guard=cancelled,
        )
    assert observations == 2

    with pytest.raises(RGB8PNGError, match="^png_input_invalid$"):
        encode_rgb8_png(b"too short", width=2, height=2, maximum_bytes=100)
    with pytest.raises(RGB8PNGError, match="^resource_limit$"):
        encode_rgb8_png(bytes(12), width=2, height=2, maximum_bytes=40)


@pytest.mark.parametrize("mutation", ["crc", "trailing", "ancillary", "filter"])
def test_png_probe_rejects_non_profile_bytes(mutation: str) -> None:
    payload = encode_rgb8_png(
        bytes((1, 2, 3, 4, 5, 6)),
        width=2,
        height=1,
        maximum_bytes=PNG_IMAGE_MAX_BYTES,
    )
    if mutation == "crc":
        payload[-1] ^= 1
    elif mutation == "trailing":
        payload.extend(b"x")
    elif mutation == "ancillary":
        ihdr_end = 8 + 12 + 13
        name = b"tEXt"
        body = b"x"
        ancillary_chunk = struct.pack(">I", len(body)) + name + body
        ancillary_chunk += struct.pack(">I", zlib.crc32(name + body) & 0xFFFFFFFF)
        payload[ihdr_end:ihdr_end] = ancillary_chunk
    else:
        chunks = _chunks(payload)
        raw = bytearray(zlib.decompress(chunks[1][1]))
        raw[0] = 1
        compressed = zlib.compress(bytes(raw), level=9)
        ihdr = chunks[0][1]

        def build_chunk(name: bytes, body: bytes) -> bytes:
            return (
                struct.pack(">I", len(body))
                + name
                + body
                + struct.pack(">I", zlib.crc32(name + body) & 0xFFFFFFFF)
            )

        payload = bytearray(b"\x89PNG\r\n\x1a\n")
        payload.extend(build_chunk(b"IHDR", ihdr))
        payload.extend(build_chunk(b"IDAT", compressed))
        payload.extend(build_chunk(b"IEND", b""))

    with pytest.raises(RGB8PNGError, match="^png_profile_invalid$"):
        probe_rgb8_png(
            payload,
            maximum_width=8192,
            maximum_height=8192,
            maximum_pixels=4_194_304,
            maximum_bytes=PNG_IMAGE_MAX_BYTES,
        )


def test_png_probe_rejects_dimensions_and_compressed_expansion() -> None:
    payload = encode_rgb8_png(
        bytes(3 * 3 * 3),
        width=3,
        height=3,
        maximum_bytes=PNG_IMAGE_MAX_BYTES,
    )
    with pytest.raises(RGB8PNGError, match="^resource_limit$"):
        probe_rgb8_png(
            payload,
            maximum_width=2,
            maximum_height=8192,
            maximum_pixels=4_194_304,
            maximum_bytes=PNG_IMAGE_MAX_BYTES,
        )

    chunks = _chunks(payload)
    expanded = zlib.compress(bytes(10_000), level=9)

    def chunk(name: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + name
            + body
            + struct.pack(">I", zlib.crc32(name + body) & 0xFFFFFFFF)
        )

    oversized = bytearray(b"\x89PNG\r\n\x1a\n")
    oversized.extend(chunk(b"IHDR", chunks[0][1]))
    oversized.extend(chunk(b"IDAT", expanded))
    oversized.extend(chunk(b"IEND", b""))
    with pytest.raises(RGB8PNGError, match="^png_profile_invalid$"):
        probe_rgb8_png(
            oversized,
            maximum_width=8192,
            maximum_height=8192,
            maximum_pixels=4_194_304,
            maximum_bytes=PNG_IMAGE_MAX_BYTES,
        )


def test_png_probe_explicitly_clears_owned_decode_buffers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = encode_rgb8_png(
        bytes((1, 2, 3, 4, 5, 6)),
        width=2,
        height=1,
        maximum_bytes=PNG_IMAGE_MAX_BYTES,
    )
    payload_before = bytes(payload)
    owned: list[bytearray] = []

    class TrackedBytearray(bytearray):
        def __init__(self, value: bytes | bytearray | int = b"") -> None:
            super().__init__(value)
            owned.append(self)

    monkeypatch.setattr(png_module, "bytearray", TrackedBytearray, raising=False)
    probe = probe_rgb8_png(
        bytes(payload),
        maximum_width=8192,
        maximum_height=8192,
        maximum_pixels=4_194_304,
        maximum_bytes=PNG_IMAGE_MAX_BYTES,
    )

    assert (probe.width, probe.height) == (2, 1)
    assert bytes(payload) == payload_before
    assert len(owned) >= 4
    assert all(not buffer for buffer in owned)
