"""Closed corpus identity and frozen tolerance profile for preview/final semantic conformance.

M25-20 compares what the browser actually presented against what an independent extractor actually
decoded out of the final artifact. This module owns neither observation. It owns the two questions
that must be answerable *before* any runtime executes:

- **Which cases exist?** The corpus is a closed set expanded from the accepted M25-10 contract
  domains and the 34 accepted M25-11 command identifiers, never from executed results. Deriving
  membership from what happened to run is how a conformance suite silently stops covering the thing
  it was built for, so `build_corpus()` reads the contract and nothing else.
- **What counts as agreement?** The tolerances in `TOLERANCE_PROFILE` are the plan's frozen
  acceptance targets. They are inherited, not chosen: the 2,000-sample preview drift and
  2,048-sample final impulse bounds come from the accepted M25-10 output profile, and the raster
  bounds come from the M25-18 synthetic observation scale.

Case identity is stable and per-leaf. A family that names several boundaries expands into one
identifier per boundary, because a row that can be satisfied by "the transform family passed"
cannot tell a reviewer whether the upper bound was ever exercised.

The comparator lives in `semantic_conformance_compare`; keeping it separate is deliberate, so that
the corpus can be enumerated and audited without importing observation machinery.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from .composition_contract import NLE_OPERATION_IDS, OUTPUT_PROFILE_ID
from .errors import ContractValidationError

CORPUS_VERSION: Final = "nle_semantic_corpus.v1"
TOLERANCE_VERSION: Final = "nle_semantic_tolerance.v1"

#: The one way a final artifact may reach the independent extractor: the accepted M25-19
#: transport (`AuthoringOutputRegistry` create / status / download), whose download is the
#: store's completely verified body. The render stage labels every observed phase with how its
#: bytes were obtained, and the join admits only this label -- a phase that decoded the
#: renderer's own staging file proves the encoder and not the transport, and the plan's AC7 does
#: not accept that in place of the verified original.
ACCEPTED_ARTIFACT_RETRIEVAL: Final = "m25_19_verified_download"
CONFORMANCE_MANIFEST_SCHEMA: Final = "h3.context.nle_semantic_conformance_manifest.v1"
CONFORMANCE_REPORT_SCHEMA: Final = "h3.context.nle_semantic_conformance_report.v1"

#: The plan's expanded-case ceiling. It is a resource bound, not permission to drop a required row:
#: an overflow is resolved by narrowing the corpus design, never by silently omitting a leaf.
MAX_CORPUS_CASES: Final = 512
#: Derived landmark payload per case, and the aggregate report ceiling.
#: GUARD: a render row records three source-mapping measurements per output frame, so the
#: per-case budget must hold a row of `MAX_CASE_OUTPUT_FRAMES` frames (about 115 KB at long
#: values); the join refuses the whole report when one row outgrows it. 65,536 stopped the join on
#: every row of the 248-frame `clip_audio` base. The report ceiling is `MAX_CORPUS_CASES` rows at
#: that budget; change the two together. Dropping per-frame measurements to fit is a change to
#: what every row reports, not a fix for this bound.
MAX_CASE_PAYLOAD_BYTES: Final = 131_072
MAX_REPORT_BYTES: Final = 67_108_864
#: Ordinary temporal cases stay well inside the accepted render ceilings.
MAX_CASE_OUTPUT_FRAMES: Final = 384

#: Every row belongs to exactly one class, and the classes are never summed into a single
#: "coverage" number: a UI invariant is not a command, and a declared negative is not either.
CASE_CLASSES: Final = (
    "command",
    "property",
    "ui_invariant",
    "import_integration",
    "deferred_negative",
)

#: The closed report statuses. `MISMATCH` means two real observations disagreed; `BLOCKED` means an
#: observation or authority was missing; `NOT_RUN` means execution never happened. Only `PASS` and a
#: predeclared `DECLARED_UNSUPPORTED` are conformant outcomes.
REPORT_STATUSES: Final = (
    "PASS",
    "MISMATCH",
    "DECLARED_UNSUPPORTED",
    "BLOCKED",
    "NOT_RUN",
)

#: Which report statuses a row of each support kind may carry.
#:
#: CRITICAL: this is the rule status counting cannot express. `summarize` adds up dispositions, so
#: a run that answered every hard supported row with `DECLARED_UNSUPPORTED` produces no `MISMATCH`,
#: no `BLOCKED` and no `NOT_RUN`, and every total looks like conformance. `DECLARED_UNSUPPORTED`
#: belongs to a predeclared negative and to nothing else; a supported row is proven or it is named
#: as unproven.
SUPPORTED_STATUSES: Final = ("PASS", "MISMATCH", "BLOCKED", "NOT_RUN")
UNSUPPORTED_STATUSES: Final = ("DECLARED_UNSUPPORTED", "BLOCKED", "NOT_RUN")

#: Mismatch classes are closed so a report cannot invent a soft category for a hard disagreement.
MISMATCH_CLASSES: Final = (
    "identity",
    "frame_grid",
    "source_mapping",
    "geometry",
    "patch",
    "color_adjust",
    "alpha",
    "text",
    "output_metadata",
    "av_drift",
    "audio_onset",
    "audio_silence",
    "audio_extent",
    # M25-77: a window of output audio whose level is not the one a clip's adjustments define.
    "audio_level",
    "refusal_code",
    # B-66: an audio-surface declared negative whose counted control or exception is not zero.
    "audio_surface",
    # B-65: an import row whose recorded chain effect is not what Section 13 requires.
    "import_effect",
)

#: The seven frozen shell rows. They are not M25-11 commands and can never satisfy a command row.
SIDEBAR_UI_INVARIANT_IDS: Final = (
    "global_shell_identity",
    "function_switch",
    "overlay_open",
    "duplicate_open",
    "overlay_close_return_focus",
    "overlay_unavailable_status",
    "view_destroy_cleanup",
)

#: `view_destroy` is excluded here on purpose: destroy cleanup is its own row, and the return-focus
#: row exists to prove every *other* close reason lands on its exact recorded destination.
NON_DESTROY_CLOSE_REASONS: Final = (
    "explicit_close",
    "escape",
    "function_switch",
    "top_level_navigation",
    "capability_or_mount_failure",
)

#: Deferred standalone-audio surfaces. Each needs negative evidence -- no control, no command, no
#: lease, no waveform, no mixer, no audio-edit receipt -- rather than a silent skip.
#:
#: `gain`, `mute` and `envelope` here are the controls of an independent audio track, which is
#: still deferred. A video clip's own gain, mute and fades (M25-77) are a different surface: they
#: live on the clip, are measured by the `clip_audio` family, and leave these seams as they were.
DEFERRED_AUDIO_SEAMS: Final = (
    "independent_audio_seam",
    "independent_audio_import",
    "independent_audio_replacement",
    "waveform",
    "gain",
    "pan",
    "mute",
    "solo",
    "envelope",
    "multi_track_mixer",
)

#: The M25-owned surfaces that must not expose a preview volume/mute control or a local-only
#: exception to the embedded-audio policy.
AUDIO_NEGATIVE_SURFACES: Final = ("compact", "expanded", "fallback")

#: What the browser journey counts on each of those surfaces, and what the join requires of a
#: `deferred_negative.surface_*` row before it may close as `DECLARED_UNSUPPORTED`: every one of
#: these facts present and zero. A nonzero count is a MISMATCH (the surface exposes what the
#: policy forbids); an absent or unreadable count is BLOCKED; no record is NOT_RUN. Mirrored
#: field-for-field by `frontend/tests/e2e/journeys/nleSemanticAudioSurfaceNegatives.spec.ts`.
#:
#: GUARD (B-66): a declared negative used to close on any executed browser row, whatever it
#: observed -- an `overlayPresent` fact closed an audio claim. Dropping a name from this tuple
#: reopens exactly that hole for the thing it counted; add names, never remove them.
AUDIO_SURFACE_NEGATIVE_FACTS: Final = (
    "native_controls_elements",
    "audio_track_surfaces",
    "audio_word_controls",
    "soundtrack_pairing_controls",
    "disabled_audio_placeholders",
    "context_menu_audio_entries",
    "drag_drop_audio_entries",
    "mute_shortcut_changed_elements",
    "video_elements_with_controls",
    "unmuted_non_follower_video_elements",
)

#: B-65: the facts both halves of an `import_integration` row record about the one chain they
#: each drive (the browser journey through the shell's own controls, the render stage through the
#: real registries), and which the join requires to be present on both sides and equal. They are
#: the chain's invariant identities -- what the product's insert control drafts, what the timeline
#: measured of the imported bytes, how the history moved -- never the product-minted asset id or
#: a fingerprint over it, which differ between two runs of the same chain by construction.
IMPORT_IDENTITY_FACTS: Final = (
    "source_fingerprint",
    "imported_source_frame_count",
    "imported_source_landmark_table_sha256",
    "receipt_rows",
    "pre_import_timeline_revision",
    "post_import_timeline_revision",
    "post_insert_timeline_revision",
    "post_undo_timeline_revision",
    "post_redo_timeline_revision",
    "inserted_clip_id",
    "inserted_clip_track_id",
    "inserted_clip_start_frame",
    "inserted_clip_duration_frames",
    "inserted_clip_source_start_frame",
    "output_width",
    "output_height",
    "output_duration_frames",
    "post_import_clip_count",
    "post_insert_clip_count",
    "post_undo_clip_count",
    "post_redo_clip_count",
)

#: B-65: what the chain must have shown, on both sides, for the row to close -- the original
#: plan's Section 13 / AC10 in fact form: the import mutates no timeline and does advance the
#: workspace, exactly one receipt row and one inserted clip, undo across the pre-import history
#: restores the pre-insert composition while keeping the admitted library, redo restores the
#: insertion. (That the product's own probe measured the imported source's frame table is checked
#: by the join directly on the recorded composition, `imported_source_landmarks_match`, and tied
#: to the browser's snapshot through the landmark-table digest above.) Values are compared as
#: `str()` of the recorded fact.
#:
#: GUARD: a row missing any of these on either side is BLOCKED, and one that recorded a different
#: value is a MISMATCH -- never fold an absent fact into "as required".
IMPORT_EFFECT_FACTS: Final[Mapping[str, str]] = {
    "receipt_rows": "1",
    "import_changed_timeline_revision": "False",
    "import_changed_timeline_fingerprint": "False",
    "import_advanced_workspace_revision": "True",
    "post_import_clip_count": "0",
    "post_insert_clip_count": "1",
    "post_undo_clip_count": "0",
    "post_redo_clip_count": "1",
    "undo_restored_pre_insert_timeline": "True",
    "library_retained_after_undo": "True",
    "redo_restored_post_insert_timeline": "True",
}

#: Each expanded shell-invariant leaf and the executable case that proves it, cited as
#: `<path under frontend/tests>::<collected test name>`. The format matches the M25-16 control
#: coverage manifest so one frontend collector resolves both: `frontend/tests/nleEvidenceCollection`
#: asks the real runners what they collected rather than parsing test sources, which is what stops a
#: citation from naming a test that was renamed, skipped or never written.
UI_INVARIANT_EVIDENCE: Final = {
    "global_shell_identity": (
        "e2e/journeys/nleSemanticConformance.spec.ts::"
        "the accepted runtime presents an edited composition that can be observed semantically"
    ),
    "function_switch": (
        "e2e/journeys/nleSemanticConformance.spec.ts::"
        "the accepted runtime presents an edited composition that can be observed semantically"
    ),
    "overlay_open": (
        "nleWorkspaceLifecycle.test.tsx::M25-16 NLE workspace session lifecycle > "
        "opens with a fresh generation, default bounds and the assets pane, then expands on mount"
    ),
    "duplicate_open": (
        "e2e/journeys/nleSemanticConformance.spec.ts::"
        "the accepted runtime presents an edited composition that can be observed semantically"
    ),
    "overlay_close_return_focus.explicit_close": (
        "nleWorkspaceLifecycle.test.tsx::M25-20 overlay close return-focus destinations > "
        "returns focus to the launcher after explicit_close"
    ),
    "overlay_close_return_focus.escape": (
        "nleWorkspaceLifecycle.test.tsx::M25-20 overlay close return-focus destinations > "
        "returns focus to the launcher after escape"
    ),
    "overlay_close_return_focus.function_switch": (
        "nleWorkspaceLifecycle.test.tsx::M25-20 overlay close return-focus destinations > "
        "leaves focus where the new surface put it after function_switch"
    ),
    "overlay_close_return_focus.top_level_navigation": (
        "nleWorkspaceLifecycle.test.tsx::M25-20 overlay close return-focus destinations > "
        "leaves focus where the new surface put it after top_level_navigation"
    ),
    "overlay_close_return_focus.capability_or_mount_failure": (
        "nleWorkspaceLifecycle.test.tsx::M25-20 overlay close return-focus destinations > "
        "returns focus to the launcher after capability_or_mount_failure"
    ),
    "overlay_unavailable_status": (
        "nleWorkspaceLifecycle.test.tsx::M25-16 NLE workspace session lifecycle > "
        "refuses to open when the media runtime is unsupported and reports the unavailable status"
    ),
    "view_destroy_cleanup": (
        "nleWorkspaceLifecycle.test.tsx::M25-16 NLE workspace session lifecycle > "
        "resets geometry and pane on view destroy and records view_destroy once"
    ),
}

#: The two explicit import gestures the Section 13 amendment requires as separate acceptance rows,
#: and the case name the plan froze for them.
IMPORT_INTEGRATION_GESTURES: Final = ("pointer", "keyboard")
IMPORT_INTEGRATION_CASE: Final = "generated_source.explicit_import_then_insert"

#: Every property case whose expected outcome is a refusal, except the numeric-domain boundaries.
#:
#: CRITICAL: this used to be guessed from whether a leaf's name ended in `_refused`, and the guess
#: was wrong for eleven rows -- most importantly the whole timing family, where the accepted decoder
#: answers a negative timestamp with `negative_timestamp` and a duplicate or non-advancing landmark
#: with `invalid_timing`. Those rows were frozen into the manifest as accepted cases, which
#: looks like coverage and is not. Adding a refusal case means adding it here; a name is not a
#: disposition.
#:
#: The `underflow`/`overflow` rows of `transform`, `effect` and the two text metrics are
#: deliberately absent: they are *defined* as the first values outside a declared
#: `NumericDomain`, and `tests/test_m25_render_conformance.py` proves that definition against
#: the real decoder for every domain. The two `opacity_blend.opacity` boundaries are listed here
#: instead, because that subfamily spells its field literally rather than taking it from
#: `OPACITY_DOMAIN.field`.
NAMED_REFUSAL_CASES: Final = frozenset(
    {
        #: Refused by the packaged font resolver when the plan's font facts are bound,
        #: not by the composition decoder: the snapshot itself is valid.
        "text.font.unsupported_glyph",
        # The whole image removed from one side, and each axis sum constraint separately.
        "crop.left_bp.full_removal_refused",
        "crop.top_bp.full_removal_refused",
        "crop.right_bp.full_removal_refused",
        "crop.bottom_bp.full_removal_refused",
        "crop.sum_horizontal_refused",
        "crop.sum_vertical_refused",
        # Text content is bounded by length, line count, control characters and NFC form.
        "text.content.underflow",
        "text.content.overflow",
        "text.content.control_refused",
        "text.content.non_nfc_refused",
        "text.content.line_count_overflow",
        "text.weight.unsupported_refused",
        "text.fill_rgba.channel_overflow_refused",
        "text.background_rgba.channel_overflow_refused",
        # A cross-dissolve shorter than one frame or longer than its clip, and an invalid
        # participant, are all refused by the clip decoder.
        "transition.duration_underflow",
        "transition.duration_overflow",
        "transition.participant_invalid",
        "effect.kind.none_identity_refused",
        # `nonnegative_monotonic_v1`: a negative timestamp is `negative_timestamp`, and a landmark
        # table that repeats, goes backwards or leaves the admitted range is `invalid_timing`.
        "timing.negative_pts",
        "timing.missing_landmark",
        "timing.duplicate_landmark",
        "timing.nonmonotonic_landmark",
        "timing.source_range_unavailable",
        "output.dimensions_odd_refused",
        "output.dimensions_underflow_refused",
        "output.dimensions_overflow_refused",
        "output.dimensions_area_overflow_refused",
        "output.color_policy_refused",
        "output.container_refused",
        "output.video_codec_refused",
        "output.pixel_aspect_refused",
        "opacity_blend.opacity.underflow",
        "opacity_blend.opacity.overflow",
        "opacity_blend.blend.invalid_refused",
        "edge_trim.invalid_transition_refused",
        "edge_trim.draft_refused",
        "history_currentness.stale_snapshot_refused",
        "history_currentness.source_replaced_refused",
        "history_currentness.output_replaced_refused",
        # Two fades each inside their bound whose sum exceeds the clip, and a member on a clip
        # without bound audio.
        "clip_audio.fade_sum_overflow_refused",
        "clip_audio.unbound_audio_refused",
    }
)

_CASE_ID = re.compile(r"[a-z0-9][a-z0-9_.]{0,127}\Z")


class SemanticConformanceError(ContractValidationError):
    """Raised when a corpus, tolerance or report projection is invalid or unsupported."""


def _reject(message: str) -> SemanticConformanceError:
    return SemanticConformanceError(message)


@dataclass(frozen=True, slots=True)
class NumericDomain:
    """One closed integer field of the accepted composition contract.

    `identity` is the declared reference value -- the contract's own default where the field has
    one -- `interior` a distinct admitted value, and `lower`/`upper` the exact inclusive bounds.
    `underflow`/`overflow` are the first values outside them, so a corpus leaf exists on each side
    of every boundary rather than near it.
    """

    field: str
    lower: int
    upper: int
    identity: int
    interior: int

    def __post_init__(self) -> None:
        if not self.lower <= self.identity <= self.upper:
            raise _reject(f"{self.field} identity is outside its own bounds")
        if not self.lower <= self.interior <= self.upper:
            raise _reject(f"{self.field} interior is outside its own bounds")
        if self.interior == self.identity:
            raise _reject(f"{self.field} interior must differ from identity")

    @property
    def underflow(self) -> int:
        return self.lower - 1

    @property
    def overflow(self) -> int:
        return self.upper + 1

    def boundaries(self) -> tuple[tuple[str, int], ...]:
        return (
            ("identity", self.identity),
            ("interior", self.interior),
            ("lower", self.lower),
            ("upper", self.upper),
            ("underflow", self.underflow),
            ("overflow", self.overflow),
        )

    def distinct_boundaries(self) -> tuple[tuple[str, int], ...]:
        """The boundaries a corpus expands: each leaf whose value no earlier leaf states.

        A leaf with an earlier leaf's value is the same experiment under a second name -- a fade's
        `lower` bound of zero is its identity -- and a second row for it would be counted as
        coverage while measuring nothing new.
        """

        seen: set[int] = set()
        leaves: list[tuple[str, int]] = []
        for leaf, value in self.boundaries():
            if value not in seen:
                seen.add(value)
                leaves.append((leaf, value))
        return tuple(leaves)


# CRITICAL: these restate the bounds enforced inside `composition_contract._transform`, `_crop`,
# `_text`, `_clip` and `_output`. They are restated rather than imported because those decoders keep
# their bounds as literals inside validation calls, and a corpus that guessed would drift silently.
# `tests/test_m25_render_conformance.py` drives the real decoders at each bound and one step past
# it, so moving a product bound without moving the domain here turns that test red. Never "fix" a
# failure there by editing this table to match observed behaviour -- that is the exact inversion the
# conformance corpus exists to prevent.
TRANSFORM_DOMAINS: Final = (
    # Asymmetric interiors: an x/y swap in the renderer must not be able to pass the pair.
    NumericDomain("anchor_x_bp", 0, 10_000, 5_000, 2_500),
    NumericDomain("anchor_y_bp", 0, 10_000, 5_000, 7_500),
    NumericDomain("position_x_bp", -40_000, 40_000, 0, 1_250),
    NumericDomain("position_y_bp", -40_000, 40_000, 0, -3_750),
    NumericDomain("scale_x_bp", 1, 80_000, 10_000, 6_000),
    NumericDomain("scale_y_bp", 1, 80_000, 10_000, 14_000),
    NumericDomain("rotation_mdeg", -180_000, 180_000, 0, 30_000),
)

CROP_EDGES: Final = ("left_bp", "top_bp", "right_bp", "bottom_bp")
CROP_DOMAIN: Final = NumericDomain("crop_bp", 0, 9_999, 0, 1_500)

OPACITY_DOMAIN: Final = NumericDomain("opacity_bp", 0, 10_000, 10_000, 5_000)
BLEND_MODES: Final = ("normal", "multiply", "screen")

TEXT_SIZE_DOMAIN: Final = NumericDomain("size_px", 8, 512, 48, 96)
TEXT_LINE_HEIGHT_DOMAIN: Final = NumericDomain("line_height_bp", 7_500, 30_000, 12_000, 20_000)
TEXT_WEIGHTS: Final = (400, 700)
TEXT_STYLES: Final = ("normal", "italic")
TEXT_ALIGNMENTS: Final = ("left", "center", "right")
#: `content` is bounded by length, line count, control characters and NFC form rather than by a
#: single integer range, so its leaves are named individually.
TEXT_CONTENT_CASES: Final = (
    "identity",
    "nfc_composed",
    "newline",
    "tab_expansion",
    "underflow",
    "overflow",
    "control_refused",
    "non_nfc_refused",
    "line_count_overflow",
)
TEXT_FONT_CASES: Final = ("qualified", "fallback", "unsupported_glyph")
TEXT_FILL_CASES: Final = ("identity", "interior", "channel_overflow_refused")
TEXT_BACKGROUND_CASES: Final = ("absent", "present", "channel_overflow_refused")

TRANSITION_KINDS: Final = ("none", "cross_dissolve_v1")
TRANSITION_CASES: Final = (
    "none_identity",
    "cross_dissolve_legal",
    "first_included_frame",
    "interior_frame",
    "last_included_frame",
    "exclusive_end",
    "alpha_progression",
    "duration_lower",
    "duration_upper",
    "duration_underflow",
    "duration_overflow",
    "participant_invalid",
)

EFFECT_KINDS: Final = ("none", "color_adjust_v1")
EFFECT_DOMAINS: Final = (
    NumericDomain("brightness_permille", -1_000, 1_000, 0, 250),
    NumericDomain("contrast_permille", 0, 2_000, 1_000, 1_400),
    NumericDomain("saturation_permille", 0, 2_000, 1_000, 600),
)

#: M25-77: a clip's audio member (`composition_contract._clip_audio`). The gain is in millibels; a
#: fade is in output frames, and its `lower` bound is its identity, so it expands without one.
CLIP_AUDIO_DOMAINS: Final = (
    NumericDomain("gain_mb", -6_000, 1_200, 0, -600),
    NumericDomain("fade_in_frames", 0, 240, 0, 12),
    NumericDomain("fade_out_frames", 0, 240, 0, 12),
)
#: The rows a single field's boundary cannot state: a mute, all three adjustments at once, an owner
#: hand-over inside a fade-out in each direction, and the two refusals that need more than a bound.
CLIP_AUDIO_CASES: Final = (
    "muted",
    "combination",
    "handoff_inside_fade_out",
    "handback_inside_fade_out",
    "fade_sum_overflow_refused",
    "unbound_audio_refused",
)

TIMING_CASES: Final = (
    "cfr_24",
    "source_rate_different",
    "vfr_unequal_intervals",
    "source_in",
    "source_out",
    "gap",
    "cut",
    "last_included_frame",
    "exclusive_end",
    "negative_pts",
    "missing_landmark",
    "duplicate_landmark",
    "nonmonotonic_landmark",
    "source_range_unavailable",
)

OUTPUT_CASES: Final = (
    "dimensions_square",
    "dimensions_nonsquare",
    "dimensions_minimum",
    "dimensions_maximum",
    "dimensions_odd_refused",
    "dimensions_underflow_refused",
    "dimensions_overflow_refused",
    "dimensions_area_overflow_refused",
    "color_policy_bt709_sdr_limited",
    "color_policy_refused",
    "frame_grid_exact_24",
    "container_mp4",
    "container_refused",
    "video_codec_h264",
    "video_codec_refused",
    "pixel_format_yuv420p",
    "pixel_aspect_identity",
    "pixel_aspect_refused",
)

EMBEDDED_AUDIO_CASES: Final = (
    "primary_audible",
    "primary_without_audio",
    "hard_cut",
    "silence_gap",
    "primary_disabled",
    "overlay_excluded",
    "overlay_same_source_no_doubling",
    "trim_owner_range",
    "slip_owner_range",
    "ripple_owner_range",
    "roll_owner_range",
    "undo_owner_range",
    "redo_owner_range",
    "visual_only_mapping_preserved",
)

#: The six property identifiers M25-16 froze as `NLE-EDGE-TRIM-V1`. They ride the existing
#: `trim_clip` command; they are never two extra command identifiers.
EDGE_TRIM_PROPERTY_IDS: Final = (
    "edge_trim_start_in",
    "edge_trim_start_out",
    "edge_trim_end_in",
    "edge_trim_end_out",
    "edge_trim_bounds_timing",
    "edge_trim_transition_audio",
)
EDGE_TRIM_OBSERVATION_CASES: Final = (
    "opposite_edge_invariant",
    "non_target_clip_invariant",
    "source_pts_boundary",
    "incoming_transition_removed",
    "invalid_transition_refused",
    "embedded_audio_follow",
    "draft_cancelled",
    "draft_no_op",
    "draft_refused",
    "undo",
    "redo",
)

HISTORY_CURRENTNESS_CASES: Final = (
    "selection_invariant",
    "lock_invariant",
    "inverse",
    "replay",
    "explicit_rebase",
    "stale_snapshot_refused",
    "source_replaced_refused",
    "output_replaced_refused",
    "old_output_accepted_label",
    # The M25-16 second corrective made an unknown outcome carry a `reconciliation` fact rather
    # than an inferred verdict. Both of its values get a case, because the whole point is that an
    # unknown outcome is attributed to neither acceptance nor rejection.
    "unknown_outcome_pending",
    "unknown_outcome_failed",
)


@dataclass(frozen=True, slots=True)
class ToleranceProfile:
    """The frozen numeric agreement bounds, inherited from the accepted upstream profiles.

    Every field is an acceptance target fixed before execution. A measured value may not widen one,
    and no per-case fitted offset is permitted anywhere: an offset that hides drift is drift.
    """

    version: str
    #: Inherited unchanged from the accepted M25-10 output profile.
    preview_max_av_drift_samples: int
    final_impulse_tolerance_samples: int
    #: Output-space pixels, against actual Canvas backing dimensions rather than CSS coordinates.
    geometry_max_abs_pixels: int
    text_bounds_max_abs_pixels: int
    #: RGB8 channel error on declared interior patches, at least `patch_min_edge_distance_px` from
    #: any edge so antialiased boundary pixels can never be sampled as interior.
    patch_max_abs_channel: int
    color_adjust_max_abs_channel: int
    patch_size_px: int
    patch_min_edge_distance_px: int
    #: Alpha progression on a deliberately high-contrast linear ramp.
    alpha_max_abs_error_milli: int
    #: PCM16 absolute peak permitted inside a declared silence interior.
    silence_max_abs_peak: int
    #: Samples excluded at each declared codec cut boundary. A measured gap must be longer than
    #: twice this, or the silence check would be inspecting an empty interval and always "pass".
    codec_boundary_exclusion_samples: int
    #: Rational conversion may round by at most one sample, and the conversion must be disclosed.
    duration_max_abs_samples: int
    #: M25-77: a window's measured level against the level a clip's audio adjustments define there,
    #: both in millionths of the source tone's own level. A window passes when its error is within
    #: this many millionths of the defined level, or within the floor below where the defined
    #: level is so small that a relative bound would be smaller than what an encode can hold. Both
    #: are set from the calibration on the pinned pair (TEST_SOP 3.27) and frozen before any
    #: acceptance run.
    #:
    #: Calibrated: every rendering row of the family through the product program on the pinned
    #: pair, twice, window for window identical. The largest error was 2.9 % of the defined level
    #: (the middle of a 12-frame fade-in); the relative bound is 5 %, 1.7 times that, and still
    #: detects a 12-frame fade one frame long or short (7.7 % or 9.1 % at its quarter and middle)
    #: and 1 dB of gain (12 %). A muted clip measured exactly zero and the -60 dB clip within 12
    #: millionths, so the floor is 100.
    #:
    #: GUARD: do not raise the floor towards the quietest defined level. At a floor of 1,000 a
    #: silent output passes for the -60 dB row, which is then judged on nothing.
    audio_level_relative_error_ppm: int
    audio_level_floor_ppm: int

    def as_wire(self) -> dict[str, object]:
        return {
            "version": self.version,
            "preview_max_av_drift_samples": self.preview_max_av_drift_samples,
            "final_impulse_tolerance_samples": self.final_impulse_tolerance_samples,
            "geometry_max_abs_pixels": self.geometry_max_abs_pixels,
            "text_bounds_max_abs_pixels": self.text_bounds_max_abs_pixels,
            "patch_max_abs_channel": self.patch_max_abs_channel,
            "color_adjust_max_abs_channel": self.color_adjust_max_abs_channel,
            "patch_size_px": self.patch_size_px,
            "patch_min_edge_distance_px": self.patch_min_edge_distance_px,
            "alpha_max_abs_error_milli": self.alpha_max_abs_error_milli,
            "silence_max_abs_peak": self.silence_max_abs_peak,
            "codec_boundary_exclusion_samples": self.codec_boundary_exclusion_samples,
            "duration_max_abs_samples": self.duration_max_abs_samples,
            "audio_level_relative_error_ppm": self.audio_level_relative_error_ppm,
            "audio_level_floor_ppm": self.audio_level_floor_ppm,
        }


TOLERANCE_PROFILE: Final = ToleranceProfile(
    version=TOLERANCE_VERSION,
    preview_max_av_drift_samples=2_000,
    final_impulse_tolerance_samples=2_048,
    geometry_max_abs_pixels=2,
    text_bounds_max_abs_pixels=2,
    patch_max_abs_channel=8,
    color_adjust_max_abs_channel=12,
    patch_size_px=5,
    patch_min_edge_distance_px=4,
    alpha_max_abs_error_milli=30,
    silence_max_abs_peak=1,
    codec_boundary_exclusion_samples=2_048,
    duration_max_abs_samples=1,
    audio_level_relative_error_ppm=50_000,
    audio_level_floor_ppm=100,
)


@dataclass(frozen=True, slots=True)
class CorpusCase:
    """One expanded leaf of the closed corpus.

    `case_class` decides what may satisfy the row. `command` is populated only for the command
    class, and `property_family` only for the property class, so a report row can never claim a
    category-level pass on behalf of an individual boundary.
    """

    case_id: str
    case_class: str
    family: str
    leaf: str
    command: str | None = None
    supported: bool = True
    refusal_expected: bool = False
    evidence: str | None = None

    def __post_init__(self) -> None:
        if self.case_class not in CASE_CLASSES:
            raise _reject(f"{self.case_id} has an unknown case class")
        if not _CASE_ID.fullmatch(self.case_id):
            raise _reject(f"{self.case_id} is not a stable lowercase case identifier")
        if (self.command is not None) != (self.case_class == "command"):
            raise _reject(f"{self.case_id} binds a command outside the command class")
        if self.refusal_expected and self.supported is False:
            raise _reject(f"{self.case_id} cannot be both unsupported and an expected refusal")

    def as_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "case_class": self.case_class,
            "family": self.family,
            "leaf": self.leaf,
            "command": self.command,
            "supported": self.supported,
            "refusal_expected": self.refusal_expected,
            "evidence": self.evidence,
        }


def _command_cases() -> Iterator[CorpusCase]:
    for command in NLE_OPERATION_IDS:
        yield CorpusCase(
            case_id=f"command.{command}.accepted",
            case_class="command",
            family="command",
            leaf="accepted",
            command=command,
        )
        yield CorpusCase(
            case_id=f"command.{command}.refused",
            case_class="command",
            family="command",
            leaf="refused",
            command=command,
            refusal_expected=True,
        )


def _numeric_family(family: str, domains: Sequence[NumericDomain]) -> Iterator[CorpusCase]:
    for domain in domains:
        for leaf, _value in domain.distinct_boundaries():
            yield CorpusCase(
                case_id=f"{family}.{domain.field}.{leaf}",
                case_class="property",
                family=family,
                leaf=f"{domain.field}.{leaf}",
                refusal_expected=leaf in ("underflow", "overflow"),
            )


def _named_family(family: str, leaves: Sequence[str]) -> Iterator[CorpusCase]:
    for leaf in leaves:
        yield CorpusCase(
            case_id=f"{family}.{leaf}",
            case_class="property",
            family=family,
            leaf=leaf,
            refusal_expected=f"{family}.{leaf}" in NAMED_REFUSAL_CASES,
        )


def _crop_cases() -> Iterator[CorpusCase]:
    for edge in CROP_EDGES:
        # Each edge independently: absent, a legal nonzero crop, the observation that the surviving
        # source edge really moved, and the refusal that removes the whole image from that side.
        for leaf in ("absent", "legal_nonzero", "source_edge_observed", "full_removal_refused"):
            yield CorpusCase(
                case_id=f"crop.{edge}.{leaf}",
                case_class="property",
                family="crop",
                leaf=f"{edge}.{leaf}",
                refusal_expected=f"crop.{edge}.{leaf}" in NAMED_REFUSAL_CASES,
            )
    # The horizontal and vertical sum constraints are separate negatives: a single "sum" case can be
    # satisfied by whichever axis the implementation happens to check first.
    for axis in ("horizontal", "vertical"):
        yield CorpusCase(
            case_id=f"crop.sum_{axis}_refused",
            case_class="property",
            family="crop",
            leaf=f"sum_{axis}_refused",
            refusal_expected=True,
        )


def _opacity_blend_cases() -> Iterator[CorpusCase]:
    # Two distinguishable layers are required by the fixture spec; black-on-black cannot show a
    # blend difference and would pass every mode.
    for leaf, _value in (
        ("transparent", OPACITY_DOMAIN.lower),
        ("half", OPACITY_DOMAIN.interior),
        ("opaque", OPACITY_DOMAIN.upper),
    ):
        yield CorpusCase(
            case_id=f"opacity_blend.opacity.{leaf}",
            case_class="property",
            family="opacity_blend",
            leaf=f"opacity.{leaf}",
        )
    for leaf in ("underflow", "overflow"):
        yield CorpusCase(
            case_id=f"opacity_blend.opacity.{leaf}",
            case_class="property",
            family="opacity_blend",
            leaf=f"opacity.{leaf}",
            refusal_expected=True,
        )
    for mode in BLEND_MODES:
        yield CorpusCase(
            case_id=f"opacity_blend.blend.{mode}",
            case_class="property",
            family="opacity_blend",
            leaf=f"blend.{mode}",
        )
    yield CorpusCase(
        case_id="opacity_blend.blend.invalid_refused",
        case_class="property",
        family="opacity_blend",
        leaf="blend.invalid_refused",
        refusal_expected=True,
    )


def _text_cases() -> Iterator[CorpusCase]:
    yield from _named_family("text", tuple(f"content.{leaf}" for leaf in TEXT_CONTENT_CASES))
    yield from _named_family("text", tuple(f"font.{leaf}" for leaf in TEXT_FONT_CASES))
    yield from _numeric_family("text", (TEXT_SIZE_DOMAIN, TEXT_LINE_HEIGHT_DOMAIN))
    for weight in TEXT_WEIGHTS:
        yield CorpusCase(
            case_id=f"text.weight.{weight}",
            case_class="property",
            family="text",
            leaf=f"weight.{weight}",
        )
    yield CorpusCase(
        case_id="text.weight.unsupported_refused",
        case_class="property",
        family="text",
        leaf="weight.unsupported_refused",
        refusal_expected=True,
    )
    yield from _named_family("text", tuple(f"style.{value}" for value in TEXT_STYLES))
    yield from _named_family("text", tuple(f"align.{value}" for value in TEXT_ALIGNMENTS))
    yield from _named_family("text", tuple(f"fill_rgba.{leaf}" for leaf in TEXT_FILL_CASES))
    yield from _named_family(
        "text", tuple(f"background_rgba.{leaf}" for leaf in TEXT_BACKGROUND_CASES)
    )


def _edge_trim_cases() -> Iterator[CorpusCase]:
    for property_id in EDGE_TRIM_PROPERTY_IDS:
        yield CorpusCase(
            case_id=f"edge_trim.{property_id}",
            case_class="property",
            family="edge_trim",
            leaf=property_id,
        )
    yield from _named_family("edge_trim", EDGE_TRIM_OBSERVATION_CASES)


#: M25-45 AC45-05: one row per visual layer class, on the 1280 x 720 base whose sources are
#: 512-square. Each isolates its own layer so the landmarks it is judged on are the ones that layer
#: drew, unoccluded, at the full backing the shipped preview now reaches; the base corpus keeps
#: every occluded-composite row it already had, at its own size, unchanged.
HIGH_RESOLUTION_CASES: Final = (
    "layer.primary_video",
    "layer.video_overlay",
    "layer.image_overlay",
    "layer.text_overlay",
)


def _property_cases() -> Iterator[CorpusCase]:
    yield from _numeric_family("transform", TRANSFORM_DOMAINS)
    yield from _crop_cases()
    yield from _opacity_blend_cases()
    yield from _text_cases()
    yield from _named_family("transition", TRANSITION_CASES)
    yield from _named_family("effect", tuple(f"kind.{kind}" for kind in EFFECT_KINDS))
    yield CorpusCase(
        case_id="effect.kind.none_identity_refused",
        case_class="property",
        family="effect",
        leaf="kind.none_identity_refused",
        refusal_expected=True,
    )
    yield from _numeric_family("effect", EFFECT_DOMAINS)
    yield from _named_family("timing", TIMING_CASES)
    yield from _named_family("output", OUTPUT_CASES)
    yield from _named_family("embedded_audio", EMBEDDED_AUDIO_CASES)
    yield from _edge_trim_cases()
    yield from _named_family("history_currentness", HISTORY_CURRENTNESS_CASES)
    yield from _named_family("high_resolution", HIGH_RESOLUTION_CASES)
    yield from _numeric_family("clip_audio", CLIP_AUDIO_DOMAINS)
    yield from _named_family("clip_audio", CLIP_AUDIO_CASES)


def _ui_invariant_cases() -> Iterator[CorpusCase]:
    for row in SIDEBAR_UI_INVARIANT_IDS:
        if row == "overlay_close_return_focus":
            # One case per non-destroy close reason: a single "focus returned" row cannot show that
            # an Escape and a top-level navigation land on the same recorded destination.
            for reason in NON_DESTROY_CLOSE_REASONS:
                leaf = f"{row}.{reason}"
                yield CorpusCase(
                    case_id=f"ui_invariant.{leaf}",
                    case_class="ui_invariant",
                    family="ui_invariant",
                    leaf=leaf,
                    evidence=UI_INVARIANT_EVIDENCE[leaf],
                )
            continue
        yield CorpusCase(
            case_id=f"ui_invariant.{row}",
            case_class="ui_invariant",
            family="ui_invariant",
            leaf=row,
            evidence=UI_INVARIANT_EVIDENCE[row],
        )


def _import_integration_cases() -> Iterator[CorpusCase]:
    for gesture in IMPORT_INTEGRATION_GESTURES:
        # The plan froze this case as `generated_source.explicit_import_then_insert`; the class
        # prefix every other row carries is kept in front of it rather than replacing it.
        leaf = f"{IMPORT_INTEGRATION_CASE}.{gesture}"
        yield CorpusCase(
            case_id=f"import_integration.{leaf}",
            case_class="import_integration",
            family="import_integration",
            leaf=leaf,
        )


def _deferred_negative_cases() -> Iterator[CorpusCase]:
    for seam in DEFERRED_AUDIO_SEAMS:
        yield CorpusCase(
            case_id=f"deferred_negative.{seam}",
            case_class="deferred_negative",
            family="deferred_negative",
            leaf=seam,
            supported=False,
        )
    for surface in AUDIO_NEGATIVE_SURFACES:
        yield CorpusCase(
            case_id=f"deferred_negative.surface_{surface}",
            case_class="deferred_negative",
            family="deferred_negative",
            leaf=f"surface_{surface}",
            supported=False,
        )


@dataclass(frozen=True, slots=True)
class Corpus:
    """The closed, expanded corpus with its per-class counts kept separate."""

    version: str
    operation_profile_id: str
    output_profile_id: str
    cases: tuple[CorpusCase, ...]

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(case.case_id for case in self.cases)

    def counts_by_class(self) -> dict[str, int]:
        counts = dict.fromkeys(CASE_CLASSES, 0)
        for case in self.cases:
            counts[case.case_class] += 1
        return counts

    def commands(self) -> tuple[str, ...]:
        seen: list[str] = []
        for case in self.cases:
            if case.command is not None and case.command not in seen:
                seen.append(case.command)
        return tuple(seen)

    def supported_cases(self) -> tuple[CorpusCase, ...]:
        return tuple(case for case in self.cases if case.supported)

    def as_wire(self) -> dict[str, object]:
        return {
            "version": self.version,
            "operation_profile_id": self.operation_profile_id,
            "output_profile_id": self.output_profile_id,
            "counts_by_class": self.counts_by_class(),
            "cases": [case.as_wire() for case in self.cases],
        }


def build_corpus() -> Corpus:
    """Expand the closed corpus from the accepted contract, never from executed results."""

    cases = (
        *_command_cases(),
        *_property_cases(),
        *_ui_invariant_cases(),
        *_import_integration_cases(),
        *_deferred_negative_cases(),
    )
    identifiers = [case.case_id for case in cases]
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if duplicates:
        raise _reject(f"corpus contains duplicate case identifiers: {duplicates}")
    if len(cases) > MAX_CORPUS_CASES:
        # Resolve an overflow by narrowing the corpus design in the plan, never by dropping rows.
        raise _reject(f"corpus expands to {len(cases)} cases, above the {MAX_CORPUS_CASES} ceiling")
    corpus = Corpus(
        version=CORPUS_VERSION,
        operation_profile_id="h3.authoring.nle_operation.v1",
        output_profile_id=OUTPUT_PROFILE_ID,
        cases=cases,
    )
    covered = set(corpus.commands())
    missing = sorted(set(NLE_OPERATION_IDS) - covered)
    if missing:
        raise _reject(f"corpus is missing command cases for {missing}")
    extra = sorted(covered - set(NLE_OPERATION_IDS))
    if extra:
        raise _reject(f"corpus names commands outside the accepted profile: {extra}")
    return corpus


def join_control_manifest(
    corpus: Corpus, control_manifest: Mapping[str, object]
) -> tuple[tuple[str, str], ...]:
    """Bijectively join corpus commands to `command_rows[].command`, never to display IDs.

    The control manifest's `operation_id` is a display string chosen for the UI surface
    (`track.add`), while `command` is the accepted backend identifier (`create_track`). Joining on
    the display string is the mistake this function exists to make impossible: it would silently
    produce an empty join and read as full coverage.
    """

    rows = control_manifest.get("command_rows")
    if not isinstance(rows, list):
        raise _reject("control manifest has no command_rows table")
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise _reject("control manifest row is not a mapping")
        command = row.get("command")
        operation_id = row.get("operation_id")
        if not isinstance(command, str) or not isinstance(operation_id, str):
            raise _reject("control manifest row is missing its command or operation identifier")
        if command in seen:
            raise _reject(f"control manifest repeats command {command}")
        seen.add(command)
        pairs.append((command, operation_id))
    corpus_commands = set(corpus.commands())
    if seen != corpus_commands:
        missing = sorted(corpus_commands - seen)
        extra = sorted(seen - corpus_commands)
        raise _reject(f"control manifest join is not bijective: missing={missing} extra={extra}")
    return tuple(sorted(pairs))


@dataclass(frozen=True, slots=True)
class ReportAdmission:
    """Whether a set of executed rows may be read as a result for the whole corpus.

    CRITICAL: a report is admitted on identity, never on arithmetic. `summarize` counts statuses,
    and a count cannot tell 306 rows apart from one row repeated 306 times -- that exact
    substitution reported `conformant: true` while 305 required cases had never run. Membership,
    uniqueness and per-class status eligibility are therefore checked against the frozen corpus
    before any conformance claim is allowed, and an unadmitted report still carries every row it
    did execute: refusing the claim is not a licence to delete the evidence.
    """

    duplicates: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    ineligible: tuple[tuple[str, str], ...] = ()

    def admitted(self) -> bool:
        return not (self.duplicates or self.missing or self.unknown or self.ineligible)

    def as_wire(self) -> dict[str, object]:
        return {
            "admitted": self.admitted(),
            "duplicates": list(self.duplicates),
            "missing": list(self.missing),
            "unknown": list(self.unknown),
            "ineligible": [[case_id, status] for case_id, status in self.ineligible],
        }


def admit_report(corpus: Corpus, rows: Sequence[tuple[str, str]]) -> ReportAdmission:
    """Join executed `(case_id, status)` rows to corpus membership and status eligibility.

    Rows arrive as pairs rather than as comparator outcomes so this module stays free of the
    comparator it is meant to police; `semantic_conformance_compare` already imports this one.
    """

    supported = {case.case_id: case.supported for case in corpus.cases}
    seen: list[str] = []
    duplicates: list[str] = []
    unknown: list[str] = []
    ineligible: list[tuple[str, str]] = []
    for case_id, status in rows:
        if status not in REPORT_STATUSES:
            raise _reject(f"{status} is not a declared report status")
        if case_id in seen:
            if case_id not in duplicates:
                duplicates.append(case_id)
            continue
        seen.append(case_id)
        if case_id not in supported:
            unknown.append(case_id)
            continue
        allowed = SUPPORTED_STATUSES if supported[case_id] else UNSUPPORTED_STATUSES
        if status not in allowed:
            ineligible.append((case_id, status))
    return ReportAdmission(
        duplicates=tuple(duplicates),
        missing=tuple(case_id for case_id in supported if case_id not in seen),
        unknown=tuple(unknown),
        ineligible=tuple(ineligible),
    )
