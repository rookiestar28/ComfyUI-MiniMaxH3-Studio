"""V1 ComfyUI registration surface with atomic namespaced collision checks.

The package root exports the product mapping without importing ``comfy_api`` or any optional
runtime. The historical M0-07 probe remains available as an unregistered harness fixture; it is
not exposed as a product node.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping

from .core.errors import RegistrationConflictError, RegistrationProbeError
from .core.public_manifest import PublicManifest
from .nodes import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
)

REGISTRATION_NAMESPACE = "comfyui_h3_context"
REGISTRATION_NODE_ID = f"{REGISTRATION_NAMESPACE}.H3Context.RegistrationProbe"
REGISTRATION_DISPLAY_NAME = "H3 Context (registration probe)"
_MISSING = object()


class RegistrationProbe:
    """Non-product node used only to verify custom-node discovery and collision handling."""

    __h3_context_node_id__ = REGISTRATION_NODE_ID
    RETURN_TYPES: tuple[str, ...] = ()
    FUNCTION = "execute"
    CATEGORY = "h3_context/internal"
    EXPERIMENTAL = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, ...]]]:
        """Expose no inputs; M0-07 must not imply a product contract."""

        return {"required": {}}

    def execute(self) -> tuple[object, ...]:
        """Fail closed because the M0-07 probe is retired and never a product node."""

        raise RegistrationProbeError(
            "H3 Context registration probe is retired and is not executable."
        )


def _is_compatible_node(value: object, node_id: str, expected: object) -> bool:
    return value is expected or (
        isinstance(value, type) and getattr(value, "__h3_context_node_id__", None) == node_id
    )


def register_nodes(
    node_registry: MutableMapping[str, object],
    display_registry: MutableMapping[str, str],
) -> None:
    """Register product nodes once and reject all collisions before either mapping mutates."""

    manifest = get_public_manifest()
    if set(manifest.node_ids) != set(NODE_CLASS_MAPPINGS):
        raise RegistrationConflictError("canonical registration drifted from the public manifest")
    _register_mapping(
        node_registry,
        display_registry,
        NODE_CLASS_MAPPINGS,
        NODE_DISPLAY_NAME_MAPPINGS,
    )


def get_public_manifest() -> PublicManifest:
    """Return the verified canonical public surface without exposing a second mapping."""

    from .public_api import get_public_manifest as build_manifest

    return build_manifest()


def register_registration_probe(
    node_registry: MutableMapping[str, object],
    display_registry: MutableMapping[str, str],
) -> None:
    """Register the retired M0-07 probe only for an explicit harness test."""

    _register_mapping(
        node_registry,
        display_registry,
        {REGISTRATION_NODE_ID: RegistrationProbe},
        {REGISTRATION_NODE_ID: REGISTRATION_DISPLAY_NAME},
    )


def _register_mapping(
    node_registry: MutableMapping[str, object],
    display_registry: MutableMapping[str, str],
    node_mapping: Mapping[str, object],
    display_mapping: Mapping[str, str],
) -> None:
    """Preflight one namespaced mapping and mutate only after every collision is cleared."""

    for node_id, node_class in node_mapping.items():
        existing_node = node_registry.get(node_id, _MISSING)
        if existing_node is not _MISSING and not _is_compatible_node(
            existing_node, node_id, node_class
        ):
            raise RegistrationConflictError(
                f"node ID {node_id!r} is already owned by a foreign class"
            )
        existing_display = display_registry.get(node_id)
        expected_display = display_mapping.get(node_id)
        if existing_display is not None and existing_display != expected_display:
            raise RegistrationConflictError(
                f"display name for {node_id!r} is already owned by another registration"
            )

    for node_id, node_class in node_mapping.items():
        if node_id not in node_registry:
            node_registry[node_id] = node_class
        if node_id not in display_registry:
            display_registry[node_id] = display_mapping[node_id]


__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "REGISTRATION_DISPLAY_NAME",
    "REGISTRATION_NAMESPACE",
    "REGISTRATION_NODE_ID",
    "RegistrationProbe",
    "get_public_manifest",
    "register_registration_probe",
    "register_nodes",
]
