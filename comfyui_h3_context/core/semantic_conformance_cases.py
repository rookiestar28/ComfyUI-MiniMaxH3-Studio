"""The executable recipe for every row of the closed semantic conformance corpus.

`semantic_conformance` says *which* cases exist. This module says, for each of them, what has to be
set up, what is expected to happen, and which kind of observation can satisfy the row. Keeping the
two apart is deliberate: the corpus is expanded from the accepted contract, and a recipe that could
add or drop a case would let the runner decide its own coverage.

Three rules shape everything here.

- **A recipe is declared before execution, never derived from it.** Every refusal code below is a
  statement about the accepted decoder taken from the contract that raises it, not a value observed
  from a run and written back. When a declaration is wrong the row reports `MISMATCH`, which is a
  real finding about either the product or this table -- and that is the outcome that has to remain
  possible. Never repair a red row by editing the expectation to match what was measured.
- **The numeric families are derived from the same declarations the corpus is.** A boundary case's
  value comes from its `NumericDomain`, so moving a product bound moves the corpus row, the domain
  test and this recipe together instead of leaving two of the three behind.
- **An observation kind is a claim about what evidence closes the row.** It is closed vocabulary so
  a runner cannot invent a cheaper kind of evidence for an expensive row -- answering a
  `render_and_browser` row with a shell snapshot is the substitution the review rejected.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from .semantic_conformance import (
    AUDIO_NEGATIVE_SURFACES,
    BLEND_MODES,
    CLIP_AUDIO_DOMAINS,
    CROP_EDGES,
    DEFERRED_AUDIO_SEAMS,
    EFFECT_DOMAINS,
    OPACITY_DOMAIN,
    TEXT_LINE_HEIGHT_DOMAIN,
    TEXT_SIZE_DOMAIN,
    TRANSFORM_DOMAINS,
    Corpus,
    CorpusCase,
    NumericDomain,
    SemanticConformanceError,
    build_corpus,
)
from .semantic_conformance_media import TIMING_ASSET as _TIMING_ASSET
from .semantic_conformance_media import TIMING_VFR_START_FRAME as _TIMING_VFR_START_FRAME

#: How a row is observed. Closed, because the substitution the post-closeout review rejected was
#: exactly this: answering a row that needs a rendered artifact and a presented canvas with a
#: cheaper piece of evidence and reporting it as closed.
#:
#: - `render_and_browser`: the composition is rendered by the accepted renderer and presented by the
#:   accepted runtime, and the fixture, the canvas and the independently decoded artifact are
#:   compared three ways.
#: - `contract_refusal`: the accepted decoder refuses the composition with an exact code, the
#:   runtime refuses the same edit with the same code, and no artifact exists.
#: - `command_effect`: one M25-11 command is applied through the real history, and the before and
#:   after compositions are rendered and presented separately.
#: - `command_refusal`: the command is refused with an exact code and mutates nothing.
#: - `shell_invariant`: a frozen `SidebarEditorUiInvariantV1` row, observed in the browser only --
#:   it has no artifact and can never satisfy a command row.
#: - `import_integration`: the explicit import journey, whose import and insertion effects are
#:   counted separately.
#: - `declared_negative`: a predeclared deferred seam, proven absent rather than skipped.
OBSERVATION_KINDS: Final = (
    "render_and_browser",
    "contract_refusal",
    "command_effect",
    "command_refusal",
    "shell_invariant",
    "import_integration",
    "declared_negative",
)

#: The identifiers the accepted base composition actually uses, quoted from
#: `tests/fixtures/m25_20_semantic_corpus_base_v1.json`. They are named once so a recipe can never
#: spell a clip the fixture does not contain -- a recipe naming an absent clip would be refused by
#: the decoder for the wrong reason and would look like the refusal it was supposed to prove.
PRIMARY_CLIP: Final = "clip-main"
OVERLAY_CLIP: Final = "clip-video-overlay"
IMAGE_CLIP: Final = "clip-image"
TITLE_CLIP: Final = "clip-title"
PRIMARY_TRACK: Final = "track-primary"
OVERLAY_TRACK: Final = "track-video"
IMAGE_TRACK: Final = "track-image"
TEXT_TRACK: Final = "track-text"
PRIMARY_ASSET: Final = "vid-primary"
OVERLAY_ASSET: Final = "vid-overlay"
IMAGE_ASSET: Final = "img-overlay"
#: The timing source (B-58), declared with its timing in `semantic_conformance_media`.
TIMING_ASSET: Final = _TIMING_ASSET
TIMING_VFR_START_FRAME: Final = _TIMING_VFR_START_FRAME
#: The second primary clip each owner hand-over row of the `clip_audio` family appends: one that
#: takes the sound from `clip-main` inside its fade-out and keeps it to the end, and one that takes
#: it and hands it back while the fade-out is under way.
HANDOFF_CLIP: Final = "clip-handoff"
HANDBACK_CLIP: Final = "clip-handback"
#: The clip identifier a split introduces, named once so the setups that split the primary clip and
#: then operate on its right half cannot drift apart.
SPLIT_CLIP: Final = "clip-split"
#: The clip a second split introduces, for the one row that needs a disabled clip with a
#: contributing neighbour on each side.
SPLIT_TAIL_CLIP: Final = "clip-split-tail"
#: The source frames the *timing* family's literal landmark tables declare. They are the sparse
#: boundary shapes that family is about, and they are no longer what the base carries: the corpus
#: base now declares one landmark per source frame, because that is what a claimed source really
#: produces (`AuthoringVideoFacts` requires exactly one landmark per decoded frame).
#:
#: GUARD: a source-shifting command -- `trim_clip` on the start edge, `slip_clip`, `split_clip`,
#: `roll_edit` -- is refused with `source_range_unavailable` ("source shift is not exactly
#: representable") when the shift lands between declared landmarks. Against the dense base every
#: shift lands on one, so the deltas below are free; against a row that has replaced the table with
#: one of these sparse ones they are not. Six corpus rows were once refused outright for exactly
#: that reason, and a recipe that combines a timing edit with a source-shifting setup would be
#: again.
TIMING_FAMILY_LANDMARKS: Final = (0, 12, 47)
#: The accepted M25-10 fixture names its font asset `font-main`, which is a placeholder: the
#: render planner resolves a text clip's `font_asset_id` against the *packaged* font manifest
#: (`comfyui_h3_context/fonts/font_manifest_v1.json`), and a composition naming a font that is
#: not packaged cannot be rendered at all. Every text row would then be permanently BLOCKED for
#: a reason that has nothing to do with what it tests. `normalize_base` renames the placeholder
#: to the packaged identifier once, so the corpus and the renderer name the same real font.
PLACEHOLDER_FONT_ASSET: Final = "font-main"
FONT_ASSET: Final = "h3.font.noto_sans.v1"

#: The exact codes `composition_contract` raises, quoted from the `_reject` call that raises each.
#: A generic bounds violation is `invalid_contract`; the timing family and the currentness family
#: have their own codes, and the output profile has one of its own.
INVALID_CONTRACT: Final = "invalid_contract"
#: The timeline command decoder's own code. It is a separate authority from the composition
#: decoder: a command that names a clip or track that is not there is `invalid_command`, while a
#: command whose *result* would violate the composition contract carries that contract's code
#: through instead. Declaring `invalid_contract` for the first kind was wrong for seventeen rows.
INVALID_COMMAND: Final = "invalid_command"
#: The history and rebase authorities have codes of their own. A cursor that names no retained
#: entry is `history_cursor_invalid`, and a rebase whose base is gone is `rebase_conflict`;
#: neither is an `invalid_command`, and declaring one as the other passes for the wrong reason.
HISTORY_CURSOR_INVALID: Final = "history_cursor_invalid"
REBASE_CONFLICT: Final = "rebase_conflict"
INVALID_TIMING: Final = "invalid_timing"
NEGATIVE_TIMESTAMP: Final = "negative_timestamp"
SOURCE_RANGE_UNAVAILABLE: Final = "source_range_unavailable"
UNSUPPORTED_OUTPUT_PROFILE: Final = "unsupported_output_profile"
STALE_SNAPSHOT: Final = "stale_snapshot"
SOURCE_REPLACED: Final = "source_replaced"
#: The packaged font resolver's own code, raised when a title's content contains a
#: codepoint no packaged face covers. It is neither a composition nor a command refusal:
#: the snapshot is valid and no command is involved, and the refusal happens where the
#: render plan's font facts are bound. Declaring it as `invalid_contract` would have the
#: row pass on a decoder that never looks at coverage at all.
FONT_GLYPH_UNSUPPORTED: Final = "font_glyph_unsupported"

#: Which decoder refuses a row. The snapshot decoder, the timeline command decoder and the render
#: planner's currentness check are three separate authorities, and a row that names the wrong one
#: passes for the wrong reason: an edge-trim draft refusal is not a refusal of the composition, and
#: a stale-snapshot refusal happens when a plan is bound rather than when a snapshot is decoded.
REFUSAL_LAYERS: Final = ("snapshot", "command", "currentness", "font_binding")

REFUSAL_CODES: Final = (
    INVALID_CONTRACT,
    INVALID_COMMAND,
    HISTORY_CURSOR_INVALID,
    REBASE_CONFLICT,
    INVALID_TIMING,
    NEGATIVE_TIMESTAMP,
    SOURCE_RANGE_UNAVAILABLE,
    UNSUPPORTED_OUTPUT_PROFILE,
    STALE_SNAPSHOT,
    SOURCE_REPLACED,
    FONT_GLYPH_UNSUPPORTED,
)

#: Where a `SnapshotEdit` lands in the composition wire. Closed so a recipe cannot reach into a
#: field the accepted contract does not expose.
#: Which composition a row is a case of. A recipe names one of these; a stage resolves the name to
#: a fixture, and a recipe never carries a path.
#:
#: - `corpus`: the accepted 320 x 180 base, `tests/fixtures/m25_20_semantic_corpus_base_v1.json`.
#:   Every row written before M25-45 is one of these and none of them changed.
#: - `high_resolution`: the same composition at 1280 x 720 with 512-square sources
#:   (`tests/fixtures/m25_45_semantic_highres_base_v1.json`, derived by
#:   `scripts/m25_45_highres_base_fixture.py`). M25-45's AC45-05 rows, which observe real detail at
#:   the shipped preview's own cap rather than a 128-pixel picture enlarged to fill it.
#: - `clip_audio`: the accepted base's canvas and image layer over one 248-frame clip of the tone
#:   source (`tests/fixtures/m25_77_semantic_clip_audio_base_v1.json`, derived by
#:   `scripts/m25_77_clip_audio_base_fixture.py`). M25-77's rows, which measure a clip's audio
#:   adjustments as levels of a steady tone, with room for the longest fade the contract admits.
CORPUS_BASE: Final = "corpus"
HIGH_RESOLUTION_BASE: Final = "high_resolution"
CLIP_AUDIO_BASE: Final = "clip_audio"
BASE_NAMES: Final = (CORPUS_BASE, HIGH_RESOLUTION_BASE, CLIP_AUDIO_BASE)

#: The fixture file each base name is stored in, under `tests/fixtures/`. It is stated here, once,
#: for the same reason the setup driver is shared: four stages resolve a row's composition, and a
#: stage that chose its own file would judge a row against a composition another stage never
#: rendered. Names only -- this module owns no filesystem path.
BASE_FIXTURE_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        CORPUS_BASE: "m25_20_semantic_corpus_base_v1.json",
        HIGH_RESOLUTION_BASE: "m25_45_semantic_highres_base_v1.json",
        CLIP_AUDIO_BASE: "m25_77_semantic_clip_audio_base_v1.json",
    }
)

#: The picture area and device pixel ratio each base is presented in, and the backing that
#: arrangement produces. One table, here, because two stages depend on it and would otherwise keep
#: their own copies: the browser stage presents the row, and the join decides from the same numbers
#: whether the row's landmarks were observable at output precision at all.
#:
#: GUARD: a backing is CSS pixels times the device pixel ratio, and the preview never scales up, so
#: presenting a 1280 x 720 composition at output scale needs `pane x dpr >= 1280 x 720`. A
#: 1440 x 900 viewport has no 1280 CSS-pixel-wide monitor pane and cannot be given one without a
#: layout no user starts from, so the high-resolution rows change the ratio instead and keep the
#: viewport. Were
#: this left at the default, `browser_observable` would call those rows scaled-down, the browser
#: stage would prescribe nothing for them, and the join would judge them on the final artifact
#: alone -- a high-resolution row closing without ever being presented at high resolution, which is
#: exactly what AC45-05 excludes. Change these numbers only with the layout that produces them.
BASE_PRESENTATION: Final[Mapping[str, Mapping[str, float]]] = MappingProxyType(
    {
        # Unmeasured: 320 x 180 is inside the legacy floor, so the product presents it at output
        # scale in any picture box and the corpus rows are unchanged by M25-45.
        CORPUS_BASE: MappingProxyType(
            {"pane_css_width": 0.0, "pane_css_height": 0.0, "device_pixel_ratio": 1.0}
        ),
        HIGH_RESOLUTION_BASE: MappingProxyType(
            {"pane_css_width": 640.0, "pane_css_height": 360.0, "device_pixel_ratio": 2.0}
        ),
        # The accepted base's own canvas, presented as the accepted base is.
        CLIP_AUDIO_BASE: MappingProxyType(
            {"pane_css_width": 0.0, "pane_css_height": 0.0, "device_pixel_ratio": 1.0}
        ),
    }
)

EDIT_TARGETS: Final = (
    "clip",
    "clip.transform",
    "clip.crop",
    "clip.text",
    "clip.text.style",
    "clip.transition",
    "clip.effect",
    "track",
    "output",
    #: The asset row itself, addressed by `subject` asset identifier. The embedded-audio family
    #: needs it: whether a source carries bound audio at all is a property of the asset, and no
    #: timeline command can change it, so a row like `embedded_audio.primary_without_audio` has no
    #: other way to be the case its name states.
    "asset",
    "asset.landmarks",
    #: A second clip on the primary track, appended as a copy of the `subject` clip with the
    #: literal fields in `value` written over it. The timing family needs it: a hard cut is two
    #: clips meeting at a frame, and no single-clip edit can be that case.
    "clips",
    #: One field of the `subject` clip's audio member (M25-77). The member is optional on the wire
    #: and its identity is its absence, so this target is applied by `_apply_clip_audio` rather
    #: than as a plain field write: an absent member is the identity to edit from, and an edit that
    #: lands back on the identity removes the member instead of writing it out, which the decoder
    #: would refuse as a second encoding of the same value.
    "clip.audio",
)

#: The fields of a clip's audio member, and the identity an absent member stands for.
_CLIP_AUDIO_IDENTITY: Final[Mapping[str, object]] = MappingProxyType(
    {"gain_mb": 0, "muted": False, "fade_in_frames": 0, "fade_out_frames": 0}
)


def _reject(message: str) -> SemanticConformanceError:
    return SemanticConformanceError(message)


@dataclass(frozen=True, slots=True)
class SnapshotEdit:
    """One closed edit applied to the base composition before the case runs."""

    target: str
    field: str
    value: object
    subject: str = PRIMARY_CLIP

    def __post_init__(self) -> None:
        if self.target not in EDIT_TARGETS:
            raise _reject(f"{self.target} is not a declared edit target")
        if not self.field:
            raise _reject("an edit needs a field")

    def as_wire(self) -> dict[str, object]:
        return {
            "target": self.target,
            "field": self.field,
            "value": self.value,
            "subject": self.subject,
        }


#: Payload values a recipe cannot know before it runs, resolved by the runner against the state
#: the setup actually produced. They are tokens rather than callbacks so a recipe stays inert data
#: that can be serialised into the report beside the outcome it produced.
#:
#: - `$setup_cursor`: the history cursor the last setup transaction returned.
#: - `$timeline_fingerprint`: the current timeline fingerprint, for an honest rebase.
#: - `$stale_fingerprint`: a well-formed fingerprint that binds nothing, for a rebase that must be
#:   refused for being stale rather than for being malformed.
SETUP_CURSOR: Final = "$setup_cursor"
TIMELINE_FINGERPRINT: Final = "$timeline_fingerprint"
STALE_FINGERPRINT: Final = "$stale_fingerprint"
PAYLOAD_TOKENS: Final = (SETUP_CURSOR, TIMELINE_FINGERPRINT, STALE_FINGERPRINT)


@dataclass(frozen=True, slots=True)
class CommandStep:
    """One M25-11 command, named by its accepted backend identifier and its payload."""

    kind: str
    payload: Mapping[str, object]

    def as_wire(self) -> dict[str, object]:
        return {"kind": self.kind, "payload": dict(self.payload)}


@dataclass(frozen=True, slots=True)
class CaseRecipe:
    """What one corpus row needs in order to be executed and judged.

    `setup` runs first and its receipts are retained separately from the operation under test, so a
    command row never credits its setup with the effect it is supposed to be proving.
    """

    case_id: str
    case_class: str
    family: str
    observation: str
    supported: bool = True
    edits: tuple[SnapshotEdit, ...] = ()
    setup: tuple[CommandStep, ...] = ()
    command: CommandStep | None = None
    refusal_code: str | None = None
    refusal_layer: str | None = None
    unsupported_reason: str | None = None
    renders: bool = True
    #: Which composition this row is a case of; see `BASE_NAMES`.
    base: str = CORPUS_BASE
    #: The declared value is the one the base composition already carries, so the row observes how
    #: that value renders rather than a transition to it. Every other row's edits must actually
    #: change the composition -- a recipe that meant to change something and silently did not would
    #: be compared against an unmodified composition and would pass having measured nothing.
    identity_expected: bool = False
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.observation not in OBSERVATION_KINDS:
            raise _reject(f"{self.case_id} names an undeclared observation kind")
        if self.base not in BASE_NAMES:
            raise _reject(f"{self.case_id} names an undeclared base composition")
        if self.refusal_code is not None and self.refusal_code not in REFUSAL_CODES:
            raise _reject(f"{self.case_id} names an undeclared refusal code")
        if (self.refusal_layer is None) != (self.refusal_code is None):
            raise _reject(f"{self.case_id} must name the layer that refuses it")
        if self.refusal_layer is not None and self.refusal_layer not in REFUSAL_LAYERS:
            raise _reject(f"{self.case_id} names an undeclared refusal layer")
        if self.identity_expected and self.refusal_code is not None:
            raise _reject(f"{self.case_id} cannot both refuse and render the identity")
        refusing = self.observation in ("contract_refusal", "command_refusal")
        if refusing != (self.refusal_code is not None):
            raise _reject(f"{self.case_id} must declare exactly one refusal code when it refuses")
        if refusing and self.renders:
            # A refusal that still produced an artifact is the failure a code-only assertion
            # cannot see, so the recipe must not ask for one.
            raise _reject(f"{self.case_id} refuses and therefore renders nothing")
        if (self.observation == "declared_negative") is self.supported:
            raise _reject(f"{self.case_id} disagrees with the corpus about being supported")
        if (self.unsupported_reason is None) is (self.observation == "declared_negative"):
            raise _reject(f"{self.case_id} needs exactly one declared reason when unsupported")
        if self.command is not None and self.observation not in (
            "command_effect",
            "command_refusal",
        ):
            raise _reject(f"{self.case_id} carries a command outside a command observation")

    def as_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "case_class": self.case_class,
            "family": self.family,
            "observation": self.observation,
            "supported": self.supported,
            "edits": [edit.as_wire() for edit in self.edits],
            "setup": [step.as_wire() for step in self.setup],
            "command": None if self.command is None else self.command.as_wire(),
            "refusal_code": self.refusal_code,
            "refusal_layer": self.refusal_layer,
            "unsupported_reason": self.unsupported_reason,
            "renders": self.renders,
            "base": self.base,
            "identity_expected": self.identity_expected,
            "notes": self.notes,
        }


def _media_clip(
    clip_id: str,
    asset_id: str,
    track_id: str,
    *,
    start: int,
    duration: int,
    source_start: int = 0,
) -> dict[str, object]:
    """The accepted media-clip shape, spelled out rather than copied from a resolver."""

    return {
        "clip_id": clip_id,
        "asset_id": asset_id,
        "track_id": track_id,
        "start_frame": start,
        "duration_frames": duration,
        "source_start_frame": source_start,
        "enabled": True,
        "transform": {
            "anchor_x_bp": 5_000,
            "anchor_y_bp": 5_000,
            "position_x_bp": 0,
            "position_y_bp": 0,
            "scale_x_bp": 10_000,
            "scale_y_bp": 10_000,
            "rotation_mdeg": 0,
        },
        "crop": {"left_bp": 0, "top_bp": 0, "right_bp": 0, "bottom_bp": 0},
        "opacity_bp": 10_000,
        "blend": "normal",
        "text": None,
        "transition": {"kind": "none", "duration_frames": 0},
        "effect": {
            "kind": "none",
            "brightness_permille": 0,
            "contrast_permille": 1_000,
            "saturation_permille": 1_000,
        },
    }


def _title_clip(clip_id: str, *, start: int, duration: int) -> dict[str, object]:
    clip = _media_clip(clip_id, PRIMARY_ASSET, TEXT_TRACK, start=start, duration=duration)
    clip["asset_id"] = None
    clip["text"] = {
        "content": "AB",
        "font_asset_id": FONT_ASSET,
        "size_px": 42,
        "weight": 700,
        "style": "normal",
        "align": "center",
        "line_height_bp": 12_000,
        "fill_rgba": [255, 255, 255, 255],
        "background_rgba": [0, 0, 0, 128],
    }
    return clip


#: The accepted group wires the `set_*` commands carry whole, and the first value outside each
#: bound that the composition contract refuses when the command's result is decoded.
_TRANSFORM_WIRE: Final = {
    "anchor_x_bp": 5_000,
    "anchor_y_bp": 5_000,
    "position_x_bp": 1_250,
    "position_y_bp": -3_750,
    "scale_x_bp": 6_000,
    "scale_y_bp": 14_000,
    "rotation_mdeg": 30_000,
}
_CROP_WIRE: Final = {"left_bp": 1_500, "top_bp": 0, "right_bp": 0, "bottom_bp": 0}
_STYLE_WIRE: Final = {
    "font_asset_id": FONT_ASSET,
    "size_px": 96,
    "weight": 400,
    "style": "italic",
    "align": "left",
    "line_height_bp": 20_000,
    "fill_rgba": [16, 200, 64, 255],
    "background_rgba": [0, 0, 0, 255],
}
#: GUARD: six frames, not the four the overlay clip already carries. `set_transition`'s accepted
#: row is the only evidence that the command has a rendered consequence, and a payload that
#: restates the value already in the fixture makes its `before` and `after` the same picture --
#: an accepted-command row proving only that the decoder accepts a no-op. Six is inside the
#: eight-frame upper bound and outside every value the transition family declares, so the pair is
#: measurable and collides with no other row.
_TRANSITION_WIRE: Final = {"kind": "cross_dissolve_v1", "duration_frames": 6}
_EFFECT_WIRE: Final = {
    "kind": "color_adjust_v1",
    "brightness_permille": 250,
    "contrast_permille": 1_400,
    "saturation_permille": 600,
}


def _numeric_recipes(
    family: str,
    domains: Sequence[NumericDomain],
    target: str,
    subject: str = PRIMARY_CLIP,
    *,
    enabling: tuple[SnapshotEdit, ...] = (),
    base: str = CORPUS_BASE,
) -> Iterator[CaseRecipe]:
    """Expand a numeric family from the same domains the corpus expands from.

    The value is taken from the domain rather than restated, so a moved product bound cannot leave
    the corpus row and its recipe describing two different experiments. The leaves are the
    domain's distinct ones (`NumericDomain.distinct_boundaries`), as the corpus expands them.

    `enabling` carries the edits a field needs before it may hold a non-identity value at all. The
    effect family needs one: `composition_contract` refuses a `none` effect that carries anything
    but identity values, so a brightness interior applied to the fixture's default `none` effect is
    refused for the wrong reason and would look like the boundary refusal it is not.
    """

    for domain in domains:
        for leaf, value in domain.distinct_boundaries():
            refuses = leaf in ("underflow", "overflow")
            yield CaseRecipe(
                case_id=f"{family}.{domain.field}.{leaf}",
                case_class="property",
                family=family,
                observation="contract_refusal" if refuses else "render_and_browser",
                edits=(*enabling, SnapshotEdit(target, domain.field, value, subject)),
                refusal_code=INVALID_CONTRACT if refuses else None,
                refusal_layer="snapshot" if refuses else None,
                renders=not refuses,
                base=base,
                identity_expected=leaf == "identity",
            )


def _property(
    case_id: str,
    family: str,
    *,
    edits: tuple[SnapshotEdit, ...] = (),
    setup: tuple[CommandStep, ...] = (),
    refusal_code: str | None = None,
    refusal_layer: str = "snapshot",
    identity_expected: bool = False,
    notes: str | None = None,
    base: str = CORPUS_BASE,
) -> CaseRecipe:
    return CaseRecipe(
        case_id=case_id,
        case_class="property",
        family=family,
        observation="contract_refusal" if refusal_code else "render_and_browser",
        edits=edits,
        setup=setup,
        refusal_code=refusal_code,
        refusal_layer=refusal_layer if refusal_code else None,
        renders=refusal_code is None,
        base=base,
        identity_expected=identity_expected,
        notes=notes,
    )


def _crop_recipes() -> Iterator[CaseRecipe]:
    #: A legal crop and the value that removes the whole image from one side. `10_000` basis points
    #: is the entire edge, which `composition_contract` refuses as "removes the full image".
    legal, full = 1_500, 10_000
    for edge in CROP_EDGES:
        yield _property(
            f"crop.{edge}.absent",
            "crop",
            edits=(SnapshotEdit("clip.crop", edge, 0),),
            identity_expected=True,
        )
        yield _property(
            f"crop.{edge}.legal_nonzero", "crop", edits=(SnapshotEdit("clip.crop", edge, legal),)
        )
        yield _property(
            f"crop.{edge}.source_edge_observed",
            "crop",
            edits=(SnapshotEdit("clip.crop", edge, legal),),
            notes="the surviving source edge must have moved by the cropped amount",
        )
        yield _property(
            f"crop.{edge}.full_removal_refused",
            "crop",
            edits=(SnapshotEdit("clip.crop", edge, full),),
            refusal_code=INVALID_CONTRACT,
        )
    # Each axis separately: one combined "sum" case can be satisfied by whichever axis the
    # implementation happens to check first.
    for axis, edges in (
        ("horizontal", ("left_bp", "right_bp")),
        ("vertical", ("top_bp", "bottom_bp")),
    ):
        yield _property(
            f"crop.sum_{axis}_refused",
            "crop",
            edits=tuple(SnapshotEdit("clip.crop", edge, 5_000) for edge in edges),
            refusal_code=INVALID_CONTRACT,
        )


def _opacity_blend_recipes() -> Iterator[CaseRecipe]:
    for leaf, value in (
        ("transparent", OPACITY_DOMAIN.lower),
        ("half", OPACITY_DOMAIN.interior),
        ("opaque", OPACITY_DOMAIN.upper),
    ):
        yield _property(
            f"opacity_blend.opacity.{leaf}",
            "opacity_blend",
            edits=(SnapshotEdit("clip", "opacity_bp", value, IMAGE_CLIP),),
            notes="two distinguishable layers; a black-on-black fixture passes every mode",
        )
    for leaf, value in (
        ("underflow", OPACITY_DOMAIN.underflow),
        ("overflow", OPACITY_DOMAIN.overflow),
    ):
        yield _property(
            f"opacity_blend.opacity.{leaf}",
            "opacity_blend",
            edits=(SnapshotEdit("clip", "opacity_bp", value, IMAGE_CLIP),),
            refusal_code=INVALID_CONTRACT,
        )
    # Blend rides the overlay clip, whose accepted default is `screen`, so `normal` and `multiply`
    # are both real transitions rather than restatements of what the fixture already carried.
    for mode in BLEND_MODES:
        yield _property(
            f"opacity_blend.blend.{mode}",
            "opacity_blend",
            edits=(SnapshotEdit("clip", "blend", mode, OVERLAY_CLIP),),
            identity_expected=mode == "screen",
        )
    yield _property(
        "opacity_blend.blend.invalid_refused",
        "opacity_blend",
        edits=(SnapshotEdit("clip", "blend", "overlay", OVERLAY_CLIP),),
        refusal_code=INVALID_CONTRACT,
    )


#: The declared text content for each content leaf, and whether the decoder refuses it. `overflow`
#: and `underflow` are the content-length bounds rather than a numeric domain, because
#: `composition_contract` spells them as a length check on the string.
_TEXT_CONTENT: Final = (
    ("identity", "AB", None),
    ("nfc_composed", "é", None),
    ("newline", "A\nB", None),
    ("tab_expansion", "A\tB", None),
    ("underflow", "", INVALID_CONTRACT),
    ("overflow", "A" * 4_097, INVALID_CONTRACT),
    ("control_refused", "A\x07B", INVALID_CONTRACT),
    ("non_nfc_refused", "é", INVALID_CONTRACT),
    ("line_count_overflow", "\n".join("A" for _ in range(65)), INVALID_CONTRACT),
)


#: The three font leaves, each as the content that makes it the case its name states, plus what the
#: packaged font package actually decides about that content.
#:
#: CRITICAL: all three must name a *packaged* font asset. The composition contract refuses a text
#: clip whose `font_asset_id` is not a font asset of the same composition, and the render planner
#: refuses one outside `fallback_order` (`font_asset_unsupported`), so a row that renamed the asset
#: to an unpackaged identity would be refused for a reason its own family cannot observe. The leaf
#: is therefore carried by the *content*, which is what `derive_text` can actually state.
_TEXT_FONT: Final = (
    (
        "qualified",
        "Ag",
        "content restricted to glyphs the packaged Noto Sans face provides, so the exact "
        "weight/style face resolves and draws",
    ),
    (
        "fallback",
        "Åß",
        "coverage is resolved by walking the packaged fallback order and never from a system "
        "font; the packaged manifest declares exactly one family, so this row cannot observe a "
        "second-family substitution and does not claim to",
    ),
    (
        "unsupported_glyph",
        "A中",
        "U+4E2D is outside every packaged coverage profile, so the accepted behaviour is the "
        "explicit font_glyph_unsupported refusal when the plan is bound, never a silent "
        "substitution",
    ),
)


def _text_recipes() -> Iterator[CaseRecipe]:
    for leaf, content, refusal in _TEXT_CONTENT:
        yield _property(
            f"text.content.{leaf}",
            "text",
            edits=(SnapshotEdit("clip.text", "content", content, TITLE_CLIP),),
            refusal_code=refusal,
        )
    for leaf, content, note in _TEXT_FONT:
        # The unsupported-glyph leaf is a refusal, and it is refused by the font resolver rather
        # than by the composition decoder: the snapshot is perfectly valid, and the coverage check
        # runs where the render plan's font facts are bound. Declaring it as a rendering row said
        # the product draws a glyph it has no face for.
        refusal = FONT_GLYPH_UNSUPPORTED if leaf == "unsupported_glyph" else None
        yield _property(
            f"text.font.{leaf}",
            "text",
            edits=(SnapshotEdit("clip.text", "content", content, TITLE_CLIP),),
            refusal_code=refusal,
            refusal_layer="font_binding",
            notes=note,
        )
    yield from _numeric_recipes(
        "text", (TEXT_SIZE_DOMAIN, TEXT_LINE_HEIGHT_DOMAIN), "clip.text", TITLE_CLIP
    )
    for weight in (400, 700):
        yield _property(
            f"text.weight.{weight}",
            "text",
            edits=(SnapshotEdit("clip.text", "weight", weight, TITLE_CLIP),),
            identity_expected=weight == 700,
        )
    yield _property(
        "text.weight.unsupported_refused",
        "text",
        edits=(SnapshotEdit("clip.text", "weight", 500, TITLE_CLIP),),
        refusal_code=INVALID_CONTRACT,
    )
    for style in ("normal", "italic"):
        yield _property(
            f"text.style.{style}",
            "text",
            edits=(SnapshotEdit("clip.text", "style", style, TITLE_CLIP),),
            identity_expected=style == "normal",
        )
    for align in ("left", "center", "right"):
        yield _property(
            f"text.align.{align}",
            "text",
            edits=(SnapshotEdit("clip.text", "align", align, TITLE_CLIP),),
            identity_expected=align == "center",
        )
    for leaf, value, refusal in (
        ("identity", [255, 255, 255, 255], None),
        ("interior", [16, 200, 64, 255], None),
        ("channel_overflow_refused", [256, 0, 0, 255], INVALID_CONTRACT),
    ):
        yield _property(
            f"text.fill_rgba.{leaf}",
            "text",
            edits=(SnapshotEdit("clip.text", "fill_rgba", value, TITLE_CLIP),),
            refusal_code=refusal,
            identity_expected=leaf == "identity",
        )
    for leaf, value, refusal in (
        ("absent", [0, 0, 0, 0], None),
        ("present", [0, 0, 0, 255], None),
        ("channel_overflow_refused", [0, 0, 0, 256], INVALID_CONTRACT),
    ):
        yield _property(
            f"text.background_rgba.{leaf}",
            "text",
            edits=(SnapshotEdit("clip.text", "background_rgba", value, TITLE_CLIP),),
            refusal_code=refusal,
        )


#: Each transition leaf, its declared transition wire, and the code the decoder raises for it.
#: A cross-dissolve shorter than one frame or longer than its clip is invalid, and so is a
#: participant that cannot take part.
_TRANSITION: Final = (
    ("none_identity", {"kind": "none", "duration_frames": 0}, None),
    ("cross_dissolve_legal", {"kind": "cross_dissolve_v1", "duration_frames": 4}, None),
    ("first_included_frame", {"kind": "cross_dissolve_v1", "duration_frames": 4}, None),
    ("interior_frame", {"kind": "cross_dissolve_v1", "duration_frames": 4}, None),
    ("last_included_frame", {"kind": "cross_dissolve_v1", "duration_frames": 4}, None),
    ("exclusive_end", {"kind": "cross_dissolve_v1", "duration_frames": 4}, None),
    ("alpha_progression", {"kind": "cross_dissolve_v1", "duration_frames": 8}, None),
    ("duration_lower", {"kind": "cross_dissolve_v1", "duration_frames": 1}, None),
    ("duration_upper", {"kind": "cross_dissolve_v1", "duration_frames": 8}, None),
    ("duration_underflow", {"kind": "cross_dissolve_v1", "duration_frames": 0}, INVALID_CONTRACT),
    ("duration_overflow", {"kind": "cross_dissolve_v1", "duration_frames": 999}, INVALID_CONTRACT),
    ("participant_invalid", {"kind": "cross_dissolve_v1", "duration_frames": 4}, INVALID_CONTRACT),
)


def _transition_recipes() -> Iterator[CaseRecipe]:
    for leaf, wire, refusal in _TRANSITION:
        # `participant_invalid` puts the transition on the first clip of a track, which has nothing
        # to dissolve from; every other leaf puts it on the second.
        subject = PRIMARY_CLIP if leaf == "participant_invalid" else OVERLAY_CLIP
        yield _property(
            f"transition.{leaf}",
            "transition",
            edits=tuple(
                SnapshotEdit("clip.transition", key, value, subject) for key, value in wire.items()
            ),
            refusal_code=refusal,
            # The overlay clip already carries a four-frame cross dissolve, so the rows that
            # observe where that dissolve begins, ends and progresses are reading the accepted
            # fixture rather than switching it on.
            identity_expected=leaf
            in (
                "cross_dissolve_legal",
                "first_included_frame",
                "interior_frame",
                "last_included_frame",
                "exclusive_end",
            ),
        )


def _effect_recipes() -> Iterator[CaseRecipe]:
    for kind in ("none", "color_adjust_v1"):
        yield _property(
            f"effect.kind.{kind}",
            "effect",
            edits=(SnapshotEdit("clip.effect", "kind", kind),),
            identity_expected=kind == "none",
        )
    yield _property(
        "effect.kind.none_identity_refused",
        "effect",
        edits=(
            SnapshotEdit("clip.effect", "kind", "none"),
            SnapshotEdit("clip.effect", "brightness_permille", 250),
        ),
        refusal_code=INVALID_CONTRACT,
    )
    yield from _numeric_recipes(
        "effect",
        EFFECT_DOMAINS,
        "clip.effect",
        enabling=(SnapshotEdit("clip.effect", "kind", "color_adjust_v1"),),
    )


#: Each refused timing leaf and the code the decoder raises for it, quoted from
#: `composition_contract`: a negative member is `negative_timestamp`, and a landmark table that
#: does not begin at zero, repeats, goes backwards or leaves the admitted range is `invalid_timing`.
#: These rows write a literal table onto the primary asset; the decoder refuses the table itself,
#: before any source is bound, so the table need not be one a probe could produce.
_TIMING_REFUSALS: Final = (
    ("negative_pts", NEGATIVE_TIMESTAMP),
    ("missing_landmark", INVALID_TIMING),
    ("duplicate_landmark", INVALID_TIMING),
    ("nonmonotonic_landmark", INVALID_TIMING),
    ("source_range_unavailable", SOURCE_RANGE_UNAVAILABLE),
)

#: The rendered timing rows: each is a composition the product can bind and render, whose source
#: mapping differs from the base's by construction (B-58). The base's primary is 72 frames of
#: 24 fps source, one output frame each; a clip of 48 output frames therefore reads 48 of them.
#: - `cfr_24` and `last_included_frame` are the two identity statements about that source: the
#:   base's own clip, and the clip that runs exactly to the source's last frame (source 24..71).
#: - `source_rate_different` and `vfr_unequal_intervals` swap the primary for `vid-timing`, whose
#:   first 36 frames are 12 fps and whose last 48 alternate 640 and 384 ticks; a clip from source
#:   frame 0 reads the constant-rate part and one from frame 36 reads the unequal part to its end.
#: - `source_in` starts the clip at source frame 4; `source_out` ends it at the source's last
#:   frame after 44 output frames (source 28..71); `exclusive_end` ends the clip fourteen output
#:   frames short of the timeline, so frames 34..47 show no primary. Every primary duration from
#:   35 to 47 is already some end-trim, history or audio row's composition (`edge_trim.*`,
#:   `embedded_audio.*_owner_range`, `history_currentness.old_output_accepted_label`), and two
#:   rows must not run one experiment.
#: - `gap` leaves frames 20..27 without a primary between two clips that continue the source
#:   across the hole (the overlay's cross dissolve at 12..15 needs a lower layer, so the hole
#:   cannot be at the start); `cut` meets a second primary clip at frame 24 that restarts the
#:   source from its first frame.
_TIMING_COMPOSITIONS: Final[dict[str, tuple[SnapshotEdit, ...]]] = {
    "cfr_24": (SnapshotEdit("clip", "source_start_frame", 0),),
    "source_rate_different": (SnapshotEdit("clip", "asset_id", TIMING_ASSET),),
    "vfr_unequal_intervals": (
        SnapshotEdit("clip", "asset_id", TIMING_ASSET),
        SnapshotEdit("clip", "source_start_frame", TIMING_VFR_START_FRAME),
    ),
    "source_in": (SnapshotEdit("clip", "source_start_frame", 4),),
    "source_out": (
        SnapshotEdit("clip", "source_start_frame", 28),
        SnapshotEdit("clip", "duration_frames", 44),
    ),
    "gap": (
        SnapshotEdit("clip", "duration_frames", 20),
        SnapshotEdit(
            "clips",
            "append",
            {
                "clip_id": "clip-main-tail",
                "start_frame": 28,
                "duration_frames": 20,
                "source_start_frame": 28,
            },
        ),
    ),
    "cut": (
        SnapshotEdit("clip", "duration_frames", 24),
        SnapshotEdit(
            "clips",
            "append",
            {
                "clip_id": "clip-main-tail",
                "start_frame": 24,
                "duration_frames": 24,
                "source_start_frame": 0,
            },
        ),
    ),
    "last_included_frame": (SnapshotEdit("clip", "source_start_frame", 24),),
    "exclusive_end": (SnapshotEdit("clip", "duration_frames", 34),),
}


#: `source_range_unavailable` is refused for a different reason from every other timing leaf: the
#: landmark table is legal and the *clip* asks for source the asset does not have. The accepted
#: check is `source_start_frame >= source_frame_count`, and the primary asset declares 72 source
#: frames, so the first refused offset is 72 itself. A clip that merely *ends* past the source is
#: admitted here; declaring 60 would have described a check the contract does not make.
_SOURCE_RANGE_EDIT: Final = SnapshotEdit("clip", "source_start_frame", 72, PRIMARY_CLIP)


def _timing_recipes() -> Iterator[CaseRecipe]:
    for leaf, composition in _TIMING_COMPOSITIONS.items():
        yield _property(
            f"timing.{leaf}",
            "timing",
            edits=composition,
            identity_expected=leaf == "cfr_24",
            notes="a composition the product binds and renders; the mapping follows the table",
        )
    for leaf, refusal in _TIMING_REFUSALS:
        edits: tuple[SnapshotEdit, ...] = (SnapshotEdit("asset.landmarks", leaf, True),)
        if leaf == "source_range_unavailable":
            edits = (*edits, _SOURCE_RANGE_EDIT)
        yield _property(
            f"timing.{leaf}",
            "timing",
            edits=edits,
            refusal_code=refusal,
            notes="literal source landmark table; never a value copied from resolve_composition",
        )


#: The output profile leaves. Every refusal here is `unsupported_output_profile`, because the
#: profile is decoded as one exact object rather than field by field.
_OUTPUT: Final = (
    ("dimensions_square", ("width", 64), ("height", 64), None),
    ("dimensions_nonsquare", ("width", 96), ("height", 64), None),
    ("dimensions_minimum", ("width", 16), ("height", 16), None),
    ("dimensions_maximum", ("width", 1_920), ("height", 1_080), None),
    # Odd dimensions pass the integer-range check and are refused by the profile's parity rule.
    ("dimensions_odd_refused", ("width", 65), ("height", 65), UNSUPPORTED_OUTPUT_PROFILE),
    # A dimension outside `[16, 1920]` or `[16, 1080]` never reaches the profile rule: the closed
    # integer bound refuses it first, as `invalid_contract`. Declaring the profile code here would
    # have been a guess about which check fires, and the two are different authorities.
    ("dimensions_underflow_refused", ("width", 14), ("height", 16), INVALID_CONTRACT),
    ("dimensions_overflow_refused", ("width", 1_922), ("height", 1_080), INVALID_CONTRACT),
    ("dimensions_area_overflow_refused", ("width", 1_920), ("height", 1_082), INVALID_CONTRACT),
)

_OUTPUT_SCALAR: Final = (
    ("color_policy_bt709_sdr_limited", "color_policy", "bt709_sdr_limited_v1", None),
    ("color_policy_refused", "color_policy", "bt2020_pq_v1", UNSUPPORTED_OUTPUT_PROFILE),
    # The accepted 48-frame extent, restated: shortening it would put the fixture's own clips
    # past the end of the output and refuse for `source_range_unavailable` instead.
    ("frame_grid_exact_24", "duration_frames", 48, None),
    ("container_mp4", "container", "mp4", None),
    ("container_refused", "container", "webm", UNSUPPORTED_OUTPUT_PROFILE),
    ("video_codec_h264", "video_codec", "h264", None),
    ("video_codec_refused", "video_codec", "vp9", UNSUPPORTED_OUTPUT_PROFILE),
    ("pixel_format_yuv420p", "pixel_format", "yuv420p", None),
)


def _output_recipes() -> Iterator[CaseRecipe]:
    for leaf, width, height, refusal in _OUTPUT:
        yield _property(
            f"output.{leaf}",
            "output",
            edits=(
                SnapshotEdit("output", width[0], width[1]),
                SnapshotEdit("output", height[0], height[1]),
            ),
            refusal_code=refusal,
        )
    for leaf, field_name, value, refusal in _OUTPUT_SCALAR:
        yield _property(
            f"output.{leaf}",
            "output",
            edits=(SnapshotEdit("output", field_name, value),),
            refusal_code=refusal,
            identity_expected=refusal is None,
        )
    yield _property(
        "output.pixel_aspect_identity",
        "output",
        edits=(SnapshotEdit("output", "pixel_aspect", {"num": 1, "den": 1}),),
        identity_expected=True,
    )
    yield _property(
        "output.pixel_aspect_refused",
        "output",
        edits=(SnapshotEdit("output", "pixel_aspect", {"num": 4, "den": 3}),),
        refusal_code=UNSUPPORTED_OUTPUT_PROFILE,
    )


def _trim(clip_id: str, edge: str, delta: int) -> CommandStep:
    return CommandStep("trim_clip", {"clip_id": clip_id, "edge": edge, "delta_frames": delta})


#: Split the primary clip at source frame 12. Every setup that needs two clips on the primary
#: track starts here, and the offset is held stable rather than tuned per row: several rows'
#: declared audio and source-mapping claims are written against this cut.
_SPLIT_PRIMARY: Final = CommandStep(
    "split_clip",
    {"clip_id": PRIMARY_CLIP, "at_offset_frames": 12, "right_clip_id": SPLIT_CLIP},
)

#: The same split at the last frame the primary clip covers. Two rows need the cut on the far side
#: of the second declared audio burst, which the source puts at sample 48000, output frame 24.
_SPLIT_PRIMARY_LATE: Final = CommandStep(
    "split_clip",
    {"clip_id": PRIMARY_CLIP, "at_offset_frames": 47, "right_clip_id": SPLIT_CLIP},
)

#: Each embedded-audio leaf, the edits and the setup that make it the case its name states. The
#: initial release has one audio path, `primary_embedded_follow_video_v1`: audio follows the
#: file-backed primary VIDEO and is never independently edited or mixed, so every row here is an
#: observation of that follow rather than of an audio control.
#:
#: CRITICAL: nine of these leaves carried neither an edit nor a setup and so ran the untouched base
#: composition -- one experiment counted nine times, which is the defect the post-closeout review
#: named. Each now reaches its own condition. Three of them still *expect* exactly what the base
#: expects, and that is the point of those three rather than an oversight: an overlay that carries
#: audio, an overlay that reuses the primary's own source, and a visual-only edit must all leave the
#: output audio bit-identical to the audible base, so their evidence is an unchanged expectation
#: measured against a changed composition. `tests/test_m25_20_conformance_cases.py` pins that as a
#: named group; never satisfy it by inventing an audio difference those rows must not have.
_EMBEDDED_AUDIO: Final[
    tuple[tuple[str, tuple[SnapshotEdit, ...], tuple[CommandStep, ...]], ...]
] = (
    ("primary_audible", (), ()),
    # LIMITATION, stated rather than papered over: this row cannot reach its own literal subject
    # from the corpus media, and what it observes instead is named here so nobody reads it as the
    # case its identifier promises.
    #
    # The literal case needs a primary clip whose source carries no embedded audio. Every video
    # source the render stage claims declares `present_bound`, because that is what the real media
    # carries; an image or title clip on a primary_video track is refused by the composition
    # contract ("clip asset kind does not match its track"); and editing an asset's declared audio
    # field would describe media that does not exist, which is the category error the corpus base
    # was just corrected to remove. An ownerless *interval* is reachable, but only the interior
    # form is expressible -- `derive_silences` runs its chain from the first onset to the last
    # owner's span end, so a hole before the first owner or after the last one is claimed by
    # nobody -- and the interior form is `silence_gap`.
    #
    # What the row does observe is the nearest reachable property of the same policy, and it is one
    # no other row states: two primary owners drawn from two *different* sources, so each owner's
    # audio is placed through its own source's mapping. The second owner is the twenty-four frame
    # overlay source, whose own first burst then lands mid-timeline as `owner1_burst_0` -- a
    # position no single-source composition can produce. The clip is trimmed to twenty-four frames
    # before the swap because the shorter source cannot fill thirty-six.
    (
        "primary_without_audio",
        (),
        (
            _SPLIT_PRIMARY,
            _trim(SPLIT_CLIP, "end", -12),
            CommandStep(
                "replace_clip_asset",
                {"clip_id": SPLIT_CLIP, "asset_id": OVERLAY_ASSET, "source_start_frame": 0},
            ),
        ),
    ),
    # The tail trim is not decoration: the cut itself stays at frame 12, but without it this row
    # states exactly the audio picture `roll_owner_range` reaches, and the roll can only put its
    # boundary on a declared source landmark, so 12 is the only place either row can cut ahead of
    # the second burst. Shortening the tail leaves the cut where it is and gives the row its own
    # silent chain.
    ("hard_cut", (), (_SPLIT_PRIMARY, _trim(SPLIT_CLIP, "end", -5))),
    # The hole has to swallow a declared burst, or the row proves nothing: a gap over a stretch the
    # source is silent through anyway is indistinguishable from the abutting case, since "silent
    # because no clip covers it" and "silent because the covering clip's source is silent" are the
    # same observation. The source declares bursts at samples 0, 48000 and 96000, so the hole runs
    # from output frame 20 to 46 and the second burst's output position, frame 24, is inside it.
    #
    # GUARD: the hole must never cover output frames 12 to 15. The overlay clip's cross dissolve
    # lives there and `composition_contract` refuses a dissolve with no lower layer for its full
    # duration -- and every intermediate state a setup passes through is decoded too, so a step
    # order that opens the hole before the primary clip is extended back over those frames is
    # refused even when the final state would have been admitted.
    ("silence_gap", (), (_SPLIT_PRIMARY_LATE, _trim(PRIMARY_CLIP, "end", -27))),
    # Disabling the one primary clip is refused outright -- `composition_contract` will not admit
    # a cross dissolve with no lower layer under it for its full duration -- so the clip is split
    # and only the tail is disabled. That is also the better case rather than a workaround: an
    # empty composition asserts an absence, and a run that rendered nothing at all produces the
    # same absence, so no landmark can tell "correctly silent" from "measured nothing". Disabling
    # a clip that has a contributing clip before it isolates what the disabled one contributes --
    # neither the source landmark it covers nor any sound -- while its neighbours must still
    # contribute both.
    #
    # GUARD: the disabled clip must not cover the overlay's cross dissolve (frames 12 to 15) or
    # the composition is refused for the missing lower layer, and it must not be the *only* clip
    # left standing at the start of the track or the row degenerates into `edge_trim_start_in`,
    # which reaches exactly the same picture by trimming instead of disabling.
    (
        "primary_disabled",
        (),
        (
            _SPLIT_PRIMARY,
            CommandStep(
                "split_clip",
                {
                    "clip_id": SPLIT_CLIP,
                    "at_offset_frames": 35,
                    "right_clip_id": SPLIT_TAIL_CLIP,
                },
            ),
            CommandStep("set_clip_enabled", {"clip_id": SPLIT_TAIL_CLIP, "enabled": False}),
        ),
    ),
    # No edit, and that is the point rather than an omission. `embedded_audio` is a property of the
    # *source*, and a claimed overlay source carries bound audio like any other; the exclusion is a
    # per-clip decision `authoring_derivative_source` makes from track kind and enablement, which no
    # asset declaration can express. The base therefore already declares what this row needs, and
    # the row asserts that the output takes none of it.
    #
    # GUARD: do not "restore" the two edits this row used to carry. They set the asset's
    # `embedded_audio` and `source_sample_count` to the values the base now declares, so they
    # changed nothing and the row ran the untouched base while looking like it had set something up.
    ("overlay_excluded", (), ()),
    (
        "overlay_same_source_no_doubling",
        (SnapshotEdit("clip", "asset_id", PRIMARY_ASSET, OVERLAY_CLIP),),
        (),
    ),
    ("trim_owner_range", (), (_trim(PRIMARY_CLIP, "end", -4),)),
    # Slip has to land on a declared source landmark, and it has to move the clip the audio policy
    # names as the owner. `slip_clip` with delta 2 was refused outright, and slipping the *right*
    # half of the split would have left the owner untouched.
    (
        "slip_owner_range",
        (),
        (_SPLIT_PRIMARY, CommandStep("slip_clip", {"clip_id": PRIMARY_CLIP, "delta_frames": 12})),
    ),
    # A ripple with one clip in scope is indistinguishable from a plain trim, so the split runs
    # first and the ripple actually moves the successor.
    (
        "ripple_owner_range",
        (),
        (
            _SPLIT_PRIMARY,
            CommandStep(
                "ripple_trim",
                {
                    "clip_id": PRIMARY_CLIP,
                    "edge": "end",
                    "delta_frames": -2,
                    "scope_track_ids": [PRIMARY_TRACK],
                },
            ),
        ),
    ),
    # Roll needs adjacent clips on one track; the declared pair named clips on two different tracks
    # and was refused as `invalid_command`. The direction matters as much as the adjacency: a roll
    # only becomes audible when the boundary crosses a declared burst, so it has to move the shared
    # edit point from source landmark 47 *back* to 12, which is ahead of the second burst's output
    # position at frame 24. That burst then changes owner, which is the whole claim of the row.
    # Rolling the other way leaves both bursts with the first owner and states exactly what an
    # untouched primary states.
    (
        "roll_owner_range",
        (),
        (
            _SPLIT_PRIMARY_LATE,
            CommandStep(
                "roll_edit",
                {"left_clip_id": PRIMARY_CLIP, "right_clip_id": SPLIT_CLIP, "delta_frames": -35},
            ),
        ),
    ),
    # Undo and redo need a committed edit to be *inside* rather than at the end of history: an undo
    # of the only transaction returns the composition to the base, and the row would then be the
    # base composition again. The first trim stays, the second is undone (and redone).
    (
        "undo_owner_range",
        (),
        (
            _trim(PRIMARY_CLIP, "end", -6),
            _trim(PRIMARY_CLIP, "end", -2),
            CommandStep("undo", {"history_cursor": SETUP_CURSOR}),
        ),
    ),
    (
        "redo_owner_range",
        (),
        (
            _trim(PRIMARY_CLIP, "end", -6),
            _trim(PRIMARY_CLIP, "end", -2),
            CommandStep("undo", {"history_cursor": SETUP_CURSOR}),
            CommandStep("redo", {"history_cursor": SETUP_CURSOR}),
        ),
    ),
    (
        "visual_only_mapping_preserved",
        (),
        (
            CommandStep(
                "set_opacity_blend",
                {"clip_id": IMAGE_CLIP, "opacity_bp": 6_000, "blend": "normal"},
            ),
        ),
    ),
)


def _embedded_audio_recipes() -> Iterator[CaseRecipe]:
    for leaf, edits, setup in _EMBEDDED_AUDIO:
        yield _property(
            f"embedded_audio.{leaf}",
            "embedded_audio",
            edits=edits,
            setup=setup,
            notes="the follow invariant is asserted on every row, visual-only edits included",
        )


#: Edge trim rides the existing `trim_clip` command; these are properties of that command, never
#: two extra command identifiers. Each leaf carries the step sequence that reaches its own edge
#: condition, and the last step is always the trim the row is about.
#:
#: CRITICAL: four of these were declared as a single trim of plus or minus one or two frames and
#: were refused outright by the real decoder, so the rows reached no composition at all and nothing
#: in the suite noticed. Two separate rules did it. A start-edge trim and a slip move the clip's
#: `source_start_frame`, and the primary asset only declares landmarks at
#: `PRIMARY_SOURCE_LANDMARKS`, so any shift that does not land on one of them is
#: `source_range_unavailable` ("source shift is not exactly representable") -- which is why the
#: start rows move by twelve, not by two. And the primary clip already fills the whole 48-frame
#: output, so an outward trim of either edge has to be given room by a shortening trim first, or the
#: result violates the composition contract's own extent rule. Never "simplify" one of these back to
#: a bare one-frame trim.
_EDGE_TRIM_PROPERTY: Final[tuple[tuple[str, tuple[CommandStep, ...]], ...]] = (
    ("edge_trim_start_in", (_trim(PRIMARY_CLIP, "start", 12),)),
    (
        "edge_trim_start_out",
        (
            _trim(PRIMARY_CLIP, "end", -5),
            _trim(PRIMARY_CLIP, "start", 12),
            _trim(PRIMARY_CLIP, "start", -12),
        ),
    ),
    ("edge_trim_end_in", (_trim(PRIMARY_CLIP, "end", -2),)),
    ("edge_trim_end_out", (_trim(PRIMARY_CLIP, "end", -5), _trim(PRIMARY_CLIP, "end", 2))),
    ("edge_trim_bounds_timing", (_trim(PRIMARY_CLIP, "end", -1),)),
    (
        "edge_trim_transition_audio",
        (_trim(PRIMARY_CLIP, "end", -3), _trim(PRIMARY_CLIP, "start", 12)),
    ),
)

#: Each edge-trim observation leaf, the setup that reaches the state it observes, and the code its
#: refusal carries. The nine accepted leaves carried no setup at all and every one of them ran the
#: untouched base composition; each now commits a distinct trim, so a row that claims an invariant
#: is claiming it about a timeline that actually moved.
#:
#: The two draft leaves deserve a word: a draft is live UI state and is never a committed command,
#: so what these rows can assert in the backend is that the composition equals the committed state
#: the draft was started from. That is only worth asserting when the committed state is *not* the
#: base -- otherwise "the draft changed nothing" and "the row did nothing" are the same picture.
#: Each entry is the leaf, the setup that reaches the state it observes, the code its refusal
#: carries, and -- for a refusing leaf -- the exact command the decoder must refuse.
#:
#: CRITICAL: a refusing leaf must name that command. Both refusals here were written as a code with
#: no payload, and `nle_semantic_qualify._evaluate_recipe` routes a command-layer refusal to
#: `_command_refusal_row`, which has nothing to run and blocks the row. Each then asserted "the
#: command layer refuses this" without ever naming the thing to be refused, which is the same
#: vacuity as a row that renders the untouched fixture: it cannot fail for its stated reason.
_EDGE_TRIM_OBSERVATION: Final[
    tuple[tuple[str, tuple[CommandStep, ...], str | None, CommandStep | None], ...]
] = (
    ("opposite_edge_invariant", (_trim(PRIMARY_CLIP, "end", -9),), None, None),
    # The trim lands on the *last* primary clip and the untouched neighbour is the one before it.
    # Trimming the first clip instead is invisible in this family: only the final owner's span end
    # reaches the silent chain, every earlier gap ending at the next declared burst instead, so the
    # row would state exactly what `embedded_audio.roll_owner_range` states. Seven frames is chosen
    # to keep that span end clear of every other multi-owner row's.
    (
        "non_target_clip_invariant",
        (_SPLIT_PRIMARY, _trim(SPLIT_CLIP, "end", -7)),
        None,
        None,
    ),
    (
        "source_pts_boundary",
        (_trim(PRIMARY_CLIP, "end", -9), _trim(PRIMARY_CLIP, "start", 12)),
        None,
        None,
    ),
    (
        "incoming_transition_removed",
        (
            # The trim first, so the removal is observed against a timeline that has already
            # moved: with only the removal the row's composition is the same one
            # `transition.none_identity` already renders, and it would pass on that row's picture.
            _trim(PRIMARY_CLIP, "end", -20),
            CommandStep(
                "set_transition",
                {"clip_id": OVERLAY_CLIP, "transition": {"kind": "none", "duration_frames": 0}},
            ),
        ),
        None,
        None,
    ),
    # An end trim that leaves the clip shorter than the dissolve it already carries. This is the
    # edge-trim family's own invalid transition and not a restatement of the transition family's:
    # `transition.duration_overflow` declares an over-long duration on a clip that never moved,
    # while here a legal four-frame dissolve becomes invalid because the clip shrank under it. The
    # boundary is exact -- the overlay clip is twelve frames long, so a nine-frame shortening
    # leaves three and is refused, and an eight-frame shortening leaves four and is admitted.
    #
    # GUARD: it has to be the *end* edge. A start-edge trim sets the trimmed clip's transition to
    # `none` on the way through (that is what `incoming_transition_removed` observes), so no start
    # trim can ever leave a transition invalid and the row would be refused for the wrong rule or
    # not at all.
    (
        "invalid_transition_refused",
        (),
        INVALID_CONTRACT,
        _trim(OVERLAY_CLIP, "end", -9),
    ),
    ("embedded_audio_follow", (_trim(PRIMARY_CLIP, "end", -10),), None, None),
    ("draft_cancelled", (_trim(PRIMARY_CLIP, "end", -11),), None, None),
    ("draft_no_op", (_trim(PRIMARY_CLIP, "end", -12),), None, None),
    # A refused draft is backend-observable after all: the shipped gesture submits a draft as an
    # ordinary `trim_clip` transaction (`trimCommandForDraft` in the timeline component), so the
    # draft the contract refuses is the trim whose result the composition contract will not admit.
    # The canonical one is the drag that consumes the clip: forty-eight frames off a forty-eight
    # frame clip leaves nothing, and a clip must have at least one frame. Forty-seven is admitted,
    # which is what makes the refusal the collapse rather than a bound on the delta.
    #
    # It rides the image clip deliberately. Collapsing the primary clip is refused too, but for the
    # overlay dissolve that then has no lower layer -- a second rule, and the row would no longer
    # be able to say which one refused it.
    (
        "draft_refused",
        (),
        INVALID_CONTRACT,
        _trim(IMAGE_CLIP, "end", -48),
    ),
    (
        "undo",
        (
            _trim(PRIMARY_CLIP, "end", -13),
            _trim(PRIMARY_CLIP, "end", -2),
            CommandStep("undo", {"history_cursor": SETUP_CURSOR}),
        ),
        None,
        None,
    ),
    (
        "redo",
        (
            _trim(PRIMARY_CLIP, "end", -14),
            _trim(PRIMARY_CLIP, "end", -2),
            CommandStep("undo", {"history_cursor": SETUP_CURSOR}),
            CommandStep("redo", {"history_cursor": SETUP_CURSOR}),
        ),
        None,
        None,
    ),
)


def _edge_trim_recipes() -> Iterator[CaseRecipe]:
    for leaf, setup in _EDGE_TRIM_PROPERTY:
        yield _property(
            f"edge_trim.{leaf}",
            "edge_trim",
            setup=setup,
            notes="both shortening and legal extension, with spare source handles at both edges",
        )
    for leaf, setup, refusal, command in _EDGE_TRIM_OBSERVATION:
        # An edge-trim refusal is a refusal of the *draft*, decided by the timeline command
        # decoder. The composition it would have produced is never built, so the snapshot decoder
        # accepts the untouched fixture and can say nothing about the row -- which is why a
        # refusing leaf is a `command_refusal` carrying the payload, never a `contract_refusal`.
        if refusal is None:
            yield _property(f"edge_trim.{leaf}", "edge_trim", setup=setup)
            continue
        if command is None:
            raise _reject(f"edge_trim.{leaf} refuses a command it does not name")
        yield CaseRecipe(
            case_id=f"edge_trim.{leaf}",
            case_class="property",
            family="edge_trim",
            observation="command_refusal",
            setup=setup,
            command=command,
            refusal_code=refusal,
            refusal_layer="command",
            renders=False,
        )


def _opacity(value: int) -> CommandStep:
    """One committed visual edit, at a value no other row commits."""

    return CommandStep(
        "set_opacity_blend", {"clip_id": IMAGE_CLIP, "opacity_bp": value, "blend": "normal"}
    )


#: The currentness leaves, the history position each row's name states, and the exact code each
#: refusal carries. `stale_snapshot` is raised when a fingerprint does not bind its snapshot;
#: `source_replaced` when a source generation moved.
#:
#: CRITICAL: the eight accepted leaves carried no setup, so all eight ran the untouched base
#: composition from history position zero -- a row about an inverse, a replay or an explicit rebase
#: that never applied one. Each now reaches the position it names. Two of them deliberately stay on
#: the base composition: a selection is not timeline state and an unknown job outcome is a fact
#: about a render job, so neither has a composition to reach, and `selection_invariant` in
#: particular would stop being an invariant if it were given one.
_HISTORY_CURRENTNESS: Final[tuple[tuple[str, tuple[CommandStep, ...], str | None], ...]] = (
    ("selection_invariant", (CommandStep("select_clips", {"clip_ids": [IMAGE_CLIP]}),), None),
    (
        "lock_invariant",
        (CommandStep("set_track_locked", {"track_id": OVERLAY_TRACK, "locked": True}),),
        None,
    ),
    # An inverse returns the composition to what it was, so a row whose whole history is one edit
    # and its undo is the base composition again. The committed effect ahead of it is what makes
    # "returned to the previous state" a statement with content.
    (
        "inverse",
        (
            CommandStep("set_effect", {"clip_id": PRIMARY_CLIP, "effect": _EFFECT_WIRE}),
            _opacity(7_000),
            CommandStep("undo", {"history_cursor": SETUP_CURSOR}),
        ),
        None,
    ),
    (
        "replay",
        (
            _opacity(7_500),
            CommandStep("undo", {"history_cursor": SETUP_CURSOR}),
            CommandStep("redo", {"history_cursor": SETUP_CURSOR}),
        ),
        None,
    ),
    (
        "explicit_rebase",
        (
            CommandStep(
                "rebase_transaction",
                {
                    "base_timeline_fingerprint": TIMELINE_FINGERPRINT,
                    "commands": [
                        {
                            "kind": "set_opacity_blend",
                            "payload": {
                                "clip_id": IMAGE_CLIP,
                                "opacity_bp": 8_000,
                                "blend": "normal",
                            },
                        }
                    ],
                },
            ),
        ),
        None,
    ),
    ("stale_snapshot_refused", (), STALE_SNAPSHOT),
    ("source_replaced_refused", (), SOURCE_REPLACED),
    ("output_replaced_refused", (), STALE_SNAPSHOT),
    # The label is about an artifact the timeline has since moved past, so the timeline has to have
    # moved: without the trim the row would be labelling an output that is still current.
    ("old_output_accepted_label", (_trim(PRIMARY_CLIP, "end", -7),), None),
    ("unknown_outcome_pending", (), None),
    ("unknown_outcome_failed", (), None),
)


def _history_currentness_recipes() -> Iterator[CaseRecipe]:
    for leaf, setup, refusal in _HISTORY_CURRENTNESS:
        yield _property(
            f"history_currentness.{leaf}",
            "history_currentness",
            setup=setup,
            refusal_code=refusal,
            # Currentness is checked when a plan is bound, not when a snapshot is decoded: the
            # render planner raises `stale_snapshot` and `source_replaced`, and a snapshot that is
            # internally consistent passes the decoder no matter how stale its binding is.
            refusal_layer="currentness",
            notes=(
                "an unknown outcome carries a reconciliation fact and is attributed to neither "
                "acceptance nor rejection; it is a fact about a render job rather than about a "
                "composition, so this row deliberately runs the accepted base and an edit here "
                "would fabricate a difference the outcome does not have"
                if leaf.startswith("unknown_outcome")
                else None
            ),
        )


def _property_recipes() -> Iterator[CaseRecipe]:
    yield from _numeric_recipes("transform", TRANSFORM_DOMAINS, "clip.transform")
    yield from _crop_recipes()
    yield from _opacity_blend_recipes()
    yield from _text_recipes()
    yield from _transition_recipes()
    yield from _effect_recipes()
    yield from _timing_recipes()
    yield from _output_recipes()
    yield from _embedded_audio_recipes()
    yield from _edge_trim_recipes()
    yield from _history_currentness_recipes()


#: The accepted payload for each command's accepted case, and the payload that its refused case
#: uses. The refused payloads are all rejected by the accepted decoder rather than by a guard added
#: for this corpus: an unknown track, a clip that is not there, an order outside the profile.
#: Per command: the setup that makes its accepted case reachable, the accepted payload, the refused
#: payload, and the code the accepted decoder raises for that refusal.
#:
#: CRITICAL: the refusal codes here were declared as `invalid_contract` throughout and seventeen of
#: them were wrong. The timeline command decoder refuses an unknown clip, an unknown track, an
#: unsupported edge or an out-of-bounds integer as `invalid_command`; only a command whose *result*
#: would violate the composition contract carries `invalid_contract` through from that decoder. They
#: are two authorities, and a row naming the wrong one passes for the wrong reason.
#:
#: Nine accepted payloads were unreachable as first written -- a non-empty track cannot be removed,
#: merge and roll and slide need adjacent clips, and a split or slip has to land on a declared
#: source landmark. Each now carries the setup that makes it reachable, and the setup's receipts are
#: kept apart from the operation under test.
_COMMAND_ROWS: Final[
    dict[
        str,
        tuple[
            tuple[CommandStep, ...],
            Mapping[str, object],
            Mapping[str, object],
            str,
        ],
    ]
] = {
    "create_track": (
        (),
        {"track_id": "track-extra", "kind": "video_overlay", "order": 4},
        {"track_id": PRIMARY_TRACK, "kind": "video_overlay", "order": 4},
        INVALID_CONTRACT,
    ),
    "remove_track": (
        (
            CommandStep(
                "create_track",
                {"track_id": "track-extra", "kind": "video_overlay", "order": 4},
            ),
        ),
        {"track_id": "track-extra"},
        {"track_id": "track-absent"},
        INVALID_COMMAND,
    ),
    "reorder_track": (
        (),
        {"track_id": OVERLAY_TRACK, "order": 3},
        {"track_id": OVERLAY_TRACK, "order": 99},
        INVALID_COMMAND,
    ),
    "set_track_enabled": (
        (),
        {"track_id": OVERLAY_TRACK, "enabled": False},
        {"track_id": "track-absent", "enabled": False},
        INVALID_COMMAND,
    ),
    "set_track_locked": (
        (),
        {"track_id": OVERLAY_TRACK, "locked": True},
        {"track_id": "track-absent", "locked": True},
        INVALID_COMMAND,
    ),
    "insert_asset_clip": (
        (),
        {"clip": _media_clip("clip-inserted", "vid-overlay", OVERLAY_TRACK, start=30, duration=8)},
        {"clip": _media_clip("clip-inserted", "asset-absent", OVERLAY_TRACK, start=30, duration=8)},
        INVALID_CONTRACT,
    ),
    "insert_title_clip": (
        (),
        {"clip": _title_clip("clip-inserted-title", start=30, duration=8)},
        {"clip": _media_clip("clip-bad-title", "vid-overlay", TEXT_TRACK, start=30, duration=8)},
        INVALID_COMMAND,
    ),
    "replace_clip_asset": (
        (),
        {"clip_id": OVERLAY_CLIP, "asset_id": PRIMARY_ASSET, "source_start_frame": 0},
        {"clip_id": "clip-absent", "asset_id": PRIMARY_ASSET, "source_start_frame": 0},
        INVALID_COMMAND,
    ),
    "remove_clip": ((), {"clip_id": IMAGE_CLIP}, {"clip_id": "clip-absent"}, INVALID_COMMAND),
    "move_clip": (
        (),
        {"clip_id": OVERLAY_CLIP, "delta_frames": 1, "target_track_id": OVERLAY_TRACK},
        {"clip_id": OVERLAY_CLIP, "delta_frames": 1, "target_track_id": "track-absent"},
        INVALID_COMMAND,
    ),
    "move_group": (
        (),
        {"clip_ids": [OVERLAY_CLIP], "delta_frames": 1, "target_track_ids": [OVERLAY_TRACK]},
        {"clip_ids": ["clip-absent"], "delta_frames": 1, "target_track_ids": [OVERLAY_TRACK]},
        INVALID_COMMAND,
    ),
    "trim_clip": (
        (),
        {"clip_id": PRIMARY_CLIP, "edge": "end", "delta_frames": -2},
        {"clip_id": PRIMARY_CLIP, "edge": "middle", "delta_frames": -2},
        INVALID_COMMAND,
    ),
    # A split has to land on a declared source landmark, and the primary asset declares 0, 12
    # and 47. Splitting at an arbitrary offset is refused for a source shift that is not exactly
    # representable, which is a true statement about the contract and not the case under test.
    "split_clip": (
        (),
        {"clip_id": PRIMARY_CLIP, "at_offset_frames": 12, "right_clip_id": "clip-split"},
        {"clip_id": PRIMARY_CLIP, "at_offset_frames": 0, "right_clip_id": "clip-split"},
        INVALID_COMMAND,
    ),
    "merge_clips": (
        (
            CommandStep(
                "split_clip",
                {"clip_id": PRIMARY_CLIP, "at_offset_frames": 12, "right_clip_id": "clip-split"},
            ),
        ),
        {"left_clip_id": PRIMARY_CLIP, "right_clip_id": "clip-split"},
        {"left_clip_id": PRIMARY_CLIP, "right_clip_id": "clip-absent"},
        INVALID_COMMAND,
    ),
    "insert_range": (
        (),
        {
            "clip": _media_clip("clip-range", "vid-overlay", OVERLAY_TRACK, start=12, duration=4),
            "scope_track_ids": [OVERLAY_TRACK],
        },
        {
            "clip": _media_clip("clip-range", "vid-overlay", OVERLAY_TRACK, start=12, duration=4),
            "scope_track_ids": ["track-absent"],
        },
        INVALID_COMMAND,
    ),
    # A range that straddles a clip splits it, and `remainder_ids` must name the survivor. The
    # range rides the image track because an untimed image has no source landmarks to shift: on a
    # timed video the same range is refused for a source shift that is not exactly representable,
    # which is a true statement about the contract and not the case under test.
    "overwrite_range": (
        (),
        {
            "clip": _media_clip("clip-over", IMAGE_ASSET, IMAGE_TRACK, start=12, duration=4),
            "start_frame": 12,
            "duration_frames": 4,
            "scope_track_ids": [IMAGE_TRACK],
            "remainder_ids": {IMAGE_CLIP: "clip-image-remainder"},
        },
        {
            "clip": _media_clip("clip-over", IMAGE_ASSET, IMAGE_TRACK, start=12, duration=4),
            "start_frame": -1,
            "duration_frames": 4,
            "scope_track_ids": [IMAGE_TRACK],
            "remainder_ids": {IMAGE_CLIP: "clip-image-remainder"},
        },
        INVALID_COMMAND,
    ),
    "ripple_delete": (
        (),
        {
            "start_frame": 12,
            "duration_frames": 4,
            "scope_track_ids": [IMAGE_TRACK],
            "remainder_ids": {IMAGE_CLIP: "clip-image-remainder"},
        },
        {
            "start_frame": -1,
            "duration_frames": 4,
            "scope_track_ids": [IMAGE_TRACK],
            "remainder_ids": {IMAGE_CLIP: "clip-image-remainder"},
        },
        INVALID_COMMAND,
    ),
    "ripple_trim": (
        (),
        {
            "clip_id": PRIMARY_CLIP,
            "edge": "end",
            "delta_frames": -2,
            "scope_track_ids": [PRIMARY_TRACK],
        },
        {
            "clip_id": PRIMARY_CLIP,
            "edge": "end",
            "delta_frames": -2,
            "scope_track_ids": ["track-absent"],
        },
        INVALID_COMMAND,
    ),
    # Roll and slide need adjacent clips on one track, and a non-zero delta. The image track
    # supplies both without a source shift, because an untimed image has no landmarks to land on.
    "roll_edit": (
        (
            CommandStep(
                "split_clip",
                {"clip_id": IMAGE_CLIP, "at_offset_frames": 12, "right_clip_id": "clip-image-2"},
            ),
        ),
        {"left_clip_id": IMAGE_CLIP, "right_clip_id": "clip-image-2", "delta_frames": 4},
        {"left_clip_id": IMAGE_CLIP, "right_clip_id": "clip-absent", "delta_frames": 4},
        INVALID_COMMAND,
    ),
    # Slip needs timed video media and a shift that lands on a declared source landmark. The
    # primary asset declares 0, 12 and 47, so the right half of a split at 12 can slip back by 12.
    "slip_clip": (
        (
            CommandStep(
                "split_clip",
                {"clip_id": PRIMARY_CLIP, "at_offset_frames": 12, "right_clip_id": "clip-split"},
            ),
        ),
        {"clip_id": "clip-split", "delta_frames": -12},
        {"clip_id": "clip-absent", "delta_frames": -12},
        INVALID_COMMAND,
    ),
    "slide_clip": (
        (
            CommandStep(
                "split_clip",
                {"clip_id": IMAGE_CLIP, "at_offset_frames": 12, "right_clip_id": "clip-image-2"},
            ),
            CommandStep(
                "split_clip",
                {
                    "clip_id": "clip-image-2",
                    "at_offset_frames": 12,
                    "right_clip_id": "clip-image-3",
                },
            ),
        ),
        {
            "clip_id": "clip-image-2",
            "left_clip_id": IMAGE_CLIP,
            "right_clip_id": "clip-image-3",
            "delta_frames": 2,
        },
        {
            "clip_id": "clip-image-2",
            "left_clip_id": IMAGE_CLIP,
            "right_clip_id": "clip-absent",
            "delta_frames": 2,
        },
        INVALID_COMMAND,
    ),
    "set_clip_enabled": (
        (),
        {"clip_id": IMAGE_CLIP, "enabled": False},
        {"clip_id": "clip-absent", "enabled": False},
        INVALID_COMMAND,
    ),
    "set_visual_transform": (
        (),
        {"clip_id": PRIMARY_CLIP, "transform": _TRANSFORM_WIRE},
        {
            "clip_id": PRIMARY_CLIP,
            "transform": {**_TRANSFORM_WIRE, "scale_x_bp": 80_001},
        },
        INVALID_CONTRACT,
    ),
    "set_crop": (
        (),
        {"clip_id": PRIMARY_CLIP, "crop": _CROP_WIRE},
        {"clip_id": PRIMARY_CLIP, "crop": {**_CROP_WIRE, "left_bp": 10_000}},
        INVALID_CONTRACT,
    ),
    "set_opacity_blend": (
        (),
        {"clip_id": IMAGE_CLIP, "opacity_bp": 6_000, "blend": "normal"},
        {"clip_id": IMAGE_CLIP, "opacity_bp": 6_000, "blend": "overlay"},
        INVALID_CONTRACT,
    ),
    "set_text_content": (
        (),
        {"clip_id": TITLE_CLIP, "content": "AB"},
        {"clip_id": TITLE_CLIP, "content": "A\x07B"},
        INVALID_CONTRACT,
    ),
    "set_text_style": (
        (),
        {"clip_id": TITLE_CLIP, "style": _STYLE_WIRE},
        {"clip_id": TITLE_CLIP, "style": {**_STYLE_WIRE, "weight": 500}},
        INVALID_CONTRACT,
    ),
    "set_transition": (
        (),
        {"clip_id": OVERLAY_CLIP, "transition": _TRANSITION_WIRE},
        {"clip_id": OVERLAY_CLIP, "transition": {**_TRANSITION_WIRE, "duration_frames": 999}},
        INVALID_CONTRACT,
    ),
    "set_effect": (
        (),
        {"clip_id": PRIMARY_CLIP, "effect": _EFFECT_WIRE},
        {"clip_id": PRIMARY_CLIP, "effect": {**_EFFECT_WIRE, "brightness_permille": 1_001}},
        INVALID_CONTRACT,
    ),
    # On the `clip_audio` base (`_COMMAND_BASES`), whose `clip-main` plays the tone for 248 frames.
    # The refused fades fit the bound one by one and exceed the clip together, which the command
    # refuses itself before the result reaches the decoder.
    "set_clip_audio": (
        (),
        {
            "clip_id": PRIMARY_CLIP,
            "gain_mb": -600,
            "muted": False,
            "fade_in_frames": 12,
            "fade_out_frames": 12,
        },
        {
            "clip_id": PRIMARY_CLIP,
            "gain_mb": -600,
            "muted": False,
            "fade_in_frames": 200,
            "fade_out_frames": 100,
        },
        INVALID_COMMAND,
    ),
    "select_clips": (
        (),
        {"clip_ids": [IMAGE_CLIP]},
        {"clip_ids": ["clip-absent"]},
        INVALID_COMMAND,
    ),
    # Undo and redo need a real cursor from a real prior transaction, so the setup produces one and
    # the payload names it by token. A literal cursor would be a cursor for a history that does not
    # exist, which is a different refusal from the one under test.
    # The setup has to move the timeline. Selection is not timeline state, so undoing a selection
    # changes no fingerprint and the row would pass having proved nothing.
    "undo": (
        (
            CommandStep(
                "set_opacity_blend",
                {"clip_id": IMAGE_CLIP, "opacity_bp": 6_000, "blend": "normal"},
            ),
        ),
        {"history_cursor": SETUP_CURSOR},
        {"history_cursor": "h3.context.timeline_history_cursor.v1:99:" + "0" * 64},
        HISTORY_CURSOR_INVALID,
    ),
    "redo": (
        (
            CommandStep(
                "set_opacity_blend",
                {"clip_id": IMAGE_CLIP, "opacity_bp": 6_000, "blend": "normal"},
            ),
            CommandStep("undo", {"history_cursor": SETUP_CURSOR}),
        ),
        {"history_cursor": SETUP_CURSOR},
        {"history_cursor": "h3.context.timeline_history_cursor.v1:99:" + "0" * 64},
        HISTORY_CURSOR_INVALID,
    ),
    "rebase_transaction": (
        (),
        {
            "base_timeline_fingerprint": TIMELINE_FINGERPRINT,
            "commands": [
                {
                    "kind": "set_opacity_blend",
                    "payload": {"clip_id": IMAGE_CLIP, "opacity_bp": 6_000, "blend": "normal"},
                }
            ],
        },
        {
            "base_timeline_fingerprint": STALE_FINGERPRINT,
            "commands": [
                {
                    "kind": "set_opacity_blend",
                    "payload": {"clip_id": IMAGE_CLIP, "opacity_bp": 6_000, "blend": "normal"},
                }
            ],
        },
        REBASE_CONFLICT,
    ),
}


#: M25-45 AC45-05. Each row keeps the natural stack up to its own layer class and disables what
#: sits above it, so the landmarks it is judged on are the ones that layer drew. The image overlay
#: keeps the primary beneath it because `multiply` over an empty canvas is black and would erase
#: the very marks the row exists to read; the video overlay keeps it because `screen` over the
#: primary is exactly the composite the accepted base declares for those two layers. Nothing else
#: is edited: the clip's own opacity, blend, effect and transition stay as the base declares them,
#: so the row observes the real composite at 1280 x 720 rather than an identity arrangement.
_HIGH_RESOLUTION_LAYERS: Final = (
    ("layer.primary_video", (OVERLAY_CLIP, IMAGE_CLIP, TITLE_CLIP)),
    ("layer.video_overlay", (IMAGE_CLIP, TITLE_CLIP)),
    ("layer.image_overlay", (OVERLAY_CLIP, TITLE_CLIP)),
    ("layer.text_overlay", (OVERLAY_CLIP, IMAGE_CLIP)),
)


def _high_resolution_recipes() -> Iterator[CaseRecipe]:
    for leaf, disabled in _HIGH_RESOLUTION_LAYERS:
        yield CaseRecipe(
            case_id=f"high_resolution.{leaf}",
            case_class="property",
            family="high_resolution",
            observation="render_and_browser",
            base=HIGH_RESOLUTION_BASE,
            edits=tuple(SnapshotEdit("clip", "enabled", False, clip_id) for clip_id in disabled),
            notes=(
                "AC45-05: the shipped preview's own 1280 x 720 cap, with 512-square sources placed "
                "1:1, so every landmark is real detail at the backing it is sampled at"
            ),
        )


def _clip_audio(field_name: str, value: object, subject: str = PRIMARY_CLIP) -> SnapshotEdit:
    return SnapshotEdit("clip.audio", field_name, value, subject)


#: M25-77: the named `clip_audio` rows -- each leaf, its edits and the code a refusing leaf carries.
#: A boundary states one field at a time; these state what only a combination or a second clip can.
#:
#: GUARD: in the two hand-over rows the appended clip comes before the fade-out, so the copy of
#: `clip-main` it starts from carries no audio member and plays at the tone's own level. The new
#: clip shows the same source frame `clip-main` shows at every frame they share, so the picture is
#: the base's and the only change is who is heard: at the hand-off the incoming clip takes the sound
#: wherever `clip-main`'s fade-out stands, and at the hand-back `clip-main` resumes its own fade-out
#: at its own clip-relative position, mid-ramp. Reverse the order and the copy fades too.
_CLIP_AUDIO_NAMED: Final[tuple[tuple[str, tuple[SnapshotEdit, ...], str | None], ...]] = (
    ("muted", (_clip_audio("muted", True),), None),
    (
        "combination",
        (
            _clip_audio("gain_mb", -600),
            _clip_audio("fade_in_frames", 12),
            _clip_audio("fade_out_frames", 12),
        ),
        None,
    ),
    (
        "handoff_inside_fade_out",
        (
            SnapshotEdit(
                "clips",
                "append",
                {
                    "clip_id": HANDOFF_CLIP,
                    "start_frame": 224,
                    "duration_frames": 24,
                    "source_start_frame": 224,
                },
            ),
            _clip_audio("fade_out_frames", 48),
        ),
        None,
    ),
    (
        "handback_inside_fade_out",
        (
            SnapshotEdit(
                "clips",
                "append",
                {
                    "clip_id": HANDBACK_CLIP,
                    "start_frame": 160,
                    "duration_frames": 52,
                    "source_start_frame": 160,
                },
            ),
            _clip_audio("fade_out_frames", 48),
        ),
        None,
    ),
    # Each fade alone is inside its bound; together they exceed the 248-frame clip, so the sum rule
    # refuses the row and nothing else does.
    (
        "fade_sum_overflow_refused",
        (_clip_audio("fade_in_frames", 200), _clip_audio("fade_out_frames", 100)),
        INVALID_CONTRACT,
    ),
    # An image has no embedded audio to scale, so the member is refused on its clip.
    ("unbound_audio_refused", (_clip_audio("gain_mb", -600, IMAGE_CLIP),), INVALID_CONTRACT),
)


def _clip_audio_recipes() -> Iterator[CaseRecipe]:
    yield from _numeric_recipes(
        "clip_audio", CLIP_AUDIO_DOMAINS, "clip.audio", base=CLIP_AUDIO_BASE
    )
    for leaf, edits, refusal in _CLIP_AUDIO_NAMED:
        yield _property(
            f"clip_audio.{leaf}",
            "clip_audio",
            edits=edits,
            refusal_code=refusal,
            base=CLIP_AUDIO_BASE,
        )


#: The commands whose rows are cases of another composition than the corpus base: a clip's audio
#: adjustments are measured as the level of the tone source, which only the `clip_audio` base plays.
_COMMAND_BASES: Final[Mapping[str, str]] = {"set_clip_audio": CLIP_AUDIO_BASE}


def _command_recipes(corpus: Corpus) -> Iterator[CaseRecipe]:
    for command in corpus.commands():
        setup, accepted, refused, code = _COMMAND_ROWS[command]
        base = _COMMAND_BASES.get(command, CORPUS_BASE)
        yield CaseRecipe(
            case_id=f"command.{command}.accepted",
            case_class="command",
            family="command",
            observation="command_effect",
            setup=setup,
            command=CommandStep(command, accepted),
            base=base,
            notes=(
                "selection, lock and empty-track changes may legitimately leave the output "
                "invariant; every other command's fixture forces a measurable effect"
            ),
        )
        yield CaseRecipe(
            case_id=f"command.{command}.refused",
            case_class="command",
            family="command",
            observation="command_refusal",
            setup=setup,
            command=CommandStep(command, refused),
            refusal_code=code,
            refusal_layer="command",
            renders=False,
            base=base,
        )


def _shell_recipes(corpus: Corpus) -> Iterator[CaseRecipe]:
    for case in corpus.cases:
        if case.case_class != "ui_invariant":
            continue
        yield CaseRecipe(
            case_id=case.case_id,
            case_class="ui_invariant",
            family=case.family,
            observation="shell_invariant",
            renders=False,
            notes="no shell row changes an Authoring revision or fingerprint",
        )


def _import_recipes(corpus: Corpus) -> Iterator[CaseRecipe]:
    for case in corpus.cases:
        if case.case_class != "import_integration":
            continue
        yield CaseRecipe(
            case_id=case.case_id,
            case_class="import_integration",
            family=case.family,
            observation="import_integration",
            notes=(
                "the real M25-29 service supplies the source identity; import and insertion "
                "effects are counted separately and no import triggers a render, queue, resume, "
                "assembly, provider, model or external-network effect. Neither row starts from "
                "the corpus base: the composition under judgement is the product's own Authoring "
                "timeline after the explicit insertion of the minted asset, reached by both the "
                "browser journey and the render stage through the real registries "
                "(scripts/nle_semantic_import_scenario.py), and the imported source is "
                "IMPORTED_SOURCE_PROFILE, read for the minted asset only after the product's "
                "own probe measured that profile's frame table. The two rows differ by input "
                "modality (pointer, keyboard), which no composition can hold; giving them an "
                "edit would fabricate a difference the journey does not have."
            ),
        )


def _deferred_recipes() -> Iterator[CaseRecipe]:
    for seam in DEFERRED_AUDIO_SEAMS:
        yield CaseRecipe(
            case_id=f"deferred_negative.{seam}",
            case_class="deferred_negative",
            family="deferred_negative",
            observation="declared_negative",
            supported=False,
            renders=False,
            unsupported_reason=(
                f"{seam} is deferred: the seam proves track_profile=none_v1, empty reserved "
                "command membership, edit capabilities unsupported, independent renderer variant "
                "none_v1, and zero state, history, job or output change"
            ),
        )
    for surface in AUDIO_NEGATIVE_SURFACES:
        yield CaseRecipe(
            case_id=f"deferred_negative.surface_{surface}",
            case_class="deferred_negative",
            family="deferred_negative",
            observation="declared_negative",
            supported=False,
            renders=False,
            unsupported_reason=(
                f"the {surface} surface exposes no preview volume or mute control and no "
                "local-only exception to the embedded-audio policy"
            ),
        )


#: How each timing leaf is realised in the source landmark table. The accepted asset declares
#: `nonnegative_monotonic_v1`, so the refusals below are the exact shapes that policy names: a
#: negative member, a table that does not begin at frame and PTS zero, a repeated landmark, one
#: that goes backwards, and a landmark past the admitted frame count.
#:
#: CRITICAL: these are literal fixture tables. Never build one by reading what `resolve_composition`
#: produced -- an expectation copied from the resolver cannot disagree with the resolver, which is
#: the one thing this corpus exists to be able to do.
TIMING_LANDMARKS: Final[dict[str, tuple[tuple[int, int], ...]]] = {
    "negative_pts": ((0, 0), (12, -6_144), (47, 24_064)),
    "missing_landmark": ((4, 2_048), (12, 6_144), (47, 24_064)),
    "duplicate_landmark": ((0, 0), (12, 6_144), (12, 6_144)),
    "nonmonotonic_landmark": ((0, 0), (24, 12_288), (12, 6_144)),
    "source_range_unavailable": ((0, 0), (12, 6_144), (47, 24_064)),
}


def _clip(wire: MutableMapping[str, object], clip_id: str) -> MutableMapping[str, object]:
    clips = wire["clips"]
    if not isinstance(clips, list):
        raise _reject("the composition has no clip table")
    for clip in clips:
        if isinstance(clip, MutableMapping) and clip.get("clip_id") == clip_id:
            return clip
    raise _reject(f"the composition has no clip {clip_id}")


def _track(wire: MutableMapping[str, object], track_id: str) -> MutableMapping[str, object]:
    tracks = wire["tracks"]
    if not isinstance(tracks, list):
        raise _reject("the composition has no track table")
    for track in tracks:
        if isinstance(track, MutableMapping) and track.get("track_id") == track_id:
            return track
    raise _reject(f"the composition has no track {track_id}")


def _asset_row(wire: MutableMapping[str, object], asset_id: str) -> MutableMapping[str, object]:
    assets = wire["assets"]
    if not isinstance(assets, list):
        raise _reject("the composition has no asset table")
    for asset in assets:
        if isinstance(asset, MutableMapping) and asset.get("asset_id") == asset_id:
            return asset
    raise _reject(f"the composition has no asset {asset_id}")


def _section(
    wire: MutableMapping[str, object], target: str, subject: str
) -> MutableMapping[str, object]:
    if target == "output":
        section = wire["output"]
    elif target == "asset":
        section = _asset_row(wire, subject)
    elif target == "track":
        section = _track(wire, subject)
    elif target == "clip":
        section = _clip(wire, subject)
    else:
        section = _clip(wire, subject)
        for step in target.split(".")[1:]:
            member = section.get(step)
            if not isinstance(member, MutableMapping):
                raise _reject(f"{subject}.{step} is not an editable object")
            section = member
    if not isinstance(section, MutableMapping):
        raise _reject(f"{target} is not an editable object")
    return section


def normalize_base(wire: MutableMapping[str, object]) -> None:
    """Rename the fixture's placeholder font asset to the packaged font, in place.

    GUARD: the fixture is accepted M25-10 material and is not edited on disk; the rename happens on
    the copy every stage builds. Do not skip this and do not do it per stage -- a stage that renders
    with the real font while another decodes with the placeholder would be comparing two different
    compositions, and the difference would surface as an unexplained text mismatch rather than as a
    setup error.
    """

    encoded = json.dumps(wire)
    if f'"{PLACEHOLDER_FONT_ASSET}"' not in encoded:
        return
    replaced = json.loads(encoded.replace(f'"{PLACEHOLDER_FONT_ASSET}"', f'"{FONT_ASSET}"'))
    wire.clear()
    wire.update(replaced)


def apply_edits(wire: MutableMapping[str, object], recipe: CaseRecipe) -> None:
    """Apply a recipe's declared edits to a mutable composition wire, in place.

    The caller re-signs the snapshot afterwards. Signing here would hide the one failure mode that
    matters most: an edit that changed nothing still produces a valid snapshot, and a case whose
    setup silently did nothing would then be compared against an unmodified composition and pass.
    """

    normalize_base(wire)
    for edit in recipe.edits:
        if edit.target == "asset.landmarks":
            _apply_timing(wire, edit.field)
            continue
        if edit.target == "clips":
            _append_clip(wire, edit)
            continue
        if edit.target == "clip.audio":
            _apply_clip_audio(wire, edit)
            continue
        section = _section(wire, edit.target, edit.subject)
        if edit.field not in section:
            raise _reject(f"{edit.target}.{edit.field} is not a field of the accepted contract")
        section[edit.field] = edit.value


def _append_clip(wire: MutableMapping[str, object], edit: SnapshotEdit) -> None:
    """Append a copy of the subject clip with the edit's literal fields written over it."""

    if edit.field != "append" or not isinstance(edit.value, Mapping):
        raise _reject("a clips edit appends a literal mapping of clip fields")
    clips = wire["clips"]
    if not isinstance(clips, list):
        raise _reject("the composition has no clip table")
    appended = copy.deepcopy(dict(_clip(wire, edit.subject)))
    for field_name, value in edit.value.items():
        if field_name not in appended:
            raise _reject(f"clips.{field_name} is not a field of the accepted contract")
        appended[field_name] = value
    if any(
        isinstance(clip, Mapping) and clip.get("clip_id") == appended["clip_id"] for clip in clips
    ):
        raise _reject(f"the composition already has clip {appended['clip_id']}")
    clips.append(appended)


def _apply_clip_audio(wire: MutableMapping[str, object], edit: SnapshotEdit) -> None:
    """Write one field of the subject clip's audio member, keeping the identity absent."""

    if edit.field not in _CLIP_AUDIO_IDENTITY:
        raise _reject(f"clip.audio.{edit.field} is not a field of the accepted contract")
    clip = _clip(wire, edit.subject)
    current = clip.get("audio")
    member: dict[str, object] = dict(_CLIP_AUDIO_IDENTITY)
    if isinstance(current, Mapping):
        member.update(current)
    elif current is not None:
        raise _reject(f"{edit.subject} carries an audio member that is not an object")
    member[edit.field] = edit.value
    if member == dict(_CLIP_AUDIO_IDENTITY):
        clip.pop("audio", None)
    else:
        clip["audio"] = member


def _apply_timing(wire: MutableMapping[str, object], leaf: str) -> None:
    table = TIMING_LANDMARKS.get(leaf)
    if table is None:
        raise _reject(f"{leaf} has no declared source landmark table")
    assets = wire["assets"]
    if not isinstance(assets, list):
        raise _reject("the composition has no asset table")
    for asset in assets:
        if not isinstance(asset, MutableMapping) or asset.get("asset_id") != PRIMARY_ASSET:
            continue
        asset["landmarks"] = [
            {"frame_index": frame, "pts": pts, "dts": pts, "duration_ticks": 512}
            for frame, pts in table
        ]
        return
    raise _reject(f"the composition has no asset {PRIMARY_ASSET}")


@dataclass(frozen=True, slots=True)
class RecipeBook:
    """Every corpus row's recipe, indexed by case identifier."""

    recipes: tuple[CaseRecipe, ...]
    by_id: Mapping[str, CaseRecipe] = field(default_factory=dict)

    def rendering(self) -> tuple[CaseRecipe, ...]:
        return tuple(recipe for recipe in self.recipes if recipe.renders)

    def counts_by_observation(self) -> dict[str, int]:
        counts = dict.fromkeys(OBSERVATION_KINDS, 0)
        for recipe in self.recipes:
            counts[recipe.observation] += 1
        return counts


def build_recipes(corpus: Corpus | None = None) -> RecipeBook:
    """Build one recipe per corpus row, and refuse anything that is not a bijection.

    CRITICAL: the corpus decides membership and this module decides execution, so the join between
    them is checked in both directions here. A recipe for a case that does not exist would run an
    experiment nobody asked for; a case with no recipe would silently never run, which is precisely
    the shape the post-closeout review rejected -- rows that were enumerated, counted and reported
    as covered without ever being executed.
    """

    corpus = build_corpus() if corpus is None else corpus
    recipes = tuple(
        (
            *_command_recipes(corpus),
            *_property_recipes(),
            *_high_resolution_recipes(),
            *_clip_audio_recipes(),
            *_shell_recipes(corpus),
            *_import_recipes(corpus),
            *_deferred_recipes(),
        )
    )
    identifiers = [recipe.case_id for recipe in recipes]
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if duplicates:
        raise _reject(f"recipes repeat case identifiers: {duplicates}")
    declared = {case.case_id: case for case in corpus.cases}
    missing = sorted(set(declared) - set(identifiers))
    if missing:
        raise _reject(f"corpus rows have no recipe: {missing}")
    extra = sorted(set(identifiers) - set(declared))
    if extra:
        raise _reject(f"recipes name cases outside the corpus: {extra}")
    for recipe in recipes:
        case = declared[recipe.case_id]
        _check_against_corpus(recipe, case)
    return RecipeBook(recipes=recipes, by_id={recipe.case_id: recipe for recipe in recipes})


def _check_against_corpus(recipe: CaseRecipe, case: CorpusCase) -> None:
    if recipe.case_class != case.case_class:
        raise _reject(f"{recipe.case_id} disagrees with the corpus about its class")
    if recipe.supported != case.supported:
        raise _reject(f"{recipe.case_id} disagrees with the corpus about being supported")
    refusing = recipe.observation in ("contract_refusal", "command_refusal")
    if refusing != case.refusal_expected:
        raise _reject(
            f"{recipe.case_id} disagrees with the corpus about expecting a refusal: "
            f"recipe={refusing} corpus={case.refusal_expected}"
        )
