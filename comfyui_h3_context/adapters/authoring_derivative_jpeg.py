"""Bounded structural admission for generated baseline JPEG filmstrips."""

from __future__ import annotations


class FilmstripJpegError(ValueError):
    """Closed refusal for a malformed or unsupported generated JPEG."""


def probe_filmstrip_jpeg(body: bytes | bytearray, *, maximum_bytes: int) -> tuple[int, int]:
    """Return baseline JPEG dimensions without decoding pixels.

    The browser decoder remains the pixel authority. This parser only establishes that the owned
    output is a complete, bounded baseline JPEG with one start-of-frame and scan marker.
    """

    if (
        type(body) not in (bytes, bytearray)
        or type(maximum_bytes) is not int
        or maximum_bytes < 1
        or not 4 <= len(body) <= maximum_bytes
        or body[:2] != b"\xff\xd8"
        or body[-2:] != b"\xff\xd9"
    ):
        raise FilmstripJpegError("output_invalid")
    offset = 2
    dimensions: tuple[int, int] | None = None
    scan_seen = False
    while offset < len(body) - 2:
        if body[offset] != 0xFF:
            raise FilmstripJpegError("output_invalid")
        while offset < len(body) - 2 and body[offset] == 0xFF:
            offset += 1
        if offset >= len(body) - 2:
            raise FilmstripJpegError("output_invalid")
        marker = body[offset]
        offset += 1
        if marker in {0x01, *range(0xD0, 0xD8)}:
            continue
        if marker == 0xD9:
            break
        if offset + 2 > len(body) - 2:
            raise FilmstripJpegError("output_invalid")
        segment_length = int.from_bytes(body[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(body) - 2:
            raise FilmstripJpegError("output_invalid")
        payload = offset + 2
        end = offset + segment_length
        if marker == 0xC0:
            if dimensions is not None or segment_length < 11 or body[payload] != 8:
                raise FilmstripJpegError("output_invalid")
            height = int.from_bytes(body[payload + 1 : payload + 3], "big")
            width = int.from_bytes(body[payload + 3 : payload + 5], "big")
            components = body[payload + 5]
            if width < 1 or height < 1 or components != 3:
                raise FilmstripJpegError("output_invalid")
            dimensions = (width, height)
        elif marker in range(0xC1, 0xD0) and marker not in {0xC4, 0xC8, 0xCC}:
            # CRITICAL: the filmstrip profile is baseline-only. Admitting progressive or lossless
            # SOF markers makes browser support and resource use vary by decoder.
            raise FilmstripJpegError("output_invalid")
        if marker == 0xDA:
            scan_seen = True
            break
        offset = end
    if dimensions is None or not scan_seen:
        raise FilmstripJpegError("output_invalid")
    return dimensions


__all__ = ["FilmstripJpegError", "probe_filmstrip_jpeg"]
