"""M22-05 assisted drafting: one model pass, a deterministic audit, and at most one repair.

A model may propose a prompt here. It may not decide that its proposal is good. The audit that
judges the candidate is `M21-01`'s, unchanged; the conditions under which a repair replaces the
original are fixed in this module; and both are pure functions of recorded inputs, so the whole
decision replays from a transcript without a model.

The repair is bounded at exactly one attempt and is adopted only when all four conditions hold: the
re-audit is clean, the reference inventory is unchanged and inside the allowed set, every piece of
user-supplied dialogue and visible text survives byte-identical, and the repair did not hit its
output limit. Any other result keeps the original candidate and records which condition failed, as
its own identifier -- "the re-audit still failed" and "the repair rewrote the user's dialogue" are
different problems for the person deciding whether to accept a draft, and collapsing them into one
message would lose exactly the distinction they need.

Model output is data. Instruction-like text inside a candidate is not followed and not propagated:
the candidate is refused, because passing it downstream stripped-but-adopted would leave a prompt
whose provenance nobody can explain.

Regeneration anchors on the recorded first pass rather than on whatever currently sits in the
editor, so repeated rounds cannot drift away from the evidence. The anchor is invalidated by any
change to the underlying evidence and is overwritten only by a pass that succeeded, so a failed
attempt never destroys the last good one.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import NoReturn, Protocol

from .context_reporting import ContextPlan, PromptDocument, PromptRenderStatus
from .contracts import ValidationSeverity
from .prompt_fidelity import (
    PromptFidelityAuditResult,
    PromptFidelityDiagnostic,
    audit_prompt_fidelity,
)
from .prompt_model_provider import (
    PromptModelContractError,
    PromptModelOutcome,
    PromptModelOutcomeId,
    PromptModelRemediation,
    build_prompt_model_outcome,
)

ASSISTED_DRAFT_SCHEMA = "h3-context-assisted-draft/1"

MAX_CANDIDATE_CHARACTERS = 65_536
MAX_GUARANTEED_SPANS = 64
MAX_SPAN_CHARACTERS = 4_096
MAX_REFERENCE_LABELS = 32

# Reference labels are rendered by `core/registry.py` in exactly this shape, so the inventory is
# read from the text by an exact pattern rather than by anything the model could paraphrase.
_REFERENCE_LABEL = re.compile(r"<(?:Picture|Video|Audio) [1-9][0-9]{0,2}>")

# Instruction-like text a model sometimes echoes back. These are refusal triggers, not filters: the
# candidate is rejected whole, because a stripped candidate is one nobody can account for.
_INJECTION_PATTERNS = (
    re.compile(r"(?i)\bignore (?:all |any )?(?:the )?(?:previous|prior|above) instructions?\b"),
    re.compile(r"(?i)\bdisregard (?:the )?(?:previous|prior|above|system)\b"),
    re.compile(r"(?i)\byou are now\b"),
    re.compile(r"(?i)^\s*system\s*:", re.MULTILINE),
    re.compile(r"(?i)<\s*/?\s*(?:system|instruction)\s*>"),
    re.compile(r"(?i)\bnew instructions?\s*:"),
)


def _fail(code: str) -> NoReturn:
    raise PromptModelContractError(code)


class RepairShape(str, Enum):
    """Which of the two repair shapes ran. Chosen mechanically, never by preference."""

    NARROW_TEXT_CORRECTION = "narrow_text_correction"
    EVIDENCE_CONTINUATION = "evidence_continuation"


class AdoptionCondition(str, Enum):
    """The four conditions a repair must satisfy. Each fails on its own identifier."""

    REAUDIT_PASSED = "reaudit_passed"
    REFERENCE_INVENTORY_UNCHANGED = "reference_inventory_unchanged"
    USER_TEXT_PRESERVED = "user_text_preserved"
    OUTPUT_WITHIN_LIMIT = "output_within_limit"


_CONDITION_OUTCOMES = {
    AdoptionCondition.REAUDIT_PASSED: PromptModelOutcomeId.REPAIR_REAUDIT_FAILED,
    AdoptionCondition.REFERENCE_INVENTORY_UNCHANGED: PromptModelOutcomeId.REPAIR_REFERENCE_DRIFT,
    AdoptionCondition.USER_TEXT_PRESERVED: PromptModelOutcomeId.REPAIR_USER_TEXT_ALTERED,
    AdoptionCondition.OUTPUT_WITHIN_LIMIT: PromptModelOutcomeId.REPAIR_OUTPUT_TRUNCATED,
}


def _spans(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        _fail(field)
    if len(value) > MAX_GUARANTEED_SPANS:
        _fail(field)
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_SPAN_CHARACTERS:
            _fail(field)
    return value


@dataclass(frozen=True, slots=True)
class DraftGuarantee:
    """What the draft is not allowed to change, stated before the model is asked anything."""

    allowed_labels: tuple[str, ...]
    required_labels: tuple[str, ...]
    user_dialogue: tuple[str, ...] = ()
    visible_text: tuple[str, ...] = ()
    hard_constraints: tuple[str, ...] = ()
    output_character_limit: int = MAX_CANDIDATE_CHARACTERS

    def __post_init__(self) -> None:
        for name in ("allowed_labels", "required_labels"):
            labels = _spans(getattr(self, name), f"guarantee_{name}")
            if len(labels) > MAX_REFERENCE_LABELS:
                _fail(f"guarantee_{name}")
            for label in labels:
                if _REFERENCE_LABEL.fullmatch(label) is None:
                    _fail(f"guarantee_{name}")
        if not set(self.required_labels) <= set(self.allowed_labels):
            _fail("guarantee_required_labels")
        _spans(self.user_dialogue, "guarantee_user_dialogue")
        _spans(self.visible_text, "guarantee_visible_text")
        _spans(self.hard_constraints, "guarantee_hard_constraints")
        if (
            type(self.output_character_limit) is not int
            or not 0 < self.output_character_limit <= MAX_CANDIDATE_CHARACTERS
        ):
            _fail("guarantee_output_limit")

    @property
    def preserved_spans(self) -> tuple[str, ...]:
        return self.user_dialogue + self.visible_text + self.hard_constraints


@dataclass(frozen=True, slots=True)
class DraftCandidate:
    """What the model returned, with the one fact the model itself reports: was it cut off."""

    text: str
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or len(self.text) > MAX_CANDIDATE_CHARACTERS:
            _fail("candidate_text")
        if type(self.truncated) is not bool:
            _fail("candidate_truncated")


def reference_inventory(text: object) -> tuple[str, ...]:
    """The reference labels a text uses, in first-appearance order, each counted once."""

    if not isinstance(text, str):
        _fail("inventory_text")
    seen: list[str] = []
    for match in _REFERENCE_LABEL.finditer(text):
        label = match.group(0)
        if label not in seen:
            seen.append(label)
    return tuple(seen)


def contains_instruction_injection(text: object) -> bool:
    """True when model output contains text shaped like an instruction to whoever reads it next."""

    if not isinstance(text, str):
        _fail("injection_text")
    return any(pattern.search(text) is not None for pattern in _INJECTION_PATTERNS)


def missing_required_labels(guarantee: object, text: object) -> tuple[str, ...]:
    """Required reference labels the candidate did not use, in declared order."""

    if not isinstance(guarantee, DraftGuarantee):
        _fail("repair_guarantee")
    present = set(reference_inventory(text))
    return tuple(label for label in guarantee.required_labels if label not in present)


def select_repair_shape(
    guarantee: object, text: object, *, media_attached: object = False
) -> RepairShape:
    """Choose the repair shape from the failure class. Mechanical, and the same for equal inputs.

    The choice is made from the reference inventory rather than from a fidelity diagnostic identity,
    because "a required reference is missing" is a fact about the candidate's labels and is not one
    of the audit's identities. A text-only correction cannot invent a reference the model never
    looked at, so when evidence is attached the repair continues the original request instead.
    """

    if type(media_attached) is not bool:
        _fail("repair_media_flag")
    if missing_required_labels(guarantee, text) and media_attached:
        return RepairShape.EVIDENCE_CONTINUATION
    return RepairShape.NARROW_TEXT_CORRECTION


def build_repair_instruction(guarantee: object, diagnostics: object, shape: object) -> str:
    """Name the violations and enumerate the exact labels, so a repair cannot renumber one."""

    if not isinstance(guarantee, DraftGuarantee):
        _fail("repair_guarantee")
    if not isinstance(shape, RepairShape):
        _fail("repair_shape")
    if not isinstance(diagnostics, Sequence) or isinstance(diagnostics, (str, bytes)):
        _fail("repair_diagnostics")
    violations: list[str] = []
    for item in diagnostics:
        if not isinstance(item, PromptFidelityDiagnostic):
            _fail("repair_diagnostics")
        violations.append(item.diagnostic_id.value)
    lines = [
        f"shape: {shape.value}",
        "violations: " + ", ".join(violations),
        "allowed_references: " + ", ".join(guarantee.allowed_labels),
        "required_references: " + ", ".join(guarantee.required_labels),
        "preserve_exactly: " + " | ".join(guarantee.preserved_spans),
    ]
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class RepairAdoption:
    """Whether the repair replaced the original, and if not, exactly which condition stopped it."""

    adopted: bool
    failed_condition: AdoptionCondition | None
    outcome: PromptModelOutcome

    def __post_init__(self) -> None:
        if type(self.adopted) is not bool:
            _fail("adoption_flag")
        if self.adopted is (self.failed_condition is not None):
            _fail("adoption_condition")
        if not isinstance(self.outcome, PromptModelOutcome):
            _fail("adoption_outcome")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": ASSISTED_DRAFT_SCHEMA,
            "adopted": self.adopted,
            "failed_condition": (
                None if self.failed_condition is None else self.failed_condition.value
            ),
            "outcome": self.outcome.to_wire(),
        }


def _refused(
    condition: AdoptionCondition, parameters: tuple[tuple[str, str | int | bool], ...]
) -> RepairAdoption:
    return RepairAdoption(
        adopted=False,
        failed_condition=condition,
        outcome=build_prompt_model_outcome(
            _CONDITION_OUTCOMES[condition],
            severity=ValidationSeverity.WARNING,
            remediation=PromptModelRemediation.NONE,
            parameters=parameters,
        ),
    )


def evaluate_repair_adoption(
    *,
    guarantee: object,
    original_text: object,
    repaired: object,
    reaudit: object,
) -> RepairAdoption:
    """Apply the four conditions in a fixed order. Nothing here reads an instruction."""

    if not isinstance(guarantee, DraftGuarantee):
        _fail("adoption_guarantee")
    if not isinstance(original_text, str):
        _fail("adoption_original")
    if not isinstance(repaired, DraftCandidate):
        _fail("adoption_repaired")
    if not isinstance(reaudit, PromptFidelityAuditResult):
        _fail("adoption_reaudit")

    if repaired.truncated or len(repaired.text) > guarantee.output_character_limit:
        return _refused(
            AdoptionCondition.OUTPUT_WITHIN_LIMIT,
            (("characters", len(repaired.text)), ("limit", guarantee.output_character_limit)),
        )
    if reaudit.prose_blocking:
        return _refused(
            AdoptionCondition.REAUDIT_PASSED,
            (("remaining_findings", len(reaudit.prose_blocking)),),
        )

    original_inventory = reference_inventory(original_text)
    repaired_inventory = reference_inventory(repaired.text)
    allowed = set(guarantee.allowed_labels)
    if (
        repaired_inventory != original_inventory
        or not set(repaired_inventory) <= allowed
        or not set(guarantee.required_labels) <= set(repaired_inventory)
    ):
        return _refused(
            AdoptionCondition.REFERENCE_INVENTORY_UNCHANGED,
            (
                ("before", len(original_inventory)),
                ("after", len(repaired_inventory)),
            ),
        )

    for index, span in enumerate(guarantee.preserved_spans):
        if span not in repaired.text:
            return _refused(
                AdoptionCondition.USER_TEXT_PRESERVED,
                (("span_index", index), ("characters", len(span))),
            )

    return RepairAdoption(
        adopted=True,
        failed_condition=None,
        outcome=build_prompt_model_outcome(
            PromptModelOutcomeId.OK,
            severity=ValidationSeverity.INFO,
            remediation=PromptModelRemediation.NONE,
            parameters=(("adopted", True),),
        ),
    )


@dataclass(frozen=True, slots=True)
class AssistedDraftAnchor:
    """The recorded first pass a regeneration starts from, and the evidence it was made against."""

    evidence_fingerprint: str
    candidate_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_fingerprint, str) or not self.evidence_fingerprint:
            _fail("anchor_fingerprint")
        if len(self.evidence_fingerprint) > 128:
            _fail("anchor_fingerprint")
        if not isinstance(self.candidate_text, str):
            _fail("anchor_candidate")
        if len(self.candidate_text) > MAX_CANDIDATE_CHARACTERS:
            _fail("anchor_candidate")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": ASSISTED_DRAFT_SCHEMA,
            "evidence_fingerprint": self.evidence_fingerprint,
            "characters": len(self.candidate_text),
        }


def anchor_is_valid(anchor: object, evidence_fingerprint: object) -> bool:
    """An anchor survives only while the evidence it was drafted against is unchanged."""

    if anchor is None:
        return False
    if not isinstance(anchor, AssistedDraftAnchor):
        _fail("anchor_type")
    if not isinstance(evidence_fingerprint, str) or not evidence_fingerprint:
        _fail("anchor_fingerprint")
    return anchor.evidence_fingerprint == evidence_fingerprint


def next_anchor(
    previous: object, *, candidate_text: object, evidence_fingerprint: object, succeeded: object
) -> AssistedDraftAnchor | None:
    """Overwrite the anchor only on success; drop it only when the evidence moved."""

    if type(succeeded) is not bool:
        _fail("anchor_success_flag")
    if not isinstance(evidence_fingerprint, str) or not evidence_fingerprint:
        _fail("anchor_fingerprint")
    if succeeded:
        if not isinstance(candidate_text, str):
            _fail("anchor_candidate")
        return AssistedDraftAnchor(
            evidence_fingerprint=evidence_fingerprint, candidate_text=candidate_text
        )
    if isinstance(previous, AssistedDraftAnchor) and anchor_is_valid(
        previous, evidence_fingerprint
    ):
        return previous
    return None


class DraftModel(Protocol):
    """The one adapter concern. Everything else in this module is a pure function."""

    def __call__(self, instruction: str, *, shape: RepairShape | None) -> DraftCandidate: ...


class DraftModelExecutionError(RuntimeError):
    """A content-free control-flow error carrying one closed provider outcome."""

    def __init__(self, outcome: object) -> None:
        if not isinstance(outcome, PromptModelOutcome):
            _fail("draft_execution_outcome")
        self.outcome = outcome
        # SECURITY: provider prose never enters Exception.args or repr/log output.
        super().__init__("draft_model_execution_failed")


@dataclass(frozen=True, slots=True)
class AssistedDraftResult:
    """A candidate offered for human review, with the whole basis for offering it attached.

    There is deliberately no method here that turns this into a prompt output. The candidate goes on
    through the ordinary render and validation path like any other text.
    """

    candidate_text: str
    audit: PromptFidelityAuditResult
    outcome: PromptModelOutcome
    attempts: int
    repair_shape: RepairShape | None = None
    adoption: RepairAdoption | None = None
    anchor: AssistedDraftAnchor | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_text, str):
            _fail("result_candidate")
        if not isinstance(self.audit, PromptFidelityAuditResult):
            _fail("result_audit")
        if not isinstance(self.outcome, PromptModelOutcome):
            _fail("result_outcome")
        if type(self.attempts) is not int or not 1 <= self.attempts <= 2:
            # One draft, and at most one repair. There is no arrangement of inputs that reaches
            # three, and the type refuses to represent one.
            _fail("result_attempts")

    @property
    def usable(self) -> bool:
        return self.outcome.outcome_id is PromptModelOutcomeId.OK

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": ASSISTED_DRAFT_SCHEMA,
            "characters": len(self.candidate_text),
            "attempts": self.attempts,
            "repair_shape": None if self.repair_shape is None else self.repair_shape.value,
            "adoption": None if self.adoption is None else self.adoption.to_wire(),
            "audit": self.audit.to_wire(),
            "outcome": self.outcome.to_wire(),
        }


def _document_for(template: PromptDocument, text: str) -> PromptDocument:
    return replace(template, text=text, status=PromptRenderStatus.DRAFT)


def _typed_failure(
    outcome_id: PromptModelOutcomeId,
    audit: PromptFidelityAuditResult,
    attempts: int,
    anchor: AssistedDraftAnchor | None,
) -> AssistedDraftResult:
    return AssistedDraftResult(
        candidate_text="",
        audit=audit,
        outcome=build_prompt_model_outcome(
            outcome_id,
            severity=ValidationSeverity.ERROR,
            remediation=PromptModelRemediation.RETRY_LATER,
            parameters=(),
        ),
        attempts=attempts,
        anchor=anchor,
    )


def _execution_failure(
    outcome: PromptModelOutcome,
    audit: PromptFidelityAuditResult,
    attempts: int,
    anchor: AssistedDraftAnchor | None,
) -> AssistedDraftResult:
    """Preserve the closed identity while dropping all provider-authored detail."""

    scrubbed = build_prompt_model_outcome(
        outcome.outcome_id,
        severity=outcome.severity,
        remediation=outcome.remediation,
        parameters=tuple(outcome.parameters),
    )
    return AssistedDraftResult(
        candidate_text="",
        audit=audit,
        outcome=scrubbed,
        attempts=attempts,
        anchor=anchor,
    )


def _unexpected_execution_failure(
    audit: PromptFidelityAuditResult,
    attempts: int,
    anchor: AssistedDraftAnchor | None,
) -> AssistedDraftResult:
    return _execution_failure(
        build_prompt_model_outcome(
            PromptModelOutcomeId.TRANSPORT,
            severity=ValidationSeverity.ERROR,
            remediation=PromptModelRemediation.RETRY_LATER,
            parameters=(),
        ),
        audit,
        attempts,
        anchor,
    )


def run_assisted_draft(
    *,
    plan: ContextPlan,
    template: PromptDocument,
    guarantee: DraftGuarantee,
    model: DraftModel,
    instruction: str,
    media_attached: bool = False,
    evidence_fingerprint: str,
    previous_anchor: AssistedDraftAnchor | None = None,
    auditor: Callable[[ContextPlan, PromptDocument], PromptFidelityAuditResult] | None = None,
) -> AssistedDraftResult:
    """Draft once, audit, repair at most once, and adopt only under the four fixed conditions."""

    if not isinstance(guarantee, DraftGuarantee):
        _fail("draft_guarantee")
    if not isinstance(instruction, str) or not instruction:
        _fail("draft_instruction")
    if type(media_attached) is not bool:
        _fail("draft_media_flag")
    audit_with = auditor if auditor is not None else audit_prompt_fidelity

    empty_audit = audit_with(plan, _document_for(template, template.text))
    try:
        candidate = model(instruction, shape=None)
    except DraftModelExecutionError as exc:
        return _execution_failure(exc.outcome, empty_audit, 1, previous_anchor)
    except Exception:
        return _unexpected_execution_failure(empty_audit, 1, previous_anchor)
    if not isinstance(candidate, DraftCandidate):
        _fail("draft_candidate")

    if not candidate.text.strip():
        return _typed_failure(PromptModelOutcomeId.DRAFT_EMPTY, empty_audit, 1, previous_anchor)
    if candidate.truncated:
        return _typed_failure(PromptModelOutcomeId.DRAFT_TRUNCATED, empty_audit, 1, previous_anchor)
    if contains_instruction_injection(candidate.text):
        return _typed_failure(
            PromptModelOutcomeId.DRAFT_INSTRUCTION_REFUSED, empty_audit, 1, previous_anchor
        )

    audit = audit_with(plan, _document_for(template, candidate.text))
    if not audit.prose_blocking:
        return AssistedDraftResult(
            candidate_text=candidate.text,
            audit=audit,
            outcome=build_prompt_model_outcome(
                PromptModelOutcomeId.OK,
                severity=ValidationSeverity.INFO,
                remediation=PromptModelRemediation.NONE,
                parameters=(("attempts", 1),),
            ),
            attempts=1,
            anchor=next_anchor(
                previous_anchor,
                candidate_text=candidate.text,
                evidence_fingerprint=evidence_fingerprint,
                succeeded=True,
            ),
        )

    shape = select_repair_shape(guarantee, candidate.text, media_attached=media_attached)
    try:
        repaired = model(
            build_repair_instruction(guarantee, audit.prose_blocking, shape), shape=shape
        )
    except DraftModelExecutionError as exc:
        return _execution_failure(exc.outcome, audit, 2, previous_anchor)
    except Exception:
        return _unexpected_execution_failure(audit, 2, previous_anchor)
    if not isinstance(repaired, DraftCandidate) or contains_instruction_injection(repaired.text):
        return _keep_original(candidate, audit, shape, evidence_fingerprint, previous_anchor)

    reaudit = audit_with(plan, _document_for(template, repaired.text))
    adoption = evaluate_repair_adoption(
        guarantee=guarantee,
        original_text=candidate.text,
        repaired=repaired,
        reaudit=reaudit,
    )
    if not adoption.adopted:
        return AssistedDraftResult(
            candidate_text=candidate.text,
            audit=audit,
            outcome=adoption.outcome,
            attempts=2,
            repair_shape=shape,
            adoption=adoption,
            anchor=next_anchor(
                previous_anchor,
                candidate_text=candidate.text,
                evidence_fingerprint=evidence_fingerprint,
                succeeded=False,
            ),
        )
    return AssistedDraftResult(
        candidate_text=repaired.text,
        audit=reaudit,
        outcome=adoption.outcome,
        attempts=2,
        repair_shape=shape,
        adoption=adoption,
        anchor=next_anchor(
            previous_anchor,
            candidate_text=repaired.text,
            evidence_fingerprint=evidence_fingerprint,
            succeeded=True,
        ),
    )


def _keep_original(
    candidate: DraftCandidate,
    audit: PromptFidelityAuditResult,
    shape: RepairShape,
    evidence_fingerprint: str,
    previous_anchor: AssistedDraftAnchor | None,
) -> AssistedDraftResult:
    adoption = _refused(
        AdoptionCondition.REAUDIT_PASSED, (("remaining_findings", len(audit.prose_blocking)),)
    )
    return AssistedDraftResult(
        candidate_text=candidate.text,
        audit=audit,
        outcome=adoption.outcome,
        attempts=2,
        repair_shape=shape,
        adoption=adoption,
        anchor=next_anchor(
            previous_anchor,
            candidate_text=candidate.text,
            evidence_fingerprint=evidence_fingerprint,
            succeeded=False,
        ),
    )
