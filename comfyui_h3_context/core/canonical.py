"""Bounded JCS-informed canonical bytes and material context fingerprints.

The canonicalizer is intentionally narrower than RFC 8785: controlled ASCII object keys, NFC
values, explicit IEEE-754 tokens, bounded integers, and no arbitrary runtime objects.  SHA-256
fingerprints identify exact projection bytes; they do not provide authenticity.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from .context_reporting import ContextReport
from .errors import CanonicalizationError

CANONICAL_SCHEMA = "h3-context-canonical/1"
MAX_CANONICAL_DEPTH = 32
MAX_CANONICAL_ITEMS = 256
MAX_CANONICAL_STRING_LENGTH = 65_536
MAX_CANONICAL_BYTES = 1_000_000
MAX_INTEROPERABLE_INTEGER = (2**53) - 1
_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class FloatPrecision(str, Enum):
    """Explicit IEEE-754 precision used before a semantic float enters JSON."""

    BINARY32 = "binary32"
    BINARY64 = "binary64"


_MATERIAL_METADATA_BY_PARENT: dict[str | None, frozenset[str]] = {
    None: frozenset({"report_id"}),
    "plan": frozenset({"plan_id"}),
    "prompt_document": frozenset({"document_id", "plan_id"}),
    "validation": frozenset({"validation_id", "validator_version"}),
    "provider_receipt": frozenset(
        {
            "receipt_id",
            "provider_version",
            "endpoint_revision",
            "task_id",
            "redacted_message",
        }
    ),
    "steps": frozenset({"step_id"}),
    "sections": frozenset({"section_id"}),
    "limitations": frozenset({"limitation_id"}),
}


def _reject_text(value: str, field: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized) > MAX_CANONICAL_STRING_LENGTH:
        raise CanonicalizationError(f"{field} exceeds canonical string limit")
    astral = 0
    for character in normalized:
        code_point = ord(character)
        if code_point == 0 or 0xD800 <= code_point <= 0xDFFF:
            raise CanonicalizationError(f"{field} contains an unsafe Unicode code point")
        if code_point > 0xFFFF:
            astral += 1
    # CROSS-LANGUAGE: `len()` counts Unicode code points, but the browser partner
    # (frontend/src/contracts/canonicalFingerprint.ts) measures `String.length`, which counts UTF-16
    # code units.  A string of 32,769 astral characters is inside this limit and outside that one,
    # so Python would emit a fingerprint the browser could not reproduce, and the one identity
    # comparison in the repository would raise instead of comparing.  Refusing what the browser
    # already refuses closes that split in the fail-closed direction: any string inside both limits
    # keeps its exact canonical bytes and therefore its exact fingerprint.
    if len(normalized) + astral > MAX_CANONICAL_STRING_LENGTH:
        raise CanonicalizationError(f"{field} exceeds the canonical UTF-16 code-unit limit")
    return normalized


def _normalize_value(value: object, depth: int, field: str = "value") -> object:
    if depth > MAX_CANONICAL_DEPTH:
        raise CanonicalizationError("canonical value exceeds maximum nesting depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -MAX_INTEROPERABLE_INTEGER <= value <= MAX_INTEROPERABLE_INTEGER:
            raise CanonicalizationError(f"{field} integer exceeds interoperable range")
        return value
    if isinstance(value, float):
        raise CanonicalizationError(
            f"{field} contains a raw float; encode it with binary32_token or binary64_token"
        )
    if isinstance(value, str):
        return _reject_text(value, field)
    if isinstance(value, Mapping):
        if len(value) > MAX_CANONICAL_ITEMS:
            raise CanonicalizationError(f"{field} mapping exceeds collection limit")
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _KEY_PATTERN.fullmatch(key) is None:
                raise CanonicalizationError(
                    "canonical object keys must be controlled ASCII identifiers"
                )
            if key in normalized:
                raise CanonicalizationError(f"duplicate canonical object key: {key}")
            normalized[key] = _normalize_value(item, depth + 1, f"{field}.{key}")
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_CANONICAL_ITEMS:
            raise CanonicalizationError(f"{field} array exceeds collection limit")
        return [
            _normalize_value(item, depth + 1, f"{field}[{index}]")
            for index, item in enumerate(value)
        ]
    raise CanonicalizationError(f"{field} contains unsupported type {type(value).__name__}")


def canonical_bytes(value: object) -> bytes:
    """Serialize a restricted projection as compact UTF-8 JSON without a trailing newline."""

    normalized = _normalize_value(value, 0)
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CanonicalizationError("value cannot be encoded as canonical UTF-8 JSON") from exc
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise CanonicalizationError("canonical JSON exceeds byte limit")
    return encoded


def canonical_json(value: object) -> str:
    """Return canonical JSON text for diagnostics and fixture inspection."""

    return canonical_bytes(value).decode("utf-8")


def canonical_fingerprint(value: object) -> str:
    """Hash canonical bytes with an explicit algorithm prefix."""

    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def float_token(value: int | float, precision: FloatPrecision) -> str:
    """Encode one finite numeric value as a fixed-width big-endian IEEE-754 hex token."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanonicalizationError("float token input must be an int or float, not bool")
    if not isinstance(precision, FloatPrecision):
        raise CanonicalizationError("float token precision must be FloatPrecision")
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as exc:
        raise CanonicalizationError("float token input is not representable") from exc
    if not math.isfinite(numeric):
        raise CanonicalizationError("float token input must be finite")
    if precision is FloatPrecision.BINARY32:
        try:
            packed = struct.pack(">f", numeric)
        except (OverflowError, struct.error) as exc:
            raise CanonicalizationError("binary32 float token overflow") from exc
        width = 8
    else:
        try:
            packed = struct.pack(">d", numeric)
        except (OverflowError, struct.error) as exc:
            raise CanonicalizationError("binary64 float token overflow") from exc
        width = 16
    if numeric == 0:
        packed = b"\x00" * len(packed)
    return packed.hex().rjust(width, "0")


def binary32_token(value: int | float) -> str:
    """Encode a finite value as eight lowercase binary32 hex digits."""

    return float_token(value, FloatPrecision.BINARY32)


def binary64_token(value: int | float) -> str:
    """Encode a finite value as sixteen lowercase binary64 hex digits."""

    return float_token(value, FloatPrecision.BINARY64)


def _material_projection(value: object, parent: str | None = None) -> object:
    if isinstance(value, float):
        return binary64_token(value)
    if isinstance(value, Mapping):
        if set(value) == {"raw", "seconds"} and isinstance(value.get("seconds"), str):
            try:
                seconds = Decimal(value["seconds"])
            except (InvalidOperation, TypeError) as exc:
                raise CanonicalizationError("time seconds are not a finite decimal") from exc
            if not seconds.is_finite() or seconds < 0:
                raise CanonicalizationError("time seconds are not a finite nonnegative decimal")
            normalized_seconds = seconds.normalize()
            if normalized_seconds == 0:
                normalized_seconds = Decimal(0)
            return {"seconds": format(normalized_seconds, "f")}
        omissions = _MATERIAL_METADATA_BY_PARENT.get(parent, frozenset())
        material: dict[str, object] = {}
        for key, item in value.items():
            if key in omissions:
                continue
            material[key] = _material_projection(item, key)
        return material
    if isinstance(value, list):
        ordered = value
        if parent in {"records", "limitations"} and all(
            isinstance(item, Mapping) for item in value
        ):
            sort_key = "evidence_id" if parent == "records" else "limitation_id"
            ordered = sorted(value, key=lambda item: str(item.get(sort_key, "")))
        return [_material_projection(item, parent) for item in ordered]
    if isinstance(value, tuple):
        return [_material_projection(item, parent) for item in value]
    return value


def context_report_projection(report: ContextReport) -> dict[str, object]:
    """Build the versioned material projection used for context-report identity."""

    if not isinstance(report, ContextReport):
        raise CanonicalizationError("context report projection requires a ContextReport")
    wire = report.to_wire()
    material = _material_projection(wire)
    if not isinstance(material, dict):
        raise CanonicalizationError("context report wire value is not an object")
    material["projection_schema"] = CANONICAL_SCHEMA
    return material


def canonical_context_report_bytes(report: ContextReport) -> bytes:
    """Return exact canonical bytes for a typed context report."""

    return canonical_bytes(context_report_projection(report))


def fingerprint_context_report(report: ContextReport) -> str:
    """Return the material SHA-256 identity of a typed context report."""

    return "sha256:" + hashlib.sha256(canonical_context_report_bytes(report)).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalArtifact:
    """Immutable canonical bytes and their identity for an already validated projection."""

    schema: str
    payload: bytes
    fingerprint: str

    def __post_init__(self) -> None:
        if self.schema != CANONICAL_SCHEMA:
            raise CanonicalizationError("unsupported canonical artifact schema")
        if not isinstance(self.payload, bytes) or len(self.payload) > MAX_CANONICAL_BYTES:
            raise CanonicalizationError("canonical artifact payload exceeds byte limit")
        expected = "sha256:" + hashlib.sha256(self.payload).hexdigest()
        if self.fingerprint != expected:
            raise CanonicalizationError("canonical artifact fingerprint does not match payload")

    @classmethod
    def from_report(cls, report: ContextReport) -> CanonicalArtifact:
        payload = canonical_context_report_bytes(report)
        return cls(CANONICAL_SCHEMA, payload, "sha256:" + hashlib.sha256(payload).hexdigest())


__all__ = [
    "CANONICAL_SCHEMA",
    "CanonicalArtifact",
    "FloatPrecision",
    "MAX_CANONICAL_BYTES",
    "MAX_CANONICAL_DEPTH",
    "MAX_CANONICAL_ITEMS",
    "binary32_token",
    "binary64_token",
    "canonical_bytes",
    "canonical_fingerprint",
    "canonical_json",
    "canonical_context_report_bytes",
    "context_report_projection",
    "fingerprint_context_report",
    "float_token",
]
