"""Derive and enforce the exact retained lazy pure-core export surface.

The supported consumer surface lives in ``comfyui_h3_context.public_api``.  The pure-core hub is an
implementation convenience: it retains names that repository tests, scripts or executable examples
actually import through that hub, plus pure values re-exported by the package facade.  Everything
else remains available from its defining leaf module.

This tool parses fixed repository paths only.  It does not import the hub, execute source, inspect a
host or contact a network.
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "comfyui_h3_context"
CORE_ROOT = ROOT / PACKAGE / "core"
CORE_INIT = CORE_ROOT / "__init__.py"
PUBLIC_API = ROOT / PACKAGE / "public_api.py"
ACCEPTANCE_BASELINE = ROOT / "tests" / "acceptance_baseline.json"
CONSUMER_ROOTS = ("tests", "scripts", "examples")
CORE_HUB = f"{PACKAGE}.core"
CONSUMER_HUBS = {CORE_HUB, PACKAGE, f"{PACKAGE}.public_api"}

FACADE_REASON = "requires_node_registration_metadata_forbidden_to_pure_core"
PACKAGE_FACADE_REASONS = {
    "build_runtime_public_manifest": FACADE_REASON,
    "build_runtime_public_manifest_v2": FACADE_REASON,
    "collect_node_object_info": FACADE_REASON,
    "get_node_mappings": FACADE_REASON,
    "get_public_manifest": FACADE_REASON,
    "get_public_manifest_v2": FACADE_REASON,
}


class CorePublicSurfaceError(ValueError):
    """Raised when the core surface cannot be derived without guessing."""


class ImportBinding(NamedTuple):
    module: str
    original: str
    bound: str

    def render(self) -> str:
        if self.original == self.bound:
            return self.original
        return f"{self.original} as {self.bound}"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _dunder_all(path: Path) -> tuple[str, ...]:
    assignments = [
        node
        for node in _tree(path).body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    ]
    if len(assignments) != 1:
        raise CorePublicSurfaceError(f"{path.relative_to(ROOT)} needs exactly one __all__")
    value = assignments[0].value
    if not isinstance(value, ast.List | ast.Tuple):
        raise CorePublicSurfaceError(f"{path.relative_to(ROOT)}.__all__ must be literal")
    names: list[str] = []
    for element in value.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            raise CorePublicSurfaceError(f"{path.relative_to(ROOT)}.__all__ must contain strings")
        names.append(element.value)
    if len(names) != len(set(names)):
        raise CorePublicSurfaceError(f"{path.relative_to(ROOT)}.__all__ contains a duplicate")
    return tuple(names)


def current_core_bindings() -> tuple[ImportBinding, ...]:
    bindings: list[ImportBinding] = []
    tree = _tree(CORE_INIT)
    nodes = list(tree.body)
    for node in tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name):
            if node.test.id == "TYPE_CHECKING":
                nodes.extend(node.body)
    for node in nodes:
        if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
            continue
        for alias in node.names:
            bindings.append(
                ImportBinding(
                    module=node.module,
                    original=alias.name,
                    bound=alias.asname or alias.name,
                )
            )
    return tuple(bindings)


def current_core_export_names() -> set[str]:
    return set(_dunder_all(CORE_INIT))


def _consumer_paths() -> tuple[Path, ...]:
    paths: list[Path] = []
    for root_name in CONSUMER_ROOTS:
        for path in sorted((ROOT / root_name).rglob("*.py")):
            if "__pycache__" not in path.parts:
                paths.append(path)
    return tuple(paths)


def _imported_names_from(hubs: set[str]) -> set[str]:
    names: set[str] = set()
    for path in _consumer_paths():
        try:
            tree = _tree(path)
        except SyntaxError:  # pragma: no cover - another gate reports a malformed source file
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 0:
                continue
            if node.module not in hubs:
                continue
            for alias in node.names:
                if alias.name == "*":
                    raise CorePublicSurfaceError(f"star import from {node.module} in {path}")
                names.add(alias.name)
    return names


def hub_consumer_names() -> set[str]:
    """Names read through any of the three recorded package/core facade hubs."""

    return _imported_names_from(CONSUMER_HUBS)


def _direct_core_consumer_names() -> set[str]:
    """Names whose import syntax specifically requires a core-hub binding."""

    return _imported_names_from({CORE_HUB})


def acceptance_abi_names() -> set[str]:
    document = json.loads(ACCEPTANCE_BASELINE.read_text(encoding="utf-8"))
    rows = document.get("public_python_abi")
    if not isinstance(rows, list):
        raise CorePublicSurfaceError("acceptance baseline public_python_abi must be a list")
    names: set[str] = set()
    for row in rows:
        name = row.get("export") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name:
            raise CorePublicSurfaceError("acceptance baseline has an invalid public Python ABI")
        names.add(name)
    if not names:
        raise CorePublicSurfaceError("acceptance baseline has an invalid public Python ABI")
    return names


def _pure_package_bindings() -> dict[str, ImportBinding]:
    public_names = set(_dunder_all(PUBLIC_API))
    bindings: dict[str, ImportBinding] = {}
    for node in _tree(PUBLIC_API).body:
        if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
            continue
        if not node.module.startswith("core."):
            continue
        module = node.module.removeprefix("core.")
        for alias in node.names:
            bound = alias.asname or alias.name
            if bound in public_names:
                bindings[bound] = ImportBinding(module, alias.name, bound)
    return bindings


def package_surface_partition() -> tuple[set[str], dict[str, str]]:
    package_names = set(_dunder_all(PUBLIC_API))
    pure = set(_pure_package_bindings())
    facade_only = package_names - pure
    if set(PACKAGE_FACADE_REASONS) != facade_only:
        missing = sorted(facade_only - set(PACKAGE_FACADE_REASONS))
        stale = sorted(set(PACKAGE_FACADE_REASONS) - facade_only)
        raise CorePublicSurfaceError(
            f"package facade reasons drifted; missing={missing!r}, stale={stale!r}"
        )
    return pure, dict(PACKAGE_FACADE_REASONS)


def _core_submodule_names() -> set[str]:
    return {
        path.relative_to(CORE_ROOT).with_suffix("").as_posix().replace("/", ".")
        for path in CORE_ROOT.rglob("*.py")
        if path.name != "__init__.py" and "__pycache__" not in path.parts
    }


def derive_required_core_exports() -> set[str]:
    current_bindings = current_core_bindings()
    current_bound = {binding.bound for binding in current_bindings}
    pure_bindings = _pure_package_bindings()
    consumers = hub_consumer_names()

    # ``from package import submodule`` is Python's normal submodule import path, not a named hub
    # export.  Every other consumer name needs an existing or package-facade-derived binding.
    unknown = (
        _direct_core_consumer_names() - current_bound - set(pure_bindings) - _core_submodule_names()
    )
    if unknown:
        raise CorePublicSurfaceError(
            f"hub consumer has no unambiguous binding: {sorted(unknown)[0]}"
        )

    required = (consumers & current_bound) | set(pure_bindings) | acceptance_abi_names()
    available = current_bound | set(pure_bindings)
    missing = required - available
    if missing:
        raise CorePublicSurfaceError(f"required core export has no binding: {sorted(missing)[0]}")
    return required


def unexplained_core_exports() -> set[str]:
    """Current hub exports justified by neither a reader nor a containment obligation."""

    return current_core_export_names() - derive_required_core_exports()


def _render_import(module: str, bindings: Sequence[ImportBinding]) -> list[str]:
    lines = [f"from .{module} import ("]
    lines.extend(f"    {binding.render()}," for binding in bindings)
    lines.append(")")
    return lines


def _render_module_imports(module: str, bindings: Sequence[ImportBinding]) -> list[str]:
    direct = [binding for binding in bindings if binding.original == binding.bound]
    aliases = [binding for binding in bindings if binding.original != binding.bound]
    rendered: list[str] = []
    if direct:
        rendered.extend(_render_import(module, direct))
    if aliases:
        rendered.extend(_render_import(module, aliases))
    return rendered


def render_core_init() -> bytes:
    required = derive_required_core_exports()
    records = list(current_core_bindings())
    currently_bound = {binding.bound for binding in records}
    for name, binding in _pure_package_bindings().items():
        if name not in currently_bound:
            records.append(binding)

    available = {binding.bound for binding in records}
    if required - available:
        raise CorePublicSurfaceError(f"cannot render binding for {sorted(required - available)[0]}")

    grouped: OrderedDict[str, list[ImportBinding]] = OrderedDict()
    seen_records: set[ImportBinding] = set()
    for binding in sorted(records, key=lambda item: item.module):
        if binding.bound not in required or binding in seen_records:
            continue
        grouped.setdefault(binding.module, []).append(binding)
        seen_records.add(binding)

    rendered = [
        '"""Dependency-free pure-core namespace with finite lazy exports.',
        "",
        "Contracts remain independent from host, provider, model, HTTP and GPU runtimes.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from importlib import import_module as _import_module",
        "from threading import RLock as _RLock",
        "from types import MappingProxyType as _MappingProxyType",
        "from typing import TYPE_CHECKING",
        "from typing import Any as _Any",
        "",
        "if TYPE_CHECKING:",
    ]
    for module, bindings in grouped.items():
        rendered.extend("    " + line for line in _render_module_imports(module, bindings))

    rendered.extend(["", "_EXPORTS = _MappingProxyType(", "    {"])
    resolved = {binding.bound: binding for binding in records if binding.bound in required}
    for name, binding in sorted(resolved.items()):
        line = f'        "{name}": (".{binding.module}", "{binding.original}"),'
        if len(line) <= 100:
            rendered.append(line)
        else:
            rendered.extend(
                [
                    f'        "{name}": (',
                    f'            ".{binding.module}",',
                    f'            "{binding.original}",',
                    "        ),",
                ]
            )
    rendered.extend(
        [
            "    }",
            ")",
            "_EXPORT_LOCK = _RLock()",
            "",
            "",
            "def __getattr__(name: str) -> _Any:",
            "    binding = _EXPORTS.get(name)",
            "    if binding is None:",
            '        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")',
            "    # CRITICAL: importing under this cache lock can invert Python module-lock order.",
            "    value = getattr(_import_module(binding[0], __name__), binding[1])",
            "    with _EXPORT_LOCK:",
            "        return globals().setdefault(name, value)",
            "",
            "",
            "def __dir__() -> list[str]:",
            "    return sorted(set(globals()) | set(__all__))",
            "",
        ]
    )

    current_order = _dunder_all(CORE_INIT)
    export_order = [name for name in current_order if name in required]
    export_order.extend(sorted(required - set(export_order)))
    rendered.extend(["", "__all__ = ["])
    rendered.extend(f'    "{name}",' for name in export_order)
    rendered.extend(["]", ""])
    return "\n".join(rendered).encode("utf-8")


def validate_core_surface() -> dict[str, int]:
    pure, facades = package_surface_partition()
    current = current_core_export_names()
    required = derive_required_core_exports()
    if current != required:
        missing = sorted(required - current)
        extra = sorted(current - required)
        raise CorePublicSurfaceError(
            f"core export set drifted; missing={missing[:1]!r}, extra={extra[:1]!r}"
        )
    imported = {binding.bound for binding in current_core_bindings()}
    if imported != current:
        raise CorePublicSurfaceError("core import bindings and __all__ differ")
    if not acceptance_abi_names() <= current:
        raise CorePublicSurfaceError("gate-enforced ABI is not a pure-core subset")
    if not pure <= current:
        raise CorePublicSurfaceError("pure package value is absent from the core surface")
    if render_core_init() != CORE_INIT.read_bytes().replace(b"\r\n", b"\n"):
        raise CorePublicSurfaceError("core hub is not byte-identical to the derived rendering")
    return {
        "core_exports": len(current),
        "facade_only": len(facades),
        "gate_enforced": len(acceptance_abi_names()),
        "hub_consumers": len(hub_consumer_names() & current),
        "package_pure": len(pure),
        "unexplained": len(unexplained_core_exports()),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true", help="rewrite core/__init__.py")
    action.add_argument("--check", action="store_true", help="fail unless the hub is exact")
    args = parser.parse_args(argv)
    try:
        package_surface_partition()
        if args.write:
            CORE_INIT.write_bytes(render_core_init())
        summary = (
            validate_core_surface()
            if args.write or args.check
            else {
                "core_exports": len(current_core_export_names()),
                "facade_only": len(PACKAGE_FACADE_REASONS),
                "gate_enforced": len(acceptance_abi_names()),
                "hub_consumers": len(hub_consumer_names() & current_core_export_names()),
                "package_pure": len(_pure_package_bindings()),
                "unexplained": len(unexplained_core_exports()),
            }
        )
    except (CorePublicSurfaceError, OSError, ValueError, SyntaxError) as exc:
        print(json.dumps({"detail": str(exc), "status": "FAIL"}, sort_keys=True))
        return 1
    print(json.dumps({**summary, "status": "PASS"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
