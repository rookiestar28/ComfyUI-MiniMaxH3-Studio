"""Build and verify the bounded HC-16 tooling-retirement census.

The policy scans Git-tracked repository text as data. A name reference is evidence that a tool has
a current reader, but the absence of one is never enough to delete it: every unreferenced retained
tool needs an explicit capability rationale, and every retired path must be absent and recoverable
from Git history. The scanner opens no network and executes none of the files it inventories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "h3-context-tooling-retirement/1"
MAX_TRACKED_FILES = 4_096
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SCANNED_BYTES = 64 * 1024 * 1024
MAX_CONSUMERS = 1_024
MAX_RATIONALE = 240
REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

TEXT_SUFFIXES = frozenset(
    {
        "",
        ".css",
        ".in",
        ".js",
        ".json",
        ".md",
        ".ps1",
        ".py",
        ".pyi",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)
TOOL_SUFFIXES = frozenset({".ps1", ".py", ".sh"})

#: A directly-invocable Python entry point. Only used for nested files; see
#: `is_nested_tool_path` for why the top-level rule stays a pure path rule.
_ENTRY_POINT = re.compile("^if __name__ == [\"']__main__[\"']:", re.MULTILINE)
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,127}")

# These generated records name tools because they describe repository structure. Counting them as
# readers would let the census manufacture reachability for the very tools it is judging.
SELF_DESCRIBING_PATHS = frozenset(
    {
        # CRITICAL: a record that *describes* which tool consumes what is not itself a consumer.
        # M23-50 gave the fitness artifact a `source_reachability.owners` field naming the exact
        # scripts that reach each module, which made every fixture script look referenced and
        # emptied the manual-retention population. A tool must not be kept alive by an artifact
        # whose only statement about it is that nothing else uses it.
        "governance/contracts/architecture_fitness_v1.json",
        "governance/contracts/architecture_inventory_v1.json",
        "comfyui_h3_context/contracts/contract_inventory_v1.json",
        "governance/contracts/fingerprint_domain_v1.json",
        # The M23-50 review found the same defect already live here: an `internal_consumers`
        # field names `scripts/m14_05_fixed_h3_fixture.py`, and it was that tool's *only* tracked
        # mention, so the census silently called it referenced.
        "governance/contracts/retirement_eligibility_v1.json",
        # Same shape again, from M23-48: the readership record lists, per packaged artifact, every
        # tracked file that mentions it. Those `readers` entries are script paths, so a fixture
        # generator whose only mention is "this record observed it" would count as referenced.
        "governance/contracts/packaged_artifact_readership_v1.json",
        "scripts/tooling_retirement.py",
        # M25-45 found the same shape a fourth time, and this one is a test rather than a record.
        # `tests/test_script_modules_import.py` discovers every script by glob and names a module
        # literally only in `REFUSES_TO_IMPORT`, its exemption list -- whose entire statement about
        # a module is "this one refuses to import outside its host". That is a description, not a
        # consumption: an import-smoke test cannot establish that any entry point is still needed,
        # and counting it as a reader would let the census call a dead tool referenced. It broke
        # exactly that way when `06eca16e` landed, taking `scripts/m17_10_supported_host_smoke.py`
        # out of the manual-retention population and failing seven of this suite's cases.
        "tests/test_script_modules_import.py",
    }
)

# These commands have no tracked string reader after the self-describing records are removed, but
# each still owns a capability that cannot be inferred from its filename. Reasons are bounded,
# content-free and executable-capability based; historical ownership alone is not a reason.
MANUAL_RETENTIONS: Mapping[str, str] = {
    "scripts/audit_official_oracle_decision.py": (
        "manual content-free audit entry point for explicitly authorized official-oracle decisions"
    ),
    "scripts/m14_05_fixed_h3_fixture.py": (
        "sole current byte-identical generator and check for the fixed-H3 terminal record "
        "fixture and its exact portable schema"
    ),
    "scripts/m14_03_semantic_graph_fixture.py": (
        "sole current byte-identical generator and check for the compressed semantic-graph fixture"
    ),
    "scripts/m17_10_supported_host_smoke.py": (
        "bounded supported-host continuity smoke retained for pinned-host requalification"
    ),
    "scripts/m19_07_temporal_qualification.py": (
        "modular temporal qualification entry point retained for reproducible "
        "pinned-source evidence"
    ),
    "scripts/m19_08_build_matrix.py": (
        "bounded builder for the retained latent-row qualification matrix and evidence inputs"
    ),
    "scripts/m19_08_latent_row_requalification.py": (
        "weight-backed latent-row qualification entry point retained for reproducible "
        "model evidence"
    ),
    "scripts/m20_07_bridge_qualification.py": (
        "supported-host bridge qualification harness retained for masked-continuation "
        "reproducibility"
    ),
    "scripts/m22_10_source_drift_fixture.py": (
        "current byte-identical generator and check for the source-drift v2 fixture"
    ),
}

# A later provider item consumes this capability even though the item that introduced the script is
# closed. Required capability beats item-name ownership and ordinary reference counts.
REQUIRED_RETENTIONS: Mapping[str, str] = {
    "scripts/m22_14_remote_qualification.py": (
        "only live remote qualification entry point that emits the typed receipt required "
        "for catalog promotion"
    )
}

RETIRED_PATHS = (
    "scripts/m10_04_coordinator_fixture.py",
    "scripts/m13_02_reference_role_fixture.py",
    "scripts/m13_03_temporal_event_alignment_fixture.py",
    "scripts/m13_04_directive_authority_fixture.py",
    "scripts/m13_05_task_mode_retention_fixture.py",
    "scripts/m13_06_feasible_av_timeline_fixture.py",
    "scripts/m13_07_hierarchical_evidence_reduction_fixture.py",
    "scripts/m13_08_constrained_semantic_planning_fixture.py",
    "scripts/m14_09_source_drift_fixture.py",
)
RETIRED_DISPOSITIONS: Mapping[str, tuple[str, str]] = {
    "scripts/m10_04_coordinator_fixture.py": (
        "standalone synthetic-summary command has no tracked reader or executable workflow",
        "owned no persisted artifact; related fixtures and contract implementations remain",
    ),
    "scripts/m13_02_reference_role_fixture.py": (
        "standalone synthetic-summary command has no tracked reader or executable workflow",
        "owned no persisted artifact; reference-role implementation and focused tests remain",
    ),
    "scripts/m13_03_temporal_event_alignment_fixture.py": (
        "standalone synthetic-summary command has no tracked reader or executable workflow",
        "owned no persisted artifact; related fixtures and contract implementations remain",
    ),
    "scripts/m13_04_directive_authority_fixture.py": (
        "standalone synthetic-summary command has no tracked reader or executable workflow",
        "owned no persisted artifact; related fixtures and contract implementations remain",
    ),
    "scripts/m13_05_task_mode_retention_fixture.py": (
        "standalone synthetic-summary command has no tracked reader or executable workflow",
        "owned no persisted artifact; related fixtures and contract implementations remain",
    ),
    "scripts/m13_06_feasible_av_timeline_fixture.py": (
        "standalone synthetic-summary command has no tracked reader or executable workflow",
        "owned no persisted artifact; feasible-timeline implementation and focused tests remain",
    ),
    "scripts/m13_07_hierarchical_evidence_reduction_fixture.py": (
        "standalone synthetic-summary command has no tracked reader or executable workflow",
        "owned no persisted artifact; related fixtures and contract implementations remain",
    ),
    "scripts/m13_08_constrained_semantic_planning_fixture.py": (
        "standalone synthetic-summary command has no tracked reader or executable workflow",
        "owned no persisted artifact; related fixtures and contract implementations remain",
    ),
    "scripts/m14_09_source_drift_fixture.py": (
        "superseded duplicate generator is stale while the current M22-10 generator passes",
        "historical M16-01 fixture remains; the active source-drift fixture uses M22-10",
    ),
}


class ToolingRetirementError(RuntimeError):
    """Raised when the live tooling population cannot support a closed retirement claim."""


class ToolRecord(NamedTuple):
    path: str
    disposition: str
    consumers: tuple[str, ...]
    rationale: str

    def to_wire(self) -> dict[str, object]:
        return {
            "path": self.path,
            "disposition": self.disposition,
            "consumers": list(self.consumers),
            "rationale": self.rationale,
        }


class RetiredToolRecord(NamedTuple):
    path: str
    consumers: tuple[str, ...]
    rationale: str
    artifact_disposition: str
    recovery_commit: str

    def to_wire(self) -> dict[str, object]:
        return {
            "path": self.path,
            "consumers": list(self.consumers),
            "rationale": self.rationale,
            "artifact_disposition": self.artifact_disposition,
            "recovery_commit": self.recovery_commit,
        }


class ToolingCensus(NamedTuple):
    tools: tuple[ToolRecord, ...]
    retired_tools: tuple[RetiredToolRecord, ...]
    fingerprint: str

    @property
    def retired_paths(self) -> tuple[str, ...]:
        return tuple(tool.path for tool in self.retired_tools)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "tools": [tool.to_wire() for tool in self.tools],
            "retired_paths": list(self.retired_paths),
            "retired_tools": [tool.to_wire() for tool in self.retired_tools],
            "fingerprint": self.fingerprint,
        }


def _run_git(root: Path, arguments: Sequence[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolingRetirementError(
            "tracked tooling census requires a readable Git worktree"
        ) from exc
    return result.stdout


def tracked_paths(root: Path = ROOT) -> tuple[str, ...]:
    raw = _run_git(root, ("ls-files", "-z"))
    try:
        paths = tuple(sorted(item for item in raw.decode("utf-8").split("\0") if item))
    except UnicodeDecodeError as exc:  # pragma: no cover - Git emits repository path bytes
        raise ToolingRetirementError("tracked paths are not UTF-8") from exc
    if not paths or len(paths) > MAX_TRACKED_FILES or len(paths) != len(set(paths)):
        raise ToolingRetirementError("tracked path population is empty, duplicated or over budget")
    return paths


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        metadata = path.lstat()
    except OSError:
        return True
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & REPARSE_ATTRIBUTE)


def _safe_file(root: Path, relative: str) -> Path:
    # SECURITY: Git path reads must remain inside the worktree and reject reparse targets.
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
        raise ToolingRetirementError("tracked path is not a normalized repository-relative path")
    candidate = root / path
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise ToolingRetirementError("tracked path escapes the repository") from exc
    if not candidate.is_file() or _is_reparse(candidate):
        raise ToolingRetirementError(f"tracked path is missing or linked: {relative}")
    return candidate


def _reader_index(root: Path, paths: Sequence[str]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    total = 0
    for relative in paths:
        if (
            relative in SELF_DESCRIBING_PATHS
            or Path(relative).suffix.casefold() not in TEXT_SUFFIXES
        ):
            continue
        candidate = _safe_file(root, relative)
        raw = candidate.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            raise ToolingRetirementError(f"tracked text exceeds the per-file budget: {relative}")
        total += len(raw)
        if total > MAX_SCANNED_BYTES:
            raise ToolingRetirementError("tracked text exceeds the aggregate scan budget")
        try:
            tokens = set(TOKEN.findall(raw.decode("utf-8")))
        except UnicodeDecodeError:
            continue
        for token in tokens:
            index.setdefault(token, set()).add(relative)
    return index


def classify_tool(
    path: str,
    consumers: Sequence[str],
    *,
    manual_retentions: Mapping[str, str] = MANUAL_RETENTIONS,
    required_retentions: Mapping[str, str] = REQUIRED_RETENTIONS,
) -> tuple[str, str]:
    readers = tuple(consumers)
    if readers != tuple(sorted(set(readers))) or len(readers) > MAX_CONSUMERS:
        raise ToolingRetirementError(
            f"tool consumers are unsorted, duplicated or over budget: {path}"
        )
    required = required_retentions.get(path)
    if required is not None:
        return "retain_required", _rationale(required, path)
    if readers:
        return "retain_referenced", f"{len(readers)} tracked reader(s) name this tool"
    manual = manual_retentions.get(path)
    if manual is None:
        raise ToolingRetirementError(
            f"unreferenced tool lacks an explicit capability rationale: {path}"
        )
    return "retain_explicit", _rationale(manual, path)


def _rationale(value: str, path: str) -> str:
    if not isinstance(value, str) or len(value) < 24 or len(value) > MAX_RATIONALE:
        raise ToolingRetirementError(f"tool rationale is missing or unbounded: {path}")
    if any(marker in value for marker in ("\r", "\n", "|")):
        raise ToolingRetirementError(f"tool rationale is not single-line safe: {path}")
    return value


def validate_retired_readers(
    retired_paths: Sequence[str], reader_index: Mapping[str, set[str]]
) -> None:
    for path in retired_paths:
        readers = tuple(sorted(reader_index.get(Path(path).stem, set())))
        if readers:
            raise ToolingRetirementError(
                f"retired tool still has tracked readers: {path}: {readers!r}"
            )


def _recovery_commit(root: Path, path: str) -> str:
    raw = _run_git(root, ("log", "--diff-filter=A", "-1", "--format=%H", "--", path))
    try:
        commit = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:  # pragma: no cover - Git OIDs are ASCII
        raise ToolingRetirementError(f"recovery commit is not ASCII: {path}") from exc
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ToolingRetirementError(f"retired tool has no exact recovery commit: {path}")
    _run_git(root, ("cat-file", "-e", f"{commit}:{path}"))
    return commit


def _fingerprint(tools: Sequence[ToolRecord], retired_tools: Sequence[RetiredToolRecord]) -> str:
    wire = {
        "schema": SCHEMA,
        "tools": [tool.to_wire() for tool in tools],
        "retired_tools": [tool.to_wire() for tool in retired_tools],
    }
    payload = json.dumps(wire, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def is_top_level_tool_path(path: str) -> bool:
    return (
        path.startswith("scripts/")
        and path.count("/") == 1
        and path != "scripts/__init__.py"
        and Path(path).suffix.casefold() in TOOL_SUFFIXES
    )


def is_nested_tool_path(path: str, root: Path = ROOT) -> bool:
    """A tool inside a `scripts/` package: a file that is actually invoked directly.

    CRITICAL: nested files need a stricter test than top-level ones, and the asymmetry is
    deliberate rather than an oversight. At the top level every tracked tool file other than
    `__init__.py` is a tool by convention, and the entry-point test is not merely unnecessary there
    but inapplicable: `TOOL_SUFFIXES` admits `.ps1` and `.sh`, which can never carry a Python
    `__main__` guard, and three of the 94 censused top-level tools have none. Inside a package the
    convention is the opposite -- most files are library modules that a top-level tool imports,
    eighteen of the nineteen tracked ones -- so admitting them by path would fill the census with
    implementation detail and make the unreferenced population, the number the census exists to
    compute, meaningless. The entry-point guard is what separates the two, and it is read from the
    file rather than kept in a list, so a new nested tool is censused the day it is written.

    The gap this closes is real: `scripts/governance/relocate_packaged_artifacts.py` shipped in
    M23-55 and sat outside a census that reports itself complete, because `path.count("/") == 1`
    silently excluded it. M23-55 recorded that as a follow-up; this is it.
    """

    if not path.startswith("scripts/") or path.count("/") < 2:
        return False
    if Path(path).name == "__init__.py" or Path(path).suffix.casefold() not in TOOL_SUFFIXES:
        return False
    # CRITICAL: nothing is swallowed here. `_safe_file` refuses a path that escapes the
    # repository, is missing, or is a reparse point, and each of those is a real problem for a
    # census that claims completeness -- answering "not a tool" for a tracked file that cannot be
    # read is how a tool disappears from the census quietly, which is the exact defect row 8 fixes.
    source = _safe_file(root, path).read_text(encoding="utf-8", errors="replace")
    return _ENTRY_POINT.search(source) is not None


def is_tool_path(path: str, root: Path = ROOT) -> bool:
    return is_top_level_tool_path(path) or is_nested_tool_path(path, root)


def build_census(root: Path = ROOT) -> ToolingCensus:
    paths = tracked_paths(root)
    path_set = set(paths)
    if set(RETIRED_DISPOSITIONS) != set(RETIRED_PATHS):
        raise ToolingRetirementError("retired-tool disposition population drifted")
    for retired in RETIRED_PATHS:
        if retired in path_set or (root / retired).exists():
            raise ToolingRetirementError(f"retired tool is still present: {retired}")
    tool_paths = tuple(path for path in paths if is_tool_path(path, root))
    if not tool_paths:
        raise ToolingRetirementError("tracked tooling population is empty")
    missing_required = set(REQUIRED_RETENTIONS) - set(tool_paths)
    if missing_required:
        raise ToolingRetirementError(
            f"required retained tools are missing: {sorted(missing_required)!r}"
        )

    reader_index = _reader_index(root, paths)
    validate_retired_readers(RETIRED_PATHS, reader_index)
    consumers_by_tool: dict[str, tuple[str, ...]] = {}
    for path in tool_paths:
        stem = Path(path).stem
        consumers_by_tool[path] = tuple(sorted(reader_index.get(stem, set()) - {path}))
    unreferenced = {path for path, consumers in consumers_by_tool.items() if not consumers}
    expected_manual = unreferenced - set(REQUIRED_RETENTIONS)
    if set(MANUAL_RETENTIONS) != expected_manual:
        missing = sorted(expected_manual - set(MANUAL_RETENTIONS))
        stale = sorted(set(MANUAL_RETENTIONS) - expected_manual)
        raise ToolingRetirementError(
            f"explicit retention population drifted; missing={missing!r}; stale={stale!r}"
        )

    tools: list[ToolRecord] = []
    for path in tool_paths:
        consumers = consumers_by_tool[path]
        disposition, rationale = classify_tool(path, consumers)
        tools.append(
            ToolRecord(
                path=path,
                disposition=disposition,
                consumers=consumers,
                rationale=rationale,
            )
        )
    ordered = tuple(tools)
    retired_tools = tuple(
        RetiredToolRecord(
            path=path,
            consumers=(),
            rationale=_rationale(RETIRED_DISPOSITIONS[path][0], path),
            artifact_disposition=_rationale(RETIRED_DISPOSITIONS[path][1], path),
            recovery_commit=_recovery_commit(root, path),
        )
        for path in RETIRED_PATHS
    )
    return ToolingCensus(
        tools=ordered,
        retired_tools=retired_tools,
        fingerprint=_fingerprint(ordered, retired_tools),
    )


def report_bytes(census: ToolingCensus) -> bytes:
    return (
        json.dumps(census.to_wire(), indent=2, ensure_ascii=True, sort_keys=True) + "\n"
    ).encode()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate and print a bounded summary")
    parser.add_argument(
        "--json", action="store_true", help="emit the complete deterministic census"
    )
    args = parser.parse_args(argv)
    try:
        census = build_census(ROOT)
    except ToolingRetirementError as exc:
        print(f"tooling retirement: FAIL: {exc}", file=sys.stderr)
        return 1
    if args.json:
        sys.stdout.buffer.write(report_bytes(census))
        return 0
    summary = {
        "status": "PASS",
        "schema": SCHEMA,
        "tool_count": len(census.tools),
        "explicit_retentions": sum(tool.disposition == "retain_explicit" for tool in census.tools),
        "required_retentions": sum(tool.disposition == "retain_required" for tool in census.tools),
        "retired_count": len(census.retired_paths),
        "fingerprint": census.fingerprint,
    }
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
