"""Validate exact public release objects and own the immediately following force-free push."""

from __future__ import annotations

import argparse
import ast
import json
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import public_ci_gate as ci  # noqa: E402

VERSION_PATHS = (
    "pyproject.toml",
    "frontend/package.json",
    "comfyui_h3_context/__init__.py",
    "comfyui_h3_context/core/installation_profiles.py",
    "comfyui_h3_context/contracts/installation_profiles_v1.json",
    "tests/fixtures/m8_04_release_matrix.json",
)
FULL_OID = re.compile(r"[0-9a-f]{40}")
ORIGINS = frozenset(
    {
        f"https://github.com/{ci.REPOSITORY}",
        f"https://github.com/{ci.REPOSITORY}.git",
        f"git@github.com:{ci.REPOSITORY}.git",
        f"ssh://git@github.com/{ci.REPOSITORY}.git",
    }
)


class ReleasePushError(ValueError):
    """A closed, content-free refusal before publication or a failed native push."""


def _oid(value: str) -> str:
    if not FULL_OID.fullmatch(value) or value == "0" * 40:
        raise ReleasePushError("OBJECT_ID_INVALID")
    return value


def _json(raw: bytes) -> dict[str, Any]:
    value = json.loads(
        raw, object_pairs_hook=ci._unique_members, parse_constant=ci._finite_constant
    )
    if not isinstance(value, dict):
        raise ReleasePushError("VERSION_JSON_INVALID")
    return value


def _literal(node: ast.AST) -> str:
    if not isinstance(node, ast.Constant) or type(node.value) is not str:
        raise ReleasePushError("VERSION_LITERAL_REQUIRED")
    return node.value


def _package_version(raw: bytes) -> str:
    tree = ast.parse(raw)
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "__version__"
    ]
    bindings = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "__version__"
        and isinstance(node.ctx, ast.Store)
    ]
    if len(assignments) != 1 or len(bindings) != 1:
        raise ReleasePushError("VERSION_PACKAGE_AUTHORITY_AMBIGUOUS")
    return _literal(assignments[0].value)


def _profile_version(raw: bytes) -> str:
    tree = ast.parse(raw)
    builders = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_default_installation_profiles"
    ]
    if len(builders) != 1:
        raise ReleasePushError("VERSION_PROFILE_BUILDER_AMBIGUOUS")
    body = builders[0].body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if (
        len(body) != 1
        or not isinstance(body[0], ast.Return)
        or not isinstance(body[0].value, ast.Call)
    ):
        raise ReleasePushError("VERSION_PROFILE_BUILDER_UNSUPPORTED")
    registry = body[0].value
    if not isinstance(registry.func, ast.Name) or registry.func.id != "InstallationProfileManifest":
        raise ReleasePushError("VERSION_PROFILE_BUILDER_UNSUPPORTED")
    groups = [keyword.value for keyword in registry.keywords if keyword.arg == "profiles"]
    if len(groups) != 1 or not isinstance(groups[0], ast.Tuple):
        raise ReleasePushError("VERSION_PROFILE_LITERAL_REQUIRED")
    core = []
    ids = set()
    for node in groups[0].elts:
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id != "InstallationProfile"
        ):
            raise ReleasePushError("VERSION_PROFILE_LITERAL_REQUIRED")
        names = [keyword.arg for keyword in node.keywords]
        if None in names or len(names) != len(set(names)):
            raise ReleasePushError("VERSION_PROFILE_AUTHORITY_AMBIGUOUS")
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        profile = _literal(keywords["profile_id"])
        if profile in ids:
            raise ReleasePushError("VERSION_PROFILE_AUTHORITY_AMBIGUOUS")
        ids.add(profile)
        if profile == "core_manual":
            core.append(_literal(keywords["distribution"]))
    if len(core) != 1:
        raise ReleasePushError("VERSION_CORE_PROFILE_AMBIGUOUS")
    return core[0]


def validate_version_authorities(payloads: dict[str, bytes], expected_version: str) -> str:
    """Read the six exact Git blobs; never import or evaluate candidate Python."""
    try:
        # CRITICAL: keep parser loading lazy; an eager import breaks isolated --help on
        # Python 3.10 without site-packaged tomli before argparse can handle the request.
        from scripts.registry_publish_guard import SEMVER, parse_project_version

        if (
            not isinstance(expected_version, str)
            or len(expected_version) > 64
            or SEMVER.fullmatch(expected_version) is None
        ):
            raise ReleasePushError("VERSION_EXPECTATION_INVALID")
        for name in VERSION_PATHS:
            if not isinstance(payloads[name], bytes) or len(payloads[name]) > 262_144:
                raise ReleasePushError("VERSION_BLOB_INVALID")
        project = parse_project_version(payloads[VERSION_PATHS[0]], "candidate")[0]
        frontend = _json(payloads[VERSION_PATHS[1]])["version"]
        package = _package_version(payloads[VERSION_PATHS[2]])
        profile = _profile_version(payloads[VERSION_PATHS[3]])
        manifest = _json(payloads[VERSION_PATHS[4]])["profiles"]
        if not isinstance(manifest, list) or any(not isinstance(row, dict) for row in manifest):
            raise ReleasePushError("VERSION_PROFILE_JSON_INVALID")
        core = [row for row in manifest if row.get("profile_id") == "core_manual"]
        if len(core) != 1:
            raise ReleasePushError("VERSION_CORE_PROFILE_AMBIGUOUS")
        matrix = _json(payloads[VERSION_PATHS[5]])
        values = (
            project,
            frontend,
            package,
            profile,
            core[0]["distribution"],
            matrix["package"]["version"],
            matrix["artifact"]["prefix"],
        )
        expected = (
            expected_version,
            expected_version,
            expected_version,
            f"minimax-h3-studio=={expected_version}",
            f"minimax-h3-studio=={expected_version}",
            expected_version,
            f"minimax_h3_studio-{expected_version}",
        )
        if any(type(value) is not str for value in values) or values != expected:
            raise ReleasePushError("VERSION_AUTHORITIES_DISAGREE")
        return expected_version
    except ReleasePushError:
        raise
    except Exception:  # noqa: BLE001 - parser input must never enter failure output
        raise ReleasePushError("VERSION_AUTHORITY_INVALID") from None


def _execute(root: Path, argv: list[str], runner: Any) -> subprocess.CompletedProcess[bytes]:
    try:
        return cast(
            subprocess.CompletedProcess[bytes],
            runner(argv, cwd=root, capture_output=True, check=False, timeout=60),
        )
    except (OSError, subprocess.SubprocessError):
        raise ReleasePushError("GIT_OPERATION_UNAVAILABLE") from None


def _git(root: Path, runner: Any, *args: str) -> bytes:
    result = _execute(root, ["git", "--no-optional-locks", *args], runner)
    if result.returncode != 0 or len(result.stdout) > 2_000_000:
        raise ReleasePushError("GIT_READ_FAILED")
    return result.stdout


def _remote(root: Path, runner: Any, ref: str) -> str | None:
    raw = _git(root, runner, "ls-remote", "origin", ref).decode("ascii").strip()
    if not raw:
        return None
    rows = raw.splitlines()
    if len(rows) != 1:
        raise ReleasePushError("REMOTE_REF_AMBIGUOUS")
    fields = rows[0].split("\t")
    if len(fields) != 2 or fields[1] != ref:
        raise ReleasePushError("REMOTE_REF_INVALID")
    return _oid(fields[0])


def _ancestry(root: Path, runner: Any, candidate: str, parent: str) -> None:
    # CRITICAL: a new RC/tag can upload every reachable private ancestor. Git's ordinary
    # fast-forward check protects an existing branch, not the ancestry of newly created refs.
    fields = (
        _git(root, runner, "rev-list", "--parents", "-n", "1", candidate).decode("ascii").split()
    )
    if fields != [candidate, parent]:
        raise ReleasePushError("PUBLIC_SINGLE_PARENT_REQUIRED")
    if _git(root, runner, "rev-list", candidate, "--not", parent).decode("ascii").splitlines() != [
        candidate
    ]:
        raise ReleasePushError("PUBLIC_ONE_COMMIT_DELTA_REQUIRED")


def build_push_arguments(
    operation: str, candidate: str, version: str, tag: str | None
) -> list[str]:
    if operation == "candidate":
        return [
            "git",
            "push",
            "--porcelain",
            "origin",
            f"{candidate}:refs/heads/release-candidate/{version}",
        ]
    return [
        "git",
        "push",
        "--porcelain",
        "--atomic",
        "origin",
        f"{candidate}:refs/heads/main",
        f"{tag}:refs/tags/v{version}",
    ]


def validate_push_arguments(
    argv: list[str], operation: str, candidate: str, version: str, tag: str | None
) -> None:
    # CRITICAL: validate the final outgoing argv, not a preceding dry-run. Rebuilding a push
    # afterward lets a force flag, private source ref or split main/tag operation bypass admission.
    if operation == "candidate":
        expected = [
            "git",
            "push",
            "--porcelain",
            "origin",
            f"{candidate}:refs/heads/release-candidate/{version}",
        ]
    elif operation == "promotion" and tag is not None:
        expected = [
            "git",
            "push",
            "--porcelain",
            "--atomic",
            "origin",
            f"{candidate}:refs/heads/main",
            f"{tag}:refs/tags/v{version}",
        ]
    else:
        raise ReleasePushError("PUSH_OPERATION_INVALID")
    if argv != expected:
        raise ReleasePushError("OUTGOING_PUSH_ARGUMENTS_REFUSED")


def _receipt_path(root: Path, target: Path) -> Path:
    absolute = target.absolute()
    try:
        relative = absolute.relative_to(root)
    except ValueError:
        raise ReleasePushError("RECEIPT_OUTSIDE_WORKSPACE") from None
    if not relative.parts or relative.parts[0] != ".tmp" or ".." in relative.parts:
        raise ReleasePushError("RECEIPT_MUST_BE_LOCAL_TEMPORARY")
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.exists() or cursor.is_symlink():
            mode = cursor.lstat()
            if stat.S_ISLNK(mode.st_mode) or getattr(mode, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            ):
                raise ReleasePushError("RECEIPT_LINK_REFUSED")
    if absolute.exists():
        raise ReleasePushError("RECEIPT_ALREADY_EXISTS")
    absolute.parent.mkdir(parents=True, exist_ok=True)
    return absolute


def push_release(
    *,
    root: Path,
    candidate: str,
    expected_version: str,
    operation: str,
    receipt: Path,
    runner: Any = subprocess.run,
    reader: Any = None,
    expected_origin: str | None = None,
) -> dict[str, Any]:
    """Own validation, native push and safe readback within one operation boundary."""
    root = root.resolve(strict=True)
    output = _receipt_path(root, receipt)
    proof: dict[str, Any] = {
        "schema": "h3-context-guarded-release-push/1",
        "started": datetime.now(timezone.utc).isoformat(),
        "operation": operation if operation in {"candidate", "promotion"} else None,
        "status": "REFUSED",
        "native_push_exit": None,
    }
    try:
        _oid(candidate)
        if operation not in {"candidate", "promotion"}:
            raise ReleasePushError("PUSH_OPERATION_INVALID")
        origins = ORIGINS if expected_origin is None else frozenset({expected_origin})
        for args in (
            ("remote", "get-url", "--all", "origin"),
            ("remote", "get-url", "--push", "--all", "origin"),
        ):
            urls = _git(root, runner, *args).decode("utf-8").splitlines()
            if len(urls) != 1 or urls[0] not in origins:
                raise ReleasePushError("PUBLIC_ORIGIN_IDENTITY_REQUIRED")
        if (
            _git(root, runner, "rev-parse", "--verify", candidate + "^{commit}").decode().strip()
            != candidate
        ):
            raise ReleasePushError("EXACT_COMMIT_REQUIRED")
        parent = _remote(root, runner, "refs/heads/main")
        if parent is None:
            raise ReleasePushError("LIVE_PUBLIC_PARENT_REQUIRED")
        _ancestry(root, runner, candidate, parent)
        blobs = {}
        for path in VERSION_PATHS:
            entry = (
                _git(root, runner, "ls-tree", candidate, "--", path).decode().strip().split("\t")
            )
            if (
                len(entry) != 2
                or entry[1] != path
                or entry[0].split()[:2] not in (["100644", "blob"], ["100755", "blob"])
            ):
                raise ReleasePushError("VERSION_REGULAR_BLOB_REQUIRED")
            blobs[path] = _git(root, runner, "cat-file", "blob", entry[0].split()[2])
        version = validate_version_authorities(blobs, expected_version)
        tree = _oid(_git(root, runner, "rev-parse", candidate + "^{tree}").decode().strip())
        rc = f"refs/heads/release-candidate/{version}"
        tag_ref = f"refs/tags/v{version}"
        tag: str | None = None
        run: dict[str, Any] | None = None
        read = ci.github_json if reader is None else reader
        if operation == "candidate":
            if _remote(root, runner, rc) is not None:
                raise ReleasePushError("CANDIDATE_REF_ALREADY_EXISTS")
            if ci.inventory_runs(
                read(f"actions/workflows/ci.yml/runs?head_sha={candidate}&per_page=100"), candidate
            ):
                raise ReleasePushError("CANDIDATE_SHA_ALREADY_RAN")
        else:
            if _remote(root, runner, rc) != candidate:
                raise ReleasePushError("LIVE_RC_IDENTITY_CHANGED")
            tag = _oid(_git(root, runner, "rev-parse", "--verify", tag_ref).decode().strip())
            if _git(root, runner, "cat-file", "-t", tag).strip() != b"tag":
                raise ReleasePushError("ANNOTATED_TAG_REQUIRED")
            headers = _git(root, runner, "cat-file", "-p", tag).split(b"\n\n", 1)[0].splitlines()
            for header in (
                f"object {candidate}".encode(),
                b"type commit",
                f"tag v{version}".encode(),
            ):
                if headers.count(header) != 1:
                    raise ReleasePushError("TAG_TARGET_OR_NAME_INVALID")
            if _git(root, runner, "rev-parse", tag + "^{}").decode().strip() != candidate:
                raise ReleasePushError("TAG_TARGET_INVALID")
            if (
                _remote(root, runner, tag_ref) is not None
                or _remote(root, runner, tag_ref + "^{}") is not None
            ):
                raise ReleasePushError("REMOTE_TAG_ALREADY_EXISTS")
            run, advisory = ci.read_ci_proof(candidate, f"release-candidate/{version}", reader=read)
            proof.update(
                run_id=run["id"],
                run_attempt=1,
                run_conclusion=run["conclusion"],
                windows_browser_conclusion=advisory,
            )
        # Keep the last live identities adjacent to the owned push; native force-free/atomic
        # semantics refuse a concurrent main update instead of silently overwriting it.
        if _remote(root, runner, "refs/heads/main") != parent or _remote(root, runner, rc) != (
            None if operation == "candidate" else candidate
        ):
            raise ReleasePushError("LIVE_PUBLIC_REFS_CHANGED")
        if operation == "promotion" and (
            _remote(root, runner, tag_ref) is not None
            or _remote(root, runner, tag_ref + "^{}") is not None
        ):
            raise ReleasePushError("REMOTE_TAG_ALREADY_EXISTS")
        argv = build_push_arguments(operation, candidate, version, tag)
        validate_push_arguments(argv, operation, candidate, version, tag)
        proof.update(
            candidate=candidate,
            tree=tree,
            public_parent=parent,
            version=version,
            tag_object=tag,
            refspecs=argv[5:] if operation == "promotion" else argv[4:],
        )
        pushed = _execute(root, argv, runner)
        proof["native_push_exit"] = pushed.returncode
        if pushed.returncode != 0:
            proof["status"] = "PUSH_FAILED"
            raise ReleasePushError("NATIVE_PUSH_FAILED")
        proof["status"] = "PUSHED_READBACK_FAILED"
        if _remote(root, runner, rc) != candidate:
            raise ReleasePushError("CANDIDATE_PUSH_READBACK_FAILED")
        if operation == "promotion":
            if (
                _remote(root, runner, "refs/heads/main") != candidate
                or _remote(root, runner, tag_ref) != tag
                or _remote(root, runner, tag_ref + "^{}") != candidate
            ):
                raise ReleasePushError("ATOMIC_PROMOTION_READBACK_FAILED")
            ci.approved_run(
                read(f"actions/workflows/ci.yml/runs?head_sha={candidate}&per_page=100"),
                candidate,
                f"release-candidate/{version}",
            )
        proof["status"] = "PUSHED"
        return proof
    except ReleasePushError as error:
        proof["reason"] = str(error)
        raise
    except Exception:  # noqa: BLE001 - raw API/Git/parser payloads must remain private
        proof["reason"] = "RELEASE_PROOF_UNAVAILABLE"
        raise ReleasePushError("RELEASE_PROOF_UNAVAILABLE") from None
    finally:
        proof["finished"] = datetime.now(timezone.utc).isoformat()
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(proof, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--operation", choices=("candidate", "promotion"), required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        proof = push_release(
            root=args.root,
            candidate=args.candidate,
            expected_version=args.expected_version,
            operation=args.operation,
            receipt=args.receipt,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Release push: FAIL; inspect the local content-free receipt", file=sys.stderr)
        return 1
    print(
        f"Release push: PASS candidate={proof['candidate']} "
        f"operation={proof['operation']} native-exit={proof['native_push_exit']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
