"""Generate the M19-03 node surface record by asking the classes what a ComfyUI host would ask.

This generator imports the package and calls ``INPUT_TYPES()``, which is deliberate and is the only
honest way to build this record: the point is to capture what a host receives, and a host receives
the result of running that classmethod, not a static reading of the source.  ``nodes.py`` imports no
host runtime, so calling it costs nothing but the package import.

Every attribute below is read the way the host's own node-info route reads it, defaults included,
so an attribute that is merely absent records the value the host would compute rather than a hole.

The record is a *baseline*.  Generate it before an adapter move, regenerate after, and byte-equality
is the proof.  If it differs, the move was wrong.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.governance.node_surface import (  # noqa: E402
    NodeRecord,
    NodeSurface,
    NodeSurfaceError,
    SocketRecord,
    SocketSection,
)

ARTIFACT_PATH = Path("governance/contracts/node_surface_v1.json")

#: A bare string declaration is ComfyUI's hidden-input convention: the string *is* the socket type.
_SECTIONS = {
    "required": SocketSection.REQUIRED,
    "optional": SocketSection.OPTIONAL,
    "hidden": SocketSection.HIDDEN,
}


def _socket(name: str, section: SocketSection, declaration: object) -> SocketRecord:
    """Reduce one declaration to a comparable record, refusing whatever it cannot represent."""

    if isinstance(declaration, str):
        return SocketRecord(name=name, section=section, socket_type=declaration)
    if not isinstance(declaration, tuple) or not declaration:
        raise NodeSurfaceError(f"socket {name!r} has an unrecognized declaration")

    head = declaration[0]
    choices: tuple[str, ...] | None = None
    if isinstance(head, str):
        socket_type = head
    elif isinstance(head, list | tuple):
        # An inline choice list. The literal values are the type as far as a host is concerned, so
        # they are recorded in order rather than collapsed to the word COMBO.
        if not all(isinstance(item, str) for item in head):
            raise NodeSurfaceError(f"socket {name!r} declares a non-string choice")
        socket_type = "COMBO"
        choices = tuple(head)
    else:
        raise NodeSurfaceError(f"socket {name!r} declares a {type(head).__name__} socket type")

    options: dict[str, object] = {}
    if len(declaration) > 1:
        raw = declaration[1]
        if not isinstance(raw, dict):
            raise NodeSurfaceError(f"socket {name!r} declares non-dict options")
        options = dict(raw)
    if len(declaration) > 2:
        raise NodeSurfaceError(f"socket {name!r} declares more than a type and an options dict")

    return SocketRecord(
        name=name,
        section=section,
        socket_type=socket_type,
        options=options,
        choices=choices,
    )


def _node(node_id: str, node_class: type[object], display_name: str) -> NodeRecord:
    spec = node_class.INPUT_TYPES()  # type: ignore[attr-defined]
    if not isinstance(spec, dict):
        raise NodeSurfaceError(f"node {node_id!r} did not return an INPUT_TYPES mapping")

    sockets: list[SocketRecord] = []
    declared: list[tuple[str, tuple[str, ...]]] = []
    for section_name, entries in spec.items():
        section = _SECTIONS.get(section_name)
        if section is None:
            raise NodeSurfaceError(f"node {node_id!r} declares unknown section {section_name!r}")
        if not isinstance(entries, dict):
            raise NodeSurfaceError(f"node {node_id!r} section {section_name!r} is not a mapping")
        for socket_name, declaration in entries.items():
            sockets.append(_socket(socket_name, section, declaration))
        # Captured before any sorting: the host ships this verbatim as `input_order`.
        declared.append((section.value, tuple(entries)))
    # Sockets sort so the record is diffable; `input_order` above is what preserves the contract.
    sockets.sort(key=lambda socket: (socket.section.value, socket.name))

    description = getattr(node_class, "DESCRIPTION", None)
    return_types = tuple(str(item) for item in node_class.RETURN_TYPES)  # type: ignore[attr-defined]
    tooltips = getattr(node_class, "OUTPUT_TOOLTIPS", None)
    return NodeRecord(
        node_id=node_id,
        display_name=display_name,
        class_name=node_class.__name__,
        category=str(node_class.CATEGORY),  # type: ignore[attr-defined]
        function=str(node_class.FUNCTION),  # type: ignore[attr-defined]
        return_types=return_types,
        return_names=tuple(str(item) for item in node_class.RETURN_NAMES),  # type: ignore[attr-defined]
        sockets=tuple(sockets),
        input_order=tuple(declared),
        output_node=(bool(node_class.OUTPUT_NODE) if hasattr(node_class, "OUTPUT_NODE") else None),
        # Stored, not hashed: 28 descriptions total 2.5 kB, and an opaque digest would tell a
        # reviewer only that something changed -- the same argument the float encoding makes.
        description=description if isinstance(description, str) else None,
        # Read exactly the way the host reads them, defaults included, so an attribute that is
        # merely absent records the same value the host would compute for it.
        input_is_list=bool(getattr(node_class, "INPUT_IS_LIST", False)),
        output_is_list=tuple(
            bool(flag)
            for flag in getattr(node_class, "OUTPUT_IS_LIST", [False] * len(return_types))
        ),
        output_tooltips=(tuple(str(item) for item in tooltips) if tooltips is not None else None),
        search_aliases=tuple(str(item) for item in getattr(node_class, "SEARCH_ALIASES", ())),
        essentials_category=(
            str(node_class.ESSENTIALS_CATEGORY)
            if hasattr(node_class, "ESSENTIALS_CATEGORY")
            else None
        ),
        deprecated=bool(getattr(node_class, "DEPRECATED", False)),
        experimental=bool(getattr(node_class, "EXPERIMENTAL", False)),
        dev_only=bool(getattr(node_class, "DEV_ONLY", False)),
        has_intermediate_output=bool(getattr(node_class, "HAS_INTERMEDIATE_OUTPUT", False)),
        api_node=(bool(node_class.API_NODE) if hasattr(node_class, "API_NODE") else None),
        validates_inputs=hasattr(node_class, "VALIDATE_INPUTS"),
        declares_is_changed=hasattr(node_class, "IS_CHANGED"),
    )


def build_surface() -> NodeSurface:
    from comfyui_h3_context import nodes as nodes_module

    class_mappings = nodes_module.NODE_CLASS_MAPPINGS
    display_mappings = nodes_module.NODE_DISPLAY_NAME_MAPPINGS
    if set(class_mappings) != set(display_mappings):
        raise NodeSurfaceError("class and display mappings do not name the same nodes")

    records = []
    for node_id, node_class in class_mappings.items():
        declared = getattr(node_class, "__h3_context_node_id__", None)
        if declared != node_id:
            raise NodeSurfaceError(
                f"node {node_id!r} is registered under a class that declares {declared!r}"
            )
        records.append(_node(node_id, node_class, display_mappings[node_id]))
    records.sort(key=lambda record: record.node_id)

    return NodeSurface(
        nodes=tuple(records),
        mapping_order=tuple(class_mappings),
        exported_names=tuple(sorted(set(nodes_module.__all__))),
    )


def artifact_bytes(surface: NodeSurface) -> bytes:
    document = json.dumps(surface.to_wire(), ensure_ascii=False, indent=2, sort_keys=True)
    return (document + "\n").encode("utf-8")


def summary(surface: NodeSurface) -> dict[str, object]:
    return {
        "schema": surface.schema,
        "node_count": len(surface.nodes),
        "socket_count": surface.socket_count,
        "exported_names": len(surface.exported_names),
        "fingerprint": surface.fingerprint,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the artifact")
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale")
    args = parser.parse_args(argv)
    try:
        surface = build_surface()
    except (NodeSurfaceError, OSError, ValueError, AttributeError) as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc)}, ensure_ascii=False))
        return 1
    expected = artifact_bytes(surface)
    target = ROOT / ARTIFACT_PATH
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
    if args.check and (target.read_bytes() if target.is_file() else b"") != expected:
        print(
            json.dumps(
                {"status": "FAIL", "detail": "node surface artifact is stale"}, ensure_ascii=False
            )
        )
        return 1
    print(json.dumps({"status": "PASS", **summary(surface)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
