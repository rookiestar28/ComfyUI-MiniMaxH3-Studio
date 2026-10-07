"""What a ComfyUI host can observe about this package's nodes, frozen as a comparable record.

The host never reads a Python module layout.  It reads ``NODE_CLASS_MAPPINGS``, asks each class for
``INPUT_TYPES()``, and reads a handful of class attributes.  That set -- not the file a class
happens to live in -- is the compatibility contract, and until this record existed the repository
had no way to state it: the declarative registry in ``node_contracts`` describes intended contracts,
the loader test checks that mappings are exported at all, and the per-node suites each see one node.
Nothing covered the mapping as a whole.

So this record is what makes a large adapter move safe to perform.  Take it before the move, take it
again after, and byte-equality is the proof that nothing a host can see changed.  A record that
differs means the move was wrong -- never that the record needs updating.

What "observable" means is settled against the host's own source rather than by intuition.  The
route that serves node definitions builds ``input_order`` straight from ``INPUT_TYPES()`` key order
and reads ``INPUT_IS_LIST``, ``OUTPUT_IS_LIST``, ``OUTPUT_TOOLTIPS``, ``DEPRECATED``,
``EXPERIMENTAL``, ``DEV_ONLY``, ``API_NODE``, ``SEARCH_ALIASES`` and ``ESSENTIALS_CATEGORY``, so all
of them are recorded.  An earlier version of this module recorded none of them and sorted sockets
by name on the reasoning that a host lays inputs out from names; that reasoning was wrong, and the
frontend reconciles a saved workflow's positional widget values against the declared order, which
makes socket order exactly the kind of thing an old workflow breaks on.

Two further details decide whether the comparison is honest.  Choice order is preserved, because a
reordered dropdown is a visible change to a user even though the choice *set* is identical.  And a
socket whose declaration this module cannot represent raises instead of being flattened into
something comparable, because a normalizer that quietly discards what it does not understand
reports equality it did not establish.

This module imports no host and reads no file.  It is handed already-extracted values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from comfyui_h3_context.core.canonical import canonical_bytes, canonical_fingerprint
from comfyui_h3_context.core.errors import ContractValidationError

NODE_SURFACE_SCHEMA = "h3-context-node-surface/1"

MAX_NODES = 64
MAX_SOCKETS = 64
MAX_CHOICES = 64
MAX_RETURNS = 32
MAX_TEXT = 512
MAX_NODE_ID = 200

_NODE_ID = re.compile(r"comfyui_h3_context\.[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_SOCKET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CATEGORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]*(?:/[A-Za-z0-9][A-Za-z0-9 _.-]*){0,3}\Z")


class NodeSurfaceError(ContractValidationError):
    """Raised when a node surface record would misstate what a host can observe."""


class SocketSection(str, Enum):
    """Which ``INPUT_TYPES()`` section a socket was declared in.  Hosts treat these differently."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    HIDDEN = "hidden"


def _float_wire(value: float) -> dict[str, str]:
    """Record a float as its shortest round-tripping decimal, tagged so it cannot read as a string.

    ``canonical_bytes`` refuses a raw float outright, and the package-wide answer to that is
    ``binary64_token``.  It is the right answer for a fingerprint projection and the wrong one here:
    a reviewer checking that a duration ceiling survived a refactor needs to see ``149.687``, not
    ``4062ec1a36e2eb1c``.  This record carried both for a while, until it became clear the token was
    buying nothing -- ``repr`` is the shortest string that round-trips to the identical double, so
    the decimal *is* the exact value, not a rendering of it.  Dropping the token also stops a
    16-digit hex string in a shipped artifact from reading as a leaked credential to a scanner.

    The ``float`` key is what keeps ``"149.687"`` from being mistaken for a socket option that
    genuinely holds the string ``"149.687"``.
    """

    return {"float": repr(value)}


def _text(value: str, field_name: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NodeSurfaceError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise NodeSurfaceError(f"{field_name} exceeds {maximum} characters")
    return value


def _option_value(value: object, field_name: str) -> str | int | float | bool | list[object]:
    """Keep a JSON-safe option, in order.  Refuse anything a record cannot faithfully compare."""

    if isinstance(value, bool | int | float | str):
        if isinstance(value, str) and len(value) > MAX_TEXT:
            raise NodeSurfaceError(f"{field_name} exceeds {MAX_TEXT} characters")
        return value
    if isinstance(value, list | tuple):
        if len(value) > MAX_CHOICES:
            raise NodeSurfaceError(f"{field_name} declares more than {MAX_CHOICES} choices")
        return [_option_value(item, f"{field_name}[]") for item in value]
    raise NodeSurfaceError(f"{field_name} is a {type(value).__name__}, which cannot be recorded")


def _wire_option(value: object) -> object:
    """Project one validated option onto the wire, encoding floats and leaving the rest alone."""

    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return _float_wire(value)
    if isinstance(value, list | tuple):
        return [_wire_option(item) for item in value]
    raise NodeSurfaceError(f"a {type(value).__name__} option cannot be projected")


@dataclass(frozen=True, slots=True)
class SocketRecord:
    """One declared input socket, reduced to what a host reads and a diff can compare."""

    name: str
    section: SocketSection
    socket_type: str
    options: dict[str, object] = field(default_factory=dict)
    choices: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _text(self.name, "socket.name", maximum=MAX_NODE_ID)
        if not _SOCKET_NAME.fullmatch(self.name):
            raise NodeSurfaceError(f"socket name {self.name!r} is not an identifier")
        if not isinstance(self.section, SocketSection):
            raise NodeSurfaceError("socket.section must be a SocketSection")
        _text(self.socket_type, "socket.socket_type", maximum=MAX_NODE_ID)
        if len(self.options) > MAX_SOCKETS:
            raise NodeSurfaceError(f"socket {self.name!r} declares too many options")
        for key, value in self.options.items():
            if not isinstance(key, str) or not _SOCKET_NAME.fullmatch(key):
                raise NodeSurfaceError(f"socket {self.name!r} has a non-identifier option key")
            _option_value(value, f"socket.{self.name}.{key}")
        if self.choices is not None:
            if not self.choices:
                raise NodeSurfaceError(f"socket {self.name!r} declares an empty choice list")
            if len(self.choices) > MAX_CHOICES:
                raise NodeSurfaceError(f"socket {self.name!r} declares too many choices")
            if len(set(self.choices)) != len(self.choices):
                raise NodeSurfaceError(f"socket {self.name!r} repeats a choice")

    def to_wire(self) -> dict[str, object]:
        """Choices stay in declaration order: a reordered dropdown is a visible change."""

        wire: dict[str, object] = {
            "name": self.name,
            "section": self.section.value,
            "socket_type": self.socket_type,
        }
        if self.options:
            wire["options"] = {
                key: _wire_option(value) for key, value in sorted(self.options.items())
            }
        if self.choices is not None:
            wire["choices"] = list(self.choices)
        return wire


@dataclass(frozen=True, slots=True)
class NodeRecord:
    """Everything a ComfyUI host can observe about one registered node."""

    node_id: str
    display_name: str
    class_name: str
    category: str
    function: str
    return_types: tuple[str, ...]
    return_names: tuple[str, ...]
    sockets: tuple[SocketRecord, ...]
    #: Per section, the order ``INPUT_TYPES()`` declared its keys in.  ComfyUI ships this verbatim
    #: as ``input_order``; the frontend lays widgets out from it and reconciles a saved workflow's
    #: positional ``widgets_values`` against it.  Sorting sockets for readability would throw away
    #: exactly the thing an old workflow depends on, so it is kept separately and fingerprinted.
    input_order: tuple[tuple[str, tuple[str, ...]], ...] = ()
    output_node: bool | None = None
    description: str | None = None
    #: ``getattr(cls, "INPUT_IS_LIST", False)`` -- changes the host's calling convention.
    input_is_list: bool = False
    #: ``OUTPUT_IS_LIST`` when declared, else ComfyUI's all-False default of matching length.
    output_is_list: tuple[bool, ...] = ()
    output_tooltips: tuple[str, ...] | None = None
    search_aliases: tuple[str, ...] = ()
    essentials_category: str | None = None
    #: Presence-only flags ComfyUI reads through ``getattr(cls, NAME, False)``.
    deprecated: bool = False
    experimental: bool = False
    dev_only: bool = False
    has_intermediate_output: bool = False
    api_node: bool | None = None
    #: Execution hooks.  ComfyUI calls them when they exist, so their presence is host-visible even
    #: though it never appears in object-info.
    validates_inputs: bool = False
    declares_is_changed: bool = False

    def __post_init__(self) -> None:
        _text(self.node_id, "node.node_id", maximum=MAX_NODE_ID)
        if not _NODE_ID.fullmatch(self.node_id):
            raise NodeSurfaceError(f"node ID {self.node_id!r} is not namespaced to this package")
        _text(self.display_name, "node.display_name")
        _text(self.class_name, "node.class_name", maximum=MAX_NODE_ID)
        if not _SYMBOL.fullmatch(self.class_name):
            raise NodeSurfaceError(f"class name {self.class_name!r} is not an identifier")
        _text(self.category, "node.category", maximum=MAX_NODE_ID)
        if not _CATEGORY.fullmatch(self.category):
            raise NodeSurfaceError(f"category {self.category!r} is not a category path")
        _text(self.function, "node.function", maximum=MAX_NODE_ID)
        if len(self.return_types) > MAX_RETURNS:
            raise NodeSurfaceError(f"node {self.node_id!r} declares too many outputs")
        if len(self.return_types) != len(self.return_names):
            raise NodeSurfaceError(
                f"node {self.node_id!r} declares {len(self.return_types)} return types "
                f"but {len(self.return_names)} return names"
            )
        if self.output_is_list and len(self.output_is_list) != len(self.return_types):
            raise NodeSurfaceError(
                f"node {self.node_id!r} declares {len(self.output_is_list)} output_is_list flags "
                f"for {len(self.return_types)} outputs"
            )
        if self.output_tooltips is not None and len(self.output_tooltips) != len(self.return_types):
            raise NodeSurfaceError(
                f"node {self.node_id!r} declares output tooltips for the wrong number of outputs"
            )
        if len(self.sockets) > MAX_SOCKETS:
            raise NodeSurfaceError(f"node {self.node_id!r} declares too many sockets")
        names = [socket.name for socket in self.sockets]
        if len(set(names)) != len(names):
            raise NodeSurfaceError(f"node {self.node_id!r} declares a duplicate socket name")
        # The declared order must account for every socket exactly once, or the record would be
        # claiming an ordering it does not actually cover.
        ordered: list[str] = []
        for section, section_names in self.input_order:
            if section not in {member.value for member in SocketSection}:
                raise NodeSurfaceError(f"node {self.node_id!r} orders unknown section {section!r}")
            ordered.extend(section_names)
        if self.input_order and sorted(ordered) != sorted(names):
            raise NodeSurfaceError(
                f"node {self.node_id!r} declares an input order that does not match its sockets"
            )

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "node_id": self.node_id,
            "display_name": self.display_name,
            "class_name": self.class_name,
            "category": self.category,
            "function": self.function,
            "return_types": list(self.return_types),
            "return_names": list(self.return_names),
            "sockets": [socket.to_wire() for socket in self.sockets],
            "input_order": {section: list(names) for section, names in self.input_order},
            "input_is_list": self.input_is_list,
            "output_is_list": list(self.output_is_list),
        }
        if self.output_node is not None:
            wire["output_node"] = self.output_node
        if self.description is not None:
            wire["description"] = self.description
        if self.output_tooltips is not None:
            wire["output_tooltips"] = list(self.output_tooltips)
        if self.search_aliases:
            wire["search_aliases"] = list(self.search_aliases)
        if self.essentials_category is not None:
            wire["essentials_category"] = self.essentials_category
        if self.api_node is not None:
            wire["api_node"] = self.api_node
        for name, value in (
            ("deprecated", self.deprecated),
            ("experimental", self.experimental),
            ("dev_only", self.dev_only),
            ("has_intermediate_output", self.has_intermediate_output),
            ("validates_inputs", self.validates_inputs),
            ("declares_is_changed", self.declares_is_changed),
        ):
            if value:
                wire[name] = True
        return wire


@dataclass(frozen=True, slots=True)
class NodeSurface:
    """The registered node surface as a whole, including the order the mapping declares it in."""

    nodes: tuple[NodeRecord, ...]
    mapping_order: tuple[str, ...]
    exported_names: tuple[str, ...] = ()
    schema: str = NODE_SURFACE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != NODE_SURFACE_SCHEMA:
            raise NodeSurfaceError(f"unsupported node surface schema {self.schema!r}")
        if not self.nodes:
            raise NodeSurfaceError("a node surface with no nodes records nothing")
        if len(self.nodes) > MAX_NODES:
            raise NodeSurfaceError(f"a node surface may not exceed {MAX_NODES} nodes")
        ids = [node.node_id for node in self.nodes]
        if len(set(ids)) != len(ids):
            raise NodeSurfaceError("a node surface repeats a node ID")
        if sorted(ids) != ids:
            raise NodeSurfaceError("node records must be sorted by node ID")
        # Mapping order is separate from record order on purpose: the records sort so a diff is
        # readable, and the order the host actually receives is recorded next to them.
        if sorted(self.mapping_order) != sorted(ids):
            raise NodeSurfaceError("mapping order does not name exactly the recorded nodes")
        display = [node.display_name for node in self.nodes]
        if len(set(display)) != len(display):
            raise NodeSurfaceError("two nodes claim the same display name")
        classes = [node.class_name for node in self.nodes]
        if len(set(classes)) != len(classes):
            raise NodeSurfaceError("two node IDs resolve to the same class name")
        if len(set(self.exported_names)) != len(self.exported_names):
            raise NodeSurfaceError("exported names repeat")
        if sorted(self.exported_names) != list(self.exported_names):
            raise NodeSurfaceError("exported names must be sorted")

    @property
    def socket_count(self) -> int:
        return sum(len(node.sockets) for node in self.nodes)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "nodes": [node.to_wire() for node in self.nodes],
            "mapping_order": list(self.mapping_order),
            "exported_names": list(self.exported_names),
            "fingerprint": self.fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        """Covers the nodes and the order the host sees them in, not the record's own framing."""

        return canonical_fingerprint(
            {
                "schema": self.schema,
                "nodes": [node.to_wire() for node in self.nodes],
                "mapping_order": list(self.mapping_order),
                "exported_names": list(self.exported_names),
            }
        )

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_bytes(
            {
                "schema": self.schema,
                "nodes": [node.to_wire() for node in self.nodes],
                "mapping_order": list(self.mapping_order),
            }
        )


__all__ = [
    "MAX_CHOICES",
    "MAX_NODES",
    "MAX_RETURNS",
    "MAX_SOCKETS",
    "NODE_SURFACE_SCHEMA",
    "NodeRecord",
    "NodeSurface",
    "NodeSurfaceError",
    "SocketRecord",
    "SocketSection",
]
