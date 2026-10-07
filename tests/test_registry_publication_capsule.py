"""Publication capsule transfer and extraction safety contracts."""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.registry_publication_capsule import (
    PublicationCapsuleError,
    compute_capsule_sha256,
    create_capsule,
    unpack_capsule,
    verify_capsule_sha256,
)

CANDIDATE = "a" * 40


class RegistryPublicationCapsuleTests(unittest.TestCase):
    @staticmethod
    def _inputs(root: Path) -> tuple[Path, Path, Path]:
        environment = root / ".publish-venv"
        executable = environment / "bin" / "comfy"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        archive = root / "node.zip"
        archive.write_bytes(b"PK fixture")
        report = root / "registry-payload.json"
        report.write_text('{"status":"PASS"}\n', encoding="utf-8")
        return environment, archive, report

    @staticmethod
    def _write_minimal_capsule(
        capsule: Path,
        *,
        environment_name: str = ".publish-venv",
        environment_type: bytes = tarfile.DIRTYPE,
        archive_type: bytes = tarfile.REGTYPE,
        extra_environment_name: str | None = None,
    ) -> None:
        members = [
            (environment_name, environment_type, b"environment"),
            ("node.zip", archive_type, b"PK fixture"),
            ("registry-payload.json", tarfile.REGTYPE, b'{"status":"PASS"}\n'),
            ("publication-candidate.txt", tarfile.REGTYPE, (CANDIDATE + "\n").encode()),
        ]
        if extra_environment_name is not None:
            members.append((extra_environment_name, tarfile.DIRTYPE, b""))
        with tarfile.open(capsule, "w:gz") as stream:
            for name, member_type, payload in members:
                info = tarfile.TarInfo(name)
                info.type = member_type
                info.size = len(payload) if member_type == tarfile.REGTYPE else 0
                stream.addfile(info, io.BytesIO(payload) if info.size else None)

    def test_create_and_unpack_preserve_exact_bounded_subject(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, archive, report = self._inputs(root)
            capsule = root / "publication-capsule.tgz"

            created = create_capsule(
                capsule,
                environment=environment,
                archive=archive,
                report=report,
                candidate=CANDIDATE,
            )
            destination = root / "unpacked"
            destination.mkdir()
            unpacked = unpack_capsule(capsule, destination, expected_candidate=CANDIDATE)

            self.assertEqual(created["candidate_commit"], CANDIDATE)
            self.assertEqual(unpacked["candidate_commit"], CANDIDATE)
            self.assertEqual((destination / "node.zip").read_bytes(), archive.read_bytes())
            self.assertEqual(
                (destination / "registry-payload.json").read_bytes(), report.read_bytes()
            )
            self.assertEqual(
                (destination / ".publish-venv/bin/comfy").read_bytes(),
                (environment / "bin/comfy").read_bytes(),
            )

    def test_full_capsule_digest_rejects_any_transfer_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, archive, report = self._inputs(root)
            capsule = root / "publication-capsule.tgz"
            create_capsule(
                capsule,
                environment=environment,
                archive=archive,
                report=report,
                candidate=CANDIDATE,
            )
            digest = compute_capsule_sha256(capsule)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertEqual(verify_capsule_sha256(capsule, digest), digest)

            with capsule.open("ab") as stream:
                stream.write(b"tamper")
            with self.assertRaisesRegex(PublicationCapsuleError, "digest does not match"):
                verify_capsule_sha256(capsule, digest)
            with self.assertRaisesRegex(PublicationCapsuleError, "SHA-256"):
                verify_capsule_sha256(capsule, "not-a-digest")

    def test_unpack_rejects_traversal_symlink_unexpected_and_candidate_mismatch(self) -> None:
        cases = (
            ("../escape", tarfile.REGTYPE, b"escape"),
            (".publish-venv/bin/comfy", tarfile.SYMTYPE, b"target"),
            ("unexpected.txt", tarfile.REGTYPE, b"unexpected"),
            ("publication-candidate.txt", tarfile.REGTYPE, ("b" * 40 + "\n").encode()),
        )
        for name, member_type, payload in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                capsule = root / "publication-capsule.tgz"
                with tarfile.open(capsule, "w:gz") as stream:
                    info = tarfile.TarInfo(name)
                    info.type = member_type
                    info.size = len(payload) if member_type == tarfile.REGTYPE else 0
                    if member_type == tarfile.SYMTYPE:
                        info.linkname = payload.decode()
                    stream.addfile(info, io.BytesIO(payload) if info.size else None)
                destination = root / "unpacked"
                destination.mkdir()
                with self.assertRaises(PublicationCapsuleError):
                    unpack_capsule(capsule, destination, expected_candidate=CANDIDATE)
                self.assertFalse((root / "escape").exists())

    def test_unpack_rejects_noncanonical_and_wrong_required_member_types(self) -> None:
        cases = (
            {"extra_environment_name": ".publish-venv//bin"},
            {"environment_type": tarfile.REGTYPE},
            {"archive_type": tarfile.DIRTYPE},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                capsule = root / "publication-capsule.tgz"
                self._write_minimal_capsule(capsule, **overrides)
                destination = root / "unpacked"
                destination.mkdir()
                with self.assertRaises(PublicationCapsuleError):
                    unpack_capsule(capsule, destination, expected_candidate=CANDIDATE)

    def test_create_and_unpack_enforce_file_and_expanded_size_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, archive, report = self._inputs(root)
            capsule = root / "publication-capsule.tgz"
            with (
                patch("scripts.registry_publication_capsule.MAX_FILES", 2),
                self.assertRaisesRegex(PublicationCapsuleError, "file count"),
            ):
                create_capsule(
                    capsule,
                    environment=environment,
                    archive=archive,
                    report=report,
                    candidate=CANDIDATE,
                )

            create_capsule(
                capsule,
                environment=environment,
                archive=archive,
                report=report,
                candidate=CANDIDATE,
            )
            destination = root / "unpacked"
            destination.mkdir()
            with (
                patch("scripts.registry_publication_capsule.MAX_TOTAL_BYTES", 4),
                self.assertRaisesRegex(PublicationCapsuleError, "expanded size"),
            ):
                unpack_capsule(capsule, destination, expected_candidate=CANDIDATE)


if __name__ == "__main__":
    unittest.main()
