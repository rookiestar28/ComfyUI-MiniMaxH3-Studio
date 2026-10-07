"""HC-06 fail-closed visual qualification and contract-only lane tests."""

from __future__ import annotations

import unittest
from dataclasses import fields, replace
from datetime import datetime
from typing import Any
from unittest.mock import Mock, patch

import comfyui_h3_context.core.visual_qualification as visual_qualification_module
from comfyui_h3_context.adapters.comfyui_vlm import (
    NativeVLMObservationAdapter,
    execute_native_vlm_contract_test,
    execute_native_vlm_observation,
)
from comfyui_h3_context.core import (
    AcceptanceCandidateReceipt,
    LocalAdapterCapabilityError,
    LocalDeviceKind,
    LocalDeviceSpec,
    LocalResourceBudget,
    ModelCapabilityState,
    ModelManifest,
    ModelRuntimeProfile,
    VisualAcceptanceReport,
    VisualAcceptanceRuntime,
    VisualBenchmarkLimits,
    VisualBenchmarkPlan,
    VisualCandidateProfile,
    VisualCapabilityMeasurement,
    VisualDeviceProfile,
    VisualDisposition,
    VisualProfileEvidence,
    VisualQualificationBinding,
    VisualQualificationError,
    VisualQualificationReceipt,
    bind_visual_qualification,
    build_default_visual_benchmark_plan,
    create_visual_qualification_receipt,
    evaluate_visual_acceptance,
)
from comfyui_h3_context.core.errors import VLMObservationError
from scripts import m11_02_native_vlm_fixture, m11_08_visual_acceptance_fixture
from scripts.m11_02_native_vlm_fixture import _FixtureClip, _manifest, _output, _request

DECIDED = "2026-08-07T00:00:00Z"
CURRENT = "2026-08-07T12:00:00Z"
EXPIRES = "2026-08-08T00:00:00Z"


def _qualified_evidence() -> tuple[VisualBenchmarkPlan, VisualAcceptanceReport, ModelManifest]:
    manifest = _manifest(cancellation=ModelCapabilityState.QUALIFIED)
    plan = build_default_visual_benchmark_plan()
    candidate = plan.candidates[0]
    qualified = replace(
        candidate,
        adapter_id=manifest.adapter_id,
        adapter_version=manifest.adapter_version,
        model_id=manifest.model_id,
        supports_cancellation_cleanup=True,
        disposition=VisualDisposition.QUALIFIED,
        disposition_reason="synthetic qualification fixture",
        measurements=tuple(
            VisualCapabilityMeasurement(
                capability,
                accuracy_basis_points=9_000,
                calibration_ece_basis_points=1_000,
                deterministic=True,
                platform_supported=True,
            )
            for capability in candidate.capabilities
        ),
    )
    qualified_plan = replace(plan, candidates=(qualified, *plan.candidates[1:]))
    acceptance = evaluate_visual_acceptance(
        qualified_plan,
        runtime=VisualAcceptanceRuntime(
            profile_evidence=(
                VisualProfileEvidence(
                    qualified.candidate_id,
                    latency_ms=100,
                    peak_vram_mb=100,
                    peak_ram_mb=100,
                    deterministic=True,
                    platform_supported=True,
                    cancellation_cleanup_verified=True,
                ),
            )
        ),
    )
    return qualified_plan, acceptance, manifest


def _receipt_evidence() -> tuple[
    VisualBenchmarkPlan,
    VisualAcceptanceReport,
    ModelManifest,
    VisualQualificationReceipt,
]:
    plan, acceptance, manifest = _qualified_evidence()
    candidate = plan.candidates[0]
    receipt = create_visual_qualification_receipt(
        plan,
        acceptance,
        manifest,
        candidate_id=candidate.candidate_id,
        device_profile_id=candidate.device_profiles[0].profile_id,
        decided_at_utc=DECIDED,
        expires_at_utc=EXPIRES,
    )
    return plan, acceptance, manifest, receipt


def _field_values(value: object) -> dict[str, Any]:
    return {
        field.name: getattr(value, field.name)
        for field in fields(value)  # type: ignore[arg-type]
    }


class VisualQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        # The production admission boundary intentionally rechecks wall-clock expiry.
        # Freeze only this test module's clock so the 24-hour fixture cannot become date-flaky.
        clock = Mock(wraps=datetime)
        clock.now.return_value = datetime.fromisoformat(CURRENT.replace("Z", "+00:00"))
        patcher = patch.object(visual_qualification_module, "datetime", clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_program_exposes_one_empty_visual_runtime_surface(self) -> None:
        native = m11_02_native_vlm_fixture.run()
        acceptance = m11_08_visual_acceptance_fixture.run()
        self.assertEqual(native["qualification"], "contract_only")
        self.assertFalse(native["executable_profile"])
        self.assertEqual(acceptance["status"], "unsupported_research")
        self.assertEqual(acceptance["executable_candidate_ids"], [])
        self.assertEqual(
            visual_qualification_module._TRUSTED_VISUAL_QUALIFICATION_RECEIPT_FINGERPRINTS,
            frozenset(),
        )

    def test_default_all_negative_plan_cannot_issue_receipt(self) -> None:
        plan = build_default_visual_benchmark_plan()
        acceptance = evaluate_visual_acceptance(plan)
        candidate = plan.candidates[0]
        with self.assertRaisesRegex(VisualQualificationError, "acceptance is not qualified"):
            create_visual_qualification_receipt(
                plan,
                acceptance,
                _manifest(cancellation=ModelCapabilityState.QUALIFIED),
                candidate_id=candidate.candidate_id,
                device_profile_id=candidate.device_profiles[0].profile_id,
                decided_at_utc=DECIDED,
                expires_at_utc=EXPIRES,
            )

    def test_matching_receipt_admits_exact_qualified_runtime(self) -> None:
        plan, acceptance, manifest, receipt = _receipt_evidence()
        with patch.object(
            visual_qualification_module,
            "_TRUSTED_VISUAL_QUALIFICATION_RECEIPT_FINGERPRINTS",
            frozenset({receipt.receipt_fingerprint}),
        ):
            binding = bind_visual_qualification(
                plan, acceptance, manifest, receipt, current_time_utc=CURRENT
            )
            adapter = NativeVLMObservationAdapter(
                _FixtureClip(_output()), manifest, qualification=binding
            )
            document = execute_native_vlm_observation(
                adapter,
                _request(),
                device=LocalDeviceSpec(LocalDeviceKind.CPU),
            )
        self.assertTrue(document.complete)
        self.assertFalse(adapter.contract_only)
        self.assertEqual(receipt.to_public_dict()["disposition"], "qualified")

    def test_expired_and_future_receipts_fail_closed(self) -> None:
        plan, acceptance, manifest, receipt = _receipt_evidence()
        with patch.object(
            visual_qualification_module,
            "_TRUSTED_VISUAL_QUALIFICATION_RECEIPT_FINGERPRINTS",
            frozenset({receipt.receipt_fingerprint}),
        ):
            with self.assertRaisesRegex(VisualQualificationError, "expired"):
                bind_visual_qualification(
                    plan,
                    acceptance,
                    manifest,
                    receipt,
                    current_time_utc="2026-08-08T00:00:00Z",
                )
            with self.assertRaisesRegex(VisualQualificationError, "future"):
                bind_visual_qualification(
                    plan,
                    acceptance,
                    manifest,
                    receipt,
                    current_time_utc="2026-08-06T00:00:00Z",
                )

    def test_capability_cancellation_privacy_and_device_drift_fail_closed(self) -> None:
        plan, acceptance, manifest = _qualified_evidence()
        candidate = plan.candidates[0]
        common = {
            "candidate_id": candidate.candidate_id,
            "device_profile_id": candidate.device_profiles[0].profile_id,
            "decided_at_utc": DECIDED,
            "expires_at_utc": EXPIRES,
        }
        without_vision = replace(
            manifest,
            capabilities=frozenset(
                item for item in manifest.capabilities if item.value != "vision"
            ),
        )
        with self.assertRaisesRegex(VisualQualificationError, "lacks vision"):
            create_visual_qualification_receipt(plan, acceptance, without_vision, **common)
        with self.assertRaisesRegex(VisualQualificationError, "cancellation cleanup"):
            create_visual_qualification_receipt(
                plan,
                acceptance,
                replace(manifest, cancellation=ModelCapabilityState.UNQUALIFIED),
                **common,
            )
        over_budget = replace(manifest.runtime.limits, max_wall_time_seconds=31.0)
        with self.assertRaisesRegex(VisualQualificationError, "resource limits"):
            create_visual_qualification_receipt(
                plan,
                acceptance,
                replace(
                    manifest,
                    runtime=replace(manifest.runtime, limits=over_budget),
                ),
                **common,
            )

        network_candidate = replace(candidate, requires_network=True)
        network_plan = replace(plan, candidates=(network_candidate, *plan.candidates[1:]))
        network_acceptance = evaluate_visual_acceptance(
            network_plan,
            runtime=VisualAcceptanceRuntime(
                profile_evidence=(
                    VisualProfileEvidence(
                        network_candidate.candidate_id,
                        latency_ms=100,
                        peak_vram_mb=100,
                        peak_ram_mb=100,
                        deterministic=True,
                        platform_supported=True,
                        cancellation_cleanup_verified=True,
                    ),
                )
            ),
        )
        with self.assertRaisesRegex(VisualQualificationError, "cannot require network"):
            create_visual_qualification_receipt(
                network_plan, network_acceptance, manifest, **common
            )

        unsafe_device = replace(candidate.device_profiles[0], supports_cancellation_cleanup=False)
        unsafe_candidate = replace(candidate, device_profiles=(unsafe_device,))
        unsafe_plan = replace(plan, candidates=(unsafe_candidate, *plan.candidates[1:]))
        unsafe_acceptance = evaluate_visual_acceptance(
            unsafe_plan,
            runtime=VisualAcceptanceRuntime(
                profile_evidence=(
                    VisualProfileEvidence(
                        unsafe_candidate.candidate_id,
                        latency_ms=100,
                        peak_vram_mb=100,
                        peak_ram_mb=100,
                        deterministic=True,
                        platform_supported=True,
                        cancellation_cleanup_verified=True,
                    ),
                )
            ),
        )
        with self.assertRaisesRegex(VisualQualificationError, "deterministic cleanup"):
            create_visual_qualification_receipt(unsafe_plan, unsafe_acceptance, manifest, **common)

    def test_plan_manifest_and_tamper_mismatch_fail_closed(self) -> None:
        plan, acceptance, manifest, receipt = _receipt_evidence()
        with patch.object(
            visual_qualification_module,
            "_TRUSTED_VISUAL_QUALIFICATION_RECEIPT_FINGERPRINTS",
            frozenset({receipt.receipt_fingerprint}),
        ):
            with self.assertRaisesRegex(VisualQualificationError, "does not match"):
                bind_visual_qualification(
                    replace(plan, plan_version="1.0.1"),
                    acceptance,
                    manifest,
                    receipt,
                    current_time_utc=CURRENT,
                )
            changed_manifest = replace(manifest, model_digest="sha256:" + "e" * 64)
            with self.assertRaisesRegex(VisualQualificationError, "identity mismatch"):
                bind_visual_qualification(
                    plan,
                    acceptance,
                    changed_manifest,
                    receipt,
                    current_time_utc=CURRENT,
                )
            changed_runtime = replace(manifest.runtime, dtype="float16")
            with self.assertRaisesRegex(VisualQualificationError, "runtime does not match"):
                bind_visual_qualification(
                    plan,
                    acceptance,
                    replace(manifest, runtime=changed_runtime),
                    receipt,
                    current_time_utc=CURRENT,
                )
            object.__setattr__(receipt, "host_profile", "tampered.fixture")
            with self.assertRaisesRegex(VisualQualificationError, "tampered"):
                bind_visual_qualification(
                    plan,
                    acceptance,
                    manifest,
                    receipt,
                    current_time_utc=CURRENT,
                )

    def test_binding_cannot_be_constructed_without_the_validated_join(self) -> None:
        plan, acceptance, manifest, receipt = _receipt_evidence()
        with self.assertRaisesRegex(VisualQualificationError, "not trusted"):
            VisualQualificationBinding(plan, acceptance, manifest, receipt, CURRENT)

    def test_binding_and_runtime_manifest_cannot_split(self) -> None:
        plan, acceptance, manifest, receipt = _receipt_evidence()
        with patch.object(
            visual_qualification_module,
            "_TRUSTED_VISUAL_QUALIFICATION_RECEIPT_FINGERPRINTS",
            frozenset({receipt.receipt_fingerprint}),
        ):
            binding = bind_visual_qualification(
                plan, acceptance, manifest, receipt, current_time_utc=CURRENT
            )
            changed_manifest = replace(manifest, model_digest="sha256:" + "f" * 64)
            with self.assertRaisesRegex(
                LocalAdapterCapabilityError, "qualification manifest mismatch"
            ):
                NativeVLMObservationAdapter(
                    _FixtureClip(_output()), changed_manifest, qualification=binding
                )

    def test_unchecked_binding_factory_is_not_an_admission_authority(self) -> None:
        _, _, manifest, receipt = _receipt_evidence()
        with self.assertRaises(AttributeError):
            VisualQualificationBinding._from_validated(  # type: ignore[attr-defined]
                receipt, manifest.fingerprint
            )

    def test_binding_subclass_cannot_replace_admission_validation(self) -> None:
        manifest = _manifest(cancellation=ModelCapabilityState.QUALIFIED)

        class ForgedBinding(VisualQualificationBinding):
            @property
            def manifest_fingerprint(self) -> str:
                return manifest.fingerprint

            def assert_admitted(
                self,
                runtime_manifest: ModelManifest,
                execution_device_kind: str,
                execution_device_index: int | None,
            ) -> None:
                return None

        forged = object.__new__(ForgedBinding)
        with self.assertRaisesRegex(LocalAdapterCapabilityError, "exact concrete binding"):
            NativeVLMObservationAdapter(_FixtureClip(_output()), manifest, qualification=forged)

    def test_binding_class_spoof_cannot_enter_adapter_or_production_executor(self) -> None:
        manifest = _manifest(cancellation=ModelCapabilityState.QUALIFIED)
        proxy = Mock(spec=VisualQualificationBinding)
        self.assertIsInstance(proxy, VisualQualificationBinding)
        proxy.manifest_fingerprint = manifest.fingerprint
        proxy.assert_admitted = Mock(return_value=None)

        with self.assertRaisesRegex(LocalAdapterCapabilityError, "exact concrete binding"):
            NativeVLMObservationAdapter(_FixtureClip(_output()), manifest, qualification=proxy)

        forged_adapter = NativeVLMObservationAdapter.for_contract_test(
            _FixtureClip(_output()), manifest
        )
        forged_adapter._qualification = proxy
        forged_adapter._contract_only = False
        with self.assertRaisesRegex(VisualQualificationError, "exact concrete binding"):
            execute_native_vlm_observation(
                forged_adapter,
                _request(),
                device=LocalDeviceSpec(LocalDeviceKind.CUDA, index=7),
            )

    def test_entire_qualification_evidence_graph_requires_exact_types(self) -> None:
        plan, acceptance, manifest, receipt = _receipt_evidence()

        class PlanValue(VisualBenchmarkPlan):
            pass

        class AcceptanceValue(VisualAcceptanceReport):
            pass

        class ManifestValue(ModelManifest):
            pass

        class ReceiptValue(VisualQualificationReceipt):
            pass

        class CandidateValue(VisualCandidateProfile):
            pass

        class DeviceProfileValue(VisualDeviceProfile):
            pass

        class BenchmarkLimitsValue(VisualBenchmarkLimits):
            pass

        class AcceptanceReceiptValue(AcceptanceCandidateReceipt):
            pass

        class RuntimeValue(ModelRuntimeProfile):
            pass

        class DeviceValue(LocalDeviceSpec):
            pass

        class ResourceBudgetValue(LocalResourceBudget):
            pass

        candidate = plan.candidates[0]
        device_profile = candidate.device_profiles[0]
        runtime = manifest.runtime
        variants = {
            "plan": (
                PlanValue(**_field_values(plan)),
                acceptance,
                manifest,
                receipt,
            ),
            "acceptance": (
                plan,
                AcceptanceValue(**_field_values(acceptance)),
                manifest,
                receipt,
            ),
            "manifest": (
                plan,
                acceptance,
                ManifestValue(**_field_values(manifest)),
                receipt,
            ),
            "receipt": (
                plan,
                acceptance,
                manifest,
                ReceiptValue(**_field_values(receipt)),
            ),
            "candidate": (
                replace(
                    plan,
                    candidates=(
                        CandidateValue(**_field_values(candidate)),
                        *plan.candidates[1:],
                    ),
                ),
                acceptance,
                manifest,
                receipt,
            ),
            "device_profile": (
                replace(
                    plan,
                    candidates=(
                        replace(
                            candidate,
                            device_profiles=(DeviceProfileValue(**_field_values(device_profile)),),
                        ),
                        *plan.candidates[1:],
                    ),
                ),
                acceptance,
                manifest,
                receipt,
            ),
            "benchmark_limits": (
                replace(plan, limits=BenchmarkLimitsValue(**_field_values(plan.limits))),
                acceptance,
                manifest,
                receipt,
            ),
            "acceptance_receipt": (
                plan,
                replace(
                    acceptance,
                    candidate_receipts=(
                        AcceptanceReceiptValue(**_field_values(acceptance.candidate_receipts[0])),
                        *acceptance.candidate_receipts[1:],
                    ),
                ),
                manifest,
                receipt,
            ),
            "runtime": (
                plan,
                acceptance,
                replace(manifest, runtime=RuntimeValue(**_field_values(runtime))),
                receipt,
            ),
            "local_device": (
                plan,
                acceptance,
                replace(
                    manifest,
                    runtime=replace(
                        runtime,
                        device=DeviceValue(**_field_values(runtime.device)),
                    ),
                ),
                receipt,
            ),
            "resource_budget": (
                plan,
                acceptance,
                replace(
                    manifest,
                    runtime=replace(
                        runtime,
                        limits=ResourceBudgetValue(**_field_values(runtime.limits)),
                    ),
                ),
                receipt,
            ),
        }
        with patch.object(
            visual_qualification_module,
            "_TRUSTED_VISUAL_QUALIFICATION_RECEIPT_FINGERPRINTS",
            frozenset({receipt.receipt_fingerprint}),
        ):
            for field_name, evidence in variants.items():
                with self.subTest(field_name=field_name):
                    with self.assertRaisesRegex(
                        VisualQualificationError, "exact concrete evidence"
                    ):
                        bind_visual_qualification(
                            *evidence,
                            current_time_utc=CURRENT,
                        )

    def test_receipt_freshness_cannot_be_overridden_by_nested_value(self) -> None:
        plan, acceptance, manifest, receipt = _receipt_evidence()

        class ReceiptValue(VisualQualificationReceipt):
            def assert_current(self, current_time_utc: str) -> None:
                return None

        stale = ReceiptValue(**_field_values(receipt))
        object.__setattr__(stale, "expires_at_utc", "2026-08-07T01:00:00Z")
        with patch.object(
            visual_qualification_module,
            "_TRUSTED_VISUAL_QUALIFICATION_RECEIPT_FINGERPRINTS",
            frozenset({receipt.receipt_fingerprint}),
        ):
            with self.assertRaisesRegex(VisualQualificationError, "exact concrete evidence"):
                bind_visual_qualification(
                    plan,
                    acceptance,
                    manifest,
                    stale,
                    current_time_utc=CURRENT,
                )

    def test_receipt_issuance_validates_exact_roots_before_fingerprints(self) -> None:
        plan, acceptance, manifest = _qualified_evidence()
        candidate = plan.candidates[0]

        class PlanValue(VisualBenchmarkPlan):
            pass

        class AcceptanceValue(VisualAcceptanceReport):
            @property
            def fingerprint(self) -> str:
                return "sha256:" + "a" * 64

        class ManifestValue(ModelManifest):
            @property
            def fingerprint(self) -> str:
                return "sha256:" + "b" * 64

        variants = {
            "plan": (PlanValue(**_field_values(plan)), acceptance, manifest),
            "acceptance": (
                plan,
                AcceptanceValue(**_field_values(acceptance)),
                manifest,
            ),
            "manifest": (plan, acceptance, ManifestValue(**_field_values(manifest))),
        }
        for field_name, evidence in variants.items():
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(VisualQualificationError, "exact concrete evidence"):
                    create_visual_qualification_receipt(
                        *evidence,
                        candidate_id=candidate.candidate_id,
                        device_profile_id=candidate.device_profiles[0].profile_id,
                        decided_at_utc=DECIDED,
                        expires_at_utc=EXPIRES,
                    )

    def test_receipt_issuance_and_runtime_manifest_malformed_inputs_are_typed(self) -> None:
        plan, acceptance, manifest, receipt = _receipt_evidence()
        candidate = plan.candidates[0]
        malformed_roots = {
            "plan": (object.__new__(VisualBenchmarkPlan), acceptance, manifest),
            "acceptance": (plan, object.__new__(VisualAcceptanceReport), manifest),
            "manifest": (plan, acceptance, object.__new__(ModelManifest)),
        }
        for field_name, evidence in malformed_roots.items():
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(VisualQualificationError, "malformed evidence"):
                    create_visual_qualification_receipt(
                        *evidence,
                        candidate_id=candidate.candidate_id,
                        device_profile_id=candidate.device_profiles[0].profile_id,
                        decided_at_utc=DECIDED,
                        expires_at_utc=EXPIRES,
                    )

        with patch.object(
            visual_qualification_module,
            "_TRUSTED_VISUAL_QUALIFICATION_RECEIPT_FINGERPRINTS",
            frozenset({receipt.receipt_fingerprint}),
        ):
            binding = bind_visual_qualification(
                plan, acceptance, manifest, receipt, current_time_utc=CURRENT
            )
            with self.assertRaisesRegex(LocalAdapterCapabilityError, "malformed qualification"):
                NativeVLMObservationAdapter(
                    _FixtureClip(_output()),
                    object.__new__(ModelManifest),
                    qualification=binding,
                )

    def test_uninitialized_exact_binding_fails_with_typed_adapter_error(self) -> None:
        malformed = object.__new__(VisualQualificationBinding)
        with self.assertRaisesRegex(LocalAdapterCapabilityError, "malformed qualification"):
            NativeVLMObservationAdapter(
                _FixtureClip(_output()),
                _manifest(cancellation=ModelCapabilityState.QUALIFIED),
                qualification=malformed,
            )

    def test_self_signed_receipt_is_not_trusted_for_runtime_admission(self) -> None:
        plan, acceptance, manifest = _qualified_evidence()
        candidate = plan.candidates[0]
        receipt = create_visual_qualification_receipt(
            plan,
            acceptance,
            manifest,
            candidate_id=candidate.candidate_id,
            device_profile_id=candidate.device_profiles[0].profile_id,
            decided_at_utc=DECIDED,
            expires_at_utc=EXPIRES,
        )
        with self.assertRaisesRegex(VisualQualificationError, "trusted"):
            bind_visual_qualification(plan, acceptance, manifest, receipt, current_time_utc=CURRENT)

    def test_execution_device_must_match_qualified_device(self) -> None:
        plan, acceptance, manifest, receipt = _receipt_evidence()
        with patch.object(
            visual_qualification_module,
            "_TRUSTED_VISUAL_QUALIFICATION_RECEIPT_FINGERPRINTS",
            frozenset({receipt.receipt_fingerprint}),
        ):
            binding = bind_visual_qualification(
                plan, acceptance, manifest, receipt, current_time_utc=CURRENT
            )
            for kind in (LocalDeviceKind.CUDA, LocalDeviceKind.MPS, LocalDeviceKind.AUTO):
                with self.subTest(kind=kind.value):
                    adapter = NativeVLMObservationAdapter(
                        _FixtureClip(_output()), manifest, qualification=binding
                    )
                    with self.assertRaisesRegex(VisualQualificationError, "execution device"):
                        execute_native_vlm_observation(
                            adapter, _request(), device=LocalDeviceSpec(kind)
                        )
            adapter = NativeVLMObservationAdapter(
                _FixtureClip(_output()), manifest, qualification=binding
            )
            with self.assertRaisesRegex(VisualQualificationError, "device index"):
                execute_native_vlm_observation(
                    adapter,
                    _request(),
                    device=LocalDeviceSpec(LocalDeviceKind.CPU, index=0),
                )

    def test_receipt_validity_interval_is_bounded(self) -> None:
        plan, acceptance, manifest = _qualified_evidence()
        candidate = plan.candidates[0]
        with self.assertRaisesRegex(VisualQualificationError, "maximum TTL"):
            create_visual_qualification_receipt(
                plan,
                acceptance,
                manifest,
                candidate_id=candidate.candidate_id,
                device_profile_id=candidate.device_profiles[0].profile_id,
                decided_at_utc=DECIDED,
                expires_at_utc="9999-12-31T23:59:59Z",
            )

    def test_contract_only_lane_cannot_enter_runtime_executor(self) -> None:
        adapter = NativeVLMObservationAdapter.for_contract_test(
            _FixtureClip(_output()), _manifest()
        )
        with self.assertRaisesRegex(VLMObservationError, "contract-only"):
            execute_native_vlm_observation(adapter, _request())
        document = execute_native_vlm_contract_test(
            adapter, _request(), device=LocalDeviceSpec(LocalDeviceKind.CPU)
        )
        self.assertTrue(document.complete)
        self.assertTrue(adapter.contract_only)


if __name__ == "__main__":
    unittest.main()
