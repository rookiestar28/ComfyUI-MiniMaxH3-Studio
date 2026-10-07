"""HC-04 machine-readable build-stage evidence contracts."""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.build_gate_report import (
    BuildGateReportError,
    _purge_stale_build_state,
    _safe_extract_sdist,
    run_build_gate,
)

ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE_RUNTIME_PATHS = {
    f"comfyui_h3_context/core/{name}.py"
    for name in (
        "architecture_inventory",
        "closeout_matrix",
        "cross_language_surface",
        "m19_closeout",
        "node_surface",
        "public_surface",
        "retirement_eligibility",
    )
}


class SdistSourceRootTests(unittest.TestCase):
    @staticmethod
    def _archive(root: Path, filename: str, member: str, *, symlink: bool = False) -> Path:
        archive_path = root / filename
        payload = b"synthetic source\n"
        info = tarfile.TarInfo(member)
        if symlink:
            info.type = tarfile.SYMTYPE
            info.linkname = "../../foreign-sentinel"
        else:
            info.size = len(payload)
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.addfile(info, None if symlink else io.BytesIO(payload))
        return archive_path

    def test_current_distribution_stable_versions_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            root = Path(temporary)
            for version in ("1.0.2", "2.3.4"):
                with self.subTest(version=version):
                    source_name = f"minimax_h3_studio-{version}"
                    archive = self._archive(
                        root, f"{source_name}.tar.gz", f"{source_name}/source.txt"
                    )
                    destination = root / f"extract-{version}"
                    destination.mkdir()
                    source = _safe_extract_sdist(archive, destination)
                    self.assertEqual(source, destination / source_name)
                    self.assertEqual((source / "source.txt").read_bytes(), b"synthetic source\n")

    def test_retired_foreign_and_unstable_distribution_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            root = Path(temporary)
            for name in (
                "minimax_h3_context-1.0.2",
                "foreign_package-1.0.2",
                "minimax_h3_studio-1.0",
                "minimax_h3_studio-1.0.2rc1",
            ):
                with self.subTest(name=name):
                    archive = self._archive(root, f"{name}.tar.gz", f"{name}/source.txt")
                    destination = root / f"extract-{name}"
                    with self.assertRaisesRegex(BuildGateReportError, "unexpected source root"):
                        _safe_extract_sdist(archive, destination)
                    self.assertFalse(destination.exists())

    def test_filename_and_member_root_must_match(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            root = Path(temporary)
            archive = self._archive(
                root, "minimax_h3_studio-1.0.2.tar.gz", "minimax_h3_studio-1.0.0/source.txt"
            )
            with self.assertRaisesRegex(BuildGateReportError, "sdist root is unexpected"):
                _safe_extract_sdist(archive, root / "extract")
            self.assertFalse((root / "extract").exists())

    def test_unsafe_paths_and_links_are_rejected_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            root = Path(temporary)
            for member, symlink, reason in (
                ("minimax_h3_studio-1.0.2/../escape", False, "unsafe path"),
                ("minimax_h3_studio-1.0.2/link", True, "unsafe member type"),
            ):
                with self.subTest(member=member):
                    archive = self._archive(
                        root, "minimax_h3_studio-1.0.2.tar.gz", member, symlink=symlink
                    )
                    with self.assertRaisesRegex(BuildGateReportError, reason):
                        _safe_extract_sdist(archive, root / "extract")
                    self.assertFalse((root / "extract").exists())


class BuildGateReportTests(unittest.TestCase):
    @staticmethod
    def _passing_stage(stage_id: str, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "id": stage_id,
            "status": "PASS",
            "exit_code": 0,
            "failure_kind": None,
            "started_at": "2026-08-07T00:00:00+00:00",
            "ended_at": "2026-08-07T00:00:01+00:00",
            "duration_seconds": 1.0,
        }

    def test_real_build_emits_stage_artifact_and_wheel_comparison_evidence(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="hc04-build-report-", dir=ROOT / ".tmp"
        ) as temporary:
            root = Path(temporary)
            report_path = root / "report.json"
            report = run_build_gate(root / "artifacts", report_path=report_path)

            self.assertEqual(report["schema"], "h3-context-build-gate/1")
            self.assertEqual(report["status"], "PASS")
            self.assertIsNone(report["first_failure"])
            self.assertEqual(
                [stage["id"] for stage in report["stages"]],
                ["sdist", "direct_wheel", "wheel_from_sdist"],
            )
            self.assertTrue(all(stage["status"] == "PASS" for stage in report["stages"]))
            self.assertTrue(all(stage["duration_seconds"] >= 0 for stage in report["stages"]))
            self.assertEqual(len(report["artifacts"]), 3)
            self.assertTrue(all(len(artifact["sha256"]) == 64 for artifact in report["artifacts"]))
            self.assertEqual(
                report["wheel_comparison"],
                {
                    "content_files_equal": True,
                    "metadata_equal": True,
                    "only_in_direct": [],
                    "only_in_from_sdist": [],
                    "content_mismatches": [],
                    "metadata_mismatches": [],
                },
            )
            self.assertTrue(report["cleanup_verified"])
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)

            sdist = root / "artifacts" / "sdist" / str(report["artifacts"][0]["filename"])
            with tarfile.open(sdist, "r:gz") as archive:
                sdist_names = {member.name.replace("\\", "/") for member in archive.getmembers()}
                sdist_root = str(report["artifacts"][0]["filename"]).removesuffix(".tar.gz")
                self.assertIn(f"{sdist_root}/LICENSE", sdist_names)
                self.assertIn(f"{sdist_root}/NOTICE", sdist_names)
            for artifact in report["artifacts"]:
                if artifact["kind"] not in {"direct_wheel", "wheel_from_sdist"}:
                    continue
                wheel = (
                    root
                    / "artifacts"
                    / ("direct-wheel" if artifact["kind"] == "direct_wheel" else "wheel-from-sdist")
                    / str(artifact["filename"])
                )
                with zipfile.ZipFile(wheel) as archive:
                    names = set(archive.namelist())
                self.assertTrue(GOVERNANCE_RUNTIME_PATHS.isdisjoint(names))
                self.assertTrue(any(name.endswith(".dist-info/licenses/LICENSE") for name in names))
                self.assertTrue(any(name.endswith(".dist-info/licenses/NOTICE") for name in names))

                # A deleted contract must actually stop shipping. `build_py` stages into `build/lib`
                # incrementally and never removes a staged file whose source is gone, so before the
                # purge a wheel built from this tree kept shipping contracts the repository no
                # longer had -- and it looked like an ordinary sdist/wheel mismatch. Comparing the
                # wheel against the source directory names the real property directly.
                packaged = {
                    name.rsplit("/", 1)[-1]
                    for name in names
                    if "/contracts/" in name and name.endswith(".json")
                }
                on_disk = {
                    path.name for path in (ROOT / "comfyui_h3_context" / "contracts").glob("*.json")
                }
                self.assertEqual(
                    sorted(packaged - on_disk),
                    [],
                    f"{artifact['kind']} ships a contract the repository no longer contains",
                )
                self.assertEqual(
                    sorted(on_disk - packaged),
                    [],
                    f"{artifact['kind']} omits a contract the repository contains",
                )

    def test_the_purge_removes_setuptools_staging_and_nothing_else(self) -> None:
        """It must clear the two directories setuptools owns, and refuse to wander.

        Run against a throwaway root rather than the repository, so the test can assert the
        deletion happened without deleting anything a developer is using.
        """

        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            fake_root = Path(temporary)
            staged = fake_root / "build" / "lib" / "pkg" / "contracts"
            staged.mkdir(parents=True)
            (staged / "retired_v1.schema.json").write_text("{}", encoding="utf-8")
            (fake_root / "pkg.egg-info").mkdir()
            keep_dir = fake_root / "comfyui_h3_context"
            keep_dir.mkdir()
            keep_file = fake_root / "build-notes.md"
            keep_file.write_text("not a directory", encoding="utf-8")

            with patch("scripts.build_gate_report.ROOT", fake_root):
                removed = _purge_stale_build_state()

            self.assertEqual(sorted(removed), ["build", "pkg.egg-info"])
            self.assertFalse((fake_root / "build").exists())
            self.assertFalse((fake_root / "pkg.egg-info").exists())
            self.assertTrue(keep_dir.is_dir())
            self.assertTrue(keep_file.is_file())

            # Idempotent: nothing left to remove, and no error for the absent directories.
            with patch("scripts.build_gate_report.ROOT", fake_root):
                self.assertEqual(_purge_stale_build_state(), [])

    def test_missing_artifact_still_emits_typed_first_failure_report(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            root = Path(temporary)
            report_path = root / "report.json"
            with (
                patch(
                    "scripts.build_gate_report._run_stage",
                    side_effect=self._passing_stage,
                ),
                # These three exercise failure paths with no real build, so the purge has nothing
                # to do -- and must not reach the working tree's own staging directories.
                patch(
                    "scripts.build_gate_report._purge_stale_build_state",
                    return_value=[],
                ),
                self.assertRaises(BuildGateReportError),
            ):
                run_build_gate(root / "artifacts", report_path=report_path)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "FAIL")
            self.assertEqual(
                report["first_failure"],
                {"stage": "artifact_discovery", "kind": "artifact_error"},
            )
            self.assertTrue(report["cleanup_verified"])

    def test_extraction_failure_still_emits_typed_first_failure_report(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            root = Path(temporary)
            report_path = root / "report.json"
            with (
                patch(
                    "scripts.build_gate_report._run_stage",
                    side_effect=self._passing_stage,
                ),
                # These three exercise failure paths with no real build, so the purge has nothing
                # to do -- and must not reach the working tree's own staging directories.
                patch(
                    "scripts.build_gate_report._purge_stale_build_state",
                    return_value=[],
                ),
                patch(
                    "scripts.build_gate_report._single_artifact",
                    return_value=root / "fake.tar.gz",
                ),
                patch(
                    "scripts.build_gate_report._safe_extract_sdist",
                    side_effect=BuildGateReportError("invalid sdist"),
                ),
                self.assertRaises(BuildGateReportError),
            ):
                run_build_gate(root / "artifacts", report_path=report_path)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                report["first_failure"],
                {"stage": "sdist_extraction", "kind": "artifact_error"},
            )
            self.assertTrue(report["cleanup_verified"])

    def test_cleanup_failure_is_reported_without_losing_the_first_failure(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        equal = {
            "content_files_equal": True,
            "metadata_equal": True,
            "only_in_direct": [],
            "only_in_from_sdist": [],
            "content_mismatches": [],
            "metadata_mismatches": [],
        }
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            root = Path(temporary)
            report_path = root / "report.json"
            with (
                patch(
                    "scripts.build_gate_report._run_stage",
                    side_effect=self._passing_stage,
                ),
                # These three exercise failure paths with no real build, so the purge has nothing
                # to do -- and must not reach the working tree's own staging directories.
                patch(
                    "scripts.build_gate_report._purge_stale_build_state",
                    return_value=[],
                ),
                patch(
                    "scripts.build_gate_report._single_artifact",
                    side_effect=[
                        root / "fake.tar.gz",
                        root / "direct.whl",
                        root / "from-sdist.whl",
                    ],
                ),
                patch("scripts.build_gate_report._safe_extract_sdist", return_value=ROOT),
                patch("scripts.build_gate_report.compare_wheels", return_value=equal),
                patch(
                    "scripts.build_gate_report._artifact_record",
                    return_value={
                        "kind": "test",
                        "filename": "test",
                        "bytes": 1,
                        "sha256": "0" * 64,
                    },
                ),
                patch("scripts.build_gate_report.shutil.rmtree", side_effect=OSError("locked")),
                self.assertRaises(BuildGateReportError),
            ):
                run_build_gate(root / "artifacts", report_path=report_path)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                report["first_failure"],
                {"stage": "cleanup", "kind": "cleanup_error"},
            )
            self.assertEqual(
                report["cleanup_failure"],
                {"stage": "cleanup", "kind": "cleanup_error"},
            )
            self.assertFalse(report["cleanup_verified"])


if __name__ == "__main__":
    unittest.main()
