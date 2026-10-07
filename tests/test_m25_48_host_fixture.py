from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from scripts import m25_48_host_fixture as fixture


class M2548HostFixtureTests(unittest.TestCase):
    def test_prepare_and_cleanup_are_explicit_exact_and_path_private(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "output"
            root.mkdir()
            prepared = fixture.prepare(root, "proof-one")
            locator = cast(dict[str, str], prepared["locator"])
            self.assertEqual(
                locator,
                {
                    "filename": "h3-m25-48-proof-one.mp4",
                    "subfolder": "h3-context-m25-48-fixtures",
                    "type": "output",
                },
            )
            self.assertEqual(prepared["bytes"], 5_749)
            self.assertEqual(prepared["sha256"], fixture.FIXTURE_SHA256)
            self.assertNotIn(str(root), json.dumps(prepared))
            target = root / locator["subfolder"] / locator["filename"]
            self.assertTrue(target.is_file())

            with self.assertRaisesRegex(fixture.FixtureError, "already exists"):
                fixture.prepare(root, "proof-one")

            cleaned = fixture.cleanup(root, json.dumps(locator))
            self.assertEqual(cleaned, {"status": "removed", "sha256": fixture.FIXTURE_SHA256})
            self.assertFalse(target.exists())

    def test_cleanup_refuses_traversal_and_changed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "output"
            root.mkdir()
            prepared = fixture.prepare(root, "proof-two")
            locator = cast(dict[str, str], prepared["locator"])
            target = root / locator["subfolder"] / locator["filename"]
            target.write_bytes(b"changed")
            with self.assertRaisesRegex(fixture.FixtureError, "digest mismatch"):
                fixture.cleanup(root, json.dumps(locator))
            with self.assertRaisesRegex(fixture.FixtureError, "locator"):
                fixture.cleanup(
                    root,
                    json.dumps(
                        {
                            "filename": "outside.mp4",
                            "subfolder": "..",
                            "type": "output",
                        }
                    ),
                )


if __name__ == "__main__":
    unittest.main()
