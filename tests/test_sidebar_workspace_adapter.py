"""M15-04 strict action decoding and bounded adapter-session tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from comfyui_h3_context.adapters.authoring_source_binding import (
    AuthoringSourceBindingReceipt,
    RuntimeVideoCapability,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    MAX_SIDEBAR_ACTION_BYTES,
    SidebarWorkspaceRegistry,
    decode_sidebar_action_json,
    decode_sidebar_transfer_json,
)
from comfyui_h3_context.core import (
    ContextReport,
    ExecutionCorrelation,
    NativeH3Wiring,
    SidebarWorkspaceError,
    SidebarWorkspaceProjection,
    TaskMode,
    build_native_h3_wiring,
    canonical_fingerprint,
)
from comfyui_h3_context.core.contracts import AssetRole, MediaKind
from comfyui_h3_context.core.registry import (
    ReferenceAsset,
    ReferenceRegistry,
    build_reference_registry,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)


class _BindingReceipt(AuthoringSourceBindingReceipt):
    def __init__(self, report: ContextReport, *, duration_milliseconds: int | None = None) -> None:
        super().__init__(
            exact_registry=report.request.reference_registry,
            generation=1,
        )
        self.release_count = 0
        self.duration_milliseconds = duration_milliseconds

    def _capability_for(self, _source_id: str) -> RuntimeVideoCapability:
        return (
            RuntimeVideoCapability.AVAILABLE
            if self.duration_milliseconds is not None
            else RuntimeVideoCapability.UNSUPPORTED
        )

    def _claim_source(self, _source_id: str) -> object:
        raise AssertionError("this lifecycle test must not claim a source")

    def _duration_for(self, _source_id: str) -> int | None:
        return self.duration_milliseconds

    def _release_sources(self) -> None:
        self.release_count += 1


def _authority() -> tuple[ContextReport, NativeH3Wiring, ExecutionCorrelation]:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "A safe operator-owned prompt.",
        duration_seconds=5.0,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report = H3ContextValidatorNode().validate(plan, document)[1]
    return report, build_native_h3_wiring(report), ExecutionCorrelation("prompt-1", "17")


def _workspace(
    value: SidebarWorkspaceProjection | dict[str, object],
) -> SidebarWorkspaceProjection:
    if not isinstance(value, SidebarWorkspaceProjection):
        raise AssertionError("workspace action did not return a projection")
    return value


def _transfer(
    value: SidebarWorkspaceProjection | dict[str, object],
) -> dict[str, object]:
    if type(value) is not dict:
        raise AssertionError("export action did not return a transfer")
    return value


class SidebarWorkspaceAdapterTests(unittest.TestCase):
    def test_metadata_light_video_uses_only_opaque_receipt_duration(self) -> None:
        exact_registry = build_reference_registry(
            (ReferenceAsset("video-1", MediaKind.VIDEO, AssetRole.REFERENCE, 1),)
        )
        report = SimpleNamespace(
            is_successful=True,
            validation=SimpleNamespace(is_valid=True),
            report_id="context-report-1",
            request=SimpleNamespace(
                reference_registry=exact_registry,
                task_mode=TaskMode.T2VA,
            ),
        )
        receipt = _BindingReceipt(report, duration_milliseconds=4_000)  # type: ignore[arg-type]
        entry = SimpleNamespace(report=report, authoring_source_binding=receipt)

        seed = SidebarWorkspaceRegistry._build_authoring_seed(entry)  # type: ignore[arg-type]

        self.assertEqual(len(seed.sources), 1)
        self.assertEqual(seed.sources[0].duration_milliseconds, 4_000)

    def test_strict_decoder_rejects_duplicates_subclasses_and_excess(self) -> None:
        valid = {
            "schema": "h3.context.sidebar.action.v2",
            "workspace_id": "ws_0123456789abcdefghijklmnopqrstuv",
            "expected_revision": 0,
            "expected_report_fingerprint": "sha256:" + "a" * 64,
            "action": "validate",
            "payload": {},
        }
        self.assertEqual(
            decode_sidebar_action_json(json.dumps(valid).encode("utf-8"))["action"],
            "validate",
        )
        hostile = (
            '{"schema":"h3.context.sidebar.action.v2","schema":"x",'
            '"workspace_id":"ws_0123456789abcdefghijklmnopqrstuv",'
            '"expected_revision":0,"expected_report_fingerprint":"sha256:'
            + "a" * 64
            + '","action":"validate","payload":{}}'
        ).encode("utf-8")
        for value in (
            hostile,
            b"not-json",
            b'"scalar"',
            b"{}" + b" " * MAX_SIDEBAR_ACTION_BYTES,
        ):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                decode_sidebar_action_json(value)
        with self.assertRaises(ValueError):
            decode_sidebar_action_json(bytearray(b"{}"))  # type: ignore[arg-type]

        class Spoof(bytes):
            pass

        with self.assertRaises(ValueError):
            decode_sidebar_action_json(Spoof(json.dumps(valid).encode("utf-8")))

    def test_registry_stale_actions_fail_without_partial_mutation(self) -> None:
        report, wiring, correlation = _authority()
        registry = SidebarWorkspaceRegistry(max_entries=2, ttl_seconds=60)
        initial = registry.publish(report, wiring, correlation)
        current = _workspace(
            registry.dispatch(
                {
                    "schema": "h3.context.sidebar.action.v2",
                    "workspace_id": initial.workspace_id,
                    "expected_revision": initial.report_revision,
                    "expected_report_fingerprint": initial.report_fingerprint,
                    "action": "stage_prompt",
                    "payload": {
                        "reason": "Clarify motion",
                        # M24-05: the manual skeleton renders the user's own sentence now.
                        "prompt_text": initial.prompt_text.replace(
                            "A safe operator-owned prompt.",
                            "A safe operator-owned prompt, held in a slow pan.",
                        ),
                    },
                }
            )
        )
        self.assertEqual(current.lifecycle, "stale")
        self.assertEqual(current.validation_status, "not_run")
        self.assertFalse(current.actions.export)

        with self.assertRaises(ValueError):
            registry.dispatch(
                {
                    "schema": "h3.context.sidebar.action.v2",
                    "workspace_id": initial.workspace_id,
                    "expected_revision": initial.report_revision,
                    "expected_report_fingerprint": initial.report_fingerprint,
                    "action": "validate",
                    "payload": {},
                }
            )
        readback = registry.get(initial.workspace_id)
        self.assertEqual(readback.report_revision, current.report_revision)
        self.assertEqual(readback.report_fingerprint, current.report_fingerprint)

    def test_registry_bounds_evict_oldest_and_export_is_explicit(self) -> None:
        registry = SidebarWorkspaceRegistry(max_entries=1, ttl_seconds=60)
        report, wiring, correlation = _authority()
        first = registry.publish(report, wiring, correlation)
        second = registry.publish(
            report,
            wiring,
            ExecutionCorrelation("prompt-2", "18"),
        )
        with self.assertRaises(KeyError):
            registry.get(first.workspace_id)
        exported = _transfer(
            registry.dispatch(
                {
                    "schema": "h3.context.sidebar.action.v2",
                    "workspace_id": second.workspace_id,
                    "expected_revision": second.report_revision,
                    "expected_report_fingerprint": second.report_fingerprint,
                    "action": "export",
                    "payload": {},
                }
            )
        )
        self.assertEqual(exported["schema"], "h3.context.sidebar.transfer.v1")
        self.assertEqual(exported["prompt_text"], second.prompt_text)

        imported = _workspace(
            registry.dispatch(
                {
                    "schema": "h3.context.sidebar.action.v2",
                    "workspace_id": second.workspace_id,
                    "expected_revision": second.report_revision,
                    "expected_report_fingerprint": second.report_fingerprint,
                    "action": "import_prompt",
                    "payload": {"transfer_json": json.dumps(exported)},
                }
            )
        )
        self.assertEqual(imported.lifecycle, "stale")
        self.assertEqual(imported.report_revision, second.report_revision + 1)

        tampered = {**exported, "prompt_text": "A different prompt."}
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            decode_sidebar_transfer_json(json.dumps(tampered).encode("utf-8"))
        self.assertEqual(
            exported["prompt_fingerprint"], canonical_fingerprint(exported["prompt_text"])
        )

    def test_authoring_binding_transfers_once_and_sidebar_eviction_releases_owner(self) -> None:
        report, wiring, correlation = _authority()
        first_receipt = _BindingReceipt(report)
        second_receipt = _BindingReceipt(report)
        third_receipt = _BindingReceipt(report)
        pending = [first_receipt, second_receipt, third_receipt]

        def claim(exact_registry: ReferenceRegistry) -> AuthoringSourceBindingReceipt:
            self.assertIs(exact_registry, report.request.reference_registry)
            return pending.pop(0)

        registry = SidebarWorkspaceRegistry(
            max_entries=1,
            ttl_seconds=60,
            source_binding_claim=claim,
        )
        first = registry.publish(report, wiring, correlation)
        self.assertEqual(registry.claim_authoring_seed(first.workspace_id).sources, ())
        transferred = registry.claim_authoring_workspace(first.workspace_id)
        self.assertIs(transferred.source_binding, first_receipt)
        self.assertIsNone(registry.claim_authoring_workspace(first.workspace_id).source_binding)
        assert transferred.source_binding is not None
        transferred.source_binding.release()
        self.assertEqual(first_receipt.release_count, 1)

        second = registry.publish(report, wiring, correlation)
        self.assertFalse(second_receipt.released)
        third = registry.publish(report, wiring, ExecutionCorrelation("prompt-3", "19"))
        self.assertTrue(second_receipt.released)
        self.assertEqual(second_receipt.release_count, 1)
        with self.assertRaises(KeyError):
            registry.get(second.workspace_id)
        final_claim = registry.claim_authoring_workspace(third.workspace_id)
        assert final_claim.source_binding is not None
        final_claim.source_binding.release()

    def test_import_decoder_preserves_raw_duplicate_detection(self) -> None:
        duplicate = (
            '{"schema":"h3.context.sidebar.transfer.v1","report_id":"r",'
            '"report_revision":0,"report_fingerprint":"sha256:'
            + "a" * 64
            + '","prompt_fingerprint":"sha256:'
            + "b" * 64
            + '","task_mode":"t2va","profile":"base",'
            '"prompt_text":"safe","prompt_text":"forged"}'
        ).encode("utf-8")
        with self.assertRaises(ValueError):
            decode_sidebar_transfer_json(duplicate)

        malformed_identity = {
            "schema": "h3.context.sidebar.transfer.v1",
            "report_id": "not a report identity",
            "report_revision": 0,
            "report_fingerprint": "sha256:" + "g" * 64,
            "prompt_fingerprint": "sha256:" + "b" * 64,
            "task_mode": "invented",
            "profile": "invented",
            "prompt_text": "safe",
        }
        with self.assertRaises(ValueError):
            decode_sidebar_transfer_json(json.dumps(malformed_identity).encode("utf-8"))

    def test_redacted_or_truncated_projection_cannot_export_transfer(self) -> None:
        source, _, correlation = _authority()
        for suffix in ("\napi_key=secret-value", "\n" + "A" * 5_000):
            with self.subTest(suffix_length=len(suffix)):
                report = replace(
                    source,
                    prompt_document=replace(
                        source.prompt_document,
                        text=source.prompt_document.text + suffix,
                    ),
                )
                registry = SidebarWorkspaceRegistry(max_entries=1, ttl_seconds=60)
                projection = registry.publish(
                    report,
                    build_native_h3_wiring(report),
                    correlation,
                )
                self.assertTrue(projection.prompt_text_redacted)
                self.assertEqual(projection.lifecycle, "blocked")
                self.assertFalse(projection.actions.export)
                with self.assertRaises(SidebarWorkspaceError):
                    registry.dispatch(
                        {
                            "schema": "h3.context.sidebar.action.v2",
                            "workspace_id": projection.workspace_id,
                            "expected_revision": projection.report_revision,
                            "expected_report_fingerprint": projection.report_fingerprint,
                            "action": "export",
                            "payload": {},
                        }
                    )


if __name__ == "__main__":
    unittest.main()
