"""Compare the declared Python consumer facade against an explicit released Git source.

Only static call shapes, public members and expression identities are compared. Semantic
annotations, transitive values, JSON wire contracts and runtime behavior need their owning tests.
Neither source snapshot is imported or executed.
"""

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
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from griffe import (
        Alias,
        Class,
        DataclassesExtension,
        Extensions,
        Function,
        GriffeLoader,
        Module,
        Object,
        find_breaking_changes,
    )
except ImportError:
    LIBRARY_AVAILABLE = False
else:
    LIBRARY_AVAILABLE = True

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "comfyui_h3_context"
FACADE = PACKAGE + ".public_api"
POLICY = "governance/contracts/package_compatibility_policy_v1.json"
MAX_FILES = 512
MAX_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024
LIMITS = [
    "Static call shapes and public expression identities only",
    "No semantic annotation or transitive constant-value proof",
    "No JSON schema, prompt, report, node/socket or runtime compatibility proof",
]


class Refusal(Exception):
    """A fixed, content-free diagnostic category."""


def _library() -> None:
    # CRITICAL: missing or changed diff semantics must refuse, never emit a traceback or PASS.
    if not LIBRARY_AVAILABLE:
        raise Refusal("install_pinned_dev_dependencies")
    if importlib.metadata.version("griffelib") != "2.3.0":
        raise Refusal("unsupported_static_library_version")


def _exports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_bytes())
    declarations = []
    for node in ast.walk(tree):
        # CRITICAL: indexed, aliased or passed export lists may mutate after a literal declaration.
        if isinstance(node, ast.Name) and node.id == "__all__" and isinstance(node.ctx, ast.Load):
            raise Refusal("dynamic_exports")
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = node.targets
        if any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
            if node not in tree.body or not isinstance(node, (ast.Assign, ast.AnnAssign)):
                raise Refusal("dynamic_exports")
            declarations.append(node.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "__all__":
                raise Refusal("dynamic_exports")
    if len(declarations) != 1:
        raise Refusal("dynamic_exports")
    value = declarations[0]
    if not isinstance(value, (ast.List, ast.Tuple)):
        raise Refusal("dynamic_exports")
    names = []
    for element in value.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            raise Refusal("dynamic_exports")
        if not element.value.isidentifier():
            raise Refusal("invalid_export")
        names.append(element.value)
    if len(set(names)) != len(names):
        raise Refusal("duplicate_export")
    return tuple(names)


def _target(obj: Object | Alias) -> Object:
    # CRITICAL: Griffe skips unresolved aliases during diff; resolve first or a breach can PASS.
    return obj.final_target if isinstance(obj, Alias) else obj


def _validate_public(obj: Object | Alias, loader: GriffeLoader, seen: set[int]) -> None:
    target = _target(obj)
    if id(target) in seen:
        return
    seen.add(id(target))
    if len(seen) > 4096:
        raise Refusal("public_graph_limit")
    if isinstance(target, (Class, Function)):
        allowed = {"dataclasses.dataclass", "staticmethod", "classmethod", "property"}
        for decorator in target.decorators:
            canonical = getattr(decorator.value, "canonical_path", str(decorator.value))
            if canonical not in allowed:
                raise Refusal("unsupported_decorator")
        # IMPORTANT: ordinary class methods create empty overload entries; only real lists refuse.
        overloads = (
            any(target.overloads.values()) if isinstance(target, Class) else target.overloads
        )
        if overloads:
            raise Refusal("unsupported_overloads")
    if isinstance(target, Class):
        if target.keywords:
            raise Refusal("unsupported_class_keywords")
        for base in target.bases:
            canonical = getattr(base, "canonical_path", str(base))
            if canonical in {"object", "builtins.object"}:
                continue
            resolved = _target(loader.modules_collection[canonical])
            if not isinstance(resolved, Class):
                raise Refusal("unsupported_base")
            _validate_public(resolved, loader, seen)
        for member in target.all_members.values():
            if member.is_public:
                _validate_public(member, loader, seen)
    elif isinstance(target, Module):
        raise Refusal("unsupported_module_export")


def _surface(root: Path, package: str, facade: str, names: tuple[str, ...]) -> Module:
    facade_path = root / Path(*facade.split(".")).with_suffix(".py")
    exports = _exports(facade_path)
    # CRITICAL: inspection defaults can import candidate code and execute factories or INPUT_TYPES.
    loader = GriffeLoader(
        search_paths=[root],
        allow_inspection=False,
        force_inspection=False,
        extensions=Extensions(DataclassesExtension()),
    )
    loader.load(package, try_relative_path=False, find_stubs_package=False)
    loader.resolve_aliases(implicit=False, external=False, max_iterations=32)
    module = loader.modules_collection[facade]
    public = Module("supported_api")
    public.exports = list(names)
    for name in names:
        if name not in exports:
            continue
        member = module.members[name]
        _validate_public(member, loader, set())
        public.set_member(name, Alias(name, _target(member), parent=public))
    return public


def compare_packages(
    old_root: Path,
    new_root: Path,
    *,
    package: str,
    facade: str,
    names: tuple[str, ...],
) -> dict[str, Any]:
    """Return public names/categories, never source expressions or library exception payloads."""
    try:
        _library()
        old_exports = _exports(old_root / Path(*facade.split(".")).with_suffix(".py"))
        new_exports = _exports(new_root / Path(*facade.split(".")).with_suffix(".py"))
        if not set(names).issubset(old_exports):
            raise Refusal("invalid_baseline_exports")
        findings = [
            {"name": name, "category": "OBJECT_REMOVED"}
            for name in names
            if name not in new_exports
        ]
        old = _surface(old_root, package, facade, names)
        new = _surface(new_root, package, facade, tuple(dict.fromkeys((*names, *new_exports))))
        # IMPORTANT: diff traverses members; direct Function comparisons miss call-shape changes.
        for breach in find_breaking_changes(old, new):
            finding = {"name": breach.obj.name, "category": breach.kind.name}
            if finding not in findings:
                findings.append(finding)
        return {
            "status": "BREAKING" if findings else "PASS",
            "findings": findings,
            "compared_exports": len(names),
            "limits": LIMITS,
        }
    except Exception as error:
        category = str(error) if isinstance(error, Refusal) else "unsupported_static_resolution"
        return {"status": "UNSUPPORTED", "findings": [{"category": category}], "limits": LIMITS}


def _git(root: Path, *args: str, input_data: bytes | None = None) -> bytes:
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
        cwd=root,
        env=environment,
        input=input_data,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise Refusal("git_input_unavailable")
    return result.stdout


def _safe_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise Refusal("invalid_input_path")
    if path.parts[:1] != (PACKAGE,):
        raise Refusal("invalid_package_path")
    return path


def _regular(root: Path, relative: str) -> Path:
    path = root / relative
    for candidate in (root, *reversed(path.parents), path):
        if candidate != root and root not in candidate.parents:
            continue
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise Refusal("nonregular_working_input")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise Refusal("nonregular_working_input")
    return path


def _bounded(files: dict[str, bytes]) -> None:
    if (
        sum(name.endswith(".py") for name in files) > MAX_FILES
        or sum(map(len, files.values())) > MAX_BYTES
    ):
        raise Refusal("snapshot_limit")
    if any(len(value) > MAX_FILE_BYTES for value in files.values()):
        raise Refusal("file_limit")


def _baseline(root: Path, oid: str) -> dict[str, bytes]:
    selected: list[tuple[str, bytes]] = []
    entries = _git(root, "ls-tree", "-rz", oid, "--", PACKAGE, POLICY).split(b"\0")
    for entry in entries:
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, blob = metadata.split()
        name = raw_name.decode("utf-8")
        if name != POLICY:
            _safe_relative(name)
        if not name.endswith(".py") and name != POLICY:
            continue
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise Refusal("nonregular_baseline_input")
        selected.append((name, blob))
        if sum(path.endswith(".py") for path, _ in selected) > MAX_FILES:
            raise Refusal("snapshot_limit")
    requests = b"".join(blob + b"\n" for _, blob in selected)
    headers = _git(root, "cat-file", "--batch-check", input_data=requests).splitlines()
    sizes = []
    for (_, blob), header in zip(selected, headers, strict=True):
        actual, kind, raw_size = header.split()
        if actual != blob or kind != b"blob":
            raise Refusal("invalid_baseline_blob")
        size = int(raw_size)
        if size > MAX_FILE_BYTES:
            raise Refusal("file_limit")
        sizes.append(size)
    if sum(sizes) > MAX_BYTES:
        raise Refusal("snapshot_limit")
    # IMPORTANT: check all sizes before reading batch payloads; a huge blob must never be loaded.
    payload = _git(root, "cat-file", "--batch", input_data=requests)
    files: dict[str, bytes] = {}
    offset = 0
    for (name, blob), size in zip(selected, sizes, strict=True):
        end = payload.index(b"\n", offset)
        if payload[offset:end] != blob + b" blob " + str(size).encode():
            raise Refusal("invalid_baseline_blob")
        offset = end + 1
        files[name] = payload[offset : offset + size]
        offset += size
        if payload[offset : offset + 1] != b"\n":
            raise Refusal("invalid_baseline_blob")
        offset += 1
    if offset != len(payload):
        raise Refusal("invalid_baseline_blob")
    return files


def _candidate(root: Path) -> dict[str, bytes]:
    modes = {}
    for entry in _git(root, "ls-files", "--stage", "-z", "--", PACKAGE, POLICY).split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, _, stage = metadata.split()
        modes[raw_name.decode("utf-8")] = (mode, stage)
    files: dict[str, bytes] = {}
    for raw_name in _git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", PACKAGE, POLICY
    ).split(b"\0"):
        if not raw_name:
            continue
        name = raw_name.decode("utf-8")
        if name != POLICY:
            _safe_relative(name)
        if not name.endswith(".py") and name != POLICY:
            continue
        if name in modes and modes[name] not in {(b"100644", b"0"), (b"100755", b"0")}:
            raise Refusal("nonregular_candidate_index")
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        path = _regular(root, name)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise Refusal("file_limit")
        if sum(map(len, files.values())) + size > MAX_BYTES:
            raise Refusal("snapshot_limit")
        # IMPORTANT: policy and Python share one budget; bound reads even if a file grows.
        with path.open("rb") as stream:
            files[name] = stream.read(MAX_FILE_BYTES + 1)
        _bounded(files)
    return files


def _policy(raw: bytes, exports: tuple[str, ...]) -> tuple[str, ...]:
    if len(raw) > MAX_FILE_BYTES:
        raise Refusal("policy_limit")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise Refusal("duplicate_policy_key")
            value[key] = item
        return value

    # CRITICAL: last-key-wins parsing can hide an authority redirect in duplicated JSON keys.
    policy = json.loads(raw, object_pairs_hook=unique)
    if policy.get("schema") != "h3.context.package_compatibility_policy.v1":
        raise Refusal("invalid_policy")
    authority = policy["consumer_authority"]
    if (
        authority.get("authority_id") != "package"
        or authority.get("path") != PACKAGE + "/public_api.py"
        or authority.get("selector") != "__all__"
        or authority.get("relation") != "supported_python_consumer_surface"
    ):
        raise Refusal("authority_redirect")
    names = authority["names"]
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) or not name.isidentifier() for name in names)
        or len(set(names)) != len(names)
        or set(names) != set(exports)
    ):
        raise Refusal("invalid_policy_exports")
    return tuple(names)


def _write_snapshot(root: Path, files: dict[str, bytes]) -> None:
    for name, raw in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Explicit local released Git reference")
    args = parser.parse_args(argv)
    temporary: Path | None = None
    try:
        _library()
        root = ROOT.resolve()
        oid = (
            _git(root, "rev-parse", "--verify", "--end-of-options", args.base + "^{commit}")
            .decode()
            .strip()
        )
        if len(oid) not in {40, 64} or any(char not in "0123456789abcdef" for char in oid):
            raise Refusal("invalid_baseline_oid")
        old_files = _baseline(root, oid)
        new_files = _candidate(root)
        old_policy = old_files.pop(POLICY)
        new_policy = new_files.pop(POLICY)
        parent = root / ".tmp"
        if parent.exists():
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise Refusal("unsafe_temporary_parent")
        parent.mkdir(exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="public-consumer-", dir=parent))
        if not temporary.resolve().is_relative_to(root):
            raise Refusal("unsafe_temporary_path")
        old_root, new_root = temporary / "old", temporary / "new"
        _write_snapshot(old_root, old_files)
        _write_snapshot(new_root, new_files)
        facade_path = Path(PACKAGE) / "public_api.py"
        names = _policy(old_policy, _exports(old_root / facade_path))
        _policy(new_policy, _exports(new_root / facade_path))
        result = compare_packages(old_root, new_root, package=PACKAGE, facade=FACADE, names=names)
        # IMPORTANT: a result must describe unchanged candidate bytes, not a concurrent newer tree.
        if _candidate(root) != {**new_files, POLICY: new_policy}:
            raise Refusal("candidate_drift")
        result["baseline_oid"] = oid
        result["candidate_digest"] = hashlib.sha256(
            b"".join(
                name.encode() + b"\0" + hashlib.sha256(raw).digest()
                for name, raw in sorted(new_files.items())
            )
            + hashlib.sha256(new_policy).digest()
        ).hexdigest()
        result["snapshot_counts"] = {"baseline": len(old_files), "candidate": len(new_files)}
        exit_code = {"PASS": 0, "BREAKING": 1, "UNSUPPORTED": 2}[result["status"]]
    except Exception as error:
        category = str(error) if isinstance(error, Refusal) else "invalid_static_input"
        result = {"status": "REFUSED", "findings": [{"category": category}], "limits": LIMITS}
        exit_code = 2
    finally:
        if temporary is not None:
            # The destination was newly created and contained; never delete a caller-supplied path.
            try:
                shutil.rmtree(temporary)
            except OSError:
                # CRITICAL: cleanup errors contain private paths; refuse without raw exceptions.
                result = {
                    "status": "REFUSED",
                    "findings": [{"category": "snapshot_cleanup_failed"}],
                    "limits": LIMITS,
                }
                exit_code = 2
    print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
