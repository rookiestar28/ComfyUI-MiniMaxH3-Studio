"""Source-bound composition of the deterministic H3 prompt pipeline.

This module does not define another prompt grammar.  It composes the accepted
renderer, parser, and linter while making source provenance and local budgets
part of the returned contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum

from .constraints import ExactTextKind
from .context_reporting import ContextPlan, PromptDocument, PromptRenderStatus, PromptSection
from .contracts import (
    EvidenceLevel,
    ProfileIdentity,
    PromptProfile,
    SchemaVersion,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .dialogue_speakers import render_dialogue_line
from .errors import ContractValidationError, PromptParseError, PromptRenderingError
from .guide_conformance import GuideConformanceResult, readiness_from_audit
from .linting import lint_prompt
from .parsing import (
    PromptParsedField,
    PromptParseDiagnostic,
    PromptParseResult,
    PromptRecognizedPreamble,
    PromptUnparsedSpan,
    parse_prompt,
)
from .prompt_fidelity import audit_prompt_fidelity
from .registry import BackendLabel, BackendLabelKind, BackendTarget
from .rendering import (
    _EVENT_PROSE,
    _prose_alternation,
    render_base_prompt,
    render_full_reference_prompt,
)

SOURCE_PROFILED_PROMPT_SCHEMA = "h3-source-profiled-prompt/2"
LEGACY_SOURCE_PROFILED_PROMPT_SCHEMA = "h3-source-profiled-prompt/1"
OFFICIAL_H3_GUIDE_REVISION = "fa9c8ab1eaa21c8ae25e7e40b83b2e6002f340af"  # pragma: allowlist secret
OFFICIAL_H3_BASE_GUIDE_DIGEST = (
    "2cfebc096a6e08370f288d468d90b60f7f9bcb938f94bf090816e910e48e75fc"  # pragma: allowlist secret
)
OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST = (
    "1e574f356716ad55612247ffb7bbccbcdb484ad96599d63c7dca1af186b1fab7"  # pragma: allowlist secret
)
_OFFICIAL_SOURCE_URIS = {
    PromptProfile.BASE: (
        "https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/"
        f"{OFFICIAL_H3_GUIDE_REVISION}/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md"
    ),
    PromptProfile.FULL_REFERENCE: (
        "https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/"
        f"{OFFICIAL_H3_GUIDE_REVISION}/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md"
    ),
}
_OFFICIAL_DIGESTS = {
    PromptProfile.BASE: OFFICIAL_H3_BASE_GUIDE_DIGEST,
    PromptProfile.FULL_REFERENCE: OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
}
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9A-Za-z._-]{1,128}")
_SPEAKER = re.compile(r"\((?P<ordinals>S[0-9]+(?:,S[0-9]+)*)\)")
_LABEL = re.compile(r"<(?:Picture|Video|Audio) [1-9][0-9]*>")
_TIMESTAMP = re.compile(
    r"\[Shot (?P<ordinal>(?:[2-9]|[1-9][0-9]+))\] "
    r"At (?P<time>[0-9]{2}:[0-9]{2}\.[0-9]{3}),"
)
# GUARD: this fact is compared between the canonical rendering and the observed document, so the
# pattern must recognise both the guide prose the renderer writes today and the
# `event <id> uses <mode>` form that prompts carried before M24-05. If it stops matching, both
# sides yield an empty vector and `semantic.copy_mode` silently admits every substitution.
_EVENT_COPY = re.compile(
    r"(?P<asset><Video [1-9][0-9]*>) (?:"
    r"event (?P<event>[A-Za-z0-9_.:-]+) uses (?P<mode>[a-z_]+)"
    rf"|(?P<mode_prose>{_prose_alternation(tuple(_EVENT_PROSE.values()))})"
    r")\b"
)
_EVENT_PROSE_TO_MODE = {prose: mode for mode, prose in _EVENT_PROSE.items()}


def _event_copy_fact(match: re.Match[str]) -> tuple[str, str]:
    """The (reference label, copy mode) pair a copy sentence asserts, in either grammar."""

    declared = match.group("mode")
    mode = declared if declared else _EVENT_PROSE_TO_MODE[match.group("mode_prose")]
    return match.group("asset"), mode


_SECTION_HEADING = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_BUDGET = 1_000_000
_RESULT_AUTHORITY = object()


class SourceProfileDecision(str, Enum):
    """Fail-closed disposition of one caller-supplied source observation."""

    ACCEPTED = "accepted"
    DRIFTED = "drifted"
    MODIFIED = "modified"
    UNSUPPORTED = "unsupported"


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_digest(value: object, field: str) -> str:
    if type(value) is not str or _HEX_DIGEST.fullmatch(value) is None:
        raise ContractValidationError(f"{field} must be a lower-case SHA-256 digest")
    return value


def _require_revision(value: object, field: str) -> str:
    if type(value) is not str or _REVISION.fullmatch(value) is None:
        raise ContractValidationError(f"{field} must be a bounded source revision")
    return value


def _source_decision(
    source_revision: str,
    source_digest: str,
    expected_revision: str,
    expected_digest: str,
    evidence_level: EvidenceLevel,
) -> SourceProfileDecision:
    if evidence_level is EvidenceLevel.OFFICIAL:
        return (
            SourceProfileDecision.ACCEPTED
            if source_revision == expected_revision and source_digest == expected_digest
            else SourceProfileDecision.DRIFTED
        )
    if evidence_level in {EvidenceLevel.MODIFIED, EvidenceLevel.FRAMEWORK_REFERENCE}:
        return SourceProfileDecision.MODIFIED
    return SourceProfileDecision.UNSUPPORTED


@dataclass(frozen=True, slots=True)
class SourceProfileBinding:
    """Profile identity joined to expected and observed source evidence."""

    profile: ProfileIdentity
    source_uri: str
    source_revision: str
    source_digest: str
    expected_revision: str
    expected_digest: str
    evidence_level: EvidenceLevel
    decision: SourceProfileDecision
    binding_fingerprint: str

    def __post_init__(self) -> None:
        if type(self) is not SourceProfileBinding:
            raise ContractValidationError("source binding must be a concrete SourceProfileBinding")
        if type(self.profile) is not ProfileIdentity:
            raise ContractValidationError("source binding profile must be a ProfileIdentity")
        _validate_profile_current(self.profile)
        if type(self.source_uri) is not str or not self.source_uri.startswith("https://"):
            raise ContractValidationError("source_uri must be an explicit HTTPS source")
        _require_revision(self.source_revision, "source_revision")
        _require_revision(self.expected_revision, "expected_revision")
        _require_digest(self.source_digest, "source_digest")
        _require_digest(self.expected_digest, "expected_digest")
        if type(self.evidence_level) is not EvidenceLevel:
            raise ContractValidationError("evidence_level must be an EvidenceLevel")
        if type(self.decision) is not SourceProfileDecision:
            raise ContractValidationError("decision must be a SourceProfileDecision")
        if self.source_uri != _OFFICIAL_SOURCE_URIS.get(self.profile.name):
            raise ContractValidationError("source binding URI does not match the pinned profile")
        if self.expected_revision != OFFICIAL_H3_GUIDE_REVISION:
            raise ContractValidationError("source binding expected revision is not pinned")
        if self.expected_digest != _OFFICIAL_DIGESTS.get(self.profile.name):
            raise ContractValidationError("source binding expected digest is not pinned")
        expected_decision = _source_decision(
            self.source_revision,
            self.source_digest,
            self.expected_revision,
            self.expected_digest,
            self.evidence_level,
        )
        if self.decision is not expected_decision:
            raise ContractValidationError("source binding decision contradicts its evidence")
        _require_digest(self.binding_fingerprint, "binding_fingerprint")
        if self.binding_fingerprint != _fingerprint(self._fingerprint_payload()):
            raise ContractValidationError("source binding fingerprint does not match its evidence")

    def _fingerprint_payload(self) -> dict[str, str]:
        return {
            "profile": self.profile.name.value,
            "profile_version": str(self.profile.version),
            "source_uri": self.source_uri,
            "source_revision": self.source_revision,
            "source_digest": self.source_digest,
            "expected_revision": self.expected_revision,
            "expected_digest": self.expected_digest,
            "evidence_level": self.evidence_level.value,
            "decision": self.decision.value,
        }

    @property
    def is_official(self) -> bool:
        self.__post_init__()
        return (
            self.decision is SourceProfileDecision.ACCEPTED
            and self.evidence_level is EvidenceLevel.OFFICIAL
        )

    def to_wire(self) -> dict[str, object]:
        self.__post_init__()
        return {**self._fingerprint_payload(), "binding_fingerprint": self.binding_fingerprint}


def _binding(
    profile: ProfileIdentity,
    source_revision: str,
    source_digest: str,
    evidence_level: EvidenceLevel,
    decision: SourceProfileDecision,
) -> SourceProfileBinding:
    if type(profile) is not ProfileIdentity:
        raise ContractValidationError("source binding profile must be a ProfileIdentity")
    if profile.name not in _OFFICIAL_DIGESTS:
        raise ContractValidationError("source binding profile is unsupported")
    if type(evidence_level) is not EvidenceLevel or type(decision) is not SourceProfileDecision:
        raise ContractValidationError("source binding evidence decision is malformed")
    revision = _require_revision(source_revision, "source_revision")
    digest = _require_digest(source_digest, "source_digest")
    expected_digest = _OFFICIAL_DIGESTS[profile.name]
    payload = {
        "profile": profile.name.value,
        "profile_version": str(profile.version),
        "source_uri": _OFFICIAL_SOURCE_URIS[profile.name],
        "source_revision": revision,
        "source_digest": digest,
        "expected_revision": OFFICIAL_H3_GUIDE_REVISION,
        "expected_digest": expected_digest,
        "evidence_level": evidence_level.value,
        "decision": decision.value,
    }
    return SourceProfileBinding(
        profile,
        _OFFICIAL_SOURCE_URIS[profile.name],
        revision,
        digest,
        OFFICIAL_H3_GUIDE_REVISION,
        expected_digest,
        evidence_level,
        decision,
        _fingerprint(payload),
    )


def build_official_source_profile_binding(
    profile: ProfileIdentity,
    *,
    observed_revision: str,
    observed_digest: str,
) -> SourceProfileBinding:
    """Bind an observation to the pinned official snapshot without fetching it."""

    if type(profile) is not ProfileIdentity or profile.name not in _OFFICIAL_DIGESTS:
        raise ContractValidationError("source binding profile is unsupported")
    revision = _require_revision(observed_revision, "observed_revision")
    digest = _require_digest(observed_digest, "observed_digest")
    expected_digest = _OFFICIAL_DIGESTS[profile.name]
    decision = _source_decision(
        revision,
        digest,
        OFFICIAL_H3_GUIDE_REVISION,
        expected_digest or "",
        EvidenceLevel.OFFICIAL,
    )
    return _binding(profile, revision, digest, EvidenceLevel.OFFICIAL, decision)


def build_source_profile_binding(
    profile: ProfileIdentity,
    *,
    source_revision: str,
    source_digest: str,
    evidence_level: EvidenceLevel,
) -> SourceProfileBinding:
    """Create a non-official binding; caller labels cannot grant official status."""

    if type(evidence_level) is not EvidenceLevel:
        raise ContractValidationError("evidence_level must be an EvidenceLevel")
    if evidence_level is EvidenceLevel.OFFICIAL:
        raise ContractValidationError(
            "official evidence must use build_official_source_profile_binding"
        )
    if type(profile) is not ProfileIdentity or profile.name not in _OFFICIAL_DIGESTS:
        raise ContractValidationError("source binding profile is unsupported")
    expected_digest = _OFFICIAL_DIGESTS[profile.name]
    revision = _require_revision(source_revision, "source_revision")
    digest = _require_digest(source_digest, "source_digest")
    decision = _source_decision(
        revision,
        digest,
        OFFICIAL_H3_GUIDE_REVISION,
        expected_digest,
        evidence_level,
    )
    return _binding(profile, revision, digest, evidence_level, decision)


def _require_limit(value: object, field: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_BUDGET:
        raise ContractValidationError(f"{field} must be an integer between 1 and {_MAX_BUDGET}")
    return value


@dataclass(frozen=True, slots=True)
class PromptBudget:
    """Deterministic local limits; token counts are estimates, never tokenizer parity claims."""

    max_characters: int = 65_536
    max_estimated_tokens: int = 32_768
    section_character_limits: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if type(self) is not PromptBudget:
            raise ContractValidationError("prompt budget must be a concrete PromptBudget")
        _require_limit(self.max_characters, "max_characters")
        _require_limit(self.max_estimated_tokens, "max_estimated_tokens")
        if type(self.section_character_limits) is not tuple:
            raise ContractValidationError("section_character_limits must be a tuple")
        names: list[str] = []
        for item in self.section_character_limits:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or _SECTION_HEADING.fullmatch(item[0]) is None
            ):
                raise ContractValidationError(
                    "section_character_limits entries must be name/limit pairs"
                )
            names.append(item[0])
            _require_limit(item[1], f"section limit {item[0]}")
        if len(names) != len(set(names)):
            raise ContractValidationError("section_character_limits headings must be unique")

    def to_wire(self) -> dict[str, object]:
        self.__post_init__()
        return {
            "max_characters": self.max_characters,
            "max_estimated_tokens": self.max_estimated_tokens,
            "section_character_limits": [
                {"heading": heading, "max_characters": limit}
                for heading, limit in self.section_character_limits
            ],
        }


@dataclass(frozen=True, slots=True)
class PromptBudgetUsage:
    characters: int
    estimated_tokens: int
    section_characters: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if type(self) is not PromptBudgetUsage:
            raise ContractValidationError("usage must be a concrete PromptBudgetUsage")
        _require_limit(self.characters, "usage characters")
        _require_limit(self.estimated_tokens, "usage estimated_tokens")
        if type(self.section_characters) is not tuple or not self.section_characters:
            raise ContractValidationError("usage section_characters must be a non-empty tuple")
        names: list[str] = []
        for item in self.section_characters:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or _SECTION_HEADING.fullmatch(item[0]) is None
            ):
                raise ContractValidationError("usage sections must be heading/count pairs")
            names.append(item[0])
            _require_limit(item[1], f"usage section {item[0]}")
        if len(names) != len(set(names)):
            raise ContractValidationError("usage section headings must be unique")

    def to_wire(self) -> dict[str, object]:
        self.__post_init__()
        return {
            "characters": self.characters,
            "estimated_tokens": self.estimated_tokens,
            "estimator": "utf8-bytes-ceil-div-4/v1",
            "section_characters": [
                {"heading": heading, "characters": count}
                for heading, count in self.section_characters
            ],
        }


def _validate_profile_current(profile: object) -> None:
    if (
        type(profile) is not ProfileIdentity
        or type(profile.name) is not PromptProfile
        or type(profile.version) is not SchemaVersion
        or type(profile.version.major) is not int
        or type(profile.version.minor) is not int
    ):
        raise ContractValidationError("profile identity has a malformed current value")
    profile.__post_init__()


def _validate_document_current(document: object) -> None:
    if type(document) is not PromptDocument:
        raise ContractValidationError("profiled validation requires a concrete PromptDocument")
    _validate_profile_current(document.profile)
    if (
        type(document.document_id) is not str
        or type(document.plan_id) is not str
        or type(document.text) is not str
        or type(document.schema_version) is not SchemaVersion
        or type(document.task_mode) is not TaskMode
        or type(document.status) is not PromptRenderStatus
        or type(document.sections) is not tuple
        or type(document.source_evidence_ids) is not tuple
        or not all(type(item) is str for item in document.source_evidence_ids)
    ):
        raise ContractValidationError("prompt document has a malformed current value")
    for section in document.sections:
        if (
            type(section) is not PromptSection
            or type(section.section_id) is not str
            or type(section.order) is not int
            or type(section.heading) is not str
            or type(section.body) is not str
            or type(section.source_evidence_ids) is not tuple
            or not all(type(item) is str for item in section.source_evidence_ids)
        ):
            raise ContractValidationError("prompt section has a malformed current value")
        section.__post_init__()
    document.__post_init__()


def _validate_parse_current(parsed: object) -> None:
    if type(parsed) is not PromptParseResult:
        raise ContractValidationError("result parse value must be a PromptParseResult")
    _validate_profile_current(parsed.profile)
    if (
        type(parsed.source_text) is not str
        or type(parsed.task_mode) is not TaskMode
        or type(parsed.fields) is not tuple
        or type(parsed.unparsed_spans) is not tuple
        or type(parsed.diagnostics) is not tuple
        or type(parsed.canonical) is not bool
    ):
        raise ContractValidationError("parse result has a malformed current value")
    if parsed.preamble is not None and type(parsed.preamble) is not PromptRecognizedPreamble:
        raise ContractValidationError("parse preamble has a malformed current value")
    if not all(type(item) is PromptParsedField for item in parsed.fields):
        raise ContractValidationError("parsed fields contain a malformed value")
    for parsed_field in parsed.fields:
        if (
            type(parsed_field.name) is not str
            or type(parsed_field.value) is not str
            or type(parsed_field.start) is not int
            or type(parsed_field.end) is not int
            or type(parsed_field.value_start) is not int
            or type(parsed_field.value_end) is not int
        ):
            raise ContractValidationError("parsed field has a malformed current value")
    if parsed.preamble is not None and (
        type(parsed.preamble.text) is not str
        or type(parsed.preamble.start) is not int
        or type(parsed.preamble.end) is not int
    ):
        raise ContractValidationError("parse preamble has a malformed current value")
    if not all(type(item) is PromptUnparsedSpan for item in parsed.unparsed_spans):
        raise ContractValidationError("unparsed spans contain a malformed value")
    for span in parsed.unparsed_spans:
        if (
            type(span.text) is not str
            or type(span.start) is not int
            or type(span.end) is not int
            or type(span.reason) is not str
        ):
            raise ContractValidationError("unparsed span has a malformed current value")
    if not all(type(item) is PromptParseDiagnostic for item in parsed.diagnostics):
        raise ContractValidationError("parse diagnostics contain a malformed value")
    for parse_diagnostic in parsed.diagnostics:
        if (
            type(parse_diagnostic.severity) is not ValidationSeverity
            or type(parse_diagnostic.code) is not str
            or type(parse_diagnostic.message) is not str
            or type(parse_diagnostic.location) is not str
            or type(parse_diagnostic.remediation) is not str
        ):
            raise ContractValidationError("parse diagnostic has a malformed current value")
    parsed.__post_init__()


def _validate_diagnostic_current(item: ValidationDiagnostic) -> None:
    if (
        type(item.severity) is not ValidationSeverity
        or type(item.code) is not str
        or type(item.message) is not str
        or (item.location is not None and type(item.location) is not str)
    ):
        raise ContractValidationError("result diagnostic has a malformed current value")
    item.__post_init__()


def _validate_backend_label_current(item: BackendLabel) -> None:
    if (
        type(item.backend) is not BackendTarget
        or type(item.kind) is not BackendLabelKind
        or type(item.ordinal) is not int
        or type(item.asset_id) is not str
        or type(item.label) is not str
    ):
        raise ContractValidationError("backend label has a malformed current value")
    item.__post_init__()


@dataclass(frozen=True, slots=True)
class SourceProfiledPromptResult:
    binding: SourceProfileBinding
    document: PromptDocument
    parse_result: PromptParseResult
    diagnostics: tuple[ValidationDiagnostic, ...]
    backend_labels: tuple[BackendLabel, ...]
    budget: PromptBudget
    usage: PromptBudgetUsage
    conformance: GuideConformanceResult | None = None
    _authority: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self) is not SourceProfiledPromptResult:
            raise ContractValidationError("result must be a concrete SourceProfiledPromptResult")
        if self._authority is not _RESULT_AUTHORITY:
            raise ContractValidationError("result must be created by the profiled validator")
        if type(self.binding) is not SourceProfileBinding:
            raise ContractValidationError("result binding must be a SourceProfileBinding")
        self.binding.__post_init__()
        _validate_document_current(self.document)
        _validate_parse_current(self.parse_result)
        if type(self.diagnostics) is not tuple or not all(
            type(item) is ValidationDiagnostic for item in self.diagnostics
        ):
            raise ContractValidationError("result diagnostics must be concrete typed values")
        for diagnostic in self.diagnostics:
            _validate_diagnostic_current(diagnostic)
        if type(self.backend_labels) is not tuple or not all(
            type(item) is BackendLabel for item in self.backend_labels
        ):
            raise ContractValidationError("result backend labels must be concrete typed values")
        for backend_label in self.backend_labels:
            _validate_backend_label_current(backend_label)
        if type(self.budget) is not PromptBudget:
            raise ContractValidationError("result budget must be a PromptBudget")
        self.budget.__post_init__()
        if type(self.usage) is not PromptBudgetUsage:
            raise ContractValidationError("result usage must be a PromptBudgetUsage")
        self.usage.__post_init__()
        if self.document.profile != self.parse_result.profile:
            raise ContractValidationError("result document and parse profiles do not match")
        if self.document.task_mode is not self.parse_result.task_mode:
            raise ContractValidationError("result task modes do not match")
        if self.document.text != self.parse_result.source_text:
            raise ContractValidationError("result document and parse source text do not match")
        if self.usage != _budget_usage(self.document):
            raise ContractValidationError("result budget usage does not match the document")

    @property
    def is_valid(self) -> bool:
        """Structural validity: an accepted source and no blocking diagnostic.

        This stays exactly what it has always been. A guide-incomplete plan still renders a
        bounded, inspectable, runnable prompt, and the reconstruction route depends on that.
        """

        self.__post_init__()
        return self.binding.decision is SourceProfileDecision.ACCEPTED and not any(
            item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
            for item in self.diagnostics
        )

    @property
    def official_claim_admitted(self) -> bool:
        """Whether this result may be presented as conforming to the official guide.

        GUARD: structural validity is necessary and was never sufficient, and the two must stay
        separate properties. `_semantic_diagnostics` compares this document against the *same*
        canonical renderer, so it detects a mutation away from the renderer and nothing else -- a
        systematic mistake inside the renderer reproduces itself and passes. Only the independent
        readiness verdict can withhold the claim, so an official/source-profiled claim reads this
        property; folding readiness into `is_valid` instead would refuse every manual plan the
        product is required to keep running.
        """

        return self.is_valid and self.conformance is not None and self.conformance.is_ready

    def to_wire(self) -> dict[str, object]:
        self.__post_init__()
        parse_wire = self.parse_result.to_wire()
        parse_wire.pop("source_text")
        return {
            "schema": SOURCE_PROFILED_PROMPT_SCHEMA,
            "binding": self.binding.to_wire(),
            "document": self.document.to_wire(),
            "parse_result": parse_wire,
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "backend_labels": [item.to_wire() for item in self.backend_labels],
            "budget": self.budget.to_wire(),
            "usage": self.usage.to_wire(),
            "conformance": (None if self.conformance is None else self.conformance.to_wire()),
        }


def _diagnostic(code: str, message: str, location: str) -> ValidationDiagnostic:
    return ValidationDiagnostic(ValidationSeverity.ERROR, code, message, location)


def _source_diagnostics(
    plan: ContextPlan, binding: SourceProfileBinding
) -> list[ValidationDiagnostic]:
    if binding.profile != plan.request.profile:
        return [
            _diagnostic(
                "source.profile_mismatch",
                "source binding profile differs from plan profile",
                "binding.profile",
            )
        ]
    if binding.decision is SourceProfileDecision.DRIFTED:
        return [
            _diagnostic(
                "source.drifted",
                "observed source revision or digest differs from the pinned snapshot",
                "binding",
            )
        ]
    if binding.decision is SourceProfileDecision.MODIFIED:
        return [
            _diagnostic(
                "source.modified_evidence",
                "modified or framework evidence is not an official source binding",
                "binding.evidence_level",
            )
        ]
    if binding.decision is SourceProfileDecision.UNSUPPORTED:
        return [
            _diagnostic(
                "source.unsupported",
                "source evidence cannot authorize this profile",
                "binding.evidence_level",
            )
        ]
    return []


def _budget_usage(document: PromptDocument) -> PromptBudgetUsage:
    return PromptBudgetUsage(
        len(document.text),
        max(1, (len(document.text.encode("utf-8")) + 3) // 4),
        tuple((section.heading, len(section.body)) for section in document.sections),
    )


def _budget_diagnostics(
    usage: PromptBudgetUsage, budget: PromptBudget
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    if usage.characters > budget.max_characters:
        diagnostics.append(
            _diagnostic(
                "budget.characters",
                "prompt exceeds the configured character budget",
                "document.text",
            )
        )
    if usage.estimated_tokens > budget.max_estimated_tokens:
        diagnostics.append(
            _diagnostic(
                "budget.estimated_tokens",
                "prompt exceeds the configured estimated-token budget",
                "document.text",
            )
        )
    observed = dict(usage.section_characters)
    for heading, limit in budget.section_character_limits:
        if heading not in observed:
            diagnostics.append(
                _diagnostic(
                    "budget.unknown_section",
                    "budget names a section absent from the document",
                    f"budget.sections.{heading}",
                )
            )
        elif observed[heading] > limit:
            diagnostics.append(
                _diagnostic(
                    "budget.section_characters",
                    "prompt section exceeds its configured character budget",
                    f"document.sections.{heading}",
                )
            )
    return diagnostics


def _wire_object(value: object, field: str) -> dict[str, object]:
    if type(value) is not dict or not all(type(key) is str for key in value):
        raise ContractValidationError(f"{field} must be a concrete string-keyed object")
    return value


def _wire_integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise ContractValidationError(f"{field} must be an integer")
    return value


def _wire_string(value: object, field: str) -> str:
    if type(value) is not str:
        raise ContractValidationError(f"{field} must be a string")
    return value


def validate_source_profiled_prompt_wire(wire: object) -> None:
    """Validate semantic joins that JSON Schema Draft 2020-12 cannot express.

    The committed schema remains the structural first stage.  Consumers must run this
    validator before trusting hashes, document-derived usage, or budget diagnostics.
    """

    _validate_profiled_wire(wire, legacy=False)


def validate_legacy_source_profiled_prompt_wire(wire: object) -> None:
    """Inspect the old report without creating a current result or admission authority."""

    _validate_profiled_wire(wire, legacy=True)


def _validate_profiled_wire(wire: object, *, legacy: bool) -> None:
    root = _wire_object(wire, "source-profiled wire envelope")
    expected_schema = (
        LEGACY_SOURCE_PROFILED_PROMPT_SCHEMA if legacy else SOURCE_PROFILED_PROMPT_SCHEMA
    )
    if root.get("schema") != expected_schema:
        raise ContractValidationError("source-profiled wire schema is unsupported")
    # GUARD: old successful reports remain readable only through the explicit inspection path.
    # A current envelope must not launder an old readiness claim by changing just its outer tag.
    conformance = root.get("conformance")
    if conformance is not None:
        claim = _wire_object(conformance, "wire conformance")
        expected_guide = (
            "h3.context.guide_conformance.v1" if legacy else "h3.context.guide_conformance.v2"
        )
        if (
            set(claim) != {"schema", "readiness", "reasons"}
            or claim.get("schema") != expected_guide
        ):
            raise ContractValidationError("wire conformance schema is unsupported")
        reasons = claim.get("reasons")
        if (
            type(reasons) is not list
            or len(reasons) > 64
            or any(type(reason) is not str for reason in reasons)
        ):
            raise ContractValidationError("wire conformance reasons are invalid")
        readiness = claim.get("readiness")
        if readiness not in {"ready", "incomplete", "modified"} or (
            readiness == "ready" and reasons
        ):
            raise ContractValidationError("wire conformance readiness is invalid")

    binding_wire = _wire_object(root.get("binding"), "wire binding")
    try:
        profile = ProfileIdentity(
            PromptProfile(_wire_string(binding_wire.get("profile"), "wire binding profile")),
            SchemaVersion.from_wire(binding_wire.get("profile_version")),
        )
        binding = SourceProfileBinding(
            profile,
            _wire_string(binding_wire.get("source_uri"), "wire binding source_uri"),
            _wire_string(binding_wire.get("source_revision"), "wire binding source_revision"),
            _wire_string(binding_wire.get("source_digest"), "wire binding source_digest"),
            _wire_string(binding_wire.get("expected_revision"), "wire binding expected_revision"),
            _wire_string(binding_wire.get("expected_digest"), "wire binding expected_digest"),
            EvidenceLevel(
                _wire_string(binding_wire.get("evidence_level"), "wire binding evidence_level")
            ),
            SourceProfileDecision(
                _wire_string(binding_wire.get("decision"), "wire binding decision")
            ),
            _wire_string(binding_wire.get("binding_fingerprint"), "wire binding fingerprint"),
        )
    except ValueError as exc:
        raise ContractValidationError("wire binding contains an unknown closed value") from exc
    binding.__post_init__()

    document = _wire_object(root.get("document"), "wire document")
    text = _wire_string(document.get("text"), "wire document text")
    raw_sections = document.get("sections")
    if type(raw_sections) is not list or not raw_sections:
        raise ContractValidationError("wire document sections must be a non-empty list")
    section_counts: list[tuple[str, int]] = []
    for index, raw_section in enumerate(raw_sections):
        section = _wire_object(raw_section, f"wire document sections[{index}]")
        heading = _wire_string(section.get("heading"), f"wire document sections[{index}].heading")
        body = _wire_string(section.get("body"), f"wire document sections[{index}].body")
        section_counts.append((heading, len(body)))
    expected_usage = PromptBudgetUsage(
        len(text),
        max(1, (len(text.encode("utf-8")) + 3) // 4),
        tuple(section_counts),
    )

    usage_wire = _wire_object(root.get("usage"), "wire usage")
    raw_usage_sections = usage_wire.get("section_characters")
    if type(raw_usage_sections) is not list or not raw_usage_sections:
        raise ContractValidationError("wire usage sections must be a non-empty list")
    usage_sections: list[tuple[str, int]] = []
    for index, raw_section in enumerate(raw_usage_sections):
        section = _wire_object(raw_section, f"wire usage sections[{index}]")
        usage_sections.append(
            (
                _wire_string(section.get("heading"), f"wire usage sections[{index}].heading"),
                _wire_integer(
                    section.get("characters"), f"wire usage sections[{index}].characters"
                ),
            )
        )
    usage = PromptBudgetUsage(
        _wire_integer(usage_wire.get("characters"), "wire usage characters"),
        _wire_integer(usage_wire.get("estimated_tokens"), "wire usage estimated_tokens"),
        tuple(usage_sections),
    )
    if usage_wire.get("estimator") != "utf8-bytes-ceil-div-4/v1" or usage != expected_usage:
        raise ContractValidationError("wire usage does not match the serialized document")

    budget_wire = _wire_object(root.get("budget"), "wire budget")
    raw_limits = budget_wire.get("section_character_limits")
    if type(raw_limits) is not list:
        raise ContractValidationError("wire budget section limits must be a list")
    limits: list[tuple[str, int]] = []
    for index, raw_limit in enumerate(raw_limits):
        limit = _wire_object(raw_limit, f"wire budget limits[{index}]")
        limits.append(
            (
                _wire_string(limit.get("heading"), f"wire budget limits[{index}].heading"),
                _wire_integer(
                    limit.get("max_characters"),
                    f"wire budget limits[{index}].max_characters",
                ),
            )
        )
    budget = PromptBudget(
        _wire_integer(budget_wire.get("max_characters"), "wire budget max_characters"),
        _wire_integer(budget_wire.get("max_estimated_tokens"), "wire budget max_estimated_tokens"),
        tuple(limits),
    )

    raw_diagnostics = root.get("diagnostics")
    if type(raw_diagnostics) is not list:
        raise ContractValidationError("wire diagnostics must be a list")
    observed_budget_diagnostics: list[tuple[object, object, object, object]] = []
    for index, raw_diagnostic in enumerate(raw_diagnostics):
        diagnostic = _wire_object(raw_diagnostic, f"wire diagnostics[{index}]")
        code = diagnostic.get("code")
        if type(code) is str and code.startswith("budget."):
            observed_budget_diagnostics.append(
                (
                    diagnostic.get("severity"),
                    code,
                    diagnostic.get("message"),
                    diagnostic.get("location"),
                )
            )
    expected_budget_diagnostics = [
        (item.severity.value, item.code, item.message, item.location)
        for item in _budget_diagnostics(usage, budget)
    ]
    if observed_budget_diagnostics != expected_budget_diagnostics:
        raise ContractValidationError("wire budget diagnostics contradict budget and usage")


def _semantic_diagnostics(
    plan: ContextPlan, document: PromptDocument, parsed: PromptParseResult
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    canonical = (
        render_base_prompt(plan)
        if plan.request.profile.name is PromptProfile.BASE
        else render_full_reference_prompt(plan)
    )
    expected_speakers = tuple(
        match.group("ordinals") for match in _SPEAKER.finditer(canonical.text)
    )
    observed_speakers = tuple(match.group("ordinals") for match in _SPEAKER.finditer(document.text))
    if (
        observed_speakers != expected_speakers
        or any(int(ordinal[1:]) < 1 for group in observed_speakers for ordinal in group.split(","))
        or any(
            render_dialogue_line(plan, value) not in document.text
            for value in plan.hard_constraints.exact_texts
            if value.kind in (ExactTextKind.DIALOGUE, ExactTextKind.LYRICS)
        )
    ):
        diagnostics.append(
            _diagnostic(
                "semantic.speaker_id",
                "speaker identifiers do not match the typed exact-dialogue identity",
                "document.text",
            )
        )
    expected_sections = {section.heading: section.body for section in canonical.sections}
    for parsed_field in parsed.fields:
        if parsed_field.value == "N/A" and expected_sections.get(parsed_field.name) != "N/A":
            diagnostics.append(
                _diagnostic(
                    "semantic.na_allowed",
                    "N/A is not permitted for a populated semantic section",
                    f"document.sections.{parsed_field.name}",
                )
            )
    if plan.request.profile.name is PromptProfile.FULL_REFERENCE:
        expected_times = tuple(
            (match.group("ordinal"), match.group("time"))
            for match in _TIMESTAMP.finditer(canonical.text)
        )
        observed_times = tuple(
            (match.group("ordinal"), match.group("time"))
            for match in _TIMESTAMP.finditer(document.text)
        )
        if observed_times != expected_times:
            diagnostics.append(
                _diagnostic(
                    "semantic.timestamp",
                    "shot timestamps do not match the canonical typed timeline",
                    "document.text",
                )
            )
        expected_copy = tuple(
            _event_copy_fact(match) for match in _EVENT_COPY.finditer(canonical.text)
        )
        observed_copy = tuple(
            _event_copy_fact(match) for match in _EVENT_COPY.finditer(document.text)
        )
        if observed_copy != expected_copy:
            diagnostics.append(
                _diagnostic(
                    "semantic.copy_mode",
                    "event copy/reference modes do not match the typed event relations",
                    "document.text",
                )
            )
    expected_labels = tuple(_LABEL.findall(canonical.text))
    observed_labels = tuple(_LABEL.findall(document.text))
    if observed_labels != expected_labels:
        diagnostics.append(
            _diagnostic(
                "backend.label_ownership",
                "backend label occurrences do not preserve typed asset ownership and order",
                "document.text",
            )
        )
        diagnostics.append(
            _diagnostic(
                "backend.missing_label",
                "backend labels differ from the deterministic reference registry",
                "document.text",
            )
        )
    return diagnostics


def validate_profiled_prompt(
    plan: ContextPlan,
    document: PromptDocument,
    binding: SourceProfileBinding,
    budget: PromptBudget | None = None,
) -> SourceProfiledPromptResult:
    """Validate without repairing text or changing the source-evidence decision."""

    if type(plan) is not ContextPlan:
        raise ContractValidationError("profiled validation requires a ContextPlan")
    _validate_document_current(document)
    if type(binding) is not SourceProfileBinding:
        raise ContractValidationError("profiled validation requires a SourceProfileBinding")
    # SECURITY: admission is recomputed by code-owned validation, never a caller-set decision flag.
    binding.__post_init__()
    limits = PromptBudget() if budget is None else budget
    if type(limits) is not PromptBudget:
        raise ContractValidationError("profiled validation requires a PromptBudget")
    limits.__post_init__()
    diagnostics = _source_diagnostics(plan, binding)
    try:
        parsed = parse_prompt(document.text, plan.request.profile, plan.request.task_mode)
    except PromptParseError as exc:
        raise ContractValidationError("profiled parser rejected the typed prompt document") from exc
    diagnostics.extend(
        ValidationDiagnostic(item.severity, item.code, item.message, item.location)
        for item in parsed.diagnostics
    )
    diagnostics.extend(
        ValidationDiagnostic(item.severity, item.code, item.message, item.location)
        for item in lint_prompt(plan, document).diagnostics
    )
    diagnostics.extend(_semantic_diagnostics(plan, document, parsed))
    usage = _budget_usage(document)
    diagnostics.extend(_budget_diagnostics(usage, limits))
    return SourceProfiledPromptResult(
        binding,
        document,
        parsed,
        tuple(diagnostics),
        plan.request.reference_registry.labels,
        limits,
        usage,
        readiness_from_audit(audit_prompt_fidelity(plan, document)),
        _RESULT_AUTHORITY,
    )


def render_profiled_prompt(
    plan: ContextPlan,
    binding: SourceProfileBinding,
    budget: PromptBudget | None = None,
) -> SourceProfiledPromptResult:
    """Render through the existing profile renderer, then validate the exact output."""

    if type(plan) is not ContextPlan:
        raise PromptRenderingError("profiled rendering requires a ContextPlan")
    document = (
        render_base_prompt(plan)
        if plan.request.profile.name is PromptProfile.BASE
        else render_full_reference_prompt(plan)
    )
    return validate_profiled_prompt(plan, document, binding, budget)


__all__ = [
    "OFFICIAL_H3_BASE_GUIDE_DIGEST",
    "OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST",
    "OFFICIAL_H3_GUIDE_REVISION",
    "PromptBudget",
    "PromptBudgetUsage",
    "SOURCE_PROFILED_PROMPT_SCHEMA",
    "LEGACY_SOURCE_PROFILED_PROMPT_SCHEMA",
    "validate_legacy_source_profiled_prompt_wire",
    "SourceProfileBinding",
    "SourceProfileDecision",
    "SourceProfiledPromptResult",
    "build_official_source_profile_binding",
    "build_source_profile_binding",
    "render_profiled_prompt",
    "validate_profiled_prompt",
    "validate_source_profiled_prompt_wire",
]
