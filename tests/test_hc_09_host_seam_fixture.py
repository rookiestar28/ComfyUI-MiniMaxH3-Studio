from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.hc_09_host_seam_fixture import (
    HostSeamFixtureError,
    check_observed_fixture,
    deterministic_fixture_bytes,
    normalized_fixture_wire,
)

ROOT = Path(__file__).resolve().parents[1]
CENSUS = ROOT / "comfyui_h3_context" / "contracts" / "host_seam_census_v1.json"
FIXTURE = ROOT / "comfyui_h3_context" / "contracts" / "host_seam_shape_fixture_v1.json"


class HostSeamFixtureNormalizerTests(unittest.TestCase):
    def test_tracked_fixture_regenerates_byte_identically(self) -> None:
        census = json.loads(CENSUS.read_text(encoding="utf-8"))
        observed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        normalized = normalized_fixture_wire(census, observed)
        self.assertEqual(deterministic_fixture_bytes(normalized), FIXTURE.read_bytes())
        summary = check_observed_fixture(CENSUS, FIXTURE, FIXTURE)
        self.assertEqual(summary, {"status": "PASS", "seams": 29, "bytes": 7793})

    def test_corrupted_live_shape_reports_drift(self) -> None:
        observed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        corrupted = copy.deepcopy(observed)
        corrupted["observations"][0]["kind"] = "object"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observed.json"
            path.write_text(json.dumps(corrupted), encoding="utf-8")
            with self.assertRaisesRegex(HostSeamFixtureError, "DRIFTED"):
                check_observed_fixture(CENSUS, path, FIXTURE)

    def test_live_evidence_wrapper_is_normalized_without_retaining_other_members(self) -> None:
        observed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "h3.context.host_seam_live_evidence.v1",
                        "observed_fixture": observed,
                        "private_unread_member": "not copied",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(check_observed_fixture(CENSUS, path, FIXTURE)["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
