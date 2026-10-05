"""Guide readiness: whether a structurally valid prompt actually satisfies the official guide.

Structural validity and guide readiness are different facts, and conflating them is the defect this
module exists to remove. The renderer can produce a perfectly shaped Base document -- correct
preamble, correct three fields, correct timestamps -- from a plan that owns almost no semantic
content, and comparing that document against the same canonical renderer only proves the renderer
reproduced itself.

Readiness is therefore derived from the independent fidelity audit over the *typed plan*, never
from the prose and never from a caller-supplied flag. The result is bounded, versioned and
content-free: it carries stable identities, not the user's words.

This module imports no ComfyUI, browser, HTTP, filesystem or provider runtime, and it makes no
claim about the private hosted H3-Context-IR workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .context_reporting import ContextPlan, PromptDocument
from .errors import PromptLintError
from .prompt_fidelity import (
    PromptFidelityAuditResult,
    PromptFidelityDiagnosticId,
    audit_prompt_fidelity,
)

GUIDE_CONFORMANCE_SCHEMA = "h3.context.guide_conformance.v2"

_MAX_REASONS = 64
_RESULT_AUTHORITY = object()


class GuideReadiness(str, Enum):
    """The closed guide-conformance disposition."""

    READY = "ready"
    """Every mode, audio and structural obligation is satisfied by owned typed evidence."""

    INCOMPLETE = "incomplete"
    """A bounded, inspectable draft whose required semantics are absent or unowned."""

    MODIFIED = "modified"
    """A deliberate local behavior that differs from the official guide and says so."""


@dataclass(frozen=True, slots=True)
class GuideConformanceResult:
    """One versioned readiness verdict. Only the evaluator may construct it."""

    readiness: GuideReadiness
    reasons: tuple[PromptFidelityDiagnosticId, ...]
    _authority: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        # SECURITY: readiness is recomputed by code-owned evaluation and can never be asserted by
        # a caller. A self-declared `READY` would let a semantically empty plan claim official
        # conformance, which is precisely the claim this contract exists to withhold.
        if self._authority is not _RESULT_AUTHORITY:
            raise PromptLintError("guide conformance must be produced by the evaluator")
        if not isinstance(self.readiness, GuideReadiness):
            raise PromptLintError("guide readiness must be a GuideReadiness")
        if not isinstance(self.reasons, tuple) or len(self.reasons) > _MAX_REASONS:
            raise PromptLintError("guide conformance reasons exceed the bounded limit")
        if not all(isinstance(item, PromptFidelityDiagnosticId) for item in self.reasons):
            raise PromptLintError("guide conformance reasons must be fidelity identities")
        if self.readiness is GuideReadiness.READY and self.reasons:
            raise PromptLintError("a ready verdict cannot carry withholding reasons")

    @property
    def is_ready(self) -> bool:
        return self.readiness is GuideReadiness.READY

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": GUIDE_CONFORMANCE_SCHEMA,
            "readiness": self.readiness.value,
            "reasons": [item.value for item in self.reasons],
        }


def readiness_from_audit(audit: PromptFidelityAuditResult) -> GuideConformanceResult:
    """Derive readiness from an already computed fidelity audit, without auditing twice."""

    if not isinstance(audit, PromptFidelityAuditResult):
        raise PromptLintError("guide conformance requires a PromptFidelityAuditResult")
    seen: list[PromptFidelityDiagnosticId] = []
    for diagnostic in audit.readiness_findings:
        if diagnostic.diagnostic_id not in seen:
            seen.append(diagnostic.diagnostic_id)
    readiness = GuideReadiness.INCOMPLETE if seen else GuideReadiness.READY
    return GuideConformanceResult(readiness, tuple(seen), _RESULT_AUTHORITY)


def evaluate_guide_conformance(
    plan: ContextPlan, document: PromptDocument
) -> GuideConformanceResult:
    """Audit the plan and document once, and report whether the guide is actually satisfied."""

    return readiness_from_audit(audit_prompt_fidelity(plan, document))


__all__ = [
    "GUIDE_CONFORMANCE_SCHEMA",
    "GuideConformanceResult",
    "GuideReadiness",
    "evaluate_guide_conformance",
    "readiness_from_audit",
]
