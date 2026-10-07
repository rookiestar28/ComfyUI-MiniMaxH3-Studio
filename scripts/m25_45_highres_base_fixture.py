"""Generate the 1280 x 720 semantic corpus base from the accepted 320 x 180 one.

M25-45 AC45-05 adds rendering rows whose composition is the shipped preview's own cap, 1280 x 720,
with sources that carry real detail at that size rather than a 128-pixel picture enlarged to fill
it. The composition itself is the accepted base at exactly four times the size -- 320 x 180 times
four is 1280 x 720 -- so this generator derives it rather than restating it: every relative
quantity (basis-point transforms, opacities, blends, timing) is copied unchanged, and the few
absolute pixel quantities are multiplied by the same four.

GUARD: derive, never hand-edit. The base's public fingerprint covers every field, so a hand-edited
copy is a composition nobody computed, and the three stages that read landmarks back out of a
rendered artifact would be judging one picture against another picture's expectations. Run this
again after any change to the accepted base.

Usage:
    python scripts/m25_45_highres_base_fixture.py [--check]

`--check` recomputes and compares without writing, which is what the test suite calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.composition_contract import (  # noqa: E402
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.semantic_conformance_media import (  # noqa: E402
    HIGH_RES_IMAGE_ASSET,
    HIGH_RES_OVERLAY_ASSET,
    HIGH_RES_PRIMARY_ASSET,
    HIGH_RES_SCALE,
    IMAGE_ASSET,
    OVERLAY_ASSET,
    PRIMARY_ASSET,
)

BASE_PATH = ROOT / "tests" / "fixtures" / "m25_20_semantic_corpus_base_v1.json"
TARGET_PATH = ROOT / "tests" / "fixtures" / "m25_45_semantic_highres_base_v1.json"

#: The high-resolution composition's own identity. The workspace handle and revisions differ from
#: the accepted base so that no stage can mistake one composition's history for the other's.
WORKSPACE_HANDLE = "authoring-" + "45" * 16
PROJECT_ID = "project-fixture-highres"

#: Which asset each source becomes. The timing source has no high-resolution row -- unequal
#: intervals are a property of the file's timing, which the accepted rows already prove -- so it is
#: dropped rather than scaled, and its clip does not exist in this base.
ASSET_RENAMES = {
    PRIMARY_ASSET: HIGH_RES_PRIMARY_ASSET,
    OVERLAY_ASSET: HIGH_RES_OVERLAY_ASSET,
    IMAGE_ASSET: HIGH_RES_IMAGE_ASSET,
}


def _scale_text(text: dict[str, Any]) -> dict[str, Any]:
    """The same title, four times the size. Only `size_px` is absolute; everything else is not."""

    scaled = dict(text)
    scaled["size_px"] = int(text["size_px"]) * HIGH_RES_SCALE
    return scaled


def build(base: dict[str, Any]) -> dict[str, Any]:
    snapshot = json.loads(json.dumps(base["snapshot"]))
    output = snapshot["output"]
    output["width"] = int(output["width"]) * HIGH_RES_SCALE
    output["height"] = int(output["height"]) * HIGH_RES_SCALE
    snapshot["project_id"] = PROJECT_ID
    snapshot["workspace_handle"] = WORKSPACE_HANDLE
    snapshot["assets"] = [
        {**asset, "asset_id": ASSET_RENAMES.get(asset["asset_id"], asset["asset_id"])}
        for asset in snapshot["assets"]
        if asset["asset_id"] in ASSET_RENAMES or asset["kind"] == "font"
    ]
    clips = []
    for clip in snapshot["clips"]:
        asset_id = clip.get("asset_id")
        if asset_id is not None and asset_id not in ASSET_RENAMES:
            continue
        row = dict(clip)
        if asset_id is not None:
            row["asset_id"] = ASSET_RENAMES[asset_id]
        if row.get("text") is not None:
            row["text"] = _scale_text(row["text"])
        clips.append(row)
    snapshot["clips"] = clips
    snapshot["public_fingerprint"] = public_snapshot_fingerprint(snapshot)
    return {"snapshot": snapshot}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare without writing")
    arguments = parser.parse_args()
    base = json.loads(BASE_PATH.read_text(encoding="utf-8"))
    built = build(base)
    body = json.dumps(built, indent=2, ensure_ascii=True) + "\n"
    if arguments.check:
        current = TARGET_PATH.read_text(encoding="utf-8") if TARGET_PATH.is_file() else ""
        if current.replace("\r\n", "\n") != body:
            print("the high-resolution base is stale; run this script without --check")
            return 1
        print(json.dumps({"status": "current", "path": str(TARGET_PATH.relative_to(ROOT))}))
        return 0
    TARGET_PATH.write_text(body, encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "wrote": str(TARGET_PATH.relative_to(ROOT)),
                "output": [
                    built["snapshot"]["output"]["width"],
                    built["snapshot"]["output"]["height"],
                ],
                "clips": [clip["clip_id"] for clip in built["snapshot"]["clips"]],
                "public_fingerprint": built["snapshot"]["public_fingerprint"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
