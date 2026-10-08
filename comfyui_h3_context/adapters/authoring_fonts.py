"""File-backed authority for the repository-packaged authoring fonts.

The browser and backend consume the same closed manifest.  This module never searches system font
directories and never accepts a caller-provided font path.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

from ..core.canonical import canonical_fingerprint

FONT_MANIFEST_SCHEMA: Final = "h3.authoring.packaged_font_manifest.v1"
FONT_MANIFEST_PROFILE_ID: Final = "h3.authoring.font_profile.v1"
FONT_MANIFEST_FILENAME: Final = "font_manifest_v1.json"

_PUBLIC_WEIGHTS: Final = frozenset({400, 700})
_PUBLIC_STYLES: Final = frozenset({"normal", "italic"})
_PUBLIC_STYLE_KEYS: Final = frozenset(
    (weight, style) for weight in _PUBLIC_WEIGHTS for style in _PUBLIC_STYLES
)
_LAYOUT_CODEPOINTS: Final = frozenset({0x09, 0x0A})
_MAX_MANIFEST_BYTES: Final = 512 * 1024
_MAX_FONT_BYTES: Final = 8 * 1024 * 1024
_MAX_LICENSE_BYTES: Final = 64 * 1024
_MAX_CODEPOINTS: Final = 65_536
_MAX_RANGES: Final = 4_096
_HEX_SHA256_LENGTH: Final = 64

_ERROR_CODES: Final = frozenset(
    {
        "font_manifest_invalid",
        "font_artifact_missing",
        "font_artifact_hash_mismatch",
        "font_license_missing",
        "font_license_hash_mismatch",
        "font_asset_unsupported",
        "font_style_unsupported",
        "font_glyph_unsupported",
    }
)


class AuthoringFontError(RuntimeError):
    """A content-free typed blocker safe to map into an Authoring disposition."""

    def __init__(self, code: str, message: str) -> None:
        if code not in _ERROR_CODES:
            code = "font_manifest_invalid"
        super().__init__(f"{code}: {message}")
        self.code = code


def _fail(code: str, message: str) -> AuthoringFontError:
    return AuthoringFontError(code, message)


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _git_blob_sha1(payload: bytes) -> str:
    framed = f"blob {len(payload)}\0".encode("ascii") + payload
    # Git object identity is SHA-1 by contract; SHA-256 remains the artifact integrity authority.
    return hashlib.sha1(framed, usedforsecurity=False).hexdigest()


def _sha256_value(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != len("sha256:") + _HEX_SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise _fail("font_manifest_invalid", f"{field_name} must be a lowercase SHA-256")
    return value


def _git_oid(value: object, field_name: str) -> str:
    oid = _string(value, field_name, maximum=40)
    if len(oid) != 40 or any(character not in "0123456789abcdef" for character in oid):
        raise _fail("font_manifest_invalid", f"{field_name} must be a lowercase Git SHA-1")
    return oid


def _string(value: object, field_name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _fail("font_manifest_invalid", f"{field_name} must be a bounded string")
    return value


def _integer(value: object, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _fail("font_manifest_invalid", f"{field_name} is outside its closed bounds")
    return value


def _mapping(value: object, keys: Sequence[str], field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise _fail("font_manifest_invalid", f"{field_name} must be a closed object")
    if not all(isinstance(key, str) for key in value):
        raise _fail("font_manifest_invalid", f"{field_name} keys must be strings")
    return value


def _array(value: object, field_name: str, *, maximum: int) -> list[object]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise _fail("font_manifest_invalid", f"{field_name} must be a bounded non-empty array")
    return value


def _safe_package_file(
    package_root: Path,
    package_path: object,
    *,
    missing_code: str,
) -> tuple[str, Path]:
    relative = _string(package_path, "package_path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative or pure.parts[0] != "fonts":
        raise _fail("font_manifest_invalid", "package_path is outside the packaged font directory")
    candidate = package_root.joinpath(*pure.parts)
    try:
        resolved_root = package_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise _fail(missing_code, "a declared packaged artifact is unavailable") from exc
    # SECURITY: manifest paths stay beneath the installed package and cannot select a system font.
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise _fail("font_manifest_invalid", "a packaged artifact escapes the package root")
    cursor = resolved_root
    for part in pure.parts:
        cursor /= part
        if cursor.is_symlink():
            raise _fail("font_manifest_invalid", "packaged font artifacts cannot be symbolic links")
    return relative, resolved


def _read_bounded(path: Path, maximum: int, missing_code: str) -> bytes:
    try:
        size = path.stat().st_size
        if not 1 <= size <= maximum:
            raise _fail("font_manifest_invalid", "a packaged artifact exceeds its size profile")
        payload = path.read_bytes()
    except AuthoringFontError:
        raise
    except OSError as exc:
        raise _fail(missing_code, "a declared packaged artifact is unavailable") from exc
    if len(payload) != size:
        raise _fail(missing_code, "a declared packaged artifact changed while reading")
    return payload


def _sfnt_tables(payload: bytes) -> dict[bytes, tuple[int, int]]:
    if len(payload) < 12:
        raise _fail("font_manifest_invalid", "a packaged font has no SFNT header")
    signature, table_count = struct.unpack_from(">IH", payload, 0)
    if signature not in {0x00010000, 0x4F54544F} or not 1 <= table_count <= 128:
        raise _fail("font_manifest_invalid", "a packaged font uses an unsupported SFNT profile")
    directory_end = 12 + table_count * 16
    if directory_end > len(payload):
        raise _fail("font_manifest_invalid", "a packaged font table directory is truncated")
    tables: dict[bytes, tuple[int, int]] = {}
    for index in range(table_count):
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", payload, 12 + index * 16)
        if tag in tables or offset > len(payload) or length > len(payload) - offset:
            raise _fail("font_manifest_invalid", "a packaged font table is malformed")
        tables[tag] = (offset, length)
    return tables


def _decode_name(payload: bytes, platform_id: int) -> str | None:
    try:
        if platform_id in {0, 3}:
            return payload.decode("utf-16-be").strip("\x00")
        if platform_id == 1:
            return payload.decode("mac_roman").strip("\x00")
    except UnicodeDecodeError:
        return None
    return None


def _sfnt_names(payload: bytes, tables: Mapping[bytes, tuple[int, int]]) -> dict[int, str]:
    table = tables.get(b"name")
    if table is None:
        raise _fail("font_manifest_invalid", "a packaged font has no name table")
    offset, length = table
    if length < 6:
        raise _fail("font_manifest_invalid", "a packaged font name table is truncated")
    _format, count, storage_offset = struct.unpack_from(">HHH", payload, offset)
    records_end = 6 + count * 12
    if count > 4_096 or records_end > length or storage_offset > length:
        raise _fail("font_manifest_invalid", "a packaged font name table is malformed")
    candidates: dict[int, tuple[int, str]] = {}
    for index in range(count):
        record = offset + 6 + index * 12
        platform_id, _encoding_id, language_id, name_id, size, relative = struct.unpack_from(
            ">HHHHHH", payload, record
        )
        start = offset + storage_offset + relative
        end = start + size
        if start < offset or end > offset + length:
            raise _fail("font_manifest_invalid", "a packaged font name record is malformed")
        if name_id not in {1, 2, 5, 6}:
            continue
        decoded = _decode_name(payload[start:end], platform_id)
        if not decoded:
            continue
        priority = 3 if platform_id == 3 and language_id == 0x0409 else 2 if platform_id == 3 else 1
        existing = candidates.get(name_id)
        if existing is None or priority > existing[0]:
            candidates[name_id] = (priority, decoded)
    if set(candidates) != {1, 2, 5, 6}:
        raise _fail("font_manifest_invalid", "a packaged font is missing required build names")
    return {name_id: value for name_id, (_priority, value) in candidates.items()}


def _format4_codepoints(table: bytes) -> set[int]:
    if len(table) < 16:
        raise _fail("font_manifest_invalid", "a packaged font cmap format 4 is truncated")
    length, segment_count_x2 = (
        struct.unpack_from(">HH", table, 2)[0],
        struct.unpack_from(">H", table, 6)[0],
    )
    segment_count = segment_count_x2 // 2
    if segment_count_x2 % 2 or not 1 <= segment_count <= 4_096 or length > len(table):
        raise _fail("font_manifest_invalid", "a packaged font cmap format 4 is malformed")
    end_codes_offset = 14
    start_codes_offset = end_codes_offset + 2 * segment_count + 2
    deltas_offset = start_codes_offset + 2 * segment_count
    ranges_offset = deltas_offset + 2 * segment_count
    if ranges_offset + 2 * segment_count > length:
        raise _fail("font_manifest_invalid", "a packaged font cmap format 4 is truncated")
    result: set[int] = set()
    for index in range(segment_count):
        end_code = struct.unpack_from(">H", table, end_codes_offset + 2 * index)[0]
        start_code = struct.unpack_from(">H", table, start_codes_offset + 2 * index)[0]
        delta = struct.unpack_from(">h", table, deltas_offset + 2 * index)[0]
        range_offset = struct.unpack_from(">H", table, ranges_offset + 2 * index)[0]
        if start_code > end_code:
            raise _fail("font_manifest_invalid", "a packaged font cmap range is reversed")
        for codepoint in range(start_code, min(end_code, 0xFFFE) + 1):
            if range_offset == 0:
                glyph_id = (codepoint + delta) & 0xFFFF
            else:
                glyph_offset = (
                    ranges_offset + 2 * index + range_offset + 2 * (codepoint - start_code)
                )
                if glyph_offset + 2 > length:
                    raise _fail("font_manifest_invalid", "a packaged font cmap glyph is truncated")
                glyph_id = struct.unpack_from(">H", table, glyph_offset)[0]
                if glyph_id:
                    glyph_id = (glyph_id + delta) & 0xFFFF
            if glyph_id:
                result.add(codepoint)
    return result


def _format12_or_13_codepoints(table: bytes, format_id: int) -> set[int]:
    if len(table) < 16:
        raise _fail("font_manifest_invalid", "a packaged font cmap group table is truncated")
    length, group_count = (
        struct.unpack_from(">I", table, 4)[0],
        struct.unpack_from(">I", table, 12)[0],
    )
    if group_count > 65_536 or length > len(table) or 16 + 12 * group_count > length:
        raise _fail("font_manifest_invalid", "a packaged font cmap group table is malformed")
    result: set[int] = set()
    previous_end = -1
    codepoint_count = 0
    for index in range(group_count):
        start, end, glyph = struct.unpack_from(">III", table, 16 + index * 12)
        if start > end or end > 0x10FFFF or start <= previous_end:
            raise _fail("font_manifest_invalid", "a packaged font cmap group is malformed")
        codepoint_count += end - start + 1
        if codepoint_count > _MAX_CODEPOINTS:
            raise _fail("font_manifest_invalid", "a packaged font cmap exceeds its resource limit")
        previous_end = end
        for codepoint in range(start, end + 1):
            mapped = glyph + codepoint - start if format_id == 12 else glyph
            if mapped:
                result.add(codepoint)
    return result


def _sfnt_codepoints(payload: bytes, tables: Mapping[bytes, tuple[int, int]]) -> frozenset[int]:
    table = tables.get(b"cmap")
    if table is None:
        raise _fail("font_manifest_invalid", "a packaged font has no cmap table")
    offset, length = table
    if length < 4:
        raise _fail("font_manifest_invalid", "a packaged font cmap table is truncated")
    _version, subtable_count = struct.unpack_from(">HH", payload, offset)
    if not 1 <= subtable_count <= 256 or 4 + 8 * subtable_count > length:
        raise _fail("font_manifest_invalid", "a packaged font cmap directory is malformed")
    codepoints: set[int] = set()
    supported_seen = False
    for index in range(subtable_count):
        platform_id, encoding_id, relative = struct.unpack_from(
            ">HHI", payload, offset + 4 + index * 8
        )
        if platform_id != 0 and not (platform_id == 3 and encoding_id in {1, 10}):
            continue
        start = offset + relative
        if start + 2 > offset + length:
            raise _fail("font_manifest_invalid", "a packaged font cmap subtable is malformed")
        format_id = struct.unpack_from(">H", payload, start)[0]
        if format_id == 4:
            subtable_length = struct.unpack_from(">H", payload, start + 2)[0]
            if start + subtable_length > offset + length:
                raise _fail("font_manifest_invalid", "a packaged font cmap subtable is truncated")
            codepoints.update(_format4_codepoints(payload[start : start + subtable_length]))
            supported_seen = True
        elif format_id in {12, 13}:
            if start + 8 > offset + length:
                raise _fail("font_manifest_invalid", "a packaged font cmap subtable is truncated")
            subtable_length = struct.unpack_from(">I", payload, start + 4)[0]
            if start + subtable_length > offset + length:
                raise _fail("font_manifest_invalid", "a packaged font cmap subtable is truncated")
            codepoints.update(
                _format12_or_13_codepoints(payload[start : start + subtable_length], format_id)
            )
            supported_seen = True
    if not supported_seen or not 1 <= len(codepoints) <= _MAX_CODEPOINTS:
        raise _fail("font_manifest_invalid", "a packaged font has no bounded Unicode cmap")
    return frozenset(codepoints)


def _codepoint_bytes(codepoints: Sequence[int]) -> bytes:
    return b"".join(struct.pack(">I", codepoint) for codepoint in codepoints)


def _codepoint_ranges(codepoints: Sequence[int]) -> tuple[tuple[int, int], ...]:
    if not codepoints:
        return ()
    ranges: list[tuple[int, int]] = []
    start = end = codepoints[0]
    for codepoint in codepoints[1:]:
        if codepoint == end + 1:
            end = codepoint
        else:
            ranges.append((start, end))
            start = end = codepoint
    ranges.append((start, end))
    return tuple(ranges)


@dataclass(frozen=True, slots=True)
class ResolvedPackagedFont:
    artifact_id: str
    font_asset_id: str
    family_name: str
    package_path: str
    weight: int
    style: str
    size_bytes: int
    file_fingerprint: str
    upstream_git_blob_sha1: str
    cmap_fingerprint: str
    covered_codepoint_count: int
    build_version: str
    postscript_name: str
    _covered_codepoints: frozenset[int] = field(repr=False, compare=False)
    _filesystem_path: Path = field(repr=False, compare=False)

    def read_verified_bytes(self) -> bytes:
        payload = _read_bounded(self._filesystem_path, _MAX_FONT_BYTES, "font_artifact_missing")
        if len(payload) != self.size_bytes or _sha256(payload) != self.file_fingerprint:
            raise _fail("font_artifact_hash_mismatch", "a packaged font differs from its manifest")
        return payload

    def covers(self, text: str) -> bool:
        return all(
            ord(character) in _LAYOUT_CODEPOINTS or ord(character) in self._covered_codepoints
            for character in text
        )


@dataclass(frozen=True, slots=True)
class PackagedFontManifest:
    schema_version: str
    profile_id: str
    fallback_order: tuple[str, ...]
    faces: tuple[ResolvedPackagedFont, ...]
    license_spdx_id: str
    license_file_fingerprint: str
    manifest_fingerprint: str
    package_fingerprint: str

    @property
    def faces_by_style(self) -> Mapping[tuple[int, str], ResolvedPackagedFont]:
        primary = self.fallback_order[0]
        return {
            (face.weight, face.style): face for face in self.faces if face.font_asset_id == primary
        }


def _load_packaged_font_manifest_from_root(package_root: Path) -> PackagedFontManifest:
    _relative_manifest_path, manifest_path = _safe_package_file(
        package_root,
        f"fonts/{FONT_MANIFEST_FILENAME}",
        missing_code="font_artifact_missing",
    )
    try:
        payload = _read_bounded(manifest_path, _MAX_MANIFEST_BYTES, "font_artifact_missing")
        decoded = json.loads(payload)
    except AuthoringFontError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _fail("font_manifest_invalid", "the packaged font manifest is invalid JSON") from exc
    root = _mapping(
        decoded,
        (
            "schema_version",
            "profile_id",
            "font_assets",
            "fallback_order",
            "coverage_profiles",
            "license",
        ),
        "manifest",
    )
    if (
        root["schema_version"] != FONT_MANIFEST_SCHEMA
        or root["profile_id"] != FONT_MANIFEST_PROFILE_ID
    ):
        raise _fail("font_manifest_invalid", "the packaged font manifest profile is unsupported")

    license_wire = _mapping(
        root["license"],
        ("spdx_id", "package_path", "file_sha256", "upstream_revision"),
        "license",
    )
    if license_wire["spdx_id"] != "OFL-1.1":
        raise _fail("font_manifest_invalid", "the packaged font license is unsupported")
    license_path_value, license_path = _safe_package_file(
        package_root, license_wire["package_path"], missing_code="font_license_missing"
    )
    if not license_path_value.endswith(".txt"):
        raise _fail("font_manifest_invalid", "the packaged font license artifact is invalid")
    license_payload = _read_bounded(license_path, _MAX_LICENSE_BYTES, "font_license_missing")
    license_hash = _sha256_value(license_wire["file_sha256"], "license.file_sha256")
    # CRITICAL: Git for Windows can check these bounded text bytes out as CRLF. Hash only
    # CRLF -> LF equivalence; raw hashes reject valid installs, broader cleanup hides tampering.
    if _sha256(license_payload.replace(b"\r\n", b"\n")) != license_hash:
        raise _fail("font_license_hash_mismatch", "the packaged font license hash differs")
    license_revision = _git_oid(license_wire["upstream_revision"], "license.upstream_revision")
    if (
        b"SIL OPEN FONT LICENSE Version 1.1" not in license_payload
        or b"Copyright 2022 The Noto Project Authors" not in license_payload
    ):
        raise _fail("font_manifest_invalid", "the packaged font license content is unsupported")

    coverage_by_id: dict[str, frozenset[int]] = {}
    for index, raw_profile in enumerate(
        _array(root["coverage_profiles"], "coverage_profiles", maximum=64)
    ):
        profile = _mapping(
            raw_profile,
            ("profile_id", "codepoint_count", "ranges", "codepoint_sha256"),
            f"coverage_profiles[{index}]",
        )
        profile_id = _string(profile["profile_id"], f"coverage_profiles[{index}].profile_id")
        if profile_id in coverage_by_id:
            raise _fail("font_manifest_invalid", "coverage profile IDs must be unique")
        ranges_raw = _array(
            profile["ranges"], f"coverage_profiles[{index}].ranges", maximum=_MAX_RANGES
        )
        codepoints: list[int] = []
        previous_end = -1
        for raw_range in ranges_raw:
            if not isinstance(raw_range, list) or len(raw_range) != 2:
                raise _fail("font_manifest_invalid", "coverage ranges must be closed pairs")
            start = _integer(raw_range[0], "coverage range start", 0, 0x10FFFF)
            end = _integer(raw_range[1], "coverage range end", start, 0x10FFFF)
            if start <= previous_end:
                raise _fail("font_manifest_invalid", "coverage ranges must be ordered and disjoint")
            if len(codepoints) + end - start + 1 > _MAX_CODEPOINTS:
                raise _fail("font_manifest_invalid", "coverage profile exceeds its resource limit")
            codepoints.extend(range(start, end + 1))
            previous_end = end
        declared = frozenset(codepoints)
        if len(declared) != _integer(
            profile["codepoint_count"], "coverage codepoint_count", 1, _MAX_CODEPOINTS
        ):
            raise _fail("font_manifest_invalid", "coverage count differs from its ranges")
        expected_coverage_hash = _sha256_value(
            profile["codepoint_sha256"], "coverage codepoint_sha256"
        )
        if _sha256(_codepoint_bytes(sorted(declared))) != expected_coverage_hash:
            raise _fail("font_manifest_invalid", "coverage fingerprint differs from its ranges")
        coverage_by_id[profile_id] = declared

    faces: list[ResolvedPackagedFont] = []
    assets_seen: set[str] = set()
    face_ids_seen: set[str] = set()
    style_keys_seen: set[tuple[str, int, str]] = set()
    raw_assets = _array(root["font_assets"], "font_assets", maximum=16)
    if len(raw_assets) != 1:
        raise _fail("font_manifest_invalid", "the v1 font profile requires exactly one font asset")
    for asset_index, raw_asset in enumerate(raw_assets):
        asset = _mapping(
            raw_asset,
            (
                "font_asset_id",
                "family_name",
                "release_id",
                "build_revision",
                "distribution_revision",
                "faces",
            ),
            f"font_assets[{asset_index}]",
        )
        asset_id = _string(asset["font_asset_id"], "font_asset_id")
        if asset_id in assets_seen:
            raise _fail("font_manifest_invalid", "font asset IDs must be unique")
        assets_seen.add(asset_id)
        family_name = _string(asset["family_name"], "family_name")
        _string(asset["release_id"], "release_id")
        build_revision = _git_oid(asset["build_revision"], "build_revision")
        _git_oid(asset["distribution_revision"], "distribution_revision")
        if build_revision != license_revision:
            raise _fail("font_manifest_invalid", "the font build and license revisions differ")
        for face_index, raw_face in enumerate(_array(asset["faces"], "faces", maximum=16)):
            face = _mapping(
                raw_face,
                (
                    "artifact_id",
                    "package_path",
                    "weight",
                    "style",
                    "size_bytes",
                    "file_sha256",
                    "upstream_git_blob_sha1",
                    "postscript_name",
                    "subfamily_name",
                    "build_version",
                    "coverage_profile_id",
                    "cmap_sha256",
                ),
                f"font_assets[{asset_index}].faces[{face_index}]",
            )
            artifact_id = _string(face["artifact_id"], "artifact_id")
            if artifact_id in face_ids_seen:
                raise _fail("font_manifest_invalid", "font artifact IDs must be unique")
            face_ids_seen.add(artifact_id)
            weight = _integer(face["weight"], "weight", 400, 700)
            style = _string(face["style"], "style", maximum=16)
            if weight not in _PUBLIC_WEIGHTS or style not in _PUBLIC_STYLES:
                raise _fail(
                    "font_manifest_invalid", "a font face expands the public style vocabulary"
                )
            style_key = (asset_id, weight, style)
            if style_key in style_keys_seen:
                raise _fail("font_manifest_invalid", "font face styles must be unique per asset")
            style_keys_seen.add(style_key)
            package_path_value, font_path = _safe_package_file(
                package_root, face["package_path"], missing_code="font_artifact_missing"
            )
            if not package_path_value.endswith(".ttf"):
                raise _fail("font_manifest_invalid", "only static packaged TTF faces are admitted")
            font_payload = _read_bounded(font_path, _MAX_FONT_BYTES, "font_artifact_missing")
            expected_size = _integer(face["size_bytes"], "size_bytes", 1, _MAX_FONT_BYTES)
            expected_hash = _sha256_value(face["file_sha256"], "file_sha256")
            if len(font_payload) != expected_size or _sha256(font_payload) != expected_hash:
                raise _fail(
                    "font_artifact_hash_mismatch", "a packaged font differs from its manifest"
                )
            blob_sha1 = _git_oid(face["upstream_git_blob_sha1"], "upstream_git_blob_sha1")
            if _git_blob_sha1(font_payload) != blob_sha1:
                raise _fail(
                    "font_artifact_hash_mismatch",
                    "a packaged font differs from its upstream Git blob identity",
                )
            tables = _sfnt_tables(font_payload)
            names = _sfnt_names(font_payload, tables)
            postscript_name = _string(face["postscript_name"], "postscript_name")
            subfamily_name = _string(face["subfamily_name"], "subfamily_name")
            build_version = _string(face["build_version"], "build_version")
            if (
                names[1] != family_name
                or names[2] != subfamily_name
                or names[5] != build_version
                or names[6] != postscript_name
            ):
                raise _fail("font_manifest_invalid", "a packaged font build name differs")
            actual_codepoints = _sfnt_codepoints(font_payload, tables)
            coverage_profile_id = _string(face["coverage_profile_id"], "coverage_profile_id")
            declared_codepoints = coverage_by_id.get(coverage_profile_id)
            if declared_codepoints is None or actual_codepoints != declared_codepoints:
                raise _fail(
                    "font_manifest_invalid",
                    "a packaged font cmap differs from its coverage profile",
                )
            cmap_hash = _sha256_value(face["cmap_sha256"], "cmap_sha256")
            if _sha256(_codepoint_bytes(sorted(actual_codepoints))) != cmap_hash:
                raise _fail("font_manifest_invalid", "a packaged font cmap fingerprint differs")
            faces.append(
                ResolvedPackagedFont(
                    artifact_id=artifact_id,
                    font_asset_id=asset_id,
                    family_name=family_name,
                    package_path=package_path_value,
                    weight=weight,
                    style=style,
                    size_bytes=expected_size,
                    file_fingerprint=expected_hash,
                    upstream_git_blob_sha1=blob_sha1,
                    cmap_fingerprint=cmap_hash,
                    covered_codepoint_count=len(actual_codepoints),
                    build_version=build_version,
                    postscript_name=postscript_name,
                    _covered_codepoints=actual_codepoints,
                    _filesystem_path=font_path,
                )
            )

    for asset_id in assets_seen:
        actual_styles = {
            (face.weight, face.style) for face in faces if face.font_asset_id == asset_id
        }
        if actual_styles != _PUBLIC_STYLE_KEYS:
            raise _fail(
                "font_manifest_invalid",
                "each v1 font asset must cover the exact public weight/style vocabulary",
            )

    fallback_raw = _array(root["fallback_order"], "fallback_order", maximum=16)
    fallback_order = tuple(_string(value, "fallback_order entry") for value in fallback_raw)
    if len(set(fallback_order)) != len(fallback_order) or set(fallback_order) != assets_seen:
        raise _fail(
            "font_manifest_invalid", "fallback_order must contain every font asset exactly once"
        )
    for asset_id in fallback_order:
        if not any(face.font_asset_id == asset_id for face in faces):
            raise _fail("font_manifest_invalid", "fallback_order names an empty font asset")

    package_fingerprint = canonical_fingerprint(
        {
            "schema_version": FONT_MANIFEST_SCHEMA,
            # Keep the package identity stable across Git's text checkout conversion, as above.
            "manifest_sha256": _sha256(payload.replace(b"\r\n", b"\n")),
            "license_sha256": license_hash,
            "font_files": [
                {
                    "artifact_id": face.artifact_id,
                    "file_sha256": face.file_fingerprint,
                    "upstream_git_blob_sha1": face.upstream_git_blob_sha1,
                    "cmap_sha256": face.cmap_fingerprint,
                }
                for face in sorted(faces, key=lambda item: item.artifact_id)
            ],
        }
    )
    return PackagedFontManifest(
        schema_version=FONT_MANIFEST_SCHEMA,
        profile_id=FONT_MANIFEST_PROFILE_ID,
        fallback_order=fallback_order,
        faces=tuple(faces),
        license_spdx_id="OFL-1.1",
        license_file_fingerprint=license_hash,
        manifest_fingerprint=canonical_fingerprint(decoded),
        package_fingerprint=package_fingerprint,
    )


def load_packaged_font_manifest() -> PackagedFontManifest:
    """Load and fully verify the product font package without consulting system fonts."""

    package_root = Path(__file__).resolve().parents[1]
    return _load_packaged_font_manifest_from_root(package_root)


def resolve_packaged_font(
    manifest: PackagedFontManifest,
    font_asset_id: str,
    weight: int,
    style: str,
) -> ResolvedPackagedFont:
    """Resolve an exact public style; weight/style substitution is forbidden."""

    if font_asset_id not in manifest.fallback_order:
        raise _fail("font_asset_unsupported", "the requested font asset is not packaged")
    for face in manifest.faces:
        if face.font_asset_id == font_asset_id and face.weight == weight and face.style == style:
            return face
    raise _fail("font_style_unsupported", "the requested font style is not packaged")


def require_text_coverage(
    manifest: PackagedFontManifest,
    font_asset_id: str,
    weight: int,
    style: str,
    text: str,
) -> ResolvedPackagedFont:
    """Resolve one exact deterministic fallback for all codepoints in ``text``."""

    if not isinstance(text, str) or not text:
        raise _fail("font_glyph_unsupported", "text must be a non-empty string")
    primary = resolve_packaged_font(manifest, font_asset_id, weight, style)
    candidates = [primary]
    for fallback_id in manifest.fallback_order:
        if fallback_id == font_asset_id:
            continue
        try:
            candidates.append(resolve_packaged_font(manifest, fallback_id, weight, style))
        except AuthoringFontError as exc:
            if exc.code != "font_style_unsupported":
                raise
    for candidate in candidates:
        if candidate.covers(text):
            return candidate
    missing = sorted(
        {
            ord(character)
            for character in text
            if ord(character) not in _LAYOUT_CODEPOINTS
            and not any(ord(character) in face._covered_codepoints for face in candidates)
        }
    )
    first = missing[0] if missing else 0
    raise _fail(
        "font_glyph_unsupported",
        f"the packaged fallback sequence does not cover U+{first:04X}",
    )
