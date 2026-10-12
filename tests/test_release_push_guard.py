"""Exact-blob version admission and real bare-remote publication refusals."""

from __future__ import annotations

import copy
import importlib
import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.2.0"
PATHS = (
    "pyproject.toml",
    "frontend/package.json",
    "comfyui_h3_context/__init__.py",
    "comfyui_h3_context/core/installation_profiles.py",
    "comfyui_h3_context/contracts/installation_profiles_v1.json",
    "tests/fixtures/m8_04_release_matrix.json",
)


def guard() -> Any:
    return importlib.import_module("scripts.release_push_guard")


def payloads() -> dict[str, bytes]:
    return {path: (ROOT / path).read_bytes() for path in PATHS}


def mismatch(values: dict[str, bytes], field: int) -> None:
    path = PATHS[min(field, 5)]
    if field in {0, 2, 3}:
        values[path] = values[path].replace(b"1.2.0", b"1.2.1")
        return
    document = json.loads(values[path])
    if field == 1:
        document["version"] = "1.2.1"
    elif field == 4:
        document["profiles"][0]["distribution"] = "minimax-h3-studio==1.2.1"
    elif field == 5:
        document["package"]["version"] = "1.2.1"
    else:
        document["artifact"]["prefix"] = "minimax_h3_studio-1.2.1"
    values[path] = json.dumps(document).encode()


def test_literal_version_reader_does_not_import_candidate_code(tmp_path: Path) -> None:
    values = payloads()
    marker = tmp_path / "must-not-exist"
    values[PATHS[2]] += (
        f"\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n".encode()
    )
    assert guard().validate_version_authorities(values, VERSION) == VERSION
    assert not marker.exists()


@pytest.mark.parametrize("field", range(7))
def test_each_of_seven_version_authorities_must_agree(field: int) -> None:
    values = payloads()
    mismatch(values, field)
    with pytest.raises(ValueError, match="VERSION"):
        guard().validate_version_authorities(values, VERSION)


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "expected",
        "computed",
        "duplicate_assignment",
        "duplicate_json",
        "nonfinite",
        "boolean",
        "duplicate_profile",
        "computed_profile",
    ],
)
def test_ambiguous_or_computed_version_authority_is_refused(defect: str) -> None:
    values = payloads()
    expected = VERSION
    if defect == "missing":
        del values[PATHS[0]]
    elif defect == "expected":
        expected = "1.2.1"
    elif defect == "computed":
        values[PATHS[2]] = values[PATHS[2]].replace(
            b'__version__ = "1.2.0"', b'__version__ = "1.2." + "0"'
        )
    elif defect == "duplicate_assignment":
        values[PATHS[2]] += b'\n__version__ = "1.2.0"\n'
    elif defect == "duplicate_json":
        values[PATHS[1]] = b'{"version":"1.2.1","version":"1.2.0"}'
    elif defect == "nonfinite":
        values[PATHS[1]] = b'{"version":"1.2.0","x":NaN}'
    elif defect == "boolean":
        values[PATHS[1]] = b'{"version":true}'
    elif defect == "duplicate_profile":
        document = json.loads(values[PATHS[4]])
        document["profiles"].append(copy.deepcopy(document["profiles"][0]))
        values[PATHS[4]] = json.dumps(document).encode()
    else:
        values[PATHS[3]] = values[PATHS[3]].replace(
            b'distribution="minimax-h3-studio==1.2.0"',
            b'distribution="minimax-h3-studio==" + "1.2.0"',
        )
    with pytest.raises(ValueError, match="VERSION"):
        guard().validate_version_authorities(values, expected)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return result.stdout.decode().strip()


@pytest.fixture
def remote(tmp_path: Path) -> Any:
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--quiet", str(bare))
    repo = tmp_path / "candidate"
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "remote", "add", "origin", bare.as_posix())
    (repo / "README.md").write_text("Public baseline\n")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "test: public baseline")
    parent = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", f"{parent}:refs/heads/main")
    for name, data in payloads().items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "test: public candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    yield repo, bare, parent, candidate


def reader(candidate: str, *, defect: str | None = None) -> Any:
    from scripts import public_ci_gate as gate

    subject = dict(
        id=42,
        run_attempt=1,
        head_sha=candidate,
        head_branch=f"release-candidate/{VERSION}",
        event="push",
        path=gate.WORKFLOW_PATH,
        status="completed",
        conclusion="success",
        repository={"full_name": gate.REPOSITORY},
        head_repository={"full_name": gate.REPOSITORY},
    )
    if defect == "retry":
        subject["run_attempt"] = 2

    def response(path: str) -> dict[str, Any]:
        if "workflows" in path:
            rows = [] if defect == "empty" else [subject]
            if defect == "duplicate":
                rows.append({**subject, "id": 43})
            return {"total_count": len(rows), "workflow_runs": copy.deepcopy(rows)}
        if path == "actions/runs/42":
            return copy.deepcopy(subject)
        jobs = [
            dict(name=name, run_id=42, head_sha=candidate, status="completed", conclusion="success")
            for name in gate.EXPECTED_JOBS
        ]
        return {"total_count": len(jobs), "jobs": jobs}

    return response


def invoke(
    remote: Any,
    operation: str,
    *,
    candidate: str | None = None,
    api: Any = None,
    boundary: str = "guard",
) -> tuple[dict[str, Any], list[list[str]]]:
    repo, bare, _parent, original = remote
    subject = original if candidate is None else candidate
    calls = []

    def execute(argv: list[str], **kwargs: Any) -> Any:
        if "push" in argv:
            calls.append(argv)
        return subprocess.run(argv, **kwargs)

    push = (
        guard().push_release
        if boundary == "guard"
        else importlib.import_module("scripts.registry_release_controller").guarded_push
    )
    receipt = push(
        root=repo,
        candidate=subject,
        expected_version=VERSION,
        operation=operation,
        receipt=repo / ".tmp" / f"{operation}.json",
        runner=execute,
        reader=reader(subject, defect="empty" if operation == "candidate" else None)
        if api is None
        else api,
        expected_origin=bare.as_posix(),
    )
    return receipt, calls


@pytest.mark.parametrize("boundary", ["guard", "controller"])
def test_real_candidate_push_once_then_atomic_same_oid_main_and_annotated_tag(
    remote: Any, boundary: str
) -> None:
    repo, bare, _parent, candidate = remote
    proof, calls = invoke(remote, "candidate", boundary=boundary)
    assert proof["status"] == "PUSHED" and len(calls) == 1
    assert git(bare, "rev-parse", f"refs/heads/release-candidate/{VERSION}") == candidate
    git(repo, "tag", "-a", f"v{VERSION}", candidate, "-m", f"Release {VERSION}")
    proof, calls = invoke(remote, "promotion", boundary=boundary)
    assert len(calls) == 1 and "--atomic" in calls[0]
    assert proof["native_push_exit"] == 0 and proof["run_attempt"] == 1
    assert git(bare, "rev-parse", "refs/heads/main") == candidate
    assert git(bare, "rev-parse", f"refs/tags/v{VERSION}^{{}}") == candidate
    assert proof["tag_object"] == git(bare, "rev-parse", f"refs/tags/v{VERSION}")


@pytest.mark.parametrize(
    "defect",
    [
        "stale_parent",
        "extra_ancestor",
        "wrong_version",
        "retry",
        "duplicate",
        "lightweight_tag",
        "stale_rc",
    ],
)
def test_guard_refusal_performs_zero_pushes_and_preserves_all_remote_refs(
    remote: Any, defect: str
) -> None:
    repo, bare, parent, candidate = remote
    if defect == "stale_parent":
        git(repo, "checkout", "--detach", parent)
        (repo / "moved.txt").write_text("Public change\n")
        git(repo, "add", "moved.txt")
        git(repo, "commit", "--quiet", "-m", "test: move public tip")
        git(repo, "push", "origin", "HEAD:refs/heads/main")
    elif defect == "extra_ancestor":
        (repo / "extra.txt").write_text("Extra unpublished ancestor\n")
        git(repo, "add", "extra.txt")
        git(repo, "commit", "--quiet", "-m", "test: extra ancestor")
        candidate = git(repo, "rev-parse", "HEAD")
    elif defect == "wrong_version":
        (repo / "frontend/package.json").write_text('{"version":"1.2.1"}')
        git(repo, "add", "frontend/package.json")
        git(repo, "commit", "--amend", "--quiet", "-m", "test: version disagreement")
        candidate = git(repo, "rev-parse", "HEAD")
    git(
        repo,
        "push",
        "origin",
        f"{parent if defect == 'stale_rc' else candidate}:refs/heads/release-candidate/{VERSION}",
    )
    if defect == "lightweight_tag":
        git(repo, "tag", f"v{VERSION}", candidate)
    else:
        git(repo, "tag", "-a", f"v{VERSION}", candidate, "-m", f"Release {VERSION}")
    before = git(bare, "show-ref")
    calls = []

    def execute(argv: list[str], **kwargs: Any) -> Any:
        if "push" in argv:
            calls.append(argv)
        return subprocess.run(argv, **kwargs)

    with pytest.raises(ValueError):
        guard().push_release(
            root=repo,
            candidate=candidate,
            expected_version=VERSION,
            operation="promotion",
            receipt=repo / ".tmp" / "refusal.json",
            runner=execute,
            reader=reader(candidate, defect=defect),
            expected_origin=bare.as_posix(),
        )
    assert calls == []
    assert git(bare, "show-ref") == before


def refuse(
    remote: Any,
    *,
    operation: str = "promotion",
    api: Any = None,
    runner: Any = None,
    reason: str | None = None,
    expected_version: str = VERSION,
) -> dict[str, Any]:
    repo, bare, _parent, candidate = remote
    before = git(bare, "show-ref")
    calls = []

    def execute(argv: list[str], **kwargs: Any) -> Any:
        if "push" in argv:
            calls.append(argv)
        return subprocess.run(argv, **kwargs) if runner is None else runner(argv, **kwargs)

    target = repo / ".tmp/refusal-proof.json"
    with pytest.raises(ValueError, match=reason):
        guard().push_release(
            root=repo,
            candidate=candidate,
            expected_version=expected_version,
            operation=operation,
            receipt=target,
            runner=execute,
            reader=reader(candidate) if api is None else api,
            expected_origin=bare.as_posix(),
        )
    assert not calls
    assert git(bare, "show-ref") == before
    proof = cast(dict[str, Any], json.loads(target.read_text()))
    assert proof["status"] == "REFUSED" and proof["native_push_exit"] is None
    return proof


def ready(remote: Any) -> None:
    repo, _bare, _parent, candidate = remote
    git(repo, "push", "origin", f"{candidate}:refs/heads/release-candidate/{VERSION}")
    git(repo, "tag", "-a", f"v{VERSION}", candidate, "-m", f"Release {VERSION}")


@pytest.mark.parametrize("field", range(7))
def test_exact_projection_versions_refuse_even_when_checkout_is_matching(
    remote: Any, field: int
) -> None:
    repo, bare, parent, _candidate = remote
    values = payloads()
    mismatch(values, field)
    for name, data in values.items():
        (repo / name).write_bytes(data)
    git(repo, "add", "--all")
    git(repo, "commit", "--amend", "--quiet", "-m", "test: mismatched projected version")
    candidate = git(repo, "rev-parse", "HEAD")
    subject = repo, bare, parent, candidate
    ready(subject)
    for name, data in payloads().items():
        (repo / name).write_bytes(data)
    refuse(subject, reason="VERSION_AUTHORITIES_DISAGREE")


@pytest.mark.parametrize(
    "defect", ["expected", "missing", "computed", "duplicate_json", "duplicate_profile"]
)
def test_invalid_version_shapes_have_zero_remote_effect(remote: Any, defect: str) -> None:
    repo, bare, parent, candidate = remote
    expected = VERSION
    if defect == "expected":
        expected = "1.2.1"
    elif defect == "missing":
        git(repo, "rm", "--quiet", PATHS[0])
    elif defect == "computed":
        target = repo / PATHS[2]
        target.write_bytes(
            target.read_bytes().replace(b'__version__ = "1.2.0"', b'__version__ = "1.2." + "0"')
        )
    elif defect == "duplicate_json":
        (repo / PATHS[1]).write_bytes(b'{"version":"1.2.1","version":"1.2.0"}')
    else:
        target = repo / PATHS[4]
        data = json.loads(target.read_bytes())
        data["profiles"].append(copy.deepcopy(data["profiles"][0]))
        target.write_text(json.dumps(data))
    if defect != "expected":
        git(repo, "add", "--all")
        git(repo, "commit", "--amend", "--quiet", "-m", "test: invalid authority")
        candidate = git(repo, "rev-parse", "HEAD")
    subject = repo, bare, parent, candidate
    ready(subject)
    refuse(subject, expected_version=expected)


@pytest.mark.parametrize(
    "defect", ["missing", "lightweight", "misnamed", "mistargeted", "existing_remote"]
)
def test_tag_identity_refuses_without_retagging(remote: Any, defect: str) -> None:
    repo, _bare, parent, candidate = remote
    git(repo, "push", "origin", f"{candidate}:refs/heads/release-candidate/{VERSION}")
    name = f"v{VERSION}"
    if defect == "lightweight":
        git(repo, "tag", name, candidate)
    elif defect != "missing":
        actual_name = "different" if defect == "misnamed" else name
        git(
            repo,
            "tag",
            "-a",
            actual_name,
            parent if defect == "mistargeted" else candidate,
            "-m",
            "Public fixture",
        )
        if defect == "misnamed":
            git(
                repo,
                "update-ref",
                f"refs/tags/{name}",
                git(repo, "rev-parse", "refs/tags/different"),
            )
        elif defect == "existing_remote":
            git(repo, "push", "origin", f"refs/tags/{name}:refs/tags/{name}")
    refuse(remote)


@pytest.mark.parametrize(
    "defect", ["force", "lease", "plus", "other_ref", "other_source", "split", "delete"]
)
def test_final_outgoing_arguments_refuse_zero_native_pushes(
    remote: Any, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    repo, _bare, parent, candidate = remote
    ready(remote)
    original = guard().build_push_arguments

    def altered(operation: str, oid: str, version: str, tag: str | None) -> list[str]:
        argv = cast(list[str], original(operation, oid, version, tag))
        if defect == "force":
            argv.insert(2, "--force")
        elif defect == "lease":
            argv.insert(2, "--force-with-lease")
        elif defect == "plus":
            argv[-2] = "+" + argv[-2]
        elif defect == "other_ref":
            argv[-2] = f"{candidate}:refs/heads/dev"
        elif defect == "other_source":
            argv[-2] = f"{parent}:refs/heads/main"
        elif defect == "split":
            argv.remove("--atomic")
            argv.pop()
        else:
            argv[-2] = ":refs/heads/main"
        return argv

    monkeypatch.setattr(guard(), "build_push_arguments", altered)
    refuse(remote, reason="OUTGOING_PUSH_ARGUMENTS_REFUSED")


def test_g2_complete_ancestry_refuses_if_parent_read_is_faulted(remote: Any) -> None:
    repo, bare, parent, _candidate = remote
    (repo / "private-ancestor.txt").write_text("Fixture ancestor\n")
    git(repo, "add", "private-ancestor.txt")
    git(repo, "commit", "--quiet", "-m", "test: extra ancestor")
    candidate = git(repo, "rev-parse", "HEAD")
    subject = repo, bare, parent, candidate
    ready(subject)

    def faulted(argv: list[str], **kwargs: Any) -> Any:
        if argv[2:5] == ["rev-list", "--parents", "-n"]:
            return subprocess.CompletedProcess(argv, 0, f"{candidate} {parent}\n".encode(), b"")
        return subprocess.run(argv, **kwargs)

    refuse(subject, runner=faulted, reason="PUBLIC_ONE_COMMIT_DELTA_REQUIRED")


@pytest.mark.parametrize("defect", ["already_ran", "existing_rc"])
def test_same_candidate_cannot_start_another_hosted_run(remote: Any, defect: str) -> None:
    repo, _bare, _parent, candidate = remote
    if defect == "existing_rc":
        git(repo, "push", "origin", f"{candidate}:refs/heads/release-candidate/{VERSION}")
    refuse(
        remote,
        operation="candidate",
        api=reader(candidate, defect="empty" if defect == "existing_rc" else None),
    )


def test_mutable_checkout_does_not_replace_version_blob_authority(remote: Any) -> None:
    repo, bare, _parent, candidate = remote
    (repo / PATHS[1]).write_text('{"version":"9.9.9"}')
    proof, calls = invoke(remote, "candidate")
    assert len(calls) == 1 and proof["version"] == VERSION
    assert git(bare, "rev-parse", f"refs/heads/release-candidate/{VERSION}") == candidate
