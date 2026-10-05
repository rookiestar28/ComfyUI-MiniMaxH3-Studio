"""Deterministic speaker ownership and exact dialogue grammar without inference."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from .constraints import DialogueDelivery, DialogueSpeaker, ExactTextConstraint, ExactTextKind
from .contracts import ValidationDiagnostic, ValidationSeverity
from .dialogue_language import RESERVED_NON_LANGUAGE_TAGS, derive_dialogue_language
from .errors import PromptRenderingError

if TYPE_CHECKING:
    from .context_reporting import ContextPlan


def speaker_entries(value: ExactTextConstraint) -> tuple[tuple[str, DialogueSpeaker | None], ...]:
    if value.speakers:
        return tuple((speaker.key, speaker) for speaker in value.speakers)
    # IMPORTANT: declared identifiers cannot start with @. Unbound lines must not alias an
    # explicit speaker key or each other, which would silently invent a shared identity.
    return (("@anonymous:" + value.constraint_id, None),)


def ordered_dialogue_lines(plan: ContextPlan) -> tuple[ExactTextConstraint, ...]:
    values = tuple(
        value
        for value in plan.hard_constraints.exact_texts
        if value.kind in (ExactTextKind.DIALOGUE, ExactTextKind.LYRICS)
    )
    anchored = tuple(
        value
        for segment in sorted(plan.intent_graph.segments, key=lambda row: row.start.seconds)
        for value in values
        if value.segment_id == segment.segment_id
    )
    return anchored + tuple(value for value in values if value.segment_id is None)


def assign_speaker_ids(plan: ContextPlan) -> Mapping[str, int]:
    """Assign ordinals only to vocalizing keys, in actual render order."""
    ordinals: dict[str, int] = {}
    for value in ordered_dialogue_lines(plan):
        for key, _speaker in speaker_entries(value):
            if key not in ordinals:
                ordinals[key] = len(ordinals) + 1
    return MappingProxyType(ordinals)


def validate_dialogue_bindings(plan: ContextPlan) -> tuple[ValidationDiagnostic, ...]:
    subjects = {value.subject_id for value in plan.intent_graph.subjects}
    segments = {value.segment_id for value in plan.intent_graph.segments}
    diagnostics: list[ValidationDiagnostic] = []
    for value in plan.hard_constraints.exact_texts:
        if value.segment_id is not None and value.segment_id not in segments:
            diagnostics.append(
                ValidationDiagnostic(
                    ValidationSeverity.ERROR,
                    "unknown_dialogue_segment",
                    "dialogue anchor does not belong to the plan graph",
                    value.constraint_id,
                )
            )
        for speaker in value.speakers:
            if speaker.subject_id is not None and speaker.subject_id not in subjects:
                diagnostics.append(
                    ValidationDiagnostic(
                        ValidationSeverity.ERROR,
                        "unknown_speaker_subject",
                        "dialogue speaker subject does not belong to the plan graph",
                        value.constraint_id,
                    )
                )
    return tuple(diagnostics)


def exact_dialogue_markup(value: ExactTextConstraint) -> str:
    language = value.language or derive_dialogue_language(value.text)
    # IMPORTANT: unknown language stays untagged. A placeholder becomes a model language tag
    # and silently changes exact dialogue conditioning; never restore an "unspecified" fallback.
    if language is not None and language.casefold() in RESERVED_NON_LANGUAGE_TAGS:
        raise PromptRenderingError("exact dialogue cannot render a reserved language tag")
    tag = "" if language is None else f"[{language}] "
    return f"<d>{tag}{value.text}</d>"


def render_dialogue_line(plan: ContextPlan, value: ExactTextConstraint) -> str:
    ids = assign_speaker_ids(plan)
    entries = sorted(speaker_entries(value), key=lambda entry: ids.get(entry[0], 0))
    subjects = {subject.subject_id: subject for subject in plan.intent_graph.subjects}
    identities: list[str] = []
    possessives: list[str] = []
    for key, speaker in entries:
        if key not in ids:
            raise PromptRenderingError("unknown dialogue anchor prevents speaker assignment")
        subject = None if speaker is None else subjects.get(speaker.subject_id or "")
        if speaker is not None and speaker.subject_id is not None and subject is None:
            raise PromptRenderingError("unknown speaker subject prevents dialogue rendering")
        identities.append(
            speaker.identity
            if speaker is not None and speaker.identity is not None
            else subject.label
            if subject is not None
            else "A speaker"
        )
        possessives.append(subject.label + "'s" if subject is not None else "the speaker's")
    group = "(" + ",".join(f"S{ids[key]}" for key, _speaker in entries) + ")"
    verb = "sings" if value.kind is ExactTextKind.LYRICS else "says"
    delivery = " in an off-screen voiceover" if value.delivery is DialogueDelivery.VOICEOVER else ""
    result = f"{' and '.join(identities)} {group} {verb}{delivery}: {exact_dialogue_markup(value)}"
    if value.delivery is DialogueDelivery.VOICEOVER:
        result += f" while {' and '.join(possessives)} lips remain completely closed."
    return result
