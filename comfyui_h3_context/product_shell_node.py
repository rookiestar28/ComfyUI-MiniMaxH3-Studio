"""Thin ComfyUI boundary for the backend-owned product-shell projection."""

from __future__ import annotations

from .adapters.comfyui_production_workspace import publish_generation_sequence_authority
from .adapters.comfyui_sidebar_workspace import (
    commit_semantic_proposal_review,
    publish_sidebar_workspace,
    rollback_semantic_proposal_review,
    stage_semantic_proposal_review,
)
from .core.context_reporting import ContextReport
from .core.errors import NativeH3AdapterError, ProductShellError, SidebarWorkspaceError
from .core.generation_sequence import (
    GenerationSequenceError,
    GenerationSequenceState,
    build_generation_sequence_projection,
)
from .core.native_h3 import NativeH3Wiring
from .core.pipeline_transaction import PipelineTransaction
from .core.product_shell import PRODUCT_SHELL_NODE_ID, build_product_shell_projection
from .core.recompute_closure import RecomputePlan
from .core.semantic_proposal_producer import (
    SemanticProposalProducerError,
    SemanticProposalReviewAuthority,
    claim_semantic_proposal_review_authority,
)
from .core.semantic_proposal_review import SemanticProposalReviewError
from .core.transaction_transparency import (
    TransactionTransparencyError,
    build_transaction_transparency_projection,
)
from .core.ui_projection import ExecutionCorrelation

PRODUCT_SHELL_DISPLAY_NAME = "H3 Product Shell Boundary"
PRODUCT_SHELL_SOCKET_TYPE = "H3_PRODUCT_SHELL"
REPORT_SOCKET_TYPE = "H3_CONTEXT_REPORT"
NATIVE_H3_WIRING_SOCKET_TYPE = "H3_NATIVE_H3_WIRING"
RECOMPUTE_PLAN_SOCKET_TYPE = "H3_RECOMPUTE_PLAN"
PIPELINE_TRANSACTION_SOCKET_TYPE = "H3_PIPELINE_TRANSACTION"
GENERATION_SEQUENCE_STATE_SOCKET_TYPE = "H3_GENERATION_SEQUENCE_STATE"
SEMANTIC_PROPOSAL_REVIEW_AUTHORITY_SOCKET_TYPE = "H3_SEMANTIC_PROPOSAL_REVIEW_AUTHORITY"


class ProductShellNodeError(ValueError):
    """Raised when the terminal product-shell boundary cannot prove its authorities."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class H3ContextProductShellNode:
    """Expose a standard prompt plus one bounded, content-free product-shell payload."""

    NODE_ID = PRODUCT_SHELL_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = ("STRING", PRODUCT_SHELL_SOCKET_TYPE)
    RETURN_NAMES = ("prompt", "product_shell")
    FUNCTION = "emit"
    CATEGORY = "h3_context/product"
    DESCRIPTION = "Emits the manual-only product projection for the supported sidebar host pair."
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, object]]:
        return {
            "required": {
                "report": (REPORT_SOCKET_TYPE, {}),
                "native_h3_wiring": (NATIVE_H3_WIRING_SOCKET_TYPE, {}),
            },
            "optional": {
                "recompute_plan": (RECOMPUTE_PLAN_SOCKET_TYPE, {}),
                "pipeline_transaction": (PIPELINE_TRANSACTION_SOCKET_TYPE, {}),
                "generation_sequence_state": (GENERATION_SEQUENCE_STATE_SOCKET_TYPE, {}),
                "semantic_proposal_review_authority": (
                    SEMANTIC_PROPOSAL_REVIEW_AUTHORITY_SOCKET_TYPE,
                    {},
                ),
            },
            "hidden": {"execution_node_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(cls, **_: object) -> float:
        # CRITICAL: correlation contains per-prompt host authority and must never reuse cached UI.
        return float("nan")

    def emit(
        self,
        report: object,
        native_h3_wiring: object,
        prompt_id: object | None = None,
        execution_node_id: object | None = None,
        recompute_plan: object | None = None,
        pipeline_transaction: object | None = None,
        generation_sequence_state: object | None = None,
        semantic_proposal_review_authority: object | None = None,
    ) -> dict[str, object]:
        if type(report) is not ContextReport or type(native_h3_wiring) is not NativeH3Wiring:
            raise ProductShellNodeError(
                "invalid_authority", "product shell requires exact report and wiring values"
            )
        host_correlation: tuple[str, str] | None = None
        if prompt_id is None or execution_node_id is None:
            try:
                # IMPORTANT: keep the optional ComfyUI execution dependency behind this lazy seam.
                from .adapters.comfyui_execution import current_execution_correlation

                host_correlation = current_execution_correlation()
            except (ImportError, RuntimeError, TypeError, ValueError):
                host_correlation = None
        if host_correlation is not None:
            host_prompt_id, host_execution_node_id = host_correlation
            if execution_node_id is not None and execution_node_id != host_execution_node_id:
                raise ProductShellNodeError(
                    "correlation_mismatch",
                    "hidden execution-node authority does not match the active host context",
                )
            if prompt_id is None:
                prompt_id = host_prompt_id
            if execution_node_id is None:
                execution_node_id = host_execution_node_id
        if not isinstance(prompt_id, str) or not isinstance(execution_node_id, str):
            raise ProductShellNodeError(
                "missing_correlation", "product shell requires host prompt and execution-node IDs"
            )
        try:
            correlation = ExecutionCorrelation(prompt_id, execution_node_id)
            projection = build_product_shell_projection(
                report,
                native_h3_wiring,
                correlation,
            )
            workspace = publish_sidebar_workspace(
                report,
                native_h3_wiring,
                correlation,
            )
            transparency = None
            sequence_projection = None
            semantic_review_wire = None
            if recompute_plan is not None or pipeline_transaction is not None:
                if (
                    type(recompute_plan) is not RecomputePlan
                    or type(pipeline_transaction) is not PipelineTransaction
                ):
                    raise TransactionTransparencyError("incomplete_transaction_authority")
                transparency = build_transaction_transparency_projection(
                    recompute_plan,
                    pipeline_transaction,
                    correlation,
                )
            if generation_sequence_state is not None:
                if type(generation_sequence_state) is not GenerationSequenceState:
                    raise GenerationSequenceError("generation_sequence_state_type")
                sequence_projection = build_generation_sequence_projection(
                    generation_sequence_state,
                    correlation,
                )
                # CRITICAL: retain only the builder-owned source behind this exact Context handle;
                # never copy workspace authority or commands into either browser wire.
                publish_generation_sequence_authority(
                    sequence_projection,
                    workspace.workspace_id,
                )
            if semantic_proposal_review_authority is not None:
                if type(semantic_proposal_review_authority) is not SemanticProposalReviewAuthority:
                    raise SemanticProposalProducerError("review_authority")
                review_publication = None
                with claim_semantic_proposal_review_authority(
                    semantic_proposal_review_authority
                ) as claim:
                    try:
                        review_publication = stage_semantic_proposal_review(
                            claim.bundle,
                            native_h3_wiring,
                            correlation,
                            workspace,
                        )
                        # IMPORTANT: publish before consuming the one-time predecessor so every
                        # publication failure remains retryable; the opaque publication can still
                        # be rolled back if predecessor commit itself fails.
                        commit_semantic_proposal_review(review_publication)
                        claim.commit()
                        semantic_review_wire = review_publication.handle.to_wire()
                    except Exception:
                        if review_publication is not None:
                            rollback_semantic_proposal_review(review_publication)
                        raise
        except (
            NativeH3AdapterError,
            ProductShellError,
            SidebarWorkspaceError,
            TransactionTransparencyError,
            GenerationSequenceError,
            SemanticProposalProducerError,
            SemanticProposalReviewError,
            TypeError,
            ValueError,
        ) as exc:
            code = exc.code if isinstance(exc, ProductShellError) else "projection_failed"
            raise ProductShellNodeError(code, "product-shell projection failed closed") from exc
        except Exception:
            # CRITICAL: unexpected registry failures must not expose private review content or IDs.
            raise ProductShellNodeError(
                "projection_failed", "product-shell projection failed closed"
            ) from None
        ui = projection.to_ui()
        ui["sidebar_workspace"] = (workspace.to_wire(),)
        if transparency is not None:
            ui["transaction_transparency"] = (transparency.to_wire(),)
        if sequence_projection is not None:
            ui["generation_sequence"] = (sequence_projection.to_wire(),)
        if semantic_review_wire is not None:
            ui["semantic_proposal_review"] = (semantic_review_wire,)
        return {
            "ui": ui,
            "result": (report.prompt_document.text, projection),
        }


__all__ = [
    "H3ContextProductShellNode",
    "PRODUCT_SHELL_DISPLAY_NAME",
    "PRODUCT_SHELL_NODE_ID",
    "PRODUCT_SHELL_SOCKET_TYPE",
    "PIPELINE_TRANSACTION_SOCKET_TYPE",
    "GENERATION_SEQUENCE_STATE_SOCKET_TYPE",
    "RECOMPUTE_PLAN_SOCKET_TYPE",
    "SEMANTIC_PROPOSAL_REVIEW_AUTHORITY_SOCKET_TYPE",
    "ProductShellNodeError",
]
