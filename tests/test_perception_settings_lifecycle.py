"""One-attempt operator ownership, non-host import and content-free status reads."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock, patch

from comfyui_h3_context.adapters import perception_host as host
from comfyui_h3_context.core.perception_execution import AUDIO_PROFILE, VISUAL_PROFILE
from scripts.hc_09_host_seam_test_double import host_folder_paths_module

# IMPORTANT: these methods enter Windows-only guards; portable refusal tests stay active.
windows_perception = unittest.skipUnless(
    os.name == "nt", "operator perception settings require Windows"
)


class PerceptionSettingsLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        source = Path(__file__).resolve().parents[1]
        owned = source / ".tmp/perception-lifetime-tests"
        owned.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=owned)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.private = self.root / "private"
        self.served = tuple(self.root / name for name in ("input", "output", "temp"))
        for directory in (self.private, *self.served):
            directory.mkdir()
        self.folder = host_folder_paths_module()
        self.folder.__dict__.update(
            {
                "get_system_user_directory": lambda name: (
                    str(self.private) if name == "h3_context" else None
                ),
                "get_input_directory": lambda: str(self.served[0]),
                "get_output_directory": lambda: str(self.served[1]),
                "get_temp_directory": lambda: str(self.served[2]),
            }
        )
        self.observer = Mock(return_value=False)
        self.model_management = ModuleType("comfy.model_management")
        self.model_management.__dict__["processing_interrupted"] = self.observer
        self.settings_module = importlib.import_module(
            "comfyui_h3_context.adapters.perception_settings"
        )
        self.owner = self.settings_module._OperatorSettingsOwner()
        self.addCleanup(self.owner.close)
        binding_patch = patch.object(host, "_BINDING", None)
        binding_patch.start()
        self.addCleanup(binding_patch.stop)

    def settings(
        self, cancelled: Any = None, *, audio: bool = False
    ) -> host.PerceptionHostSettings:
        return host.PerceptionHostSettings(
            Path(sys.executable),
            self.root,
            Path(sys.executable) if audio else None,
            self.root if audio else None,
            cancelled or (lambda: False),
            profiles=frozenset({AUDIO_PROFILE if audio else VISUAL_PROFILE}),
        )

    def host_modules(self, *, observer: bool = True) -> Any:
        return patch.dict(
            sys.modules,
            {
                "folder_paths": self.folder,
                "comfy.model_management": self.model_management if observer else None,
            },
        )

    def activate(self, loader: Mock) -> None:
        self.assertTrue(self.owner.ensure_configured(), "operator_file_binding_not_activated")
        self.assertEqual(loader.call_args.args, (self.private / "perception_host.json",))
        self.assertTrue(callable(loader.call_args.kwargs["cancelled"]))

    @windows_perception
    def test_no_host_does_not_attempt_or_read_then_later_real_host_can_activate(self) -> None:
        module = self.settings_module
        with patch.object(
            module, "load_perception_settings", return_value=self.settings()
        ) as loader:
            with patch.dict(sys.modules, {"folder_paths": None}):
                self.assertFalse(self.owner.ensure_configured())
                self.assertEqual(self.owner.status()["reason"], "host_unavailable")
                loader.assert_not_called()
            with self.host_modules(), patch.object(module.atexit, "register"):
                self.activate(loader)
            loader.assert_called_once()

    @windows_perception
    def test_success_owns_one_binding_and_one_exit_callback_without_probe_execution(self) -> None:
        module = self.settings_module
        with (
            self.host_modules(),
            patch.object(module.atexit, "register") as register,
            patch.object(
                module,
                "load_perception_settings",
                side_effect=lambda path, *, cancelled: self.settings(cancelled),
            ) as loader,
        ):
            self.activate(loader)
            self.assertTrue(self.owner.ensure_configured())
            self.assertEqual(
                self.owner.status(),
                {
                    "schema": "h3.context.perception_host_status.v1",
                    "state": "configured",
                    "visual": True,
                    "audio": False,
                    "reason": "none",
                    "cancellation_observed": True,
                },
            )
            loader.assert_called_once()
            register.assert_called_once()
            self.observer.assert_not_called()
            self.assertIs(register.call_args.args[0].__self__, self.owner)

    @windows_perception
    def test_missing_file_is_cached_after_real_attempt_and_status_never_reloads_it(self) -> None:
        module = self.settings_module
        with (
            self.host_modules(),
            patch.object(module.atexit, "register") as register,
            patch.object(
                module,
                "load_perception_settings",
                side_effect=module.PerceptionSettingsError("settings_missing"),
            ) as loader,
        ):
            self.assertFalse(self.owner.ensure_configured())
            self.assertFalse(self.owner.ensure_configured())
            with patch.object(Path, "open", side_effect=AssertionError("status_read_settings")):
                self.assertEqual(self.owner.status()["reason"], "settings_missing")
                self.assertEqual(self.owner.status()["state"], "not_configured")
            loader.assert_called_once()
            register.assert_not_called()

    @windows_perception
    def test_finite_loader_refusal_is_cached_without_paths_or_raw_error(self) -> None:
        module = self.settings_module
        with (
            self.host_modules(),
            patch.object(module.atexit, "register"),
            patch.object(
                module,
                "load_perception_settings",
                side_effect=module.PerceptionSettingsError("path_refused"),
            ) as loader,
        ):
            self.assertFalse(self.owner.ensure_configured())
            self.assertEqual(self.owner.status()["state"], "refused")
            self.assertEqual(self.owner.status()["reason"], "path_refused")
            self.assertNotIn(str(self.root), json.dumps(self.owner.status()))
            self.assertFalse(self.owner.ensure_configured())
            loader.assert_called_once()

    @windows_perception
    def test_existing_manual_binding_is_preserved_without_loader_or_exit_registration(self) -> None:
        module = self.settings_module
        manual = host.configure_perception_host(self.settings(audio=True))
        with (
            self.host_modules(),
            patch.object(module.atexit, "register") as register,
            patch.object(module, "load_perception_settings") as loader,
        ):
            self.assertFalse(self.owner.ensure_configured())
            self.assertEqual(
                self.owner.status(),
                {
                    "schema": "h3.context.perception_host_status.v1",
                    "state": "configured",
                    "visual": False,
                    "audio": True,
                    "reason": "none",
                    "cancellation_observed": False,
                },
            )
            self.assertIs(host._BINDING, manual)
            loader.assert_not_called()
            register.assert_not_called()

    @windows_perception
    def test_owner_exit_never_clears_a_manual_successor_or_inherits_observer_provenance(
        self,
    ) -> None:
        module = self.settings_module
        with (
            self.host_modules(),
            patch.object(module.atexit, "register") as register,
            patch.object(
                module, "load_perception_settings", return_value=self.settings()
            ) as loader,
        ):
            self.activate(loader)
            successor = host.configure_perception_host(self.settings(audio=True))
            self.assertFalse(self.owner.status()["cancellation_observed"])
            register.call_args.args[0]()
            self.assertIs(host._BINDING, successor)
            self.assertTrue(host.perception_configured(AUDIO_PROFILE))

    @windows_perception
    def test_changed_owned_subset_revokes_status_without_a_retry_or_probe_call(self) -> None:
        module = self.settings_module
        configured = self.settings()
        with (
            self.host_modules(),
            patch.object(module.atexit, "register"),
            patch.object(module, "load_perception_settings", return_value=configured) as loader,
        ):
            self.activate(loader)
            object.__setattr__(configured, "profiles", frozenset({AUDIO_PROFILE}))
            self.assertEqual(self.owner.status()["state"], "refused")
            self.assertEqual(self.owner.status()["reason"], "configuration_revoked")
            self.assertFalse(self.owner.status()["visual"])
            self.assertFalse(self.owner.status()["audio"])
            self.assertFalse(self.owner.ensure_configured())
            loader.assert_called_once()
            self.observer.assert_not_called()

    @windows_perception
    def test_absent_cancellation_observer_uses_false_without_claiming_observation(self) -> None:
        module = self.settings_module
        with (
            self.host_modules(observer=False),
            patch.object(module.atexit, "register"),
            patch.object(
                module,
                "load_perception_settings",
                side_effect=lambda path, *, cancelled: self.settings(cancelled),
            ) as loader,
        ):
            self.activate(loader)
            self.assertIs(loader.call_args.kwargs["cancelled"](), False)
            self.assertFalse(self.owner.status()["cancellation_observed"])

    @windows_perception
    def test_private_root_overlap_is_refused_before_any_file_read(self) -> None:
        module = self.settings_module
        self.folder.__dict__["get_system_user_directory"] = lambda name: str(self.served[0])
        with self.host_modules(), patch.object(module, "load_perception_settings") as loader:
            self.assertFalse(self.owner.ensure_configured())
            self.assertEqual(self.owner.status()["reason"], "path_refused")
            loader.assert_not_called()

    def test_non_windows_owner_and_status_refuse_before_roots_loader_or_probe(self) -> None:
        module = self.settings_module
        root_factory = Mock(side_effect=AssertionError("unsupported_platform_resolved_root"))
        self.folder.__dict__["get_system_user_directory"] = root_factory
        with (
            self.host_modules(),
            patch.object(module.os, "name", "posix"),
            patch.object(module, "load_perception_settings") as loader,
        ):
            self.assertFalse(self.owner.ensure_configured())
            self.assertEqual(self.owner.status()["reason"], "unsupported_platform")
            root_factory.assert_not_called()
            loader.assert_not_called()
            self.observer.assert_not_called()

    @windows_perception
    def test_actual_package_reload_attempts_once_after_later_real_host_load(self) -> None:
        import comfyui_h3_context as package
        from comfyui_h3_context.adapters import comfyui_route_seam as seam

        module = self.settings_module
        saved = {
            name: adapter.__dict__["_ROUTE_REGISTERED"]
            for name, adapter in tuple(sys.modules.items())
            if name.startswith("comfyui_h3_context.adapters.")
            and adapter is not None
            and "_ROUTE_REGISTERED" in adapter.__dict__
        }
        try:
            with (
                patch.object(module, "_OWNER", self.owner),
                patch.object(seam, "host_web_and_routes", return_value=None),
                patch.object(module.atexit, "register") as register,
                patch.object(
                    module, "load_perception_settings", return_value=self.settings()
                ) as loader,
            ):
                with patch.dict(
                    sys.modules, {"folder_paths": None, "server": None, "aiohttp": None}
                ):
                    importlib.reload(package)
                    loader.assert_not_called()
                with (
                    self.host_modules(),
                    patch.dict(sys.modules, {"server": None, "aiohttp": None}),
                ):
                    importlib.reload(package)
                    importlib.reload(package)
                    loader.assert_called_once()
                    register.assert_called_once()
                    self.assertTrue(host.perception_configured(VISUAL_PROFILE))
        finally:
            for name, previous in saved.items():
                sys.modules[name].__dict__["_ROUTE_REGISTERED"] = previous


if __name__ == "__main__":
    unittest.main()
