"""M25-16: the scene-resolver parity fixture must equal the backend authority.

The browser composition monitor resolves frames locally with a TypeScript port of
``resolve_composition``.  ``scripts/m25_16_scene_parity.py`` derives the shared vectors from the
backend; this test fails when the committed fixture no longer matches what the backend resolves,
so a backend change cannot leave the frontend replaying stale expectations.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "m25_16_scene_parity.py"
FIXTURE = ROOT / "tests" / "fixtures" / "m25_16_scene_resolver_parity_v1.json"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m25_16_scene_parity", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SceneResolverParityFixtureTest(unittest.TestCase):
    def test_fixture_matches_backend_resolution(self) -> None:
        module = _load_script()
        self.assertEqual(FIXTURE.read_text(encoding="utf-8"), module.render())

    def test_fixture_covers_boundary_and_refusal_cases(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "h3.context.m25_16_scene_resolver_parity.v1")
        names = {case["name"] for case in payload["cases"]}
        self.assertIn("primary_split_two_owners", names)
        self.assertIn("primary_audio_unavailable", names)
        for case in payload["cases"]:
            refusals = {row["frame"]: row["refusal"] for row in case["frames"] if "refusal" in row}
            self.assertEqual(refusals[-1], "source_range_unavailable", case["name"])
            duration = case["snapshot"]["output"]["duration_frames"]
            self.assertEqual(refusals[duration], "source_range_unavailable", case["name"])
            scenes = [row["scene"] for row in case["frames"] if "scene" in row]
            self.assertTrue(scenes, case["name"])
            for scene in scenes:
                self.assertEqual(
                    scene["public_fingerprint"], case["snapshot"]["public_fingerprint"]
                )


if __name__ == "__main__":
    unittest.main()
