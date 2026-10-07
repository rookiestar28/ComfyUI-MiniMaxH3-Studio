"""Connection consent and server-owned model authority regressions."""

import asyncio
import json
from dataclasses import replace
from threading import Event
from unittest.mock import patch

import pytest
from test_provider_settings import REMOTE_ID, REMOTE_PROFILE, select, state

from comfyui_h3_context.adapters.comfyui_provider_settings import (
    ProviderSettingsRequestError,
    ProviderSettingsSessionError,
    ProviderSettingsSessionRegistry,
    decode_provider_settings_request,
    dispatch_provider_settings_async,
)
from comfyui_h3_context.adapters.provider_readiness import probe_provider_readiness
from comfyui_h3_context.core.prompt_model_provider import (
    ConnectionQualificationEvidence,
    LegacyPromptModelProfile,
    ModelChoice,
    ModelMetadata,
    PromptModelFamily,
    PromptModelOutcomeId,
    PromptModelQualificationState,
    load_prompt_model_catalog,
)
from comfyui_h3_context.core.prompt_model_session import DiscoveryCandidate, DiscoveryRejection
from comfyui_h3_context.core.provider_settings import (
    ProviderIntentRejection,
    ProviderReadiness,
    ProviderSettingsIntent,
    ProviderSettingsState,
    ReadinessObservation,
)

SESSION = "ps_" + "9" * 32
KEY = "test-credential-" + "R" * 40
MODEL = "arbitrary-text-model:latest"
DIGEST = "sha256:" + "b" * 64


def connection_state(*, qualified: bool = False) -> ProviderSettingsState:
    profiles = load_prompt_model_catalog().profiles
    if qualified:
        remote = next(row for row in profiles if row.profile_id == "openai.remote")
        evidence = ConnectionQualificationEvidence(
            profile_id=remote.profile_id,
            family=remote.family,
            observation_model_id="hermetic-observation",
            adapter_version=remote.adapter_version,
            parser_version=remote.parser_version,
            observed_on="2026-10-06",
            evidence_basis_sha256="sha256:" + "a" * 64,
            max_transmissions=4,
        )
        profiles = tuple(
            replace(
                row,
                qualification_state=PromptModelQualificationState.QUALIFIED,
                qualification_evidence=evidence,
            )
            if row is remote
            else row
            for row in profiles
        )
    return ProviderSettingsState(profiles=profiles)


def ownership(subject: ProviderSettingsState) -> dict[str, object]:
    return {"profile_id": subject.selected_profile_id, "expected_revision": subject.revision}


def listed(_profile: object, _credential: object) -> ReadinessObservation:
    return ReadinessObservation(
        reachable=True, candidates=(DiscoveryCandidate(MODEL, DiscoveryRejection.ADMITTED),)
    )


def connect(subject: ProviderSettingsState) -> None:
    select(subject, "openai.remote")
    result = subject.apply(
        ProviderSettingsIntent.CONNECT_AND_REFRESH,
        {
            **ownership(subject),
            "credential": KEY,
            "network_permitted": True,
            "media_upload_consented": False,
        },
        readiness_probe=listed,
    )
    assert result.accepted


def choose(subject: ProviderSettingsState) -> None:
    result = subject.apply(
        ProviderSettingsIntent.SELECT_MODEL, {**ownership(subject), "model_id": MODEL}
    )
    assert result.accepted


def test_model_selection_keeps_connection_consent() -> None:
    subject = state(REMOTE_PROFILE)
    select(subject, REMOTE_ID)
    subject.record_candidates((DiscoveryCandidate("model-a", DiscoveryRejection.ADMITTED),))
    granted = subject.apply(
        ProviderSettingsIntent.GRANT_CONSENT,
        {"network_permitted": True, "media_upload_consented": False},
    )
    assert granted.accepted
    selected = subject.apply(ProviderSettingsIntent.SELECT_MODEL, {"model_id": "model-a"})
    assert selected.accepted
    consent = selected.projection.consent
    assert consent is not None and consent.network_permitted and consent.status == "granted"


def test_reload_keeps_current_choice_and_consent_then_reports_a_missing_model() -> None:
    subject = connection_state()
    connect(subject)
    choose(subject)
    consent = subject.project().consent
    refreshed = subject.apply(
        ProviderSettingsIntent.CONNECT_AND_REFRESH,
        ownership(subject),
        readiness_probe=listed,
    )
    assert refreshed.accepted
    assert subject.model_choice is not None and subject.model_choice.model_id == MODEL
    assert subject.project().consent == consent
    missing = subject.apply(
        ProviderSettingsIntent.CONNECT_AND_REFRESH,
        ownership(subject),
        readiness_probe=lambda *_: ReadinessObservation(reachable=True, candidates=()),
    )
    assert missing.accepted and subject.model_choice is None
    assert subject.project().consent == consent
    assert subject.project().readiness is not ProviderReadiness.READY
    assert subject.project().diagnostic is not None
    assert subject.project().diagnostic.outcome_id == PromptModelOutcomeId.MODEL_MISSING.value


@pytest.mark.parametrize("profile_id", ["openai.remote", "ollama.local"])
def test_production_reload_reads_a_fresh_census_after_model_selection(profile_id: str) -> None:
    from comfyui_h3_context.adapters import provider_readiness as readiness_adapter

    async def scenario() -> None:
        subject = connection_state(qualified=True)
        select(subject, profile_id)
        registry = ProviderSettingsSessionRegistry(state_factory=lambda: subject)
        calls: list[tuple[str, str]] = []
        models = [MODEL]
        digest = [DIGEST]

        class Exchange:
            def request(
                self,
                method: str,
                path: str,
                payload: object = None,
                *,
                timeout_seconds: float | None = None,
            ) -> dict[str, object]:
                calls.append((method, path))
                if method == "POST":
                    assert payload == {"model": MODEL, "verbose": False}
                    return {
                        "capabilities": ["completion"],
                        "details": {"family": "testfamily"},
                        "model_info": {"testfamily.context_length": 32768},
                    }
                if profile_id == "ollama.local":
                    return {
                        "models": [
                            {"name": model, "digest": digest[0].removeprefix("sha256:")}
                            for model in models
                        ]
                    }
                return {"data": [{"id": model} for model in models]}

        payload = ownership(subject)
        if profile_id == "openai.remote":
            payload.update(credential=KEY, network_permitted=True, media_upload_consented=False)
        with (
            patch.object(
                readiness_adapter, "resolve_pinned_address_bounded", return_value="8.8.8.8"
            ),
            patch.object(
                readiness_adapter, "RemoteHttpsExchange", side_effect=lambda *_a, **_k: Exchange()
            ),
            patch.object(
                readiness_adapter, "LoopbackJsonExchange", side_effect=lambda *_a, **_k: Exchange()
            ),
        ):
            connected = await dispatch_provider_settings_async(
                SESSION, ProviderSettingsIntent.CONNECT_AND_REFRESH, payload, registry=registry
            )
            assert connected["accepted"]
            choose(subject)
            await dispatch_provider_settings_async(
                SESSION,
                ProviderSettingsIntent.RECHECK_READINESS,
                ownership(subject),
                registry=registry,
            )
            consent = subject.project().consent
            assert len([call for call in calls if call[0] == "GET"]) == 1
            models.append("new-model")
            refreshed = await dispatch_provider_settings_async(
                SESSION,
                ProviderSettingsIntent.CONNECT_AND_REFRESH,
                ownership(subject),
                registry=registry,
            )
            assert refreshed["accepted"]
            assert {row.identifier for row in subject.candidates} == {MODEL, "new-model"}
            assert subject.model_choice is not None and subject.model_choice.model_id == MODEL
            assert subject.project().consent == consent
            assert len([call for call in calls if call[0] == "GET"]) == 2
            expected_lists = 2
            if profile_id == "ollama.local":
                digest[0] = "sha256:" + "c" * 64
                await dispatch_provider_settings_async(
                    SESSION,
                    ProviderSettingsIntent.CONNECT_AND_REFRESH,
                    ownership(subject),
                    registry=registry,
                )
                assert (
                    subject.model_choice is not None and subject.model_choice.metadata is not None
                )
                assert subject.model_choice.metadata.model_digest == digest[0]
                expected_lists += 1
            models.remove(MODEL)
            await dispatch_provider_settings_async(
                SESSION,
                ProviderSettingsIntent.CONNECT_AND_REFRESH,
                ownership(subject),
                registry=registry,
            )
            assert subject.model_choice is None
            assert subject.project().readiness is not ProviderReadiness.READY
            assert subject.project().consent == consent
            expected_lists += 1
            assert len([call for call in calls if call[0] == "GET"]) == expected_lists
            models.clear()
            await dispatch_provider_settings_async(
                SESSION,
                ProviderSettingsIntent.CONNECT_AND_REFRESH,
                ownership(subject),
                registry=registry,
            )
            assert subject.candidates == () and subject.model_choice is None
            assert subject.project().consent == consent
            assert len([call for call in calls if call[0] == "GET"]) == expected_lists + 1
            registry.release(SESSION)

    asyncio.run(scenario())


def test_current_catalog_is_model_free_with_observed_connection_qualification() -> None:
    catalog = load_prompt_model_catalog()
    assert catalog.schema.endswith(".v6")
    assert set(catalog.profile_ids) == {
        "ollama.local",
        "openai.remote",
        "anthropic.remote",
        "gemini.remote",
    }
    for row in catalog.profiles:
        assert not isinstance(row, LegacyPromptModelProfile)
        assert (
            not {"model_id", "model_digest", "model_revision", "license_id", "license_source"}
            & row.to_wire().keys()
        )
        if row.profile_id == "gemini.remote":
            assert row.qualification_state is PromptModelQualificationState.QUALIFIED
            assert isinstance(row.qualification_evidence, ConnectionQualificationEvidence)
            assert row.qualification_evidence.observation_model_id == "gemini-3.8-flash"
            assert row.qualification_evidence.adapter_version == row.adapter_version
            assert row.qualification_evidence.parser_version == row.parser_version
            assert row.qualification_evidence.max_transmissions == 4
            assert "remote_activation_pending" not in row.limitations
        elif row.family is not PromptModelFamily.OLLAMA:
            assert row.qualification_evidence is None
            assert row.qualification_state is PromptModelQualificationState.CATALOG_ONLY
            assert "remote_activation_pending" in row.limitations


def test_arbitrary_current_model_preserves_consent_but_unqualified_connection_never_executes() -> (
    None
):
    subject = connection_state()
    connect(subject)
    before = subject.project().consent
    choose(subject)
    assert subject.project().consent == before
    assert subject.model_choice is not None and subject.model_choice.model_id == MODEL
    assert not subject.execution_decision().admitted
    subject.apply(ProviderSettingsIntent.CLEAR_MODEL)
    assert subject.project().consent == before
    assert subject.model_choice is None
    assert KEY not in json.dumps(subject.project().to_wire())


@pytest.mark.parametrize(
    "mutation",
    [
        ProviderSettingsIntent.REVOKE_CONSENT,
        ProviderSettingsIntent.DISCARD_CREDENTIAL,
        ProviderSettingsIntent.CLEAR_SELECTION,
        ProviderSettingsIntent.SELECT_PROFILE,
    ],
)
def test_connection_mutations_invalidate_model_and_consent(
    mutation: ProviderSettingsIntent,
) -> None:
    subject = connection_state()
    connect(subject)
    choose(subject)
    payload = (
        {"profile_id": "anthropic.remote"}
        if mutation is ProviderSettingsIntent.SELECT_PROFILE
        else {}
    )
    assert subject.apply(mutation, payload).accepted
    assert subject.model_choice is None
    assert not subject.execution_decision().admitted
    consent = subject.project().consent
    assert consent is None or not consent.network_permitted


def test_key_replacement_clears_old_model_and_observation_and_requires_new_explicit_grant() -> None:
    subject = connection_state()
    connect(subject)
    choose(subject)
    before = subject.project()
    rejected = subject.apply(
        ProviderSettingsIntent.CONNECT_AND_REFRESH,
        {**ownership(subject), "credential": "test-replacement-" + "K" * 40},
        readiness_probe=lambda *_args: pytest.fail("invalid request sent"),
    )
    assert rejected.rejection is ProviderIntentRejection.CONSENT_REQUIRED
    assert subject.project() == before
    changed = subject.apply(
        ProviderSettingsIntent.CONNECT_AND_REFRESH,
        {
            **ownership(subject),
            "credential": "test-replacement-" + "K" * 40,
            "network_permitted": True,
            "media_upload_consented": False,
        },
        readiness_probe=listed,
    )
    assert changed.accepted and subject.model_choice is None
    assert subject.readiness_evidence is None


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_revision": 0},
        {"profile_id": "anthropic.remote"},
        {"model_id": "absent"},
        {"model_id": "../hostile"},
        {"_legacy_request": True},
    ],
)
def test_invalid_model_ownership_does_not_mutate(changes: dict[str, object]) -> None:
    subject = connection_state()
    connect(subject)
    before = subject.project()
    result = subject.apply(
        ProviderSettingsIntent.SELECT_MODEL, {**ownership(subject), "model_id": MODEL, **changes}
    )
    assert not result.accepted and subject.project() == before


def test_duplicate_and_expired_census_cannot_create_or_keep_choice() -> None:
    subject = connection_state()
    connect(subject)
    candidate = DiscoveryCandidate(MODEL, DiscoveryRejection.ADMITTED)
    subject.record_candidates((candidate, candidate))
    assert not subject.apply(
        ProviderSettingsIntent.SELECT_MODEL, {**ownership(subject), "model_id": MODEL}
    ).accepted
    subject.record_candidates((candidate,))
    choose(subject)
    subject.record_candidates(())
    assert subject.model_choice is None and not subject.execution_decision().admitted


def test_local_setup_lists_once_then_selected_show_enriches_exact_choice() -> None:
    subject = connection_state()
    select(subject, "ollama.local")
    calls: list[tuple[str, str]] = []

    class Exchange:
        def request(
            self,
            method: str,
            path: str,
            payload: object = None,
            *,
            timeout_seconds: float | None = None,
        ) -> dict[str, object]:
            calls.append((method, path))
            if method == "GET":
                return {"models": [{"name": MODEL, "digest": DIGEST.removeprefix("sha256:")}]}
            assert payload == {"model": MODEL, "verbose": False}
            return {
                "capabilities": ["completion"],
                "details": {"family": "testfamily"},
                "model_info": {"testfamily.context_length": 32768},
            }

    def probe(profile: object, credential: object) -> ReadinessObservation:
        return probe_provider_readiness(
            profile,
            credential,
            model_choice=subject.model_choice,
            cached_candidates=subject.project().candidates,
            exchange_factory=lambda *_args: Exchange(),
        )

    assert subject.apply(
        ProviderSettingsIntent.CONNECT_AND_REFRESH, ownership(subject), readiness_probe=probe
    ).accepted
    choose(subject)
    assert subject.apply(
        ProviderSettingsIntent.RECHECK_READINESS, ownership(subject), readiness_probe=probe
    ).accepted
    assert calls == [("GET", "/api/tags"), ("POST", "/api/show")]
    assert subject.readiness() is ProviderReadiness.READY
    decision = subject.execution_decision()
    assert decision.admitted and decision.snapshot is not None
    assert decision.snapshot.model.metadata == ModelMetadata(
        model_digest=DIGEST,
        context_length=32768,
        capabilities=("completion",),
        locality="local",
        family="testfamily",
    )


@pytest.mark.parametrize("cloud_source", ["tags", "show"])
def test_native_cloud_routing_is_refused_before_metadata_reduction(cloud_source: str) -> None:
    subject = connection_state()
    select(subject, "ollama.local")
    profile = next(row for row in subject.profiles if row.profile_id == "ollama.local")

    class Exchange:
        def request(
            self,
            method: str,
            _path: str,
            _payload: object = None,
            *,
            timeout_seconds: float | None = None,
        ) -> dict[str, object]:
            if method == "GET":
                row: dict[str, object] = {"name": MODEL, "digest": DIGEST.removeprefix("sha256:")}
                if cloud_source == "tags":
                    row.update(remote_host="https://untrusted.invalid", digest=None)
                return {"models": [row]}
            return {"capabilities": ["completion"], "remote_model": "cloud-routing"}

    initial = probe_provider_readiness(profile, exchange_factory=lambda *_args: Exchange())
    assert KEY not in json.dumps([candidate.to_wire() for candidate in initial.candidates])
    if cloud_source == "tags":
        assert initial.candidates[0].reason is DiscoveryRejection.CLOUD_ROUTED
    else:
        candidate = initial.candidates[0]
        choice = ModelChoice(
            profile.profile_id, MODEL, "sha256:" + "c" * 64, 1.0, candidate.metadata
        )
        result = probe_provider_readiness(
            profile,
            model_choice=choice,
            cached_candidates=initial.candidates,
            exchange_factory=lambda *_args: Exchange(),
        )
        assert result.identity is None and result.outcome is not None


def test_v3_request_owns_revision_and_legacy_cannot_compose() -> None:
    def raw(schema: str, payload: dict[str, object]) -> bytes:
        return json.dumps(
            {"schema": schema, "intent": "connect_and_refresh", "payload": payload}
        ).encode()

    with pytest.raises(ProviderSettingsRequestError):
        decode_provider_settings_request(raw("h3.context.provider_settings.request.v2", {}))
    for payload in (
        {},
        {"profile_id": "openai.remote", "expected_revision": True},
        {"profile_id": "openai.remote", "expected_revision": 1, "selected_model": {}},
    ):
        with pytest.raises(ProviderSettingsRequestError):
            decode_provider_settings_request(
                raw("h3.context.provider_settings.request.v3", payload)
            )


@pytest.mark.parametrize("revision", [None, True, 1.0, 0, -1, 2_147_483_648])
def test_v3_invalid_revision_never_decodes_as_authority(revision: object) -> None:
    body = json.dumps(
        {
            "schema": "h3.context.provider_settings.request.v3",
            "intent": "select_model",
            "payload": {
                "profile_id": "openai.remote",
                "expected_revision": revision,
                "model_id": MODEL,
            },
        }
    ).encode()
    with pytest.raises(ProviderSettingsRequestError):
        decode_provider_settings_request(body)


def test_real_v3_remote_setup_reuses_the_current_census() -> None:
    from test_provider_settings import (
        TEST_SESSION,
        _provider_route_handler,
        _provider_route_request,
    )

    from comfyui_h3_context.adapters import comfyui_provider_settings as adapter

    async def scenario() -> None:
        subject = connection_state(qualified=True)
        registry = ProviderSettingsSessionRegistry(state_factory=lambda: subject)
        handler = _provider_route_handler()
        calls: list[tuple[str, str]] = []

        class Exchange:
            def request(
                self,
                method: str,
                path: str,
                _payload: object = None,
                *,
                timeout_seconds: float | None = None,
            ) -> dict[str, object]:
                calls.append((method, path))
                return {"data": [{"id": MODEL}]}

        def probe(
            current: ProviderSettingsState, selected: object, credential: object, **_options: object
        ) -> ReadinessObservation:
            return probe_provider_readiness(
                selected,
                credential,
                model_choice=current.model_choice,
                cached_candidates=current.candidates if current.model_choice else (),
                exchange_factory=lambda *_args: Exchange(),
            )

        with (
            patch.object(adapter, "_registry", return_value=registry),
            patch.object(adapter, "_probe_provider_readiness", side_effect=probe),
        ):
            selected = await handler(
                _provider_route_request("select_profile", payload={"profile_id": "openai.remote"})
            )
            assert selected.status == 200
            connected = await handler(
                _provider_route_request(
                    "connect_and_refresh",
                    payload={
                        **ownership(subject),
                        "credential": KEY,
                        "network_permitted": True,
                        "media_upload_consented": False,
                    },
                )
            )
            assert connected.status == 200 and connected.body["accepted"]
            consent = connected.body["projection"]["consent"]
            chosen = await handler(
                _provider_route_request(
                    "select_model", payload={**ownership(subject), "model_id": MODEL}
                )
            )
            assert chosen.status == 200 and chosen.body["projection"]["consent"] == consent
            ready = await handler(
                _provider_route_request("recheck_readiness", payload=ownership(subject))
            )
            assert ready.status == 200 and ready.body["projection"]["readiness"] == "ready"
            assert ready.body["projection"]["assisted_authoring"]["authorized_for_this_action"]
            decision = registry.begin_assisted_execution(TEST_SESSION)
            assert decision.lease is not None and decision.lease.snapshot.model.model_id == MODEL
            assert calls == [("GET", "/v1/models")]
            assert KEY not in json.dumps(ready.body)
            assert registry.finish_assisted_execution(decision.lease)

    asyncio.run(scenario())


def test_projection_wait_on_one_session_does_not_block_another_session() -> None:
    async def scenario() -> None:
        registry = ProviderSettingsSessionRegistry(state_factory=connection_state)
        subject = registry.state_for(SESSION)
        select(subject, "ollama.local")
        started, finish = Event(), Event()

        def slow(*_args: object) -> ReadinessObservation:
            started.set()
            assert finish.wait(3)
            return listed(None, None)

        first = asyncio.create_task(
            dispatch_provider_settings_async(
                SESSION,
                ProviderSettingsIntent.CONNECT_AND_REFRESH,
                ownership(subject),
                readiness_probe=slow,
                registry=registry,
            )
        )
        reader = None
        try:
            assert await asyncio.to_thread(started.wait, 1)
            reader = asyncio.create_task(
                dispatch_provider_settings_async(
                    SESSION, ProviderSettingsIntent.READ_PROJECTION, {}, registry=registry
                )
            )
            other = await asyncio.wait_for(
                dispatch_provider_settings_async(
                    "ps_" + "8" * 32, ProviderSettingsIntent.READ_PROJECTION, {}, registry=registry
                ),
                timeout=0.5,
            )
            assert other["accepted"] and not reader.done()
        finally:
            finish.set()
            await first
            if reader is not None:
                await reader

    asyncio.run(scenario())


def test_async_busy_setup_has_one_send_and_release_prevents_old_publication() -> None:
    async def scenario() -> None:
        subject = connection_state()
        select(subject, "openai.remote")
        registry = ProviderSettingsSessionRegistry(state_factory=lambda: subject)
        started, finish = Event(), Event()
        sends: list[str] = []

        def slow(profile: object, credential: object) -> ReadinessObservation:
            sends.append("list")
            started.set()
            assert finish.wait(3)
            return listed(profile, credential)

        payload = {
            **ownership(subject),
            "credential": KEY,
            "network_permitted": True,
            "media_upload_consented": False,
        }
        first = asyncio.create_task(
            dispatch_provider_settings_async(
                SESSION,
                ProviderSettingsIntent.CONNECT_AND_REFRESH,
                payload,
                readiness_probe=slow,
                registry=registry,
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 3)
            with pytest.raises(ProviderSettingsRequestError, match="request_busy"):
                await dispatch_provider_settings_async(
                    SESSION,
                    ProviderSettingsIntent.CONNECT_AND_REFRESH,
                    payload,
                    readiness_probe=slow,
                    registry=registry,
                )
            registry.release(SESSION)
        finally:
            finish.set()
        with pytest.raises(ProviderSettingsSessionError, match="session_released"):
            await first
        assert sends == ["list"]

    asyncio.run(scenario())


@pytest.mark.parametrize("drop_reason", ["release", "expiry", "eviction"])
def test_dropped_session_cannot_send_after_pending_dns_returns(drop_reason: str) -> None:
    from comfyui_h3_context.adapters import provider_readiness as readiness_adapter

    async def scenario() -> None:
        subject = connection_state()
        select(subject, "openai.remote")
        now = [0.0]
        registry = ProviderSettingsSessionRegistry(
            state_factory=lambda: subject,
            clock=lambda: now[0],
            idle_ttl_seconds=1,
            absolute_ttl_seconds=10,
            max_sessions=1,
        )
        started, resume, finished = Event(), Event(), Event()
        sends: list[str] = []

        def resolver(*_args: object, **_kwargs: object) -> str:
            started.set()
            assert resume.wait(3)
            return "8.8.8.8"

        class Exchange:
            def request(self, *_args: object, **_kwargs: object) -> dict[str, object]:
                sends.append("list")
                return {"data": [{"id": MODEL}]}

        def observed(*args: object, **kwargs: object) -> ReadinessObservation:
            try:
                return probe_provider_readiness(*args, **kwargs)  # type: ignore[arg-type]
            finally:
                finished.set()

        with (
            patch.object(readiness_adapter, "probe_provider_readiness", side_effect=observed),
            patch.object(readiness_adapter, "resolve_pinned_address_bounded", side_effect=resolver),
            patch.object(
                readiness_adapter, "RemoteHttpsExchange", side_effect=lambda *_a, **_k: Exchange()
            ),
        ):
            operation = asyncio.create_task(
                dispatch_provider_settings_async(
                    SESSION,
                    ProviderSettingsIntent.CONNECT_AND_REFRESH,
                    {
                        **ownership(subject),
                        "credential": KEY,
                        "network_permitted": True,
                        "media_upload_consented": False,
                    },
                    registry=registry,
                )
            )
            try:
                assert await asyncio.to_thread(started.wait, 3)
                if drop_reason == "release":
                    registry.release(SESSION)
                elif drop_reason == "expiry":
                    now[0] = 2.0
                    assert registry.session_count == 0
                else:
                    registry.entry_for("ps_" + "8" * 32)
                resume.set()
                with pytest.raises(ProviderSettingsSessionError, match="session_released"):
                    await operation
                assert await asyncio.to_thread(finished.wait, 3)
                assert sends == []
            finally:
                resume.set()

    asyncio.run(scenario())


def test_cancelled_local_listing_cannot_send_selected_model_show() -> None:
    subject = connection_state()
    profile = next(row for row in subject.profiles if row.profile_id == "ollama.local")
    choice = ModelChoice(
        profile.profile_id,
        MODEL,
        "sha256:" + "a" * 64,
        1.0,
        ModelMetadata(model_digest=DIGEST, locality="local"),
    )
    cancelled = Event()
    calls: list[str] = []

    class Exchange:
        def request(self, method: str, *_args: object, **_kwargs: object) -> dict[str, object]:
            calls.append(method)
            assert method == "GET"
            cancelled.set()
            return {"models": [{"name": MODEL, "digest": DIGEST.removeprefix("sha256:")}]}

    observed = probe_provider_readiness(
        profile,
        model_choice=choice,
        exchange_factory=lambda *_args: Exchange(),
        cancellation=cancelled.is_set,
    )
    assert calls == ["GET"]
    assert observed.identity is None and observed.outcome is not None
    assert observed.outcome.outcome_id is PromptModelOutcomeId.CANCELLED
