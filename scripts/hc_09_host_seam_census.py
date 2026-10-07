"""Statically verify the closed HC-09 ComfyUI host-seam source census.

The scanner reads repository source as text and reports only closed seam IDs and
repository-relative paths. It never imports scanned modules, contacts a host, or
retains source values. Unknown ``app``/``api``/``LiteGraph`` members fail closed.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal, TypedDict

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CENSUS = Path("comfyui_h3_context/contracts/host_seam_census_v1.json")
DEFAULT_FIXTURE = Path("comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json")


class HostSeamCensusError(ValueError):
    """Raised when source and the closed host-seam census disagree."""


class HostSeamCensusSummary(TypedDict):
    status: Literal["PASS"]
    seams: int
    source_paths: int
    unknown_members: int


_DETECTORS: Mapping[str, re.Pattern[str]] = {
    "backend.comfy_api.latest": re.compile(r"import_module\(\s*[\"']comfy_api\.latest[\"']"),
    "backend.folder_paths.get_filename_list": re.compile(r"\bget_filename_list\b"),
    "backend.node_class_mappings": re.compile(r"\bNODE_CLASS_MAPPINGS\b"),
    "backend.node_display_name_mappings": re.compile(r"\bNODE_DISPLAY_NAME_MAPPINGS\b"),
    "backend.prompt_server.routes": re.compile(r"\bPromptServer\b"),
    "backend.web_directory": re.compile(r"\bWEB_DIRECTORY\b"),
    # IMPORTANT: a family whose member read is delegated to a typed probe (M23-28) is detected by
    # the probe's name as well as by the raw member forms, so the probe owner and every caller
    # stay in the census even though neither names the host member directly.
    "frontend.api.event_target": re.compile(
        r"(?:api:\s*EventTarget|HostApi = EventTarget|"
        r"dependencies\.api\.(?:add|remove)EventListener|\bprobeApiEventTarget\b)"
    ),
    "frontend.api.fetch_api": re.compile(r"\bfetchApi\b"),
    "frontend.api.file_url": re.compile(r"\bfileURL\b"),
    "frontend.api.queue_prompt": re.compile(r"(?:api\.queuePrompt\b|queuePrompt\?\s*\()"),
    "frontend.app.extension_manager.get_sidebar_tabs": re.compile(r"\bgetSidebarTabs\b"),
    "frontend.app.extension_manager.register_sidebar_tab": re.compile(r"\bregisterSidebarTab\b"),
    "frontend.app.extension_manager.unregister_sidebar_tab": re.compile(
        r"\bunregisterSidebarTab\b"
    ),
    "frontend.app.extension_manager.workflow.active_workflow": re.compile(r"\bactiveWorkflow\b"),
    "frontend.app.extension_manager.workflow.open_workflows": re.compile(r"\bopenWorkflows\b"),
    "frontend.app.graph": re.compile(r"(?:\bapp\.graph\b|\bgraph\?:\s*\{)"),
    "frontend.app.graph.change": re.compile(r"(?:\bgraph\.change!?\s*\(|\bchange\?\s*\(\s*\)\s*:)"),
    "frontend.app.graph.events": re.compile(
        r"(?:\bapp\.graph\?\.events\b|\bevents\?:\s*(?:EventTarget|EventSource)|"
        r"\bprobeGraphEvents\b)"
    ),
    "frontend.app.graph.get_node_by_id": re.compile(
        r"(?:\bgraph\.getNodeById\b|\bgetNodeById\?\s*\()"
    ),
    "frontend.app.graph.serialize": re.compile(
        r"(?:\bapp\.graph[^\n;]*\bserialize\b|\bgraph\?:\s*\{[^\n}]*\bserialize\b|"
        r"\bserialize\?\s*:\s*\()"
    ),
    "frontend.app.graph.set_dirty_canvas": re.compile(
        r"(?:\bgraph\.setDirtyCanvas!?\s*\(|\bsetDirtyCanvas\?\s*\()"
    ),
    "frontend.app.graph_to_prompt": re.compile(r"\bgraphToPrompt\b"),
    "frontend.app.load_api_json": re.compile(r"\bloadApiJson\b"),
    "frontend.app.load_graph_data": re.compile(r"\bloadGraphData\b"),
    "frontend.app.modal_keyboard_guard": re.compile(
        r"\b(?:probeModalKeyboardGuard|acquireModalKeyboardGuard|maskeditor_is_opended)\b"
    ),
    "frontend.app.canvas.keyboard_capture": re.compile(
        r"\b(?:probeCanvasKeyboardGuard|acquireCanvasKeyboardGuard|_key_callback|_ghostKeyHandler)\b"
    ),
    "frontend.app.register_extension": re.compile(r"\bregisterExtension\b"),
    "frontend.app.ui_settings": re.compile(r"(?:\bapp\.ui\??\.settings\b|\bui\?:\s*\{)"),
    "frontend.litegraph.registered_node_types": re.compile(
        r"(?:\bLiteGraph\??\.[^\n;]*\bregistered_node_types\b|\breadOfficialAssetInventoryFromNodeDefinitions\b)"
    ),
}

_ALLOWED_APP_MEMBERS = frozenset(
    {
        "extensionManager",
        "graph",
        "graphToPrompt",
        "loadApiJson",
        "loadGraphData",
        "registerExtension",
        "ui",
    }
)
_ALLOWED_API_MEMBERS = frozenset(
    {"addEventListener", "fetchApi", "fileURL", "queuePrompt", "removeEventListener"}
)
_ALLOWED_LITEGRAPH_MEMBERS = frozenset({"registered_node_types"})
_HOST_MEMBER = re.compile(r"\b(app|api)\.([A-Za-z_$][A-Za-z0-9_$]*)")
_LITEGRAPH_MEMBER = re.compile(r"\bLiteGraph\??\.([A-Za-z_$][A-Za-z0-9_$]*)")
_LITERAL_OR_COMMENT = re.compile(
    r"/\*.*?\*/|//[^\r\n]*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`",
    re.DOTALL,
)
_TEST_HOST_OBJECT = re.compile(r"\b(?:const|let)\s+(app|api)\s*=\s*\{")
_DIRECT_HOST_CONSTRUCTOR = re.compile(
    r"\b(createSidebarHost|createAppModeController)\s*\(\s*\{(?!\s*\.\.\.)"
)
_TEST_HOST_MEMBER = re.compile(
    r"\b(extensionManager|fetchApi|fileURL|graph|graphToPrompt|loadApiJson|loadGraphData|"
    r"queuePrompt|registerExtension|registered_node_types|ui)\s*:"
)
_FIXTURE_DOUBLE_AUTHORITIES = frozenset(
    {
        "frontend/tests/fixtures/entryHostModules.ts",
        "frontend/tests/support/hostSeamFixture.ts",
        "frontend/tests/support/hostSeamTestDouble.ts",
    }
)
_PYTHON_FIXTURE_DOUBLE_AUTHORITY = "scripts/hc_09_host_seam_test_double.py"
_GOVERNED_PYTHON_MODULES = frozenset({"comfy_api.latest", "folder_paths", "nodes", "server"})
_GOVERNED_PYTHON_MEMBERS = frozenset(
    {
        "NODE_CLASS_MAPPINGS",
        "NODE_DISPLAY_NAME_MAPPINGS",
        "PromptServer",
        "get_filename_list",
    }
)


def _python_module_type_name(call: ast.Call) -> str | None:
    function = call.func
    is_module_type = isinstance(function, ast.Name) and function.id == "ModuleType"
    if isinstance(function, ast.Attribute):
        is_module_type = (
            isinstance(function.value, ast.Name)
            and function.value.id == "types"
            and function.attr == "ModuleType"
        )
    if (
        not is_module_type
        or not call.args
        or not isinstance(call.args[0], ast.Constant)
        or not isinstance(call.args[0].value, str)
    ):
        return None
    return call.args[0].value


def _python_host_double_violations(root: Path, path: Path) -> set[str]:
    relative = _relative(root, path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError):
        return {f"{relative}:backend.syntax.unresolved"}
    violations: set[str] = set()

    def assigned_member(target: ast.expr) -> str | None:
        if isinstance(target, ast.Attribute) and target.attr in _GOVERNED_PYTHON_MEMBERS:
            return target.attr
        if (
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and target.slice.value in _GOVERNED_PYTHON_MEMBERS
        ):
            return str(target.slice.value)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            module_name = _python_module_type_name(node)
            if module_name in _GOVERNED_PYTHON_MODULES:
                violations.add(f"{relative}:backend.module.{module_name}")
            for keyword in node.keywords:
                if keyword.arg in _GOVERNED_PYTHON_MEMBERS:
                    violations.add(f"{relative}:backend.member.{keyword.arg}")
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _GOVERNED_PYTHON_MEMBERS
            ):
                violations.add(f"{relative}:backend.member.{node.args[1].value}")
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                member = assigned_member(target)
                if member is not None:
                    violations.add(f"{relative}:backend.member.{member}")
    return violations


def _source_paths(root: Path) -> Iterable[Path]:
    frontend = root / "frontend" / "src"
    if frontend.is_dir():
        for path in sorted(frontend.rglob("*")):
            if path.is_file() and path.suffix in {".ts", ".tsx"}:
                yield path
    package = root / "comfyui_h3_context"
    for name in ("__init__.py", "nodes.py", "public_api.py", "registration.py"):
        path = package / name
        if path.is_file():
            yield path
    adapters = package / "adapters"
    if adapters.is_dir():
        yield from sorted(adapters.glob("*.py"))


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise HostSeamCensusError("scan path escapes the repository root") from error


def _object_literal(text: str, start: int) -> str | None:
    opening = text.find("{", start)
    if opening < 0:
        return None
    depth = 0
    for index in range(opening, len(text)):
        value = text[index]
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
            if depth == 0:
                return text[opening : index + 1]
    return None


def scan_hermetic_host_double_uses(root: Path) -> tuple[str, ...]:
    """Reject governed frontend and Python shapes outside fixture authorities."""

    tests_root = root / "frontend" / "tests"
    violations: set[str] = set()
    if tests_root.is_dir():
        for path in sorted(tests_root.rglob("*.ts")):
            relative = _relative(root, path)
            if relative in _FIXTURE_DOUBLE_AUTHORITIES or "/e2e/" in relative:
                continue
            text = _LITERAL_OR_COMMENT.sub("", path.read_text(encoding="utf-8"))
            for match in _DIRECT_HOST_CONSTRUCTOR.finditer(text):
                violations.add(f"{relative}:{match.group(1)}.unbound")
            for match in _TEST_HOST_OBJECT.finditer(text):
                body = _object_literal(text, match.end() - 1)
                if body is None:
                    violations.add(f"{relative}:{match.group(1)}.unresolved")
                    continue
                for member in _TEST_HOST_MEMBER.findall(body):
                    violations.add(f"{relative}:{match.group(1)}.{member}")
    python_tests = root / "tests"
    if python_tests.is_dir():
        for path in sorted(python_tests.rglob("*.py")):
            violations.update(_python_host_double_violations(root, path))
    return tuple(sorted(violations))


def scan_host_seams(
    root: Path,
) -> tuple[dict[str, set[str]], tuple[str, ...]]:
    """Return detected seam paths and privacy-safe unknown member identities."""

    detected: dict[str, set[str]] = {seam_id: set() for seam_id in _DETECTORS}
    unknown: set[str] = set()
    for path in _source_paths(root):
        relative = _relative(root, path)
        text = path.read_text(encoding="utf-8")
        for seam_id, pattern in _DETECTORS.items():
            if pattern.search(text) is not None:
                detected[seam_id].add(relative)
        if relative.startswith("frontend/src/"):
            member_text = _LITERAL_OR_COMMENT.sub("", text)
            for object_name, member_name in _HOST_MEMBER.findall(member_text):
                allowed = _ALLOWED_APP_MEMBERS if object_name == "app" else _ALLOWED_API_MEMBERS
                if member_name not in allowed:
                    unknown.add(f"{relative}:{object_name}.{member_name}")
            for member_name in _LITEGRAPH_MEMBER.findall(member_text):
                if member_name not in _ALLOWED_LITEGRAPH_MEMBERS:
                    unknown.add(f"{relative}:LiteGraph.{member_name}")
    return detected, tuple(sorted(unknown))


def validate_tracked_census(
    root: Path,
    census_path: Path,
    *,
    parse_contract: bool = True,
) -> HostSeamCensusSummary:
    """Require an exact source-path join between detectors and the census."""

    census = json.loads(census_path.read_text(encoding="utf-8"))
    if not isinstance(census, dict) or not isinstance(census.get("seams"), list):
        raise HostSeamCensusError("census has no closed seam list")
    rows: dict[str, set[str]] = {}
    for value in census["seams"]:
        if not isinstance(value, dict):
            raise HostSeamCensusError("census contains a malformed seam row")
        seam_id = value.get("id")
        source_paths = value.get("source_paths")
        if (
            not isinstance(seam_id, str)
            or seam_id in rows
            or not isinstance(source_paths, list)
            or not all(isinstance(path, str) for path in source_paths)
        ):
            raise HostSeamCensusError("census contains an invalid seam identity or path list")
        rows[seam_id] = set(source_paths)
    detected, unknown = scan_host_seams(root)
    if unknown:
        raise HostSeamCensusError("source contains an unclassified host member")
    if scan_hermetic_host_double_uses(root):
        raise HostSeamCensusError("tests contain a hand-authored governed host double")
    if not (root / _PYTHON_FIXTURE_DOUBLE_AUTHORITY).is_file():
        raise HostSeamCensusError("backend fixture-backed double authority is missing")
    if set(rows) != set(detected):
        raise HostSeamCensusError("census and the closed detector inventory disagree")
    mismatches = [seam_id for seam_id in sorted(rows) if rows[seam_id] != detected[seam_id]]
    if mismatches:
        raise HostSeamCensusError("census contains stale or missing source paths")
    if parse_contract:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from comfyui_h3_context.core.host_seam_contract import (  # noqa: PLC0415
            parse_host_seam_contract,
        )

        fixture_path = census_path.with_name(DEFAULT_FIXTURE.name)
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        parse_host_seam_contract(census, fixture)
    return {
        "status": "PASS",
        "seams": len(rows),
        "source_paths": sum(len(paths) for paths in rows.values()),
        "unknown_members": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the tracked census")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--census", type=Path, default=DEFAULT_CENSUS)
    args = parser.parse_args(argv)
    if not args.check:
        parser.error("only the read-only --check operation is supported")
    census_path = args.census
    if not census_path.is_absolute():
        census_path = args.root / census_path
    try:
        summary = validate_tracked_census(args.root, census_path)
    except (HostSeamCensusError, OSError, ValueError) as error:
        print(json.dumps({"status": "FAIL", "detail": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
