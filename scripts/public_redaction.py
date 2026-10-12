"""Deterministic comment-only redaction and independent public source proofs."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
from bisect import bisect_left
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import Any

from comfyui_h3_context.core.safe_paths import (
    UnsafePathError,
    ensure_directory,
    read_regular_file_bytes,
    validate_directory,
    validate_regular_file,
)
from scripts.public_source_policy import PUBLICATION_FILES, allowed

ROOT = Path(__file__).resolve().parents[1]
TRANSFORM_ID = "h3-context-public-redaction/1"
ALLOWLIST_PATH = "governance/contracts/public_redaction_allowlist_v1.json"
TOOLCHAIN_ENV = "H3_CONTEXT_REDACTION_TOOLCHAIN_BINDING"
TOOLCHAIN_INPUTS = (
    "scripts/public_redaction.py",
    "scripts/public_redaction_frontend.mjs",
    "scripts/public_source_policy.py",
    ALLOWLIST_PATH,
    "frontend/package.json",
    "frontend/pnpm-lock.yaml",
)
FRONTEND_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".mjs"})
SOURCE_SUFFIXES = FRONTEND_SUFFIXES | {".py", ".css"}
CODE = r"(?<![A-Za-z0-9])(?:M\d{2}[-_]\d{2,3}|RM[-_]\d+|HC[-_]\d+)(?![A-Za-z0-9])"
DOCUMENT = r"(?:[\w./-]*\d{6}[-_][\w.-]*(?:PLAN|RECORD)|[\w.-]+_(?:PLAN|RECORD))\.md"
OWNER = r"owner decision|\(owner,\s*\d{4}-[^)]*\)|the owner's\s+"
REFERENCE = re.compile(f"(?:{CODE})|(?:{DOCUMENT})|(?:{OWNER})", re.IGNORECASE)
NEWLINE = re.compile(r"\r\n|\r|\n|\u2028|\u2029")
CSS_URL = re.compile(r"url\s*\(\s*", re.IGNORECASE)
LINE_RULES = (
    (
        "R3",
        rf"(?P<mark>GUARD|IMPORTANT|CRITICAL|WARNING|NOTE)\s*\(\s*(?:{CODE})\s*\)\s*:",
        r"\g<mark>:",
    ),
    ("R1", rf"\s*\((?:{CODE}|[\s,;§\d.])+\)", ""),
    ("R2", rf"^(\s*(?:#+|//+|/\*+|\*)?\s*)(?:{CODE})\s*[:—–-]\s*", r"\1"),
    (
        "R4",
        rf"\b(?:see|per|owned by|from)\s+[`'\"]?(?:{DOCUMENT}|{CODE})(?:'s\s+row)?[`'\"]?[;:]?\s*",
        "",
    ),
    ("R5", r"\b(the) owner's\s+", r"\1 "),
    ("R5", r"\(owner,\s*\d{4}-[^)]*\)", ""),
    ("R5", r"\bowner decision\s*:?\s*", ""),
    ("R6", f"(?:{CODE})|(?:{DOCUMENT})", ""),
)
RULE_TABLE = {
    "version": 1,
    "codes": CODE,
    "documents": DOCUMENT,
    "owner": OWNER,
    "line_rules": LINE_RULES,
    "empty_docstring": "Internal implementation detail.",
    "opening_reference_separator": {
        "rule": "R6",
        "pattern": rf"^(?:{CODE})[ \t]+",
        "scope": "changed non-allowlisted docstring first row with no original leading whitespace",
    },
    "cleanup": [r"[ \t]{2,}", r"[ \t]+([,.)])", r"\(\s*\)"],
    "rules": [
        "reference-only parentheses",
        "leading reference label",
        "guard marker with reference",
        "reference connective clause",
        "neutral owner wording",
        "remaining references and punctuation cleanup",
        "retain empty comment delimiter",
        "retain empty docstring",
    ],
}


class RedactionRefusal(ValueError):
    """A content-free refusal at a stable source location."""

    def __init__(self, path: str, line: int, reason: str) -> None:
        self.path, self.line, self.reason = path, line, reason
        super().__init__(f"{path}:{line}: {reason}")


def _binding_inputs(root: Path) -> dict[str, str]:
    return {
        path: hashlib.sha256(
            read_regular_file_bytes(root / path, maximum_bytes=2 * 1024 * 1024)
        ).hexdigest()
        for path in TOOLCHAIN_INPUTS
    }


def _loaded_binding_inputs() -> dict[str, str]:
    try:
        return _binding_inputs(ROOT)
    except (OSError, UnsafePathError):
        # IMPORTANT: package-only imports need no parser; absent tools refuse only at execution.
        return {}


_RUNNING_BINDING_INPUTS = _loaded_binding_inputs()


def _binding_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate binding field")
        result[key] = value
    return result


def validate_toolchain_binding(value: str, *, candidate_root: Path) -> Path:
    """Admit an explicit read-only parser authority, never an ambient dependency fallback."""
    try:
        if not isinstance(value, str) or len(value.encode("utf-8")) > 16 * 1024:
            raise ValueError("invalid binding size")
        binding = json.loads(value, object_pairs_hook=_binding_object)
        if (
            not isinstance(binding, dict)
            or set(binding) != {"schema", "root", "candidate", "inputs"}
            or binding["schema"] != "h3-context-redaction-toolchain-binding/1"
            or not isinstance(binding["root"], str)
            or not isinstance(binding["candidate"], str)
            or not isinstance(binding["inputs"], dict)
            or set(binding["inputs"]) != set(TOOLCHAIN_INPUTS)
            or any(
                not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in binding["inputs"].values()
            )
        ):
            raise ValueError("invalid binding shape")
        trusted = Path(binding["root"])
        candidate = Path(binding["candidate"])
        if not trusted.is_absolute() or not candidate.is_absolute():
            raise ValueError("binding roots must be absolute")
        trusted = validate_directory(trusted)
        candidate = validate_directory(candidate)
        if candidate != validate_directory(candidate_root):
            raise ValueError("binding candidate mismatch")
        # CRITICAL: validate even a cached parse's authority before and after use; a matching
        # source digest cannot authorize a replaced helper, allowlist, manifest or lockfile.
        if (
            _binding_inputs(trusted) != binding["inputs"]
            or _binding_inputs(candidate) != binding["inputs"]
            or binding["inputs"] != _RUNNING_BINDING_INPUTS
        ):
            raise ValueError("binding tool identity mismatch")
        return trusted
    except (OSError, ValueError, TypeError, RecursionError):
        raise RedactionRefusal(
            "scripts/public_redaction.py", 1, "TOOLCHAIN_BINDING_REFUSED"
        ) from None


def make_toolchain_binding(trusted_root: Path, candidate_root: Path) -> str:
    """Bind the current transaction's exact helper bytes to its isolated candidate seed."""
    try:
        trusted = validate_directory(trusted_root)
        candidate = validate_directory(candidate_root)
        if trusted != validate_directory(ROOT):
            raise ValueError("producer must name its running toolchain root")
        value = json.dumps(
            {
                "schema": "h3-context-redaction-toolchain-binding/1",
                "root": str(trusted),
                "candidate": str(candidate),
                "inputs": _binding_inputs(trusted),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        validate_toolchain_binding(value, candidate_root=candidate)
        return value
    except (OSError, UnsafePathError, ValueError):
        raise RedactionRefusal(
            "scripts/public_redaction.py", 1, "TOOLCHAIN_BINDING_REFUSED"
        ) from None


def redact_bound_blobs(blobs: Mapping[str, bytes], *, candidate_root: Path) -> dict[str, bytes]:
    """Redact candidate data with its own parser or an explicitly hash-bound transaction parser."""
    value = os.environ.get(TOOLCHAIN_ENV)
    if value is None:
        return redact_blobs(blobs, toolchain_root=candidate_root)
    trusted = validate_toolchain_binding(value, candidate_root=candidate_root)
    public = redact_blobs(blobs, toolchain_root=trusted)
    validate_toolchain_binding(value, candidate_root=candidate_root)
    return public


@dataclass(frozen=True)
class ProofFailure:
    proof: str
    path: str
    line: int
    reason: str


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    line: int
    kind: str


_FRONTEND_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}


def decode_allowlist(document: Any) -> dict[tuple[str, int], str]:
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "exceptions"}
        or document["schema"] != "h3-context-public-redaction-allowlist/1"
        or not isinstance(document["exceptions"], list)
    ):
        raise RedactionRefusal(ALLOWLIST_PATH, 1, "ALLOWLIST_SCHEMA_REFUSED")
    entries: dict[tuple[str, int], str] = {}
    for row in document["exceptions"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "line", "reason"}
            or not isinstance(row["path"], str)
            or not allowed(row["path"])
            or PurePosixPath(row["path"]).as_posix() != row["path"]
            or any(character in row["path"] for character in "*?[]")
            or type(row["line"]) is not int
            or row["line"] < 1
            or not isinstance(row["reason"], str)
            or not row["reason"].strip()
        ):
            raise RedactionRefusal(ALLOWLIST_PATH, 1, "ALLOWLIST_ENTRY_REFUSED")
        key = (row["path"], row["line"])
        if key in entries:
            raise RedactionRefusal(ALLOWLIST_PATH, 1, "ALLOWLIST_DUPLICATE_REFUSED")
        entries[key] = row["reason"]
    return entries


def _allowlist(root: Path = ROOT) -> dict[tuple[str, int], str]:
    try:
        return decode_allowlist(json.loads((root / ALLOWLIST_PATH).read_bytes()))
    except (OSError, ValueError) as error:
        if isinstance(error, RedactionRefusal):
            raise
        raise RedactionRefusal(ALLOWLIST_PATH, 1, "ALLOWLIST_UNAVAILABLE") from error


def _decode(path: str, data: bytes) -> tuple[str, str]:
    try:
        encoding = (
            tokenize.detect_encoding(io.BytesIO(data).readline)[0]
            if path.endswith(".py")
            else "utf-8-sig"
        )
        return data.decode(encoding), encoding
    except (UnicodeError, SyntaxError) as error:
        raise RedactionRefusal(path, 1, "SOURCE_ENCODING_REFUSED") from error


def _encode(text: str, encoding: str, original: bytes) -> bytes:
    if encoding == "utf-8-sig" and not original.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8"
    return text.encode(encoding)


def _frontend_analysis(root: Path, blobs: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    root = root.resolve()
    pending: list[dict[str, str]] = []
    keys: dict[str, tuple[str, str, str]] = {}
    for path, data in blobs.items():
        if Path(path).suffix not in FRONTEND_SUFFIXES:
            continue
        key = (str(root), path, hashlib.sha256(data).hexdigest())
        keys[path] = key
        if key not in _FRONTEND_CACHE:
            pending.append({"path": path, "text": _decode(path, data)[0]})
    if pending:
        node = shutil.which("node")
        helper = root / "scripts" / "public_redaction_frontend.mjs"
        if not node or not helper.is_file() or not (root / "frontend/node_modules/vite").is_dir():
            raise RedactionRefusal(pending[0]["path"], 1, "P2_TOOLCHAIN_UNAVAILABLE")
        request = json.dumps(
            {"root": str(root), "files": pending, "codePattern": CODE}, ensure_ascii=False
        )
        try:
            process = subprocess.run(
                [node, str(helper)],
                input=request.encode(),
                capture_output=True,
                cwd=root,
                timeout=60,
                check=False,
            )
            if process.returncode:
                raise RedactionRefusal(pending[0]["path"], 1, "P2_PARSER_PROCESS_REFUSED")
            result = json.loads(process.stdout)
            if result.get("parser") != "vite/parseSync" or len(result["files"]) != len(pending):
                raise RedactionRefusal(pending[0]["path"], 1, "P2_PARSER_RESPONSE_REFUSED")
            for row in result["files"]:
                if "error" in row:
                    raise RedactionRefusal(row["path"], 1, row["error"])
                _FRONTEND_CACHE[keys[row["path"]]] = row
        except (OSError, subprocess.TimeoutExpired, KeyError, ValueError) as error:
            if isinstance(error, RedactionRefusal):
                raise
            raise RedactionRefusal(pending[0]["path"], 1, "P2_PARSER_RESPONSE_REFUSED") from error
    return {path: _FRONTEND_CACHE[key] for path, key in keys.items()}


@lru_cache(maxsize=16)
def _line_offsets(text: str) -> tuple[list[str], list[int]]:
    # CRITICAL: Python rows use physical CR/LF lines, not Unicode separators
    # inside literals; splitlines() shifts later comment spans and misses residue.
    lines = io.StringIO(text, newline="").readlines()
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    return lines, offsets


def _position(text: str, row: int, column: int, *, ast_column: bool = False) -> int:
    lines, offsets = _line_offsets(text)
    prefix = offsets[row - 1]
    if ast_column:
        # IMPORTANT: AST columns count UTF-8 bytes, while tokenize columns count
        # characters; treating them alike cuts the wrong span after non-ASCII text.
        column = len(lines[row - 1].encode("utf-8")[:column].decode("utf-8"))
    return prefix + column


@lru_cache(maxsize=4096)
def _python_spans(path: str, text: str) -> list[Span]:
    try:
        tree = ast.parse(text, filename=path)
        tokens = list(tokenize.generate_tokens(io.StringIO(text, newline=None).readline))
    except (SyntaxError, tokenize.TokenError, IndentationError) as error:
        line = getattr(error, "lineno", None) or 1
        raise RedactionRefusal(path, line, "PYTHON_PARSE_REFUSED") from error
    spans = [
        Span(_position(text, *token.start), _position(text, *token.end), token.start[0], "comment")
        for token in tokens
        if token.type == tokenize.COMMENT
    ]
    strings = [
        (_position(text, *token.start), _position(text, *token.end), token.start[0])
        for token in tokens
        if token.type == tokenize.STRING
    ]
    string_starts = [start for start, _, _ in strings]
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body or not isinstance(node.body[0], ast.Expr):
            continue
        expression = node.body[0].value
        if not isinstance(expression, ast.Constant) or not isinstance(expression.value, str):
            continue
        start = _position(text, expression.lineno, expression.col_offset, ast_column=True)
        end = _position(
            text,
            expression.end_lineno or expression.lineno,
            expression.end_col_offset or 0,
            ast_column=True,
        )
        # IMPORTANT: index string tokens once; scanning all tokens per docstring
        # makes the whole-tree guard quadratic on large modules with many methods.
        index = bisect_left(string_starts, start)
        while index < len(strings) and strings[index][0] < end:
            token_start, token_end, line = strings[index]
            if token_end <= end:
                spans.append(Span(token_start, token_end, line, "docstring"))
            index += 1
    return sorted(spans, key=lambda span: span.start)


@lru_cache(maxsize=4096)
def _css_spans(path: str, text: str) -> list[Span]:
    spans: list[Span] = []
    index = 0
    while index < len(text):
        url = CSS_URL.match(text, index)
        if (
            url
            and (index == 0 or not re.match(r"[\w-]", text[index - 1]))
            and (url.end() >= len(text) or text[url.end()] not in "\"'")
        ):
            # IMPORTANT: an unquoted CSS URL is one token; slash-star text inside
            # it is URL content, and treating it as a comment rewrites the asset URL.
            index = url.end()
            while index < len(text) and text[index] != ")":
                index += 2 if text[index] == "\\" else 1
            if index >= len(text):
                raise RedactionRefusal(path, 1 + len(NEWLINE.findall(text)), "CSS_URL_REFUSED")
            index += 1
        elif text[index] in "\"'":
            quote = text[index]
            index += 1
            while index < len(text) and text[index] != quote:
                index += 2 if text[index] == "\\" else 1
            if index >= len(text):
                raise RedactionRefusal(path, 1 + len(NEWLINE.findall(text)), "CSS_STRING_REFUSED")
            index += 1
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise RedactionRefusal(
                    path, 1 + len(NEWLINE.findall(text[:index])), "CSS_COMMENT_REFUSED"
                )
            spans.append(Span(index, end + 2, 1 + len(NEWLINE.findall(text[:index])), "comment"))
            index = end + 2
        elif text[index] == "\\":
            index += 2
        else:
            index += 1
    return spans


def _spans(path: str, text: str, data: bytes, root: Path) -> list[Span]:
    if path.endswith(".py"):
        return _python_spans(path, text)
    if path.endswith(".css"):
        return _css_spans(path, text)
    analysis = _frontend_analysis(root, {path: data})[path]
    return [Span(row["start"], row["end"], row["line"], "comment") for row in analysis["comments"]]


PRAGMA = re.compile(
    r"(?:type:\s*ignore(?:\[[^\]\r\n]*\])?|type:\s*[^#\r\n]+|noqa(?::\s*[A-Z0-9, ]+)?|"
    r"pragma:\s*(?:no cover|allowlist secret)|fmt:\s*(?:off|on|skip)|"
    r"@ts-(?:expect-error|ignore|check|nocheck)|(?:eslint|stylelint|oxlint)-(?:disable|enable)"
    r"(?:-next-line|-line)?(?:\s+(?!--)[\w@/., -]+)?|prettier-ignore|"
    r"<reference\b[^>]*>|[@#]__(?:PURE|NO_SIDE_EFFECTS)__|"
    r"(?:sourceMappingURL|sourceURL)=[^\s*]+)",
    re.IGNORECASE,
)
RULE_TABLE["pragmas"] = PRAGMA.pattern
RULE_TABLE_SHA256 = hashlib.sha256(
    json.dumps(RULE_TABLE, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def _pragma_spans(line: str) -> list[tuple[int, int]]:
    if line.startswith("#!") or re.match(r"#.*coding[:=]\s*[-\w.]+", line):
        return [(0, len(line))]
    spans: list[tuple[int, int]] = []
    for match in PRAGMA.finditer(line):
        directive = match.group()
        end = match.start() + len(directive.split(" --", 1)[0].rstrip())
        spans.append((match.start(), end))
    return spans


def _line_transform(text: str) -> tuple[str, set[str]]:
    if not REFERENCE.search(text):
        return text, set()
    rules: set[str] = set()

    def replace(rule: str, pattern: str, replacement: str, value: str) -> str:
        updated = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
        if updated != value:
            rules.add(rule)
        return updated

    for rule, pattern, replacement in LINE_RULES:
        text = replace(rule, pattern, replacement, text)
    prefix = re.match(r"\s*(?:(?:#+|//+|/\*+|\*)\s*)?", text)
    boundary = prefix.end() if prefix else 0
    leading, body = text[:boundary], text[boundary:]
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"[ \t]+([,.)])", r"\1", body)
    body = re.sub(r"\(\s*\)", "", body)
    text = (leading + body).rstrip(" \t")
    if re.fullmatch(r"\s*(?:#+|//+|/\*+\s*\*/|\*)\s*", text):
        rules.add("R7")
        if text.lstrip().startswith("/*"):
            text = text[: len(text) - len(text.lstrip())] + "/* */"
    return text, rules


def _fragment(
    path: str, span: Span, raw: str, exceptions: Mapping[tuple[str, int], str]
) -> tuple[str, Counter[str]]:
    if not REFERENCE.search(raw):
        return raw, Counter()
    counts: Counter[str] = Counter()
    parts = NEWLINE.split(raw)
    breaks = NEWLINE.findall(raw)
    result: list[str] = []
    for offset, line in enumerate(parts):
        if (path, span.line + offset) in exceptions:
            updated = line
        else:
            protected = _pragma_spans(line) if span.kind == "comment" else []
            updated_parts: list[str] = []
            cursor = 0
            active: set[str] = set()
            for start, end in protected:
                free, rules = _line_transform(line[cursor:start])
                active.update(rules)
                updated_parts.extend((free, line[start:end]))
                cursor = end
            free, rules = _line_transform(line[cursor:])
            active.update(rules)
            updated_parts.append(free)
            updated = "".join(updated_parts)
            counts.update(active)
        result.append(updated)
        if offset < len(breaks):
            result.append(breaks[offset])
    return "".join(result), counts


def _transform(
    path: str, data: bytes, root: Path, exceptions: Mapping[tuple[str, int], str]
) -> tuple[bytes, Counter[str]]:
    if not allowed(path) or Path(path).suffix not in SOURCE_SUFFIXES:
        return data, Counter()
    text, encoding = _decode(path, data)
    spans = _spans(path, text, data, root)
    parts: list[str] = []
    counts: Counter[str] = Counter()
    cursor = 0
    for span in spans:
        raw = text[span.start : span.end]
        if span.kind == "docstring":
            delimiter = re.match(r"(?i)([ru]*)(\"\"\"|'''|\"|')", raw)
            if delimiter is None:
                raise RedactionRefusal(path, span.line, "DOCSTRING_LITERAL_REFUSED")
            quote = delimiter.group(2)
            body = raw[delimiter.end() : -len(quote)]
            updated, tally = _fragment(path, span, body, exceptions)
            # CRITICAL: a removed opening label owns its separator. Idempotence alone
            # accepts the leftover space, but the configured formatter then changes it.
            if (
                updated != body
                and (path, span.line) not in exceptions
                and re.match(rf"^(?:{CODE})[ \t]+", body, re.IGNORECASE)
            ):
                updated = updated.lstrip(" \t")
            if REFERENCE.search(body) and not updated.strip(" \t\r\n.,;:()'-—"):
                updated = "Internal implementation detail." + "".join(NEWLINE.findall(body))
                tally["R8"] += 1
            replacement = raw[: delimiter.end()] + updated + quote
        else:
            replacement, tally = _fragment(path, span, raw, exceptions)
        parts.extend((text[cursor : span.start], replacement))
        counts.update(tally)
        cursor = span.end
    parts.append(text[cursor:])
    public = _encode("".join(parts), encoding, data)
    if NEWLINE.findall(text) != NEWLINE.findall(_decode(path, public)[0]):
        raise RedactionRefusal(path, 1, "P4_LINE_BREAK_REFUSED")
    return public, counts


def redact_blob(path: str, data: bytes) -> bytes:
    public = _transform(path, data, ROOT, _allowlist())[0]
    if allowed(path) and Path(path).suffix in SOURCE_SUFFIXES:
        failures = verify_pair(path, data, public) + verify_pragmas(path, data, public)
        if failures:
            failure = failures[0]
            raise RedactionRefusal(path, failure.line, failure.proof + "_TRANSFORM_REFUSED")
    return public


def redact_blobs(
    blobs: Mapping[str, bytes], *, toolchain_root: Path | None = None
) -> dict[str, bytes]:
    root = toolchain_root or ROOT
    selected = {path: data for path, data in blobs.items() if allowed(path)}
    _frontend_analysis(root, selected)
    exceptions = _allowlist(root)
    public = {path: _transform(path, data, root, exceptions)[0] for path, data in blobs.items()}
    _frontend_analysis(root, public)
    for path, data in blobs.items():
        if not allowed(path) or Path(path).suffix not in SOURCE_SUFFIXES:
            continue
        failures = _residue(path, public[path], root)
        if path.endswith(".py") and data != public[path]:
            try:
                if _python_signature(path, data) != _python_signature(path, public[path]):
                    failures.append(ProofFailure("P1", path, 1, "PYTHON_AST_CHANGED"))
            except (SyntaxError, ValueError) as error:
                raise RedactionRefusal(
                    path, getattr(error, "lineno", None) or 1, "P1_TRANSFORM_REFUSED"
                ) from error
        if (
            path.endswith(".css")
            and data != public[path]
            and _css_signature(path, data) != _css_signature(path, public[path])
        ):
            failures.append(ProofFailure("P3", path, 1, "CSS_DECLARATIONS_CHANGED"))
        if failures:
            failure = failures[0]
            raise RedactionRefusal(path, failure.line, failure.proof + "_TRANSFORM_REFUSED")
    failures = verify_frontend_pairs(
        root,
        {
            path: (blobs[path], public[path])
            for path in selected
            if Path(path).suffix in FRONTEND_SUFFIXES
        },
    )
    if failures:
        failure = failures[0]
        raise RedactionRefusal(failure.path, failure.line, "P2_TRANSFORM_REFUSED")
    return public


def verify_formatter(root: Path) -> list[ProofFailure]:
    """Run the configured all-files formatter in check-only mode on one checkout."""
    # CRITICAL: consumers import the transform without prepared tooling dependencies.
    # Keep YAML local to this explicit gate, or ordinary projection imports fail.
    try:
        import yaml
    except ImportError as error:
        raise RedactionRefusal(
            ".pre-commit-config.yaml", 1, "P8_FORMATTER_TOOLCHAIN_UNAVAILABLE"
        ) from error
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        root = validate_directory(root)
        validate_regular_file(root / ".venv/pyvenv.cfg", maximum_bytes=65536)
        validate_directory(python.parent)
        # IMPORTANT: Linux venv launchers may link to their base Python; only the
        # launcher may link. Reject escaping venv parents and linked Windows launchers.
        if os.name == "nt":
            python = validate_regular_file(python)
        elif not python.is_file():
            raise UnsafePathError("project-local interpreter is unavailable")
    except UnsafePathError as error:
        raise RedactionRefusal("pyproject.toml", 1, "P8_PROJECT_VENV_UNAVAILABLE") from error
    try:
        config = yaml.safe_load(
            read_regular_file_bytes(root / ".pre-commit-config.yaml", maximum_bytes=2 * 1024 * 1024)
        )
        if not isinstance(config, dict) or not isinstance(config.get("repos"), list):
            raise ValueError("invalid hook configuration")
        hooks = []
        for repo in config["repos"]:
            if not isinstance(repo, dict) or not isinstance(repo.get("hooks"), list):
                raise ValueError("invalid hook repository")
            for hook in repo["hooks"]:
                if not isinstance(hook, dict):
                    raise ValueError("invalid hook")
                if hook.get("id") == "ruff-format":
                    hooks.append(hook)
        if len(hooks) != 1:
            raise ValueError("formatter must be unique")
        hook = hooks[0]
        args = hook.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError("invalid formatter arguments")
        # CRITICAL: retain the real hook's pinned revision and all-file selectors.
        # A hand-picked directory list misses root files; writing mode hides stale output.
        hook["args"] = [*args, "--check"]
        temporary = ensure_directory(root / ".tmp")
        cache = ensure_directory(temporary / "pre-commit-cache")
        environment = dict(os.environ)
        environment.pop("SKIP", None)
        environment["PRE_COMMIT_HOME"] = str(cache)
        with tempfile.TemporaryDirectory(prefix="redaction-format-", dir=temporary) as directory:
            copied = Path(directory) / "check-only.yaml"
            copied.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            command = [
                str(python),
                "-m",
                "pre_commit",
                "run",
                "ruff-format",
                "--all-files",
                "--config",
                str(copied),
                "--color",
                "never",
                "--verbose",
            ]
            result = subprocess.run(
                command,
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=300,
                check=False,
            )
    except (UnsafePathError, yaml.YAMLError, ValueError, OSError) as error:
        raise RedactionRefusal(
            ".pre-commit-config.yaml", 1, "P8_FORMATTER_CONFIG_REFUSED"
        ) from error
    except subprocess.TimeoutExpired:
        return [ProofFailure("P8", ".pre-commit-config.yaml", 1, "FORMATTER_CHECK_ABORTED")]
    output = result.stdout.decode("utf-8", errors="replace")
    # The pinned manifest's display name differs from its id. Join the verbose
    # hook identity and real passed result, rather than trusting a display label.
    passes = re.findall(r"^.+\.{2,}Passed\s*$", output, re.MULTILINE)
    identities = re.findall(r"^- hook id: (\S+)\s*$", output, re.MULTILINE)
    if (
        result.returncode
        or len(passes) != 1
        or identities != ["ruff-format"]
        or re.search(r"Skipped|Failed|files were modified", output)
    ):
        return [ProofFailure("P8", ".pre-commit-config.yaml", 1, "FORMATTER_CHECK_FAILED")]
    return []


def verify_lint(source_root: Path, public_root: Path) -> list[ProofFailure]:
    """Compare configured-tool diagnostics on two prepared, isolated checkouts."""
    failures: list[ProofFailure] = []
    for root in (source_root, public_root):
        failures.extend(verify_formatter(root))
        if failures:
            return failures
    for tool, options in (
        ("ruff", ["check", "comfyui_h3_context", "scripts", "tests"]),
        (
            "mypy",
            [
                "--ignore-missing-imports",
                "--scripts-are-modules",
                "--python-version",
                "3.10",
                "--platform",
                "linux",
            ],
        ),
        (
            "mypy",
            [
                "--ignore-missing-imports",
                "--scripts-are-modules",
                "--python-version",
                "3.10",
                "--platform",
                "win32",
            ],
        ),
        ("frontend", []),
    ):
        observations: list[tuple[int, bytes]] = []
        for root in (source_root, public_root):
            python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if tool == "frontend":
                pnpm = shutil.which("pnpm.cmd" if os.name == "nt" else "pnpm")
                if pnpm is None or not (root / "frontend/node_modules").is_dir():
                    raise RedactionRefusal(
                        "frontend/package.json", 1, "P8_FRONTEND_TOOLCHAIN_UNAVAILABLE"
                    )
                command = [pnpm, "--dir", "frontend", "run", "check"]
            else:
                if not python.is_file():
                    raise RedactionRefusal("pyproject.toml", 1, "P8_PROJECT_VENV_UNAVAILABLE")
                command = [str(python), "-m", tool, *options]
                if tool == "mypy":
                    paths = [
                        path
                        for path in source_blobs(root)
                        if path.endswith(".py")
                        and path.startswith(("comfyui_h3_context/", "scripts/", "tests/"))
                    ]
                    if not paths:
                        raise RedactionRefusal("pyproject.toml", 1, "P8_MYPY_SELECTION_EMPTY")
                    manifest = root / ".tmp/public-redaction-mypy.json"
                    manifest.parent.mkdir(parents=True, exist_ok=True)
                    # IMPORTANT: retain the all-file hook's deterministic batching.
                    # One giant invocation duplicates existing namespace test modules
                    # and aborts before checking them; never accept matching aborts.
                    manifest.write_text(json.dumps(paths), encoding="utf-8")
                    runner = (
                        "import json,sys; from pre_commit.lang_base import run_xargs; "
                        "from pathlib import Path; "
                        "paths=json.loads(Path(sys.argv[-1]).read_text(encoding='utf-8')); "
                        "code,out=run_xargs((sys.executable,'-m',*sys.argv[1:-1]),"
                        "paths,require_serial=False,color=False); "
                        "sys.stdout.buffer.write(out); sys.exit(code)"
                    )
                    command = [str(python), "-c", runner, tool, *options, str(manifest)]
            result = subprocess.run(
                command,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=300,
                check=False,
            )
            # Matching failed/aborted checks prove no successful diagnostics parity.
            if result.returncode:
                failures.append(
                    ProofFailure("P8", "pyproject.toml", 1, tool.upper() + "_CHECK_FAILED")
                )
                return failures
            normalized = result.stdout.replace(str(root).encode(), b"<root>").replace(
                b"\r\n", b"\n"
            )
            observations.append((result.returncode, normalized))
        if observations[0] != observations[1]:
            failures.append(
                ProofFailure("P8", "pyproject.toml", 1, tool.upper() + "_DIAGNOSTICS_CHANGED")
            )
    return failures


@lru_cache(maxsize=4096)
def _python_signature(path: str, data: bytes) -> str:
    text = _decode(path, data)[0]
    tree = ast.parse(text, filename=path)
    compile(tree, path, "exec")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr):
                value = node.body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    value.value = ""
    return ast.dump(tree, include_attributes=False)


@lru_cache(maxsize=4096)
def _css_signature(path: str, data: bytes) -> str:
    text = _decode(path, data)[0]
    parts: list[str] = []
    cursor = 0
    for span in _css_spans(path, text):
        parts.append(text[cursor : span.start])
        cursor = span.end
    parts.append(text[cursor:])
    return "".join(parts)


def _residue(path: str, data: bytes, root: Path) -> list[ProofFailure]:
    if not allowed(path) or Path(path).suffix not in SOURCE_SUFFIXES:
        return []
    text = _decode(path, data)[0]
    exceptions = _allowlist(root)
    failures: list[ProofFailure] = []
    for span in _spans(path, text, data, root):
        raw = text[span.start : span.end]
        for offset, line in enumerate(NEWLINE.split(raw)):
            if REFERENCE.search(line) and (path, span.line + offset) not in exceptions:
                failures.append(
                    ProofFailure("P7", path, span.line + offset, "CLASS_A_REFERENCE_REMAINS")
                )
    return failures


def verify_pair(path: str, source: bytes, public: bytes) -> list[ProofFailure]:
    return _verify_pair(path, source, public, ROOT)


def _verify_pair(path: str, source: bytes, public: bytes, root: Path) -> list[ProofFailure]:
    if not allowed(path) or Path(path).suffix not in SOURCE_SUFFIXES:
        return []
    # Byte identity already proves equivalence; still scan every unchanged public
    # file for residue. Semantic proofs remain mandatory for every changed file.
    if source == public:
        return _residue(path, public, root)
    failures: list[ProofFailure] = []
    source_text, public_text = _decode(path, source)[0], _decode(path, public)[0]
    if NEWLINE.findall(source_text) != NEWLINE.findall(public_text):
        failures.append(ProofFailure("P4", path, 1, "LINE_BREAK_SEQUENCE_CHANGED"))
    if path.endswith(".py"):
        try:
            if _python_signature(path, source) != _python_signature(path, public):
                failures.append(ProofFailure("P1", path, 1, "PYTHON_AST_CHANGED"))
        except (SyntaxError, ValueError) as error:
            failures.append(
                ProofFailure(
                    "P1", path, getattr(error, "lineno", None) or 1, "PYTHON_COMPILE_REFUSED"
                )
            )
    elif path.endswith(".css"):
        if _css_signature(path, source) != _css_signature(path, public):
            failures.append(ProofFailure("P3", path, 1, "CSS_DECLARATIONS_CHANGED"))
    failures.extend(_residue(path, public, root))
    return failures


def verify_frontend_pairs(
    root: Path, pairs: Mapping[str, tuple[bytes, bytes]]
) -> list[ProofFailure]:
    source = _frontend_analysis(root, {path: pair[0] for path, pair in pairs.items()})
    public = _frontend_analysis(root, {path: pair[1] for path, pair in pairs.items()})
    return [
        ProofFailure("P2", path, 1, "FRONTEND_AST_CHANGED")
        for path in source
        if source[path]["signature"] != public[path]["signature"]
    ]


def _pragma_signature(path: str, data: bytes, root: Path = ROOT) -> list[tuple[int, str]]:
    text = _decode(path, data)[0]
    signature: list[tuple[int, str]] = []
    for span in _spans(path, text, data, root):
        if span.kind != "comment":
            continue
        for offset, line in enumerate(NEWLINE.split(text[span.start : span.end])):
            signature.extend(
                (span.line + offset, line[start:end]) for start, end in _pragma_spans(line)
            )
    return signature


def verify_pragmas(path: str, source: bytes, public: bytes) -> list[ProofFailure]:
    if source == public:
        return []
    if _pragma_signature(path, source) != _pragma_signature(path, public):
        return [ProofFailure("P8", path, 1, "PRAGMA_BYTES_CHANGED")]
    return []


def verify_bundle(source: bytes, public: bytes) -> list[ProofFailure]:
    return (
        []
        if source == public
        else [
            ProofFailure(
                "P6",
                "comfyui_h3_context/web/h3-context-sidebar.js",
                1,
                "ISSUED_BUNDLE_BYTES_CHANGED",
            )
        ]
    )


def source_blobs(root: Path) -> dict[str, bytes]:
    root = root.resolve()
    identity = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    # IMPORTANT: a materialized view inside another checkout inherits its Git
    # root; those parent-relative paths must never stand in for this view's files.
    local_git = (
        identity.returncode == 0 and Path(os.fsdecode(identity.stdout).strip()).resolve() == root
    )
    process = (
        subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if local_git
        else None
    )
    if process is not None and process.returncode == 0:
        paths = sorted(set(os.fsdecode(path) for path in process.stdout.split(b"\0") if path))
    else:
        paths = []
        for directory, names, files in os.walk(root, followlinks=False):
            # Exact publication exceptions can live below a private parent;
            # pruning that parent would silently drop the CI/publish workflows.
            names[:] = [
                name
                for name in names
                if allowed((Path(directory) / name).relative_to(root).as_posix() + "/")
                or any(
                    path.startswith((Path(directory) / name).relative_to(root).as_posix() + "/")
                    for path in PUBLICATION_FILES
                )
            ]
            paths.extend((Path(directory) / name).relative_to(root).as_posix() for name in files)
    result: dict[str, bytes] = {}
    for path in paths:
        if not allowed(path):
            continue
        target = root / path
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise RedactionRefusal(path, 1, "SOURCE_PATH_ESCAPE_REFUSED")
        if target.is_file():
            result[path] = target.read_bytes()
    return result


def verify_tree(root: Path, *, blobs: Mapping[str, bytes] | None = None) -> dict[str, object]:
    started = time.perf_counter()
    source = dict(blobs) if blobs is not None else source_blobs(root)
    public = redact_blobs(source, toolchain_root=root)
    _frontend_analysis(root, public)
    failures: list[ProofFailure] = []
    checked = 0
    for path, data in source.items():
        if not allowed(path) or Path(path).suffix not in SOURCE_SUFFIXES:
            continue
        checked += 1
        # IMPORTANT: supplied views own their allowlist/parser; using the module's
        # checkout here validates a different root and ignores pinned exceptions.
        failures.extend(_verify_pair(path, data, public[path], root))
        if data != public[path] and _pragma_signature(path, data, root) != _pragma_signature(
            path, public[path], root
        ):
            failures.append(ProofFailure("P8", path, 1, "PRAGMA_BYTES_CHANGED"))
        if _transform(path, public[path], root, _allowlist(root))[0] != public[path]:
            failures.append(ProofFailure("A9", path, 1, "TRANSFORM_NOT_IDEMPOTENT"))
    failures.extend(
        verify_frontend_pairs(
            root,
            {
                path: (source[path], data)
                for path, data in public.items()
                if Path(path).suffix in FRONTEND_SUFFIXES
            },
        )
    )
    return {
        "status": "FAIL" if failures else "PASS",
        "transform": TRANSFORM_ID,
        "rule_table_sha256": RULE_TABLE_SHA256,
        "checked_files": checked,
        "changed_files": sum(source[path] != data for path, data in public.items()),
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "failures": [asdict(failure) for failure in failures],
    }


def verify_secrets(root: Path, public_blobs: Mapping[str, bytes]) -> list[ProofFailure]:
    try:
        from detect_secrets.core.scan import scan_file
        from detect_secrets.settings import transient_settings

        if version("detect-secrets").split(".")[:2] != ["1", "5"]:
            raise RedactionRefusal(
                "requirements/secret-scan-baseline.json", 1, "P5_SCANNER_VERSION_REFUSED"
            )
        baseline = json.loads((root / "requirements/secret-scan-baseline.json").read_bytes())
    except (ImportError, OSError, ValueError) as error:
        if isinstance(error, RedactionRefusal):
            raise
        raise RedactionRefusal(
            "requirements/secret-scan-baseline.json", 1, "P5_SCANNER_UNAVAILABLE"
        ) from error
    failures: list[ProofFailure] = []
    with tempfile.TemporaryDirectory(
        prefix="public-redaction-secrets-", dir=root / ".tmp" if (root / ".tmp").is_dir() else root
    ) as temporary:
        target_root = Path(temporary)
        scanner_settings = {key: baseline[key] for key in ("plugins_used", "filters_used")}
        # CRITICAL: verification filters call external services; source/public detection
        # parity must use the same offline filters without verifying any credential.
        scanner_settings["filters_used"] = [
            row
            for row in baseline["filters_used"]
            if row["path"]
            != "detect_secrets.filters.common.is_ignored_due_to_verification_policies"
        ]
        with transient_settings(scanner_settings):
            for path, public in public_blobs.items():
                if not allowed(path) or path == "requirements/secret-scan-baseline.json":
                    continue
                source = root / path
                if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
                    raise RedactionRefusal(path, 1, "P5_SOURCE_PATH_ESCAPE_REFUSED")
                # Byte-identical per-file scanner input proves the same findings
                # and lines; changed files still undergo both actual offline scans.
                if source.read_bytes() == public:
                    continue
                target = target_root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(public)
                original = {
                    (secret.type, secret.secret_hash, secret.line_number)
                    for secret in scan_file(str(source))
                }
                issued = {
                    (secret.type, secret.secret_hash, secret.line_number)
                    for secret in scan_file(str(target))
                }
                if original != issued:
                    failures.append(ProofFailure("P5", path, 1, "SECRET_FINDING_OR_LINE_CHANGED"))
    return failures


def census(root: Path, output: Path) -> dict[str, object]:
    """Write bounded source samples and conservative closure only to private evidence."""
    root, output = root.resolve(), output.resolve()
    if (
        output == root
        or not output.is_relative_to(root)
        or output.relative_to(root).parts[0] not in {".planning", "reference"}
    ):
        raise RedactionRefusal("census", 1, "PRIVATE_EVIDENCE_PATH_REQUIRED")
    source = source_blobs(root)
    public = redact_blobs(source, toolchain_root=root)
    exceptions = _allowlist(root)
    classes: dict[str, Counter[str]] = {}
    rule_counts: Counter[str] = Counter()
    empty_counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, object]]] = {}
    code_pattern = re.compile(CODE, re.IGNORECASE)
    for path, data in source.items():
        area = (
            "frontend/src"
            if path.startswith("frontend/src/")
            else "frontend/tests"
            if path.startswith("frontend/tests/")
            else path.split("/")[0]
        )
        counters = classes.setdefault(area, Counter())
        counters["path_identifiers"] += len(code_pattern.findall(path))
        if Path(path).suffix not in SOURCE_SUFFIXES:
            if Path(path).suffix in {".json", ".md", ".yml", ".yaml", ".toml", ".txt"}:
                counters["data_or_prose"] += len(
                    code_pattern.findall(data.decode("utf-8", errors="replace"))
                )
            continue
        text = _decode(path, data)[0]
        spans = _spans(path, text, data, root)
        for span in spans:
            counters["A_" + span.kind] += len(REFERENCE.findall(text[span.start : span.end]))
        _, tallies = _transform(path, data, root, exceptions)
        rule_counts.update(tallies)
        empty_counts["comments"] += tallies["R7"]
        empty_counts["docstrings"] += tallies["R8"]
        if path.endswith(".py"):
            doc_starts = {span.start for span in spans if span.kind == "docstring"}
            for token in tokenize.generate_tokens(io.StringIO(text, newline=None).readline):
                if token.type == tokenize.NAME:
                    counters["C_identifiers"] += len(code_pattern.findall(token.string))
                elif (
                    token.type == tokenize.STRING
                    and _position(text, *token.start) not in doc_starts
                ):
                    counters["B_strings"] += len(code_pattern.findall(token.string))
        elif Path(path).suffix in FRONTEND_SUFFIXES:
            counters.update(_frontend_analysis(root, {path: data})[path]["classes"])
        else:
            without_comments = _css_signature(path, data)
            counters["B_CSS_text"] += len(code_pattern.findall(without_comments))
        before, after = text.splitlines(), _decode(path, public[path])[0].splitlines()
        selected = samples.setdefault(area, [])
        for number, (old, new) in enumerate(zip(before, after, strict=True), 1):
            if old == new or len(selected) >= 50:
                continue
            sensitive = re.search(
                r"(?:password|credential|api[_-]?key|access[_-]?token)\s*[:=]", old, re.IGNORECASE
            )
            selected.append(
                {
                    "path": path,
                    "line": number,
                    "source": "[content omitted]" if sensitive else old[:600],
                    "public": "[content omitted]" if sensitive else new[:600],
                }
            )
    candidates = sorted(
        path
        for path in source
        if re.search(r"(?:roadmap|closeout|retirement_eligibility)", Path(path).stem, re.IGNORECASE)
    )
    closure_rows: list[dict[str, object]] = []
    readers: dict[str, set[str]] = {}
    for candidate in candidates:
        pending = [candidate]
        closure = {candidate}
        while pending:
            dependency = pending.pop()
            stem = Path(dependency).stem
            aliases = {
                dependency,
                Path(dependency).name,
                stem,
                dependency.removesuffix(".py").replace("/", "."),
                "".join(part.title() for part in stem.split("_")),
            }
            # IMPORTANT: candidate closures share readers; rebuilding each edge
            # for every candidate repeats the same full-source byte scan.
            if dependency not in readers:
                needles = [alias.encode() for alias in aliases if len(alias) >= 8]
                readers[dependency] = {
                    reader
                    for reader, data in source.items()
                    if any(needle in data for needle in needles)
                }
            for reader in sorted(readers[dependency]):
                if reader not in closure:
                    closure.add(reader)
                    pending.append(reader)
        product = sorted(
            path
            for path in closure
            if path.startswith(
                (
                    "comfyui_h3_context/",
                    "frontend/src/",
                    "assets/",
                    "workflows/",
                    "subgraphs/",
                    "examples/",
                )
            )
        )
        closure_rows.append(
            {
                "candidate": candidate,
                "closure": sorted(closure),
                "product_files": product,
                "holds_product": bool(product),
                "exclusion_admission": (
                    "PENDING_DOWNSTREAM_PRODUCT_COMPLETENESS_AND_PROJECTED_TESTS"
                ),
            }
        )
    result: dict[str, object] = {
        "schema": "h3-context-public-redaction-census/1",
        "transform": TRANSFORM_ID,
        "rule_table_sha256": RULE_TABLE_SHA256,
        "source_files": len(source),
        "classes_by_area": {area: dict(counts) for area, counts in sorted(classes.items())},
        "redacted_lines_by_rule": {
            f"R{number}": rule_counts[f"R{number}"] for number in range(1, 9)
        },
        "empty_locations": dict(empty_counts),
        "changed_files": sum(source[path] != data for path, data in public.items()),
        "sample_counts": {area: len(rows) for area, rows in samples.items()},
        "closure_method": (
            "conservative transitive import/name/selector byte references; no exclusions admitted"
        ),
    }
    output.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("census.json", result),
        ("samples.json", samples),
        ("class-d-closure.json", closure_rows),
    ):
        (output / name).write_text(
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("guard", "census"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--paths-file", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "census":
            if arguments.output is None:
                parser.error("census requires --output under private repository evidence")
            result = {"status": "PASS", **census(arguments.root, arguments.output)}
        else:
            blobs = None
            if arguments.paths_file is not None:
                try:
                    paths = json.loads(arguments.paths_file.read_bytes())
                except (OSError, ValueError) as error:
                    raise RedactionRefusal(
                        "selection", 1, "EXPLICIT_PATH_SELECTION_REFUSED"
                    ) from error
                available = source_blobs(arguments.root)
                if (
                    not isinstance(paths, list)
                    or not paths
                    or any(
                        not isinstance(path, str)
                        or not allowed(path)
                        or PurePosixPath(path).as_posix() != path
                        or path not in available
                        for path in paths
                    )
                    or len(paths) != len(set(paths))
                    or not any(Path(path).suffix in SOURCE_SUFFIXES for path in paths)
                ):
                    raise RedactionRefusal("selection", 1, "EXPLICIT_PATH_SELECTION_REFUSED")
                blobs = {path: available[path] for path in paths}
            result = verify_tree(arguments.root, blobs=blobs)
            result["selection"] = "WHOLE_TREE" if blobs is None else "EXPLICIT_PATHS"
            if blobs is not None:
                result["selected_files"] = len(blobs)
    except RedactionRefusal as error:
        result = {"status": "FAIL", "path": error.path, "line": error.line, "reason": error.reason}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
