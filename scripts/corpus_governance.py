"""Validate the offline H3 corpus and annotation governance contract.

This checker validates metadata only. It never opens URLs, reads media, resolves a media path, or
executes an oracle/model. Portable cases contain fingerprints and bounded annotations, not content.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ID = "h3-context-corpus-governance/1"
MAX_CORPUS_BYTES = 8 * 1024 * 1024
MAX_STRATA = 512
MAX_PARTITIONS = 16
MAX_PROVENANCE = 512
MAX_CASES = 4096
MAX_ANNOTATIONS_PER_CASE = 64

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
FORBIDDEN_TEXT_PATTERN = re.compile(
    r"(?i)(?:-----BEGIN|authorization\s*:|cookie\s*:|api[_-]?key\s*[:=]|"
    r"signed[_-]?url|private[_-]?path|raw[_-]?media|prompt[_-]?content|"
    r"provider[_-]?error)"
)
MODES = frozenset({"t2va", "i2va", "fl2va", "l2va", "ref2va"})
LANGUAGES = frozenset({"chinese", "english", "code_switch", "multilingual"})
MODALITIES = frozenset({"text", "image", "video", "audio", "reference"})
RISK_CLASSES = frozenset(
    {
        "standard",
        "ambiguity",
        "conflict",
        "impossible_constraint",
        "adversarial",
        "resource",
        "missing_media",
        "malformed_media",
    }
)
TIMING_SEMANTICS = frozenset({"explicit", "implicit", "mixed"})
EXACT_TEXT_FIELDS = frozenset({"dialogue", "lyrics", "visible_text"})
DIMENSIONS = frozenset(
    {
        "identity",
        "style",
        "motion",
        "camera",
        "editing",
        "dialogue",
        "ocr",
        "audio",
        "ambiguity",
        "adversarial",
        "resource",
    }
)
PARTITION_KINDS = frozenset({"train", "dev", "test", "restricted_oracle"})
PROVENANCE_KINDS = frozenset(
    {
        "synthetic_ground_truth",
        "licensed_public",
        "official_guide_golden",
        "authorized_oracle",
        "human_adjudication",
    }
)
ANNOTATION_PROVENANCE = frozenset(
    {
        "user_declared",
        "synthetic_ground_truth",
        "licensed_source",
        "official_oracle",
        "human_adjudication",
        "model_observation",
    }
)
FORBIDDEN_PORTABLE_FIELDS = frozenset(
    {"raw_media", "prompt_content", "credentials", "cookies", "signed_urls", "private_paths"}
)
REVIEW_DIMENSIONS = frozenset(
    {
        "hard_constraints",
        "subject_identity",
        "reference_role",
        "temporal_order",
        "camera_motion",
        "audio_visual",
        "hallucination_omission",
        "overall_intent",
    }
)


class CorpusError(ValueError):
    """Raised when corpus governance metadata violates its versioned contract."""


@dataclass(frozen=True)
class Corpus:
    corpus_id: str
    corpus_version: str
    updated_at: str
    stratum_count: int
    partition_count: int
    provenance_count: int
    case_count: int
    modes: tuple[str, ...]
    dimensions: tuple[str, ...]


@dataclass(frozen=True)
class CorpusReport:
    schema: str
    corpus_id: str | None
    corpus_version: str | None
    status: str
    stratum_count: int
    partition_count: int
    provenance_count: int
    case_count: int
    diagnostics: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "corpus_id": self.corpus_id,
            "corpus_version": self.corpus_version,
            "status": self.status,
            "stratum_count": self.stratum_count,
            "partition_count": self.partition_count,
            "provenance_count": self.provenance_count,
            "case_count": self.case_count,
            "diagnostics": list(self.diagnostics),
        }


JsonObject = dict[str, object]


def _object(value: object, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise CorpusError(f"{field} must be an object")
    return cast(JsonObject, value)


def _string(
    value: object, field: str, *, pattern: re.Pattern[str] | None = None, maximum: int = 4096
) -> str:
    if not isinstance(value, str) or not value:
        raise CorpusError(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise CorpusError(f"{field} exceeds the {maximum}-character limit")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise CorpusError(f"{field} has an invalid format")
    return value


def _safe_text(value: object, field: str, maximum: int = 512) -> str:
    result = _string(value, field, maximum=maximum)
    if FORBIDDEN_TEXT_PATTERN.search(result):
        raise CorpusError(f"{field} contains a forbidden private/content marker")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in result):
        raise CorpusError(f"{field} contains control characters")
    return result


def _date(value: object, field: str) -> str:
    result = _string(value, field, maximum=10)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise CorpusError(f"{field} must be an ISO date") from exc
    return result


def _enum(value: object, field: str, allowed: frozenset[str]) -> str:
    result = _string(value, field)
    if result not in allowed:
        raise CorpusError(f"{field} must be one of: {', '.join(sorted(allowed))}")
    return result


def _keys(
    value: Mapping[str, object], required: frozenset[str], allowed: frozenset[str], field: str
) -> None:
    missing = sorted(required.difference(value))
    extra = sorted(set(value).difference(allowed))
    if missing:
        raise CorpusError(f"{field} is missing required fields: {', '.join(missing)}")
    if extra:
        raise CorpusError(f"{field} contains unsupported fields: {', '.join(extra)}")


def _identifiers(value: object, field: str, maximum: int = 64) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CorpusError(f"{field} must be a non-empty list")
    if len(value) > maximum:
        raise CorpusError(f"{field} exceeds the {maximum}-item limit")
    result = tuple(
        _string(item, f"{field}[{index}]", pattern=IDENTIFIER_PATTERN)
        for index, item in enumerate(value)
    )
    if len(result) != len(set(result)):
        raise CorpusError(f"{field} contains duplicate values")
    return result


def _enum_list(value: object, field: str, allowed: frozenset[str], maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CorpusError(f"{field} must be a non-empty list")
    if len(value) > maximum:
        raise CorpusError(f"{field} exceeds the {maximum}-item limit")
    result = tuple(_enum(item, f"{field}[{index}]", allowed) for index, item in enumerate(value))
    if len(result) != len(set(result)):
        raise CorpusError(f"{field} contains duplicate values")
    return result


def _boolean(value: object, field: str, expected: bool | None = None) -> bool:
    if not isinstance(value, bool):
        raise CorpusError(f"{field} must be a boolean")
    if expected is not None and value is not expected:
        expected_text = "true" if expected else "false"
        raise CorpusError(f"{field} must be {expected_text}")
    return value


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise CorpusError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _validate_url(value: object, field: str) -> str:
    result = _string(value, field, maximum=2048)
    try:
        parsed = urlsplit(result)
    except ValueError as exc:
        raise CorpusError(f"{field} is not a valid URL") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise CorpusError(f"{field} must be an HTTPS URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise CorpusError(f"{field} must not contain userinfo")
    if any(
        marker in parsed.query.casefold() for marker in ("token=", "sig=", "signature=", "x-amz-")
    ):
        raise CorpusError(f"{field} must not contain signed or credential query parameters")
    return result


def _validate_policy(document: JsonObject) -> None:
    license_policy = _object(document["license_policy"], "license_policy")
    _keys(
        license_policy,
        frozenset(
            {
                "review_status",
                "reviewed_source_refs",
                "allowed_license_classes",
                "forbidden_content_classes",
            }
        ),
        frozenset(
            {
                "review_status",
                "reviewed_source_refs",
                "allowed_license_classes",
                "forbidden_content_classes",
            }
        ),
        "license_policy",
    )
    if (
        _enum(
            license_policy["review_status"],
            "license_policy.review_status",
            frozenset({"reviewed", "pending", "restricted"}),
        )
        != "reviewed"
    ):
        raise CorpusError("license_policy.review_status must be reviewed")
    _identifiers(license_policy["reviewed_source_refs"], "license_policy.reviewed_source_refs")
    _identifiers(
        license_policy["allowed_license_classes"], "license_policy.allowed_license_classes"
    )
    forbidden_license = _identifiers(
        license_policy["forbidden_content_classes"], "license_policy.forbidden_content_classes"
    )
    if "unreviewed_or_prohibited" not in forbidden_license:
        raise CorpusError("license_policy must forbid unreviewed_or_prohibited content")

    privacy_policy = _object(document["privacy_policy"], "privacy_policy")
    _keys(
        privacy_policy,
        frozenset(
            {
                "review_status",
                "portable_projection",
                "forbidden_fields",
                "restricted_retention_days",
            }
        ),
        frozenset(
            {
                "review_status",
                "portable_projection",
                "forbidden_fields",
                "restricted_retention_days",
            }
        ),
        "privacy_policy",
    )
    if (
        _enum(
            privacy_policy["review_status"],
            "privacy_policy.review_status",
            frozenset({"reviewed", "pending", "restricted"}),
        )
        != "reviewed"
    ):
        raise CorpusError("privacy_policy.review_status must be reviewed")
    if privacy_policy["portable_projection"] != "fingerprints_and_bounded_metadata":
        raise CorpusError("privacy_policy.portable_projection is not safe")
    forbidden_fields = set(
        _identifiers(privacy_policy["forbidden_fields"], "privacy_policy.forbidden_fields")
    )
    missing_fields = sorted(FORBIDDEN_PORTABLE_FIELDS.difference(forbidden_fields))
    if missing_fields:
        raise CorpusError(f"privacy_policy omits forbidden fields: {', '.join(missing_fields)}")
    _integer(
        privacy_policy["restricted_retention_days"],
        "privacy_policy.restricted_retention_days",
        1,
        3650,
    )


def _validate_coverage(
    document: JsonObject,
) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    coverage = _object(document["coverage_policy"], "coverage_policy")
    _keys(
        coverage,
        frozenset(
            {
                "languages",
                "modalities",
                "risk_classes",
                "timing_semantics",
                "exact_text_fields",
                "rationale",
                "quota_policy",
            }
        ),
        frozenset(
            {
                "languages",
                "modalities",
                "risk_classes",
                "timing_semantics",
                "exact_text_fields",
                "rationale",
                "quota_policy",
            }
        ),
        "coverage_policy",
    )
    languages = set(_enum_list(coverage["languages"], "coverage_policy.languages", LANGUAGES, 4))
    modalities = set(
        _enum_list(coverage["modalities"], "coverage_policy.modalities", MODALITIES, 5)
    )
    risk_classes = set(
        _enum_list(coverage["risk_classes"], "coverage_policy.risk_classes", RISK_CLASSES, 8)
    )
    timing_semantics = set(
        _enum_list(
            coverage["timing_semantics"],
            "coverage_policy.timing_semantics",
            TIMING_SEMANTICS,
            3,
        )
    )
    exact_text_fields = set(
        _identifiers(coverage["exact_text_fields"], "coverage_policy.exact_text_fields")
    )
    for label, expected, actual in (
        ("languages", LANGUAGES, languages),
        ("modalities", MODALITIES, modalities),
        ("risk_classes", RISK_CLASSES, risk_classes),
        ("timing_semantics", TIMING_SEMANTICS, timing_semantics),
    ):
        if actual != expected:
            raise CorpusError(f"coverage_policy.{label} must enumerate the complete required set")
    if not EXACT_TEXT_FIELDS.issubset(exact_text_fields):
        missing = sorted(EXACT_TEXT_FIELDS.difference(exact_text_fields))
        raise CorpusError(
            "coverage_policy.exact_text_fields omits required fields: " + ", ".join(missing)
        )
    rationale = _object(coverage["rationale"], "coverage_policy.rationale")
    rationale_fields = frozenset({"pilot", "power", "variability", "budget", "source_clusters"})
    _keys(rationale, rationale_fields, rationale_fields, "coverage_policy.rationale")
    for field in sorted(rationale_fields):
        _safe_text(rationale[field], f"coverage_policy.rationale.{field}")
    if coverage["quota_policy"] != "evidence_justified_not_fixed":
        raise CorpusError("coverage_policy.quota_policy must reject arbitrary fixed quotas")
    return languages, modalities, risk_classes, timing_semantics, exact_text_fields


def _validate_review(document: JsonObject) -> None:
    review = _object(document["review_protocol"], "review_protocol")
    _keys(
        review,
        frozenset(
            {
                "protocol_id",
                "method",
                "blind",
                "pairwise",
                "tie_allowed",
                "order_swap_required",
                "dimensions",
                "qualification",
                "adjudication",
            }
        ),
        frozenset(
            {
                "protocol_id",
                "method",
                "blind",
                "pairwise",
                "tie_allowed",
                "order_swap_required",
                "dimensions",
                "qualification",
                "adjudication",
            }
        ),
        "review_protocol",
    )
    _string(review["protocol_id"], "review_protocol.protocol_id", pattern=IDENTIFIER_PATTERN)
    if review["method"] != "blinded_pairwise":
        raise CorpusError("review_protocol.method must be blinded_pairwise")
    for field in ("blind", "pairwise", "tie_allowed", "order_swap_required"):
        _boolean(review[field], f"review_protocol.{field}", True)
    dimensions = set(_identifiers(review["dimensions"], "review_protocol.dimensions"))
    missing = sorted(REVIEW_DIMENSIONS.difference(dimensions))
    if missing:
        raise CorpusError(
            f"review_protocol omits non-compensatory dimensions: {', '.join(missing)}"
        )
    _safe_text(review["qualification"], "review_protocol.qualification")
    _safe_text(review["adjudication"], "review_protocol.adjudication")


def _validate_annotation(
    value: object, field: str, *, expected_language: str | None = None
) -> tuple[str, ...]:
    annotation = _object(value, field)
    _keys(
        annotation,
        frozenset(
            {"annotation_id", "provenance", "uncertainty", "agreement", "exact_text_ownership"}
        ),
        frozenset(
            {"annotation_id", "provenance", "uncertainty", "agreement", "exact_text_ownership"}
        ),
        field,
    )
    _string(annotation["annotation_id"], f"{field}.annotation_id", pattern=IDENTIFIER_PATTERN)
    provenance = set(_identifiers(annotation["provenance"], f"{field}.provenance"))
    if not provenance or not provenance.issubset(ANNOTATION_PROVENANCE):
        raise CorpusError(f"{field}.provenance contains unsupported provenance labels")

    uncertainty = _object(annotation["uncertainty"], f"{field}.uncertainty")
    _keys(
        uncertainty,
        frozenset({"level", "fields", "rationale_code"}),
        frozenset({"level", "fields", "rationale_code"}),
        f"{field}.uncertainty",
    )
    _enum(
        uncertainty["level"],
        f"{field}.uncertainty.level",
        frozenset({"low", "medium", "high", "unknown"}),
    )
    _identifiers(uncertainty["fields"], f"{field}.uncertainty.fields")
    _string(
        uncertainty["rationale_code"],
        f"{field}.uncertainty.rationale_code",
        pattern=IDENTIFIER_PATTERN,
    )

    agreement = _object(annotation["agreement"], f"{field}.agreement")
    _keys(
        agreement,
        frozenset({"method", "annotator_count", "status"}),
        frozenset({"method", "annotator_count", "status"}),
        f"{field}.agreement",
    )
    _enum(
        agreement["method"],
        f"{field}.agreement.method",
        frozenset({"single_annotator", "multi_annotator", "adjudicated", "imported"}),
    )
    _integer(agreement["annotator_count"], f"{field}.agreement.annotator_count", 1, 128)
    _enum(agreement["status"], f"{field}.agreement.status", frozenset({"complete", "adjudicated"}))

    exact = _object(annotation["exact_text_ownership"], f"{field}.exact_text_ownership")
    _keys(
        exact,
        frozenset({"owner", "language_class", "field_ids", "preserve_verbatim"}),
        frozenset({"owner", "language_class", "field_ids", "preserve_verbatim"}),
        f"{field}.exact_text_ownership",
    )
    owner = _enum(
        exact["owner"],
        f"{field}.exact_text_ownership.owner",
        frozenset(
            {
                "user_declared",
                "annotator_transcribed",
                "synthetic_ground_truth",
                "not_applicable",
                "unknown",
            }
        ),
    )
    language_class = _enum(
        exact["language_class"],
        f"{field}.exact_text_ownership.language_class",
        frozenset(
            {"chinese", "english", "code_switch", "multilingual", "unknown", "not_applicable"}
        ),
    )
    field_ids = _identifiers(exact["field_ids"], f"{field}.exact_text_ownership.field_ids")
    preserve = _boolean(
        exact["preserve_verbatim"], f"{field}.exact_text_ownership.preserve_verbatim"
    )
    if owner == "not_applicable" and preserve:
        raise CorpusError(f"{field}.exact_text_ownership cannot preserve text when not_applicable")
    if owner == "not_applicable" and language_class != "not_applicable":
        raise CorpusError(f"{field}.exact_text_ownership language_class must be not_applicable")
    if owner != "not_applicable" and language_class == "not_applicable":
        raise CorpusError(f"{field}.exact_text_ownership language_class cannot be not_applicable")
    if (
        expected_language is not None
        and owner in {"user_declared", "annotator_transcribed", "synthetic_ground_truth"}
        and language_class != expected_language
    ):
        raise CorpusError(f"{field}.exact_text_ownership language_class must match case language")
    if (
        owner in {"user_declared", "annotator_transcribed", "synthetic_ground_truth"}
        and not preserve
    ):
        raise CorpusError(f"{field}.exact_text_ownership must preserve declared exact text")
    return field_ids


def validate_corpus(value: object) -> Corpus:
    """Validate a decoded corpus specification and return summary metadata."""

    document = _object(value, "corpus")
    required = frozenset(
        {
            "schema",
            "corpus_id",
            "corpus_version",
            "updated_at",
            "purpose",
            "license_policy",
            "privacy_policy",
            "review_protocol",
            "coverage_policy",
            "strata",
            "partitions",
            "provenance",
            "cases",
        }
    )
    _keys(document, required, required, "corpus")
    if _string(document["schema"], "corpus.schema") != SCHEMA_ID:
        raise CorpusError("unsupported corpus schema")
    corpus_id = _string(document["corpus_id"], "corpus.corpus_id", pattern=IDENTIFIER_PATTERN)
    corpus_version = _string(document["corpus_version"], "corpus.corpus_version")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", corpus_version) is None:
        raise CorpusError("corpus.corpus_version must be semantic version text")
    updated_at = _date(document["updated_at"], "corpus.updated_at")
    _safe_text(document["purpose"], "corpus.purpose")
    _validate_policy(document)
    _validate_review(document)
    (
        coverage_languages,
        coverage_modalities,
        coverage_risks,
        coverage_timing,
        coverage_text_fields,
    ) = _validate_coverage(document)

    strata_value = document["strata"]
    if not isinstance(strata_value, list) or not strata_value or len(strata_value) > MAX_STRATA:
        raise CorpusError("corpus.strata is outside its bounded list range")
    strata_ids: set[str] = set()
    strata_modes: dict[str, set[str]] = {}
    strata_dimensions: dict[str, set[str]] = {}
    strata_languages: dict[str, set[str]] = {}
    strata_modalities: dict[str, set[str]] = {}
    strata_risks: dict[str, set[str]] = {}
    strata_timing: dict[str, set[str]] = {}
    for index, raw in enumerate(strata_value):
        field = f"strata[{index}]"
        stratum = _object(raw, field)
        _keys(
            stratum,
            frozenset(
                {
                    "stratum_id",
                    "task_modes",
                    "dimensions",
                    "languages",
                    "modalities",
                    "risk_classes",
                    "timing_semantics",
                }
            ),
            frozenset(
                {
                    "stratum_id",
                    "task_modes",
                    "dimensions",
                    "languages",
                    "modalities",
                    "risk_classes",
                    "timing_semantics",
                }
            ),
            field,
        )
        stratum_id = _string(
            stratum["stratum_id"], f"{field}.stratum_id", pattern=IDENTIFIER_PATTERN
        )
        if stratum_id in strata_ids:
            raise CorpusError(f"duplicate stratum ID: {stratum_id}")
        strata_ids.add(stratum_id)
        strata_modes[stratum_id] = set(
            _enum_list(stratum["task_modes"], f"{field}.task_modes", MODES, 5)
        )
        strata_dimensions[stratum_id] = set(
            _enum_list(stratum["dimensions"], f"{field}.dimensions", DIMENSIONS, 11)
        )
        strata_languages[stratum_id] = set(
            _enum_list(stratum["languages"], f"{field}.languages", LANGUAGES, 4)
        )
        strata_modalities[stratum_id] = set(
            _enum_list(stratum["modalities"], f"{field}.modalities", MODALITIES, 5)
        )
        strata_risks[stratum_id] = set(
            _enum_list(stratum["risk_classes"], f"{field}.risk_classes", RISK_CLASSES, 8)
        )
        strata_timing[stratum_id] = set(
            _enum_list(
                stratum["timing_semantics"], f"{field}.timing_semantics", TIMING_SEMANTICS, 3
            )
        )
    covered_modes = set().union(*strata_modes.values())
    covered_dimensions = set().union(*strata_dimensions.values())
    covered_languages = set().union(*strata_languages.values())
    covered_modalities = set().union(*strata_modalities.values())
    covered_risks = set().union(*strata_risks.values())
    covered_timing = set().union(*strata_timing.values())
    missing_modes = sorted(MODES.difference(covered_modes))
    missing_dimensions = sorted(DIMENSIONS.difference(covered_dimensions))
    if missing_modes:
        raise CorpusError(f"strata omit task modes: {', '.join(missing_modes)}")
    if missing_dimensions:
        raise CorpusError(f"strata omit required dimensions: {', '.join(missing_dimensions)}")
    for label, expected, actual in (
        ("languages", coverage_languages, covered_languages),
        ("modalities", coverage_modalities, covered_modalities),
        ("risk_classes", coverage_risks, covered_risks),
        ("timing_semantics", coverage_timing, covered_timing),
    ):
        missing = sorted(expected.difference(actual))
        if missing:
            raise CorpusError(f"strata omit coverage_policy.{label}: {', '.join(missing)}")

    partitions_value = document["partitions"]
    if (
        not isinstance(partitions_value, list)
        or not partitions_value
        or len(partitions_value) > MAX_PARTITIONS
    ):
        raise CorpusError("corpus.partitions is outside its bounded list range")
    partitions: dict[str, tuple[str, bool, bool]] = {}
    for index, raw in enumerate(partitions_value):
        field = f"partitions[{index}]"
        partition = _object(raw, field)
        _keys(
            partition,
            frozenset({"partition_id", "kind", "public", "oracle_allowed", "purpose"}),
            frozenset({"partition_id", "kind", "public", "oracle_allowed", "purpose"}),
            field,
        )
        partition_id = _string(
            partition["partition_id"], f"{field}.partition_id", pattern=IDENTIFIER_PATTERN
        )
        if partition_id in partitions:
            raise CorpusError(f"duplicate partition ID: {partition_id}")
        kind = _enum(partition["kind"], f"{field}.kind", PARTITION_KINDS)
        public = _boolean(partition["public"], f"{field}.public")
        oracle_allowed = _boolean(partition["oracle_allowed"], f"{field}.oracle_allowed")
        _safe_text(partition["purpose"], f"{field}.purpose")
        if kind == "restricted_oracle" and (public or not oracle_allowed):
            raise CorpusError(f"{field} restricted_oracle must be private and oracle-enabled")
        if kind != "restricted_oracle" and (not public or oracle_allowed):
            raise CorpusError(f"{field} public partitions must be public and oracle-disabled")
        partitions[partition_id] = (kind, public, oracle_allowed)
    partition_kinds = {item[0] for item in partitions.values()}
    if partition_kinds != PARTITION_KINDS:
        missing = sorted(PARTITION_KINDS.difference(partition_kinds))
        raise CorpusError(
            "partition set must contain train/dev/test/restricted_oracle; "
            f"missing {', '.join(missing)}"
        )

    provenance_value = document["provenance"]
    if (
        not isinstance(provenance_value, list)
        or not provenance_value
        or len(provenance_value) > MAX_PROVENANCE
    ):
        raise CorpusError("corpus.provenance is outside its bounded list range")
    provenance: dict[str, tuple[str, str]] = {}
    for index, raw in enumerate(provenance_value):
        field = f"provenance[{index}]"
        item = _object(raw, field)
        _keys(
            item,
            frozenset({"provenance_id", "kind", "rights_status", "license_ref", "source_ref"}),
            frozenset(
                {
                    "provenance_id",
                    "kind",
                    "rights_status",
                    "license_ref",
                    "source_ref",
                    "source_url",
                }
            ),
            field,
        )
        provenance_id = _string(
            item["provenance_id"], f"{field}.provenance_id", pattern=IDENTIFIER_PATTERN
        )
        if provenance_id in provenance:
            raise CorpusError(f"duplicate provenance ID: {provenance_id}")
        kind = _enum(item["kind"], f"{field}.kind", PROVENANCE_KINDS)
        rights = _enum(
            item["rights_status"],
            f"{field}.rights_status",
            frozenset({"reviewed", "restricted", "pending", "prohibited"}),
        )
        if kind == "authorized_oracle" and rights != "restricted":
            raise CorpusError(f"{field} authorized_oracle provenance must be restricted")
        if kind != "authorized_oracle" and rights != "reviewed":
            raise CorpusError(f"{field} non-oracle provenance must have reviewed rights")
        _string(item["license_ref"], f"{field}.license_ref", pattern=IDENTIFIER_PATTERN)
        _string(item["source_ref"], f"{field}.source_ref", pattern=IDENTIFIER_PATTERN)
        if "source_url" in item:
            _validate_url(item["source_url"], f"{field}.source_url")
        provenance[provenance_id] = (kind, rights)

    cases_value = document["cases"]
    if not isinstance(cases_value, list) or not cases_value or len(cases_value) > MAX_CASES:
        raise CorpusError("corpus.cases is outside its bounded list range")
    case_ids: set[str] = set()
    source_partitions: dict[str, str] = {}
    family_partitions: dict[str, str] = {}
    fingerprint_partitions: dict[str, str] = {}
    observed_languages: set[str] = set()
    observed_modalities: set[str] = set()
    observed_risks: set[str] = set()
    observed_timing: set[str] = set()
    observed_exact_text_fields: set[str] = set()
    for index, raw in enumerate(cases_value):
        field = f"cases[{index}]"
        case = _object(raw, field)
        required_case = frozenset(
            {
                "case_id",
                "source_bundle_id",
                "derivation_family_id",
                "partition_id",
                "stratum_ids",
                "task_mode",
                "language",
                "modalities",
                "risk_classes",
                "timing",
                "provenance_id",
                "privacy_class",
                "media_fingerprints",
                "review_eligible",
                "annotation",
            }
        )
        _keys(case, required_case, required_case, field)
        case_id = _string(case["case_id"], f"{field}.case_id", pattern=IDENTIFIER_PATTERN)
        if case_id in case_ids:
            raise CorpusError(f"duplicate case ID: {case_id}")
        case_ids.add(case_id)
        source_bundle = _string(
            case["source_bundle_id"], f"{field}.source_bundle_id", pattern=IDENTIFIER_PATTERN
        )
        family = _string(
            case["derivation_family_id"],
            f"{field}.derivation_family_id",
            pattern=IDENTIFIER_PATTERN,
        )
        partition_id = _string(
            case["partition_id"], f"{field}.partition_id", pattern=IDENTIFIER_PATTERN
        )
        if partition_id not in partitions:
            raise CorpusError(f"{field}.partition_id references an unknown partition")
        kind, public, oracle_allowed = partitions[partition_id]
        for label, value, registry in (
            ("source_bundle_id", source_bundle, source_partitions),
            ("derivation_family_id", family, family_partitions),
        ):
            previous = registry.get(value)
            if previous is not None and previous != partition_id:
                raise CorpusError(f"{field}.{label} leaks across partitions: {value}")
            registry[value] = partition_id
        stratum_ids = _identifiers(case["stratum_ids"], f"{field}.stratum_ids")
        unknown_strata = sorted(set(stratum_ids).difference(strata_ids))
        if unknown_strata:
            raise CorpusError(
                f"{field}.stratum_ids references unknown strata: {', '.join(unknown_strata)}"
            )
        task_mode = _enum(case["task_mode"], f"{field}.task_mode", MODES)
        if not any(task_mode in strata_modes[stratum_id] for stratum_id in stratum_ids):
            raise CorpusError(f"{field}.task_mode is not covered by its strata")
        language = _enum(case["language"], f"{field}.language", LANGUAGES)
        modalities = set(_enum_list(case["modalities"], f"{field}.modalities", MODALITIES, 5))
        risk_classes = set(
            _enum_list(case["risk_classes"], f"{field}.risk_classes", RISK_CLASSES, 8)
        )
        timing = _enum(case["timing"], f"{field}.timing", TIMING_SEMANTICS)
        stratum_language_union = set().union(
            *(strata_languages[stratum_id] for stratum_id in stratum_ids)
        )
        stratum_modality_union = set().union(
            *(strata_modalities[stratum_id] for stratum_id in stratum_ids)
        )
        stratum_risk_union = set().union(*(strata_risks[stratum_id] for stratum_id in stratum_ids))
        stratum_timing_union = set().union(
            *(strata_timing[stratum_id] for stratum_id in stratum_ids)
        )
        if language not in stratum_language_union:
            raise CorpusError(f"{field}.language is not covered by its strata")
        if not modalities.issubset(stratum_modality_union):
            raise CorpusError(f"{field}.modalities are not covered by its strata")
        if not risk_classes.issubset(stratum_risk_union):
            raise CorpusError(f"{field}.risk_classes are not covered by its strata")
        if timing not in stratum_timing_union:
            raise CorpusError(f"{field}.timing is not covered by its strata")
        observed_languages.add(language)
        observed_modalities.update(modalities)
        observed_risks.update(risk_classes)
        observed_timing.add(timing)
        provenance_id = _string(
            case["provenance_id"], f"{field}.provenance_id", pattern=IDENTIFIER_PATTERN
        )
        if provenance_id not in provenance:
            raise CorpusError(f"{field}.provenance_id references an unknown provenance entry")
        provenance_kind, _rights = provenance[provenance_id]
        privacy_class = _enum(
            case["privacy_class"],
            f"{field}.privacy_class",
            frozenset({"synthetic", "licensed_public", "restricted_oracle"}),
        )
        if kind == "restricted_oracle":
            if privacy_class != "restricted_oracle" or provenance_kind != "authorized_oracle":
                raise CorpusError(f"{field} restricted_oracle case lacks restricted provenance")
            _boolean(case["review_eligible"], f"{field}.review_eligible", False)
        else:
            if privacy_class == "restricted_oracle" or provenance_kind == "authorized_oracle":
                raise CorpusError(
                    f"{field} public partition cannot contain restricted oracle material"
                )
            _boolean(case["review_eligible"], f"{field}.review_eligible")
        fingerprints = case["media_fingerprints"]
        if not isinstance(fingerprints, list) or not fingerprints or len(fingerprints) > 64:
            raise CorpusError(f"{field}.media_fingerprints is outside its bounded list range")
        seen_case_fingerprints: set[str] = set()
        for fp_index, fingerprint in enumerate(fingerprints):
            fp = _string(fingerprint, f"{field}.media_fingerprints[{fp_index}]")
            if FINGERPRINT_PATTERN.fullmatch(fp) is None:
                raise CorpusError(
                    f"{field}.media_fingerprints[{fp_index}] must be a SHA-256 fingerprint"
                )
            if fp in seen_case_fingerprints:
                raise CorpusError(f"{field}.media_fingerprints contains duplicates")
            seen_case_fingerprints.add(fp)
            previous = fingerprint_partitions.get(fp)
            if previous is not None and previous != partition_id:
                raise CorpusError(f"{field}.media_fingerprints leaks across partitions")
            fingerprint_partitions[fp] = partition_id
        observed_exact_text_fields.update(
            _validate_annotation(
                case["annotation"], f"{field}.annotation", expected_language=language
            )
        )

    for label, expected, actual in (
        ("languages", coverage_languages, observed_languages),
        ("modalities", coverage_modalities, observed_modalities),
        ("risk_classes", coverage_risks, observed_risks),
        ("timing_semantics", coverage_timing, observed_timing),
        ("exact_text_fields", coverage_text_fields, observed_exact_text_fields),
    ):
        missing = sorted(expected.difference(actual))
        if missing:
            raise CorpusError(f"cases omit coverage_policy.{label}: {', '.join(missing)}")

    return Corpus(
        corpus_id,
        corpus_version,
        updated_at,
        len(strata_value),
        len(partitions),
        len(provenance),
        len(cases_value),
        tuple(sorted(covered_modes)),
        tuple(sorted(covered_dimensions)),
    )


def load_corpus(path: Path) -> Corpus:
    """Read and validate a bounded UTF-8 JSON corpus file without following a symlink."""

    if path.is_symlink():
        raise CorpusError("corpus path must not be a symlink")
    try:
        if not path.is_file():
            raise CorpusError("corpus file does not exist")
        if path.stat().st_size > MAX_CORPUS_BYTES:
            raise CorpusError(f"corpus exceeds the {MAX_CORPUS_BYTES}-byte limit")
        payload = path.read_bytes()
    except CorpusError:
        raise
    except OSError as exc:
        raise CorpusError("corpus file cannot be read") from exc
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusError("corpus must be UTF-8 JSON") from exc
    return validate_corpus(document)


def inspect_corpus(path: Path) -> CorpusReport:
    """Validate a corpus and return a deterministic, redacted summary."""

    try:
        corpus = load_corpus(path)
    except CorpusError as exc:
        return CorpusReport(SCHEMA_ID, None, None, "INVALID", 0, 0, 0, 0, (str(exc),))
    return CorpusReport(
        SCHEMA_ID,
        corpus.corpus_id,
        corpus.corpus_version,
        "PASS",
        corpus.stratum_count,
        corpus.partition_count,
        corpus.provenance_count,
        corpus.case_count,
        (),
    )


def render_json(report: CorpusReport) -> str:
    """Render a stable compact JSON report."""

    return json.dumps(report.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path, help="path to corpus governance JSON")
    parser.add_argument("--json", action="store_true", help="emit one deterministic JSON report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the validator; malformed or unsafe metadata returns a non-zero status."""

    arguments = _parser().parse_args(argv)
    report = inspect_corpus(arguments.corpus)
    if arguments.json:
        print(render_json(report))
    else:
        print(f"{report.status}: {arguments.corpus}")
        for diagnostic in report.diagnostics:
            print(f"- {diagnostic}")
    return 0 if report.status == "PASS" else 4


if __name__ == "__main__":
    sys.exit(main())
