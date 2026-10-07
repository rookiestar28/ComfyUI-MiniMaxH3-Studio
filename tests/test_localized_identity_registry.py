"""The generated browser identity inventories stay bound to the Python enum authorities."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "localized_identity_registry.py"
GENERATED = ROOT / "frontend" / "src" / "i18n" / "generatedBackendIdentities.ts"


class LocalizedIdentityRegistryTests(unittest.TestCase):
    def test_generator_check_passes_for_the_committed_browser_inventory(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--check"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = GENERATED.read_text(encoding="utf-8")
        self.assertIn("promptFidelityDiagnosticIds", text)
        self.assertIn("promptModelOutcomeIds", text)
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)


if __name__ == "__main__":
    unittest.main()
