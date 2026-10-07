"""M23-31: the cross-registry invariants the four registries could not hold between them.

`release_workspace` used to remove a production workspace unconditionally. Nothing asked whether a
generation sequence still depended on it, because nothing could: the production registry has no run
identity, and the coordinator that does was never consulted. A release under a live run stranded the
sequence -- the M23-19 class of defect, one join over.

The aggregate now answers the question and the production registry enforces the answer, so the rule
`comfyui_h3_context.core.managed_run.NO_LIVE_SEQUENCE_GUARD` names has exactly one definition.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from test_m23_15_sequence_coordinator import (
    _action,
    _coordinator,
    _prepare,
    _production_workspace,
    _required_response,
)

from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.core.managed_run import (
    MANAGED_RUN_MACHINE,
    MODEL_ONLY_TRIGGERS,
    GuardRejectedError,
    InvalidTriggerError,
    ManagedRun,
    ManagedRunState,
    ManagedRunTrigger,
    advance,
    holds_live_sequence,
)
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection


def _release(
    registry: ProductionWorkspaceRegistry,
    projection: ProductionWorkbenchProjection,
    request_id: str,
) -> object:
    return registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": request_id,
            "action": "release_workspace",
            "payload": {
                "workspace_handle": projection.workspace_handle,
                "expected_workspace_revision": projection.workspace_revision,
                "expected_workspace_fingerprint": projection.workspace_fingerprint,
            },
        }
    )


def test_a_workspace_cannot_be_released_while_a_sequence_depends_on_it(tmp_path: Path) -> None:
    registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)
    assert coordinator._managed.read(prepared.run_handle).prepared_sequence is not None

    current = coordinator._managed.read(prepared.run_handle)
    assert current.production_workspace == production.workspace_handle

    with pytest.raises(ProductionWorkbenchError) as refused:
        _release(registry, prepared.production, "m23_31.release.live")
    assert (refused.value.status, refused.value.code) == (409, "workspace_sequence_live")


def test_a_workspace_is_released_once_its_run_has_been_released(tmp_path: Path) -> None:
    # The other half: the guard must not become a workspace that can never be released. Releasing
    # the sequence releases the aggregate, and the workspace is then free.
    #
    # M23-51: this case used to submit the run first and then release it with the legacy V1
    # payload, which is exactly the unconditional removal of a host-owned prompt that item
    # removed. It now exercises the release that remained legitimate -- a prepared, unsubmitted
    # child -- and the submitted case has its own coverage in
    # `tests/test_m23_51_release_route_contract.py`, where the refusal and the terminal-proof route
    # out are both asserted.
    registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)

    released_run = _required_response(
        coordinator.dispatch(
            _action(
                "m23_31.release.sequence",
                "release_sequence",
                {"run_handle": prepared.run_handle},
            )
        )
    )
    assert released_run.disposition == "released"

    result = _release(registry, prepared.production, "m23_31.release.free")
    assert result.status == 204  # type: ignore[attr-defined]


def test_binding_a_second_liveness_authority_is_refused(tmp_path: Path) -> None:
    # Two probes would mean two authorities, which is the thing this item removes. The coordinator
    # binds one at construction; a second coordinator over the same production registry is a
    # configuration defect, not a supported topology.
    registry, _ = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    _coordinator(registry, output_root, tmp_path / "private")
    with pytest.raises(ProductionWorkbenchError) as refused:
        registry.bind_live_sequence_probe(lambda handle: False)
    assert refused.value.code == "live_sequence_probe_bound"


def test_the_release_guard_is_exactly_the_shared_predicate() -> None:
    # One rule, two callers. Asserted behaviourally over every state a production release is
    # accepted from and both values of the rule: the machine refuses the release exactly when
    # `holds_live_sequence` says a sequence still depends on the workspace. Written this way rather
    # than by reading the guard's source so that inlining the predicate back into either site --
    # the way it was written twice before -- fails here instead of drifting silently.
    sources = {
        state
        for transition in MANAGED_RUN_MACHINE.transitions
        if transition.trigger == ManagedRunTrigger.RELEASE_PRODUCTION.value
        for state in transition.source
    }
    assert sources, "release_production has no source states"
    for state in sorted(sources):
        for prepared in (None, "run_seq_1"):
            run = ManagedRun(
                run_handle="run_1",
                state=ManagedRunState(state),
                production_workspace="pw_1",
                prepared_sequence=prepared,
                segment_count=1,
            )
            refused = False
            try:
                advance(run, ManagedRunTrigger.RELEASE_PRODUCTION)
            except GuardRejectedError:
                refused = True
            except InvalidTriggerError:  # pragma: no cover - sources are taken from the table
                raise AssertionError(
                    f"{state} is a declared source but refused the trigger"
                ) from None
            assert refused == holds_live_sequence(run), (
                f"the guard and the predicate disagree at {state} with prepared={prepared!r}"
            )


def test_the_release_probe_is_the_same_predicate(tmp_path: Path) -> None:
    # The other caller. The probe the production registry consults reports a workspace live for
    # exactly the runs the predicate calls live, so the 409 the client sees and the refusal the
    # machine would make are one decision.
    registry, production = _production_workspace()
    output_root = tmp_path / "output"
    output_root.mkdir()
    coordinator = _coordinator(registry, output_root, tmp_path / "private")
    prepared = _prepare(coordinator, production)

    run = coordinator._managed.read(prepared.run_handle)
    assert holds_live_sequence(run) is True
    assert coordinator._workspace_has_live_run(production.workspace_handle) is True

    # A workspace no run owns is not live, which is the other half of the same predicate.
    assert coordinator._workspace_has_live_run("pw_" + "0" * 32) is False


def test_a_cold_start_race_binds_the_liveness_authority_exactly_once() -> None:
    # Route handlers run on `asyncio.to_thread` worker threads, so several first requests can reach
    # the lazy coordinator singleton at once. Since this item its constructor binds the process's
    # live-sequence authority onto the shared production registry, and a second binding is refused,
    # so an unsynchronised construction answers one of those requests with a 500.
    #
    # The assertion is that the authority is bound exactly once, not merely that one object comes
    # back: without the lock every thread past the `None` check builds its own coordinator and
    # binds, and the losers raise. The window such a race needs is microseconds wide, so it is
    # widened here -- by holding the first binder inside the call, on the registry instance rather
    # than in the code under test. The coordinator type-checks its registry exactly, so this
    # replaces the bound method on a real instance instead of subclassing it.
    from comfyui_h3_context.adapters import comfyui_sequence_coordinator as coordinator_module
    from comfyui_h3_context.adapters import composition_root

    saved_registry = composition_root.installed(composition_root.PRODUCTION_WORKSPACE)
    saved_coordinator = composition_root.installed(composition_root.SEQUENCE_COORDINATOR)
    # A real, empty registry of the exact type the coordinator requires. Built through the M23-15
    # helper so this fixture cannot drift from the one the rest of the suite uses.
    registry, _unused = _production_workspace()
    entered = threading.Event()
    proceed = threading.Event()
    counter_lock = threading.Lock()
    bind_calls = 0
    real_bind = registry.bind_live_sequence_probe

    def blocking_bind(
        probe: Callable[[str], bool],
        *,
        run_probe: Callable[[str], bool] | None = None,
    ) -> None:
        nonlocal bind_calls
        with counter_lock:
            bind_calls += 1
        entered.set()
        proceed.wait(timeout=5)
        real_bind(probe, run_probe=run_probe)

    registry.bind_live_sequence_probe = blocking_bind  # type: ignore[method-assign]

    followers = 7
    results: list[object] = []
    failures: list[BaseException] = []
    guard = threading.Lock()

    def enter() -> None:
        try:
            coordinator = coordinator_module._coordinator()
        except BaseException as exc:  # noqa: BLE001 - the assertion is that none of these happen
            with guard:
                failures.append(exc)
            return
        with guard:
            results.append(coordinator)

    try:
        composition_root.install(composition_root.PRODUCTION_WORKSPACE, registry)
        composition_root.reset(composition_root.SEQUENCE_COORDINATOR)

        first = threading.Thread(target=enter)
        first.start()
        assert entered.wait(timeout=5), "the first constructor never reached the binding"

        rest = [threading.Thread(target=enter) for _ in range(followers)]
        for worker in rest:
            worker.start()
        # A bounded window for the followers to reach the singleton. With the lock they block on
        # it, which is what makes `bind_calls` stay at one; without it they construct here. The
        # wait always ends, so the test cannot hang and cannot flake red.
        threading.Event().wait(0.25)
        proceed.set()

        for worker in (first, *rest):
            worker.join(timeout=15)

        assert not failures, f"a concurrent first request failed: {failures[0]!r}"
        assert bind_calls == 1, (
            f"the liveness authority was bound {bind_calls} times; the lazy coordinator singleton "
            "is not constructed under its lock"
        )
        assert len(results) == followers + 1
        assert len({id(item) for item in results}) == 1
    finally:
        proceed.set()
        for name, instance in (
            (composition_root.SEQUENCE_COORDINATOR, saved_coordinator),
            (composition_root.PRODUCTION_WORKSPACE, saved_registry),
        ):
            if instance is None:
                composition_root.reset(name)
            else:
                composition_root.install(name, instance)


def test_the_model_only_register_matches_what_the_adapters_actually_fire() -> None:
    # A shipped stategraph that advertises a lifecycle edge no production path takes is a contract
    # promising something the system does not do. Rather than delete those edges -- they state the
    # rule that is intended, and the gap is registered roadmap work -- the exceptions are named, and
    # this asserts the register is exactly the set of triggers the adapters never reference. It
    # fails in both directions: wiring a model-only trigger without updating the register, and
    # letting a live trigger quietly become dead.
    package = Path(__file__).resolve().parents[1] / "comfyui_h3_context"
    referenced: set[str] = set()
    for path in sorted(package.rglob("*.py")):
        if path.name in {"managed_run.py", "managed_run_machine.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        for trigger in ManagedRunTrigger:
            if f"ManagedRunTrigger.{trigger.name}" in text:
                referenced.add(trigger.name)

    unreferenced = {trigger for trigger in ManagedRunTrigger if trigger.name not in referenced}
    assert unreferenced == set(MODEL_ONLY_TRIGGERS), (
        "triggers no adapter fires: "
        f"{sorted(item.value for item in unreferenced)}; "
        f"registered as model-only: {sorted(item.value for item in MODEL_ONLY_TRIGGERS)}"
    )


def test_every_model_only_trigger_states_why_it_is_modelled() -> None:
    for trigger, reason in MODEL_ONLY_TRIGGERS.items():
        assert trigger in set(ManagedRunTrigger)
        assert len(reason) >= 80, f"{trigger.value} has no substantive reason recorded"
