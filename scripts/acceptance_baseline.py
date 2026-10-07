"""Validate mandatory regressions, monotonic policies and selected public Python ABI."""

from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import json
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASELINE_RELATIVE_PATH = "tests/acceptance_baseline.json"
BASELINE_PATH = ROOT / BASELINE_RELATIVE_PATH
MAX_BASELINE_BYTES = 65_536
MAX_COLLECTION_ITEMS = 256
MAX_TEXT_LENGTH = 512
MAX_DEPTH = 12
REVISION_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")

JsonObject = dict[str, Any]


class AcceptanceBaselineError(ValueError):
    """Raised when the public acceptance baseline is incomplete or unsafe."""


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AcceptanceBaselineError("acceptance baseline contains a duplicate member")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise AcceptanceBaselineError("acceptance baseline contains an invalid JSON constant")


def _bounded(value: object, *, depth: int = 0, count: list[int]) -> None:
    if depth > MAX_DEPTH:
        raise AcceptanceBaselineError("acceptance baseline exceeds the depth bound")
    count[0] += 1
    if count[0] > 4096:
        raise AcceptanceBaselineError("acceptance baseline exceeds the item bound")
    if type(value) is dict:
        for key, child in cast(dict[object, object], value).items():
            if type(key) is not str or not key or len(key) > MAX_TEXT_LENGTH:
                raise AcceptanceBaselineError("acceptance baseline has an invalid member name")
            _bounded(child, depth=depth + 1, count=count)
    elif type(value) is list:
        for child in cast(list[object], value):
            _bounded(child, depth=depth + 1, count=count)
    elif type(value) is str:
        if len(value) > MAX_TEXT_LENGTH:
            raise AcceptanceBaselineError("acceptance baseline text is unbounded")
    elif value is None or type(value) in {int, bool}:
        return
    else:
        raise AcceptanceBaselineError("acceptance baseline contains an unsupported value")


def decode_baseline(payload: bytes) -> JsonObject:
    if type(payload) is not bytes or not payload or len(payload) > MAX_BASELINE_BYTES:
        raise AcceptanceBaselineError("acceptance baseline exceeds the byte bound")
    try:
        value = json.loads(
            payload.decode("utf-8", "strict"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except AcceptanceBaselineError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AcceptanceBaselineError("acceptance baseline is not strict JSON") from exc
    _bounded(value, count=[0])
    if type(value) is not dict:
        raise AcceptanceBaselineError("acceptance baseline must be an object")
    return cast(JsonObject, value)


def _closed(value: object, *, field: str, members: set[str]) -> JsonObject:
    if type(value) is not dict:
        raise AcceptanceBaselineError(f"{field} must be an object")
    result = cast(JsonObject, value)
    if set(result) != members:
        raise AcceptanceBaselineError(f"{field} has invalid members")
    return result


def _safe_regular_file(root: Path, relative: str) -> Path:
    if type(relative) is not str or not relative or "\\" in relative:
        raise AcceptanceBaselineError("acceptance test path is invalid")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise AcceptanceBaselineError("acceptance test path is invalid")
    allowed = (
        (relative.startswith("tests/") and relative.endswith(".py"))
        or (relative.startswith("frontend/tests/") and relative.endswith((".ts", ".tsx")))
        or (relative.startswith("frontend/e2e/") and relative.endswith((".ts", ".tsx")))
    )
    if not allowed:
        raise AcceptanceBaselineError("acceptance test path is outside the allowed test roots")
    candidate = root
    for part in pure.parts:
        candidate = candidate / part
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise AcceptanceBaselineError("acceptance test path is unavailable") from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(metadata.st_mode) or (reparse and attributes & reparse):
            raise AcceptanceBaselineError("acceptance test path contains a link or reparse point")
    if not stat.S_ISREG(candidate.lstat().st_mode):
        raise AcceptanceBaselineError("acceptance test path is not a regular file")
    return candidate


def _python_selector_count(path: Path, selector: str) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="strict"), filename=path.name)
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise AcceptanceBaselineError("registered Python test cannot be parsed") from exc
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == selector
    )


def _literal_test_titles(text: str) -> tuple[str, ...]:
    titles: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text.startswith("//", index):
            end = text.find("\n", index + 2)
            index = length if end < 0 else end + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise AcceptanceBaselineError("registered frontend test has an open comment")
            index = end + 2
            continue
        if text[index] in {'"', "'", "`"}:
            quote = text[index]
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2
                elif text[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
            continue
        match = re.match(r"(?:it|test)\b", text[index:])
        if not match:
            index += 1
            continue
        cursor = index + len(match.group(0))
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor >= length or text[cursor] != "(":
            index = cursor
            continue
        cursor += 1
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor >= length or text[cursor] not in {'"', "'", "`"}:
            index = cursor
            continue
        quote = text[cursor]
        cursor += 1
        value: list[str] = []
        valid = True
        while cursor < length:
            character = text[cursor]
            if character == quote:
                break
            if character == "\\":
                if cursor + 1 >= length:
                    valid = False
                    break
                value.append(text[cursor + 1])
                cursor += 2
                continue
            if (
                quote == "`"
                and character == "$"
                and cursor + 1 < length
                and text[cursor + 1] == "{"
            ):
                valid = False
            value.append(character)
            cursor += 1
        if valid and cursor < length and text[cursor] == quote:
            titles.append("".join(value))
        index = max(index + 1, cursor + 1)
    return tuple(titles)


def _validate_criteria(values: object, *, root: Path) -> int:
    if type(values) is not list or not values or len(values) > MAX_COLLECTION_ITEMS:
        raise AcceptanceBaselineError("criteria must be a bounded non-empty list")
    identities: set[str] = set()
    selectors: set[tuple[str, str, str]] = set()
    for raw in cast(list[object], values):
        item = _closed(
            raw,
            field="criterion",
            members={"id", "path", "runner", "selector"},
        )
        identity = item["id"]
        path_value = item["path"]
        runner = item["runner"]
        selector = item["selector"]
        if (
            type(identity) is not str
            or ID_PATTERN.fullmatch(identity) is None
            or identity in identities
        ):
            raise AcceptanceBaselineError("criterion identity is invalid or duplicated")
        if type(selector) is not str or not selector or len(selector) > MAX_TEXT_LENGTH:
            raise AcceptanceBaselineError("criterion selector is invalid")
        if runner not in {"pytest", "vitest", "playwright"}:
            raise AcceptanceBaselineError("criterion runner is invalid")
        path = _safe_regular_file(root, cast(str, path_value))
        key = (cast(str, path_value), cast(str, runner), selector)
        if key in selectors:
            raise AcceptanceBaselineError("criterion selector is not unique")
        selectors.add(key)
        identities.add(identity)
        if runner == "pytest":
            count = _python_selector_count(path, selector)
        else:
            try:
                count = _literal_test_titles(
                    path.read_text(encoding="utf-8", errors="strict")
                ).count(selector)
            except (OSError, UnicodeError) as exc:
                raise AcceptanceBaselineError("registered frontend test is unavailable") from exc
        if count != 1:
            reason = "not unique" if count > 1 else "missing"
            raise AcceptanceBaselineError(f"criterion selector is {reason}")
    return len(identities)


def describe_public_callable(export: str) -> JsonObject:
    if type(export) is not str or not export or len(export) > 128:
        raise AcceptanceBaselineError("public ABI export is invalid")
    core = importlib.import_module("comfyui_h3_context.core")
    exports = getattr(core, "__all__", ())
    if export not in exports or not hasattr(core, export):
        raise AcceptanceBaselineError("public ABI export is unavailable")
    subject = getattr(core, export)
    try:
        signature = inspect.signature(subject)
    except (TypeError, ValueError) as exc:
        raise AcceptanceBaselineError("public ABI export has no inspectable signature") from exc
    return {
        "export": export,
        "parameters": [
            {
                "has_default": parameter.default is not inspect.Parameter.empty,
                "kind": parameter.kind.name,
                "name": parameter.name,
            }
            for parameter in signature.parameters.values()
        ],
    }


def validate_public_abi(values: object) -> int:
    if type(values) is not list or not values or len(values) > MAX_COLLECTION_ITEMS:
        raise AcceptanceBaselineError("public Python ABI must be a bounded non-empty list")
    names: set[str] = set()
    for raw in cast(list[object], values):
        item = _closed(raw, field="public ABI", members={"export", "parameters"})
        export = item["export"]
        parameters = item["parameters"]
        if type(export) is not str or export in names:
            raise AcceptanceBaselineError("public ABI export identity is invalid or duplicated")
        if type(parameters) is not list or len(parameters) > 128:
            raise AcceptanceBaselineError("public ABI parameter inventory is unbounded")
        for parameter in parameters:
            entry = _closed(
                parameter,
                field="public ABI parameter",
                members={"has_default", "kind", "name"},
            )
            if (
                type(entry["name"]) is not str
                or type(entry["kind"]) is not str
                or type(entry["has_default"]) is not bool
            ):
                raise AcceptanceBaselineError("public ABI parameter is invalid")
        if describe_public_callable(export) != item:
            raise AcceptanceBaselineError(f"public ABI drifted for {export}")
        names.add(export)
    return len(names)


def _validate_policies(values: object) -> int:
    if type(values) is not list or len(values) > MAX_COLLECTION_ITEMS:
        raise AcceptanceBaselineError("policies must be a bounded list")
    identities: set[str] = set()
    for raw in cast(list[object], values):
        item = _closed(
            raw,
            field="policy",
            members={"consumer", "direction", "id", "value"},
        )
        identity = item["id"]
        consumer = item["consumer"]
        if (
            type(identity) is not str
            or ID_PATTERN.fullmatch(identity) is None
            or identity in identities
            or item["direction"] not in {"maximum", "minimum"}
            or type(item["value"]) is not int
            or item["value"] < 0
            or type(consumer) is not str
            or re.fullmatch(r"[a-zA-Z_][\w.]*:[A-Z][A-Z0-9_]*", consumer) is None
        ):
            raise AcceptanceBaselineError("policy is invalid or duplicated")
        module_name, attribute = consumer.split(":", 1)
        try:
            observed = getattr(importlib.import_module(module_name), attribute)
        except (ImportError, AttributeError) as exc:
            raise AcceptanceBaselineError("policy consumer is unavailable") from exc
        if type(observed) is not int or observed != item["value"]:
            raise AcceptanceBaselineError("policy consumer drifted from its authority")
        identities.add(identity)
    return len(identities)


def validate_baseline(path: Path, *, root: Path) -> JsonObject:
    try:
        metadata = path.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(metadata.st_mode) or (reparse and attributes & reparse):
            raise AcceptanceBaselineError("acceptance baseline is a link or reparse point")
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BASELINE_BYTES:
            raise AcceptanceBaselineError("acceptance baseline is not a bounded regular file")
        payload = decode_baseline(path.read_bytes())
    except AcceptanceBaselineError:
        raise
    except OSError as exc:
        raise AcceptanceBaselineError("acceptance baseline is unavailable") from exc
    if set(payload) != {"criteria", "policies", "public_python_abi", "schema"}:
        raise AcceptanceBaselineError("acceptance baseline has invalid members")
    if payload["schema"] != "h3-context-acceptance-baseline/1":
        raise AcceptanceBaselineError("acceptance baseline schema is unsupported")
    criteria = _validate_criteria(payload["criteria"], root=root)
    abi = validate_public_abi(payload["public_python_abi"])
    policies = _validate_policies(payload["policies"])
    return {
        "criteria": criteria,
        "policies": policies,
        "public_python_abi": abi,
        "schema": "h3-context-acceptance-baseline-report/1",
        "status": "PASS",
    }


def validate_repository_baseline(root: Path = ROOT) -> JsonObject:
    return validate_baseline(root / BASELINE_RELATIVE_PATH, root=root)


def _policy_map(payload: JsonObject) -> dict[str, tuple[str, int]]:
    values = payload.get("policies")
    if type(values) is not list:
        raise AcceptanceBaselineError("policies must be a list")
    result: dict[str, tuple[str, int]] = {}
    for raw in values:
        if type(raw) is not dict:
            raise AcceptanceBaselineError("policy must be an object")
        item = cast(JsonObject, raw)
        identity = item.get("id")
        direction = item.get("direction")
        value = item.get("value")
        if (
            type(identity) is not str
            or identity in result
            or direction not in {"maximum", "minimum"}
            or type(value) is not int
        ):
            raise AcceptanceBaselineError("policy identity is invalid")
        result[identity] = (cast(str, direction), value)
    return result


def compare_policy_values(previous: JsonObject, current: JsonObject) -> tuple[str, ...]:
    old = _policy_map(previous)
    new = _policy_map(current)
    if old.keys() - new.keys():
        raise AcceptanceBaselineError("policy identity was deleted")
    relaxations: list[str] = []
    for identity in sorted(old.keys() & new.keys()):
        old_direction, old_value = old[identity]
        new_direction, new_value = new[identity]
        if old_direction != new_direction:
            raise AcceptanceBaselineError("policy identity direction changed")
        if (old_direction == "maximum" and new_value > old_value) or (
            old_direction == "minimum" and new_value < old_value
        ):
            relaxations.append(identity)
    return tuple(relaxations)


def load_policy_value(identity: str, *, root: Path = ROOT) -> int:
    payload = decode_baseline((root / BASELINE_RELATIVE_PATH).read_bytes())
    matches = [
        item
        for item in cast(list[JsonObject], payload.get("policies", []))
        if type(item) is dict and item.get("id") == identity
    ]
    if len(matches) != 1 or type(matches[0].get("value")) is not int:
        raise AcceptanceBaselineError("policy value is unavailable")
    return cast(int, matches[0]["value"])


def compare_git_base(base_ref: str, *, root: Path = ROOT) -> tuple[str, ...]:
    if type(base_ref) is not str or REVISION_PATTERN.fullmatch(base_ref) is None:
        raise AcceptanceBaselineError("base revision is invalid")
    commit = subprocess.run(
        ["git", "cat-file", "-e", f"{base_ref}^{{commit}}"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if commit.returncode != 0:
        raise AcceptanceBaselineError("base revision is unavailable")
    previous = subprocess.run(
        ["git", "show", f"{base_ref}:{BASELINE_RELATIVE_PATH}"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if previous.returncode != 0:
        return ()
    old_payload = decode_baseline(previous.stdout)
    current_payload = decode_baseline((root / BASELINE_RELATIVE_PATH).read_bytes())
    return compare_policy_values(old_payload, current_payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    compare = subparsers.add_parser("compare")
    compare.add_argument("--base-ref", required=True)
    args = parser.parse_args(argv)
    try:
        report = validate_repository_baseline()
        if args.command == "compare":
            relaxations = compare_git_base(args.base_ref)
            if relaxations:
                print("ACCEPTANCE BASELINE: POLICY RELAXATION: " + ",".join(relaxations))
                return 3
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    except (AcceptanceBaselineError, OSError, subprocess.SubprocessError):
        print("ACCEPTANCE BASELINE: FAIL", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
