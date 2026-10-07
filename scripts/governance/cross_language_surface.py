"""The one structural authority for shapes that cross the Python/TypeScript boundary.

Six browser codecs declare, by hand, the key set of a wire that Python already declares in a
``to_wire`` method.  Two declarations of one shape in two languages is not a contract; it is two
contracts that happen to agree today.  This module makes the Python declaration the authority and
derives the browser's key set from it, so drift becomes a build failure instead of a bug a user
finds.

The extractor reads a module **as data**.  It parses the source with :mod:`ast` and never imports or
executes it, which is the same posture the M18-01 inventory generator takes and for the same reason:
a tool that must run product code to describe it can be defeated by the code it is describing.

It also **fails closed**.  A ``to_wire`` whose keys are not string literals -- built in a loop,
keyed by a variable, spread from another mapping -- is recorded as unresolved and stops
generation.  A
partially-extracted key set would be worse than none, because a closed-shape check derived from it
would reject valid payloads.

The authority is the runtime producer, not a schema, and that choice is forced rather than
preferred: only five of the fourteen cross-language wires have a JSON Schema at all, while every one
of them has a ``to_wire``.  Where a schema does exist it is checked *against* this extraction rather
than treated as a second authority.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from comfyui_h3_context.core.canonical import canonical_bytes
from comfyui_h3_context.core.contract_inventory import FORBIDDEN_RECORD_TEXT
from comfyui_h3_context.core.errors import ContractValidationError

CROSS_LANGUAGE_SURFACE_SCHEMA = "h3-context-cross-language-surface/1"
MAX_SHAPES = 256
MAX_KEYS = 128
MAX_TEXT = 240
MAX_PATH_LENGTH = 260

_WIRE_METHOD = "to_wire"
_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_CLASS_NAME = re.compile(r"[A-Z][A-Za-z0-9]{0,63}\Z")
_EXPORT_NAME = re.compile(r"[a-z][A-Za-z0-9]{0,63}\Z")
_PATH_SEGMENT = r"[A-Za-z0-9._-](?:[A-Za-z0-9._ -]*[A-Za-z0-9._-])?"
_RELATIVE_PATH = re.compile(rf"{_PATH_SEGMENT}(?:/{_PATH_SEGMENT})*\Z")


class CrossLanguageSurfaceError(ContractValidationError):
    """Raised when a shape cannot be resolved, or a record claims more than its extraction."""


def _text(value: object, field_name: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CrossLanguageSurfaceError(f"{field_name} must be bounded non-empty text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise CrossLanguageSurfaceError(f"{field_name} contains a control character")
    if FORBIDDEN_RECORD_TEXT.search(value):
        raise CrossLanguageSurfaceError(
            f"{field_name} looks like a private path, URL or credential"
        )
    return value


def _relative_path(value: object, field_name: str) -> str:
    text = _text(value, field_name, maximum=MAX_PATH_LENGTH)
    if _RELATIVE_PATH.fullmatch(text) is None or ".." in text.split("/"):
        raise CrossLanguageSurfaceError(f"{field_name} must be a safe repository-relative path")
    return text


def _keys(value: object, field_name: str, *, required: bool) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > MAX_KEYS:
        raise CrossLanguageSurfaceError(f"{field_name} must be a bounded tuple")
    if required and not value:
        raise CrossLanguageSurfaceError(f"{field_name} must not be empty")
    for item in value:
        if not isinstance(item, str) or _KEY.fullmatch(item) is None:
            raise CrossLanguageSurfaceError(f"{field_name} contains an invalid wire key")
    if len(set(value)) != len(value):
        raise CrossLanguageSurfaceError(f"{field_name} contains a duplicate key")
    if list(value) != sorted(value):
        raise CrossLanguageSurfaceError(f"{field_name} must be sorted")
    return value


@dataclass(frozen=True, slots=True)
class WireShape:
    """One declared shape: where it is declared, and exactly which keys it emits."""

    #: The browser export this shape is the authority for.  One shape, one export, so a reader can
    #: go from a codec's key list to the Python method that decides it without searching.
    export_name: str
    module_path: str
    class_name: str
    required_keys: tuple[str, ...]
    #: Keys a `to_wire` adds conditionally.  They are part of the closed shape -- the browser must
    #: accept them -- but their absence is not an error, so they are recorded apart.
    optional_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        name = _text(self.export_name, "export_name", maximum=64)
        if _EXPORT_NAME.fullmatch(name) is None:
            raise CrossLanguageSurfaceError("export_name must be a lowerCamelCase identifier")
        _relative_path(self.module_path, "module_path")
        class_name = _text(self.class_name, "class_name", maximum=64)
        if _CLASS_NAME.fullmatch(class_name) is None:
            raise CrossLanguageSurfaceError("class_name must be a CapWords identifier")
        object.__setattr__(
            self, "required_keys", _keys(self.required_keys, "required_keys", required=True)
        )
        object.__setattr__(
            self, "optional_keys", _keys(self.optional_keys, "optional_keys", required=False)
        )
        overlap = set(self.required_keys) & set(self.optional_keys)
        if overlap:
            raise CrossLanguageSurfaceError(
                f"a key cannot be both required and optional: {sorted(overlap)[0]}"
            )

    @property
    def all_keys(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.required_keys) | set(self.optional_keys)))

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "export_name": self.export_name,
            "module_path": self.module_path,
            "class_name": self.class_name,
            "required_keys": list(self.required_keys),
        }
        if self.optional_keys:
            wire["optional_keys"] = list(self.optional_keys)
        return wire


@dataclass(frozen=True, slots=True)
class CrossLanguageSurface:
    """Every frozen cross-language shape, once, in one order."""

    shapes: tuple[WireShape, ...] = field(default=())
    schema: str = CROSS_LANGUAGE_SURFACE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CROSS_LANGUAGE_SURFACE_SCHEMA:
            raise CrossLanguageSurfaceError("unsupported cross-language surface schema")
        if not isinstance(self.shapes, tuple) or not self.shapes or len(self.shapes) > MAX_SHAPES:
            raise CrossLanguageSurfaceError("shapes are outside the bounded limit")
        if not all(isinstance(item, WireShape) for item in self.shapes):
            raise CrossLanguageSurfaceError("shapes contain an invalid value")
        exports = [item.export_name for item in self.shapes]
        if len(set(exports)) != len(exports):
            raise CrossLanguageSurfaceError("an export names more than one shape")
        if exports != sorted(exports):
            raise CrossLanguageSurfaceError("shapes must be sorted by export name")

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(canonical_bytes({"schema": self.schema}))
        digest.update(b"\n")
        for item in self.shapes:
            digest.update(canonical_bytes(item.to_wire()))
            digest.update(b"\n")
        return "sha256:" + digest.hexdigest()

    def shape(self, export_name: str) -> WireShape:
        for item in self.shapes:
            if item.export_name == export_name:
                return item
        raise CrossLanguageSurfaceError(f"no shape is declared for {export_name!r}")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "shapes": [item.to_wire() for item in self.shapes],
            "fingerprint": self.fingerprint,
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "shape_count": len(self.shapes),
            "key_count": sum(len(item.all_keys) for item in self.shapes),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ExtractedShape:
    """One `to_wire` as the parser found it, before any export is attached to it."""

    class_name: str
    required_keys: tuple[str, ...]
    optional_keys: tuple[str, ...]
    #: Non-empty when the parser could not resolve the shape.  A caller must refuse it rather than
    #: use the partial key set: a closed-shape check built from half a shape rejects valid payloads.
    unresolved: tuple[str, ...]


def _literal_key(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def extract_module_shapes(source: str, module_path: str) -> dict[str, ExtractedShape]:
    """Parse one module's ``to_wire`` methods into literal key sets, without executing it.

    Three forms are recognised, and they are the three this repository writes:

    * ``return {"a": ..., "b": ...}`` -- the whole shape in one literal;
    * ``wire: dict[str, object] = {...}`` followed by ``return wire`` -- the same, named;
    * ``wire["c"] = ...`` under a condition -- a key the shape emits only sometimes.

    Anything else is unresolved.  In particular a non-literal key, a dict comprehension or a spread
    from another mapping means the key set is not decidable from the source, and the shape is
    reported as such rather than approximated.
    """

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - a syntactically invalid module is a build break
        raise CrossLanguageSurfaceError(f"{module_path} is not parseable Python") from exc

    found: dict[str, ExtractedShape] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef) or item.name != _WIRE_METHOD:
                continue
            literal: list[str] = []
            conditional: list[str] = []
            unresolved: list[str] = []
            for statement in ast.walk(item):
                if isinstance(statement, ast.Dict):
                    for key in statement.keys:
                        resolved = _literal_key(key)
                        if resolved is None:
                            unresolved.append("a mapping key is not a string literal")
                        else:
                            literal.append(resolved)
                elif isinstance(statement, ast.DictComp):
                    unresolved.append("a comprehension builds the mapping")
                elif (
                    isinstance(statement, ast.Subscript)
                    and isinstance(statement.ctx, ast.Store)
                    and isinstance(statement.slice, ast.Constant)
                    and isinstance(statement.slice.value, str)
                ):
                    conditional.append(statement.slice.value)
                elif isinstance(statement, ast.Subscript) and isinstance(statement.ctx, ast.Store):
                    unresolved.append("a subscript assignment key is not a string literal")
            required = sorted(set(literal) - set(conditional))
            found[node.name] = ExtractedShape(
                class_name=node.name,
                required_keys=tuple(required),
                optional_keys=tuple(sorted(set(conditional))),
                unresolved=tuple(sorted(set(unresolved))),
            )
    return found


def extract_shape(path: Path, module_path: str, class_name: str) -> ExtractedShape:
    """Extract exactly one declared shape, refusing anything the parser could not decide."""

    shapes = extract_module_shapes(path.read_text(encoding="utf-8"), module_path)
    shape = shapes.get(class_name)
    if shape is None:
        raise CrossLanguageSurfaceError(f"{module_path} declares no {class_name}.{_WIRE_METHOD}")
    if shape.unresolved:
        raise CrossLanguageSurfaceError(
            f"{module_path}::{class_name} is unresolved: {shape.unresolved[0]}"
        )
    if not shape.required_keys and not shape.optional_keys:
        raise CrossLanguageSurfaceError(f"{module_path}::{class_name} emits no keys")
    return shape


def build_cross_language_surface(shapes: tuple[WireShape, ...]) -> CrossLanguageSurface:
    """Sort and validate one complete surface; a caller never has to pre-sort."""

    if not isinstance(shapes, tuple):
        raise CrossLanguageSurfaceError("shapes must be a tuple")
    if not all(isinstance(item, WireShape) for item in shapes):
        raise CrossLanguageSurfaceError("shapes contain an invalid value")
    return CrossLanguageSurface(shapes=tuple(sorted(shapes, key=lambda item: item.export_name)))


__all__ = [
    "CROSS_LANGUAGE_SURFACE_SCHEMA",
    "MAX_KEYS",
    "MAX_SHAPES",
    "CrossLanguageSurface",
    "CrossLanguageSurfaceError",
    "ExtractedShape",
    "WireShape",
    "build_cross_language_surface",
    "extract_module_shapes",
    "extract_shape",
]
