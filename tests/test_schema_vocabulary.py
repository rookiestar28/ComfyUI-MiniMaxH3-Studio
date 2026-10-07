"""M1-01 versioned vocabulary and pure-core contract tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError
from enum import Enum
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import ContractValidationError
from comfyui_h3_context.core.contracts import (
    CURRENT_SCHEMA_VERSION,
    AssetDescriptor,
    AssetRole,
    EvidenceLevel,
    H3ContextContract,
    MediaKind,
    ModelVariant,
    ProfileIdentity,
    PromptProfile,
    ProviderIdentity,
    SchemaVersion,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "governance" / "contracts" / "h3_context_v1.schema.json"


def enum_values(enum_type: type[Enum]) -> set[str]:
    return {str(member.value) for member in enum_type}


class SchemaVocabularyTests(unittest.TestCase):
    def test_closed_wire_vocabularies_are_versioned_and_complete(self) -> None:
        expected: tuple[tuple[type[Enum], set[str]], ...] = (
            (TaskMode, {"t2va", "i2va", "fl2va", "l2va", "ref2va"}),
            (ModelVariant, {"base_fl2va", "base_ref2va"}),
            (
                EvidenceLevel,
                {
                    "official",
                    "framework_reference",
                    "community_recommended",
                    "experimental",
                    "modified",
                },
            ),
            (
                AssetRole,
                {
                    "primary",
                    "first_frame",
                    "last_frame",
                    "reference",
                    "subject_reference",
                    "style_reference",
                    "motion_reference",
                    "camera_reference",
                    "editing_source",
                    "continuation_source",
                    "audio_source",
                },
            ),
            (MediaKind, {"image", "video", "audio"}),
            (ProviderIdentity, {"manual", "local", "remote_custom", "official_minimax"}),
            (ValidationSeverity, {"info", "warning", "error", "fatal"}),
            (PromptProfile, {"h3_base", "h3_full_reference"}),
        )
        for enum_type, values in expected:
            with self.subTest(enum_type=enum_type.__name__):
                self.assertEqual(enum_values(enum_type), values)
                self.assertTrue(all(isinstance(member.value, str) for member in enum_type))

    def test_unknown_wire_values_fail_without_coercion(self) -> None:
        for enum_type in (
            TaskMode,
            ModelVariant,
            EvidenceLevel,
            AssetRole,
            MediaKind,
            ProviderIdentity,
            ValidationSeverity,
            PromptProfile,
        ):
            with self.subTest(enum_type=enum_type.__name__), self.assertRaises(ValueError):
                enum_type("unsupported")

        with self.assertRaises(ContractValidationError):
            SchemaVersion.from_wire("v1")
        with self.assertRaises(ContractValidationError):
            SchemaVersion(0, 0)
        with self.assertRaises(ContractValidationError):
            SchemaVersion(1, True)

    def test_versions_and_profile_identity_are_bounded(self) -> None:
        self.assertEqual(str(CURRENT_SCHEMA_VERSION), "1.0")
        self.assertEqual(SchemaVersion.from_wire("1.2"), SchemaVersion(1, 2))
        profile = ProfileIdentity(PromptProfile.BASE, SchemaVersion(1, 0))
        self.assertEqual(profile.to_wire(), {"name": "h3_base", "version": "1.0"})

        with self.assertRaises(ContractValidationError):
            ProfileIdentity(PromptProfile.BASE, SchemaVersion(2, 0))
        with self.assertRaises(ContractValidationError):
            ProfileIdentity("h3_base", SchemaVersion(1, 0))  # type: ignore[arg-type]

    def test_typed_contract_rejects_incompatible_mode_and_profile(self) -> None:
        base = H3ContextContract(
            schema_version=CURRENT_SCHEMA_VERSION,
            profile=ProfileIdentity(PromptProfile.BASE, SchemaVersion(1, 0)),
            task_mode=TaskMode.FL2VA,
            model_variant=ModelVariant.BASE_FL2VA,
            evidence_level=EvidenceLevel.OFFICIAL,
            provider=ProviderIdentity.MANUAL,
        )
        self.assertEqual(base.to_wire()["task_mode"], "fl2va")

        reference = H3ContextContract(
            schema_version=CURRENT_SCHEMA_VERSION,
            profile=ProfileIdentity(PromptProfile.FULL_REFERENCE, SchemaVersion(1, 0)),
            task_mode=TaskMode.REF2VA,
            model_variant=ModelVariant.BASE_REF2VA,
            evidence_level=EvidenceLevel.FRAMEWORK_REFERENCE,
            provider=ProviderIdentity.OFFICIAL_MINIMAX,
            assets=(
                AssetDescriptor(
                    asset_id="asset_1",
                    kind=MediaKind.IMAGE,
                    role=AssetRole.SUBJECT_REFERENCE,
                ),
            ),
            diagnostics=(
                ValidationDiagnostic(
                    severity=ValidationSeverity.WARNING,
                    code="missing_observation",
                    message="A reference observation is unavailable.",
                    location="assets[0]",
                ),
            ),
        )
        wire = reference.to_wire()
        self.assertEqual(wire["profile"], {"name": "h3_full_reference", "version": "1.0"})
        assets = cast(list[dict[str, object]], wire["assets"])
        diagnostics = cast(list[dict[str, object]], wire["diagnostics"])
        self.assertEqual(assets[0]["kind"], "image")
        self.assertEqual(diagnostics[0]["severity"], "warning")

        with self.assertRaises(ContractValidationError):
            H3ContextContract(
                schema_version=CURRENT_SCHEMA_VERSION,
                profile=ProfileIdentity(PromptProfile.BASE, SchemaVersion(1, 0)),
                task_mode=TaskMode.REF2VA,
                model_variant=ModelVariant.BASE_REF2VA,
                evidence_level=EvidenceLevel.OFFICIAL,
                provider=ProviderIdentity.MANUAL,
            )
        with self.assertRaises(ContractValidationError):
            H3ContextContract(
                schema_version=CURRENT_SCHEMA_VERSION,
                profile=ProfileIdentity(PromptProfile.FULL_REFERENCE, SchemaVersion(1, 0)),
                task_mode=TaskMode.REF2VA,
                model_variant=ModelVariant.BASE_FL2VA,
                evidence_level=EvidenceLevel.OFFICIAL,
                provider=ProviderIdentity.MANUAL,
            )

    def test_contract_values_are_immutable_and_json_serializable(self) -> None:
        asset = AssetDescriptor("asset_1", MediaKind.VIDEO, AssetRole.EDITING_SOURCE)
        with self.assertRaises(FrozenInstanceError):
            asset.asset_id = "asset_2"  # type: ignore[misc]

        contract = H3ContextContract(
            CURRENT_SCHEMA_VERSION,
            ProfileIdentity(PromptProfile.BASE, SchemaVersion(1, 0)),
            TaskMode.T2VA,
            ModelVariant.BASE_FL2VA,
            EvidenceLevel.COMMUNITY_RECOMMENDED,
            ProviderIdentity.LOCAL,
            assets=(asset,),
        )
        encoded = json.dumps(contract.to_wire(), sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["schema_version"], "1.0")
        self.assertEqual(decoded["assets"][0]["role"], "editing_source")

    def test_json_schema_artifact_matches_python_vocabularies(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["$id"], "comfyui-h3-context://contracts/h3_context_v1.schema.json")
        self.assertEqual(schema["properties"]["schema_version"]["const"], "1.0")
        enum_fields: tuple[tuple[str, type[Enum]], ...] = (
            ("task_mode", TaskMode),
            ("model_variant", ModelVariant),
            ("evidence_level", EvidenceLevel),
            ("provider", ProviderIdentity),
        )
        for field, enum_type in enum_fields:
            with self.subTest(field=field):
                self.assertEqual(
                    set(schema["properties"][field]["enum"]),
                    enum_values(enum_type),
                )

        profile = schema["$defs"]["ProfileIdentity"]
        self.assertEqual(
            set(profile["properties"]["name"]["enum"]),
            enum_values(PromptProfile),
        )
        self.assertEqual(
            set(schema["$defs"]["AssetDescriptor"]["properties"]["kind"]["enum"]),
            enum_values(MediaKind),
        )
        self.assertEqual(
            set(schema["$defs"]["AssetDescriptor"]["properties"]["role"]["enum"]),
            enum_values(AssetRole),
        )
        self.assertEqual(
            set(schema["$defs"]["ValidationDiagnostic"]["properties"]["severity"]["enum"]),
            enum_values(ValidationSeverity),
        )

    def test_contract_module_has_no_optional_runtime_imports(self) -> None:
        source = (ROOT / "comfyui_h3_context" / "core" / "contracts.py").read_text(encoding="utf-8")
        for forbidden in ("comfy", "diffusers", "httpx", "requests", "torch", "transformers"):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from {forbidden}", source)


if __name__ == "__main__":
    unittest.main()
