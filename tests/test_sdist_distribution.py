"""Exercise the standard backend's real tarball, rather than manifest glob guesses."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import public_build_backend as backend
from scripts import sdist_payload

ROOT = Path(__file__).resolve().parents[1]
REVIEWED_INPUTS = (
    "frontend/tests/e2e/helpers/nleHardeningGatherer.mjs",
    "frontend/tests/e2e/helpers/nleSemanticBrowserStageGatherer.d.mts",
    "frontend/tests/e2e/helpers/nleSemanticBrowserStageGatherer.mjs",
    "frontend/tests/fixtures/generic_media/still-32.png",
    "frontend/tests/fixtures/m25_20_semantic_media/img-overlay-hd.png",
    "frontend/tests/fixtures/m25_20_semantic_media/img-overlay.png",
    "frontend/tests/fixtures/m25_56_audio_observer_fake.py",
    "tests/fixtures/m14_03_semantic_graph_comparator.json.gz",
)


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    temporary = root / ".tmp"
    temporary.mkdir(exist_ok=True)
    environment.update(TMP=str(temporary), TEMP=str(temporary), TMPDIR=str(temporary))
    result = subprocess.run(
        list(arguments),
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    return result


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    build_system = (ROOT / "pyproject.toml").read_text().split("[project]", 1)[0]
    metadata = (
        build_system
        + """[project]
name = "sdist-probe"
version = "1.0.0"
requires-python = ">=3.10"
license = "Apache-2.0"
license-files = ["LICENSE", "NOTICE"]
[tool.setuptools.packages.find]
include = ["comfyui_h3_context*"]
[tool.comfy]
includes = ["comfyui_h3_context"]
[tool.h3-context.development]
roots = ["tests", "frontend", "scripts", "governance", "compatibility", "requirements"]
required = [".gitignore", "MANIFEST.in"]
"""
    )
    files = {
        "pyproject.toml": metadata.encode(),
        "MANIFEST.in": (ROOT / "MANIFEST.in").read_bytes(),
        ".gitignore": (ROOT / ".gitignore").read_bytes(),
        ".gitattributes": b"scripts/probe.py text eol=lf\n",
        "README.md": b"Public source build probe.\n",
        "RELEASE_NOTES.md": b"Public release build probe.\n",
        "LICENSE": b"Apache-2.0\n",
        "NOTICE": b"Public probe.\n",
        "__init__.py": b"import comfyui_h3_context\n",
        "comfyui_h3_context/__init__.py": b"VALUE = 1\n",
        "scripts/__init__.py": b"",
        "tests/test_probe.py": b"def test_probe(): assert True\n",
        "governance/probe.json": b"{}\n",
        "compatibility/probe.json": b"{}\n",
        "requirements/probe.txt": b"\n",
    }
    for name in ("public_source_policy.py", "public_build_backend.py"):
        candidate = ROOT / "scripts" / name
        if candidate.exists():
            files["scripts/" + name] = candidate.read_bytes()
    for relative in REVIEWED_INPUTS:
        files[relative] = (ROOT / relative).read_bytes()
    for relative, payload in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    _run(root, "git", "init", "-q")
    _run(root, "git", "add", ".")
    return root


def _sdist(root: Path) -> Path:
    _run(
        root,
        sys.executable,
        "-B",
        "-m",
        "build",
        "--sdist",
        "--no-isolation",
        "--outdir",
        str(root / ".tmp" / "artifacts"),
    )
    return next((root / ".tmp" / "artifacts").glob("*.tar.gz"))


def _members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path, "r:gz") as archive:
        result = {}
        for member in archive.getmembers():
            if member.isfile():
                stream = archive.extractfile(member)
                assert stream is not None
                result[member.name.split("/", 1)[1]] = stream.read()
        return result


def test_standard_sdist_carries_all_reviewed_suffixes_and_ignore_policy(source: Path) -> None:
    members = _members(_sdist(source))
    required = {*REVIEWED_INPUTS, ".gitignore", ".gitattributes", "MANIFEST.in", "RELEASE_NOTES.md"}
    assert required <= members.keys(), sorted(required - members.keys())
    for relative in required:
        assert members[relative] == (source / relative).read_bytes()


def test_standard_sdist_excludes_nested_ignored_records_and_stale_metadata(source: Path) -> None:
    probes = (
        "tests/fixtures/.planning/private-probe.json",
        "frontend/tests/reference/private-probe.json",
        "scripts/.sessions/private-probe.py",
        "tests/fixtures/untracked-probe.json",
        "tests/fixtures/.cache/probe.txt",
        "tests/TEST_SOP.md",
        "frontend/node_modules/private-probe.json",
        "comfyui_h3_context/untracked_private.py",
    )
    for relative in probes:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic private build probe\n")
    stale = source / "sdist_probe.egg-info"
    stale.mkdir()
    (stale / "SOURCES.txt").write_text("\n".join(probes) + "\n")
    members = _members(_sdist(source))
    assert not members.keys() & set(probes)
    assert all(b"synthetic private build probe" not in value for value in members.values())
    _run(
        source,
        sys.executable,
        "-B",
        "-m",
        "build",
        "--wheel",
        "--no-isolation",
        "--outdir",
        str(source / ".tmp" / "wheels"),
    )
    wheel = next((source / ".tmp" / "wheels").glob("*.whl"))
    with zipfile.ZipFile(wheel) as built:
        assert not any("untracked_private" in name for name in built.namelist())


def _extract(path: Path, target: Path) -> Path:
    target.mkdir()
    for name, payload in _members(path).items():
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    return target


def test_issued_sdist_rebuilds_without_git_and_preserves_editable_source(
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _sdist(source)
    issued = _extract(archive, source.parent / "issued")
    extra = issued / "tests" / "unlisted.json"
    extra.write_text("synthetic extra input\n")
    monkeypatch.setattr(
        backend, "_git", lambda *args, **kwargs: pytest.fail("Git-less rebuild needs no Git")
    )
    payloads = backend.public_sources(issued)
    assert "tests/unlisted.json" not in payloads
    rebuilt = _members(_sdist(issued))
    for relative in payloads:
        assert rebuilt[relative] == payloads[relative]
    assert "tests/unlisted.json" not in rebuilt
    _run(
        issued,
        sys.executable,
        "-B",
        "-m",
        "build",
        "--wheel",
        "--no-isolation",
        "--outdir",
        str(issued / ".tmp" / "wheels"),
    )
    wheel = next((issued / ".tmp" / "wheels").glob("*.whl"))
    with zipfile.ZipFile(wheel) as built:
        assert (
            built.read("comfyui_h3_context/__init__.py")
            == payloads["comfyui_h3_context/__init__.py"]
        )
        assert all("unlisted" not in name for name in built.namelist())
    _run(
        source,
        sys.executable,
        "-B",
        "-c",
        "from build import ProjectBuilder; ProjectBuilder('.').build('editable', '.tmp/editable')",
    )
    editable = next((source / ".tmp" / "editable").glob("*.whl"))
    with zipfile.ZipFile(editable) as built:
        finders = [built.read(n).decode() for n in built.namelist() if n.endswith("_finder.py")]
        assert finders and str(source / "comfyui_h3_context") in "".join(finders).replace(
            "\\\\", "\\"
        )


@pytest.mark.parametrize("damage", ["bytes", "schema", "duplicate", "private", "missing"])
def test_issued_inventory_rejects_damage(source: Path, damage: str) -> None:
    issued = _extract(_sdist(source), source.parent / "issued")
    inventory = issued / backend.INVENTORY
    if damage == "bytes":
        (issued / REVIEWED_INPUTS[0]).write_bytes(b"changed\n")
    elif damage == "missing":
        (issued / REVIEWED_INPUTS[0]).unlink()
    elif damage == "duplicate":
        inventory.write_text(
            '{"schema":"h3-public-source-inventory/1","schema":"duplicate","files":{}}'
        )
    else:
        value = json.loads(inventory.read_bytes())
        if damage == "schema":
            value["unexpected"] = True
        else:
            value["files"]["tests/.planning/private.json"] = "0" * 64
        inventory.write_text(json.dumps(value))
    with pytest.raises(backend.PublicBuildError):
        backend.public_sources(issued)


def test_actual_tar_audit_detects_omission_private_member_and_changed_bytes(source: Path) -> None:
    archive = _sdist(source)
    assert sdist_payload.build_sdist_payload_report(archive, source_root=source)["status"] == "PASS"
    original = _members(archive)
    prefix = "sdist_probe-1.0.0/"
    for defect in ("omission", "private", "bytes"):
        payloads = dict(original)
        if defect == "omission":
            del payloads[REVIEWED_INPUTS[0]]
        elif defect == "private":
            payloads["tests/.planning/private.json"] = b"synthetic probe\n"
        else:
            payloads[REVIEWED_INPUTS[0]] = b"changed\n"
        damaged = source / ".tmp" / (defect + ".tar.gz")
        with tarfile.open(damaged, "w:gz") as target:
            for relative, payload in payloads.items():
                member = tarfile.TarInfo(prefix + relative)
                member.size = len(payload)
                target.addfile(member, io.BytesIO(payload))
        with pytest.raises(sdist_payload.SdistPayloadError):
            sdist_payload.audited_members(damaged, source)


def test_candidate_ignore_and_source_bounds_fail_closed(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A tracked public fixture becomes denied by the candidate policy without force staging.
    with (source / ".gitignore").open("a") as policy:
        policy.write("\nfrontend/tests/e2e/helpers/*.mjs\n")
    with pytest.raises(backend.PublicBuildError, match="Git-ignored"):
        backend.public_sources(source)
    (source / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    monkeypatch.setattr(backend, "MAX_FILE_BYTES", 4)
    with pytest.raises(backend.PublicBuildError, match="bounded regular"):
        backend.public_sources(source)


@pytest.mark.parametrize(
    ("bound", "message"),
    [("MAX_FILES", "excessive"), ("MAX_TOTAL_BYTES", "total size bound")],
)
def test_source_aggregate_bounds_fail_closed(
    source: Path, monkeypatch: pytest.MonkeyPatch, bound: str, message: str
) -> None:
    monkeypatch.setattr(backend, bound, 1)
    with pytest.raises(backend.PublicBuildError, match=message):
        backend.public_sources(source)


@pytest.mark.parametrize(
    "name",
    [
        "../private",
        "tests/CON.txt",
        "tests/e\u0301.txt",
        "tests/a//b",
        "tests/a\\b",
        "tests/a:stream",
        "tests/a. ",
    ],
)
def test_noncanonical_source_paths_are_rejected(name: str) -> None:
    with pytest.raises(backend.PublicBuildError):
        backend.canonical(name)


def test_raw_gitless_archive_cannot_use_parent_repository_authority(source: Path) -> None:
    raw = source.parent / "raw"
    raw.mkdir()
    (raw / "pyproject.toml").write_text("[project]\nname='raw'\n")
    with pytest.raises(backend.PublicBuildError, match="exact Git root"):
        backend.public_sources(raw)


@pytest.mark.parametrize(
    "defect", ["link", "duplicate", "case", "traversal", "private_metadata", "oversize", "corrupt"]
)
def test_tar_hostile_members_are_rejected(source: Path, defect: str) -> None:
    archive = source / ".tmp" / "hostile.tar.gz"
    prefix = "sdist_probe-1.0.0/"
    with tarfile.open(archive, "w:gz") as target:
        if defect == "link":
            info = tarfile.TarInfo(prefix + REVIEWED_INPUTS[0])
            info.type = tarfile.SYMTYPE
            info.linkname = "../private"
            target.addfile(info)
        else:
            names = {
                "duplicate": ["README.md", "README.md"],
                "case": ["README.md", "readme.md"],
                "traversal": ["../private.json"],
                "private_metadata": ["PKG-INFO"],
                "oversize": ["README.md"],
                "corrupt": ["README.md"],
            }[defect]
            for relative in names:
                payload = (
                    b"reference/docs/private-report\n"
                    if defect == "private_metadata"
                    else (source / "README.md").read_bytes()
                )
                info = tarfile.TarInfo(prefix + relative)
                info.size = backend.MAX_FILE_BYTES + 1 if defect == "oversize" else len(payload)
                if defect == "oversize":
                    payload = b"x" * info.size
                target.addfile(info, io.BytesIO(payload))
    if defect == "corrupt":
        raw = bytearray(archive.read_bytes())
        raw[-8] ^= 255
        archive.write_bytes(raw)
    with pytest.raises(sdist_payload.SdistPayloadError):
        sdist_payload.audited_members(archive, source)


def test_source_symlink_is_rejected(source: Path) -> None:
    target = source / "tests" / "linked.py"
    # This is a fresh owned fixture; the Git entry is staged before replacing it with a link.
    target.write_text("VALUE = 1\n")
    _run(source, "git", "add", "tests/linked.py")
    target.unlink()
    try:
        target.symlink_to(source / "__init__.py")
    except OSError as exc:
        pytest.skip("Windows symlink privilege unavailable: " + type(exc).__name__)
    with pytest.raises(backend.PublicBuildError, match="link or reparse"):
        backend.public_sources(source)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction guard")
def test_source_directory_junction_is_rejected_without_symlink_privilege(source: Path) -> None:
    directory = source / "tests" / "junction"
    directory.mkdir()
    member = directory / "probe.json"
    member.write_bytes(b"{}\n")
    _run(source, "git", "add", "tests/junction/probe.json")
    member.unlink()
    directory.rmdir()
    target = source / ".tmp" / "junction-target"
    target.mkdir()
    (target / "probe.json").write_bytes(b"{}\n")
    _run(source, "cmd", "/c", "mklink", "/J", str(directory), str(target))
    with pytest.raises(backend.PublicBuildError, match="link or reparse"):
        backend.public_sources(source)
