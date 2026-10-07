"""Product regressions by default; explicit partitioned coverage diagnostics with opt-in resume."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from coverage import CoverageData

VERSION = 1
GROUPS = 16
STATE = ".tmp/backend-gate/state.json"
# These test the development tools themselves. Run them when those tools change, or with
# --coverage for the complete diagnostic suite. Product and safety regressions stay selected.
TOOLING_TESTS = frozenset(
    f"tests/{name}.py"
    for name in (
        "test_acceptance_baseline",
        "test_architecture_fitness",
        "test_architecture_inventory",
        "test_contract_inventory",
        "test_core_public_surface",
        "test_public_surface",
        "test_fingerprint_domain",
        "test_frontend_build_report",
        "test_frontend_module_budget",
        "test_build_gate_report",
        "test_gate_backend",
        "test_gate_stages",
        "test_m23_48_derived_artifacts",
        "test_m23_48_packaged_readership",
        "test_retirement_eligibility",
        "test_tooling_retirement",
        "test_tooling_contract",
        "test_roadmap_registry",
    )
)
# IMPORTANT: ignored input presence, membership and bytes belong to the reader and its consumers;
# tracked-source hashes alone reuse stale results, while unconditional reruns discard valid work.
EXTERNAL_TEST_INPUTS = {
    "tests/test_generation_profile_adapter.py": (
        "reference/rm02/official/workflow_templates/templates",
        "*.json",
    ),
    "tests/test_m25_20_conformance_join.py": (
        ".planning/evidence",
        "*/backend-qualification.json",
    ),
}
# Keep undeclared external readers nonreusable until their complete input set is fingerprinted.
UNCACHED_TEST_INPUTS: frozenset[str] = frozenset()
# These tools inspect test source as data, beyond Python imports. Keep their consumers together
# so a test edit reruns the repository audits without invalidating unrelated expensive suites.
TEST_SOURCE_READERS = frozenset(
    {
        "acceptance_baseline",
        "architecture_fitness",
        "architecture_inventory",
        "contract_inventory",
        "core_public_surface",
        "public_surface",
        "retirement_eligibility",
        "security_audit",
    }
)


@contextmanager
def execution_lock(cache: Path) -> Iterator[None]:
    """OS releases the lock after interruption, so a retry needs no stale-lock override."""
    with (cache / "runner.lock").open("a+b") as stream:
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "absent"


def encoded_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@lru_cache(maxsize=16)
def _binary_digest(path: str, modified: int, size: int) -> str:
    return digest(Path(path))


def binary_identity() -> dict[str, str]:
    result = {}
    # IMPORTANT: tests use PATH tools and both explicit media-tool pins. Hash every selection;
    # hashing only an override leaves a changed PATH or Gate-0 executable behind a stale PASS.
    selections = {name: shutil.which(name) for name in ("node", "ffmpeg", "ffprobe")}
    for prefix in ("H3_CONTEXT_AUTHORIZED", "H3_M17_11"):
        for tool in ("FFMPEG", "FFPROBE"):
            key = f"{prefix}_{tool}_PATH"
            selections[key] = os.environ.get(key)
    for name, selected in selections.items():
        path = Path(selected or f"<missing-{name}>")
        try:
            metadata = path.stat()
            result[name] = _binary_digest(str(path), metadata.st_mtime_ns, metadata.st_size)
        except OSError:
            result[name] = "absent"
    return result


def partition(module: str, groups: int) -> int:
    return int(hashlib.sha256(module.encode()).hexdigest()[:8], 16) % groups


def _literal_target(
    node: ast.AST, bindings: dict[str, ast.expr], seen: frozenset[str]
) -> str | None:
    """Resolve bounded literal import names or final path components without executing code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in bindings and node.id not in seen:
        return _literal_target(bindings[node.id], bindings, seen | {node.id})
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _literal_target(node.right, bindings, seen)
    if isinstance(node, ast.Attribute) and node.attr in {"stem", "__name__"}:
        value = _literal_target(node.value, bindings, seen)
        return Path(value).stem if value and node.attr == "stem" else value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_name"
        and node.args
    ):
        return _literal_target(node.args[0], bindings, seen)
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        value = _literal_target(
            first.value if isinstance(first, ast.FormattedValue) else first, bindings, seen
        )
        if value:
            prefix = value.split(".")[0]
            if "." in value and prefix != "tests" and not prefix.startswith("test_"):
                return value
    return None


def _single_bindings(tree: ast.AST) -> dict[str, ast.expr]:
    """Ambiguous assignments, parameters and loop targets remain unresolved."""
    stores: dict[str, int] = {}
    bindings: dict[str, ast.expr] = {}
    pending = [tree]
    while pending:
        node = pending.pop()
        if node is not tree and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            if not isinstance(node, ast.Lambda):
                stores[node.name] = stores.get(node.name, 0) + 1
            continue
        pending.extend(ast.iter_child_nodes(node))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            stores[node.id] = stores.get(node.id, 0) + 1
        elif isinstance(node, ast.arg):
            stores[node.arg] = stores.get(node.arg, 0) + 1
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                stores[name] = stores.get(name, 0) + 1
                bindings[name] = ast.Constant(alias.name if alias.asname else name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                stores[name] = stores.get(name, 0) + 1
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            bindings[node.target.id] = node.value
    return {
        name: bindings.get(name, ast.Constant(None)) if count == 1 else ast.Constant(None)
        for name, count in stores.items()
    }


def _bindings_at(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> dict[str, ast.expr]:
    scopes = []
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scopes.append(current)
    bindings: dict[str, ast.expr] = {}
    for scope in reversed(scopes):
        bindings.update(_single_bindings(scope))
    return bindings


def dependencies(root: Path, modules: list[str]) -> dict[str, set[str]]:
    """Follow test helpers imported normally or through literal dynamic module names.

    Unresolved dynamic imports conservatively depend on every collected test module.
    Non-test helpers and fixtures are global inputs.
    """
    collected = set(modules)
    helpers = {
        path.relative_to(root).as_posix()
        for folder in ("scripts", "tests")
        for path in (root / folder).rglob("*.py")
    }
    graph_paths = sorted(collected | helpers)
    by_name: dict[str, set[str]] = {}
    for module in graph_paths:
        by_name.setdefault(Path(module).stem, set()).add(module)
    result: dict[str, set[str]] = {}
    for module in graph_paths:
        try:
            tree = ast.parse((root / module).read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeError):
            result[module] = {module} | collected
            continue
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        importers = {
            name: name for name in ("import_module", "__import__", "spec_from_file_location")
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in importers:
                        importers[alias.asname or alias.name] = alias.name
        refs = {module}
        dynamic = module.startswith("scripts/") and Path(module).stem in TEST_SOURCE_READERS
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or "", *[alias.name for alias in node.names]]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value.replace("\\", "/").split("/")[-1].removesuffix(".py")]
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in importers:
                    index = 1 if importers[name] == "spec_from_file_location" else 0
                    bindings = _bindings_at(node, parents)
                    target = (
                        _literal_target(node.args[index], bindings, frozenset())
                        if len(node.args) > index
                        else None
                    )
                    # IMPORTANT: only unresolved imports widen to all tests. Literal imports of
                    # shared source must not destroy unrelated test-group checkpoints.
                    if target is None:
                        dynamic = True
                    else:
                        names = [target.replace("\\", "/").split("/")[-1].removesuffix(".py")]
            for name in names:
                for part in name.split("."):
                    refs.update(by_name.get(part, set()))
        result[module] = refs | collected if dynamic else refs
    for module in graph_paths:
        refs = result[module]
        while True:
            expanded = refs | set().union(*(result[ref] for ref in refs))
            if expanded == refs:
                break
            refs = expanded
        result[module] = refs
    return {module: result[module] & collected for module in modules}


def external_input_fingerprints(root: Path) -> dict[str, str]:
    result = {}
    for reader, (folder, pattern) in EXTERNAL_TEST_INPUTS.items():
        directory = root / folder
        if not directory.resolve().is_relative_to(root):
            raise ValueError("External gate input directory escapes workspace")
        inputs = {}
        for path in [directory, *sorted(directory.glob(pattern))]:
            if not path.resolve().is_relative_to(root):
                raise ValueError("External gate input escapes workspace")
            try:
                mode = path.stat().st_mode
            except FileNotFoundError:
                value = "absent"
            else:
                if stat.S_ISDIR(mode):
                    value = "directory"
                elif stat.S_ISREG(mode):
                    value = "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
                else:
                    raise ValueError("Unsupported external gate input type")
            inputs[path.relative_to(root).as_posix()] = value
        result[reader] = encoded_digest(inputs)
    return result


def common_fingerprint(root: Path, modules: list[str], source: str, groups: int) -> str:
    files = (
        subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    excluded = set(modules)
    inputs = {p: digest(root / p) for p in sorted(set(files)) if p and p not in excluded}
    packages = sorted(
        (dist.metadata["Name"], dist.version) for dist in importlib.metadata.distributions()
    )
    # Store only a digest of the environment: credentials and local values never enter cache/logs.
    environment = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "PYTEST_CURRENT_TEST",
            "COVERAGE_FILE",
            "GATE_INVENTORY_OUT",
            "GATE_INVENTORY_EXPECTED",
        }
    }
    return encoded_digest(
        [
            VERSION,
            inputs,
            source,
            groups,
            sys.executable,
            sys.version,
            packages,
            environment,
            binary_identity(),
            external_input_fingerprints(root) if not modules else None,
            digest(Path(__file__)),
            digest(Path(__file__).with_name("gate_pytest_inventory.py")),
        ]
    )


def read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict) and state.get("version") == VERSION:
            passed = state.get("passed")
            if isinstance(passed, dict):
                return passed
    except (OSError, ValueError):
        pass
    return {}


def write_state(path: Path, passed: dict[str, Any]) -> None:
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps({"version": VERSION, "passed": passed}), encoding="utf-8")
    temporary.replace(path)


def valid_coverage(path: Path, expected_digest: str) -> bool:
    if path.is_symlink() or not expected_digest or digest(path) != expected_digest:
        return False
    try:
        data = CoverageData(basename=str(path))
        data.read()
        return data.has_arcs() and bool(data.measured_files())
    except Exception:
        return False


def command(root: Path, args: list[str], env: dict[str, str]) -> int:
    return subprocess.run([sys.executable, *args], cwd=root, env=env, check=False).returncode


def run_backend(
    root: Path,
    *,
    resume: bool = False,
    groups: int = GROUPS,
    source: str = "comfyui_h3_context",
    floor: float = 75,
) -> int:
    root = root.resolve()
    cache = root / ".tmp/backend-gate"
    if not cache.resolve().is_relative_to(root):
        raise ValueError("Backend cache escapes workspace")
    cache.mkdir(parents=True, exist_ok=True)
    for path in [
        root / ".coverage",
        cache / "state.json",
        cache / "state.pending",
        cache / "runner.lock",
        cache / "coverage-shared",
        *(cache / f"coverage-{i}" for i in range(groups)),
    ]:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Backend cache file escapes its declared location")
    with execution_lock(cache):
        return _run_backend(root, resume=resume, groups=groups, source=source, floor=floor)


def _run_backend(root: Path, *, resume: bool, groups: int, source: str, floor: float) -> int:
    if groups < 1:
        raise ValueError("Backend group count must be positive")
    cache = root / ".tmp/backend-gate"
    state_path = root / STATE
    passed = read_state(state_path) if resume else {}
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(root), str(Path(__file__).resolve().parents[1])])
    plugin = ["-p", "scripts.gate_pytest_inventory"]
    # A fresh run directory prevents interrupted output from being mistaken for a new PASS.
    with tempfile.TemporaryDirectory(prefix="run-", dir=cache) as temporary:
        run_dir = Path(temporary)
        inventory = run_dir / "inventory.json"
        env["GATE_INVENTORY_OUT"] = str(inventory)
        env.pop("GATE_INVENTORY_EXPECTED", None)
        before_collection = common_fingerprint(root, [], source, groups)
        code = command(root, ["-m", "pytest", *plugin, "--collect-only", "-qq", "--no-cov"], env)
        if code:
            return code
        if before_collection != common_fingerprint(root, [], source, groups):
            print("Backend inputs changed during collection; no PASS recorded.", flush=True)
            return 2
        nodes: list[str] = json.loads(inventory.read_text(encoding="utf-8"))
        if not nodes or len(set(nodes)) != len(nodes):
            print("Backend inventory is empty or contains duplicates.", flush=True)
            return 2
        modules = sorted({node.split("::")[0] for node in nodes})
        for module in modules:
            if not (root / module).resolve().is_relative_to(root):
                raise ValueError("Collected test module escapes the workspace")
        deps = dependencies(root, modules)
        common = common_fingerprint(root, modules, source, groups)
        external = external_input_fingerprints(root)
        all_inputs = common_fingerprint(root, [], source, groups)
        if all_inputs != before_collection:
            print(
                "Backend inputs changed while preparing partitions; no PASS recorded.", flush=True
            )
            return 2
        fragments: list[Path] = []
        ran = skipped = 0
        started = time.monotonic()
        shared = {module for module in modules if deps[module] == set(modules)}
        partitions = [("shared", sorted(shared))] if shared else []
        partitions.extend(
            (
                str(group),
                [
                    module
                    for module in modules
                    if module not in shared and partition(module, groups) == group
                ],
            )
            for group in range(groups)
        )
        partitions = [(key, members) for key, members in partitions if members]
        for ordinal, (key, members) in enumerate(partitions, 1):
            label = f"Backend group {ordinal}/{len(partitions)} [{key}]"
            selected = [node for node in nodes if node.split("::")[0] in members]
            member_inputs = set().union(*(deps[member] for member in members))
            fingerprint = encoded_digest(
                [
                    common,
                    selected,
                    {p: digest(root / p) for p in sorted(member_inputs)},
                    {p: external[p] for p in sorted(member_inputs) if p in external},
                ]
            )
            reusable = not any(deps[member] & UNCACHED_TEST_INPUTS for member in members)
            fragment = cache / f"coverage-{key}"
            previous = passed.get(key, {})
            if (
                resume
                and reusable
                and isinstance(previous, dict)
                and previous.get("fingerprint") == fingerprint
                and valid_coverage(fragment, previous.get("coverage", ""))
            ):
                print(f"{label}: SKIP ({len(selected)} tests; {fingerprint[:16]})", flush=True)
                fragments.append(fragment)
                skipped += 1
                continue
            # IMPORTANT: revoke old PASS before executing; an interrupted retry cannot reuse it.
            passed.pop(key, None)
            if resume:
                write_state(state_path, passed)
            arguments = run_dir / "modules.txt"
            arguments.write_text("\n".join(members), encoding="utf-8")
            expected = run_dir / "expected.json"
            expected.write_text(json.dumps(selected), encoding="utf-8")
            env["GATE_INVENTORY_EXPECTED"] = str(expected)
            env["GATE_INVENTORY_OUT"] = str(run_dir / "actual.json")
            output = run_dir / ".coverage"
            output.unlink(missing_ok=True)
            env["COVERAGE_FILE"] = str(output)
            print(f"{label}: RUN ({len(selected)} tests; {fingerprint[:16]})", flush=True)
            group_start = time.monotonic()
            code = command(
                root,
                [
                    "-m",
                    "pytest",
                    *plugin,
                    f"@{arguments}",
                    f"--cov={source}",
                    "--cov-branch",
                    "--cov-report=",
                    "--cov-fail-under=0",
                    "--durations=10",
                    "--maxfail=1",
                ],
                env,
            )
            print(
                f"{label}: {time.monotonic() - group_start:.2f}s",
                flush=True,
            )
            if code:
                return code
            if all_inputs != common_fingerprint(root, [], source, groups) or not valid_coverage(
                output, digest(output)
            ):
                print("Backend inputs changed or coverage invalid; no PASS recorded.", flush=True)
                return 2
            if resume:
                output.replace(fragment)
                passed[key] = {"fingerprint": fingerprint, "coverage": digest(fragment)}
                write_state(state_path, passed)
            else:
                fragment = run_dir / f"coverage-{key}"
                output.replace(fragment)
            fragments.append(fragment)
            ran += 1
        # CRITICAL: always recombine verified current fragments and enforce the aggregate floor;
        # a whole-stage cached PASS would bypass missing data and permit false acceptance.
        if all_inputs != common_fingerprint(root, [], source, groups):
            print("Backend inputs changed during execution; aggregate rejected.", flush=True)
            return 2
        env["COVERAGE_FILE"] = str(root / ".coverage")
        code = command(root, ["-m", "coverage", "combine", "--keep", *map(str, fragments)], env)
        if code:
            return code
        code = command(
            root, ["-m", "coverage", "report", "--show-missing", f"--fail-under={floor}"], env
        )
        print(
            f"Backend groups: {ran} ran, {skipped} reused; {len(nodes)} tests; "
            f"{time.monotonic() - started:.2f}s; coverage exit {code}",
            flush=True,
        )
        return code


def product_test_modules(root: Path) -> tuple[list[str], list[str]]:
    """Explicit tooling exclusions; every new product test is included by default."""
    root = root.resolve()
    paths = sorted((root / "tests").rglob("test_*.py"))
    if any(not path.resolve().is_relative_to(root) for path in paths):
        raise ValueError("Product test path escapes workspace")
    modules = [path.relative_to(root).as_posix() for path in paths]
    return (
        [name for name in modules if name not in TOOLING_TESTS],
        [name for name in modules if name in TOOLING_TESTS],
    )


def run_product_backend(root: Path, *, list_only: bool = False) -> int:
    root = root.resolve()
    modules, deferred = product_test_modules(root)
    print(f"Product backend: {len(modules)} modules; coverage is not collected.", flush=True)
    print("Tooling modules reserved for affected checks / --coverage:", flush=True)
    for name in deferred:
        print(f"  {name}", flush=True)
    if not modules:
        print("No product test modules found; gate cannot pass.", flush=True)
        return 2
    if list_only:
        for name in modules:
            print(name)
        return 0
    cache = root / ".tmp/backend-gate"
    if not cache.resolve().is_relative_to(root):
        raise ValueError("Backend cache escapes workspace")
    cache.mkdir(parents=True, exist_ok=True)
    lock = cache / "runner.lock"
    if lock.is_symlink() or not lock.resolve().is_relative_to(cache.resolve()):
        raise ValueError("Backend lock escapes workspace")
    # IMPORTANT: product runs never consume coverage fragments or a previous whole-stage PASS.
    # Use the same lock as coverage diagnostics, so simultaneous runs cannot share test state.
    with execution_lock(cache), tempfile.TemporaryDirectory(prefix="product-", dir=cache) as temp:
        arguments = Path(temp) / "modules.txt"
        arguments.write_text("\n".join(modules), encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root)
        return command(
            root,
            ["-m", "pytest", f"@{arguments}", "--no-cov", "--maxfail=1", "--durations=10"],
            env,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--coverage", action="store_true", help="run all tests and the 75%% floor")
    parser.add_argument("--list", action="store_true", help="list product modules without testing")
    args = parser.parse_args()
    if args.coverage and args.list:
        parser.error("--list describes the product gate; use pytest --collect-only for all tests")
    try:
        root = Path(__file__).resolve().parents[1]
        if args.coverage:
            return run_backend(root, resume=args.resume)
        return run_product_backend(root, list_only=args.list)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Backend gate failed ({type(exc).__name__}); check inputs and active runners.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
