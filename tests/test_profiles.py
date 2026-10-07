"""M2-01 versioned prompt-profile registry tests."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    CURRENT_PROFILE_VERSION,
    CURRENT_SCHEMA_VERSION,
    DurationAlignment,
    DurationRule,
    EvidenceLevel,
    LabelRule,
    ProfileRegistryError,
    ProfileSource,
    PromptProfile,
    ReferenceLabelKind,
    TaskMode,
    default_profile_registry,
)

ROOT = Path(__file__).resolve().parents[1]


class ProfileRegistryTests(unittest.TestCase):
    def test_builtins_declare_required_contract_surface_and_order(self) -> None:
        registry = default_profile_registry()
        self.assertEqual(registry.schema_version, CURRENT_SCHEMA_VERSION)
        base = registry.resolve(PromptProfile.BASE, CURRENT_PROFILE_VERSION)
        full = registry.resolve("h3_full_reference", "1.0")
        self.assertEqual(
            base.supported_modes,
            (TaskMode.T2VA, TaskMode.I2VA, TaskMode.FL2VA, TaskMode.L2VA),
        )
        self.assertEqual(base.required_fields, base.render_order)
        self.assertEqual(
            full.render_order,
            (
                "subject_definitions",
                "summary",
                "retention_analysis",
                "detailed_description",
                "overall_soundscape",
                "non_diegetic_music",
            ),
        )
        self.assertEqual(full.required_fields, full.render_order)
        self.assertEqual(base.source.evidence_level, EvidenceLevel.FRAMEWORK_REFERENCE)
        self.assertEqual(full.source.evidence_level, EvidenceLevel.FRAMEWORK_REFERENCE)
        self.assertEqual(
            {rule.kind for rule in full.label_rules},
            {
                ReferenceLabelKind.SUBJECT,
                ReferenceLabelKind.PICTURE,
                ReferenceLabelKind.VIDEO,
                ReferenceLabelKind.AUDIO,
            },
        )
        self.assertEqual(base.duration_rule.alignment, DurationAlignment.NONE)
        self.assertEqual(base.duration_rule.alignment_for(TaskMode.I2VA), DurationAlignment.FIRST)
        self.assertEqual(
            base.duration_rule.alignment_for(TaskMode.FL2VA), DurationAlignment.FIRST_AND_LAST
        )
        self.assertEqual(base.duration_rule.alignment_for(TaskMode.L2VA), DurationAlignment.LAST)

    def test_lookup_is_explicit_and_unknown_name_or_version_fails(self) -> None:
        registry = default_profile_registry()
        self.assertEqual(len(registry.for_mode(TaskMode.REF2VA)), 1)
        self.assertEqual(len(registry.for_mode(TaskMode.T2VA)), 1)
        with self.assertRaises(ProfileRegistryError):
            registry.resolve("h3_unknown", "1.0")
        with self.assertRaises(ProfileRegistryError):
            registry.resolve("h3_base", "2.0")
        with self.assertRaises(ProfileRegistryError):
            registry.resolve("h3_base", "not-a-version")
        with self.assertRaises(ProfileRegistryError):
            registry.for_mode("t2va")  # type: ignore[arg-type]

    def test_duplicate_registration_and_invalid_metadata_fail_closed(self) -> None:
        registry = default_profile_registry()
        base = registry.resolve("h3_base", "1.0")
        with self.assertRaises(ProfileRegistryError):
            registry.with_definition(base)
        with self.assertRaises(ProfileRegistryError):
            ProfileSource(
                "source",
                "https://private.example/guide",
                EvidenceLevel.FRAMEWORK_REFERENCE,
            )
        with self.assertRaises(ProfileRegistryError):
            ProfileSource(
                "source",
                "revision",
                EvidenceLevel.OFFICIAL,
                official_override=True,
                modified_from="h3_base/1.0",
            )
        with self.assertRaises(ProfileRegistryError):
            ProfileSource(
                "source",
                "revision",
                EvidenceLevel.MODIFIED,
                official_override=True,
            )
        with self.assertRaises(ProfileRegistryError):
            LabelRule(
                ReferenceLabelKind.PICTURE,
                "Picture {ordinal}",
                "picture_reference_order",
                "bad syntax",
            )
        with self.assertRaises(ProfileRegistryError):
            DurationRule(
                (),
                24,
                5,
                124,
                DurationAlignment.NONE,
            )

    def test_registry_wire_and_schema_are_inspectable_and_deterministic(self) -> None:
        registry = default_profile_registry()
        wire = registry.to_wire()
        self.assertEqual(wire["registry_schema"], "h3-prompt-profile-registry/1")
        profiles_wire = cast(list[dict[str, object]], wire["profiles"])
        self.assertEqual(
            [cast(dict[str, object], item["identity"])["name"] for item in profiles_wire],
            [
                "h3_base",
                "h3_full_reference",
            ],
        )
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "h3_prompt_profile_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/h3_prompt_profile_v1.schema.json"
        )
        self.assertEqual(set(schema["required"]), {"registry_schema", "schema_version", "profiles"})
        self.assertEqual(
            schema["properties"]["registry_schema"]["const"], "h3-prompt-profile-registry/1"
        )

    def test_profiles_module_has_no_optional_runtime_imports(self) -> None:
        module = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "profiles.py").read_text(encoding="utf-8")
        )
        optional_roots = {"comfy", "torch", "torchaudio", "requests", "httpx", "PIL", "numpy"}
        imported: set[str] = set()
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(optional_roots.isdisjoint(imported))


if __name__ == "__main__":
    unittest.main()
