"""M23-48: every packaged artifact has an exact disposition, decided by who reads it.

The governance-only set can only be moved once it is known which files are not governance-only, and
the question has one specific way of going wrong. `host_seam_census_v1.json` reads like a governance
record -- a census, hand-maintained, no generator, no `--write` -- and a module under
`frontend/src/host/` imports it, so the bundle compiles it in. "Generated versus hand-maintained"
misclassifies it, "record versus contract" misclassifies it, and so does any rule about its name.
M23-47 spent a gate run finding that out.

So these tests assert the property that survives a refactor: the class comes from the readers, the
coverage is total, and no file is left without a disposition.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from scripts import packaged_artifact_readership as readership

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / readership.ARTIFACT_PATH

#: Measured in M23-48's reconnaissance and reproduced by the scan. Pinned because these are the
#: files a move must not touch, and a silent change to either set is the failure that matters.
FRONTEND_SOURCE_READ = frozenset(
    {
        "comfyui_h3_context/contracts/cross_language_surface_v1.json",
        "comfyui_h3_context/contracts/host_seam_census_v1.json",
        "comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json",
        "comfyui_h3_context/contracts/official_h3_assets_v2.json",
    }
)
RUNTIME_READ = frozenset(
    {
        "comfyui_h3_context/contracts/authoring_renderer_qualification_v1.json",
        "comfyui_h3_context/contracts/build_provenance_v1.json",
        "comfyui_h3_context/contracts/contract_inventory_v1.json",
        "comfyui_h3_context/contracts/installation_profiles_v1.json",
        "comfyui_h3_context/contracts/prompt_model_profiles_v5.json",
        "comfyui_h3_context/contracts/prompt_model_profiles_v6.json",
        "comfyui_h3_context/contracts/semantic_provider_profiles_v1.json",
        "comfyui_h3_context/contracts/semantic_provider_profiles_v2.json",
    }
)


def _declared_font_artifacts() -> frozenset[str]:
    manifest_path = REPO_ROOT / readership.FONT_MANIFEST_PATH
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    face_paths = {
        f"{readership.PACKAGE}/{face['package_path']}"
        for asset in manifest["font_assets"]
        for face in asset["faces"]
    }
    return frozenset(
        {
            readership.FONT_MANIFEST_PATH,
            f"{readership.PACKAGE}/{manifest['license']['package_path']}",
            *face_paths,
        }
    )


def _record() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    return loaded


class TheCoverageIsTotalTests(unittest.TestCase):
    """AC 4: no omitted row and no unknown disposition."""

    def test_every_packaged_artifact_has_a_row(self) -> None:
        on_disk = set(readership.packaged_artifacts())
        fresh = cast(dict[str, Any], readership.build_record())
        rows = {row["path"] for row in fresh["artifacts"]}
        self.assertEqual(rows, on_disk)

    def test_private_schemas_are_included_rather_than_skipped(self) -> None:
        # The scan takes every `*.json`, and a `.schema.json` is not a different kind of file to
        # it. Until M23-55 this was asserted by counting the sixty-odd packaged schemas; all of
        # them turned out to have no reader inside the shipped product and moved to
        # `governance/contracts/`, so the packaged root now holds none and a count would assert
        # that the relocation had failed. The discriminating fact is proven directly instead.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example_v1.json").write_text("{}", encoding="utf-8")
            (root / "example_v1.schema.json").write_text("{}", encoding="utf-8")
            (root / "notes.md").write_text("not json", encoding="utf-8")
            with patch.object(readership, "ROOT", root), patch.object(readership, "CONTRACTS", "."):
                found = {Path(path).name for path in readership._contract_artifacts()}  # noqa: SLF001
        self.assertEqual(found, {"example_v1.json", "example_v1.schema.json"})

    def test_manifest_declares_exactly_the_shipped_font_artifacts(self) -> None:
        declared = _declared_font_artifacts()
        packaged_fonts = {
            path
            for path in readership.packaged_artifacts()
            if path.startswith(f"{readership.FONTS}/")
        }
        self.assertEqual(
            packaged_fonts,
            set(declared),
        )
        self.assertEqual(sum(path.endswith(".ttf") for path in declared), 4)
        self.assertEqual(sum(path.endswith(".txt") for path in declared), 1)
        self.assertIn(readership.FONT_MANIFEST_PATH, declared)

    def test_every_row_carries_a_known_disposition(self) -> None:
        for row in _record()["artifacts"]:
            with self.subTest(path=row["path"]):
                self.assertIn(row["readership"], readership.DISPOSITIONS)
                self.assertEqual(row["disposition"], readership.DISPOSITIONS[row["readership"]])

    def test_the_counts_add_up(self) -> None:
        record = _record()
        counts = record["counts"]
        assert isinstance(counts, dict)
        self.assertEqual(sum(counts.values()), record["artifact_count"])
        self.assertEqual(record["artifact_count"], len(record["artifacts"]))


class TheClassComesFromTheReadersTests(unittest.TestCase):
    def test_the_bundle_inputs_are_exactly_the_measured_four(self) -> None:
        found = {
            row["path"]
            for row in _record()["artifacts"]
            if row["readership"] == "frontend_source_read"
        }
        self.assertEqual(found, set(FRONTEND_SOURCE_READ))

    def test_the_runtime_reads_match_the_measured_connection_and_legacy_catalogs(self) -> None:
        found = {
            row["path"]
            for row in _record()["artifacts"]
            if row["readership"] == "runtime_read"
            and row["path"].startswith(f"{readership.CONTRACTS}/")
        }
        self.assertEqual(found, set(RUNTIME_READ))

    def test_manifest_selected_fonts_are_runtime_reads_of_the_real_loader(self) -> None:
        found = {
            row.path: row
            for row in readership.scan()
            if row.path.startswith(f"{readership.FONTS}/")
        }
        self.assertEqual(set(found), set(_declared_font_artifacts()))
        for path, row in found.items():
            with self.subTest(path=path):
                self.assertEqual(row.readership, "runtime_read")
                self.assertEqual(row.readers, (readership.FONT_RUNTIME_READER,))

    def test_text_that_names_dynamic_reads_is_not_reader_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reader = root / readership.FONT_RUNTIME_READER
            reader.parent.mkdir(parents=True)
            reader.write_text(
                '"""Mentions _safe_package_file, _read_bounded, and package_path only."""\n',
                encoding="utf-8",
            )
            with patch.object(readership, "ROOT", root):
                with self.assertRaises(readership.ReadershipError):
                    readership._font_runtime_reader()  # noqa: SLF001

    def test_missing_and_invented_font_artifacts_fail_closed(self) -> None:
        def write_fixture(root: Path, *, omit: str | None = None, extra: bool = False) -> None:
            fonts = root / readership.FONTS
            fonts.mkdir(parents=True)
            face_paths = [f"fonts/face-{index}.ttf" for index in range(4)]
            manifest = {
                "font_assets": [{"faces": [{"package_path": path} for path in face_paths]}],
                "license": {"package_path": "fonts/license.txt"},
            }
            (root / readership.FONT_MANIFEST_PATH).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            for package_path in (*face_paths, "fonts/license.txt"):
                if package_path != omit:
                    (root / readership.PACKAGE / package_path).write_bytes(b"fixture")
            if extra:
                (fonts / "undeclared.ttf").write_bytes(b"fixture")

        for label, omitted, extra in (
            ("missing", "fonts/face-3.ttf", False),
            ("invented", None, True),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_fixture(root, omit=omitted, extra=extra)
                with patch.object(readership, "ROOT", root):
                    with self.assertRaises(readership.ReadershipError):
                        readership.packaged_artifacts()

    def test_manifest_paths_cannot_escape_the_packaged_font_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fonts = root / readership.FONTS
            fonts.mkdir(parents=True)
            manifest = {
                "font_assets": [
                    {
                        "faces": [
                            {"package_path": "fonts/a.ttf"},
                            {"package_path": "fonts/b.ttf"},
                            {"package_path": "fonts/c.ttf"},
                            {"package_path": "fonts/../outside.ttf"},
                        ]
                    }
                ],
                "license": {"package_path": "fonts/license.txt"},
            }
            (root / readership.FONT_MANIFEST_PATH).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with patch.object(readership, "ROOT", root):
                with self.assertRaises(readership.ReadershipError):
                    readership.packaged_artifacts()

    def test_the_census_is_not_classified_as_movable(self) -> None:
        # CRITICAL: this is the whole point. Every heuristic about the file's kind puts the census
        # in the governance set; only its readers put it where it belongs. A change that makes this
        # row movable would, at the next governance move, silently change the shipped bundle.
        row = next(
            row
            for row in _record()["artifacts"]
            if row["path"].endswith("host_seam_census_v1.json")
        )
        self.assertEqual(row["readership"], "frontend_source_read")
        self.assertTrue(any(r.startswith("frontend/src/") for r in row["readers"]))

    def test_no_retained_class_appears_in_the_movable_set(self) -> None:
        record = _record()
        movable = set(record["movable"])
        self.assertFalse(movable & FRONTEND_SOURCE_READ)
        self.assertFalse(movable & RUNTIME_READ)

    def test_a_file_nothing_reads_is_referred_rather_than_moved(self) -> None:
        # Neither "retain" nor "move" is honest for a file with no reader, so these get their own
        # class and a disposition that names the referral instead of being swept into the move.
        #
        # The rule is asserted rather than an instance of it. M23-55 relocated the three files that
        # carried this class at the time, so the packaged directory now holds none -- and a test
        # that required one would have turned red for the relocation succeeding.
        self.assertIn("retirement", readership.DISPOSITIONS["no_reader"])
        self.assertNotEqual(
            readership.DISPOSITIONS["no_reader"],
            readership.DISPOSITIONS["tooling_or_evidence_only"],
        )
        for row in _record()["artifacts"]:
            if row["readership"] != "no_reader":
                continue
            with self.subTest(path=row["path"]):
                self.assertEqual(row["readers"], [])
                self.assertIn("retirement", row["disposition"])
                self.assertNotIn(row["path"], _record()["movable"])


class ProseIsNotAReaderTests(unittest.TestCase):
    """A document that names an artifact does not open it.

    AGENTS.md section 5.1 requires that a prose document stay rewritable without a test turning
    red. Before prose suffixes were excluded, adding two artifact names to a paragraph of
    `tests/TEST_SOP.md` made this record stale -- so a documentation edit failed
    `test_the_stored_record_matches_a_fresh_scan`, which is exactly the coupling that rule forbids.
    """

    def test_no_row_credits_a_prose_document_as_a_reader(self) -> None:
        fresh = cast(dict[str, Any], readership.build_record())
        for row in fresh["artifacts"]:
            for reader in row["readers"]:
                with self.subTest(path=row["path"], reader=reader):
                    self.assertFalse(reader.endswith(readership.PROSE_SUFFIXES))

    def test_a_prose_reader_is_classified_as_nothing(self) -> None:
        for name in ("docs/anything.md", "tests/TEST_SOP.md", "NOTICE.txt", "reference/a.rst"):
            with self.subTest(reader=name):
                self.assertIsNone(readership._classify(name))  # noqa: SLF001

    def test_the_same_path_would_count_were_it_not_prose(self) -> None:
        # CRITICAL: without this the test above passes vacuously for the wrong reason -- a path
        # that no rule matches also returns None. The prose suffix has to be the deciding fact.
        self.assertEqual(readership._classify("tests/TEST_SOP.py"), "tooling_or_evidence_only")  # noqa: SLF001
        self.assertEqual(readership._classify("frontend/src/a.md"), None)  # noqa: SLF001
        self.assertEqual(readership._classify("frontend/src/a.ts"), "frontend_source_read")  # noqa: SLF001


class TheArtifactIsRegeneratedNotEditedTests(unittest.TestCase):
    def test_the_stored_record_matches_a_fresh_scan(self) -> None:
        self.assertEqual(_record(), readership.build_record())

    def test_the_scan_is_deterministic(self) -> None:
        self.assertEqual(readership.build_record(), readership.build_record())

    def test_the_record_names_no_absolute_path(self) -> None:
        text = ARTIFACT.read_text(encoding="utf-8")
        for marker in ("C:\\", "B:\\", "/Users/", "/home/", "AppData"):
            self.assertNotIn(marker, text)

    def test_the_record_is_no_longer_packaged_and_so_does_not_classify_itself(self) -> None:
        # Until M23-55 it was a packaged JSON like any other and had to appear in its own rows.
        # It has no reader inside the shipped product, so it moved out with the governance set, and
        # coverage of the packaged directory -- the property that mattered -- is unaffected:
        # `test_every_packaged_json_has_a_row` still compares the rows against that directory.
        #
        # CRITICAL: the classified directory and the output path are now deliberately different
        # constants. Collapsing them back would make the census classify its own output, which is
        # the shape `SELF_DESCRIBING_PATHS` exists to prevent everywhere else.
        self.assertFalse(readership.ARTIFACT_PATH.as_posix().startswith(f"{readership.CONTRACTS}/"))
        rows = {row["path"] for row in _record()["artifacts"]}
        self.assertNotIn(readership.ARTIFACT_PATH.as_posix(), rows)
        for row in rows:
            with self.subTest(path=row):
                self.assertTrue(
                    row.startswith(f"{readership.CONTRACTS}/")
                    or row.startswith(f"{readership.FONTS}/")
                )


if __name__ == "__main__":
    unittest.main()
