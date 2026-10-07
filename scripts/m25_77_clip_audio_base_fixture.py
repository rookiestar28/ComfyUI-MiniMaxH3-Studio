"""Generate the `clip_audio` semantic corpus base from the accepted 320 x 180 one.

M25-77 measures a clip's audio adjustments -- gain, mute and fades -- as the level of a steady tone,
window by window. The accepted base cannot carry those rows: its sources sound as short bursts with
silence between them, and its 48-frame clip has no room for the longest fade the contract admits
(240 frames). So this base keeps the accepted base's canvas, output profile and image layer, and
replaces everything else with one 248-frame clip of the tone source
(`semantic_conformance_media.TONE_ASSET`) on the primary track.

GUARD: derive, never hand-edit. The base's public fingerprint covers every field, and the asset's
landmark table and sample count are the ones the tone source's profile declares
(`SourceProfile.pts_table`) -- the same table the product measures from the file the render stage
encodes. A hand-edited copy would judge one source against another source's facts. Run this again
after any change to the accepted base or to the tone source.

Usage:
    python scripts/m25_77_clip_audio_base_fixture.py [--check]

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
    AUDIO_SAMPLE_RATE,
    IMAGE_ASSET,
    PRIMARY_ASSET,
    SOURCE_TIME_BASE_DEN,
    TONE_ASSET,
    profile_for,
)

BASE_PATH = ROOT / "tests" / "fixtures" / "m25_20_semantic_corpus_base_v1.json"
TARGET_PATH = ROOT / "tests" / "fixtures" / "m25_77_semantic_clip_audio_base_v1.json"

#: The base's own identity. The workspace handle and project differ from the accepted base so that
#: no stage can mistake one composition's history for the other's.
WORKSPACE_HANDLE = "authoring-" + "77" * 16
PROJECT_ID = "project-fixture-clip-audio"
PRIMARY_CLIP = "clip-main"
IMAGE_CLIP = "clip-image"
PRIMARY_TRACK = "track-primary"
IMAGE_TRACK = "track-image"


def _tone_asset(template: dict[str, Any]) -> dict[str, Any]:
    """The tone source's asset entry, in the accepted primary's shape, from its own profile."""

    profile = profile_for(TONE_ASSET)
    if profile is None:
        raise SystemExit("the tone source has no declared profile")
    table = profile.pts_table()
    ticks = sum(duration for _index, _pts, duration in table)
    samples, remainder = divmod(ticks * AUDIO_SAMPLE_RATE, SOURCE_TIME_BASE_DEN)
    if remainder:
        raise SystemExit("the tone source does not last a whole number of samples")
    asset = dict(template)
    asset.update(
        asset_id=TONE_ASSET,
        source_frame_count=profile.frame_count,
        source_sample_count=samples,
        landmarks=[
            {"frame_index": index, "pts": pts, "dts": pts, "duration_ticks": duration}
            for index, pts, duration in table
        ],
    )
    return asset


def build(base: dict[str, Any]) -> dict[str, Any]:
    snapshot = json.loads(json.dumps(base["snapshot"]))
    profile = profile_for(TONE_ASSET)
    if profile is None:
        raise SystemExit("the tone source has no declared profile")
    frames = profile.frame_count
    snapshot["project_id"] = PROJECT_ID
    snapshot["workspace_handle"] = WORKSPACE_HANDLE
    snapshot["output"]["duration_frames"] = frames
    assets = {asset["asset_id"]: asset for asset in snapshot["assets"]}
    snapshot["assets"] = [_tone_asset(assets[PRIMARY_ASSET]), assets[IMAGE_ASSET]]
    snapshot["tracks"] = [
        track for track in snapshot["tracks"] if track["track_id"] in (PRIMARY_TRACK, IMAGE_TRACK)
    ]
    clips = {clip["clip_id"]: clip for clip in snapshot["clips"]}
    primary = dict(clips[PRIMARY_CLIP])
    primary.update(asset_id=TONE_ASSET, start_frame=0, duration_frames=frames, source_start_frame=0)
    image = dict(clips[IMAGE_CLIP])
    image.update(start_frame=0, duration_frames=frames)
    snapshot["clips"] = [primary, image]
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
            print("the clip audio base is stale; run this script without --check")
            return 1
        print(json.dumps({"status": "current", "path": str(TARGET_PATH.relative_to(ROOT))}))
        return 0
    TARGET_PATH.write_text(body, encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "wrote": str(TARGET_PATH.relative_to(ROOT)),
                "duration_frames": built["snapshot"]["output"]["duration_frames"],
                "clips": [clip["clip_id"] for clip in built["snapshot"]["clips"]],
                "public_fingerprint": built["snapshot"]["public_fingerprint"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
