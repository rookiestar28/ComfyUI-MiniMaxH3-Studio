from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import tomli

from comfyui_h3_context.core.generation_profile import (
    FAMILY_ANCHOR,
    PINNED_TEMPLATE_DIGESTS,
    AssetSlot,
    FamilyDisposition,
    HostObservation,
    SlotDisposition,
    SlotObservation,
    TemplateCapability,
    qualify_generation_profile,
)
from comfyui_h3_context.core.installation_profiles import qualify_installation_environment

ROOT = Path(__file__).resolve().parents[1]
ANCHORS = frozenset(FAMILY_ANCHOR.values())
DRIFTED_TEMPLATE_DIGESTS = tuple(
    (name, f"{index:064x}") for index, (name, _digest) in enumerate(PINNED_TEMPLATE_DIGESTS, 1)
)
CAPABILITIES = (
    TemplateCapability(
        template_name="video_minimax_h3_t2v",
        anchor_node_type="MiniMaxH3ImageToVideo",
        input_names=frozenset({"prompt"}),
    ),
    TemplateCapability(
        template_name="video_minimax_h3_i2v",
        anchor_node_type="MiniMaxH3ImageToVideo",
        input_names=frozenset({"prompt"}),
    ),
    TemplateCapability(
        template_name="video_minimax_h3_r2v",
        anchor_node_type="MiniMaxH3ReferenceToVideo",
        input_names=frozenset({"prompt"}),
    ),
)
HOST_IDENTITY_SCHEMA_PATHS = (
    "assisted_authoring_compatibility_v1.schema.json",
    "product_shell_v1.schema.json",
    "sidebar_workspace_v1.schema.json",
    "native_mode_matrix_v1.schema.json",
)
HISTORICAL_IDENTITY_VALUES = {
    "0.32.0",
    "1.48.7",
    "b323a345bbbfb2f3a95b5b73b68eb7919a26515e",  # pragma: allowlist secret
    "6d6af63c00f132cd25dc29307fc56bd2c094fa22",  # pragma: allowlist secret
    "0b1840e851c248f89e9920159c3c8237fa2e7186",  # pragma: allowlist secret
}


def _slots() -> tuple[SlotObservation, ...]:
    return tuple(
        SlotObservation(slot=slot, disposition=SlotDisposition.PRESENT) for slot in AssetSlot
    )


def _collect_const_values(value: object) -> set[object]:
    if isinstance(value, dict):
        const = value.get("const")
        found: set[object] = (
            {const} if isinstance(const, (str, int, float, bool, type(None))) else set()
        )
        for child in value.values():
            found.update(_collect_const_values(child))
        return found
    if isinstance(value, list):
        list_found: set[object] = set()
        for child in value:
            list_found.update(_collect_const_values(child))
        return list_found
    return set()


def test_different_host_and_template_identity_do_not_refuse_observed_capability() -> None:
    profile = qualify_generation_profile(
        HostObservation(
            anchor_node_types=ANCHORS,
            template_digests=DRIFTED_TEMPLATE_DIGESTS,
            slots=_slots(),
            host_version="0.33.0",
            template_capabilities=CAPABILITIES,
        ),
        expected_template_digests=PINNED_TEMPLATE_DIGESTS,
    )

    assert {family.disposition for family in profile.families} == {FamilyDisposition.AVAILABLE}


def test_missing_observed_anchor_still_refuses_and_names_the_capability() -> None:
    missing = FAMILY_ANCHOR[next(iter(FAMILY_ANCHOR))]
    profile = qualify_generation_profile(
        HostObservation(
            anchor_node_types=ANCHORS - {missing},
            template_digests=DRIFTED_TEMPLATE_DIGESTS,
            slots=_slots(),
            host_version="0.33.0",
            template_capabilities=CAPABILITIES,
        ),
        expected_template_digests=PINNED_TEMPLATE_DIGESTS,
    )

    refused = [family for family in profile.families if family.anchor_node_type == missing]
    assert refused
    assert all(family.disposition is FamilyDisposition.UNSUPPORTED_HOST for family in refused)
    assert all(family.anchor_node_type == missing for family in refused)


def test_installation_metadata_and_qualification_do_not_pin_a_host_version() -> None:
    metadata = tomli.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "requires-comfyui" not in metadata["tool"]["comfy"]
    assert metadata["project"]["dependencies"] == []

    result = qualify_installation_environment(
        python_version="3.13.9",
        comfyui_version="0.33.0",
        frontend_version="1.49.6",
    )
    assert result.status == "supported"
    assert result.diagnostics == ()


def test_registration_smoke_accepts_an_arbitrary_valid_host_identity(tmp_path: Path) -> None:
    host_root = tmp_path / "host"
    host_root.mkdir()
    (host_root / "pyproject.toml").write_text(
        '[project]\nname = "synthetic-comfyui"\nversion = "9.8.7-dev.1"\n',
        encoding="utf-8",
    )
    for command in (
        ("git", "init", "--quiet", "--initial-branch=main"),
        ("git", "add", "pyproject.toml"),
        (
            "git",
            "-c",
            "user.name=M23 Test",
            "-c",
            "user.email=m23@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "synthetic host",
        ),
    ):
        subprocess.run(command, cwd=host_root, check=True, capture_output=True, text=True)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "registration_smoke.py")],
        cwd=ROOT,
        env={**os.environ, "H3_CONTEXT_HOST_ROOT": str(host_root)},
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["host_version"] == "9.8.7-dev.1"
    assert len(payload["host_revision"]) == 40


def test_shipped_schemas_type_host_provenance_without_const_pins() -> None:
    contracts = ROOT / "governance" / "contracts"
    for name in HOST_IDENTITY_SCHEMA_PATHS:
        schema = json.loads((contracts / name).read_text(encoding="utf-8"))
        assert _collect_const_values(schema).isdisjoint(HISTORICAL_IDENTITY_VALUES), name


def test_shipped_schema_provenance_patterns_reject_trailing_newlines() -> None:
    contracts = ROOT / "governance" / "contracts"
    schemas = {
        name: json.loads((contracts / name).read_text(encoding="utf-8"))
        for name in HOST_IDENTITY_SCHEMA_PATHS
    }
    assisted = schemas["assisted_authoring_compatibility_v1.schema.json"]["properties"][
        "host_subject"
    ]["properties"]
    native = schemas["native_mode_matrix_v1.schema.json"]["properties"]
    product = schemas["product_shell_v1.schema.json"]["properties"]["host"]["properties"]
    sidebar = schemas["sidebar_workspace_v1.schema.json"]["properties"]["resources"]["properties"]
    revision = "1" * 40
    cases = (
        (assisted["comfyui_version"], "0.33.0"),
        (assisted["comfyui_revision"], revision),
        (assisted["frontend_version"], "1.49.6"),
        (native["host_version"], "0.33.0"),
        (native["host_revision"], revision),
        (native["native_source_blob"], revision),
        (product["core_version"], "0.33.0"),
        (product["core_revision"], revision),
        (product["frontend_version"], "1.49.6"),
        (product["frontend_revision"], revision),
        (sidebar["core_version"], "0.33.0"),
        (sidebar["frontend_version"], "1.49.6"),
    )
    for field_schema, valid in cases:
        validator = jsonschema.Draft202012Validator(field_schema)
        assert validator.is_valid(valid)
        for suffix in ("\n", "\r", "\r\n"):
            assert not validator.is_valid(valid + suffix)
