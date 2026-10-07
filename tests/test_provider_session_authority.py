"""M22-09 per-browser provider authority regressions."""

from __future__ import annotations

import asyncio
import json
import unittest
from threading import Event

from comfyui_h3_context.adapters.comfyui_provider_settings import (
    MAX_PROVIDER_SESSIONS,
    PROVIDER_SESSION_ABSOLUTE_TTL_SECONDS,
    PROVIDER_SESSION_HEADER,
    PROVIDER_SESSION_IDLE_TTL_SECONDS,
    ProviderSettingsSessionError,
    ProviderSettingsSessionRegistry,
    dispatch_provider_settings,
    dispatch_provider_settings_async,
)
from comfyui_h3_context.core.contracts import ValidationSeverity
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PromptModelCapabilities,
    PromptModelFamily,
    PromptModelOutcomeId,
    PromptModelRemediation,
    build_prompt_model_capabilities,
    build_prompt_model_outcome,
)
from comfyui_h3_context.core.provider_settings import (
    ProviderSettingsIntent,
    ProviderSettingsState,
    ReadinessObservation,
)

SESSION_A = "ps_" + "a" * 32
SESSION_B = "ps_" + "b" * 32
SESSION_C = "ps_" + "c" * 32
SECRET = "sk-" + "S" * 40  # pragma: allowlist secret


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _capabilities(family: PromptModelFamily) -> PromptModelCapabilities:
    remote = family is PromptModelFamily.REMOTE_OPENAI_COMPATIBLE
    return build_prompt_model_capabilities(
        {
            "family": family.value,
            "accepted_media": ["text"],
            "provider_managed_context": True,
            "provider_managed_kv_cache": True,
            "max_request_bytes": 262_144,
            "max_context_tokens": 32_768,
            "max_output_tokens": 4_096,
            "streaming": False,
            "local_only": not remote,
            "requires_credential": remote,
        }
    )


def _profile(profile_id: str, family: PromptModelFamily, endpoint: str) -> PromptModelProfile:
    return PromptModelProfile(
        profile_id=profile_id,
        family=family,
        endpoint=endpoint,
        model_id="model-a",
        model_digest="sha256:" + "3" * 64,
        adapter_version="1.0.0",
        parser_version="h3.prompt_model.draft_json.v1",
        license_id="proprietary",
        license_source="provider_terms",
        license_text_sha256="sha256:" + "4" * 64,
        capabilities=_capabilities(family),
    )


REMOTE = _profile(
    "remote.example.gpt",
    PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
    "https://api.example.com/v1",
)
LOCAL = _profile("ollama.local.qwen", PromptModelFamily.OLLAMA, "http://127.0.0.1:11434")


def _state() -> ProviderSettingsState:
    return ProviderSettingsState(profiles=(REMOTE, LOCAL))


class ProviderSessionRegistryTests(unittest.TestCase):
    def test_sync_and_async_setup_clear_only_the_owning_session_hints(self) -> None:
        registry = ProviderSettingsSessionRegistry(state_factory=_state)
        first = registry.entry_for(SESSION_A)
        other = registry.entry_for(SESSION_B)
        preference = (REMOTE.profile_id, REMOTE.model_id)
        other.safe_dialect_preferences.add(preference)

        for asynchronous in (False, True):
            first.safe_dialect_preferences.add(preference)
            if asynchronous:
                result = asyncio.run(
                    dispatch_provider_settings_async(
                        SESSION_A,
                        ProviderSettingsIntent.SELECT_PROFILE,
                        {"profile_id": REMOTE.profile_id},
                        registry=registry,
                    )
                )
            else:
                result = dispatch_provider_settings(
                    SESSION_A,
                    ProviderSettingsIntent.SELECT_PROFILE,
                    {"profile_id": REMOTE.profile_id},
                    registry=registry,
                )
            self.assertTrue(result["accepted"])
            self.assertEqual(first.safe_dialect_preferences, set())
            self.assertEqual(other.safe_dialect_preferences, {preference})

        first.safe_dialect_preferences.add(preference)
        rejected = dispatch_provider_settings(
            SESSION_A,
            ProviderSettingsIntent.SELECT_PROFILE,
            {"profile_id": "unknown"},
            registry=registry,
        )
        self.assertFalse(rejected["accepted"])
        self.assertEqual(first.safe_dialect_preferences, {preference})
        changed = dispatch_provider_settings(
            SESSION_A,
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": SECRET},
            registry=registry,
        )
        self.assertTrue(changed["accepted"])
        self.assertEqual(first.safe_dialect_preferences, set())

    def test_limits_are_finite_and_the_header_is_not_a_url_field(self) -> None:
        self.assertEqual(PROVIDER_SESSION_HEADER, "X-H3-Provider-Session")
        self.assertGreater(MAX_PROVIDER_SESSIONS, 1)
        self.assertLessEqual(MAX_PROVIDER_SESSIONS, 128)
        self.assertGreater(PROVIDER_SESSION_IDLE_TTL_SECONDS, 0)
        self.assertGreater(
            PROVIDER_SESSION_ABSOLUTE_TTL_SECONDS,
            PROVIDER_SESSION_IDLE_TTL_SECONDS,
        )

    def test_selection_consent_and_credential_are_isolated(self) -> None:
        registry = ProviderSettingsSessionRegistry(state_factory=_state)
        selected = dispatch_provider_settings(
            SESSION_A,
            ProviderSettingsIntent.SELECT_PROFILE,
            {"profile_id": REMOTE.profile_id},
            registry=registry,
        )
        self.assertTrue(selected["accepted"])
        dispatch_provider_settings(
            SESSION_A,
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": SECRET},
            registry=registry,
        )
        dispatch_provider_settings(
            SESSION_A,
            ProviderSettingsIntent.GRANT_CONSENT,
            {"network_permitted": True},
            registry=registry,
        )

        other = dispatch_provider_settings(
            SESSION_B,
            ProviderSettingsIntent.READ_PROJECTION,
            {},
            registry=registry,
        )
        projection = other["projection"]
        assert isinstance(projection, dict)
        self.assertEqual(projection["selected_profile_id"], "")
        self.assertFalse(projection["credential_present"])
        self.assertEqual(projection["credential_last_four"], "")
        self.assertIsNone(projection["consent"])

        first_wire = json.dumps(
            dispatch_provider_settings(
                SESSION_A,
                ProviderSettingsIntent.READ_PROJECTION,
                {},
                registry=registry,
            )
        )
        self.assertNotIn(SECRET, first_wire)
        self.assertNotIn(SECRET[-4:], first_wire)

    def test_idle_and_absolute_expiry_create_fresh_authority(self) -> None:
        clock = Clock()
        idle_registry = ProviderSettingsSessionRegistry(
            state_factory=_state,
            clock=clock,
            idle_ttl_seconds=10,
            absolute_ttl_seconds=25,
            max_sessions=4,
        )
        idle_original = idle_registry.state_for(SESSION_A)
        clock.now = 11
        self.assertIsNot(idle_registry.state_for(SESSION_A), idle_original)

        clock.now = 0
        absolute_registry = ProviderSettingsSessionRegistry(
            state_factory=_state,
            clock=clock,
            idle_ttl_seconds=20,
            absolute_ttl_seconds=25,
            max_sessions=4,
        )
        absolute_original = absolute_registry.state_for(SESSION_B)
        clock.now = 19
        self.assertIs(absolute_registry.state_for(SESSION_B), absolute_original)
        clock.now = 24
        self.assertIs(absolute_registry.state_for(SESSION_B), absolute_original)
        clock.now = 26
        self.assertIsNot(absolute_registry.state_for(SESSION_B), absolute_original)

    def test_capacity_evicts_least_recently_used_with_a_deterministic_tie_break(self) -> None:
        clock = Clock()
        registry = ProviderSettingsSessionRegistry(
            state_factory=_state,
            clock=clock,
            idle_ttl_seconds=100,
            absolute_ttl_seconds=200,
            max_sessions=2,
        )
        first = registry.state_for(SESSION_A)
        second = registry.state_for(SESSION_B)
        clock.now = 1
        self.assertIs(registry.state_for(SESSION_A), first)
        registry.state_for(SESSION_C)
        self.assertEqual(registry.session_count, 2)
        self.assertTrue(registry.has_session(SESSION_A))
        self.assertFalse(registry.has_session(SESSION_B))
        self.assertTrue(registry.has_session(SESSION_C))
        self.assertIsNot(first, second)

        tied = ProviderSettingsSessionRegistry(
            state_factory=_state,
            clock=Clock(),
            idle_ttl_seconds=100,
            absolute_ttl_seconds=200,
            max_sessions=2,
        )
        tied.state_for(SESSION_B)
        tied.state_for(SESSION_A)
        tied.state_for(SESSION_C)
        self.assertFalse(tied.has_session(SESSION_A))
        self.assertTrue(tied.has_session(SESSION_B))

    def test_release_is_idempotent_and_tombstones_the_same_handle(self) -> None:
        registry = ProviderSettingsSessionRegistry(state_factory=_state)
        dispatch_provider_settings(
            SESSION_A,
            ProviderSettingsIntent.SELECT_PROFILE,
            {"profile_id": REMOTE.profile_id},
            registry=registry,
        )
        dispatch_provider_settings(
            SESSION_A,
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": SECRET},
            registry=registry,
        )
        self.assertTrue(registry.release(SESSION_A))
        self.assertFalse(registry.release(SESSION_A))
        with self.assertRaises(ProviderSettingsSessionError) as caught:
            registry.state_for(SESSION_A)
        self.assertEqual(caught.exception.code, "session_released")
        fresh = registry.state_for(SESSION_B).project()
        self.assertEqual(fresh.selected_profile_id, "")
        self.assertFalse(fresh.credential_present)
        self.assertIsNone(fresh.consent)

    def test_release_tombstones_are_bounded_and_expire(self) -> None:
        clock = Clock()
        registry = ProviderSettingsSessionRegistry(
            state_factory=_state,
            clock=clock,
            idle_ttl_seconds=10,
            absolute_ttl_seconds=20,
            max_sessions=2,
        )
        self.assertFalse(registry.release(SESSION_A))
        clock.now = 1
        self.assertFalse(registry.release(SESSION_B))
        clock.now = 2
        self.assertFalse(registry.release(SESSION_C))

        self.assertEqual(registry.state_for(SESSION_A).project().selected_profile_id, "")
        for handle in (SESSION_B, SESSION_C):
            with self.subTest(handle=handle):
                with self.assertRaises(ProviderSettingsSessionError):
                    registry.state_for(handle)

        clock.now = 12
        self.assertEqual(registry.state_for(SESSION_B).project().selected_profile_id, "")

    def test_invalid_handles_fail_closed_without_creating_state(self) -> None:
        registry = ProviderSettingsSessionRegistry(state_factory=_state)
        for value in ("", "ps_short", "PS_" + "a" * 32, "ps_" + "g" * 32, 7, None):
            with self.subTest(value=value):
                with self.assertRaises(ProviderSettingsSessionError) as caught:
                    registry.state_for(value)
                self.assertEqual(caught.exception.code, "session_rejected")
        self.assertEqual(registry.session_count, 0)


class ProviderSessionCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_invalidates_an_inflight_readiness_task(self) -> None:
        registry = ProviderSettingsSessionRegistry(state_factory=_state)
        dispatch_provider_settings(
            SESSION_A,
            ProviderSettingsIntent.SELECT_PROFILE,
            {"profile_id": LOCAL.profile_id},
            registry=registry,
        )
        started = Event()
        finish_worker = Event()

        def probe(_profile: object, _credential: object) -> ReadinessObservation:
            started.set()
            if not finish_worker.wait(2):
                return ReadinessObservation(
                    reachable=False,
                    outcome=build_prompt_model_outcome(
                        PromptModelOutcomeId.TIMEOUT,
                        severity=ValidationSeverity.ERROR,
                        remediation=PromptModelRemediation.RETRY_LATER,
                        parameters=(),
                    ),
                )
            return ReadinessObservation(reachable=True)

        pending = asyncio.create_task(
            dispatch_provider_settings_async(
                SESSION_A,
                ProviderSettingsIntent.RECHECK_READINESS,
                {},
                readiness_probe=probe,
                registry=registry,
            )
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        self.assertTrue(registry.release(SESSION_A))
        finish_worker.set()
        with self.assertRaises(ProviderSettingsSessionError):
            await pending
        with self.assertRaises(ProviderSettingsSessionError) as caught:
            registry.state_for(SESSION_A)
        self.assertEqual(caught.exception.code, "session_released")
        fresh = registry.state_for(SESSION_B).project()
        self.assertEqual(fresh.selected_profile_id, "")
        self.assertFalse(fresh.reachability_observed)

    async def test_different_sessions_never_share_readiness_work(self) -> None:
        registry = ProviderSettingsSessionRegistry(state_factory=_state)
        for handle in (SESSION_A, SESSION_B):
            dispatch_provider_settings(
                handle,
                ProviderSettingsIntent.SELECT_PROFILE,
                {"profile_id": LOCAL.profile_id},
                registry=registry,
            )
        calls = 0

        def probe(_profile: object, _credential: object) -> ReadinessObservation:
            nonlocal calls
            calls += 1
            return ReadinessObservation(reachable=True)

        first, second = await asyncio.gather(
            dispatch_provider_settings_async(
                SESSION_A,
                ProviderSettingsIntent.RECHECK_READINESS,
                {},
                readiness_probe=probe,
                registry=registry,
            ),
            dispatch_provider_settings_async(
                SESSION_B,
                ProviderSettingsIntent.RECHECK_READINESS,
                {},
                readiness_probe=probe,
                registry=registry,
            ),
        )
        self.assertEqual(calls, 2)
        self.assertTrue(first["accepted"])
        self.assertTrue(second["accepted"])


if __name__ == "__main__":
    unittest.main()
