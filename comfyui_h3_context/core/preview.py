"""Bounded, redacted presentation projections for typed H3 context reports.

Preview construction is deliberately one-way: it copies a validated ``ContextReport`` into a
small UI/audit value, redacts untrusted display strings, and never changes the source report or its
material identity. It does not inspect media, select providers, or execute a host/runtime.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from .canonical import canonical_fingerprint, fingerprint_context_report
from .context_reporting import ContextReport
from .contracts import CURRENT_SCHEMA_VERSION, SchemaVersion
from .errors import ContextPreviewError

PREVIEW_SCHEMA = "h3-context-preview/1"
MAX_PREVIEW_TEXT = 8192
MAX_PREVIEW_ITEMS = 64
MAX_PREVIEW_BYTES = 131_072
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REDACTION_PATTERN = re.compile(
    r"https?://[^\s<>\"']+"
    r"|file://[^\s<>\"']+"
    r"|(?i:\b(?:authorization\s*[:=]\s*)?bearer\s+[^\s,;]+)"
    r"|(?i:\b(?:authorization|bearer|api[_-]?key|apikey|password|secret|token|sig|"
    r"x-amz-[a-z0-9-]*)\s*[:=]\s*[^\s,;]+)"
    r"|(?i:(?:[A-Za-z]:[\\/]|/(?:home|mnt|tmp|var|Users|private|workspace)/)[^\s,;]+)"
)


class PreviewStatus(str, Enum):
    """Whether all bounded preview fields fit without display truncation."""

    COMPLETE = "complete"
    TRUNCATED = "truncated"


@dataclass(slots=True)
class _PreviewTracker:
    truncated: bool = False
    omitted_fields: set[str] = field(default_factory=set)
    redacted_fields: set[str] = field(default_factory=set)

    def omit(self, path: str) -> None:
        self.truncated = True
        self.omitted_fields.add(path)

    def redact(self, path: str) -> None:
        self.redacted_fields.add(path)


def _require_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContextPreviewError(f"{field_name} must be a bounded identifier")
    return value


def _require_fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ContextPreviewError(f"{field_name} must be a SHA-256 fingerprint")
    return value


def _redact_text(value: str, path: str, tracker: _PreviewTracker) -> str:
    def replace(match: re.Match[str]) -> str:
        tracker.redact(path)
        return "[REDACTED]"

    return _REDACTION_PATTERN.sub(replace, value)


def _truncate_text(value: str, limit: int, path: str, tracker: _PreviewTracker) -> str:
    if len(value) <= limit:
        return value
    marker = "…[TRUNCATED]"
    tracker.omit(path)
    return value[: max(0, limit - len(marker))] + marker


def _sanitize(
    value: object,
    path: str,
    *,
    text_limit: int,
    item_limit: int,
    tracker: _PreviewTracker,
    depth: int = 0,
) -> object:
    if depth > 16:
        tracker.omit(path)
        return "[TRUNCATED]"
    if isinstance(value, str):
        return _truncate_text(_redact_text(value, path, tracker), text_limit, path, tracker)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        items = list(value.items())
        if len(items) > item_limit:
            tracker.omit(path)
            items = items[:item_limit]
        output: dict[str, object] = {}
        for index, (key, item) in enumerate(items):
            if not isinstance(key, str):
                tracker.omit(f"{path}.<key:{index}>")
                continue
            output[key] = _sanitize(
                item,
                f"{path}.{key}",
                text_limit=text_limit,
                item_limit=item_limit,
                tracker=tracker,
                depth=depth + 1,
            )
        return output
    if isinstance(value, (list, tuple)):
        values = list(value)
        if len(values) > item_limit:
            tracker.omit(path)
            values = values[:item_limit]
        return [
            _sanitize(
                item,
                f"{path}[{index}]",
                text_limit=text_limit,
                item_limit=item_limit,
                tracker=tracker,
                depth=depth + 1,
            )
            for index, item in enumerate(values)
        ]
    tracker.omit(path)
    return "[UNSUPPORTED]"


def _require_safe_projection(value: object, path: str = "preview") -> None:
    if isinstance(value, str):
        if _REDACTION_PATTERN.search(value):
            raise ContextPreviewError(f"{path} contains unredacted sensitive text")
        if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ContextPreviewError(f"{path} contains an unsafe wire code point")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContextPreviewError(f"{path} contains a non-string key")
            _require_safe_projection(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _require_safe_projection(item, f"{path}[{index}]")
        return
    raise ContextPreviewError(f"{path} contains an unsupported value")


def _require_section(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContextPreviewError(f"{field_name} must be a JSON object")
    return value


def _require_dict_tuple(value: object, field_name: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, dict) for item in value):
        raise ContextPreviewError(f"{field_name} must be a tuple of JSON objects")
    return value


@dataclass(frozen=True, slots=True)
class ContextPreview:
    """Immutable bounded report projection for a UI/audit boundary."""

    preview_id: str
    schema_version: SchemaVersion
    report_id: str
    status: PreviewStatus
    request: dict[str, object]
    reference_mapping: dict[str, object]
    evidence: dict[str, object]
    plan: dict[str, object]
    prompt: dict[str, object]
    validation: dict[str, object]
    provider_identity: str
    provider_receipt: dict[str, object] | None
    fingerprints: dict[str, str]
    limitations: tuple[dict[str, object], ...]
    diagnostics: tuple[dict[str, object], ...]
    omitted_fields: tuple[str, ...] = ()
    redacted_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.preview_id, "preview_id")
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise ContextPreviewError("preview schema version is unsupported")
        _require_identifier(self.report_id, "report_id")
        if not isinstance(self.status, PreviewStatus):
            raise ContextPreviewError("preview status must be a PreviewStatus")
        for value, field_name in (
            (self.request, "request"),
            (self.reference_mapping, "reference_mapping"),
            (self.evidence, "evidence"),
            (self.plan, "plan"),
            (self.prompt, "prompt"),
            (self.validation, "validation"),
        ):
            if not isinstance(value, dict):
                raise ContextPreviewError(f"{field_name} must be a JSON object")
        if self.provider_receipt is not None and not isinstance(self.provider_receipt, dict):
            raise ContextPreviewError("provider_receipt must be a JSON object or None")
        if not isinstance(self.provider_identity, str) or not self.provider_identity:
            raise ContextPreviewError("provider_identity must be non-empty")
        if not isinstance(self.fingerprints, dict) or set(self.fingerprints) != {
            "report",
            "prompt",
        }:
            raise ContextPreviewError("fingerprints must contain report and prompt")
        _require_fingerprint(self.fingerprints["report"], "report fingerprint")
        _require_fingerprint(self.fingerprints["prompt"], "prompt fingerprint")
        self._check_paths(self.omitted_fields, "omitted_fields")
        self._check_paths(self.redacted_fields, "redacted_fields")
        _require_dict_tuple(self.limitations, "limitations")
        _require_dict_tuple(self.diagnostics, "diagnostics")
        _require_safe_projection(self.to_wire())
        encoded = json.dumps(self.to_wire(), ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_PREVIEW_BYTES:
            raise ContextPreviewError("preview exceeds the bounded byte limit")

    @staticmethod
    def _check_paths(values: tuple[str, ...], field_name: str) -> None:
        if not isinstance(values, tuple) or not all(
            isinstance(value, str) and 0 < len(value) <= 256 for value in values
        ):
            raise ContextPreviewError(f"{field_name} must be a tuple of bounded paths")
        if len(values) != len(set(values)):
            raise ContextPreviewError(f"{field_name} must not contain duplicates")

    def to_wire(self) -> dict[str, object]:
        return {
            "preview_schema": PREVIEW_SCHEMA,
            "preview_id": self.preview_id,
            "schema_version": str(self.schema_version),
            "report_id": self.report_id,
            "status": self.status.value,
            "request": self.request,
            "reference_mapping": self.reference_mapping,
            "evidence": self.evidence,
            "plan": self.plan,
            "prompt": self.prompt,
            "validation": self.validation,
            "provider_identity": self.provider_identity,
            "provider_receipt": self.provider_receipt,
            "fingerprints": self.fingerprints,
            "limitations": list(self.limitations),
            "diagnostics": list(self.diagnostics),
            "omitted_fields": list(self.omitted_fields),
            "redacted_fields": list(self.redacted_fields),
        }


def _preview_projection(
    report: ContextReport,
    *,
    text_limit: int,
    item_limit: int,
    tracker: _PreviewTracker,
) -> ContextPreview:
    wire = report.to_wire()
    safe_wire = _sanitize(
        wire,
        "report",
        text_limit=text_limit,
        item_limit=item_limit,
        tracker=tracker,
    )
    if not isinstance(safe_wire, dict):
        raise ContextPreviewError("report projection did not produce a JSON object")
    request = _require_section(safe_wire.get("request"), "request")
    reference_mapping = _require_section(request.get("reference_registry"), "reference_mapping")
    evidence = _require_section(safe_wire.get("evidence"), "evidence")
    plan = _require_section(safe_wire.get("plan"), "plan")
    prompt = _require_section(safe_wire.get("prompt_document"), "prompt")
    validation = _require_section(safe_wire.get("validation"), "validation")
    provider_value = safe_wire.get("provider_receipt")
    provider_receipt = (
        None if provider_value is None else _require_section(provider_value, "provider_receipt")
    )
    provider_identity = (
        provider_receipt.get("provider", "unknown") if provider_receipt else "unknown"
    )
    if not isinstance(provider_identity, str):
        provider_identity = "unknown"
    report_fingerprint = fingerprint_context_report(report)
    prompt_fingerprint = canonical_fingerprint(report.prompt_document.text)
    preview_id = (
        "preview_"
        + canonical_fingerprint({"report": report_fingerprint, "prompt": prompt_fingerprint}).split(
            ":", 1
        )[1][:24]
    )
    return ContextPreview(
        preview_id=preview_id,
        schema_version=report.schema_version,
        report_id=report.report_id,
        status=(PreviewStatus.TRUNCATED if tracker.truncated else PreviewStatus.COMPLETE),
        request=request,
        reference_mapping=reference_mapping,
        evidence=evidence,
        plan=plan,
        prompt=prompt,
        validation=validation,
        provider_identity=provider_identity,
        provider_receipt=provider_receipt,
        fingerprints={"report": report_fingerprint, "prompt": prompt_fingerprint},
        limitations=_require_dict_tuple(
            tuple(item for item in safe_wire.get("limitations", []) if isinstance(item, dict)),
            "limitations",
        ),
        diagnostics=_require_dict_tuple(
            tuple(item for item in safe_wire.get("diagnostics", []) if isinstance(item, dict)),
            "diagnostics",
        ),
        omitted_fields=tuple(sorted(tracker.omitted_fields)),
        redacted_fields=tuple(sorted(tracker.redacted_fields)),
    )


def build_context_preview(report: ContextReport) -> ContextPreview:
    """Build a deterministic redacted preview, shrinking only the presentation copy if needed."""

    if not isinstance(report, ContextReport):
        raise ContextPreviewError("preview construction requires a ContextReport")
    text_limits = (MAX_PREVIEW_TEXT, 4096, 2048, 1024, 512, 256, 128, 64)
    item_limits = (MAX_PREVIEW_ITEMS, 32, 16, 8, 4, 1)
    for text_limit in text_limits:
        for item_limit in item_limits:
            tracker = _PreviewTracker()
            if text_limit != MAX_PREVIEW_TEXT or item_limit != MAX_PREVIEW_ITEMS:
                tracker.truncated = True
            try:
                preview = _preview_projection(
                    report,
                    text_limit=text_limit,
                    item_limit=item_limit,
                    tracker=tracker,
                )
            except ContextPreviewError:
                continue
            encoded = json.dumps(
                preview.to_wire(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            if len(encoded.encode("utf-8")) <= MAX_PREVIEW_BYTES:
                return preview
    raise ContextPreviewError("report cannot be represented within the preview byte limit")


__all__ = [
    "ContextPreview",
    "ContextPreviewError",
    "MAX_PREVIEW_BYTES",
    "MAX_PREVIEW_ITEMS",
    "MAX_PREVIEW_TEXT",
    "PREVIEW_SCHEMA",
    "PreviewStatus",
    "build_context_preview",
]
