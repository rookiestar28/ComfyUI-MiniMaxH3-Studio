"""Typed errors shared at optional integration boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .context_reporting import ProviderReceipt


class OptionalDependencyError(ImportError):
    """Raised when an explicitly selected optional integration is unavailable."""


class SecurityPolicyError(ValueError):
    """Raised when an input violates an explicit security or privacy policy."""


class ContractValidationError(ValueError):
    """Raised when a typed H3 contract contains an invalid or unsupported value."""


class BaseAssistantMigrationError(ContractValidationError):
    """Raised when a legacy Base assistant Subgraph cannot be migrated safely."""


class ReferenceAssistantMigrationError(ContractValidationError):
    """Raised when a legacy Reference assistant Subgraph cannot be migrated safely."""


class AuditOverrideError(ContractValidationError):
    """Raised when an explicit manual audit override cannot be applied safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class LocalAdapterError(ContractValidationError):
    """Base error for an explicitly selected local adapter execution."""


class LocalAdapterUnavailableError(LocalAdapterError):
    """Raised when an explicitly selected optional adapter cannot be loaded."""


class LocalAdapterVersionError(LocalAdapterError):
    """Raised when a resolved adapter or result is outside its declared version range."""


class LocalAdapterCapabilityError(LocalAdapterError):
    """Raised when a selected adapter cannot satisfy a typed execution capability request."""


class LocalAdapterBudgetError(LocalAdapterError):
    """Raised when a local adapter request or result exceeds a declared budget."""


class LocalAdapterTimeoutError(LocalAdapterError):
    """Raised when an adapter exceeds its finite wall-time budget."""


class LocalAdapterMemoryError(LocalAdapterError):
    """Raised when an adapter exceeds its injected memory budget."""


class LocalAdapterConcurrencyError(LocalAdapterError):
    """Raised when an adapter's concurrency reservation is exhausted."""


class LocalAdapterCancelledError(LocalAdapterError):
    """Raised when a local adapter operation is cancelled before completion."""


class ImageObservationError(ContractValidationError):
    """Raised when image observation/OCR input or output cannot be validated safely."""


class VideoAnalysisError(ContractValidationError):
    """Raised when bounded video sampling or shot analysis cannot be represented safely."""


class VideoDecodeError(ContractValidationError):
    """Raised when source-PTS video decode output cannot be represented safely."""


class ContactSheetError(ContractValidationError):
    """Raised when a sampled contact sheet cannot be planned, composed, or bounded safely."""


class PerceptionTrackingError(ContractValidationError):
    """Raised when detections, masks, tracks, or re-ID output is unsafe or invalid."""


class TemporalVisualAnalysisError(ContractValidationError):
    """Raised when timestamped action/motion/camera/style evidence is invalid or unsafe."""


class VisualEvidenceFusionError(ContractValidationError):
    """Raised when provenance-preserving visual fusion is invalid or unsafe."""


class VisualAcceptanceError(ContractValidationError):
    """Raised when the visual-perception acceptance gate cannot close safely."""


class AudioAnalysisError(ContractValidationError):
    """Raised when bounded audio analysis cannot be represented safely."""


class AudioPreprocessError(ContractValidationError):
    """Raised when source-clock audio extraction or normalization is unsafe."""


class ASRPerceptionError(ContractValidationError):
    """Raised when source-owned ASR output or benchmark policy is unsafe."""


class SpeakerPerceptionError(ContractValidationError):
    """Raised when source-owned speaker or voice-reference output is unsafe."""


class AudioEventPerceptionError(ContractValidationError):
    """Raised when source-owned audio-event output or bake-off policy is unsafe."""


class AVSyncPerceptionError(ContractValidationError):
    """Raised when source-owned audiovisual synchronization output is unsafe."""


class AudioEvidenceFusionError(ContractValidationError):
    """Raised when provenance-preserving audio evidence fusion is invalid or unsafe."""


class AudioAcceptanceError(ContractValidationError):
    """Raised when the audio-perception acceptance gate cannot close safely."""


class UnifiedEvidenceGraphError(ContractValidationError):
    """Raised when the unified multimodal evidence graph is invalid or unsafe."""


class ReferenceRoleResolutionError(ContractValidationError):
    """Raised when explicit cross-asset role resolution is invalid or unsafe."""


class TemporalEventAlignmentError(ContractValidationError):
    """Raised when cross-modal temporal grounding is invalid or unsafe."""


class DirectiveAuthorityEngineError(ContractValidationError):
    """Raised when the M13 directive authority projection is invalid or unsafe."""


class TaskModeRetentionClassifierError(ContractValidationError):
    """Raised when explicit H3 mode or retention classification is invalid or unsafe."""


class FeasibleAVTimelineError(ContractValidationError):
    """Raised when a bounded audiovisual timeline planner value is unsafe."""


class HierarchicalEvidenceReductionError(ContractValidationError):
    """Raised when a deterministic evidence-reduction value is invalid or unsafe."""


class ConstrainedSemanticPlanningError(ContractValidationError):
    """Raised when a model-assisted semantic proposal cannot be validated safely."""


class CrossReferenceError(ContractValidationError):
    """Raised when an explicit cross-reference graph cannot be represented safely."""


class DirectiveSemanticsError(ContractValidationError):
    """Raised when a Full-Reference directive or precedence result is unsafe or invalid."""


class FullReferenceTimelineError(ContractValidationError):
    """Raised when a deterministic Full-Reference timeline cannot be represented safely."""


class ResourceSchedulingError(ContractValidationError):
    """Raised when a bounded resource execution contract cannot be represented safely."""


class ReliabilityError(ContractValidationError):
    """Raised when progress, cancellation, or recovery state is invalid or unsafe."""


class BasePlanningError(ContractValidationError):
    """Raised when deterministic Base-mode planning cannot be represented safely."""


class SemanticEnrichmentError(ContractValidationError):
    """Raised when constrained semantic enrichment cannot be validated safely."""


class FailureContainmentError(ContractValidationError):
    """Raised when the failure-containment contract cannot be represented safely."""


class OfficialContextIRError(ContractValidationError):
    """Raised when the official H3-Context-IR adapter cannot produce a valid result."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        receipt: ProviderReceipt | None = None,
    ) -> None:
        self.category = category
        self.receipt = receipt
        super().__init__(f"{category}: {message}")


class OfficialOracleGovernanceError(ContractValidationError):
    """Raised when an official-oracle terms, privacy, budget, or abort gate denies admission."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ProgramEnvelopeError(ContractValidationError):
    """Raised when a frozen reconstruction resource envelope denies or expires work."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ConstraintConflictError(ContractValidationError):
    """Raised when same-ID hard constraints cannot be merged deterministically."""


class ConstraintTransformationError(ContractValidationError):
    """Raised when an authorized hard-constraint transformation cannot be applied."""


class ReferenceRegistryError(ContractValidationError):
    """Raised when canonical assets or their ownership metadata are invalid."""


class ReferenceOrderError(ReferenceRegistryError):
    """Raised when references are reordered, gapped, or in an invalid host group order."""


class ReferenceLabelError(ReferenceRegistryError):
    """Raised when a backend label is malformed or mismatched with canonical assets."""


class EvidenceConflictError(ContractValidationError):
    """Raised when same-ID evidence records cannot be merged deterministically."""


class IntentGraphError(ContractValidationError):
    """Raised when an immutable intent graph value cannot be constructed safely."""


class ContextReportError(ContractValidationError):
    """Raised when a plan, prompt document, receipt, or report is invalid."""


class ReportLifecycleError(ContractValidationError):
    """Raised when a report is missing validation or has a stale execution identity."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ContextPreviewError(ContractValidationError):
    """Raised when a bounded, redacted context preview cannot be constructed safely."""


class NativeH3AdapterError(ContractValidationError):
    """Raised when a typed report cannot map safely to the pinned native MiniMax H3 nodes."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class CanonicalizationError(ContractValidationError):
    """Raised when a value cannot enter the bounded canonical projection."""


class ProfileRegistryError(ContractValidationError):
    """Raised when a prompt-profile definition or registry lookup is invalid."""


class PromptRenderingError(ContractValidationError):
    """Raised when a typed plan cannot produce a deterministic prompt document."""


class PromptLintError(ContractValidationError):
    """Raised when prompt linting receives an invalid audit input or result shape."""


class PromptParseError(ContractValidationError):
    """Raised when prompt parsing receives invalid explicit inputs or a non-renderable result."""


class NodeContractError(ContractValidationError):
    """Raised when a declarative ComfyUI node/socket contract is invalid or unsupported."""


class PublicManifestError(ContractValidationError):
    """Raised when the stable public node/API manifest cannot be reconciled safely."""


class GraphBindingError(ContractValidationError):
    """Raised when a visible graph, role anchor, or transparent Subgraph is unsafe."""


class ContractV2Error(ContractValidationError):
    """Raised when a portable M10 contract-v2 value is invalid or unsafe."""


class MediaAdmissionError(ContractValidationError):
    """Raised when media admission, probing, timestamps, or preprocessing is unsafe."""


class MediaProcessError(ContractValidationError):
    """Raised when an injected media subprocess invocation violates its bounded policy."""


class ModelManifestError(ContractValidationError):
    """Raised when a model-family manifest or qualification observation is unsafe."""


class ModelOutputError(ContractValidationError):
    """Raised when model output is incomplete, untyped, oversized, or invalid."""


class ModelTransportError(ContractValidationError):
    """Raised when the explicitly selected native model transport violates its policy."""


class VisualBenchmarkError(ContractValidationError):
    """Raised when a frozen M11 visual benchmark or adapter profile is unsafe or incomplete."""


class VisualQualificationError(ContractValidationError):
    """Raised when visual runtime qualification is absent, stale, or inconsistent."""


class VLMObservationError(ContractValidationError):
    """Raised when a strict source/region VLM observation cannot be represented safely."""


class OCRObservationError(ContractValidationError):
    """Raised when strict exact-text OCR output cannot be represented safely."""


class ExecutionCoordinatorError(ContractValidationError):
    """Raised when coordinator admission, ownership, or artifact policy is unsafe."""


class ExecutionResourceError(ExecutionCoordinatorError):
    """Raised when a finite coordinator resource capacity cannot admit a request."""


class ExecutionArtifactError(ExecutionCoordinatorError):
    """Raised when a content-bearing artifact lacks explicit safe scope or identity."""


class CapabilityManifestError(ContractValidationError):
    """Raised when capability maturity or binding-manifest reconciliation fails."""


class HostCanaryError(ContractValidationError):
    """Raised when a bounded public host-seam canary report is invalid."""


class CompatibilityError(ContractValidationError):
    """Raised when a compatibility profile or observation is invalid or unsafe."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ProductShellError(ContractValidationError):
    """Raised when the bounded product-shell projection is stale, forged, or unsafe."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class SidebarWorkspaceError(ContractValidationError):
    """Raised when a sidebar revision, projection, or action fails closed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class RegistrationConflictError(ValueError):
    """Raised when a project node ID or display name collides with a foreign registration."""


class RegistrationProbeError(RuntimeError):
    """Raised when the M0-07 registration-only placeholder is executed before M3."""
