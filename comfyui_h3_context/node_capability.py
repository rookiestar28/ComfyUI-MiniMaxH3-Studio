"""Finite operational node capability, separate from declarative socket maturity."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class NodeCapabilityStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    REQUIRES_CONFIGURATION = "requires_configuration"
    CANCELLED = "cancelled"


class NodeCapabilityReason(str, Enum):
    PROFILE_UNAVAILABLE = "profile_unavailable"
    PERCEPTION_PROFILE_UNAVAILABLE = "perception_profile_unavailable"
    TRANSPORT_UNCONFIGURED = "transport_unconfigured"
    TRANSPORT_CONFIGURED = "transport_configured"
    CANCELLED_BEFORE_EXECUTION = "cancelled_before_execution"
    PERCEPTION_CONFIGURED = "perception_configured"
    PERCEPTION_UNCONFIGURED = "perception_unconfigured"
    QUALIFIED_TIMELINE = "qualified_timeline"


_DETAILS = {
    NodeCapabilityReason.PERCEPTION_CONFIGURED: (
        NodeCapabilityStatus.AVAILABLE,
        "Selected perception profile is configured; "
        "consent, media and runtime pins apply at execution.",
    ),
    NodeCapabilityReason.PERCEPTION_UNCONFIGURED: (
        NodeCapabilityStatus.REQUIRES_CONFIGURATION,
        "Configure the selected finite perception profile through the host integration API.",
    ),
    NodeCapabilityReason.QUALIFIED_TIMELINE: (
        NodeCapabilityStatus.AVAILABLE,
        "Connect a qualified visual result for a decoded CFR video; exact source joins apply.",
    ),
    NodeCapabilityReason.PROFILE_UNAVAILABLE: (
        NodeCapabilityStatus.UNAVAILABLE,
        "The selected perception profile is unavailable. Select a configured finite profile.",
    ),
    NodeCapabilityReason.PERCEPTION_PROFILE_UNAVAILABLE: (
        NodeCapabilityStatus.UNAVAILABLE,
        "Connect a qualified visual result for Full-Reference, or use manual Plan and Compiler.",
    ),
    NodeCapabilityReason.TRANSPORT_UNCONFIGURED: (
        NodeCapabilityStatus.REQUIRES_CONFIGURATION,
        "Configure the host's official Context-IR transport before queueing this node.",
    ),
    NodeCapabilityReason.TRANSPORT_CONFIGURED: (
        NodeCapabilityStatus.AVAILABLE,
        "Transport is configured; request validation, consent and runtime credentials still apply.",
    ),
    NodeCapabilityReason.CANCELLED_BEFORE_EXECUTION: (
        NodeCapabilityStatus.CANCELLED,
        "Cancellation returns a typed result without model execution.",
    ),
}


@dataclass(frozen=True, slots=True)
class NodeCapability:
    node_id: str
    reason_code: NodeCapabilityReason

    def __post_init__(self) -> None:
        if type(self.node_id) is not str or not self.node_id.startswith("comfyui_h3_context."):
            raise ValueError("capability requires a public node identity")
        if type(self.reason_code) is not NodeCapabilityReason:
            raise ValueError("capability requires a finite reason")

    @property
    def status(self) -> NodeCapabilityStatus:
        return _DETAILS[self.reason_code][0]

    @property
    def remediation(self) -> str:
        return _DETAILS[self.reason_code][1]

    @property
    def ready(self) -> bool:
        return self.status is NodeCapabilityStatus.AVAILABLE

    def validation_result(self) -> bool | str:
        if self.ready or self.status is NodeCapabilityStatus.CANCELLED:
            return True
        return f"{self.reason_code.value}: {self.remediation}"

    def to_wire(self) -> dict[str, str | bool]:
        return {
            "node_id": self.node_id,
            "status": self.status.value,
            "reason_code": self.reason_code.value,
            "remediation": self.remediation,
            "ready": self.ready,
        }
