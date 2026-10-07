"""Which names this package promises, who promises them, and what the hub silently picks.

M19-01 recorded that "public" has three answers here and that they barely intersect.  They are not
competitors -- they sit at different levels -- but nothing said so, and their names do not overlap,
which is what made them look like rivals:

* ``comfyui_h3_context/public_api.py`` declares the **package** surface: what a host imports.
* ``comfyui_h3_context/core/__init__.py`` declares the **pure-core** surface: the contract and
  boundary primitives, with no ComfyUI, provider, runtime or media dependency.
* ``tests/acceptance_baseline.json`` names the **gate-enforced subset** of the pure-core surface,
  pinned by exact parameter inventory and checked in every Full Gate.

Stating the levels is the point.  Once they are stated, the only real constraint between them is
mechanical and this record enforces it: every gate-enforced name must be a pure-core export, because
``describe_public_callable`` resolves it through ``core.__all__`` and a name that leaves the hub
fails the gate rather than the review.

The second thing recorded here has teeth of its own.  A flat re-export list cannot express that two
modules define the same name, so the hub silently binds whichever import statement runs last.  The
live reader-backed surface has two such divergent names.  Both preserve the established binding and
expose the second definition under an explicit role-qualified alias.  This record pins each choice
with a closed disposition and rationale so a new or changed shadow fails generation until it is
reviewed deliberately.

This module reads no file and imports no host.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum

from comfyui_h3_context.core.canonical import canonical_bytes
from comfyui_h3_context.core.errors import ContractValidationError

PUBLIC_SURFACE_SCHEMA = "h3-context-public-surface/1"

MAX_AUTHORITIES = 8
MAX_NAMED_EXPORTS = 256
MAX_SHADOWED = 64
MAX_TEXT = 200
MAX_PATH_LENGTH = 260

_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_PATH_SEGMENT = r"[A-Za-z0-9._-](?:[A-Za-z0-9._ -]*[A-Za-z0-9._-])?"
_RELATIVE_PATH = re.compile(rf"{_PATH_SEGMENT}(?:/{_PATH_SEGMENT})*\Z")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class PublicSurfaceError(ContractValidationError):
    """Raised when a surface record would state a promise the package does not keep."""


class SurfaceLevel(str, Enum):
    """What a declared surface is the surface *of*.  The three are not rivals."""

    PACKAGE = "package"
    PURE_CORE = "pure_core"
    GATE_ENFORCED = "gate_enforced"


class ShadowDisposition(str, Enum):
    """Closed resolution choices for two definitions competing for one hub name."""

    ALIAS = "alias"
    NON_PUBLIC = "non_public"
    MERGE = "merge"
    REBIND = "rebind"


def _text(value: str, field_name: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicSurfaceError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise PublicSurfaceError(f"{field_name} exceeds {maximum} characters")
    return value


def _symbol(value: str, field_name: str) -> str:
    _text(value, field_name)
    if not _SYMBOL.fullmatch(value):
        raise PublicSurfaceError(f"{field_name} is not a Python identifier")
    return value


def _module(value: str, field_name: str) -> str:
    _text(value, field_name)
    if not _MODULE.fullmatch(value):
        raise PublicSurfaceError(f"{field_name} is not a dotted module name")
    return value


def _relative_path(value: str, field_name: str) -> str:
    _text(value, field_name, maximum=MAX_PATH_LENGTH)
    if value.startswith("/") or ":" in value or ".." in value.split("/"):
        raise PublicSurfaceError(f"{field_name} must be a repository-relative path")
    if not _RELATIVE_PATH.fullmatch(value):
        raise PublicSurfaceError(f"{field_name} is not a relative path")
    return value


def _count(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PublicSurfaceError(f"{field_name} must be a non-negative integer")
    return value


def _sorted_unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if list(values) != sorted(values) or len(set(values)) != len(values):
        raise PublicSurfaceError(f"{field_name} must be sorted and unique")
    return values


@dataclass(frozen=True, slots=True)
class SurfaceAuthority:
    """One file that declares a surface, and the names it declares."""

    authority_id: str
    path: str
    level: SurfaceLevel
    names: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.authority_id, "authority_id")
        if not _IDENTIFIER.fullmatch(self.authority_id):
            raise PublicSurfaceError("authority_id is not a stable lowercase identifier")
        _relative_path(self.path, "path")
        if not self.names:
            raise PublicSurfaceError("an authority must declare at least one name")
        for name in self.names:
            _symbol(name, "names")
        _sorted_unique(self.names, "names")

    @property
    def declared(self) -> int:
        return len(self.names)

    @property
    def names_digest(self) -> str:
        """A digest over the declared names, so a large surface is still pinned exactly."""

        digest = hashlib.sha256()
        for name in self.names:
            digest.update(name.encode("utf-8"))
            digest.update(b"\n")
        return "sha256:" + digest.hexdigest()

    def to_wire(self) -> dict[str, object]:
        # A surface of two thousand names is pinned by its digest rather than listed: the list is
        # bulk, it exceeds the canonical encoder's collection bound, and nothing reads it as data.
        # The small authorities are listed, because naming fourteen or fifteen things is the point.
        wire: dict[str, object] = {
            "authority_id": self.authority_id,
            "declared": self.declared,
            "level": self.level.value,
            "names_digest": self.names_digest,
            "path": self.path,
        }
        if self.declared <= MAX_NAMED_EXPORTS:
            wire["names"] = list(self.names)
        return wire


@dataclass(frozen=True, slots=True)
class ShadowedExport:
    """An exported name two modules define, where the hub binds exactly one of them.

    ``values_agree`` is the field that decides how much this matters.  Two modules agreeing on a
    bound is untidy; disagreeing means an importer gets a limit eight or sixteen times the one it
    meant to ask for, and never finds out.
    """

    name: str
    bound_origin: str
    shadowed_origin: str
    values_agree: bool
    resolved_alias: str | None = None
    disposition: ShadowDisposition | None = None
    rationale: str | None = None

    def __post_init__(self) -> None:
        _symbol(self.name, "name")
        _module(self.bound_origin, "bound_origin")
        _module(self.shadowed_origin, "shadowed_origin")
        if self.bound_origin == self.shadowed_origin:
            raise PublicSurfaceError("a shadowed export needs two different modules")
        if not isinstance(self.values_agree, bool):
            raise PublicSurfaceError("values_agree must be a boolean")
        if self.resolved_alias is not None:
            _symbol(self.resolved_alias, "resolved_alias")
            if self.resolved_alias == self.name:
                raise PublicSurfaceError("an alias that repeats the name resolves nothing")
        if self.disposition is None:
            if self.rationale is not None:
                raise PublicSurfaceError("an unresolved shadow cannot carry a rationale")
            if self.resolved_alias is not None:
                raise PublicSurfaceError("an alias requires an explicit disposition")
            return
        if not isinstance(self.disposition, ShadowDisposition):
            raise PublicSurfaceError("disposition is outside the closed vocabulary")
        if self.rationale is None:
            raise PublicSurfaceError("a disposition requires a rationale")
        _text(self.rationale, "rationale")
        if self.disposition is ShadowDisposition.ALIAS and self.resolved_alias is None:
            raise PublicSurfaceError("an alias disposition requires an exported alias")
        if self.disposition is not ShadowDisposition.ALIAS and self.resolved_alias is not None:
            raise PublicSurfaceError("only an alias disposition may name an exported alias")

    @property
    def resolved(self) -> bool:
        """True when the competing definitions have an explicit recorded disposition."""

        return self.disposition is not None

    def to_wire(self) -> dict[str, object]:
        return {
            "bound_origin": self.bound_origin,
            "disposition": self.disposition.value if self.disposition is not None else None,
            "name": self.name,
            "rationale": self.rationale,
            "resolved": self.resolved,
            "resolved_alias": self.resolved_alias,
            "shadowed_origin": self.shadowed_origin,
            "values_agree": self.values_agree,
        }


@dataclass(frozen=True, slots=True)
class PublicSurface:
    """The three authorities, the exports that need naming, and what the hub silently picks."""

    authorities: tuple[SurfaceAuthority, ...]
    shadowed: tuple[ShadowedExport, ...] = ()
    hub_consumed: int = 0
    hub_unverified: int = 0
    schema: str = PUBLIC_SURFACE_SCHEMA
    notes: tuple[str, ...] = ()
    aliased_exports: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != PUBLIC_SURFACE_SCHEMA:
            raise PublicSurfaceError("unsupported public surface schema")
        if not self.authorities:
            raise PublicSurfaceError("a surface record must name at least one authority")
        if len(self.authorities) > MAX_AUTHORITIES:
            raise PublicSurfaceError(f"more than {MAX_AUTHORITIES} authorities")
        ids = tuple(item.authority_id for item in self.authorities)
        _sorted_unique(ids, "authorities")

        levels = [item.level for item in self.authorities]
        if len(set(levels)) != len(levels):
            raise PublicSurfaceError("two authorities may not claim the same level")

        by_level = {item.level: item for item in self.authorities}
        core = by_level.get(SurfaceLevel.PURE_CORE)
        gate = by_level.get(SurfaceLevel.GATE_ENFORCED)

        # The one mechanical constraint between the levels, and the reason to record them together:
        # `describe_public_callable` resolves every gate-enforced name through `core.__all__`, so a
        # name that leaves the pure-core surface fails the Full Gate rather than a review.
        if gate is not None:
            if core is None:
                raise PublicSurfaceError("a gate-enforced surface needs a pure-core surface")
            missing = sorted(set(gate.names) - set(core.names))
            if missing:
                raise PublicSurfaceError(
                    f"gate-enforced name is not a pure-core export: {missing[0]}"
                )

        _sorted_unique(tuple(item.name for item in self.shadowed), "shadowed")
        if len(self.shadowed) > MAX_SHADOWED:
            raise PublicSurfaceError(f"more than {MAX_SHADOWED} shadowed exports")
        if core is not None:
            known = set(core.names)
            for item in self.shadowed:
                if item.name not in known:
                    raise PublicSurfaceError(f"{item.name} is shadowed but not exported")
                if item.resolved_alias is not None and item.resolved_alias not in known:
                    raise PublicSurfaceError(
                        f"{item.name} claims alias {item.resolved_alias}, which is not exported"
                    )

        _count(self.hub_consumed, "hub_consumed")
        _count(self.hub_unverified, "hub_unverified")
        if core is not None and self.hub_consumed + self.hub_unverified > core.declared:
            raise PublicSurfaceError("classified exports exceed the declared pure-core surface")
        for alias, origin in self.aliased_exports.items():
            _symbol(alias, "aliased_exports")
            _symbol(origin, "aliased_exports")
        for note in self.notes:
            _text(note, "notes")

    @property
    def unresolved_shadowed(self) -> tuple[ShadowedExport, ...]:
        return tuple(item for item in self.shadowed if not item.resolved)

    @property
    def divergent_shadowed(self) -> tuple[ShadowedExport, ...]:
        """Shadowed exports whose two definitions are not even the same value."""

        return tuple(item for item in self.shadowed if not item.values_agree)

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(canonical_bytes({"schema": self.schema}))
        digest.update(b"\n")
        for authority in self.authorities:
            digest.update(canonical_bytes(authority.to_wire()))
            digest.update(b"\n")
        for item in self.shadowed:
            digest.update(canonical_bytes(item.to_wire()))
            digest.update(b"\n")
        digest.update(
            canonical_bytes({"consumed": self.hub_consumed, "unverified": self.hub_unverified})
        )
        return "sha256:" + digest.hexdigest()

    def to_wire(self) -> dict[str, object]:
        return {
            "aliased_exports": dict(sorted(self.aliased_exports.items())),
            "authorities": [item.to_wire() for item in self.authorities],
            "fingerprint": self.fingerprint,
            "hub_consumed": self.hub_consumed,
            "hub_unverified": self.hub_unverified,
            "notes": list(self.notes),
            "schema": self.schema,
            "shadowed": [item.to_wire() for item in self.shadowed],
        }


__all__ = [
    "MAX_AUTHORITIES",
    "MAX_NAMED_EXPORTS",
    "MAX_SHADOWED",
    "PUBLIC_SURFACE_SCHEMA",
    "PublicSurface",
    "PublicSurfaceError",
    "ShadowDisposition",
    "ShadowedExport",
    "SurfaceAuthority",
    "SurfaceLevel",
]
