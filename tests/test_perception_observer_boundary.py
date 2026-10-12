"""Already-loaded optional observer lookup must not escape package activation."""

from __future__ import annotations

import os
import unittest
from types import ModuleType
from unittest.mock import patch

import test_perception_settings_lifecycle as _lifecycle

# IMPORTANT: these methods enter Windows-only guards; portable refusal tests stay active.
windows_perception = unittest.skipUnless(
    os.name == "nt", "operator perception settings require Windows"
)


class PerceptionObserverBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _lifecycle.PerceptionSettingsLifecycleTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    @windows_perception
    def test_observer_lookup_exception_is_finite_cached_and_does_not_read_settings(self) -> None:
        hostile = ModuleType("comfy.model_management")

        def unavailable(name: str) -> object:
            raise RuntimeError("untrusted_operator_value")

        hostile.__dict__["__getattr__"] = unavailable
        module = self.fixture.settings_module
        with (
            self.fixture.host_modules(),
            patch.dict(module.sys.modules, {"comfy.model_management": hostile}),
            patch.object(module, "load_perception_settings") as loader,
            patch.object(module.atexit, "register") as register,
        ):
            try:
                result = self.fixture.owner.ensure_configured()
            except Exception:
                self.fail("observer_lookup_exception_escaped")
            self.assertFalse(result)
            self.assertFalse(self.fixture.owner.ensure_configured())
            self.assertEqual(self.fixture.owner.status()["reason"], "settings_unavailable")
            self.assertNotIn("untrusted_operator_value", repr(self.fixture.owner.status()))
            loader.assert_not_called()
            register.assert_not_called()
