"""M17-20 phase 1: the host-observing half of the generation-profile contract.

Every row runs without a ComfyUI host. Where a host is needed it is a stub in
`sys.modules`, which is how the repository proves that the integration is
optional and that importing the package never pulls a host in.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from typing import cast
from unittest.mock import patch

import comfyui_h3_context.adapters.comfyui_generation_profile as generation_profile_adapter
from comfyui_h3_context.adapters.comfyui_generation_profile import (
    ANCHOR_NODE_TYPES,
    GENERATION_PROFILE_ROUTE,
    OFFICIAL_SLOT_ASSETS,
    TEMPLATE_DEFAULT_ASSETS,
    build_generation_profile,
    observe_host,
    observe_host_node_types,
    observe_host_version,
    observe_slots,
    observe_template_capabilities,
    observe_template_digests,
)
from comfyui_h3_context.core.continuity_handoff import QUALIFIED_COMFYUI_HOST_VERSION
from comfyui_h3_context.core.generation_profile import (
    PINNED_TEMPLATE_DIGESTS,
    AssetSlot,
    FamilyDisposition,
    GenerationProfile,
    HostObservation,
    Remediation,
    SlotDisposition,
    TemplateCapability,
    qualify_generation_profile,
)
from scripts.hc_09_host_seam_test_double import (
    InstalledHostModules as _HostModules,
)
from scripts.hc_09_host_seam_test_double import (
    host_folder_paths_module,
)
from scripts.hc_09_host_seam_test_double import (
    host_nodes_module as node_module,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDORED_TEMPLATES = (
    REPO_ROOT / "reference" / "rm02" / "official" / "workflow_templates" / "templates"
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


def template_payload(name: str, *, include_prompt: bool = True) -> dict[str, object]:
    anchor = "MiniMaxH3ReferenceToVideo" if name.endswith("_r2v") else "MiniMaxH3ImageToVideo"
    inputs = (
        [{"name": "prompt", "type": "STRING"}]
        if include_prompt
        else [{"name": "width", "type": "INT"}]
    )
    subgraph_id = f"subgraph:{name}"
    return {
        "nodes": [{"type": subgraph_id, "inputs": []}],
        "definitions": {
            "subgraphs": [
                {
                    "id": subgraph_id,
                    "nodes": [
                        {"type": "UnrelatedNode", "inputs": []},
                        {"type": anchor, "inputs": inputs, "widgets_values": [name]},
                    ],
                }
            ]
        },
    }


def profile_from_stub_host() -> GenerationProfile:
    """Qualify a stub host without needing the vendored template bytes.

    These rows are about what a host *inventory* means, and the templates are
    only present in them because qualification needs a digest observation. The
    corpus lives under the ignored `reference/` tree, so reading it makes the row
    pass or fail on whether a checkout happens to have it -- and a fresh clone,
    including every hosted run, never does. Observing the pinned digests states
    the premise directly: the host serves the bytes this build pinned. Whether
    the vendored corpus still hashes to them is a separate row, and it is the one
    that skips when the corpus is absent.
    """

    return qualify_generation_profile(
        HostObservation(
            anchor_node_types=observe_host_node_types(),
            template_digests=PINNED_TEMPLATE_DIGESTS,
            slots=observe_slots(),
            host_version=observe_host_version(),
            template_capabilities=CAPABILITIES,
        ),
        expected_template_digests=PINNED_TEMPLATE_DIGESTS,
    )


def folder_module(**inventory: list[str]) -> types.ModuleType:
    return host_folder_paths_module(inventory=inventory)


def version_module(version: str) -> types.ModuleType:
    module = types.ModuleType("comfyui_version")
    module.__version__ = version  # type: ignore[attr-defined]
    return module


class AdapterObservationTests(unittest.TestCase):
    def test_no_host_yields_an_empty_but_valid_observation(self) -> None:
        """The package must import and answer with no ComfyUI present at all.
        The answer is "nothing is available", not an exception."""

        for name in ("nodes", "folder_paths", "comfyui_version", "comfyui_workflow_templates"):
            self.assertNotIn(name, sys.modules, f"{name} leaked into the test process")
        observation = observe_host()
        self.assertEqual(observation.anchor_node_types, frozenset())
        self.assertEqual(observation.template_digests, ())
        self.assertEqual(observation.template_capabilities, ())
        self.assertEqual(observation.host_version, "")
        self.assertEqual(len(observation.slots), len(AssetSlot))
        self.assertTrue(all(not slot.present for slot in observation.slots))

    def test_anchor_presence_is_read_from_the_host_node_mappings(self) -> None:
        with _HostModules(nodes=node_module("MiniMaxH3ImageToVideo", "SaveVideo")):
            self.assertEqual(observe_host_node_types(), frozenset({"MiniMaxH3ImageToVideo"}))
        with _HostModules(nodes=node_module(*ANCHOR_NODE_TYPES)):
            self.assertEqual(observe_host_node_types(), frozenset(ANCHOR_NODE_TYPES))

    def test_the_host_version_is_observed_rather_than_assumed(self) -> None:
        with _HostModules(comfyui_version=version_module(QUALIFIED_COMFYUI_HOST_VERSION)):
            self.assertEqual(observe_host_version(), QUALIFIED_COMFYUI_HOST_VERSION)
        with _HostModules(comfyui_version=types.ModuleType("comfyui_version")):
            self.assertEqual(observe_host_version(), "")

    def test_slot_presence_is_a_membership_test_and_leaks_no_inventory(self) -> None:
        """The host's model list is read but never carried. The observation only
        answers whether the template's own published default resolves."""

        private = ["a_users_private_model.safetensors", "minimax_h3_video_vae_fp16.safetensors"]
        with _HostModules(folder_paths=folder_module(vae=private)):
            slots = observe_slots()
        present = {slot.slot for slot in slots if slot.present}
        self.assertEqual(present, {AssetSlot.VIDEO_VAE})
        self.assertNotIn("a_users_private_model", repr([slot.to_wire() for slot in slots]))

    def test_the_measured_host_state_reproduces_as_missing_assets(self) -> None:
        """The supplied host on 2026-08-18 had both VAEs and none of the other
        three defaults. Reproduced here as a stub so the row keeps meaning after
        the host changes."""

        with _HostModules(
            nodes=node_module(*ANCHOR_NODE_TYPES),
            comfyui_version=version_module(QUALIFIED_COMFYUI_HOST_VERSION),
            folder_paths=folder_module(
                vae=[
                    "minimax_h3_video_vae_fp16.safetensors",
                    "minimax_h3_audio_vae_fp32.safetensors",
                ],
                diffusion_models=[],
                text_encoders=[],
            ),
        ):
            profile = profile_from_stub_host()
        for entry in profile.families:
            with self.subTest(basis=entry.template_name):
                self.assertIs(entry.disposition, FamilyDisposition.MISSING_ASSET)
                self.assertIn(AssetSlot.TEXT_ENCODER, entry.unsatisfied_slots)

    def test_a_fully_provisioned_stub_host_qualifies_every_basis(self) -> None:
        inventory: dict[str, list[str]] = {}
        for _slot, folder, filename in TEMPLATE_DEFAULT_ASSETS:
            inventory.setdefault(folder, []).append(filename)
        with _HostModules(
            nodes=node_module(*ANCHOR_NODE_TYPES),
            comfyui_version=version_module(QUALIFIED_COMFYUI_HOST_VERSION),
            folder_paths=folder_module(**inventory),
        ):
            profile = profile_from_stub_host()
        self.assertEqual(len(profile.families), 3)
        for entry in profile.families:
            with self.subTest(basis=entry.template_name):
                self.assertTrue(entry.available)

    def test_a_host_inventory_that_raises_is_treated_as_absent_not_as_present(self) -> None:
        def explode(folder: str) -> list[str]:
            raise RuntimeError("host inventory unavailable")

        module = host_folder_paths_module(resolver=explode)
        with _HostModules(folder_paths=module):
            slots = observe_slots()
        self.assertTrue(all(not slot.present for slot in slots))


class TemplateDigestTests(unittest.TestCase):
    def test_the_pinned_digests_match_the_vendored_template_bytes(self) -> None:
        """The pin is the bytes. If the vendored corpus moves, this fails rather
        than silently qualifying a template the splice was never read against."""

        if not VENDORED_TEMPLATES.is_dir():
            self.skipTest("vendored template corpus is not present in this checkout")
        for name, expected in PINNED_TEMPLATE_DIGESTS:
            with self.subTest(template=name):
                payload = (VENDORED_TEMPLATES / f"{name}.json").read_bytes()
                self.assertEqual(hashlib.sha256(payload).hexdigest(), expected)

    def test_observed_digests_reflect_the_served_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "video_minimax_h3_i2v.json").write_bytes(b'{"nodes": []}')
            observed = dict(observe_template_digests(root))
        self.assertEqual(
            observed,
            {"video_minimax_h3_i2v": hashlib.sha256(b'{"nodes": []}').hexdigest()},
        )

    def test_the_multi_bundle_layout_is_resolved_through_the_package(self) -> None:
        """The layout the qualified host actually ships.

        `comfyui_workflow_templates` became a thin wrapper whose JSON lives in a
        separate distribution, and its own `get_templates_path()` raises rather
        than guessing. Reading only the legacy directory found nothing there and
        reported every basis as drifted -- a refusal whose remediation would have
        sent the user to requalify bytes that had never moved.
        """

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name, _digest in PINNED_TEMPLATE_DIGESTS:
                (root / f"{name}.json").write_text(json.dumps({"nodes": [name]}))
            module = types.ModuleType("comfyui_workflow_templates")

            def get_asset_path(template_id: str, filename: str) -> str:
                return str(root / filename)

            module.get_asset_path = get_asset_path  # type: ignore[attr-defined]
            with _HostModules(comfyui_workflow_templates=module):
                observed = dict(observe_template_digests())
        self.assertEqual(set(observed), {name for name, _digest in PINNED_TEMPLATE_DIGESTS})
        self.assertEqual(
            observed["video_minimax_h3_t2v"],
            hashlib.sha256(json.dumps({"nodes": ["video_minimax_h3_t2v"]}).encode()).hexdigest(),
        )

    def test_the_legacy_directory_layout_still_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "comfyui_workflow_templates"
            (package / "templates").mkdir(parents=True)
            (package / "templates" / "video_minimax_h3_i2v.json").write_bytes(b"{}")
            module = types.ModuleType("comfyui_workflow_templates")
            module.__file__ = str(package / "__init__.py")
            with _HostModules(comfyui_workflow_templates=module):
                observed = dict(observe_template_digests())
        self.assertEqual(
            observed,
            {"video_minimax_h3_i2v": hashlib.sha256(b"{}").hexdigest()},
        )

    def test_a_resolver_that_raises_falls_back_instead_of_reporting_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "comfyui_workflow_templates"
            (package / "templates").mkdir(parents=True)
            (package / "templates" / "video_minimax_h3_r2v.json").write_bytes(b"[]")
            module = types.ModuleType("comfyui_workflow_templates")
            module.__file__ = str(package / "__init__.py")

            def explode(template_id: str, filename: str) -> str:
                raise FileNotFoundError(template_id)

            module.get_asset_path = explode  # type: ignore[attr-defined]
            with _HostModules(comfyui_workflow_templates=module):
                observed = dict(observe_template_digests())
        self.assertEqual(
            observed,
            {"video_minimax_h3_r2v": hashlib.sha256(b"[]").hexdigest()},
        )

    def test_no_templates_package_reports_no_digests(self) -> None:
        saved = sys.modules.pop("comfyui_workflow_templates", None)
        try:
            self.assertEqual(observe_template_digests(), ())
        finally:
            if saved is not None:
                sys.modules["comfyui_workflow_templates"] = saved

    def test_an_oversized_template_is_skipped_rather_than_read(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "video_minimax_h3_i2v.json").write_bytes(b"x" * 16)
            with patch(
                "comfyui_h3_context.adapters.comfyui_generation_profile.MAX_TEMPLATE_BYTES",
                8,
            ):
                self.assertEqual(observe_template_digests(root), ())

    def test_benign_template_byte_changes_are_admitted_by_structure(self) -> None:
        inventory: dict[str, list[str]] = {}
        for _slot, folder, filename in TEMPLATE_DEFAULT_ASSETS:
            inventory.setdefault(folder, []).append(filename)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name, _digest in PINNED_TEMPLATE_DIGESTS:
                (root / f"{name}.json").write_text(
                    json.dumps(template_payload(name), indent=2), encoding="utf-8"
                )
            with _HostModules(
                nodes=node_module(*ANCHOR_NODE_TYPES),
                comfyui_version=version_module("0.33.0"),
                folder_paths=folder_module(**inventory),
            ):
                profile = build_generation_profile(root)
        for entry in profile.families:
            with self.subTest(basis=entry.template_name):
                self.assertTrue(entry.available)

    def test_only_the_basis_missing_its_prompt_socket_is_refused(self) -> None:
        inventory: dict[str, list[str]] = {}
        for _slot, folder, filename in TEMPLATE_DEFAULT_ASSETS:
            inventory.setdefault(folder, []).append(filename)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name, _digest in PINNED_TEMPLATE_DIGESTS:
                (root / f"{name}.json").write_text(
                    json.dumps(
                        template_payload(name, include_prompt=name != "video_minimax_h3_t2v")
                    ),
                    encoding="utf-8",
                )
            with _HostModules(
                nodes=node_module(*ANCHOR_NODE_TYPES),
                comfyui_version=version_module("0.33.0"),
                folder_paths=folder_module(**inventory),
            ):
                profile = build_generation_profile(root)
        self.assertIs(profile.for_task_mode("t2va").disposition, FamilyDisposition.TEMPLATE_DRIFT)
        for mode in ("i2va", "fl2va", "l2va", "ref2va"):
            with self.subTest(mode=mode):
                self.assertTrue(profile.for_task_mode(mode).available)

    def test_nested_template_parser_projects_only_anchor_input_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            name = "video_minimax_h3_i2v"
            (root / f"{name}.json").write_text(json.dumps(template_payload(name)), encoding="utf-8")
            observed = observe_template_capabilities(root)
        self.assertEqual(
            observed,
            (
                TemplateCapability(
                    template_name=name,
                    anchor_node_type="MiniMaxH3ImageToVideo",
                    input_names=frozenset({"prompt"}),
                ),
            ),
        )
        self.assertNotIn("widgets_values", repr(observed))

    def test_only_active_subgraphs_and_string_prompt_sockets_count_as_capability(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            name = "video_minimax_h3_i2v"
            payload = template_payload(name)
            payload["nodes"] = []
            path = root / f"{name}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(observe_template_capabilities(root), ())

            payload = template_payload(name)
            definitions = cast(dict[str, object], payload["definitions"])
            subgraphs = cast(list[object], definitions["subgraphs"])
            subgraph = cast(dict[str, object], subgraphs[0])
            nodes = cast(list[object], subgraph["nodes"])
            anchor = cast(dict[str, object], nodes[1])
            anchor["inputs"] = [{"name": "prompt", "type": "IMAGE"}]
            path.write_text(json.dumps(payload), encoding="utf-8")
            observed = observe_template_capabilities(root)
        self.assertEqual(len(observed), 1)
        self.assertNotIn("prompt", observed[0].input_names)

    def test_partial_asset_resolution_is_completed_from_the_legacy_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "comfyui_workflow_templates"
            legacy = package / "templates"
            legacy.mkdir(parents=True)
            resolved_root = Path(raw) / "resolved"
            resolved_root.mkdir()
            names = [
                name
                for name, _anchor in generation_profile_adapter.TEMPLATE_CAPABILITY_REQUIREMENTS
            ]
            (resolved_root / f"{names[0]}.json").write_text(
                json.dumps(template_payload(names[0])), encoding="utf-8"
            )
            for name in names[1:]:
                (legacy / f"{name}.json").write_text(
                    json.dumps(template_payload(name)), encoding="utf-8"
                )
            module = types.ModuleType("comfyui_workflow_templates")
            module.__file__ = str(package / "__init__.py")

            def get_asset_path(template_id: str, filename: str) -> str | None:
                return str(resolved_root / filename) if template_id == names[0] else None

            module.get_asset_path = get_asset_path  # type: ignore[attr-defined]
            with _HostModules(comfyui_workflow_templates=module):
                observed = observe_template_capabilities()
        self.assertEqual([item.template_name for item in observed], names)

    def test_malformed_resolved_asset_is_not_hidden_by_a_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "comfyui_workflow_templates"
            legacy = package / "templates"
            legacy.mkdir(parents=True)
            resolved_root = Path(raw) / "resolved"
            resolved_root.mkdir()
            names = [
                name
                for name, _anchor in generation_profile_adapter.TEMPLATE_CAPABILITY_REQUIREMENTS
            ]
            blocked = names[1]
            for name in names:
                (legacy / f"{name}.json").write_text(
                    json.dumps(template_payload(name)), encoding="utf-8"
                )
            (resolved_root / f"{blocked}.json").write_text("not-json", encoding="utf-8")
            module = types.ModuleType("comfyui_workflow_templates")
            module.__file__ = str(package / "__init__.py")

            def get_asset_path(template_id: str, filename: str) -> str | None:
                return str(resolved_root / filename) if template_id == blocked else None

            module.get_asset_path = get_asset_path  # type: ignore[attr-defined]
            with _HostModules(comfyui_workflow_templates=module):
                observed = observe_template_capabilities()
        self.assertEqual(
            [item.template_name for item in observed],
            [name for name in names if name != blocked],
        )

    def test_duplicate_members_and_exhausted_structure_budget_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            name = "video_minimax_h3_i2v"
            path = root / f"{name}.json"
            path.write_text(
                '{"nodes":[],"nodes":[{"type":"MiniMaxH3ImageToVideo",'
                '"inputs":[{"name":"prompt"}]}]}',
                encoding="utf-8",
            )
            self.assertEqual(observe_template_capabilities(root), ())

            nested: dict[str, object] = template_payload(name)
            cursor = nested
            for _ in range(generation_profile_adapter.MAX_TEMPLATE_STRUCTURE_DEPTH + 1):
                child: dict[str, object] = {}
                cursor["nested"] = child
                cursor = child
            path.write_text(json.dumps(nested), encoding="utf-8")
            self.assertEqual(observe_template_capabilities(root), ())

            cyclic = {
                "nodes": [{"type": "loop", "inputs": []}],
                "definitions": {
                    "subgraphs": [
                        {
                            "id": "loop",
                            "nodes": [
                                {
                                    "type": "MiniMaxH3ImageToVideo",
                                    "inputs": [{"name": "prompt", "type": "STRING"}],
                                },
                                {"type": "loop", "inputs": []},
                            ],
                        }
                    ]
                },
            }
            path.write_text(json.dumps(cyclic), encoding="utf-8")
            self.assertEqual(observe_template_capabilities(root), ())

            malformed_duplicate = {
                "nodes": [{"type": "duplicate", "inputs": []}],
                "definitions": {
                    "subgraphs": [
                        {
                            "id": "duplicate",
                            "nodes": [
                                {
                                    "type": "MiniMaxH3ImageToVideo",
                                    "inputs": [{"name": "prompt", "type": "STRING"}],
                                }
                            ],
                        },
                        {"id": "duplicate", "nodes": "malformed"},
                    ]
                },
            }
            path.write_text(json.dumps(malformed_duplicate), encoding="utf-8")
            self.assertEqual(observe_template_capabilities(root), ())

            nonstandard_constant = template_payload(name)
            nonstandard_constant["metadata"] = float("nan")
            path.write_text(json.dumps(nonstandard_constant), encoding="utf-8")
            self.assertEqual(observe_template_capabilities(root), ())


class RouteRegistrationTests(unittest.TestCase):
    def test_registration_declines_without_an_owned_host_server(self) -> None:
        from comfyui_h3_context.adapters.comfyui_generation_profile import (
            ensure_generation_profile_route_registered,
        )

        self.assertFalse(ensure_generation_profile_route_registered())

    def test_the_route_path_is_the_frozen_read_only_one(self) -> None:
        self.assertEqual(GENERATION_PROFILE_ROUTE, "/h3-context/v1/generation/profile")


if __name__ == "__main__":
    unittest.main()


class OfficialNameResolutionTests(unittest.TestCase):
    """M17-28: admission accepts every official basename, from anywhere in the slot's folder.

    The defect these rows pin was an equality test between one published default and
    `folder_paths.get_filename_list`, which returns *relative paths* under every configured
    root. A user who installed an official non-default quantization, or who moved the weights
    into a subdirectory, was told the weight was missing on a fully provisioned host.
    """

    def _present(self, **inventory: list[str]) -> set[AssetSlot]:
        with _HostModules(folder_paths=folder_module(**inventory)):
            return {slot.slot for slot in observe_slots() if slot.present}

    def test_manifest_versions_internal_materialization_without_widening_profile_v1(self) -> None:
        """M23-05: LoRA roles are template facts, never public AssetSlot values."""

        manifest_path = (
            REPO_ROOT / "comfyui_h3_context" / "contracts" / "official_h3_assets_v2.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "h3.context.official_assets.v2")
        self.assertEqual(
            manifest["profile_families"],
            {
                "image_to_video": [
                    "video_unet",
                    "text_encoder",
                    "video_vae",
                    "audio_vae",
                ],
                "reference_to_video": [
                    "reference_unet",
                    "text_encoder",
                    "video_vae",
                    "audio_vae",
                ],
            },
        )
        self.assertEqual(
            set(AssetSlot),
            {
                AssetSlot.VIDEO_UNET,
                AssetSlot.REFERENCE_UNET,
                AssetSlot.TEXT_ENCODER,
                AssetSlot.VIDEO_VAE,
                AssetSlot.AUDIO_VAE,
            },
        )
        self.assertEqual(
            manifest["materialization_families"]["image_to_video"][-1],
            "image_turbo_lora",
        )
        self.assertEqual(
            manifest["materialization_families"]["reference_to_video"][-1],
            "reference_turbo_lora",
        )

    def test_the_published_default_still_satisfies_its_slot(self) -> None:
        """Widening admission must not stop accepting the name the template ships."""

        for slot, folder, default, _accepted in OFFICIAL_SLOT_ASSETS:
            with self.subTest(slot=slot):
                self.assertIn(slot, self._present(**{folder: [default]}))

    def test_python_derives_the_official_names_from_the_packaged_shared_manifest(self) -> None:
        """M17-29: JSON is the one Python/TypeScript filename authority."""

        manifest_path = (
            REPO_ROOT / "comfyui_h3_context" / "contracts" / "official_h3_assets_v2.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_rows = tuple(
            (
                AssetSlot(row["slot"]),
                row["folder_category"],
                row["template_default"],
                tuple(row["accepted_basenames"]),
            )
            for row in manifest["slots"]
            if row["slot"] in {slot.value for slot in AssetSlot}
        )
        self.assertEqual(OFFICIAL_SLOT_ASSETS, manifest_rows)
        adapter_source = (
            REPO_ROOT / "comfyui_h3_context" / "adapters" / "comfyui_generation_profile.py"
        ).read_text(encoding="utf-8")
        for row in manifest["slots"]:
            for basename in row["accepted_basenames"]:
                self.assertNotIn(basename, adapter_source)

    def test_manifest_parser_rejects_policy_order_loader_and_name_drift(self) -> None:
        """M17-29: Python enforces the same bounded authority as TypeScript."""

        manifest_path = (
            REPO_ROOT / "comfyui_h3_context" / "contracts" / "official_h3_assets_v2.json"
        )
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        candidates: list[tuple[str, dict[str, object]]] = []

        wrong_policy = deepcopy(original)
        wrong_policy["policy_id"] = "another_policy"
        candidates.append(("policy", wrong_policy))

        swapped_slots = deepcopy(original)
        swapped_slots["slots"][0], swapped_slots["slots"][1] = (
            swapped_slots["slots"][1],
            swapped_slots["slots"][0],
        )
        candidates.append(("slot order", swapped_slots))

        missing_profile_scope = deepcopy(original)
        del missing_profile_scope["profile_families"]
        candidates.append(("profile scope", missing_profile_scope))

        widened_profile_scope = deepcopy(original)
        widened_profile_scope["profile_families"]["image_to_video"].append("image_turbo_lora")
        candidates.append(("profile widening", widened_profile_scope))

        missing_materialization_role = deepcopy(original)
        missing_materialization_role["materialization_families"]["image_to_video"].pop()
        candidates.append(("materialization role", missing_materialization_role))

        missing_loader = deepcopy(original)
        missing_loader["slots"][0]["loader_type"] = ""
        candidates.append(("loader", missing_loader))

        unsafe_widget = deepcopy(original)
        unsafe_widget["slots"][0]["widget_name"] = "../widget"
        candidates.append(("widget", unsafe_widget))

        duplicate_name = deepcopy(original)
        duplicate_name["slots"][0]["accepted_basenames"].append(
            duplicate_name["slots"][0]["accepted_basenames"][0]
        )
        candidates.append(("duplicate", duplicate_name))

        oversized_name = deepcopy(original)
        oversized_name["slots"][0]["accepted_basenames"][0] = "x" * 256
        candidates.append(("bounded name", oversized_name))

        for label, candidate in candidates:
            with self.subTest(label=label):
                with self.assertRaisesRegex(RuntimeError, "invalid official asset manifest"):
                    generation_profile_adapter._parse_official_asset_manifest(candidate)

    def test_an_official_non_default_quantization_satisfies_its_slot(self) -> None:
        """The maintainer's host holds `int8_convrot` where the template names
        `pruned_int8_convrot`. Both are published by the same official distribution."""

        self.assertIn(
            AssetSlot.VIDEO_UNET,
            self._present(diffusion_models=["minimax_h3_fl2va_int8_convrot.safetensors"]),
        )
        self.assertIn(
            AssetSlot.TEXT_ENCODER,
            self._present(text_encoders=["qwen3vl_32b_minimax_h3_int8_convrot.safetensors"]),
        )

    def test_a_relocated_weight_satisfies_its_slot_on_both_separators(self) -> None:
        """`get_filename_list` returns `os.path.relpath` entries, so a subdirectory
        shows up as `H3\\name` on Windows and `H3/name` on POSIX. Neither can ever
        equal a bare basename, which is the whole of the reported defect."""

        for entry in (
            "H3\\minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "H3/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "vendor\\H3\\nested\\minimax_h3_fl2va_bf16.safetensors",
        ):
            with self.subTest(entry=entry):
                self.assertIn(AssetSlot.VIDEO_UNET, self._present(diffusion_models=[entry]))

    def test_unsafe_inventory_paths_cannot_satisfy_an_official_slot(self) -> None:
        """A matching basename is insufficient when its relative path is unsafe."""

        default = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
        unsafe_entries = (
            f"../{default}",
            f"nested/../{default}",
            f"/{default}",
            f"\\{default}",
            f"C:\\models\\{default}",
            f"https://weights.invalid/{default}",
            f"nested//{default}",
            f"nested/./{default}",
            f"nested/\x00/{default}",
            f"\\\\server\\share\\{default}",
        )
        for entry in unsafe_entries:
            with self.subTest(entry=repr(entry)):
                self.assertNotIn(
                    AssetSlot.VIDEO_UNET,
                    self._present(diffusion_models=[entry]),
                )

    def test_matching_is_case_insensitive(self) -> None:
        """Recorded decision: the pinned host runs on case-insensitive Windows
        filesystems, so a case variant is the same file rather than another one."""

        self.assertIn(
            AssetSlot.AUDIO_VAE,
            self._present(vae=["MiniMax_H3_Audio_VAE_FP32.SafeTensors"]),
        )

    def test_a_non_official_near_miss_never_satisfies_a_slot(self) -> None:
        """`D2` still holds for anything the official distribution does not publish:
        no fuzzy matching, no stem prefix, no arbitrary pick."""

        self.assertEqual(
            self._present(
                diffusion_models=[
                    "minimax_h3_fl2va_pruned_int4_mine.safetensors",
                    "minimax_h3_fl2va.safetensors",
                    "my_minimax_h3_fl2va_pruned_int8_convrot.safetensors",
                    "minimax_h3_fl2va_pruned_int8_convrot.safetensors.bak",
                ]
            ),
            set(),
        )

    def test_a_slot_is_not_satisfied_from_another_folder_category(self) -> None:
        """Cross-category acceptance is out of scope: a UNet parked under
        `checkpoints/` is not an installed diffusion model."""

        self.assertEqual(
            self._present(checkpoints=["minimax_h3_fl2va_pruned_int8_convrot.safetensors"]),
            set(),
        )

    def test_the_two_vae_slots_stay_independent_in_one_shared_folder(self) -> None:
        """Both VAEs live in `vae`. Widening admission must not let either one
        answer for the other."""

        self.assertEqual(
            self._present(vae=["minimax_h3_video_vae_fp16.safetensors"]),
            {AssetSlot.VIDEO_VAE},
        )

    def test_an_empty_or_untyped_inventory_is_absent_rather_than_present(self) -> None:
        inventories: tuple[list[object], ...] = ([], [None, 17, b"bytes"])
        for inventory in inventories:
            with self.subTest(inventory=inventory):
                # Bound as a default so the stub answers with this iteration's
                # inventory rather than whatever the loop variable ends on.
                def answer(_folder: str, listing: list[object] = inventory) -> list[object]:
                    return listing

                module = host_folder_paths_module(resolver=answer)
                with _HostModules(folder_paths=module):
                    self.assertTrue(all(not slot.present for slot in observe_slots()))

    def test_the_widened_match_still_leaks_no_inventory(self) -> None:
        """AC-M17-28-05. The list is read and thrown away; only a boolean leaves."""

        private = [
            "H3\\a_users_private_model.safetensors",
            "H3\\minimax_h3_fl2va_int8_convrot.safetensors",
        ]
        with _HostModules(folder_paths=folder_module(diffusion_models=private)):
            slots = observe_slots()
        wire = repr([slot.to_wire() for slot in slots])
        self.assertNotIn("a_users_private_model", wire)
        self.assertNotIn("H3", wire)
        self.assertNotIn("int8_convrot", wire)

    def test_the_maintainer_host_recorded_in_c8ad1d2_is_relocated_not_missing(self) -> None:
        """AC-M17-28-02 and AC-M17-28-04. This is the exact host state the hand-off
        measured: every required weight installed, under `H3\\` and at a different
        official quantization, yet all three families reported `missing_asset`.

        The weights are installed, so `missing_asset` is now false and gone. The
        materialized workflow would still name files this host does not have, so
        `available` would be false too. `asset_relocated` is the fact."""

        with _HostModules(
            nodes=node_module(*ANCHOR_NODE_TYPES),
            comfyui_version=version_module(QUALIFIED_COMFYUI_HOST_VERSION),
            folder_paths=folder_module(
                diffusion_models=[
                    "H3\\minimax_h3_fl2va_int8_convrot.safetensors",
                    "H3\\minimax_h3_ref2va_int8_convrot.safetensors",
                ],
                text_encoders=["qwen3vl_32b_minimax_h3_int8_convrot.safetensors"],
                vae=[
                    "minimax_h3_video_vae_fp16.safetensors",
                    "minimax_h3_audio_vae_fp32.safetensors",
                ],
            ),
        ):
            profile = profile_from_stub_host()
        self.assertEqual(len(profile.families), 3)
        for entry in profile.families:
            with self.subTest(basis=entry.template_name):
                self.assertIs(entry.disposition, FamilyDisposition.ASSET_RELOCATED)
                self.assertIsNot(entry.disposition, FamilyDisposition.MISSING_ASSET)
                self.assertIs(entry.remediation, Remediation.SELECT_INSTALLED_ASSET_ON_CANVAS)
                # The named roles are the ones whose widget the user must change,
                # and only those: both VAEs are installed under their own names.
                self.assertNotIn(AssetSlot.VIDEO_VAE, entry.unsatisfied_slots)
                self.assertNotIn(AssetSlot.AUDIO_VAE, entry.unsatisfied_slots)
                self.assertIn(AssetSlot.TEXT_ENCODER, entry.unsatisfied_slots)

    def test_a_host_holding_the_published_defaults_is_available_not_relocated(self) -> None:
        """The relocated state must not swallow the ordinary ready host."""

        inventory: dict[str, list[str]] = {}
        for _slot, folder, filename in TEMPLATE_DEFAULT_ASSETS:
            inventory.setdefault(folder, []).append(filename)
        with _HostModules(
            nodes=node_module(*ANCHOR_NODE_TYPES),
            comfyui_version=version_module(QUALIFIED_COMFYUI_HOST_VERSION),
            folder_paths=folder_module(**inventory),
        ):
            profile = profile_from_stub_host()
        for entry in profile.families:
            with self.subTest(basis=entry.template_name):
                self.assertTrue(entry.available)
                self.assertEqual(entry.unsatisfied_slots, ())

    def test_a_default_installed_under_a_subdirectory_is_relocated(self) -> None:
        """The name is right and the path is not, so the widget still names a file
        the host cannot resolve. That is the whole reason `PRESENT` stays an exact
        string test rather than becoming a basename test."""

        with _HostModules(
            folder_paths=folder_module(vae=["nested\\minimax_h3_audio_vae_fp32.safetensors"])
        ):
            observed = {slot.slot: slot.disposition for slot in observe_slots()}
        self.assertIs(observed[AssetSlot.AUDIO_VAE], SlotDisposition.RELOCATED)
        self.assertIs(observed[AssetSlot.VIDEO_VAE], SlotDisposition.ABSENT)

    def test_a_genuinely_missing_slot_is_still_reported_missing(self) -> None:
        """AC-M17-28-03. Widening admission must not turn the warning off."""

        with _HostModules(
            nodes=node_module(*ANCHOR_NODE_TYPES),
            comfyui_version=version_module(QUALIFIED_COMFYUI_HOST_VERSION),
            folder_paths=folder_module(
                diffusion_models=["H3\\minimax_h3_fl2va_int8_convrot.safetensors"],
                text_encoders=[],
                vae=["minimax_h3_video_vae_fp16.safetensors"],
            ),
        ):
            profile = profile_from_stub_host()
        entry = profile.for_task_mode("i2va")
        self.assertIs(entry.disposition, FamilyDisposition.MISSING_ASSET)
        self.assertEqual(
            set(entry.unsatisfied_slots),
            {AssetSlot.VIDEO_UNET, AssetSlot.TEXT_ENCODER, AssetSlot.AUDIO_VAE},
        )
