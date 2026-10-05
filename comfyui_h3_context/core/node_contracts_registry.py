"""Which node contracts this package declares.

One function builds the whole registry, and it is deliberately left whole. It is data written as
code -- a socket list per node, in one place, in a fixed order -- and splitting it into composed
builders would be a rewrite rather than a move, with no reader served by the seam.

Its output is not unverified for being large: `scripts/contract_inventory.py` walks this registry
and folds it into `contract_inventory_v1.json`, whose fingerprint the Full Gate checks. A change to
any socket here moves that fingerprint.
"""

from __future__ import annotations

from .constraints import DialogueDelivery
from .dialogue_language import OFFICIAL_STABLE_DIALOGUE_LANGUAGES
from .node_contracts_types import (
    HostApiFamily,
    HostVersion,
    NodeContract,
    NodeContractRegistry,
    NodeSocket,
    NodeSocketType,
    ScalarDefault,
)


def _socket(
    name: str,
    socket_type: NodeSocketType,
    *,
    required: bool,
    default: ScalarDefault = None,
    max_items: int = 1,
    choices: tuple[str, ...] = (),
    description: str,
) -> NodeSocket:
    return NodeSocket(
        name,
        socket_type,
        required,
        default,
        1 if required else 0,
        max_items,
        choices,
        description,
    )


def default_node_contract_registry() -> NodeContractRegistry:
    """Return the reviewed declarative M3 node/socket contract set."""

    host_api = HostApiFamily.V1
    host_version = HostVersion(0, 30, 0)
    return NodeContractRegistry(
        (
            NodeContract(
                "comfyui_h3_context.H3Context.Request",
                "H3 Context Request",
                "h3_context/contracts",
                (
                    _socket(
                        "task_mode",
                        NodeSocketType.STRING,
                        required=True,
                        default="t2va",
                        choices=("t2va", "i2va", "fl2va", "l2va", "ref2va"),
                        description="explicit H3 task mode",
                    ),
                    _socket(
                        "user_intent",
                        NodeSocketType.STRING,
                        required=True,
                        description="caller-declared intent",
                    ),
                    _socket(
                        "duration_seconds",
                        NodeSocketType.FLOAT,
                        required=False,
                        description="optional requested duration; the frame count is derived",
                    ),
                    _socket(
                        "hard_constraints",
                        NodeSocketType.H3_HARD_CONSTRAINTS,
                        required=False,
                        description="explicit immutable constraints",
                    ),
                ),
                (
                    _socket(
                        "request",
                        NodeSocketType.H3_CONTEXT_REQUEST,
                        required=True,
                        description="typed canonical request envelope",
                    ),
                ),
                host_api,
                host_version,
                "M3-02 owns execution; socket names/defaults are migration-sensitive.",
                "M3-02",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.ReferenceRegistry",
                "H3 Reference Registry",
                "h3_context/contracts",
                (
                    _socket(
                        "first_frame",
                        NodeSocketType.IMAGE,
                        required=False,
                        max_items=1,
                        description="explicit first-frame image anchor",
                    ),
                    _socket(
                        "last_frame",
                        NodeSocketType.IMAGE,
                        required=False,
                        max_items=1,
                        description="explicit last-frame image anchor",
                    ),
                    _socket(
                        "images",
                        NodeSocketType.IMAGE,
                        required=False,
                        max_items=9,
                        description="ordered image references",
                    ),
                    _socket(
                        "videos",
                        NodeSocketType.VIDEO,
                        required=False,
                        max_items=3,
                        description="ordered video references",
                    ),
                    _socket(
                        "paired_audios",
                        NodeSocketType.AUDIO,
                        required=False,
                        max_items=3,
                        description="explicit positional paired video soundtracks",
                    ),
                    _socket(
                        "audios",
                        NodeSocketType.AUDIO,
                        required=False,
                        max_items=3,
                        description="ordered audio references",
                    ),
                ),
                (
                    _socket(
                        "reference_registry",
                        NodeSocketType.H3_REFERENCE_REGISTRY,
                        required=True,
                        description="owned ordered references",
                    ),
                ),
                host_api,
                host_version,
                "M3-03 owns connection-order mapping; changing cardinality requires migration.",
                "M3-03",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.Plan",
                "H3 Context Plan",
                "h3_context/compiler",
                (
                    _socket(
                        "request",
                        NodeSocketType.H3_CONTEXT_REQUEST,
                        required=True,
                        description="canonical request envelope before plan normalization",
                    ),
                    _socket(
                        "reference_registry",
                        NodeSocketType.H3_REFERENCE_REGISTRY,
                        required=False,
                        description="explicit reference ownership",
                    ),
                    _socket(
                        "intent_graph",
                        NodeSocketType.H3_INTENT_GRAPH,
                        required=False,
                        description="explicit immutable intent graph",
                    ),
                ),
                (
                    _socket(
                        "plan",
                        NodeSocketType.H3_CONTEXT_PLAN,
                        required=True,
                        description="validated intent plan",
                    ),
                    _socket(
                        "report",
                        NodeSocketType.H3_CONTEXT_REPORT,
                        required=True,
                        description="inspectable planning report",
                    ),
                ),
                host_api,
                host_version,
                (
                    "M3-04 owns plan construction; optional intent_graph is additive; no "
                    "prompt/provider logic belongs in this node contract."
                ),
                "M3-04",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.Compiler",
                "H3 Context Compiler",
                "h3_context/compiler",
                (
                    _socket(
                        "plan",
                        NodeSocketType.H3_CONTEXT_PLAN,
                        required=True,
                        description="validated intent plan",
                    ),
                ),
                (
                    _socket(
                        "prompt",
                        NodeSocketType.H3_PROMPT_STRING,
                        required=True,
                        description="final native-H3 prompt string",
                    ),
                    _socket(
                        "report",
                        NodeSocketType.H3_CONTEXT_REPORT,
                        required=True,
                        description="render and provenance report",
                    ),
                    _socket(
                        "prompt_document",
                        NodeSocketType.H3_PROMPT_DOCUMENT,
                        required=True,
                        description="rendered prompt document for validation",
                    ),
                ),
                host_api,
                host_version,
                (
                    "M3-04 owns deterministic rendering; provider execution is a "
                    "later explicit boundary; prompt_document is an appended output."
                ),
                "M3-04",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.FullReference",
                "H3 Full-Reference Plan",
                "h3_context/compiler",
                (
                    _socket(
                        "timeline",
                        NodeSocketType.H3_FULL_REFERENCE_TIMELINE,
                        required=True,
                        description="validated M6 Full-Reference timeline result",
                    ),
                ),
                (
                    _socket(
                        "plan",
                        NodeSocketType.H3_CONTEXT_PLAN,
                        required=True,
                        description="canonical ContextPlan handed to the shared compiler",
                    ),
                    _socket(
                        "report",
                        NodeSocketType.H3_CONTEXT_REPORT,
                        required=True,
                        description="inspectable draft report with timeline limitations",
                    ),
                ),
                host_api,
                host_version,
                (
                    "M6-07 only adopts a validated M6 timeline; M2 compiler/validator and M3 "
                    "native adapter remain the sole downstream behavior."
                ),
                "M6-07",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.Validator",
                "H3 Context Validator",
                "h3_context/validation",
                (
                    _socket(
                        "plan",
                        NodeSocketType.H3_CONTEXT_PLAN,
                        required=True,
                        description="typed plan under audit",
                    ),
                    _socket(
                        "prompt_document",
                        NodeSocketType.H3_PROMPT_DOCUMENT,
                        required=True,
                        description="rendered or parsed prompt",
                    ),
                ),
                (
                    _socket(
                        "validation",
                        NodeSocketType.H3_VALIDATION_RESULT,
                        required=True,
                        description="structural validation result",
                    ),
                    _socket(
                        "validated_report",
                        NodeSocketType.H3_CONTEXT_REPORT,
                        required=True,
                        description="canonical report with terminal validation state",
                    ),
                ),
                host_api,
                host_version,
                (
                    "M10-05 owns the additive canonical validated_report output; failures remain "
                    "visible and are never auto-corrected."
                ),
                "M10-05",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.Preview",
                "H3 Context Preview",
                "h3_context/audit",
                (
                    _socket(
                        "report",
                        NodeSocketType.H3_CONTEXT_REPORT,
                        required=True,
                        description="inspectable context report",
                    ),
                ),
                (
                    _socket(
                        "prompt",
                        NodeSocketType.H3_PROMPT_STRING,
                        required=True,
                        description="preview prompt string",
                    ),
                    _socket(
                        "preview",
                        NodeSocketType.H3_CONTEXT_PREVIEW,
                        required=True,
                        description="bounded redacted context preview",
                    ),
                ),
                host_api,
                host_version,
                (
                    "M3-05 owns presentation only; preview must not add hidden prompt behavior; "
                    "the structured preview output is additive."
                ),
                "M3-05",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.AuditOverride",
                "H3 Context Audit Override",
                "h3_context/audit",
                (
                    _socket(
                        "report",
                        NodeSocketType.H3_CONTEXT_REPORT,
                        required=True,
                        description="source report under explicit manual audit",
                    ),
                    _socket(
                        "base_report_fingerprint",
                        NodeSocketType.STRING,
                        required=True,
                        description="exact source report fingerprint",
                    ),
                    _socket(
                        "revision",
                        NodeSocketType.INT,
                        required=True,
                        default=1,
                        description="positive manual edit revision",
                    ),
                    _socket(
                        "reason",
                        NodeSocketType.STRING,
                        required=True,
                        description="bounded operator reason for the edit",
                    ),
                    _socket(
                        "prompt_text",
                        NodeSocketType.STRING,
                        required=True,
                        description="exact caller-authored prompt text",
                    ),
                ),
                (
                    _socket(
                        "edited_prompt",
                        NodeSocketType.H3_PROMPT_STRING,
                        required=True,
                        description="exact revalidated edited prompt",
                    ),
                    _socket(
                        "updated_report",
                        NodeSocketType.H3_CONTEXT_REPORT,
                        required=True,
                        description="new report with override validation",
                    ),
                    _socket(
                        "override",
                        NodeSocketType.H3_AUDIT_OVERRIDE,
                        required=True,
                        description="versioned override metadata and fingerprints",
                    ),
                    _socket(
                        "prompt_document",
                        NodeSocketType.H3_PROMPT_DOCUMENT,
                        required=True,
                        description="edited document for independent downstream validation",
                    ),
                ),
                host_api,
                host_version,
                (
                    "M7-03 requires an exact base fingerprint and revision; edits are revalidated "
                    "without repair or provider execution."
                ),
                "M7-03",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.ProviderTransparency",
                "H3 Provider Transparency",
                "h3_context/providers",
                (
                    _socket(
                        "provider",
                        NodeSocketType.STRING,
                        required=True,
                        default="manual",
                        choices=("manual", "local", "remote_custom", "official_minimax"),
                        description="explicit provider identity",
                    ),
                    _socket(
                        "privacy_mode",
                        NodeSocketType.STRING,
                        required=True,
                        default="local_only",
                        choices=("local_only", "explicit_remote"),
                        description="explicit local or remote privacy decision",
                    ),
                    _socket(
                        "offline",
                        NodeSocketType.BOOLEAN,
                        required=True,
                        default=True,
                        description="offline execution policy",
                    ),
                    _socket(
                        "network_allowed",
                        NodeSocketType.BOOLEAN,
                        required=True,
                        default=False,
                        description="explicit network permission",
                    ),
                    _socket(
                        "upload_consent",
                        NodeSocketType.BOOLEAN,
                        required=True,
                        default=False,
                        description="explicit remote media-upload consent",
                    ),
                    _socket(
                        "credential_reference",
                        NodeSocketType.STRING,
                        required=True,
                        default="none",
                        description="opaque runtime credential reference",
                    ),
                    _socket(
                        "fallback_provider",
                        NodeSocketType.STRING,
                        required=True,
                        default="none",
                        choices=("none", "manual", "local", "remote_custom", "official_minimax"),
                        description="caller-selected fallback metadata only",
                    ),
                    _socket(
                        "local_backend",
                        NodeSocketType.STRING,
                        required=False,
                        default="none",
                        choices=("none", "comfyui_native", "ollama"),
                        description="explicit local backend; local defaults to comfyui_native",
                    ),
                ),
                (
                    _socket(
                        "transparency",
                        NodeSocketType.H3_PROVIDER_TRANSPARENCY,
                        required=True,
                        description="bounded pre-execution provider disclosure",
                    ),
                    _socket(
                        "consent_notice",
                        NodeSocketType.H3_PROVIDER_CONSENT,
                        required=True,
                        description="redacted consent notice",
                    ),
                    _socket(
                        "disclosure",
                        NodeSocketType.STRING,
                        required=True,
                        description="bounded human-readable disclosure",
                    ),
                    _socket(
                        "provider_setup",
                        NodeSocketType.H3_PROVIDER_SETUP,
                        required=True,
                        description="revision-bound operational provider setup",
                    ),
                ),
                host_api,
                host_version,
                (
                    "M7-04 displays provider/privacy implications before execution; "
                    "policy failures "
                    "remain visible and no provider or fallback is selected automatically."
                ),
                "M7-04",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.Reliability",
                "H3 Reliability Status",
                "h3_context/runtime",
                (
                    _socket(
                        "operation_id",
                        NodeSocketType.STRING,
                        required=True,
                        default="h3.context",
                        description="bounded operation identity",
                    ),
                    _socket(
                        "stage",
                        NodeSocketType.STRING,
                        required=True,
                        default="planning",
                        choices=(
                            "extraction",
                            "provider",
                            "planning",
                            "rendering",
                            "validation",
                            "host",
                        ),
                        description="explicit long-running stage",
                    ),
                    _socket(
                        "completed_units",
                        NodeSocketType.INT,
                        required=True,
                        default=0,
                        description="completed bounded work units",
                    ),
                    _socket(
                        "total_units",
                        NodeSocketType.INT,
                        required=True,
                        default=1,
                        description="total bounded work units",
                    ),
                    _socket(
                        "run_state",
                        NodeSocketType.STRING,
                        required=True,
                        default="running",
                        choices=(
                            "pending",
                            "running",
                            "completed",
                            "cancel_requested",
                            "cancelled",
                            "retryable_failure",
                            "failed",
                            "stale",
                            "recovered",
                        ),
                        description="explicit terminal or active run state",
                    ),
                    _socket(
                        "cancel_requested",
                        NodeSocketType.BOOLEAN,
                        required=True,
                        default=False,
                        description="caller cancellation request",
                    ),
                    _socket(
                        "attempt",
                        NodeSocketType.INT,
                        required=True,
                        default=1,
                        description="bounded current attempt",
                    ),
                    _socket(
                        "max_attempts",
                        NodeSocketType.INT,
                        required=True,
                        default=2,
                        description="bounded retry attempt limit",
                    ),
                    _socket(
                        "checkpoint_status",
                        NodeSocketType.STRING,
                        required=True,
                        default="incompatible",
                        choices=("none", "compatible", "stale", "incompatible"),
                        description="caller-provided checkpoint compatibility result",
                    ),
                    _socket(
                        "resume_requested",
                        NodeSocketType.BOOLEAN,
                        required=True,
                        default=False,
                        description="explicit caller resume request",
                    ),
                ),
                (
                    _socket(
                        "status",
                        NodeSocketType.H3_EXECUTION_STATUS,
                        required=True,
                        description="bounded execution status projection",
                    ),
                    _socket(
                        "progress",
                        NodeSocketType.H3_PROGRESS_EVENT,
                        required=True,
                        description="monotonic progress observation",
                    ),
                    _socket(
                        "recovery",
                        NodeSocketType.H3_RECOVERY_DECISION,
                        required=True,
                        description="caller-visible retry/resume decision",
                    ),
                    _socket(
                        "disclosure",
                        NodeSocketType.STRING,
                        required=True,
                        description="bounded runtime reliability disclosure",
                    ),
                ),
                host_api,
                host_version,
                (
                    "M7-05 exposes bounded progress and explicit recovery state; it never starts, "
                    "retries, resumes, or cancels a worker implicitly."
                ),
                "M7-05",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.NativeH3Adapter",
                "H3 Native MiniMax H3 Adapter",
                "h3_context/native",
                (
                    _socket(
                        "report",
                        NodeSocketType.H3_CONTEXT_REPORT,
                        required=True,
                        description="rendered context report for native H3 wiring",
                    ),
                ),
                (
                    _socket(
                        "prompt",
                        NodeSocketType.H3_PROMPT_STRING,
                        required=True,
                        description="exact final prompt passed to native H3",
                    ),
                    _socket(
                        "native_h3_wiring",
                        NodeSocketType.H3_NATIVE_H3_WIRING,
                        required=True,
                        description="pinned native H3 node and direct-media wiring manifest",
                    ),
                ),
                host_api,
                host_version,
                (
                    "M3-06 adds a declarative native-H3 manifest; prompt output remains exact and "
                    "original media stays directly connected to native conditioning."
                ),
                "M3-06",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.ProductShell",
                "H3 Product Shell Boundary",
                "h3_context/product",
                (
                    _socket(
                        "report",
                        NodeSocketType.H3_CONTEXT_REPORT,
                        required=True,
                        description="exact validated report",
                    ),
                    _socket(
                        "native_h3_wiring",
                        NodeSocketType.H3_NATIVE_H3_WIRING,
                        required=True,
                        description="runtime-issued native wiring",
                    ),
                    _socket(
                        "recompute_plan",
                        NodeSocketType.H3_RECOMPUTE_PLAN,
                        required=False,
                        description="optional canonical recompute authority",
                    ),
                    _socket(
                        "pipeline_transaction",
                        NodeSocketType.H3_PIPELINE_TRANSACTION,
                        required=False,
                        description="optional single-transaction authority",
                    ),
                    _socket(
                        "generation_sequence_state",
                        NodeSocketType.H3_GENERATION_SEQUENCE_STATE,
                        required=False,
                        description="optional backend-owned generation sequence authority",
                    ),
                    _socket(
                        "semantic_proposal_review_authority",
                        NodeSocketType.H3_SEMANTIC_PROPOSAL_REVIEW_AUTHORITY,
                        required=False,
                        description="optional exact M17-16 process-local review authority",
                    ),
                ),
                (
                    _socket(
                        "prompt",
                        NodeSocketType.STRING,
                        required=True,
                        description="ordinary native-compatible prompt string",
                    ),
                    _socket(
                        "product_shell",
                        NodeSocketType.H3_PRODUCT_SHELL,
                        required=True,
                        description="bounded backend-owned product-shell projection",
                    ),
                ),
                host_api,
                host_version,
                "M17-06 additively consumes an exact M17-16 review authority while preserving "
                "transaction transparency, existing optional inputs and standard shell outputs.",
                "M17-06",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.SemanticProposalProducer",
                "H3 Semantic Proposal Producer",
                "h3_context/semantic",
                (
                    _socket(
                        "report",
                        NodeSocketType.H3_CONTEXT_REPORT,
                        required=True,
                        description="exact validated report",
                    ),
                    _socket(
                        "wiring",
                        NodeSocketType.H3_NATIVE_H3_WIRING,
                        required=True,
                        description="runtime-issued native wiring",
                    ),
                    _socket(
                        "provider_setup",
                        NodeSocketType.H3_PROVIDER_SETUP,
                        required=True,
                        description="exact local provider setup",
                    ),
                    _socket(
                        "ollama_profile",
                        NodeSocketType.STRING,
                        required=True,
                        default="ollama.qwen3_8.27b_bf16.local",
                        choices=("ollama.qwen3_8.27b_bf16.local",),
                        description="reviewed package-owned Ollama profile ID",
                    ),
                ),
                (
                    _socket(
                        "review_authority",
                        NodeSocketType.H3_SEMANTIC_PROPOSAL_REVIEW_AUTHORITY,
                        required=True,
                        description="content-free process-local semantic review authority",
                    ),
                ),
                host_api,
                host_version,
                "M17-16 adds one independently queueable local semantic proposal producer; "
                "proposal content remains process-local and M17-06 owns review UI/actions.",
                "M17-16",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.OfficialContextIR",
                "H3 Official Context-IR",
                "h3_context/providers",
                (
                    _socket(
                        "request",
                        NodeSocketType.H3_CONTEXT_REQUEST,
                        required=True,
                        description="typed request for the explicitly selected provider",
                    ),
                    _socket(
                        "ratio",
                        NodeSocketType.STRING,
                        required=True,
                        default="adaptive",
                        choices=("adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"),
                        description="explicit official aspect-ratio setting",
                    ),
                    _socket(
                        "upload_consent",
                        NodeSocketType.BOOLEAN,
                        required=True,
                        default=False,
                        description="explicit consent for media to leave this host",
                    ),
                    _socket(
                        "network_allowed",
                        NodeSocketType.BOOLEAN,
                        required=True,
                        default=False,
                        description="explicit provider network permission",
                    ),
                    _socket(
                        "credential_reference",
                        NodeSocketType.STRING,
                        required=True,
                        default="env.official_minimax",
                        description="opaque runtime credential reference",
                    ),
                    _socket(
                        "reference_registry",
                        NodeSocketType.H3_REFERENCE_REGISTRY,
                        required=False,
                        description="optional canonical registry to reconcile",
                    ),
                    _socket(
                        "media",
                        NodeSocketType.H3_CONTEXT_IR_MEDIA,
                        required=False,
                        max_items=12,
                        description="already-admitted typed provider media values",
                    ),
                ),
                (
                    _socket(
                        "prompt",
                        NodeSocketType.H3_PROMPT_STRING,
                        required=True,
                        description="provider-produced prompt string",
                    ),
                    _socket(
                        "receipt",
                        NodeSocketType.H3_PROVIDER_RECEIPT,
                        required=True,
                        description="redacted provider lifecycle receipt",
                    ),
                    _socket(
                        "consent_notice",
                        NodeSocketType.H3_PROVIDER_CONSENT,
                        required=True,
                        description="pre-execution privacy and consent disclosure",
                    ),
                ),
                host_api,
                host_version,
                (
                    "M9-05 remains injected-only and governance-gated; no transport, credential, "
                    "or media upload is implied by this declarative surface."
                ),
                "M9-05",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.MediaAdmissionProducer",
                "H3 Media Admission Producer",
                "h3_context/perception",
                (
                    _socket(
                        "media_kind",
                        NodeSocketType.STRING,
                        required=True,
                        default="image",
                        choices=("image", "video", "audio"),
                        description="explicit host media kind",
                    ),
                    _socket(
                        "asset_id",
                        NodeSocketType.STRING,
                        required=True,
                        default="asset-1",
                        description="stable public asset identifier",
                    ),
                    _socket(
                        "declared_source_fingerprint",
                        NodeSocketType.STRING,
                        required=True,
                        description="unverified caller-declared SHA-256 identity",
                    ),
                    _socket(
                        "width_pixels",
                        NodeSocketType.INT,
                        required=True,
                        default=64,
                        description="declared width; zero means not applicable",
                    ),
                    _socket(
                        "height_pixels",
                        NodeSocketType.INT,
                        required=True,
                        default=64,
                        description="declared height; zero means not applicable",
                    ),
                    _socket(
                        "duration_seconds",
                        NodeSocketType.FLOAT,
                        required=True,
                        default=0.0,
                        description="declared source-relative duration",
                    ),
                    _socket(
                        "sample_rate_hz",
                        NodeSocketType.INT,
                        required=True,
                        default=0,
                        description="declared sample rate; zero means not applicable",
                    ),
                    _socket(
                        "channel_count",
                        NodeSocketType.INT,
                        required=True,
                        default=0,
                        description="declared channel count; zero means not applicable",
                    ),
                    _socket(
                        "reference_role",
                        NodeSocketType.STRING,
                        required=True,
                        default="reference",
                        choices=("input", "reference", "first_frame", "last_frame", "paired_audio"),
                        description="explicit reference ownership role",
                    ),
                    _socket(
                        "reference_order",
                        NodeSocketType.INT,
                        required=True,
                        default=0,
                        description="stable reference ordering index",
                    ),
                    _socket(
                        "image",
                        NodeSocketType.IMAGE,
                        required=False,
                        description="host-owned image value",
                    ),
                    _socket(
                        "video",
                        NodeSocketType.VIDEO,
                        required=False,
                        description="host-owned video value",
                    ),
                    _socket(
                        "audio",
                        NodeSocketType.AUDIO,
                        required=False,
                        description="host-owned audio value",
                    ),
                ),
                (
                    _socket(
                        "media",
                        NodeSocketType.H3_MEDIA_PRODUCER_RESULT,
                        required=True,
                        description="locator-free admitted host media envelope",
                    ),
                ),
                host_api,
                host_version,
                (
                    "M15-01 admits exactly one matching host-owned value; runtime payloads never "
                    "enter portable wires."
                ),
                "M15-01",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.VisualPerceptionProducer",
                "H3 Visual Perception Producer",
                "h3_context/perception",
                (
                    _socket(
                        "media",
                        NodeSocketType.H3_MEDIA_PRODUCER_RESULT,
                        required=True,
                        description="complete admitted image/video media",
                    ),
                    _socket(
                        "route",
                        NodeSocketType.STRING,
                        required=True,
                        default="comfyui_native",
                        choices=("comfyui_native", "ollama", "specialist"),
                        description="explicit visual route without fallback",
                    ),
                    _socket(
                        "profile_id",
                        NodeSocketType.STRING,
                        required=True,
                        default="unqualified",
                        description="explicit qualified profile identifier",
                    ),
                    _socket(
                        "device",
                        NodeSocketType.STRING,
                        required=True,
                        default="auto",
                        choices=("auto", "cpu", "cuda", "mps"),
                        description="explicit execution device selection",
                    ),
                    _socket(
                        "cancel_requested",
                        NodeSocketType.BOOLEAN,
                        required=True,
                        default=False,
                        description="explicit pre-execution cancellation control",
                    ),
                    _socket(
                        "local_service_consent",
                        NodeSocketType.BOOLEAN,
                        required=False,
                        default=False,
                        description="explicit consent for transfer to a local perception service",
                    ),
                    _socket(
                        "request",
                        NodeSocketType.H3_CONTEXT_REQUEST,
                        required=False,
                        description=(
                            "optional reference request limiting visual sampling "
                            "to conditioned frames"
                        ),
                    ),
                ),
                (
                    _socket(
                        "result",
                        NodeSocketType.H3_VISUAL_PRODUCER_RESULT,
                        required=True,
                        description="typed visual result or explicit non-complete disposition",
                    ),
                ),
                host_api,
                host_version,
                (
                    "A finite local visual profile supports CPU IMAGE and "
                    "already-decoded CFR VIDEO. "
                    "Host configuration, local-service consent and execution pins are required."
                ),
                "M15-01",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.AudioPerceptionProducer",
                "H3 Audio Perception Producer",
                "h3_context/perception",
                (
                    _socket(
                        "media",
                        NodeSocketType.H3_MEDIA_PRODUCER_RESULT,
                        required=True,
                        description="complete admitted audio/video media",
                    ),
                    _socket(
                        "route",
                        NodeSocketType.STRING,
                        required=True,
                        default="comfyui_native",
                        choices=("comfyui_native", "ollama", "specialist"),
                        description="explicit audio route without fallback",
                    ),
                    _socket(
                        "profile_id",
                        NodeSocketType.STRING,
                        required=True,
                        default="unqualified",
                        description="explicit qualified profile identifier",
                    ),
                    _socket(
                        "device",
                        NodeSocketType.STRING,
                        required=True,
                        default="auto",
                        choices=("auto", "cpu", "cuda", "mps"),
                        description="explicit execution device selection",
                    ),
                    _socket(
                        "cancel_requested",
                        NodeSocketType.BOOLEAN,
                        required=True,
                        default=False,
                        description="explicit pre-execution cancellation control",
                    ),
                    _socket(
                        "local_service_consent",
                        NodeSocketType.BOOLEAN,
                        required=False,
                        default=False,
                        description="explicit consent for transfer to a local perception service",
                    ),
                ),
                (
                    _socket(
                        "result",
                        NodeSocketType.H3_AUDIO_PRODUCER_RESULT,
                        required=True,
                        description="typed audio result or explicit non-complete disposition",
                    ),
                ),
                host_api,
                host_version,
                (
                    "A finite offline CPU profile supports short English speech. "
                    "Host configuration and exact installed asset/runtime pins are required."
                ),
                "M15-01",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.HardConstraintProducer",
                "H3 Hard Constraint Producer",
                "h3_context/assembly",
                tuple(
                    _socket(
                        name,
                        NodeSocketType.STRING,
                        required=True,
                        description=description,
                    )
                    for name, description in (
                        ("dialogue", "exact caller-authored dialogue"),
                        ("visible_text", "exact caller-authored visible text"),
                        ("required_content", "required content declaration"),
                        ("forbidden_content", "forbidden content declaration"),
                    )
                )
                + (
                    _socket(
                        "timing_start",
                        NodeSocketType.STRING,
                        required=True,
                        description="optional exact timing start",
                    ),
                    _socket(
                        "timing_end",
                        NodeSocketType.STRING,
                        required=True,
                        description="optional exact timing end",
                    ),
                    _socket(
                        "keep_target",
                        NodeSocketType.STRING,
                        required=True,
                        default="subject",
                        choices=(
                            "subject",
                            "scene",
                            "action",
                            "camera",
                            "style",
                            "audio",
                            "dialogue",
                            "lyrics",
                            "visible_text",
                            "asset",
                        ),
                        description="closed keep/change target",
                    ),
                    _socket(
                        "keep_value",
                        NodeSocketType.STRING,
                        required=True,
                        description="exact keep/change source value",
                    ),
                    _socket(
                        "change_replacement",
                        NodeSocketType.STRING,
                        required=True,
                        description="optional exact replacement value",
                    ),
                    _socket(
                        "dialogue_language",
                        NodeSocketType.STRING,
                        required=False,
                        default="auto",
                        choices=("auto", *OFFICIAL_STABLE_DIALOGUE_LANGUAGES),
                        description="stable dialogue language or automatic script derivation",
                    ),
                    _socket(
                        "dialogue_speaker",
                        NodeSocketType.STRING,
                        required=False,
                        description="authored identity phrase of the dialogue speaker",
                    ),
                    _socket(
                        "dialogue_delivery",
                        NodeSocketType.STRING,
                        required=False,
                        default="on_screen",
                        choices=tuple(value.value for value in DialogueDelivery),
                        description="on-screen speech or off-screen voiceover",
                    ),
                ),
                (
                    _socket(
                        "hard_constraints",
                        NodeSocketType.H3_HARD_CONSTRAINTS,
                        required=True,
                        description="immutable exact hard constraints",
                    ),
                    _socket(
                        "producer_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="bounded downstream provenance report",
                    ),
                ),
                host_api,
                host_version,
                "M15-02 preserves exact manual text and performs no rewriting.",
                "M15-02",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.IntentGraphProducer",
                "H3 Intent Graph Producer",
                "h3_context/assembly",
                (
                    _socket(
                        "request",
                        NodeSocketType.H3_CONTEXT_REQUEST,
                        required=True,
                        description="caller request",
                    ),
                    _socket(
                        "reference_registry",
                        NodeSocketType.H3_REFERENCE_REGISTRY,
                        required=True,
                        description="canonical reference ownership",
                    ),
                    _socket(
                        "subject_label",
                        NodeSocketType.STRING,
                        required=True,
                        description="optional manual subject label",
                    ),
                    _socket(
                        "action_description",
                        NodeSocketType.STRING,
                        required=True,
                        description="optional manual action description",
                    ),
                    _socket(
                        "secondary_subject_label",
                        NodeSocketType.STRING,
                        required=False,
                        description="optional second manual subject label",
                    ),
                    _socket(
                        "secondary_action_description",
                        NodeSocketType.STRING,
                        required=False,
                        description="optional second manual action description",
                    ),
                    _socket(
                        "complete_silence",
                        NodeSocketType.BOOLEAN,
                        required=False,
                        description="explicit complete-silence request, default off",
                    ),
                    _socket(
                        "keyframe_binding",
                        NodeSocketType.STRING,
                        required=False,
                        default="unbound",
                        choices=("unbound", "first_frame", "last_frame", "both"),
                        description="explicit primary subject binding to declared keyframe roles",
                    ),
                    _socket(
                        "segment_development",
                        NodeSocketType.STRING,
                        required=False,
                        default="unspecified",
                        choices=("unspecified", "anchor_development", "anchor_static_hold"),
                        description="caller-declared development of owned anchor content",
                    ),
                ),
                (
                    _socket(
                        "intent_graph",
                        NodeSocketType.H3_INTENT_GRAPH,
                        required=True,
                        description="validated deterministic manual intent graph",
                    ),
                    _socket(
                        "producer_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="bounded downstream provenance report",
                    ),
                ),
                host_api,
                host_version,
                "M15-02 delegates to the accepted intent-graph builder.",
                "M15-02",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.EvidenceFusionProducer",
                "H3 Evidence Fusion Producer",
                "h3_context/assembly",
                (
                    _socket(
                        "media",
                        NodeSocketType.H3_MEDIA_PRODUCER_RESULT,
                        required=True,
                        description="complete admitted media",
                    ),
                ),
                (
                    _socket(
                        "evidence_graph",
                        NodeSocketType.H3_UNIFIED_EVIDENCE_GRAPH,
                        required=True,
                        description="uncertainty-capped source graph",
                    ),
                    _socket(
                        "producer_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="bounded downstream provenance report",
                    ),
                ),
                host_api,
                host_version,
                "M15-02 does not promote admitted metadata to semantic perception.",
                "M15-02",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.CrossReferenceProducer",
                "H3 Cross Reference Producer",
                "h3_context/assembly",
                (
                    _socket(
                        "reference_registry",
                        NodeSocketType.H3_REFERENCE_REGISTRY,
                        required=True,
                        description="canonical reference ownership",
                    ),
                    _socket(
                        "media",
                        NodeSocketType.H3_MEDIA_PRODUCER_RESULT,
                        required=True,
                        description="complete admitted media",
                    ),
                    _socket(
                        "resolution",
                        NodeSocketType.STRING,
                        required=False,
                        default="source_only",
                        choices=("source_only", "ambiguous"),
                        description="explicit caller-owned identity resolution state",
                    ),
                    _socket(
                        "entity_kind",
                        NodeSocketType.STRING,
                        required=False,
                        default="subject",
                        choices=("subject", "voice", "object", "scene"),
                        description="explicit ambiguous entity family",
                    ),
                    _socket(
                        "candidate_a",
                        NodeSocketType.STRING,
                        required=False,
                        description="first caller-owned candidate ID",
                    ),
                    _socket(
                        "candidate_b",
                        NodeSocketType.STRING,
                        required=False,
                        description="second caller-owned candidate ID",
                    ),
                ),
                (
                    _socket(
                        "cross_reference_graph",
                        NodeSocketType.H3_CROSS_REFERENCE_GRAPH,
                        required=True,
                        description="source-owned cross-reference graph",
                    ),
                    _socket(
                        "producer_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="bounded downstream provenance report",
                    ),
                ),
                host_api,
                host_version,
                "M15-02 retains empty/ambiguous states without inferred identities.",
                "M15-02",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.DirectiveAuthorityProducer",
                "H3 Directive Authority Producer",
                "h3_context/assembly",
                (
                    _socket(
                        "request",
                        NodeSocketType.H3_CONTEXT_REQUEST,
                        required=True,
                        description="REF2VA caller request",
                    ),
                    _socket(
                        "reference_registry",
                        NodeSocketType.H3_REFERENCE_REGISTRY,
                        required=True,
                        description="canonical reference ownership",
                    ),
                    _socket(
                        "hard_constraints",
                        NodeSocketType.H3_HARD_CONSTRAINTS,
                        required=True,
                        description="immutable caller constraints",
                    ),
                    _socket(
                        "hard_constraints_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="current hard-constraint report",
                    ),
                    _socket(
                        "intent_graph",
                        NodeSocketType.H3_INTENT_GRAPH,
                        required=True,
                        description="validated manual intent targets",
                    ),
                    _socket(
                        "intent_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="current intent report",
                    ),
                    _socket(
                        "action",
                        NodeSocketType.STRING,
                        required=True,
                        default="none",
                        choices=("none", "retain", "adapt", "exclude"),
                        description="explicit directive action",
                    ),
                    _socket(
                        "target_kind",
                        NodeSocketType.STRING,
                        required=True,
                        default="subject",
                        choices=(
                            "subject",
                            "scene",
                            "action",
                            "asset",
                        ),
                        description="explicit directive target family",
                    ),
                    _socket(
                        "target_id",
                        NodeSocketType.STRING,
                        required=True,
                        description="explicit directive target identifier",
                    ),
                    _socket(
                        "source_asset_id",
                        NodeSocketType.STRING,
                        required=True,
                        description="optional source asset identifier",
                    ),
                    _socket(
                        "retention_aspect",
                        NodeSocketType.STRING,
                        required=True,
                        default="identity",
                        choices=(
                            "identity",
                            "style",
                            "camera",
                            "audio",
                            "voice",
                            "scene",
                            "action",
                            "object",
                        ),
                        description="closed retained aspect",
                    ),
                    _socket(
                        "adaptation",
                        NodeSocketType.STRING,
                        required=True,
                        description="inert caller-authored adaptation",
                    ),
                    _socket(
                        "exclusion_reason",
                        NodeSocketType.STRING,
                        required=True,
                        description="inert caller-authored exclusion reason",
                    ),
                    _socket(
                        "authority",
                        NodeSocketType.STRING,
                        required=True,
                        default="user_preference",
                        choices=(
                            "user_hard",
                            "user_preference",
                            "reference_only",
                            "assisted_proposal",
                        ),
                        description="explicit directive authority",
                    ),
                    _socket(
                        "priority",
                        NodeSocketType.INT,
                        required=True,
                        default=0,
                        description="bounded directive priority",
                    ),
                ),
                (
                    _socket(
                        "directive_authority",
                        NodeSocketType.H3_DIRECTIVE_AUTHORITY,
                        required=True,
                        description="resolved directive set and authority report",
                    ),
                    _socket(
                        "producer_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="bounded downstream provenance report",
                    ),
                ),
                host_api,
                host_version,
                "M15-02 delegates to accepted directive resolution and authority rules.",
                "M15-02",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.FullReferenceTimelineProducer",
                "H3 Full Reference Timeline Producer",
                "h3_context/assembly",
                (
                    _socket(
                        "request",
                        NodeSocketType.H3_CONTEXT_REQUEST,
                        required=True,
                        description="REF2VA caller request",
                    ),
                    _socket(
                        "reference_registry",
                        NodeSocketType.H3_REFERENCE_REGISTRY,
                        required=True,
                        description="canonical reference ownership",
                    ),
                    _socket(
                        "media",
                        NodeSocketType.H3_MEDIA_PRODUCER_RESULT,
                        required=True,
                        description="complete admitted media",
                    ),
                    _socket(
                        "evidence_graph",
                        NodeSocketType.H3_UNIFIED_EVIDENCE_GRAPH,
                        required=True,
                        description="source evidence graph",
                    ),
                    _socket(
                        "evidence_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="current evidence report",
                    ),
                    _socket(
                        "cross_reference_graph",
                        NodeSocketType.H3_CROSS_REFERENCE_GRAPH,
                        required=True,
                        description="source cross-reference graph",
                    ),
                    _socket(
                        "cross_reference_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="current cross-reference report",
                    ),
                    _socket(
                        "directive_authority",
                        NodeSocketType.H3_DIRECTIVE_AUTHORITY,
                        required=True,
                        description="resolved directive authority",
                    ),
                    _socket(
                        "directive_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="current directive report",
                    ),
                    _socket(
                        "intent_graph",
                        NodeSocketType.H3_INTENT_GRAPH,
                        required=True,
                        description="validated manual intent graph",
                    ),
                    _socket(
                        "intent_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="current intent report",
                    ),
                    _socket(
                        "visual_result",
                        NodeSocketType.H3_VISUAL_PRODUCER_RESULT,
                        required=False,
                        description="qualified observations for the exact admitted decoded video",
                    ),
                ),
                (
                    _socket(
                        "timeline",
                        NodeSocketType.H3_FULL_REFERENCE_TIMELINE,
                        required=True,
                        description=(
                            "partial timeline from qualified video, or a typed unavailable result"
                        ),
                    ),
                    _socket(
                        "producer_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="bounded partial or unavailable timeline report",
                    ),
                ),
                host_api,
                host_version,
                "M15-02 exposes explicit unavailable state without fabricating a timeline.",
                "M15-02",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.FeasibleAVTimeline",
                "H3 Feasible AV Timeline",
                "h3_context/planning",
                tuple(
                    _socket(name, socket_type, required=True, description=description)
                    for name, socket_type, description in (
                        ("request", NodeSocketType.H3_CONTEXT_REQUEST, "current request"),
                        (
                            "reference_registry",
                            NodeSocketType.H3_REFERENCE_REGISTRY,
                            "current registry",
                        ),
                        ("intent_graph", NodeSocketType.H3_INTENT_GRAPH, "authorized intent"),
                        (
                            "intent_report",
                            NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                            "intent authority",
                        ),
                        (
                            "directive_authority",
                            NodeSocketType.H3_DIRECTIVE_AUTHORITY,
                            "resolved directives",
                        ),
                        (
                            "directive_report",
                            NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                            "directive authority",
                        ),
                    )
                ),
                (
                    _socket(
                        "feasible_timeline",
                        NodeSocketType.H3_FEASIBLE_AV_TIMELINE,
                        required=True,
                        description="authorized feasible timeline",
                    ),
                ),
                host_api,
                host_version,
                "M13-10 exposes the accepted M13-06 deterministic timeline planner.",
                "M13-10",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.HierarchicalEvidenceReduction",
                "H3 Hierarchical Evidence Reduction",
                "h3_context/planning",
                (
                    _socket(
                        "media",
                        NodeSocketType.H3_MEDIA_PRODUCER_RESULT,
                        required=True,
                        description="admitted media authority",
                    ),
                    _socket(
                        "evidence_graph",
                        NodeSocketType.H3_UNIFIED_EVIDENCE_GRAPH,
                        required=True,
                        description="current evidence graph",
                    ),
                    _socket(
                        "evidence_report",
                        NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        required=True,
                        description="evidence authority",
                    ),
                    _socket(
                        "feasible_timeline",
                        NodeSocketType.H3_FEASIBLE_AV_TIMELINE,
                        required=True,
                        description="current feasible timeline",
                    ),
                ),
                (
                    _socket(
                        "reduction",
                        NodeSocketType.H3_HIERARCHICAL_EVIDENCE_REDUCTION,
                        required=True,
                        description="authorized hierarchical reduction",
                    ),
                ),
                host_api,
                host_version,
                "M13-10 exposes accepted M13-07 target-directed reduction.",
                "M13-10",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.ConstrainedSemanticPlanning",
                "H3 Constrained Semantic Planning",
                "h3_context/planning",
                (
                    _socket(
                        "reduction",
                        NodeSocketType.H3_HIERARCHICAL_EVIDENCE_REDUCTION,
                        required=True,
                        description="current hierarchical reduction",
                    ),
                    _socket(
                        "plan",
                        NodeSocketType.H3_CONTEXT_PLAN,
                        required=True,
                        description="manual deterministic plan",
                    ),
                ),
                (
                    _socket(
                        "semantic_planning",
                        NodeSocketType.H3_CONSTRAINED_SEMANTIC_PLANNING,
                        required=True,
                        description="explicit semantic-planning disposition",
                    ),
                    _socket(
                        "accepted_plan",
                        NodeSocketType.H3_CONTEXT_PLAN,
                        required=True,
                        description="exact authorized manual plan",
                    ),
                ),
                host_api,
                host_version,
                "M13-10 executes M13-08 as an explicit model-free unavailable disposition.",
                "M13-10",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.SourceProfiledRenderer",
                "H3 Source-Profiled Renderer",
                "h3_context/compiler",
                (
                    _socket(
                        "plan",
                        NodeSocketType.H3_CONTEXT_PLAN,
                        required=True,
                        description="accepted context plan",
                    ),
                ),
                (
                    _socket(
                        "prompt",
                        NodeSocketType.H3_PROMPT_STRING,
                        required=True,
                        description="source-profiled prompt text",
                    ),
                    _socket(
                        "profiled_result",
                        NodeSocketType.H3_SOURCE_PROFILED_PROMPT,
                        required=True,
                        description="source-profiled prompt result",
                    ),
                    _socket(
                        "prompt_document",
                        NodeSocketType.H3_PROMPT_DOCUMENT,
                        required=True,
                        description="rendered prompt document",
                    ),
                ),
                host_api,
                host_version,
                "M13-10 delegates to the accepted source-profiled renderer and validator.",
                "M13-10",
            ),
            NodeContract(
                "comfyui_h3_context.H3Context.LocalReconstruction",
                "H3 Local Reconstruction Acceptance",
                "h3_context/acceptance",
                tuple(
                    _socket(
                        name,
                        socket_type,
                        required=True,
                        description=f"accepted local reconstruction {name}",
                    )
                    for name, socket_type in (
                        ("request", NodeSocketType.H3_CONTEXT_REQUEST),
                        ("reference_registry", NodeSocketType.H3_REFERENCE_REGISTRY),
                        ("media", NodeSocketType.H3_MEDIA_PRODUCER_RESULT),
                        ("hard_constraints", NodeSocketType.H3_HARD_CONSTRAINTS),
                        (
                            "hard_constraints_report",
                            NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        ),
                        ("intent_graph", NodeSocketType.H3_INTENT_GRAPH),
                        ("intent_report", NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT),
                        ("evidence_graph", NodeSocketType.H3_UNIFIED_EVIDENCE_GRAPH),
                        ("evidence_report", NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT),
                        ("cross_reference_graph", NodeSocketType.H3_CROSS_REFERENCE_GRAPH),
                        (
                            "cross_reference_report",
                            NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT,
                        ),
                        ("directive_authority", NodeSocketType.H3_DIRECTIVE_AUTHORITY),
                        ("directive_report", NodeSocketType.H3_DOWNSTREAM_PRODUCER_REPORT),
                        ("feasible_timeline", NodeSocketType.H3_FEASIBLE_AV_TIMELINE),
                        (
                            "hierarchical_reduction",
                            NodeSocketType.H3_HIERARCHICAL_EVIDENCE_REDUCTION,
                        ),
                        (
                            "semantic_planning",
                            NodeSocketType.H3_CONSTRAINED_SEMANTIC_PLANNING,
                        ),
                        ("plan", NodeSocketType.H3_CONTEXT_PLAN),
                        ("profiled_prompt", NodeSocketType.H3_SOURCE_PROFILED_PROMPT),
                        ("validation", NodeSocketType.H3_VALIDATION_RESULT),
                        ("validated_report", NodeSocketType.H3_CONTEXT_REPORT),
                        ("native_h3_wiring", NodeSocketType.H3_NATIVE_H3_WIRING),
                    )
                ),
                (
                    _socket(
                        "reconstruction",
                        NodeSocketType.H3_LOCAL_RECONSTRUCTION,
                        required=True,
                        description="joined local reconstruction result",
                    ),
                    _socket(
                        "accepted_prompt",
                        NodeSocketType.H3_PROMPT_STRING,
                        required=True,
                        description="validated prompt text",
                    ),
                    _socket(
                        "accepted_report",
                        NodeSocketType.H3_CONTEXT_REPORT,
                        required=True,
                        description="current validated context report",
                    ),
                ),
                host_api,
                host_version,
                "M13-10 joins visible accepted stages without hidden inference or fallback.",
                "M13-10",
            ),
        )
    )
