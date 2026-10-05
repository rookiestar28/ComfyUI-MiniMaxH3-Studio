"""Vocabulary shared by every layer of the semantic graph comparator.

Schema identifiers, the frozen evaluation constants M14-01 terminated on, the size limits that bound
untrusted input, the one error type, the six kinds, and the scalar readers that refuse rather than
coerce.

Imports nothing from the layers above it. `_text` is the choke point every string value passes
through, which is why the limit constants live beside it instead of at the call sites.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum

from .contracts import (
    CURRENT_PROFILE_VERSION,
    ProfileIdentity,
    PromptProfile,
    SchemaVersion,
)
from .errors import PromptParseError

LEGACY_SEMANTIC_GRAPH_SCHEMA = "h3.prompt.semantic_graph.v1"


SEMANTIC_GRAPH_SCHEMA = "h3.prompt.semantic_graph.v2"


SEMANTIC_EVALUATION_SCHEMA = "h3.semantic_graph_comparator.evaluation.v1"


OFFICIAL_BEHAVIOR_PROFILE_SCHEMA = "h3.official.behavior_profile.v1"


M14_01_TERMINAL_DISPOSITION_SHA256 = (
    "4a21488a2d7f607e"  # pragma: allowlist secret
    "4a25ff6dcaac829f"  # pragma: allowlist secret
    "d347d4e25c9a619f"  # pragma: allowlist secret
    "57aeb0c443674122"  # pragma: allowlist secret
)


FROZEN_GUIDE_CLUSTER_COUNT = 2


FROZEN_GUIDE_SUPPORT = 6


FROZEN_P0_CLUSTER_COUNT = 8


FROZEN_P0_SUPPORT = 24


FROZEN_NON_P0_CLUSTER_COUNT = 10


FROZEN_NON_P0_SUPPORT = 40


FROZEN_NON_P0_POSITIVE_SUPPORT = 20


FROZEN_NON_P0_NEGATIVE_SUPPORT = 20


FROZEN_INTERVAL_METHOD = "wilson_two_sided_95"


FROZEN_INTERVAL_Z = "1.959963984540054"


FROZEN_POINT_TARGET = "0.95000000"


FROZEN_EVALUATION_BUNDLE_FINGERPRINT = (
    "sha256:a86a67781151dc7fb46a8739964a4efe4d6cc3ca11173377826bbd3b4056cf7d"
)


MAX_SEMANTIC_ITEMS = 512


MAX_SEMANTIC_TEXT = 65_536


MAX_SEMANTIC_VALUE = 4_096


MAX_EVALUATION_JSON_BYTES = 16_777_216


MAX_EVALUATION_SOURCE_TEXT = 8_192


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")


class SemanticGraphError(PromptParseError):
    """A semantic graph or comparison contract is invalid."""


class SemanticSourceKind(str, Enum):
    LOCAL = "local"
    PUBLIC_GUIDE = "public_guide"
    OFFICIAL_ORACLE = "official_oracle"


class SemanticNodeKind(str, Enum):
    SECTION = "section"
    ASSET = "asset"
    ENTITY = "entity"
    EVENT = "event"
    TIMING = "timing"
    EXACT_TEXT = "exact_text"
    AUDIO_FACT = "audio_fact"
    AV_FACT = "av_fact"


class SemanticRelationKind(str, Enum):
    ROLE = "role"
    OWNERSHIP = "ownership"
    ORDER = "order"
    COPY = "copy"
    RETAIN = "retain"
    TEMPORAL = "temporal"
    AV = "av"


class SemanticRetentionScope(str, Enum):
    SUBJECT = "subject"
    PICTURE = "picture"
    VIDEO_STRUCTURE = "video_structure"
    AUDIO_LAYER = "audio_layer"
    COMPLETE_FINAL_AUDIO_TRACK = "complete_final_audio_track"


class SemanticDiffOutcome(str, Enum):
    EXACT = "exact"
    COMPATIBLE = "compatible"
    LOCAL_ONLY = "local_only"
    OFFICIAL_ONLY = "official_only"
    CONTRADICTION = "contradiction"
    ROLE = "role"
    OWNERSHIP = "ownership"
    ORDER = "order"
    TEMPORAL = "temporal"
    HARD_MUTATION = "hard_mutation"
    UNSCORABLE = "unscorable"


class EvaluationPartition(str, Enum):
    P0 = "p0"
    NON_P0 = "non_p0"


class BehaviorProfileDisposition(str, Enum):
    UNAVAILABLE = "UNAVAILABLE"


_OUTCOME_PRIORITY = {
    SemanticDiffOutcome.EXACT: 0,
    SemanticDiffOutcome.COMPATIBLE: 1,
    SemanticDiffOutcome.OFFICIAL_ONLY: 2,
    SemanticDiffOutcome.LOCAL_ONLY: 3,
    SemanticDiffOutcome.CONTRADICTION: 4,
    SemanticDiffOutcome.TEMPORAL: 5,
    SemanticDiffOutcome.ORDER: 6,
    SemanticDiffOutcome.OWNERSHIP: 7,
    SemanticDiffOutcome.ROLE: 8,
    SemanticDiffOutcome.HARD_MUTATION: 9,
    SemanticDiffOutcome.UNSCORABLE: 10,
}


def _text(value: object, field: str, maximum: int = MAX_SEMANTIC_VALUE) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise SemanticGraphError(f"{field} must be a bounded non-empty string")
    if any(
        (ord(character) < 0x20 and character not in "\n\r\t")
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise SemanticGraphError(f"{field} contains an unsafe code point")
    return value


def _identifier(value: object, field: str) -> str:
    text = _text(value, field, 128)
    if _IDENTIFIER.fullmatch(text) is None:
        raise SemanticGraphError(f"{field} must be a bounded lower-case identifier")
    return text


def _source_text(value: object) -> str:
    return _text(value, "semantic source text", MAX_SEMANTIC_TEXT)


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _enum(value: object, enum_type: type[Enum], field: str) -> None:
    if type(value) is not enum_type:
        raise SemanticGraphError(f"{field} must be {enum_type.__name__}")


def _profile_identity(value: object) -> ProfileIdentity:
    if type(value) is not ProfileIdentity:
        raise SemanticGraphError("semantic provenance profile is invalid")
    if type(value.name) is not PromptProfile:
        raise SemanticGraphError("semantic provenance profile name is invalid")
    version = value.version
    if (
        type(version) is not SchemaVersion
        or type(version.major) is not int
        or type(version.minor) is not int
        or (version.major, version.minor)
        != (CURRENT_PROFILE_VERSION.major, CURRENT_PROFILE_VERSION.minor)
    ):
        raise SemanticGraphError("semantic provenance profile version is invalid")
    return value
