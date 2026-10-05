"""Parse-only structural admission of the loaded native MiniMax H3 T2VA surface.

The native module is untrusted input. Its bytes are decoded and parsed with :mod:`ast`; nothing
is imported, compiled for execution, evaluated or run. The classifier proves one narrow fact: the
definitions a binding-free T2VA call executes are the audited baseline definitions, and nothing
else in the module can change them at definition time. It never claims whole-module, model or
imported-library equivalence, and it cannot attest in-place runtime monkeypatches.

Refusals carry closed identifiers only. A definition identifier is always a name from the frozen
baseline manifest or a fixed envelope category, never a name read from the observed source.
"""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
import io
import json
import symtable
import tokenize
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import Enum

from . import native_t2va_baseline as baseline

NATIVE_T2VA_CLASSIFIER_SCHEMA = "h3.native_t2va_structure.v1"
NATIVE_T2VA_MANIFEST_SCHEMA = "h3.native_t2va_baseline_manifest.v1"
MAX_SOURCE_BYTES = 262_144
# Frozen from the 2026-09-28 source census (2,305..4,693 nodes, depth 16) with bounded headroom.
MAX_AST_NODES = 8_192
MAX_AST_DEPTH = 64

IMAGE_TO_VIDEO = "MiniMaxH3ImageToVideo"
_EXECUTE = "execute"
# (statement index, guarded parameter or list) of the three bodies a binding-free call skips.
_GUARDS = ((3, "first_frame"), (4, "last_frame"), (7, "keyframes"))
_KEYFRAMES = "keyframes"
_FRAME_PARAMETERS = ("first_frame", "last_frame")
_ENVELOPE_DECORATORS = frozenset({"classmethod", "staticmethod"})
# Members that CPython's class machinery calls while creating, subclassing or subscripting a
# class. Refused on every class so no member can act at definition time, whatever the other
# envelope limits later admit. Do not widen this to every dunder like module-level binding:
# the host's unselected helper classes define runtime `__init__`/`__call__`, and refusing them
# refuses the real host source.
_CLASS_CREATION_HOOKS = frozenset(
    {"__init_subclass__", "__set_name__", "__class_getitem__", "__mro_entries__", "__prepare__"}
)
_COMFY_NODE_BASE = ("io", "ComfyNode")
_EXTENSION_BASE = "ComfyExtension"
_LITERAL_BINARY_OPERATORS = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.LShift,
    ast.RShift,
    ast.BitOr,
    ast.BitXor,
    ast.BitAnd,
)
_LITERAL_UNARY_OPERATORS = (ast.UAdd, ast.USub, ast.Not, ast.Invert)


class StructureReason(str, Enum):
    ADMITTED = "native_t2va_structure_admitted"
    CHANGED = "native_t2va_structure_changed"
    UNPARSABLE = "native_t2va_structure_unparsable"


@dataclass(frozen=True, slots=True)
class NativeT2VABaselineManifest:
    """The accepted surface: per-definition digests plus the frozen binding closure."""

    source_revision: str
    source_blob: str
    definitions: tuple[tuple[str, str], ...]
    constants: tuple[tuple[str, str], ...]
    package_roots: tuple[str, ...]
    required_imports: tuple[str, ...]
    from_bindings: tuple[tuple[str, str], ...]
    envelope_bindings: tuple[tuple[str, str], ...]
    protected_builtins: tuple[str, ...]
    external_paths: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": NATIVE_T2VA_MANIFEST_SCHEMA,
            "classifier_schema": NATIVE_T2VA_CLASSIFIER_SCHEMA,
            "source_revision": self.source_revision,
            "source_blob": self.source_blob,
            "definitions": [list(item) for item in self.definitions],
            "constants": [list(item) for item in self.constants],
            "package_roots": list(self.package_roots),
            "required_imports": list(self.required_imports),
            "from_bindings": [list(item) for item in self.from_bindings],
            "envelope_bindings": [list(item) for item in self.envelope_bindings],
            "protected_builtins": list(self.protected_builtins),
            "external_paths": list(self.external_paths),
        }

    @property
    def fingerprint(self) -> str:
        return "sha256:" + hashlib.sha256(_json_bytes(self.to_wire())).hexdigest()

    def top_level_definitions(self) -> frozenset[str]:
        # A dotted row (``Class.member``) is a diagnostic sub-row of its top-level definition.
        return frozenset(name.split(".", 1)[0] for name, _digest in self.definitions)

    def protected_names(self) -> frozenset[str]:
        return frozenset(
            list(self.top_level_definitions())
            + [name for name, _digest in self.constants]
            + list(self.package_roots)
            + [name for name, _origin in self.from_bindings]
            + [name for name, _origin in self.envelope_bindings]
            + list(self.protected_builtins)
        )

    def protected_origins(self) -> frozenset[str]:
        return frozenset(
            list(self.package_roots)
            + [item for item in self.required_imports if "." in item]
            + [origin + "." + name for name, origin in self.from_bindings]
            + [origin + "." + name for name, origin in self.envelope_bindings]
        )


PRODUCTION_MANIFEST = NativeT2VABaselineManifest(
    source_revision=baseline.SOURCE_REVISION,
    source_blob=baseline.SOURCE_BLOB,
    definitions=baseline.DEFINITIONS,
    constants=baseline.CONSTANTS,
    package_roots=baseline.PACKAGE_ROOTS,
    required_imports=baseline.REQUIRED_IMPORTS,
    from_bindings=baseline.FROM_BINDINGS,
    envelope_bindings=baseline.ENVELOPE_BINDINGS,
    protected_builtins=baseline.PROTECTED_BUILTINS,
    external_paths=baseline.EXTERNAL_PATHS,
)


@dataclass(frozen=True, slots=True)
class NativeT2VAStructureVerdict:
    """A closed classification of one bounded source buffer against one manifest."""

    reason: StructureReason
    check: str
    manifest_fingerprint: str
    classifier_schema: str = NATIVE_T2VA_CLASSIFIER_SCHEMA

    @property
    def admitted(self) -> bool:
        return self.reason is StructureReason.ADMITTED


class _Refusal(Exception):
    def __init__(self, reason: StructureReason, check: str) -> None:
        super().__init__(check)
        self.reason = reason
        self.check = check


def _changed(check: str) -> _Refusal:
    return _Refusal(StructureReason.CHANGED, check)


def _unparsable(check: str) -> _Refusal:
    return _Refusal(StructureReason.UNPARSABLE, check)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


# --- parsing and bounds -------------------------------------------------------------------------


def _parse(content: bytes) -> ast.Module:
    if type(content) is not bytes:
        raise _unparsable("source_type")
    if len(content) > MAX_SOURCE_BYTES:
        raise _unparsable("source_size")
    try:
        encoding, _lines = tokenize.detect_encoding(io.BytesIO(content).readline)
        text = content.decode(encoding, errors="strict")
    except (SyntaxError, LookupError, UnicodeDecodeError, ValueError):
        raise _unparsable("source_encoding") from None
    try:
        # IMPORTANT: parse only. Never compile for execution, exec, eval or import host code.
        tree = ast.parse(text, filename="<native-t2va>", mode="exec")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise _unparsable("source_syntax") from None
    _check_bounds(tree)
    return tree


def _check_bounds(tree: ast.AST) -> None:
    # CRITICAL: count iteratively before any recursive walk. Module is depth 1 and every
    # iter_child_nodes occurrence counts, context and operator nodes included.
    pending: list[tuple[ast.AST, int]] = [(tree, 1)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_AST_NODES:
            raise _unparsable("source_node_limit")
        if depth > MAX_AST_DEPTH:
            raise _unparsable("source_depth_limit")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))


# --- canonical digest ---------------------------------------------------------------------------


_ABSENT = object()


def _canonical(value: object) -> object:
    # Every `_fields` entry in declaration order; no location attributes; no dropped empties.
    # Scalar types stay distinct (bool before int), and list order is preserved.
    if isinstance(value, ast.AST):
        fields = []
        for name in value._fields:
            fields.append([name, _canonical(getattr(value, name, _ABSENT))])
        return ["node", type(value).__name__, fields]
    if type(value) is list:
        return ["list", [_canonical(item) for item in value]]
    if value is _ABSENT:
        return ["absent"]
    if value is None:
        return ["none"]
    if value is Ellipsis:
        return ["ellipsis"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if type(value) is float:
        return ["float", value.hex()]
    if type(value) is complex:
        return ["complex", value.real.hex(), value.imag.hex()]
    if type(value) is str:
        return ["str", value]
    if type(value) is bytes:
        return ["bytes", value.hex()]
    raise _changed("unsupported_syntax")


def definition_digest(node: ast.AST) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(_canonical(node))).hexdigest()


# --- definition-time envelope -------------------------------------------------------------------


def _is_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(not isinstance(item, ast.Starred) and _is_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and _is_literal(key) and _is_literal(value)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, _LITERAL_UNARY_OPERATORS) and _is_literal(node.operand)
    if isinstance(node, ast.BinOp):
        return (
            isinstance(node.op, _LITERAL_BINARY_OPERATORS)
            and _is_literal(node.left)
            and _is_literal(node.right)
        )
    return False


def _is_reference(node: ast.AST) -> bool:
    # A non-call annotation: a string, a name or a dotted attribute chain rooted at a name.
    if isinstance(node, ast.Constant):
        return type(node.value) is str
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def _arguments(node: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.arg]:
    arguments = node.args
    yield from arguments.posonlyargs
    yield from arguments.args
    yield from arguments.kwonlyargs
    if arguments.vararg is not None:
        yield arguments.vararg
    if arguments.kwarg is not None:
        yield arguments.kwarg


def _check_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    if getattr(node, "type_params", []):
        raise _changed("envelope_function")
    for decorator in node.decorator_list:
        if not (isinstance(decorator, ast.Name) and decorator.id in _ENVELOPE_DECORATORS):
            raise _changed("envelope_function")
    defaults = [*node.args.defaults, *(item for item in node.args.kw_defaults if item is not None)]
    if not all(_is_literal(item) for item in defaults):
        raise _changed("envelope_function")
    annotations = [item.annotation for item in _arguments(node) if item.annotation is not None]
    if node.returns is not None:
        annotations.append(node.returns)
    if not all(_is_reference(item) for item in annotations):
        raise _changed("envelope_function")


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and type(node.value.value) is str
    )


def _check_class(node: ast.ClassDef, *, extension_bound: bool) -> None:
    if node.keywords or node.decorator_list or getattr(node, "type_params", []):
        raise _changed("envelope_class")
    if len(node.bases) > 1:
        raise _changed("envelope_class")
    for base in node.bases:
        comfy_node = (
            isinstance(base, ast.Attribute)
            and isinstance(base.value, ast.Name)
            and (base.value.id, base.attr) == _COMFY_NODE_BASE
        )
        # The extension base is admitted only once bound by its exact manifest import.
        extension = isinstance(base, ast.Name) and base.id == _EXTENSION_BASE and extension_bound
        if not (comfy_node or extension):
            raise _changed("envelope_class")
    members: set[str] = set()
    for index, member in enumerate(node.body):
        if index == 0 and _is_docstring(member):
            continue
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise _changed("envelope_class")
        if member.name in _CLASS_CREATION_HOOKS:
            raise _changed("envelope_dunder")
        if member.name in members:
            raise _changed("envelope_duplicate")
        members.add(member.name)
        _check_function(member)


def _bound_name(alias: ast.alias, *, from_import: bool) -> str:
    if alias.asname is not None:
        return alias.asname
    return alias.name if from_import else alias.name.split(".", 1)[0]


@dataclass(slots=True)
class _Envelope:
    definitions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]
    constants: dict[str, ast.Assign]
    imports: set[str]
    from_imports: set[tuple[str, str]]


def _check_envelope(tree: ast.Module, manifest: NativeT2VABaselineManifest) -> _Envelope:
    protected = manifest.protected_names()
    origins = manifest.protected_origins()
    from_bindings = dict(manifest.from_bindings) | dict(manifest.envelope_bindings)
    package_roots = frozenset(manifest.package_roots)
    selected = manifest.top_level_definitions()
    constants = {name for name, _digest in manifest.constants}
    envelope = _Envelope({}, {}, set(), set())
    bound: dict[str, str] = {}

    def bind(name: str, kind: str) -> None:
        if name.startswith("__") and name.endswith("__"):
            raise _changed("envelope_dunder")
        previous = bound.get(name)
        # Dotted imports rebinding one package root bind the same package object.
        if previous is not None and not (previous == kind == "package_root"):
            raise _changed("binding:" + name if name in protected else "envelope_duplicate")
        bound[name] = kind

    for index, statement in enumerate(tree.body):
        if index == 0 and _is_docstring(statement):
            continue
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                name = _bound_name(alias, from_import=False)
                if alias.asname is not None:
                    if alias.name in origins or name in protected:
                        raise _changed("binding:" + name if name in protected else "envelope_alias")
                    bind(name, "import_alias")
                elif name in protected and name not in package_roots:
                    raise _changed("binding:" + name)
                else:
                    bind(name, "package_root")
                    envelope.imports.add(alias.name)
            continue
        if isinstance(statement, ast.ImportFrom):
            if statement.level != 0 or statement.module in (None, "__future__"):
                raise _changed("envelope_import")
            module = statement.module or ""
            for alias in statement.names:
                if alias.name == "*":
                    raise _changed("envelope_import")
                name = _bound_name(alias, from_import=True)
                origin = module + "." + alias.name
                exact = alias.asname is None and from_bindings.get(name) == module
                if name in protected and not exact:
                    raise _changed("binding:" + name)
                if origin in origins and not exact:
                    raise _changed("envelope_alias")
                bind(name, "from_import")
                envelope.from_imports.add((name, module))
            continue
        if isinstance(statement, ast.Assign):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                raise _changed("envelope_assign")
            name = statement.targets[0].id
            if not _is_literal(statement.value):
                raise _changed("envelope_assign")
            if name in protected and name not in constants:
                raise _changed("binding:" + name)
            bind(name, "constant")
            if name in constants:
                envelope.constants[name] = statement
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = statement.name
            if name in protected and name not in selected:
                raise _changed("binding:" + name)
            if isinstance(statement, ast.ClassDef):
                _check_class(statement, extension_bound=bound.get(_EXTENSION_BASE) == "from_import")
            else:
                _check_function(statement)
            bind(name, "definition")
            if name in selected:
                envelope.definitions[name] = statement
            continue
        raise _changed("envelope_statement")
    for node in ast.walk(tree):
        if isinstance(node, ast.Global) and any(name in protected for name in node.names):
            raise _changed("envelope_global")
    for required in manifest.required_imports:
        if required not in envelope.imports:
            raise _changed("import:" + required)
    for name, module in manifest.from_bindings:
        if (name, module) not in envelope.from_imports:
            raise _changed("binding:" + name)
    return envelope


# --- the binding-free T2VA reachability proof ---------------------------------------------------


def _names(node: ast.AST) -> Iterator[ast.Name]:
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            yield child


def _is_none(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _guard_test(test: ast.expr, name: str) -> bool:
    if name == _KEYFRAMES:
        return isinstance(test, ast.Name) and test.id == _KEYFRAMES
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == name
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.IsNot)
        and len(test.comparators) == 1
        and _is_none(test.comparators[0])
    )


def _is_keyframe_reset(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == _KEYFRAMES
        and isinstance(statement.value, ast.List)
        and not statement.value.elts
    )


def _masked_image_to_video(node: ast.AST) -> ast.ClassDef:
    """Prove the three guarded bodies are unreachable for a binding-free call, then mask them."""
    if not isinstance(node, ast.ClassDef):
        raise _changed("definition:" + IMAGE_TO_VIDEO)
    executes = [
        member
        for member in node.body
        if isinstance(member, ast.FunctionDef) and member.name == _EXECUTE
    ]
    if len(executes) != 1:
        raise _changed("guard:" + IMAGE_TO_VIDEO)
    execute = executes[0]
    arguments = execute.args
    names = [item.arg for item in arguments.args]
    if (
        arguments.posonlyargs
        or arguments.kwonlyargs
        or arguments.vararg is not None
        or arguments.kwarg is not None
        or tuple(names[-2:]) != _FRAME_PARAMETERS
        or len(arguments.defaults) != 2
        or not all(_is_none(item) for item in arguments.defaults)
    ):
        raise _changed("guard:" + IMAGE_TO_VIDEO)
    body = execute.body
    last_guard = _GUARDS[-1][0]
    if len(body) <= last_guard:
        raise _changed("guard:" + IMAGE_TO_VIDEO)
    guard_indexes = {index for index, _name in _GUARDS}
    for index, name in _GUARDS:
        statement = body[index]
        if not (
            isinstance(statement, ast.If)
            and not statement.orelse
            and _guard_test(statement.test, name)
        ):
            raise _changed("guard:" + IMAGE_TO_VIDEO)
    resets = [index for index in range(last_guard) if _is_keyframe_reset(body[index])]
    if len(resets) != 1 or resets[0] >= _GUARDS[0][0]:
        raise _changed("guard:" + IMAGE_TO_VIDEO)
    # Between entry and the last guard nothing but the reset may touch the keyframe list, and
    # nothing may rebind a frame parameter before its guard reads it. Guard bodies are skipped
    # only because their guards are proven false for a binding-free call.
    for index in range(last_guard):
        if index in guard_indexes:
            continue
        for reference in _names(body[index]):
            if reference.id == _KEYFRAMES and index != resets[0]:
                raise _changed("guard:" + IMAGE_TO_VIDEO)
            if reference.id in _FRAME_PARAMETERS and not isinstance(reference.ctx, ast.Load):
                raise _changed("guard:" + IMAGE_TO_VIDEO)
    masked = copy.deepcopy(node)
    for member in masked.body:
        if isinstance(member, ast.FunctionDef) and member.name == _EXECUTE:
            for index, _name in _GUARDS:
                guard = member.body[index]
                if isinstance(guard, ast.If):  # proven above on the original tree
                    guard.body = [ast.Pass()]
    return masked


def _member(node: ast.AST, name: str, member: str) -> ast.AST:
    # Class members are unique here: the envelope already refused duplicates.
    if isinstance(node, ast.ClassDef):
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == member:
                return item
    raise _changed("definition:" + name)


def _surface_nodes(
    definitions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef],
    names: Iterator[str],
) -> Iterator[tuple[str, ast.AST]]:
    masked: dict[str, ast.AST] = {}
    for name in names:
        owner, _dot, member = name.partition(".")
        node: ast.AST | None = definitions.get(owner)
        if node is None:
            raise _changed("definition:" + owner)
        if owner == IMAGE_TO_VIDEO:
            if owner not in masked:
                masked[owner] = _masked_image_to_video(node)
            node = masked[owner]
        yield name, _member(node, name, member) if member else node


def _selected_surface(
    envelope: _Envelope, manifest: NativeT2VABaselineManifest
) -> Iterator[tuple[str, ast.AST]]:
    # Member rows precede their class row, so a changed member names itself, not only its class.
    yield from _surface_nodes(envelope.definitions, (name for name, _d in manifest.definitions))
    for name, _digest in manifest.constants:
        constant = envelope.constants.get(name)
        if constant is None:
            raise _changed("constant:" + name)
        yield name, constant


def _classify(content: bytes, manifest: NativeT2VABaselineManifest) -> NativeT2VAStructureVerdict:
    """Compare one bounded buffer with one manifest. Tests may pass a synthetic manifest."""
    try:
        tree = _parse(content)
        envelope = _check_envelope(tree, manifest)
        expected = dict(manifest.definitions) | dict(manifest.constants)
        kinds = {name: "definition:" for name, _digest in manifest.definitions}
        for name, node in _selected_surface(envelope, manifest):
            if definition_digest(node) != expected[name]:
                raise _changed(kinds.get(name, "constant:") + name)
    except _Refusal as refusal:
        return NativeT2VAStructureVerdict(refusal.reason, refusal.check, manifest.fingerprint)
    except RecursionError:
        return NativeT2VAStructureVerdict(
            StructureReason.UNPARSABLE, "source_depth_limit", manifest.fingerprint
        )
    return NativeT2VAStructureVerdict(StructureReason.ADMITTED, "admitted", manifest.fingerprint)


def classify_native_t2va_source(content: bytes) -> NativeT2VAStructureVerdict:
    """Classify loaded native bytes against the production baseline; no table is accepted."""
    return _classify(content, PRODUCTION_MANIFEST)


def is_production_admission(verdict: object) -> bool:
    """True only for an admitted verdict produced against the current production manifest."""
    return (
        type(verdict) is NativeT2VAStructureVerdict
        and verdict.admitted
        and verdict.classifier_schema == NATIVE_T2VA_CLASSIFIER_SCHEMA
        and verdict.manifest_fingerprint == PRODUCTION_MANIFEST.fingerprint
    )


# --- maintainer derivation (tooling and tests; never on the runtime admission path) --------------


def _attribute_path(node: ast.Attribute) -> str | None:
    parts = [node.attr]
    value: ast.expr = node.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if not isinstance(value, ast.Name):
        return None
    parts.append(value.id)
    return ".".join(reversed(parts))


def _referenced_globals(surface: Sequence[ast.stmt], imports: Sequence[ast.stmt]) -> set[str]:
    module = ast.Module(body=[*imports, *surface], type_ignores=[])
    table = symtable.symtable(ast.unparse(module), "<native-t2va-surface>", "exec")
    names: set[str] = set()
    pending = [table]
    while pending:
        current = pending.pop()
        for symbol in current.get_symbols():
            if symbol.is_referenced() and (current.get_type() == "module" or symbol.is_global()):
                names.add(symbol.get_name())
        pending.extend(current.get_children())
    return names


def derive_manifest(
    content: bytes,
    *,
    source_revision: str,
    source_blob: str,
    definitions: tuple[str, ...],
) -> NativeT2VABaselineManifest:
    """Derive a manifest from an accepted source; refuses a surface whose closure is open."""
    tree = _parse(content)
    top = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    constant_nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ]
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    try:
        rows = list(_surface_nodes(top, iter(definitions)))
    except _Refusal:
        raise ValueError("definition absent or differently shaped") from None
    # The closure is computed over top-level definitions; member rows are contained in them.
    surface = [
        (name, node) for name, node in rows if "." not in name and isinstance(node, ast.stmt)
    ]
    if {name.split(".", 1)[0] for name in definitions} != {name for name, _node in surface}:
        raise ValueError("a member row needs its top-level definition row")
    referenced = _referenced_globals([node for _name, node in surface], imports)
    constant_names = {
        target.id
        for node in constant_nodes
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    package_roots: set[str] = set()
    required: set[str] = set()
    from_bindings: set[tuple[str, str]] = set()
    paths = {
        path
        for _name, node in surface
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.ctx, ast.Load)
        and (path := _attribute_path(child)) is not None
        and path.split(".", 1)[0] in referenced
    }
    maximal = {path for path in paths if not any(other.startswith(path + ".") for other in paths)}
    for node in imports:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if alias.asname is None and root in referenced:
                    if alias.name == root or any(
                        path.startswith(alias.name + ".") for path in paths
                    ):
                        package_roots.add(root)
                        required.add(alias.name)
        elif node.module is not None:
            for alias in node.names:
                if alias.asname is None and alias.name in referenced:
                    from_bindings.add((alias.name, node.module))
    top_level = {name for name, _node in surface}
    defined = top_level | constant_names | package_roots | {n for n, _m in from_bindings}
    helpers = referenced - defined
    protected_builtins = {name for name in helpers if hasattr(builtins, name)}
    if helpers - protected_builtins:
        raise ValueError("selected surface references an unselected module binding")
    envelope_bindings = {
        (alias.name, node.module)
        for node in imports
        if isinstance(node, ast.ImportFrom) and node.module is not None
        for alias in node.names
        if alias.name == _EXTENSION_BASE and alias.asname is None
    }
    return NativeT2VABaselineManifest(
        source_revision=source_revision,
        source_blob=source_blob,
        definitions=tuple((name, definition_digest(node)) for name, node in rows),
        constants=tuple(
            (target.id, definition_digest(node))
            for node in constant_nodes
            for target in node.targets
            if isinstance(target, ast.Name)
        ),
        package_roots=tuple(sorted(package_roots)),
        required_imports=tuple(sorted(required)),
        from_bindings=tuple(sorted(from_bindings)),
        envelope_bindings=tuple(sorted(envelope_bindings)),
        protected_builtins=tuple(sorted(protected_builtins | _ENVELOPE_DECORATORS)),
        external_paths=tuple(sorted(maximal)),
    )


__all__ = [
    "NATIVE_T2VA_CLASSIFIER_SCHEMA",
    "MAX_AST_DEPTH",
    "MAX_AST_NODES",
    "MAX_SOURCE_BYTES",
    "PRODUCTION_MANIFEST",
    "NativeT2VABaselineManifest",
    "NativeT2VAStructureVerdict",
    "StructureReason",
    "classify_native_t2va_source",
    "definition_digest",
    "derive_manifest",
    "is_production_admission",
]
