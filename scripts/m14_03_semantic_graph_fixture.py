"""Generate the frozen M14-03 semantic graph/comparator evaluation bundle."""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from comfyui_h3_context.core.contracts import (
    CURRENT_SCHEMA_VERSION,
    ProfileIdentity,
    PromptProfile,
    TaskMode,
)
from comfyui_h3_context.core.semantic_graph_comparator import (
    M14_01_TERMINAL_DISPOSITION_SHA256,
    ComparatorEvaluationCase,
    EvaluationPartition,
    GuideGolden,
    SemanticDiffOutcome,
    SemanticPromptGraph,
    SemanticSourceKind,
    build_comparator_evaluation_case,
    build_semantic_evaluation_bundle,
    parse_legacy_semantic_prompt_v1,
)
from comfyui_h3_context.core.source_profiled_prompt import (
    OFFICIAL_H3_BASE_GUIDE_DIGEST,
    OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
    OFFICIAL_H3_GUIDE_REVISION,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m14_03_semantic_graph_comparator.json.gz"

BASE = (
    "integrated_multimodal_description: [Shot 1] cinematic live-action; subjects: the baker; "
    "scene: a quiet street bakery; action: opens the wooden shutters; camera: a slow push in; "
    "ambience: soft morning street ambience. Declared intent: A baker opens the bakery before "
    "sunrise.\n\n"
    "overall_soundscape: soft morning street ambience\n\n"
    "non_diegetic_music: N/A"
)

FULL = (
    "subject_definitions: <Subject 1> is the baker from <Picture 1>. <Subject 2> is the bakery "
    "interior from <Video 1>. <Picture 1> identifies asset picture_a with role reference and "
    "media kind image. <Picture 2> identifies asset picture_b with role reference and media kind "
    "image. <Audio 1> identifies asset audio_pair with role audio_source and media kind audio. "
    "<Video 1> identifies asset video_source with role editing_source and media kind video. "
    "<Audio 2> identifies asset audio_score with role audio_source and media kind audio.\n\n"
    "summary: [reference generation] Adapt the bakery reference into a new target video. "
    "References used: <Picture 1>, <Picture 2>, <Audio 1>, <Video 1>, <Audio 2>.\n\n"
    "retention_analysis: <Picture 1> -> <Subject 1>: fully_preserved - visual retention relation "
    "retention_1. <Audio 1> -> <Audio 1>: fully_copy - audio retention relation retention_2.\n\n"
    "detailed_description: Declared style: cinematic documentary. [Shot 1] cinematic "
    "documentary; subjects: the baker; scene: the reference bakery; action: lights the oven; "
    "camera: a slow tracking shot; diegetic: the oven hum; music: sparse piano; references: "
    "<Subject 1> (<Picture 1>); audio references: <Audio 1>; audio references: <Audio 2>. "
    "[Shot 2] At 00:02.000, cinematic documentary; subjects: the bakery interior; scene: the "
    "reference bakery; camera: a slow tracking shot; diegetic: the oven hum; events: event_1; "
    "references: <Subject 2> (<Video 1>); audio references: <Audio 1>; <Video 1> event event_1 "
    "uses full_copy for [Shot 2].. Exact dialogue: <d>[English] Keep the light on.</d> Visible "
    'text remains exactly "營業中".\n\n'
    "overall_soundscape: the oven hum (<Audio 1>)\n\n"
    "non_diegetic_music: sparse piano (<Audio 2>)"
)

BASE_PROFILE = ProfileIdentity(PromptProfile.BASE, CURRENT_SCHEMA_VERSION)
FULL_PROFILE = ProfileIdentity(PromptProfile.FULL_REFERENCE, CURRENT_SCHEMA_VERSION)

P0_AUDIO_VARIANTS = (
    ("oven room tone", "sparse piano"),
    ("quiet ventilation", "soft strings"),
    ("wood fire crackle", "muted cello"),
    ("morning street wash", "light marimba"),
    ("distant tray movement", "restrained guitar"),
    ("low bakery ambience", "minimal harp"),
    ("subtle fan hum", "warm clarinet"),
    ("soft counter activity", "gentle vibraphone"),
)

NON_P0_SOURCE_SPECS = (
    (
        "the baker",
        "a quiet street bakery",
        "opens the wooden shutters",
        "a slow push in",
        "soft morning street ambience",
        "closes the steel shutters",
    ),
    (
        "the gardener",
        "a glass greenhouse",
        "waters the seedling trays",
        "a gentle lateral dolly",
        "light birdsong and water drops",
        "uproots every seedling",
    ),
    (
        "the ceramic artist",
        "a sunlit pottery studio",
        "shapes a clay bowl",
        "a measured orbit",
        "a turning wheel and room tone",
        "smashes the finished bowl",
    ),
    (
        "the watchmaker",
        "a compact repair bench",
        "aligns the brass gears",
        "a precise macro slide",
        "quiet ticking and tool clicks",
        "scatters the brass gears",
    ),
    (
        "the librarian",
        "a tall reading room",
        "returns a book to the shelf",
        "a steady aisle tracking shot",
        "soft pages and distant footsteps",
        "throws the book onto the floor",
    ),
    (
        "the chef",
        "an open restaurant kitchen",
        "plates the final course",
        "a controlled counter glide",
        "quiet pans and ventilation",
        "discards the final course",
    ),
    (
        "the violinist",
        "an empty rehearsal hall",
        "tunes the violin",
        "a slow semicircle move",
        "bow friction and hall ambience",
        "breaks the violin strings",
    ),
    (
        "the mechanic",
        "a clean bicycle workshop",
        "tightens the rear wheel",
        "a low parallel track",
        "ratchet clicks and room tone",
        "loosens the rear wheel",
    ),
    (
        "the tailor",
        "a quiet fabric studio",
        "pins the blue sleeve",
        "a smooth tabletop push",
        "cloth movement and small scissors",
        "cuts away the blue sleeve",
    ),
    (
        "the astronomer",
        "a dark observatory dome",
        "aligns the telescope",
        "a slow rising crane",
        "soft motors and night wind",
        "turns the telescope away",
    ),
)


def _graph(
    text: str,
    profile: ProfileIdentity,
    mode: TaskMode,
    kind: SemanticSourceKind,
    source_id: str,
    authority_revision: str | None = None,
    authority_digest: str | None = None,
) -> SemanticPromptGraph:
    return parse_legacy_semantic_prompt_v1(
        text,
        profile,
        mode,
        kind,
        source_id,
        authority_revision,
        authority_digest,
    )


def _guide_goldens() -> tuple[GuideGolden, ...]:
    base_sources = (
        (BASE, TaskMode.T2VA),
        (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> "
            "(from [Shot 1]) is fully referenced.\n\n" + BASE,
            TaskMode.I2VA,
        ),
        (
            "How the reference pictures align with the target video — Picture 1 (from Shot 1) "
            "aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 2) aligns "
            "with the 5.17-second mark of the target video.\n\n" + BASE,
            TaskMode.FL2VA,
        ),
    )
    full_sources = (
        FULL,
        FULL.replace("bakery reference", "workshop reference").replace(
            "new target video", "new workshop video"
        ),
        FULL.replace("sparse piano", "soft strings").replace("the oven hum", "room tone"),
    )
    result: list[GuideGolden] = []
    for index, (text, mode) in enumerate(base_sources, 1):
        result.append(
            GuideGolden(
                f"guide.base.{index}",
                "guide.base.document",
                _graph(
                    text,
                    BASE_PROFILE,
                    mode,
                    SemanticSourceKind.PUBLIC_GUIDE,
                    f"guide.base.{index}",
                    OFFICIAL_H3_GUIDE_REVISION,
                    OFFICIAL_H3_BASE_GUIDE_DIGEST,
                ),
            )
        )
    for index, text in enumerate(full_sources, 1):
        result.append(
            GuideGolden(
                f"guide.reference.{index}",
                "guide.reference.document",
                _graph(
                    text,
                    FULL_PROFILE,
                    TaskMode.REF2VA,
                    SemanticSourceKind.PUBLIC_GUIDE,
                    f"guide.reference.{index}",
                    OFFICIAL_H3_GUIDE_REVISION,
                    OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
                ),
            )
        )
    return tuple(result)


def _swap(text: str, left: str, right: str) -> str:
    marker = "M14_03_SWAP_MARKER"
    if left not in text or right not in text or marker in text:
        raise RuntimeError("frozen swap inputs differ")
    return text.replace(left, marker, 1).replace(right, left, 1).replace(marker, right, 1)


def _p0_cluster_source(text: str, cluster_index: int) -> str:
    ambience, music = P0_AUDIO_VARIANTS[cluster_index - 1]
    return text.replace("the oven hum", ambience).replace("sparse piano", music)


def _non_p0_cluster_source(cluster_index: int) -> tuple[str, str, str, str]:
    subject, scene, action, camera, ambience, contradiction = NON_P0_SOURCE_SPECS[cluster_index - 1]
    intent = f"{subject.capitalize()} {action}"
    source = (
        f"integrated_multimodal_description: [Shot 1] cinematic live-action; subjects: "
        f"{subject}; scene: {scene}; action: {action}; camera: {camera}; ambience: "
        f"{ambience}. Declared intent: {intent}.\n\n"
        f"overall_soundscape: {ambience}\n\n"
        "non_diegetic_music: N/A"
    )
    return source, subject, action, contradiction


def _p0_cases() -> tuple[ComparatorEvaluationCase, ...]:
    subject_one = "<Subject 1> is the baker from <Picture 1>."
    subject_two = "<Subject 2> is the bakery interior from <Video 1>."
    audio_one = "<Audio 1> identifies asset audio_pair with role audio_source and media kind audio."
    audio_two = (
        "<Audio 2> identifies asset audio_score with role audio_source and media kind audio."
    )
    picture_two = "<Picture 2> identifies asset picture_b with role reference and media kind image."
    specs: list[tuple[SemanticDiffOutcome, str]] = [
        (SemanticDiffOutcome.HARD_MUTATION, FULL.replace("Keep the light on.", "Turn it off.")),
        (SemanticDiffOutcome.HARD_MUTATION, FULL.replace("Keep the light on.", "Leave now.")),
        (SemanticDiffOutcome.HARD_MUTATION, FULL.replace("Keep the light on.", "Wait outside.")),
        (SemanticDiffOutcome.HARD_MUTATION, FULL.replace("營業中", "已打烊")),
        (SemanticDiffOutcome.HARD_MUTATION, FULL.replace("營業中", "OPEN")),
        (SemanticDiffOutcome.HARD_MUTATION, FULL.replace("fully_copy", "adapted")),
        (
            SemanticDiffOutcome.ROLE,
            FULL.replace(
                "asset picture_a with role reference", "asset picture_a with role style_reference"
            ),
        ),
        (
            SemanticDiffOutcome.ROLE,
            FULL.replace(
                "asset picture_b with role reference", "asset picture_b with role subject_reference"
            ),
        ),
        (
            SemanticDiffOutcome.ROLE,
            FULL.replace(
                "asset video_source with role editing_source",
                "asset video_source with role continuation_source",
            ),
        ),
        (
            SemanticDiffOutcome.ROLE,
            FULL.replace(
                "asset audio_score with role audio_source", "asset audio_score with role reference"
            ),
        ),
        (
            SemanticDiffOutcome.OWNERSHIP,
            FULL.replace(
                "<Subject 1> is the baker from <Picture 1>",
                "<Subject 1> is the baker from <Picture 2>",
            ),
        ),
        (
            SemanticDiffOutcome.OWNERSHIP,
            FULL.replace(
                "<Subject 1> is the baker from <Picture 1>",
                "<Subject 1> is the baker from <Audio 1>",
            ),
        ),
        (
            SemanticDiffOutcome.OWNERSHIP,
            FULL.replace(
                "<Subject 1> is the baker from <Picture 1>",
                "<Subject 1> is the baker from <Video 1>",
            ),
        ),
        (
            SemanticDiffOutcome.OWNERSHIP,
            FULL.replace(
                "<Subject 2> is the bakery interior from <Video 1>",
                "<Subject 2> is the bakery interior from <Picture 1>",
            ),
        ),
        (SemanticDiffOutcome.ORDER, _swap(FULL, subject_one, subject_two)),
        (SemanticDiffOutcome.ORDER, _swap(FULL, audio_one, audio_two)),
        (SemanticDiffOutcome.ORDER, _swap(FULL, picture_two, audio_one)),
        (SemanticDiffOutcome.ORDER, _swap(FULL, picture_two, audio_two)),
        (SemanticDiffOutcome.TEMPORAL, FULL.replace("00:02.000", "00:01.000")),
        (SemanticDiffOutcome.TEMPORAL, FULL.replace("00:02.000", "00:03.000")),
        (SemanticDiffOutcome.TEMPORAL, FULL.replace("00:02.000", "00:04.000")),
        (SemanticDiffOutcome.TEMPORAL, FULL.replace("00:02.000", "00:05.000")),
        (
            SemanticDiffOutcome.CONTRADICTION,
            FULL.replace("cinematic documentary", "watercolor fantasy"),
        ),
        (
            SemanticDiffOutcome.CONTRADICTION,
            FULL.replace("a slow tracking shot", "a static wide shot"),
        ),
    ]
    result: list[ComparatorEvaluationCase] = []
    for index, (expected, local_text) in enumerate(specs, 1):
        cluster_index = ((index - 1) % 8) + 1
        cluster = f"p0.cluster.{cluster_index}"
        local = _graph(
            _p0_cluster_source(local_text, cluster_index),
            FULL_PROFILE,
            TaskMode.REF2VA,
            SemanticSourceKind.LOCAL,
            f"p0.local.{index}",
        )
        reference = _graph(
            _p0_cluster_source(FULL, cluster_index),
            FULL_PROFILE,
            TaskMode.REF2VA,
            SemanticSourceKind.LOCAL,
            f"p0.reference.{index}",
        )
        result.append(
            build_comparator_evaluation_case(
                f"p0.case.{index}",
                cluster,
                1000 + index,
                EvaluationPartition.P0,
                expected,
                True,
                local,
                reference,
            )
        )
    return tuple(result)


def _non_p0_cases() -> tuple[ComparatorEvaluationCase, ...]:
    result: list[ComparatorEvaluationCase] = []
    case_index = 0
    for cluster_index in range(1, 11):
        cluster = f"holdout.cluster.{cluster_index}"
        base, subject, action, contradiction = _non_p0_cluster_source(cluster_index)
        intent = f"{subject.capitalize()} {action}"
        variants = (
            (SemanticDiffOutcome.EXACT, False, base),
            (
                SemanticDiffOutcome.COMPATIBLE,
                False,
                base.replace(f"Declared intent: {intent}.", f"Declared intent: {intent}!"),
            ),
            (
                SemanticDiffOutcome.CONTRADICTION,
                True,
                base.replace(f"action: {action}", f"action: {contradiction}"),
            ),
            (
                SemanticDiffOutcome.LOCAL_ONLY,
                True,
                base.replace(f"subjects: {subject}", f"subjects: {subject} beside <Picture 9>"),
            ),
        )
        for expected, material, local_text in variants:
            case_index += 1
            local = _graph(
                local_text,
                BASE_PROFILE,
                TaskMode.T2VA,
                SemanticSourceKind.LOCAL,
                f"holdout.local.{case_index}",
            )
            reference = _graph(
                base,
                BASE_PROFILE,
                TaskMode.T2VA,
                SemanticSourceKind.LOCAL,
                f"holdout.reference.{case_index}",
            )
            result.append(
                build_comparator_evaluation_case(
                    f"holdout.case.{case_index}",
                    cluster,
                    2000 + case_index,
                    EvaluationPartition.NON_P0,
                    expected,
                    material,
                    local,
                    reference,
                )
            )
    return tuple(result)


def build_wire() -> dict[str, object]:
    return build_semantic_evaluation_bundle(
        _guide_goldens(), _p0_cases(), _non_p0_cases()
    ).to_wire()


def _render_wire(wire: dict[str, object]) -> str:
    rendered = json.dumps(wire, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    for public_pin in (
        OFFICIAL_H3_GUIDE_REVISION,
        OFFICIAL_H3_BASE_GUIDE_DIGEST,
        OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
        M14_01_TERMINAL_DISPOSITION_SHA256,
    ):
        escaped = "".join(f"\\u{ord(character):04x}" for character in public_pin)
        rendered = rendered.replace(public_pin, escaped)
    return rendered


def _compress_wire(rendered: str) -> bytes:
    compressed = bytearray(gzip.compress(rendered.encode("utf-8"), compresslevel=9, mtime=0))
    # IMPORTANT: normalize the gzip OS byte so Windows/POSIX regeneration is byte-identical.
    compressed[9] = 255
    return bytes(compressed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = _render_wire(build_wire())
    compressed = _compress_wire(rendered)
    if args.check:
        if not FIXTURE.is_file() or FIXTURE.read_bytes() != compressed:
            print("M14-03 semantic graph fixture differs")
            return 1
        print("M14-03 semantic graph fixture is byte-identical")
        return 0
    FIXTURE.write_bytes(compressed)
    print(FIXTURE.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
