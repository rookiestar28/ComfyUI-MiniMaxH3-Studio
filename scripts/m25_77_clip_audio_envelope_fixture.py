"""Generate the table both statements of the clip audio envelope are tested against.

Writes `tests/fixtures/m25_77_clip_audio_envelope_v1.json` from `core/clip_audio.py`: for a fixed
set of clips -- a duration and an audio member each -- and a fixed set of clip-relative output
samples, the factor the definition gives. The browser's statement
(`frontend/src/runtime/clipAudioEnvelope.ts`) and the final render's filters are each tested
against these rows; neither takes its expectation from the other.

GUARD: derive, never hand-edit. A row typed by hand is a number nobody computed.

Usage:
    python scripts/m25_77_clip_audio_envelope_fixture.py [--check]

`--check` recomputes and compares without writing, which is what the test suite calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.clip_audio import (  # noqa: E402
    SAMPLES_PER_FRAME,
    clip_audio_factor,
)
from comfyui_h3_context.core.composition_contract import ClipAudio  # noqa: E402

TARGET_PATH = ROOT / "tests" / "fixtures" / "m25_77_clip_audio_envelope_v1.json"
SCHEMA = "h3.test.clip_audio_envelope.v1"

#: (name, duration frames, gain mb, muted, fade-in frames, fade-out frames)
CLIPS = (
    ("identity", 48, 0, False, 0, 0),
    ("gain_lower_bound", 48, -6000, False, 0, 0),
    ("gain_minus_6_db", 48, -600, False, 0, 0),
    ("gain_upper_bound", 48, 1200, False, 0, 0),
    ("muted", 48, -600, True, 12, 12),
    ("fade_in", 48, 0, False, 12, 0),
    ("fade_out", 48, 0, False, 0, 12),
    ("fades_with_a_middle", 48, -600, False, 12, 12),
    ("fades_fill_the_clip", 48, 0, False, 24, 24),
    ("one_frame_fade_in", 1, 0, False, 1, 0),
    ("one_frame_fade_out", 1, 0, False, 0, 1),
    ("upper_bound_fades", 600, 300, False, 240, 240),
)


def _samples(duration: int, fade_in: int, fade_out: int) -> list[int]:
    """Every sample a ramp starts, ends or turns at, and its neighbours, inside the clip."""

    length = duration * SAMPLES_PER_FRAME
    marks = {0, 1, length // 2, length - 2, length - 1}
    for edge in (fade_in * SAMPLES_PER_FRAME, length - fade_out * SAMPLES_PER_FRAME):
        marks.update({edge - 1, edge, edge + 1})
    if fade_in:
        marks.add(fade_in * SAMPLES_PER_FRAME // 2)
    if fade_out:
        marks.add(length - fade_out * SAMPLES_PER_FRAME // 2)
    return sorted(value for value in marks if 0 <= value < length)


def build() -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for name, duration, gain, muted, fade_in, fade_out in CLIPS:
        audio = ClipAudio(gain, muted, fade_in, fade_out)
        rows.append(
            {
                "name": name,
                "duration_frames": duration,
                "audio": audio.to_wire(),
                "factors": [
                    [k, clip_audio_factor(audio, duration, k)]
                    for k in _samples(duration, fade_in, fade_out)
                ],
            }
        )
    return {"schema": SCHEMA, "samples_per_frame": SAMPLES_PER_FRAME, "clips": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare without writing")
    arguments = parser.parse_args(argv)
    body = json.dumps(build(), indent=1, ensure_ascii=True) + "\n"
    if arguments.check:
        current = TARGET_PATH.read_text(encoding="utf-8") if TARGET_PATH.is_file() else ""
        if current.replace("\r\n", "\n") != body:
            print("the clip audio envelope table is stale; run this script without --check")
            return 1
        print(json.dumps({"status": "current", "path": TARGET_PATH.relative_to(ROOT).as_posix()}))
        return 0
    TARGET_PATH.write_text(body, encoding="utf-8", newline="\n")
    print(json.dumps({"wrote": TARGET_PATH.relative_to(ROOT).as_posix(), "clips": len(CLIPS)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
