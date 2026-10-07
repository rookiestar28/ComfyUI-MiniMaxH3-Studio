"""Generate the deterministic cross-stack architecture-fitness baseline.

The scanner reads Python and TypeScript as inert text. It imports no product, host, browser,
provider, media, or network module and it never follows a filesystem link. Accepted debt is an
exact upper bound: a row may disappear or shrink, while a new subject or larger count fails.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import stat
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple, cast

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = Path("governance/contracts/architecture_fitness_v1.json")
SCHEMA_ID = "h3-context-architecture-fitness/1"
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_MODULES = 640

ROUTE_RESPONSIBILITIES = ("application", "decode", "domain", "response", "store")

RULE_DESCRIPTIONS = {
    "PRODUCTION_GENERATED_BUILD_AUTHORITY": (
        "production source must not import a generated build output as an authority"
    ),
    "PRODUCTION_TEST_AUTHORITY": (
        "production source must not import tests or fixture data as an authority"
    ),
    "PY_APPLICATION_ADAPTER_DIRECTION": (
        "application handlers must not depend on HTTP or host adapters"
    ),
    "PY_CORE_FORBIDDEN_DEPENDENCY": (
        "the pure core must not import host, network, provider, media, or GPU stacks"
    ),
    "TS_GRAPH_WRITE_OWNERSHIP": (
        "only the bounded App Mode owned-write seam may call loadGraphData"
    ),
    "TS_HOST_QUEUE_OWNERSHIP": ("host queue callable ownership must converge on the queue seam"),
    "TS_PRESENTATION_HOST_MUTATION": (
        "presentation modules must not queue work or write the host graph"
    ),
}

FORBIDDEN_CORE_IMPORTS = frozenset(
    {
        "PIL",
        "aiohttp",
        "comfy",
        "cv2",
        "folder_paths",
        "httpx",
        "nodes",
        "numpy",
        "requests",
        "server",
        "torch",
        "transformers",
    }
)


class ArchitectureFitnessError(ValueError):
    """Raised when a fitness record would normalize or baseline new architecture debt."""


class BoundaryFinding(NamedTuple):
    rule_id: str
    subject: str
    occurrences: int


class KnownViolation(NamedTuple):
    rule_id: str
    subject: str
    occurrences: int
    owner: str
    reason: str


class OwnershipRule(NamedTuple):
    owner_id: str
    source_patterns: tuple[str, ...]
    artifacts: tuple[str, ...]
    checks: tuple[str, ...]


# M23-28 retired the last known-debt row: the host queue callable is read only by
# `frontend/src/host/queueSeam.ts`. An empty tuple is the steady state; a new row needs an
# owner item and a reason, and `enforce_findings` refuses an unowned or duplicate row.
KNOWN_VIOLATIONS: tuple[KnownViolation, ...] = ()

OWNERSHIP_RULES = (
    OwnershipRule(
        owner_id="contract_surface_inventory",
        source_patterns=(
            "comfyui_h3_context/contracts/**",
            "comfyui_h3_context/core/**",
            "frontend/src/contracts/**",
        ),
        artifacts=("comfyui_h3_context/contracts/contract_inventory_v1.json",),
        checks=("tests/test_contract_inventory.py",),
    ),
    OwnershipRule(
        owner_id="cross_language_contract_surface",
        source_patterns=(
            "comfyui_h3_context/core/**",
            "frontend/src/contracts/**",
        ),
        artifacts=(
            "comfyui_h3_context/contracts/cross_language_surface_v1.json",
            "frontend/src/contracts/generatedSurface.ts",
        ),
        checks=("tests/test_cross_language_surface.py",),
    ),
    OwnershipRule(
        owner_id="frontend_runtime_build",
        source_patterns=("frontend/src/**",),
        artifacts=(
            "comfyui_h3_context/contracts/build_provenance_v1.json",
            "governance/contracts/supply_chain_v1.json",
            "comfyui_h3_context/web/h3-context-sidebar.js",
        ),
        checks=(
            "frontend:build",
            "frontend:check",
            "frontend:test",
            "tests/test_build_provenance.py",
            "tests/test_supply_chain_manifest.py",
        ),
    ),
    OwnershipRule(
        owner_id="python_architecture_inventory",
        source_patterns=("comfyui_h3_context/**/*.py",),
        artifacts=("governance/contracts/architecture_inventory_v1.json",),
        checks=("tests/test_architecture_inventory.py",),
    ),
)

# --- M23-50 source reachability ------------------------------------------------------------
# CRITICAL: this axis is *static module-import reachability of the shipped source*. It is not the
# question `comfyui_h3_context/core/reachability.py` answers, which is whether a public workflow
# can reach a field or task mode through a named producer at runtime. Never conflate the two: a
# `product` class here says a shipped path imports the module, never that a capability is
# supported by the product.
SOURCE_REACHABILITY_CLASSES = (
    "product",
    "planned_consumer",
    "qualification",
    "evidence_only",
    "unresolved",
)
SOURCE_REACHABILITY_EVIDENCE = (
    "product_closure",
    "ambient_declaration",
    "finalized_plan",
    "script_import",
    "test_import",
    "none",
)
RETAINED_REASONS = {
    "product": "reachable from a shipped entry point",
    "planned_consumer": "named by a currently finalized plan",
    "qualification": "reached only from repository tooling under scripts/",
    "evidence_only": "reached only from test sources",
    "unresolved": "no static edge found; an undiscovered consumer is not an absent one",
}
# Product roots are what a host actually loads: the package's own registration modules and every
# adapter, plus the shipped frontend entry. `core/` is deliberately absent -- one core module
# importing another proves nothing about whether any shipped path reaches either of them.
PYTHON_PRODUCT_ROOT_DIRS = ("comfyui_h3_context", "comfyui_h3_context/adapters")
TYPESCRIPT_PRODUCT_ROOTS = ("frontend/src/entry.tsx",)
QUALIFICATION_ROOTS = ("scripts",)
EVIDENCE_ROOTS = ("tests", "frontend/tests", "frontend/e2e")
# A module whose only consumer is a plan that has not landed yet. Removing an entry here asserts
# that no finalized plan needs the module any more, which is a claim to check against that plan,
# never an inference from a low import count.
PLANNED_CONSUMERS: dict[str, tuple[str, ...]] = {
    "comfyui_h3_context/core/composition_contract.py": ("M25-11", "M25-12"),
}
MAX_REACHABILITY_OWNERS = 8
PACKAGE_HUB = "comfyui_h3_context/core/__init__.py"
PACKAGE_HUB_MODULE = "comfyui_h3_context.core"
# An ambient `.d.ts` is never imported: the compiler loads every declaration file under the roots
# `frontend/tsconfig.json` includes. Leaving it `unresolved` would tell a reader nothing consumes
# it, which is the false impression this item exists to remove.
TYPESCRIPT_CONFIG = "frontend/tsconfig.json"
AMBIENT_DECLARATION_SUFFIX = ".d.ts"

_TS_FROM_IMPORT = re.compile(
    r"\b(?:import|export)\s+(?:type\s+)?[^;]*?\s+from\s+[\"']([^\"']+)[\"']",
    re.MULTILINE,
)
_TS_SIDE_EFFECT_IMPORT = re.compile(r"\bimport\s*[\"']([^\"']+)[\"']")
_TS_DYNAMIC_IMPORT = re.compile(r"\bimport\s*\(\s*[\"']([^\"']+)[\"']\s*\)")
_TS_NAMED_FUNCTION = re.compile(r"\b(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
_QUEUE_MEMBER = re.compile(r"\.\s*queuePrompt\b")
_GRAPH_WRITE_CALL = re.compile(r"\.\s*loadGraphData!?\s*\(")
_TEST_SEGMENT = re.compile(r"(?:^|/)tests(?:/|$)")
_BUILD_SEGMENT = re.compile(r"(?:^|/)(?:build|dist|web)(?:/|$)")
_GRAPH_WRITE_SEAM_PATH = "frontend/src/host/canvasOwnedWrite.ts"
_GRAPH_WRITE_OWNERS = frozenset({"loadOwnedGraph", "writeValidatedGraph"})


def _is_linklike(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(is_junction and is_junction(path))


def _regular_sources(base: Path, suffixes: frozenset[str]) -> tuple[Path, ...]:
    if not base.is_dir() or _is_linklike(base):
        return ()
    found: list[Path] = []
    for directory, names, files in os.walk(base, followlinks=False):
        current = Path(directory)
        names[:] = sorted(name for name in names if not _is_linklike(current / name))
        for name in sorted(files):
            path = current / name
            if path.suffix not in suffixes or _is_linklike(path):
                continue
            mode = path.stat(follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                continue
            if path.stat(follow_symlinks=False).st_size > MAX_SOURCE_BYTES:
                raise ArchitectureFitnessError(f"source exceeds byte limit: {path.name}")
            found.append(path)
    return tuple(sorted(found))


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ArchitectureFitnessError("scanned path escaped the repository root") from exc


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except UnicodeError as exc:
        raise ArchitectureFitnessError(f"source is not strict UTF-8: {path.name}") from exc


def _python_module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _python_import_names(
    module: str, tree: ast.Module, *, is_package: bool = False
) -> tuple[str, ...]:
    package = module if is_package else module.rpartition(".")[0]
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = "." * node.level + (node.module or "")
                try:
                    base = importlib.util.resolve_name(relative, package)
                except (ImportError, ValueError):
                    continue
            else:
                base = node.module or ""
            if base:
                imports.add(base)
                imports.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return tuple(sorted(imports))


def _python_modules(root: Path) -> tuple[dict[str, object], ...]:
    package_root = root / "comfyui_h3_context"
    paths = _regular_sources(package_root, frozenset({".py"}))
    modules: dict[str, tuple[Path, ast.Module, str]] = {}
    for path in paths:
        text = _read(path)
        module = _python_module_name(root, path)
        modules[module] = (path, ast.parse(text), text)
    known = set(modules)
    rows: list[dict[str, object]] = []
    for module, (path, tree, text) in modules.items():
        imports: set[str] = set()
        for target in _python_import_names(module, tree, is_package=path.name == "__init__.py"):
            candidates = [target]
            while "." in candidates[-1]:
                candidates.append(candidates[-1].rpartition(".")[0])
            resolved = next((candidate for candidate in candidates if candidate in known), None)
            if resolved and resolved != module:
                imports.add(_relative(root, modules[resolved][0]))
        rows.append(
            {
                "imports": sorted(imports),
                "language": "python",
                "lines": text.count("\n") + (0 if not text or text.endswith("\n") else 1),
                "path": _relative(root, path),
            }
        )
    return tuple(sorted(rows, key=lambda row: str(row["path"])))


def _typescript_specs(text: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(_TS_FROM_IMPORT.findall(text))
            | set(_TS_SIDE_EFFECT_IMPORT.findall(text))
            | set(_TS_DYNAMIC_IMPORT.findall(text))
        )
    )


def _resolve_typescript_import(source: Path, specifier: str, known: set[Path]) -> Path | None:
    if not specifier.startswith("."):
        return None
    # CRITICAL: normalise lexically before comparing. `source.parent / "../x"` keeps the `..`
    # component, so every parent-relative specifier silently failed to match a scanned path and
    # the TypeScript half of the graph recorded only same-directory edges -- H3Sidebar.tsx showed
    # 6 of its 25 relative imports. Use `os.path.normpath`, never `Path.resolve()`: resolve()
    # touches the filesystem and follows links, which this scanner promises never to do.
    base = Path(os.path.normpath(source.parent / specifier))
    candidates = (
        base,
        base.with_suffix(".ts"),
        base.with_suffix(".tsx"),
        base / "index.ts",
        base / "index.tsx",
    )
    return next((candidate for candidate in candidates if candidate in known), None)


def _typescript_modules(root: Path) -> tuple[dict[str, object], ...]:
    source_root = root / "frontend/src"
    paths = _regular_sources(source_root, frozenset({".ts", ".tsx"}))
    known = set(paths)
    rows: list[dict[str, object]] = []
    for path in paths:
        text = _read(path)
        imports = {
            _relative(root, target)
            for specifier in _typescript_specs(text)
            if (target := _resolve_typescript_import(path, specifier, known)) is not None
        }
        rows.append(
            {
                "imports": sorted(imports),
                "language": "typescript",
                "lines": text.count("\n") + (0 if not text or text.endswith("\n") else 1),
                "path": _relative(root, path),
            }
        )
    return tuple(sorted(rows, key=lambda row: str(row["path"])))


def _route_decorator(node: ast.expr) -> tuple[str, str] | None:
    if not isinstance(node, ast.Call) or not node.args:
        return None
    method: str | None = None
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "routes"
        and node.func.attr in {"get", "post", "put", "delete", "patch"}
    ):
        method = node.func.attr.upper()
    elif isinstance(node.func, ast.Name) and node.func.id == "get_route":
        method = "GET"
    if method is None:
        return None
    argument = node.args[0]
    if isinstance(argument, ast.Name):
        path_symbol = argument.id
    elif isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        path_symbol = argument.value
    else:
        raise ArchitectureFitnessError("route path must be a literal or stable symbol")
    return method, path_symbol


def _route_responsibilities(tree: ast.Module, handler: ast.AST, text: str) -> list[str]:
    handler_text = ast.get_source_segment(text, handler) or ""
    imported_core = any(
        (
            isinstance(node, ast.Import)
            and any(alias.name.startswith("comfyui_h3_context.core") for alias in node.names)
        )
        or (
            isinstance(node, ast.ImportFrom)
            and ((node.module or "").startswith("comfyui_h3_context.core") or node.level > 0)
        )
        for node in tree.body
    )
    responsibilities = {"application"}
    # CRITICAL: since M23-47 a handler served by the shared seam receives decoded bytes
    # and returns a `RouteResult`, so it names neither `request` nor `json_response`.
    # Both spellings have to be recognised, or every converted route would silently drop
    # its decode and response rows and the inventory would read as if the responsibility
    # had disappeared rather than moved to the seam.
    if "request" in handler_text or "decode" in handler_text:
        responsibilities.add("decode")
    if "json_response" in handler_text or "RouteResult" in handler_text:
        responsibilities.add("response")
    if imported_core:
        responsibilities.add("domain")
    if re.search(r"\b(?:registry|ledger|store|workspace|coordinator|settings)\b", text, re.I):
        responsibilities.add("store")
    return sorted(responsibilities)


def _path_symbol(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise ArchitectureFitnessError("route path must be a literal or stable symbol")


def _policy_declarations(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Every `RoutePolicy(...)` in one module, keyed by the name it is assigned to."""

    declared: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        call = node.value
        if not isinstance(target, ast.Name) or not isinstance(call, ast.Call):
            continue
        if not (isinstance(call.func, ast.Name) and call.func.id == "RoutePolicy"):
            continue
        keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        method = "POST"
        declared_method = keywords.get("method")
        if declared_method is not None:
            if not isinstance(declared_method, ast.Constant) or not isinstance(
                declared_method.value, str
            ):
                raise ArchitectureFitnessError("route method must be a literal")
            method = declared_method.value
        declared[target.id] = (method, _path_symbol(keywords.get("path")))
    return declared


def _seam_registrations(tree: ast.Module) -> list[tuple[str, str]]:
    """Every `register_owned_route(policy, module, handler, ...)` call, as symbol pairs."""

    rows: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "register_owned_route" or len(node.args) < 3:
            continue
        policy, handler = node.args[0], node.args[2]
        if not isinstance(policy, ast.Name) or not isinstance(handler, ast.Name):
            raise ArchitectureFitnessError("a seam registration names its policy and its handler")
        rows.append((policy.id, handler.id))
    return rows


def _function_named(tree: ast.Module, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    return None


def _routes(root: Path) -> tuple[dict[str, object], ...]:
    adapter_root = root / "comfyui_h3_context/adapters"
    rows: list[dict[str, object]] = []
    for path in _regular_sources(adapter_root, frozenset({".py"})):
        text = _read(path)
        tree = ast.parse(text)
        module = _relative(root, path)
        # Routes served by the shared seam declare their policy as data and hand the seam a handler.
        declared = _policy_declarations(tree)
        for policy_symbol, handler_symbol in _seam_registrations(tree):
            if policy_symbol not in declared:
                raise ArchitectureFitnessError(f"{module}: registers an undeclared route policy")
            method, path_symbol = declared[policy_symbol]
            handler = _function_named(tree, handler_symbol)
            if handler is None:
                raise ArchitectureFitnessError(f"{module}: registers a handler it does not define")
            rows.append(
                {
                    "handler": handler_symbol,
                    "method": method,
                    "module": module,
                    "path_symbol": path_symbol,
                    "responsibilities": _route_responsibilities(tree, handler, text),
                }
            )
        # The two media routes drive their own edge; see comfyui_route_seam's module docstring.
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                route = _route_decorator(decorator)
                if route is None:
                    continue
                method, path_symbol = route
                rows.append(
                    {
                        "handler": node.name,
                        "method": method,
                        "module": module,
                        "path_symbol": path_symbol,
                        "responsibilities": _route_responsibilities(tree, node, text),
                    }
                )
    return tuple(sorted(rows, key=lambda row: (str(row["method"]), str(row["path_symbol"]))))


def _strip_ts_comments(text: str) -> str:
    output = list(text)
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote = character
            index += 1
            continue
        if character == "/" and following == "/":
            while index < len(text) and text[index] not in "\r\n":
                output[index] = " "
                index += 1
            continue
        if character == "/" and following == "*":
            output[index] = " "
            output[index + 1] = " "
            index += 2
            while index < len(text):
                if text[index] == "*" and index + 1 < len(text) and text[index + 1] == "/":
                    output[index] = " "
                    output[index + 1] = " "
                    index += 2
                    break
                if text[index] not in "\r\n":
                    output[index] = " "
                index += 1
            continue
        index += 1
    return "".join(output)


def _mask_ts_literals(text: str) -> str:
    output = list(text)
    quote: str | None = None
    escaped = False
    for index, character in enumerate(text):
        if quote is not None:
            if character not in "\r\n":
                output[index] = " "
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"', "`"}:
            quote = character
            output[index] = " "
    return "".join(output)


def _matching_delimiter(text: str, start: int, opening: str, closing: str) -> int | None:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _typescript_named_function_spans(text: str) -> tuple[tuple[str, int, int], ...]:
    masked = _mask_ts_literals(text)
    spans: list[tuple[str, int, int]] = []
    for match in _TS_NAMED_FUNCTION.finditer(masked):
        opening_parenthesis = match.end() - 1
        closing_parenthesis = _matching_delimiter(masked, opening_parenthesis, "(", ")")
        if closing_parenthesis is None:
            continue
        opening_brace = masked.find("{", closing_parenthesis + 1)
        if opening_brace < 0:
            continue
        closing_brace = _matching_delimiter(masked, opening_brace, "{", "}")
        if closing_brace is not None:
            spans.append((match.group(1), opening_brace, closing_brace))
    return tuple(spans)


def _graph_write_findings(subject: str, text: str) -> dict[str, int]:
    calls = tuple(_GRAPH_WRITE_CALL.finditer(text))
    if not calls:
        return {}
    if subject != _GRAPH_WRITE_SEAM_PATH:
        return {subject: len(calls)}

    spans = _typescript_named_function_spans(text)
    owner_counts: dict[str, int] = defaultdict(int)
    for call in calls:
        containers = [
            (end - start, name) for name, start, end in spans if start <= call.start() < end
        ]
        owner = min(containers)[1] if containers else "module"
        owner_counts[owner] += 1

    findings: dict[str, int] = {}
    for owner, count in owner_counts.items():
        # IMPORTANT: do not replace this function-bound guard with a file-wide allowance; two
        # calls in the wrong function would silently pass the owned graph-write boundary.
        allowed = 1 if owner in _GRAPH_WRITE_OWNERS else 0
        if count > allowed:
            findings[f"{subject}#{owner}"] = count - allowed
    return findings


def _add_finding(
    grouped: dict[tuple[str, str], int], rule_id: str, subject: str, count: int
) -> None:
    if count > 0:
        grouped[(rule_id, subject)] += count


def scan_boundary_findings(root: Path = ROOT) -> tuple[BoundaryFinding, ...]:
    grouped: dict[tuple[str, str], int] = defaultdict(int)
    package_root = root / "comfyui_h3_context"
    for path in _regular_sources(package_root, frozenset({".py"})):
        subject = _relative(root, path)
        tree = ast.parse(_read(path))
        module = _python_module_name(root, path)
        imports = _python_import_names(module, tree, is_package=path.name == "__init__.py")
        if subject.startswith("comfyui_h3_context/core/"):
            count = sum(target.split(".")[0] in FORBIDDEN_CORE_IMPORTS for target in imports)
            _add_finding(grouped, "PY_CORE_FORBIDDEN_DEPENDENCY", subject, count)
        if subject.startswith("comfyui_h3_context/application/"):
            count = sum(target.startswith("comfyui_h3_context.adapters") for target in imports)
            _add_finding(grouped, "PY_APPLICATION_ADAPTER_DIRECTION", subject, count)
        test_count = sum(target == "tests" or target.startswith("tests.") for target in imports)
        _add_finding(grouped, "PRODUCTION_TEST_AUTHORITY", subject, test_count)
        build_count = sum(
            target.startswith("comfyui_h3_context.web") or target.split(".")[0] in {"build", "dist"}
            for target in imports
        )
        _add_finding(grouped, "PRODUCTION_GENERATED_BUILD_AUTHORITY", subject, build_count)

    frontend_root = root / "frontend/src"
    for path in _regular_sources(frontend_root, frozenset({".ts", ".tsx"})):
        subject = _relative(root, path)
        text = _strip_ts_comments(_read(path))
        # IMPORTANT: call-like examples inside strings are inert; scan the literal-masked source
        # or documentation text would create false architecture debt and block unrelated changes.
        executable_text = _mask_ts_literals(text)
        queue_count = len(_QUEUE_MEMBER.findall(executable_text))
        graph_findings = _graph_write_findings(subject, executable_text)
        graph_count = sum(graph_findings.values())
        if subject.startswith("frontend/src/components/"):
            _add_finding(
                grouped,
                "TS_PRESENTATION_HOST_MUTATION",
                subject,
                queue_count + graph_count,
            )
        if queue_count and subject != "frontend/src/host/queueSeam.ts":
            _add_finding(grouped, "TS_HOST_QUEUE_OWNERSHIP", subject, queue_count)
        for graph_subject, count in graph_findings.items():
            _add_finding(grouped, "TS_GRAPH_WRITE_OWNERSHIP", graph_subject, count)
        specs = tuple(specifier.replace("\\", "/") for specifier in _typescript_specs(text))
        test_count = sum(bool(_TEST_SEGMENT.search(specifier.lstrip("./"))) for specifier in specs)
        _add_finding(grouped, "PRODUCTION_TEST_AUTHORITY", subject, test_count)
        build_count = sum(
            bool(_BUILD_SEGMENT.search(specifier.lstrip("./"))) for specifier in specs
        )
        _add_finding(grouped, "PRODUCTION_GENERATED_BUILD_AUTHORITY", subject, build_count)
    return tuple(
        BoundaryFinding(rule_id=rule, subject=subject, occurrences=count)
        for (rule, subject), count in sorted(grouped.items())
    )


def enforce_findings(
    findings: Iterable[BoundaryFinding],
    *,
    known_violations: Iterable[KnownViolation] = KNOWN_VIOLATIONS,
) -> None:
    known_rows = tuple(known_violations)
    known: dict[tuple[str, str], KnownViolation] = {}
    for row in known_rows:
        if row.rule_id not in RULE_DESCRIPTIONS:
            raise ArchitectureFitnessError(f"unknown known-debt rule: {row.rule_id}")
        if not row.owner or not row.reason or row.occurrences < 1:
            raise ArchitectureFitnessError(f"unowned known-debt row: {row.rule_id}")
        key = (row.rule_id, row.subject)
        if key in known:
            raise ArchitectureFitnessError(f"duplicate known-debt row: {row.rule_id}")
        known[key] = row
    failures: list[str] = []
    for finding in findings:
        expected = known.get((finding.rule_id, finding.subject))
        if expected is None:
            failures.append(
                f"{finding.rule_id} new subject {finding.subject} count={finding.occurrences}"
            )
        elif finding.occurrences > expected.occurrences:
            failures.append(
                f"{finding.rule_id} grew at {finding.subject}: "
                f"{finding.occurrences}>{expected.occurrences}"
            )
    if failures:
        # IMPORTANT: include every stable rule ID. Reporting only the first finding lets a new
        # edge hide behind an older row and makes planted-rule failures order-dependent.
        raise ArchitectureFitnessError("; ".join(failures))


def _known_violation_rows(findings: tuple[BoundaryFinding, ...]) -> list[dict[str, object]]:
    current = {(row.rule_id, row.subject): row for row in findings}
    result: list[dict[str, object]] = []
    for known in KNOWN_VIOLATIONS:
        observed = current.get((known.rule_id, known.subject))
        if observed is None:
            continue
        result.append(
            {
                "count": observed.occurrences,
                "owner": known.owner,
                "reason": known.reason,
                "rule_id": known.rule_id,
                "subject": known.subject,
            }
        )
    return result


def _ownership_rows() -> list[dict[str, object]]:
    return [
        {
            "artifacts": list(rule.artifacts),
            "checks": list(rule.checks),
            "owner_id": rule.owner_id,
            "source_patterns": list(rule.source_patterns),
        }
        for rule in sorted(OWNERSHIP_RULES, key=lambda row: row.owner_id)
    ]


def select_impact(changed_paths: Iterable[str]) -> dict[str, object]:
    changed = sorted({path.replace("\\", "/") for path in changed_paths})
    if any(path.startswith("/") or ":" in path or ".." in path.split("/") for path in changed):
        raise ArchitectureFitnessError("changed paths must be repository-relative")
    owners: set[str] = set()
    artifacts: set[str] = set()
    checks: set[str] = set()
    for rule in OWNERSHIP_RULES:
        if not any(
            fnmatch.fnmatchcase(path, pattern)
            for path in changed
            for pattern in rule.source_patterns
        ):
            continue
        owners.add(rule.owner_id)
        artifacts.update(rule.artifacts)
        checks.update(rule.checks)
    return {
        "artifacts": sorted(artifacts),
        "changed_paths": changed,
        "checks": sorted(checks),
        "owners": sorted(owners),
    }


def _hub_symbol_modules(root: Path) -> dict[str, str]:
    """Map each name the `core` hub re-exports back to the module that defines it.

    A consumer writing `from comfyui_h3_context.core import Foo` produces an import edge to the
    hub, not to `Foo`'s module. Without this mapping every hub consumer collapses onto one row and
    the modules behind the hub look unreached when they are not.
    """

    hub = root / PACKAGE_HUB
    if not hub.is_file():
        return {}
    symbols: dict[str, str] = {}
    for node in ast.walk(ast.parse(_read(hub))):
        if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
            continue
        target = hub.parent / f"{node.module.replace('.', '/')}.py"
        if not target.is_file():
            continue
        relative = _relative(root, target)
        for alias in node.names:
            if alias.name != "*":
                symbols[alias.asname or alias.name] = relative
    return symbols


def _hub_aliases(tree: ast.Module) -> set[str]:
    """Names bound to the `core` hub in one module, so attribute reads on it can be attributed.

    CRITICAL: only these names are followed. Matching every attribute in a file against the hub's
    symbol table would manufacture edges out of unrelated code, and a class narrowed on
    manufactured evidence is worse than an honest `unresolved` -- it reads as proof.
    """

    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "comfyui_h3_context.core":
                    aliases.add(alias.asname or "core")
        elif isinstance(node, ast.ImportFrom) and node.module == "comfyui_h3_context":
            for alias in node.names:
                if alias.name == "core":
                    aliases.add(alias.asname or "core")
    return aliases


def _importlib_literals(tree: ast.Module) -> set[str]:
    """Literal `import_module("…")` targets. A computed argument is not evidence of anything."""

    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        if isinstance(function, ast.Attribute):
            name: str | None = function.attr
        elif isinstance(function, ast.Name):
            name = function.id
        else:
            name = None
        if name != "import_module":
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            targets.add(first.value)
    return targets


def _resolve_python_target(target: str, known: dict[str, Path], root: Path) -> str | None:
    candidates = [target]
    while "." in candidates[-1]:
        candidates.append(candidates[-1].rpartition(".")[0])
    for candidate in candidates:
        if candidate in known:
            return _relative(root, known[candidate])
    return None


def _python_consumer_edges(
    root: Path, base: str, known: dict[str, Path], hub_symbols: dict[str, str]
) -> dict[str, set[str]]:
    """Package modules each file under `base` reaches, by every evidence form this tool supports.

    `base` contributes no rows to the artifact: `scripts/` and `tests/` are not shipped, and the
    artifact describes the shipped surface. They are read only so a package row can name the
    consumer that keeps it alive.
    """

    edges: dict[str, set[str]] = {}
    for path in _regular_sources(root / base, frozenset({".py"})):
        try:
            tree = ast.parse(_read(path))
        except SyntaxError:
            continue
        reached: set[str] = set()
        module_name = _python_module_name(root, path)
        for target in _python_import_names(module_name, tree):
            resolved = _resolve_python_target(target, known, root)
            if resolved is not None:
                reached.add(resolved)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "comfyui_h3_context.core":
                for alias in node.names:
                    defining = hub_symbols.get(alias.name)
                    if defining is not None:
                        reached.add(defining)
        aliases = _hub_aliases(tree)
        if aliases:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                value = node.value
                if isinstance(value, ast.Name) and value.id in aliases:
                    defining = hub_symbols.get(node.attr)
                    if defining is not None:
                        reached.add(defining)
        for target in _importlib_literals(tree):
            resolved = _resolve_python_target(target, known, root)
            if resolved is not None:
                reached.add(resolved)
        if reached:
            edges[_relative(root, path)] = reached
    return edges


def _typescript_consumer_edges(root: Path, base: str, known: set[Path]) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for path in _regular_sources(root / base, frozenset({".ts", ".tsx"})):
        reached = {
            _relative(root, target)
            for specifier in _typescript_specs(_read(path))
            if (target := _resolve_typescript_import(path, specifier, known)) is not None
        }
        if reached:
            edges[_relative(root, path)] = reached
    return edges


def _closure(seeds: Iterable[str], graph: dict[str, tuple[str, ...]]) -> dict[str, set[str]]:
    """Every module each seed reaches transitively, keyed by module and valued by its seeds."""

    reached: dict[str, set[str]] = {}
    for seed in seeds:
        stack = [seed]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(graph.get(current, ()))
        for module in seen:
            reached.setdefault(module, set()).add(seed)
    return reached


def _attributed_closure(
    edges: dict[str, set[str]], graph: dict[str, tuple[str, ...]]
) -> dict[str, set[str]]:
    """Modules reachable from a set of consumers, each keyed back to the consumers that reach it."""

    seed_consumers: dict[str, set[str]] = {}
    for consumer, targets in edges.items():
        for module in targets:
            seed_consumers.setdefault(module, set()).add(consumer)
    reached = _closure(seed_consumers, graph)
    return {
        module: {consumer for seed in seeds for consumer in seed_consumers[seed]}
        for module, seeds in reached.items()
    }


def _ambient_declaration_roots(root: Path) -> tuple[str, ...]:
    """Repository-relative roots whose declaration files the shipped compiler config loads."""

    config = root / TYPESCRIPT_CONFIG
    if not config.is_file():
        return ()
    try:
        document = json.loads(_read(config))
    except json.JSONDecodeError as exc:
        raise ArchitectureFitnessError("typescript configuration is not valid JSON") from exc
    included = document.get("include")
    if not isinstance(included, list):
        return ()
    parent = config.parent
    roots = []
    for entry in included:
        if isinstance(entry, str) and (parent / entry).is_dir():
            roots.append(_relative(root, parent / entry))
    return tuple(sorted(roots))


def _owners(values: Iterable[str]) -> list[str]:
    return sorted(set(values))[:MAX_REACHABILITY_OWNERS]


def _python_module_edges(
    root: Path, path: Path, tree: ast.Module, known: dict[str, Path], hub_symbols: dict[str, str]
) -> set[str]:
    """Edges out of one package module, with hub re-exports attributed to their defining module."""

    module_name = _python_module_name(root, path)
    is_package = path.name == "__init__.py"
    package = module_name if is_package else module_name.rpartition(".")[0]
    edges: set[str] = set()
    for target in _python_import_names(module_name, tree, is_package=is_package):
        resolved = _resolve_python_target(target, known, root)
        if resolved is not None and resolved != _relative(root, path):
            edges.add(resolved)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        # CRITICAL: resolve a relative import against the *importing module's own package* before
        # asking whether it names the hub. Treating every level-1 `from . import X` as the core hub
        # over-credits each package that is not `core`: an `adapters` module importing a bare name
        # that happens to collide with a hub export would be recorded as reaching that symbol's
        # module, and a spurious `product` row reads as proof rather than as a guess.
        if node.level:
            try:
                module = importlib.util.resolve_name(
                    "." * node.level + (node.module or ""), package
                )
            except (ImportError, ValueError):
                continue
        else:
            module = node.module or ""
        if module != PACKAGE_HUB_MODULE:
            continue
        for alias in node.names:
            defining = hub_symbols.get(alias.name)
            if defining is not None and defining != _relative(root, path):
                edges.add(defining)
    for alias_name in _hub_aliases(tree):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            value = node.value
            if isinstance(value, ast.Name) and value.id == alias_name:
                defining = hub_symbols.get(node.attr)
                if defining is not None:
                    edges.add(defining)
    for target in _importlib_literals(tree):
        resolved = _resolve_python_target(target, known, root)
        if resolved is not None:
            edges.add(resolved)
    return edges


def _reachability_graph(
    root: Path,
    modules: tuple[dict[str, object], ...],
    known: dict[str, Path],
    hub_symbols: dict[str, str],
) -> dict[str, tuple[str, ...]]:
    """The traversal graph, which is deliberately not the artifact's own `imports` graph.

    CRITICAL: `comfyui_h3_context/core/__init__.py` is a re-export hub, so its own import list is
    every module it re-exports. Letting those edges propagate makes one `from .core import Foo`
    anywhere reach all of `core`, and the classification collapses -- 286 of 302 rows come out
    `product` and the artifact stops saying anything. Edges *into* the hub are kept, because a
    consumer really does load it; edges *out of* it are dropped, and the symbols a consumer names
    are attributed to the modules that define them instead. Restoring the hub's outgoing edges
    would silently make every row `product` again while every test still passed.
    """

    graph: dict[str, tuple[str, ...]] = {}
    for row in modules:
        relative = str(row["path"])
        if row["language"] != "python":
            graph[relative] = tuple(str(value) for value in cast(list[str], row["imports"]))
            continue
        if relative == PACKAGE_HUB:
            graph[relative] = ()
            continue
        path = root / relative
        graph[relative] = tuple(
            sorted(_python_module_edges(root, path, ast.parse(_read(path)), known, hub_symbols))
        )
    return graph


def _source_reachability(
    root: Path, modules: tuple[dict[str, object], ...]
) -> dict[str, dict[str, object]]:
    """Assign one evidence-backed class to every shipped module row.

    The order below is the precedence and it is deliberate. A module a shipped path reaches is
    `product` whoever else imports it; a module nothing reaches is `unresolved` rather than dead.
    `unresolved` records that this tool cannot see a consumer, never that none exists -- the hub is
    also read through attribute access and `importlib`, and HC-12 retained a hub name only where
    something imports it.
    """

    python_paths = {str(row["path"]) for row in modules if row["language"] == "python"}
    package_modules: dict[str, Path] = {}
    for relative in python_paths:
        path = root / relative
        package_modules[_python_module_name(root, path)] = path
    hub_symbols = _hub_symbol_modules(root)
    graph = _reachability_graph(root, modules, package_modules, hub_symbols)

    product_seeds = [
        relative
        for relative in python_paths
        if relative.rpartition("/")[0] in PYTHON_PRODUCT_ROOT_DIRS
    ]
    product_seeds.extend(target for target in TYPESCRIPT_PRODUCT_ROOTS if target in graph)
    product = _closure(product_seeds, graph)

    typescript_known = {root / relative for relative in graph if relative.endswith((".ts", ".tsx"))}
    qualification_edges: dict[str, set[str]] = {}
    for base in QUALIFICATION_ROOTS:
        qualification_edges.update(_python_consumer_edges(root, base, package_modules, hub_symbols))
    evidence_edges: dict[str, set[str]] = {}
    for base in EVIDENCE_ROOTS:
        if base.startswith("frontend/"):
            evidence_edges.update(_typescript_consumer_edges(root, base, typescript_known))
        else:
            evidence_edges.update(_python_consumer_edges(root, base, package_modules, hub_symbols))

    ambient_roots = _ambient_declaration_roots(root)
    qualification = _attributed_closure(qualification_edges, graph)
    evidence = _attributed_closure(evidence_edges, graph)

    rows: dict[str, dict[str, object]] = {}
    for relative in graph:
        if relative in product:
            row = ("product", "product_closure", _owners(product[relative]))
        elif relative.endswith(AMBIENT_DECLARATION_SUFFIX) and any(
            relative.startswith(f"{ambient_root}/") for ambient_root in ambient_roots
        ):
            row = ("product", "ambient_declaration", [TYPESCRIPT_CONFIG])
        elif relative in PLANNED_CONSUMERS:
            row = ("planned_consumer", "finalized_plan", _owners(PLANNED_CONSUMERS[relative]))
        elif relative in qualification:
            row = ("qualification", "script_import", _owners(qualification[relative]))
        elif relative in evidence:
            row = ("evidence_only", "test_import", _owners(evidence[relative]))
        else:
            row = ("unresolved", "none", [])
        rows[relative] = {
            "class": row[0],
            "evidence": row[1],
            "owners": row[2],
            "retained_reason": RETAINED_REASONS[row[0]],
        }
    return rows


def build_inventory(root: Path = ROOT) -> dict[str, object]:
    modules = (*_python_modules(root), *_typescript_modules(root))
    if not modules or len(modules) > MAX_MODULES:
        raise ArchitectureFitnessError("module inventory is empty or exceeds its bound")
    modules = tuple(sorted(modules, key=lambda row: str(row["path"])))
    reachability = _source_reachability(root, modules)
    modules = tuple(
        {**row, "source_reachability": reachability[str(row["path"])]} for row in modules
    )
    findings = scan_boundary_findings(root)
    enforce_findings(findings)
    finding_totals: dict[str, int] = defaultdict(int)
    for finding in findings:
        finding_totals[finding.rule_id] += finding.occurrences
    boundary_rules = [
        {
            "description": description,
            "finding_count": finding_totals.get(rule_id, 0),
            "rule_id": rule_id,
            "status": "known_debt" if finding_totals.get(rule_id, 0) else "pass",
        }
        for rule_id, description in sorted(RULE_DESCRIPTIONS.items())
    ]
    body: dict[str, object] = {
        "boundary_rules": boundary_rules,
        "entrypoints": [{"kind": "shipped_frontend_runtime", "path": "frontend/src/entry.tsx"}],
        "known_violations": _known_violation_rows(findings),
        "modules": list(modules),
        "ownership_edges": _ownership_rows(),
        "routes": list(_routes(root)),
        "schema": SCHEMA_ID,
    }
    fingerprint_payload = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {**body, "fingerprint": "sha256:" + hashlib.sha256(fingerprint_payload).hexdigest()}


def artifact_bytes(document: dict[str, object]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the artifact")
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale")
    parser.add_argument(
        "--changed", action="append", default=[], help="report owners for one changed path"
    )
    args = parser.parse_args(argv)
    try:
        document = build_inventory(ROOT)
        expected = artifact_bytes(document)
        target = ROOT / ARTIFACT_PATH
        if args.write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(expected)
        if args.check and (target.read_bytes() if target.is_file() else b"") != expected:
            raise ArchitectureFitnessError("architecture fitness artifact is stale")
        result: dict[str, object] = {
            "fingerprint": document["fingerprint"],
            "known_violations": len(document["known_violations"]),  # type: ignore[arg-type]
            "modules": len(document["modules"]),  # type: ignore[arg-type]
            "routes": len(document["routes"]),  # type: ignore[arg-type]
            "schema": SCHEMA_ID,
            "status": "PASS",
        }
        if args.changed:
            result["impact"] = select_impact(args.changed)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ArchitectureFitnessError, OSError, SyntaxError, UnicodeError, ValueError) as exc:
        print(json.dumps({"detail": str(exc), "status": "FAIL"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
