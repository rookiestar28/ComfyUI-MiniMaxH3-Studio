"""M16-06 offline drift classification and report-only process tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest import mock

import jsonschema
import pytest

from comfyui_h3_context.core import (
    ChangeSignal,
    DriftEventError,
    DriftKind,
    DriftProcessReport,
    PublicationMonitoringState,
    PublicationState,
    SemanticSeam,
    SourceSurface,
    build_default_drift_source_registry,
    build_default_source_drift_checkpoint,
    classify_drift_event,
    decode_drift_event_json,
    decode_drift_process_report_json,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "governance" / "contracts" / "drift_process_v1.schema.json"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "drift_process_v1.json"
CLI_PATH = ROOT / "scripts" / "drift_process.py"
FP_A = "sha256:" + ("a" * 64)
FP_B = "sha256:" + ("b" * 64)
PUBLISHED_HASH = "sha256:" + ("c" * 64)


def _surface(source_id: str = "comfyui.supported.native_h3") -> SourceSurface:
    checkpoint = build_default_source_drift_checkpoint()
    return next(item for item in checkpoint.surfaces if item.surface_id == source_id)


def _event(
    *,
    source_id: str = "comfyui.supported.native_h3",
    observed_identity: str | None = None,
    baseline_semantic_fingerprint: str | None = FP_A,
    observed_semantic_fingerprint: str | None = FP_A,
    availability: str = "AVAILABLE",
    change_signal: str = "NONE",
    publication_state: str = "APPROVED_UNPUBLISHED",
    published_artifact_hash: str | None = None,
) -> dict[str, object]:
    surface = _surface(source_id)
    baseline_identity = surface.expected_identity
    return {
        "schema": "h3.drift.event.v1",
        "source_id": source_id,
        "baseline_identity": baseline_identity,
        "observed_identity": baseline_identity if observed_identity is None else observed_identity,
        "baseline_semantic_fingerprint": baseline_semantic_fingerprint,
        "observed_semantic_fingerprint": observed_semantic_fingerprint,
        "availability": availability,
        "change_signal": change_signal,
        "observed_on": "2026-08-14",
        "publication_state": publication_state,
        "published_artifact_hash": published_artifact_hash,
    }


def _classify(value: dict[str, object]) -> DriftProcessReport:
    event = decode_drift_event_json(json.dumps(value, separators=(",", ":")))
    return classify_drift_event(event)


def test_registry_is_closed_complete_bounded_and_reuses_checkpoint_authority() -> None:
    registry = build_default_drift_source_registry()
    checkpoint = build_default_source_drift_checkpoint()

    assert len(registry) == len(checkpoint.surfaces) == 26
    assert tuple(item.source_id for item in registry) == tuple(
        item.surface_id for item in checkpoint.surfaces
    )
    assert tuple(item.baseline_identity for item in registry) == tuple(
        item.expected_identity for item in checkpoint.surfaces
    )
    assert len({item.source_id for item in registry}) == len(registry)
    assert len({route.route_id for item in registry for route in item.routes}) >= 8
    assert all(item.semantic_extractor_id.startswith("extractor.") for item in registry)
    assert all(item.required_check_ids for item in registry)
    assert all(item.resource_budget.max_observation_bytes == 65_536 for item in registry)

    seams = {item.semantic_seam.value for item in registry}
    assert seams >= {
        "prompt_media_typing",
        "native_inputs_enums",
        "v3_dynamic_paths",
        "subgraph_inputs",
        "sigma_shift",
        "open_box_sidebar_state",
        "native_manual_reversibility",
        "classified_errors",
        "container_geometry",
        "fallback_absence",
        "dependency_license_advisory",
        "artifact_sbom_hash",
    }


@pytest.mark.parametrize(
    ("source_id", "identity_changed", "semantic_changed", "signal", "expected"),
    (
        ("comfyui.supported.native_h3", False, False, "NONE", DriftKind.NO_DRIFT),
        (
            "comfyui.supported.native_h3",
            True,
            False,
            "NONE",
            DriftKind.UNCHANGED_SEAM_AT_NEW_REPO_REVISION,
        ),
        (
            "comfyui.supported.native_h3",
            True,
            True,
            "ADDITIVE",
            DriftKind.ADDITIVE_NON_SUBSTITUTING,
        ),
        (
            "comfyui.docs.sidebar",
            True,
            True,
            "PRESENTATION_BUILD",
            DriftKind.PRESENTATION_OR_BUILD_DRIFT,
        ),
        (
            "comfyui.supported.native_h3",
            True,
            True,
            "COMPATIBLE_CONTRACT",
            DriftKind.COMPATIBLE_CONTRACT_DRIFT,
        ),
        (
            "comfyui.supported.native_h3",
            True,
            True,
            "CONTRACT_BREAKING",
            DriftKind.ROUTE_BLOCKING_CONTRACT_DRIFT,
        ),
        (
            "minimax.model.license",
            True,
            True,
            "SECURITY_LICENSE",
            DriftKind.SECURITY_OR_LICENSE_BLOCKER,
        ),
        (
            "m14.fidelity_scorecard",
            True,
            True,
            "INTEGRITY_MISMATCH",
            DriftKind.SECURITY_OR_LICENSE_BLOCKER,
        ),
        (
            "comfyui.supported.native_h3",
            True,
            True,
            "UNRESOLVED",
            DriftKind.UNRESOLVED,
        ),
    ),
)
def test_classifier_derives_every_available_drift_kind(
    source_id: str,
    identity_changed: bool,
    semantic_changed: bool,
    signal: str,
    expected: DriftKind,
) -> None:
    value = _event(
        source_id=source_id,
        observed_identity=("git:" + ("d" * 40)) if identity_changed else None,
        observed_semantic_fingerprint=FP_B if semantic_changed else FP_A,
        change_signal=signal,
    )
    report = _classify(value)

    assert report.drift_kind is expected
    assert report.source_id == value["source_id"]
    assert report.event_fingerprint.startswith("sha256:")


@pytest.mark.parametrize("signal", ("SECURITY_LICENSE", "INTEGRITY_MISMATCH"))
def test_classifier_rejects_cross_seam_security_or_integrity_signal_injection(
    signal: str,
) -> None:
    event = decode_drift_event_json(
        json.dumps(
            _event(
                source_id="comfyui.supported.native_h3",
                observed_identity="gitblob:" + ("d" * 40),
                observed_semantic_fingerprint=FP_B,
                change_signal=signal,
            )
        )
    )

    with pytest.raises(DriftEventError, match="semantic seam"):
        classify_drift_event(event)


def test_classifier_rejects_every_signal_not_admitted_by_the_registered_seam() -> None:
    contract_signals = frozenset(
        {
            ChangeSignal.ADDITIVE,
            ChangeSignal.COMPATIBLE_CONTRACT,
            ChangeSignal.CONTRACT_BREAKING,
        }
    )
    expected_policy = {
        SemanticSeam.PROMPT_MEDIA_TYPING: contract_signals,
        SemanticSeam.NATIVE_INPUTS_ENUMS: contract_signals,
        SemanticSeam.V3_DYNAMIC_PATHS: contract_signals,
        SemanticSeam.SUBGRAPH_INPUTS: contract_signals,
        SemanticSeam.SIGMA_SHIFT: contract_signals,
        SemanticSeam.OPEN_BOX_SIDEBAR_STATE: frozenset(
            {*contract_signals, ChangeSignal.PRESENTATION_BUILD}
        ),
        SemanticSeam.NATIVE_MANUAL_REVERSIBILITY: contract_signals,
        SemanticSeam.CLASSIFIED_ERRORS: contract_signals,
        SemanticSeam.CONTAINER_GEOMETRY: contract_signals,
        SemanticSeam.FALLBACK_ABSENCE: contract_signals,
        SemanticSeam.DEPENDENCY_LICENSE_ADVISORY: frozenset(
            {
                ChangeSignal.ADDITIVE,
                ChangeSignal.COMPATIBLE_CONTRACT,
                ChangeSignal.SECURITY_LICENSE,
            }
        ),
        SemanticSeam.ARTIFACT_SBOM_HASH: frozenset({ChangeSignal.INTEGRITY_MISMATCH}),
    }
    semantic_change_signals = frozenset(
        {
            ChangeSignal.ADDITIVE,
            ChangeSignal.PRESENTATION_BUILD,
            ChangeSignal.COMPATIBLE_CONTRACT,
            ChangeSignal.CONTRACT_BREAKING,
            ChangeSignal.SECURITY_LICENSE,
            ChangeSignal.INTEGRITY_MISMATCH,
        }
    )

    for definition in build_default_drift_source_registry():
        identity_prefix, digest = definition.baseline_identity.split(":", 1)
        observed = f"{identity_prefix}:{'d' * len(digest)}"
        for signal in semantic_change_signals - expected_policy[definition.semantic_seam]:
            event = decode_drift_event_json(
                json.dumps(
                    _event(
                        source_id=definition.source_id,
                        observed_identity=observed,
                        observed_semantic_fingerprint=FP_B,
                        change_signal=signal.value,
                    )
                )
            )
            with pytest.raises(DriftEventError, match="semantic seam"):
                classify_drift_event(event)


def test_security_and_integrity_signals_are_admitted_only_by_their_owned_release_seams() -> None:
    license_report = _classify(
        _event(
            source_id="minimax.model.license",
            observed_identity="sha256:" + ("d" * 64),
            observed_semantic_fingerprint=FP_B,
            change_signal="SECURITY_LICENSE",
        )
    )
    integrity_report = _classify(
        _event(
            source_id="m14.fidelity_scorecard",
            observed_identity="sha256:" + ("e" * 64),
            observed_semantic_fingerprint=FP_B,
            change_signal="INTEGRITY_MISMATCH",
        )
    )

    assert license_report.drift_kind is DriftKind.SECURITY_OR_LICENSE_BLOCKER
    assert integrity_report.drift_kind is DriftKind.SECURITY_OR_LICENSE_BLOCKER
    assert {route.route_id for route in license_report.affected_routes} == {
        "release.supply.integrity"
    }
    assert {route.route_id for route in integrity_report.affected_routes} == {
        "release.supply.integrity"
    }


def test_global_source_unavailable_unresolved_and_repository_only_paths_cover_every_seam() -> None:
    for definition in build_default_drift_source_registry():
        unavailable = _event(
            source_id=definition.source_id,
            observed_semantic_fingerprint=None,
            availability="UNAVAILABLE",
            change_signal="UNAVAILABLE",
        )
        unavailable["observed_identity"] = None
        assert _classify(unavailable).drift_kind is DriftKind.SOURCE_UNAVAILABLE

        unresolved = _event(
            source_id=definition.source_id,
            observed_semantic_fingerprint=None,
            change_signal="UNRESOLVED",
        )
        assert _classify(unresolved).drift_kind is DriftKind.UNRESOLVED

        identity_prefix, digest = definition.baseline_identity.split(":", 1)
        repository_only = _event(
            source_id=definition.source_id,
            observed_identity=f"{identity_prefix}:{'d' * len(digest)}",
        )
        assert (
            _classify(repository_only).drift_kind is DriftKind.UNCHANGED_SEAM_AT_NEW_REPO_REVISION
        )


def test_source_unavailable_is_owned_and_fails_closed_without_invented_observation() -> None:
    value = _event(
        observed_identity=None,
        baseline_semantic_fingerprint=FP_A,
        observed_semantic_fingerprint=None,
        availability="UNAVAILABLE",
        change_signal="UNAVAILABLE",
    )
    value["observed_identity"] = None
    report = _classify(value)

    assert report.drift_kind is DriftKind.SOURCE_UNAVAILABLE
    assert report.blocking is True
    assert report.claim_ceiling.value == "OWNED_ROUTE_ONLY"
    assert report.affected_routes


def test_unresolved_semantic_observation_is_distinct_from_source_unavailable() -> None:
    report = _classify(_event(observed_semantic_fingerprint=None, change_signal="UNRESOLVED"))

    assert report.availability.value == "AVAILABLE"
    assert report.observed_identity is not None
    assert report.observed_semantic_fingerprint is None
    assert report.drift_kind is DriftKind.UNRESOLVED
    assert report.blocking is True


def test_smallest_route_is_registry_owned_and_does_not_block_unrelated_manual_route() -> None:
    sidebar = _classify(
        _event(
            source_id="comfyui.docs.sidebar",
            observed_identity="sha256:" + ("d" * 64),
            observed_semantic_fingerprint=FP_B,
            change_signal="CONTRACT_BREAKING",
        )
    )
    prompt = _classify(
        _event(
            source_id="minimax.guide.base",
            observed_identity="sha256:" + ("e" * 64),
            observed_semantic_fingerprint=FP_B,
            change_signal="CONTRACT_BREAKING",
        )
    )

    sidebar_ids = {route.route_id for route in sidebar.affected_routes}
    prompt_ids = {route.route_id for route in prompt.affected_routes}
    assert sidebar_ids
    assert prompt_ids
    assert sidebar_ids.isdisjoint(prompt_ids)
    assert all(route.product_surface.value == "SIDEBAR" for route in sidebar.affected_routes)
    assert all(route.task_mode.value == "MANUAL" for route in prompt.affected_routes)


@pytest.mark.parametrize(
    ("state", "published_hash", "monitoring"),
    (
        ("DO_NOT_PUBLISH", None, PublicationMonitoringState.BLOCKED_BY_POLICY),
        (
            "APPROVED_UNPUBLISHED",
            None,
            PublicationMonitoringState.INACTIVE_NO_PUBLISHED_HASH,
        ),
        ("PUBLISHED_EXACT", PUBLISHED_HASH, PublicationMonitoringState.ACTIVE_EXACT_HASH),
    ),
)
def test_publication_states_are_distinct_and_exact(
    state: str, published_hash: str | None, monitoring: PublicationMonitoringState
) -> None:
    report = _classify(_event(publication_state=state, published_artifact_hash=published_hash))

    assert report.publication_state is PublicationState(state)
    assert report.publication_monitoring_state is monitoring


def test_publication_state_cannot_claim_or_hide_a_public_hash() -> None:
    hostile = (
        _event(publication_state="PUBLISHED_EXACT", published_artifact_hash=None),
        _event(publication_state="APPROVED_UNPUBLISHED", published_artifact_hash=PUBLISHED_HASH),
        _event(publication_state="DO_NOT_PUBLISH", published_artifact_hash=PUBLISHED_HASH),
    )
    for value in hostile:
        with pytest.raises(DriftEventError, match="published"):
            decode_drift_event_json(json.dumps(value))


def test_caller_cannot_choose_classifier_route_owner_check_or_disposition_authority() -> None:
    for field, value in (
        ("drift_kind", "NO_DRIFT"),
        ("affected_routes", []),
        ("owner_roles", []),
        ("required_check_ids", []),
        ("disposition", "PASS"),
        ("notes", "free form"),
        ("url", "https://example.test"),
        ("path", "C:/private/file"),
    ):
        changed = _event()
        changed[field] = value
        with pytest.raises(DriftEventError, match="unknown"):
            decode_drift_event_json(json.dumps(changed))


def test_decoder_rejects_wrong_baseline_inconsistent_facts_and_unsafe_values() -> None:
    wrong_baseline = _event()
    wrong_baseline["baseline_identity"] = "git:" + ("0" * 40)
    semantic_drift_without_signal = _event(observed_semantic_fingerprint=FP_B)
    signal_without_semantic_drift = _event(change_signal="ADDITIVE")
    unknown_source = _event()
    unknown_source["source_id"] = "unknown.source"
    unsafe_identifier = _event()
    unsafe_identifier["source_id"] = "../private"

    for value in (
        wrong_baseline,
        semantic_drift_without_signal,
        signal_without_semantic_drift,
        unknown_source,
        unsafe_identifier,
    ):
        with pytest.raises(DriftEventError):
            decode_drift_event_json(json.dumps(value))


def test_decoder_is_exact_strict_utf8_duplicate_aware_finite_depth_and_size_bounded() -> None:
    payload = json.dumps(_event(), separators=(",", ":"))
    for encoded in (payload, payload.encode(), bytearray(payload.encode())):
        assert decode_drift_event_json(encoded).source_id == "comfyui.supported.native_h3"

    duplicate = payload.replace(
        '"source_id":"comfyui.supported.native_h3",',
        '"source_id":"comfyui.supported.native_h3","source_id":"comfyui.supported.native_h3",',
        1,
    )
    hostile: tuple[object, ...] = (
        duplicate,
        b"\xef\xbb\xbf" + payload.encode(),
        b"\xff",
        "NaN",
        b" " * 65_537,
        {"not": "wire"},
    )
    for value in hostile:
        with pytest.raises(DriftEventError):
            decode_drift_event_json(value)  # type: ignore[arg-type]

    class SpoofStr(str):
        pass

    with pytest.raises(DriftEventError, match="text or bytes"):
        decode_drift_event_json(SpoofStr(payload))


def test_schema_fixture_report_round_trip_and_content_free_templates() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    fixture_bytes = FIXTURE_PATH.read_bytes()
    fixture = json.loads(fixture_bytes.decode("utf-8"))

    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(fixture, schema)
    event_schema = {
        "$schema": schema["$schema"],
        "$id": "comfyui-h3-context://contracts/drift_event_v1.test.schema.json",
        "$ref": "#/$defs/event",
        "$defs": schema["$defs"],
    }
    jsonschema.validate(_event(), event_schema)
    unavailable = _event(
        observed_semantic_fingerprint=None,
        availability="UNAVAILABLE",
        change_signal="UNAVAILABLE",
    )
    unavailable["observed_identity"] = None
    jsonschema.validate(unavailable, event_schema)
    report = decode_drift_process_report_json(fixture_bytes)
    assert report.to_wire() == fixture
    assert report.publication_state is PublicationState.APPROVED_UNPUBLISHED
    assert report.publication_monitoring_state is (
        PublicationMonitoringState.INACTIVE_NO_PUBLISHED_HASH
    )

    projected = json.dumps(
        {"issue": fixture["issue_template"], "advisory": fixture["advisory_template"]},
        sort_keys=True,
    ).casefold()
    for forbidden in (
        "body",
        "description",
        "prompt",
        "credential",
        "token=",
        "signed",
        ".planning",
        "c:/",
        "/home/",
        "https://",
    ):
        assert forbidden not in projected


def test_report_contract_rejects_mutation_and_is_deterministic() -> None:
    event = decode_drift_event_json(json.dumps(_event()))
    first = classify_drift_event(event)
    second = classify_drift_event(event)

    assert first == second
    assert first.fingerprint == second.fingerprint
    with pytest.raises((DriftEventError, ValueError)):
        replace(first, source_id="unknown.source")
    with pytest.raises(DriftEventError, match="outcome"):
        replace(first, materiality=first.materiality.NON_BLOCKING)
    with pytest.raises(DriftEventError, match="event fingerprint"):
        replace(first, observed_identity="git:" + ("d" * 40))
    with pytest.raises(DriftEventError, match="event fingerprint"):
        replace(first, event_fingerprint="sha256:" + ("0" * 64))


def test_report_decoder_rejects_forged_event_fingerprint() -> None:
    wire = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    wire["event_fingerprint"] = "sha256:" + ("0" * 64)

    with pytest.raises(DriftEventError, match="event fingerprint"):
        decode_drift_process_report_json(json.dumps(wire))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_id", "minimax.guide.base"),
        ("observed_identity", "gitblob:" + ("d" * 40)),
        ("baseline_semantic_fingerprint", FP_B),
        ("observed_semantic_fingerprint", FP_B),
        ("availability", "UNAVAILABLE"),
        ("change_signal", "UNRESOLVED"),
        ("observed_on", "2026-08-13"),
        ("publication_state", "DO_NOT_PUBLISH"),
        ("published_artifact_hash", PUBLISHED_HASH),
    ),
)
def test_report_decoder_rejects_mutated_retained_event_fact(
    field: str,
    value: object,
) -> None:
    wire = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    wire[field] = value

    with pytest.raises(DriftEventError):
        decode_drift_process_report_json(json.dumps(wire))


def test_report_constructor_rejects_coherent_cross_seam_blocker_forgery() -> None:
    native = _classify(
        _event(
            observed_identity="git:" + ("d" * 40),
            observed_semantic_fingerprint=FP_B,
            change_signal="CONTRACT_BREAKING",
        )
    )
    forged_templates = tuple(
        replace(
            template,
            drift_kind=DriftKind.SECURITY_OR_LICENSE_BLOCKER,
            disposition=native.disposition.BLOCK_PUBLICATION,
        )
        for template in (native.issue_template, native.advisory_template)
    )

    with pytest.raises(DriftEventError, match="semantic seam"):
        replace(
            native,
            drift_kind=DriftKind.SECURITY_OR_LICENSE_BLOCKER,
            materiality=native.materiality.BLOCKING,
            claim_ceiling=native.claim_ceiling.NO_PUBLICATION,
            disposition=native.disposition.BLOCK_PUBLICATION,
            review_state=native.review_state.REQUIRED,
            closure_state=native.closure_state.OPEN,
            issue_template=forged_templates[0],
            advisory_template=forged_templates[1],
        )


def test_report_decoder_enforces_every_drift_kind_for_every_registered_seam() -> None:
    global_kinds = {
        DriftKind.NO_DRIFT,
        DriftKind.UNCHANGED_SEAM_AT_NEW_REPO_REVISION,
        DriftKind.SOURCE_UNAVAILABLE,
        DriftKind.UNRESOLVED,
    }
    contract_kinds = {
        DriftKind.ADDITIVE_NON_SUBSTITUTING,
        DriftKind.COMPATIBLE_CONTRACT_DRIFT,
        DriftKind.ROUTE_BLOCKING_CONTRACT_DRIFT,
    }
    expected_policy = {
        SemanticSeam.PROMPT_MEDIA_TYPING: contract_kinds | global_kinds,
        SemanticSeam.NATIVE_INPUTS_ENUMS: contract_kinds | global_kinds,
        SemanticSeam.V3_DYNAMIC_PATHS: contract_kinds | global_kinds,
        SemanticSeam.SUBGRAPH_INPUTS: contract_kinds | global_kinds,
        SemanticSeam.SIGMA_SHIFT: contract_kinds | global_kinds,
        SemanticSeam.OPEN_BOX_SIDEBAR_STATE: contract_kinds
        | global_kinds
        | {DriftKind.PRESENTATION_OR_BUILD_DRIFT},
        SemanticSeam.NATIVE_MANUAL_REVERSIBILITY: contract_kinds | global_kinds,
        SemanticSeam.CLASSIFIED_ERRORS: contract_kinds | global_kinds,
        SemanticSeam.CONTAINER_GEOMETRY: contract_kinds | global_kinds,
        SemanticSeam.FALLBACK_ABSENCE: contract_kinds | global_kinds,
        SemanticSeam.DEPENDENCY_LICENSE_ADVISORY: global_kinds
        | {
            DriftKind.ADDITIVE_NON_SUBSTITUTING,
            DriftKind.COMPATIBLE_CONTRACT_DRIFT,
            DriftKind.SECURITY_OR_LICENSE_BLOCKER,
        },
        SemanticSeam.ARTIFACT_SBOM_HASH: global_kinds | {DriftKind.SECURITY_OR_LICENSE_BLOCKER},
    }
    semantic_outcomes = {
        DriftKind.ADDITIVE_NON_SUBSTITUTING: (
            "NON_BLOCKING",
            "OWNED_ROUTE_ONLY",
            "RUN_AFFECTED_CHECKS",
        ),
        DriftKind.PRESENTATION_OR_BUILD_DRIFT: (
            "NON_BLOCKING",
            "OWNED_ROUTE_ONLY",
            "RUN_AFFECTED_CHECKS",
        ),
        DriftKind.COMPATIBLE_CONTRACT_DRIFT: (
            "NON_BLOCKING",
            "OWNED_ROUTE_ONLY",
            "RUN_AFFECTED_CHECKS",
        ),
        DriftKind.ROUTE_BLOCKING_CONTRACT_DRIFT: (
            "BLOCKING",
            "OWNED_ROUTE_ONLY",
            "BLOCK_OWNED_ROUTE",
        ),
        DriftKind.SECURITY_OR_LICENSE_BLOCKER: (
            "BLOCKING",
            "NO_PUBLICATION",
            "BLOCK_PUBLICATION",
        ),
    }
    signal_for_kind = {
        DriftKind.ADDITIVE_NON_SUBSTITUTING: "ADDITIVE",
        DriftKind.PRESENTATION_OR_BUILD_DRIFT: "PRESENTATION_BUILD",
        DriftKind.COMPATIBLE_CONTRACT_DRIFT: "COMPATIBLE_CONTRACT",
        DriftKind.ROUTE_BLOCKING_CONTRACT_DRIFT: "CONTRACT_BREAKING",
    }

    assert set(expected_policy) == set(SemanticSeam)
    assert set(DriftKind) == global_kinds | set(semantic_outcomes)

    for definition in build_default_drift_source_registry():
        identity_prefix, digest = definition.baseline_identity.split(":", 1)
        changed_identity = f"{identity_prefix}:{'d' * len(digest)}"
        for kind in DriftKind:
            if kind in global_kinds:
                if kind is DriftKind.NO_DRIFT:
                    event = _event(source_id=definition.source_id)
                elif kind is DriftKind.UNCHANGED_SEAM_AT_NEW_REPO_REVISION:
                    event = _event(
                        source_id=definition.source_id,
                        observed_identity=changed_identity,
                    )
                elif kind is DriftKind.SOURCE_UNAVAILABLE:
                    event = _event(
                        source_id=definition.source_id,
                        observed_semantic_fingerprint=None,
                        availability="UNAVAILABLE",
                        change_signal="UNAVAILABLE",
                    )
                    event["observed_identity"] = None
                else:
                    event = _event(
                        source_id=definition.source_id,
                        observed_semantic_fingerprint=None,
                        change_signal="UNRESOLVED",
                    )
                report = _classify(event)
                assert report.drift_kind is kind
                assert decode_drift_process_report_json(json.dumps(report.to_wire())) == report
                continue

            materiality, claim_ceiling, disposition = semantic_outcomes[kind]
            if kind in expected_policy[definition.semantic_seam]:
                signal = signal_for_kind.get(kind)
                if signal is None:
                    signal = (
                        "SECURITY_LICENSE"
                        if definition.semantic_seam is SemanticSeam.DEPENDENCY_LICENSE_ADVISORY
                        else "INTEGRITY_MISMATCH"
                    )
                report = _classify(
                    _event(
                        source_id=definition.source_id,
                        observed_identity=changed_identity,
                        observed_semantic_fingerprint=FP_B,
                        change_signal=signal,
                    )
                )
                assert report.drift_kind is kind
                assert decode_drift_process_report_json(json.dumps(report.to_wire())) == report
                continue

            wire = _classify(_event(source_id=definition.source_id)).to_wire()
            wire["observed_identity"] = changed_identity
            wire["observed_semantic_fingerprint"] = FP_B
            wire["drift_kind"] = kind.value
            wire["materiality"] = materiality
            wire["claim_ceiling"] = claim_ceiling
            wire["disposition"] = disposition
            wire["review_state"] = "REQUIRED"
            wire["closure_state"] = "OPEN"
            for template_name in ("issue_template", "advisory_template"):
                template = wire[template_name]
                assert isinstance(template, dict)
                template["observed_identity"] = wire["observed_identity"]
                template["drift_kind"] = kind.value
                template["disposition"] = disposition

            with pytest.raises(DriftEventError, match="semantic seam"):
                decode_drift_process_report_json(json.dumps(wire))


def test_release_supply_security_and_integrity_reports_round_trip() -> None:
    events = (
        _event(
            source_id="minimax.model.license",
            observed_identity="sha256:" + ("d" * 64),
            observed_semantic_fingerprint=FP_B,
            change_signal="SECURITY_LICENSE",
        ),
        _event(
            source_id="m14.fidelity_scorecard",
            observed_identity="sha256:" + ("e" * 64),
            observed_semantic_fingerprint=FP_B,
            change_signal="INTEGRITY_MISMATCH",
        ),
    )
    for event in events:
        report = _classify(event)
        decoded = decode_drift_process_report_json(json.dumps(report.to_wire()))
        assert decoded == report
        assert decoded.drift_kind is DriftKind.SECURITY_OR_LICENSE_BLOCKER
        assert tuple(route.route_id for route in decoded.affected_routes) == (
            "release.supply.integrity",
        )


def test_event_date_must_be_a_real_calendar_date() -> None:
    value = _event()
    value["observed_on"] = "2026-02-31"
    with pytest.raises(DriftEventError, match="real calendar"):
        decode_drift_event_json(json.dumps(value))


def test_cli_is_stdout_only_report_only_and_uses_expected_exit_semantics(tmp_path: Path) -> None:
    input_path = tmp_path / "event.json"
    input_path.write_text(json.dumps(_event()), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(CLI_PATH), str(input_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout)["drift_kind"] == "NO_DRIFT"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["event.json"]

    blocking = _event(
        observed_identity="git:" + ("d" * 40),
        observed_semantic_fingerprint=FP_B,
        change_signal="CONTRACT_BREAKING",
    )
    input_path.write_text(json.dumps(blocking), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(CLI_PATH), str(input_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert completed.stderr == ""
    assert json.loads(completed.stdout)["drift_kind"] == "ROUTE_BLOCKING_CONTRACT_DRIFT"


def test_cli_rejects_invalid_or_unsafe_file_entries_without_echo_or_residue(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_bytes(b"\xef\xbb\xbf{}")
    entries = [invalid, tmp_path]
    link = tmp_path / "linked.json"
    try:
        link.symlink_to(invalid)
    except OSError:
        pass
    else:
        entries.append(link)

    for entry in entries:
        completed = subprocess.run(
            [sys.executable, str(CLI_PATH), str(entry)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 1
        assert completed.stderr == ""
        error = json.loads(completed.stdout)
        assert error == {"schema": "h3.drift.process.error.v1", "status": "INVALID_INPUT"}
        assert str(tmp_path) not in completed.stdout


def test_cli_reader_detects_changed_during_read(tmp_path: Path) -> None:
    from scripts import drift_process as cli

    input_path = tmp_path / "event.json"
    input_path.write_text(json.dumps(_event()), encoding="utf-8")
    real_fstat = os.fstat
    calls = 0

    def changing_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        current = real_fstat(fd)
        calls += 1
        if calls == 2:
            values = list(current)
            values[6] = current.st_size + 1
            return os.stat_result(values)
        return current

    with mock.patch("scripts.drift_process.os.fstat", side_effect=changing_fstat):
        with pytest.raises(cli.DriftProcessCliError, match="changed"):
            cli.read_event_file(input_path)


@pytest.mark.parametrize(
    "raw_path",
    (
        r"\\server\share\event.json",
        r"\\?\UNC\server\share\event.json",
        r"\\.\pipe\event",
        r"\\?\C:\event.json",
    ),
)
def test_cli_rejects_nonlocal_windows_namespaces_before_any_filesystem_io(
    raw_path: str,
) -> None:
    from scripts import drift_process as cli

    with (
        mock.patch("scripts.drift_process.Path.lstat") as lstat,
        mock.patch("scripts.drift_process.os.stat") as os_stat,
        mock.patch("scripts.drift_process.os.open") as os_open,
        pytest.raises(cli.DriftProcessCliError, match="local filesystem"),
    ):
        cli.read_event_file(Path(raw_path))
    lstat.assert_not_called()
    os_stat.assert_not_called()
    os_open.assert_not_called()


@pytest.mark.parametrize("raw_path", ("event.json", r"C:\events\event.json"))
def test_cli_local_namespace_guard_accepts_relative_and_drive_qualified_paths(
    raw_path: str,
) -> None:
    from scripts import drift_process as cli

    cli._require_local_filesystem_namespace(Path(raw_path))


def test_cli_exposes_no_fetch_write_fix_publish_or_execute_options(tmp_path: Path) -> None:
    input_path = tmp_path / "event.json"
    input_path.write_text(json.dumps(_event()), encoding="utf-8")
    for option in ("--url", "--output", "--write", "--fix", "--publish", "--execute"):
        completed = subprocess.run(
            [sys.executable, str(CLI_PATH), option, str(input_path)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0
        assert not any(path.name != "event.json" for path in tmp_path.iterdir())


def test_public_enums_are_closed_and_change_signal_is_not_an_outcome() -> None:
    assert {item.value for item in ChangeSignal} == {
        "NONE",
        "ADDITIVE",
        "PRESENTATION_BUILD",
        "COMPATIBLE_CONTRACT",
        "CONTRACT_BREAKING",
        "SECURITY_LICENSE",
        "INTEGRITY_MISMATCH",
        "UNAVAILABLE",
        "UNRESOLVED",
    }
    assert "NO_DRIFT" not in {item.value for item in ChangeSignal}
