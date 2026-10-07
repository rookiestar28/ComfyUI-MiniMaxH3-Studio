"""Offline validator/auditor for the M9-07 metadata-only baseline fixture."""

from __future__ import annotations

import argparse
import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    BaselineEvidenceLayer,
    BaselineObservation,
    BaselineOutcome,
    ProgramEnvelope,
    ProgramEnvelopeCapability,
    ProgramEnvelopeCapabilityMaturity,
    ProgramEnvelopeDecision,
    ProgramEnvelopeLaneLimits,
    ProgramEnvelopeLimits,
    ProgramEnvelopeReauthorizationTrigger,
    ProgramEnvelopeRegressionPriority,
    ProgramEnvelopeResource,
    ProgramEnvelopeStopRule,
    ReconstructionBaselineCase,
    ReconstructionBaselineRunner,
    ReconstructionErrorTaxonomyEntry,
    ReconstructionRegressionItem,
)
from comfyui_h3_context.core.errors import ContractValidationError

ROOT = Path(__file__).resolve().parents[1]
MAX_BASELINE_BYTES = 2_000_000
AUDIT_SCHEMA = "h3.reconstruction.baseline.audit.v1"


class BaselineAuditError(ValueError):
    """Raised when the metadata-only fixture cannot be safely audited."""


def _mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise BaselineAuditError(f"{field_name} must be an object")
    return dict(value)


def _sequence(value: object, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise BaselineAuditError(f"{field_name} must be an array")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise BaselineAuditError(f"{field_name} must be text")
    return value


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BaselineAuditError(f"{field_name} must be an integer")
    return value


def _tuple_text(value: object, field_name: str) -> tuple[str, ...]:
    return tuple(_text(item, field_name) for item in _sequence(value, field_name))


def _fingerprint_tuple(value: object, field_name: str) -> tuple[str, ...]:
    return tuple(_text(item, field_name) for item in _sequence(value, field_name))


def _build_envelope(raw: Mapping[str, object]) -> ProgramEnvelope:
    limits = _mapping(raw["limits"], "envelope.limits")
    lane_limits = tuple(
        ProgramEnvelopeLaneLimits(
            lane_id=_text(item["lane_id"], "lane_limits.lane_id"),
            max_calls=_integer(item["max_calls"], "lane_limits.max_calls"),
            max_spend_minor_units=_integer(item["max_spend_minor_units"], "lane_limits.max_spend"),
            max_compute_seconds=_integer(item["max_compute_seconds"], "lane_limits.compute"),
            max_wall_seconds=_integer(item["max_wall_seconds"], "lane_limits.wall"),
        )
        for item in (
            _mapping(value, "lane_limits[]")
            for value in _sequence(raw["lane_limits"], "lane_limits")
        )
    )
    capabilities = tuple(
        ProgramEnvelopeCapability(
            capability_id=_text(item["capability_id"], "capabilities.capability_id"),
            lane_id=_text(item["lane_id"], "capabilities.lane_id"),
            maturity=ProgramEnvelopeCapabilityMaturity(
                _text(item["maturity"], "capabilities.maturity")
            ),
            rationale=_text(item["rationale"], "capabilities.rationale"),
        )
        for item in (
            _mapping(value, "capabilities[]")
            for value in _sequence(raw["capabilities"], "capabilities")
        )
    )
    stop_rules = tuple(
        ProgramEnvelopeStopRule(
            rule_id=_text(item["rule_id"], "stop_rules.rule_id"),
            resource=ProgramEnvelopeResource(_text(item["resource"], "stop_rules.resource")),
            threshold=_integer(item["threshold"], "stop_rules.threshold"),
            decision=ProgramEnvelopeDecision(_text(item["decision"], "stop_rules.decision")),
            rationale=_text(item["rationale"], "stop_rules.rationale"),
        )
        for item in (
            _mapping(value, "stop_rules[]") for value in _sequence(raw["stop_rules"], "stop_rules")
        )
    )
    triggers = tuple(
        ProgramEnvelopeReauthorizationTrigger(
            trigger_id=_text(item["trigger_id"], "reauthorization.trigger_id"),
            trigger_kind=_text(item["trigger_kind"], "reauthorization.trigger_kind"),
            due_date=_text(item["due_date"], "reauthorization.due_date"),
            rationale=_text(item["rationale"], "reauthorization.rationale"),
        )
        for item in (
            _mapping(value, "reauthorization_triggers[]")
            for value in _sequence(raw["reauthorization_triggers"], "reauthorization_triggers")
        )
    )
    return ProgramEnvelope(
        envelope_id=_text(raw["envelope_id"], "envelope_id"),
        revision=_text(raw["revision"], "envelope.revision"),
        effective_date=_text(raw["effective_date"], "envelope.effective_date"),
        expires_at=_text(raw["expires_at"], "envelope.expires_at"),
        limits=ProgramEnvelopeLimits(
            max_calls=_integer(limits["max_calls"], "limits.max_calls"),
            max_spend_minor_units=_integer(limits["max_spend_minor_units"], "limits.max_spend"),
            currency=_text(limits["currency"], "limits.currency"),
            max_compute_seconds=_integer(limits["max_compute_seconds"], "limits.compute"),
            max_storage_bytes=_integer(limits["max_storage_bytes"], "limits.storage"),
            max_retention_days=_integer(limits["max_retention_days"], "limits.retention"),
            max_reviewer_minutes=_integer(limits["max_reviewer_minutes"], "limits.reviewer"),
            max_candidates=_integer(limits["max_candidates"], "limits.candidates"),
            max_wall_seconds=_integer(limits["max_wall_seconds"], "limits.wall"),
        ),
        lane_limits=lane_limits,
        capabilities=capabilities,
        stop_rules=stop_rules,
        marginal_value_rules=_tuple_text(raw["marginal_value_rules"], "marginal_value_rules"),
        reauthorization_triggers=triggers,
    )


def _build_case(raw: Mapping[str, object]) -> ReconstructionBaselineCase:
    return ReconstructionBaselineCase(
        case_id=_text(raw["case_id"], "case_id"),
        corpus_id=_text(raw["corpus_id"], "corpus_id"),
        partition_id=_text(raw["partition_id"], "partition_id"),
        task_mode=_text(raw["task_mode"], "task_mode"),
        language=_text(raw["language"], "language"),
        risk_classes=_tuple_text(raw["risk_classes"], "risk_classes"),
        input_fingerprints=_fingerprint_tuple(raw["input_fingerprints"], "input_fingerprints"),
        source_fingerprints=_fingerprint_tuple(raw["source_fingerprints"], "source_fingerprints"),
    )


def _build_observation(raw: Mapping[str, object]) -> BaselineObservation:
    metrics = _mapping(raw.get("metrics", {}), "metrics")
    return BaselineObservation(
        case_id=_text(raw["case_id"], "observation.case_id"),
        layer=BaselineEvidenceLayer(_text(raw["layer"], "observation.layer")),
        outcome=BaselineOutcome(_text(raw["outcome"], "observation.outcome")),
        lane_id=_text(raw["lane_id"], "observation.lane_id"),
        input_fingerprints=_fingerprint_tuple(
            raw["input_fingerprints"], "observation.input_fingerprints"
        ),
        output_fingerprint=cast(str | None, raw.get("output_fingerprint")),
        metrics=tuple(
            (_text(key, "metric name"), _text(value, "metric value"))
            for key, value in metrics.items()
        ),
        diagnostic_codes=_tuple_text(raw.get("diagnostic_codes", []), "diagnostic_codes"),
        source_revision=_text(raw.get("source_revision", "baseline.manual.v1"), "source_revision"),
    )


class _FixtureExecutor:
    def __init__(
        self, observations: Mapping[tuple[str, BaselineEvidenceLayer], BaselineObservation]
    ) -> None:
        self.observations = observations

    def execute(
        self,
        baseline_case: ReconstructionBaselineCase,
        *,
        layer: BaselineEvidenceLayer,
    ) -> BaselineObservation:
        return self.observations[(baseline_case.case_id, layer)]


@dataclass(frozen=True, slots=True)
class BaselineAudit:
    status: str
    report_status: str
    baseline_id: str
    report_fingerprint: str
    envelope_fingerprint: str
    missing_observation_count: int
    failed_observation_count: int
    claim_ceiling: str
    layer_summary: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": AUDIT_SCHEMA,
            "status": self.status,
            "report_status": self.report_status,
            "baseline_id": self.baseline_id,
            "report_fingerprint": self.report_fingerprint,
            "envelope_fingerprint": self.envelope_fingerprint,
            "missing_observation_count": self.missing_observation_count,
            "failed_observation_count": self.failed_observation_count,
            "claim_ceiling": self.claim_ceiling,
            "layer_summary": list(self.layer_summary),
        }


def inspect_baseline(path: Path) -> BaselineAudit:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BaselineAuditError("baseline fixture cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BaselineAuditError("baseline fixture must be a regular non-symlink file")
    if metadata.st_size > MAX_BASELINE_BYTES:
        raise BaselineAuditError("baseline fixture exceeds the byte limit")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        root = _mapping(document, "baseline")
        if root.get("schema") != "h3.reconstruction.baseline.v1":
            raise BaselineAuditError("unsupported baseline schema")
        envelope = _build_envelope(_mapping(root["envelope"], "envelope"))
        cases = tuple(
            _build_case(_mapping(value, "cases[]")) for value in _sequence(root["cases"], "cases")
        )
        observations = tuple(
            _build_observation(_mapping(value, "observations[]"))
            for value in _sequence(root["observations"], "observations")
        )
        observation_map = {(item.case_id, item.layer): item for item in observations}
        taxonomy = tuple(
            ReconstructionErrorTaxonomyEntry(
                _text(item["error_code"], "taxonomy.error_code"),
                BaselineEvidenceLayer(_text(item["layer"], "taxonomy.layer")),
                _text(item["classification"], "taxonomy.classification"),
                _text(item["description"], "taxonomy.description"),
            )
            for item in (
                _mapping(value, "error_taxonomy[]")
                for value in _sequence(root["error_taxonomy"], "error_taxonomy")
            )
        )
        regressions = tuple(
            ReconstructionRegressionItem(
                _text(item["regression_id"], "regression.regression_id"),
                _text(item["error_code"], "regression.error_code"),
                BaselineEvidenceLayer(_text(item["layer"], "regression.layer")),
                ProgramEnvelopeRegressionPriority(_text(item["priority"], "regression.priority")),
                _text(item["status"], "regression.status"),
                _integer(item["case_count"], "regression.case_count"),
                _text(item["rationale"], "regression.rationale"),
                _text(item["next_action"], "regression.next_action"),
            )
            for item in (
                _mapping(value, "regressions[]")
                for value in _sequence(root["regressions"], "regressions")
            )
        )
        runner = ReconstructionBaselineRunner()
        result = runner.run(
            cases,
            _FixtureExecutor(observation_map),
            envelope=envelope,
            baseline_id=_text(root["baseline_id"], "baseline_id"),
            baseline_revision=_text(root["baseline_revision"], "baseline_revision"),
            as_of=envelope.effective_date,
            error_taxonomy=taxonomy,
            regressions=regressions,
        )
        return BaselineAudit(
            status="PASS",
            report_status=result.report.status.value,
            baseline_id=result.report.baseline_id,
            report_fingerprint=result.report.fingerprint,
            envelope_fingerprint=envelope.fingerprint,
            missing_observation_count=result.report.missing_observation_count,
            failed_observation_count=result.report.failed_observation_count,
            claim_ceiling=result.report.claim_ceiling.value,
            layer_summary=tuple(item.to_public_dict() for item in result.report.layer_summary),
        )
    except (KeyError, TypeError, ValueError, ContractValidationError) as exc:
        raise BaselineAuditError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = inspect_baseline(args.baseline)
    except BaselineAuditError as exc:
        payload = {"schema": AUDIT_SCHEMA, "status": "INVALID", "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
