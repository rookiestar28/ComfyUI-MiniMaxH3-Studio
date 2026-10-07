"""M23-51 route contract: the two accepted `release_sequence` payloads, over the real coordinator.

The pure table says what each (state, intent) pair means and the service says that nothing else is
mutated. This suite says the request actually reaching the coordinator is decoded exactly, that the
legacy shape still works where it is still legitimate, and that the behaviour change is real: a
submitted run can no longer be dropped by the request that used to drop it.

Plan section 3.6 keeps V1 as backend surface with no live frontend caller, so its coverage lives
here rather than in a browser test.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from test_m23_15_sequence_coordinator import (
    _action,
    _complete_managed_run,
    _coordinator,
    _observation,
    _prepare,
    _production_workspace,
    _required_response,
    _submit,
)

from comfyui_h3_context.adapters.comfyui_sequence_coordinator import (
    COORDINATOR_TTL_SECONDS,
    RELEASE_REQUEST_V2_SCHEMA,
    ObservedVideoArtifact,
    SequenceCoordinatorError,
    SequenceCoordinatorRegistry,
    SequenceCoordinatorResponse,
)
from comfyui_h3_context.core.managed_run import ManagedRunState
from comfyui_h3_context.core.managed_run_release import (
    DETACHED_GRACE_SECONDS,
    terminal_fingerprint,
)


def _v2(run_handle: str, intent: str, state_fingerprint: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": RELEASE_REQUEST_V2_SCHEMA,
        "run_handle": run_handle,
        "intent": intent,
        "expected_state_fingerprint": state_fingerprint,
    }
    payload.update(extra)
    return payload


class _MovableClock:
    """A clock the test advances, so a TTL can actually elapse."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


#: What the frontend really asks for, in `frontend/src/host/appModeManagedPreparation.ts`. The
#: shared `_observation()` helper says 60_000, which makes a detached run's lease *shorter* than the
#: coordinator's own 900-second row and hides the addressability gap entirely.
SHIPPED_TIMEOUT_MS = 3_600_000


def _long_timeout_run(
    tmp_path: Path, clock: _MovableClock
) -> tuple[SequenceCoordinatorRegistry, SequenceCoordinatorResponse, SequenceCoordinatorResponse]:
    """A prepared and submitted run under the timeout the product actually sends."""

    production_registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=lambda: output_root,
        private_root_factory=lambda: tmp_path / "private",
        artifact_inspector=lambda payload, **expected: ObservedVideoArtifact(
            "mp4", (192, 512, 512, 3)
        ),
        clock=clock,
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
    )
    observation = {**_observation(), "timeout_ms": SHIPPED_TIMEOUT_MS}
    prepared = _required_response(
        coordinator.dispatch(
            _action(
                "m23_51.longrun.prepare",
                "prepare_sequence",
                {
                    "workspace_handle": production.workspace_handle,
                    "expected_workspace_revision": production.workspace_revision,
                    "expected_workspace_fingerprint": production.workspace_fingerprint,
                    "correlation": {
                        "prompt_id": "prompt.bootstrap",
                        "execution_node_id": "node.product.shell",
                    },
                    "observation": observation,
                },
            )
        )
    )
    return coordinator, prepared, _submit(coordinator, prepared)


def _submitted_run(
    tmp_path: Path,
) -> tuple[SequenceCoordinatorRegistry, SequenceCoordinatorResponse, SequenceCoordinatorResponse]:
    registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    return coordinator, prepared, submitted


def test_v1_still_releases_a_prepared_unsubmitted_child(tmp_path: Path) -> None:
    registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)

    response = _required_response(
        coordinator.dispatch(
            _action("m23_51.v1.prepared", "release_sequence", {"run_handle": prepared.run_handle})
        )
    )
    assert response.disposition == "released"
    assert prepared.run_handle not in coordinator._runs


def test_v1_no_longer_drops_a_run_the_host_may_be_executing(tmp_path: Path) -> None:
    # The defect, asserted as a refusal. Before M23-51 this returned `released` and destroyed the
    # only authority able to accept the host's result.
    coordinator, prepared, _submitted = _submitted_run(tmp_path)

    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(
            _action("m23_51.v1.submitted", "release_sequence", {"run_handle": prepared.run_handle})
        )
    assert (refused.value.status, refused.value.code) == (409, "release_host_owned")
    assert prepared.run_handle in coordinator._runs
    assert coordinator._managed.read(prepared.run_handle).state is ManagedRunState.SUBMITTED


def test_a_v1_payload_with_any_extra_member_is_refused(tmp_path: Path) -> None:
    # CRITICAL: V1 is recognised by being *exactly* `{run_handle}`. A payload that adds a member is
    # a V2 client talking to a route that would otherwise silently treat it as the legacy cleanup
    # intent -- which is the destructive one. It must fall through to the V2 decoder and be
    # rejected there for its missing schema, never be accepted as V1.
    coordinator, prepared, _submitted = _submitted_run(tmp_path)

    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(
            _action(
                "m23_51.v1.extra",
                "release_sequence",
                {"run_handle": prepared.run_handle, "intent": "detach_client"},
            )
        )
    assert (refused.value.status, refused.value.code) == (400, "unsupported_release_schema")


def test_a_detach_retains_the_run_and_a_late_terminal_still_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry, coordinator, submitted, completed, _receipt, _payload = _complete_managed_run(
        tmp_path, monkeypatch
    )
    handle = submitted.run_handle

    # Detach while the run is still submitted is covered below; here the run has already settled,
    # so the detach reports which kind it was and keeps the result addressable.
    response = _required_response(
        coordinator.dispatch(
            _action(
                "m23_51.v2.detach_terminal",
                "release_sequence",
                _v2(handle, "detach_client", completed.sequence.state.fingerprint),
            )
        )
    )
    assert response.disposition == "detached_terminal"
    assert handle in coordinator._runs
    assert coordinator._managed.read(handle).state is ManagedRunState.TERMINAL_SUCCEEDED


def test_detaching_a_submitted_run_keeps_it_and_reports_detached(tmp_path: Path) -> None:
    coordinator, prepared, submitted = _submitted_run(tmp_path)

    response = _required_response(
        coordinator.dispatch(
            _action(
                "m23_51.v2.detach",
                "release_sequence",
                _v2(prepared.run_handle, "detach_client", submitted.sequence.state.fingerprint),
            )
        )
    )
    assert response.disposition == "detached"
    assert prepared.run_handle in coordinator._runs
    run = coordinator._managed.read(prepared.run_handle)
    assert run.state is ManagedRunState.SUBMITTED
    assert run.prompt_id is not None


def test_a_terminal_is_removed_only_when_the_caller_proves_it_saw_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry, coordinator, submitted, completed, _receipt, _payload = _complete_managed_run(
        tmp_path, monkeypatch
    )
    handle = submitted.run_handle
    fingerprint = completed.sequence.state.fingerprint

    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(
            _action(
                "m23_51.v2.terminal_bad_proof",
                "release_sequence",
                _v2(
                    handle,
                    "cleanup_terminal",
                    fingerprint,
                    observed_terminal_fingerprint=f"sha256:{'0' * 64}",
                ),
            )
        )
    assert (refused.value.status, refused.value.code) == (409, "release_wrong_intent")
    assert handle in coordinator._runs

    proof = terminal_fingerprint(coordinator._managed.read(handle))
    assert proof is not None
    response = _required_response(
        coordinator.dispatch(
            _action(
                "m23_51.v2.terminal_proved",
                "release_sequence",
                _v2(
                    handle,
                    "cleanup_terminal",
                    fingerprint,
                    observed_terminal_fingerprint=proof,
                ),
            )
        )
    )
    assert response.disposition == "released"
    assert handle not in coordinator._runs


def test_a_stale_expected_state_fingerprint_refuses_before_any_mutation(tmp_path: Path) -> None:
    coordinator, prepared, _submitted = _submitted_run(tmp_path)

    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(
            _action(
                "m23_51.v2.stale",
                "release_sequence",
                _v2(prepared.run_handle, "detach_client", f"sha256:{'1' * 64}"),
            )
        )
    assert (refused.value.status, refused.value.code) == (409, "stale_sequence_state")
    assert coordinator._managed.read(prepared.run_handle).state is ManagedRunState.SUBMITTED


@pytest.mark.parametrize(
    ("label", "mutate", "code"),
    (
        (
            "unknown_schema",
            lambda p: {**p, "schema": "h3.context.something.v9"},
            "unsupported_release_schema",
        ),
        (
            "unknown_intent",
            lambda p: {**p, "intent": "cancel_everything"},
            "invalid_release_intent",
        ),
        ("extra_member", lambda p: {**p, "surprise": 1}, "invalid_release_payload"),
        (
            "proof_on_a_detach",
            lambda p: {**p, "observed_terminal_fingerprint": f"sha256:{'2' * 64}"},
            "invalid_release_payload",
        ),
        (
            "missing_intent",
            lambda p: {k: v for k, v in p.items() if k != "intent"},
            "invalid_release_intent",
        ),
    ),
)
def test_a_malformed_v2_payload_fails_closed(
    tmp_path: Path,
    label: str,
    mutate: Callable[[dict[str, object]], dict[str, object]],
    code: str,
) -> None:
    # CRITICAL: every one of these must be refused *before* the run is read, so a malformed request
    # can never be the reason a run changed. `missing_intent` leaves a shape that is neither V1 nor
    # a decodable V2 -- it must be named as an undecodable intent, never fall back to the
    # destructive default that V1 carries implicitly.
    coordinator, prepared, submitted = _submitted_run(tmp_path)
    payload = _v2(prepared.run_handle, "detach_client", submitted.sequence.state.fingerprint)

    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(_action(f"m23_51.v2.bad.{label}", "release_sequence", mutate(payload)))
    assert (refused.value.status, refused.value.code) == (400, code)
    assert coordinator._managed.read(prepared.run_handle).state is ManagedRunState.SUBMITTED


def test_a_terminal_cleanup_without_its_proof_member_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry, coordinator, submitted, completed, _receipt, _payload = _complete_managed_run(
        tmp_path, monkeypatch
    )
    handle = submitted.run_handle

    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(
            _action(
                "m23_51.v2.terminal_no_proof",
                "release_sequence",
                _v2(handle, "cleanup_terminal", completed.sequence.state.fingerprint),
            )
        )
    assert (refused.value.status, refused.value.code) == (400, "invalid_release_payload")
    assert handle in coordinator._runs


def test_replaying_the_same_request_returns_the_same_response(tmp_path: Path) -> None:
    coordinator, prepared, submitted = _submitted_run(tmp_path)
    payload = _v2(prepared.run_handle, "detach_client", submitted.sequence.state.fingerprint)

    first = _required_response(
        coordinator.dispatch(_action("m23_51.v2.replay", "release_sequence", payload))
    )
    second = _required_response(
        coordinator.dispatch(_action("m23_51.v2.replay", "release_sequence", payload))
    )
    assert first.to_wire() == second.to_wire()
    assert first.disposition == "detached"


def test_reusing_a_request_id_for_another_intent_is_a_conflict(tmp_path: Path) -> None:
    # CRITICAL: the replay ledger is keyed by request id and guarded by the request digest, so a
    # client that reuses an id with a different intent must get a conflict rather than the earlier
    # response. Returning the cached `detached` to a `cleanup_terminal` would tell a caller its
    # removal succeeded when nothing was removed.
    coordinator, prepared, submitted = _submitted_run(tmp_path)
    fingerprint = submitted.sequence.state.fingerprint

    _required_response(
        coordinator.dispatch(
            _action(
                "m23_51.v2.conflict",
                "release_sequence",
                _v2(prepared.run_handle, "detach_client", fingerprint),
            )
        )
    )
    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(
            _action(
                "m23_51.v2.conflict",
                "release_sequence",
                _v2(prepared.run_handle, "cleanup_pre_submit", fingerprint),
            )
        )
    assert (refused.value.status, refused.value.code) == (409, "request_id_conflict")


def test_a_detached_run_stays_addressable_for_its_whole_lease(tmp_path: Path) -> None:
    # CRITICAL: the coordinator's own row is the only way to address a run, and its TTL is refreshed
    # only by `_publish`, which never happens again once the client has gone. The ManagedRun lease
    # is routinely longer than that TTL -- about 65 minutes against a 900-second row, under the
    # timeout the product really sends -- so without the retention probe a returning client is told
    # `run_gone` about a run that exists, still holds one of the sixteen live slots, and still has
    # its result.
    clock = _MovableClock()
    coordinator, prepared, submitted = _long_timeout_run(tmp_path, clock)
    handle = prepared.run_handle

    detached = _required_response(
        coordinator.dispatch(
            _action(
                "m23_51.addressable.detach",
                "release_sequence",
                _v2(handle, "detach_client", submitted.sequence.state.fingerprint),
            )
        )
    )
    assert detached.disposition == "detached"

    clock.now += COORDINATOR_TTL_SECONDS * 2
    read = _required_response(
        coordinator.dispatch(
            _action("m23_51.addressable.read", "read_managed_run", {"run_handle": handle})
        )
    )
    assert read.disposition == "current"
    assert coordinator._managed.read(handle).prompt_id is not None


def test_an_expired_lease_lets_both_records_go(tmp_path: Path) -> None:
    # The other half: retention must end. Past the lease, both records drop the run and the client
    # is told `gone` -- which is then true.
    clock = _MovableClock()
    coordinator, prepared, submitted = _long_timeout_run(tmp_path, clock)
    handle = prepared.run_handle
    coordinator.dispatch(
        _action(
            "m23_51.expiring.detach",
            "release_sequence",
            _v2(handle, "detach_client", submitted.sequence.state.fingerprint),
        )
    )

    clock.now += SHIPPED_TIMEOUT_MS / 1000.0 + DETACHED_GRACE_SECONDS + 1.0
    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(
            _action("m23_51.expiring.read", "read_managed_run", {"run_handle": handle})
        )
    assert refused.value.status == 410


def test_every_input_to_the_terminal_proof_is_a_value_an_observer_can_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CRITICAL: `cleanup_terminal` is the one intent whose success the caller has to earn, and it
    # earns it by proving it observed the terminal projection. That is only possible if every input
    # to the digest is a value the projection publishes. The aggregate therefore stores the
    # *published* receipt fingerprint; storing `artifact_id` -- an internal store identifier no
    # client ever sees -- would leave the intent with no reachable success path while still looking
    # implemented, and would put a storage identifier into every retained row.
    _registry, coordinator, submitted, completed, _receipt, _payload = _complete_managed_run(
        tmp_path, monkeypatch
    )
    run = coordinator._managed.read(submitted.run_handle)
    progress = completed.sequence.progress[0]

    assert run.artifact_receipt == progress.artifact_receipt_fingerprint
    assert run.prompt_id == progress.queue_prompt_id
    assert run.run_handle == completed.run_handle
    # And the digest really is built from those, so an observer holding the projection plus the
    # terminal category can reconstruct it.
    assert terminal_fingerprint(run) is not None


def test_an_attached_run_still_expires_on_the_coordinator_ttl(tmp_path: Path) -> None:
    # The probe must not become a blanket exemption: a run nobody detached keeps the old rule.
    clock = _MovableClock()
    coordinator, prepared, _submitted_response = _long_timeout_run(tmp_path, clock)
    clock.now += COORDINATOR_TTL_SECONDS + 1.0
    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(
            _action("m23_51.attached.read", "read_managed_run", {"run_handle": prepared.run_handle})
        )
    assert refused.value.status == 410
