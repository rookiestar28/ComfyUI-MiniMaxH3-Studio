"""Deterministic provider-free Base prompt rendering.

The renderer consumes only accepted typed contracts and emits a `PromptDocument`.  It does not
inspect media, infer observations, call providers, or silently repair an invalid plan.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal

from .constraints import (
    ExactTextConstraint,
    KeepChangeAction,
    KeepChangeDirective,
    RequiredContent,
    TimePoint,
    TimingConstraint,
)
from .context_reporting import (
    ContextPlan,
    PromptDocument,
    PromptRenderStatus,
    PromptSection,
)
from .contracts import AssetRole, MediaKind, PromptProfile, TaskMode
from .dialogue_speakers import render_dialogue_line
from .errors import PromptRenderingError
from .intent_graph import (
    AudioIntent,
    AudioLayer,
    AudioOwnership,
    CameraIntent,
    EventCopy,
    IntentAction,
    IntentGraph,
    IntentScene,
    IntentSubject,
    RetentionDomain,
    RetentionRelation,
    RetentionScope,
    SoundscapeDisposition,
    StyleIntent,
    TimelineSegment,
)
from .production_semantics import (
    ProductionLocalShotV1,
    ProductionSemanticSliceV1,
)
from .profiles import DurationAlignment, PromptProfileRegistry, default_profile_registry

# The rendered stand-in for a soundscape the author has not described. It is deliberately not
# `N/A`, which the pinned guide reserves for an explicit request for a completely silent video.
UNSPECIFIED_SOUNDSCAPE_TEXT = "Not specified."

# `subject_definitions` is one of the six required Full-Reference fields, so it must say
# something even when the plan declares no reusable subject and no asset earns a standalone
# definition. Saying so plainly is honest; inventing a role for an unbound asset is not.
NO_SUBJECT_DEFINITIONS_TEXT = "No reusable subject was declared."


@dataclass(frozen=True, slots=True)
class ProductionSegmentPromptV1:
    """Canonical sections and exact text for one local Production segment."""

    text: str
    sections: tuple[PromptSection, ...]


def render_prompt_sections(
    sections: tuple[PromptSection, ...],
    *,
    preamble: str | None = None,
) -> str:
    """Serialize ordered prompt sections through the one canonical double-newline boundary."""

    if (
        not isinstance(sections, tuple)
        or not sections
        or not all(isinstance(section, PromptSection) for section in sections)
    ):
        raise PromptRenderingError("canonical rendering requires typed prompt sections")
    if tuple(section.order for section in sections) != tuple(range(1, len(sections) + 1)):
        raise PromptRenderingError("canonical prompt sections must use contiguous declared order")
    if preamble is not None and (
        not isinstance(preamble, str) or not preamble or "\x00" in preamble
    ):
        raise PromptRenderingError("canonical prompt preamble must be bounded non-empty text")
    body = "\n\n".join(f"{section.heading}: {section.body}" for section in sections)
    return f"{preamble}\n\n{body}" if preamble is not None else body


def _format_seconds(seconds: Decimal, precision: int) -> str:
    if not seconds.is_finite() or seconds < 0:
        raise PromptRenderingError("prompt time must be a finite nonnegative Decimal")
    quantum = Decimal(1).scaleb(-precision)
    return format(seconds.quantize(quantum, rounding=ROUND_HALF_UP), f".{precision}f")


def _format_cut_time(point: TimePoint) -> str:
    total = point.seconds.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    minutes = int(total // Decimal(60))
    seconds = total - Decimal(minutes * 60)
    return f"{minutes:02d}:{seconds:06.3f}"


def _join(parts: Iterable[str], separator: str = "; ") -> str:
    return separator.join(part for part in parts if part)


def _lookup(values: Iterable[object], identifier: str, field: str) -> object:
    for value in values:
        value_id = getattr(value, field, None)
        if value_id == identifier:
            return value
    raise PromptRenderingError(f"unknown {field} {identifier!r} in render graph")


def _label_for_role(plan: ContextPlan, role: AssetRole) -> str:
    matches = [asset for asset in plan.request.reference_registry.assets if asset.role is role]
    if len(matches) != 1 or matches[0].kind is not MediaKind.IMAGE:
        raise PromptRenderingError(f"Base mode requires one image asset with role {role.value}")
    try:
        return plan.request.reference_registry.label_for(matches[0].asset_id).label
    except Exception as exc:
        raise PromptRenderingError("reference registry has no deterministic frame label") from exc


def _subject_text(graph: IntentGraph, subject_id: str) -> str:
    subject = _lookup(graph.subjects, subject_id, "subject_id")
    if not isinstance(subject, IntentSubject):
        raise PromptRenderingError(f"unknown subject {subject_id!r} in render graph")
    description = f", {subject.description}" if subject.description else ""
    return f"{subject.label}{description}"


def _scene_text(graph: IntentGraph, scene_id: str | None) -> str:
    if scene_id is None:
        return ""
    scene = _lookup(graph.scenes, scene_id, "scene_id")
    if not isinstance(scene, IntentScene):
        raise PromptRenderingError(f"unknown scene {scene_id!r} in render graph")
    return scene.description


def _action_text(graph: IntentGraph, action_id: str) -> str:
    action = _lookup(graph.actions, action_id, "action_id")
    if not isinstance(action, IntentAction):
        raise PromptRenderingError(f"unknown action {action_id!r} in render graph")
    return action.description


def _capitalize(text: str) -> str:
    """Raise only the first character. The rest is the author's text and stays untouched."""

    return text[:1].upper() + text[1:] if text else text


def _sentence(text: str) -> str:
    """Close a composed fragment without doubling punctuation the author already wrote."""

    stripped = text.strip()
    if not stripped:
        return ""
    return stripped if stripped[-1] in ".!?" else f"{stripped}."


def _segment_audio(plan: ContextPlan, segment: TimelineSegment) -> tuple[AudioIntent, ...]:
    graph = plan.intent_graph
    resolved: list[AudioIntent] = []
    for audio_id in segment.audio_ids:
        audio = _lookup(graph.audios, audio_id, "audio_id")
        if not isinstance(audio, AudioIntent):
            raise PromptRenderingError(f"unknown audio {audio_id!r} in render graph")
        resolved.append(audio)
    return tuple(resolved)


def _audio_sentence(audio: AudioIntent) -> str:
    """Dialogue is already an action; anything else is stated as something the video is heard."""

    if audio.layer is AudioLayer.DIALOGUE:
        return _sentence(_capitalize(audio.description))
    return _sentence(f"{_capitalize(audio.description)} is heard")


def _segment_style(plan: ContextPlan, segment: TimelineSegment) -> str:
    if segment.style_id is None:
        return ""
    style_value = _lookup(plan.intent_graph.styles, segment.style_id, "style_id")
    if not isinstance(style_value, StyleIntent):
        raise PromptRenderingError(f"unknown style {segment.style_id!r} in render graph")
    return style_value.description


def _segment_camera(plan: ContextPlan, segment: TimelineSegment) -> str:
    if segment.camera_id is None:
        return ""
    camera_value = _lookup(plan.intent_graph.cameras, segment.camera_id, "camera_id")
    if not isinstance(camera_value, CameraIntent):
        raise PromptRenderingError(f"unknown camera {segment.camera_id!r} in render graph")
    return camera_value.description


def _segment_description(
    plan: ContextPlan,
    segment: TimelineSegment,
    *,
    include_style: bool = True,
    lead_capital: bool = True,
) -> str:
    """Compose one shot as natural prose from owned typed content.

    GUARD: this composes, it never invents. Every fragment below is text the author already wrote
    into the typed graph; only the connectives and sentence boundaries belong to the renderer. The
    former implementation emitted `subjects:`/`scene:`/`action:`/`camera:` label stacks and a
    generic "the declared scene develops along the timeline" fallback, which made a semantically
    empty plan indistinguishable from an authored one in the final prompt. If a shot owns nothing,
    this returns an empty string and the caller decides what is honest to say instead -- it must
    never resolve to a plausible-sounding sentence about a video nobody described.
    """

    graph = plan.intent_graph
    style = _segment_style(plan, segment)
    subjects = " and ".join(_subject_text(graph, identifier) for identifier in segment.subject_ids)
    scene = _scene_text(graph, segment.scene_id)
    actions = " and ".join(_action_text(graph, identifier) for identifier in segment.action_ids)
    camera = _segment_camera(plan, segment)

    # A later shot opens with the guide's own `[Shot N] At MM:SS.mmm,` prefix, and its prose is the
    # continuation of that clause, so the first fragment of such a shot is not capitalized.
    head = _capitalize if lead_capital else (lambda value: value)

    sentences: list[str] = []
    if style and include_style:
        sentences.append(_sentence(head(style)))
        head = _capitalize
    if subjects and actions:
        clause = f"{head(subjects)} {actions}"
        if scene:
            clause = f"{clause} in {scene}"
    elif subjects:
        # No action is declared, so the shot holds the subject rather than narrating one. This
        # phrasing also avoids inventing subject-verb agreement for an author-written noun phrase.
        clause = head(f"the shot holds {subjects}")
        if scene:
            clause = f"{clause} in {scene}"
    elif actions:
        clause = head(actions)
        if scene:
            clause = f"{clause} in {scene}"
    elif scene:
        clause = head(f"the shot is set in {scene}")
    else:
        clause = ""
    if clause:
        if camera:
            clause = f"{clause}, filmed with {camera}"
        sentences.append(_sentence(clause))
    elif camera:
        sentences.append(_sentence(head(f"the camera holds {camera}")))
    for value in plan.hard_constraints.exact_texts:
        if value.segment_id == segment.segment_id:
            sentences.append(render_dialogue_line(plan, value))
    for audio in _segment_audio(plan, segment):
        if audio.resolved_ownership is AudioOwnership.INTEGRATED:
            sentences.append(_audio_sentence(audio))
    return " ".join(part for part in sentences if part)


def _constraint_text(plan: ContextPlan) -> str:
    constraints = plan.hard_constraints
    lines: list[str] = []
    for text_value in constraints.exact_texts:
        if not isinstance(text_value, ExactTextConstraint):
            raise PromptRenderingError("hard constraint set returned an invalid exact-text value")
        if text_value.kind.value == "visible_text":
            lines.append(f'Visible text remains exactly "{text_value.text}".')
        elif text_value.segment_id is None:
            lines.append(render_dialogue_line(plan, text_value))
    for timing_value in constraints.timings:
        if not isinstance(timing_value, TimingConstraint):
            raise PromptRenderingError("hard constraint set returned an invalid timing value")
        end = "" if timing_value.end is None else f" to {timing_value.end.raw}"
        label = "" if timing_value.label is None else f" ({timing_value.label})"
        lines.append(f"Timing {timing_value.constraint_id}: {timing_value.start.raw}{end}{label}.")
    for required_value in constraints.required_content:
        if not isinstance(required_value, RequiredContent):
            raise PromptRenderingError("hard constraint set returned invalid required content")
        lines.append(f"Required {required_value.scope.value} content: {required_value.content}.")
    for directive in constraints.keep_change_directives:
        if not isinstance(directive, KeepChangeDirective):
            raise PromptRenderingError("hard constraint set returned an invalid directive")
        if directive.action is KeepChangeAction.KEEP:
            lines.append(f"Keep {directive.target.value}: {directive.value}.")
        else:
            if directive.replacement is None:
                raise PromptRenderingError(
                    f"change directive {directive.constraint_id} has no replacement"
                )
            lines.append(
                f"Change {directive.target.value} from {directive.value} to "
                f"{directive.replacement}."
            )
    return _join(lines, " ")


def _assert_constraints_preserved(plan: ContextPlan, text: str) -> None:
    for text_value in plan.hard_constraints.exact_texts:
        if text_value.text not in text:
            raise PromptRenderingError(f"exact text constraint {text_value.constraint_id} was lost")
        if (
            text_value.kind.value != "visible_text"
            and render_dialogue_line(plan, text_value) not in text
        ):
            raise PromptRenderingError(
                f"exact dialogue speaker, delivery or markup for "
                f"{text_value.constraint_id} was lost"
            )
    for required_value in plan.hard_constraints.required_content:
        if required_value.content not in text:
            raise PromptRenderingError(f"required content {required_value.constraint_id} was lost")
    for forbidden_value in plan.hard_constraints.forbidden_content:
        if forbidden_value.content in text:
            raise PromptRenderingError(
                f"forbidden content {forbidden_value.constraint_id} entered the rendered prompt"
            )


def _alignment_sentence(
    alignment: DurationAlignment,
    precision: int,
    *,
    first_label: str,
    last_label: str,
    duration: Decimal,
    last_shot: int,
) -> str:
    """The guide's keyframe alignment wording for one clip: its own frames, clock and shots."""

    if alignment is DurationAlignment.NONE:
        return ""
    zero = _format_seconds(Decimal(0), precision)
    if alignment is DurationAlignment.FIRST:
        return (
            f"For the target video, at {zero} seconds into the "
            f"target video, {first_label} (from [Shot 1]) is fully referenced."
        )
    mark = _format_seconds(duration, precision)
    if alignment is DurationAlignment.FIRST_AND_LAST:
        return (
            "How the reference pictures align with the target video — "
            f"{first_label.strip('<>')} (from Shot 1) aligns with the "
            f"{zero}-second mark of the target video; "
            f"{last_label.strip('<>')} (from Shot {last_shot}) aligns with the {mark}-second mark "
            "of the target video."
        )
    if alignment is DurationAlignment.LAST:
        return (
            "How the reference pictures align with the target video — "
            f"{last_label} (from [Shot {last_shot}]) aligns with the "
            f"{mark}-second mark of the target video."
        )
    raise PromptRenderingError("unsupported duration alignment")


_RENDERED_SHOT = re.compile(r"(?<!from )\[Shot ([1-9][0-9]{0,2})\]")


def _alignment_instruction(
    plan: ContextPlan, alignment: DurationAlignment, precision: int, description: str
) -> str:
    if alignment is DurationAlignment.NONE:
        return ""
    # GUARD: the final frame belongs to the last shot the description renders, not to the last
    # typed segment. The product's intent-graph producers emit one segment even when the author
    # wrote several `[Shot N]` markers, so counting segments says "from Shot 1" about a frame that
    # closes Shot 3.
    rendered = _RENDERED_SHOT.findall(description)
    last_shot = max(len(plan.intent_graph.segments), int(rendered[-1]) if rendered else 1)
    first = alignment in {DurationAlignment.FIRST, DurationAlignment.FIRST_AND_LAST}
    last = alignment in {DurationAlignment.LAST, DurationAlignment.FIRST_AND_LAST}
    return _alignment_sentence(
        alignment,
        precision,
        first_label=_label_for_role(plan, AssetRole.FIRST_FRAME) if first else "",
        last_label=_label_for_role(plan, AssetRole.LAST_FRAME) if last else "",
        duration=plan.intent_graph.effective_duration.seconds,
        last_shot=last_shot,
    )


def _shot_prefix(index: int, segment: TimelineSegment) -> str:
    return "[Shot 1]" if index == 0 else f"[Shot {index + 1}] At {_format_cut_time(segment.start)},"


_AUTHORED_FIRST_SHOT = re.compile(r"\A\s*\[Shot 1\](?! At )\s*")


def _opening_intent_prose(plan: ContextPlan) -> str:
    """The author's own words for an opening shot the typed plan owns no content for."""

    # GUARD: the renderer owns the `[Shot 1]` prefix. An author who writes a storyboard opens it
    # with `[Shot 1]` too, and carrying that marker as prose renders `[Shot 1] [Shot 1] ...`: a
    # shot list no parser accepts, so whole-video planning refuses the Context. Drop only that one
    # leading marker; every other marker and every word stay the author's.
    intent = plan.request.user_intent
    return _sentence(_capitalize(_AUTHORED_FIRST_SHOT.sub("", intent, count=1) or intent))


def _summary_audio(plan: ContextPlan) -> tuple[list[str], list[str]]:
    """Partition audio into the two official summary fields by owned typed evidence.

    GUARD: only a *resolved* ownership and an actual contribution may place a sound. An item on an
    ambiguous layer resolves to `None` and enters neither field, and a timeline-scoped item that no
    shot links to contributes nothing. The former implementation walked every `graph.audios` entry
    and keyed off the layer alone, which put diegetic sound and audience-only score in the wrong
    guide sections and aggregated audio no shot had ever claimed.
    """

    graph = plan.intent_graph
    soundscape_values: list[str] = []
    music_values: list[str] = []
    for audio in graph.audios:
        if not graph.contributes_to_summary(audio):
            continue
        ownership = audio.resolved_ownership
        if ownership is AudioOwnership.SOUNDSCAPE:
            soundscape_values.append(audio.description)
        elif ownership is AudioOwnership.AUDIENCE_ONLY:
            music_values.append(audio.description)
    return soundscape_values, music_values


def _soundscape_text(plan: ContextPlan, values: list[str]) -> str:
    """`N/A` is a claim of complete silence and needs the author's explicit authority.

    GUARD: the pinned guide permits `overall_soundscape: N/A` only when the user asked for a
    completely silent video. Rendering it because the audio tuple happened to be empty told the
    model "silent" on the strength of the author having said nothing at all -- the exact defect
    M24-05 closes. An unspecified soundscape stays visibly unspecified.
    """

    if plan.intent_graph.soundscape is SoundscapeDisposition.EXPLICIT_COMPLETE_SILENCE:
        return "N/A"
    return _join(values, ". ") or UNSPECIFIED_SOUNDSCAPE_TEXT


def _render_sections(plan: ContextPlan, render_order: tuple[str, ...]) -> tuple[PromptSection, ...]:
    graph = plan.intent_graph
    segments = graph.segments
    if not segments:
        raise PromptRenderingError("Base rendering requires at least one timeline segment")
    description_parts: list[str] = []
    for index, segment in enumerate(segments):
        prose = _segment_description(plan, segment, lead_capital=index == 0)
        if not prose and index == 0:
            # The plan owns no semantic content for the opening shot, so the only truthful thing
            # to say is what the user actually wrote. It is carried as prose, not behind a
            # repository-owned `Declared intent:` marker that the guide has no field for.
            prose = _opening_intent_prose(plan)
        if prose:
            description_parts.append(f"{_shot_prefix(index, segment)} {prose}")
    constraint_text = _constraint_text(plan)
    if constraint_text:
        description_parts.append(constraint_text)
    integrated = " ".join(description_parts)
    soundscape_values, music_values = _summary_audio(plan)
    soundscape = _soundscape_text(plan, soundscape_values)
    music = _join(music_values, ". ") or "N/A"
    values: dict[str, str] = {
        "integrated_multimodal_description": integrated,
        "overall_soundscape": soundscape,
        "non_diegetic_music": music,
    }
    if set(render_order) != set(values):
        raise PromptRenderingError("Base profile render order does not match Base fields")
    sections = tuple(
        PromptSection(
            section_id=f"base_section_{index}",
            order=index,
            heading=field,
            body=values[field],
            source_evidence_ids=tuple(record.evidence_id for record in plan.evidence.records),
        )
        for index, field in enumerate(render_order, start=1)
    )
    return sections


def render_base_prompt(
    plan: ContextPlan,
    registry: PromptProfileRegistry | None = None,
) -> PromptDocument:
    """Render one validated Base plan into deterministic guide-shaped prompt text."""

    if not isinstance(plan, ContextPlan):
        raise PromptRenderingError("Base rendering requires a ContextPlan")
    request = plan.request
    if request.profile.name is not PromptProfile.BASE:
        raise PromptRenderingError("Base renderer received a non-Base profile")
    profiles = default_profile_registry() if registry is None else registry
    profile = profiles.get(request.profile)
    if not profile.supports(request.task_mode):
        raise PromptRenderingError(f"Base profile does not support {request.task_mode.value}")
    if plan.intent_graph.effective_duration.seconds != Decimal(
        str(request.effective_duration_seconds)
    ):
        raise PromptRenderingError("plan graph duration does not match normalized request duration")
    graph_diagnostics = plan.intent_graph.validate()
    if any(diagnostic.severity.value in {"error", "fatal"} for diagnostic in graph_diagnostics):
        raise PromptRenderingError("intent graph contains blocking diagnostics")
    sections = _render_sections(plan, profile.render_order)
    instruction = _alignment_instruction(
        plan,
        profile.duration_rule.alignment_for(request.task_mode),
        profile.duration_rule.time_precision,
        next(
            section.body
            for section in sections
            if section.heading == "integrated_multimodal_description"
        ),
    )
    text = render_prompt_sections(sections, preamble=instruction or None)
    _assert_constraints_preserved(plan, text)
    return PromptDocument(
        document_id=f"prompt_{plan.plan_id}",
        schema_version=request.schema_version,
        profile=request.profile,
        task_mode=request.task_mode,
        plan_id=plan.plan_id,
        text=text,
        sections=sections,
        status=PromptRenderStatus.RENDERED,
        source_evidence_ids=tuple(record.evidence_id for record in plan.evidence.records),
    )


def _label_for_asset_id(plan: ContextPlan, asset_id: str) -> str:
    try:
        return plan.request.reference_registry.label_for(asset_id).label
    except Exception as exc:
        raise PromptRenderingError(
            f"reference registry has no deterministic label for asset {asset_id!r}"
        ) from exc


def _labels_for_assets(plan: ContextPlan, asset_ids: tuple[str, ...]) -> str:
    return ", ".join(_label_for_asset_id(plan, asset_id) for asset_id in asset_ids)


def _full_subject_definition(plan: ContextPlan, subject: IntentSubject) -> str:
    subject_number = next(
        index
        for index, candidate in enumerate(plan.intent_graph.subjects, start=1)
        if candidate.subject_id == subject.subject_id
    )
    source = ""
    if subject.source_asset_ids:
        source = f" from {_labels_for_assets(plan, subject.source_asset_ids)}"
    description = f", {subject.description}" if subject.description else ""
    return f"<Subject {subject_number}> is {subject.label}{description}{source}."


def _full_subject_label(plan: ContextPlan, subject_id: str) -> str:
    for index, subject in enumerate(plan.intent_graph.subjects, start=1):
        if subject.subject_id == subject_id:
            return f"<Subject {index}>"
    raise PromptRenderingError(f"unknown subject {subject_id!r} in render graph")


def _full_target_label(plan: ContextPlan, target_id: str) -> str:
    """Name a retention target in prompt vocabulary.

    GUARD: `<Audio N>` numbers the *reference assets* in the registry, so an audio intent must
    never be given one -- doing so produced lines like "<Audio 1> is copied in full in <Audio 1>",
    where the same label meant two different things. An audio intent is named by what it is, and
    an unknown target fails closed rather than falling through to its internal identifier.
    """

    for index, subject in enumerate(plan.intent_graph.subjects, start=1):
        if subject.subject_id == target_id:
            return f"<Subject {index}>"
    for audio in plan.intent_graph.audios:
        if audio.audio_id == target_id:
            return audio.description
    raise PromptRenderingError(f"unknown retention target {target_id!r} in render graph")


# What each declared reference role actually is, in prompt vocabulary. A source-only asset stays
# inside its subject definition; a separately declared whole-video/frame/audio role keeps its own
# definition even when the same asset also supplies a subject.
#
# GUARD: media presence is not a semantic role. Emitting a standalone definition for an unbound
# generic input manufactures guide authority; suppressing an independently declared editing,
# continuation, frame, or audio role loses a real task. Keep those decisions role/graph-derived.
_ROLE_PROSE: dict[AssetRole, str] = {
    AssetRole.PRIMARY: "the primary composition anchor",
    AssetRole.FIRST_FRAME: "the first frame of the target video",
    AssetRole.LAST_FRAME: "the final frame of the target video",
    AssetRole.EDITING_SOURCE: "the footage being edited",
    AssetRole.CONTINUATION_SOURCE: "the footage the target continues from",
    AssetRole.MOTION_REFERENCE: "the motion reference",
    AssetRole.CAMERA_REFERENCE: "the camera reference",
    AssetRole.STYLE_REFERENCE: "the style reference",
    AssetRole.SUBJECT_REFERENCE: "a supplied subject reference",
    AssetRole.AUDIO_SOURCE: "the supplied audio track",
}

_GENERIC_REFERENCE_PROSE: dict[MediaKind, str] = {
    MediaKind.IMAGE: "a supplied visual reference",
    MediaKind.VIDEO: "a supplied video reference",
    MediaKind.AUDIO: "a supplied audio reference",
}

_INDEPENDENT_STANDALONE_ROLES = frozenset(
    {
        AssetRole.PRIMARY,
        AssetRole.FIRST_FRAME,
        AssetRole.LAST_FRAME,
        AssetRole.EDITING_SOURCE,
        AssetRole.CONTINUATION_SOURCE,
        AssetRole.MOTION_REFERENCE,
        AssetRole.CAMERA_REFERENCE,
        AssetRole.STYLE_REFERENCE,
    }
)

_RETENTION_PROSE: dict[str, str] = {
    "fully_preserved": "is preserved in full",
    "partially_preserved": "is partially preserved",
    "attribute_transfer": "contributes its attributes",
    "weak_reference": "is loosely referenced",
    "fully_copy": "is copied in full",
    "partially_copy": "is partially copied",
    "reference": "is referenced",
}

# GUARD: an event line sits inside the shot it belongs to, so its wording must name that shot
# rather than relying on position alone. `semantic_graph_model` imports this map to recover the
# copy mode from the prose; keep each phrase distinct from `_RETENTION_PROSE`, whose entries are
# prefixes of these, or a retention sentence and an event sentence become indistinguishable.
_EVENT_PROSE: dict[str, str] = {
    "full_copy": "is copied in full into this shot",
    "partial_copy": "is partially copied into this shot",
    "reference": "is used as a reference for this shot",
}


def _prose_alternation(values: Iterable[str]) -> str:
    """A regex alternation over one of the closed prose vocabularies above, longest phrase first.

    The readers of this module's output -- the semantic-graph parser and the source-profiled
    comparator -- recover typed facts from these phrases, so they build their patterns from the
    same maps the renderer writes with instead of restating the wording.
    """

    return "|".join(re.escape(item) for item in sorted(set(values), key=len, reverse=True))


def _full_asset_definitions(plan: ContextPlan) -> tuple[str, ...]:
    """Standalone reference definitions, by the role each asset actually plays.

    Subject-only provenance remains inside the subject definition. Independently role-qualified
    assets and audio sources that actually participate in the graph retain their own definition.
    """

    standalone = _full_standalone_reference_asset_ids(plan)
    lines: list[str] = []
    for asset in plan.request.reference_registry.assets:
        if asset.asset_id not in standalone:
            continue
        description = _ROLE_PROSE.get(asset.role) or _GENERIC_REFERENCE_PROSE[asset.kind]
        lines.append(f"{_label_for_asset_id(plan, asset.asset_id)} is {description}.")
    return tuple(lines)


def _full_standalone_reference_asset_ids(plan: ContextPlan) -> frozenset[str]:
    registry_ids = {asset.asset_id for asset in plan.request.reference_registry.assets}
    result = {
        asset.asset_id
        for asset in plan.request.reference_registry.assets
        if asset.role in _INDEPENDENT_STANDALONE_ROLES
    }
    result.update(
        asset_id for audio in plan.intent_graph.audios for asset_id in audio.source_asset_ids
    )
    result.update(
        asset_id
        for relation in plan.intent_graph.retention
        if relation.domain is RetentionDomain.AUDIO
        for asset_id in relation.source_asset_ids
    )
    result.update(event.source_asset_id for event in plan.intent_graph.events)
    result.update(
        relation.target_id
        for relation in plan.intent_graph.retention
        if relation.scope in {RetentionScope.PICTURE, RetentionScope.VIDEO_STRUCTURE}
        and relation.target_id in registry_ids
    )
    return frozenset(result)


def full_reference_declared_asset_ids(plan: ContextPlan) -> frozenset[str]:
    """Asset IDs whose declared typed role requires a label in the rendered prompt."""

    subject_sources = {
        asset_id for subject in plan.intent_graph.subjects for asset_id in subject.source_asset_ids
    }
    return frozenset(subject_sources | set(_full_standalone_reference_asset_ids(plan)))


_FULL_TASK_ORDER = (
    "video editing",
    "video continuation",
    "keyframe completion",
    "reference generation",
    "audio reuse",
    "audio reference",
)


def _full_task_types(plan: ContextPlan) -> tuple[str, ...]:
    """Return every independently declared Full task in one deterministic local order."""

    roles = {asset.role for asset in plan.request.reference_registry.assets}
    tasks: set[str] = set()
    if AssetRole.EDITING_SOURCE in roles:
        tasks.add("video editing")
    if AssetRole.CONTINUATION_SOURCE in roles:
        tasks.add("video continuation")
    if roles & {AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME}:
        tasks.add("keyframe completion")
    if any(subject.source_asset_ids for subject in plan.intent_graph.subjects) or roles & {
        AssetRole.MOTION_REFERENCE,
        AssetRole.CAMERA_REFERENCE,
        AssetRole.STYLE_REFERENCE,
        AssetRole.SUBJECT_REFERENCE,
    }:
        tasks.add("reference generation")

    audio_relations = tuple(
        relation
        for relation in plan.intent_graph.retention
        if relation.domain is RetentionDomain.AUDIO
    )
    if any(
        relation.marker.value in {"fully_copy", "partially_copy"} for relation in audio_relations
    ):
        tasks.add("audio reuse")
    if any(
        relation.marker.value in {"reference", "weak_reference"} for relation in audio_relations
    ):
        tasks.add("audio reference")
    declared_audio_sources = {
        asset_id for audio in plan.intent_graph.audios for asset_id in audio.source_asset_ids
    }
    related_audio_sources = {
        asset_id for relation in audio_relations for asset_id in relation.source_asset_ids
    }
    if declared_audio_sources - related_audio_sources:
        tasks.add("audio reference")
    return tuple(task for task in _FULL_TASK_ORDER if task in tasks)


def _full_summary_prefix(plan: ContextPlan) -> str:
    return " + ".join(_full_task_types(plan))


def _full_summary(plan: ContextPlan) -> str:
    task_types = _full_summary_prefix(plan)
    task_prefix = (
        f"[{task_types}]" if task_types else "The task relationship is not explicitly declared."
    )
    editing = tuple(
        _label_for_asset_id(plan, asset.asset_id)
        for asset in plan.request.reference_registry.assets
        if asset.role is AssetRole.EDITING_SOURCE
    )
    introduction = ""
    if editing:
        introduction = _sentence("The target video is an edited version of " + ", ".join(editing))
    return " ".join(
        part for part in (task_prefix, introduction, _synopsis(plan.request.user_intent)) if part
    )


_AUTHORED_SHOT = re.compile(r"(?<!from )\[Shot [1-9][0-9]{0,2}\](?: At [^,]{0,32},)?")


def _synopsis(intent: str) -> str:
    """The author's intent as prose, without the shot markers a storyboard is written with."""

    # GUARD: `summary` is a synopsis; shot markers and cut times belong to the description. Copying
    # an authored storyboard here puts every cut time into the prompt twice, and the timestamp
    # check, which reads the whole prompt, then sees the sequence start over: the Context is
    # invalid before it can be generated or planned. Remove the markers only; every word stays the
    # author's, and an intent without markers is carried exactly as before.
    if _AUTHORED_SHOT.search(intent) is None:
        return _sentence(intent)
    beats = (beat.strip() for beat in _AUTHORED_SHOT.split(intent))
    return " ".join(_sentence(_capitalize(beat)) for beat in beats if beat)


def _full_defined_denotation_label(plan: ContextPlan, relation: RetentionRelation) -> str:
    if relation.scope is RetentionScope.SUBJECT:
        return _full_subject_label(plan, relation.target_id)
    if relation.scope in {RetentionScope.PICTURE, RetentionScope.VIDEO_STRUCTURE}:
        return _label_for_asset_id(plan, relation.target_id)
    if relation.scope in {
        RetentionScope.AUDIO_LAYER,
        RetentionScope.COMPLETE_FINAL_AUDIO_TRACK,
    }:
        if len(relation.source_asset_ids) != 1:
            raise PromptRenderingError(
                "a scoped audio retention entry requires one independently tracked audio label"
            )
        return _label_for_asset_id(plan, relation.source_asset_ids[0])
    raise PromptRenderingError("official retention rendering requires an explicit denotation scope")


def _require_complete_final_audio_track(plan: ContextPlan, relation: RetentionRelation) -> None:
    if not plan.intent_graph.complete_final_audio_track_is_consistent(
        relation.relation_id,
        source_registry=plan.request.reference_registry,
        hard_constraints=plan.hard_constraints,
    ):
        raise PromptRenderingError(
            "complete final-track reuse requires one sole unchanged audio contribution"
        )


def _current_retention_analysis(plan: ContextPlan, relation: RetentionRelation, target: str) -> str:
    sources = _labels_for_assets(plan, relation.source_asset_ids)
    marker = relation.marker.value
    if relation.scope is RetentionScope.AUDIO_LAYER and marker == "fully_copy":
        raise PromptRenderingError("audio-layer scope cannot claim complete final-track reuse")
    if relation.scope is RetentionScope.COMPLETE_FINAL_AUDIO_TRACK and marker != "fully_copy":
        raise PromptRenderingError("complete final-track scope requires the fully_copy marker")
    if relation.scope is RetentionScope.COMPLETE_FINAL_AUDIO_TRACK:
        _require_complete_final_audio_track(plan, relation)
        return f"{sources} is reused 1:1 as the target video's complete final audio track"
    if relation.scope is RetentionScope.AUDIO_LAYER and marker == "partially_copy":
        return f"the selected audio layer from {sources} is copied into the target final track"
    if relation.scope is RetentionScope.AUDIO_LAYER:
        return (
            f"the target audio layer follows {sources} without claiming complete final-track reuse"
        )
    denotation = relation.scope.value.replace("_", " ")
    return f"{target} retains its defined {denotation} role from {sources}"


def _full_retention_line(plan: ContextPlan, relation: RetentionRelation) -> str:
    if relation.scope is not RetentionScope.UNSPECIFIED:
        target = _full_defined_denotation_label(plan, relation)
        return _sentence(
            f"{target}: {relation.marker.value} - "
            f"{_current_retention_analysis(plan, relation, target)}"
        )
    sources = _labels_for_assets(plan, relation.source_asset_ids)
    prose = _RETENTION_PROSE.get(relation.marker.value)
    if prose is None:
        raise PromptRenderingError(f"unmapped retention marker {relation.marker.value!r}")
    target = _full_target_label(plan, relation.target_id)
    return _sentence(f"{sources} {prose} in {target}")


def _full_event_line(plan: ContextPlan, event: EventCopy) -> str:
    source = _label_for_asset_id(plan, event.source_asset_id)
    prose = _EVENT_PROSE.get(event.mode.value)
    if prose is None:
        raise PromptRenderingError(f"unmapped event copy mode {event.mode.value!r}")
    return _sentence(f"{source} {prose}")


def _full_segment_description(
    plan: ContextPlan, segment: TimelineSegment, *, lead_capital: bool = True
) -> str:
    """One shot of the detailed description, saying where each reference applies."""

    graph = plan.intent_graph
    sentences: list[str] = []
    prose = _segment_description(plan, segment, include_style=False, lead_capital=lead_capital)
    if prose:
        sentences.append(prose)
    for subject_id in segment.subject_ids:
        subject = _lookup(graph.subjects, subject_id, "subject_id")
        if not isinstance(subject, IntentSubject):
            raise PromptRenderingError(f"unknown subject {subject_id!r} in render graph")
        if subject.source_asset_ids:
            sentences.append(
                _sentence(
                    f"{_full_subject_label(plan, subject.subject_id)} follows "
                    f"{_labels_for_assets(plan, subject.source_asset_ids)}"
                )
            )
    for audio in _segment_audio(plan, segment):
        if audio.source_asset_ids:
            sentences.append(
                _sentence(
                    f"{_capitalize(audio.description)} follows "
                    f"{_labels_for_assets(plan, audio.source_asset_ids)}"
                )
            )
    for event_id in segment.event_ids:
        event = _lookup(graph.events, event_id, "event_id")
        if not isinstance(event, EventCopy):
            raise PromptRenderingError(f"unknown event {event_id!r} in render graph")
        sentences.append(_full_event_line(plan, event))
    return " ".join(part for part in sentences if part)


def _full_description(plan: ContextPlan) -> str:
    graph = plan.intent_graph
    style_prefix = ""
    if graph.styles:
        # One or two natural sentences before Shot 1. `Declared style:` was a repository-owned
        # label with no field in the guide, so it is stated as prose instead.
        style_prefix = (
            " ".join(_sentence(_capitalize(style.description)) for style in graph.styles) + " "
        )
    shots: list[str] = []
    for index, segment in enumerate(graph.segments):
        prose = _full_segment_description(plan, segment, lead_capital=index == 0)
        if not prose and index == 0:
            prose = _opening_intent_prose(plan)
        if prose:
            shots.append(f"{_shot_prefix(index, segment)} {prose}")
    constraint_text = _constraint_text(plan)
    if constraint_text:
        shots.append(constraint_text)
    return style_prefix + " ".join(shots)


def render_full_reference_prompt(
    plan: ContextPlan,
    registry: PromptProfileRegistry | None = None,
) -> PromptDocument:
    """Render one validated REF2VA plan into the six-section Full-Reference format."""

    if not isinstance(plan, ContextPlan):
        raise PromptRenderingError("Full-Reference rendering requires a ContextPlan")
    request = plan.request
    if (
        request.profile.name is not PromptProfile.FULL_REFERENCE
        or request.task_mode is not TaskMode.REF2VA
    ):
        raise PromptRenderingError("Full-Reference renderer requires a ref2va plan")
    profiles = default_profile_registry() if registry is None else registry
    profile = profiles.get(request.profile)
    if not profile.supports(request.task_mode):
        raise PromptRenderingError("Full-Reference profile does not support ref2va")
    if not plan.request.reference_registry.assets:
        raise PromptRenderingError("Full-Reference rendering requires explicit reference assets")
    if plan.intent_graph.effective_duration.seconds != Decimal(
        str(request.effective_duration_seconds)
    ):
        raise PromptRenderingError("plan graph duration does not match normalized request duration")
    graph_diagnostics = plan.intent_graph.validate()
    if any(diagnostic.severity.value in {"error", "fatal"} for diagnostic in graph_diagnostics):
        raise PromptRenderingError("intent graph contains blocking diagnostics")
    if not plan.intent_graph.segments:
        raise PromptRenderingError(
            "Full-Reference rendering requires at least one timeline segment"
        )
    subject_lines = tuple(
        _full_subject_definition(plan, subject) for subject in plan.intent_graph.subjects
    )
    subject_definitions = (
        "\n".join(subject_lines + _full_asset_definitions(plan)) or NO_SUBJECT_DEFINITIONS_TEXT
    )
    retention_lines = tuple(
        _full_retention_line(plan, relation) for relation in plan.intent_graph.retention
    )
    if not retention_lines:
        retention_lines = ("No explicit retention relations were declared.",)
    # The same ownership and contribution rules as Base: a sound reaches a summary field only when
    # its ownership is resolved and it actually contributes, and `N/A` remains a silence claim.
    audio_lines: list[str] = []
    music_lines: list[str] = []
    for audio in plan.intent_graph.audios:
        if not plan.intent_graph.contributes_to_summary(audio):
            continue
        source = (
            f" ({_labels_for_assets(plan, audio.source_asset_ids)})"
            if audio.source_asset_ids
            else ""
        )
        line = f"{audio.description}{source}"
        ownership = audio.resolved_ownership
        if ownership is AudioOwnership.SOUNDSCAPE:
            audio_lines.append(line)
        elif ownership is AudioOwnership.AUDIENCE_ONLY:
            music_lines.append(line)
    values: dict[str, str] = {
        "subject_definitions": subject_definitions,
        "summary": _full_summary(plan),
        "retention_analysis": "\n".join(retention_lines),
        "detailed_description": _full_description(plan),
        "overall_soundscape": _soundscape_text(plan, audio_lines),
        "non_diegetic_music": _join(music_lines, ". ") or "N/A",
    }
    if set(profile.render_order) != set(values):
        raise PromptRenderingError("Full-Reference profile render order does not match fields")
    sections = tuple(
        PromptSection(
            section_id=f"full_reference_section_{index}",
            order=index,
            heading=field,
            body=values[field],
            source_evidence_ids=tuple(record.evidence_id for record in plan.evidence.records),
        )
        for index, field in enumerate(profile.render_order, start=1)
    )
    text = render_prompt_sections(sections)
    _assert_constraints_preserved(plan, text)
    return PromptDocument(
        document_id=f"prompt_{plan.plan_id}",
        schema_version=request.schema_version,
        profile=request.profile,
        task_mode=request.task_mode,
        plan_id=plan.plan_id,
        text=text,
        sections=sections,
        status=PromptRenderStatus.RENDERED,
        source_evidence_ids=tuple(record.evidence_id for record in plan.evidence.records),
    )


def _format_local_shot_time(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds, remainder = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{remainder:03d}"


def _production_alignment(
    semantic_slice: ProductionSemanticSliceV1,
    local_labels: dict[str, str],
    shot_count: int,
    clip_duration_seconds: Decimal,
) -> str | None:
    """The alignment sentence one production clip states about itself, or none."""

    # GUARD: an alignment sentence states one clip's own clock, picture numbers and shot count, so
    # it is derived here from the clip's mode, the frames it holds and its delivered duration. It
    # must never be copied from the source Context: as a carried field it was refused by planning
    # for first-frame and last-frame sources (the frame-scoped field is rejected in every segment
    # without the frame) and repeated the source clip's duration mark in every segment, text-only
    # ones included, for two-frame sources.
    definitions = default_profile_registry().for_mode(semantic_slice.task_mode)
    if len(definitions) != 1:
        raise PromptRenderingError("Production segment mode has no single prompt profile")
    rule = definitions[0].duration_rule
    alignment = rule.alignment_for(semantic_slice.task_mode)
    if alignment is DurationAlignment.NONE:
        return None

    def frame(role: AssetRole) -> str | None:
        labels = [
            local_labels[item.backend_label]
            for item in semantic_slice.reference_definitions
            if item.role is role and item.kind is MediaKind.IMAGE
        ]
        return labels[0] if len(labels) == 1 else None

    first = (
        frame(AssetRole.FIRST_FRAME)
        if alignment in {DurationAlignment.FIRST, DurationAlignment.FIRST_AND_LAST}
        else ""
    )
    last = (
        frame(AssetRole.LAST_FRAME)
        if alignment in {DurationAlignment.LAST, DurationAlignment.FIRST_AND_LAST}
        else ""
    )
    if first is None or last is None:
        # The clip does not hold the frame its mode anchors. The proposal already blocks that
        # (`required_asset_missing`); the prompt must not state an alignment it cannot name.
        return None
    return _alignment_sentence(
        alignment,
        rule.time_precision,
        first_label=first,
        last_label=last,
        duration=clip_duration_seconds,
        last_shot=shot_count,
    )


def render_production_segment_prompt(
    semantic_slice: ProductionSemanticSliceV1,
    shots: tuple[ProductionLocalShotV1, ...],
    *,
    section_heading: str,
    clip_duration_seconds: Decimal,
    hard_constraints: tuple[str, ...] = (),
) -> ProductionSegmentPromptV1:
    """Render one sliced proposal using only canonical headings and shot notation.

    GUARD: Production segmentation may rebase owned content, but it must not introduce an
    alternative label language. New `production_segment:` metadata headings would make local
    prompts impossible to compare with the canonical Base/Full renderers.
    """

    if not isinstance(semantic_slice, ProductionSemanticSliceV1):
        raise PromptRenderingError("Production rendering requires a semantic slice")
    if section_heading not in {
        "integrated_multimodal_description",
        "detailed_description",
    }:
        raise PromptRenderingError("Production rendering requires a canonical description heading")
    if (
        not isinstance(shots, tuple)
        or not shots
        or not all(isinstance(shot, ProductionLocalShotV1) for shot in shots)
    ):
        raise PromptRenderingError("Production rendering requires typed local shots")
    if tuple(shot.ordinal for shot in shots) != tuple(range(1, len(shots) + 1)):
        raise PromptRenderingError("Production local shots must use contiguous ordinals")
    if (
        tuple(shot.start_milliseconds for shot in shots)
        != tuple(sorted(shot.start_milliseconds for shot in shots))
        or shots[0].start_milliseconds != 0
    ):
        raise PromptRenderingError("Production local shots must begin at zero in timeline order")
    if not isinstance(hard_constraints, tuple) or not all(
        isinstance(value, str) and value and "\x00" not in value for value in hard_constraints
    ):
        raise PromptRenderingError("Production hard constraints must be exact bounded text")
    if (
        not isinstance(clip_duration_seconds, Decimal)
        or not clip_duration_seconds.is_finite()
        or clip_duration_seconds <= 0
    ):
        raise PromptRenderingError("Production rendering requires the clip's delivered duration")

    shot_parts: list[str] = []
    for index, shot in enumerate(shots):
        next_start = (
            shots[index + 1].start_milliseconds
            if index + 1 < len(shots)
            else semantic_slice.global_end_milliseconds - semantic_slice.global_start_milliseconds
        )
        prose_parts = [shot.prose]
        for span in semantic_slice.timed_spans:
            if shot.start_milliseconds <= span.start_milliseconds < next_start:
                # IMPORTANT: retaining words without their rebased interval silently changes
                # timed audio/action meaning. Emit the local clock even when prose repeats it.
                prose_parts.append(
                    f"From {_format_local_shot_time(span.start_milliseconds)} to "
                    f"{_format_local_shot_time(span.end_milliseconds)}, {span.text}"
                )
        for dialogue in shot.exact_dialogue:
            if dialogue not in " ".join(prose_parts):
                prose_parts.append(dialogue)
        for visible_text in shot.visible_text:
            if visible_text not in " ".join(prose_parts):
                prose_parts.append(f'Visible text remains exactly "{visible_text}".')
        prose = " ".join(prose_parts)
        prefix = (
            "[Shot 1]"
            if shot.ordinal == 1
            else f"[Shot {shot.ordinal}] At {_format_local_shot_time(shot.start_milliseconds)},"
        )
        shot_parts.append(f"{prefix} {prose}")
    description = " ".join((*shot_parts, *hard_constraints))

    by_heading: dict[str, str] = {}
    for field in semantic_slice.global_fields:
        if field.heading == "alignment_instruction":
            # Fail closed rather than drop a field the census reports as carried: see
            # `_production_alignment` for why this sentence is never source content.
            raise PromptRenderingError(
                "Production alignment is derived per clip, never carried from a source"
            )
        existing = by_heading.get(field.heading)
        if existing is not None and existing != field.text:
            raise PromptRenderingError(
                f"Production semantic slice has conflicting {field.heading} fields"
            )
        by_heading[field.heading] = field.text
    if section_heading in by_heading:
        raise PromptRenderingError("Production semantic slice cannot replace its description")
    by_heading[section_heading] = description
    definitions: list[str] = []
    labels = {item.asset_id: item.backend_label for item in semantic_slice.reference_definitions}
    cited_assets: set[str] = set()
    for subject in semantic_slice.subject_definitions:
        cited_assets.update(subject.source_asset_ids)
        source = (
            " from " + ", ".join(labels[value] for value in subject.source_asset_ids)
            if subject.source_asset_ids
            else ""
        )
        definitions.append(
            # IMPORTANT: Base does not admit <Subject> labels. Production must preserve the
            # authored identity in prose; Full-Reference alone owns these ordinal tokens.
            (
                f"<Subject {subject.connection_order}> is "
                if section_heading == "detailed_description"
                else ""
            )
            + subject.label
            + (f", {subject.description}" if subject.description else "")
            + source
            + "."
        )
    for reference in semantic_slice.reference_definitions:
        if reference.asset_id in cited_assets:
            continue
        description = _ROLE_PROSE.get(reference.role) or _GENERIC_REFERENCE_PROSE[reference.kind]
        definitions.append(f"{reference.backend_label} is {description}.")
    if definitions:
        rendered_definitions = " ".join(definitions)
        retained = by_heading.get("subject_definitions")
        if retained is not None and retained != rendered_definitions:
            raise PromptRenderingError(
                "Production typed and canonical subject definitions disagree"
            )
        by_heading["subject_definitions"] = rendered_definitions
    canonical_order = (
        "subject_definitions",
        "summary",
        "retention_analysis",
        "integrated_multimodal_description",
        "detailed_description",
        "overall_soundscape",
        "non_diegetic_music",
    )
    sections = tuple(
        PromptSection(
            section_id=f"production_{semantic_slice.segment_id}_{index}",
            order=index,
            heading=heading,
            body=by_heading[heading],
        )
        for index, heading in enumerate(
            (heading for heading in canonical_order if heading in by_heading), start=1
        )
    )
    if not sections:
        raise PromptRenderingError("Production rendering produced no canonical sections")
    # CRITICAL: a local clip has its own ordered asset registry. A last-frame asset that was
    # global Picture 2 becomes local Picture 1; substitute exact tokens once, never substring
    # replacements that cascade or alter protected dialogue/visible text.
    local_counts = {kind: 0 for kind in MediaKind}
    names = {MediaKind.IMAGE: "Picture", MediaKind.VIDEO: "Video", MediaKind.AUDIO: "Audio"}
    local_labels: dict[str, str] = {}
    for reference in semantic_slice.reference_definitions:
        local_counts[reference.kind] += 1
        local_labels[reference.backend_label] = (
            f"<{names[reference.kind]} {local_counts[reference.kind]}>"
        )
    if local_labels:
        token_pattern = re.compile("|".join(re.escape(value) for value in local_labels))
        sections = tuple(
            replace(
                section,
                body=token_pattern.sub(lambda match: local_labels[match.group()], section.body),
            )
            for section in sections
        )
    # The preamble is written with the clip's own labels already, so it takes no part in the
    # substitution above: a second pass would renumber a local label as if it were a source one.
    preamble = _production_alignment(
        semantic_slice, local_labels, len(shots), clip_duration_seconds
    )
    text = render_prompt_sections(sections, preamble=preamble)
    for shot in shots:
        for exact in (*shot.exact_dialogue, *shot.visible_text):
            if exact not in text:
                raise PromptRenderingError("Production rendering lost exact text")
    return ProductionSegmentPromptV1(text=text, sections=sections)


__all__ = [
    "NO_SUBJECT_DEFINITIONS_TEXT",
    "ProductionSegmentPromptV1",
    "UNSPECIFIED_SOUNDSCAPE_TEXT",
    "render_base_prompt",
    "render_full_reference_prompt",
    "render_production_segment_prompt",
    "render_prompt_sections",
]
