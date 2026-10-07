"""M3-08 bounded supported-host driver contract tests."""

from __future__ import annotations

import copy
import importlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from comfyui_h3_context.core.capability_manifest import PUBLIC_NODE_IDS
from scripts.m3_08_host_e2e import (
    EXPECTED_HOST_REVISION,
    EXPECTED_HOST_VERSION,
    HOST_REQUIRED_MODULES,
    LATEST_COMPATIBLE_HOST_REVISION,
    LATEST_COMPATIBLE_HOST_VERSION,
    LATEST_COMPATIBLE_NATIVE_SOURCE_BLOB,
    M7_FIXTURE_OBSERVER_COUNTS,
    PROJECT_NODE_IDS,
    PROVIDER_TRANSPARENCY_NODE_ID,
    RELIABILITY_NODE_ID,
    SUPPORTED_FIXTURE_MODES,
    HostBlockedError,
    HostDriverError,
    _assert_audit_override_observations,
    _assert_native_graph_runtime_qualification,
    _assert_source_markers,
    _collect_observer_texts,
    _expected_registration_ids,
    _fetch_registration,
    _host_dependency_versions,
    _http_json,
    _native_graph_qualification,
    _native_schema_evidence,
    _native_source_semantics,
    _registration_evidence,
    _run_registration_safety_probe,
    _start_host,
    build_host_command,
    build_model_free_downstream_prompt,
    build_model_free_full_reference_prompt,
    build_model_free_local_reconstruction_prompt,
    build_model_free_perception_prompt,
    build_model_free_prompt,
    build_model_free_reference_prompt,
    load_fixture,
    redact_text,
    validate_fixture,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = ROOT / "workflows"
NATIVE_SOURCE_BLOB = "0b1840e851c248f89e9920159c3c8237fa2e7186"  # pragma: allowlist secret


def _native_object_info_fixture() -> dict[str, Any]:
    autogrow = {
        "ref_images": ("IMAGE", "ref_image", "ref_image_", 9),
        "ref_videos": ("IMAGE", "ref_video", "ref_video_", 3),
        "ref_video_audios": ("AUDIO", "ref_video_audio", "ref_video_audio_", 3),
        "ref_audios": ("AUDIO", "ref_audio", "ref_audio_", 3),
    }
    optional = {
        name: [
            "COMFY_AUTOGROW_V3",
            {
                "template": {
                    "input": {"required": {child: [socket_type, {}]}},
                    "prefix": prefix,
                    "min": 0,
                    "max": maximum,
                }
            },
        ]
        for name, (socket_type, child, prefix, maximum) in autogrow.items()
    }
    return {
        "MiniMaxH3ImageToVideo": {
            "name": "MiniMaxH3ImageToVideo",
            "input": {
                "required": {
                    "clip": ["CLIP", {}],
                    "vae": ["VAE", {}],
                    "prompt": ["STRING", {"multiline": True, "dynamicPrompts": True}],
                    "width": [
                        "INT",
                        {"default": 1344, "min": 32, "max": 16384, "step": 32},
                    ],
                    "height": [
                        "INT",
                        {"default": 768, "min": 32, "max": 16384, "step": 32},
                    ],
                    "length": [
                        "INT",
                        {
                            "default": 124,
                            "min": 5,
                            "max": 3600,
                            "step": 17,
                            "tooltip": "Frame count at 24 fps.",
                        },
                    ],
                },
                "optional": {"first_frame": ["IMAGE", {}], "last_frame": ["IMAGE", {}]},
            },
            "input_order": {
                "required": ["clip", "vae", "prompt", "width", "height", "length"],
                "optional": ["first_frame", "last_frame"],
            },
            "output": ["CONDITIONING", "LATENT"],
        },
        "MiniMaxH3ReferenceToVideo": {
            "name": "MiniMaxH3ReferenceToVideo",
            "input": {
                "required": {
                    "clip": ["CLIP", {}],
                    "vae": ["VAE", {}],
                    "audio_vae": ["VAE", {}],
                    "prompt": ["STRING", {"multiline": True, "dynamicPrompts": True}],
                    "width": [
                        "INT",
                        {"default": 1344, "min": 32, "max": 16384, "step": 32},
                    ],
                    "height": [
                        "INT",
                        {"default": 768, "min": 32, "max": 16384, "step": 32},
                    ],
                    "length": [
                        "INT",
                        {
                            "default": 124,
                            "min": 5,
                            "max": 3600,
                            "step": 17,
                            "tooltip": "Frame count at 24 fps.",
                        },
                    ],
                    "ref_image_size": [
                        "COMBO",
                        {
                            "default": "match",
                            "multiselect": False,
                            "options": ["match", "max"],
                            "tooltip": "Reference image sizing.",
                        },
                    ],
                },
                "optional": optional,
            },
            "input_order": {
                "required": [
                    "clip",
                    "vae",
                    "audio_vae",
                    "prompt",
                    "width",
                    "height",
                    "length",
                    "ref_image_size",
                ],
                "optional": list(autogrow),
            },
            "output": ["CONDITIONING", "LATENT"],
        },
        "MiniMaxH3SigmaShift": {
            "name": "MiniMaxH3SigmaShift",
            "input": {
                "required": {
                    "model": ["MODEL", {}],
                    "shift_video": [
                        "FLOAT",
                        {"default": 12.0, "min": 0.01, "max": 100.0, "step": 0.01},
                    ],
                    "shift_audio": [
                        "FLOAT",
                        {"default": 3.0, "min": 0.01, "max": 100.0, "step": 0.01},
                    ],
                }
            },
            "input_order": {
                "required": ["model", "shift_video", "shift_audio"],
            },
            "output": ["MODEL"],
        },
    }


class SupportedHostDriverTests(unittest.TestCase):
    def test_native_mode_contract_lane_is_explicit_and_not_model_free(self) -> None:
        from scripts import m3_08_host_e2e as driver

        self.assertIn("native_mode_contract", SUPPORTED_FIXTURE_MODES)
        self.assertTrue(hasattr(driver, "_native_contract_probe_evidence"))

    def test_native_contract_probe_retains_real_classes_and_stubs_only_execute(self) -> None:
        native_schema = _native_schema_evidence(_native_object_info_fixture())
        classes = [
            {
                "node_id": node_id,
                "module": "comfy_extras.nodes_minimax_h3",
                "qualname": node_id,
                "class_identity": f"comfy_extras.nodes_minimax_h3.{node_id}",
                "schema_owner_identity": f"comfy_extras.nodes_minimax_h3.{node_id}",
                "registry_source_identity_verified": True,
                "class_retained": True,
            }
            for node_id in (
                "MiniMaxH3ImageToVideo",
                "MiniMaxH3ReferenceToVideo",
                "MiniMaxH3SigmaShift",
            )
        ]
        receipts = [
            {
                "task_mode": mode,
                "native_node_id": (
                    "MiniMaxH3ReferenceToVideo" if mode == "ref2va" else "MiniMaxH3ImageToVideo"
                ),
                "prompt_socket": "STRING",
                "real_class_retained": True,
                "real_schema_retained": True,
                "native_node_queued": True,
                "graph_admitted": True,
                "weight_callable_stubbed": True,
                "stub_invoked": True,
                "result_identity": f"native-contract:{mode}",
                "weights_loaded": False,
                "model_output_generated": False,
                "provider_calls": False,
                "media_opened": False,
            }
            for mode in ("t2va", "i2va", "fl2va", "l2va", "ref2va")
        ]
        probe = {
            "schema": "h3-context-native-contract-probe/1",
            "status": "PASS",
            "host_version": EXPECTED_HOST_VERSION,
            "host_revision": EXPECTED_HOST_REVISION,
            "native_source_blob": NATIVE_SOURCE_BLOB,
            "native_schema_sha256": native_schema["schema_sha256"],
            "execution_projection": "real_native_weight_callable_stub_only",
            "class_records": classes,
            "execution_boundary": {
                "patched_attributes": ["execute"],
                "schema_observed_before_patch": True,
                "patch_scope": "isolated_host_process",
                "restored_after_probe": True,
            },
            "mode_receipts": receipts,
        }
        evidence = __import__(
            "scripts.m3_08_host_e2e", fromlist=["_native_contract_probe_evidence"]
        )._native_contract_probe_evidence(probe, native_schema)
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(evidence["qualification_scope"], "structural_native_contract_only")
        self.assertTrue(evidence["real_native_classes_retained"])
        self.assertTrue(evidence["real_native_schema_retained"])
        self.assertTrue(evidence["weight_callable_stub_only"])
        self.assertEqual(
            [item["task_mode"] for item in evidence["mode_receipts"]],
            ["t2va", "i2va", "fl2va", "l2va", "ref2va"],
        )

        mutations: tuple[tuple[Callable[[dict[str, Any]], object], str], ...] = (
            (
                lambda value: value.update(execution_projection="model_free_non_native_only"),
                "projection",
            ),
            (
                lambda value: value["class_records"][0].update(class_retained=False),
                "class",
            ),
            (lambda value: value["mode_receipts"].pop(), "mode"),
            (
                lambda value: value["mode_receipts"][0].update(weights_loaded=True),
                "weight",
            ),
            (
                lambda value: value["execution_boundary"].update(
                    patched_attributes=["define_schema", "execute"]
                ),
                "execute",
            ),
        )
        for mutate, message in mutations:
            hostile = copy.deepcopy(probe)
            mutate(hostile)
            with self.assertRaisesRegex(HostDriverError, message):
                __import__(
                    "scripts.m3_08_host_e2e", fromlist=["_native_contract_probe_evidence"]
                )._native_contract_probe_evidence(hostile, native_schema)

    def test_native_contract_prompts_queue_real_nodes_for_all_five_modes(self) -> None:
        from scripts import m3_08_host_e2e as driver

        prompts = driver._native_contract_prompts()
        self.assertEqual(tuple(prompts), ("t2va", "i2va", "fl2va", "l2va", "ref2va"))
        for mode, prompt in prompts.items():
            classes = {node["class_type"] for node in prompt.values()}
            expected = "MiniMaxH3ReferenceToVideo" if mode == "ref2va" else "MiniMaxH3ImageToVideo"
            self.assertIn(expected, classes)
            self.assertIn("H3NativeContractReceipt", classes)
            self.assertNotIn("PreviewAny", classes)
            native_id = next(
                node_id for node_id, node in prompt.items() if node["class_type"] == expected
            )
            receipt = next(
                node for node in prompt.values() if node["class_type"] == "H3NativeContractReceipt"
            )
            self.assertEqual(receipt["inputs"]["conditioning"], [native_id, 0])
        source = driver._native_contract_canary_source()
        self.assertIn('patched_attributes": ["execute"]', source)
        self.assertIn("host_nodes.NODE_CLASS_MAPPINGS.get", source)
        self.assertIn('"ui": {"text": [payload]}', source)
        self.assertIn('_IMAGE.__dict__["execute"]', source)
        self.assertIn('_REFERENCE.__dict__["execute"]', source)
        self.assertNotIn("define_schema =", source)
        self.assertNotIn('NODE_CLASS_MAPPINGS["MiniMaxH3', source)

    def test_h0_registration_authority_covers_every_public_node_id(self) -> None:
        from scripts import m3_08_host_e2e as driver

        expected = _expected_registration_ids() - driver.NATIVE_NODE_IDS
        self.assertEqual(expected, frozenset(PUBLIC_NODE_IDS))

    def test_media_lifecycle_h2_mode_and_report_contract_are_declared(self) -> None:
        from scripts import m3_08_host_e2e as driver

        self.assertIn("media_lifecycle", SUPPORTED_FIXTURE_MODES)
        self.assertTrue(hasattr(driver, "H2_CANARY_NODE_ID"))
        self.assertTrue(hasattr(driver, "_write_host_report"))
        args = driver._parser().parse_args(
            [
                "--host-python",
                sys.executable,
                "--mode",
                "media_lifecycle",
                "--report",
                str(ROOT / ".tmp" / "host-report.json"),
            ]
        )
        self.assertEqual(args.mode, ["media_lifecycle"])
        self.assertEqual(args.report.name, "host-report.json")

    def test_perception_producer_h1_mode_queues_exact_public_outputs(self) -> None:
        self.assertIn("perception_producers", SUPPORTED_FIXTURE_MODES)
        fixture = load_fixture(WORKFLOW_ROOT / "m15_01_perception_producers.json")
        validate_fixture(fixture)
        prompt = build_model_free_perception_prompt(fixture)
        observed = {
            tuple(node["inputs"]["source"])
            for node in prompt.values()
            if node["class_type"] == "PreviewAny"
        }
        self.assertEqual(observed, {("2", 0), ("3", 0), ("5", 0), ("6", 0)})
        self.assertEqual(prompt["2"]["inputs"]["image"], ["1", 0])
        self.assertEqual(prompt["5"]["inputs"]["audio"], ["4", 0])

    def test_downstream_producer_h1_mode_observes_every_public_stage(self) -> None:
        self.assertIn("downstream_producers", SUPPORTED_FIXTURE_MODES)
        fixture = load_fixture(WORKFLOW_ROOT / "m15_02_downstream_producers.json")
        validate_fixture(fixture)
        prompt = build_model_free_downstream_prompt(fixture)
        observed = {
            tuple(node["inputs"]["source"])
            for node in prompt.values()
            if node["class_type"] == "PreviewAny"
        }
        self.assertEqual(
            observed,
            {
                ("4", 0),
                ("4", 1),
                ("5", 0),
                ("5", 1),
                ("6", 0),
                ("7", 0),
                ("7", 1),
                ("8", 0),
                ("8", 1),
                ("9", 0),
                ("9", 1),
                ("10", 0),
                ("10", 1),
            },
        )

    def test_local_reconstruction_h1_mode_observes_the_complete_public_route(self) -> None:
        self.assertIn("local_reconstruction", SUPPORTED_FIXTURE_MODES)
        fixture = load_fixture(WORKFLOW_ROOT / "m13_10_local_reconstruction.json")
        validate_fixture(fixture)
        prompt = build_model_free_local_reconstruction_prompt(fixture)
        observed = {
            tuple(node["inputs"]["source"])
            for node in prompt.values()
            if node["class_type"] == "PreviewAny"
        }
        self.assertEqual(
            observed,
            {
                ("11", 0),
                ("11", 1),
                ("11", 2),
                ("12", 0),
                ("12", 1),
                ("13", 1),
                ("15", 0),
                ("16", 0),
                ("17", 0),
                ("17", 1),
                ("14", 0),
                ("14", 1),
                ("14", 2),
            },
        )
        self.assertEqual(prompt["1"]["inputs"]["image"], "h3_context_fixture_image_1.png")
        self.assertEqual(prompt["14"]["inputs"]["profiled_prompt"], ["11", 1])
        self.assertEqual(prompt["14"]["inputs"]["native_h3_wiring"], ["13", 1])

    def test_exact_host_frontend_canary_is_truthful_and_bounded(self) -> None:
        from scripts import m3_08_host_e2e as driver

        self.assertTrue(hasattr(driver, "build_exact_host_canary_report"))
        public_count = len(PUBLIC_NODE_IDS)
        report = driver.build_exact_host_canary_report(
            EXPECTED_HOST_VERSION,
            EXPECTED_HOST_REVISION,
            registered_project_ids=public_count,
        )
        self.assertEqual(report["schema"], "h3.host.canary.v1")
        self.assertEqual(report["status"], "not_run")
        self.assertEqual(len(report["findings"]), 5)
        self.assertTrue(all(item["outcome"] == "not_run" for item in report["findings"]))
        self.assertTrue(all("not probed" in item["detail"] for item in report["findings"]))
        self.assertNotIn("no browser extension", json.dumps(report, sort_keys=True))
        self.assertIn(EXPECTED_HOST_REVISION, report["host_profile"])
        self.assertNotIn("WEB_DIRECTORY", json.dumps(report, sort_keys=True))
        self.assertEqual(
            driver._frontend_fallback_dispositions(browser_result=None),
            {
                "node_only": "supported",
                "app_mode": "not_probed",
                "subgraph": "not_probed",
            },
        )
        self.assertEqual(
            driver._frontend_fallback_dispositions(
                browser_result={
                    "status": "PASS",
                    "assertions": [
                        "app_mode_execution_parity",
                        "subgraph_execution_parity",
                    ],
                },
            ),
            {
                "node_only": "supported",
                "app_mode": "executed_live",
                "subgraph": "executed_live",
            },
        )
        browser_report = driver.build_exact_host_canary_report(
            EXPECTED_HOST_VERSION,
            EXPECTED_HOST_REVISION,
            registered_project_ids=public_count,
            browser_supported=True,
        )
        self.assertEqual(browser_report["status"], "supported")
        self.assertTrue(all(item["outcome"] == "supported" for item in browser_report["findings"]))
        with self.assertRaises(HostDriverError):
            driver.build_exact_host_canary_report(
                EXPECTED_HOST_VERSION,
                EXPECTED_HOST_REVISION,
                registered_project_ids=0,
            )
        with self.assertRaises(HostDriverError):
            driver.build_exact_host_canary_report(
                EXPECTED_HOST_VERSION,
                "0" * 40,
                registered_project_ids=public_count,
            )

        latest_report = driver.build_exact_host_canary_report(
            LATEST_COMPATIBLE_HOST_VERSION,
            LATEST_COMPATIBLE_HOST_REVISION,
            registered_project_ids=public_count,
            browser_supported=True,
            expected_version=LATEST_COMPATIBLE_HOST_VERSION,
            expected_revision=LATEST_COMPATIBLE_HOST_REVISION,
            authority="latest_compatible_observation",
        )
        self.assertEqual(latest_report["status"], "supported")
        self.assertIn(LATEST_COMPATIBLE_HOST_REVISION, latest_report["host_profile"])
        self.assertEqual(latest_report["authority"], "latest_compatible_observation")

    def test_latest_native_source_profile_is_explicit_and_non_substituting(self) -> None:
        from scripts import m3_08_host_e2e as driver

        source = "\n".join(
            (
                "comfy.model_sampling.ModelSamplingAV",
                "model_sampling.set_parameters(shift=shift_video, audio_shift=shift_audio)",
                'to["minimax_h3_sigma_shift_video"] = shift_video',
                'to["minimax_h3_sigma_shift_audio"] = shift_audio',
            )
        )
        evidence = driver._native_source_semantics(
            source,
            LATEST_COMPATIBLE_NATIVE_SOURCE_BLOB,
            profile="latest-compatible",
        )
        self.assertEqual(evidence["qualification"], "latest_compatible_observation")
        self.assertEqual(evidence["supported_authority"], "non_substituting_b323")
        with self.assertRaisesRegex(HostDriverError, "source blob drifted"):
            driver._native_source_semantics(
                source,
                "0" * 40,
                profile="latest-compatible",
            )

    def test_coinstallation_host_state_and_auth_are_process_local(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as raw:
            base_root = Path(raw)
            with patch("scripts.m3_08_host_e2e.subprocess.Popen") as popen:
                _start_host(
                    host_python=Path("python.exe"),
                    host_root=Path("host"),
                    base_root=base_root,
                    port=8188,
                    isolate_coinstallation=True,
                )
            environment = popen.call_args.kwargs["env"]
            self.assertEqual(Path(environment["OPENCLAW_STATE_DIR"]), base_root / "openclaw-state")
            self.assertEqual(Path(environment["DOCTOR_STATE_DIR"]), base_root / "doctor-state")
            self.assertGreaterEqual(len(environment["OPENCLAW_ADMIN_TOKEN"]), 32)
            self.assertNotIn(
                environment["OPENCLAW_ADMIN_TOKEN"],
                " ".join(popen.call_args.args[0]),
            )

    def test_product_shell_fixtures_retain_real_backend_outputs_model_free(self) -> None:
        from scripts import m3_08_host_e2e as driver

        self.assertIn("product_shell_base", SUPPORTED_FIXTURE_MODES)
        self.assertIn("product_shell_reference", SUPPORTED_FIXTURE_MODES)
        self.assertIn("assistant_base", SUPPORTED_FIXTURE_MODES)
        self.assertIn("assistant_reference", SUPPORTED_FIXTURE_MODES)
        for name in ("base", "reference"):
            fixture = driver.load_fixture(ROOT / "workflows" / f"m15_03_product_shell_{name}.json")
            driver.validate_fixture(fixture)
            prompt = build_model_free_prompt(fixture)
            self.assertNotIn("7" if name == "base" else "10", prompt)
            shell_id = "6" if name == "base" else "9"
            self.assertEqual(
                prompt[shell_id]["class_type"],
                "comfyui_h3_context.H3Context.ProductShell",
            )
            observed = {
                tuple(node["inputs"]["source"])
                for node in prompt.values()
                if node["class_type"] == "PreviewAny"
            }
            self.assertIn((shell_id, 0), observed)
            self.assertIn((shell_id, 1), observed)

    def test_m15_09_assistant_fixtures_retain_shell_and_reference_edges(self) -> None:
        from scripts import m3_08_host_e2e as driver

        for name in ("base", "reference"):
            fixture = driver.load_fixture(ROOT / "workflows" / f"m15_09_assistant_{name}.json")
            driver.validate_fixture(fixture)
            prompt = (
                driver.build_model_free_reference_prompt(fixture)
                if name == "reference"
                else driver.build_model_free_prompt(fixture)
            )
            shell_id = "12" if name == "reference" else "8"
            native_id = "9" if name == "reference" else "6"
            self.assertNotIn(native_id, prompt)
            observed = {
                tuple(node["inputs"]["source"])
                for node in prompt.values()
                if node["class_type"] == "PreviewAny"
            }
            self.assertIn((shell_id, 0), observed)
            self.assertIn((shell_id, 1), observed)
            if name == "reference":
                registry = prompt["4"]["inputs"]["images"]
                self.assertEqual(registry, ["3", 0])

    def test_artifact_version_is_read_from_the_deployed_subject(self) -> None:
        from scripts import m3_08_host_e2e as driver

        self.assertTrue(hasattr(driver, "_project_version"))
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            subject = Path(temporary)
            (subject / "pyproject.toml").write_text(
                '[project]\nname = "minimax-h3-studio"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
            self.assertEqual(driver._project_version(subject), "0.1.0")

    def test_h2_canary_executes_bounded_media_lifecycle_only_when_enabled(self) -> None:
        self.assertNotIn(
            "comfyui_h3_context.H3Context.MediaLifecycleCanary",
            __import__("comfyui_h3_context").NODE_CLASS_MAPPINGS,
        )
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            canary_root = Path(temporary) / "h2-canary"
            canary_root.mkdir()
            (canary_root / ".h3-context-owned-h2-canary").write_text(
                "h3-context-h2-canary/1\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "H3_CONTEXT_HOST_H2_CANARY": "1",
                    "H3_CONTEXT_H2_CANARY_ROOT": str(canary_root),
                },
            ):
                module = importlib.import_module("comfyui_h3_context.host_canary")
                node = module.H3MediaLifecycleCanaryNode()
                results = {
                    scenario: json.loads(node.execute(scenario)[0])
                    for scenario in ("success", "cancellation", "timeout")
                }

        self.assertEqual(results["success"]["status"], "succeeded")
        self.assertTrue(results["success"]["artifact_released"])
        self.assertEqual(results["cancellation"]["status"], "cancelled")
        self.assertEqual(results["timeout"]["status"], "timed_out")
        for result in results.values():
            self.assertTrue(result["reaped"])
            self.assertTrue(result["reader_threads_joined"])
            self.assertTrue(result["cleanup_succeeded"])
            self.assertNotIn("argv", result)
            self.assertNotIn("path", result)

    def test_exact_sdist_deploys_without_a_privileged_symlink(self) -> None:
        from scripts import m3_08_host_e2e as driver

        self.assertTrue(hasattr(driver, "_deploy_subject"))
        deploy_subject = driver._deploy_subject
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            root = Path(temporary)
            source = root / "source" / "minimax_h3_context-0.1.0"
            source.mkdir(parents=True)
            (source / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            artifact = root / "subject.tar.gz"
            with tarfile.open(artifact, "w:gz") as archive:
                archive.add(source, arcname=source.name)
            subject = root / "custom_nodes" / "ComfyUI-H3-Context"
            subject.parent.mkdir()

            digest = deploy_subject(
                subject_path=subject,
                staging_path=root / "staging",
                artifact_path=artifact,
            )

            self.assertEqual(len(digest), 64)
            self.assertFalse(subject.is_symlink())
            self.assertEqual(
                (subject / "pyproject.toml").read_text(encoding="utf-8"),
                "[project]\n",
            )

    def test_exact_sdist_rejects_link_member(self) -> None:
        from scripts import m3_08_host_e2e as driver

        self.assertTrue(hasattr(driver, "_deploy_subject"))
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            root = Path(temporary)
            artifact = root / "subject.tar.gz"
            with tarfile.open(artifact, "w:gz") as archive:
                member = tarfile.TarInfo("minimax_h3_context-0.1.0/escape")
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
                archive.addfile(member)
            subject = root / "custom_nodes" / "ComfyUI-H3-Context"
            subject.parent.mkdir()
            with self.assertRaisesRegex(driver.HostBlockedError, "unsafe member type"):
                driver._deploy_subject(
                    subject_path=subject,
                    staging_path=root / "staging",
                    artifact_path=artifact,
                )
            self.assertFalse(subject.exists())

    def test_host_stop_reaps_real_owned_process(self) -> None:
        from scripts import m3_08_host_e2e as driver

        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if sys.platform == "win32" else 0
        )
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=sys.platform != "win32",
            text=True,
        )
        try:
            self.assertTrue(driver._stop_host(process, timeout=5))
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_host_stop_returns_false_when_taskkill_and_hard_kill_both_fail(self) -> None:
        from scripts import m3_08_host_e2e as driver

        class BrokenProcess:
            pid = 123

            @staticmethod
            def poll() -> None:
                return None

            @staticmethod
            def kill() -> None:
                raise OSError("hard kill failed")

            @staticmethod
            def wait(*, timeout: float) -> None:
                del timeout
                raise OSError("wait failed")

        with patch("scripts.m3_08_host_e2e.subprocess.run", side_effect=OSError("taskkill failed")):
            self.assertFalse(driver._stop_host(BrokenProcess(), timeout=1))  # type: ignore[arg-type]

    def test_combined_host_failure_preserves_primary_and_cleanup_evidence(self) -> None:
        from scripts import m3_08_host_e2e as driver

        lifecycle = [
            {
                "cycle": "fresh",
                "shutdown": "FAIL",
                "port_released": False,
                "cleanup": "FAIL",
                "cleanup_failure": "owned host process did not shut down",
            }
        ]
        error = driver._host_cycle_error("PRIMARY_FIRST_FAILURE", lifecycle)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("first_failure=PRIMARY_FIRST_FAILURE", str(error))
        self.assertIn("cleanup_failure=owned host process did not shut down", str(error))
        self.assertEqual(error.evidence["cleanup"], "FAIL")
        self.assertEqual(error.evidence["port_release"], "FAIL")
        self.assertEqual(error.evidence["loopback_cycles"], lifecycle)
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            report = Path(temporary) / "combined-failure.json"
            with (
                patch("scripts.m3_08_host_e2e.run_supported_host", side_effect=error),
                patch("builtins.print"),
            ):
                exit_code = driver.main(
                    [
                        "--host-python",
                        str(ROOT / ".tmp" / "host" / "python.exe"),
                        "--report",
                        str(report),
                    ]
                )
            self.assertEqual(exit_code, 1)
            persisted = json.loads(report.read_text(encoding="utf-8"))
            self.assertIn("first_failure=PRIMARY_FIRST_FAILURE", persisted["reason"])
            self.assertIn("cleanup_failure=", persisted["reason"])
            self.assertEqual(persisted["cleanup"], "FAIL")
            self.assertEqual(persisted["port_release"], "FAIL")
            self.assertEqual(persisted["loopback_cycles"], lifecycle)

    def test_owned_host_temp_root_cleanup_denial_fails_closed(self) -> None:
        from scripts import m3_08_host_e2e as driver

        planning_root = ROOT / ".tmp"
        planning_root.mkdir(exist_ok=True)
        retained_root: Path | None = None
        try:
            with (
                patch(
                    "scripts.m3_08_host_e2e.shutil.rmtree",
                    side_effect=PermissionError("cleanup denied"),
                ),
                self.assertRaises(driver.HostDriverError) as captured,
            ):
                with driver._OwnedHostTemporaryRoot(planning_root) as owned_root:
                    retained_root = owned_root
                    raise driver.HostDriverError(
                        "PRIMARY_FIRST_FAILURE",
                        evidence={"port_release": "PASS"},
                    )

            error = captured.exception
            self.assertIn("first_failure=PRIMARY_FIRST_FAILURE", str(error))
            self.assertIn(
                "cleanup_failure=owned temp root cleanup raised PermissionError", str(error)
            )
            self.assertEqual(error.evidence["cleanup"], "FAIL")
            self.assertEqual(error.evidence["cleanup_failure"], "PermissionError")
            self.assertEqual(error.evidence["port_release"], "PASS")
            self.assertIsNotNone(retained_root)
            assert retained_root is not None
            self.assertTrue(retained_root.exists())

            with tempfile.TemporaryDirectory(dir=planning_root) as temporary:
                report = Path(temporary) / "cleanup-denial.json"
                with (
                    patch("scripts.m3_08_host_e2e.run_supported_host", side_effect=error),
                    patch("builtins.print"),
                ):
                    exit_code = driver.main(
                        [
                            "--host-python",
                            str(ROOT / ".tmp" / "host" / "python.exe"),
                            "--report",
                            str(report),
                        ]
                    )
                self.assertEqual(exit_code, 1)
                persisted = json.loads(report.read_text(encoding="utf-8"))
                self.assertEqual(persisted["cleanup"], "FAIL")
                self.assertEqual(persisted["cleanup_failure"], "PermissionError")
        finally:
            if retained_root is not None and retained_root.exists():
                shutil.rmtree(retained_root)

    def test_owned_host_temp_root_preserves_identity_replacement(self) -> None:
        from scripts import m3_08_host_e2e as driver

        planning_root = ROOT / ".tmp"
        planning_root.mkdir(exist_ok=True)
        manager = driver._OwnedHostTemporaryRoot(planning_root)
        replacement_root = manager.path
        try:
            with self.assertRaises(driver.HostDriverError) as captured:
                with manager as owned_root:
                    # The manager owns a private marker inside the root; replace the exact root
                    # as a caller would, without assuming the filesystem allocates a new inode.
                    shutil.rmtree(owned_root)
                    owned_root.mkdir()
                    (owned_root / "foreign.marker").write_text("caller-owned\n", encoding="utf-8")

            error = captured.exception
            self.assertIn("cleanup_failure=owned temp root identity changed", str(error))
            self.assertEqual(error.evidence["cleanup"], "FAIL")
            self.assertEqual(error.evidence["cleanup_failure"], "identity_changed")
            self.assertTrue(replacement_root.is_dir())
            self.assertEqual(
                (replacement_root / "foreign.marker").read_text(encoding="utf-8"),
                "caller-owned\n",
            )
        finally:
            if replacement_root.exists():
                shutil.rmtree(replacement_root)

    def test_owned_host_temp_root_path_cannot_be_reassigned_out_of_root(self) -> None:
        from scripts import m3_08_host_e2e as driver

        planning_root = ROOT / ".tmp"
        planning_root.mkdir(exist_ok=True)
        manager = driver._OwnedHostTemporaryRoot(planning_root)
        original_root = manager.path
        with tempfile.TemporaryDirectory(dir=planning_root) as caller_root_name:
            caller_root = Path(caller_root_name)
            marker = caller_root / "foreign.marker"
            marker.write_text("caller-owned\n", encoding="utf-8")
            try:
                with self.assertRaises(AttributeError):
                    manager.path = caller_root  # type: ignore[misc]
                with manager as owned_root:
                    self.assertEqual(owned_root, original_root)
                self.assertFalse(original_root.exists())
                self.assertEqual(marker.read_text(encoding="utf-8"), "caller-owned\n")
            finally:
                if original_root.exists():
                    shutil.rmtree(original_root)

    def test_owned_host_temp_root_symlink_boundary_never_calls_rmtree(self) -> None:
        from scripts import m3_08_host_e2e as driver

        planning_root = ROOT / ".tmp"
        planning_root.mkdir(exist_ok=True)
        manager = driver._OwnedHostTemporaryRoot(planning_root)
        owned_root = manager.path
        try:
            with (
                patch.object(Path, "is_symlink", return_value=True),
                patch("scripts.m3_08_host_e2e.shutil.rmtree") as remove,
                self.assertRaises(driver.HostDriverError) as captured,
            ):
                with manager:
                    pass
            remove.assert_not_called()
            self.assertEqual(captured.exception.evidence["cleanup"], "FAIL")
            self.assertEqual(captured.exception.evidence["cleanup_failure"], "identity_changed")
            self.assertTrue(owned_root.is_dir())
        finally:
            if owned_root.exists():
                shutil.rmtree(owned_root)

    def test_http_json_reads_large_object_info_payload_without_truncation(self) -> None:
        payload = {"nodes": "x" * 20_000}
        encoded = json.dumps(payload).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = _http_json(f"http://127.0.0.1:{server.server_port}", "/object_info")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(result, payload)

    def test_http_json_rejects_response_over_finite_bound(self) -> None:
        payload = json.dumps({"nodes": "x" * 100}).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("scripts.m3_08_host_e2e.MAX_JSON_RESPONSE_BYTES", 32):
                with self.assertRaises(HostDriverError):
                    _http_json(f"http://127.0.0.1:{server.server_port}", "/object_info")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_host_preflight_names_declared_runtime_dependencies(self) -> None:
        self.assertIn("torch", HOST_REQUIRED_MODULES)
        self.assertIn("comfy_aimdo", HOST_REQUIRED_MODULES)
        self.assertIn("comfy_kitchen", HOST_REQUIRED_MODULES)

    def test_host_dependency_versions_are_machine_readable_and_complete(self) -> None:
        payload = {name: f"version-{index}" for index, name in enumerate(HOST_REQUIRED_MODULES)}
        completed = subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        with patch("scripts.m3_08_host_e2e.subprocess.run", return_value=completed):
            versions = _host_dependency_versions(Path(sys.executable))
        self.assertEqual(versions, payload)
        incomplete = subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout=json.dumps({"torch": "version-only"}),
            stderr="",
        )
        with (
            patch("scripts.m3_08_host_e2e.subprocess.run", return_value=incomplete),
            self.assertRaises(HostBlockedError),
        ):
            _host_dependency_versions(Path(sys.executable))

    def test_browser_report_declares_live_app_and_subgraph_execution(self) -> None:
        from scripts import m3_08_host_e2e as driver

        receipt = {
            "schema": "h3.frontend.performance.v1",
            "source": "browser_user_timing",
            "mount_ms": 2.0,
            "refresh_ms": 1.0,
            "decode_ms": 3.0,
            "render_ms": 4.0,
            "projection_peak_bytes": 4096,
            "projection_update_count": 2,
            "graph_event_count": 6,
            "refresh_count": 3,
            "coalesced_event_count": 3,
            "cleanup_verified": True,
            "diagnostic_codes": [],
        }
        completed_wire = (
            "1 passed\nH3_CONTEXT_PERFORMANCE_RECEIPT=" + json.dumps(receipt, separators=(",", ":"))
        ).encode("utf-8")

        def browser_result(payload: bytes) -> Callable[..., subprocess.CompletedProcess[Any]]:
            def run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[Any]:
                text_mode = kwargs.get("text") is True
                return subprocess.CompletedProcess(
                    args=["pnpm"],
                    returncode=0,
                    stdout=payload.decode("utf-8", errors="replace") if text_mode else payload,
                    stderr="" if text_mode else b"",
                )

            return run

        with (
            patch("scripts.m3_08_host_e2e.shutil.which", return_value="pnpm"),
            patch(
                "scripts.m3_08_host_e2e.subprocess.run",
                side_effect=browser_result(completed_wire),
            ),
        ):
            report = driver._run_browser_e2e(
                "http://127.0.0.1:8188",
                timeout=60.0,
                output_root=ROOT / ".tmp" / "browser-output",
            )
        self.assertEqual(report["status"], "PASS")
        self.assertIn("app_mode_execution_parity", report["assertions"])
        self.assertIn("subgraph_execution_parity", report["assertions"])
        self.assertIn("frontend_performance_receipt", report["assertions"])
        self.assertEqual(report["performance_receipt"], receipt)

        driver._validate_browser_performance_receipt(receipt)
        mutations: tuple[Callable[[dict[str, Any]], object], ...] = (
            lambda value: value.update(cleanup_verified=False),
            lambda value: value.update(projection_peak_bytes=131_073),
            lambda value: value.update(coalesced_event_count=0),
            lambda value: value.update(raw_prompt="must not be retained"),
        )
        for mutation in mutations:
            hostile = copy.deepcopy(receipt)
            mutation(hostile)
            with self.assertRaises(HostDriverError):
                driver._validate_browser_performance_receipt(hostile)

        duplicate = completed_wire.replace(
            b'"cleanup_verified":true',
            b'"cleanup_verified":false,"cleanup_verified":true',
        )
        invalid_utf8 = completed_wire + b"\xff"
        for hostile_stdout in (duplicate, invalid_utf8):
            with (
                patch("scripts.m3_08_host_e2e.shutil.which", return_value="pnpm"),
                patch(
                    "scripts.m3_08_host_e2e.subprocess.run",
                    side_effect=browser_result(hostile_stdout),
                ),
                self.assertRaises(HostDriverError),
            ):
                driver._run_browser_e2e(
                    "http://127.0.0.1:8188",
                    timeout=60.0,
                    output_root=ROOT / ".tmp" / "browser-output",
                )

    def test_registration_evidence_inventories_exact_ids_and_object_info(self) -> None:
        from scripts import m3_08_host_e2e as driver

        expected = driver._expected_registration_ids(include_h2_canary=True)
        info = {node_id: {"name": node_id, "output": ["STRING"]} for node_id in expected}
        info.update(_native_object_info_fixture())
        evidence = _registration_evidence(info, include_h2_canary=True)
        self.assertEqual(evidence["schema"], "h3-context-registration-evidence/1")
        self.assertEqual(evidence["registered_ids"], sorted(expected))
        self.assertIn(driver.H2_CANARY_NODE_ID, evidence["project_ids"])
        self.assertEqual(evidence["native_ids"], sorted(driver.NATIVE_NODE_IDS))
        self.assertRegex(evidence["object_info_sha256"], r"^sha256:[0-9a-f]{64}$")
        incomplete = dict(info)
        del incomplete[sorted(expected)[0]]
        with self.assertRaises(HostDriverError):
            _registration_evidence(incomplete, include_h2_canary=True)

    def test_native_schema_evidence_pins_keyframes_and_zero_based_autogrow(self) -> None:
        info = _native_object_info_fixture()
        evidence = _native_schema_evidence(info)
        self.assertEqual(evidence["schema"], "h3-context-native-schema-evidence/1")
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(evidence["keyframe_inputs"], ["first_frame", "last_frame"])
        self.assertEqual(
            evidence["prompt_sockets"],
            {
                "MiniMaxH3ImageToVideo": "STRING",
                "MiniMaxH3ReferenceToVideo": "STRING",
            },
        )
        self.assertEqual(evidence["sigma_shift_defaults"], {"audio": 3.0, "video": 12.0})
        self.assertEqual(
            evidence["required_socket_types"],
            {
                "MiniMaxH3ImageToVideo": {
                    "clip": "CLIP",
                    "vae": "VAE",
                    "prompt": "STRING",
                    "width": "INT",
                    "height": "INT",
                    "length": "INT",
                },
                "MiniMaxH3ReferenceToVideo": {
                    "clip": "CLIP",
                    "vae": "VAE",
                    "audio_vae": "VAE",
                    "prompt": "STRING",
                    "width": "INT",
                    "height": "INT",
                    "length": "INT",
                    "ref_image_size": "COMBO",
                },
            },
        )
        self.assertEqual(
            evidence["numeric_inputs"],
            {
                "width": {"default": 1344, "min": 32, "max": 16384, "step": 32},
                "height": {"default": 768, "min": 32, "max": 16384, "step": 32},
                "length": {"default": 124, "min": 5, "max": 3600, "step": 17},
            },
        )
        self.assertEqual(
            evidence["ref_image_size"],
            {"options": ["match", "max"], "default": "match", "multiselect": False},
        )
        self.assertEqual(
            evidence["autogrow_paths"],
            [
                "ref_images.ref_image_0",
                "ref_videos.ref_video_0",
                "ref_video_audios.ref_video_audio_0",
                "ref_audios.ref_audio_0",
            ],
        )
        self.assertRegex(evidence["schema_sha256"], r"^sha256:[0-9a-f]{64}$")

        drifted = copy.deepcopy(info)
        drifted["MiniMaxH3ReferenceToVideo"]["input"]["optional"]["ref_images"][1]["template"][
            "prefix"
        ] = "ref_image_1"
        with self.assertRaisesRegex(HostDriverError, "ref_images"):
            _native_schema_evidence(drifted)

        prompt_drift = copy.deepcopy(info)
        prompt_drift["MiniMaxH3ReferenceToVideo"]["input"]["required"]["prompt"][0] = "H3_PROMPT"
        with self.assertRaisesRegex(HostDriverError, "prompt"):
            _native_schema_evidence(prompt_drift)

        sigma_drift = copy.deepcopy(info)
        sigma_drift["MiniMaxH3SigmaShift"]["input"]["required"]["shift_video"][1]["default"] = 11.0
        with self.assertRaisesRegex(HostDriverError, "shift_video"):
            _native_schema_evidence(sigma_drift)

        type_drift = copy.deepcopy(info)
        type_drift["MiniMaxH3ImageToVideo"]["input"]["required"]["clip"][0] = "STRING"
        with self.assertRaisesRegex(HostDriverError, "clip"):
            _native_schema_evidence(type_drift)

        enum_drift = copy.deepcopy(info)
        enum_drift["MiniMaxH3ReferenceToVideo"]["input"]["required"]["ref_image_size"][1][
            "options"
        ] = ["match", "large"]
        with self.assertRaisesRegex(HostDriverError, "ref_image_size"):
            _native_schema_evidence(enum_drift)

        multiselect_drift = copy.deepcopy(info)
        multiselect_drift["MiniMaxH3ReferenceToVideo"]["input"]["required"]["ref_image_size"][1][
            "multiselect"
        ] = True
        with self.assertRaisesRegex(HostDriverError, "ref_image_size"):
            _native_schema_evidence(multiselect_drift)

    def test_supported_native_sampler_semantics_are_closed_and_material_drift_blocks(self) -> None:
        supported = """
class ModelSamplingAdvanced(
    comfy.model_sampling.ModelSamplingAV, comfy.model_sampling.CONST
):
    pass
model_sampling.set_parameters(shift=shift_video, audio_shift=shift_audio)
to["minimax_h3_sigma_shift_video"] = shift_video
to["minimax_h3_sigma_shift_audio"] = shift_audio
"""
        evidence = _native_source_semantics(
            supported,
            "0b1840e851c248f89e9920159c3c8237fa2e7186",  # pragma: allowlist secret
        )
        self.assertEqual(evidence["supported_sampler"], "ModelSamplingAV")
        self.assertEqual(evidence["supported_scheduler"], "model_sampling_av_video_audio")
        self.assertEqual(evidence["current_disposition"], "REQUALIFIED_EXACT")

        with self.assertRaisesRegex(HostDriverError, "sampler"):
            _native_source_semantics(
                supported.replace("ModelSamplingAV", "ModelSamplingDiscreteFlow"),
                "0b1840e851c248f89e9920159c3c8237fa2e7186",  # pragma: allowlist secret
            )

    def test_native_graph_qualification_joins_edges_manifest_order_and_ownership(self) -> None:
        fixture = load_fixture(WORKFLOW_ROOT / "m15_03_product_shell_reference.json")
        evidence = _native_graph_qualification(
            fixture, _native_schema_evidence(_native_object_info_fixture())
        )
        self.assertTrue(evidence["identity_order_ownership_verified"])
        self.assertTrue(evidence["source_authority_verified"])
        self.assertEqual(
            [binding["native_child_path"] for binding in evidence["bindings"]],
            [
                "MiniMaxH3ReferenceToVideo.ref_images.ref_image_0",
                "MiniMaxH3ReferenceToVideo.ref_images.ref_image_1",
            ],
        )

        stripped = copy.deepcopy(fixture)
        del stripped["prompt"]["10"]
        with self.assertRaisesRegex(HostDriverError, "native node"):
            _native_graph_qualification(
                stripped, _native_schema_evidence(_native_object_info_fixture())
            )

        reordered = copy.deepcopy(fixture)
        reordered["prompt"]["10"]["inputs"]["ref_images"].reverse()
        with self.assertRaisesRegex(HostDriverError, "order"):
            _native_graph_qualification(
                reordered, _native_schema_evidence(_native_object_info_fixture())
            )

        stripped_schema = _native_schema_evidence(_native_object_info_fixture())
        del stripped_schema["autogrow"]["ref_images"]
        with self.assertRaisesRegex(HostDriverError, "autogrow"):
            _native_graph_qualification(fixture, stripped_schema)

        duplicate = copy.deepcopy(fixture)
        duplicate["prompt"]["10"]["inputs"]["ref_images"][1] = ["2", 0]
        duplicate["expected"]["direct_media_links"][1]["source"] = "2"
        with self.assertRaisesRegex(HostDriverError, "ambiguous"):
            _native_graph_qualification(
                duplicate, _native_schema_evidence(_native_object_info_fixture())
            )

        source_swap = copy.deepcopy(fixture)
        source_swap["prompt"]["10"]["inputs"]["ref_images"].reverse()
        first = source_swap["expected"]["direct_media_links"][0]
        second = source_swap["expected"]["direct_media_links"][1]
        first["source"], second["source"] = second["source"], first["source"]
        with self.assertRaisesRegex(HostDriverError, "source authority"):
            _native_graph_qualification(
                source_swap, _native_schema_evidence(_native_object_info_fixture())
            )

    def test_native_runtime_qualification_rejects_cross_associated_binding_markers(self) -> None:
        fixture = load_fixture(WORKFLOW_ROOT / "m15_03_product_shell_reference.json")
        qualification = _native_graph_qualification(
            fixture, _native_schema_evidence(_native_object_info_fixture())
        )
        swapped = (
            "asset_id='image_1', presentation_label='<Picture 2>', "
            "native_child_path='MiniMaxH3ReferenceToVideo.ref_images.ref_image_0'; "
            "asset_id='image_2', presentation_label='<Picture 1>', "
            "native_child_path='MiniMaxH3ReferenceToVideo.ref_images.ref_image_1'"
        )
        with self.assertRaisesRegex(HostDriverError, "association"):
            _assert_native_graph_runtime_qualification({("9", 1): (swapped,)}, "9", qualification)

    def test_registration_safety_probe_requires_idempotence_and_atomic_collision(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout=json.dumps(
                {
                    "schema": "h3-context-registration-safety/1",
                    "status": "PASS",
                    "idempotent": True,
                    "foreign_preserved": True,
                    "collision_failed_closed": True,
                    "collision_atomic": True,
                }
            ),
            stderr="",
        )
        with patch("scripts.m3_08_host_e2e.subprocess.run", return_value=completed):
            result = _run_registration_safety_probe(Path(sys.executable), ROOT)
        self.assertEqual(result["status"], "PASS")
        failed = subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout=json.dumps(
                {
                    "schema": "h3-context-registration-safety/1",
                    "status": "FAIL",
                    "idempotent": True,
                    "foreign_preserved": True,
                    "collision_failed_closed": False,
                    "collision_atomic": True,
                }
            ),
            stderr="",
        )
        with (
            patch("scripts.m3_08_host_e2e.subprocess.run", return_value=failed),
            self.assertRaises(HostDriverError),
        ):
            _run_registration_safety_probe(Path(sys.executable), ROOT)

    def test_m7_fixture_modes_are_explicit_and_have_exact_observer_counts(self) -> None:
        self.assertIn("audit_override", SUPPORTED_FIXTURE_MODES)
        self.assertIn("provider_transparency", SUPPORTED_FIXTURE_MODES)
        self.assertIn("reliability", SUPPORTED_FIXTURE_MODES)
        self.assertEqual(M7_FIXTURE_OBSERVER_COUNTS["m7-03-audit-override"], 6)
        self.assertEqual(M7_FIXTURE_OBSERVER_COUNTS["m7-04-provider-transparency"], 6)
        self.assertEqual(M7_FIXTURE_OBSERVER_COUNTS["m7-05-reliability"], 7)

    def test_observer_marker_assertion_rejects_unrelated_global_marker(self) -> None:
        observed = {
            ("provider", 0): ("provider_defined=true",),
            ("report", 1): ("provider_defined=true",),
        }
        with self.assertRaises(HostDriverError):
            _assert_source_markers(
                observed,
                required_sources=(("provider", 0),),
                markers=("provider_defined", "upload_consent_required"),
                label="provider transparency",
            )

    def test_audit_override_evidence_is_bound_to_exact_public_sources(self) -> None:
        observed = {
            ("override", 0): ("exact edited prompt",),
            ("override", 1): ("diagnostic audit_override_applied",),
            ("override", 2): ("base_report_fingerprint=sha256:" + ("a" * 64),),
            ("validator", 0): ("ValidationStatus.PASSED h3-context-m10-05",),
            ("preview", 1): ("prompt sha256:" + ("b" * 64) + " report sha256:" + ("c" * 64),),
        }
        _assert_audit_override_observations(
            observed,
            override_id="override",
            validator_id="validator",
            preview_id="preview",
            edited_text="exact edited prompt",
            base_report_fingerprint="sha256:" + ("a" * 64),
            prompt_fingerprint="sha256:" + ("b" * 64),
            validated_report_fingerprint="sha256:" + ("c" * 64),
        )
        drifted = dict(observed)
        drifted[("preview", 1)] = ("unrelated global marker",)
        with self.assertRaises(HostDriverError):
            _assert_audit_override_observations(
                drifted,
                override_id="override",
                validator_id="validator",
                preview_id="preview",
                edited_text="exact edited prompt",
                base_report_fingerprint="sha256:" + ("a" * 64),
                prompt_fingerprint="sha256:" + ("b" * 64),
                validated_report_fingerprint="sha256:" + ("c" * 64),
            )

    def test_observer_texts_are_keyed_by_preview_source(self) -> None:
        prompt = {
            "1": {
                "class_type": "PreviewAny",
                "inputs": {"source": ["provider", 0]},
            },
            "2": {
                "class_type": "PreviewAny",
                "inputs": {"source": ["provider", 1]},
            },
        }
        outputs = {
            "1": {"text": [["status"]]},
            "2": {"text": [["disclosure"]]},
        }
        self.assertEqual(
            _collect_observer_texts(prompt, outputs),
            {("provider", 0): ("status",), ("provider", 1): ("disclosure",)},
        )

    def test_registration_falls_back_to_bounded_per_node_requests(self) -> None:
        from scripts import m3_08_host_e2e as driver

        expected_ids = set(PUBLIC_NODE_IDS) | set(driver.NATIVE_NODE_IDS)

        def fake_wait(
            _base_url: str,
            path: str,
            *,
            timeout: float,
            process: object = None,
        ) -> object:
            del timeout, process
            if path == "/object_info":
                raise HostDriverError("host JSON response exceeds 33554432 bytes for /object_info")
            node_id = path.rsplit("/", 1)[-1]
            if node_id in _native_object_info_fixture():
                return {node_id: _native_object_info_fixture()[node_id]}
            return {node_id: {"name": node_id}}

        with patch("scripts.m3_08_host_e2e._wait_for_json", side_effect=fake_wait):
            info = _fetch_registration("http://127.0.0.1:8188", timeout=1.0)
        self.assertEqual(set(info), expected_ids)

    def test_command_is_loopback_cpu_only_and_uses_isolated_roots(self) -> None:
        command = build_host_command(
            host_python=Path("/opt/comfy/bin/python"),
            host_root=Path("/repo/reference/ComfyUI"),
            base_root=Path("isolated-root"),
            port=8189,
        )
        self.assertEqual(command[0], str(Path("/opt/comfy/bin/python")))
        self.assertIn("--cpu", command)
        self.assertIn("--disable-manager", command)
        self.assertIn("--listen", command)
        self.assertEqual(command[command.index("--listen") + 1], "127.0.0.1")
        self.assertEqual(command[command.index("--port") + 1], "8189")
        for flag in (
            "--base-directory",
            "--models-directory",
            "--input-directory",
            "--output-directory",
            "--temp-directory",
            "--user-directory",
        ):
            self.assertIn(flag, command)
            expected_root = {
                "--base-directory": "isolated-root",
                "--models-directory": "models",
                "--input-directory": "input",
                "--output-directory": "output",
                "--temp-directory": "temp",
                "--user-directory": "user",
            }[flag]
            expected = str(
                Path("isolated-root")
                if expected_root == "isolated-root"
                else Path("isolated-root") / expected_root
            )
            self.assertEqual(command[command.index(flag) + 1], expected)
        self.assertNotIn("--enable-manager", command)

    def test_redaction_removes_credentials_and_private_paths(self) -> None:
        text = "Authorization: Bearer abc123 /home/private/output token=secret sig=xyz"
        redacted = redact_text(text, private_roots=(Path("/home/private"),))
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("/home/private", redacted)
        self.assertNotIn("secret", redacted)
        self.assertNotIn("xyz", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_persisted_host_failure_redacts_repo_and_interpreter_roots(self) -> None:
        from scripts import m3_08_host_e2e as driver

        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            report = Path(temporary) / "failure.json"
            host_python = ROOT / ".tmp" / "hc05-host-venv" / "Scripts" / "python.exe"
            artifact = ROOT / ".tmp" / "candidate" / "subject.tar.gz"
            leaked = (
                f"host failed at {ROOT / 'private-run'}; "
                f"frontend package at {host_python.parent / 'Lib' / 'site-packages'}"
            )
            with (
                patch(
                    "scripts.m3_08_host_e2e.run_supported_host",
                    side_effect=HostDriverError(leaked),
                ),
                patch("builtins.print"),
            ):
                exit_code = driver.main(
                    [
                        "--host-python",
                        str(host_python),
                        "--host-root",
                        str(ROOT / "reference" / "ComfyUI"),
                        "--artifact",
                        str(artifact),
                        "--report",
                        str(report),
                    ]
                )
            self.assertEqual(exit_code, 1)
            payload = json.loads(report.read_text(encoding="utf-8"))
            reason = payload["reason"]
            command = "\n".join(payload["exact_command"])
            self.assertIn("[PRIVATE_ROOT]", reason)
            for private_root in (ROOT, host_python.parent, artifact.parent):
                self.assertNotIn(str(private_root), reason)
                self.assertNotIn(str(private_root), command)

    def test_model_free_prompt_replaces_native_output_with_inspection_nodes(self) -> None:
        fixture = load_fixture(WORKFLOW_ROOT / "m3_07_h3_context_base.json")
        prompt = build_model_free_prompt(fixture)
        self.assertTrue(PROJECT_NODE_IDS <= {node["class_type"] for node in prompt.values()})
        self.assertNotIn("MiniMaxH3ImageToVideo", {node["class_type"] for node in prompt.values()})
        preview_nodes = [node for node in prompt.values() if node["class_type"] == "PreviewAny"]
        self.assertGreaterEqual(len(preview_nodes), 3)
        self.assertTrue(any(node["inputs"]["source"] == ["5", 0] for node in preview_nodes))
        self.assertTrue(any(node["inputs"]["source"] == ["4", 0] for node in preview_nodes))
        preview_node_id = next(
            node_id
            for node_id, node in prompt.items()
            if node["class_type"] == "comfyui_h3_context.H3Context.Preview"
        )
        self.assertEqual(prompt[preview_node_id]["inputs"]["report"], ["4", 1])
        self.assertTrue(
            any(node["inputs"]["source"] == [preview_node_id, 1] for node in preview_nodes)
        )

    def test_reference_model_free_prompt_preserves_local_fixture_images(self) -> None:
        fixture = load_fixture(WORKFLOW_ROOT / "m3_07_h3_context_reference.json")
        prompt = build_model_free_prompt(fixture)
        self.assertEqual(prompt["2"]["inputs"]["image"], "h3_context_fixture_image_1.png")
        self.assertEqual(prompt["3"]["inputs"]["image"], "h3_context_fixture_image_2.png")
        self.assertNotIn(
            "MiniMaxH3ReferenceToVideo",
            {node["class_type"] for node in prompt.values()},
        )
        self.assertTrue(
            any(
                node["inputs"]["source"] == ["8", 0]
                for node in prompt.values()
                if node["class_type"] == "PreviewAny"
            )
        )

    def test_reference_host_projection_uses_a_valid_two_item_output_list_link(self) -> None:
        fixture = load_fixture(WORKFLOW_ROOT / "m3_07_h3_context_reference.json")
        prompt = build_model_free_reference_prompt(fixture)
        self.assertEqual(prompt["2"]["class_type"], "EmptyImage")
        self.assertEqual(prompt["3"]["class_type"], "SplitImageToTileList")
        self.assertEqual(prompt["3"]["inputs"]["image"], ["2", 0])
        self.assertEqual(prompt["4"]["inputs"]["images"], ["3", 0])
        self.assertNotIn("LoadImage", {node["class_type"] for node in prompt.values()})

    def test_full_reference_projection_preserves_complex_media_upstream(self) -> None:
        fixture = load_fixture(WORKFLOW_ROOT / "m6_07_h3_context_full_reference.json")
        prompt = build_model_free_full_reference_prompt(fixture)
        classes = {node["class_type"] for node in prompt.values()}
        self.assertNotIn("MiniMaxH3ReferenceToVideo", classes)
        self.assertIn("LoadVideo", classes)
        self.assertIn("LoadAudio", classes)
        self.assertIn("GetVideoComponents", classes)
        image_batch_id = next(
            node_id for node_id, node in prompt.items() if node["class_type"] == "ImageBatch"
        )
        image_list_id = next(
            node_id for node_id, node in prompt.items() if node["class_type"] == "RebatchImages"
        )
        self.assertEqual(
            prompt[image_batch_id]["inputs"],
            {"image1": ["2", 0], "image2": ["3", 0]},
        )
        self.assertEqual(prompt[image_list_id]["inputs"]["images"], [image_batch_id, 0])
        self.assertEqual(prompt[image_list_id]["inputs"]["batch_size"], 1)
        registry = prompt["7"]
        self.assertEqual(registry["inputs"]["images"], [image_list_id, 0])
        self.assertEqual(registry["inputs"]["videos"], ["4", 0])
        self.assertEqual(registry["inputs"]["audios"], ["5", 0])
        self.assertIn(
            {"source": "6", "source_output": 0, "target": "12", "target_input": "ref_videos"},
            fixture["expected"]["direct_media_links"],
        )

    def test_audit_override_projection_observes_revalidated_report(self) -> None:
        fixture = load_fixture(WORKFLOW_ROOT / "m7_03_h3_context_audit_override.json")
        prompt = build_model_free_prompt(fixture)
        self.assertNotIn("MiniMaxH3ImageToVideo", {node["class_type"] for node in prompt.values()})
        self.assertEqual(prompt["6"]["inputs"]["report"], ["5", 1])
        self.assertEqual(prompt["7"]["inputs"]["report"], ["5", 1])
        self.assertTrue(
            any(
                node["inputs"]["source"] == ["6", 1]
                for node in prompt.values()
                if node["class_type"] == "PreviewAny"
            )
        )
        self.assertTrue(
            any(
                node["inputs"]["source"] == ["5", 0]
                for node in prompt.values()
                if node["class_type"] == "PreviewAny"
            )
        )
        observed_sources = {
            tuple(node["inputs"]["source"])
            for node in prompt.values()
            if node["class_type"] == "PreviewAny"
        }
        self.assertTrue({("4", 0), ("4", 1), ("4", 2)}.issubset(observed_sources))

    def test_provider_transparency_projection_observes_all_public_outputs(self) -> None:
        fixture = load_fixture(WORKFLOW_ROOT / "m7_04_h3_context_provider_transparency.json")
        prompt = build_model_free_prompt(fixture)
        self.assertEqual(prompt["6"]["inputs"]["report"], ["5", 1])
        self.assertEqual(prompt["7"]["inputs"]["report"], ["5", 1])
        transparency_id = next(
            node_id
            for node_id, node in fixture["prompt"].items()
            if node["class_type"] == PROVIDER_TRANSPARENCY_NODE_ID
        )
        observed_sources = {
            tuple(node["inputs"]["source"])
            for node in prompt.values()
            if node["class_type"] == "PreviewAny"
        }
        self.assertTrue(
            {
                (transparency_id, 0),
                (transparency_id, 1),
                (transparency_id, 2),
            }.issubset(observed_sources)
        )

    def test_reliability_projection_observes_all_public_outputs(self) -> None:
        fixture = load_fixture(WORKFLOW_ROOT / "m7_05_h3_context_reliability.json")
        prompt = build_model_free_prompt(fixture)
        self.assertEqual(prompt["6"]["inputs"]["report"], ["5", 1])
        self.assertEqual(prompt["7"]["inputs"]["report"], ["5", 1])
        reliability_id = next(
            node_id
            for node_id, node in fixture["prompt"].items()
            if node["class_type"] == RELIABILITY_NODE_ID
        )
        observed_sources = {
            tuple(node["inputs"]["source"])
            for node in prompt.values()
            if node["class_type"] == "PreviewAny"
        }
        self.assertTrue(
            {
                (reliability_id, 0),
                (reliability_id, 1),
                (reliability_id, 2),
                (reliability_id, 3),
            }.issubset(observed_sources)
        )

    def test_fixture_validation_rejects_unvalidated_terminal_consumers(self) -> None:
        fixture = load_fixture(WORKFLOW_ROOT / "m3_07_h3_context_base.json")
        drifted = copy.deepcopy(fixture)
        drifted["prompt"]["7"]["inputs"]["report"] = ["3", 1]
        with self.assertRaisesRegex(ValueError, "validated report"):
            validate_fixture(drifted)

    def test_fixture_validation_is_pinned_and_rejects_revision_drift(self) -> None:
        fixture = load_fixture(WORKFLOW_ROOT / "m3_07_h3_context_base.json")
        validate_fixture(
            fixture,
            expected_version=EXPECTED_HOST_VERSION,
            expected_revision=EXPECTED_HOST_REVISION,
        )
        drifted = copy.deepcopy(fixture)
        drifted["host"]["revision_parts"][-1] = "00000"
        with self.assertRaises(ValueError):
            validate_fixture(
                drifted,
                expected_version=EXPECTED_HOST_VERSION,
                expected_revision=EXPECTED_HOST_REVISION,
            )

    def test_fixture_loader_rejects_private_or_non_json_values(self) -> None:
        path = WORKFLOW_ROOT / "m3_07_h3_context_base.json"
        fixture = load_fixture(path)
        self.assertEqual(
            json.loads(json.dumps(fixture, sort_keys=True))["schema"],
            "h3-context-workflow-fixture/1",
        )


if __name__ == "__main__":
    unittest.main()
