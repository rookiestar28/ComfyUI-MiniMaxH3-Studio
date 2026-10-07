"""Validate the M16-01 source/API/model/host requalification ledger offline.

The ledger is a content-free, versioned disposition record.  It never fetches URLs, imports a
runtime, opens media, downloads a model, or promotes latest-compatible observations to the pinned
supported profile.
"""

# The frozen source table keeps full URLs, hashes, and redacted claim text together so the
# machine-readable disposition remains auditable; E501 is intentionally disabled for this file.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "h3-context-source-requalification/1"
ITEM = "M16-01"
VERSION = "1.0.0"
RETRIEVED_AT = "2026-08-11"
BASELINE_FIXTURE = "tests/fixtures/m14_09_source_drift_checkpoint.json"
BASELINE_SHA256 = (
    "f1cb7e797c09deefb8b99ba9d0f1df0eb8eae9684ff50fbde4426db134bd75b7"  # pragma: allowlist secret
)

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REVISION = re.compile(r"(?:(?:git|gitblob|sha256):[0-9a-f]{40,64}|unavailable)\Z")
_URL = re.compile(r"https://[^\s?#@\\]+\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED_GROUPS = frozenset(
    {
        "minimax_repository",
        "minimax_model",
        "minimax_api",
        "comfyui_core",
        "comfyui_frontend",
        "comfyui_native_h3",
        "comfyui_docs",
        "ollama_docs",
        "ollama_server",
        "ollama_python_client",
    }
)
_ALLOWED_DISPOSITIONS = frozenset(
    {"EXACT_MATCH", "ADDITIVE_NON_SUBSTITUTING", "BLOCKED_PENDING_REQUALIFICATION"}
)
_ALLOWED_SEVERITIES = frozenset({"unchanged", "additive", "material"})
_ALLOWED_CONFIDENCE = frozenset({"high", "medium", "low", "unknown"})


class M16RequalificationError(ValueError):
    """Raised when an M16-01 ledger is incomplete, inconsistent, or claim-escalating."""


@dataclass(frozen=True, slots=True)
class RequalificationResult:
    status: str
    item: str
    supported_profile_status: str
    latest_substitution_authorized: bool
    network_performed: bool
    model_downloaded: bool
    media_read: bool
    fingerprint: str

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": "h3-context-source-requalification-report/1",
            "status": self.status,
            "item": self.item,
            "supported_profile_status": self.supported_profile_status,
            "latest_substitution_authorized": self.latest_substitution_authorized,
            "network_performed": self.network_performed,
            "model_downloaded": self.model_downloaded,
            "media_read": self.media_read,
            "fingerprint": self.fingerprint,
        }


def _object(
    value: object, field: str, *, required: set[str], allowed: set[str]
) -> dict[str, object]:
    if type(value) is not dict:
        raise M16RequalificationError(f"{field} must be an object")
    result = cast(dict[str, object], value)
    missing = sorted(required.difference(result))
    unknown = sorted(set(result).difference(allowed))
    if missing:
        raise M16RequalificationError(f"{field} is missing required fields: {', '.join(missing)}")
    if unknown:
        raise M16RequalificationError(f"{field} contains unsupported fields: {', '.join(unknown)}")
    return result


def _string(
    value: object, field: str, *, pattern: re.Pattern[str] | None = None, maximum: int = 4096
) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise M16RequalificationError(f"{field} must be bounded text")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise M16RequalificationError(f"{field} has an invalid format")
    return value


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise M16RequalificationError(f"{field} must be an exact boolean")
    return value


def _list(value: object, field: str, *, maximum: int = 64) -> list[object]:
    if type(value) is not list or len(value) > maximum:
        raise M16RequalificationError(f"{field} must be a bounded list")
    return cast(list[object], value)


def _string_list(value: object, field: str, *, maximum: int = 32) -> tuple[str, ...]:
    values = _list(value, field, maximum=maximum)
    result = tuple(
        _string(item, f"{field}[{index}]", pattern=_ID, maximum=128)
        for index, item in enumerate(values)
    )
    if len(result) != len(set(result)):
        raise M16RequalificationError(f"{field} contains duplicate values")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_baseline(value: object, repo_root: Path) -> None:
    baseline = _object(
        value,
        "baseline",
        required={"checkpoint_id", "fixture", "fingerprint"},
        allowed={"checkpoint_id", "fixture", "fingerprint"},
    )
    if (
        _string(baseline["checkpoint_id"], "baseline.checkpoint_id", pattern=_ID, maximum=128)
        != "m14_09"
    ):
        raise M16RequalificationError("baseline.checkpoint_id must remain m14_09")
    fixture = _string(baseline["fixture"], "baseline.fixture", maximum=512)
    if fixture != BASELINE_FIXTURE or "\\" in fixture or ".." in Path(fixture).parts:
        raise M16RequalificationError("baseline.fixture is not the accepted M14-09 fixture")
    raw_path = repo_root / Path(fixture)
    if raw_path.is_symlink():
        raise M16RequalificationError("baseline.fixture must not be a symlink")
    path = raw_path.resolve()
    if not path.is_file() or _sha256_file(path) != BASELINE_SHA256:
        raise M16RequalificationError("baseline.fixture bytes do not match M14-09")
    if (
        _string(baseline["fingerprint"], "baseline.fingerprint", pattern=_REVISION, maximum=80)
        != "sha256:" + BASELINE_SHA256
    ):
        raise M16RequalificationError("baseline.fingerprint is not the accepted M14-09 digest")


def _validate_supported_profile(value: object) -> dict[str, object]:
    profile = _object(
        value,
        "supported_profile",
        required={
            "core_version",
            "core_revision",
            "frontend_version",
            "frontend_revision",
            "node_api",
            "native_h3_source",
            "status",
            "latest_substitution_authorized",
            "manual_only_scoped",
            "prompt_boundary",
            "autogrow_transport",
            "presentation_labels",
            "h3_execution_status",
        },
        allowed={
            "core_version",
            "core_revision",
            "frontend_version",
            "frontend_revision",
            "node_api",
            "native_h3_source",
            "status",
            "latest_substitution_authorized",
            "manual_only_scoped",
            "prompt_boundary",
            "autogrow_transport",
            "presentation_labels",
            "h3_execution_status",
        },
    )
    expected_strings = {
        "core_version": "0.30.0",
        "core_revision": "c44dea18809e3ca0e12e25cbe6938c2c45a29c9d",
        "frontend_version": "1.47.12",
        "frontend_revision": "f339c6b2ec0cc90e1c4fa717d9f7d6d565a8f6da",
        "node_api": "V1_ONLY",
        "native_h3_source": "22bc91cd4570a071c664ec4848a5be748909e1e7",
        "status": "QUALIFIED_UNCHANGED",
        "prompt_boundary": "STRING",
        "autogrow_transport": "fully_qualified_zero_based",
        "presentation_labels": "one_based",
        "h3_execution_status": "NOT_IMPLEMENTED",
    }
    for field, expected in expected_strings.items():
        if _string(profile[field], f"supported_profile.{field}", maximum=256) != expected:
            raise M16RequalificationError(f"supported_profile.{field} is not the accepted value")
    if _bool(
        profile["latest_substitution_authorized"],
        "supported_profile.latest_substitution_authorized",
    ):
        raise M16RequalificationError(
            "supported_profile.latest_substitution_authorized must be false"
        )
    if not _bool(profile["manual_only_scoped"], "supported_profile.manual_only_scoped"):
        raise M16RequalificationError("supported_profile.manual_only_scoped must remain true")
    return profile


def _validate_surfaces(value: object) -> tuple[dict[str, object], ...]:
    surfaces = _list(value, "surfaces", maximum=32)
    if not surfaces:
        raise M16RequalificationError("surfaces must not be empty")
    result: list[dict[str, object]] = []
    ids: set[str] = set()
    for index, raw in enumerate(surfaces):
        surface = _object(
            raw,
            f"surfaces[{index}]",
            required={
                "surface_id",
                "group",
                "url",
                "baseline_revision",
                "observed_revision",
                "observed_fingerprint",
                "retrieved_at",
                "evidence_class",
                "confidence",
                "material",
                "disposition",
                "affected_seams",
                "requalification_owner",
            },
            allowed={
                "surface_id",
                "group",
                "url",
                "baseline_revision",
                "observed_revision",
                "observed_fingerprint",
                "retrieved_at",
                "evidence_class",
                "confidence",
                "material",
                "disposition",
                "affected_seams",
                "requalification_owner",
            },
        )
        field = f"surfaces[{index}]"
        surface_id = _string(surface["surface_id"], f"{field}.surface_id", pattern=_ID, maximum=128)
        if surface_id in ids:
            raise M16RequalificationError(f"{field}.surface_id is duplicated")
        ids.add(surface_id)
        if _string(surface["group"], f"{field}.group", maximum=64) not in _ALLOWED_GROUPS:
            raise M16RequalificationError(f"{field}.group is unsupported")
        if _URL.fullmatch(_string(surface["url"], f"{field}.url", maximum=2048)) is None:
            raise M16RequalificationError(f"{field}.url is not an allowed HTTPS locator")
        for name in ("baseline_revision", "observed_revision", "observed_fingerprint"):
            _string(surface[name], f"{field}.{name}", pattern=_REVISION, maximum=80)
        if _string(surface["retrieved_at"], f"{field}.retrieved_at", maximum=32) != RETRIEVED_AT:
            raise M16RequalificationError(f"{field}.retrieved_at is not the frozen retrieval date")
        if _string(surface["evidence_class"], f"{field}.evidence_class", maximum=64) not in {
            "official",
            "framework_reference",
        }:
            raise M16RequalificationError(f"{field}.evidence_class is not authoritative")
        if (
            _string(surface["confidence"], f"{field}.confidence", maximum=16)
            not in _ALLOWED_CONFIDENCE
        ):
            raise M16RequalificationError(f"{field}.confidence is unsupported")
        _bool(surface["material"], f"{field}.material")
        disposition = _string(surface["disposition"], f"{field}.disposition", maximum=64)
        if disposition not in _ALLOWED_DISPOSITIONS:
            raise M16RequalificationError(f"{field}.disposition is unsupported")
        _string_list(surface["affected_seams"], f"{field}.affected_seams")
        _string(
            surface["requalification_owner"],
            f"{field}.requalification_owner",
            pattern=_ID,
            maximum=128,
        )
        result.append(surface)
    expected_ids = {
        "minimax.repository",
        "minimax.guide",
        "minimax.model",
        "minimax.model.license",
        "minimax.model.index",
        "comfyui.core",
        "comfyui.text_generate",
        "comfyui.clip_loader",
        "comfyui.model_family",
        "comfyui.native_h3",
        "comfyui.frontend",
        "comfyui.docs.v3",
        "comfyui.docs.sidebar",
        "comfyui.docs.app_mode",
        "comfyui.docs.subgraph_blueprints",
        "comfyui.docs.subgraphs",
        "ollama.server",
        "ollama.python_client",
        "ollama.docs.introduction",
        "ollama.docs.chat",
        "ollama.docs.tags",
        "ollama.docs.show",
        "ollama.docs.ps",
        "ollama.docs.structured_outputs",
    }
    if ids != expected_ids:
        raise M16RequalificationError("surfaces do not cover the closed M16-01 inventory")
    return tuple(result)


def _require_frozen_section(
    actual: object,
    expected: object,
    field: str,
    *,
    identifier_key: str,
) -> None:
    actual_list = _list(actual, field, maximum=64)
    expected_list = _list(expected, f"expected.{field}", maximum=64)
    actual_map: dict[str, object] = {}
    expected_map: dict[str, object] = {}
    for index, item in enumerate(actual_list):
        entry = _object(
            item,
            f"{field}[{index}]",
            required={identifier_key},
            allowed=set(item) if type(item) is dict else {identifier_key},
        )
        identifier = _string(
            entry[identifier_key], f"{field}[{index}].{identifier_key}", pattern=_ID, maximum=128
        )
        if identifier in actual_map:
            raise M16RequalificationError(f"{field}[{index}].{identifier_key} is duplicated")
        actual_map[identifier] = entry
    for index, item in enumerate(expected_list):
        entry = _object(
            item,
            f"expected.{field}[{index}]",
            required={identifier_key},
            allowed=set(item) if type(item) is dict else {identifier_key},
        )
        identifier = _string(
            entry[identifier_key],
            f"expected.{field}[{index}].{identifier_key}",
            pattern=_ID,
            maximum=128,
        )
        expected_map[identifier] = entry
    if set(actual_map) != set(expected_map):
        raise M16RequalificationError(f"{field} does not match the closed record inventory")
    for identifier, expected_entry in expected_map.items():
        if actual_map[identifier] != expected_entry:
            raise M16RequalificationError(f"{field}[{identifier}] is not the frozen disposition")


def _validate_schema_diffs(value: object, surface_ids: set[str]) -> None:
    diffs = _list(value, "schema_diffs", maximum=32)
    if not diffs:
        raise M16RequalificationError("schema_diffs must not be empty")
    ids: set[str] = set()
    for index, raw in enumerate(diffs):
        diff = _object(
            raw,
            f"schema_diffs[{index}]",
            required={
                "diff_id",
                "surface_id",
                "component",
                "baseline_fingerprint",
                "observed_fingerprint",
                "severity",
                "disposition",
                "details",
                "affected_profiles",
            },
            allowed={
                "diff_id",
                "surface_id",
                "component",
                "baseline_fingerprint",
                "observed_fingerprint",
                "severity",
                "disposition",
                "details",
                "affected_profiles",
            },
        )
        field = f"schema_diffs[{index}]"
        diff_id = _string(diff["diff_id"], f"{field}.diff_id", pattern=_ID, maximum=128)
        if diff_id in ids:
            raise M16RequalificationError(f"{field}.diff_id is duplicated")
        ids.add(diff_id)
        surface_id = _string(diff["surface_id"], f"{field}.surface_id", pattern=_ID, maximum=128)
        if surface_id not in surface_ids:
            raise M16RequalificationError(f"{field}.surface_id is not declared")
        _string(diff["component"], f"{field}.component", maximum=256)
        _string(
            diff["baseline_fingerprint"],
            f"{field}.baseline_fingerprint",
            pattern=_REVISION,
            maximum=80,
        )
        _string(
            diff["observed_fingerprint"],
            f"{field}.observed_fingerprint",
            pattern=_REVISION,
            maximum=80,
        )
        severity = _string(diff["severity"], f"{field}.severity", maximum=32)
        if severity not in _ALLOWED_SEVERITIES:
            raise M16RequalificationError(f"{field}.severity is unsupported")
        disposition = _string(diff["disposition"], f"{field}.disposition", maximum=64)
        if disposition not in _ALLOWED_DISPOSITIONS and disposition != "UNCHANGED_CONTRACT":
            raise M16RequalificationError(f"{field}.disposition is unsupported")
        _string(diff["details"], f"{field}.details", maximum=1024)
        _string_list(diff["affected_profiles"], f"{field}.affected_profiles")


def _validate_records(
    value: object, field_name: str, *, kind: str
) -> tuple[dict[str, object], ...]:
    records = _list(value, field_name, maximum=32)
    if not records:
        raise M16RequalificationError(f"{field_name} must not be empty")
    result: list[dict[str, object]] = []
    ids: set[str] = set()
    for index, raw in enumerate(records):
        record = _object(
            raw,
            f"{field_name}[{index}]",
            required={
                "record_id",
                "statement",
                "status",
                "confidence",
                "source_ids",
                "affected_profiles",
            },
            allowed={
                "record_id",
                "statement",
                "status",
                "confidence",
                "source_ids",
                "affected_profiles",
            },
        )
        field = f"{field_name}[{index}]"
        record_id = _string(record["record_id"], f"{field}.record_id", pattern=_ID, maximum=128)
        if record_id in ids:
            raise M16RequalificationError(f"{field}.record_id is duplicated")
        ids.add(record_id)
        _string(record["statement"], f"{field}.statement", maximum=4096)
        status = _string(record["status"], f"{field}.status", maximum=32)
        if status not in {"accepted", "blocked", "open", "resolved"}:
            raise M16RequalificationError(f"{field}.status is unsupported")
        if kind == "claim" and record_id == "product.scope":
            if status != "resolved" or record["statement"] != "MANUAL_ONLY_SCOPED":
                raise M16RequalificationError("claims.product.scope contradicts the claim ceiling")
        _string(record["confidence"], f"{field}.confidence", maximum=16)
        _string_list(record["source_ids"], f"{field}.source_ids")
        _string_list(record["affected_profiles"], f"{field}.affected_profiles")
        result.append(record)
    return tuple(result)


def _validate_attributions(value: object) -> None:
    records = _list(value, "attributions", maximum=16)
    expected = {"minimax_h3", "qwen3_vl", "ollama", "comfyui", "project"}
    ids: set[str] = set()
    for index, raw in enumerate(records):
        record = _object(
            raw,
            f"attributions[{index}]",
            required={"attribution_id", "subject", "license", "source_ids", "status"},
            allowed={"attribution_id", "subject", "license", "source_ids", "status"},
        )
        field = f"attributions[{index}]"
        attribution_id = _string(
            record["attribution_id"], f"{field}.attribution_id", pattern=_ID, maximum=128
        )
        ids.add(attribution_id)
        _string(record["subject"], f"{field}.subject", maximum=256)
        _string(record["license"], f"{field}.license", maximum=256)
        _string_list(record["source_ids"], f"{field}.source_ids")
        if _string(record["status"], f"{field}.status", maximum=32) != "recorded":
            raise M16RequalificationError(f"{field}.status must be recorded")
    if ids != expected:
        raise M16RequalificationError("attributions do not cover the closed license inventory")


def validate_requalification(value: object, repo_root: Path = ROOT) -> RequalificationResult:
    document = _object(
        value,
        "ledger",
        required={
            "schema",
            "item",
            "version",
            "updated_at",
            "baseline",
            "supported_profile",
            "surfaces",
            "schema_diffs",
            "claims",
            "unknowns",
            "attributions",
            "guards",
        },
        allowed={
            "schema",
            "item",
            "version",
            "updated_at",
            "baseline",
            "supported_profile",
            "surfaces",
            "schema_diffs",
            "claims",
            "unknowns",
            "attributions",
            "guards",
        },
    )
    if _string(document["schema"], "schema", maximum=128) != SCHEMA:
        raise M16RequalificationError("schema is unsupported")
    if _string(document["item"], "item", pattern=_ID, maximum=32) != ITEM:
        raise M16RequalificationError("item must be M16-01")
    if _string(document["version"], "version", maximum=32) != VERSION:
        raise M16RequalificationError("version is unsupported")
    if _string(document["updated_at"], "updated_at", maximum=32) != RETRIEVED_AT:
        raise M16RequalificationError("updated_at is not the frozen retrieval date")
    _validate_baseline(document["baseline"], repo_root)
    profile = _validate_supported_profile(document["supported_profile"])
    surfaces = _validate_surfaces(document["surfaces"])
    surface_ids = {cast(str, item["surface_id"]) for item in surfaces}
    _validate_schema_diffs(document["schema_diffs"], surface_ids)
    claims = _validate_records(document["claims"], "claims", kind="claim")
    unknowns = _validate_records(document["unknowns"], "unknowns", kind="unknown")
    for field, records in (("claims", claims), ("unknowns", unknowns)):
        for index, record in enumerate(records):
            for source_id in cast(list[object], record["source_ids"]):
                if cast(str, source_id) not in surface_ids:
                    raise M16RequalificationError(
                        f"{field}[{index}].source_ids references an undeclared surface"
                    )
    _validate_attributions(document["attributions"])
    guards = _object(
        document["guards"],
        "guards",
        required={
            "network_performed",
            "model_downloaded",
            "media_read",
            "provider_called",
            "credentials_used",
            "paid_calls",
            "latest_substitution_authorized",
        },
        allowed={
            "network_performed",
            "model_downloaded",
            "media_read",
            "provider_called",
            "credentials_used",
            "paid_calls",
            "latest_substitution_authorized",
        },
    )
    for key in guards:
        if not _bool(guards[key], f"guards.{key}"):
            continue
        if key in {
            "network_performed",
            "model_downloaded",
            "media_read",
            "provider_called",
            "credentials_used",
            "paid_calls",
            "latest_substitution_authorized",
        }:
            raise M16RequalificationError(f"guards.{key} must be false for this item")
    expected = build_default_requalification()
    _require_frozen_section(
        document["surfaces"], expected["surfaces"], "surfaces", identifier_key="surface_id"
    )
    _require_frozen_section(
        document["schema_diffs"], expected["schema_diffs"], "schema_diffs", identifier_key="diff_id"
    )
    _require_frozen_section(
        document["claims"], expected["claims"], "claims", identifier_key="record_id"
    )
    _require_frozen_section(
        document["unknowns"], expected["unknowns"], "unknowns", identifier_key="record_id"
    )
    _require_frozen_section(
        document["attributions"],
        expected["attributions"],
        "attributions",
        identifier_key="attribution_id",
    )
    if document["guards"] != expected["guards"]:
        raise M16RequalificationError("guards are not the frozen no-side-effect disposition")
    fingerprint = _canonical_fingerprint(document)
    return RequalificationResult(
        status="PASS",
        item=ITEM,
        supported_profile_status=cast(str, profile["status"]),
        latest_substitution_authorized=cast(bool, profile["latest_substitution_authorized"]),
        network_performed=cast(bool, guards["network_performed"]),
        model_downloaded=cast(bool, guards["model_downloaded"]),
        media_read=cast(bool, guards["media_read"]),
        fingerprint=fingerprint,
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise M16RequalificationError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise M16RequalificationError(f"non-finite JSON constant {value!r}")


def load_requalification(path: Path, repo_root: Path = ROOT) -> RequalificationResult:
    if not path.is_file() or path.is_symlink():
        raise M16RequalificationError("ledger path is missing or unsafe")
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_constant
        )
    except M16RequalificationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M16RequalificationError("ledger is not strict UTF-8 JSON") from exc
    return validate_requalification(value, repo_root)


def _surface(
    surface_id: str,
    group: str,
    url: str,
    baseline_revision: str,
    observed_revision: str,
    observed_fingerprint: str,
    disposition: str,
    material: bool,
    affected_seams: list[str],
) -> dict[str, object]:
    return {
        "surface_id": surface_id,
        "group": group,
        "url": url,
        "baseline_revision": baseline_revision,
        "observed_revision": observed_revision,
        "observed_fingerprint": observed_fingerprint,
        "retrieved_at": RETRIEVED_AT,
        "evidence_class": "official",
        "confidence": "high",
        "material": material,
        "disposition": disposition,
        "affected_seams": affected_seams,
        "requalification_owner": ITEM,
    }


def build_default_requalification() -> dict[str, object]:
    surfaces = [
        _surface(
            "minimax.repository",
            "minimax_repository",
            "https://github.com/MiniMax-AI/MiniMax-H3/tree/05d91ff89f58b665e56424fd66db9ef0351b3015",
            "git:b7227fa6a6206e9fb30562383d39e53cf3866a48",
            "git:05d91ff89f58b665e56424fd66db9ef0351b3015",
            "sha256:b83769c3e1ca584461780017b6190f4d1b06d2e7bcdea7655b56bc1c4981e34e",
            "ADDITIVE_NON_SUBSTITUTING",
            True,
            ["prompt_guidance", "context_ir_boundary"],
        ),
        _surface(
            "minimax.guide",
            "minimax_repository",
            "https://github.com/MiniMax-AI/MiniMax-H3/blob/05d91ff89f58b665e56424fd66db9ef0351b3015/README.md",
            "sha256:6e4cfcfd85a796cf3c965a2dcc0cc328b59dd680d9718d1e0fef0add7aca8ada",
            "git:05d91ff89f58b665e56424fd66db9ef0351b3015",
            "sha256:b83769c3e1ca584461780017b6190f4d1b06d2e7bcdea7655b56bc1c4981e34e",
            "ADDITIVE_NON_SUBSTITUTING",
            True,
            ["prompt_guidance", "context_ir_boundary"],
        ),
        _surface(
            "minimax.model",
            "minimax_model",
            "https://huggingface.co/MiniMaxAI/MiniMax-H3/tree/9ac0dd7aabc2c651fcf0ace4c00b2bffd9c8c8a6",
            "git:6818f6c32d12b210915e44ad56a4228c2608f160",
            "git:9ac0dd7aabc2c651fcf0ace4c00b2bffd9c8c8a6",
            "sha256:5a587fe13b2371427415ac892463142683aefcd8d322e274a3a095eac37ac7d2",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["model_revision", "checkpoint_digest"],
        ),
        _surface(
            "minimax.model.license",
            "minimax_model",
            "https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/9ac0dd7aabc2c651fcf0ace4c00b2bffd9c8c8a6/LICENSE",
            "sha256:59b99642b95ea21630e311198ddbfffbfe05aadba0c2f5d884cbdf4efcc90f44",
            "sha256:59b99642b95ea21630e311198ddbfffbfe05aadba0c2f5d884cbdf4efcc90f44",
            "sha256:59b99642b95ea21630e311198ddbfffbfe05aadba0c2f5d884cbdf4efcc90f44",
            "EXACT_MATCH",
            False,
            ["license"],
        ),
        _surface(
            "minimax.model.index",
            "minimax_model",
            "https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/9ac0dd7aabc2c651fcf0ace4c00b2bffd9c8c8a6/model_index.json",
            "sha256:5a587fe13b2371427415ac892463142683aefcd8d322e274a3a095eac37ac7d2",
            "sha256:5a587fe13b2371427415ac892463142683aefcd8d322e274a3a095eac37ac7d2",
            "sha256:5a587fe13b2371427415ac892463142683aefcd8d322e274a3a095eac37ac7d2",
            "EXACT_MATCH",
            False,
            ["model_metadata"],
        ),
        _surface(
            "comfyui.core",
            "comfyui_core",
            "https://github.com/Comfy-Org/ComfyUI/tree/34744cd29eacea9bbdec17e628a81c2ce0737d16",
            "git:c44dea18809e3ca0e12e25cbe6938c2c45a29c9d",
            "git:34744cd29eacea9bbdec17e628a81c2ce0737d16",
            "git:34744cd29eacea9bbdec17e628a81c2ce0737d16",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["latest_host", "v3_api"],
        ),
        _surface(
            "comfyui.text_generate",
            "comfyui_core",
            "https://github.com/Comfy-Org/ComfyUI/blob/34744cd29eacea9bbdec17e628a81c2ce0737d16/comfy_extras/nodes_textgen.py",
            "gitblob:5a947d5c57b32e7e8d2ed1ef364064959d841cc4",
            "gitblob:5a947d5c57b32e7e8d2ed1ef364064959d841cc4",
            "sha256:b328e8a2dc89cfd3a93ab49c1be880a3e89ec4521eef506823753617c86c99e9",
            "EXACT_MATCH",
            False,
            ["text_generate", "model_family"],
        ),
        _surface(
            "comfyui.clip_loader",
            "comfyui_core",
            "https://github.com/Comfy-Org/ComfyUI/blob/34744cd29eacea9bbdec17e628a81c2ce0737d16/nodes.py",
            "gitblob:08529d8dc414138e56d0cc6e68367f95da70f038",
            "gitblob:432f04d8951acd75808e53a96b4eebfe89ef51aa",
            "sha256:aa7e2a87bb7c1b43273736eef9fcf4811cb55497c5bde2e201135b244b97431a",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["clip_loader", "latest_host"],
        ),
        _surface(
            "comfyui.model_family",
            "comfyui_core",
            "https://github.com/Comfy-Org/ComfyUI/blob/34744cd29eacea9bbdec17e628a81c2ce0737d16/comfy/model_detection.py",
            "gitblob:103680fd12d39395ed49258c04e0bcf49bcb6c56",
            "gitblob:103680fd12d39395ed49258c04e0bcf49bcb6c56",
            "sha256:3475b30e2c3995902b3cfefb82f4b9bfa1529022a33c7b8707791595bd3ef733",
            "EXACT_MATCH",
            False,
            ["model_family", "checkpoint_loader"],
        ),
        _surface(
            "comfyui.native_h3",
            "comfyui_native_h3",
            "https://github.com/Comfy-Org/ComfyUI/blob/34744cd29eacea9bbdec17e628a81c2ce0737d16/comfy_extras/nodes_minimax_h3.py",
            "gitblob:22bc91cd4570a071c664ec4848a5be748909e1e7",
            "git:34744cd29eacea9bbdec17e628a81c2ce0737d16",
            "sha256:f767df4074b908efb345f5a87c2fd263ba82c12e65bcca932846207cc213e064",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["sigma_sampler", "latest_native_workflow"],
        ),
        _surface(
            "comfyui.frontend",
            "comfyui_frontend",
            "https://github.com/Comfy-Org/ComfyUI_frontend/tree/36aec4afa2b71afca5ca41a1cc4293ec1682daf7",
            "git:f339c6b2ec0cc90e1c4fa717d9f7d6d565a8f6da",
            "git:36aec4afa2b71afca5ca41a1cc4293ec1682daf7",
            "sha256:fc7030a06777f25eb318b619ea493d3477f4fbd325a42933d296b536a8da1aba",
            "ADDITIVE_NON_SUBSTITUTING",
            True,
            ["latest_frontend", "sidebar_lifecycle"],
        ),
        _surface(
            "comfyui.docs.v3",
            "comfyui_docs",
            "https://docs.comfy.org/custom-nodes/v3_migration",
            "sha256:bc6c4a7bdafba5e0fcad23186ea0478a841b708451c924e0337302f8b5e677a5",
            "sha256:fafda2e4739a7d50d2cd70f3630385d8a2f8b72a62aa4de830f393484e3fd83f",
            "sha256:fafda2e4739a7d50d2cd70f3630385d8a2f8b72a62aa4de830f393484e3fd83f",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["v3_api", "hidden_fields"],
        ),
        _surface(
            "comfyui.docs.sidebar",
            "comfyui_docs",
            "https://docs.comfy.org/custom-nodes/js/javascript_sidebar_tabs",
            "sha256:da8f294db0ef5ccfd278e615495f7b2202b9a8d7e9067b22a9ba1b0c37c184f1",
            "sha256:d3faa5472a1ce636653eca58a25b3ee7dcaf7536c75ec8d8b7c1251085da3468",
            "sha256:d3faa5472a1ce636653eca58a25b3ee7dcaf7536c75ec8d8b7c1251085da3468",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["sidebar_lifecycle"],
        ),
        _surface(
            "comfyui.docs.app_mode",
            "comfyui_docs",
            "https://docs.comfy.org/interface/app-mode",
            "unavailable",
            "sha256:d9a0dc2420a40754dce4c01cc728cd56c0e638d5017d46f91b3c479870a71185",
            "sha256:d9a0dc2420a40754dce4c01cc728cd56c0e638d5017d46f91b3c479870a71185",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["app_mode", "queue_cancel"],
        ),
        _surface(
            "comfyui.docs.subgraph_blueprints",
            "comfyui_docs",
            "https://docs.comfy.org/custom-nodes/subgraph_blueprints",
            "unavailable",
            "sha256:cd572f821f1207a41e64a455ea3dab65d42ff7d0e24022591af6b0ea9479f6fe",
            "sha256:cd572f821f1207a41e64a455ea3dab65d42ff7d0e24022591af6b0ea9479f6fe",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["subgraph_blueprints", "dynamic_inputs"],
        ),
        _surface(
            "comfyui.docs.subgraphs",
            "comfyui_docs",
            "https://docs.comfy.org/custom-nodes/js/subgraphs",
            "unavailable",
            "sha256:6f45a143d5eaf3779443d6355b19c81bb48df0e25d2571122165e58ee955556b",
            "sha256:6f45a143d5eaf3779443d6355b19c81bb48df0e25d2571122165e58ee955556b",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["subgraph_traversal", "widget_promotion"],
        ),
        _surface(
            "ollama.server",
            "ollama_server",
            "https://github.com/ollama/ollama/tree/4f066a6fb0c05d7dcf68e02858a5ddd399af716a",
            "git:5a173edb63dbe5d1f555ae258a7930135d7c9336",
            "git:4f066a6fb0c05d7dcf68e02858a5ddd399af716a",
            "git:4f066a6fb0c05d7dcf68e02858a5ddd399af716a",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["ollama_server_api", "ollama_runtime_qualification"],
        ),
        _surface(
            "ollama.python_client",
            "ollama_python_client",
            "https://github.com/ollama/ollama-python/tree/25b93290d8cd07b0d00732641f812ee34fd4c989",
            "git:25b93290d8cd07b0d00732641f812ee34fd4c989",
            "git:25b93290d8cd07b0d00732641f812ee34fd4c989",
            "git:25b93290d8cd07b0d00732641f812ee34fd4c989",
            "EXACT_MATCH",
            False,
            ["python_client"],
        ),
        _surface(
            "ollama.docs.introduction",
            "ollama_docs",
            "https://docs.ollama.com/api/introduction",
            "sha256:a4f4f1d59067ebff92da446d1c92c80a2a4b6066e566d9c9dc0cba96ba877394",
            "sha256:929616d0b385d590e1217aabe3883717edbb9b6951461baca52d464eaf10c7b7",
            "sha256:929616d0b385d590e1217aabe3883717edbb9b6951461baca52d464eaf10c7b7",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["ollama_versioning", "cloud_boundary"],
        ),
        _surface(
            "ollama.docs.chat",
            "ollama_docs",
            "https://docs.ollama.com/api/chat",
            "sha256:309ef5fbdc781525eb04e091478c51e4e17be3f6baafb7bd4b6e3b23e726cb49",
            "sha256:da1ceed85db014e6a64db609bc932ddd5642e28bae5f05a2eb82be6c035dc4a8",
            "sha256:da1ceed85db014e6a64db609bc932ddd5642e28bae5f05a2eb82be6c035dc4a8",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["ollama_chat", "structured_output_validation"],
        ),
        _surface(
            "ollama.docs.tags",
            "ollama_docs",
            "https://docs.ollama.com/api/tags",
            "sha256:70b873546ce4cb19113f8de8ce3cd8fb22e36d130c80d2a753254f2a45e1b20f",
            "sha256:4daadba9d6263100f20e867c2a0c7c03304ca8d648235f65d82a14ec3345bba0",
            "sha256:4daadba9d6263100f20e867c2a0c7c03304ca8d648235f65d82a14ec3345bba0",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["ollama_model_inventory"],
        ),
        _surface(
            "ollama.docs.show",
            "ollama_docs",
            "https://docs.ollama.com/api-reference/show-model-details",
            "sha256:75e200fa9ec92986579c8fcc7319c38fd85dc1310e853ee20eeba1d1fb02d2ad",
            "sha256:9648e166574c05da6905e1364786a58ec604b0f0ee8415eee7658b3a88c356ce",
            "sha256:9648e166574c05da6905e1364786a58ec604b0f0ee8415eee7658b3a88c356ce",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["ollama_model_metadata"],
        ),
        _surface(
            "ollama.docs.ps",
            "ollama_docs",
            "https://docs.ollama.com/api/ps",
            "sha256:c7d9eb1ee9ecae4a275aa5b6a5ff209e4a303b32b3fc994d60aec853ef5fd8ee",
            "sha256:bd82a81bf71a02e1fb82a2daf7772cafd7d89379d8fd06e33b0d64d59bf202dc",
            "sha256:bd82a81bf71a02e1fb82a2daf7772cafd7d89379d8fd06e33b0d64d59bf202dc",
            "BLOCKED_PENDING_REQUALIFICATION",
            True,
            ["ollama_runtime_inventory"],
        ),
        _surface(
            "ollama.docs.structured_outputs",
            "ollama_docs",
            "https://docs.ollama.com/capabilities/structured-outputs",
            "sha256:4e21cd3eca241db09adb7775c98ca8b2e1878d76fde1684ef34531345df7aa6f",
            "sha256:7b310ff1136adb541cdaac08bbd86761bde1a6e04c63a7106cad9b9f93aa67c2",
            "sha256:7b310ff1136adb541cdaac08bbd86761bde1a6e04c63a7106cad9b9f93aa67c2",
            "ADDITIVE_NON_SUBSTITUTING",
            True,
            ["structured_output_validation"],
        ),
    ]
    return {
        "schema": SCHEMA,
        "item": ITEM,
        "version": VERSION,
        "updated_at": RETRIEVED_AT,
        "baseline": {
            "checkpoint_id": "m14_09",
            "fixture": BASELINE_FIXTURE,
            "fingerprint": "sha256:" + BASELINE_SHA256,
        },
        "supported_profile": {
            "core_version": "0.30.0",
            "core_revision": "c44dea18809e3ca0e12e25cbe6938c2c45a29c9d",
            "frontend_version": "1.47.12",
            "frontend_revision": "f339c6b2ec0cc90e1c4fa717d9f7d6d565a8f6da",
            "node_api": "V1_ONLY",
            "native_h3_source": "22bc91cd4570a071c664ec4848a5be748909e1e7",
            "status": "QUALIFIED_UNCHANGED",
            "latest_substitution_authorized": False,
            "manual_only_scoped": True,
            "prompt_boundary": "STRING",
            "autogrow_transport": "fully_qualified_zero_based",
            "presentation_labels": "one_based",
            "h3_execution_status": "NOT_IMPLEMENTED",
        },
        "surfaces": surfaces,
        "schema_diffs": [
            {
                "diff_id": "native_h3.sigma_shift",
                "surface_id": "comfyui.native_h3",
                "component": "MiniMaxH3SigmaShift",
                "baseline_fingerprint": "sha256:5c3f78efef772850f90660c5ff82e31078b5baef10f427053ed82fb8b4a7ecd1",
                "observed_fingerprint": "sha256:f767df4074b908efb345f5a87c2fd263ba82c12e65bcca932846207cc213e064",
                "severity": "material",
                "disposition": "BLOCKED_PENDING_REQUALIFICATION",
                "details": "Latest source changes ModelSamplingDiscreteFlow to ModelSamplingAV and passes audio_shift into set_parameters.",
                "affected_profiles": ["latest_native_h3"],
            },
            {
                "diff_id": "native_h3.string_autogrow",
                "surface_id": "comfyui.native_h3",
                "component": "MiniMaxH3ImageToVideo/MiniMaxH3ReferenceToVideo",
                "baseline_fingerprint": "sha256:5c3f78efef772850f90660c5ff82e31078b5baef10f427053ed82fb8b4a7ecd1",
                "observed_fingerprint": "sha256:f767df4074b908efb345f5a87c2fd263ba82c12e65bcca932846207cc213e064",
                "severity": "unchanged",
                "disposition": "UNCHANGED_CONTRACT",
                "details": "Prompt remains STRING; reference prefixes remain zero-based transport paths with one-based presentation labels.",
                "affected_profiles": ["supported_c44_v1"],
            },
            {
                "diff_id": "frontend.version",
                "surface_id": "comfyui.frontend",
                "component": "package.version",
                "baseline_fingerprint": "sha256:60c06ca4ef7b462b80d6543491f050e69981605086d34894fde049fc3d270c53",
                "observed_fingerprint": "sha256:fc7030a06777f25eb318b619ea493d3477f4fbd325a42933d296b536a8da1aba",
                "severity": "material",
                "disposition": "ADDITIVE_NON_SUBSTITUTING",
                "details": "Latest frontend package is 1.51.0; the supported profile remains pinned to 1.47.12.",
                "affected_profiles": ["latest_frontend"],
            },
            {
                "diff_id": "model.license_index",
                "surface_id": "minimax.model.license",
                "component": "LICENSE/model_index.json",
                "baseline_fingerprint": "sha256:59b99642b95ea21630e311198ddbfffbfe05aadba0c2f5d884cbdf4efcc90f44",
                "observed_fingerprint": "sha256:59b99642b95ea21630e311198ddbfffbfe05aadba0c2f5d884cbdf4efcc90f44",
                "severity": "unchanged",
                "disposition": "UNCHANGED_CONTRACT",
                "details": "License bytes remain identical; model_index bytes are also unchanged at sha256:5a587fe13b2371427415ac892463142683aefcd8d322e274a3a095eac37ac7d2.",
                "affected_profiles": ["model_metadata"],
            },
            {
                "diff_id": "comfyui.text_generate_schema",
                "surface_id": "comfyui.text_generate",
                "component": "TextGenerate schema",
                "baseline_fingerprint": "sha256:b328e8a2dc89cfd3a93ab49c1be880a3e89ec4521eef506823753617c86c99e9",
                "observed_fingerprint": "sha256:b328e8a2dc89cfd3a93ab49c1be880a3e89ec4521eef506823753617c86c99e9",
                "severity": "unchanged",
                "disposition": "UNCHANGED_CONTRACT",
                "details": "The current and supported nodes_textgen.py blob are identical; TextGenerate remains a host-owned optional V3 seam and is not a local execution claim.",
                "affected_profiles": ["text_generate"],
            },
            {
                "diff_id": "comfyui.api.versioning",
                "surface_id": "comfyui.docs.v3",
                "component": "comfy_api route authority",
                "baseline_fingerprint": "sha256:bc6c4a7bdafba5e0fcad23186ea0478a841b708451c924e0337302f8b5e677a5",
                "observed_fingerprint": "sha256:fafda2e4739a7d50d2cd70f3630385d8a2f8b72a62aa4de830f393484e3fd83f",
                "severity": "material",
                "disposition": "BLOCKED_PENDING_REQUALIFICATION",
                "details": "Current V3 documentation uses comfy_api.latest examples; no stable numbered V3 route was requalified, so the supported decision remains V1_ONLY.",
                "affected_profiles": ["supported_c44_v1", "latest_host"],
            },
            {
                "diff_id": "comfyui.dynamic_subgraph",
                "surface_id": "comfyui.docs.subgraphs",
                "component": "Subgraph traversal and widget promotion",
                "baseline_fingerprint": "unavailable",
                "observed_fingerprint": "sha256:6f45a143d5eaf3779443d6355b19c81bb48df0e25d2571122165e58ee955556b",
                "severity": "material",
                "disposition": "BLOCKED_PENDING_REQUALIFICATION",
                "details": "Current Subgraph documentation describes evolving widget promotion and graph events; this source was not present in the M14 checkpoint and cannot broaden the accepted dynamic boundary.",
                "affected_profiles": ["supported_c44_v1", "latest_host"],
            },
            {
                "diff_id": "ollama.api_versioning",
                "surface_id": "ollama.docs.introduction",
                "component": "API versioning/base URLs",
                "baseline_fingerprint": "sha256:a4f4f1d59067ebff92da446d1c92c80a2a4b6066e566d9c9dc0cba96ba877394",
                "observed_fingerprint": "sha256:929616d0b385d590e1217aabe3883717edbb9b6951461baca52d464eaf10c7b7",
                "severity": "material",
                "disposition": "BLOCKED_PENDING_REQUALIFICATION",
                "details": "Current docs describe localhost and cloud bases and say the API is not strictly versioned; the project remains loopback-only and does not promote this drift.",
                "affected_profiles": ["optional_ollama"],
            },
        ],
        "claims": [
            {
                "record_id": "product.scope",
                "statement": "MANUAL_ONLY_SCOPED",
                "status": "resolved",
                "confidence": "high",
                "source_ids": ["minimax.repository", "comfyui.native_h3"],
                "affected_profiles": ["supported_c44_v1"],
            },
            {
                "record_id": "native.string_boundary",
                "statement": "STRING",
                "status": "resolved",
                "confidence": "high",
                "source_ids": ["comfyui.native_h3"],
                "affected_profiles": ["supported_c44_v1"],
            },
            {
                "record_id": "latest.non_substituting",
                "statement": "latest observations cannot satisfy exact supported profile",
                "status": "resolved",
                "confidence": "high",
                "source_ids": ["comfyui.core", "comfyui.frontend", "ollama.server"],
                "affected_profiles": ["latest_host", "latest_frontend", "optional_ollama"],
            },
            {
                "record_id": "model.license",
                "statement": "MiniMax H3 Community License remains separate from this Apache-2.0 package",
                "status": "resolved",
                "confidence": "high",
                "source_ids": ["minimax.model.license"],
                "affected_profiles": ["model_metadata"],
            },
        ],
        "unknowns": [
            {
                "record_id": "fixed_h3.generation",
                "statement": "No weight-backed fixed-H3 generation qualification exists in this item",
                "status": "open",
                "confidence": "high",
                "source_ids": ["minimax.model", "comfyui.native_h3"],
                "affected_profiles": ["fixed_h3"],
            },
            {
                "record_id": "context_ir.equivalence",
                "statement": "Local behavior is not equivalent to the private hosted H3-Context-IR service",
                "status": "open",
                "confidence": "high",
                "source_ids": ["minimax.repository"],
                "affected_profiles": ["official_oracle"],
            },
            {
                "record_id": "latest.host_qualification",
                "statement": "Latest ComfyUI/frontend/native H3 and Ollama profiles are not qualified by this ledger",
                "status": "open",
                "confidence": "high",
                "source_ids": [
                    "comfyui.core",
                    "comfyui.native_h3",
                    "comfyui.frontend",
                    "ollama.server",
                ],
                "affected_profiles": ["latest_host", "latest_frontend", "optional_ollama"],
            },
        ],
        "attributions": [
            {
                "attribution_id": "minimax_h3",
                "subject": "MiniMax H3 model and repository",
                "license": "MiniMax H3 Community License Agreement",
                "source_ids": ["minimax.repository", "minimax.model.license"],
                "status": "recorded",
            },
            {
                "attribution_id": "qwen3_vl",
                "subject": "Qwen3-VL-32B encoder referenced by H3 license",
                "license": "Apache-2.0 (upstream notice)",
                "source_ids": ["minimax.model.license"],
                "status": "recorded",
            },
            {
                "attribution_id": "ollama",
                "subject": "Optional Ollama adapter boundary",
                "license": "MIT (upstream project)",
                "source_ids": ["ollama.server", "ollama.python_client"],
                "status": "recorded",
            },
            {
                "attribution_id": "comfyui",
                "subject": "ComfyUI core/frontend integration",
                "license": "GPL-3.0-only for frontend; host terms remain external",
                "source_ids": ["comfyui.core", "comfyui.frontend"],
                "status": "recorded",
            },
            {
                "attribution_id": "project",
                "subject": "ComfyUI-MiniMaxH3-Context package",
                "license": "Apache-2.0",
                "source_ids": ["comfyui.core"],
                "status": "recorded",
            },
        ],
        "guards": {
            "network_performed": False,
            "model_downloaded": False,
            "media_read": False,
            "provider_called": False,
            "credentials_used": False,
            "paid_calls": False,
            "latest_substitution_authorized": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger",
        type=Path,
        default=ROOT / "tests" / "fixtures" / "m16_01_source_requalification.json",
    )
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.write:
            args.ledger.parent.mkdir(parents=True, exist_ok=True)
            args.ledger.write_text(
                json.dumps(build_default_requalification(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        result = load_requalification(args.ledger, args.repo_root)
    except M16RequalificationError as exc:
        print(
            json.dumps(
                {
                    "schema": "h3-context-source-requalification-report/1",
                    "status": "INVALID",
                    "diagnostic": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result.to_wire(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
