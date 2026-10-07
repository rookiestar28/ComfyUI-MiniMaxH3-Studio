"""M8-04 release artifact, clean-install, and matrix manifest contracts."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import unicodedata
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import tomli

import scripts.release_matrix as release_matrix
from scripts.release_matrix import (
    MAX_ARCHIVE_MEMBER_BYTES,
    ReleaseMatrixError,
    _inspect_archive,
    artifact_inventory,
    load_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
PROJECT_VERSION = release_matrix._project_version()
PROJECT = tomli.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
DISTRIBUTION = PROJECT["project"]["name"].replace("-", "_")
ARTIFACT_PREFIX = f"{DISTRIBUTION}-{PROJECT_VERSION}"


class ReleaseMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)

    def _temporary_directory(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(prefix="h3-m15-07-test-", dir=ROOT / ".tmp")

    @staticmethod
    def _make_directory_link(link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            result = subprocess.run(
                [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(link), str(target)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
                timeout=5,
            )
            if result.returncode != 0:
                raise OSError("could not create Windows junction fixture") from None

    def _write_valid_pair(self, directory: Path) -> tuple[Path, Path]:
        wheel = directory / f"{ARTIFACT_PREFIX}-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                "comfyui_h3_context/__init__.py",
                f"__version__ = '{PROJECT_VERSION}'\n",
            )
        sdist = directory / f"{ARTIFACT_PREFIX}.tar.gz"
        with tarfile.open(sdist, "w:gz") as archive:
            payload = f"__version__ = '{PROJECT_VERSION}'\n".encode()
            info = tarfile.TarInfo(f"{ARTIFACT_PREFIX}/comfyui_h3_context/__init__.py")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        return wheel, sdist

    def test_manifest_freezes_owner_artifact_and_required_host_lanes(self) -> None:
        manifest = load_manifest()
        self.assertEqual(manifest["schema"], "h3-context-release-matrix/1")
        self.assertEqual(
            manifest["package"],
            {
                "name": "minimax-h3-studio",
                "version": PROJECT_VERSION,
                "author": "rookiestar28",
                "dependencies": [],
            },
        )
        self.assertEqual(manifest["host"]["required"], True)
        self.assertEqual(
            "".join(manifest["host"]["revision_parts"]),
            "b323a345bbbfb2f3a95b5b73b68eb7919a26515e",  # pragma: allowlist secret
        )
        self.assertEqual(manifest["offline"]["probe_repetitions"], 2)

    def test_artifact_inventory_accepts_exact_wheel_and_sdist(self) -> None:
        manifest = load_manifest()
        with self._temporary_directory() as temporary:
            directory = Path(temporary)
            wheel, sdist = self._write_valid_pair(directory)
            self.assertEqual(artifact_inventory(directory, manifest), (wheel, sdist))

    def test_artifact_prefix_matches_build_distribution_metadata(self) -> None:
        self.assertEqual(load_manifest()["artifact"]["prefix"], ARTIFACT_PREFIX)

    def test_uninstall_rejects_residual_distribution_metadata(self) -> None:
        names = (
            f"{DISTRIBUTION}-{PROJECT_VERSION}.dist-info",
            f"{DISTRIBUTION}-{PROJECT_VERSION}.egg-info",
            f"{DISTRIBUTION}.egg-info",
        )
        for name in names:
            with self.subTest(name=name), self._temporary_directory() as temporary:
                site_packages = Path(temporary)
                (site_packages / name).mkdir()
                sentinel = site_packages / "foreign-sentinel.txt"
                sentinel.write_text("FOREIGN_SENTINEL\n", encoding="utf-8")
                with (
                    patch.object(
                        subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                    ),
                    self.assertRaisesRegex(ReleaseMatrixError, "package-owned paths remained"),
                ):
                    release_matrix._assert_uninstalled(
                        Path("unused-python"), site_packages, sentinel
                    )

    def test_manifest_and_artifact_roots_reject_link_or_reparse_ancestor(self) -> None:
        with self._temporary_directory() as temporary:
            base = Path(temporary)
            target = base / "target"
            target.mkdir()
            self._write_valid_pair(target)
            manifest_path = target / "manifest.json"
            manifest_path.write_bytes(
                (ROOT / "tests" / "fixtures" / "m8_04_release_matrix.json").read_bytes()
            )
            link = base / "link"
            self._make_directory_link(link, target)
            try:
                with self.assertRaisesRegex(ReleaseMatrixError, "link|reparse"):
                    load_manifest(link / manifest_path.name)
                with self.assertRaisesRegex(ReleaseMatrixError, "link|reparse"):
                    artifact_inventory(link, load_manifest())
            finally:
                link.unlink() if link.is_symlink() else link.rmdir()

    def test_artifact_inventory_rejects_extra_files_and_private_payload(self) -> None:
        manifest = load_manifest()
        with self._temporary_directory() as temporary:
            directory = Path(temporary)
            for name in (
                f"{ARTIFACT_PREFIX}-py3-none-any.whl",
                f"{ARTIFACT_PREFIX}.tar.gz",
            ):
                (directory / name).write_bytes(b"not-an-archive")
            (directory / "unexpected.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseMatrixError, "exactly two"):
                artifact_inventory(directory, manifest)

    def test_artifact_inventory_rejects_zip_link_member(self) -> None:
        manifest = load_manifest()
        with self._temporary_directory() as temporary:
            directory = Path(temporary)
            wheel, _ = self._write_valid_pair(directory)
            wheel.unlink()
            with zipfile.ZipFile(wheel, "w") as archive:
                link = zipfile.ZipInfo("comfyui_h3_context/link.py")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, "target.py")
            with self.assertRaisesRegex(ReleaseMatrixError, "unsafe member type"):
                artifact_inventory(directory, manifest)

    def test_artifact_inventory_rejects_tar_link_and_special_members(self) -> None:
        manifest = load_manifest()
        for member_type in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            with self.subTest(member_type=member_type), self._temporary_directory() as temporary:
                directory = Path(temporary)
                _, sdist = self._write_valid_pair(directory)
                sdist.unlink()
                with tarfile.open(sdist, "w:gz") as archive:
                    info = tarfile.TarInfo(f"{ARTIFACT_PREFIX}/comfyui_h3_context/unsafe")
                    info.type = member_type
                    info.linkname = "target" if member_type != tarfile.FIFOTYPE else ""
                    archive.addfile(info)
                with self.assertRaisesRegex(ReleaseMatrixError, "unsafe member type"):
                    artifact_inventory(directory, manifest)

    def test_artifact_inventory_rejects_ambiguous_or_unsafe_paths(self) -> None:
        manifest = load_manifest()
        path_sets = (
            ("../comfyui_h3_context/escape.py",),
            ("/comfyui_h3_context/absolute.py",),
            ("C:/comfyui_h3_context/drive.py",),
            ("comfyui_h3_context\\..\\escape.py",),
            (
                "comfyui_h3_context/Case.py",
                "comfyui_h3_context/case.py",
            ),
            (
                "comfyui_h3_context/" + unicodedata.normalize("NFC", "cafe\u0301.py"),
                "comfyui_h3_context/" + unicodedata.normalize("NFD", "café.py"),
            ),
        )
        for names in path_sets:
            with self.subTest(names=names), self._temporary_directory() as temporary:
                directory = Path(temporary)
                wheel, _ = self._write_valid_pair(directory)
                wheel.unlink()
                with zipfile.ZipFile(wheel, "w") as archive:
                    for name in names:
                        archive.writestr(name, "x = 1\n")
                with self.assertRaisesRegex(
                    ReleaseMatrixError,
                    "archive (traversal path|unsafe path|collision)",
                ):
                    artifact_inventory(directory, manifest)

    def test_artifact_inventory_rejects_lone_normalization_drift(self) -> None:
        manifest = load_manifest()
        with self._temporary_directory() as temporary:
            directory = Path(temporary)
            wheel, _ = self._write_valid_pair(directory)
            wheel.unlink()
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("comfyui_h3_context/cafe\u0301.py", "x = 1\n")
            with self.assertRaisesRegex(ReleaseMatrixError, "normalization|unsafe path"):
                artifact_inventory(directory, manifest)

    def test_zip_member_bound_is_checked_before_payload_read(self) -> None:
        class Info:
            external_attr = 0
            file_size = MAX_ARCHIVE_MEMBER_BYTES + 1
            filename = "comfyui_h3_context/oversized.py"

            @staticmethod
            def is_dir() -> bool:
                return False

        class Archive:
            def __enter__(self) -> Archive:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            @staticmethod
            def infolist() -> list[Info]:
                return [Info()]

            @staticmethod
            def read(_info: Info) -> bytes:
                raise AssertionError("PAYLOAD_READ_BEFORE_BOUND_CHECK")

        with self._temporary_directory() as temporary:
            artifact = Path(temporary) / "probe.whl"
            artifact.write_bytes(b"x")
            with (
                patch.object(zipfile, "ZipFile", return_value=Archive()),
                self.assertRaisesRegex(ReleaseMatrixError, "member size"),
            ):
                _inspect_archive(artifact, path_markers=(), suffixes=(), content_markers=())

    def test_tar_aggregate_bound_is_checked_before_payload_read(self) -> None:
        class Member:
            size = MAX_ARCHIVE_MEMBER_BYTES

            def __init__(self, index: int) -> None:
                self.name = f"comfyui_h3_context/member-{index}.py"

            @staticmethod
            def isdir() -> bool:
                return False

            @staticmethod
            def isreg() -> bool:
                return True

        class Archive:
            def __enter__(self) -> Archive:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            @staticmethod
            def getmembers() -> list[Member]:
                return [Member(index) for index in range(9)]

            @staticmethod
            def extractfile(_member: Member) -> None:
                raise AssertionError("PAYLOAD_READ_BEFORE_BOUND_CHECK")

        with self._temporary_directory() as temporary:
            artifact = Path(temporary) / "probe.tar.gz"
            artifact.write_bytes(b"x")
            with (
                patch.object(tarfile, "open", return_value=Archive()),
                self.assertRaisesRegex(ReleaseMatrixError, "expanded size"),
            ):
                _inspect_archive(artifact, path_markers=(), suffixes=(), content_markers=())

    def test_artifact_inventory_rejects_internal_markers_in_public_text(self) -> None:
        manifest = load_manifest()
        with self._temporary_directory() as temporary:
            directory = Path(temporary)
            wheel, _ = self._write_valid_pair(directory)
            wheel.unlink()
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "comfyui_h3_context/__init__.py",
                    f"__version__ = '{PROJECT_VERSION}'\n",
                )
                archive.writestr(
                    "comfyui_h3_context/contracts/public.json",
                    '{"evidence":"reference/ui/private.png"}\n',
                )
            with self.assertRaisesRegex(ReleaseMatrixError, "forbidden archive content"):
                artifact_inventory(directory, manifest)

    def test_manifest_validation_rejects_network_or_host_downgrade(self) -> None:
        source = json.loads(
            (ROOT / "tests" / "fixtures" / "m8_04_release_matrix.json").read_text(encoding="utf-8")
        )
        source["offline"]["pip_flags"] = ["--index-url", "https://example.invalid/simple"]
        with self._temporary_directory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ReleaseMatrixError, "offline pip flags"):
                load_manifest(path)

        source["offline"]["pip_flags"] = ["--no-index", "--no-deps", "--no-build-isolation"]
        source["host"]["required"] = False
        with self._temporary_directory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ReleaseMatrixError, "host lane"):
                load_manifest(path)

    def test_manifest_decoder_rejects_duplicate_nonfinite_unknown_and_resources(self) -> None:
        valid = (ROOT / "tests" / "fixtures" / "m8_04_release_matrix.json").read_bytes()
        text = valid.decode("utf-8")
        hostile = (
            text.replace(
                '"schema": "h3-context-release-matrix/1"',
                '"schema": "h3-context-release-matrix/1", "schema": "h3-context-release-matrix/1"',
                1,
            ).encode(),
            text.replace(
                '"name": "minimax-h3-studio"',
                '"name": "minimax-h3-studio", "name": "minimax-h3-studio"',
                1,
            ).encode(),
            text.replace('"package": {', '"unknown": NaN, "package": {', 1).encode(),
            text.replace('"package": {', '"unknown": Infinity, "package": {', 1).encode(),
            text.replace('"package": {', '"unknown": -Infinity, "package": {', 1).encode(),
            b"\xff",
            b"[" * 40 + b"0" + b"]" * 40,
            (b"[" + b"0," * release_matrix.MAX_RELEASE_MANIFEST_JSON_ITEMS + b"0]"),
            (
                '{"text":"' + "x" * (release_matrix.MAX_RELEASE_MANIFEST_TEXT_LENGTH + 1) + '"}'
            ).encode(),
            b" " * (release_matrix.MAX_RELEASE_MANIFEST_WIRE_BYTES + 1),
        )
        decoder = release_matrix.decode_release_manifest_json
        for payload in hostile:
            with self.subTest(payload=payload[:40]), self.assertRaises(ReleaseMatrixError):
                decoder(payload)

        for built_in in (str, bytes, bytearray):
            hostile_type = type(f"Hostile{built_in.__name__}", (built_in,), {})
            subclass_payload = hostile_type(text if built_in is str else valid)
            with self.subTest(subclass=built_in.__name__), self.assertRaises(ReleaseMatrixError):
                decoder(subclass_payload)

        for unsupported in (1, None, memoryview(valid)):
            with (
                self.subTest(type=type(unsupported).__name__),
                self.assertRaises(ReleaseMatrixError),
            ):
                decoder(unsupported)  # type: ignore[arg-type]

        unknowns = (
            text.replace('"package": {', '"unknown": true, "package": {', 1),
            text.replace('"artifact": {', '"artifact": {"unknown": true,', 1),
            text.replace('"offline": {', '"offline": {"unknown": true,', 1),
            text.replace('"contracts": {', '"contracts": {"unknown": true,', 1),
            text.replace('"host": {', '"host": {"unknown": true,', 1),
        )
        for unknown_text in unknowns:
            with self.subTest(section=unknown_text[:60]), self._temporary_directory() as temporary:
                path = Path(temporary) / "manifest.json"
                path.write_text(unknown_text, encoding="utf-8")
                with self.assertRaisesRegex(ReleaseMatrixError, "undeclared"):
                    load_manifest(path)


if __name__ == "__main__":
    unittest.main()
