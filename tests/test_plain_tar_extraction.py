"""Real archive bytes, containment and metadata on the minimum tarfile API."""

from __future__ import annotations

import io
import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts.build_gate_report import BuildGateReportError, _safe_extract_sdist

SOURCE = "minimax_h3_studio-1.0.2"


@pytest.fixture(params=[False, True], ids=["native-filter", "absent-filter"])
def filter_capability(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.param:
        monkeypatch.setattr(tarfile, "data_filter", None, raising=False)


def _archive(root: Path, members: list[tuple[str, bytes, bytes]]) -> Path:
    path = root / (SOURCE + ".tar.gz")
    with tarfile.open(path, "w:gz") as archive:
        for name, kind, body in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = 0o7777
            info.uid = info.gid = 123_456
            info.uname = info.gname = "synthetic-owner"
            if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                info.linkname = "../../foreign"
            info.size = len(body) if kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(body) if kind == tarfile.REGTYPE else None)
    return path


def _link(link: Path, target: Path, root: Path) -> None:
    assert link.is_relative_to(root) and target.is_relative_to(root)
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, "owned junction creation is required for this guard"
    else:
        link.symlink_to(target, target_is_directory=True)


def test_plain_bytes_extract_without_replaying_privileged_metadata(
    tmp_path: Path, filter_capability: None
) -> None:
    archive = _archive(
        tmp_path,
        [
            (SOURCE, tarfile.DIRTYPE, b""),
            (SOURCE + "/source.txt", tarfile.REGTYPE, b"actual source bytes"),
        ],
    )
    source = _safe_extract_sdist(archive, tmp_path / "extract")
    file = source / "source.txt"
    assert file.read_bytes() == b"actual source bytes"
    assert not file.stat().st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
    uid_reader = getattr(os, "getuid", None)
    if uid_reader is not None:
        assert file.stat().st_uid == uid_reader()


@pytest.mark.parametrize(
    "name",
    [
        SOURCE + "/a/../escape",
        SOURCE + "/a/..",
        SOURCE + "/a:stream",
        SOURCE + "/C:escape",
        SOURCE + "/a\\b",
        SOURCE + "//a",
        SOURCE + "/./a",
    ],
)
def test_unsafe_paths_are_refused_before_writing_any_member(
    tmp_path: Path, filter_capability: None, name: str
) -> None:
    archive = _archive(
        tmp_path,
        [(SOURCE + "/safe.txt", tarfile.REGTYPE, b"safe"), (name, tarfile.REGTYPE, b"unsafe")],
    )
    destination = tmp_path / "extract"
    with pytest.raises(BuildGateReportError):
        _safe_extract_sdist(archive, destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "kind",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE, b"Z"],
)
def test_non_plain_types_are_refused_before_writing_any_member(
    tmp_path: Path, filter_capability: None, kind: bytes
) -> None:
    archive = _archive(
        tmp_path, [(SOURCE + "/safe.txt", tarfile.REGTYPE, b"safe"), (SOURCE + "/other", kind, b"")]
    )
    destination = tmp_path / "extract"
    with pytest.raises(BuildGateReportError):
        _safe_extract_sdist(archive, destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "names",
    [
        ("same.txt", "same.txt"),
        ("Same.txt", "same.txt"),
        ("folder/a", "Folder/b"),
        ("file", "file/child"),
    ],
)
def test_aliases_and_file_ancestors_are_refused_before_writes(
    tmp_path: Path, filter_capability: None, names: tuple[str, str]
) -> None:
    archive = _archive(
        tmp_path, [(SOURCE + "/" + name, tarfile.REGTYPE, b"data") for name in names]
    )
    destination = tmp_path / "extract"
    with pytest.raises(BuildGateReportError):
        _safe_extract_sdist(archive, destination)
    assert not destination.exists()


def test_existing_file_is_not_overwritten_or_preceded_by_other_writes(
    tmp_path: Path, filter_capability: None
) -> None:
    destination = tmp_path / "extract"
    source = destination / SOURCE
    source.mkdir(parents=True)
    sentinel = source / "existing.txt"
    sentinel.write_bytes(b"owned sentinel")
    archive = _archive(
        tmp_path,
        [
            (SOURCE + "/fresh.txt", tarfile.REGTYPE, b"fresh"),
            (SOURCE + "/existing.txt", tarfile.REGTYPE, b"replacement"),
        ],
    )
    with pytest.raises(BuildGateReportError):
        _safe_extract_sdist(archive, destination)
    assert sentinel.read_bytes() == b"owned sentinel"
    assert not (source / "fresh.txt").exists()


@pytest.mark.parametrize("destination_redirect", [False, True])
def test_owned_external_sentinel_survives_real_link_or_reparse_redirect(
    tmp_path: Path, filter_capability: None, destination_redirect: bool
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "sentinel.txt"
    sentinel.write_bytes(b"owned foreign sentinel")
    destination = tmp_path / "extract"
    if destination_redirect:
        _link(destination, foreign, tmp_path)
    else:
        destination.mkdir()
        _link(destination / SOURCE, foreign, tmp_path)
    archive = _archive(tmp_path, [(SOURCE + "/fresh.txt", tarfile.REGTYPE, b"new")])
    with pytest.raises(BuildGateReportError):
        _safe_extract_sdist(archive, destination)
    assert sentinel.read_bytes() == b"owned foreign sentinel"
    assert not (foreign / "fresh.txt").exists()
    assert not (foreign / SOURCE).exists()
