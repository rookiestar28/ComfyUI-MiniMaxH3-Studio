"""The semantic graph itself: what a prompt is read into, and how it is read.

Nodes, relations, span anchors, provenance and uncertainty, the extraction patterns that find them
in a prompt, and the two directions across the boundary -- `parse_semantic_prompt` in and
`render_semantic_prompt` out.

Derivation is deterministic and total: an input that cannot be read raises rather than producing a
partial graph, because a partial graph downstream is indistinguishable from a real one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import (
    ProfileIdentity,
    PromptProfile,
    TaskMode,
)
from .parsing import PromptParseResult, parse_prompt
from .rendering import (
    _EVENT_PROSE as _RENDERED_EVENT_PROSE,
)
from .rendering import (
    _GENERIC_REFERENCE_PROSE as _RENDERED_GENERIC_REFERENCE_PROSE,
)
from .rendering import (
    _RETENTION_PROSE as _RENDERED_RETENTION_PROSE,
)
from .rendering import (
    _ROLE_PROSE as _RENDERED_ROLE_PROSE,
)
from .rendering import (
    _prose_alternation,
)
from .semantic_graph_primitives import (
    LEGACY_SEMANTIC_GRAPH_SCHEMA,
    MAX_SEMANTIC_ITEMS,
    MAX_SEMANTIC_TEXT,
    SEMANTIC_GRAPH_SCHEMA,
    SemanticGraphError,
    SemanticNodeKind,
    SemanticRelationKind,
    SemanticRetentionScope,
    SemanticSourceKind,
    _enum,
    _fingerprint,
    _identifier,
    _profile_identity,
    _source_text,
    _text,
)
from .source_profiled_prompt import (
    OFFICIAL_H3_BASE_GUIDE_DIGEST,
    OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
    OFFICIAL_H3_GUIDE_REVISION,
)

_ASSET = re.compile(r"<(?P<kind>Picture|Video|Audio) (?P<ordinal>[1-9][0-9]*)>")


_SUBJECT = re.compile(r"<Subject (?P<ordinal>[1-9][0-9]*)>")


_SHOT = re.compile(r"\[Shot (?P<ordinal>[1-9][0-9]*)\]")


_EVENT = re.compile(r"\bevent_(?P<ordinal>[1-9][0-9]*)\b")


_TIMING = re.compile(r"\bAt (?P<timestamp>[0-9]{2}:[0-9]{2}\.[0-9]{3}),")


# GUARD: two prompt grammars are read here on purpose. The current renderer writes guide prose,
# while prompts produced before M24-05 -- saved workflows, archived reports, frozen comparator
# corpora -- carry the repository's former internal syntax (`identifies asset ... with role ...`,
# `<A> -> <B>: marker - ...`, `event event_1 uses full_copy for [Shot 2]`). The comparator reads
# whatever text it is handed, so dropping either branch would silently stop deriving role,
# retention and copy facts from one whole generation of prompts and report the difference as an
# ordinary text change. The prose branch is built from the renderer's own maps rather than
# restated, so a wording change cannot desynchronise writer and reader.
_ROLE_PROSE_ALTERNATION = _prose_alternation(
    tuple(_RENDERED_ROLE_PROSE.values()) + tuple(_RENDERED_GENERIC_REFERENCE_PROSE.values())
)

_RETENTION_PROSE_TO_MARKER = {prose: marker for marker, prose in _RENDERED_RETENTION_PROSE.items()}

_EVENT_PROSE_TO_MODE = {prose: mode for mode, prose in _RENDERED_EVENT_PROSE.items()}


_ROLE = re.compile(
    r"(?P<label><(?:Picture|Video|Audio) [1-9][0-9]*>) (?:"
    r"identifies asset (?P<asset>[A-Za-z0-9_.:-]+) with role (?P<role>[a-z_]+) and media kind "
    r"(?P<media>[a-z_]+)"
    rf"|is (?P<role_prose>{_ROLE_PROSE_ALTERNATION})"
    r")\."
)


_OWNERSHIP = re.compile(
    r"(?P<subject><Subject [1-9][0-9]*>) is [^.]+? from "
    r"(?P<asset><(?:Picture|Video|Audio) [1-9][0-9]*>)\."
)


_RETENTION = re.compile(
    r"(?P<source><(?:Picture|Video|Audio) [1-9][0-9]*>) (?:"
    r"-> (?P<target><(?:Subject|Picture|Video|Audio) [1-9][0-9]*>): (?P<mode>[a-z_]+) - [^.]+"
    rf"|(?P<mode_prose>{_prose_alternation(tuple(_RENDERED_RETENTION_PROSE.values()))}) in "
    r"(?P<target_prose><(?:Subject|Picture|Video|Audio) [1-9][0-9]*>)"
    r")\."
)


# IMPORTANT: these fixed values are official output grammar, not private enum residue. Keep the
# current branch separate from the two legacy syntaxes above so an inspectable old row acquires an
# explicit uncertainty instead of silently receiving current official authority.
_CURRENT_RETENTION = re.compile(
    r"(?m)(?:^|retention_analysis: )"
    r"(?P<entry>(?P<target><(?:Subject|Picture|Video|Audio) [1-9][0-9]*>)"
    r"(?: \([^\r\n)]{1,512}\))?: "
    r"(?P<mode>fully_preserved|partially_preserved|attribute_transfer|weak_reference|"
    r"fully_copy|partially_copy|reference) - [^\r\n]{1,4096})\r?$"
)


_EVENT_COPY = re.compile(
    r"(?P<asset><Video [1-9][0-9]*>) (?:"
    r"event (?P<event>event_[1-9][0-9]*) uses (?P<mode>[a-z_]+) for (?P<shot>\[Shot [1-9][0-9]*\])"
    rf"|(?P<mode_prose>{_prose_alternation(tuple(_RENDERED_EVENT_PROSE.values()))})"
    r")"
)


def _role_value(match: re.Match[str]) -> str:
    return match.group("role") or match.group("role_prose")


def _retention_target(match: re.Match[str]) -> str:
    return match.group("target") or match.group("target_prose")


def _retention_mode(match: re.Match[str]) -> str:
    declared = match.group("mode")
    return declared if declared else _RETENTION_PROSE_TO_MARKER[match.group("mode_prose")]


def _event_mode(match: re.Match[str]) -> str:
    declared = match.group("mode")
    return declared if declared else _EVENT_PROSE_TO_MODE[match.group("mode_prose")]


def _has_legacy_retention(source: str) -> bool:
    return _RETENTION.search(source) is not None


def _current_retention_scope(target: str, marker: str) -> SemanticRetentionScope | None:
    visual_markers = {
        "fully_preserved",
        "partially_preserved",
        "attribute_transfer",
        "weak_reference",
    }
    audio_markers = {"fully_copy", "partially_copy", "reference", "weak_reference"}
    if target.startswith("<Subject "):
        return SemanticRetentionScope.SUBJECT if marker in visual_markers else None
    if target.startswith("<Picture "):
        return SemanticRetentionScope.PICTURE if marker in visual_markers else None
    if target.startswith("<Video "):
        return SemanticRetentionScope.VIDEO_STRUCTURE if marker in visual_markers else None
    if not target.startswith("<Audio ") or marker not in audio_markers:
        return None
    if marker == "fully_copy":
        return SemanticRetentionScope.COMPLETE_FINAL_AUDIO_TRACK
    return SemanticRetentionScope.AUDIO_LAYER


def _has_invalid_current_retention(source: str) -> bool:
    return any(
        _current_retention_scope(match.group("target"), match.group("mode")) is None
        for match in _CURRENT_RETENTION.finditer(source)
    )


_DIALOGUE = re.compile(
    # IMPORTANT: authored and canonical blocks share <d>; requiring the retired repository
    # prefix makes exact words invisible to semantic comparison after speaker rendering.
    r"(?:\((?P<speakers>S[0-9]+(?:,S[0-9]+)*)\)\s+"
    r"(?P<verb>says|sings)(?: in an off-screen voiceover)?:\s*)?"
    r"<d>(?:\[(?P<language>[^\]\r\n]{1,64})\] )?(?P<text>.{1,65536}?)</d>",
    re.DOTALL,
)


_VISIBLE_TEXT = re.compile(r'Visible text remains exactly "(?P<text>[^"\r\n]{1,4096})"')


_FROZEN_GUIDE_SOURCE_FINGERPRINTS = {
    "guide.base.1": "sha256:a1d758652601a61eb5551ec543179c2674014a095b766e475c84280d59de8fb7",
    "guide.base.2": "sha256:fa1188872e62242eb6f79bfa24ac3c9f522d1f0375a83bba0207173fc85d56ad",
    "guide.base.3": "sha256:749bb9fd0f29cde28c891aa27e216cd36bab07ceb1f85928b77e21624f9997a8",
    "guide.reference.1": "sha256:335d883254440a6e37f55e6dd373b0f13ef02b12defd024465e5d635dfe82e0b",
    "guide.reference.2": "sha256:ffe684090879e810e9cf58fdafd4e1d9fc95e173928d1d88680c18668154b72c",
    "guide.reference.3": "sha256:81bc0167460cd2d714d2f453859ab83158aec9361f31b5c81cd9f78012243568",
}


@dataclass(frozen=True, slots=True)
class SemanticSpanAnchor:
    start: int
    end: int
    text: str

    def __post_init__(self) -> None:
        if type(self.start) is not int or type(self.end) is not int:
            raise SemanticGraphError("semantic anchor offsets must be integers")
        if not 0 <= self.start < self.end <= MAX_SEMANTIC_TEXT:
            raise SemanticGraphError("semantic anchor offsets must be non-empty and bounded")
        _text(self.text, "semantic anchor text")
        if len(self.text) != self.end - self.start:
            raise SemanticGraphError("semantic anchor text length differs from its offsets")

    def to_wire(self) -> dict[str, object]:
        return {"start": self.start, "end": self.end, "text": self.text}


@dataclass(frozen=True, slots=True)
class SemanticNode:
    node_id: str
    kind: SemanticNodeKind
    value: str
    anchor: SemanticSpanAnchor
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.node_id, "semantic node_id")
        _enum(self.kind, SemanticNodeKind, "semantic node kind")
        _text(self.value, "semantic node value")
        if type(self.anchor) is not SemanticSpanAnchor:
            raise SemanticGraphError("semantic node anchor is invalid")
        if type(self.attributes) is not tuple or len(self.attributes) > 16:
            raise SemanticGraphError("semantic node attributes must be bounded")
        previous = ""
        for item in self.attributes:
            if type(item) is not tuple or len(item) != 2:
                raise SemanticGraphError("semantic node attribute is invalid")
            key = _identifier(item[0], "semantic node attribute key")
            _text(item[1], "semantic node attribute value", 256)
            if key <= previous:
                raise SemanticGraphError("semantic node attributes must be unique and sorted")
            previous = key

    def to_wire(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "value": self.value,
            "anchor": self.anchor.to_wire(),
            "attributes": {key: value for key, value in self.attributes},
        }


@dataclass(frozen=True, slots=True)
class SemanticRelation:
    relation_id: str
    kind: SemanticRelationKind
    source_id: str
    target_id: str
    value: str
    anchor: SemanticSpanAnchor
    retention_scope: SemanticRetentionScope | None = None

    def __post_init__(self) -> None:
        _identifier(self.relation_id, "semantic relation_id")
        _enum(self.kind, SemanticRelationKind, "semantic relation kind")
        _identifier(self.source_id, "semantic relation source_id")
        _identifier(self.target_id, "semantic relation target_id")
        _text(self.value, "semantic relation value", 256)
        if type(self.anchor) is not SemanticSpanAnchor:
            raise SemanticGraphError("semantic relation anchor is invalid")
        if self.retention_scope is not None:
            _enum(
                self.retention_scope,
                SemanticRetentionScope,
                "semantic relation retention_scope",
            )
            if self.kind not in {SemanticRelationKind.COPY, SemanticRelationKind.RETAIN}:
                raise SemanticGraphError("only retention relations can declare a scope")

    def to_wire(self, *, include_retention_scope: bool = False) -> dict[str, object]:
        wire: dict[str, object] = {
            "relation_id": self.relation_id,
            "kind": self.kind.value,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "value": self.value,
            "anchor": self.anchor.to_wire(),
        }
        if include_retention_scope:
            wire["retention_scope"] = (
                None if self.retention_scope is None else self.retention_scope.value
            )
        return wire


@dataclass(frozen=True, slots=True)
class SemanticUncertainty:
    code: str
    node_id: str | None

    def __post_init__(self) -> None:
        _identifier(self.code, "semantic uncertainty code")
        if self.node_id is not None:
            _identifier(self.node_id, "semantic uncertainty node_id")

    def to_wire(self) -> dict[str, object]:
        return {"code": self.code, "node_id": self.node_id}


@dataclass(frozen=True, slots=True)
class SemanticProvenance:
    source_kind: SemanticSourceKind
    source_id: str
    profile: ProfileIdentity
    task_mode: TaskMode
    source_fingerprint: str
    authority_revision: str | None = None
    authority_digest: str | None = None

    def __post_init__(self) -> None:
        _enum(self.source_kind, SemanticSourceKind, "semantic source kind")
        _identifier(self.source_id, "semantic source_id")
        _profile_identity(self.profile)
        _enum(self.task_mode, TaskMode, "semantic provenance task mode")
        if (
            type(self.source_fingerprint) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.source_fingerprint) is None
        ):
            raise SemanticGraphError("semantic source fingerprint is invalid")
        # CRITICAL: source authority requires exact strings before equality comparisons.
        for value, field in (
            (self.authority_revision, "authority_revision"),
            (self.authority_digest, "authority_digest"),
        ):
            if value is not None and type(value) is not str:
                raise SemanticGraphError(f"semantic provenance {field} is invalid")
        if self.source_kind is SemanticSourceKind.LOCAL:
            if self.authority_revision is not None or self.authority_digest is not None:
                raise SemanticGraphError("local source cannot claim external authority")
        elif self.source_kind is SemanticSourceKind.PUBLIC_GUIDE:
            expected_digest = (
                OFFICIAL_H3_BASE_GUIDE_DIGEST
                if self.profile.name is PromptProfile.BASE
                else OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST
            )
            if (
                self.authority_revision != OFFICIAL_H3_GUIDE_REVISION
                or self.authority_digest != expected_digest
            ):
                raise SemanticGraphError("public guide authority binding is invalid")
            if _FROZEN_GUIDE_SOURCE_FINGERPRINTS.get(self.source_id) != self.source_fingerprint:
                raise SemanticGraphError("public guide source is not an admitted golden")
        else:
            # CRITICAL: M14-01 prohibits an issuer for official-oracle observations.
            raise SemanticGraphError("official oracle authority is unavailable")

    def to_wire(self) -> dict[str, object]:
        return {
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "profile": self.profile.to_wire(),
            "task_mode": self.task_mode.value,
            "source_fingerprint": self.source_fingerprint,
            "authority_revision": self.authority_revision,
            "authority_digest": self.authority_digest,
        }


def _anchor(source: str, start: int, end: int) -> SemanticSpanAnchor:
    return SemanticSpanAnchor(start, end, source[start:end])


def _attributes(**values: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(values.items()))


def _unique_matches(pattern: re.Pattern[str], source: str, group: str = "0") -> list[re.Match[str]]:
    seen: set[str] = set()
    result: list[re.Match[str]] = []
    for match in pattern.finditer(source):
        key = match.group() if group == "0" else match.group(group)
        if key in seen:
            continue
        seen.add(key)
        result.append(match)
    return result


def _derive_nodes(source: str, parsed: PromptParseResult) -> tuple[SemanticNode, ...]:
    nodes: list[SemanticNode] = []
    for ordinal, field in enumerate(parsed.fields, 1):
        nodes.append(
            SemanticNode(
                f"section.{ordinal}",
                SemanticNodeKind.SECTION,
                field.value,
                _anchor(source, field.start, field.end),
                _attributes(heading=field.name, ordinal=str(ordinal)),
            )
        )
    for match in _unique_matches(_ASSET, source):
        label = match.group()
        nodes.append(
            SemanticNode(
                "asset." + label[1:-1].lower().replace(" ", "."),
                SemanticNodeKind.ASSET,
                label,
                _anchor(source, match.start(), match.end()),
                _attributes(
                    media_kind=match.group("kind").lower(),
                    ordinal=match.group("ordinal"),
                ),
            )
        )
    for match in _unique_matches(_SUBJECT, source):
        nodes.append(
            SemanticNode(
                f"entity.subject.{match.group('ordinal')}",
                SemanticNodeKind.ENTITY,
                match.group(),
                _anchor(source, match.start(), match.end()),
                _attributes(entity_kind="subject", ordinal=match.group("ordinal")),
            )
        )
    for match in _unique_matches(_SHOT, source):
        nodes.append(
            SemanticNode(
                f"event.shot.{match.group('ordinal')}",
                SemanticNodeKind.EVENT,
                match.group(),
                _anchor(source, match.start(), match.end()),
                _attributes(event_kind="shot", ordinal=match.group("ordinal")),
            )
        )
    for match in _unique_matches(_EVENT, source):
        nodes.append(
            SemanticNode(
                f"event.named.{match.group('ordinal')}",
                SemanticNodeKind.EVENT,
                match.group(),
                _anchor(source, match.start(), match.end()),
                _attributes(event_kind="named", ordinal=match.group("ordinal")),
            )
        )
    for ordinal, match in enumerate(_TIMING.finditer(source), 1):
        nodes.append(
            SemanticNode(
                f"timing.{ordinal}",
                SemanticNodeKind.TIMING,
                match.group("timestamp"),
                _anchor(source, match.start("timestamp"), match.end("timestamp")),
                _attributes(ordinal=str(ordinal)),
            )
        )
    exact_ordinal = 0
    for exact_kind, pattern in (("dialogue", _DIALOGUE), ("visible_text", _VISIBLE_TEXT)):
        for match in pattern.finditer(source):
            exact_ordinal += 1
            nodes.append(
                SemanticNode(
                    f"exact_text.{exact_ordinal}",
                    SemanticNodeKind.EXACT_TEXT,
                    match.group("text"),
                    _anchor(source, match.start("text"), match.end("text")),
                    _attributes(
                        exact_kind="lyrics"
                        if exact_kind == "dialogue" and match.group("verb") == "sings"
                        else exact_kind,
                        **(
                            {"speaker_ids": match.group("speakers")}
                            if exact_kind == "dialogue" and match.group("speakers")
                            else {}
                        ),
                    ),
                )
            )
    audio_ordinal = 0
    for field in parsed.fields:
        if field.name not in {"overall_soundscape", "non_diegetic_music"}:
            continue
        audio_ordinal += 1
        nodes.append(
            SemanticNode(
                f"audio_fact.{audio_ordinal}",
                SemanticNodeKind.AUDIO_FACT,
                field.value,
                _anchor(source, field.value_start, field.value_end),
                _attributes(audio_kind=field.name),
            )
        )
    for ordinal, match in enumerate(_EVENT_COPY.finditer(source), 1):
        nodes.append(
            SemanticNode(
                f"av_fact.{ordinal}",
                SemanticNodeKind.AV_FACT,
                match.group(),
                _anchor(source, match.start(), match.end()),
                _attributes(mode=_event_mode(match)),
            )
        )
    if len(nodes) > MAX_SEMANTIC_ITEMS:
        raise SemanticGraphError("semantic nodes exceed the bounded item limit")
    return tuple(nodes)


def _node_id(nodes: tuple[SemanticNode, ...], value: str) -> str | None:
    for node in nodes:
        if node.value == value:
            return node.node_id
    return None


def _derive_relations(
    source: str, nodes: tuple[SemanticNode, ...], *, schema: str
) -> tuple[SemanticRelation, ...]:
    relations: list[SemanticRelation] = []

    def append(
        kind: SemanticRelationKind,
        source_id: str,
        target_id: str,
        value: str,
        start: int,
        end: int,
        retention_scope: SemanticRetentionScope | None = None,
    ) -> None:
        relations.append(
            SemanticRelation(
                f"relation.{len(relations) + 1}",
                kind,
                source_id,
                target_id,
                value,
                _anchor(source, start, end),
                retention_scope,
            )
        )

    sections = [node for node in nodes if node.kind is SemanticNodeKind.SECTION]
    for left, right in zip(sections, sections[1:], strict=False):
        append(
            SemanticRelationKind.ORDER,
            left.node_id,
            right.node_id,
            "section_order",
            right.anchor.start,
            right.anchor.end,
        )
    for match in _ROLE.finditer(source):
        asset_id = _node_id(nodes, match.group("label"))
        section_id = sections[0].node_id if sections else None
        if asset_id is not None and section_id is not None:
            append(
                SemanticRelationKind.ROLE,
                asset_id,
                section_id,
                _role_value(match),
                match.start(),
                match.end(),
            )
    for match in _OWNERSHIP.finditer(source):
        subject_id = _node_id(nodes, match.group("subject"))
        asset_id = _node_id(nodes, match.group("asset"))
        if subject_id is not None and asset_id is not None:
            append(
                SemanticRelationKind.OWNERSHIP,
                subject_id,
                asset_id,
                "from",
                match.start(),
                match.end(),
            )
    if schema == SEMANTIC_GRAPH_SCHEMA:
        for match in _CURRENT_RETENTION.finditer(source):
            target_id = _node_id(nodes, match.group("target"))
            if target_id is None:
                continue
            mode = match.group("mode")
            target = match.group("target")
            scope = _current_retention_scope(target, mode)
            if scope is None:
                continue
            kind = SemanticRelationKind.COPY if "copy" in mode else SemanticRelationKind.RETAIN
            append(
                kind,
                target_id,
                target_id,
                mode,
                match.start("entry"),
                match.end("entry"),
                scope,
            )
    for match in _RETENTION.finditer(source):
        source_id = _node_id(nodes, match.group("source"))
        target_id = _node_id(nodes, _retention_target(match))
        if source_id is None or target_id is None:
            continue
        mode = _retention_mode(match)
        kind = SemanticRelationKind.COPY if "copy" in mode else SemanticRelationKind.RETAIN
        append(kind, source_id, target_id, mode, match.start(), match.end())
    timing_nodes = [node for node in nodes if node.kind is SemanticNodeKind.TIMING]
    shot_nodes = [
        node
        for node in nodes
        if node.kind is SemanticNodeKind.EVENT and node.node_id.startswith("event.shot.")
    ]
    for timing in timing_nodes:
        preceding_shot = next(
            (shot for shot in reversed(shot_nodes) if shot.anchor.start < timing.anchor.start),
            None,
        )
        if preceding_shot is not None:
            append(
                SemanticRelationKind.TEMPORAL,
                preceding_shot.node_id,
                timing.node_id,
                timing.value,
                timing.anchor.start,
                timing.anchor.end,
            )
    for match in _EVENT_COPY.finditer(source):
        asset_id = _node_id(nodes, match.group("asset"))
        declared_event = match.group("event")
        event_id = _node_id(nodes, declared_event) if declared_event else None
        declared_shot = match.group("shot")
        # Guide prose states the copy inside the shot it applies to instead of naming it, so the
        # owning shot is the nearest `[Shot N]` anchor before the sentence.
        shot_id = (
            _node_id(nodes, declared_shot)
            if declared_shot
            else next(
                (
                    shot.node_id
                    for shot in reversed(shot_nodes)
                    if shot.anchor.start < match.start()
                ),
                None,
            )
        )
        if asset_id is not None and event_id is None and shot_id is not None:
            append(
                SemanticRelationKind.COPY,
                asset_id,
                shot_id,
                _event_mode(match),
                match.start(),
                match.end(),
            )
        if asset_id is not None and event_id is not None:
            append(
                SemanticRelationKind.COPY,
                asset_id,
                event_id,
                _event_mode(match),
                match.start(),
                match.end(),
            )
        if asset_id is not None and shot_id is not None:
            append(
                SemanticRelationKind.AV,
                asset_id,
                shot_id,
                "video_event",
                match.start(),
                match.end(),
            )
    if len(relations) > MAX_SEMANTIC_ITEMS:
        raise SemanticGraphError("semantic relations exceed the bounded item limit")
    return tuple(relations)


def _graph_payload(
    schema: str,
    source: str,
    provenance: SemanticProvenance,
    nodes: tuple[SemanticNode, ...],
    relations: tuple[SemanticRelation, ...],
    uncertainties: tuple[SemanticUncertainty, ...],
    observed_rules: tuple[str, ...],
    hypotheses: tuple[str, ...],
    unknowns: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema": schema,
        "source_text": source,
        "provenance": provenance.to_wire(),
        "nodes": [node.to_wire() for node in nodes],
        "relations": [
            relation.to_wire(include_retention_scope=schema == SEMANTIC_GRAPH_SCHEMA)
            for relation in relations
        ],
        "uncertainties": [item.to_wire() for item in uncertainties],
        "observed_rules": list(observed_rules),
        "hypotheses": list(hypotheses),
        "unknowns": list(unknowns),
    }


def _derive(
    schema: str,
    source: str,
    profile: ProfileIdentity,
    task_mode: TaskMode,
    source_kind: SemanticSourceKind,
    source_id: str,
    authority_revision: str | None,
    authority_digest: str | None,
) -> tuple[
    SemanticProvenance,
    tuple[SemanticNode, ...],
    tuple[SemanticRelation, ...],
    tuple[SemanticUncertainty, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    if schema not in {LEGACY_SEMANTIC_GRAPH_SCHEMA, SEMANTIC_GRAPH_SCHEMA}:
        raise SemanticGraphError("semantic graph schema is invalid")
    parsed = parse_prompt(source, profile, task_mode)
    if not parsed.is_valid:
        raise SemanticGraphError("semantic graph requires a valid canonical prompt")
    provenance = SemanticProvenance(
        source_kind,
        source_id,
        profile,
        task_mode,
        _fingerprint(source),
        authority_revision,
        authority_digest,
    )
    nodes = _derive_nodes(source, parsed)
    kinds = {node.kind for node in nodes}
    required = set(SemanticNodeKind)
    uncertainties = tuple(
        SemanticUncertainty(f"missing.{kind.value}", None)
        for kind in sorted(required - kinds, key=lambda item: item.value)
    )
    if schema == SEMANTIC_GRAPH_SCHEMA and _has_legacy_retention(source):
        uncertainties += (SemanticUncertainty("legacy.retention_scope_unspecified", None),)
    if schema == SEMANTIC_GRAPH_SCHEMA and _has_invalid_current_retention(source):
        uncertainties += (SemanticUncertainty("invalid.retention_marker_domain", None),)
    relations = _derive_relations(source, nodes, schema=schema)
    observed_rules = tuple(
        sorted(
            {f"parse.{node.kind.value}" for node in nodes}
            | {f"relation.{relation.kind.value}" for relation in relations}
        )
    )
    hypotheses: tuple[str, ...] = ()
    unknowns = tuple(item.code for item in uncertainties)
    return provenance, nodes, relations, uncertainties, observed_rules, hypotheses, unknowns


@dataclass(frozen=True, slots=True)
class SemanticPromptGraph:
    schema: str
    source_text: str
    provenance: SemanticProvenance
    nodes: tuple[SemanticNode, ...]
    relations: tuple[SemanticRelation, ...]
    uncertainties: tuple[SemanticUncertainty, ...]
    observed_rules: tuple[str, ...]
    hypotheses: tuple[str, ...]
    unknowns: tuple[str, ...]
    graph_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.schema) is not str or self.schema not in {
            LEGACY_SEMANTIC_GRAPH_SCHEMA,
            SEMANTIC_GRAPH_SCHEMA,
        }:
            raise SemanticGraphError("semantic graph schema is invalid")
        source = _source_text(self.source_text)
        if type(self.provenance) is not SemanticProvenance:
            raise SemanticGraphError("semantic graph provenance is invalid")
        # CRITICAL: close aggregate types before derived equality or member method dispatch.
        if type(self.nodes) is not tuple or not all(
            type(node) is SemanticNode for node in self.nodes
        ):
            raise SemanticGraphError("semantic graph nodes inventory is invalid")
        if type(self.relations) is not tuple or not all(
            type(relation) is SemanticRelation for relation in self.relations
        ):
            raise SemanticGraphError("semantic graph relations inventory is invalid")
        if type(self.uncertainties) is not tuple or not all(
            type(item) is SemanticUncertainty for item in self.uncertainties
        ):
            raise SemanticGraphError("semantic graph uncertainties inventory is invalid")
        for values, field in (
            (self.observed_rules, "observed_rules"),
            (self.hypotheses, "hypotheses"),
            (self.unknowns, "unknowns"),
        ):
            if type(values) is not tuple or not all(type(item) is str for item in values):
                raise SemanticGraphError(f"semantic graph {field} inventory is invalid")
        if (
            type(self.graph_fingerprint) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.graph_fingerprint) is None
        ):
            raise SemanticGraphError("semantic graph fingerprint is invalid")
        derived = _derive(
            self.schema,
            source,
            self.provenance.profile,
            self.provenance.task_mode,
            self.provenance.source_kind,
            self.provenance.source_id,
            self.provenance.authority_revision,
            self.provenance.authority_digest,
        )
        expected = (
            self.provenance,
            self.nodes,
            self.relations,
            self.uncertainties,
            self.observed_rules,
            self.hypotheses,
            self.unknowns,
        )
        if derived != expected:
            raise SemanticGraphError("semantic graph is not fully source-derived")
        node_ids = {node.node_id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise SemanticGraphError("semantic graph node IDs must be unique")
        for relation in self.relations:
            if relation.source_id not in node_ids or relation.target_id not in node_ids:
                raise SemanticGraphError("semantic relation references an unknown node")
        for node in self.nodes:
            if source[node.anchor.start : node.anchor.end] != node.anchor.text:
                raise SemanticGraphError("semantic node anchor differs from source")
        for relation in self.relations:
            if source[relation.anchor.start : relation.anchor.end] != relation.anchor.text:
                raise SemanticGraphError("semantic relation anchor differs from source")
        payload = _graph_payload(
            self.schema,
            source,
            self.provenance,
            self.nodes,
            self.relations,
            self.uncertainties,
            self.observed_rules,
            self.hypotheses,
            self.unknowns,
        )
        if self.graph_fingerprint != _fingerprint(payload):
            raise SemanticGraphError("semantic graph fingerprint differs from derived content")

    def to_wire(self) -> dict[str, object]:
        payload = _graph_payload(
            self.schema,
            self.source_text,
            self.provenance,
            self.nodes,
            self.relations,
            self.uncertainties,
            self.observed_rules,
            self.hypotheses,
            self.unknowns,
        )
        payload["graph_fingerprint"] = self.graph_fingerprint
        return payload


def _parse_semantic_prompt_with_schema(
    schema: str,
    source_text: str,
    profile: ProfileIdentity,
    task_mode: TaskMode,
    source_kind: SemanticSourceKind,
    source_id: str,
    authority_revision: str | None = None,
    authority_digest: str | None = None,
) -> SemanticPromptGraph:
    source = _source_text(source_text)
    _enum(source_kind, SemanticSourceKind, "semantic source kind")
    _identifier(source_id, "semantic source_id")
    derived = _derive(
        schema,
        source,
        profile,
        task_mode,
        source_kind,
        source_id,
        authority_revision,
        authority_digest,
    )
    provenance, nodes, relations, uncertainties, observed, hypotheses, unknowns = derived
    payload = _graph_payload(
        schema,
        source,
        provenance,
        nodes,
        relations,
        uncertainties,
        observed,
        hypotheses,
        unknowns,
    )
    return SemanticPromptGraph(
        schema,
        source,
        provenance,
        nodes,
        relations,
        uncertainties,
        observed,
        hypotheses,
        unknowns,
        _fingerprint(payload),
    )


def parse_semantic_prompt(
    source_text: str,
    profile: ProfileIdentity,
    task_mode: TaskMode,
    source_kind: SemanticSourceKind,
    source_id: str,
    authority_revision: str | None = None,
    authority_digest: str | None = None,
) -> SemanticPromptGraph:
    """Build a current fully regenerated graph from one explicit canonical prompt."""

    return _parse_semantic_prompt_with_schema(
        SEMANTIC_GRAPH_SCHEMA,
        source_text,
        profile,
        task_mode,
        source_kind,
        source_id,
        authority_revision,
        authority_digest,
    )


def parse_legacy_semantic_prompt_v1(
    source_text: str,
    profile: ProfileIdentity,
    task_mode: TaskMode,
    source_kind: SemanticSourceKind,
    source_id: str,
    authority_revision: str | None = None,
    authority_digest: str | None = None,
) -> SemanticPromptGraph:
    """Regenerate the bounded v1 inspection form without granting current authority."""

    return _parse_semantic_prompt_with_schema(
        LEGACY_SEMANTIC_GRAPH_SCHEMA,
        source_text,
        profile,
        task_mode,
        source_kind,
        source_id,
        authority_revision,
        authority_digest,
    )


def render_semantic_prompt(graph: SemanticPromptGraph) -> str:
    """Return the exact retained prompt only after full graph revalidation."""

    if type(graph) is not SemanticPromptGraph:
        raise SemanticGraphError("semantic rendering requires a SemanticPromptGraph")
    SemanticPromptGraph(**graph.__dict__) if hasattr(graph, "__dict__") else SemanticPromptGraph(
        graph.schema,
        graph.source_text,
        graph.provenance,
        graph.nodes,
        graph.relations,
        graph.uncertainties,
        graph.observed_rules,
        graph.hypotheses,
        graph.unknowns,
        graph.graph_fingerprint,
    )
    return graph.source_text
