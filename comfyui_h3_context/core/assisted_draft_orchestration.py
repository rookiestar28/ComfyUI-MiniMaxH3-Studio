"""M22-12 fail-closed assisted-draft execution and content-free receipts.

This pure boundary decides whether an exact catalog profile may construct an execution binding,
wraps every model call with cancellation checks, delegates drafting and repair to the accepted
M22-05 core, and emits a new browser-safe receipt rather than forwarding provider transport data.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn

from .assisted_draft import (
    AssistedDraftResult,
    DraftCandidate,
    DraftGuarantee,
    DraftModel,
    DraftModelExecutionError,
    RepairShape,
    run_assisted_draft,
)
from .context_reporting import ContextPlan, PromptDocument
from .contracts import ValidationSeverity
from .prompt_model_provider import (
    LegacyPromptModelProfile,
    ModelChoice,
    PromptModelContractError,
    PromptModelOutcome,
    PromptModelOutcomeId,
    PromptModelProfile,
    PromptModelQualificationState,
    PromptModelRemediation,
    build_prompt_model_outcome,
)

ASSISTED_DRAFT_EXECUTION_SCHEMA = "h3.context.assisted_draft.execution.v1"
ASSISTED_DRAFT_RECEIPT_SCHEMA = "h3.context.assisted_draft.receipt.v2"
MAX_USAGE_COUNT = 1 << 48


def _fail(code: str) -> NoReturn:
    raise PromptModelContractError(code)


def _observed_model(value: object) -> None:
    if not isinstance(value, str) or (
        value and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}", value) is None
    ):
        _fail("assisted_observed_model")


def _count(value: object, code: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_USAGE_COUNT:
        _fail(code)
    return value


def _closed_outcome(
    outcome_id: PromptModelOutcomeId,
    remediation: PromptModelRemediation = PromptModelRemediation.NONE,
) -> PromptModelOutcome:
    return build_prompt_model_outcome(
        outcome_id,
        severity=ValidationSeverity.ERROR,
        remediation=remediation,
        parameters=(),
    )


@dataclass(frozen=True, slots=True)
class AssistedDraftUsage:
    """Content-free aggregate measurements. No endpoint or text field can be represented."""

    requests: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    downgraded: bool = False
    observed_model_id: str = ""

    def __post_init__(self) -> None:
        if type(self.downgraded) is not bool:
            _fail("assisted_usage")
        _observed_model(self.observed_model_id)
        for name in (
            "requests",
            "request_bytes",
            "response_bytes",
            "prompt_tokens",
            "completion_tokens",
            "duration_ms",
        ):
            _count(getattr(self, name), "assisted_usage")


@dataclass(frozen=True, slots=True)
class AssistedDraftModelBinding:
    """One admitted model callable plus a content-free usage snapshot provider."""

    model: DraftModel
    usage: Callable[[], AssistedDraftUsage]

    def __post_init__(self) -> None:
        if not callable(self.model) or not callable(self.usage):
            _fail("assisted_model_binding")


@dataclass(frozen=True, slots=True)
class AssistedDraftReceipt:
    """The only provider-execution facts allowed to reach assisted review."""

    action_id: str
    profile_id: str
    provider_family: str
    model_id: str
    attempts: int
    outcome_id: PromptModelOutcomeId
    requests: int
    request_bytes: int
    response_bytes: int
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    evidence_fingerprint: str
    provider_revision: int
    downgraded: bool = False
    observed_model_id: str = ""

    def __post_init__(self) -> None:
        if type(self.downgraded) is not bool:
            _fail("assisted_receipt_downgraded")
        _observed_model(self.observed_model_id)
        if not self.action_id.startswith("action_") or len(self.action_id) != 39:
            _fail("assisted_receipt_action")
        for value in (self.profile_id, self.provider_family, self.model_id):
            if not isinstance(value, str) or not value or len(value) > 256:
                _fail("assisted_receipt_identity")
        if type(self.attempts) is not int or not 1 <= self.attempts <= 2:
            _fail("assisted_receipt_attempts")
        if not isinstance(self.outcome_id, PromptModelOutcomeId):
            _fail("assisted_receipt_outcome")
        for name in (
            "requests",
            "request_bytes",
            "response_bytes",
            "prompt_tokens",
            "completion_tokens",
            "duration_ms",
        ):
            _count(getattr(self, name), "assisted_receipt_count")
        if (
            not isinstance(self.evidence_fingerprint, str)
            or len(self.evidence_fingerprint) != 71
            or not self.evidence_fingerprint.startswith("sha256:")
        ):
            _fail("assisted_receipt_fingerprint")
        try:
            int(self.evidence_fingerprint[7:], 16)
        except ValueError:
            _fail("assisted_receipt_fingerprint")
        if type(self.provider_revision) is not int or not 0 <= self.provider_revision <= 1_000_000:
            _fail("assisted_receipt_revision")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": ASSISTED_DRAFT_RECEIPT_SCHEMA,
            "action_id": self.action_id,
            "profile_id": self.profile_id,
            "provider_family": self.provider_family,
            "model_id": self.model_id,
            "attempts": self.attempts,
            "outcome_id": self.outcome_id.value,
            "requests": self.requests,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "duration_ms": self.duration_ms,
            "evidence_fingerprint": self.evidence_fingerprint,
            "provider_revision": self.provider_revision,
            "downgraded": self.downgraded,
            "observed_model_id": self.observed_model_id,
        }


@dataclass(frozen=True, slots=True)
class AssistedDraftExecutionResult:
    """An optional usable draft plus its closed outcome and content-free usage evidence."""

    outcome: PromptModelOutcome
    draft: AssistedDraftResult | None = None
    receipt: AssistedDraftReceipt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, PromptModelOutcome):
            _fail("assisted_execution_outcome")
        if self.draft is not None and (
            not isinstance(self.draft, AssistedDraftResult) or not self.draft.usable
        ):
            _fail("assisted_execution_draft")
        if self.receipt is not None and not isinstance(self.receipt, AssistedDraftReceipt):
            _fail("assisted_execution_receipt")

    @property
    def usable(self) -> bool:
        return self.draft is not None and self.outcome.outcome_id is PromptModelOutcomeId.OK

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": ASSISTED_DRAFT_EXECUTION_SCHEMA,
            "outcome": self.outcome.to_wire(),
            "draft": None if self.draft is None else self.draft.to_wire(),
            "receipt": None if self.receipt is None else self.receipt.to_wire(),
        }


class _CancellationBoundModel:
    __slots__ = ("_inner", "_cancelled")

    def __init__(self, inner: DraftModel, cancelled: Callable[[], bool]) -> None:
        self._inner = inner
        self._cancelled = cancelled

    @staticmethod
    def _cancelled_outcome() -> PromptModelOutcome:
        return _closed_outcome(PromptModelOutcomeId.CANCELLED)

    def __call__(self, instruction: str, *, shape: RepairShape | None) -> DraftCandidate:
        # CRITICAL: this check occurs before both the first request and a possible repair request.
        if self._cancelled():
            raise DraftModelExecutionError(self._cancelled_outcome())
        candidate = self._inner(instruction, shape=shape)
        # CRITICAL: an authority mutation while a synchronous request is in flight discards its
        # response before audit/repair/publication can observe it.
        if self._cancelled():
            raise DraftModelExecutionError(self._cancelled_outcome())
        return candidate


def _receipt(
    *,
    profile: PromptModelProfile,
    model_id: str,
    usage: AssistedDraftUsage,
    attempts: int,
    outcome_id: PromptModelOutcomeId,
    evidence_fingerprint: str,
    provider_revision: int,
    session_generation: str,
    authority_epoch: int,
) -> AssistedDraftReceipt:
    # SECURITY: a receipt identifies one explicit execution, not merely its reusable authority.
    # The session generation and epoch still bind proposal ownership privately; neither is exposed.
    del session_generation, authority_epoch
    return AssistedDraftReceipt(
        action_id="action_" + secrets.token_hex(16),
        profile_id=profile.profile_id,
        provider_family=profile.family.value,
        model_id=model_id,
        attempts=attempts,
        outcome_id=outcome_id,
        requests=usage.requests,
        request_bytes=usage.request_bytes,
        response_bytes=usage.response_bytes,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        duration_ms=usage.duration_ms,
        evidence_fingerprint=evidence_fingerprint,
        provider_revision=provider_revision,
        downgraded=usage.downgraded,
        observed_model_id=usage.observed_model_id,
    )


def run_assisted_draft_orchestration(
    *,
    profile: object,
    plan: ContextPlan,
    template: PromptDocument,
    guarantee: DraftGuarantee,
    instruction: str,
    evidence_fingerprint: str,
    provider_revision: int,
    session_generation: str,
    authority_epoch: int,
    model_factory: Callable[[], AssistedDraftModelBinding],
    cancellation: Callable[[], bool],
    model_choice: ModelChoice | None = None,
) -> AssistedDraftExecutionResult:
    """Admit qualification before binding construction, then draft under cancellable authority."""

    if not isinstance(profile, PromptModelProfile):
        _fail("assisted_profile")
    if model_choice is not None and model_choice.profile_id == profile.profile_id:
        model_id = model_choice.model_id
    elif isinstance(profile, LegacyPromptModelProfile):
        model_id = profile.model_id
    else:
        _fail("assisted_model_choice")
    if not callable(model_factory) or not callable(cancellation):
        _fail("assisted_dependency")
    if type(provider_revision) is not int or not 0 <= provider_revision <= 1_000_000:
        _fail("assisted_provider_revision")
    if not isinstance(session_generation, str) or not session_generation:
        _fail("assisted_session_generation")
    if type(authority_epoch) is not int or not 0 <= authority_epoch <= 1_000_000:
        _fail("assisted_authority_epoch")

    # SECURITY: qualification is checked before the factory can import or construct an exchange.
    if profile.qualification_state is not PromptModelQualificationState.QUALIFIED:
        return AssistedDraftExecutionResult(
            outcome=_closed_outcome(PromptModelOutcomeId.PROFILE_NOT_QUALIFIED)
        )
    if cancellation():
        return AssistedDraftExecutionResult(outcome=_closed_outcome(PromptModelOutcomeId.CANCELLED))

    try:
        binding = model_factory()
    except DraftModelExecutionError as exc:
        return AssistedDraftExecutionResult(outcome=exc.outcome)
    except Exception:
        return AssistedDraftExecutionResult(
            outcome=_closed_outcome(
                PromptModelOutcomeId.TRANSPORT, PromptModelRemediation.RETRY_LATER
            )
        )
    if not isinstance(binding, AssistedDraftModelBinding):
        _fail("assisted_model_binding")

    result = run_assisted_draft(
        plan=plan,
        template=template,
        guarantee=guarantee,
        model=_CancellationBoundModel(binding.model, cancellation),
        instruction=instruction,
        evidence_fingerprint=evidence_fingerprint,
    )
    try:
        usage = binding.usage()
    except Exception:
        usage = None
    if not isinstance(usage, AssistedDraftUsage):
        return AssistedDraftExecutionResult(
            outcome=_closed_outcome(
                PromptModelOutcomeId.TRANSPORT, PromptModelRemediation.RETRY_LATER
            )
        )

    outcome = result.outcome
    if cancellation():
        outcome = _closed_outcome(PromptModelOutcomeId.CANCELLED)
    receipt = _receipt(
        profile=profile,
        model_id=model_id,
        usage=usage,
        attempts=result.attempts,
        outcome_id=outcome.outcome_id,
        evidence_fingerprint=evidence_fingerprint,
        provider_revision=provider_revision,
        session_generation=session_generation,
        authority_epoch=authority_epoch,
    )
    return AssistedDraftExecutionResult(
        outcome=outcome,
        draft=result if outcome.outcome_id is PromptModelOutcomeId.OK and result.usable else None,
        receipt=receipt,
    )
