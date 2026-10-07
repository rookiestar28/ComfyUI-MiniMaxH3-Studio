"""M15-03 thin ComfyUI product-shell node boundary tests."""

from __future__ import annotations

import math
import sys
import unittest
from dataclasses import replace
from hashlib import sha256
from types import ModuleType
from typing import cast
from unittest.mock import patch

from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    dispatch_production_action,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    SidebarProductionSeed,
    claim_sidebar_production_seed,
)
from comfyui_h3_context.core import (
    AcceptedIntentAuthority,
    ContextReport,
    FingerprintDomain,
    GenerationJobSpec,
    GenerationSequencePlan,
    GenerationSequenceState,
    MultiSegmentWorkspace,
    PipelineTransaction,
    PipelineTransactionState,
    ProductionWorkbenchProjection,
    ProductShellProjection,
    RecomputeDisposition,
    RecomputePlan,
    SegmentDeclaration,
    SegmentRecomputeDecision,
    SegmentRelationKind,
    TaskMode,
    build_generation_sequence_plan,
    build_native_h3_wiring,
    canonical_fingerprint,
    create_generation_sequence_state,
    create_workspace,
    derive_segment_manifests,
    plan_recompute,
)
from comfyui_h3_context.nodes import (
    NODE_CLASS_MAPPINGS,
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextProductShellNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
    ProductShellNodeError,
)


def _report() -> ContextReport:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "Preserve the exact declared intent.",
        duration_seconds=5.0,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    return H3ContextValidatorNode().validate(plan, document)[1]


def _fp(label: str) -> str:
    return f"sha256:{sha256(label.encode('ascii')).hexdigest()}"


def _generation_source(
    seed: SidebarProductionSeed,
    *,
    duplicate_seed_segment: bool = False,
) -> tuple[MultiSegmentWorkspace, GenerationSequenceState]:
    segment_id = "segment.product.shell.source"
    declaration = SegmentDeclaration(
        segment_id=segment_id,
        task_mode=seed.task_mode,
        source_id=seed.source_id,
        reference_ids=seed.reference_ids,
        duration=seed.duration,
        relation=SegmentRelationKind.INDEPENDENT,
        predecessor_segment_id=None,
        accepted_intent_fingerprint=seed.accepted_intent_fingerprint,
        semantic_receipt_fingerprint=None,
        profile_fingerprint=seed.profile_fingerprint,
        reference_registry_fingerprint=seed.reference_registry_fingerprint,
        native_binding_fingerprint=seed.native_binding_fingerprint,
        producer_settings_fingerprint=seed.producer_settings_fingerprint,
    )
    declarations = (declaration,) + (
        (replace(declaration, segment_id="segment.product.shell.duplicate"),)
        if duplicate_seed_segment
        else ()
    )
    workspace_id = (
        "workspace.product.shell.ambiguous"
        if duplicate_seed_segment
        else "workspace.product.shell.source"
    )
    authorities = tuple(
        AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
        for item in declarations
    )
    source = create_workspace(
        workspace_id,
        declarations,
        accepted_intent_authorities=authorities,
        selected_segment_ids=tuple(item.segment_id for item in declarations),
    )
    previous_declarations = tuple(
        replace(
            item,
            producer_settings_fingerprint=_fp(f"product.shell.previous.{item.segment_id}"),
        )
        for item in declarations
    )
    previous = create_workspace(
        source.workspace_id,
        previous_declarations,
        accepted_intent_authorities=authorities,
        selected_segment_ids=tuple(item.segment_id for item in declarations),
    )
    manifests = derive_segment_manifests(source)
    plan = build_generation_sequence_plan(
        source,
        manifests,
        plan_recompute(derive_segment_manifests(previous), manifests),
        tuple(
            GenerationJobSpec(
                segment_id=item.segment_id,
                job_id=f"job.{item.segment_id}",
                graph_fingerprint=_fp(f"product.shell.graph.{item.segment_id}"),
                compiled_prompt_fingerprint=_fp(f"product.shell.prompt.{item.segment_id}"),
                model_fingerprint=_fp(f"product.shell.model.{item.segment_id}"),
                runtime_fingerprint=_fp(f"product.shell.runtime.{item.segment_id}"),
                expected_format="video.mp4",
                expected_shape=(124, 512, 512, 3),
                timeout_ms=60_000,
                fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
            )
            for item in declarations
        ),
    )
    return source, create_generation_sequence_state(plan)


class ProductShellNodeTests(unittest.TestCase):
    def test_legacy_positional_correlation_remains_compatible(self) -> None:
        report = _report()
        wiring = build_native_h3_wiring(report)

        emitted = H3ContextProductShellNode().emit(
            report,
            wiring,
            "prompt.legacy",
            "node.legacy",
        )
        ui = cast(dict[str, tuple[object, ...]], emitted["ui"])

        assert ui["correlation"] == (
            {"prompt_id": "prompt.legacy", "execution_node_id": "node.legacy"},
        )
        assert "transaction_transparency" not in ui

    def test_metadata_exposes_standard_string_and_typed_shell(self) -> None:
        node = H3ContextProductShellNode
        self.assertEqual(
            node.NODE_ID,
            "comfyui_h3_context.H3Context.ProductShell",
        )
        self.assertEqual(node.RETURN_TYPES, ("STRING", "H3_PRODUCT_SHELL"))
        self.assertEqual(node.RETURN_NAMES, ("prompt", "product_shell"))
        self.assertTrue(node.OUTPUT_NODE)
        self.assertEqual(
            tuple(node.INPUT_TYPES()["required"]),
            ("report", "native_h3_wiring"),
        )
        self.assertEqual(
            node.INPUT_TYPES()["hidden"],
            {"execution_node_id": "UNIQUE_ID"},
        )
        self.assertEqual(
            tuple(node.INPUT_TYPES()["optional"]),
            (
                "recompute_plan",
                "pipeline_transaction",
                "generation_sequence_state",
                "semantic_proposal_review_authority",
            ),
        )
        self.assertTrue(math.isnan(node.IS_CHANGED()))
        self.assertIs(NODE_CLASS_MAPPINGS[node.NODE_ID], node)

    def test_emission_uses_exact_report_wiring_and_list_valued_ui(self) -> None:
        report = _report()
        wiring = build_native_h3_wiring(report)
        emitted = H3ContextProductShellNode().emit(
            report,
            wiring,
            prompt_id="prompt-1",
            execution_node_id="17",
        )
        self.assertEqual(set(emitted), {"ui", "result"})
        result = cast(tuple[str, ProductShellProjection], emitted["result"])
        ui = cast(dict[str, tuple[object, ...]], emitted["ui"])
        self.assertEqual(result[0], report.prompt_document.text)
        self.assertEqual(result[1].product_scope.value, "MANUAL_ONLY_SCOPED")
        self.assertTrue(all(isinstance(value, tuple) for value in ui.values()))
        self.assertEqual(len(ui["sidebar_workspace"]), 1)
        workspace = cast(dict[str, object], ui["sidebar_workspace"][0])
        self.assertEqual(workspace["schema"], "h3.context.sidebar.workspace.v2")
        self.assertEqual(workspace["report_fingerprint"], result[1].report_fingerprint)
        self.assertEqual(workspace["correlation"], result[1].correlation.to_wire())

    def test_optional_m17_authority_emits_one_correlated_transparency_payload(self) -> None:
        report = _report()
        wiring = build_native_h3_wiring(report)
        plan = RecomputePlan(
            decisions=(
                SegmentRecomputeDecision(
                    "segment.1",
                    RecomputeDisposition.DIRTY_SELF,
                    ("producer_fingerprint_changed",),
                    ("segment.1",),
                ),
            ),
            mandatory_segment_ids=("segment.1",),
            requested_segment_ids=("segment.1",),
            missing_required_segment_ids=(),
            selection_safe=True,
            requires_full_recompute=False,
        )
        transaction = PipelineTransaction(
            transaction_id="transaction.1",
            attempt=1,
            state=PipelineTransactionState.PREPARED,
            workspace_id="workspace.1",
            workspace_revision=2,
            workspace_fingerprint="sha256:" + "a" * 64,
            manifest_fingerprints=("sha256:" + "b" * 64,),
            recompute_plan_fingerprint=canonical_fingerprint(plan.to_public_dict()),
            dirty_segment_ids=("segment.1",),
            graph_fingerprint="sha256:" + "c" * 64,
            compiled_prompt_fingerprint="sha256:" + "d" * 64,
        )
        emitted = H3ContextProductShellNode().emit(
            report,
            wiring,
            recompute_plan=plan,
            pipeline_transaction=transaction,
            prompt_id="prompt-1",
            execution_node_id="17",
        )
        ui = cast(dict[str, tuple[object, ...]], emitted["ui"])
        transparency = cast(dict[str, object], ui["transaction_transparency"][0])
        self.assertEqual(transparency["schema"], "h3.context.transaction_transparency.v1")
        self.assertEqual(
            transparency["correlation"],
            {"prompt_id": "prompt-1", "execution_node_id": "17"},
        )
        with self.assertRaises(ProductShellNodeError):
            H3ContextProductShellNode().emit(
                report,
                wiring,
                recompute_plan=plan,
                prompt_id="prompt-1",
                execution_node_id="17",
            )

    def test_optional_generation_sequence_state_emits_only_correlated_commands(self) -> None:
        report = _report()
        wiring = build_native_h3_wiring(report)
        sequence_plan = GenerationSequencePlan(
            sequence_id="sequence.1",
            workspace_id="workspace.1",
            workspace_revision=1,
            workspace_fingerprint="sha256:" + "a" * 64,
            recompute_plan_fingerprint="sha256:" + "b" * 64,
            manifest_fingerprints=(),
            jobs=(),
            clean_segment_ids=(),
        )
        state = create_generation_sequence_state(sequence_plan)
        emitted = H3ContextProductShellNode().emit(
            report,
            wiring,
            generation_sequence_state=state,
            prompt_id="prompt-1",
            execution_node_id="17",
        )
        ui = cast(dict[str, tuple[object, ...]], emitted["ui"])
        sequence = cast(dict[str, object], ui["generation_sequence"][0])
        self.assertEqual(sequence["schema"], "h3.context.generation_sequence_projection.v1")
        self.assertEqual(
            sequence["correlation"],
            {"prompt_id": "prompt-1", "execution_node_id": "17"},
        )
        self.assertEqual(cast(list[object], sequence["eligible_commands"]), [])

        with self.assertRaises(ProductShellNodeError):
            H3ContextProductShellNode().emit(
                report,
                wiring,
                generation_sequence_state=object(),
                prompt_id="prompt-1",
                execution_node_id="17",
            )

    def test_builder_source_reaches_production_through_public_product_shell_path(self) -> None:
        report = _report()
        wiring = build_native_h3_wiring(report)
        seed_emission = H3ContextProductShellNode().emit(
            report,
            wiring,
            prompt_id="prompt.public.seed",
            execution_node_id="node.public.seed",
        )
        seed_ui = cast(dict[str, tuple[object, ...]], seed_emission["ui"])
        seed_wire = cast(dict[str, object], seed_ui["sidebar_workspace"][0])
        seed = claim_sidebar_production_seed(cast(str, seed_wire["workspace_id"]))
        source, state = _generation_source(seed)

        staged_emission = H3ContextProductShellNode().emit(
            report,
            wiring,
            generation_sequence_state=state,
            prompt_id="prompt.public.staged",
            execution_node_id="node.public.staged",
        )
        staged_ui = cast(dict[str, tuple[object, ...]], staged_emission["ui"])
        staged_wire = cast(dict[str, object], staged_ui["sidebar_workspace"][0])
        context_handle = cast(str, staged_wire["workspace_id"])
        created = dispatch_production_action(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "request.product.shell.public.adopt",
                "action": "create_workspace_from_context",
                "payload": {"context_workspace_handle": context_handle},
            }
        ).projection
        self.assertIsNotNone(created)
        assert isinstance(created, ProductionWorkbenchProjection)
        self.assertEqual(created.workspace_id, source.workspace_id)
        self.assertEqual(created.workspace_revision, source.revision)
        self.assertEqual(created.workspace_fingerprint, source.fingerprint)
        self.assertIsNotNone(created.generation_sequence)
        self.assertIn("submit_generation_job", created.allowed_actions)

        released = dispatch_production_action(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "request.product.shell.public.release",
                "action": "release_workspace",
                "payload": {
                    "workspace_handle": created.workspace_handle,
                    "expected_workspace_revision": created.workspace_revision,
                    "expected_workspace_fingerprint": created.workspace_fingerprint,
                },
            }
        )
        self.assertEqual(released.status, 204)

    def test_ambiguous_stage_is_nonfatal_to_context_and_sequence_output(self) -> None:
        report = _report()
        wiring = build_native_h3_wiring(report)
        seed_emission = H3ContextProductShellNode().emit(
            report,
            wiring,
            prompt_id="prompt.ambiguous.seed",
            execution_node_id="node.ambiguous.seed",
        )
        seed_ui = cast(dict[str, tuple[object, ...]], seed_emission["ui"])
        seed_wire = cast(dict[str, object], seed_ui["sidebar_workspace"][0])
        seed = claim_sidebar_production_seed(cast(str, seed_wire["workspace_id"]))
        source, state = _generation_source(seed, duplicate_seed_segment=True)

        emitted = H3ContextProductShellNode().emit(
            report,
            wiring,
            generation_sequence_state=state,
            prompt_id="prompt.ambiguous.output",
            execution_node_id="node.ambiguous.output",
        )
        ui = cast(dict[str, tuple[object, ...]], emitted["ui"])
        self.assertIn("generation_sequence", ui)
        context_wire = cast(dict[str, object], ui["sidebar_workspace"][0])
        context_handle = cast(str, context_wire["workspace_id"])

        ordinary = dispatch_production_action(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "request.product.shell.ambiguous.ordinary",
                "action": "create_workspace_from_context",
                "payload": {"context_workspace_handle": context_handle},
            }
        ).projection
        self.assertIsNotNone(ordinary)
        assert isinstance(ordinary, ProductionWorkbenchProjection)
        self.assertNotEqual(ordinary.workspace_id, source.workspace_id)
        dispatch_production_action(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "request.product.shell.ambiguous.release",
                "action": "release_workspace",
                "payload": {
                    "workspace_handle": ordinary.workspace_handle,
                    "expected_workspace_revision": ordinary.workspace_revision,
                    "expected_workspace_fingerprint": ordinary.workspace_fingerprint,
                },
            }
        )

    def test_missing_correlation_and_forged_wiring_fail_closed(self) -> None:
        report = _report()
        wiring = build_native_h3_wiring(report)
        with self.assertRaises(ProductShellNodeError):
            H3ContextProductShellNode().emit(report, wiring)
        with self.assertRaises(ProductShellNodeError):
            H3ContextProductShellNode().emit(
                report,
                replace(wiring),
                prompt_id="prompt-1",
                execution_node_id="17",
            )

    def test_hidden_execution_id_must_match_active_host_context(self) -> None:
        report = _report()
        wiring = build_native_h3_wiring(report)
        module_name = "comfyui_h3_context.adapters.comfyui_execution"
        execution_module = ModuleType(module_name)
        execution_module.__dict__["current_execution_correlation"] = lambda: (
            "prompt-1",
            "host-node-17",
        )
        with patch.dict(sys.modules, {module_name: execution_module}):
            with self.assertRaises(ProductShellNodeError) as raised:
                H3ContextProductShellNode().emit(
                    report,
                    wiring,
                    execution_node_id="forged-node-99",
                )
        self.assertEqual(raised.exception.code, "correlation_mismatch")


if __name__ == "__main__":
    unittest.main()
