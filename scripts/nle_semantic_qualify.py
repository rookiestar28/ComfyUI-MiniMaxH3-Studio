"""M25-20 corrective: the bounded backend qualification runner (post-closeout finding F1).

The 306-row corpus in ``semantic_conformance`` and its per-case recipes in
``semantic_conformance_cases`` were enumerated and counted, but the post-closeout review found they
had never actually been executed against the product. This script is the backend half of closing
that gap: for every row whose disposition a pure, in-process call can decide, it drives the real
decoder -- ``decode_public_snapshot``, ``apply_timeline_transaction`` or
``revalidate_render_plan_currentness`` -- and records what actually happened. It owns none of the
render or browser evidence; a row that needs a rendered artifact or a presented canvas is named as
still awaiting one rather than answered with a cheaper substitute, which is exactly the substitution
the review rejected.

Three rules are enforced here rather than trusted:

- **An expectation is never written from an observation.** Every declared refusal code or effect
  comes from the recipe, which was declared before this script ever ran. When the product disagrees,
  the row becomes ``MISMATCH`` and stays that way; "fixing" a red row by copying the observed value
  back into the declaration is the exact inversion this corpus exists to catch.
- **Every row gets a disposition, never a shorter corpus.** A row this runner cannot reach -- no
  declared command payload, a construction failure, a timeout -- is reported ``BLOCKED`` with a
  bounded reason. It is never silently dropped, and `admit_report` is run over the full 306-row
  output so a short or duplicated run cannot read as complete.
- **No private material leaves this process.** Declared and observed values are closed-vocabulary
  codes or the literal strings this script writes itself, never a raw exception message, a path or a
  URL; every free-text field is checked with the same privacy discipline
  ``semantic_conformance_compare`` applies to its own report rows.

Row disposition by observation kind (see the module docstrings in ``semantic_conformance_cases`` for
what each kind means):

- ``contract_refusal`` rows whose ``refusal_layer`` is ``"snapshot"``, and every
  ``render_and_browser`` row: the recipe's edits are applied to a fresh copy of the accepted
  fixture, the snapshot is re-signed and decoded, and the observed code (or acceptance) is compared
  to the declaration. An accepted ``render_and_browser`` row is reported ``BLOCKED`` -- it is
  genuinely not this runner's job to render a frame or drive a browser -- with the composition's own
  public fingerprint recorded as a measurement so the row is traceable to what would be rendered.
- ``command_refusal`` rows (``refusal_layer == "command"``, a command payload declared): the
  recipe's setup runs first, then the subject command, through the real timeline transaction
  decoder.
- ``command_effect`` rows: setup then the subject command, expected to succeed; before/after
  timeline and public fingerprints are recorded, and whether the timeline actually changed.
- The three ``history_currentness`` rows whose ``refusal_layer`` is ``"currentness"``: refused by
  ``render_planner.revalidate_render_plan_currentness`` when a bound plan's currentness claim no
  longer matches, never by the snapshot decoder. A minimal accepted plan is built once (mirroring
  the accepted ``tests/test_m25_render_planner.py`` fixture shape) and revalidated against a
  confirmation perturbed in exactly the field the row declares stale.
- The two ``edge_trim`` rows whose refusal is command-layer but which declare no command payload
  (the refusal is of a live UI "draft" state, never built as a discrete command here): ``BLOCKED``.
- ``shell_invariant`` and ``import_integration`` rows: ``NOT_RUN`` -- they are browser and UI
  evidence, out of a backend runner's reach by definition.
- ``declared_negative`` rows split. The ten seam rows are a claim about the accepted contract and
  the command decoder, both of which are here: the composition's ``audio_extension`` must declare
  the deferred profile, and a forged member of the reserved audio command namespace must change no
  state and no history. The three ``surface_*`` rows are a claim about what the shipped UI exposes,
  so they stay ``NOT_RUN`` for the browser collector. A declared negative that did not execute is
  ``NOT_RUN``, never a free ``DECLARED_UNSUPPORTED``.
"""

from __future__ import annotations

import argparse
import copy
import json
import queue
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comfyui_h3_context.adapters.authoring_fonts import (  # noqa: E402
    AuthoringFontError,
    load_packaged_font_manifest,
    require_text_coverage,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint  # noqa: E402
from comfyui_h3_context.core.composition_contract import (  # noqa: E402
    CompositionContractError,
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.errors import ContractValidationError  # noqa: E402
from comfyui_h3_context.core.render_planner import (  # noqa: E402
    FONT_FACTS_SCHEMA,
    PRIVATE_SOURCE_FACTS_MANIFEST_SCHEMA,
    SOURCE_CURRENTNESS_SCHEMA,
    FontTitleBindingFact,
    PackagedFontFacts,
    PrivateSourceFact,
    PrivateSourceFactsManifest,
    RenderPlanV1,
    SourceCurrentnessConfirmation,
    SourceCurrentnessToken,
    packaged_font_facts_fingerprint,
    plan_render,
    private_source_manifest_fingerprint,
    revalidate_render_plan_currentness,
    source_facts_fingerprint,
)
from comfyui_h3_context.core.semantic_conformance import (  # noqa: E402
    CORPUS_VERSION,
    REPORT_STATUSES,
    Corpus,
    SemanticConformanceError,
    admit_report,
    build_corpus,
)
from comfyui_h3_context.core.semantic_conformance_cases import (  # noqa: E402
    BASE_FIXTURE_NAMES,
    CORPUS_BASE,
    PRIMARY_CLIP,
    CaseRecipe,
    RecipeBook,
    apply_edits,
    build_recipes,
)
from comfyui_h3_context.core.semantic_conformance_compare import _reject_private  # noqa: E402
from comfyui_h3_context.core.semantic_conformance_drive import (  # noqa: E402
    resolve_payload,
    run_setup,
    transact,
)

FIXTURES_DIR: Final = ROOT / "tests" / "fixtures"
FIXTURE_PATH: Final = FIXTURES_DIR / BASE_FIXTURE_NAMES[CORPUS_BASE]
QUALIFY_REPORT_SCHEMA: Final = "h3.context.nle_semantic_qualify_report.v1"

DEFAULT_ROW_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_WALL_CEILING_SECONDS: Final = 180.0

#: `select_clips` writes only `TimelineHistoryState.selection`, and `set_track_locked` writes a
#: field the accepted fingerprint does not cover; both may legitimately leave the timeline
#: fingerprint unchanged. Every other `command_effect` row's fixture forces a measurable effect, so
#: an unchanged fingerprint there means the setup silently did nothing -- the exact failure mode a
#: code-only refusal check cannot see, and the reason this set is not "every selection/lock command"
#: but this recipe author's own closed pair.
_COMMAND_EFFECT_INVARIANT_OK: Final = frozenset(
    {
        "command.set_track_locked.accepted",
        "command.select_clips.accepted",
    }
)


def _load_base_wire(name: str) -> dict[str, Any]:
    path = FIXTURES_DIR / BASE_FIXTURE_NAMES[name]
    document = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    return cast(dict[str, Any], document["snapshot"])


_BASE_WIRES: Final[dict[str, dict[str, Any]]] = {
    name: _load_base_wire(name) for name in BASE_FIXTURE_NAMES
}
BASE_WIRE: Final[dict[str, Any]] = _BASE_WIRES[CORPUS_BASE]


def base_wire(recipe: CaseRecipe) -> dict[str, Any]:
    """The composition this row is a case of.

    GUARD: read it through the recipe, never through `BASE_WIRE`. A row on the high-resolution base
    judged against the 320 x 180 one would be compared with a composition nothing rendered, and the
    mismatch would look like a conformance failure of the renderer rather than of this lookup.
    """

    return _BASE_WIRES[recipe.base]


def _error_code(exc: BaseException) -> str:
    """Read a stable content-free code off any of the module's typed rejections.

    Every refusing authority here (`CompositionContractError`, `TimelineHistoryError`,
    `TimelineCommandError`, `RenderPlannerError`, ...) defines its own `code` attribute rather than
    sharing one on `ContractValidationError`, so it is read defensively and never assumed present.
    """

    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else "unknown"


@dataclass(frozen=True, slots=True)
class QualifyRow:
    """One executed row of the backend qualification run.

    `declared` and `observed` are closed-vocabulary codes or one of the literal strings this script
    writes itself (`"accepted"`, `"not_executed"`, ...) -- never a raw exception message, a path or
    a URL. The same privacy discipline `semantic_conformance_compare` applies to its own report
    rows is applied here, so a leak fails the row's own construction, never the report file.
    """

    case_id: str
    case_class: str
    observation: str
    declared: str
    observed: str
    status: str
    reason: str | None = None
    measurements: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.status not in REPORT_STATUSES:
            raise SemanticConformanceError(f"{self.status} is not a declared report status")
        _reject_private(self.declared, "declared expectation")
        _reject_private(self.observed, "observed value")
        if self.reason is not None:
            _reject_private(self.reason, "row reason")
        for subject, value in self.measurements:
            _reject_private(subject, "measurement subject")
            _reject_private(value, "measurement value")

    def as_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "case_class": self.case_class,
            "observation": self.observation,
            "declared": self.declared,
            "observed": self.observed,
            "status": self.status,
            "reason": self.reason,
            "measurements": [{"subject": s, "value": v} for s, v in self.measurements],
        }


def _blocked_row(recipe: CaseRecipe, *, reason: str) -> QualifyRow:
    return QualifyRow(
        case_id=recipe.case_id,
        case_class=recipe.case_class,
        observation=recipe.observation,
        declared=recipe.refusal_code or "accepted",
        observed="not_executed",
        status="BLOCKED",
        reason=reason,
    )


def _browser_scope_row(recipe: CaseRecipe) -> QualifyRow:
    return QualifyRow(
        case_id=recipe.case_id,
        case_class=recipe.case_class,
        observation=recipe.observation,
        declared="browser_evidence",
        observed="not_executed",
        status="NOT_RUN",
        reason="this row is browser or UI evidence collected outside the backend qualification "
        "runner",
    )


# ---------------------------------------------------------------------------------------------
# Snapshot-layer rows: `contract_refusal` (refusal_layer == "snapshot") and `render_and_browser`.
# ---------------------------------------------------------------------------------------------


#: The reserved namespace the accepted contract names and deliberately leaves empty. A forged
#: member of it is the only way to prove the deferral is real rather than merely written down.
AUDIO_COMMAND_NAMESPACE: Final = "h3.authoring.audio.command.v1"

#: What the deferred seam must declare, field by field. Written from
#: `tests/fixtures/m25_10_composition_contract_v1.json`'s accepted `audio_extension` section, not
#: read back from it at runtime: a check that reads its own expectation proves nothing.
DEFERRED_AUDIO_SEAM: Final = {
    "track_profile": "none_v1",
    "command_namespace": AUDIO_COMMAND_NAMESPACE,
    "preview_edit_capability": "unsupported",
    "final_render_edit_capability": "unsupported",
    "embedded_renderer_variant": "EmbeddedAudioSpanV1",
    "independent_audio_renderer_variant": "none_v1",
}

#: The three deferred rows whose claim is about the shipped surface rather than the seam.
_SURFACE_NEGATIVE_PREFIX: Final = "deferred_negative.surface_"


def _deferred_negative_row(recipe: CaseRecipe) -> QualifyRow:
    """Execute the deferred audio negative instead of asserting it.

    Two things have to be true at once, and neither implies the other. The contract must declare the
    deferred profile with an empty reserved command membership, and the command decoder must refuse
    a forged member of that namespace without moving the timeline or the history. A seam that
    declared the deferral while quietly accepting the command would satisfy the first and fail the
    product; one that refused the command while declaring an editable profile would satisfy the
    second and mislead every reader of the contract.
    """

    if recipe.case_id.startswith(_SURFACE_NEGATIVE_PREFIX):
        return _browser_scope_row(recipe)

    section = BASE_WIRE.get("audio_extension")
    if not isinstance(section, Mapping):
        return _blocked_row(recipe, reason="the composition declares no audio extension seam")
    disagreements = [
        field for field, value in DEFERRED_AUDIO_SEAM.items() if section.get(field) != value
    ]
    members = section.get("command_members")
    if not isinstance(members, list) or members:
        disagreements.append("command_members")

    setup = run_setup(recipe, base_wire(recipe))
    if setup.error is not None or setup.state is None:
        return _blocked_row(recipe, reason=setup.error or "setup produced no usable state")
    before_timeline = setup.state.snapshot.timeline_fingerprint
    before_public = setup.state.snapshot.public_fingerprint
    try:
        result, _receipt = transact(
            setup.state, AUDIO_COMMAND_NAMESPACE, {"clip_id": PRIMARY_CLIP}, "forged-audio"
        )
    except ContractValidationError as exc:
        forged = _error_code(exc)
        after_timeline, after_public = before_timeline, before_public
    else:
        forged = "accepted"
        after_timeline = result.snapshot.timeline_fingerprint
        after_public = result.snapshot.public_fingerprint

    measurements = (
        ("forged_audio_command", forged),
        ("timeline_fingerprint_changed", str(after_timeline != before_timeline)),
        ("public_fingerprint_changed", str(after_public != before_public)),
        ("reserved_command_members", str(len(members) if isinstance(members, list) else -1)),
    )
    changed = after_timeline != before_timeline or after_public != before_public
    if disagreements or forged == "accepted" or changed:
        return QualifyRow(
            case_id=recipe.case_id,
            case_class=recipe.case_class,
            observation=recipe.observation,
            declared="deferred",
            observed="not_deferred",
            status="MISMATCH",
            reason="the deferred audio seam did not hold",
            measurements=measurements,
        )
    return QualifyRow(
        case_id=recipe.case_id,
        case_class=recipe.case_class,
        observation=recipe.observation,
        declared="deferred",
        observed="deferred",
        status="DECLARED_UNSUPPORTED",
        reason=recipe.unsupported_reason,
        measurements=measurements,
    )


def _snapshot_layer_row(recipe: CaseRecipe) -> QualifyRow:
    """Decide a snapshot-layer row, and record the composition it actually reaches.

    CRITICAL: a recipe states its case two ways and both are load-bearing. A refusal row is about
    the edited snapshot itself, so it is decoded exactly as edited. An accepted row may reach its
    subject by running real timeline commands first, and the fingerprint this row records is the
    evidence of *which* composition the later render stage was supposed to render. Recording the
    edited base for a row whose whole case is in its setup makes every such row report the same
    composition -- which is how a corpus of distinct cases quietly becomes one case repeated.
    """

    wire = copy.deepcopy(base_wire(recipe))
    try:
        apply_edits(wire, recipe)
    except SemanticConformanceError:
        return _blocked_row(recipe, reason="applying the recipe's declared edits raised an error")
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    try:
        decode_public_snapshot(wire)
    except CompositionContractError as exc:
        observed_code: str | None = _error_code(exc)
    else:
        observed_code = None

    if recipe.refusal_code is not None:
        declared = recipe.refusal_code
        observed = observed_code or "accepted"
        status = "PASS" if observed == declared else "MISMATCH"
        return QualifyRow(
            recipe.case_id, recipe.case_class, recipe.observation, declared, observed, status
        )

    declared = "accepted"
    if observed_code is not None:
        return QualifyRow(
            recipe.case_id,
            recipe.case_class,
            recipe.observation,
            declared,
            observed_code,
            "MISMATCH",
        )
    fingerprint = cast(str, wire["public_fingerprint"])
    if recipe.setup:
        setup = run_setup(recipe, base_wire(recipe))
        if setup.state is None:
            return _blocked_row(recipe, reason=setup.error or "the row reached no composition")
        fingerprint = setup.state.snapshot.public_fingerprint
    return QualifyRow(
        recipe.case_id,
        recipe.case_class,
        recipe.observation,
        declared,
        "accepted",
        "BLOCKED",
        reason="awaiting the render and browser observation stage",
        measurements=(("public_fingerprint", fingerprint),),
    )


# ---------------------------------------------------------------------------------------------
# Command-layer rows: `command_refusal` and `command_effect`, driven through the real timeline
# transaction decoder, in the same shape `tests/test_m25_20_conformance_cases.py` asserts.
# ---------------------------------------------------------------------------------------------


def _command_refusal_row(recipe: CaseRecipe) -> QualifyRow:
    command = recipe.command
    if command is None:
        # The caller only reaches here after checking `recipe.command is not None`; this is a
        # narrowing guard for the type checker, not a reachable runtime path.
        return _blocked_row(recipe, reason="this command-layer refusal has no command payload")
    declared = recipe.refusal_code or "accepted"
    setup = run_setup(recipe, base_wire(recipe))
    if setup.error is not None or setup.state is None:
        return _blocked_row(recipe, reason=setup.error or "setup produced no usable state")
    payload = resolve_payload(
        dict(command.payload), setup.cursor, setup.state.snapshot.timeline_fingerprint
    )
    try:
        transact(setup.state, command.kind, payload, "subject")
    except ContractValidationError as exc:
        observed = _error_code(exc)
    else:
        observed = "accepted"
    status = "PASS" if observed == declared else "MISMATCH"
    return QualifyRow(
        recipe.case_id, recipe.case_class, recipe.observation, declared, observed, status
    )


def _command_effect_row(recipe: CaseRecipe) -> QualifyRow:
    command = recipe.command
    if command is None:
        # Only `command_effect` recipes reach this function, and every one declares a command; this
        # is a narrowing guard for the type checker, not a reachable runtime path.
        return _blocked_row(recipe, reason="this command-effect row has no command payload")
    expects_change = recipe.case_id not in _COMMAND_EFFECT_INVARIANT_OK
    declared = "accepted:changed" if expects_change else "accepted:invariant_ok"
    setup = run_setup(recipe, base_wire(recipe))
    if setup.error is not None or setup.state is None:
        return _blocked_row(recipe, reason=setup.error or "setup produced no usable state")
    before_timeline = setup.state.snapshot.timeline_fingerprint
    before_public = setup.state.snapshot.public_fingerprint
    payload = resolve_payload(dict(command.payload), setup.cursor, before_timeline)
    try:
        result, _receipt = transact(setup.state, command.kind, payload, "subject")
    except ContractValidationError as exc:
        return QualifyRow(
            recipe.case_id,
            recipe.case_class,
            recipe.observation,
            declared,
            _error_code(exc),
            "MISMATCH",
        )
    after_timeline = result.snapshot.timeline_fingerprint
    after_public = result.snapshot.public_fingerprint
    changed = after_timeline != before_timeline
    observed = "accepted:changed" if changed else "accepted:unchanged"
    status = "PASS" if (not expects_change or changed) else "MISMATCH"
    measurements = (
        ("timeline_fingerprint_changed", str(changed)),
        ("public_fingerprint_changed", str(after_public != before_public)),
    )
    return QualifyRow(
        recipe.case_id,
        recipe.case_class,
        recipe.observation,
        declared,
        observed,
        status,
        measurements=measurements,
    )


# ---------------------------------------------------------------------------------------------
# Currentness rows: refused by the render planner when a bound plan is revalidated, never by the
# snapshot decoder. The fixture shape mirrors the accepted `tests/test_m25_render_planner.py`
# helpers exactly, kept local so this script never imports from `tests/`.
# ---------------------------------------------------------------------------------------------


def _currentness_snapshot_wire() -> dict[str, Any]:
    wire = copy.deepcopy(BASE_WIRE)
    wire["assets"][3]["asset_id"] = "h3.font.noto_sans.v1"
    wire["clips"][3]["text"]["font_asset_id"] = "h3.font.noto_sans.v1"
    primary = wire["assets"][0]
    primary["source_frame_count"] = 48
    primary["landmarks"] = [
        {"frame_index": frame, "pts": frame * 512, "dts": frame * 512, "duration_ticks": 512}
        for frame in range(48)
    ]
    overlay = wire["assets"][1]
    overlay["embedded_audio"] = "absent"
    overlay["source_sample_count"] = None
    overlay["source_frame_count"] = 12
    overlay["landmarks"] = [
        {"frame_index": frame, "pts": frame * 512, "dts": frame * 512, "duration_ticks": 512}
        for frame in range(12)
    ]
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return wire


def _currentness_snapshot() -> PublicCompositionSnapshot:
    return decode_public_snapshot(_currentness_snapshot_wire())


def _enabled_source_fact(
    *, asset_id: str, origin: str, width: int, height: int, duration_ms: int | None
) -> PrivateSourceFact:
    source_profile_fingerprint = canonical_fingerprint(
        {"asset_id": asset_id, "profile": "measured_source_profile_v1"}
    )
    color_facts_fingerprint = canonical_fingerprint(
        {"asset_id": asset_id, "color": "measured_color_facts_v1"}
    )
    size_bytes = 1_000_000 if origin == "context_video" else 32_768
    facts_fingerprint = source_facts_fingerprint(
        asset_id=asset_id,
        source_id=asset_id,
        origin=origin,
        width=width,
        height=height,
        size_bytes=size_bytes,
        duration_milliseconds=duration_ms,
        source_profile_fingerprint=source_profile_fingerprint,
        color_facts_fingerprint=color_facts_fingerprint,
    )
    return PrivateSourceFact(
        asset_id=asset_id,
        source_id=asset_id,
        origin=origin,
        disposition="enabled",
        generation=3,
        source_fingerprint=canonical_fingerprint({"asset_id": asset_id, "bytes": "opaque"}),
        source_facts_fingerprint=facts_fingerprint,
        source_profile_fingerprint=source_profile_fingerprint,
        color_facts_fingerprint=color_facts_fingerprint,
        currentness_token=f"current:{asset_id}:g3",
        lease_fingerprint=canonical_fingerprint({"asset_id": asset_id, "lease": 3}),
        width=width,
        height=height,
        size_bytes=size_bytes,
        duration_milliseconds=duration_ms,
    )


def _currentness_source_manifest(snapshot: PublicCompositionSnapshot) -> PrivateSourceFactsManifest:
    facts = (
        _enabled_source_fact(
            asset_id="img-overlay", origin="runtime_image", width=320, height=180, duration_ms=None
        ),
        _enabled_source_fact(
            asset_id="vid-overlay", origin="context_video", width=320, height=180, duration_ms=500
        ),
        _enabled_source_fact(
            asset_id="vid-primary", origin="context_video", width=320, height=180, duration_ms=2_000
        ),
        # The timing source (B-58): the manifest must cover exactly the base's media assets.
        _enabled_source_fact(
            asset_id="vid-timing", origin="context_video", width=320, height=180, duration_ms=5_000
        ),
    )
    provisional = PrivateSourceFactsManifest(
        schema_version=PRIVATE_SOURCE_FACTS_MANIFEST_SCHEMA,
        workspace_handle=snapshot.workspace_handle,
        workspace_revision=snapshot.workspace_revision,
        timeline_revision=snapshot.timeline_revision,
        public_fingerprint=snapshot.public_fingerprint,
        generation=3,
        facts=facts,
        manifest_fingerprint="sha256:" + "0" * 64,
    )
    return replace(
        provisional, manifest_fingerprint=private_source_manifest_fingerprint(provisional)
    )


def _currentness_confirmation(
    manifest: PrivateSourceFactsManifest,
) -> SourceCurrentnessConfirmation:
    return SourceCurrentnessConfirmation(
        schema_version=SOURCE_CURRENTNESS_SCHEMA,
        manifest_fingerprint=manifest.manifest_fingerprint,
        workspace_handle=manifest.workspace_handle,
        workspace_revision=manifest.workspace_revision,
        timeline_revision=manifest.timeline_revision,
        public_fingerprint=manifest.public_fingerprint,
        generation=manifest.generation,
        tokens=tuple(
            SourceCurrentnessToken(
                asset_id=fact.asset_id,
                source_fingerprint=cast(str, fact.source_fingerprint),
                source_facts_fingerprint=cast(str, fact.source_facts_fingerprint),
                currentness_token=cast(str, fact.currentness_token),
                lease_fingerprint=cast(str, fact.lease_fingerprint),
            )
            for fact in manifest.facts
            if fact.disposition == "enabled"
        ),
    )


def _currentness_font_facts(snapshot: PublicCompositionSnapshot) -> PackagedFontFacts:
    title = next(clip for clip in snapshot.clips if clip.text is not None)
    text = title.text
    if text is None:
        raise SemanticConformanceError("the currentness fixture's title clip has no text")
    bindings = (
        FontTitleBindingFact(
            clip_id=title.clip_id,
            font_asset_id=text.font_asset_id,
            artifact_id="noto-sans-bold-normal",
            weight=text.weight,
            style=text.style,
            text_fingerprint=canonical_fingerprint({"content": text.content}),
            file_fingerprint=canonical_fingerprint({"font": "bold-normal"}),
            cmap_fingerprint=canonical_fingerprint({"cmap": "latin-v1"}),
            build_version="noto-sans-2.015",
        ),
    )
    provisional = PackagedFontFacts(
        schema_version=FONT_FACTS_SCHEMA,
        manifest_schema_version="h3.authoring.packaged_font_manifest.v1",
        profile_id="h3.authoring.font_profile.v1",
        fallback_order=("h3.font.noto_sans.v1",),
        license_spdx_id="OFL-1.1",
        manifest_fingerprint="sha256:" + "1" * 64,
        package_fingerprint="sha256:" + "2" * 64,
        license_file_fingerprint="sha256:" + "3" * 64,
        title_bindings=bindings,
        facts_fingerprint="sha256:" + "0" * 64,
    )
    return replace(provisional, facts_fingerprint=packaged_font_facts_fingerprint(provisional))


@lru_cache(maxsize=1)
def _currentness_fixture() -> tuple[
    RenderPlanV1 | None, SourceCurrentnessConfirmation | None, str | None
]:
    """Build the one accepted plan every currentness row revalidates against, once.

    A failure here blocks all three currentness rows with the same bounded reason rather than
    silently treating one as the others' proof: they revalidate the same plan against three
    different perturbations of its own currentness claim, never three independent plans.
    """

    try:
        snapshot = _currentness_snapshot()
        manifest = _currentness_source_manifest(snapshot)
        confirmation = _currentness_confirmation(manifest)
        fonts = _currentness_font_facts(snapshot)
        plan = plan_render(
            snapshot=snapshot, source_manifest=manifest, currentness=confirmation, font_facts=fonts
        )
    except ContractValidationError:
        return None, None, "constructing the bound render plan for currentness revalidation failed"
    return plan, confirmation, None


#: Which confirmation field each currentness row perturbs, declared before execution: the first two
#: exercise `revalidate_render_plan_currentness`'s snapshot-identity check (any of the three fields
#: it compares raises the same `stale_snapshot` code), and the third exercises its source-token
#: check. Never derive this mapping from what the call happens to raise.
_CURRENTNESS_PERTURBATION: Final = {
    "history_currentness.stale_snapshot_refused": "timeline_revision",
    "history_currentness.output_replaced_refused": "manifest_fingerprint",
    "history_currentness.source_replaced_refused": "source_token",
}


def _perturbed_currentness(
    confirmation: SourceCurrentnessConfirmation, field: str
) -> SourceCurrentnessConfirmation:
    if field == "timeline_revision":
        return replace(confirmation, timeline_revision=confirmation.timeline_revision + 1)
    if field == "manifest_fingerprint":
        return replace(confirmation, manifest_fingerprint="sha256:" + "9" * 64)
    first = confirmation.tokens[0]
    replacement = replace(
        first,
        source_fingerprint=canonical_fingerprint(
            {"asset_id": first.asset_id, "bytes": "qualify-replacement"}
        ),
    )
    return replace(confirmation, tokens=(replacement, *confirmation.tokens[1:]))


def _currentness_row(recipe: CaseRecipe) -> QualifyRow:
    declared = recipe.refusal_code or "accepted"
    field = _CURRENTNESS_PERTURBATION.get(recipe.case_id)
    if field is None:
        return _blocked_row(recipe, reason="no currentness perturbation is declared for this case")
    plan, base_confirmation, error = _currentness_fixture()
    if error is not None or plan is None or base_confirmation is None:
        return _blocked_row(recipe, reason=error or "the currentness fixture is unavailable")
    perturbed = _perturbed_currentness(base_confirmation, field)
    try:
        revalidate_render_plan_currentness(plan=plan, confirmation=perturbed)
    except ContractValidationError as exc:
        observed = _error_code(exc)
    else:
        observed = "accepted"
    status = "PASS" if observed == declared else "MISMATCH"
    return QualifyRow(
        recipe.case_id,
        recipe.case_class,
        recipe.observation,
        declared,
        observed,
        status,
        measurements=(("perturbed_field", field),),
    )


# ---------------------------------------------------------------------------------------------
# Dispatch and bounded execution.
# ---------------------------------------------------------------------------------------------


def _font_binding_row(recipe: CaseRecipe) -> QualifyRow:
    """Resolve the row's own title text against the packaged font manifest and record the outcome.

    GUARD: this route has to *run* the resolver. A declared refusal with no executable route is the
    B-25 defect -- two command-layer rows named a refusal the runner had nothing to drive, so they
    reported their own declaration back for the whole life of the corpus. The check here is the
    product's own `require_text_coverage`, called with the composition's declared font identity,
    weight, style and content; nothing about the expected code is consulted before it raises.
    """

    wire = copy.deepcopy(base_wire(recipe))
    try:
        apply_edits(wire, recipe)
    except SemanticConformanceError:
        return _blocked_row(recipe, reason="applying the recipe's declared edits raised an error")
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    declared = recipe.refusal_code or "accepted"
    try:
        snapshot = decode_public_snapshot(wire)
    except CompositionContractError as exc:
        # The composition decoder refused a row whose refusal is supposed to come later, which is a
        # real finding about the row rather than a reason to report the declared code.
        return QualifyRow(
            recipe.case_id,
            recipe.case_class,
            recipe.observation,
            declared,
            _error_code(exc),
            "MISMATCH",
        )
    title = next((clip for clip in snapshot.clips if clip.text is not None), None)
    if title is None or title.text is None:
        return _blocked_row(recipe, reason="the row's composition declares no text clip to bind")
    text = title.text
    try:
        face = require_text_coverage(
            load_packaged_font_manifest(), text.font_asset_id, text.weight, text.style, text.content
        )
    except AuthoringFontError as exc:
        observed = exc.code
        measurements = (("font_asset_id", text.font_asset_id),)
    else:
        observed = "accepted"
        measurements = (("resolved_font_artifact", face.artifact_id),)
    status = "PASS" if observed == declared else "MISMATCH"
    return QualifyRow(
        recipe.case_id,
        recipe.case_class,
        recipe.observation,
        declared,
        observed,
        status,
        measurements=measurements,
    )


def _evaluate_recipe(recipe: CaseRecipe) -> QualifyRow:
    if recipe.refusal_layer == "font_binding":
        return _font_binding_row(recipe)
    if recipe.refusal_layer == "currentness":
        return _currentness_row(recipe)
    if recipe.refusal_layer == "command":
        if recipe.command is None:
            return _blocked_row(
                recipe,
                reason="this command-layer refusal declares no backend-executable command payload",
            )
        return _command_refusal_row(recipe)
    if recipe.observation == "command_effect":
        return _command_effect_row(recipe)
    if recipe.observation == "declared_negative":
        return _deferred_negative_row(recipe)
    if recipe.observation in ("contract_refusal", "render_and_browser"):
        return _snapshot_layer_row(recipe)
    return _browser_scope_row(recipe)


def _run_row(recipe: CaseRecipe, *, row_timeout_seconds: float) -> QualifyRow:
    """Run one row on a daemon worker thread and enforce its deadline from this thread.

    CRITICAL: every path here is a pure, deterministic, in-process call -- no I/O, no subprocess --
    so a timeout means a genuine defect, not slow I/O. The worker is a daemon thread that is
    abandoned, never joined, on timeout: joining would let one stuck row freeze every row after it,
    and a non-daemon thread would block interpreter exit even after the run "finished". This mirrors
    the reader-thread-plus-queue shape already proven in `nle_semantic_conformance.decode_stream`.
    """

    outcome: queue.Queue[QualifyRow | BaseException] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            outcome.put(_evaluate_recipe(recipe))
        except BaseException as exc:  # noqa: BLE001 -- reported back on the caller's thread
            outcome.put(exc)

    threading.Thread(target=_worker, daemon=True).start()
    try:
        result = outcome.get(timeout=row_timeout_seconds)
    except queue.Empty:
        return _blocked_row(recipe, reason="the row exceeded its per-row execution deadline")
    if isinstance(result, BaseException):
        return _blocked_row(recipe, reason="the row raised an unexpected error during execution")
    return result


def build_report(
    book: RecipeBook,
    corpus: Corpus,
    *,
    row_timeout_seconds: float = DEFAULT_ROW_TIMEOUT_SECONDS,
    wall_ceiling_seconds: float = DEFAULT_WALL_CEILING_SECONDS,
) -> dict[str, Any]:
    """Execute every recipe and assemble the closed qualification report.

    Every recipe produces exactly one row, in corpus order, whether it ran, was blocked or was ruled
    out of this runner's scope -- a timeout or a wall-ceiling breach still emits a `BLOCKED` row for
    every remaining case, never a shorter output that `admit_report` would have to catch instead.
    """

    start = time.monotonic()
    ceiling_hit = False
    rows: list[QualifyRow] = []
    for recipe in book.recipes:
        if not ceiling_hit and time.monotonic() - start > wall_ceiling_seconds:
            ceiling_hit = True
        if ceiling_hit:
            rows.append(
                _blocked_row(
                    recipe,
                    reason="the run exceeded its wall-clock ceiling before this row executed",
                )
            )
            continue
        rows.append(_run_row(recipe, row_timeout_seconds=row_timeout_seconds))

    totals: dict[str, int] = dict.fromkeys(REPORT_STATUSES, 0)
    for row in rows:
        totals[row.status] += 1
    admission = admit_report(corpus, [(row.case_id, row.status) for row in rows])
    executed_rows = sum(count for status, count in totals.items() if status != "NOT_RUN")

    report: dict[str, Any] = {
        "schema": QUALIFY_REPORT_SCHEMA,
        "corpus_version": CORPUS_VERSION,
        "required_rows": len(corpus.cases),
        "executed_rows": executed_rows,
        "counts_by_class": corpus.counts_by_class(),
        "counts_by_observation": book.counts_by_observation(),
        "totals": totals,
        "admission": admission.as_wire(),
        "rows": [row.as_wire() for row in rows],
    }
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="path to write the JSON qualification report")
    parser.add_argument(
        "--describe",
        action="store_true",
        help="print corpus and recipe counts and exit without executing anything",
    )
    parser.add_argument(
        "--row-timeout-seconds",
        type=float,
        default=DEFAULT_ROW_TIMEOUT_SECONDS,
        help="per-row execution deadline",
    )
    parser.add_argument(
        "--wall-ceiling-seconds",
        type=float,
        default=DEFAULT_WALL_CEILING_SECONDS,
        help="whole-run wall-clock ceiling",
    )
    args = parser.parse_args(argv)

    corpus = build_corpus()
    book = build_recipes(corpus)

    if args.describe:
        print(
            json.dumps(
                {
                    "corpus_version": CORPUS_VERSION,
                    "required_rows": len(corpus.cases),
                    "counts_by_class": corpus.counts_by_class(),
                    "counts_by_observation": book.counts_by_observation(),
                },
                indent=2,
            )
        )
        return 0

    if args.output is None:
        parser.error("--output is required unless --describe is given")

    report = build_report(
        book,
        corpus,
        row_timeout_seconds=args.row_timeout_seconds,
        wall_ceiling_seconds=args.wall_ceiling_seconds,
    )
    cast(Path, args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "required_rows": report["required_rows"],
                "executed_rows": report["executed_rows"],
                "totals": report["totals"],
                "admitted": report["admission"]["admitted"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
