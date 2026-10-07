"""Lexical admission of the POSIX directory locators a ComfyUI host reports for its own roots.

`media_runtime_discovery_worker.lexical_local_directory` is the Windows counterpart. Both are purely
lexical: no filesystem access, no variable or user expansion, no quote stripping. They decide only
whether a host-reported string is a plain absolute directory locator; the callers still run the
link-free-prefix and served-root overlap checks against the real filesystem afterwards.
"""

from __future__ import annotations

from pathlib import PurePosixPath

#: Linux `PATH_MAX` counts the terminating NUL, so a usable path is at most 4095 bytes.
MAX_POSIX_LOCATOR_BYTES = 4095
#: Linux `NAME_MAX`: the longest single directory entry name, in bytes.
MAX_POSIX_SEGMENT_BYTES = 255


def lexical_posix_directory(value: object) -> PurePosixPath | None:
    """Return a normalized absolute POSIX directory locator, or `None` if it is not one.

    Refused: anything but an exact `str`; relative paths; a leading `//` (its meaning is
    implementation-defined in POSIX); NUL and other C0 or DEL control characters; text that is not
    UTF-8 encodable (lone surrogates); more than 4095 encoded bytes; a `..` segment; a segment
    longer than 255 encoded bytes. Repeated `/` and `.` segments are normalized away.
    """

    if type(value) is not str or not value.startswith("/") or value.startswith("//"):
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > MAX_POSIX_LOCATOR_BYTES:
        return None
    segments = [segment for segment in value.split("/") if segment not in {"", "."}]
    for segment in segments:
        if segment == ".." or len(segment.encode("utf-8")) > MAX_POSIX_SEGMENT_BYTES:
            return None
    return PurePosixPath("/" + "/".join(segments))


__all__ = ["MAX_POSIX_LOCATOR_BYTES", "MAX_POSIX_SEGMENT_BYTES", "lexical_posix_directory"]
