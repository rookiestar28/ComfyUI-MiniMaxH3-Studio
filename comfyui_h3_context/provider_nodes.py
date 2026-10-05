"""Node adapters for provider transparency and reliability.

Provider selection and hosted-upload consent are explicit in this package, and these adapters are
where a user sees what would be sent and to whom before any of it is.
"""

from __future__ import annotations

from typing import cast

from .core.contracts import (
    ProviderIdentity,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .core.errors import (
    ContractValidationError,
    ReliabilityError,
    SecurityPolicyError,
)
from .core.provider_policy import (
    ProviderConsentNotice,
    ProviderExecutionPolicy,
    ProviderPrivacyMode,
    ProviderTransparency,
    build_provider_transparency,
)
from .core.provider_setup import ProviderLocalBackend, ProviderSetup, build_provider_setup
from .core.reliability import (
    ProgressEvent,
    RecoveryDecision,
    ReliabilityRunState,
    ReliabilityStage,
    ReliabilityStatus,
    build_reliability_status,
)
from .node_support import (
    ProviderTransparencyNodeError,
    ReliabilityNodeError,
)

PROVIDER_TRANSPARENCY_NODE_ID = "comfyui_h3_context.H3Context.ProviderTransparency"

PROVIDER_TRANSPARENCY_DISPLAY_NAME = "H3 Provider Transparency"

PROVIDER_TRANSPARENCY_SOCKET_TYPE = "H3_PROVIDER_TRANSPARENCY"

PROVIDER_CONSENT_SOCKET_TYPE = "H3_PROVIDER_CONSENT"

PROVIDER_SETUP_SOCKET_TYPE = "H3_PROVIDER_SETUP"

RELIABILITY_NODE_ID = "comfyui_h3_context.H3Context.Reliability"

RELIABILITY_DISPLAY_NAME = "H3 Reliability Status"

RELIABILITY_STATUS_SOCKET_TYPE = "H3_EXECUTION_STATUS"

RELIABILITY_PROGRESS_SOCKET_TYPE = "H3_PROGRESS_EVENT"

RELIABILITY_RECOVERY_SOCKET_TYPE = "H3_RECOVERY_DECISION"


class H3ContextProviderTransparencyNode:
    """Show explicit provider/privacy implications without executing any route."""

    NODE_ID = PROVIDER_TRANSPARENCY_NODE_ID
    __h3_context_node_id__ = PROVIDER_TRANSPARENCY_NODE_ID
    RETURN_TYPES = (
        PROVIDER_TRANSPARENCY_SOCKET_TYPE,
        PROVIDER_CONSENT_SOCKET_TYPE,
        "STRING",
        PROVIDER_SETUP_SOCKET_TYPE,
    )
    RETURN_NAMES = ("transparency", "consent_notice", "disclosure", "provider_setup")
    FUNCTION = "describe"
    CATEGORY = "h3_context/providers"
    DESCRIPTION = (
        "Displays explicit network, upload, credential, retention, cost, and fallback policy "
        "before execution; it never calls a provider or selects a fallback."
    )
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "provider": (
                    "COMBO",
                    {
                        "default": ProviderIdentity.MANUAL.value,
                        "options": [identity.value for identity in ProviderIdentity],
                        "tooltip": "Explicit provider identity; no route is inferred.",
                    },
                ),
                "privacy_mode": (
                    "COMBO",
                    {
                        "default": ProviderPrivacyMode.LOCAL_ONLY.value,
                        "options": [mode.value for mode in ProviderPrivacyMode],
                        "tooltip": "Explicit local-only or remote privacy decision.",
                    },
                ),
                "offline": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Keep the selected route offline when true.",
                    },
                ),
                "network_allowed": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "Explicitly permit the selected provider's network path.",
                    },
                ),
                "upload_consent": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "Explicitly consent to remote media transfer.",
                    },
                ),
                "credential_reference": (
                    "STRING",
                    {
                        "default": "none",
                        "multiline": False,
                        "dynamicPrompts": False,
                        "tooltip": "Opaque runtime reference only; never returned in disclosure.",
                    },
                ),
                "fallback_provider": (
                    "COMBO",
                    {
                        "default": "none",
                        "options": ["none", *(identity.value for identity in ProviderIdentity)],
                        "tooltip": "Optional caller-selected fallback metadata; never automatic.",
                    },
                ),
            },
            "optional": {
                "local_backend": (
                    "COMBO",
                    {
                        "default": ProviderLocalBackend.NONE.value,
                        "options": [backend.value for backend in ProviderLocalBackend],
                        "tooltip": (
                            "Local defaults to preferred comfyui_native; Ollama is explicit and "
                            "uses a disclosed loopback process boundary."
                        ),
                    },
                )
            },
        }

    @classmethod
    def VALIDATE_INPUTS(
        cls,
        provider: object = ProviderIdentity.MANUAL.value,
        privacy_mode: object = ProviderPrivacyMode.LOCAL_ONLY.value,
        offline: object = True,
        network_allowed: object = False,
        upload_consent: object = False,
        credential_reference: object = "",
        fallback_provider: object = "none",
        local_backend: object = ProviderLocalBackend.NONE.value,
    ) -> bool | str:
        try:
            cls().describe(
                provider,
                privacy_mode,
                offline,
                network_allowed,
                upload_consent,
                credential_reference,
                fallback_provider,
                local_backend,
            )
        except ProviderTransparencyNodeError as exc:
            return str(exc)
        return True

    def describe(
        self,
        provider: object = ProviderIdentity.MANUAL.value,
        privacy_mode: object = ProviderPrivacyMode.LOCAL_ONLY.value,
        offline: object = True,
        network_allowed: object = False,
        upload_consent: object = False,
        credential_reference: object = "",
        fallback_provider: object = "none",
        local_backend: object = ProviderLocalBackend.NONE.value,
    ) -> tuple[ProviderTransparency, ProviderConsentNotice, str, ProviderSetup]:
        if not isinstance(provider, str):
            raise ProviderTransparencyNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "provider",
                        "provider must be an explicit provider identity",
                    ),
                )
            )
        if not isinstance(privacy_mode, str):
            raise ProviderTransparencyNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "privacy_mode",
                        "privacy_mode must be an explicit privacy mode",
                    ),
                )
            )
        if not all(isinstance(value, bool) for value in (offline, network_allowed, upload_consent)):
            raise ProviderTransparencyNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "policy",
                        "offline, network_allowed, and upload_consent must be booleans",
                    ),
                )
            )
        if not isinstance(credential_reference, str):
            raise ProviderTransparencyNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "credential_reference",
                        "credential_reference must be a string",
                    ),
                )
            )
        if not isinstance(fallback_provider, str):
            raise ProviderTransparencyNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "fallback_provider",
                        "fallback_provider must be an explicit identity or none",
                    ),
                )
            )
        if not isinstance(local_backend, str):
            raise ProviderTransparencyNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "local_backend",
                        "local_backend must be an explicit backend identity",
                    ),
                )
            )
        offline_value = cast(bool, offline)
        network_allowed_value = cast(bool, network_allowed)
        upload_consent_value = cast(bool, upload_consent)
        try:
            selected_provider = ProviderIdentity(provider)
            selected_privacy = ProviderPrivacyMode(privacy_mode)
            fallback = None if fallback_provider == "none" else ProviderIdentity(fallback_provider)
            policy = ProviderExecutionPolicy(
                provider=selected_provider,
                privacy_mode=selected_privacy,
                offline=offline_value,
                network_allowed=network_allowed_value,
                upload_consent=upload_consent_value,
                credential_reference=(
                    None if credential_reference in {"", "none"} else credential_reference
                ),
                fallback_provider=fallback,
            )
            transparency = build_provider_transparency(policy)
            selected_backend = ProviderLocalBackend(local_backend)
            setup = build_provider_setup(
                policy,
                local_backend=None
                if selected_backend is ProviderLocalBackend.NONE
                else selected_backend,
            )
        except (ContractValidationError, SecurityPolicyError, ValueError) as exc:
            raise ProviderTransparencyNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "policy",
                        "provider transparency controls are invalid",
                    ),
                )
            ) from exc
        disclosure = f"{transparency.disclosure} {setup.disclosure}"
        return transparency, transparency.consent_notice, disclosure, setup


class H3ContextReliabilityNode:
    """Expose bounded progress and caller-visible recovery state without executing a worker."""

    NODE_ID = RELIABILITY_NODE_ID
    __h3_context_node_id__ = RELIABILITY_NODE_ID
    RETURN_TYPES = (
        RELIABILITY_STATUS_SOCKET_TYPE,
        RELIABILITY_PROGRESS_SOCKET_TYPE,
        RELIABILITY_RECOVERY_SOCKET_TYPE,
        "STRING",
    )
    RETURN_NAMES = ("status", "progress", "recovery", "disclosure")
    FUNCTION = "describe"
    CATEGORY = "h3_context/runtime"
    DESCRIPTION = (
        "Shows bounded progress, cancellation, and retry/resume decisions; it never starts, "
        "retries, resumes, or cancels a worker implicitly."
    )
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "operation_id": (
                    "STRING",
                    {
                        "default": "h3.context",
                        "multiline": False,
                        "dynamicPrompts": False,
                        "tooltip": "Bounded operation identity; no paths or credentials.",
                    },
                ),
                "stage": (
                    "COMBO",
                    {
                        "default": "planning",
                        "options": [
                            "extraction",
                            "provider",
                            "planning",
                            "rendering",
                            "validation",
                            "host",
                        ],
                        "tooltip": "Explicit long-running stage.",
                    },
                ),
                "completed_units": (
                    "INT",
                    {"default": 0, "min": 0, "max": 1000000, "step": 1},
                ),
                "total_units": (
                    "INT",
                    {"default": 1, "min": 1, "max": 1000000, "step": 1},
                ),
                "run_state": (
                    "COMBO",
                    {
                        "default": "running",
                        "options": [
                            "pending",
                            "running",
                            "completed",
                            "cancel_requested",
                            "cancelled",
                            "retryable_failure",
                            "failed",
                            "stale",
                            "recovered",
                        ],
                    },
                ),
                "cancel_requested": (
                    "BOOLEAN",
                    {"default": False, "tooltip": "Explicit caller cancellation request."},
                ),
                "attempt": (
                    "INT",
                    {"default": 1, "min": 1, "max": 8, "step": 1},
                ),
                "max_attempts": (
                    "INT",
                    {"default": 2, "min": 1, "max": 8, "step": 1},
                ),
                "checkpoint_status": (
                    "COMBO",
                    {
                        "default": "incompatible",
                        "options": ["none", "compatible", "stale", "incompatible"],
                        "tooltip": "Explicit caller checkpoint compatibility result.",
                    },
                ),
                "resume_requested": (
                    "BOOLEAN",
                    {"default": False, "tooltip": "Explicit caller resume request."},
                ),
            }
        }

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs: object) -> bool | str:
        try:
            cls().describe(**kwargs)
        except ReliabilityNodeError as exc:
            return str(exc)
        return True

    def describe(
        self,
        operation_id: object = "h3.context",
        stage: object = "planning",
        completed_units: object = 0,
        total_units: object = 1,
        run_state: object = "running",
        cancel_requested: object = False,
        attempt: object = 1,
        max_attempts: object = 2,
        checkpoint_status: object = "incompatible",
        resume_requested: object = False,
    ) -> tuple[ReliabilityStatus, ProgressEvent, RecoveryDecision, str]:
        values = (
            operation_id,
            stage,
            completed_units,
            total_units,
            run_state,
            cancel_requested,
            attempt,
            max_attempts,
            checkpoint_status,
            resume_requested,
        )
        if not isinstance(operation_id, str) or not isinstance(stage, str):
            raise ReliabilityNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "reliability_controls",
                        "operation_id and stage must be strings",
                    ),
                )
            )
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in values[2:4]):
            raise ReliabilityNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "reliability_progress",
                        "completed_units and total_units must be integers",
                    ),
                )
            )
        if not isinstance(run_state, str) or not isinstance(checkpoint_status, str):
            raise ReliabilityNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "reliability_state",
                        "run_state and checkpoint_status must be strings",
                    ),
                )
            )
        if not all(isinstance(value, bool) for value in (cancel_requested, resume_requested)):
            raise ReliabilityNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "reliability_cancellation",
                        "cancel_requested and resume_requested must be booleans",
                    ),
                )
            )
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (attempt, max_attempts)
        ):
            raise ReliabilityNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "reliability_attempt",
                        "attempt and max_attempts must be integers",
                    ),
                )
            )
        completed_units_value = cast(int, completed_units)
        total_units_value = cast(int, total_units)
        cancel_requested_value = cast(bool, cancel_requested)
        attempt_value = cast(int, attempt)
        max_attempts_value = cast(int, max_attempts)
        resume_requested_value = cast(bool, resume_requested)
        try:
            status = build_reliability_status(
                operation_id,
                stage=stage,
                completed_units=completed_units_value,
                total_units=total_units_value,
                state=run_state,
                cancel_requested=cancel_requested_value,
                attempt=attempt_value,
                max_attempts=max_attempts_value,
                checkpoint_status=checkpoint_status,
                resume_requested=resume_requested_value,
            )
        except (ReliabilityError, TypeError, ValueError) as exc:
            raise ReliabilityNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "reliability_controls",
                        "reliability controls are invalid",
                    ),
                )
            ) from exc
        progress = status.progress
        recovery = status.decision
        disclosure = (
            f"Reliability operation {status.operation_id}: "
            f"stage={cast(ReliabilityStage, progress.stage).value}; "
            f"progress={progress.completed_units}/{progress.total_units}; "
            f"state={cast(ReliabilityRunState, progress.state).value}; "
            f"action={recovery.action.value}; attempt={recovery.attempt}; "
            f"execution_allowed={'yes' if status.execution_allowed else 'no'}; "
            f"cancel_requested={'yes' if status.cancel_requested else 'no'}; "
            "no implicit retry/resume/cancellation."
        )
        return status, progress, recovery, disclosure
