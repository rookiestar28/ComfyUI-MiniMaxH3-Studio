"""Generate and verify the frozen fidelity scorecard fixture and schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from comfyui_h3_context.core.fidelity_scorecard import build_fidelity_scorecard

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m14_08_fidelity_scorecard.json"
SCHEMA = ROOT / "governance" / "contracts" / "fidelity_scorecard_v1.schema.json"


def _render(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _schema(wire: dict[str, object]) -> dict[str, object]:
    # The portable record has no JSON-number leaves, avoiding integer/float const equivalence.
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.invalid/h3/fidelity_scorecard_v1.schema.json",
        "title": "H3 Frozen Fidelity Scorecard",
        "description": (
            "Closed manual-only scorecard with conditional thresholds and no imputed evidence."
        ),
        "type": "object",
        "const": wire,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    wire = build_fidelity_scorecard().to_wire()
    expected = {
        FIXTURE: _render(wire),
        SCHEMA: _render(_schema(wire)),
    }
    if args.check:
        drift = [path for path, content in expected.items() if path.read_text("utf-8") != content]
        if drift:
            print("fidelity scorecard assets drifted: " + ", ".join(str(path) for path in drift))
            return 1
        print("fidelity scorecard assets: PASS")
        return 0
    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    print("fidelity scorecard assets: written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
