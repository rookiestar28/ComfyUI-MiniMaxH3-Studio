"""Prepare a verified public release capsule using separate tooling and payload identities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.safe_paths import (  # noqa: E402
    validate_directory,
    validate_regular_file,
)
from scripts.product_completeness import smoke_archive, smoke_checkout  # noqa: E402
from scripts.public_projection import entries, exact_commit, git, project, snapshot  # noqa: E402
from scripts.registry_payload import build_registry_payload_report  # noqa: E402
from scripts.registry_publication_capsule import (  # noqa: E402
    compute_capsule_sha256,
    create_capsule,
    unpack_capsule,
    verify_capsule_sha256,
)
from scripts.registry_publish_guard import decide_publish, parse_project_version  # noqa: E402
from scripts.validate_comfy_registry_metadata import validate_metadata  # noqa: E402


class ReleaseControllerError(ValueError):
    """The release subject or token-free prepare chain could not be proven."""


def _environment(output: Path) -> dict[str, str]:
    # CRITICAL: preparation never receives inherited provider/Registry credentials and never
    # executes payload-selected tooling. The fixed pack entry comes from the verified checkout.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(
            mark in key.upper()
            for mark in ("TOKEN", "API_KEY", "PASSWORD", "SECRET", "COOKIE", "CREDENTIAL")
        )
        and key not in {"PYTHONPATH", "PYTHONHOME", "WSLENV"}
    }
    environment.update(
        PYTHONDONTWRITEBYTECODE="1",
        COMFY_NO_TELEMETRY="1",
        DO_NOT_TRACK="1",
        TMP=str(output),
        TEMP=str(output),
        TMPDIR=str(output),
    )
    return environment


def _run(command: list[str], *, cwd: Path, output: Path) -> None:
    completed = subprocess.run(
        command, cwd=cwd, env=_environment(output), capture_output=True, check=False, timeout=600
    )
    # CLI output may contain candidate file names, but this chain cannot obtain provider payloads.
    (output / "pack.stdout.log").write_bytes(completed.stdout)
    (output / "pack.stderr.log").write_bytes(completed.stderr)
    if completed.returncode != 0:
        raise ReleaseControllerError("pinned CLI pack failed; inspect workspace logs")


def prepare(
    *, tooling_sha: str, source: str, parent: str, version: str, environment: Path, output: Path
) -> dict[str, Any]:
    root = validate_directory(ROOT)
    tooling_sha = exact_commit(root, tooling_sha)
    if git(root, "rev-parse", "HEAD").decode().strip() != tooling_sha:
        raise ReleaseControllerError("tooling SHA does not match checked-out HEAD")
    if git(root, "diff", "HEAD", "--name-only") or git(
        root, "ls-files", "-ci", "--exclude-standard"
    ):
        raise ReleaseControllerError("tooling checkout is dirty or tracks ignored paths")
    tooling_entries = entries(root, tooling_sha)
    for name in (
        "__init__.py",
        "registry_release_controller.py",
        "public_projection.py",
        "product_completeness.py",
        "registry_pack_entry.py",
        "registry_payload.py",
        "registry_publication_capsule.py",
        "registry_publish_guard.py",
        "validate_comfy_registry_metadata.py",
    ):
        relative = "scripts/" + name
        binding = tooling_entries.get(relative)
        # CRITICAL: a clean diff does not bind untracked scripts. Require each fixed executable
        # helper to be a regular blob at the tooling SHA with identical checked-out bytes.
        if binding is None or binding[0] not in {"100644", "100755"} or binding[1] != "blob":
            raise ReleaseControllerError("required tooling is not tracked at the tooling SHA")
        if validate_regular_file(root / relative).read_bytes() != git(
            root, "cat-file", "blob", binding[2]
        ):
            raise ReleaseControllerError("required tooling bytes differ from the tooling SHA")
    source, parent = exact_commit(root, source), exact_commit(root, parent)
    current = parse_project_version(git(root, "show", source + ":pyproject.toml"), "source")
    previous_tree = entries(root, parent)
    previous = (
        parse_project_version(git(root, "show", parent + ":pyproject.toml"), "predecessor")
        if "pyproject.toml" in previous_tree
        else None
    )
    if current[0] != version or not decide_publish(current, previous)[0]:
        raise ReleaseControllerError("version expectation or predecessor progression failed")
    environment = validate_directory(environment)
    output = output.absolute()
    if (
        not environment.is_relative_to(root)
        or not output.is_relative_to(root)
        or ".." in output.parts
        or output.exists()
    ):
        raise ReleaseControllerError("environment/output must be workspace-owned and output fresh")
    # Validate all ancestor components before creating output; resolve() alone hides reparse paths.
    validate_directory(output.parent)
    python = validate_regular_file(
        environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    lock = (root / "requirements/registry-publish-py310-linux-x86_64.txt").read_text(
        encoding="utf-8"
    )
    versions = dict(
        re.findall(r"^([A-Za-z0-9._-]+)==([^ ]+) --hash=sha256:[0-9a-f]{64}$", lock, re.M)
    )
    if len(versions) != 51 or versions.get("comfy-cli") != "1.16.0":
        raise ReleaseControllerError("publication lock is not the expected closed dependency set")
    probe = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import importlib.metadata as m,json,sys; "
            "expected=json.loads(sys.argv[1]); "
            "assert all(m.version(k)==v for k,v in expected.items())",
            json.dumps(versions),
        ],
        env=_environment(output.parent),
        capture_output=True,
        check=False,
        timeout=60,
    )
    if probe.returncode != 0:
        raise ReleaseControllerError("installed publication environment does not match the lock")
    before = snapshot(root)
    output.mkdir()
    projection = project(root, source=source, parent=parent, output=output / "projection")
    public_commit = str(projection["public_commit"])
    payload = output / "payload"
    payload.mkdir()
    git(payload, "init", "--quiet")
    hooks = output / "empty-hooks"
    hooks.mkdir()
    git(payload, "config", "core.hooksPath", str(hooks))
    git(payload, "fetch", "--quiet", str(root), public_commit)
    git(payload, "checkout", "--quiet", "--detach", public_commit)
    if entries(payload, "HEAD") != entries(root, public_commit):
        raise ReleaseControllerError("isolated public payload does not match projection")
    validate_metadata(require_finalized=True, source_root=payload, public_projection=True)
    smoke_checkout(payload)
    config = output / "cli-config"
    config.mkdir()
    _run(
        [
            str(python),
            "-I",
            str(root / "scripts/registry_pack_entry.py"),
            "--config-root",
            str(config),
        ],
        cwd=payload,
        output=output,
    )
    archive = payload / "node.zip"
    report = build_registry_payload_report(archive, source_root=payload)
    smoke_archive(archive, payload, report)
    report_path = output / "registry-payload.json"
    report_bytes = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    report_path.write_bytes(report_bytes)
    capsule = output / "publication-capsule.tgz"
    create_capsule(
        capsule,
        environment=environment,
        archive=archive,
        report=report_path,
        candidate=public_commit,
    )
    digest = compute_capsule_sha256(capsule)
    verify_capsule_sha256(capsule, digest)
    transferred = output / "verified-transfer"
    transferred.mkdir()
    unpack_capsule(capsule, transferred, expected_candidate=public_commit)
    if (transferred / "registry-payload.json").read_bytes() != report_bytes:
        raise ReleaseControllerError("transferred report does not match audited report")
    if build_registry_payload_report(transferred / "node.zip", source_root=payload) != report:
        raise ReleaseControllerError("transferred archive does not match audited payload")
    if snapshot(root) != before:
        raise ReleaseControllerError("source index or HEAD changed during preparation")
    receipt: dict[str, Any] = {
        "schema": "h3-context-release-preparation/1",
        "status": "PREPARED",
        "tooling_commit": tooling_sha,
        "source_commit": source,
        "public_parent": parent,
        "public_commit": public_commit,
        "public_tree": projection["public_tree"],
        "version": version,
        "archive_sha256": report["archive_sha256"],
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "capsule_sha256": digest,
        "pack_count": 1,
        "source_index_head_unchanged": True,
        "publication_performed": False,
    }
    (output / "release-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tooling-sha", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--public-parent", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = prepare(
            tooling_sha=args.tooling_sha,
            source=args.source,
            parent=args.public_parent,
            version=args.expected_version,
            environment=args.environment,
            output=args.output,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print("Release preparation: FAIL; no publication performed")
        return 1
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
