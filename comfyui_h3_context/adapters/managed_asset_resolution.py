"""Observe official loader combos for explicit managed readiness, without loading weights."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from ..core.canonical import canonical_fingerprint
from ..core.context_reporting import ContextReport
from ..core.native_asset_resolution import MATERIALIZATION_ASSET_ROLES, NativeAssetResolutionV1
from ..core.native_composition import NativeCompositionQualification, qualify_native_composition
from ..core.native_h3 import NativeH3Wiring
from . import comfyui_generation_profile as profile_adapter
from .comfyui_production_workspace import ProductionWorkbenchError

MAX_INVENTORY_ENTRIES = 4096
MAX_INVENTORY_BYTES = 4 * 1024 * 1024


def _observe() -> tuple[str, str, tuple[str, ...]]:
    try:
        with profile_adapter._OFFICIAL_ASSET_MANIFEST_PATH.open("rb") as stream:
            manifest_bytes = stream.read(65_537)
        if len(manifest_bytes) > 65_536:
            raise ValueError("manifest capacity")
        manifest = json.loads(manifest_bytes)
        profile_adapter._parse_official_asset_manifest(manifest)
        node_module = profile_adapter._host_module("nodes")
        nodes = getattr(node_module, "NODE_CLASS_MAPPINGS", None)
        if not isinstance(nodes, Mapping):
            raise ValueError("host node inventory")
        observed: dict[str, list[str]] = {}
        resolved = []
        budget = 0
        # IMPORTANT: inspect the same loader combo arrays as the shipped detached resolver.
        # Folder presence alone can miss a missing loader, LoRA or unresolved widget role.
        for row in manifest["slots"]:
            key = row["loader_type"] + ":" + row["widget_name"]
            if key not in observed:
                node = nodes.get(row["loader_type"])
                input_types = getattr(node, "INPUT_TYPES", None)
                if not callable(input_types):
                    raise ValueError("loader unavailable")
                specification = input_types()
                values = specification["required"][row["widget_name"]][0]
                if type(values) not in (list, tuple) or len(values) > MAX_INVENTORY_ENTRIES:
                    raise ValueError("loader inventory capacity")
                safe = []
                for value in values:
                    if type(value) is not str:
                        raise ValueError("loader inventory shape")
                    budget += len(value.encode("utf-8"))
                    if budget > MAX_INVENTORY_BYTES:
                        raise ValueError("inventory byte capacity")
                    safe.append(value)
                observed[key] = safe
            accepted = {name.casefold() for name in row["accepted_basenames"]}
            # CRITICAL: official-name matches are advice, not readiness authority. Preserve
            # unresolved roles for native queue validation instead of refusing the workflow.
            if any(
                profile_adapter._safe_inventory_entry(value)
                and profile_adapter._basename(value) in accepted
                for value in observed[key]
            ):
                resolved.append(row["slot"])
        if tuple(row["slot"] for row in manifest["slots"]) != MATERIALIZATION_ASSET_ROLES:
            raise ValueError("materialization role drift")
        # CRITICAL: loader inventories exceed the canonical contract's 256-item bound.
        # Hash every already-bounded entry with length framing before canonicalizing;
        # truncating to selected assets would miss drift in the remaining inventory.
        inventories = {}
        for key, values in observed.items():
            digest = hashlib.sha256(b"h3.managed_loader_inventory.v1\0")
            for value in values:
                encoded = value.encode("utf-8")
                digest.update(len(encoded).to_bytes(4, "big"))
                digest.update(encoded)
            inventories[key] = {"count": len(values), "sha256": "sha256:" + digest.hexdigest()}
        return (
            profile_adapter.build_generation_profile().fingerprint(),
            canonical_fingerprint({"manifest": manifest, "loader_combos": inventories}),
            tuple(resolved),
        )
    except Exception:
        # CRITICAL: neither native exceptions nor filenames from loader combos leave this seam.
        raise ProductionWorkbenchError("qualification_assets_unresolved", 409) from None


def build_managed_asset_resolution() -> NativeAssetResolutionV1:
    host_fingerprint, inventory_fingerprint, roles = _observe()
    return NativeAssetResolutionV1(host_fingerprint, inventory_fingerprint, roles, _observe)


def build_managed_native_composition(
    report: ContextReport, wiring: NativeH3Wiring, resolution: NativeAssetResolutionV1
) -> NativeCompositionQualification:
    from ..core.native_source_compatibility import qualify_native_source_compatibility

    observed = profile_adapter._observe_native_composition()
    return qualify_native_composition(
        report,
        wiring,
        host=observed.host,
        source=observed.source,
        observe_current=profile_adapter._observe_native_composition,
        asset_resolution=resolution,
        source_compatibility=qualify_native_source_compatibility(observed.source, wiring),
    )
