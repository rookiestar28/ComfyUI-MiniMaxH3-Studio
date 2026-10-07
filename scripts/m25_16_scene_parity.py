"""M25-16: generate the cross-language scene-resolver parity vectors.

The browser composition monitor resolves output frames locally with a TypeScript port of
``comfyui_h3_context.core.composition_contract.resolve_composition``.  This script derives
deterministic variants of the accepted M25-10 composition fixture, resolves every output frame
with the backend authority, and writes the canonical resolved-scene wire (or the typed refusal
code) for each case to ``tests/fixtures/m25_16_scene_resolver_parity_v1.json``.

``tests/test_m25_16_scene_resolver_parity.py`` re-derives the vectors and fails when the fixture
drifts from the backend; ``frontend/tests/sceneResolver.test.ts`` replays the same vectors
against the TypeScript port.  Run with ``--write`` to refresh the fixture, ``--check`` to verify.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.canonical import canonical_bytes  # noqa: E402
from comfyui_h3_context.core.composition_contract import (  # noqa: E402
    CompositionContractError,
    decode_public_snapshot,
    public_snapshot_fingerprint,
    resolve_composition,
)

SOURCE_FIXTURE = ROOT / "tests" / "fixtures" / "m25_10_composition_contract_v1.json"
PARITY_FIXTURE = ROOT / "tests" / "fixtures" / "m25_16_scene_resolver_parity_v1.json"
SCHEMA = "h3.context.m25_16_scene_resolver_parity.v1"


def _base_snapshot() -> dict[str, Any]:
    with SOURCE_FIXTURE.open(encoding="utf-8") as handle:
        return cast(dict[str, Any], json.load(handle)["snapshot"])


def _finalize(wire: dict[str, Any]) -> dict[str, Any]:
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return wire


def _variants() -> list[tuple[str, dict[str, Any]]]:
    base = _base_snapshot()
    variants: list[tuple[str, dict[str, Any]]] = [("accepted_fixture", copy.deepcopy(base))]

    disabled_track = copy.deepcopy(base)
    for track in disabled_track["tracks"]:
        if track["kind"] == "video_overlay":
            track["enabled"] = False
    variants.append(("video_overlay_track_disabled", disabled_track))

    disabled_primary = copy.deepcopy(base)
    for clip in disabled_primary["clips"]:
        if clip["track_id"] == "track-primary":
            clip["enabled"] = False
        # The overlay dissolve needs a lower layer for its whole duration; without the primary
        # clip the accepted contract refuses it, so this variant disables the transition too.
        if clip["clip_id"] == "clip-video-overlay":
            clip["transition"] = {"kind": "none", "duration_frames": 0}
    variants.append(("primary_clip_disabled", disabled_primary))

    trimmed_primary = copy.deepcopy(base)
    for clip in trimmed_primary["clips"]:
        if clip["clip_id"] == "clip-main":
            clip["start_frame"] = 6
            clip["duration_frames"] = 30
            clip["source_start_frame"] = 12
    variants.append(("primary_trimmed_to_source_landmark", trimmed_primary))

    split_primary = copy.deepcopy(base)
    for clip in split_primary["clips"]:
        if clip["clip_id"] == "clip-main":
            clip["duration_frames"] = 12
    right = copy.deepcopy(next(c for c in base["clips"] if c["clip_id"] == "clip-main"))
    right["clip_id"] = "clip-main-right"
    right["start_frame"] = 12
    right["duration_frames"] = 36
    right["source_start_frame"] = 12
    split_primary["clips"].append(right)
    variants.append(("primary_split_two_owners", split_primary))

    dissolve = copy.deepcopy(base)
    for clip in dissolve["clips"]:
        if clip["clip_id"] == "clip-title":
            clip["transition"] = {"kind": "cross_dissolve_v1", "duration_frames": 4}
        if clip["clip_id"] == "clip-image":
            clip["effect"] = {
                "kind": "color_adjust_v1",
                "brightness_permille": 100,
                "contrast_permille": 1100,
                "saturation_permille": 900,
            }
    variants.append(("title_dissolve_and_image_color_adjust", dissolve))

    silent = copy.deepcopy(base)
    for asset in silent["assets"]:
        if asset["asset_id"] == "vid-primary":
            asset["embedded_audio"] = "absent"
            asset["source_sample_count"] = None
    variants.append(("primary_source_silent", silent))

    unavailable_audio = copy.deepcopy(base)
    for asset in unavailable_audio["assets"]:
        if asset["asset_id"] == "vid-primary":
            asset["embedded_audio"] = "unavailable"
            asset["source_sample_count"] = None
    variants.append(("primary_audio_unavailable", unavailable_audio))

    return [(name, _finalize(wire)) for name, wire in variants]


def build() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for name, wire in _variants():
        snapshot = decode_public_snapshot(wire)
        frames: list[dict[str, Any]] = []
        duration = int(wire["output"]["duration_frames"])
        # Boundary-dense sample: clip starts/ends, dissolve edges, the split cut and both
        # out-of-range refusals. Every frame would cost 1.7 MiB for no additional coverage.
        sampled = (0, 5, 6, 11, 12, 13, 15, 16, 23, 24, 25, 26, 35, 36, 47, duration, -1)
        for frame in sampled:
            try:
                scene = resolve_composition(snapshot, frame)
            except CompositionContractError as exc:
                frames.append({"frame": frame, "refusal": exc.code})
                continue
            frames.append(
                {
                    "frame": frame,
                    "scene": json.loads(canonical_bytes(scene.to_wire()).decode("utf-8")),
                }
            )
        cases.append({"name": name, "snapshot": wire, "frames": frames})
    return {"schema": SCHEMA, "source": SOURCE_FIXTURE.relative_to(ROOT).as_posix(), "cases": cases}


def render() -> str:
    return json.dumps(build(), separators=(",", ":"), ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="refresh the parity fixture")
    group.add_argument("--check", action="store_true", help="verify the parity fixture")
    args = parser.parse_args(argv)
    expected = render()
    if args.write:
        # Byte-exact LF output: Windows text-mode ``write_text`` would emit CRLF and the
        # committed fixture must be identical on every platform for ``--check`` to hold.
        PARITY_FIXTURE.write_bytes(expected.encode("utf-8"))
        print(f"wrote {PARITY_FIXTURE.relative_to(ROOT).as_posix()}")
        return 0
    actual = PARITY_FIXTURE.read_bytes().decode("utf-8") if PARITY_FIXTURE.exists() else ""
    if actual != expected:
        print("scene resolver parity fixture drifted; rerun with --write", file=sys.stderr)
        return 1
    print("scene resolver parity fixture is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
