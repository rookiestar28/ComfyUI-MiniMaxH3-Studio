"""Node adapters for the plan, compile and validate stages.

The middle of the pipeline: a normalized request becomes a plan, a plan becomes a prompt document,
and the result is checked against the profile's declared validation severity.
"""

from __future__ import annotations

from .core.canonical_context_pipeline import (
    CanonicalContextPipelineError,
    build_canonical_context_plan,
    compile_canonical_context_plan,
    validate_canonical_context_document,
)
from .core.context_reporting import (
    ContextPlan,
    ContextReport,
    PromptDocument,
    ValidationResult,
)
from .node_support import (
    INTENT_GRAPH_SOCKET_TYPE,
    PLAN_SOCKET_TYPE,
    PROMPT_DOCUMENT_SOCKET_TYPE,
    PROMPT_STRING_SOCKET_TYPE,
    REFERENCE_SOCKET_TYPE,
    REQUEST_SOCKET_TYPE,
    VALIDATION_SOCKET_TYPE,
    PipelineNodeError,
)
from .product_shell_node import (
    REPORT_SOCKET_TYPE,
)

PLAN_NODE_ID = "comfyui_h3_context.H3Context.Plan"

PLAN_DISPLAY_NAME = "H3 Context Plan"

COMPILER_NODE_ID = "comfyui_h3_context.H3Context.Compiler"

COMPILER_DISPLAY_NAME = "H3 Context Compiler"

VALIDATOR_NODE_ID = "comfyui_h3_context.H3Context.Validator"

VALIDATOR_DISPLAY_NAME = "H3 Context Validator"


class H3ContextPlanNode:
    """Normalize a request and assemble an explicit or visibly limited intent plan."""

    NODE_ID = PLAN_NODE_ID
    __h3_context_node_id__ = PLAN_NODE_ID
    RETURN_TYPES = (PLAN_SOCKET_TYPE, REPORT_SOCKET_TYPE)
    RETURN_NAMES = ("plan", "report")
    FUNCTION = "build_plan"
    CATEGORY = "h3_context/compiler"
    DESCRIPTION = (
        "Normalizes a typed request and assembles an explicit intent graph; omitted semantic "
        "detail remains a visible manual skeleton."
    )
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "request": (
                    REQUEST_SOCKET_TYPE,
                    {"tooltip": "Typed request envelope from H3 Context Request."},
                ),
            },
            "optional": {
                "reference_registry": (
                    REFERENCE_SOCKET_TYPE,
                    {"tooltip": "Optional explicit reference ownership."},
                ),
                "intent_graph": (
                    INTENT_GRAPH_SOCKET_TYPE,
                    {"tooltip": "Optional explicit semantic graph; no free-text inference."},
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(
        cls,
        request: object,
        reference_registry: object | None = None,
        intent_graph: object | None = None,
    ) -> bool | str:
        # ComfyUI validates linked nodes before upstream execution values exist; defer typed
        # request validation to ``build_plan`` once the connected object is resolved.
        if request is None:
            return True
        try:
            cls().build_plan(request, reference_registry, intent_graph)
        except PipelineNodeError as exc:
            return str(exc)
        return True

    def build_plan(
        self,
        request: object,
        reference_registry: object | None = None,
        intent_graph: object | None = None,
    ) -> tuple[ContextPlan, ContextReport]:
        try:
            return build_canonical_context_plan(
                request,
                reference_registry,
                intent_graph,
            )
        except CanonicalContextPipelineError as exc:
            raise PipelineNodeError(exc.diagnostics) from exc


class H3ContextCompilerNode:
    """Delegate deterministic prompt rendering to the selected pure-core profile."""

    NODE_ID = COMPILER_NODE_ID
    __h3_context_node_id__ = COMPILER_NODE_ID
    RETURN_TYPES = (PROMPT_STRING_SOCKET_TYPE, REPORT_SOCKET_TYPE, PROMPT_DOCUMENT_SOCKET_TYPE)
    RETURN_NAMES = ("prompt", "report", "prompt_document")
    FUNCTION = "compile"
    CATEGORY = "h3_context/compiler"
    DESCRIPTION = "Renders the normalized plan with its explicit H3 prompt profile."
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "plan": (
                    PLAN_SOCKET_TYPE,
                    {"tooltip": "Validated typed plan from H3 Context Plan."},
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, plan: object) -> bool | str:
        # A linked plan is unavailable during ComfyUI's pre-execution validation pass.
        if plan is None:
            return True
        try:
            cls().compile(plan)
        except PipelineNodeError as exc:
            return str(exc)
        return True

    def compile(self, plan: object) -> tuple[str, ContextReport, PromptDocument]:
        try:
            return compile_canonical_context_plan(plan)
        except CanonicalContextPipelineError as exc:
            raise PipelineNodeError(exc.diagnostics) from exc


class H3ContextValidatorNode:
    """Audit a plan/document pair without mutating or repairing prompt text."""

    NODE_ID = VALIDATOR_NODE_ID
    __h3_context_node_id__ = VALIDATOR_NODE_ID
    RETURN_TYPES = (VALIDATION_SOCKET_TYPE, REPORT_SOCKET_TYPE)
    RETURN_NAMES = ("validation", "validated_report")
    FUNCTION = "validate"
    CATEGORY = "h3_context/validation"
    DESCRIPTION = "Audits prompt structure and hard constraints without auto-correction."
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "plan": (
                    PLAN_SOCKET_TYPE,
                    {"tooltip": "Typed plan under audit."},
                ),
                "prompt_document": (
                    PROMPT_DOCUMENT_SOCKET_TYPE,
                    {"tooltip": "Rendered or explicitly parsed prompt document."},
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, plan: object, prompt_document: object) -> bool | str:
        # Linked typed outputs are resolved only during execution; validate them there.
        if plan is None or prompt_document is None:
            return True
        try:
            cls().validate(plan, prompt_document)
        except PipelineNodeError as exc:
            return str(exc)
        return True

    def validate(
        self,
        plan: object,
        prompt_document: object,
    ) -> tuple[ValidationResult, ContextReport]:
        try:
            return validate_canonical_context_document(plan, prompt_document)
        except CanonicalContextPipelineError as exc:
            raise PipelineNodeError(exc.diagnostics) from exc
