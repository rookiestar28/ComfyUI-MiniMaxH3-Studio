"""Bounded backend-owned workspace projection for the five-stage product sidebar."""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from typing import TypedDict

from .assisted_authoring_scope import AssistedAuthoringState
from .canonical import canonical_fingerprint, fingerprint_context_report
from .context_reporting import ContextReport, ValidationStatus
from .contracts import PromptProfile
from .errors import NativeH3AdapterError, PromptLintError, SidebarWorkspaceError
from .guide_conformance import GuideConformanceResult, readiness_from_audit
from .length import milliseconds_from_frames
from .native_h3 import (
    NATIVE_H3_BASE_MAX_AUDIO,
    NATIVE_H3_BASE_MAX_AUDIO_TOTAL_SECONDS,
    NATIVE_H3_BASE_MAX_IMAGES,
    NATIVE_H3_BASE_MAX_OUTPUT_SECONDS,
    NATIVE_H3_BASE_MAX_REFERENCES,
    NATIVE_H3_BASE_MAX_TIMED_REFERENCE_SECONDS,
    NATIVE_H3_BASE_MAX_VIDEO_TOTAL_SECONDS,
    NATIVE_H3_BASE_MAX_VIDEOS,
    NATIVE_H3_BASE_MIN_OUTPUT_SECONDS,
    NATIVE_H3_BASE_MIN_TIMED_REFERENCE_SECONDS,
    NATIVE_H3_DURATION_UNVERIFIED,
    NATIVE_H3_IMAGE_NODE_ID,
    NATIVE_H3_REFERENCE_NODE_ID,
    NativeH3Wiring,
    assert_native_h3_wiring_authority,
    build_native_h3_wiring,
)
from .preview import ContextPreviewError, build_context_preview
from .product_shell import (
    SUPPORTED_CORE_VERSION,
    SUPPORTED_FRONTEND_VERSION,
    ProductShellBinding,
    build_product_shell_projection,
)
from .prompt_fidelity import PromptFidelityAuditResult, audit_prompt_fidelity
from .ui_projection import ExecutionCorrelation, UIEventState, build_ui_projection
from .validation_lifecycle import validate_context_report

SIDEBAR_WORKSPACE_SCHEMA = "h3.context.sidebar.workspace.v2"
SIDEBAR_TRANSFER_SCHEMA = "h3.context.sidebar.transfer.v1"
MAX_SIDEBAR_WORKSPACE_BYTES = 65_536
MAX_SIDEBAR_PROMPT_TEXT = 65_536
MAX_SIDEBAR_ITEMS = 64
_WORKSPACE_ID = re.compile(r"ws_[A-Za-z0-9_-]{32,128}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_STAGE_IDS = ("intent", "media", "understand", "audit", "execute")


class SidebarPlanningTimeline(TypedDict):
    start_seconds: int
    end_seconds: float
    effective_frame_count: int
    # M17-25: the delivered duration in integer milliseconds, so App Mode can
    # author a duration on the Request node without recomputing the frame grid.
    # There is one alignment authority and it is `core.length`, not the frontend.
    effective_duration_milliseconds: int
    duration_source: str


class SidebarPlanning(TypedDict):
    policy: str
    alternatives_status: str
    alternatives: list[object]
    timeline: SidebarPlanningTimeline
    creative_additions_status: str
    creative_additions: list[str]


class SidebarOutputDuration(TypedDict):
    requested_seconds: float
    effective_seconds: float
    min_seconds: float
    max_seconds: float
    status: str


class SidebarReferenceLimits(TypedDict):
    total: int
    image: int
    video: int
    audio: int


class SidebarTimedReferenceLimits(TypedDict):
    per_item_min_seconds: float
    per_item_max_seconds: float
    video_total_max_seconds: float
    audio_total_max_seconds: float
    status: str
    limitation: str | None


class SidebarCapabilities(TypedDict):
    mode_status: str
    supported_modes: list[str]
    native_prompt_boundary: str
    output_duration: SidebarOutputDuration
    reference_limits: SidebarReferenceLimits
    timed_reference_limits: SidebarTimedReferenceLimits


class SidebarMediaReceipt(TypedDict):
    status: str
    queue_ready: bool
    asset_count: int
    image_count: int
    video_count: int
    audio_count: int
    binding_count: int


class SidebarProposalDiff(TypedDict):
    status: str
    lines: list[str]


class SidebarProposal(TypedDict):
    changed: bool
    reason: str | None
    base_prompt_fingerprint: str
    current_prompt_fingerprint: str
    diff: SidebarProposalDiff


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise SidebarWorkspaceError("invalid_identity", f"{field} must be a SHA-256 fingerprint")
    return value


def _bounded_text(value: object, field: str, maximum: int = 4096) -> str:
    if type(value) is not str or len(value) > maximum:
        raise SidebarWorkspaceError("projection_limit", f"{field} is outside its text bound")
    if any(
        (ord(char) < 0x20 and char not in "\n\r\t") or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    ):
        raise SidebarWorkspaceError("unsafe_projection", f"{field} contains an unsafe code point")
    return value


@dataclass(frozen=True, slots=True)
class SidebarStage:
    stage_id: str
    status: str
    summary: str

    def __post_init__(self) -> None:
        if self.stage_id not in _STAGE_IDS:
            raise SidebarWorkspaceError("invalid_stage", "sidebar stage is unsupported")
        if self.status not in {"complete", "active", "blocked", "pending"}:
            raise SidebarWorkspaceError("invalid_stage", "sidebar stage status is unsupported")
        _bounded_text(self.summary, "stage summary", 512)

    def to_wire(self) -> dict[str, str]:
        return {"stage_id": self.stage_id, "status": self.status, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class SidebarReferenceCandidate:
    asset_id: str
    kind: str
    label: str
    ordinal: int
    paired_with: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.asset_id, "candidate asset_id", 128)
        if self.kind not in {"image", "video", "audio"}:
            raise SidebarWorkspaceError("invalid_candidate", "candidate kind is unsupported")
        expected = {"image": "Picture", "video": "Video", "audio": "Audio"}[self.kind]
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= 256:
            raise SidebarWorkspaceError("invalid_candidate", "candidate ordinal is invalid")
        if self.label != f"<{expected} {self.ordinal}>":
            raise SidebarWorkspaceError("invalid_candidate", "candidate label drifted")
        if self.paired_with is not None:
            if (
                self.kind != "audio"
                or re.fullmatch(r"<Video [1-9][0-9]{0,2}>", self.paired_with) is None
            ):
                raise SidebarWorkspaceError("invalid_candidate", "candidate pairing is invalid")

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "label": self.label,
            "ordinal": self.ordinal,
            "paired_with": self.paired_with,
        }


@dataclass(frozen=True, slots=True)
class SidebarSubjectCandidate:
    subject_id: str
    ordinal: int
    label: str
    display: str

    def __post_init__(self) -> None:
        if (
            type(self.subject_id) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.subject_id) is None
        ):
            raise SidebarWorkspaceError("invalid_candidate", "subject candidate id is invalid")
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= 256:
            raise SidebarWorkspaceError("invalid_candidate", "subject candidate ordinal is invalid")
        if self.label != f"<Subject {self.ordinal}>":
            raise SidebarWorkspaceError("invalid_candidate", "subject candidate label drifted")
        if not self.display:
            raise SidebarWorkspaceError("invalid_candidate", "subject candidate display is empty")
        _bounded_text(self.display, "subject candidate display", 512)

    def to_wire(self) -> dict[str, object]:
        return {
            "subject_id": self.subject_id,
            "ordinal": self.ordinal,
            "label": self.label,
            "display": self.display,
        }


@dataclass(frozen=True, slots=True)
class SidebarActionPermissions:
    stage_prompt: bool
    import_prompt: bool
    validate: bool
    export: bool
    copy_prompt: bool

    def __post_init__(self) -> None:
        if not all(
            type(value) is bool
            for value in (
                self.stage_prompt,
                self.import_prompt,
                self.validate,
                self.export,
                self.copy_prompt,
            )
        ):
            raise SidebarWorkspaceError("invalid_actions", "sidebar action flags must be booleans")
        if self.copy_prompt != self.export:
            raise SidebarWorkspaceError("invalid_actions", "copy and export readiness must agree")

    def to_wire(self) -> dict[str, bool]:
        return {
            "stage_prompt": self.stage_prompt,
            "import_prompt": self.import_prompt,
            "validate": self.validate,
            "export": self.export,
            "copy_prompt": self.copy_prompt,
        }


@dataclass(frozen=True, slots=True)
class SidebarWorkspaceProjection:
    workspace_id: str
    report_id: str
    report_revision: int
    report_fingerprint: str
    prompt_fingerprint: str
    base_prompt_fingerprint: str
    correlation: ExecutionCorrelation
    lifecycle: str
    validation_status: str
    guide_conformance: GuideConformanceResult
    task_mode: str
    profile: str
    prompt_text: str
    prompt_text_redacted: bool
    evidence: dict[str, object]
    plan_steps: tuple[dict[str, str], ...]
    exact_text: tuple[dict[str, str], ...]
    diagnostics: tuple[dict[str, object], ...]
    limitations: tuple[dict[str, str], ...]
    planning: SidebarPlanning
    capabilities: SidebarCapabilities
    media_receipt: SidebarMediaReceipt
    receipt: dict[str, str]
    comparison: dict[str, str]
    resources: dict[str, str]
    bindings: tuple[ProductShellBinding, ...]
    reference_candidates: tuple[SidebarReferenceCandidate, ...]
    subject_candidates: tuple[SidebarSubjectCandidate, ...]
    proposal: SidebarProposal
    stages: tuple[SidebarStage, ...]
    actions: SidebarActionPermissions
    assisted_authoring: AssistedAuthoringState
    product_scope: str = "MANUAL_ONLY_SCOPED"
    schema: str = SIDEBAR_WORKSPACE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SIDEBAR_WORKSPACE_SCHEMA or self.product_scope != "MANUAL_ONLY_SCOPED":
            raise SidebarWorkspaceError(
                "unsupported_schema", "sidebar workspace schema is unsupported"
            )
        if self.assisted_authoring != AssistedAuthoringState(True, False, False, False, False):
            raise SidebarWorkspaceError(
                "invalid_projection", "sidebar assisted-authoring scope is inconsistent"
            )
        if type(self.workspace_id) is not str or _WORKSPACE_ID.fullmatch(self.workspace_id) is None:
            raise SidebarWorkspaceError("invalid_identity", "workspace_id is malformed")
        _bounded_text(self.report_id, "report_id", 192)
        if type(self.report_revision) is not int or not 0 <= self.report_revision <= 1_000_000:
            raise SidebarWorkspaceError("invalid_revision", "report revision is outside its bound")
        for value, field in (
            (self.report_fingerprint, "report_fingerprint"),
            (self.prompt_fingerprint, "prompt_fingerprint"),
            (self.base_prompt_fingerprint, "base_prompt_fingerprint"),
        ):
            _fingerprint(value, field)
        if type(self.correlation) is not ExecutionCorrelation:
            raise SidebarWorkspaceError("invalid_identity", "correlation is not runtime-issued")
        if self.lifecycle not in {"ready", "stale", "blocked"}:
            raise SidebarWorkspaceError("invalid_lifecycle", "sidebar lifecycle is unsupported")
        if self.validation_status not in {"passed", "failed", "not_run"}:
            raise SidebarWorkspaceError("invalid_lifecycle", "validation status is unsupported")
        if type(self.guide_conformance) is not GuideConformanceResult:
            raise SidebarWorkspaceError(
                "invalid_projection", "guide conformance is not evaluator-issued"
            )
        if self.lifecycle == "ready" and self.validation_status != "passed":
            raise SidebarWorkspaceError("invalid_lifecycle", "ready requires passed validation")
        if self.actions.export != (self.lifecycle == "ready"):
            raise SidebarWorkspaceError("invalid_actions", "export readiness contradicts lifecycle")
        _bounded_text(self.prompt_text, "prompt_text", MAX_SIDEBAR_PROMPT_TEXT)
        if type(self.prompt_text_redacted) is not bool:
            raise SidebarWorkspaceError("invalid_projection", "prompt redaction flag is invalid")
        if tuple(stage.stage_id for stage in self.stages) != _STAGE_IDS:
            raise SidebarWorkspaceError("invalid_stage", "five sidebar stages must remain fixed")
        for values, field in (
            (self.plan_steps, "plan_steps"),
            (self.exact_text, "exact_text"),
            (self.diagnostics, "diagnostics"),
            (self.limitations, "limitations"),
            (self.bindings, "bindings"),
            (self.reference_candidates, "reference_candidates"),
            (self.subject_candidates, "subject_candidates"),
        ):
            if type(values) is not tuple or len(values) > MAX_SIDEBAR_ITEMS:
                raise SidebarWorkspaceError("projection_limit", f"{field} exceeds its item bound")
        if self.profile != PromptProfile.FULL_REFERENCE.value and self.subject_candidates:
            raise SidebarWorkspaceError(
                "invalid_candidate", "subject candidates require the full-reference profile"
            )
        if tuple(candidate.ordinal for candidate in self.subject_candidates) != tuple(
            range(1, len(self.subject_candidates) + 1)
        ):
            raise SidebarWorkspaceError(
                "invalid_candidate", "subject candidate order is not canonical"
            )
        subject_ids = tuple(candidate.subject_id for candidate in self.subject_candidates)
        if len(subject_ids) != len(set(subject_ids)):
            raise SidebarWorkspaceError("invalid_candidate", "subject candidate ids must be unique")
        wire = self.to_wire()
        try:
            encoded = json.dumps(
                wire, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise SidebarWorkspaceError("invalid_projection", "workspace is not JSON-safe") from exc
        if len(encoded) > MAX_SIDEBAR_WORKSPACE_BYTES:
            raise SidebarWorkspaceError("projection_limit", "workspace exceeds its byte bound")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "workspace_id": self.workspace_id,
            "report_id": self.report_id,
            "report_revision": self.report_revision,
            "report_fingerprint": self.report_fingerprint,
            "prompt_fingerprint": self.prompt_fingerprint,
            "base_prompt_fingerprint": self.base_prompt_fingerprint,
            "correlation": self.correlation.to_wire(),
            "lifecycle": self.lifecycle,
            "validation_status": self.validation_status,
            "guide_conformance": self.guide_conformance.to_wire(),
            "task_mode": self.task_mode,
            "profile": self.profile,
            "product_scope": self.product_scope,
            "assisted_authoring": self.assisted_authoring.to_wire(),
            "prompt_text": self.prompt_text,
            "prompt_text_redacted": self.prompt_text_redacted,
            "evidence": self.evidence,
            "plan_steps": list(self.plan_steps),
            "exact_text": list(self.exact_text),
            "diagnostics": list(self.diagnostics),
            "limitations": list(self.limitations),
            "planning": self.planning,
            "capabilities": self.capabilities,
            "media_receipt": self.media_receipt,
            "receipt": self.receipt,
            "comparison": self.comparison,
            "resources": self.resources,
            "bindings": [value.to_wire() for value in self.bindings],
            "reference_candidates": [value.to_wire() for value in self.reference_candidates],
            "subject_candidates": [value.to_wire() for value in self.subject_candidates],
            "proposal": self.proposal,
            "stages": [value.to_wire() for value in self.stages],
            "actions": self.actions.to_wire(),
        }


def _safe_plan_steps(preview_plan: dict[str, object]) -> tuple[dict[str, str], ...]:
    raw = preview_plan.get("steps", [])
    if not isinstance(raw, list):
        return ()
    result: list[dict[str, str]] = []
    for value in raw[:MAX_SIDEBAR_ITEMS]:
        if not isinstance(value, dict):
            continue
        fields = {key: value.get(key) for key in ("step_id", "stage", "status", "description")}
        if all(isinstance(item, str) for item in fields.values()):
            result.append({key: item for key, item in fields.items() if isinstance(item, str)})
    return tuple(result)


def _safe_exact_text(preview_request: dict[str, object]) -> tuple[dict[str, str], ...]:
    constraints = preview_request.get("hard_constraints")
    if not isinstance(constraints, dict) or not isinstance(constraints.get("constraints"), list):
        return ()
    result: list[dict[str, str]] = []
    for value in constraints["constraints"][:MAX_SIDEBAR_ITEMS]:
        if not isinstance(value, dict):
            continue
        constraint_id, kind, text = value.get("constraint_id"), value.get("kind"), value.get("text")
        if isinstance(constraint_id, str) and isinstance(kind, str) and isinstance(text, str):
            result.append({"constraint_id": constraint_id, "kind": kind, "text": text})
    return tuple(result)


def _fidelity_parameters(
    result: PromptFidelityAuditResult,
) -> dict[str, list[dict[str, str | int]]]:
    """The typed parameters M21-01 produced, grouped by diagnostic identity in emission order.

    M21-03 section 5. A diagnostic reaches the browser as `{code, severity, message}`, so a
    presentation layer that wants to compose its own localised sentence has the identity but not
    the values it names. M24-05 computes the pure audit once at the projection boundary so
    readiness and diagnostic parameters cannot observe different results; it still avoids
    threading presentation-only parameters through every fingerprinted `ValidationDiagnostic`.

    One identity can be emitted more than once -- two unrequested camera moves are two findings
    with the same code -- so the values are a list per identity, consumed positionally rather than
    a single map that the last finding would win.
    """

    grouped: dict[str, list[dict[str, str | int]]] = {}
    for item in result.diagnostics:
        if item.parameters:
            grouped.setdefault(item.diagnostic_id.value, []).append(item.parameter_map)
    return grouped


def _with_parameters(
    values: tuple[dict[str, str], ...], parameters: dict[str, list[dict[str, str | int]]]
) -> tuple[dict[str, object], ...]:
    """Attach parameters to the diagnostics that have them, leaving every other one untouched.

    Both streams preserve the audit's emission order, so the nth diagnostic carrying a given code
    takes the nth parameter map recorded for that identity.
    """

    pending = {code: list(found) for code, found in parameters.items()}
    result: list[dict[str, object]] = []
    for value in values:
        entry: dict[str, object] = dict(value)
        queue = pending.get(str(value.get("code", "")))
        if queue:
            entry["parameters"] = dict(queue.pop(0))
        result.append(entry)
    return tuple(result)


def _candidates(report: ContextReport) -> tuple[SidebarReferenceCandidate, ...]:
    registry = report.request.reference_registry
    labels = {asset.asset_id: registry.label_for(asset.asset_id).label for asset in registry.assets}
    return tuple(
        SidebarReferenceCandidate(
            asset_id=asset.asset_id,
            kind=asset.kind.value,
            label=labels[asset.asset_id],
            ordinal=registry.label_for(asset.asset_id).ordinal,
            paired_with=(labels.get(asset.paired_video_id) if asset.paired_video_id else None),
        )
        for asset in registry.assets
    )


def _subject_candidates(report: ContextReport) -> tuple[SidebarSubjectCandidate, ...]:
    if report.request.profile.name is not PromptProfile.FULL_REFERENCE:
        return ()
    preview = build_context_preview(report)
    graph = preview.plan.get("intent_graph")
    safe_subjects = graph.get("subjects") if isinstance(graph, dict) else None
    subjects = report.plan.intent_graph.subjects
    if not isinstance(safe_subjects, list) or len(safe_subjects) != len(subjects):
        raise SidebarWorkspaceError(
            "projection_limit", "subject candidates exceed the safe preview bound"
        )
    rows: list[SidebarSubjectCandidate] = []
    for ordinal, (subject, safe_subject) in enumerate(zip(subjects, safe_subjects, strict=True), 1):
        if (
            not isinstance(safe_subject, dict)
            or safe_subject.get("subject_id") != subject.subject_id
        ):
            raise SidebarWorkspaceError(
                "invalid_candidate", "subject candidate preview identity drifted"
            )
        safe_description = safe_subject.get("description")
        safe_label = safe_subject.get("label")
        display = safe_description if isinstance(safe_description, str) else safe_label
        if not isinstance(display, str):
            raise SidebarWorkspaceError(
                "invalid_candidate", "subject candidate display is unavailable"
            )
        rows.append(
            SidebarSubjectCandidate(
                subject_id=subject.subject_id,
                ordinal=ordinal,
                label=f"<Subject {ordinal}>",
                display=display[:512],
            )
        )
    return tuple(rows)


def _stages(lifecycle: str, native_ready: bool = True) -> tuple[SidebarStage, ...]:
    if lifecycle == "ready":
        statuses = ("complete", "complete", "complete", "complete", "active")
        summaries = (
            "Mode and profile are explicit.",
            "Reference roles and order are backend-owned.",
            "Evidence and plan are inspectable.",
            "Backend validation passed.",
            "Explicit export and native queue are ready."
            if native_ready
            else "Prompt export is ready; native execution is unqualified.",
        )
    elif lifecycle == "stale":
        statuses = ("complete", "complete", "complete", "active", "blocked")
        summaries = (
            "Mode and profile are explicit.",
            "Reference roles and order remain backend-owned.",
            "A new prompt revision is staged.",
            "Backend validation is required.",
            "Export and native queue are blocked.",
        )
    else:
        statuses = ("complete", "complete", "complete", "blocked", "blocked")
        summaries = (
            "Mode and profile are explicit.",
            "Reference roles and order remain backend-owned.",
            "The proposed revision remains inspectable.",
            "Backend validation failed.",
            "Export and native queue are blocked.",
        )
    return tuple(
        SidebarStage(stage_id, status, summary)
        for stage_id, status, summary in zip(_STAGE_IDS, statuses, summaries, strict=True)
    )


def _declared_duration_seconds(report: ContextReport) -> float:
    request = report.request
    if request.requested_duration_seconds is not None:
        return request.requested_duration_seconds
    return request.effective_duration_seconds


def _timed_reference_status(report: ContextReport) -> tuple[str, str | None]:
    timed = tuple(
        asset
        for asset in report.request.reference_registry.assets
        if asset.kind.value in {"video", "audio"}
    )
    if not timed:
        return "not_applicable", None
    if any(asset.metadata is None or asset.metadata.duration_seconds is None for asset in timed):
        return "unverified", NATIVE_H3_DURATION_UNVERIFIED
    return "verified", None


def _planning_projection(
    report: ContextReport,
    *,
    changed: bool,
    proposal_reason: str | None,
) -> SidebarPlanning:
    request = report.request
    return {
        "policy": "deterministic_manual",
        "alternatives_status": "not_available",
        "alternatives": [],
        "timeline": {
            "start_seconds": 0,
            "end_seconds": request.effective_duration_seconds,
            "effective_frame_count": request.effective_frame_count,
            "effective_duration_milliseconds": milliseconds_from_frames(
                request.effective_frame_count
            ),
            "duration_source": request.duration_source.value,
        },
        "creative_additions_status": "user_authored" if changed else "none",
        "creative_additions": [proposal_reason] if changed and proposal_reason is not None else [],
    }


def _capability_projection(report: ContextReport) -> SidebarCapabilities:
    timed_status, limitation = _timed_reference_status(report)
    return {
        "mode_status": "available",
        "supported_modes": ["t2va", "i2va", "fl2va", "l2va", "ref2va"],
        "native_prompt_boundary": "STRING",
        "output_duration": {
            "requested_seconds": _declared_duration_seconds(report),
            "effective_seconds": report.request.effective_duration_seconds,
            "min_seconds": float(NATIVE_H3_BASE_MIN_OUTPUT_SECONDS),
            "max_seconds": float(NATIVE_H3_BASE_MAX_OUTPUT_SECONDS),
            "status": "within_limit",
        },
        "reference_limits": {
            "total": NATIVE_H3_BASE_MAX_REFERENCES,
            "image": NATIVE_H3_BASE_MAX_IMAGES,
            "video": NATIVE_H3_BASE_MAX_VIDEOS,
            "audio": NATIVE_H3_BASE_MAX_AUDIO,
        },
        "timed_reference_limits": {
            "per_item_min_seconds": float(NATIVE_H3_BASE_MIN_TIMED_REFERENCE_SECONDS),
            "per_item_max_seconds": float(NATIVE_H3_BASE_MAX_TIMED_REFERENCE_SECONDS),
            "video_total_max_seconds": float(NATIVE_H3_BASE_MAX_VIDEO_TOTAL_SECONDS),
            "audio_total_max_seconds": float(NATIVE_H3_BASE_MAX_AUDIO_TOTAL_SECONDS),
            "status": timed_status,
            "limitation": limitation,
        },
    }


def _media_receipt_projection(
    report: ContextReport,
    wiring: NativeH3Wiring | None,
    native_ready: bool,
) -> SidebarMediaReceipt:
    assets = report.request.reference_registry.assets
    timed_status, _ = _timed_reference_status(report)
    # CRITICAL: image-only receipts are verified; not_applicable belongs only to timed-reference
    # status and is outside the workspace wire schema.
    if not assets:
        status = "not_required"
    elif timed_status == "unverified":
        status = "unverified"
    else:
        status = "verified"
    return {
        "status": status,
        "queue_ready": native_ready,
        "asset_count": len(assets),
        "image_count": sum(asset.kind.value == "image" for asset in assets),
        "video_count": sum(asset.kind.value == "video" for asset in assets),
        "audio_count": sum(asset.kind.value == "audio" for asset in assets),
        "binding_count": len(wiring.bindings) if wiring is not None else 0,
    }


def _proposal_diff(
    base_report: ContextReport | None,
    report: ContextReport,
    *,
    changed: bool,
    correlation: ExecutionCorrelation,
) -> SidebarProposalDiff:
    if not changed:
        return {"status": "unchanged", "lines": []}
    if base_report is None:
        return {"status": "unavailable", "lines": []}
    base_ui = build_ui_projection(base_report, correlation, UIEventState.VALIDATED)
    current_ui = build_ui_projection(report, correlation, UIEventState.ERROR)
    if base_ui.redacted_fields or current_ui.redacted_fields:
        return {"status": "unavailable_redacted", "lines": []}
    lines = list(
        difflib.unified_diff(
            base_ui.prompt_text.splitlines(),
            current_ui.prompt_text.splitlines(),
            fromfile="accepted",
            tofile="proposed",
            lineterm="",
            n=1,
        )
    )
    bounded: list[str] = []
    remaining = 4096
    for line in lines[:32]:
        value = line[:512]
        if len(value) > remaining:
            break
        bounded.append(value)
        remaining -= len(value)
    return {"status": "changed", "lines": bounded}


def build_sidebar_workspace_projection(
    report: ContextReport,
    wiring: NativeH3Wiring | None,
    correlation: ExecutionCorrelation,
    *,
    workspace_id: str,
    base_prompt_fingerprint: str,
    proposal_reason: str | None = None,
    base_report: ContextReport | None = None,
) -> SidebarWorkspaceProjection:
    """Project exact report authority without exposing locators, media, or provider payloads."""

    if type(report) is not ContextReport or type(correlation) is not ExecutionCorrelation:
        raise SidebarWorkspaceError(
            "invalid_authority", "exact report/correlation authority is required"
        )
    _fingerprint(base_prompt_fingerprint, "base_prompt_fingerprint")
    if proposal_reason is not None:
        _bounded_text(proposal_reason, "proposal reason", 1024)
    if report.validation.status is ValidationStatus.PASSED:
        if type(wiring) is not NativeH3Wiring:
            raise SidebarWorkspaceError("missing_wiring", "passed report requires runtime wiring")
        try:
            assert_native_h3_wiring_authority(wiring, report)
            shell = build_product_shell_projection(report, wiring, correlation)
        except NativeH3AdapterError as exc:
            raise SidebarWorkspaceError(
                "invalid_authority", "wiring is not owned by this report"
            ) from exc
        # IMPORTANT: prompt validation/export and native execution have different authorities.
        # Missing runtime composition evidence must not relabel a passed prompt as invalid.
        lifecycle = "ready" if wiring.queue_ready else "blocked"
        bindings = shell.bindings
        ui_state = UIEventState.VALIDATED
    else:
        if wiring is not None:
            raise SidebarWorkspaceError("stale_wiring", "non-passed report cannot carry wiring")
        lifecycle = "stale" if report.validation.status is ValidationStatus.NOT_RUN else "blocked"
        bindings = ()
        ui_state = UIEventState.ERROR
    try:
        preview = build_context_preview(report)
        ui = build_ui_projection(report, correlation, ui_state)
    except (ContextPreviewError, TypeError, ValueError) as exc:
        raise SidebarWorkspaceError(
            "projection_failed", "report could not be projected safely"
        ) from exc
    origins = tuple(dict.fromkeys(record.origin.value for record in report.evidence.records))
    if len(origins) > MAX_SIDEBAR_ITEMS:
        raise SidebarWorkspaceError("projection_limit", "evidence origin inventory is too large")
    provider = report.provider_receipt.provider.value if report.provider_receipt else "manual"
    outcome = report.provider_receipt.outcome.value if report.provider_receipt else "not_requested"
    native_node_id = (
        wiring.native_node_id
        if wiring is not None
        else (
            NATIVE_H3_REFERENCE_NODE_ID
            if report.request.task_mode.value == "ref2va"
            else NATIVE_H3_IMAGE_NODE_ID
        )
    )
    current_prompt_fingerprint = canonical_fingerprint(report.prompt_document.text)
    changed = current_prompt_fingerprint != base_prompt_fingerprint
    prompt_text_redacted = "prompt_text" in ui.redacted_fields
    if prompt_text_redacted:
        lifecycle = "blocked"
    actions = SidebarActionPermissions(
        stage_prompt=report.revision < 1_000_000,
        import_prompt=report.revision < 1_000_000,
        validate=report.validation.status is ValidationStatus.NOT_RUN,
        export=lifecycle == "ready" and not prompt_text_redacted,
        copy_prompt=lifecycle == "ready" and not prompt_text_redacted,
    )
    try:
        # CRITICAL: App Mode readiness and its parameters must come from this one independent
        # audit. Dropping readiness when the audit refuses would let a structurally ready
        # workspace conceal that guide conformance was never established.
        fidelity_audit = audit_prompt_fidelity(report.plan, report.prompt_document)
        guide_conformance = readiness_from_audit(fidelity_audit)
    except PromptLintError as exc:
        raise SidebarWorkspaceError(
            "projection_failed", "guide conformance could not be projected safely"
        ) from exc
    return SidebarWorkspaceProjection(
        workspace_id=workspace_id,
        report_id=report.report_id,
        report_revision=report.revision,
        report_fingerprint=fingerprint_context_report(report),
        prompt_fingerprint=current_prompt_fingerprint,
        base_prompt_fingerprint=base_prompt_fingerprint,
        correlation=correlation,
        lifecycle=lifecycle,
        validation_status=report.validation.status.value,
        guide_conformance=guide_conformance,
        task_mode=report.request.task_mode.value,
        profile=report.request.profile.name.value,
        prompt_text=ui.prompt_text,
        prompt_text_redacted=prompt_text_redacted,
        evidence={"count": len(report.evidence.records), "origins": list(origins)},
        plan_steps=_safe_plan_steps(preview.plan),
        exact_text=_safe_exact_text(preview.request),
        diagnostics=_with_parameters(ui.diagnostics, _fidelity_parameters(fidelity_audit)),
        limitations=ui.limitations,
        planning=_planning_projection(
            report,
            changed=changed,
            proposal_reason=proposal_reason,
        ),
        capabilities=_capability_projection(report),
        media_receipt=_media_receipt_projection(
            report, wiring, lifecycle == "ready" and shell.native_queue_ready
        ),
        receipt={"provider": provider, "outcome": outcome},
        comparison={"status": "not_available", "reason": "manual_only_scoped"},
        resources={
            "native_node_id": native_node_id,
            "core_version": SUPPORTED_CORE_VERSION,
            "frontend_version": SUPPORTED_FRONTEND_VERSION,
        },
        bindings=bindings,
        reference_candidates=_candidates(report),
        subject_candidates=_subject_candidates(report),
        proposal={
            "changed": changed,
            "reason": proposal_reason,
            "base_prompt_fingerprint": base_prompt_fingerprint,
            "current_prompt_fingerprint": current_prompt_fingerprint,
            "diff": _proposal_diff(
                base_report,
                report,
                changed=changed,
                correlation=correlation,
            ),
        },
        stages=_stages(lifecycle, lifecycle == "ready" and shell.native_queue_ready),
        actions=actions,
        assisted_authoring=AssistedAuthoringState(True, False, False, False, False),
    )


def validate_sidebar_revision(
    report: ContextReport,
    *,
    expected_revision: int,
    expected_report_fingerprint: str,
) -> tuple[ContextReport, NativeH3Wiring | None]:
    """Validate one exact staged revision and rebuild wiring only after a pass."""

    if type(report) is not ContextReport:
        raise SidebarWorkspaceError("invalid_report", "exact ContextReport authority is required")
    if type(expected_revision) is not int or report.revision != expected_revision:
        raise SidebarWorkspaceError("stale_revision", "report revision changed before validation")
    if (
        type(expected_report_fingerprint) is not str
        or fingerprint_context_report(report) != expected_report_fingerprint
    ):
        raise SidebarWorkspaceError("stale_report", "report fingerprint changed before validation")
    if report.validation.status is not ValidationStatus.NOT_RUN:
        raise SidebarWorkspaceError(
            "validation_not_required", "only staged revisions can be validated"
        )
    envelope = validate_context_report(report)
    validated = envelope.report
    if validated.validation.status is ValidationStatus.FAILED or validated.has_errors:
        return validated, None
    try:
        return validated, build_native_h3_wiring(
            validated,
            expected_revision=validated.revision,
            expected_report_fingerprint=fingerprint_context_report(validated),
        )
    except NativeH3AdapterError as exc:
        raise SidebarWorkspaceError(
            "native_wiring_failed", "validated revision could not rebuild wiring"
        ) from exc


def build_sidebar_transfer(projection: SidebarWorkspaceProjection) -> dict[str, object]:
    """Create the only portable prompt transfer document, after backend readiness proof."""

    if type(projection) is not SidebarWorkspaceProjection or not projection.actions.export:
        raise SidebarWorkspaceError("export_blocked", "current revision is not export-ready")
    return {
        "schema": SIDEBAR_TRANSFER_SCHEMA,
        "report_id": projection.report_id,
        "report_revision": projection.report_revision,
        "report_fingerprint": projection.report_fingerprint,
        "prompt_fingerprint": projection.prompt_fingerprint,
        "task_mode": projection.task_mode,
        "profile": projection.profile,
        "prompt_text": projection.prompt_text,
    }


__all__ = [
    "MAX_SIDEBAR_ITEMS",
    "MAX_SIDEBAR_PROMPT_TEXT",
    "MAX_SIDEBAR_WORKSPACE_BYTES",
    "SIDEBAR_TRANSFER_SCHEMA",
    "SIDEBAR_WORKSPACE_SCHEMA",
    "SidebarActionPermissions",
    "SidebarReferenceCandidate",
    "SidebarStage",
    "SidebarWorkspaceProjection",
    "build_sidebar_transfer",
    "build_sidebar_workspace_projection",
    "validate_sidebar_revision",
]
