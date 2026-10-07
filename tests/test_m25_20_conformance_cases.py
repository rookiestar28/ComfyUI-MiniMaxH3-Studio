"""M25-20 post-closeout corrective: every corpus row has an executable, honest recipe.

The post-closeout review rejected the item because 306 rows were enumerated, counted and reported as
covered without being executed. A recipe per row is the first half of fixing that, and a recipe is
only worth having if it is bound to the product rather than to an intention. So the tests here do
two separable jobs.

1. **The join is a bijection.** Every corpus row has exactly one recipe and every recipe names a
   corpus row, and the two agree about class, support and whether a refusal is expected. A recipe
   set that could add or drop a case would let the runner decide its own coverage, which is the
   defect being repaired rather than a new way to express it.
2. **The declarations are true of the real decoder.** Every accepted property recipe is driven
   through the actual `decode_public_snapshot` and must be admitted; every snapshot-layer refusal is
   driven through it and must produce the exact declared code. Nineteen declarations were wrong when
   this was first run -- an effect field that cannot leave identity while its kind is `none`, four
   output bounds refused by the integer check rather than the profile rule, a source range that is
   admitted until the offset passes the source frame count, and five refusals that belong to the
   command and currentness layers rather than to the snapshot decoder. Each was corrected against
   the contract that raises it, never against the observation.

These are still declarations. They say what should happen; the qualification runner says what did.
"""

from __future__ import annotations

import copy
import json
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Final, cast

import pytest

from comfyui_h3_context.adapters.authoring_fonts import (
    AuthoringFontError,
    load_packaged_font_manifest,
    require_text_coverage,
)
from comfyui_h3_context.core.composition_contract import (
    NLE_OPERATION_IDS,
    CompositionContractError,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.semantic_conformance import CASE_CLASSES, build_corpus
from comfyui_h3_context.core.semantic_conformance_cases import (
    BASE_FIXTURE_NAMES,
    CORPUS_BASE,
    FONT_ASSET,
    FONT_GLYPH_UNSUPPORTED,
    IMAGE_ASSET,
    IMAGE_CLIP,
    INVALID_CONTRACT,
    OBSERVATION_KINDS,
    OVERLAY_ASSET,
    OVERLAY_CLIP,
    PRIMARY_ASSET,
    PRIMARY_CLIP,
    PRIMARY_TRACK,
    REFUSAL_CODES,
    REFUSAL_LAYERS,
    SETUP_CURSOR,
    STALE_FINGERPRINT,
    TIMELINE_FINGERPRINT,
    TITLE_CLIP,
    CaseRecipe,
    apply_edits,
    build_recipes,
    normalize_base,
)
from comfyui_h3_context.core.semantic_conformance_drive import command_phases, run_setup
from comfyui_h3_context.core.semantic_conformance_expect import (
    carries_audio,
    derive_audio_onsets,
    derive_expectation,
    derive_source_mapping,
)
from comfyui_h3_context.core.timeline_history import (
    TIMELINE_TRANSACTION_SCHEMA,
    TimelineHistoryState,
    apply_timeline_transaction,
    decode_timeline_transaction,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
#: A well-formed fingerprint that binds nothing, so a rebase is refused for being stale rather
#: than for being malformed. The two are different refusals and only one is under test.
STALE = "sha256:" + "0" * 64

BOOK = build_recipes()
CORPUS = build_corpus()


def _base(base: str = CORPUS_BASE) -> dict[str, Any]:
    """A corpus base's snapshot; a row is always driven from the base its own recipe names."""

    path = FIXTURES / BASE_FIXTURE_NAMES[base]
    document = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    return cast(dict[str, Any], copy.deepcopy(document["snapshot"]))


def _decoded(recipe: CaseRecipe) -> None:
    """Apply a recipe to its own base composition and decode it for real."""

    wire = _base(recipe.base)
    apply_edits(wire, recipe)
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    decode_public_snapshot(wire)


def _properties(*, refusing: bool) -> list[CaseRecipe]:
    return [
        recipe
        for recipe in BOOK.recipes
        if recipe.case_class == "property"
        and recipe.refusal_layer in ((None,) if not refusing else ("snapshot",))
        and (recipe.refusal_code is not None) is refusing
    ]


def _identity(recipe: CaseRecipe) -> str:
    return recipe.case_id


# --------------------------------------------------------------------------------------------
# The join
# --------------------------------------------------------------------------------------------


def test_every_corpus_row_has_exactly_one_recipe_and_no_recipe_invents_a_row() -> None:
    assert [recipe.case_id for recipe in BOOK.recipes] != []
    assert set(BOOK.by_id) == set(CORPUS.case_ids)
    # M25-45 appended the four high-resolution rows to the accepted 306 rather than replacing any
    # of them, and M25-77 the 22 `clip_audio` rows and the two rows of its `set_clip_audio`
    # command, so the total moves and the original set is asserted to be untouched beneath it.
    assert len(BOOK.recipes) == len(CORPUS.cases) == 334
    high_resolution = {case.case_id for case in CORPUS.cases if case.family == "high_resolution"}
    clip_audio = {case.case_id for case in CORPUS.cases if case.family == "clip_audio"}
    clip_audio_command = {"command.set_clip_audio.accepted", "command.set_clip_audio.refused"}
    assert len(high_resolution) == 4
    assert len(clip_audio) == 22
    assert clip_audio_command <= set(CORPUS.case_ids)
    assert len(set(CORPUS.case_ids) - high_resolution - clip_audio - clip_audio_command) == 306
    assert len(set(BOOK.by_id)) == len(BOOK.recipes)


def test_recipe_classes_are_counted_the_way_the_corpus_counts_them() -> None:
    counts = dict.fromkeys(CASE_CLASSES, 0)
    for recipe in BOOK.recipes:
        counts[recipe.case_class] += 1
    assert counts == CORPUS.counts_by_class()


def test_each_observation_kind_carries_the_rows_its_class_implies() -> None:
    # The numbers are asserted so that moving a row between kinds -- answering a rendered row with
    # a shell observation, say -- cannot happen quietly. The two edge-trim refusals moved from
    # `contract_refusal` to `command_refusal` when they were given the payload the command decoder
    # actually refuses: a row that names no command cannot be answered by the command layer at all,
    # and the qualification runner blocked both for exactly that reason.
    # M25-45's four high-resolution rows are `render_and_browser` like the rows they sit beside:
    # 150 + 4. M25-77's 22 `clip_audio` rows are 14 that render (+14) and the eight the snapshot
    # decoder refuses (+8): two out-of-range leaves for each of its three fields, the fade sum
    # and the member on a clip without bound audio. Its `set_clip_audio` command adds one
    # accepted and one refused command row (+1, +1), as every command does.
    assert BOOK.counts_by_observation() == {
        "render_and_browser": 168,
        "contract_refusal": 70,
        "command_effect": 34,
        "command_refusal": 36,
        "shell_invariant": 11,
        "import_integration": 2,
        "declared_negative": 13,
    }
    assert set(BOOK.counts_by_observation()) == set(OBSERVATION_KINDS)


def test_every_accepted_command_has_an_accepted_and_a_refused_recipe() -> None:
    for command in NLE_OPERATION_IDS:
        accepted = BOOK.by_id[f"command.{command}.accepted"]
        refused = BOOK.by_id[f"command.{command}.refused"]
        assert accepted.command is not None and accepted.command.kind == command
        assert refused.command is not None and refused.command.kind == command
        assert refused.refusal_code in REFUSAL_CODES
        assert refused.refusal_layer == "command"
    assert len([r for r in BOOK.recipes if r.case_class == "command"]) == 2 * len(NLE_OPERATION_IDS)


def test_a_refusal_never_asks_for_an_artifact_and_always_names_its_layer() -> None:
    for recipe in BOOK.recipes:
        if recipe.refusal_code is None:
            assert recipe.refusal_layer is None
            continue
        # A refusal that still rendered would be the failure a code-only assertion cannot see.
        assert recipe.renders is False, recipe.case_id
        assert recipe.refusal_layer in REFUSAL_LAYERS, recipe.case_id


def test_every_declared_negative_carries_its_reason_and_renders_nothing() -> None:
    negatives = [r for r in BOOK.recipes if r.observation == "declared_negative"]
    assert len(negatives) == 13
    for recipe in negatives:
        assert recipe.supported is False
        assert recipe.renders is False
        assert recipe.unsupported_reason
        # A declared negative proves an absence; it is never a skip and never a refusal code.
        assert recipe.refusal_code is None


def test_shell_and_import_rows_never_carry_a_command_or_an_artifact() -> None:
    for recipe in BOOK.recipes:
        if recipe.observation != "shell_invariant":
            continue
        assert recipe.command is None
        assert recipe.renders is False
    imports = [r for r in BOOK.recipes if r.observation == "import_integration"]
    assert len(imports) == 2
    assert {r.case_id.rsplit(".", 1)[-1] for r in imports} == {"pointer", "keyboard"}


def test_the_rendering_set_is_exactly_the_rows_that_produce_an_artifact() -> None:
    rendering = BOOK.rendering()
    # 185 accepted rows, M25-45's four, M25-77's fourteen and its accepted command row.
    assert len(rendering) == 204
    assert all(recipe.refusal_code is None for recipe in rendering)
    assert all(recipe.observation != "declared_negative" for recipe in rendering)


# --------------------------------------------------------------------------------------------
# The declarations, against the real decoder
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", _properties(refusing=False), ids=_identity)
def test_every_accepted_property_recipe_is_admitted_by_the_real_decoder(
    recipe: CaseRecipe,
) -> None:
    """A recipe that the product refuses is not a conformance case; it is a broken fixture.

    Nine effect rows failed here first: `composition_contract` refuses a `none` effect that carries
    anything but identity values, so a brightness interior applied to the fixture's default effect
    was refused for a reason that had nothing to do with the boundary under test.
    """

    _decoded(recipe)


@pytest.mark.parametrize("recipe", _properties(refusing=True), ids=_identity)
def test_every_snapshot_refusal_produces_its_exact_declared_code(recipe: CaseRecipe) -> None:
    with pytest.raises(CompositionContractError) as refusal:
        _decoded(recipe)
    assert getattr(refusal.value, "code", None) == recipe.refusal_code


def test_a_command_or_currentness_refusal_is_not_claimed_of_the_snapshot_decoder() -> None:
    """The three refusing authorities are separate, and naming the wrong one passes for the wrong
    reason.

    An edge-trim draft refusal is a refusal of the draft, not of a composition -- the composition it
    would have produced is never built. A currentness refusal happens when a plan is bound, and a
    snapshot that is internally consistent decodes cleanly however stale its binding is. Both were
    declared against the snapshot decoder at first, and both were accepted by it.
    """

    deferred = [
        recipe
        for recipe in BOOK.recipes
        if recipe.case_class == "property" and recipe.refusal_layer in ("command", "currentness")
    ]
    assert {recipe.case_id for recipe in deferred} == {
        "edge_trim.invalid_transition_refused",
        "edge_trim.draft_refused",
        "history_currentness.stale_snapshot_refused",
        "history_currentness.source_replaced_refused",
        "history_currentness.output_replaced_refused",
    }
    for recipe in deferred:
        # The snapshot decoder really does admit these, which is why they must not claim it.
        _decoded(recipe)


@pytest.mark.parametrize(
    "recipe",
    [r for r in BOOK.recipes if r.edits and not r.identity_expected],
    ids=_identity,
)
def test_a_recipe_that_means_to_change_the_composition_actually_changes_it(
    recipe: CaseRecipe,
) -> None:
    """A silently ineffective edit would be compared against an unmodified composition and pass.

    Ten rows were caught by this: they restated a value the accepted fixture already carried. Those
    are legitimate observations of how the accepted value renders, so they are declared as such and
    excluded here by name rather than by the edit quietly doing nothing.
    """

    wire = _base(recipe.base)
    apply_edits(wire, recipe)
    # GUARD: compare against the *normalized* base. `apply_edits` renames the fixture's placeholder
    # font on the way through, so a comparison with the raw fixture is different for every row
    # whatever its edits did -- this assertion passed for a row whose two edits set an asset's
    # audio fields to the values the base already declared, which is exactly what it exists to
    # catch.
    baseline = _base(recipe.base)
    normalize_base(baseline)
    assert wire != baseline, recipe.case_id


def test_an_edit_naming_a_field_outside_the_accepted_contract_is_refused() -> None:
    from comfyui_h3_context.core.semantic_conformance import SemanticConformanceError
    from comfyui_h3_context.core.semantic_conformance_cases import SnapshotEdit

    invented = CaseRecipe(
        case_id="transform.anchor_x_bp.identity",
        case_class="property",
        family="transform",
        observation="render_and_browser",
        edits=(SnapshotEdit("clip.transform", "skew_bp", 10),),
    )
    with pytest.raises(SemanticConformanceError, match="not a field"):
        apply_edits(_base(), invented)


def test_an_edit_naming_a_clip_the_fixture_does_not_have_is_refused() -> None:
    from comfyui_h3_context.core.semantic_conformance import SemanticConformanceError
    from comfyui_h3_context.core.semantic_conformance_cases import SnapshotEdit

    invented = CaseRecipe(
        case_id="transform.anchor_x_bp.identity",
        case_class="property",
        family="transform",
        observation="render_and_browser",
        edits=(SnapshotEdit("clip.transform", "anchor_x_bp", 10, "clip-absent"),),
    )
    # Otherwise the decoder would refuse the composition for a missing clip and the row would look
    # like the refusal it was supposed to be proving.
    with pytest.raises(SemanticConformanceError, match="no clip"):
        apply_edits(_base(), invented)


def test_only_the_refused_timing_rows_write_a_table_and_the_rendered_ones_use_a_measured_one() -> (
    None
):
    """A rendering timing row is a composition over a real source, never a declared table.

    The first corpus wrote a literal three-landmark table onto the primary for every timing leaf,
    and the product's source binding -- which measures the real file -- refused each one as
    `source_facts_mismatch`, so nine rows named after source timing were never rendered at all
    (B-58). The rule now: a timing row that renders keeps every asset's table exactly as the
    product measured it (the base's, which equals the media declaration's `pts_table()`), and only
    the rows the decoder refuses before any source is bound may carry a literal table.
    """

    from comfyui_h3_context.core.semantic_conformance_cases import TIMING_LANDMARKS
    from comfyui_h3_context.core.semantic_conformance_media import TIMING_ASSET, profile_for

    timing = [r for r in BOOK.recipes if r.family == "timing"]
    refused = {r.case_id.split(".", 1)[1] for r in timing if r.refusal_code is not None}
    rendered = [r for r in timing if r.refusal_code is None]
    assert refused == set(TIMING_LANDMARKS)
    assert len(rendered) == 9
    for leaf, table in TIMING_LANDMARKS.items():
        assert table, leaf
        assert all(isinstance(frame, int) and isinstance(pts, int) for frame, pts in table), leaf

    base = _base()
    declared = {asset["asset_id"]: asset.get("landmarks") for asset in base["assets"]}
    profile = profile_for(TIMING_ASSET)
    assert profile is not None
    assert [
        (row["frame_index"], row["pts"], row["duration_ticks"]) for row in declared[TIMING_ASSET]
    ] == list(profile.pts_table()), "the base declares the table the product measured"
    swapped = 0
    for recipe in rendered:
        wire = _base()
        apply_edits(wire, recipe)
        for asset in wire["assets"]:
            if asset.get("kind") == "video":
                assert asset["landmarks"] == declared[asset["asset_id"]], recipe.case_id
        if any(clip["asset_id"] == TIMING_ASSET for clip in wire["clips"]):
            swapped += 1
        assert wire != base, recipe.case_id
    assert swapped == 2, "the different-rate and unequal-interval rows read the timing source"


def test_a_cut_is_two_primary_clips_meeting_at_a_frame() -> None:
    """`timing.cut` needs a second clip, which no single-clip edit can express (B-58)."""

    recipe = next(r for r in BOOK.recipes if r.case_id == "timing.cut")
    wire = _base()
    apply_edits(wire, recipe)
    primary = sorted(
        (clip for clip in wire["clips"] if clip["track_id"] == PRIMARY_TRACK),
        key=lambda clip: clip["start_frame"],
    )
    assert [clip["clip_id"] for clip in primary] == [PRIMARY_CLIP, "clip-main-tail"]
    first, second = primary
    assert first["start_frame"] + first["duration_frames"] == second["start_frame"]
    assert second["source_start_frame"] == 0, "the tail restarts the source, so the cut is visible"
    assert second["asset_id"] == first["asset_id"]
    with pytest.raises(ContractValidationError):
        # A second copy of an existing clip id is refused rather than silently duplicated.
        apply_edits(wire, recipe)


# --------------------------------------------------------------------------------------------
# The command declarations, against the real timeline command decoder
# --------------------------------------------------------------------------------------------


def _resolve(payload: Any, cursor: str | None, fingerprint: str) -> Any:
    """Fill the payload tokens a recipe cannot know before its setup has run."""

    if isinstance(payload, str):
        if payload == SETUP_CURSOR:
            return cursor
        if payload == TIMELINE_FINGERPRINT:
            return fingerprint
        if payload == STALE_FINGERPRINT:
            return STALE
        return payload
    if isinstance(payload, dict):
        return {key: _resolve(value, cursor, fingerprint) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_resolve(item, cursor, fingerprint) for item in payload]
    return payload


def _transact(state: Any, kind: str, payload: Any, identifier: str) -> Any:
    current = state.snapshot
    transaction = decode_timeline_transaction(
        {
            "schema": TIMELINE_TRANSACTION_SCHEMA,
            "request_id": identifier,
            "transaction_id": f"tx-{identifier}",
            "workspace_handle": current.workspace_handle,
            "expected_workspace_revision": current.workspace_revision,
            "expected_timeline_revision": current.timeline_revision,
            "expected_timeline_fingerprint": current.timeline_fingerprint,
            "commands": [{"kind": kind, "payload": payload}],
        }
    )
    return apply_timeline_transaction(state, transaction)


def _run_command(recipe: CaseRecipe) -> tuple[str | None, str, str]:
    """Run a recipe's setup and then its subject command through the real history.

    Returns `(refusal code or None, timeline fingerprint before, after)`. Setup runs as its own
    transactions so its receipts stay apart from the operation under test: a command row that
    credited its setup with the effect would be measuring the wrong thing.
    """

    wire = _base()
    apply_edits(wire, recipe)
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    state = TimelineHistoryState.initialize(decode_public_snapshot(wire))
    cursor: str | None = None
    for index, step in enumerate(recipe.setup):
        payload = _resolve(dict(step.payload), cursor, state.snapshot.timeline_fingerprint)
        state, receipt = _transact(state, step.kind, payload, f"setup-{index}")
        cursor = receipt.history_cursor
    assert recipe.command is not None
    payload = _resolve(dict(recipe.command.payload), cursor, state.snapshot.timeline_fingerprint)
    before = state.snapshot.timeline_fingerprint
    try:
        result, _ = _transact(state, recipe.command.kind, payload, "subject")
    except ContractValidationError as exc:
        return getattr(exc, "code", None), before, before
    return None, before, result.snapshot.timeline_fingerprint


@pytest.mark.parametrize(
    "recipe",
    [r for r in BOOK.recipes if r.observation == "command_effect"],
    ids=_identity,
)
def test_every_accepted_command_reaches_the_real_decoder_and_moves_the_timeline(
    recipe: CaseRecipe,
) -> None:
    """An accepted command row is worth nothing if the command cannot actually be applied.

    Ten of these were unreachable as first declared: a non-empty track cannot be removed, merge,
    roll and slide need adjacent clips, a range command must name a remainder for every clip it
    splits, a split or slip has to land on a declared source landmark, and undo and redo need a
    setup that actually moved the timeline rather than a selection, which is not timeline state.
    Each now carries the setup that makes it reachable.
    """

    code, before, after = _run_command(recipe)
    assert code is None, f"{recipe.case_id} was refused with {code}"
    # Every one of the 34 changes the timeline. The plan allows selection, lock and empty-track
    # rows to leave the *output* invariant; none of them leaves the timeline identity invariant,
    # and a row that did would be comparing a composition against itself.
    assert after != before, recipe.case_id


@pytest.mark.parametrize(
    "recipe",
    [r for r in BOOK.recipes if r.observation == "command_refusal"],
    ids=_identity,
)
def test_every_refused_command_produces_its_exact_declared_code(recipe: CaseRecipe) -> None:
    """Seventeen of these declared `invalid_contract` and were wrong.

    The timeline command decoder refuses an unknown clip or track, an unsupported edge or an
    out-of-bounds integer as `invalid_command`; a command whose *result* would violate the
    composition contract carries that contract's code through instead; and the history and rebase
    authorities have codes of their own. Four authorities, four vocabularies.
    """

    code, before, after = _run_command(recipe)
    assert code == recipe.refusal_code, recipe.case_id
    # A refused command mutates nothing.
    assert after == before, recipe.case_id


def test_the_command_refusal_codes_span_every_authority_that_refuses() -> None:
    observed = {
        recipe.refusal_code for recipe in BOOK.recipes if recipe.observation == "command_refusal"
    }
    # If this ever collapses to one code again, the rows have stopped distinguishing the
    # authorities and the declaration has become a formality.
    assert observed == {
        "invalid_command",
        "invalid_contract",
        "history_cursor_invalid",
        "rebase_conflict",
    }


# ---------------------------------------------------------------------------------------------
# What each row actually executes, and whether two rows execute the same experiment
# ---------------------------------------------------------------------------------------------

#: The snapshot members that are the *composition*. The revision and fingerprint members are
#: excluded on purpose.
#:
#: GUARD: do not key this on `public_fingerprint`. That fingerprint covers `timeline_revision` and
#: `timeline_fingerprint` as well, so every row that runs any setup at all gets a fresh value
#: whatever the setup actually did -- a duplicate check keyed on it would report 186 distinct rows
#: while a third of them rendered the untouched accepted fixture, which is precisely the defect
#: these two tests exist to pin.
_COMPOSITION_MEMBERS: Final = ("output", "assets", "tracks", "clips", "audio_extension")


def _executed(recipe: CaseRecipe) -> tuple[dict[str, Any], ...]:
    """The composition wires this row actually runs, through the shared driver the stages use.

    A command row executes a pair -- the composition before its subject command and the composition
    after it -- because a command's claim is what it changed and one artifact cannot express that.
    """

    if recipe.command is not None:
        return command_phases(recipe, _base(recipe.base)).wires()
    setup = run_setup(recipe, _base(recipe.base))
    assert setup.state is not None, f"{recipe.case_id}: {setup.error}"
    return (dict(setup.state.snapshot.to_wire()),)


def _composition_key(recipe: CaseRecipe) -> str:
    return json.dumps(
        [{member: wire[member] for member in _COMPOSITION_MEMBERS} for wire in _executed(recipe)],
        sort_keys=True,
    )


def _claim_key(recipe: CaseRecipe) -> str:
    """What this row expects, computed the way the join computes it, with the row's name removed."""

    return repr(
        tuple(replace(derive_expectation(recipe, wire), case_id="") for wire in _executed(recipe))
    )


def _grouped(key: Any) -> dict[str, tuple[str, ...]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for recipe in BOOK.rendering():
        groups[key(recipe)].append(recipe.case_id)
    return {item: tuple(ids) for item, ids in groups.items() if len(ids) > 1}


def _assert_pinned(groups: dict[str, tuple[str, ...]], pinned: Any, label: str) -> None:
    """Every set of rows that share something must be exactly one pinned group, named and reasoned.

    GUARD: the repair for a new duplicate is a corrected recipe, not a new allow-list entry. Add
    one only when the rows are *legitimately* one experiment -- an identity row whose declared value
    is the one the fixture already carries, a pair whose pictures are identical by construction, or
    a row whose subject the expectation cannot express -- and write the concrete reason. Never widen
    an entry to absorb an unrelated row, and never relax the universal to make a group fit.
    """

    declared = {frozenset(ids): name for name, (_reason, ids) in pinned.items()}
    for ids in groups.values():
        assert frozenset(ids) in declared, (
            f"{label}: these rows execute the same experiment and are not pinned: {sorted(ids)}"
        )
    seen = {frozenset(ids) for ids in groups.values()}
    for name, (_reason, ids) in pinned.items():
        assert frozenset(ids) in seen, (
            f"{label}: the pinned group {name} no longer shares anything; delete the entry"
        )


def test_every_rendering_rows_setup_applies_through_the_shared_driver() -> None:
    """A row whose own setup the decoder refuses is not a case, it is a broken recipe.

    Six rows were refused outright for the whole life of the corpus and nothing noticed, because
    every stage derived its composition from the base plus `edits` and never ran a `setup`. Two
    rules did it: a start-edge trim or a slip must land on a declared source landmark, and the
    primary clip already fills the output so an outward trim needs room made for it first.
    """

    broken = []
    for recipe in BOOK.rendering():
        setup = run_setup(recipe, _base(recipe.base))
        if setup.state is None:
            broken.append((recipe.case_id, setup.error))
    assert not broken, broken


#: The accepted command rows whose payload legitimately leaves the composition untouched.
#:
#: GUARD: exactly one command qualifies, and only because selection is not part of the public
#: snapshot at all. Every other accepted payload has to change the composition it is applied to. A
#: payload that restates the value the fixture already carries makes the row's `before` and
#: `after` the same picture, and the row then proves only that the decoder accepts a no-op --
#: `set_transition` shipped exactly that, carrying the four-frame dissolve the overlay clip already
#: had. Do not add a command here to make this test pass; give it a payload with an effect.
COMMANDS_WITHOUT_A_COMPOSITION_EFFECT: Final = ("command.select_clips.accepted",)


def test_every_accepted_command_payload_actually_changes_the_composition() -> None:
    """An accepted command row's evidence is the difference between its two phases."""

    unmoved = []
    for recipe in BOOK.recipes:
        if recipe.observation != "command_effect":
            continue
        phases = command_phases(recipe, _base(recipe.base))
        assert phases.before.public_fingerprint != phases.after.public_fingerprint, recipe.case_id
        before, after = (
            {member: wire[member] for member in _COMPOSITION_MEMBERS}
            for wire in command_phases(recipe, _base(recipe.base)).wires()
        )
        if before == after:
            unmoved.append(recipe.case_id)
    assert tuple(sorted(unmoved)) == COMMANDS_WITHOUT_A_COMPOSITION_EFFECT, unmoved


#: The accepted commands whose rendered picture cannot change, each with the concrete reason its
#: two phases are the same picture. A command row's proof is that its "after" artifact fails its
#: "before" expectation, so a command missing from this table has to state an expectation its own
#: change moves -- through `semantic_conformance_expect.COMMAND_LANDMARKS` -- or it closes on its
#: container. GUARD: never add a command here to make the test below pass; a command that changes
#: which clip is present at some frame, or what a present clip shows, belongs in that table.
COMMANDS_INVISIBLE_BY_CONSTRUCTION: Final[dict[str, str]] = {
    "create_track": "an empty track paints nothing",
    "remove_track": "the track removed is the empty one the setup step created",
    "reorder_track": (
        "the overlay's footprint never meets the title's band, and the still is white multiply, "
        "which commutes with everything; the stack renders the same in either order"
    ),
    "set_track_locked": "locking is state an artifact cannot show",
    "split_clip": "the two halves are contiguous pieces of the same source at the same place",
    "merge_clips": "the halves it rejoins were contiguous pieces of the same source",
    "roll_edit": "both sides of the cut are the same untimed still at the same place",
    "slide_clip": "every clip it moves between is the same untimed still at the same place",
    "select_clips": "selection is state an artifact cannot show",
}


def test_every_accepted_command_either_moves_its_expectation_or_is_invisible_by_construction() -> (
    None
):
    """A command row closes on the difference between its phases, so there has to be one.

    The composition-level test above only proves the payload changed the wire; what the join
    compares is the *expectation* derived from each phase, and a command whose two expectations
    are equal is judged on nothing -- `set_track_enabled` disabled the overlay track and the row
    passed because no landmark it required could see the overlay go. Every accepted command
    either states an expectation its change moves, or is pinned here with the reason its picture
    cannot move.
    """

    unmoved: dict[str, str] = {}
    for recipe in BOOK.recipes:
        if recipe.observation != "command_effect" or recipe.command is None:
            continue
        before, after = (derive_expectation(recipe, wire) for wire in _executed(recipe))
        if replace(before, case_id="") == replace(after, case_id=""):
            unmoved[recipe.command.kind] = recipe.case_id
    assert set(unmoved) == set(COMMANDS_INVISIBLE_BY_CONSTRUCTION), unmoved


#: The only rendering rows allowed to execute the same composition, each group named with the
#: concrete reason it is legitimately one experiment rather than two.
#:
#: A command row's composition is the *pair* its two render phases are pictures of, so a command
#: whose "after" happens to equal another row's single composition is not a duplicate of it.
SHARED_COMPOSITIONS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "the accepted base composition, observed field by field": (
        "Every member declares the value the accepted fixture already carries -- that is what "
        "`identity`, `absent` and `screen` mean -- or has no composition of its own to reach: a "
        "selection is not timeline state, an unknown render-job outcome is a fact about a job, and "
        "the import journey is a browser interaction whose two rows differ by input modality "
        "(pointer, keyboard) rather than by anything a composition can hold. `overlay_excluded` "
        "joined them when the base began declaring what a claimed source really carries: an "
        "overlay source has bound audio like any other, and excluding it is a per-clip policy "
        "decision, so the base is already the composition that row is about. `timing.cfr_24` "
        "rejoined when the timing rows became compositions over real sources (B-58): it is the "
        "identity statement about the constant-rate primary, a clip from its source frame 0, "
        "which is the base. Each member still observes a different declared field of that one "
        "picture.",
        (
            "timing.cfr_24",
            "transform.anchor_x_bp.identity",
            "transform.anchor_y_bp.identity",
            "transform.position_x_bp.identity",
            "transform.position_y_bp.identity",
            "transform.scale_x_bp.identity",
            "transform.scale_y_bp.identity",
            "transform.rotation_mdeg.identity",
            "crop.left_bp.absent",
            "crop.top_bp.absent",
            "crop.right_bp.absent",
            "crop.bottom_bp.absent",
            "opacity_blend.blend.screen",
            "text.line_height_bp.identity",
            "text.weight.700",
            "text.style.normal",
            "text.align.center",
            "text.fill_rgba.identity",
            "transition.cross_dissolve_legal",
            "transition.first_included_frame",
            "transition.interior_frame",
            "transition.last_included_frame",
            "transition.exclusive_end",
            "effect.kind.none",
            "output.color_policy_bt709_sdr_limited",
            "output.frame_grid_exact_24",
            "output.container_mp4",
            "output.video_codec_h264",
            "output.pixel_format_yuv420p",
            "output.pixel_aspect_identity",
            "embedded_audio.primary_audible",
            "embedded_audio.overlay_excluded",
            "history_currentness.selection_invariant",
            "history_currentness.unknown_outcome_pending",
            "history_currentness.unknown_outcome_failed",
            "import_integration.generated_source.explicit_import_then_insert.pointer",
            "import_integration.generated_source.explicit_import_then_insert.keyboard",
        ),
    ),
    "the effect switched on at its identity values": (
        "`composition_contract` refuses a `none` effect that carries anything but identity values, "
        "so each effect field's identity row has to enable `color_adjust_v1` first, which is the "
        "whole of what `effect.kind.color_adjust_v1` is. The four rows then observe four different "
        "declared fields of that one composition.",
        (
            "effect.kind.color_adjust_v1",
            "effect.brightness_permille.identity",
            "effect.contrast_permille.identity",
            "effect.saturation_permille.identity",
        ),
    ),
    "the clip audio base, observed field by field": (
        "An audio member at its identity values is no member at all -- the `clip.audio` edit "
        "writes none, which is how the wire states identity -- so the three identity rows of the "
        "`clip_audio` family each render that family's own base, the tone clip at its source "
        "level. Each still names a different declared field of that one composition, and the "
        "base's level is the reference every other row of the family is a change from.",
        (
            "clip_audio.gain_mb.identity",
            "clip_audio.fade_in_frames.identity",
            "clip_audio.fade_out_frames.identity",
        ),
    ),
    "three commands whose accepted payload is one opacity change": (
        "`redo` replays the opacity edit its setup undid and `rebase_transaction` carries the same "
        "edit as its rebased command, so all three land on the same before/after pair by "
        "construction. What separates them is which authority accepted the command, which the "
        "artifact cannot show and the receipt can.",
        (
            "command.set_opacity_blend.accepted",
            "command.redo.accepted",
            "command.rebase_transaction.accepted",
        ),
    ),
    "moving one clip alone and moving it as a group of one": (
        "`move_group` is `move_clip`'s multi-clip form and the accepted payload moves the same "
        "overlay clip by the same delta, so the pictures are identical by construction; the rows "
        "differ in which decoder accepted the payload.",
        ("command.move_clip.accepted", "command.move_group.accepted"),
    ),
    "a trim and a ripple trim with one clip in scope": (
        "A ripple shifts the clips after the trimmed one, and the accepted `ripple_trim` payload "
        "scopes the primary track, which holds exactly one clip in the accepted fixture. With "
        "nothing to ripple the two commands produce the same picture by construction.",
        ("command.trim_clip.accepted", "command.ripple_trim.accepted"),
    ),
    **{
        f"the {edge} crop admitted, then its source edge measured": (
            "The corpus declares the same legal crop twice on purpose: one row asks whether the "
            "decoder admits it, the next asks whether the surviving source edge moved by exactly "
            "the cropped amount. Same composition, two questions.",
            (f"crop.{edge}_bp.legal_nonzero", f"crop.{edge}_bp.source_edge_observed"),
        )
        for edge in ("left", "top", "right", "bottom")
    },
    "the longest admitted dissolve, as a bound and as a ramp": (
        "Eight frames is both the upper bound `transition.duration_upper` names and the longest "
        "ramp available for `transition.alpha_progression` to measure, so the two rows necessarily "
        "share one composition.",
        ("transition.alpha_progression", "transition.duration_upper"),
    ),
}

#: The only rendering rows allowed to state the same executed expectation. This is the stricter of
#: the two: a row that reaches a different composition but claims the same thing about it is still
#: two runs of one experiment, and the difference goes unobserved.
SHARED_CLAIMS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "the clip audio identities the clip audio base already carries": (
        "The three identity rows render one composition (see `SHARED_COMPOSITIONS`), so they "
        "state one expectation: the tone at its source level in every window, and the base's "
        "source mapping. That is what the rows are for -- the declared identity value of each "
        "field really is an identity -- and every other row of the family states a level these "
        "do not.",
        (
            "clip_audio.gain_mb.identity",
            "clip_audio.fade_in_frames.identity",
            "clip_audio.fade_out_frames.identity",
        ),
    ),
    "two commands that take the same still away": (
        "`remove_clip` deletes the image clip and `set_clip_enabled` switches the same clip off; "
        "the composition that renders is the same three layers either way, so both phases of "
        "each row state the same picture: the still's mark present before, and the primary's "
        "field where it was after. The rows still differ in the composition they reach -- one "
        "has no image clip, the other a disabled one -- which the per-phase fingerprint check "
        "keeps apart.",
        (
            "command.remove_clip.accepted",
            "command.set_clip_enabled.accepted",
        ),
    ),
    "a clip moved alone and the same clip moved as a group of one": (
        "`move_clip` and `move_group` both shift the overlay one frame later on its own track; a "
        "group of one clip is that clip, so the composition after is the same and so is what it "
        "states -- the overlay's colour a frame later than before at both of its ends. The rows "
        "differ in the command that reached it, which is the subject, and the per-phase "
        "fingerprint check still holds each phase to its own composition.",
        (
            "command.move_clip.accepted",
            "command.move_group.accepted",
        ),
    ),
    "accepted commands whose picture cannot move": (
        "`FAMILY_LANDMARKS['command']` is empty and none of these commands appears in "
        "`COMMAND_LANDMARKS`, so each row claims only the universal landmarks, and none of them "
        "puts anything over the primary's identity row, so every one states the same 48 legible "
        "identity frames -- in both phases, because each is invisible by construction for the "
        "reason `COMMANDS_INVISIBLE_BY_CONSTRUCTION` records. Their rendered difference is "
        "genuinely nothing; the join still separates the rows by the per-phase public fingerprint "
        "identity check, so they are not vacuous. Every other accepted command states a landmark "
        "its own change moves (`COMMAND_LANDMARKS`), which is what makes its claim its own.",
        (
            "command.create_track.accepted",
            "command.remove_track.accepted",
            "command.reorder_track.accepted",
            "command.set_track_locked.accepted",
            "command.split_clip.accepted",
            "command.merge_clips.accepted",
            "command.roll_edit.accepted",
            "command.slide_clip.accepted",
            "command.select_clips.accepted",
        ),
    ),
    "the same committed wash reached through redo and through a rebase": (
        "Both rows end on the composition where the still is a 60% white wash in normal blend, "
        "reached by redoing an undone edit and by rebasing a transaction onto it. That wash lifts "
        "every black identity cell past the observer's threshold, so neither row can state an "
        "identity and both substitute the composited colour at the same points. They are one "
        "picture reached by two history paths, which is what each row is for.",
        ("command.redo.accepted", "command.rebase_transaction.accepted"),
    ),
    "text rows that need font metrics to be distinguishable": (
        "`TextObservation` carries content, font identity, line count, weight, style, align and "
        "per-line bounds. Size, line height, fill colour and background colour are expressible "
        "only through the per-line bounds, which need real font metrics; until those are derived "
        "these rows state the base text observation. The identity members restate the value the "
        "fixture already carries and are legitimately here. These eleven keep the title's declared "
        "line box clear of the primary's identity row, so all state the same 48 legible frames.",
        (
            "text.size_px.identity",
            "text.size_px.lower",
            "text.line_height_bp.identity",
            "text.line_height_bp.lower",
            "text.weight.700",
            "text.style.normal",
            "text.align.center",
            "text.fill_rgba.identity",
            "text.fill_rgba.interior",
            "text.background_rgba.absent",
            "text.background_rgba.present",
        ),
    ),
    "text rows whose larger declared line box reaches the identity row": (
        "The same font-metrics wall as the group above, but the interior and upper size and line "
        "height values widen the title's declared occlusion band until it covers the primary's "
        "identity row for the title's twenty frames, so these four state 28 legible frames rather "
        "than 48. They differ from the base-size rows by that band and from each other not at all.",
        (
            "text.size_px.interior",
            "text.size_px.upper",
            "text.line_height_bp.interior",
            "text.line_height_bp.upper",
        ),
    ),
    "the geometry identities the accepted fixture already carries": (
        "An identity transform and an absent crop leave the layer exactly where the fixture puts "
        "it, which is what those rows are for: the picture is the accepted one and the row asserts "
        "that the declared identity value really is an identity.",
        (
            "transform.anchor_x_bp.identity",
            "transform.anchor_y_bp.identity",
            "transform.position_x_bp.identity",
            "transform.position_y_bp.identity",
            "transform.scale_x_bp.identity",
            "transform.scale_y_bp.identity",
            "transform.rotation_mdeg.identity",
            "crop.left_bp.absent",
            "crop.top_bp.absent",
            "crop.right_bp.absent",
            "crop.bottom_bp.absent",
        ),
    ),
    "profile values the accepted output already carries, and one browser journey": (
        "`FAMILY_LANDMARKS` gives `output` and `import_integration` no landmark of their own, so "
        "these rows claim only the universal landmarks. Each output member restates a value the "
        "accepted output profile already declares -- that is what the row is for -- and the two "
        "import rows are one browser journey whose members differ by input modality, pointer "
        "against keyboard, which no composition can hold. `timing.cfr_24` is the base composition "
        "itself (B-58) and its family's landmark is the source mapping every rendering row "
        "already states, so it claims exactly what these do.",
        (
            "output.color_policy_bt709_sdr_limited",
            "output.frame_grid_exact_24",
            "output.container_mp4",
            "output.video_codec_h264",
            "output.pixel_format_yuv420p",
            "output.pixel_aspect_identity",
            "import_integration.generated_source.explicit_import_then_insert.pointer",
            "import_integration.generated_source.explicit_import_then_insert.keyboard",
            "timing.cfr_24",
        ),
    ),
    "the effect identities, and the kind that has to be switched on for them": (
        "The three field identities need `color_adjust_v1` enabled before they may hold a value at "
        "all, so all four state the same picture: the accepted one passed through the renderer's "
        "colour-adjust path at identity values. That path converts the layer to 8-bit BT.709 "
        "planes and back, which is not the identity on a saturated patch (`effect.kind.none` "
        "states the untouched patches and stands alone), while the `eq` filter itself copies a "
        "plane whose parameters are identity, so the four differ from each other not at all.",
        (
            "effect.kind.color_adjust_v1",
            "effect.brightness_permille.identity",
            "effect.contrast_permille.identity",
            "effect.saturation_permille.identity",
        ),
    ),
    "five frames of one cross dissolve": (
        "The overlay clip already carries the four-frame dissolve, and these rows name its first "
        "included frame, an interior frame, its last included frame and its exclusive end. They "
        "are five questions about one ramp, and `derive_alphas` states the whole ramp for each.",
        (
            "transition.cross_dissolve_legal",
            "transition.first_included_frame",
            "transition.interior_frame",
            "transition.last_included_frame",
            "transition.exclusive_end",
        ),
    ),
    "audio that must not change when the composition does": (
        "These reach genuinely different compositions -- the audible base and an overlay asset "
        "that carries bound audio -- and both must leave the output audio identical to the "
        "audible base. An identical expectation against a changed composition IS the evidence "
        "here, so never satisfy this group by inventing an audio difference these rows must not "
        "have. The two other rows of the family make the same audio claim and stand alone for "
        "what their visual edit does to the identity row: `visual_only_mapping_preserved` commits "
        "a 60% white wash that hides it everywhere, and `overlay_same_source_no_doubling` "
        "re-points the overlay at the primary's own asset, whose blue patch then covers the last "
        "identity cell at every even frame the overlay spans.",
        (
            "embedded_audio.primary_audible",
            "embedded_audio.overlay_excluded",
        ),
    ),
    "history positions that are invisible in the picture by construction": (
        "`FAMILY_LANDMARKS['history_currentness']` now asks for `patches`, which samples the "
        "composited picture, and these four rows reach history positions that a picture cannot "
        "show: a selection and a track lock change no pixel by definition -- asserting that they "
        "do not is the whole of `selection_invariant` and `lock_invariant` -- and an unknown "
        "render-job outcome is a fact about a job rather than about a composition. Their "
        "compositions do differ, which the join separates by public fingerprint. The other seven "
        "rows in the family reach positions the patches can see and are distinct.",
        (
            "history_currentness.selection_invariant",
            "history_currentness.lock_invariant",
            "history_currentness.unknown_outcome_pending",
            "history_currentness.unknown_outcome_failed",
        ),
    ),
    "positions clamped off canvas in opposite directions": (
        "At the declared position bounds the layer leaves the canvas entirely, and a layer that is "
        "off the left edge and one off the right edge produce the same picture. This is a fact "
        "about the corpus's declared bounds, not a defect in the rows.",
        (
            "transform.position_x_bp.lower",
            "transform.position_x_bp.upper",
            "transform.position_y_bp.lower",
            "transform.position_y_bp.upper",
        ),
    ),
    "a trim and a ripple trim with one clip in scope": (
        "The accepted `ripple_trim` payload scopes the primary track, which holds one clip, so the "
        "ripple has nothing to shift and both commands produce the same pair of pictures.",
        ("command.trim_clip.accepted", "command.ripple_trim.accepted"),
    ),
    **{
        f"the {edge} crop admitted, then its source edge measured": (
            "The two rows carry the same crop deliberately: one asks whether the decoder admits "
            "it, the other whether the surviving source edge moved by exactly that amount. Both "
            "claim the same geometry and patches, and only the observation differs.",
            (f"crop.{edge}_bp.legal_nonzero", f"crop.{edge}_bp.source_edge_observed"),
        )
        for edge in ("left", "top", "right", "bottom")
    },
    "the longest admitted dissolve, as a bound and as a ramp": (
        "Eight frames is both the upper bound and the longest ramp available to measure.",
        ("transition.alpha_progression", "transition.duration_upper"),
    ),
}


def test_no_two_rendering_rows_execute_the_same_composition() -> None:
    """Rows that render the identical composition are one experiment counted twice.

    Thirty-one rows carried no edit, no setup and no command at all and so every one of them ran
    the untouched accepted fixture while being enumerated, executed and reported as a distinct
    case. That is the post-closeout finding; this is the regression for it.
    """

    _assert_pinned(_grouped(_composition_key), SHARED_COMPOSITIONS, "composition")


def test_no_two_rendering_rows_state_the_same_executed_expectation() -> None:
    """A different composition that claims the same thing is still one experiment run twice."""

    _assert_pinned(_grouped(_claim_key), SHARED_CLAIMS, "expectation")


#: The two members of the audio group whose composition is legitimately the accepted base.
#:
#: `primary_audible` is the base by definition. `overlay_excluded` became the base when the corpus
#: base started declaring what a claimed source really carries: an overlay source has bound audio
#: like every other video source, and the exclusion is decided per clip from track kind and
#: enablement, so there is nothing left for the row to set up.
AUDIO_GROUP_BASE_ROWS: Final = ("embedded_audio.primary_audible", "embedded_audio.overlay_excluded")


def test_a_pinned_group_that_shares_an_expectation_still_renders_its_own_composition() -> None:
    """The audio group's evidence is an unchanged claim against a *changed* picture.

    That is only evidence while the compositions really differ, so the members that do change one
    have to change it to something no other member reaches. The two members named above change
    nothing, and are held to being exactly the base rather than merely being excused.
    """

    ids = SHARED_CLAIMS["audio that must not change when the composition does"][1]
    base = _composition_key(BOOK.by_id["embedded_audio.primary_audible"])
    changed = {
        case_id: _composition_key(BOOK.by_id[case_id])
        for case_id in ids
        if case_id not in AUDIO_GROUP_BASE_ROWS
    }
    assert len(set(changed.values())) == len(changed), changed
    assert base not in set(changed.values()), changed
    for case_id in AUDIO_GROUP_BASE_ROWS:
        assert case_id in ids, case_id
        assert _composition_key(BOOK.by_id[case_id]) == base, case_id


# ---------------------------------------------------------------------------------------------
# The conditions the repaired rows introduce, each asserted where the row claims it
# ---------------------------------------------------------------------------------------------


def _wire(case_id: str) -> dict[str, Any]:
    recipe = BOOK.by_id[case_id]
    setup = run_setup(recipe, _base(recipe.base))
    assert setup.state is not None, setup.error
    return dict(setup.state.snapshot.to_wire())


def _clips(wire: dict[str, Any], track_id: str) -> list[dict[str, Any]]:
    return sorted(
        (clip for clip in wire["clips"] if clip["track_id"] == track_id and clip["enabled"]),
        key=lambda clip: cast(int, clip["start_frame"]),
    )


def _asset_row(wire: dict[str, Any], asset_id: str) -> dict[str, Any]:
    return next(asset for asset in wire["assets"] if asset["asset_id"] == asset_id)


def test_the_soundless_source_this_row_names_is_unreachable_from_the_corpus_media() -> None:
    """The row cannot state its own subject, and the reason is a decoder fact, not a preference.

    A primary clip whose source carries no embedded audio needs a source that declares none. Every
    video source the render stage claims declares `present_bound`, because that is what the real
    media carries, and editing that declaration would describe media that does not exist. The only
    other soundless asset in the base is the image, and the composition contract refuses an image
    on a primary_video track. This test pins the refusal so the limitation cannot be quietly
    forgotten once a soundless source exists: when one is added, this test is what should fail.
    """

    wire = _base()
    for asset in wire["assets"]:
        if asset["kind"] == "video":
            assert asset["embedded_audio"] == "present_bound", asset["asset_id"]
    next(clip for clip in wire["clips"] if clip["clip_id"] == PRIMARY_CLIP)["asset_id"] = (
        IMAGE_ASSET
    )
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    with pytest.raises(CompositionContractError) as refusal:
        decode_public_snapshot(wire)
    assert refusal.value.code == INVALID_CONTRACT


def test_the_primary_without_audio_row_draws_its_two_owners_from_two_different_sources() -> None:
    """What the row observes instead, stated so it is not read as the case its name promises.

    Two primary owners on two different sources, so each owner's audio is placed through its own
    source's mapping rather than through one global one. The second owner is the shorter overlay
    source, whose own first burst lands mid-timeline -- a position no single-source composition can
    produce, and the nearest reachable property of the same declared policy.
    """

    wire = _wire("embedded_audio.primary_without_audio")
    clips = _clips(wire, PRIMARY_TRACK)
    assert len(clips) == 2, clips
    assert clips[0]["asset_id"] != clips[1]["asset_id"], "the two owners need two sources"
    second = _asset_row(wire, cast(str, clips[1]["asset_id"]))
    assert cast(int, clips[1]["duration_frames"]) <= cast(int, second["source_frame_count"]), (
        "the shorter source has to be able to fill the clip"
    )
    onsets = derive_audio_onsets(wire)
    assert len(onsets) == 2, onsets
    # The second owner restarts at its own source's first burst rather than continuing the first
    # owner's numbering, which is the whole of what two sources make observable.
    assert [onset.label for onset in onsets] == ["owner0_burst_0", "owner1_burst_0"], onsets
    assert onsets[1].sample_index == cast(int, clips[1]["start_frame"]) * 2_000


def test_the_primary_disabled_row_disables_a_primary_clip_and_silences_only_that_clip() -> None:
    """Disabling the *only* primary clip is refused, so the row disables one clip of several.

    `composition_contract` will not admit the overlay's cross dissolve with no lower layer beneath
    it, and a composition with no primary clip at all asserts an absence a failed collector
    produces just as well. The row splits the primary clip and disables the tail, so the disabled
    clip's own contribution is isolated against a neighbour that must still contribute.
    """

    wire = _wire("embedded_audio.primary_disabled")
    every = [clip for clip in wire["clips"] if clip["track_id"] == PRIMARY_TRACK]
    disabled = [clip for clip in every if not clip["enabled"]]
    assert len(disabled) == 1, every
    assert len(every) - len(disabled) >= 1, "a disabled clip needs a contributing neighbour"
    assert carries_audio(wire) is True, "the enabled neighbour still has to be heard"
    # Nothing the disabled clip covers is claimed, as picture or as sound.
    covered = range(
        cast(int, disabled[0]["start_frame"]),
        cast(int, disabled[0]["start_frame"]) + cast(int, disabled[0]["duration_frames"]),
    )
    assert all(item.output_frame not in covered for item in derive_source_mapping(wire))
    assert derive_source_mapping(wire), "the enabled neighbours still have to be seen"
    silent = range(covered.start * 2_000, covered.stop * 2_000)
    assert all(onset.sample_index not in silent for onset in derive_audio_onsets(wire))


def test_the_overlay_excluded_row_gives_the_overlay_audio_the_output_must_not_take() -> None:
    wire = _wire("embedded_audio.overlay_excluded")
    overlay = _asset_row(wire, OVERLAY_ASSET)
    assert overlay["embedded_audio"] == "present_bound"
    assert overlay["source_sample_count"] is not None
    # The policy is `primary_embedded_follow_video_v1`: the overlay's audio changes nothing.
    assert derive_audio_onsets(wire) == derive_audio_onsets(_wire("embedded_audio.primary_audible"))


def test_the_same_source_row_points_the_overlay_at_the_primary_asset() -> None:
    wire = _wire("embedded_audio.overlay_same_source_no_doubling")
    overlay = next(clip for clip in wire["clips"] if clip["clip_id"] == OVERLAY_CLIP)
    assert overlay["asset_id"] == PRIMARY_ASSET
    audible = _wire("embedded_audio.primary_audible")
    assert derive_audio_onsets(wire) == derive_audio_onsets(audible), "no doubled onset"


def test_the_hard_cut_row_abuts_two_primary_clips_and_the_gap_row_separates_them() -> None:
    """And the gap has to swallow a declared burst, or it observes nothing the cut does not.

    A hole over a stretch the source is silent through anyway leaves "silent because nothing
    covers it" and "silent because the covering clip's source is silent" as the same measurement.
    Dropping the second burst's output position inside the hole is what makes the two rows
    different experiments rather than one experiment with a different clip table.
    """

    cut_wire = _wire("embedded_audio.hard_cut")
    cut = _clips(cut_wire, PRIMARY_TRACK)
    assert len(cut) == 2
    assert cut[0]["start_frame"] + cut[0]["duration_frames"] == cut[1]["start_frame"]

    gap_wire = _wire("embedded_audio.silence_gap")
    gap = _clips(gap_wire, PRIMARY_TRACK)
    assert len(gap) == 2
    hole = range(
        cast(int, gap[0]["start_frame"]) + cast(int, gap[0]["duration_frames"]),
        cast(int, gap[1]["start_frame"]),
    )
    assert hole, "the gap row needs an uncovered range"
    # The overlay's cross dissolve needs a lower layer for its whole duration, so the hole may
    # never open over those frames however the clips are arranged.
    assert all(frame not in hole for frame in range(12, 16)), hole
    swallowed = derive_audio_onsets(cut_wire)
    assert len(derive_audio_onsets(gap_wire)) < len(swallowed), "the hole must lose a burst"


def test_the_owner_range_rows_move_the_audio_owner_rather_than_a_neighbour() -> None:
    """Trim, slip, ripple and roll each have to change the clip the audio policy names as owner."""

    audible = _clips(_wire("embedded_audio.primary_audible"), PRIMARY_TRACK)[0]
    for case_id in (
        "embedded_audio.trim_owner_range",
        "embedded_audio.slip_owner_range",
        "embedded_audio.ripple_owner_range",
        "embedded_audio.roll_owner_range",
        "embedded_audio.undo_owner_range",
        "embedded_audio.redo_owner_range",
    ):
        owner = _clips(_wire(case_id), PRIMARY_TRACK)[0]
        assert owner != audible, case_id


def test_the_roll_row_moves_a_declared_burst_to_the_other_side_of_the_cut() -> None:
    """A roll is only audible when its boundary crosses a burst.

    The shared edit point may only sit on a declared source landmark, and 12 is the only one ahead
    of the second burst's output position, so the roll has to move the boundary back to it. Rolling
    the other way leaves both bursts with the first owner and states what an untouched primary
    states.
    """

    audible = derive_audio_onsets(_wire("embedded_audio.primary_audible"))
    rolled = derive_audio_onsets(_wire("embedded_audio.roll_owner_range"))
    assert len(rolled) == len(audible), "the roll loses no burst, it re-owns one"
    assert [onset.sample_index for onset in rolled] == [onset.sample_index for onset in audible], (
        "a roll moves the cut, not the bursts"
    )
    assert [onset.label for onset in rolled] != [onset.label for onset in audible], (
        "the second burst has to change owner, or the roll is inaudible"
    )


def test_the_undo_and_redo_rows_leave_history_somewhere_other_than_the_base() -> None:
    """An undo of the only transaction is the accepted fixture again, which proves nothing."""

    base = _composition_key(BOOK.by_id["embedded_audio.primary_audible"])
    for case_id in ("embedded_audio.undo_owner_range", "embedded_audio.redo_owner_range"):
        assert _composition_key(BOOK.by_id[case_id]) != base, case_id
    undone = _clips(_wire("embedded_audio.undo_owner_range"), PRIMARY_TRACK)[0]
    redone = _clips(_wire("embedded_audio.redo_owner_range"), PRIMARY_TRACK)[0]
    assert redone["duration_frames"] < undone["duration_frames"], "redo must reapply the trim"


def test_every_start_edge_trim_lands_on_a_declared_source_landmark() -> None:
    """The decoder refuses a source shift it cannot represent, and four rows were refused by it.

    The landmark frames are read from the composition each row actually reached rather than
    restated here: the base now declares one landmark per source frame, so a hardcoded triple would
    pass for the wrong reason, and a row that replaces the table with a sparse one has to be
    checked against the table it declared.
    """

    for recipe in BOOK.rendering():
        if recipe.family != "edge_trim":
            continue
        wire = _wire(recipe.case_id)
        for clip in _clips(wire, PRIMARY_TRACK):
            asset = _asset_row(wire, cast(str, clip["asset_id"]))
            declared = {entry["frame_index"] for entry in asset["landmarks"]}
            assert clip["source_start_frame"] in declared, recipe.case_id


def test_the_font_rows_name_the_packaged_font_and_differ_by_their_own_content() -> None:
    """All three must stay renderable, so the leaf is carried by the content, not by the asset.

    A row that renamed the asset to something the packaging does not provide would be refused by
    the render planner as `font_asset_unsupported` -- a true statement, but not the one the corpus
    declares for these rows, which expect no refusal.
    """

    contents = {}
    for leaf in ("qualified", "fallback", "unsupported_glyph"):
        wire = _wire(f"text.font.{leaf}")
        title = next(clip for clip in wire["clips"] if clip["clip_id"] == TITLE_CLIP)
        assert title["text"]["font_asset_id"] == FONT_ASSET, leaf
        contents[leaf] = title["text"]["content"]
    assert len(set(contents.values())) == 3, contents
    # The unsupported leaf has to actually carry a codepoint the packaged coverage excludes.
    assert "\u4e2d" in contents["unsupported_glyph"]


def _clip_of(wire: dict[str, Any], clip_id: str) -> dict[str, Any]:
    return next(clip for clip in wire["clips"] if clip["clip_id"] == clip_id)


def test_the_history_rows_reach_the_position_their_name_states() -> None:
    """Eight of these rows ran from history position zero, so none of them had a position at all.

    A row about an inverse that never applied one, or about a replay that never replayed, is the
    base composition wearing a name.
    """

    assert any(track["locked"] for track in _wire("history_currentness.lock_invariant")["tracks"])

    # The inverse leaves the earlier committed effect and undoes only the edit after it, so the
    # row states "the timeline returned to where it was" about somewhere other than the fixture.
    inverse = _wire("history_currentness.inverse")
    assert _clip_of(inverse, PRIMARY_CLIP)["effect"]["kind"] == "color_adjust_v1"
    assert _clip_of(inverse, IMAGE_CLIP)["opacity_bp"] != 7_000, "the undone edit is still applied"

    # A replay is an undo followed by a redo, so the redone value has to be back.
    assert _clip_of(_wire("history_currentness.replay"), IMAGE_CLIP)["opacity_bp"] == 7_500
    # An explicit rebase carries its own commands, and they have to have been applied.
    assert _clip_of(_wire("history_currentness.explicit_rebase"), IMAGE_CLIP)["opacity_bp"] == 8_000


def test_no_command_layer_refusal_omits_the_command_it_claims_is_refused() -> None:
    """A refusal that names nothing to refuse cannot fail for its stated reason.

    `nle_semantic_qualify._evaluate_recipe` routes every `refusal_layer == "command"` row to
    `_command_refusal_row`, which needs `recipe.command`; with none it blocks. Two edge-trim rows
    shipped exactly that shape, so for the whole life of the corpus they asserted "the command
    layer refuses this" without naming the thing to be refused.
    """

    silent = [
        recipe.case_id
        for recipe in BOOK.recipes
        if recipe.refusal_layer == "command" and recipe.command is None
    ]
    assert not silent, silent


def _refused(case_id: str) -> str:
    """Drive a refusing row's setup and subject command and return the code the decoder raised."""

    recipe = BOOK.by_id[case_id]
    assert recipe.command is not None, case_id
    setup = run_setup(recipe, _base())
    assert setup.state is not None, setup.error
    payload = _resolve(
        dict(recipe.command.payload), setup.cursor, setup.state.snapshot.timeline_fingerprint
    )
    try:
        _transact(setup.state, recipe.command.kind, payload, "subject")
    except ContractValidationError as exc:
        return str(getattr(exc, "code", ""))
    raise AssertionError(f"{case_id} was accepted")


def test_the_invalid_transition_row_is_refused_for_the_transition_the_trim_broke() -> None:
    """The edge-trim family's own invalid transition, not the transition family's.

    `transition.duration_overflow` declares an over-long dissolve on a clip that never moved. Here
    a legal four-frame dissolve becomes invalid because an end trim leaves the clip shorter than
    it. The boundary is what proves the refusal is the dissolve rule rather than a bound on the
    delta or on the duration: one frame less is refused, one frame more is admitted.
    """

    recipe = BOOK.by_id["edge_trim.invalid_transition_refused"]
    assert recipe.command is not None
    assert recipe.command.kind == "trim_clip"
    assert recipe.command.payload["edge"] == "end", (
        "a start trim sets the clip's transition to none on the way through, so it can never "
        "leave one invalid"
    )
    overlay = next(clip for clip in _base()["clips"] if clip["clip_id"] == OVERLAY_CLIP)
    dissolve = cast(int, overlay["transition"]["duration_frames"])
    span = cast(int, overlay["duration_frames"])
    delta = cast(int, recipe.command.payload["delta_frames"])
    assert span + delta < dissolve <= span, (span, delta, dissolve)
    assert _refused("edge_trim.invalid_transition_refused") == INVALID_CONTRACT

    # One frame less of trimming leaves exactly the dissolve's own length and is admitted, so the
    # row is refused for the transition and not for a shorter clip in general.
    state = run_setup(recipe, _base()).state
    assert state is not None
    admitted, _receipt = _transact(
        state,
        "trim_clip",
        {"clip_id": OVERLAY_CLIP, "edge": "end", "delta_frames": delta + 1},
        "boundary",
    )
    kept = next(
        clip for clip in admitted.snapshot.to_wire()["clips"] if clip["clip_id"] == OVERLAY_CLIP
    )
    assert kept["duration_frames"] == dissolve


def test_the_refused_draft_row_submits_the_trim_a_draft_submits_and_is_refused() -> None:
    """A draft is live UI state, but the gesture submits it as an ordinary `trim_clip`.

    `trimCommandForDraft` in the timeline component turns a settled drag into exactly that
    transaction, so the draft the contract refuses is a trim whose result it will not admit. The
    canonical one is the drag that consumes the clip; a clip must keep at least one frame.
    """

    recipe = BOOK.by_id["edge_trim.draft_refused"]
    assert recipe.command is not None
    assert recipe.command.kind == "trim_clip"
    clip = next(item for item in _base()["clips"] if item["clip_id"] == IMAGE_CLIP)
    delta = cast(int, recipe.command.payload["delta_frames"])
    assert cast(int, clip["duration_frames"]) + delta == 0, "the drag has to consume the clip"
    assert _refused("edge_trim.draft_refused") == INVALID_CONTRACT

    # One frame short of consuming it is admitted, so the refusal is the collapse itself.
    state = run_setup(recipe, _base()).state
    assert state is not None
    admitted, _receipt = _transact(
        state,
        "trim_clip",
        {"clip_id": IMAGE_CLIP, "edge": "end", "delta_frames": delta + 1},
        "boundary",
    )
    kept = next(
        item for item in admitted.snapshot.to_wire()["clips"] if item["clip_id"] == IMAGE_CLIP
    )
    assert kept["duration_frames"] == 1


def test_the_unsupported_glyph_row_declares_the_refusal_the_resolver_actually_raises() -> None:
    """A row that says the product draws a glyph it has no face for is a false claim.

    The row's content is `A中`, and nothing in the packaged manifest covers U+4E2D. The composition
    decoder never looks at coverage, so declaring this a rendering row -- or an `invalid_contract`
    refusal -- would have it pass on an authority that cannot see its subject. The refusal comes
    from the packaged font resolver, where the render plan's font facts are bound, which is why the
    corpus carries a `font_binding` layer at all.
    """

    recipe = BOOK.by_id["text.font.unsupported_glyph"]
    assert recipe.refusal_code == FONT_GLYPH_UNSUPPORTED
    assert recipe.refusal_layer == "font_binding"
    assert recipe.renders is False

    wire = _wire("text.font.unsupported_glyph")
    title = next(clip for clip in wire["clips"] if clip.get("text"))
    text = title["text"]
    with pytest.raises(AuthoringFontError) as raised:
        require_text_coverage(
            load_packaged_font_manifest(),
            text["font_asset_id"],
            text["weight"],
            text["style"],
            text["content"],
        )
    assert raised.value.code == recipe.refusal_code

    # And the two rows that share the family still resolve, or the leaf above would be measuring
    # nothing but a broken font package.
    for leaf in ("qualified", "fallback"):
        sibling = next(clip for clip in _wire(f"text.font.{leaf}")["clips"] if clip.get("text"))[
            "text"
        ]
        require_text_coverage(
            load_packaged_font_manifest(),
            sibling["font_asset_id"],
            sibling["weight"],
            sibling["style"],
            sibling["content"],
        )


def test_every_declared_refusal_layer_has_a_runner_route_that_executes_it() -> None:
    """A refusal nobody drives reports its own declaration back, which is B-25's defect class.

    `_evaluate_recipe` is the whole dispatch, so a layer it does not name is a layer whose rows fall
    through to a route built for something else. This asserts the routing by *running* one row of
    every declared layer and requiring a decided status -- never by reading the dispatch's source.
    """

    from scripts.nle_semantic_qualify import _evaluate_recipe

    for layer in REFUSAL_LAYERS:
        rows = [recipe for recipe in BOOK.recipes if recipe.refusal_layer == layer]
        assert rows, layer
        # The two edge-trim drafts genuinely have no backend-executable payload and block for that
        # stated reason; every other row of every layer has to reach a verdict.
        decidable = [recipe for recipe in rows if recipe.command is not None or layer != "command"]
        outcome = _evaluate_recipe(decidable[0])
        assert outcome.status in ("PASS", "MISMATCH"), (layer, outcome.status, outcome.reason)
        assert outcome.declared == decidable[0].refusal_code
