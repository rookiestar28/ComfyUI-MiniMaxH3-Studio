"""Official prompt fidelity audit rules over a rendered prompt document.

`linting.py` audits structure: label syntax, residue, security, duration and graph coherence. This
module audits the prose against the rules the official MiniMax guide states and this repository
already renders to -- shot cut timestamps, camera and cut vocabulary, speaker identity, description
length, this repository's own internal vocabulary, and the user's explicit reference-role
restrictions.

Two properties are deliberate and are what the rest of the chain depends on.

Nothing here corrects anything. Every rule emits a diagnostic and returns; a prompt is never
rewritten, and no plan, report or constraint is mutated.

Every diagnostic is display-neutral. It carries a stable `diagnostic_id` and typed `parameters` for
each variable part, so a consumer can compose a complete sentence without parsing `message`. That is
what makes this rule family localisable at all: today a diagnostic reaches the sidebar as
backend-produced English rendered verbatim, and shipping a whole new family in that shape would
multiply an existing defect by the size of the family. `message` remains the log surface and the
honest English fallback, never the only surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .constraints import ExactTextKind
from .context_reporting import ContextPlan, PromptDocument
from .contracts import AssetRole, EvidenceLevel, TaskMode, ValidationSeverity
from .dialogue_language import (
    OFFICIAL_STABLE_DIALOGUE_LANGUAGES,
    derive_dialogue_language,
    normalize_dialogue_language,
)
from .dialogue_speakers import speaker_entries
from .errors import ContractValidationError, PromptLintError
from .intent_graph import (
    AudioScope,
    IntentGraph,
    RetentionScope,
    SegmentDevelopment,
    SoundscapeDisposition,
    TimelineSegment,
)

PROMPT_FIDELITY_SCHEMA = "h3.context.prompt_fidelity.v2"

_MAX_TEXT = 65_536
_MAX_DIAGNOSTICS = 256
_MAX_PARAMETER_TEXT = 160

# Rule 4 is a graded quality signal and is not in the official guide, so it never fails a report and
# never claims `official` evidence. The bounds are stated here rather than buried in the rule so the
# band a consumer is shown can be traced to a number.
SEVERELY_SHORT_CHARACTERS = 200
BELOW_TARGET_CHARACTERS = 400
ABOVE_TARGET_CHARACTERS = 4_000

# Guide 4.2. A later shot begins with a strictly increasing cut time inside the video duration, and
# the first shot carries no timestamp at all.
_SHOT_PREFIX = re.compile(r"\[Shot (\d{1,3})\](?: At ([^,]{0,32}),)?")
_CUT_TIME = re.compile(r"\A(\d{2}):(\d{2}\.\d{3})\Z")


# Guide 4.3, motion type column. The guide names each move in the infinitive but writes it as a
# natural English action inside the shot -- "the camera pushes in", "the camera pans right" -- so a
# literal substring match would miss every real sentence. Each entry pairs the canonical term a
# diagnostic reports with the inflections that actually appear in prose.
def _motion(term: str, verb: str, tail: str) -> tuple[str, re.Pattern[str]]:
    return term, re.compile(rf"\b{verb}(?:s|es|ing)?\s+{tail}\b")


_CAMERA_MOTION_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    _motion("zoom in", "zoom", "in"),
    _motion("zoom out", "zoom", "out"),
    _motion("push in", "push", "in"),
    _motion("pull out", "pull", "out"),
    _motion("pan left", "pan", "left"),
    _motion("pan right", "pan", "right"),
    _motion("truck left", "truck", "left"),
    _motion("truck right", "truck", "right"),
    _motion("tilt up", "tilt", "up"),
    _motion("tilt down", "tilt", "down"),
    _motion("pedestal up", "pedestal", "up"),
    _motion("pedestal down", "pedestal", "down"),
    ("arc shot", re.compile(r"\barc shot\b")),
    ("tracking shot", re.compile(r"\btracking shot\b")),
    _motion("shake slightly", "shake", "slightly"),
    _motion("shake strongly", "shake", "strongly"),
    _motion("roll clockwise", "roll", "clockwise"),
    _motion("roll counterclockwise", "roll", "counterclockwise"),
)

# Guide 4.2, ordinary cuts plus the transitions allowed only on explicit request.
_CUT_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (term, re.compile(re.escape(term)))
    for term in (
        "the camera cuts to",
        "the shot cuts to",
        "the shot transitions to",
        "the shot changes to",
        "the shot switches to",
        "cross-dissolve",
    )
)

# Guide 4.4. A speaker keeps a stable ID such as `(S1)`, or a compound `(S1,S2)` when several
# already-numbered speakers vocalize together.
_SPEAKER_ID = re.compile(r"\(S[1-9][0-9]{0,2}(?:,\s*S[1-9][0-9]{0,2})*\)")
_DIALOGUE_BLOCK = re.compile(r"<d>")
_DIALOGUE_CONTENT = re.compile(r"<d>(.*?)</d>", re.DOTALL)
_LANGUAGE_TAG = re.compile(r"\A\[([^\]\r\n]{0,160})\]")

# This repository's own internal representation vocabulary. M21-02 introduces a producer of it; the
# rule exists first so that producer cannot ship without a check already watching for leakage.
_INTERNAL_VOCABULARY = (
    "contact sheet",
    "contact-sheet",
    "panel index",
    "grid position",
    "tile index",
    "sheet position",
    "slot index",
)

# Traits a motion- or camera-only reference must never be credited with. These are the source
# properties the reference-role model excludes by construction.
_EXCLUDED_SOURCE_TRAITS = (
    "wardrobe",
    "clothing",
    "outfit",
    "costume",
    "hairstyle",
    "location",
    "setting",
    "lighting",
    "colour palette",
    "color palette",
)
_MOTION_ONLY_ROLES = (AssetRole.MOTION_REFERENCE, AssetRole.CAMERA_REFERENCE)

# Phrases by which a user forbids a technique outright in their own authored intent.
_NEGATED_CUTS = ("no cuts", "without cuts", "no cut", "single shot", "one continuous shot")
_NEGATED_MOTION = (
    "static camera",
    "no camera movement",
    "no camera motion",
    "camera does not move",
    "locked-off camera",
)

# Roles that carry their own meaning in the prompt without being bound to a graph element: a
# supplied first or last frame is already the anchor the mode instruction names.
_SELF_EVIDENT_ASSET_ROLES = (AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME, AssetRole.PRIMARY)

_DESCRIPTION_FIELDS = ("integrated_multimodal_description:", "detailed_description:")
_ENGLISH_WORD = re.compile(r"\b[A-Za-z]+(?:['’-][A-Za-z]+)?\b")
_CJK_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_OFFICIAL_DESCRIPTION_MIN_WORDS = 350
_OFFICIAL_DESCRIPTION_MAX_WORDS = 500
_MIN_ENGLISH_PROSE_WORDS = 20


class PromptFidelityDiagnosticId(str, Enum):
    """The closed set of fidelity diagnostic identities.

    `M21-03` asserts three-locale coverage against this enum rather than against a hand-maintained
    list, so a rule that emits an identity absent from here is a contract break, not a typo.
    """

    SHOT_TIMESTAMP_MALFORMED = "fidelity.shot_timestamp.malformed"
    SHOT_TIMESTAMP_NOT_INCREASING = "fidelity.shot_timestamp.not_increasing"
    SHOT_TIMESTAMP_OUT_OF_DURATION = "fidelity.shot_timestamp.out_of_duration"
    SHOT_TIMESTAMP_ON_FIRST_SHOT = "fidelity.shot_timestamp.on_first_shot"
    CAMERA_UNREQUESTED_MOTION = "fidelity.camera.unrequested_motion"
    CAMERA_UNREQUESTED_CUT = "fidelity.camera.unrequested_cut"
    DIALOGUE_SPEAKER_IDENTITY_MISSING = "fidelity.dialogue.speaker_identity_missing"
    DIALOGUE_SPEAKER_UNBOUND = "fidelity.dialogue.speaker_unbound"
    DIALOGUE_LANGUAGE_MISSING = "fidelity.dialogue.language_missing"
    DIALOGUE_LANGUAGE_TAG_INVALID = "fidelity.dialogue.language_tag_invalid"
    DIALOGUE_LANGUAGE_UNSTABLE = "fidelity.dialogue.language_unstable"
    DIALOGUE_LANGUAGE_DERIVED = "fidelity.dialogue.language_derived"
    DESCRIPTION_LENGTH_BAND = "fidelity.description.length_band"
    INTERNAL_VOCABULARY_PRESENT = "fidelity.internal_vocabulary.present"
    CONSTRAINT_EXCLUDED_SOURCE_TRAIT = "fidelity.constraint.excluded_source_trait"
    CONSTRAINT_NEGATED_TECHNIQUE_USED = "fidelity.constraint.negated_technique_used"
    # M24-05. These are guide-readiness findings: the prompt is a valid, runnable draft, but the
    # typed plan does not own enough for an official conformance claim. They are warnings on
    # purpose -- an error would refuse a canvas for how the user chose to author it.
    SOUNDSCAPE_UNSPECIFIED = "fidelity.soundscape.unspecified"
    AUDIO_OWNERSHIP_UNRESOLVED = "fidelity.audio.ownership_unresolved"
    AUDIO_UNLINKED = "fidelity.audio.unlinked"
    KEYFRAME_ANCHOR_MISSING = "fidelity.keyframe.anchor_missing"
    KEYFRAME_DEVELOPMENT_MISSING = "fidelity.keyframe.development_missing"
    KEYFRAME_PATH_UNOWNED = "fidelity.keyframe.path_unowned"
    KEYFRAME_STATIC_HOLD_CONTRADICTED = "fidelity.keyframe.static_hold_contradicted"
    DESCRIPTION_SEMANTIC_EMPTY = "fidelity.description.semantic_empty"
    REFERENCE_UNUSED = "fidelity.reference.unused"
    RETENTION_SCOPE_UNSPECIFIED = "fidelity.retention.scope_unspecified"
    AUDIO_FINAL_TRACK_CONTRADICTED = "fidelity.audio.final_track_contradicted"


PROMPT_FIDELITY_DIAGNOSTIC_IDS: frozenset[str] = frozenset(
    item.value for item in PromptFidelityDiagnosticId
)

# The M24-05 identities that describe the typed plan rather than the rendered prose. They are the
# reasons guide readiness is withheld, and they are deliberately excluded from `prose_blocking`.
PLAN_READINESS_IDS: frozenset[PromptFidelityDiagnosticId] = frozenset(
    {
        PromptFidelityDiagnosticId.SOUNDSCAPE_UNSPECIFIED,
        PromptFidelityDiagnosticId.AUDIO_OWNERSHIP_UNRESOLVED,
        PromptFidelityDiagnosticId.AUDIO_UNLINKED,
        PromptFidelityDiagnosticId.KEYFRAME_ANCHOR_MISSING,
        PromptFidelityDiagnosticId.KEYFRAME_DEVELOPMENT_MISSING,
        PromptFidelityDiagnosticId.KEYFRAME_PATH_UNOWNED,
        PromptFidelityDiagnosticId.KEYFRAME_STATIC_HOLD_CONTRADICTED,
        PromptFidelityDiagnosticId.DESCRIPTION_SEMANTIC_EMPTY,
        PromptFidelityDiagnosticId.REFERENCE_UNUSED,
        PromptFidelityDiagnosticId.RETENTION_SCOPE_UNSPECIFIED,
        PromptFidelityDiagnosticId.AUDIO_FINAL_TRACK_CONTRADICTED,
        PromptFidelityDiagnosticId.DIALOGUE_LANGUAGE_MISSING,
        PromptFidelityDiagnosticId.DIALOGUE_SPEAKER_UNBOUND,
    }
)


class DescriptionLengthBand(str, Enum):
    """A graded length signal. It is never a pass/fail and never blocks a report."""

    SEVERELY_SHORT = "severely_short"
    BELOW_TARGET = "below_target"
    WITHIN_TARGET = "within_target"
    ABOVE_TARGET = "above_target"


def _bounded_parameter(value: object, field: str) -> str | int:
    if isinstance(value, bool):
        raise PromptLintError(f"{field} must be bounded text or an integer")
    if isinstance(value, int):
        if not -1_000_000 <= value <= 1_000_000:
            raise PromptLintError(f"{field} must be a bounded integer")
        return value
    if not isinstance(value, str) or not value or len(value) > _MAX_PARAMETER_TEXT:
        raise PromptLintError(f"{field} must be bounded text or an integer")
    return value


@dataclass(frozen=True, slots=True)
class PromptFidelityDiagnostic:
    """One display-neutral fidelity finding.

    `parameters` carries every variable part of the sentence separately from `message`. A consumer
    that reads only `diagnostic_id` and `parameters` can render the finding in any locale; a
    consumer that reads `message` gets the same finding in English.
    """

    diagnostic_id: PromptFidelityDiagnosticId
    severity: ValidationSeverity
    evidence_level: EvidenceLevel
    location: str
    message: str
    remediation: str
    parameters: tuple[tuple[str, str | int], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.diagnostic_id, PromptFidelityDiagnosticId):
            raise PromptLintError("fidelity diagnostic_id must be a known identity")
        if not isinstance(self.severity, ValidationSeverity):
            raise PromptLintError("fidelity severity must be a ValidationSeverity")
        if not isinstance(self.evidence_level, EvidenceLevel):
            raise PromptLintError("fidelity evidence_level must be an EvidenceLevel")
        for field, value in (
            ("location", self.location),
            ("message", self.message),
            ("remediation", self.remediation),
        ):
            if not isinstance(value, str) or not value or len(value) > 512:
                raise PromptLintError(f"fidelity {field} must be bounded text")
        if not isinstance(self.parameters, tuple) or len(self.parameters) > 8:
            raise PromptLintError("fidelity parameters must be a bounded tuple")
        seen: set[str] = set()
        for entry in self.parameters:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise PromptLintError("fidelity parameter must be a name/value pair")
            name = entry[0]
            if not isinstance(name, str) or not name or name in seen:
                raise PromptLintError("fidelity parameter name must be unique bounded text")
            seen.add(name)
            _bounded_parameter(entry[1], f"fidelity parameter {name}")

    @property
    def parameter_map(self) -> dict[str, str | int]:
        return dict(self.parameters)

    def to_wire(self) -> dict[str, object]:
        return {
            "diagnostic_id": self.diagnostic_id.value,
            "severity": self.severity.value,
            "evidence_level": self.evidence_level.value,
            "location": self.location,
            "message": self.message,
            "remediation": self.remediation,
            "parameters": {name: value for name, value in self.parameters},
        }


@dataclass(frozen=True, slots=True)
class PromptFidelityAuditResult:
    """Immutable audit result. It never contains a corrected prompt."""

    diagnostics: tuple[PromptFidelityDiagnostic, ...]
    length_band: DescriptionLengthBand
    description_characters: int

    def __post_init__(self) -> None:
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > _MAX_DIAGNOSTICS:
            raise PromptLintError("fidelity diagnostics exceed the bounded result limit")
        if not all(isinstance(value, PromptFidelityDiagnostic) for value in self.diagnostics):
            raise PromptLintError("fidelity diagnostics contain an invalid value")
        if not isinstance(self.length_band, DescriptionLengthBand):
            raise PromptLintError("fidelity length_band must be a DescriptionLengthBand")
        if not isinstance(self.description_characters, int) or self.description_characters < 0:
            raise PromptLintError("fidelity description_characters must be a count")

    @property
    def blocking(self) -> tuple[PromptFidelityDiagnostic, ...]:
        """The findings that may change a validation verdict.

        An `info` finding is advisory: the length band is a quality signal, and folding it into the
        validation stream would add a diagnostic to every report without changing what any consumer
        may do with it.
        """

        return tuple(
            item for item in self.diagnostics if item.severity is not ValidationSeverity.INFO
        )

    @property
    def readiness_findings(self) -> tuple[PromptFidelityDiagnostic, ...]:
        """The M24-05 findings that describe the typed plan rather than the rendered prose."""

        return tuple(item for item in self.diagnostics if item.diagnostic_id in PLAN_READINESS_IDS)

    @property
    def prose_blocking(self) -> tuple[PromptFidelityDiagnostic, ...]:
        """The findings a rewrite of the prompt text could actually resolve.

        GUARD: a prose-repair loop must use this, never `blocking`. A guide-readiness finding is a
        statement about the typed plan -- an unspecified soundscape, an unowned keyframe anchor --
        and no rewording of the prompt can satisfy it. Handing one to a model as repair work costs
        a second provider call that cannot succeed, and then reports the unchanged finding as the
        model's failure. Readiness still reaches the user through `blocking` and the validation
        stream, which is where a fact about the plan belongs.
        """

        return tuple(item for item in self.blocking if item.diagnostic_id not in PLAN_READINESS_IDS)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PROMPT_FIDELITY_SCHEMA,
            "length_band": self.length_band.value,
            "description_characters": self.description_characters,
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


def _add(
    diagnostics: list[PromptFidelityDiagnostic],
    diagnostic_id: PromptFidelityDiagnosticId,
    severity: ValidationSeverity,
    evidence_level: EvidenceLevel,
    location: str,
    message: str,
    remediation: str,
    parameters: tuple[tuple[str, str | int], ...] = (),
) -> None:
    if len(diagnostics) >= _MAX_DIAGNOSTICS:
        return
    diagnostics.append(
        PromptFidelityDiagnostic(
            diagnostic_id,
            severity,
            evidence_level,
            location,
            message,
            remediation,
            parameters,
        )
    )


def _description_section(text: str) -> str:
    """The Base or Full detailed-description body, or the whole document when absent."""

    matches = tuple(
        (text.find(field), field) for field in _DESCRIPTION_FIELDS if text.find(field) >= 0
    )
    if not matches:
        return text
    start, field = min(matches, key=lambda item: item[0])
    body = text[start + len(field) :]
    for terminator in ("\n\noverall_soundscape:", "\n\nnon_diegetic_music:"):
        end = body.find(terminator)
        if end >= 0:
            body = body[:end]
    return body.strip()


def _cut_seconds(value: str) -> Decimal | None:
    match = _CUT_TIME.match(value.strip())
    if match is None:
        return None
    return Decimal(match.group(1)) * 60 + Decimal(match.group(2))


def _check_shot_timestamps(
    plan: ContextPlan, text: str, diagnostics: list[PromptFidelityDiagnostic]
) -> None:
    """Guide 4.2: no timestamp on the first shot, then strictly increasing in-duration cut times."""

    duration = plan.intent_graph.effective_duration.seconds
    previous: Decimal | None = None
    for match in _SHOT_PREFIX.finditer(text):
        shot = int(match.group(1))
        raw = match.group(2)
        if shot == 1:
            if raw is not None:
                _add(
                    diagnostics,
                    PromptFidelityDiagnosticId.SHOT_TIMESTAMP_ON_FIRST_SHOT,
                    ValidationSeverity.ERROR,
                    EvidenceLevel.OFFICIAL,
                    "prompt_document.text",
                    "the first shot carries a cut timestamp, which the official guide forbids",
                    "Remove the timestamp from [Shot 1]; only later shots begin with a cut time.",
                    (("shot_index", shot), ("cut_time", raw.strip())),
                )
            continue
        if raw is None:
            continue
        seconds = _cut_seconds(raw)
        if seconds is None:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.SHOT_TIMESTAMP_MALFORMED,
                ValidationSeverity.ERROR,
                EvidenceLevel.OFFICIAL,
                "prompt_document.text",
                "a shot cut time is not in the official MM:SS.mmm form",
                "Render the cut time as MM:SS.mmm, for example 00:03.500.",
                (("shot_index", shot), ("cut_time", raw.strip()), ("expected_format", "MM:SS.mmm")),
            )
            continue
        if previous is not None and seconds <= previous:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.SHOT_TIMESTAMP_NOT_INCREASING,
                ValidationSeverity.ERROR,
                EvidenceLevel.OFFICIAL,
                "prompt_document.text",
                "a shot cut time does not strictly increase over the previous shot",
                "Order the shots by cut time and make each one strictly later than the last.",
                (
                    ("shot_index", shot),
                    ("cut_time", raw.strip()),
                    ("previous_cut_seconds", str(previous)),
                ),
            )
        if seconds >= duration:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.SHOT_TIMESTAMP_OUT_OF_DURATION,
                ValidationSeverity.ERROR,
                EvidenceLevel.OFFICIAL,
                "prompt_document.text",
                "a shot cut time falls outside the declared video duration",
                "Move the cut inside the clip or extend the authored duration.",
                (
                    ("shot_index", shot),
                    ("cut_time", raw.strip()),
                    ("duration_seconds", str(duration)),
                ),
            )
        previous = seconds


def _requested_terms(plan: ContextPlan) -> str:
    """The text a user's own request is read from, lowercased once."""

    return plan.request.user_intent.casefold()


def _check_camera_and_cuts(
    plan: ContextPlan, text: str, diagnostics: list[PromptFidelityDiagnostic]
) -> None:
    """Guide 4.2 and 4.3: neither a move nor a cut may be invented on the user's behalf."""

    lowered = text.casefold()
    intent = _requested_terms(plan)
    # A camera intent in the typed plan is an explicit request, and so is the user naming the move
    # in their own words. Detection is lexical over prose, so this stays a warning: a false error
    # would reject a legitimate prompt.
    motion_requested = bool(plan.intent_graph.cameras) or any(
        pattern.search(intent) for _, pattern in _CAMERA_MOTION_TERMS
    )
    if not motion_requested:
        for term, pattern in _CAMERA_MOTION_TERMS:
            if pattern.search(lowered):
                _add(
                    diagnostics,
                    PromptFidelityDiagnosticId.CAMERA_UNREQUESTED_MOTION,
                    ValidationSeverity.WARNING,
                    EvidenceLevel.OFFICIAL,
                    "prompt_document.text",
                    "the prompt adds camera motion the request did not ask for",
                    "Remove the invented camera move or record it as an authored camera intent.",
                    (("term", term),),
                )
                break
    # More than one authored segment is a request for a cut; so is the user naming one.
    cut_requested = len(plan.intent_graph.segments) > 1 or any(
        term in intent for term in ("cut", "transition", "dissolve")
    )
    if not cut_requested:
        for term, pattern in _CUT_TERMS:
            if pattern.search(lowered):
                _add(
                    diagnostics,
                    PromptFidelityDiagnosticId.CAMERA_UNREQUESTED_CUT,
                    ValidationSeverity.WARNING,
                    EvidenceLevel.OFFICIAL,
                    "prompt_document.text",
                    "the prompt adds a shot cut the request did not ask for",
                    "Remove the invented cut or author the additional shot on the timeline.",
                    (("term", term),),
                )
                break


def _check_speaker_identity(text: str, diagnostics: list[PromptFidelityDiagnostic]) -> None:
    """Guide 4.4: a speaker ID such as `(S1)` says who is speaking, before the `<d>` block."""

    for index, match in enumerate(_DIALOGUE_BLOCK.finditer(text), start=1):
        preceding = text[: match.start()]
        line_start = preceding.rfind("\n") + 1
        if _SPEAKER_ID.search(preceding[line_start:]) is None:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.DIALOGUE_SPEAKER_IDENTITY_MISSING,
                # IMPORTANT: this also audits user-authored prose. Keep WARNING: ERROR would
                # refuse the user's canvas; repository renders prove ID compliance in tests.
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                "prompt_document.text",
                "a dialogue block is not preceded by a stable speaker identity",
                "Give the speaker a stable ID such as (S1) before the <d> block.",
                (("block_index", index),),
            )


def _check_dialogue_languages(
    plan: ContextPlan, text: str, diagnostics: list[PromptFidelityDiagnostic]
) -> None:
    """Report language readiness and prose tags without changing the author's words."""

    typed_missing: set[str] = set()
    for value in plan.hard_constraints.exact_texts:
        if value.kind in (ExactTextKind.DIALOGUE, ExactTextKind.LYRICS):
            if any(
                speaker is None or (speaker.subject_id is None and speaker.identity is None)
                for _key, speaker in speaker_entries(value)
            ):
                _add(
                    diagnostics,
                    PromptFidelityDiagnosticId.DIALOGUE_SPEAKER_UNBOUND,
                    ValidationSeverity.WARNING,
                    EvidenceLevel.OFFICIAL,
                    "plan.hard_constraints",
                    "dialogue speaker has no authored identity",
                    "Bind the speaker to a subject or declare an identity phrase.",
                    (("line_id", value.constraint_id),),
                )
        if value.kind not in (ExactTextKind.DIALOGUE, ExactTextKind.LYRICS) or value.language:
            continue
        derived = derive_dialogue_language(value.text)
        if derived is None:
            typed_missing.add(value.text)
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.DIALOGUE_LANGUAGE_MISSING,
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                "plan.hard_constraints",
                "exact dialogue has no declared or unambiguously derived language",
                "Declare the dialogue language to establish guide readiness.",
                (("line_id", value.constraint_id),),
            )
        else:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.DIALOGUE_LANGUAGE_DERIVED,
                ValidationSeverity.INFO,
                EvidenceLevel.OFFICIAL,
                "plan.hard_constraints",
                "dialogue language was derived from an unambiguous script",
                "Declare the language explicitly if a different language is intended.",
                (("line_id", value.constraint_id), ("language", derived)),
            )

    for index, block in enumerate(_DIALOGUE_CONTENT.finditer(text), start=1):
        content = block.group(1)
        match = _LANGUAGE_TAG.match(content.lstrip())
        if match is None:
            if content in typed_missing:
                continue
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.DIALOGUE_LANGUAGE_MISSING,
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                "prompt_document.text",
                "a dialogue block has no leading language tag",
                "Declare a language for this dialogue line.",
                (("line_id", index),),
            )
            continue
        tag = match.group(1)
        try:
            normalize_dialogue_language(tag)
        except ContractValidationError:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.DIALOGUE_LANGUAGE_TAG_INVALID,
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                "prompt_document.text",
                "a dialogue language tag is not a usable language name",
                "Replace the placeholder or unsafe tag with a language name.",
                (("tag", tag or "(empty)"), ("line_id", index)),
            )
            continue
        if tag.casefold() not in {name.casefold() for name in OFFICIAL_STABLE_DIALOGUE_LANGUAGES}:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.DIALOGUE_LANGUAGE_UNSTABLE,
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                "prompt_document.text",
                "the dialogue language is outside the eleven stable languages",
                "This language may be supported to a varying degree; review the result.",
                (("tag", tag), ("line_id", index)),
            )


def _length_band(characters: int) -> DescriptionLengthBand:
    if characters < SEVERELY_SHORT_CHARACTERS:
        return DescriptionLengthBand.SEVERELY_SHORT
    if characters < BELOW_TARGET_CHARACTERS:
        return DescriptionLengthBand.BELOW_TARGET
    if characters > ABOVE_TARGET_CHARACTERS:
        return DescriptionLengthBand.ABOVE_TARGET
    return DescriptionLengthBand.WITHIN_TARGET


def _description_advisory(
    plan: ContextPlan, description: str, character_band: DescriptionLengthBand
) -> tuple[DescriptionLengthBand, int, EvidenceLevel] | None:
    """Return a low-cost length advisory only where the guide's range applies.

    The pinned guide gives 350--500 English words for ordinary generation. Editing scales with
    source complexity, while CJK and exact dialogue need language-aware accounting this pure rule
    does not own. Short synthetic/legacy fragments retain the former character advisory so the
    result contract remains readable without pretending that one long token is English prose.
    """

    if any(
        asset.role is AssetRole.EDITING_SOURCE for asset in plan.request.reference_registry.assets
    ):
        return None
    if any(item.kind is ExactTextKind.DIALOGUE for item in plan.hard_constraints.exact_texts):
        return None
    if _CJK_CHARACTER.search(description) is not None:
        return None
    words = len(_ENGLISH_WORD.findall(description))
    if words >= _MIN_ENGLISH_PROSE_WORDS:
        if words < _OFFICIAL_DESCRIPTION_MIN_WORDS:
            band = DescriptionLengthBand.BELOW_TARGET
        elif words <= _OFFICIAL_DESCRIPTION_MAX_WORDS:
            band = DescriptionLengthBand.WITHIN_TARGET
        else:
            band = DescriptionLengthBand.ABOVE_TARGET
        return band, words, EvidenceLevel.OFFICIAL
    return character_band, words, EvidenceLevel.COMMUNITY_RECOMMENDED


def _check_internal_vocabulary(text: str, diagnostics: list[PromptFidelityDiagnostic]) -> None:
    """No word this repository uses to describe its own internals may reach a final prompt."""

    lowered = text.casefold()
    for term in _INTERNAL_VOCABULARY:
        if term in lowered:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.INTERNAL_VOCABULARY_PRESENT,
                ValidationSeverity.ERROR,
                EvidenceLevel.FRAMEWORK_REFERENCE,
                "prompt_document.text",
                "internal representation vocabulary reached the final prompt",
                "Describe the subject itself; internal representation words never ship.",
                (("term", term),),
            )


def _check_explicit_constraints(
    plan: ContextPlan, text: str, diagnostics: list[PromptFidelityDiagnostic]
) -> None:
    """The user's own restrictions, re-checked against what was actually rendered."""

    lowered = text.casefold()
    registry = plan.request.reference_registry
    labels = {label.asset_id: label.label for label in registry.labels}
    for asset in registry.assets:
        if asset.role not in _MOTION_ONLY_ROLES:
            continue
        label = labels.get(asset.asset_id)
        if label is None:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if label not in sentence:
                continue
            sentence_lowered = sentence.casefold()
            for trait in _EXCLUDED_SOURCE_TRAITS:
                if trait in sentence_lowered:
                    _add(
                        diagnostics,
                        PromptFidelityDiagnosticId.CONSTRAINT_EXCLUDED_SOURCE_TRAIT,
                        ValidationSeverity.WARNING,
                        EvidenceLevel.OFFICIAL,
                        "prompt_document.text",
                        "a motion-only reference is credited with an excluded source trait",
                        "Restrict the reference to motion, or change its declared role.",
                        (("label", label), ("trait", trait), ("role", asset.role.value)),
                    )
                    break

    intent = _requested_terms(plan)
    for phrases, terms, technique in (
        (_NEGATED_CUTS, _CUT_TERMS, "cut"),
        (_NEGATED_MOTION, _CAMERA_MOTION_TERMS, "camera_motion"),
    ):
        if not any(phrase in intent for phrase in phrases):
            continue
        for term, pattern in terms:
            if pattern.search(lowered):
                _add(
                    diagnostics,
                    PromptFidelityDiagnosticId.CONSTRAINT_NEGATED_TECHNIQUE_USED,
                    ValidationSeverity.WARNING,
                    EvidenceLevel.OFFICIAL,
                    "prompt_document.text",
                    "the prompt uses a technique the request explicitly ruled out",
                    "Honour the stated restriction or record an authorized transformation.",
                    (("technique", technique), ("term", term)),
                )
                break


def _is_semantically_empty(segment: TimelineSegment) -> bool:
    """A shot with no subject, action or scene carries no concrete timeline content."""

    return not segment.subject_ids and not segment.action_ids and segment.scene_id is None


def _asset_ids_for_role(plan: ContextPlan, role: AssetRole) -> frozenset[str]:
    return frozenset(
        asset.asset_id for asset in plan.request.reference_registry.assets if asset.role is role
    )


def _anchor_subject_ids(graph: IntentGraph, assets: frozenset[str]) -> frozenset[str]:
    """Subjects that explicitly name one of `assets` as a source.

    Ownership is a typed join, never a keyword. A frame that merely exists in the registry proves
    nothing about whether the shot was written from it.
    """

    if not assets:
        return frozenset()
    return frozenset(
        subject.subject_id
        for subject in graph.subjects
        if assets.intersection(subject.source_asset_ids)
    )


def _anchor_is_owned(graph: IntentGraph, segment: TimelineSegment, assets: frozenset[str]) -> bool:
    return bool(_anchor_subject_ids(graph, assets).intersection(segment.subject_ids))


def _check_retention_readiness(
    plan: ContextPlan, diagnostics: list[PromptFidelityDiagnostic]
) -> None:
    for relation in plan.intent_graph.retention:
        if relation.scope is RetentionScope.UNSPECIFIED:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.RETENTION_SCOPE_UNSPECIFIED,
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                f"intent_graph.retention.{relation.relation_id}",
                "the retention relation does not declare which output denotation it preserves",
                (
                    "Declare the retained subject, picture, video structure, audio layer or "
                    "complete final track."
                ),
                (("relation_id", relation.relation_id),),
            )
        if (
            relation.scope is RetentionScope.COMPLETE_FINAL_AUDIO_TRACK
            and not plan.intent_graph.complete_final_audio_track_is_consistent(
                relation.relation_id,
                source_registry=plan.request.reference_registry,
                hard_constraints=plan.hard_constraints,
            )
        ):
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.AUDIO_FINAL_TRACK_CONTRADICTED,
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                f"intent_graph.retention.{relation.relation_id}",
                "the complete-final-track claim conflicts with the declared audio contributions",
                (
                    "Keep one unchanged source carrier for the complete output, or declare layer "
                    "or event retention instead."
                ),
                (("relation_id", relation.relation_id),),
            )


def _check_guide_semantics(plan: ContextPlan, diagnostics: list[PromptFidelityDiagnostic]) -> None:
    """M24-05 guide-readiness rules over the typed plan, never over the prose.

    Every rule reads owned typed joins. None of them inspects media, infers an observation, or
    matches an English keyword, because a deterministic renderer that guessed here would produce
    exactly the confidently wrong prompt this item exists to stop.
    """

    graph = plan.intent_graph
    if graph.soundscape is SoundscapeDisposition.UNSPECIFIED:
        _add(
            diagnostics,
            PromptFidelityDiagnosticId.SOUNDSCAPE_UNSPECIFIED,
            ValidationSeverity.WARNING,
            EvidenceLevel.OFFICIAL,
            "intent_graph.soundscape",
            "the whole-video soundscape is unspecified, so no silence claim may be rendered",
            "Describe the ambient sound, or state explicitly that the video is completely silent.",
        )
    linked = {audio_id for segment in graph.segments for audio_id in segment.audio_ids}
    for audio in graph.audios:
        if audio.resolved_ownership is None:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.AUDIO_OWNERSHIP_UNRESOLVED,
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                f"intent_graph.audios.{audio.audio_id}",
                "the audio layer does not decide which official section owns this sound",
                "Declare whether it is heard in the scene or is audience-only score.",
                (("layer", audio.layer.value),),
            )
        if audio.scope is AudioScope.TIMELINE and audio.audio_id not in linked:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.AUDIO_UNLINKED,
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                f"intent_graph.audios.{audio.audio_id}",
                "the audio is not linked to any shot and is not a whole-video summary",
                "Attach it to a shot, or scope it to the whole video.",
                (("layer", audio.layer.value),),
            )
    _check_unused_references(plan, diagnostics)
    _check_retention_readiness(plan, diagnostics)
    for segment in graph.segments:
        if _is_semantically_empty(segment):
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.DESCRIPTION_SEMANTIC_EMPTY,
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                f"intent_graph.segments.{segment.segment_id}",
                "the shot declares no subject, action or scene, so it has no concrete content",
                "Describe the subjects, the scene and what happens in this shot.",
                (("segment_id", segment.segment_id),),
            )
    _check_keyframe_readiness(plan, diagnostics)


def _check_unused_references(
    plan: ContextPlan, diagnostics: list[PromptFidelityDiagnostic]
) -> None:
    """Report a reference the plan never binds, instead of dropping it from the prompt.

    GUARD: the guide gives a standalone label only to an asset whose role earns one, and cites
    every other image inside the subject it is the source of. An asset that is neither is not a
    reason to invent a role for it -- but it must not vanish silently either, because the user
    attached it on purpose. Naming it here is what keeps "not in the prompt" visible.
    """

    graph = plan.intent_graph
    bound = {asset_id for subject in graph.subjects for asset_id in subject.source_asset_ids}
    bound |= {asset_id for audio in graph.audios for asset_id in audio.source_asset_ids}
    bound |= {event.source_asset_id for event in graph.events}
    bound |= {asset_id for relation in graph.retention for asset_id in relation.source_asset_ids}
    for asset in plan.request.reference_registry.assets:
        if asset.asset_id in bound or asset.role in _SELF_EVIDENT_ASSET_ROLES:
            continue
        _add(
            diagnostics,
            PromptFidelityDiagnosticId.REFERENCE_UNUSED,
            ValidationSeverity.WARNING,
            EvidenceLevel.OFFICIAL,
            "reference_registry",
            "the reference is attached but nothing in the plan uses it",
            "Name it as the source of a subject, a sound or a retained element, or remove it.",
            (
                ("label", plan.request.reference_registry.label_for(asset.asset_id).label),
                ("role", asset.role.value),
            ),
        )


def _check_keyframe_readiness(
    plan: ContextPlan, diagnostics: list[PromptFidelityDiagnostic]
) -> None:
    """Mode-specific endpoint and path obligations from caller-owned typed declarations."""

    graph = plan.intent_graph
    mode = plan.request.task_mode
    if mode not in {TaskMode.I2VA, TaskMode.FL2VA, TaskMode.L2VA} or not graph.segments:
        return
    first_segment = graph.segments[0]
    last_segment = graph.segments[-1]
    first_assets = _asset_ids_for_role(plan, AssetRole.FIRST_FRAME)
    last_assets = _asset_ids_for_role(plan, AssetRole.LAST_FRAME)
    missing: list[str] = []
    if mode in {TaskMode.I2VA, TaskMode.FL2VA} and not _anchor_is_owned(
        graph, first_segment, first_assets
    ):
        missing.append(AssetRole.FIRST_FRAME.value)
    if mode in {TaskMode.FL2VA, TaskMode.L2VA} and not _anchor_is_owned(
        graph, last_segment, last_assets
    ):
        missing.append(AssetRole.LAST_FRAME.value)
    if missing:
        _add(
            diagnostics,
            PromptFidelityDiagnosticId.KEYFRAME_ANCHOR_MISSING,
            ValidationSeverity.WARNING,
            EvidenceLevel.OFFICIAL,
            "intent_graph.segments",
            "the keyframe is not owned by the shot it anchors",
            "Name the supplied frame as the source of a subject in that shot.",
            (("roles", ", ".join(missing)), ("task_mode", mode.value)),
        )
        return

    anchor_subject_ids: frozenset[str] = frozenset()
    if mode in {TaskMode.I2VA, TaskMode.FL2VA}:
        anchor_subject_ids |= _anchor_subject_ids(graph, first_assets)
    if mode in {TaskMode.FL2VA, TaskMode.L2VA}:
        anchor_subject_ids |= _anchor_subject_ids(graph, last_assets)
    actions = {action.action_id: action for action in graph.actions}
    cameras = {camera.camera_id: camera for camera in graph.cameras}
    for segment in graph.segments:
        owned_in_segment = anchor_subject_ids.intersection(segment.subject_ids)
        if not owned_in_segment:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.KEYFRAME_PATH_UNOWNED,
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                f"intent_graph.segments.{segment.segment_id}",
                "the shot does not carry any subject owned by the declared keyframe anchors",
                "Bind the anchor-owned subject to every required path shot.",
                (("task_mode", mode.value), ("segment_id", segment.segment_id)),
            )
            continue
        if segment.development is SegmentDevelopment.UNSPECIFIED:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.KEYFRAME_DEVELOPMENT_MISSING,
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                f"intent_graph.segments.{segment.segment_id}",
                "the shot does not declare whether the anchor develops or remains static",
                "Declare anchor development or an anchor static hold for this shot.",
                (("task_mode", mode.value), ("segment_id", segment.segment_id)),
            )
            continue
        owned_actions = tuple(
            action
            for action_id in segment.action_ids
            if (action := actions.get(action_id)) is not None
            and owned_in_segment.intersection(action.subject_ids)
        )
        if segment.development is SegmentDevelopment.ANCHOR_STATIC_HOLD:
            if owned_actions:
                _add(
                    diagnostics,
                    PromptFidelityDiagnosticId.KEYFRAME_STATIC_HOLD_CONTRADICTED,
                    ValidationSeverity.WARNING,
                    EvidenceLevel.OFFICIAL,
                    f"intent_graph.segments.{segment.segment_id}",
                    "an action joined to the held anchor contradicts the declared static hold",
                    "Remove the anchor-owned action or declare anchor development for this shot.",
                    (("task_mode", mode.value), ("segment_id", segment.segment_id)),
                )
            continue
        camera = cameras.get(segment.camera_id) if segment.camera_id is not None else None
        camera_owns_anchor = camera is not None and bool(
            owned_in_segment.intersection(camera.subject_ids)
        )
        if not owned_actions and not camera_owns_anchor:
            _add(
                diagnostics,
                PromptFidelityDiagnosticId.KEYFRAME_DEVELOPMENT_MISSING,
                ValidationSeverity.WARNING,
                EvidenceLevel.OFFICIAL,
                f"intent_graph.segments.{segment.segment_id}",
                "declared anchor development has no action or camera path joined to the anchor",
                "Join an action or camera path to the anchor-owned subject in this shot.",
                (("task_mode", mode.value), ("segment_id", segment.segment_id)),
            )


def audit_prompt_fidelity(plan: ContextPlan, document: PromptDocument) -> PromptFidelityAuditResult:
    """Audit a rendered prompt against the official prose rules, without mutating anything."""

    if not isinstance(plan, ContextPlan):
        raise PromptLintError("prompt fidelity audit requires a ContextPlan")
    if not isinstance(document, PromptDocument):
        raise PromptLintError("prompt fidelity audit requires a PromptDocument")
    text = document.text
    if not isinstance(text, str) or len(text) > _MAX_TEXT:
        raise PromptLintError("prompt fidelity audit requires bounded prompt text")

    diagnostics: list[PromptFidelityDiagnostic] = []
    _check_shot_timestamps(plan, text, diagnostics)
    _check_camera_and_cuts(plan, text, diagnostics)
    _check_speaker_identity(text, diagnostics)
    _check_dialogue_languages(plan, text, diagnostics)
    _check_internal_vocabulary(text, diagnostics)
    _check_explicit_constraints(plan, text, diagnostics)
    _check_guide_semantics(plan, diagnostics)

    description = _description_section(text)
    band = _length_band(len(description))
    advisory = _description_advisory(plan, description, band)
    if advisory is not None and advisory[0] is not DescriptionLengthBand.WITHIN_TARGET:
        advisory_band, words, evidence_level = advisory
        _add(
            diagnostics,
            PromptFidelityDiagnosticId.DESCRIPTION_LENGTH_BAND,
            # Length remains advisory. It never decides validation or guide readiness.
            ValidationSeverity.INFO,
            evidence_level,
            "prompt_document.text",
            "the generation description length is outside the applicable advisory band",
            (
                "Develop the English generation description toward 350 to 500 words, or trim it "
                "toward that range."
            ),
            (
                ("band", advisory_band.value),
                ("characters", len(description)),
                ("words", words),
            ),
        )
    return PromptFidelityAuditResult(tuple(diagnostics), band, len(description))


__all__ = [
    "ABOVE_TARGET_CHARACTERS",
    "BELOW_TARGET_CHARACTERS",
    "PROMPT_FIDELITY_DIAGNOSTIC_IDS",
    "PROMPT_FIDELITY_SCHEMA",
    "SEVERELY_SHORT_CHARACTERS",
    "DescriptionLengthBand",
    "PromptFidelityAuditResult",
    "PromptFidelityDiagnostic",
    "PromptFidelityDiagnosticId",
    "audit_prompt_fidelity",
]
