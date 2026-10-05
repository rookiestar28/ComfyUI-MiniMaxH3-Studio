"""Read the explicitly supplied ComfyUI host and publish its capability.

`M17-20` `D1` splits this in two on purpose. The decision -- what a host state
means for each materialization basis -- lives in the pure
`core.generation_profile`. This module only *observes*, and it observes in
process: node-class presence, bounded template structure, whether each
template's published default weight resolves, and the host's own version.

It never starts, discovers or restarts a host, never contacts a network, never
executes a model, and never puts a host filename into anything it returns. The
observation type it builds has no field a filename could travel in, so the
privacy guarantee survives a careless edit here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from ..core.context_reporting import ContextReport
from ..core.generation_profile import (
    FAMILY_ANCHOR,
    FAMILY_SLOTS,
    PINNED_TEMPLATE_DIGESTS,
    AssetSlot,
    GenerationProfile,
    HostObservation,
    ModeFamily,
    SlotDisposition,
    SlotObservation,
    TemplateCapability,
    materialization_bases,
    qualify_generation_profile,
)
from ..core.native_composition import (
    NativeCompositionObservation,
    NativeCompositionQualification,
    qualify_native_composition,
)
from ..core.native_h3 import NativeH3Wiring
from ..core.native_source_compatibility import NativeSourceObservation
from ..core.native_t2va_structure import MAX_SOURCE_BYTES, classify_native_t2va_source
from ..core.perception_producer import PerceptionProducerResult
from .comfyui_route_seam import (
    OriginRule,
    RoutePolicy,
    RouteResult,
    register_owned_route,
)

GENERATION_PROFILE_ROUTE = "/h3-context/v1/generation/profile"
GENERATION_PROFILE_SCHEMA = "h3.context.generation_profile.v1"

_ROUTE_OWNER_ATTRIBUTE = "_h3_context_route_owner"

_OFFICIAL_ASSET_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "contracts" / "official_h3_assets_v2.json"
)
_OFFICIAL_ASSET_MANIFEST_SCHEMA = "h3.context.official_assets.v2"
_OFFICIAL_ASSET_POLICY_ID = "template_default_then_manifest_order_v1"
_PROFILE_SLOT_ORDER = tuple(slot.value for slot in AssetSlot)
_MATERIALIZATION_SLOT_ORDER = _PROFILE_SLOT_ORDER + (
    "image_turbo_lora",
    "reference_turbo_lora",
)
_MATERIALIZATION_FAMILY_SLOTS = {
    ModeFamily.IMAGE_TO_VIDEO.value: (
        *(slot.value for slot in FAMILY_SLOTS[ModeFamily.IMAGE_TO_VIDEO]),
        "image_turbo_lora",
    ),
    ModeFamily.REFERENCE_TO_VIDEO.value: (
        *(slot.value for slot in FAMILY_SLOTS[ModeFamily.REFERENCE_TO_VIDEO]),
        "reference_turbo_lora",
    ),
}
_MAX_MANIFEST_NAME_LENGTH = 255
_MAX_ACCEPTED_BASENAMES = 32
_MAX_INVENTORY_ENTRY_LENGTH = 4_096
_MANIFEST_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


def _bounded_manifest_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_MANIFEST_NAME_LENGTH
        or _MANIFEST_NAME.fullmatch(value) is None
    ):
        raise RuntimeError("invalid official asset manifest")
    return value


def _parse_official_asset_manifest(
    value: object,
) -> tuple[tuple[AssetSlot, str, str, tuple[str, ...]], ...]:
    """Validate the complete cross-runtime authority and return Python rows."""

    if (
        not isinstance(value, dict)
        or value.get("schema") != _OFFICIAL_ASSET_MANIFEST_SCHEMA
        or value.get("policy_id") != _OFFICIAL_ASSET_POLICY_ID
    ):
        raise RuntimeError("invalid official asset manifest")
    slots = value.get("slots")
    profile_families = value.get("profile_families")
    materialization_families = value.get("materialization_families")
    if (
        not isinstance(slots, list)
        or len(slots) != len(_MATERIALIZATION_SLOT_ORDER)
        or not isinstance(profile_families, dict)
        or set(profile_families) != {family.value for family in ModeFamily}
        or not isinstance(materialization_families, dict)
        or set(materialization_families) != {family.value for family in ModeFamily}
    ):
        raise RuntimeError("invalid official asset manifest")

    rows: list[tuple[AssetSlot, str, str, tuple[str, ...]]] = []
    for index, raw in enumerate(slots):
        if not isinstance(raw, dict) or raw.get("slot") != _MATERIALIZATION_SLOT_ORDER[index]:
            raise RuntimeError("invalid official asset manifest")
        try:
            folder = _bounded_manifest_name(raw["folder_category"])
            _bounded_manifest_name(raw["loader_type"])
            _bounded_manifest_name(raw["widget_name"])
            default = _bounded_manifest_name(raw["template_default"])
            accepted_raw = raw["accepted_basenames"]
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("invalid official asset manifest") from error
        if (
            not isinstance(accepted_raw, list)
            or not accepted_raw
            or len(accepted_raw) > _MAX_ACCEPTED_BASENAMES
        ):
            raise RuntimeError("invalid official asset manifest")
        accepted = tuple(_bounded_manifest_name(name) for name in accepted_raw)
        if len(set(accepted)) != len(accepted) or default not in accepted:
            raise RuntimeError("invalid official asset manifest")
        if raw["slot"] in _PROFILE_SLOT_ORDER:
            rows.append((AssetSlot(raw["slot"]), folder, default, accepted))

    for family in ModeFamily:
        raw_slots = profile_families.get(family.value)
        if not isinstance(raw_slots, list):
            raise RuntimeError("invalid official asset manifest")
        try:
            declared = tuple(AssetSlot(slot) for slot in raw_slots)
        except (TypeError, ValueError) as error:
            raise RuntimeError("invalid official asset manifest") from error
        if declared != FAMILY_SLOTS[family]:
            raise RuntimeError("invalid official asset manifest")
        raw_materialization_slots = materialization_families.get(family.value)
        if (
            not isinstance(raw_materialization_slots, list)
            or tuple(raw_materialization_slots) != _MATERIALIZATION_FAMILY_SLOTS[family.value]
            or len(set(raw_materialization_slots)) != len(raw_materialization_slots)
        ):
            raise RuntimeError("invalid official asset manifest")
    return tuple(rows)


def _load_official_asset_manifest() -> tuple[tuple[AssetSlot, str, str, tuple[str, ...]], ...]:
    """Load and validate the one packaged Python/TypeScript name authority."""

    # CRITICAL: keep this package-relative and stdlib-only; importing the adapter
    # must remain safe when no ComfyUI runtime or optional dependency is present.
    try:
        payload = json.loads(_OFFICIAL_ASSET_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("invalid official asset manifest") from error
    return _parse_official_asset_manifest(payload)


#: Every official basename, derived from the packaged cross-language authority.
OFFICIAL_SLOT_ASSETS: tuple[tuple[AssetSlot, str, str, tuple[str, ...]], ...] = (
    _load_official_asset_manifest()
)

#: The published default per slot, derived so there is one authority for it.
TEMPLATE_DEFAULT_ASSETS: tuple[tuple[AssetSlot, str, str], ...] = tuple(
    (slot, folder, default) for slot, folder, default, _accepted in OFFICIAL_SLOT_ASSETS
)

ANCHOR_NODE_TYPES = ("MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo")

MAX_TEMPLATE_BYTES = 1_048_576
MAX_TEMPLATE_STRUCTURE_DEPTH = 32
MAX_TEMPLATE_STRUCTURE_ITEMS = 20_000

TEMPLATE_CAPABILITY_REQUIREMENTS = tuple(
    (template_name, FAMILY_ANCHOR[family])
    for template_name, family, _task_modes in materialization_bases()
)


def _host_module(name: str) -> object | None:
    """Return a host module only if the active host already imported it.

    Importing it here would make an optional host integration an import-time
    dependency, which the repository forbids: the package must import cleanly
    with no ComfyUI present.
    """

    return sys.modules.get(name)


def observe_host_node_types() -> frozenset[str]:
    nodes = _host_module("nodes")
    mappings = getattr(nodes, "NODE_CLASS_MAPPINGS", None)
    if not isinstance(mappings, dict):
        return frozenset()
    return frozenset(name for name in ANCHOR_NODE_TYPES if name in mappings)


def observe_host_version() -> str:
    module = _host_module("comfyui_version")
    version = getattr(module, "__version__", None)
    return version if isinstance(version, str) and version else ""


def observe_slots() -> tuple[SlotObservation, ...]:
    """Ask the host whether each slot is satisfied by an official weight, and nothing more.

    `folder_paths.get_filename_list` returns the host's inventory as `relpath`
    entries under every configured root of the category, subdirectories included.
    Only a boolean is taken from it; the list itself never leaves this function,
    which is what keeps a private filename out of every downstream value.
    """

    folder_paths = _host_module("folder_paths")
    lookup = getattr(folder_paths, "get_filename_list", None)
    observations: list[SlotObservation] = []
    for slot, folder, default, accepted in OFFICIAL_SLOT_ASSETS:
        disposition = SlotDisposition.ABSENT
        if callable(lookup):
            try:
                available = lookup(folder)
            except Exception:  # pragma: no cover - host inventory is untrusted
                available = None
            if isinstance(available, (list, tuple)):
                safe_available = tuple(entry for entry in available if _safe_inventory_entry(entry))
                # PRESENT is still the original exact-string test, and deliberately
                # so: it answers "will the value the materialized widget carries
                # resolve", and the host matches that value as a whole relative
                # path. A default installed under a subdirectory is RELOCATED,
                # because the widget would name a file the host cannot find.
                if default in safe_available:
                    disposition = SlotDisposition.PRESENT
                else:
                    wanted = frozenset(name.casefold() for name in accepted)
                    if any(_basename(entry) in wanted for entry in safe_available):
                        disposition = SlotDisposition.RELOCATED
        observations.append(SlotObservation(slot=slot, disposition=disposition))
    return tuple(observations)


def _safe_inventory_entry(value: object) -> bool:
    """Accept only bounded, portable relative host inventory paths.

    CRITICAL: basename matching must never turn an absolute, URI, traversal,
    control-bearing, or ambiguous path into an installed official asset.
    """

    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_INVENTORY_ENTRY_LENGTH
        or _CONTROL_CHARACTER.search(value) is not None
        or value.startswith(("/", "\\"))
        or _SCHEME_PREFIX.match(value) is not None
    ):
        return False
    segments = value.replace("\\", "/").split("/")
    return all(segment not in {"", ".", ".."} and ":" not in segment for segment in segments)


def _basename(entry: object) -> str:
    r"""The comparable final segment of one untrusted host inventory entry.

    CRITICAL: the host reports subdirectory files with the *platform's* separator,
    so a Windows host yields `H3\name.safetensors` and `Path(...).name` on POSIX
    would return the whole string. Both separators are normalized here rather than
    delegated, so the same host state resolves identically on either platform.

    Case is folded because supported Windows hosts use case-insensitive
    filesystems, where a case variant is the same file rather than another one.
    """

    if not isinstance(entry, str):
        return ""
    return entry.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _digest_template_file(candidate: Path) -> str | None:
    """Digest one served template for content-free provenance."""

    try:
        if not candidate.is_file() or candidate.stat().st_size > MAX_TEMPLATE_BYTES:
            return None
        payload = candidate.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(payload).hexdigest()


def _template_structure(candidate: Path) -> object | None:
    """Read one untrusted template as bounded strict UTF-8 JSON."""

    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate template member")
            result[key] = value
        return result

    def reject_nonstandard_constant(value: str) -> object:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        if not candidate.is_file() or candidate.stat().st_size > MAX_TEMPLATE_BYTES:
            return None
        payload = candidate.read_bytes()
        value: object = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_nonstandard_constant,
        )
        return value
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def _anchor_inputs(value: object, anchor_node_type: str) -> frozenset[str] | None:
    """Return inputs from matching nodes reachable from the template root.

    SECURITY: host-served templates are untrusted. Traversal has explicit depth
    and item budgets, follows only referenced subgraphs, and never retains widget
    values, prompt text or paths. A prompt socket counts only when it is STRING.
    """

    visited = 0
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_TEMPLATE_STRUCTURE_DEPTH:
            return None
        visited += 1
        if visited > MAX_TEMPLATE_STRUCTURE_ITEMS:
            return None
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)

    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
        return None

    definitions: dict[str, list[object]] = {}
    raw_definitions = value.get("definitions")
    if raw_definitions is not None:
        if not isinstance(raw_definitions, dict):
            return None
        raw_subgraphs = raw_definitions.get("subgraphs", [])
        if not isinstance(raw_subgraphs, list):
            return None
        for subgraph in raw_subgraphs:
            if not isinstance(subgraph, dict):
                return None
            subgraph_id = subgraph.get("id")
            nodes = subgraph.get("nodes")
            if not isinstance(subgraph_id, str) or not isinstance(nodes, list):
                return None
            if subgraph_id in definitions:
                return None
            definitions[subgraph_id] = nodes

    found_anchor = False
    invalid_reference = False
    names: set[str] = set()
    semantic_visits = 0
    node_groups: list[tuple[list[object], frozenset[str]]] = [(value["nodes"], frozenset())]
    while node_groups and not invalid_reference:
        nodes, active_subgraphs = node_groups.pop()
        for node in nodes:
            semantic_visits += 1
            if semantic_visits > MAX_TEMPLATE_STRUCTURE_ITEMS:
                invalid_reference = True
                break
            if not isinstance(node, dict):
                continue
            node_type = node.get("type", node.get("class_type"))
            if node_type == anchor_node_type:
                found_anchor = True
                inputs = node.get("inputs")
                if not isinstance(inputs, list):
                    continue
                for entry in inputs:
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name")
                    if not isinstance(name, str) or not 0 < len(name) <= 128:
                        continue
                    if name == "prompt" and entry.get("type") != "STRING":
                        continue
                    names.add(name)
            if not isinstance(node_type, str) or node_type not in definitions:
                continue
            if node_type in active_subgraphs:
                invalid_reference = True
                break
            node_groups.append((definitions[node_type], active_subgraphs | {node_type}))
    return frozenset(names) if found_anchor and not invalid_reference else None


def _template_capability(
    candidate: Path, template_name: str, anchor_node_type: str
) -> TemplateCapability | None:
    structure = _template_structure(candidate)
    if structure is None:
        return None
    inputs = _anchor_inputs(structure, anchor_node_type)
    if inputs is None:
        return None
    return TemplateCapability(
        template_name=template_name,
        anchor_node_type=anchor_node_type,
        input_names=inputs,
    )


def _digests_from_directory(root: Path) -> tuple[tuple[str, str], ...]:
    digests: list[tuple[str, str]] = []
    for name, _expected in PINNED_TEMPLATE_DIGESTS:
        digest = _digest_template_file(root / f"{name}.json")
        if digest is not None:
            digests.append((name, digest))
    return tuple(digests)


def _capabilities_from_directory(root: Path) -> tuple[TemplateCapability, ...]:
    capabilities: list[TemplateCapability] = []
    for name, anchor in TEMPLATE_CAPABILITY_REQUIREMENTS:
        capability = _template_capability(root / f"{name}.json", name, anchor)
        if capability is not None:
            capabilities.append(capability)
    return tuple(capabilities)


def _digests_from_asset_resolver(module: object) -> tuple[tuple[str, str], ...]:
    """Ask the templates package where each named template actually is.

    `comfyui_workflow_templates` no longer ships one `templates/` directory: from
    the multi-bundle layout on an observed host it is a thin wrapper whose JSON
    lives in a separate distribution, and its own `get_templates_path()` raises
    rather than guessing. Resolving each asset through the package is therefore
    the supported way to find the bytes, and it is what a host actually serves.
    """

    resolver = getattr(module, "get_asset_path", None)
    if not callable(resolver):
        return ()
    digests: list[tuple[str, str]] = []
    for name, _expected in PINNED_TEMPLATE_DIGESTS:
        located: object = None
        try:
            located = resolver(name, f"{name}.json")
        except Exception:  # pragma: no cover - host resolution is untrusted
            # A host that cannot resolve one template is not an error to report:
            # the digest is simply not observed, and qualification decides what
            # an unobserved template means.
            located = None
        if not isinstance(located, str) or not located:
            continue
        digest = _digest_template_file(Path(located))
        if digest is not None:
            digests.append((name, digest))
    return tuple(digests)


def _capabilities_from_asset_resolver(
    module: object,
) -> tuple[tuple[TemplateCapability, ...], frozenset[str]]:
    resolver = getattr(module, "get_asset_path", None)
    if not callable(resolver):
        return (), frozenset()
    capabilities: list[TemplateCapability] = []
    located_names: set[str] = set()
    for name, anchor in TEMPLATE_CAPABILITY_REQUIREMENTS:
        try:
            located = resolver(name, f"{name}.json")
        except Exception:  # pragma: no cover - host resolution is untrusted
            located = None
        if not isinstance(located, str) or not located:
            continue
        # SECURITY: once the host resolver names an asset, that exact asset is
        # authoritative. A malformed resolved file must not be hidden by a
        # same-name legacy fallback that the host is not actually serving.
        located_names.add(name)
        capability = _template_capability(Path(located), name, anchor)
        if capability is not None:
            capabilities.append(capability)
    return tuple(capabilities), frozenset(located_names)


def observe_template_digests(templates_root: Path | None = None) -> tuple[tuple[str, str], ...]:
    """Digest host-served templates as provenance, never as admission gates."""

    if templates_root is not None:
        return _digests_from_directory(templates_root)
    module = _host_module("comfyui_workflow_templates")
    if module is None:
        return ()
    resolved = _digests_from_asset_resolver(module)
    if resolved:
        return resolved
    location = getattr(module, "__file__", None)
    if not isinstance(location, str):
        return ()
    return _digests_from_directory(Path(location).resolve().parent / "templates")


def observe_template_capabilities(
    templates_root: Path | None = None,
) -> tuple[TemplateCapability, ...]:
    """Observe bounded, content-free splice capabilities from served templates."""

    if templates_root is not None:
        return _capabilities_from_directory(templates_root)
    module = _host_module("comfyui_workflow_templates")
    if module is None:
        return ()
    resolved, located_names = _capabilities_from_asset_resolver(module)
    location = getattr(module, "__file__", None)
    if not isinstance(location, str):
        return resolved
    legacy = _capabilities_from_directory(Path(location).resolve().parent / "templates")
    by_name = {
        capability.template_name: capability
        for capability in legacy
        if capability.template_name not in located_names
    }
    by_name.update({capability.template_name: capability for capability in resolved})
    return tuple(
        by_name[name] for name, _anchor in TEMPLATE_CAPABILITY_REQUIREMENTS if name in by_name
    )


def observe_host(templates_root: Path | None = None) -> HostObservation:
    return HostObservation(
        anchor_node_types=observe_host_node_types(),
        template_digests=observe_template_digests(templates_root),
        slots=observe_slots(),
        host_version=observe_host_version(),
        template_capabilities=observe_template_capabilities(templates_root),
    )


def build_generation_profile(templates_root: Path | None = None) -> GenerationProfile:
    return qualify_generation_profile(
        observe_host(templates_root), expected_template_digests=PINNED_TEMPLATE_DIGESTS
    )


# The audited execution surface lives in one registered native module; SigmaShift is part of it.
_NATIVE_SOURCE_CLASSES = (
    "EmptyMiniMaxH3LatentAV",
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
    "MiniMaxH3SigmaShift",
)


def _registered_native_module() -> tuple[object, tuple[type, ...]] | None:
    # CRITICAL: ComfyUI registers native modules under path-derived names. Resolve the
    # actual registered classes; a dotted alias can miss them or observe an inactive copy.
    mappings = getattr(_host_module("nodes"), "NODE_CLASS_MAPPINGS", None)
    if not isinstance(mappings, dict):
        return None
    module = None
    classes: list[type] = []
    for name in _NATIVE_SOURCE_CLASSES:
        node = mappings.get(name)
        if not isinstance(node, type):
            return None
        registered: type = node
        candidate = _host_module(getattr(registered, "__module__", ""))
        if (
            candidate is None
            or getattr(candidate, name, None) is not registered
            or (module is not None and candidate is not module)
        ):
            return None
        module = candidate
        classes.append(registered)
    return (module, tuple(classes)) if module is not None else None


def _observe_native_source() -> NativeSourceObservation | None:
    """Read the registered native module once; blob and structure share that one buffer."""
    registered = _registered_native_module()
    if registered is None:
        return None
    module, classes = registered
    location = getattr(module, "__file__", None)
    if not isinstance(location, str):
        return None
    path = Path(location)
    try:
        # IMPORTANT: only read the already-loaded native source, with a byte bound; package
        # presence, a host version or a browser-provided revision cannot qualify its behavior.
        if path.name != "nodes_minimax_h3.py" or path.stat().st_size > MAX_SOURCE_BYTES:
            return None
        with path.open("rb") as stream:
            content = stream.read(MAX_SOURCE_BYTES + 1)
            status = os.fstat(stream.fileno())
    except OSError:
        return None
    if len(content) > MAX_SOURCE_BYTES:
        return None
    blob = hashlib.sha1(
        b"blob " + str(len(content)).encode("ascii") + b"\0" + content,
        usedforsecurity=False,
    ).hexdigest()
    # CRITICAL: parse-only classification of the same bytes; host code is never imported here.
    # The origin stays in process for currentness and is never serialized or sent to a browser.
    return NativeSourceObservation(
        blob,
        classify_native_t2va_source(content),
        (module, classes, location, status.st_dev, status.st_ino),
    )


def _observe_native_composition() -> NativeCompositionObservation:
    """Read fresh loaded-source and host facts at each qualification issuance and consumption."""
    return NativeCompositionObservation(observe_host(), _observe_native_source())


def build_native_composition_qualification(
    report: ContextReport,
    wiring: NativeH3Wiring,
    *,
    media: tuple[PerceptionProducerResult, ...] = (),
) -> NativeCompositionQualification:
    """Observe the current in-process host; never import, discover or start another host."""
    from ..core.native_source_compatibility import qualify_native_source_compatibility

    observed = _observe_native_composition()
    return qualify_native_composition(
        report,
        wiring,
        host=observed.host,
        source=observed.source,
        media=media,
        observe_current=_observe_native_composition,
        source_compatibility=qualify_native_source_compatibility(observed.source, wiring),
    )


_ROUTE_POLICY = RoutePolicy(
    path=GENERATION_PROFILE_ROUTE,
    owner=GENERATION_PROFILE_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    method="GET",
    # CRITICAL: a browser omits `Origin` on a same-origin GET, so this read-only route must admit an
    # absent header. It is the one documented exception to the seam's exact-origin rule, and
    # tightening it to EXACT would make the sidebar's own page unable to read its profile.
    origin=OriginRule.EXACT_OR_ABSENT,
)


def _refusal_body(_status: int, _reason: str) -> None:
    """This route has always answered every refusal with a bodiless response."""

    return None


async def _profile(_payload: bytes, _context: object) -> RouteResult:
    return RouteResult(200, build_generation_profile().to_wire())


def ensure_generation_profile_route_registered() -> bool:
    """Register the read-only profile route against an already-owned host server."""

    return register_owned_route(_ROUTE_POLICY, __name__, _profile, _refusal_body)


__all__ = [
    "ANCHOR_NODE_TYPES",
    "GENERATION_PROFILE_ROUTE",
    "OFFICIAL_SLOT_ASSETS",
    "TEMPLATE_DEFAULT_ASSETS",
    "build_generation_profile",
    "build_native_composition_qualification",
    "ensure_generation_profile_route_registered",
    "observe_host",
    "observe_host_node_types",
    "observe_host_version",
    "observe_slots",
    "observe_template_digests",
    "observe_template_capabilities",
]
