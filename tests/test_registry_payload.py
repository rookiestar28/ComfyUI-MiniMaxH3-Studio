"""Actual Comfy Registry pack-archive and source-binding contracts."""

from __future__ import annotations

import hashlib
import importlib
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = "comfyui_h3_context/web/h3-context-sidebar.js"


class RegistryPayloadTests(unittest.TestCase):
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

    @staticmethod
    def _write_source(root: Path, relative: str, payload: bytes) -> None:
        target = root / Path(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    @classmethod
    def _baseline(cls, root: Path) -> dict[str, bytes]:
        payloads = {
            "README.md": b"Public package\n",
            "__init__.py": b"# synthetic product loader\n",
            "pyproject.toml": b'[tool.comfy]\nincludes = ["comfyui_h3_context"]\n',
            "comfyui_h3_context/__init__.py": b"# synthetic product package\n",
            "comfyui_h3_context/py.typed": b"",
            RUNTIME: b"export {};\n",
        }
        for relative, payload in payloads.items():
            cls._write_source(root, relative, payload)
        return payloads

    @staticmethod
    def _write_archive(
        archive_path: Path,
        entries: dict[str, bytes],
        *,
        infos: tuple[tuple[zipfile.ZipInfo, bytes], ...] = (),
        compression: int = zipfile.ZIP_STORED,
    ) -> None:
        with zipfile.ZipFile(archive_path, "w", compression=compression) as archive:
            for relative, payload in entries.items():
                archive.writestr(relative, payload)
            for info, payload in infos:
                archive.writestr(info, payload)

    @staticmethod
    def _audit(
        module: Any,
        root: Path,
        archive_path: Path,
        tracked: set[str],
    ) -> dict[str, Any]:
        with (
            patch.object(module, "ROOT", root),
            patch.object(
                module, "_git_tracked_paths", return_value=frozenset(tracked), create=True
            ),
        ):
            return cast(dict[str, Any], module.build_registry_payload_report(archive_path))

    def test_actual_archive_is_closed_hashed_and_bound_to_exact_sources(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payloads = self._baseline(root)
            archive_path = root / "node.zip"
            self._write_archive(archive_path, payloads, compression=zipfile.ZIP_DEFLATED)
            expected_archive_sha = "sha256:" + hashlib.sha256(archive_path.read_bytes()).hexdigest()
            expected_archive_size = archive_path.stat().st_size

            report = self._audit(module, root, archive_path, set(payloads))

        self.assertEqual(report["schema"], "h3-context-registry-payload/2")
        self.assertEqual(report["source"], "comfy-cli-pack-archive")
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["archive_sha256"], expected_archive_sha)
        self.assertEqual(report["archive_size"], expected_archive_size)
        self.assertEqual(report["file_count"], len(payloads))
        entries = report["entries"]
        paths = [entry["path"] for entry in entries]
        self.assertEqual(paths, sorted(payloads))
        self.assertEqual(paths.count(RUNTIME), 1)
        for entry in entries:
            self.assertEqual(entry["sha256"], entry["source_sha256"])
            self.assertGreaterEqual(entry["compressed_size"], 0)
            self.assertEqual(entry["size"], len(payloads[entry["path"]]))

    def test_missing_runtime_dependencies_are_rejected_even_when_present_bytes_match(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        for required in (
            "comfyui_h3_context/core/required.py",
            "comfyui_h3_context/contracts/required.json",
            "comfyui_h3_context/web/required.wasm",
        ):
            with self.subTest(required=required), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                payloads = self._baseline(root)
                self._write_source(root, required, b"{}\n")
                archive = root / "node.zip"
                self._write_archive(archive, payloads)
                with self.assertRaisesRegex(module.RegistryPayloadError, "missing required"):
                    self._audit(module, root, archive, {*payloads, required})

    def test_force_included_untracked_bytecode_is_rejected(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payloads = self._baseline(root)
            leaked = "comfyui_h3_context/__pycache__/leak.cpython-313.pyc"
            self._write_source(root, leaked, b"not public")
            archive_path = root / "node.zip"
            self._write_archive(archive_path, {**payloads, leaked: b"not public"})
            with self.assertRaisesRegex(module.RegistryPayloadError, "bytecode|cache"):
                self._audit(module, root, archive_path, set(payloads))

    def test_force_included_untracked_regular_file_is_rejected(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payloads = self._baseline(root)
            leaked = "comfyui_h3_context/local-note.txt"
            self._write_source(root, leaked, b"local only\n")
            archive_path = root / "node.zip"
            self._write_archive(archive_path, {**payloads, leaked: b"local only\n"})
            with self.assertRaisesRegex(module.RegistryPayloadError, "Git-tracked"):
                self._audit(module, root, archive_path, set(payloads))

    def test_archive_rejects_tracked_private_development_tree(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payloads = self._baseline(root)
            private = "tests/TEST_SOP.md"
            self._write_source(root, private, b"dist\n")
            archive_path = root / "node.zip"
            self._write_archive(archive_path, {**payloads, private: b"dist\n"})
            with self.assertRaisesRegex(module.RegistryPayloadError, "private path"):
                self._audit(module, root, archive_path, {*payloads, private})

    def test_archive_member_must_match_tracked_source_bytes(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payloads = self._baseline(root)
            archive_path = root / "node.zip"
            self._write_archive(archive_path, {**payloads, "README.md": b"stale archive\n"})
            with self.assertRaisesRegex(module.RegistryPayloadError, "source bytes"):
                self._audit(module, root, archive_path, set(payloads))

    def test_archive_rejects_noncanonical_or_escaping_names(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        hostile = (
            "../escape.py",
            "/absolute.py",
            "C:/drive.py",
            "./dot.py",
            "cafe\u0301.py",
            "folder/line\nbreak.py",
            "NUL.txt",
            "folder/trailing. ",
            "folder/" + "x" * 256,
        )
        for member in hostile:
            with self.subTest(member=member), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive_path = root / "node.zip"
                self._write_archive(archive_path, {member: b"x"})
                with self.assertRaisesRegex(module.RegistryPayloadError, "path|name"):
                    self._audit(module, root, archive_path, {member})
        normalized_by_zipfile = zipfile.ZipInfo("folder/backslash.py")
        normalized_by_zipfile.orig_filename = "folder\\backslash.py"
        with self.assertRaisesRegex(module.RegistryPayloadError, "path|name"):
            module._canonical_member_path(normalized_by_zipfile)

    def test_report_path_cannot_alias_and_overwrite_audited_archive(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payloads = self._baseline(root)
            archive_path = root / "node.zip"
            self._write_archive(archive_path, payloads)
            original = archive_path.read_bytes()
            with (
                patch.object(module, "ROOT", root),
                patch.object(
                    module,
                    "_git_tracked_paths",
                    return_value=frozenset(payloads),
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(
                    module.main(["--archive", str(archive_path), "--report", str(archive_path)]),
                    1,
                )
            self.assertEqual(archive_path.read_bytes(), original)

    def test_archive_rejects_normalized_case_and_duplicate_collisions(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        collisions = (
            {"tests/Name.py": b"a", "tests/name.py": b"b"},
            {"tests/same.py": b"last"},
        )
        for index, entries in enumerate(collisions):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive_path = root / "node.zip"
                if index == 0:
                    self._write_archive(archive_path, entries)
                else:
                    duplicate = zipfile.ZipInfo("tests/same.py")
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", UserWarning)
                        self._write_archive(
                            archive_path,
                            entries,
                            infos=((duplicate, b"again"),),
                        )
                with self.assertRaisesRegex(module.RegistryPayloadError, "collision|duplicate"):
                    self._audit(module, root, archive_path, set(entries))

    def test_archive_rejects_directory_symlink_and_unsupported_compression(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        directory = zipfile.ZipInfo("payload/")
        symlink = zipfile.ZipInfo("payload/link.py")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = (
            ("directory", ((directory, b""),), zipfile.ZIP_STORED),
            ("symlink", ((symlink, b"target.py"),), zipfile.ZIP_STORED),
            ("compression", (), zipfile.ZIP_BZIP2),
        )
        for label, infos, compression in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive_path = root / "node.zip"
                entries = {"payload/member.py": b"x"} if not infos else {}
                self._write_archive(archive_path, entries, infos=infos, compression=compression)
                with self.assertRaisesRegex(
                    module.RegistryPayloadError, "directory|regular|symlink|compression"
                ):
                    self._audit(module, root, archive_path, set(entries))
        encrypted = zipfile.ZipInfo("payload/encrypted.py")
        encrypted.flag_bits = 0x1
        with self.assertRaisesRegex(module.RegistryPayloadError, "encrypted"):
            module._validate_member_metadata(encrypted, encrypted.filename)

    def test_archive_enforces_file_member_total_and_archive_bounds(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        bounds = (
            ("MAX_FILES", 2, "count"),
            ("MAX_FILE_BYTES", 8, "member size"),
            ("MAX_TOTAL_BYTES", 10, "expanded size"),
            ("MAX_ARCHIVE_BYTES", 16, "archive size"),
        )
        for constant, value, message in bounds:
            with self.subTest(constant=constant), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                payloads = self._baseline(root)
                archive_path = root / "node.zip"
                self._write_archive(archive_path, payloads)
                with (
                    patch.object(module, constant, value, create=True),
                    self.assertRaisesRegex(module.RegistryPayloadError, message),
                ):
                    self._audit(module, root, archive_path, set(payloads))

    def test_archive_rejects_forbidden_text_and_corruption(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payloads = self._baseline(root)
            payloads["README.md"] = b"See .planning/private.md\n"
            self._write_source(root, "README.md", payloads["README.md"])
            archive_path = root / "node.zip"
            self._write_archive(archive_path, payloads)
            with self.assertRaisesRegex(module.RegistryPayloadError, "internal marker"):
                self._audit(module, root, archive_path, set(payloads))

            archive_path.write_bytes(b"not a zip")
            with self.assertRaisesRegex(module.RegistryPayloadError, "ZIP|archive"):
                self._audit(module, root, archive_path, set(payloads))

    def test_archive_wraps_malformed_deflate_as_typed_corruption(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payloads = self._baseline(root)
            archive_path = root / "node.zip"
            self._write_archive(archive_path, payloads, compression=zipfile.ZIP_DEFLATED)
            with zipfile.ZipFile(archive_path) as archive:
                first = archive.infolist()[0]
            raw = bytearray(archive_path.read_bytes())
            local_header = struct.unpack_from("<IHHHHHIIIHH", raw, first.header_offset)
            data_offset = first.header_offset + 30 + local_header[-2] + local_header[-1]
            raw[data_offset] = (raw[data_offset] & 0xF8) | 0x07
            archive_path.write_bytes(raw)

            with self.assertRaisesRegex(module.RegistryPayloadError, "corrupt"):
                self._audit(module, root, archive_path, set(payloads))

    def test_archive_wraps_invalid_utf8_filename_as_typed_corruption(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payloads = self._baseline(root)
            archive_path = root / "node.zip"
            self._write_archive(archive_path, payloads)
            raw = bytearray(archive_path.read_bytes())
            central = raw.index(b"PK\x01\x02")
            flags = struct.unpack_from("<H", raw, central + 8)[0]
            struct.pack_into("<H", raw, central + 8, flags | 0x0800)
            filename_length = struct.unpack_from("<H", raw, central + 28)[0]
            self.assertGreater(filename_length, 0)
            raw[central + 46] = 0xFF
            archive_path.write_bytes(raw)

            with self.assertRaisesRegex(module.RegistryPayloadError, "corrupt"):
                self._audit(module, root, archive_path, set(payloads))

    def test_archive_rejects_link_or_reparse_source_ancestor(self) -> None:
        module = importlib.import_module("scripts.registry_payload")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payloads = self._baseline(root)
            target_root = root / "target"
            target_root.mkdir()
            (root / "comfyui_h3_context").rename(target_root / "comfyui_h3_context")
            link = root / "comfyui_h3_context"
            self._make_directory_link(link, target_root / "comfyui_h3_context")
            archive_path = root / "node.zip"
            self._write_archive(archive_path, payloads)
            try:
                with self.assertRaisesRegex(module.RegistryPayloadError, "link|reparse"):
                    self._audit(module, root, archive_path, set(payloads))
            finally:
                link.unlink() if link.is_symlink() else link.rmdir()

    def test_preflight_disables_python_bytecode_before_project_imports(self) -> None:
        importlib.import_module("scripts.registry_payload")
        self.assertEqual(os.environ["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertTrue(sys.dont_write_bytecode)
        source = (ROOT / "scripts/registry_payload.py").read_text(encoding="utf-8")
        guard = source.index('os.environ["PYTHONDONTWRITEBYTECODE"] = "1"')
        project_import = source.index("from comfyui_h3_context.core.safe_paths import")
        self.assertLess(guard, project_import)


if __name__ == "__main__":
    unittest.main()
