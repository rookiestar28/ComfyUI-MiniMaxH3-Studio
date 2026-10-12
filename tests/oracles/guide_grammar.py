"""Independent prompt-interface grammar, with content-free anchors in the catalogue."""

from __future__ import annotations

import re

SOURCE_ANCHORS = (
    "guide.base.shots@d21241f0",
    "guide.reference.structure@d21241f0",
    "guide.reference.description@d21241f0",
)

FULL_REFERENCE_FIELDS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
SHOT = re.compile(r"\[Shot ([1-9][0-9]*)\]")
DIALOGUE = re.compile(r"<d>[^<>]*</d>")
SHOT_START = re.compile(r"\[Shot ([1-9][0-9]*)\](?: At ([0-9]{2}):([0-5][0-9])\.([0-9]{3}),)?")
CUT_VOCABULARY = frozenset(
    {
        "the camera cuts to",
        "the shot cuts to",
        "the shot transitions to",
        "the shot changes to",
        "the shot switches to",
    }
)
CAMERA_MOTION_TYPES = frozenset(
    {
        "ZoomIn",
        "ZoomOut",
        "PushIn",
        "PullOut",
        "PanLeft",
        "PanRight",
        "TruckLeft",
        "TruckRight",
        "TiltUp",
        "TiltDown",
        "PedestalUp",
        "PedestalDown",
        "ArcShot",
        "TrackingShot",
        "StaticShot",
        "ShakeSlightly",
        "ShakeStrongly",
        "POV",
        "RollClockwise",
        "RollCounterclockwise",
    }
)


def detailed_description_has_shot_lines(text: str) -> bool:
    """Each shot marker begins a separate nonempty line, after the optional style line."""
    markers = SHOT.findall(text)
    if not markers:
        return False
    marked_lines = [line for line in text.splitlines() if SHOT.search(line)]
    return len(marked_lines) == len(markers) and all(
        SHOT.match(line.strip()) and len(SHOT.findall(line)) == 1 for line in marked_lines
    )


def shot_sequence(text: str) -> tuple[int, ...]:
    return tuple(int(value) for value in SHOT.findall(text))


def shot_starts_are_ordered(text: str, *, duration_milliseconds: int) -> bool:
    markers = tuple(SHOT_START.finditer(text))
    if not markers or shot_sequence(text) != tuple(range(1, len(markers) + 1)):
        return False
    if markers[0].group(2) is not None:
        return False
    previous = 0
    for marker in markers[1:]:
        if marker.group(2) is None:
            return False
        minutes, seconds, milliseconds = map(int, marker.groups()[1:])
        moment = ((minutes * 60) + seconds) * 1000 + milliseconds
        if not previous < moment < duration_milliseconds:
            return False
        previous = moment
    return True
