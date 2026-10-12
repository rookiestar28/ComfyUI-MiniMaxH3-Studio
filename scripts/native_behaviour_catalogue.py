"""Admission boundary for independent, revision-bound behaviour evidence."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "governance/contracts/native_behaviour_catalogue_v1.schema.json"
CATALOGUE = ROOT / "governance/contracts/native_behaviour_catalogue_v1.json"
_PRIVATE_PARTS = {".planning", "reference", ".reference", ".tmp", ".git", ".venv", ".venv-wsl"}
_DEFINITION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class CatalogueError(ValueError):
    """Behaviour evidence is incomplete, open or derived from the product."""


def _public_file(root: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", name) or any(
        part in {".", ".."} or part.lower() in _PRIVATE_PARTS for part in parts
    ):
        raise CatalogueError("ignored or escaping evidence path")
    path = root / name
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise CatalogueError("missing or escaping evidence file")
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--no-index", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if result.returncode != 1:
            raise CatalogueError("ignored or unavailable Git evidence path")
    return path


def _tree(path: Path) -> ast.Module:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise CatalogueError("evidence source exceeds parsing budget")
    try:
        return ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, SyntaxError):
        raise CatalogueError("evidence source is not parseable") from None


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        raise CatalogueError("oracle anchor metadata must be literal") from None


def _oracle_authorities(root: Path, registered: set[str]) -> dict[str, set[str]]:
    modules: dict[str, set[str]] = {}
    directory = root / "tests/oracles"
    for path in sorted(directory.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        tree = _tree(_public_file(root, relative))
        if path.name == "__init__.py":
            # CRITICAL: inert package init prevents product loads before oracle checks.
            if any(
                not (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                )
                for node in tree.body
            ):
                raise CatalogueError("oracle package initialization must remain inert")
            continue
        metadata = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "SOURCE_ANCHORS"
                for target in node.targets
            )
        ]
        if len(metadata) != 1:
            raise CatalogueError("oracle source anchor metadata is missing or ambiguous")
        anchors = _literal(metadata[0])
        if (
            not isinstance(anchors, tuple)
            or not anchors
            or any(not isinstance(anchor, str) or anchor not in registered for anchor in anchors)
        ):
            raise CatalogueError("oracle source anchor authority is not registered")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for imported in imports:
                    base = imported.split(".", 1)[0]
                    if base in {"importlib", "runpy"}:
                        raise CatalogueError("dynamic oracle imports are forbidden")
                    if (
                        base not in sys.stdlib_module_names
                        or base in {"comfyui_h3_context", "torch"}
                        or isinstance(node, ast.ImportFrom)
                        and node.level
                    ):
                        raise CatalogueError("product or non-stdlib oracle import")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"__import__", "eval", "exec", "compile"}:
                    raise CatalogueError("dynamic oracle execution is forbidden")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                segments = re.split(r"[/\\]", node.value.lower())
                if any(segment in _PRIVATE_PARTS for segment in segments):
                    raise CatalogueError("ignored corpus is not an oracle dependency")
        module = ".".join(path.relative_to(root / "tests").with_suffix("").parts)
        modules[module] = set(anchors)
    if not modules:
        raise CatalogueError("independent source-anchored oracles are absent")
    return modules


def _test_function(
    tree: ast.Module, selectors: list[str]
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    scope: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef = tree
    for selector in selectors:
        matches = [
            node for node in scope.body if isinstance(node, _DEFINITION) and node.name == selector
        ]
        if len(matches) != 1:
            raise CatalogueError("evidence test is missing or ambiguous")
        scope = matches[0]
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise CatalogueError("evidence must identify a test function")
    return scope


def _used_oracles(
    tree: ast.Module, function: ast.FunctionDef | ast.AsyncFunctionDef, modules: dict[str, set[str]]
) -> set[str]:
    aliases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                module = f"oracles.{alias.name}" if node.module == "oracles" else node.module
                if module in modules:
                    aliases[alias.asname or alias.name] = modules[module]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in modules:
                    aliases[alias.asname or alias.name.split(".")[0]] = modules[alias.name]
    definitions = {node.name: node for node in ast.walk(tree) if isinstance(node, _DEFINITION)}
    pending: list[ast.AST] = [function]
    visited: set[str] = set()
    result: set[str] = set()
    while pending:
        current = pending.pop()
        for node in ast.walk(current):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                result.update(aliases.get(node.id, ()))
                if node.id in definitions and node.id not in visited:
                    visited.add(node.id)
                    pending.append(definitions[node.id])
    return result


def _strict_defect(function: ast.FunctionDef | ast.AsyncFunctionDef, defect: str) -> bool:
    for decorator in function.decorator_list:
        if (
            not isinstance(decorator, ast.Call)
            or ast.unparse(decorator.func) != "pytest.mark.xfail"
        ):
            continue
        keywords = {keyword.arg: keyword.value for keyword in decorator.keywords}
        strict = keywords.get("strict")
        reason = keywords.get("reason")
        if (
            isinstance(strict, ast.Constant)
            and strict.value is True
            and isinstance(reason, ast.Constant)
            and isinstance(reason.value, str)
            and defect in reason.value
        ):
            return True
    return False


def validate_catalogue(document: object, *, root: Path) -> None:
    """Admit closed evidence with existing consumers and actually used independent authorities."""
    try:
        jsonschema.Draft202012Validator(
            json.loads(SCHEMA.read_bytes()), format_checker=jsonschema.FormatChecker()
        ).validate(document)
    except jsonschema.ValidationError:
        raise CatalogueError("catalogue does not satisfy its closed schema") from None
    if not isinstance(document, dict):
        raise CatalogueError("catalogue must be an object")
    entries = document["entries"]
    identities: set[str] = set()
    anchors: dict[str, dict[str, Any]] = {}
    for entry in entries:
        identity = entry["behaviour_id"]
        if identity in identities:
            raise CatalogueError("duplicate behaviour identity")
        identities.add(identity)
        for anchor in entry["anchors"]:
            if anchor["anchor_id"] in anchors and anchors[anchor["anchor_id"]] != anchor:
                raise CatalogueError("ambiguous source anchor identity")
            anchors[anchor["anchor_id"]] = anchor
        if not entry["reason"].strip() or not entry["statement"].strip():
            raise CatalogueError("evidence requires an explicit reason and statement")
    modules = _oracle_authorities(root, set(anchors))
    paths: dict[str, Path] = {}
    trees: dict[str, ast.Module] = {}
    for entry in entries:
        for name in entry["repo_sites"]:
            if name not in paths:
                paths[name] = _public_file(root, name)
        expected = {anchor["anchor_id"] for anchor in entry["anchors"]}
        for reference in entry["oracle_tests"]:
            name, *selectors = reference.split("::")
            if not selectors or not name.endswith(".py") or not name.startswith("tests/"):
                raise CatalogueError("bounded Python test reference required")
            if name not in trees:
                trees[name] = _tree(_public_file(root, name))
            function = _test_function(trees[name], selectors)
            if entry["disposition"] in {"covered", "divergence"}:
                # IMPORTANT: an unused import cannot turn product self-comparison into parity proof.
                if not (_used_oracles(trees[name], function, modules) & expected):
                    raise CatalogueError("independent oracle authority is not used by the test")
            if entry["disposition"] == "divergence" and not _strict_defect(
                function, entry["defect"]
            ):
                raise CatalogueError("divergence needs its owned strict expected failure")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", required=True)
    args = parser.parse_args()
    del args
    try:
        document = json.loads(CATALOGUE.read_bytes())
        validate_catalogue(document, root=ROOT)
    except (CatalogueError, OSError, ValueError) as error:
        print(f"Native behaviour catalogue: FAIL ({error})")
        return 1
    print(f"Native behaviour catalogue: PASS ({len(document['entries'])} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
