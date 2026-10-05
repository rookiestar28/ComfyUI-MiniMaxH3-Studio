"""Deterministic bounded RGB8 PNG encoding for private Authoring derivatives."""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import zlib
from collections.abc import Callable
from dataclasses import dataclass

PNG_PROFILE_ID = "h3.authoring.png.rgb8.v1"
PNG_ENCODER_ID = "h3.repo.png.rgb8.zfixed.v1"
PNG_PROBE_ID = "h3.repo.png.rgb8.probe.v1"
PNG_MEDIA_TYPE = "image/png"
PNG_IMAGE_MAX_BYTES = 16 * 1024 * 1024
PNG_THUMBNAIL_MAX_BYTES = 512 * 1024
PNG_MAX_EDGE = 8192
PNG_MAX_PIXELS = 4_194_304
THUMBNAIL_MAX_EDGE = 320

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IHDR = b"IHDR"
_IDAT = b"IDAT"
_IEND = b"IEND"
_MAX_PROFILE_BYTES = PNG_IMAGE_MAX_BYTES


class RGB8PNGError(RuntimeError):
    """One closed PNG generation/probe failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _sha256(payload: bytes | bytearray) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def png_generator_profile_fingerprint() -> str:
    """Bind deterministic bytes to the exact language and zlib implementation profile."""

    payload = {
        "algorithm": PNG_ENCODER_ID,
        "chunks": ["IHDR", "IDAT", "IEND"],
        "color_type": 2,
        "compression_level": 9,
        "filter": 0,
        "interlace": 0,
        "mem_level": 9,
        "profile_id": PNG_PROFILE_ID,
        "python": ".".join(str(value) for value in sys.version_info[:3]),
        "strategy": "Z_FIXED",
        "zlib_compile": zlib.ZLIB_VERSION,
        "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return _sha256(encoded)


@dataclass(frozen=True, slots=True)
class RGB8PNGProbe:
    width: int
    height: int
    byte_count: int
    payload_fingerprint: str
    rgb_fingerprint: str
    profile_fingerprint: str = png_generator_profile_fingerprint()
    profile_id: str = PNG_PROFILE_ID
    media_type: str = PNG_MEDIA_TYPE


def _positive_bound(value: object, *, maximum: int) -> bool:
    return type(value) is int and 1 <= value <= maximum


def _chunk(name: bytes, body: bytes | bytearray) -> bytes:
    length = struct.pack(">I", len(body))
    checksum = struct.pack(">I", zlib.crc32(name + body) & 0xFFFFFFFF)
    return length + name + body + checksum


def encode_rgb8_png(
    rgb24: bytes | bytearray,
    *,
    width: int,
    height: int,
    maximum_bytes: int,
    guard: Callable[[], None] | None = None,
) -> bytearray:
    """Encode one exact row-major RGB8 body as a closed standard PNG profile."""

    if (
        type(rgb24) not in {bytes, bytearray}
        or not _positive_bound(width, maximum=PNG_MAX_EDGE)
        or not _positive_bound(height, maximum=PNG_MAX_EDGE)
        or width * height > PNG_MAX_PIXELS
        or len(rgb24) != width * height * 3
        or (guard is not None and not callable(guard))
    ):
        raise RGB8PNGError("png_input_invalid")
    if not _positive_bound(maximum_bytes, maximum=_MAX_PROFILE_BYTES):
        raise RGB8PNGError("resource_limit")

    compressed = bytearray()
    row = bytearray()
    result = bytearray()
    try:
        if guard is not None:
            guard()
        compressor = zlib.compressobj(
            level=9,
            method=zlib.DEFLATED,
            wbits=zlib.MAX_WBITS,
            memLevel=9,
            strategy=zlib.Z_FIXED,
        )
        row_bytes = width * 3
        for row_index in range(height):
            if guard is not None:
                guard()
            start = row_index * row_bytes
            row = bytearray(1 + row_bytes)
            row[1:] = rgb24[start : start + row_bytes]
            compressed.extend(compressor.compress(row))
            row.clear()
            # Account while building rather than discovering an oversized derivative at publish.
            if len(compressed) + 57 > maximum_bytes:
                raise RGB8PNGError("resource_limit")
        compressed.extend(compressor.flush(zlib.Z_FINISH))
        if guard is not None:
            guard()

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        result = bytearray(_PNG_SIGNATURE)
        result.extend(_chunk(_IHDR, ihdr))
        result.extend(_chunk(_IDAT, compressed))
        result.extend(_chunk(_IEND, b""))
        if len(result) > maximum_bytes:
            raise RGB8PNGError("resource_limit")
        if guard is not None:
            guard()
        owned = result
        result = bytearray()
        return owned
    except RGB8PNGError:
        raise
    except (MemoryError, OverflowError, struct.error, ValueError, zlib.error) as exc:
        raise RGB8PNGError("generation_failed") from exc
    finally:
        row.clear()
        compressed.clear()
        result.clear()


def _parse_chunks(payload: bytes | bytearray) -> tuple[bytes, bytes, bytes]:
    if len(payload) < 57 or bytes(payload[:8]) != _PNG_SIGNATURE:
        raise RGB8PNGError("png_profile_invalid")
    offset = len(_PNG_SIGNATURE)
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(payload):
        if len(chunks) >= 3 or len(payload) - offset < 12:
            raise RGB8PNGError("png_profile_invalid")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        end = offset + 12 + length
        if length > _MAX_PROFILE_BYTES or end > len(payload):
            raise RGB8PNGError("png_profile_invalid")
        name = bytes(payload[offset + 4 : offset + 8])
        body = bytes(payload[offset + 8 : offset + 8 + length])
        observed_crc = struct.unpack(">I", payload[offset + 8 + length : end])[0]
        if observed_crc != zlib.crc32(name + body) & 0xFFFFFFFF:
            raise RGB8PNGError("png_profile_invalid")
        chunks.append((name, body))
        offset = end
    if offset != len(payload) or [name for name, _body in chunks] != [_IHDR, _IDAT, _IEND]:
        raise RGB8PNGError("png_profile_invalid")
    ihdr, compressed, iend = (body for _name, body in chunks)
    if len(ihdr) != 13 or not compressed or iend:
        raise RGB8PNGError("png_profile_invalid")
    return ihdr, compressed, iend


def probe_rgb8_png(
    payload: bytes | bytearray,
    *,
    maximum_width: int,
    maximum_height: int,
    maximum_pixels: int,
    maximum_bytes: int,
) -> RGB8PNGProbe:
    """Strictly inspect only the encoder's metadata-free RGB8 PNG profile."""

    if type(payload) not in {bytes, bytearray}:
        raise RGB8PNGError("png_profile_invalid")
    if (
        not _positive_bound(maximum_width, maximum=PNG_MAX_EDGE)
        or not _positive_bound(maximum_height, maximum=PNG_MAX_EDGE)
        or not _positive_bound(maximum_pixels, maximum=PNG_MAX_PIXELS)
        or not _positive_bound(maximum_bytes, maximum=_MAX_PROFILE_BYTES)
        or len(payload) > maximum_bytes
    ):
        raise RGB8PNGError("resource_limit")

    # IMPORTANT: keep partial decode buffers explicitly owned; locals() based cleanup is
    # rejected by the offline security audit and obscures failure-path buffer erasure.
    raw = bytearray()
    rgb = bytearray()
    try:
        ihdr, compressed, _iend = _parse_chunks(payload)
        width, height, depth, color, compression, filtering, interlace = struct.unpack(
            ">IIBBBBB", ihdr
        )
        if (
            not 1 <= width <= maximum_width
            or not 1 <= height <= maximum_height
            or width * height > maximum_pixels
        ):
            raise RGB8PNGError("resource_limit")
        if (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0):
            raise RGB8PNGError("png_profile_invalid")

        expected_raw_bytes = height * (1 + width * 3)
        decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS)
        raw = bytearray(decompressor.decompress(compressed, expected_raw_bytes + 1))
        if (
            len(raw) != expected_raw_bytes
            or not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
        ):
            raise RGB8PNGError("png_profile_invalid")
        rgb = bytearray(width * height * 3)
        row_bytes = width * 3
        for row_index in range(height):
            raw_start = row_index * (row_bytes + 1)
            if raw[raw_start] != 0:
                raise RGB8PNGError("png_profile_invalid")
            rgb_start = row_index * row_bytes
            rgb[rgb_start : rgb_start + row_bytes] = raw[raw_start + 1 : raw_start + 1 + row_bytes]
        return RGB8PNGProbe(
            width=width,
            height=height,
            byte_count=len(payload),
            payload_fingerprint=_sha256(payload),
            rgb_fingerprint=_sha256(rgb),
        )
    except RGB8PNGError:
        raise
    except (MemoryError, OverflowError, struct.error, ValueError, zlib.error) as exc:
        raise RGB8PNGError("png_profile_invalid") from exc
    finally:
        raw.clear()
        rgb.clear()


__all__ = [
    "PNG_ENCODER_ID",
    "PNG_IMAGE_MAX_BYTES",
    "PNG_MAX_EDGE",
    "PNG_MAX_PIXELS",
    "PNG_MEDIA_TYPE",
    "PNG_PROBE_ID",
    "PNG_PROFILE_ID",
    "PNG_THUMBNAIL_MAX_BYTES",
    "RGB8PNGError",
    "RGB8PNGProbe",
    "THUMBNAIL_MAX_EDGE",
    "encode_rgb8_png",
    "png_generator_profile_fingerprint",
    "probe_rgb8_png",
]
