"""Node adapters that speak to H3 itself: native ComfyUI H3 wiring and the official Context-IR
surface.

Neither claims local equivalence with the official implementation.  The IR adapter is a comparison
surface, not a reimplementation of it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from .adapters.official_context_ir_host import (
    OfficialContextIRHostLease,
    acquire_official_context_ir_host,
    official_context_ir_host_configured,
)
from .core.context_reporting import (
    ContextReport,
    ProviderReceipt,
)
from .core.contracts import (
    ProviderIdentity,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .core.errors import (
    ContextReportError,
    ContractValidationError,
    NativeH3AdapterError,
    OfficialContextIRError,
    SecurityPolicyError,
)
from .core.native_h3 import NativeH3Wiring, build_native_h3_wiring
from .core.normalization import (
    RawContextRequest,
    normalize_request,
)
from .core.official_context_ir import (
    DEFAULT_OFFICIAL_CONTEXT_IR_LIFECYCLE_POLICY,
    OFFICIAL_CONTEXT_IR_DESCRIPTOR,
    OfficialAspectRatio,
    OfficialContextIRAdapter,
    OfficialContextIRCancellationProbe,
    OfficialContextIRLifecyclePolicy,
    OfficialContextIRMedia,
    OfficialContextIRRequest,
    OfficialContextIRResult,
    OfficialContextIRTransport,
    build_official_context_ir_request,
)
from .core.provider_policy import (
    CredentialResolver,
    ProviderConsentNotice,
    ProviderExecutionPolicy,
    ProviderPrivacyMode,
    build_provider_consent_notice,
    validate_provider_policy,
)
from .core.reconstruction_stages import (
    _register_reconstruction_native,
)
from .core.registry import (
    ReferenceRegistry,
)
from .node_capability import NodeCapability, NodeCapabilityReason
from .node_support import (
    PROMPT_STRING_SOCKET_TYPE,
    REFERENCE_SOCKET_TYPE,
    REQUEST_SOCKET_TYPE,
    NativeH3AdapterNodeError,
    OfficialContextIRNodeError,
)
from .product_shell_node import (
    NATIVE_H3_WIRING_SOCKET_TYPE,
    REPORT_SOCKET_TYPE,
)

NATIVE_H3_ADAPTER_NODE_ID = "comfyui_h3_context.H3Context.NativeH3Adapter"

NATIVE_H3_ADAPTER_DISPLAY_NAME = "H3 Native MiniMax H3 Adapter"

OFFICIAL_CONTEXT_IR_NODE_ID = "comfyui_h3_context.H3Context.OfficialContextIR"

OFFICIAL_CONTEXT_IR_DISPLAY_NAME = "H3 Official Context-IR"

OFFICIAL_CONTEXT_IR_RECEIPT_SOCKET_TYPE = "H3_PROVIDER_RECEIPT"

OFFICIAL_CONTEXT_IR_CONSENT_SOCKET_TYPE = "H3_PROVIDER_CONSENT"

OFFICIAL_CONTEXT_IR_MEDIA_SOCKET_TYPE = "H3_CONTEXT_IR_MEDIA"


class H3ContextNativeH3AdapterNode:
    """Expose exact prompt plus a declarative direct-to-native H3 wiring manifest."""

    NODE_ID = NATIVE_H3_ADAPTER_NODE_ID
    __h3_context_node_id__ = NATIVE_H3_ADAPTER_NODE_ID
    RETURN_TYPES = (PROMPT_STRING_SOCKET_TYPE, NATIVE_H3_WIRING_SOCKET_TYPE)
    RETURN_NAMES = ("prompt", "native_h3_wiring")
    FUNCTION = "adapt"
    CATEGORY = "h3_context/native"
    DESCRIPTION = (
        "Preserves the final prompt and describes direct original-media connections to pinned "
        "MiniMax H3 conditioning nodes."
    )
    OUTPUT_NODE = False

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "report": (
                    REPORT_SOCKET_TYPE,
                    {"tooltip": "Rendered H3 context report to map to native H3."},
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, report: object) -> bool | str:
        # Defer linked report type checking until the upstream compiler has executed.
        if report is None:
            return True
        try:
            cls().adapt(report)
        except NativeH3AdapterNodeError as exc:
            return str(exc)
        return True

    def adapt(self, report: object) -> tuple[str, NativeH3Wiring]:
        if not isinstance(report, ContextReport):
            raise NativeH3AdapterNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "invalid_report",
                        "report must be a ContextReport",
                    ),
                )
            )
        try:
            wiring = build_native_h3_wiring(report)
            _register_reconstruction_native(report, wiring)
        except (
            ContractValidationError,
            NativeH3AdapterError,
            ContextReportError,
            TypeError,
            ValueError,
        ) as exc:
            code = exc.code if isinstance(exc, NativeH3AdapterError) else "native_h3_wiring_failed"
            raise NativeH3AdapterNodeError(
                (
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        code,
                        "native H3 wiring construction failed closed",
                    ),
                )
            ) from exc
        return report.prompt_document.text, wiring


class H3OfficialContextIRNode:
    """Thin optional node for an injected official Context-IR provider/oracle transport."""

    NODE_ID = OFFICIAL_CONTEXT_IR_NODE_ID
    __h3_context_node_id__ = OFFICIAL_CONTEXT_IR_NODE_ID
    RETURN_TYPES = (
        PROMPT_STRING_SOCKET_TYPE,
        OFFICIAL_CONTEXT_IR_RECEIPT_SOCKET_TYPE,
        OFFICIAL_CONTEXT_IR_CONSENT_SOCKET_TYPE,
    )
    RETURN_NAMES = ("prompt", "receipt", "consent_notice")
    FUNCTION = "execute"
    CATEGORY = "h3_context/providers"
    DESCRIPTION = (
        "Requires host configuration (transport_unconfigured): configure the official Context-IR "
        "transport before queueing. Explicit upload/network consent and credentials are required."
    )
    OUTPUT_NODE = False

    def __init__(
        self,
        *,
        resolver: CredentialResolver | None = None,
        transport: OfficialContextIRTransport | None = None,
        lifecycle: OfficialContextIRLifecyclePolicy = DEFAULT_OFFICIAL_CONTEXT_IR_LIFECYCLE_POLICY,
        cancellation_probe: OfficialContextIRCancellationProbe | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._resolver = resolver
        self._transport = transport
        self._use_host_composition = resolver is None and transport is None
        self._lifecycle = lifecycle
        self._cancellation_probe = cancellation_probe
        self._clock = clock
        self._sleep = sleep
        self._last_result: OfficialContextIRResult | None = None

    @property
    def last_result(self) -> OfficialContextIRResult | None:
        """Expose the last typed result to a host-side recording harness, not workflow state."""

        return self._last_result

    def capability(self) -> NodeCapability:
        configured = self._transport is not None and all(
            callable(getattr(self._transport, method, None)) for method in ("create", "query")
        )
        if self._use_host_composition:
            configured = official_context_ir_host_configured()
        return NodeCapability(
            self.NODE_ID,
            NodeCapabilityReason.TRANSPORT_UNCONFIGURED
            if not configured
            else NodeCapabilityReason.TRANSPORT_CONFIGURED,
        )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, dict[str, object]]]]:
        return {
            "required": {
                "request": (
                    REQUEST_SOCKET_TYPE,
                    {"tooltip": "Typed H3 request envelope; no native H3 generation is queued."},
                ),
                "ratio": (
                    "COMBO",
                    {
                        "default": OfficialAspectRatio.ADAPTIVE.value,
                        "options": [ratio.value for ratio in OfficialAspectRatio],
                        "tooltip": "Official Context-IR aspect-ratio contract.",
                    },
                ),
                "upload_consent": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "Explicitly confirm that media may leave this host.",
                    },
                ),
                "network_allowed": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "Explicitly allow the selected official provider network path.",
                    },
                ),
                "credential_reference": (
                    "STRING",
                    {
                        "default": "env.official_minimax",
                        "multiline": False,
                        "dynamicPrompts": False,
                        "tooltip": (
                            "Opaque runtime credential reference; never the credential value."
                        ),
                    },
                ),
            },
            "optional": {
                "reference_registry": (
                    REFERENCE_SOCKET_TYPE,
                    {"tooltip": "Optional canonical registry to reconcile before mapping media."},
                ),
                "media": (
                    OFFICIAL_CONTEXT_IR_MEDIA_SOCKET_TYPE,
                    {
                        "tooltip": (
                            "Already-admitted OfficialContextIRMedia values in canonical order."
                        ),
                    },
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(
        cls,
        request: object | None = None,
        ratio: OfficialAspectRatio | str = OfficialAspectRatio.ADAPTIVE.value,
        reference_registry: object | None = None,
        media: object | None = None,
        upload_consent: bool = False,
        network_allowed: bool = False,
        credential_reference: str = "env.official_minimax",
    ) -> bool | str:
        """Validate typed controls without resolving credentials or calling the transport."""

        if request is None:
            # CRITICAL: an injected test instance does not configure the class registered with
            # ComfyUI. Preflight must inspect the same default host composition as execution.
            return cls().capability().validation_result()
        try:
            cls()._prepare(
                request,
                ratio=ratio,
                reference_registry=reference_registry,
                media=media,
                upload_consent=upload_consent,
                network_allowed=network_allowed,
                credential_reference=credential_reference,
            )
        except OfficialContextIRNodeError as exc:
            return str(exc)
        return cls().capability().validation_result()

    @staticmethod
    def _policy(
        *,
        upload_consent: bool,
        network_allowed: bool,
        credential_reference: str,
    ) -> ProviderExecutionPolicy:
        if not isinstance(upload_consent, bool) or not isinstance(network_allowed, bool):
            raise OfficialContextIRNodeError("policy", "provider controls must be booleans")
        if not isinstance(credential_reference, str):
            raise OfficialContextIRNodeError("policy", "credential_reference must be a string")
        try:
            return ProviderExecutionPolicy(
                provider=ProviderIdentity.OFFICIAL_MINIMAX,
                privacy_mode=ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=not network_allowed,
                network_allowed=network_allowed,
                upload_consent=upload_consent,
                credential_reference=credential_reference,
            )
        except (ContractValidationError, SecurityPolicyError) as exc:
            raise OfficialContextIRNodeError(
                "policy", "official provider controls are invalid"
            ) from exc

    @staticmethod
    def _media_values(media: object | None) -> tuple[OfficialContextIRMedia, ...]:
        if media is None:
            return ()
        values = (media,) if isinstance(media, OfficialContextIRMedia) else media
        if not isinstance(values, (list, tuple)):
            raise OfficialContextIRNodeError(
                "invalid_media", "media must contain typed official values"
            )
        if not all(isinstance(item, OfficialContextIRMedia) for item in values):
            raise OfficialContextIRNodeError(
                "invalid_media", "media contains an invalid typed value"
            )
        return tuple(values)

    def _prepare(
        self,
        request: object,
        *,
        ratio: OfficialAspectRatio | str,
        reference_registry: object | None,
        media: object | None,
        upload_consent: bool,
        network_allowed: bool,
        credential_reference: str,
    ) -> tuple[OfficialContextIRRequest, ProviderExecutionPolicy]:
        if not isinstance(request, RawContextRequest):
            raise OfficialContextIRNodeError(
                "invalid_request", "request must be a RawContextRequest"
            )
        selected_registry = request.reference_registry
        if reference_registry is not None:
            if not isinstance(reference_registry, ReferenceRegistry):
                raise OfficialContextIRNodeError(
                    "invalid_reference_registry", "reference_registry must be a ReferenceRegistry"
                )
            if selected_registry.assets and selected_registry != reference_registry:
                raise OfficialContextIRNodeError(
                    "reference_registry_mismatch", "request and connected registries disagree"
                )
            selected_registry = reference_registry
        try:
            normalized_result = normalize_request(
                replace(request, reference_registry=selected_registry)
            )
        except (ContextReportError, TypeError, ValueError) as exc:
            raise OfficialContextIRNodeError(
                "invalid_request", "request normalization failed closed"
            ) from exc
        if normalized_result.request is None:
            raise OfficialContextIRNodeError(
                "invalid_request", "request normalization failed closed"
            )
        policy = self._policy(
            upload_consent=upload_consent,
            network_allowed=network_allowed,
            credential_reference=credential_reference,
        )
        diagnostics = validate_provider_policy(OFFICIAL_CONTEXT_IR_DESCRIPTOR, policy)
        if diagnostics:
            raise OfficialContextIRNodeError("policy", diagnostics[0].code)
        try:
            official_request = build_official_context_ir_request(
                normalized_result.request,
                ratio=ratio,
                media=self._media_values(media),
            )
        except (OfficialContextIRError, ContractValidationError, TypeError, ValueError) as exc:
            if isinstance(exc, OfficialContextIRError):
                raise OfficialContextIRNodeError(
                    exc.category, "official request mapping failed"
                ) from exc
            raise OfficialContextIRNodeError(
                "invalid_request", "official request mapping failed"
            ) from exc
        return official_request, policy

    def execute(
        self,
        request: object,
        ratio: OfficialAspectRatio | str = OfficialAspectRatio.ADAPTIVE.value,
        reference_registry: object | None = None,
        media: object | None = None,
        upload_consent: bool = False,
        network_allowed: bool = False,
        credential_reference: str = "env.official_minimax",
    ) -> tuple[str, ProviderReceipt, ProviderConsentNotice]:
        """Execute the injected official provider and return prompt, receipt, and disclosure."""

        self._last_result = None
        official_request, policy = self._prepare(
            request,
            ratio=ratio,
            reference_registry=reference_registry,
            media=media,
            upload_consent=upload_consent,
            network_allowed=network_allowed,
            credential_reference=credential_reference,
        )
        notice = build_provider_consent_notice(OFFICIAL_CONTEXT_IR_DESCRIPTOR, policy)
        lease: OfficialContextIRHostLease | None = None
        try:
            resolver, transport = self._resolver, self._transport
            if self._use_host_composition and official_context_ir_host_configured():
                lease = acquire_official_context_ir_host(credential_reference)
                resolver, transport = lease.resolver, lease.transport
            if transport is None:
                raise OfficialContextIRNodeError(
                    "transport_unconfigured", "official provider transport is not configured"
                )
            try:
                result = OfficialContextIRAdapter().execute(
                    official_request,
                    policy=policy,
                    resolver=resolver,
                    transport=transport,
                    lifecycle=self._lifecycle,
                    cancellation_probe=self._cancellation_probe,
                    clock=self._clock,
                    sleep=self._sleep,
                )
            finally:
                if lease is not None:
                    lease.close()
            if lease is not None:
                lease.ensure_current()
        except OfficialContextIRError:
            raise
        except SecurityPolicyError as exc:
            raise OfficialContextIRNodeError("policy", "official provider policy failed") from exc
        except (ContractValidationError, TypeError, ValueError) as exc:
            raise OfficialContextIRNodeError(
                "provider", "official provider execution failed"
            ) from exc
        self._last_result = result
        return result.prompt, result.receipt, notice
