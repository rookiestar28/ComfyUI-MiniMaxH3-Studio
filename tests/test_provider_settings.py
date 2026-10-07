"""M22-06 provider settings projection, intents and route tests."""

from __future__ import annotations

import asyncio
import json
import subprocess as subprocess_module
import sys
import unittest
from collections.abc import Callable, Coroutine
from dataclasses import replace
from threading import Event, Lock, Thread
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from deployment_request_doubles import LOOPBACK_HOST, ListenerTransport

import comfyui_h3_context.adapters.comfyui_provider_settings as settings_adapter
import comfyui_h3_context.adapters.provider_readiness as readiness_adapter
from comfyui_h3_context.adapters.comfyui_provider_settings import (
    MAX_PROVIDER_SETTINGS_BYTES,
    PROVIDER_SETTINGS_REQUEST_SCHEMA,
    PROVIDER_SETTINGS_ROUTE,
    ProviderAuthorityClaim,
    ProviderSessionExecutionDecision,
    ProviderSettingsRequestError,
    ProviderSettingsSessionError,
    ProviderSettingsSessionRegistry,
    decode_provider_settings_request,
    dispatch_provider_settings,
    provider_settings_state,
    reset_provider_settings_state,
)
from comfyui_h3_context.core.assisted_authoring_scope import AssistedAuthoringState
from comfyui_h3_context.core.contracts import ValidationSeverity
from comfyui_h3_context.core.prompt_model_provider import (
    MAX_DISCOVERY_ROWS,
    PromptModelCapabilities,
    PromptModelContractError,
    PromptModelFamily,
    PromptModelOutcomeId,
    PromptModelQualificationEvidence,
    PromptModelQualificationState,
    PromptModelRemediation,
    QualifiedIdentityObservation,
    build_prompt_model_capabilities,
    build_prompt_model_outcome,
    build_qualification_evidence,
    compute_endpoint_fingerprint,
    route_for_family,
)
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.prompt_model_session import (
    DiscoveryCandidate,
    DiscoveryRejection,
)
from comfyui_h3_context.core.provider_settings import (
    CONSENT_SCOPE,
    MAX_PROJECTED_CANDIDATES,
    PROVIDER_SETTINGS_SCHEMA,
    ProviderExecutionDecision,
    ProviderIntentRejection,
    ProviderReadiness,
    ProviderSettingsIntent,
    ProviderSettingsState,
    ReadinessObservation,
    describe_transmission,
    diagnostic_of,
    remote_families,
    view_of_profile,
)
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

REMOTE_ID = "remote.example.gpt"
LOCAL_ID = "ollama.local.qwen"
CREDENTIAL_SENTINEL = "test-credential-" + "Q" * 40
TEST_SESSION = "ps_" + "1" * 32


def capabilities(
    family: PromptModelFamily, *, media: tuple[str, ...] = ("text",)
) -> PromptModelCapabilities:
    remote = family is PromptModelFamily.REMOTE_OPENAI_COMPATIBLE
    return build_prompt_model_capabilities(
        {
            "family": family.value,
            "accepted_media": list(media),
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


def profile(
    profile_id: str,
    family: PromptModelFamily,
    endpoint: str,
    *,
    media: tuple[str, ...] = ("text",),
) -> PromptModelProfile:
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
        capabilities=capabilities(family, media=media),
    )


def qualification_evidence(source: PromptModelProfile) -> PromptModelQualificationEvidence:
    """M22-13: `qualified` now has to carry the evidence that justifies it.

    Built from the profile so the two never drift apart; the fingerprint is derived by the builder
    rather than restated here, which is the whole point of the builder existing.
    """

    return build_qualification_evidence(
        model_id=source.model_id,
        digest=source.model_digest or "sha256:" + "3" * 64,
        model_size_bytes=4_294_967_296,
        model_format="gguf",
        model_family="testfamily",
        parameter_size="8.0B",
        quantization_level="Q4_K_M",
        context_length=32_768,
        capabilities=("completion",),
        license_text_sha256=source.license_text_sha256 or "sha256:" + "4" * 64,
        adapter_version=source.adapter_version,
        parser_version=source.parser_version,
        evidence_basis_id="test.evidence.1",
    )


def qualified_profile(source: PromptModelProfile) -> PromptModelProfile:
    return replace(
        source,
        qualification_state=PromptModelQualificationState.QUALIFIED,
        qualification_evidence=qualification_evidence(source),
    )


REMOTE_PROFILE = profile(
    REMOTE_ID, PromptModelFamily.REMOTE_OPENAI_COMPATIBLE, "https://api.example.com/v1"
)
LOCAL_PROFILE = profile(LOCAL_ID, PromptModelFamily.OLLAMA, "http://127.0.0.1:11434")


def state(*profiles: PromptModelProfile) -> ProviderSettingsState:
    return ProviderSettingsState(profiles=profiles or (REMOTE_PROFILE, LOCAL_PROFILE))


def select(subject: ProviderSettingsState, profile_id: str) -> None:
    result = subject.apply(ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": profile_id})
    assert result.accepted, result.rejection


def select_exact_model(subject: ProviderSettingsState, profile_id: str) -> None:
    """Seed the explicit-census prerequisite for tests whose subject starts after model choice."""

    select(subject, profile_id)
    seed_model_choice(subject, profile_id)


def seed_model_choice(subject: ProviderSettingsState, profile_id: str) -> None:
    """Admit a fixture census after credential replacement without changing its consent."""
    selected_profile = next(item for item in subject.profiles if item.profile_id == profile_id)
    assert isinstance(selected_profile, PromptModelProfile)
    subject.record_candidates(
        (
            DiscoveryCandidate(
                identifier=selected_profile.model_id,
                reason=DiscoveryRejection.ADMITTED,
            ),
        )
    )
    result = subject.apply(
        ProviderSettingsIntent.SELECT_MODEL, {"model_id": selected_profile.model_id}
    )
    assert result.accepted, result.rejection


def proved_identity(profile_value: PromptModelProfile) -> QualifiedIdentityObservation | None:
    """What a probe would have observed for an already-qualified profile.

    Built from the profile's own evidence rather than hand-written, so a fixture cannot claim a
    proof that disagrees with the claim it is proving. A profile nobody qualified gets `None`,
    which is exactly what a real probe would report for it.
    """

    evidence = profile_value.qualification_evidence
    if (
        not isinstance(evidence, PromptModelQualificationEvidence)
        or profile_value.model_digest is None
    ):
        return None
    return QualifiedIdentityObservation(
        profile_id=profile_value.profile_id,
        model_id=profile_value.model_id,
        model_digest=profile_value.model_digest,
        qualification_sha256=evidence.show_identity_sha256,
        endpoint_sha256=compute_endpoint_fingerprint(profile_value.endpoint),
        observed_at=1.0,
    )


def ready_observation(profile_value: PromptModelProfile) -> ReadinessObservation:
    return ReadinessObservation(
        reachable=True,
        candidates=(
            DiscoveryCandidate(
                identifier=profile_value.model_id,
                reason=DiscoveryRejection.ADMITTED,
            ),
        ),
        identity=proved_identity(profile_value),
    )


def bind_test_session(subject: ProviderSettingsState) -> None:
    reset_provider_settings_state()
    settings_adapter._registry().entry_for(TEST_SESSION).state = subject


class ShippedDefaultTests(unittest.TestCase):
    """The shipped catalog is available but never selected or authorized by default."""

    def test_no_settings_interaction_except_an_explicit_recheck_reaches_a_probe(self) -> None:
        """M22-13 AC-3. Activating the local row must not make the session start talking.

        The guarantee is structural -- `recheck` is the only path from this object to a socket --
        but a future intent could quietly acquire one, so the whole ordinary interaction sequence
        is driven here against a probe that fails the test if it is ever called.
        """

        calls: list[object] = []

        def forbidden(_profile: object, _credential: object) -> ReadinessObservation:
            calls.append(_profile)
            raise AssertionError("a settings interaction issued a provider request")

        subject = ProviderSettingsState.from_catalog()
        local = subject.profiles[0]
        self.assertIs(local.qualification_state, PromptModelQualificationState.QUALIFIED)

        for intent, payload in (
            (ProviderSettingsIntent.READ_PROJECTION, {}),
            (ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": local.profile_id}),
            (ProviderSettingsIntent.READ_PROJECTION, {}),
            (
                ProviderSettingsIntent.SELECT_MODEL,
                {
                    "model_id": "unlisted-model",
                    "profile_id": local.profile_id,
                    "expected_revision": subject.revision,
                },
            ),
            (ProviderSettingsIntent.CLEAR_SELECTION, {}),
            (ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": local.profile_id}),
        ):
            with self.subTest(intent=intent.value):
                subject.apply(intent, payload, readiness_probe=forbidden)

        self.assertIs(subject.readiness(), ProviderReadiness.NOT_CONFIGURED)
        self.assertFalse(subject.project().assisted_authoring.authorized_for_this_action)
        self.assertIsNone(subject.readiness_evidence)
        self.assertEqual(calls, [])

        # And the explicit recheck is the one intent that does reach the probe.
        result = subject.apply(
            ProviderSettingsIntent.RECHECK_READINESS,
            {"profile_id": local.profile_id, "expected_revision": subject.revision},
            readiness_probe=forbidden,
        )
        self.assertEqual(calls, [local])
        self.assertIsNotNone(result.projection.diagnostic)

    def test_the_shipped_catalog_is_available_but_unselected(self) -> None:
        subject = ProviderSettingsState.from_catalog()
        projection = subject.project()
        self.assertFalse(projection.catalog_empty)
        self.assertEqual(len(projection.profiles), 4)
        # M22-13: the qualified local row pins a digest and the projection surfaces it, so a
        # reader can see which exact model the claim is about. The two catalog-only remote
        # rows still have nothing to show, which is the distinction worth asserting.
        self.assertEqual(
            tuple(row.profile_id for row in projection.profiles),
            ("ollama.local", "openai.remote", "gemini.remote", "anthropic.remote"),
        )
        for row in projection.profiles:
            self.assertTrue({"model_id", "model_digest", "license_id"}.isdisjoint(row.to_wire()))
        self.assertIsNone(projection.selected_model)
        self.assertEqual(projection.selected_profile_id, "")
        self.assertEqual(projection.selected_model_id, "")
        self.assertIs(projection.readiness, ProviderReadiness.NOT_CONFIGURED)
        self.assertEqual(
            projection.assisted_authoring.to_wire(),
            {
                "available": True,
                "selected": False,
                "ready": False,
                "authorized_for_this_action": False,
                "defaulted": False,
            },
        )
        self.assertIsNone(projection.disclosure)
        self.assertIsNone(projection.consent)
        self.assertFalse(projection.consent_required)
        self.assertFalse(projection.credential_required)
        with self.assertRaises(PromptModelContractError):
            replace(
                projection,
                assisted_authoring=AssistedAuthoringState(True, True, False, False, False),
            )

    def test_only_a_qualified_exact_ready_profile_projects_execution_authority(self) -> None:
        qualified = qualified_profile(LOCAL_PROFILE)
        subject = ProviderSettingsState(profiles=(qualified,))
        select_exact_model(subject, qualified.profile_id)
        subject.recheck(lambda _profile, _credential: ready_observation(qualified))

        projection = subject.project()
        self.assertIs(projection.readiness, ProviderReadiness.READY)
        self.assertTrue(projection.assisted_authoring.authorized_for_this_action)
        decision = subject.execution_decision()
        self.assertTrue(decision.admitted)
        self.assertIsNotNone(decision.snapshot)
        assert decision.snapshot is not None
        self.assertIs(decision.snapshot.profile, qualified)
        self.assertEqual(decision.snapshot.selected_model_id, qualified.model_id)
        self.assertEqual(decision.snapshot.provider_revision, projection.revision)
        self.assertFalse(hasattr(decision.snapshot, "to_wire"))

    def test_an_authority_mutation_invalidates_the_previous_execution_epoch(self) -> None:
        qualified = qualified_profile(LOCAL_PROFILE)
        subject = ProviderSettingsState(profiles=(qualified,))
        select_exact_model(subject, qualified.profile_id)
        subject.recheck(lambda _profile, _credential: ready_observation(qualified))
        admitted = subject.execution_decision()
        assert admitted.snapshot is not None
        old_epoch = admitted.snapshot.authority_epoch

        subject.apply(ProviderSettingsIntent.CLEAR_MODEL)
        refused = subject.execution_decision()
        self.assertFalse(refused.admitted)
        self.assertIsNone(refused.snapshot)
        self.assertGreater(subject.authority_epoch, old_epoch)

    def test_no_intent_can_enable_a_provider_the_catalog_does_not_contain(self) -> None:
        subject = ProviderSettingsState.from_catalog()
        refused = subject.apply(
            ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": "invented.provider"}
        )
        self.assertFalse(refused.accepted)
        self.assertIs(refused.rejection, ProviderIntentRejection.UNKNOWN_PROFILE)
        self.assertEqual(subject.revision, 1)

    def test_a_refused_intent_does_not_move_the_revision(self) -> None:
        subject = state()
        before = subject.revision
        for intent in (
            ProviderSettingsIntent.GRANT_CONSENT,
            ProviderSettingsIntent.REVOKE_CONSENT,
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            ProviderSettingsIntent.CLEAR_SELECTION,
        ):
            with self.subTest(intent=intent):
                result = subject.apply(intent, {})
                self.assertFalse(result.accepted)
        self.assertEqual(subject.revision, before)


class AssistedExecutionLeaseTests(unittest.TestCase):
    @staticmethod
    def _ready_state() -> ProviderSettingsState:
        qualified = qualified_profile(LOCAL_PROFILE)
        subject = ProviderSettingsState(profiles=(qualified,))
        select_exact_model(subject, qualified.profile_id)
        subject.recheck(lambda _profile, _credential: ready_observation(qualified))
        return subject

    def test_authority_mutation_signals_the_worker_without_waiting_for_it(self) -> None:
        registry = ProviderSettingsSessionRegistry(state_factory=self._ready_state)
        decision = registry.begin_assisted_execution(TEST_SESSION)
        self.assertTrue(decision.admitted)
        assert decision.lease is not None
        lease = decision.lease
        self.assertFalse(lease.cancelled())

        changed = dispatch_provider_settings(
            TEST_SESSION,
            ProviderSettingsIntent.CLEAR_MODEL,
            {},
            registry=registry,
        )
        self.assertTrue(changed["accepted"])
        self.assertTrue(lease.cancelled())
        self.assertTrue(registry.finish_assisted_execution(lease))

    def test_release_tombstone_and_entry_generation_reject_a_late_worker(self) -> None:
        now = [0.0]
        registry = ProviderSettingsSessionRegistry(
            state_factory=self._ready_state,
            clock=lambda: now[0],
            idle_ttl_seconds=10,
            absolute_ttl_seconds=20,
        )
        first = registry.begin_assisted_execution(TEST_SESSION)
        assert first.lease is not None
        self.assertTrue(registry.release(TEST_SESSION))
        self.assertTrue(first.lease.cancelled())

        now[0] = 11.0
        second = registry.begin_assisted_execution(TEST_SESSION)
        assert second.lease is not None
        self.assertNotEqual(first.lease.session_generation, second.lease.session_generation)
        self.assertTrue(first.lease.cancelled())
        self.assertFalse(second.lease.cancelled())

    def test_settings_mutation_cannot_interleave_with_execution_admission(self) -> None:
        entered = Event()
        release_admission = Event()
        mutation_finished = Event()

        class PausedAdmissionState(ProviderSettingsState):
            def execution_decision(self) -> ProviderExecutionDecision:
                entered.set()
                if not release_admission.wait(2):
                    raise AssertionError("admission was not released")
                return super().execution_decision()

        ready = self._ready_state()
        subject = PausedAdmissionState(**ready.__dict__)
        registry = ProviderSettingsSessionRegistry(state_factory=lambda: subject)
        admitted: list[ProviderSessionExecutionDecision] = []

        def mutate_settings() -> None:
            dispatch_provider_settings(
                TEST_SESSION,
                ProviderSettingsIntent.CLEAR_MODEL,
                {},
                registry=registry,
            )
            mutation_finished.set()

        admission = Thread(
            target=lambda: admitted.append(registry.begin_assisted_execution(TEST_SESSION))
        )
        mutation = Thread(target=mutate_settings)
        admission.start()
        self.assertTrue(entered.wait(2))
        mutation.start()
        self.assertFalse(mutation_finished.wait(0.1))
        release_admission.set()
        admission.join(2)
        mutation.join(2)
        self.assertEqual(len(admitted), 1)
        self.assertTrue(admitted[0].admitted)
        self.assertTrue(mutation_finished.is_set())
        assert admitted[0].lease is not None
        self.assertTrue(admitted[0].lease.cancelled())

    def test_release_waits_for_an_authorized_publication_transaction(self) -> None:
        registry = ProviderSettingsSessionRegistry(state_factory=self._ready_state)
        decision = registry.begin_assisted_execution(TEST_SESSION)
        assert decision.lease is not None
        lease = decision.lease
        entered = Event()
        release_operation = Event()
        publication_finished = Event()
        release_finished = Event()
        retained_claims: list[ProviderAuthorityClaim] = []

        def publication() -> None:
            def operation(authority: ProviderAuthorityClaim) -> str:
                retained_claims.append(authority)
                self.assertTrue(
                    authority.matches(
                        lease.session_id,
                        lease.session_generation,
                        lease.snapshot.authority_epoch,
                    )
                )
                forged = ProviderAuthorityClaim(
                    session_id=lease.session_id,
                    session_generation=lease.session_generation,
                    authority_epoch=lease.snapshot.authority_epoch,
                    _registry=registry,
                    _token=object(),
                )
                self.assertFalse(
                    forged.matches(
                        lease.session_id,
                        lease.session_generation,
                        lease.snapshot.authority_epoch,
                    )
                )
                entered.set()
                if not release_operation.wait(2):
                    raise AssertionError("publication was not released")
                return "published"

            self.assertEqual(
                registry.run_under_assisted_authority(
                    lease.session_id,
                    lease.session_generation,
                    lease.snapshot.authority_epoch,
                    operation,
                    lease=lease,
                ),
                "published",
            )
            publication_finished.set()

        publisher = Thread(target=publication)

        def release_session() -> None:
            registry.release(TEST_SESSION)
            release_finished.set()

        releaser = Thread(target=release_session)
        publisher.start()
        self.assertTrue(entered.wait(2))
        releaser.start()
        self.assertFalse(release_finished.wait(0.1))
        release_operation.set()
        publisher.join(2)
        releaser.join(2)
        self.assertTrue(publication_finished.is_set())
        self.assertTrue(release_finished.is_set())
        self.assertEqual(len(retained_claims), 1)
        self.assertFalse(
            retained_claims[0].matches(
                lease.session_id,
                lease.session_generation,
                lease.snapshot.authority_epoch,
            )
        )


class DisclosureTests(unittest.TestCase):
    """AC-03 and AC-10: the disclosure is facts, and the facts match the route row."""

    def test_the_disclosure_matches_the_backend_route_row_exactly(self) -> None:
        for pinned in (REMOTE_PROFILE, LOCAL_PROFILE):
            with self.subTest(profile=pinned.profile_id):
                route = route_for_family(pinned.family)
                disclosure = describe_transmission(route, pinned.capabilities)
                self.assertEqual(disclosure.destination, route.destination.value)
                self.assertEqual(disclosure.transfer_boundary, route.transfer_boundary.value)
                self.assertIs(disclosure.consent_required, route.consent_required)
                self.assertIs(disclosure.local_only, route.local_only)
                self.assertIs(
                    disclosure.requires_credential, pinned.capabilities.requires_credential
                )

    def test_the_disclosure_carries_no_prose_a_locale_could_soften(self) -> None:
        wire = describe_transmission(
            route_for_family(REMOTE_PROFILE.family), REMOTE_PROFILE.capabilities
        ).to_wire()
        for key, value in wire.items():
            with self.subTest(key=key):
                if isinstance(value, str):
                    # Every string here is an accepted enum value or the scope constant: one token,
                    # no spaces. A sentence would be translatable and therefore weakenable.
                    self.assertNotIn(" ", value)
                else:
                    self.assertIsInstance(value, (bool, list, int))

    def test_a_text_only_family_cannot_report_transmitting_media(self) -> None:
        disclosure = describe_transmission(
            route_for_family(REMOTE_PROFILE.family),
            REMOTE_PROFILE.capabilities,
            media_attached=True,
        )
        self.assertFalse(disclosure.transmits_media)

        with_media = profile(
            "remote.vision",
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
            "https://api.example.com/v1",
            media=("text", "image"),
        )
        self.assertTrue(
            describe_transmission(
                route_for_family(with_media.family),
                with_media.capabilities,
                media_attached=True,
            ).transmits_media
        )

    def test_the_disclosure_states_the_consent_scope(self) -> None:
        # AC-12: a session-scoped decision that presents as permanent is a consent defect.
        disclosure = describe_transmission(
            route_for_family(REMOTE_PROFILE.family), REMOTE_PROFILE.capabilities
        )
        self.assertEqual(disclosure.consent_scope, CONSENT_SCOPE)
        self.assertEqual(CONSENT_SCOPE, "session_only")

    def test_a_capability_from_another_family_is_refused(self) -> None:
        with self.assertRaises(PromptModelContractError):
            describe_transmission(
                route_for_family(PromptModelFamily.OLLAMA), REMOTE_PROFILE.capabilities
            )


class ProfileViewTests(unittest.TestCase):
    def test_an_endpoint_reaches_the_browser_only_through_the_chokepoint(self) -> None:
        view = view_of_profile(REMOTE_PROFILE)
        self.assertEqual(view.host, "api.example.com")
        self.assertEqual(view.port, 443)
        self.assertNotIn("endpoint", view.to_wire())

    def test_an_endpoint_the_chokepoint_refuses_projects_no_host_at_all(self) -> None:
        hostile = profile(
            "remote.hostile",
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
            "https://user:pw@api.example.com/v1",  # pragma: allowlist secret
        )
        view = view_of_profile(hostile)
        self.assertEqual(view.host, "")
        self.assertEqual(view.port, 0)
        self.assertNotIn("pw", json.dumps(view.to_wire()))

    def test_identity_fields_survive_verbatim(self) -> None:
        wire = view_of_profile(REMOTE_PROFILE).to_wire()
        self.assertTrue({"model_id", "model_digest", "license_id"}.isdisjoint(wire))
        self.assertEqual(wire["adapter_version"], REMOTE_PROFILE.adapter_version)
        self.assertEqual(wire["parser_version"], REMOTE_PROFILE.parser_version)


class ReadinessTests(unittest.TestCase):
    """AC-02: each unready state is a distinct, backend-decided fact."""

    def test_nothing_selected_is_not_configured(self) -> None:
        self.assertIs(state().readiness(), ProviderReadiness.NOT_CONFIGURED)


class ExactModelSelectionTests(unittest.TestCase):
    """M22-11: a catalog profile names capability; only an explicit census admits a model."""

    def test_profile_selection_does_not_select_or_probe_a_model(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        projection = subject.project()
        self.assertEqual(projection.selected_model_id, "")
        self.assertEqual(projection.candidates, ())
        self.assertFalse(projection.reachability_observed)
        self.assertIs(projection.readiness, ProviderReadiness.NOT_CONFIGURED)

    def test_only_the_exact_admitted_catalog_model_can_be_selected(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        subject.recheck(
            lambda _profile, _credential: ReadinessObservation(
                reachable=True,
                candidates=(
                    DiscoveryCandidate(
                        identifier=LOCAL_PROFILE.model_id,
                        reason=DiscoveryRejection.ADMITTED,
                    ),
                    DiscoveryCandidate(
                        identifier="untrusted-model",
                        reason=DiscoveryRejection.UNPINNED,
                    ),
                ),
            )
        )

        for model_id in ("untrusted-model", "invented-model", "../model"):
            with self.subTest(model_id=model_id):
                before = subject.revision
                refused = subject.apply(ProviderSettingsIntent.SELECT_MODEL, {"model_id": model_id})
                self.assertFalse(refused.accepted)
                self.assertIs(refused.rejection, ProviderIntentRejection.UNKNOWN_MODEL)
                self.assertEqual(subject.revision, before)
                self.assertEqual(subject.project().selected_model_id, "")

        accepted = subject.apply(
            ProviderSettingsIntent.SELECT_MODEL, {"model_id": LOCAL_PROFILE.model_id}
        )
        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.projection.selected_model_id, LOCAL_PROFILE.model_id)
        self.assertFalse(accepted.projection.reachability_observed)
        self.assertEqual(
            tuple(item.identifier for item in accepted.projection.candidates),
            (LOCAL_PROFILE.model_id, "untrusted-model"),
        )

    def test_model_cannot_be_selected_before_explicit_refresh(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        refused = subject.apply(
            ProviderSettingsIntent.SELECT_MODEL, {"model_id": LOCAL_PROFILE.model_id}
        )
        self.assertFalse(refused.accepted)
        self.assertIs(refused.rejection, ProviderIntentRejection.UNKNOWN_MODEL)
        self.assertEqual(refused.projection.selected_model_id, "")

    def test_duplicate_exact_identifiers_are_never_admitted(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        subject.record_candidates(
            (
                DiscoveryCandidate(
                    identifier=LOCAL_PROFILE.model_id,
                    reason=DiscoveryRejection.ADMITTED,
                ),
                DiscoveryCandidate(
                    identifier=LOCAL_PROFILE.model_id,
                    reason=DiscoveryRejection.AMBIGUOUS_FOLDER,
                ),
            )
        )
        refused = subject.apply(
            ProviderSettingsIntent.SELECT_MODEL, {"model_id": LOCAL_PROFILE.model_id}
        )
        self.assertFalse(refused.accepted)
        self.assertIs(refused.rejection, ProviderIntentRejection.UNKNOWN_MODEL)
        self.assertEqual(refused.projection.selected_model_id, "")

    def test_a_later_empty_census_revokes_ready_evidence(self) -> None:
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        admitted = ReadinessObservation(
            reachable=True,
            candidates=(
                DiscoveryCandidate(
                    identifier=LOCAL_PROFILE.model_id,
                    reason=DiscoveryRejection.ADMITTED,
                ),
            ),
        )
        self.assertIs(
            subject.recheck(lambda _profile, _credential: admitted).projection.readiness,
            ProviderReadiness.READY,
        )
        empty = subject.recheck(
            lambda _profile, _credential: ReadinessObservation(reachable=True, candidates=())
        )
        self.assertEqual(empty.projection.selected_model_id, "")
        self.assertEqual(empty.projection.candidates, ())
        self.assertIs(empty.projection.readiness, ProviderReadiness.NOT_CONFIGURED)

    def test_ready_requires_explicit_exact_model_selection_and_a_fresh_recheck(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        observation = ReadinessObservation(
            reachable=True,
            candidates=(
                DiscoveryCandidate(
                    identifier=LOCAL_PROFILE.model_id,
                    reason=DiscoveryRejection.ADMITTED,
                ),
            ),
        )
        first = subject.recheck(lambda _profile, _credential: observation)
        self.assertIs(first.projection.readiness, ProviderReadiness.NOT_CONFIGURED)
        selected = subject.apply(
            ProviderSettingsIntent.SELECT_MODEL, {"model_id": LOCAL_PROFILE.model_id}
        )
        self.assertIs(selected.projection.readiness, ProviderReadiness.UNREACHABLE)
        ready = subject.recheck(lambda _profile, _credential: observation)
        self.assertIs(ready.projection.readiness, ProviderReadiness.READY)

    def test_model_preserves_consent_and_profile_change_revokes_it(self) -> None:
        subject = state()
        select(subject, REMOTE_ID)
        subject.apply(
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": CREDENTIAL_SENTINEL},
        )
        subject.apply(
            ProviderSettingsIntent.GRANT_CONSENT,
            {"network_permitted": True, "media_upload_consented": False},
        )
        admitted = DiscoveryCandidate(
            identifier=REMOTE_PROFILE.model_id,
            reason=DiscoveryRejection.ADMITTED,
        )
        subject.recheck(
            lambda _profile, _credential: ReadinessObservation(
                reachable=True, candidates=(admitted,)
            )
        )
        changed = subject.apply(
            ProviderSettingsIntent.SELECT_MODEL, {"model_id": REMOTE_PROFILE.model_id}
        )
        self.assertEqual(changed.projection.consent.status, "granted")  # type: ignore[union-attr]
        self.assertTrue(changed.projection.credential_present)
        self.assertIs(changed.projection.readiness, ProviderReadiness.UNREACHABLE)

        subject.apply(
            ProviderSettingsIntent.GRANT_CONSENT,
            {"network_permitted": True, "media_upload_consented": False},
        )
        select(subject, LOCAL_ID)
        select(subject, REMOTE_ID)
        returned = subject.project()
        self.assertEqual(returned.selected_model_id, "")
        returned_consent = returned.consent
        self.assertIsNotNone(returned_consent)
        assert returned_consent is not None
        self.assertEqual(returned_consent.status, "denied")
        self.assertTrue(returned.credential_present)
        self.assertIs(returned.readiness, ProviderReadiness.NOT_CONFIGURED)

    def test_none_same_profile_and_model_clear_each_invalidate_consent(self) -> None:
        subject = state()
        select_exact_model(subject, REMOTE_ID)
        subject.apply(
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": CREDENTIAL_SENTINEL},
        )

        def grant() -> None:
            result = subject.apply(
                ProviderSettingsIntent.GRANT_CONSENT,
                {"network_permitted": True, "media_upload_consented": False},
            )
            self.assertTrue(result.accepted)

        grant()
        replayed = subject.apply(ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": REMOTE_ID})
        self.assertEqual(replayed.projection.selected_model_id, "")
        replayed_consent = replayed.projection.consent
        self.assertIsNotNone(replayed_consent)
        assert replayed_consent is not None
        self.assertEqual(replayed_consent.status, "denied")

        select_exact_model(subject, REMOTE_ID)
        grant()
        cleared_model = subject.apply(ProviderSettingsIntent.CLEAR_MODEL, {})
        self.assertEqual(cleared_model.projection.selected_model_id, "")
        cleared_consent = cleared_model.projection.consent
        self.assertIsNotNone(cleared_consent)
        assert cleared_consent is not None
        self.assertEqual(cleared_consent.status, "granted")

        select_exact_model(subject, REMOTE_ID)
        grant()
        cleared_profile = subject.apply(ProviderSettingsIntent.CLEAR_SELECTION, {})
        self.assertEqual(cleared_profile.projection.selected_profile_id, "")
        self.assertEqual(cleared_profile.projection.selected_model_id, "")
        self.assertEqual(cleared_profile.projection.candidates, ())
        self.assertIsNone(cleared_profile.projection.consent)
        self.assertIs(cleared_profile.projection.readiness, ProviderReadiness.NOT_CONFIGURED)


class ProviderReadinessTests(unittest.TestCase):
    """Existing readiness states, now evaluated after an explicit exact-model choice."""

    def test_a_remote_profile_without_credential_or_consent_is_not_configured(self) -> None:
        subject = state()
        select_exact_model(subject, REMOTE_ID)
        self.assertIs(subject.readiness(), ProviderReadiness.NOT_CONFIGURED)
        subject.apply(
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": CREDENTIAL_SENTINEL},
        )
        self.assertIs(subject.readiness(), ProviderReadiness.NOT_CONFIGURED)
        subject.apply(
            ProviderSettingsIntent.GRANT_CONSENT,
            {"network_permitted": True, "media_upload_consented": False},
        )
        # Consent and a credential are not the same as being reachable.
        seed_model_choice(subject, REMOTE_ID)
        self.assertIs(subject.readiness(), ProviderReadiness.UNREACHABLE)

    def test_an_unobserved_provider_is_unreachable_rather_than_ready(self) -> None:
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        self.assertIs(subject.readiness(), ProviderReadiness.UNREACHABLE)
        subject.observe_reachable(LOCAL_ID, True)
        self.assertIs(subject.readiness(), ProviderReadiness.READY)
        subject.observe_reachable(LOCAL_ID, False)
        self.assertIs(subject.readiness(), ProviderReadiness.UNREACHABLE)

    def test_the_projection_separates_never_asked_from_asked_and_silent(self) -> None:
        # `UNREACHABLE` covers two different facts. A surface that renders only the enum would
        # tell a user their host is down when nothing had contacted it, so the projection carries
        # whether an observation exists at all.
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        self.assertIs(subject.readiness(), ProviderReadiness.UNREACHABLE)
        self.assertFalse(subject.project().reachability_observed)
        self.assertIs(subject.project().to_wire()["reachability_observed"], False)
        subject.observe_reachable(LOCAL_ID, False)
        self.assertIs(subject.readiness(), ProviderReadiness.UNREACHABLE)
        self.assertTrue(subject.project().reachability_observed)

    def test_an_observation_belongs_to_the_profile_it_was_made_about(self) -> None:
        subject = state()
        subject.observe_reachable(LOCAL_ID, True)
        select(subject, REMOTE_ID)
        # Another profile's observation is not evidence about this one.
        self.assertFalse(subject.project().reachability_observed)

    def test_a_reachable_but_mismatched_provider_is_incompatible(self) -> None:
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        subject.observe_reachable(LOCAL_ID, True)
        for outcome_id in (
            PromptModelOutcomeId.CAPABILITY_MISMATCH,
            PromptModelOutcomeId.DIGEST_MISMATCH,
            PromptModelOutcomeId.MODEL_MISSING,
        ):
            with self.subTest(outcome_id=outcome_id):
                subject.record_outcome(
                    build_prompt_model_outcome(
                        outcome_id,
                        severity=ValidationSeverity.ERROR,
                        remediation=PromptModelRemediation.SELECT_MODEL,
                        parameters=(),
                    )
                )
                self.assertIs(subject.readiness(), ProviderReadiness.INCOMPATIBLE)

    def test_rechecking_clears_a_stale_failure(self) -> None:
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        subject.observe_reachable(LOCAL_ID, True)
        subject.record_outcome(
            build_prompt_model_outcome(
                PromptModelOutcomeId.MODEL_MISSING,
                severity=ValidationSeverity.ERROR,
                remediation=PromptModelRemediation.SELECT_MODEL,
                parameters=(),
            )
        )
        self.assertIs(subject.readiness(), ProviderReadiness.INCOMPATIBLE)
        result = subject.apply(ProviderSettingsIntent.RECHECK_READINESS, {})
        self.assertTrue(result.accepted)
        self.assertIs(subject.readiness(), ProviderReadiness.NOT_CONFIGURED)
        self.assertFalse(result.projection.reachability_observed)
        self.assertEqual(result.projection.candidates, ())

    def test_no_probe_recheck_cannot_preserve_a_previous_ready_observation(self) -> None:
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        ready = subject.recheck(lambda profile_value, _credential: ready_observation(profile_value))
        self.assertIs(ready.projection.readiness, ProviderReadiness.READY)

        without_probe = subject.recheck()
        self.assertIs(without_probe.projection.readiness, ProviderReadiness.NOT_CONFIGURED)
        self.assertFalse(without_probe.projection.reachability_observed)
        self.assertEqual(without_probe.projection.candidates, ())

    def test_an_explicit_recheck_calls_one_probe_and_makes_ready_reachable(self) -> None:
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        calls: list[tuple[PromptModelProfile, object]] = []

        def probe(profile_value: PromptModelProfile, credential: object) -> ReadinessObservation:
            calls.append((profile_value, credential))
            return ready_observation(profile_value)

        result = subject.recheck(probe)
        self.assertTrue(result.accepted)
        self.assertIs(result.projection.readiness, ProviderReadiness.READY)
        self.assertTrue(result.projection.reachability_observed)
        self.assertEqual(calls, [(LOCAL_PROFILE, None)])

    def test_an_observation_refuses_contradictory_reachability_facts(self) -> None:
        timeout = build_prompt_model_outcome(
            PromptModelOutcomeId.TIMEOUT,
            severity=ValidationSeverity.ERROR,
            remediation=PromptModelRemediation.RETRY_LATER,
            parameters=(),
        )
        candidate = DiscoveryCandidate(
            identifier=LOCAL_PROFILE.model_id,
            reason=DiscoveryRejection.ADMITTED,
        )
        for values in (
            {"reachable": False},
            {"reachable": True, "outcome": timeout},
            {"reachable": False, "outcome": timeout, "candidates": (candidate,)},
        ):
            with self.subTest(values=values), self.assertRaises(PromptModelContractError):
                ReadinessObservation(**values)

    def test_remote_recheck_never_calls_probe_without_consent_and_credential(self) -> None:
        subject = state()
        select_exact_model(subject, REMOTE_ID)
        calls = 0

        def probe(_profile: PromptModelProfile, _credential: object) -> ReadinessObservation:
            nonlocal calls
            calls += 1
            return ready_observation(_profile)

        without_both = subject.recheck(probe)
        self.assertEqual(calls, 0)
        self.assertFalse(without_both.projection.reachability_observed)
        self.assertEqual(
            without_both.projection.diagnostic.outcome_id,  # type: ignore[union-attr]
            PromptModelOutcomeId.CONSENT_REQUIRED.value,
        )

        subject.apply(
            ProviderSettingsIntent.GRANT_CONSENT,
            {"network_permitted": True, "media_upload_consented": False},
        )
        without_credential = subject.recheck(probe)
        self.assertEqual(calls, 0)
        self.assertFalse(without_credential.projection.reachability_observed)

        subject.apply(
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": CREDENTIAL_SENTINEL},
        )
        # A replacement credential withdraws the old grant and census.
        subject.apply(
            ProviderSettingsIntent.GRANT_CONSENT,
            {"network_permitted": True, "media_upload_consented": False},
        )
        seed_model_choice(subject, REMOTE_ID)
        admitted = subject.recheck(probe)
        self.assertEqual(calls, 1)
        self.assertIs(admitted.projection.readiness, ProviderReadiness.READY)

    def test_a_new_observation_replaces_a_stale_census_even_when_it_is_empty(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        subject.record_candidates(
            [DiscoveryCandidate(identifier="old.model", reason=DiscoveryRejection.UNPINNED)]
        )
        result = subject.recheck(lambda _profile, _credential: ReadinessObservation(reachable=True))
        self.assertEqual(result.projection.candidates, ())
        self.assertFalse(result.projection.candidates_truncated)

    def test_selecting_a_profile_invalidates_its_old_observation_and_the_old_census(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        candidate = DiscoveryCandidate(
            identifier=LOCAL_PROFILE.model_id,
            reason=DiscoveryRejection.ADMITTED,
        )
        subject.recheck(
            lambda _profile, _credential: ReadinessObservation(
                reachable=True, candidates=(candidate,)
            )
        )
        select(subject, REMOTE_ID)
        select(subject, LOCAL_ID)
        returned = subject.project()
        self.assertFalse(returned.reachability_observed)
        self.assertIs(returned.readiness, ProviderReadiness.NOT_CONFIGURED)
        self.assertEqual(returned.candidates, ())

    def test_replacing_a_credential_invalidates_the_observation_it_authenticated(self) -> None:
        subject = state()
        select_exact_model(subject, REMOTE_ID)
        subject.apply(
            ProviderSettingsIntent.GRANT_CONSENT,
            {"network_permitted": True, "media_upload_consented": False},
        )
        subject.apply(
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": CREDENTIAL_SENTINEL},
        )
        subject.recheck(lambda _profile, _credential: ReadinessObservation(reachable=True))
        replacement = subject.apply(
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": "test-credential-" + "R" * 40},
        ).projection
        self.assertFalse(replacement.reachability_observed)
        self.assertIs(replacement.readiness, ProviderReadiness.NOT_CONFIGURED)

    def test_reading_projection_never_probes_clears_diagnostics_or_moves_revision(self) -> None:
        read_intent = getattr(ProviderSettingsIntent, "READ_PROJECTION", None)
        self.assertIsNotNone(read_intent)
        if read_intent is None:
            return
        subject = state()
        select(subject, LOCAL_ID)
        subject.record_outcome(
            build_prompt_model_outcome(
                PromptModelOutcomeId.MODEL_MISSING,
                severity=ValidationSeverity.ERROR,
                remediation=PromptModelRemediation.SELECT_MODEL,
                parameters=(),
            )
        )
        before = subject.project()
        result = subject.apply(read_intent, {})
        self.assertTrue(result.accepted)
        self.assertEqual(result.projection, before)
        self.assertEqual(subject.revision, before.revision)


class ConsentIntentTests(unittest.TestCase):
    def test_consent_is_offered_only_where_the_route_requires_it(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        self.assertFalse(subject.project().consent_required)
        refused = subject.apply(ProviderSettingsIntent.GRANT_CONSENT, {"network_permitted": True})
        self.assertIs(refused.rejection, ProviderIntentRejection.CONSENT_NOT_APPLICABLE)

        select(subject, REMOTE_ID)
        self.assertTrue(subject.project().consent_required)

    def test_granting_and_revoking_both_land_and_are_visible(self) -> None:
        subject = state()
        select(subject, REMOTE_ID)
        granted = subject.apply(
            ProviderSettingsIntent.GRANT_CONSENT,
            {"network_permitted": True, "media_upload_consented": True},
        )
        self.assertTrue(granted.accepted)
        consent = granted.projection.consent
        assert consent is not None
        self.assertEqual(consent.status, "granted")
        self.assertTrue(consent.network_permitted)
        # M22-11 profiles are text-only. A hostile client cannot create media-upload authority
        # merely by setting a hidden request flag.
        self.assertFalse(consent.media_upload_consented)
        self.assertEqual(consent.scope, CONSENT_SCOPE)

        revoked = subject.apply(ProviderSettingsIntent.REVOKE_CONSENT, {})
        self.assertTrue(revoked.accepted)
        after = revoked.projection.consent
        assert after is not None
        self.assertEqual(after.status, "denied")
        self.assertFalse(after.network_permitted)
        self.assertGreater(after.revision, consent.revision)

    def test_an_omitted_flag_is_a_refusal_to_consent_not_a_default_yes(self) -> None:
        subject = state()
        select(subject, REMOTE_ID)
        granted = subject.apply(ProviderSettingsIntent.GRANT_CONSENT, {})
        consent = granted.projection.consent
        assert consent is not None
        self.assertFalse(consent.network_permitted)
        self.assertFalse(consent.media_upload_consented)

    def test_media_consent_is_recorded_only_for_a_media_capable_profile(self) -> None:
        media_profile = profile(
            "remote.example.media",
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
            "https://media.example.com/v1",
            media=("text", "image"),
        )
        subject = state(media_profile)
        select(subject, media_profile.profile_id)
        granted = subject.apply(
            ProviderSettingsIntent.GRANT_CONSENT,
            {"network_permitted": True, "media_upload_consented": True},
        )
        assert granted.projection.consent is not None
        self.assertTrue(granted.projection.consent.media_upload_consented)

    def test_revoking_before_granting_is_refused(self) -> None:
        subject = state()
        select(subject, REMOTE_ID)
        refused = subject.apply(ProviderSettingsIntent.REVOKE_CONSENT, {})
        self.assertIs(refused.rejection, ProviderIntentRejection.CONSENT_NOT_APPLICABLE)

    def test_remote_families_are_derived_from_the_matrix(self) -> None:
        self.assertEqual(
            remote_families(),
            (
                PromptModelFamily.REMOTE_ANTHROPIC,
                PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
            ),
        )


class CredentialTests(unittest.TestCase):
    """AC-04: nothing that crosses back carries the secret."""

    def test_a_credential_is_held_but_never_projected(self) -> None:
        subject = state()
        select(subject, REMOTE_ID)
        result = subject.apply(
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": CREDENTIAL_SENTINEL},
        )
        self.assertTrue(result.accepted)
        wire = json.dumps(result.to_wire())
        self.assertNotIn(CREDENTIAL_SENTINEL, wire)
        self.assertNotIn(CREDENTIAL_SENTINEL[:12], wire)
        self.assertNotIn(CREDENTIAL_SENTINEL[-4:], wire)
        self.assertEqual(result.projection.credential_last_four, "")
        self.assertTrue(result.projection.credential_present)
        with self.assertRaises(PromptModelContractError):
            replace(result.projection, credential_last_four=CREDENTIAL_SENTINEL[-4:])

    def test_a_rejected_credential_says_only_that_it_was_rejected(self) -> None:
        subject = state()
        select(subject, REMOTE_ID)
        for value in ("", "with\nnewline", "x" * 4_096, None, 5):
            with self.subTest(value=str(value)[:12]):
                result = subject.apply(
                    ProviderSettingsIntent.SUBMIT_CREDENTIAL, {"credential": value}
                )
                self.assertIs(result.rejection, ProviderIntentRejection.CREDENTIAL_REJECTED)
                self.assertNotIn("newline", json.dumps(result.to_wire()))
        self.assertFalse(subject.project().credential_present)

    def test_a_credential_is_not_offered_where_the_family_needs_none(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        self.assertFalse(subject.project().credential_required)
        result = subject.apply(
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": CREDENTIAL_SENTINEL},
        )
        self.assertIs(result.rejection, ProviderIntentRejection.CREDENTIAL_NOT_APPLICABLE)

    def test_discarding_a_credential_takes_it_out_of_the_session(self) -> None:
        subject = state()
        select(subject, REMOTE_ID)
        subject.apply(
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": CREDENTIAL_SENTINEL},
        )
        result = subject.apply(ProviderSettingsIntent.DISCARD_CREDENTIAL, {})
        self.assertTrue(result.accepted)
        self.assertFalse(result.projection.credential_present)
        self.assertEqual(result.projection.credential_last_four, "")

    def test_nothing_in_this_state_is_persisted(self) -> None:
        subject = state()
        select(subject, REMOTE_ID)
        subject.apply(
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            {"credential": CREDENTIAL_SENTINEL},
        )
        subject.apply(ProviderSettingsIntent.GRANT_CONSENT, {"network_permitted": True})
        fresh = state()
        self.assertEqual(fresh.selected_profile_id, "")
        self.assertFalse(fresh.project().credential_present)
        self.assertIsNone(fresh.project().consent)


class ScanDetailTests(unittest.TestCase):
    """AC-06: every candidate is named with its reason, and naming is not offering."""

    def test_every_candidate_carries_its_own_closed_reason(self) -> None:
        subject = state()
        subject.record_candidates(
            [
                DiscoveryCandidate(identifier="a", reason=DiscoveryRejection.ADMITTED),
                DiscoveryCandidate(identifier="b", reason=DiscoveryRejection.UNPINNED),
                DiscoveryCandidate(identifier="c", reason=DiscoveryRejection.MISSING_ROOT),
            ]
        )
        wire = subject.project().to_wire()["candidates"]
        assert isinstance(wire, list)
        self.assertEqual(
            [entry["reason"] for entry in wire], ["admitted", "unpinned", "missing_root"]
        )

    def test_naming_an_unpinned_candidate_is_not_an_offer_to_select_it(self) -> None:
        subject = state()
        subject.record_candidates(
            [DiscoveryCandidate(identifier="unpinned.model", reason=DiscoveryRejection.UNPINNED)]
        )
        result = subject.apply(
            ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": "unpinned.model"}
        )
        self.assertIs(result.rejection, ProviderIntentRejection.UNKNOWN_PROFILE)

    def test_every_candidate_inside_the_accepted_bound_crosses_the_projection(self) -> None:
        subject = state()
        subject.record_candidates(
            [
                DiscoveryCandidate(
                    identifier=f"candidate.{index}", reason=DiscoveryRejection.UNPINNED
                )
                for index in range(MAX_PROJECTED_CANDIDATES)
            ]
        )
        projection = subject.project()
        self.assertEqual(len(projection.candidates), MAX_PROJECTED_CANDIDATES)
        self.assertFalse(projection.candidates_truncated)

    def test_an_exact_candidate_after_the_old_ui_window_remains_selectable(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        subject.record_candidates(
            [
                *(
                    DiscoveryCandidate(
                        identifier=f"candidate.{index}", reason=DiscoveryRejection.UNPINNED
                    )
                    for index in range(24)
                ),
                DiscoveryCandidate(
                    identifier=LOCAL_PROFILE.model_id,
                    reason=DiscoveryRejection.ADMITTED,
                ),
            ]
        )
        result = subject.apply(
            ProviderSettingsIntent.SELECT_MODEL,
            {"model_id": LOCAL_PROFILE.model_id},
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.projection.selected_model_id, LOCAL_PROFILE.model_id)

    def test_a_duplicate_across_the_old_projection_boundary_fails_closed(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        subject.record_candidates(
            [
                DiscoveryCandidate(
                    identifier=LOCAL_PROFILE.model_id,
                    reason=DiscoveryRejection.ADMITTED,
                ),
                *(
                    DiscoveryCandidate(
                        identifier=f"candidate.{index}", reason=DiscoveryRejection.UNPINNED
                    )
                    for index in range(23)
                ),
                DiscoveryCandidate(
                    identifier=LOCAL_PROFILE.model_id,
                    reason=DiscoveryRejection.AMBIGUOUS_FOLDER,
                ),
            ]
        )
        result = subject.apply(
            ProviderSettingsIntent.SELECT_MODEL,
            {"model_id": LOCAL_PROFILE.model_id},
        )
        self.assertFalse(result.accepted)
        self.assertIs(result.rejection, ProviderIntentRejection.UNKNOWN_MODEL)

    def test_an_oversized_provider_census_is_refused_before_it_becomes_state(self) -> None:
        entries = tuple(
            DiscoveryCandidate(identifier=f"candidate.{index}", reason=DiscoveryRejection.UNPINNED)
            for index in range(MAX_DISCOVERY_ROWS + 1)
        )
        with self.assertRaisesRegex(PromptModelContractError, "observation_candidates"):
            ReadinessObservation(reachable=True, candidates=entries)
        subject = state()
        with self.assertRaisesRegex(PromptModelContractError, "candidates"):
            subject.record_candidates(entries)
        self.assertEqual(subject.project().candidates, ())


class DiagnosticTests(unittest.TestCase):
    """AC-05: a failure carries its identity and the remediation declared for it, not prose."""

    def test_ok_is_not_a_diagnostic(self) -> None:
        self.assertIsNone(
            diagnostic_of(
                build_prompt_model_outcome(
                    PromptModelOutcomeId.OK,
                    severity=ValidationSeverity.INFO,
                    remediation=PromptModelRemediation.NONE,
                    parameters=(),
                )
            )
        )
        self.assertIsNone(diagnostic_of(None))

    def test_a_diagnostic_carries_identity_and_remediation_not_a_message(self) -> None:
        outcome = build_prompt_model_outcome(
            PromptModelOutcomeId.BACKEND_ABSENT,
            severity=ValidationSeverity.ERROR,
            remediation=PromptModelRemediation.INSTALL_BACKEND,
            parameters=(("family", "ollama"),),
        )
        diagnostic = diagnostic_of(outcome)
        assert diagnostic is not None
        self.assertEqual(diagnostic.outcome_id, "prompt_model.backend_absent")
        self.assertEqual(diagnostic.remediation, "install_backend")
        wire = diagnostic.to_wire()
        self.assertEqual(set(wire), {"outcome_id", "severity", "remediation", "parameters"})


class RequestDecodeTests(unittest.TestCase):
    def encode(self, intent: str, payload: object = None) -> bytes:
        return json.dumps(
            {
                "schema": PROVIDER_SETTINGS_REQUEST_SCHEMA,
                "intent": intent,
                "payload": payload,
            }
        ).encode("utf-8")

    def test_a_well_formed_request_decodes(self) -> None:
        intent, payload = decode_provider_settings_request(
            self.encode("select_profile", {"profile_id": REMOTE_ID})
        )
        self.assertIs(intent, ProviderSettingsIntent.SELECT_PROFILE)
        self.assertEqual(payload, {"profile_id": REMOTE_ID})

    def test_an_exact_model_selection_request_decodes(self) -> None:
        for model_id in ("model-a", "qwen3:8b", "org/model-revision"):
            with self.subTest(model_id=model_id):
                intent, payload = decode_provider_settings_request(
                    self.encode(
                        "select_model",
                        {"model_id": model_id, "profile_id": LOCAL_ID, "expected_revision": 1},
                    )
                )
                self.assertIs(intent, ProviderSettingsIntent.SELECT_MODEL)
                self.assertEqual(
                    payload, {"model_id": model_id, "profile_id": LOCAL_ID, "expected_revision": 1}
                )

    def test_a_malformed_model_identifier_never_reaches_the_state(self) -> None:
        for bad in ("../model", "model..revision", " space", "x" * 129, 7):
            with self.subTest(bad=str(bad)[:16]):
                with self.assertRaises(ProviderSettingsRequestError):
                    decode_provider_settings_request(self.encode("select_model", {"model_id": bad}))

    def test_the_request_shape_is_closed(self) -> None:
        cases = {
            "request_empty": b"",
            "request_malformed": b"{",
            "request_shape": json.dumps({"schema": PROVIDER_SETTINGS_REQUEST_SCHEMA}).encode(),
            "request_schema": json.dumps(
                {"schema": "other/1", "intent": "select_profile", "payload": None}
            ).encode(),
            "request_intent": self.encode("enable_everything"),
            "request_payload": self.encode("select_profile", {"unexpected": "x"}),
        }
        for code, payload in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(ProviderSettingsRequestError) as caught:
                    decode_provider_settings_request(payload)
                self.assertEqual(caught.exception.code, code)

    def test_an_oversized_request_is_refused_before_it_is_parsed(self) -> None:
        payload = self.encode("submit_credential", {"credential": "x" * 9_000})
        self.assertGreater(len(payload), MAX_PROVIDER_SETTINGS_BYTES)
        with self.assertRaises(ProviderSettingsRequestError) as caught:
            decode_provider_settings_request(payload)
        self.assertEqual(caught.exception.code, "request_too_large")

    def test_a_duplicate_member_is_refused(self) -> None:
        raw = (
            '{"schema": "'
            + PROVIDER_SETTINGS_REQUEST_SCHEMA
            + '", "intent": "clear_selection", "payload": null, "intent": "x"}'
        ).encode("utf-8")
        with self.assertRaises(ProviderSettingsRequestError) as caught:
            decode_provider_settings_request(raw)
        self.assertEqual(caught.exception.code, "duplicate_member")

    def test_a_malformed_profile_identifier_never_reaches_the_state(self) -> None:
        for bad in ("../etc", "Upper", "x" * 200, 7):
            with self.subTest(bad=str(bad)[:8]):
                with self.assertRaises(ProviderSettingsRequestError):
                    decode_provider_settings_request(
                        self.encode("select_profile", {"profile_id": bad})
                    )


class RouteDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_provider_settings_state()
        self.addCleanup(reset_provider_settings_state)

    def test_the_route_path_is_the_declared_one(self) -> None:
        self.assertEqual(PROVIDER_SETTINGS_ROUTE, "/h3-context/v1/provider/settings")

    def test_dispatch_uses_the_shipped_catalog_and_refuses_an_invented_provider(self) -> None:
        wire = dispatch_provider_settings(
            TEST_SESSION, ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": "invented.provider"}
        )
        self.assertFalse(wire["accepted"])
        self.assertEqual(wire["rejection"], "unknown_profile")
        projection = wire["projection"]
        assert isinstance(projection, dict)
        self.assertEqual(projection["schema"], PROVIDER_SETTINGS_SCHEMA)
        self.assertFalse(projection["catalog_empty"])

    def test_the_session_state_is_the_shipped_catalog(self) -> None:
        self.assertEqual(
            provider_settings_state(TEST_SESSION).profiles,
            ProviderSettingsState.from_catalog().profiles,
        )

    def test_route_forces_media_consent_off_for_a_text_only_profile(self) -> None:
        subject = state(REMOTE_PROFILE)
        select(subject, REMOTE_ID)
        bind_test_session(subject)
        wire = dispatch_provider_settings(
            TEST_SESSION,
            ProviderSettingsIntent.GRANT_CONSENT,
            {"network_permitted": True, "media_upload_consented": True},
        )
        projection = cast(dict[str, object], wire["projection"])
        consent = cast(dict[str, object], projection["consent"])
        self.assertIs(consent["network_permitted"], True)
        self.assertIs(consent["media_upload_consented"], False)

    def test_a_recheck_with_nothing_selected_answers_rather_than_refuses(self) -> None:
        # The Settings surface reads once on open through this intent, so a refusal here would put
        # a "choose a profile first" banner in front of a user who has not touched anything.
        wire = dispatch_provider_settings(
            TEST_SESSION, ProviderSettingsIntent.RECHECK_READINESS, {}
        )
        self.assertTrue(wire["accepted"])
        self.assertIsNone(wire["rejection"])
        projection = wire["projection"]
        assert isinstance(projection, dict)
        self.assertEqual(projection["readiness"], "not_configured")

    def test_dispatch_injects_the_probe_only_for_explicit_recheck(self) -> None:
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        calls = 0

        def probe(_profile: PromptModelProfile, _credential: object) -> ReadinessObservation:
            nonlocal calls
            calls += 1
            return ready_observation(_profile)

        bind_test_session(subject)
        with patch.object(settings_adapter, "provider_settings_state", return_value=subject):
            wire = dispatch_provider_settings(
                TEST_SESSION,
                ProviderSettingsIntent.RECHECK_READINESS,
                {},
                readiness_probe=probe,
            )
        self.assertEqual(calls, 1)
        projection = wire["projection"]
        assert isinstance(projection, dict)
        self.assertEqual(projection["readiness"], "ready")
        self.assertIs(projection["reachability_observed"], True)

    def test_production_probe_judges_the_census_against_every_family_pin(self) -> None:
        second = replace(
            LOCAL_PROFILE,
            profile_id="ollama.local.second",
            model_id="other-model",
        )
        subject = ProviderSettingsState(profiles=(LOCAL_PROFILE, second))
        select(subject, LOCAL_ID)
        observed_pins: object = None

        def probe(
            _profile: object,
            _credential: object,
            *,
            pinned_identifiers: object = (),
            model_choice: object = None,
            cached_candidates: object = (),
            cancellation: object = None,
        ) -> ReadinessObservation:
            nonlocal observed_pins
            observed_pins = pinned_identifiers
            return ReadinessObservation(reachable=True)

        bind_test_session(subject)
        with (
            patch.object(settings_adapter, "provider_settings_state", return_value=subject),
            patch.object(readiness_adapter, "probe_provider_readiness", side_effect=probe),
        ):
            settings_adapter._probe_provider_readiness(subject, LOCAL_PROFILE)
        self.assertEqual(
            set(cast(tuple[str, ...], observed_pins)), {LOCAL_PROFILE.model_id, "other-model"}
        )

    def test_resetting_discards_the_session(self) -> None:
        first = provider_settings_state(TEST_SESSION)
        reset_provider_settings_state()
        self.assertIsNot(provider_settings_state(TEST_SESSION), first)


class AsyncRouteDispatchTests(unittest.IsolatedAsyncioTestCase):
    def async_dispatch(self) -> Callable[..., Coroutine[Any, Any, dict[str, object]]]:
        value = getattr(settings_adapter, "dispatch_provider_settings_async", None)
        self.assertTrue(callable(value))
        return cast(Callable[..., Coroutine[Any, Any, dict[str, object]]], value)

    async def test_concurrent_rechecks_share_exactly_one_probe(self) -> None:
        dispatch_async = self.async_dispatch()
        if not callable(dispatch_async):
            return
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        started = Event()
        release = Event()
        counter_lock = Lock()
        calls = 0

        def probe(_profile: PromptModelProfile, _credential: object) -> ReadinessObservation:
            nonlocal calls
            with counter_lock:
                calls += 1
            started.set()
            if not release.wait(2):
                raise AssertionError("probe was not released")
            return ready_observation(_profile)

        bind_test_session(subject)
        with patch.object(settings_adapter, "provider_settings_state", return_value=subject):
            first = asyncio.create_task(
                dispatch_async(
                    TEST_SESSION,
                    ProviderSettingsIntent.RECHECK_READINESS,
                    {},
                    readiness_probe=probe,
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            second = asyncio.create_task(
                dispatch_async(
                    TEST_SESSION,
                    ProviderSettingsIntent.RECHECK_READINESS,
                    {},
                    readiness_probe=probe,
                )
            )
            await asyncio.sleep(0)
            release.set()
            first_wire, second_wire = await asyncio.gather(first, second)

        self.assertEqual(calls, 1)
        self.assertEqual(first_wire, second_wire)
        first_projection = cast(dict[str, object], first_wire["projection"])
        self.assertEqual(first_projection["readiness"], "ready")

    async def test_state_mutation_waits_until_the_inflight_observation_lands(self) -> None:
        dispatch_async = self.async_dispatch()
        if not callable(dispatch_async):
            return
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        started = Event()
        release = Event()

        def probe(_profile: PromptModelProfile, _credential: object) -> ReadinessObservation:
            started.set()
            if not release.wait(2):
                raise AssertionError("probe was not released")
            return ready_observation(_profile)

        bind_test_session(subject)
        with patch.object(settings_adapter, "provider_settings_state", return_value=subject):
            recheck = asyncio.create_task(
                dispatch_async(
                    TEST_SESSION,
                    ProviderSettingsIntent.RECHECK_READINESS,
                    {},
                    readiness_probe=probe,
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            clear = asyncio.create_task(
                dispatch_async(TEST_SESSION, ProviderSettingsIntent.CLEAR_SELECTION, {})
            )
            await asyncio.sleep(0)
            self.assertFalse(clear.done())
            with self.assertRaises(ProviderSettingsSessionError) as pending:
                settings_adapter.provider_settings_registry().begin_assisted_execution(TEST_SESSION)
            self.assertEqual(pending.exception.code, "action_in_flight")
            release.set()
            recheck_wire, clear_wire = await asyncio.gather(recheck, clear)

        recheck_projection = cast(dict[str, object], recheck_wire["projection"])
        clear_projection = cast(dict[str, object], clear_wire["projection"])
        self.assertEqual(recheck_projection["selected_profile_id"], LOCAL_ID)
        self.assertEqual(recheck_projection["readiness"], "ready")
        self.assertEqual(clear_projection["selected_profile_id"], "")

    async def test_a_cancelled_waiter_does_not_cancel_the_shared_probe(self) -> None:
        dispatch_async = self.async_dispatch()
        if not callable(dispatch_async):
            return
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        started = Event()
        release = Event()
        calls = 0

        def probe(_profile: PromptModelProfile, _credential: object) -> ReadinessObservation:
            nonlocal calls
            calls += 1
            started.set()
            if not release.wait(2):
                raise AssertionError("probe was not released")
            return ready_observation(_profile)

        bind_test_session(subject)
        with patch.object(settings_adapter, "provider_settings_state", return_value=subject):
            cancelled = asyncio.create_task(
                dispatch_async(
                    TEST_SESSION,
                    ProviderSettingsIntent.RECHECK_READINESS,
                    {},
                    readiness_probe=probe,
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            survivor = asyncio.create_task(
                dispatch_async(
                    TEST_SESSION,
                    ProviderSettingsIntent.RECHECK_READINESS,
                    {},
                    readiness_probe=probe,
                )
            )
            await asyncio.sleep(0)
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            release.set()
            wire = await survivor

        self.assertEqual(calls, 1)
        projection = cast(dict[str, object], wire["projection"])
        self.assertEqual(projection["readiness"], "ready")

    async def test_each_later_explicit_gesture_starts_one_new_probe(self) -> None:
        dispatch_async = self.async_dispatch()
        if not callable(dispatch_async):
            return
        subject = state()
        select(subject, LOCAL_ID)
        calls = 0

        def probe(_profile: PromptModelProfile, _credential: object) -> ReadinessObservation:
            nonlocal calls
            calls += 1
            return ReadinessObservation(reachable=True)

        bind_test_session(subject)
        with patch.object(settings_adapter, "provider_settings_state", return_value=subject):
            for _ in range(2):
                await dispatch_async(
                    TEST_SESSION,
                    ProviderSettingsIntent.RECHECK_READINESS,
                    {},
                    readiness_probe=probe,
                )
        self.assertEqual(calls, 2)


class _ProviderRoutes(list[SimpleNamespace]):
    def _add(self, method: str, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.append(SimpleNamespace(method=method, path=path, handler=handler))
            return handler

        return decorate

    def post(self, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._add("POST", path)

    def delete(self, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._add("DELETE", path)


class _ProviderContent:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._done = False

    async def read(self, _limit: int) -> bytes:
        if self._done:
            return b""
        self._done = True
        return self._body


class _ProviderHeaders:
    def __init__(self, session_values: list[str] | None = None) -> None:
        self._session_values = [TEST_SESSION] if session_values is None else session_values

    def getall(self, name: str, default: list[str]) -> list[str]:
        if name == "Host":
            return [LOOPBACK_HOST]
        if name == "Origin":
            return ["http://127.0.0.1:8188"]
        if name == settings_adapter.PROVIDER_SESSION_HEADER:
            return self._session_values
        return default


def _provider_route_request(
    intent: str,
    *,
    payload: dict[str, object] | None = None,
    session_values: list[str] | None = None,
) -> SimpleNamespace:
    body = json.dumps(
        {
            "schema": PROVIDER_SETTINGS_REQUEST_SCHEMA,
            "intent": intent,
            "payload": {} if payload is None else payload,
        }
    ).encode("utf-8")
    return SimpleNamespace(
        content_type="application/json",
        content_length=len(body),
        content=_ProviderContent(body),
        headers=_ProviderHeaders(session_values),
        transport=ListenerTransport(),
    )


def _provider_route_handler(
    method: str = "POST",
) -> Callable[[object], Coroutine[Any, Any, Any]]:
    routes = _ProviderRoutes()
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(
        json_response=lambda value, status=200: SimpleNamespace(status=status, body=value),
        Response=lambda status=200: SimpleNamespace(status=status, body=None),
    )
    with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
        with patch.object(settings_adapter, "_ROUTE_REGISTERED", False):
            if not settings_adapter.ensure_provider_settings_route_registered():
                raise AssertionError("provider settings route was not registered")
    if len(routes) != 2:
        raise AssertionError("provider settings route pair was not registered exactly once")
    selected = next(route for route in routes if route.method == method)
    return cast(Callable[[object], Coroutine[Any, Any, Any]], selected.handler)


class ProviderSettingsRouteRegistrationTests(unittest.TestCase):
    def modules(self, routes: _ProviderRoutes) -> tuple[ModuleType, ModuleType]:
        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = SimpleNamespace(
            json_response=lambda value, status=200: SimpleNamespace(status=status, body=value),
            Response=lambda status=200: SimpleNamespace(status=status, body=None),
        )
        return server, aiohttp

    def test_the_owned_post_delete_pair_registers_idempotently(self) -> None:
        routes = _ProviderRoutes()
        server, aiohttp = self.modules(routes)
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            with patch.object(settings_adapter, "_ROUTE_REGISTERED", False):
                self.assertTrue(settings_adapter.ensure_provider_settings_route_registered())
                self.assertTrue(settings_adapter.ensure_provider_settings_route_registered())
        self.assertEqual(
            [(route.method, route.path) for route in routes],
            [
                ("POST", PROVIDER_SETTINGS_ROUTE),
                ("DELETE", PROVIDER_SETTINGS_ROUTE),
            ],
        )

    def test_a_foreign_release_route_is_never_claimed_or_completed(self) -> None:
        async def foreign(_request: object) -> object:
            return object()

        routes = _ProviderRoutes(
            [SimpleNamespace(method="DELETE", path=PROVIDER_SETTINGS_ROUTE, handler=foreign)]
        )
        server, aiohttp = self.modules(routes)
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            with patch.object(settings_adapter, "_ROUTE_REGISTERED", False):
                self.assertFalse(settings_adapter.ensure_provider_settings_route_registered())
        self.assertEqual(len(routes), 1)
        self.assertIs(routes[0].handler, foreign)


class ProviderSettingsRouteAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_model_is_a_typed_not_found_refusal(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        handler = _provider_route_handler()
        bind_test_session(subject)
        response = await handler(
            _provider_route_request(
                "select_model",
                payload={
                    "model_id": "invented-model",
                    "profile_id": LOCAL_ID,
                    "expected_revision": subject.revision,
                },
            )
        )
        self.assertEqual(response.status, 404)
        self.assertFalse(response.body["accepted"])
        self.assertEqual(response.body["rejection"], "unknown_model")
        self.assertNotIn("invented-model", json.dumps(response.body))

    async def test_missing_or_duplicate_session_header_fails_before_body_read(self) -> None:
        handler = _provider_route_handler()
        for values in ([], [TEST_SESSION, TEST_SESSION]):
            with self.subTest(values=len(values)):
                request = _provider_route_request("read_projection", session_values=values)
                response = await handler(request)
                self.assertEqual(response.status, 400)
                self.assertEqual(response.body, {"error": "session_rejected"})
                self.assertFalse(request.content._done)

    async def test_delete_releases_the_browser_authority_idempotently(self) -> None:
        subject = state()
        select(subject, LOCAL_ID)
        bind_test_session(subject)
        handler = _provider_route_handler("DELETE")
        first = await handler(_provider_route_request("read_projection"))
        second = await handler(_provider_route_request("read_projection"))
        self.assertEqual(first.status, 204)
        self.assertEqual(second.status, 204)
        with self.assertRaises(ProviderSettingsSessionError) as caught:
            provider_settings_state(TEST_SESSION)
        self.assertEqual(caught.exception.code, "session_released")

    async def test_a_resolver_timeout_releases_the_route_single_flight_slot(self) -> None:
        local_by_name = replace(LOCAL_PROFILE, endpoint="http://localhost:11434")
        subject = state(local_by_name)
        select_exact_model(subject, LOCAL_ID)
        handler = _provider_route_handler()

        bind_test_session(subject)
        with (
            patch.object(settings_adapter, "provider_settings_state", return_value=subject),
            patch(
                "comfyui_h3_context.adapters.prompt_model_transport.subprocess.run",
                side_effect=subprocess_module.TimeoutExpired(cmd="resolver", timeout=0.01),
            ) as runner,
        ):
            first = await handler(_provider_route_request("recheck_readiness"))
            second = await handler(_provider_route_request("recheck_readiness"))

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(runner.call_count, 2)
        self.assertIsNone(settings_adapter._registry().entry_for(TEST_SESSION).readiness_task)
        for response in (first, second):
            self.assertEqual(response.body["projection"]["readiness"], "not_configured")
            self.assertEqual(
                response.body["projection"]["diagnostic"]["outcome_id"],
                "prompt_model.timeout",
            )

    async def test_explicit_recheck_reaches_ready_through_the_registered_route(self) -> None:
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        handler = _provider_route_handler()
        calls = 0

        def probe(
            _state: object, _profile: object, _credential: object, **_options: object
        ) -> ReadinessObservation:
            nonlocal calls
            calls += 1
            return ready_observation(cast(PromptModelProfile, _profile))

        bind_test_session(subject)
        with (
            patch.object(settings_adapter, "provider_settings_state", return_value=subject),
            patch.object(settings_adapter, "_probe_provider_readiness", side_effect=probe),
        ):
            response = await handler(_provider_route_request("recheck_readiness"))

        self.assertEqual(response.status, 200)
        self.assertEqual(calls, 1)
        self.assertEqual(response.body["projection"]["readiness"], "ready")
        self.assertIs(response.body["projection"]["reachability_observed"], True)

    async def test_read_projection_never_calls_the_probe_through_the_route(self) -> None:
        subject = state()
        select_exact_model(subject, LOCAL_ID)
        handler = _provider_route_handler()
        bind_test_session(subject)
        with (
            patch.object(settings_adapter, "provider_settings_state", return_value=subject),
            patch.object(
                settings_adapter,
                "_probe_provider_readiness",
                side_effect=AssertionError("read attempted a probe"),
            ),
        ):
            response = await handler(_provider_route_request("read_projection"))

        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["projection"]["readiness"], "unreachable")
        self.assertIs(response.body["projection"]["reachability_observed"], False)


class QualifiedAuthorityTests(unittest.TestCase):
    """M22-13: a qualified row plus a reachable host is not, by itself, authority to run."""

    @staticmethod
    def _selected() -> tuple[ProviderSettingsState, PromptModelProfile]:
        qualified = qualified_profile(LOCAL_PROFILE)
        subject = ProviderSettingsState(profiles=(qualified,))
        select_exact_model(subject, qualified.profile_id)
        return subject, qualified

    def test_authorization_requires_the_exact_weights_to_have_been_observed(self) -> None:
        subject, qualified = self._selected()
        observation = ready_observation(qualified)
        assert observation.identity is not None

        # Same reachable, admitted, qualified state -- with and without the proof.
        subject.recheck(lambda _profile, _credential: replace(observation, identity=None))
        unproved = subject.project().assisted_authoring
        self.assertIs(subject.readiness(), ProviderReadiness.READY)
        self.assertTrue(unproved.selected)
        self.assertTrue(unproved.ready)
        self.assertFalse(unproved.authorized_for_this_action)

        subject.recheck(lambda _profile, _credential: observation)
        proved = subject.project().assisted_authoring
        self.assertTrue(proved.authorized_for_this_action)

    def test_the_execution_gate_enforces_what_the_surface_claims(self) -> None:
        """The projection is informational; `execution_decision` is what actually admits a run.

        `begin_assisted_execution` -- and therefore the product Optimize route -- never reads
        `authorized_for_this_action`. If only the projection required observed weights, the surface
        would refuse an action the backend would still happily perform.
        """

        subject, qualified = self._selected()
        observation = ready_observation(qualified)
        assert observation.identity is not None

        subject.recheck(lambda _profile, _credential: replace(observation, identity=None))
        self.assertIs(subject.readiness(), ProviderReadiness.READY)
        self.assertFalse(subject.project().assisted_authoring.authorized_for_this_action)
        unproved = subject.execution_decision()
        self.assertFalse(unproved.admitted)
        self.assertIsNone(unproved.snapshot)

        subject.recheck(lambda _profile, _credential: observation)
        self.assertTrue(subject.project().assisted_authoring.authorized_for_this_action)
        proved = subject.execution_decision()
        self.assertTrue(proved.admitted)
        assert proved.snapshot is not None
        self.assertEqual(proved.snapshot.profile.profile_id, qualified.profile_id)

    def test_authorization_lapses_the_moment_the_proof_does(self) -> None:
        subject, qualified = self._selected()
        subject.recheck(lambda _profile, _credential: ready_observation(qualified))
        self.assertTrue(subject.project().assisted_authoring.authorized_for_this_action)

        # A recheck with no probe is not fresh evidence, so it cannot leave authority standing.
        subject.recheck(None)
        self.assertFalse(subject.project().assisted_authoring.authorized_for_this_action)

    def test_a_catalog_only_row_is_never_authorized_however_ready_it_looks(self) -> None:
        subject = ProviderSettingsState(profiles=(LOCAL_PROFILE,))
        select_exact_model(subject, LOCAL_PROFILE.profile_id)
        subject.recheck(lambda _profile, _credential: ready_observation(LOCAL_PROFILE))
        state = subject.project().assisted_authoring
        self.assertIs(subject.readiness(), ProviderReadiness.READY)
        self.assertFalse(state.authorized_for_this_action)
        self.assertIsNone(subject.readiness_evidence)


if __name__ == "__main__":
    unittest.main()
