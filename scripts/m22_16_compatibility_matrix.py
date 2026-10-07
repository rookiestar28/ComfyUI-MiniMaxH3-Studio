"""Generate the closed assisted-authoring compatibility projection.

This tool joins existing package authorities. It never grants provider authority, opens a socket,
reads a credential, imports ComfyUI, or executes a prompt. The output contains only bounded public
identifiers, enums, counts and fingerprints.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from comfyui_h3_context.core.capability_manifest import default_capability_registry
from comfyui_h3_context.core.installation_profiles import load_installation_profiles
from comfyui_h3_context.core.node_contracts import default_node_contract_registry
from comfyui_h3_context.core.prompt_model_provider import (
    ConnectionQualificationEvidence,
    PromptModelProfile,
    load_prompt_model_catalog,
)
from comfyui_h3_context.core.provider_settings import ProviderSettingsState

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = ROOT / "governance" / "contracts" / "assisted_authoring_compatibility_v2.json"
HOST_FIXTURE_PATH = ROOT / "comfyui_h3_context" / "contracts" / "host_seam_shape_fixture_v1.json"
WORKFLOW_SURFACE_PATH = ROOT / "governance" / "contracts" / "workflow_surface_matrix_v1.json"
LOCALE_SOURCE_PATH = ROOT / "frontend" / "src" / "i18n" / "catalog.ts"

SCHEMA = "h3.context.assisted_authoring_compatibility.v2"
VERSION = 2
MAX_WIRE_BYTES = 32_768
EXPECTED_PROFILE_IDS = frozenset(
    {
        "ollama.local",
        "openai.remote",
        "gemini.remote",
        "anthropic.remote",
    }
)
# IMPORTANT: move these pins and the schema seam count with the canonical host fixture;
# retaining the historical subject blocks regeneration of the current compatibility report.
EXPECTED_HOST = {
    "comfyui_version": "0.38.0",
    "comfyui_revision": "8cfe5e1ecb97512dea8deaac15e1228d7e6feeb1",
    "frontend_version": "1.53.6",
}
EXPECTED_HOST_SEAM_COUNT = 29
EXPECTED_LOCALES = ["en", "zh-TW", "zh-CN"]
EXPECTED_WORKFLOW_MODES = ["t2va", "i2va", "fl2va", "l2va", "ref2va"]

_CAPABILITY_BY_FAMILY = {
    "ollama": "provider.prompt_model_ollama",
    "remote_openai_compatible": "provider.prompt_model_remote_openai_compatible",
    "remote_anthropic": "provider.prompt_model_remote_anthropic",
}
_INSTALLATION_BY_FAMILY = {
    "ollama": "external_ollama",
    "remote_openai_compatible": "remote_openai_compatible_service",
    "remote_anthropic": "remote_anthropic_service",
}
_ROOT_KEYS = frozenset(
    {
        "schema",
        "version",
        "fingerprint",
        "manual_default",
        "proposal_review",
        "host_subject",
        "public_contract",
        "locales",
        "profiles",
    }
)
_PROFILE_KEYS = frozenset(
    {
        "profile_id",
        "provider_label",
        "family",
        "model_selection_required",
        "wire_dialect",
        "qualification_state",
        "evidence_kind",
        "evidence_fingerprint",
        "executable",
        "local_only",
        "requires_credential",
        "requires_consent",
        "capability_id",
        "capability_maturity",
        "installation_profile_id",
        "limitations",
        "manual_fallback",
        "defaulted",
    }
)
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_FORBIDDEN_VALUE = re.compile(
    r"(?:https?://|file:|token=|api[_-]?key|authorization|bearer\s|cookie|[A-Za-z]:\\)",
    re.IGNORECASE,
)


class CompatibilityMatrixError(ValueError):
    """Raised when the projection is not the closed, privacy-safe compatibility contract."""


def _fail(code: str) -> NoReturn:
    raise CompatibilityMatrixError(code)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def reseal(document: Mapping[str, object]) -> dict[str, Any]:
    """Return a deep-copied document with its root fingerprint recomputed."""

    result = copy.deepcopy(dict(document))
    result.pop("fingerprint", None)
    result["fingerprint"] = _fingerprint(result)
    return result


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        _fail("source_root")
    return value


def _locale_ids() -> list[str]:
    text = LOCALE_SOURCE_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"^export const SUPPORTED_LOCALES = \[([^\]]+)\] as const;$",
        text,
        flags=re.MULTILINE,
    )
    if match is None:
        _fail("locale_authority")
    locales = re.findall(r'"([A-Za-z-]+)"', match.group(1))
    if locales != EXPECTED_LOCALES:
        _fail("locale_set")
    return locales


def _evidence(profile: PromptModelProfile) -> tuple[str | None, str | None]:
    evidence = profile.qualification_evidence
    if evidence is None:
        return None, None
    if not isinstance(evidence, ConnectionQualificationEvidence):
        _fail("evidence_kind")
    return "provider_connection", evidence.qualification_sha256


def build_matrix() -> dict[str, Any]:
    """Join the current canonical sources into one non-authorizing compatibility report."""

    catalog = load_prompt_model_catalog()
    if catalog.default_profile_id is not None or set(catalog.profile_ids) != EXPECTED_PROFILE_IDS:
        _fail("catalog_scope")
    capabilities = default_capability_registry()
    installations = {row.profile_id: row for row in load_installation_profiles().profiles}
    projection = ProviderSettingsState.from_catalog().project()

    host_fixture = _read_json(HOST_FIXTURE_PATH)
    subject = host_fixture.get("subject")
    observations = host_fixture.get("observations")
    if not isinstance(subject, dict) or not isinstance(observations, list):
        _fail("host_fixture")
    if any(subject.get(key) != value for key, value in EXPECTED_HOST.items()):
        _fail("host_subject")

    workflow = _read_json(WORKFLOW_SURFACE_PATH)
    modes = workflow.get("modes")
    if not isinstance(modes, list):
        _fail("workflow_surface")
    workflow_modes = [row.get("mode") for row in modes if isinstance(row, dict)]
    if workflow_modes != EXPECTED_WORKFLOW_MODES:
        _fail("workflow_modes")

    node_ids = sorted(row.node_id for row in default_node_contract_registry().definitions)

    rows: list[dict[str, object]] = []
    for profile in sorted(catalog.profiles, key=lambda item: item.profile_id):
        family = profile.family.value
        capability_id = _CAPABILITY_BY_FAMILY.get(family)
        installation_id = _INSTALLATION_BY_FAMILY.get(family)
        if capability_id is None or installation_id is None or installation_id not in installations:
            _fail("profile_owner")
        capability = capabilities.get(capability_id)
        evidence_kind, evidence_fingerprint = _evidence(profile)
        rows.append(
            {
                "profile_id": profile.profile_id,
                "provider_label": profile.provider_label,
                "family": family,
                "model_selection_required": True,
                "wire_dialect": profile.wire_dialect.value,
                "qualification_state": profile.qualification_state.value,
                "evidence_kind": evidence_kind,
                "evidence_fingerprint": evidence_fingerprint,
                # SECURITY: connection qualification alone never authorizes a chosen model.
                # This content-free report owns no census, readiness proof or action lease.
                "executable": False,
                "local_only": profile.capabilities.local_only,
                "requires_credential": profile.capabilities.requires_credential,
                "requires_consent": capability.requires_consent,
                "capability_id": capability_id,
                "capability_maturity": capability.maturity.value,
                "installation_profile_id": installation_id,
                "limitations": list(profile.limitations),
                "manual_fallback": "canonical",
                "defaulted": False,
            }
        )

    document: dict[str, Any] = {
        "schema": SCHEMA,
        "version": VERSION,
        "manual_default": {
            "profile_id": projection.selected_profile_id or None,
            "assisted_authoring": projection.assisted_authoring.to_wire(),
            "network_observation": "none",
            "canonical_owner": "manual_prompt",
        },
        "proposal_review": {
            "proposal_schema": "h3.context.assisted_prompt_proposal.v1",
            "max_attempts": 2,
            "canonical_mutations": ["accept", "validated_edit"],
            "non_mutating_outcomes": ["reject", "cancel", "failure"],
        },
        "host_subject": {
            "fixture_schema": host_fixture.get("schema"),
            "fixture_profile": host_fixture.get("profile"),
            "comfyui_version": subject.get("comfyui_version"),
            "comfyui_revision": subject.get("comfyui_revision"),
            "frontend_version": subject.get("frontend_version"),
            "seam_count": len(observations),
        },
        "public_contract": {
            "node_count": len(node_ids),
            "node_ids_fingerprint": _fingerprint(node_ids),
            "workflow_modes": workflow_modes,
            "workflow_surface_fingerprint": _fingerprint(workflow),
        },
        "locales": _locale_ids(),
        "profiles": rows,
    }
    sealed = reseal(document)
    validate_matrix(sealed)
    return sealed


def _strings(value: object) -> Sequence[str]:
    if not isinstance(value, list) or len(value) > 16:
        _fail("string_list")
    if any(not isinstance(item, str) or not 1 <= len(item) <= 256 for item in value):
        _fail("string_list")
    if len(value) != len(set(value)):
        _fail("string_list")
    return value


def _scan_values(value: object) -> None:
    if isinstance(value, str):
        if len(value) > 512 or _FORBIDDEN_VALUE.search(value):
            _fail("private_value")
        return
    if isinstance(value, list):
        for item in value:
            _scan_values(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _scan_values(item)


def validate_matrix(value: object) -> None:
    """Validate the closed projection independently of JSON Schema tooling."""

    if not isinstance(value, dict) or set(value) != _ROOT_KEYS:
        _fail("root_keys")
    if value.get("schema") != SCHEMA or value.get("version") != VERSION:
        _fail("root_version")

    manual = value.get("manual_default")
    if not isinstance(manual, dict) or set(manual) != {
        "profile_id",
        "assisted_authoring",
        "network_observation",
        "canonical_owner",
    }:
        _fail("manual_default")
    assisted = manual.get("assisted_authoring")
    if manual.get("profile_id") is not None or manual.get("network_observation") != "none":
        _fail("manual_default")
    if manual.get("canonical_owner") != "manual_prompt" or assisted != {
        "available": True,
        "selected": False,
        "ready": False,
        "authorized_for_this_action": False,
        "defaulted": False,
    }:
        _fail("manual_default")

    proposal = value.get("proposal_review")
    if proposal != {
        "proposal_schema": "h3.context.assisted_prompt_proposal.v1",
        "max_attempts": 2,
        "canonical_mutations": ["accept", "validated_edit"],
        "non_mutating_outcomes": ["reject", "cancel", "failure"],
    }:
        _fail("proposal_review")

    host = value.get("host_subject")
    if not isinstance(host, dict) or set(host) != {
        "fixture_schema",
        "fixture_profile",
        "comfyui_version",
        "comfyui_revision",
        "frontend_version",
        "seam_count",
    }:
        _fail("host_subject")
    if host != {
        "fixture_schema": "h3.context.host_seam_shape_fixture.v1",
        "fixture_profile": "comfyui_host_seams_v1",
        **EXPECTED_HOST,
        "seam_count": EXPECTED_HOST_SEAM_COUNT,
    }:
        _fail("host_subject")

    public = value.get("public_contract")
    if not isinstance(public, dict) or set(public) != {
        "node_count",
        "node_ids_fingerprint",
        "workflow_modes",
        "workflow_surface_fingerprint",
    }:
        _fail("public_contract")
    if (
        public.get("node_count") != 28
        or public.get("workflow_modes") != EXPECTED_WORKFLOW_MODES
        or not all(
            isinstance(public.get(key), str) and _FINGERPRINT.fullmatch(public[key]) is not None
            for key in ("node_ids_fingerprint", "workflow_surface_fingerprint")
        )
    ):
        _fail("public_contract")

    if value.get("locales") != EXPECTED_LOCALES:
        _fail("locale_set")
    profiles = value.get("profiles")
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= 8:
        _fail("profiles")
    ids: list[str] = []
    for row in profiles:
        if not isinstance(row, dict) or set(row) != _PROFILE_KEYS:
            _fail("profile_keys")
        profile_id = row.get("profile_id")
        if not isinstance(profile_id, str) or _IDENTIFIER.fullmatch(profile_id) is None:
            _fail("profile_id")
        ids.append(profile_id)
        state = row.get("qualification_state")
        qualified = state == "qualified"
        if (
            state not in {"qualified", "catalog_only"}
            or row.get("executable") is not False
            or row.get("model_selection_required") is not True
        ):
            _fail("profile_qualification")
        evidence_kind = row.get("evidence_kind")
        evidence_fingerprint = row.get("evidence_fingerprint")
        if qualified:
            if (
                not isinstance(evidence_kind, str)
                or not isinstance(evidence_fingerprint, str)
                or _FINGERPRINT.fullmatch(evidence_fingerprint) is None
            ):
                _fail("profile_evidence")
        elif evidence_kind is not None or evidence_fingerprint is not None:
            _fail("profile_evidence")
        family = row.get("family")
        if family not in _CAPABILITY_BY_FAMILY:
            _fail("profile_family")
        if row.get("capability_id") != _CAPABILITY_BY_FAMILY[family]:
            _fail("profile_capability")
        if row.get("installation_profile_id") != _INSTALLATION_BY_FAMILY[family]:
            _fail("profile_installation")
        if family.startswith("remote_") and row.get("requires_consent") is not True:
            _fail("profile_consent")
        if row.get("manual_fallback") != "canonical" or row.get("defaulted") is not False:
            _fail("profile_fallback")
        for key in (
            "provider_label",
            "wire_dialect",
            "capability_maturity",
        ):
            if not isinstance(row.get(key), str) or not row[key]:
                _fail("profile_text")
        _strings(row.get("limitations"))
    if len(ids) != len(set(ids)) or set(ids) != EXPECTED_PROFILE_IDS:
        _fail("profile_set")

    _scan_values(value)
    supplied_fingerprint = value.get("fingerprint")
    unsigned = copy.deepcopy(value)
    unsigned.pop("fingerprint", None)
    if supplied_fingerprint != _fingerprint(unsigned):
        _fail("fingerprint")
    if len(artifact_bytes(value)) > MAX_WIRE_BYTES:
        _fail("wire_bytes")


def artifact_bytes(value: object) -> bytes:
    """Return deterministic human-readable UTF-8 bytes with one terminal newline."""

    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    matrix = build_matrix()
    expected = artifact_bytes(matrix)
    if args.check:
        if not ARTIFACT_PATH.is_file() or ARTIFACT_PATH.read_bytes() != expected:
            print("assisted authoring compatibility matrix: DRIFTED")
            return 1
        print("assisted authoring compatibility matrix: PASS")
        return 0
    ARTIFACT_PATH.write_bytes(expected)
    print(f"assisted authoring compatibility matrix: wrote {ARTIFACT_PATH}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
