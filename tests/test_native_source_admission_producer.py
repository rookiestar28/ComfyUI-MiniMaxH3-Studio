"""The real managed qualification producer admits structurally equal loaded sources only.

These drive the actual Production import, canonical materializer and producer with a model-free
host and loaded-source observation, so each hold, publication and cleanup is the product's own.
"""

import logging
import shutil
from pathlib import Path
from typing import Any

import pytest
from native_source_doubles import EQUIVALENT_BLOB, composition_observation, observed_source
from test_m26_03_planning_service import action, environment, prepared_proposal, selectors
from test_m26_04_qualification_producer import _install_loader_inventory

from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters import managed_mode_qualification as producer_module

_LOGGER = "comfyui_h3_context.adapters.managed_mode_qualification"
_CHANGED = "definition:MiniMaxH3ImageToVideo.execute"


def _ready_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: Any,
    maximum_segments: bool = False,
) -> tuple[Any, ...]:
    from test_generation_profile import observation

    from comfyui_h3_context.adapters import comfyui_generation_profile
    from comfyui_h3_context.adapters import production_planning_service as planning
    from comfyui_h3_context.core.production_duration import ProductionDurationIntentV1
    from comfyui_h3_context.core.production_storyboard import StoryboardShotV1

    _service, sidebar, production, _source, prepare, clock = environment()
    reviewed_rows = None
    if maximum_segments:
        # Only the intent's segment bounds are narrowed (4 s), so 60 s plans as the contract's
        # 15-segment maximum; import, materialization and qualification stay the product's own.
        # The cut evidence is fifteen reviewed rows: one single-clip Context is not a storyboard
        # for a 60 s target, so the canonical source is refused for it.
        reviewed_rows = tuple(
            StoryboardShotV1(
                f"reviewed_{ordinal}",
                ordinal,
                (ordinal - 1) * 4_000,
                ordinal * 4_000,
                f"The sphere holds position {ordinal}.",
            )
            for ordinal in range(1, 16)
        )
        monkeypatch.setattr(
            planning,
            "ProductionDurationIntentV1",
            lambda **fields: ProductionDurationIntentV1(
                **fields, segment_min_seconds=4, segment_max_seconds=4
            ),
        )
        prepare["target_seconds"] = 60
        prepare["policy"] = "auto_storyboard"
    monkeypatch.setattr(
        composition_root,
        "_INSTANCES",
        {
            composition_root.SIDEBAR_WORKSPACE: sidebar,
            composition_root.PRODUCTION_WORKSPACE: production,
        },
    )
    service = composition_root.get(composition_root.PRODUCTION_PLANNING)
    monkeypatch.setattr(service, "_clock", lambda: clock[0])
    # facts[0] is the observed host, facts[1] the loaded native source observation.
    facts: list[Any] = [observation(), source]
    calls = [0]

    def observe() -> Any:
        calls[0] += 1
        return composition_observation(facts[0], facts[1])

    _install_loader_inventory(monkeypatch)
    monkeypatch.setattr(comfyui_generation_profile, "observe_host", lambda *_args: facts[0])
    monkeypatch.setattr(comfyui_generation_profile, "_observe_native_composition", observe)
    _, proposed = prepared_proposal(service, prepare, reviewed_rows=reviewed_rows)
    imported = service.dispatch(
        action(
            "import_plan",
            {**selectors(proposed), "proposal_id": proposed["proposal"]["proposal_id"]},
            "import.readiness",
        )
    ).to_wire()
    registry = composition_root.get(composition_root.MANAGED_MODE_QUALIFICATION)
    monkeypatch.setattr(registry, "_clock", lambda: clock[0])
    monkeypatch.setattr(registry._producer, "_clock", lambda: clock[0])
    selection = {
        "workspace_handle": prepare["workspace_handle"],
        "expected_workspace_revision": imported["workspace_revision"],
        "expected_workspace_fingerprint": imported["workspace_fingerprint"],
        "expected_plan_fingerprint": imported["plan_fingerprint"],
    }
    return (
        service,
        registry,
        selection,
        clock,
        facts,
        sidebar,
        calls,
        len(proposed["proposal"]["segments"]),
    )


def _prepare(service: Any, selection: dict[str, Any], request_id: str) -> dict[str, Any]:
    return dict(
        service.dispatch(action("prepare_managed_readiness", selection, request_id)).to_wire()
    )


def test_structurally_equal_loaded_source_reaches_ready(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    service, registry, selection, _clock, _facts, sidebar, _calls, _count = _ready_environment(
        monkeypatch, source=observed_source(EQUIVALENT_BLOB)
    )
    before = set(sidebar._entries)
    prepared = _prepare(service, selection, "prepare.structural")
    assert prepared["status"] == "ready" and prepared["reason"] == "qualified"
    assert registry._current is not None
    assert set(sidebar._entries) == before
    issued = [record.getMessage() for record in caplog.records]
    assert issued == [
        "H3 Context managed qualification issued: segments=2 "
        f"source={EQUIVALENT_BLOB} detail=structural_receipt"
    ]
    assert "registered-module" not in str(prepared)


def test_changed_structure_holds_with_the_closed_public_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    service, registry, selection, _clock, _facts, sidebar, _calls, _count = _ready_environment(
        monkeypatch, source=observed_source(EQUIVALENT_BLOB, check=_CHANGED)
    )
    before = set(sidebar._entries)
    held = _prepare(service, selection, "prepare.changed")
    assert (held["status"], held["reason"]) == ("held", "qualification_composition_unqualified")
    assert _CHANGED not in str(held) and "native_t2va" not in str(held)
    assert registry._current is None and set(sidebar._entries) == before
    assert [record.getMessage() for record in caplog.records] == [
        "H3 Context managed qualification held: "
        f"composition=native_t2va_structure_changed detail={_CHANGED}"
    ]


def test_unavailable_source_and_observer_failure_keep_distinct_closed_paths(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from comfyui_h3_context.adapters import comfyui_generation_profile

    caplog.set_level(logging.INFO, logger=_LOGGER)
    service, registry, selection, _clock, _facts, sidebar, _calls, _count = _ready_environment(
        monkeypatch, source=None
    )
    before = set(sidebar._entries)
    held = _prepare(service, selection, "prepare.unavailable")
    assert held["reason"] == "qualification_composition_unqualified"
    assert [record.getMessage() for record in caplog.records] == [
        "H3 Context managed qualification held: "
        "composition=native_source_identity_mismatch detail=source_unavailable"
    ]

    def failing() -> Any:
        raise RuntimeError("private detail A:/host/path/nodes_minimax_h3.py")

    monkeypatch.setattr(comfyui_generation_profile, "_observe_native_composition", failing)
    failed = _prepare(service, selection, "prepare.failed")
    assert failed["reason"] == "qualification_observation_failed"
    assert "private" not in str(failed) and "nodes_minimax_h3" not in str(failed)
    assert all("private" not in record.getMessage() for record in caplog.records)
    assert registry._current is None and set(sidebar._entries) == before


def test_host_hold_precedes_a_changed_source(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_generation_profile import observation

    service, _registry, selection, _clock, facts, _sidebar, _calls, _count = _ready_environment(
        monkeypatch, source=observed_source(EQUIVALENT_BLOB, check=_CHANGED)
    )
    facts[0] = observation(anchors=frozenset())
    held = _prepare(service, selection, "prepare.host")
    assert held["reason"] == "qualification_host_unqualified"


def test_source_drift_after_issuance_invalidates_and_fresh_qualification_may_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, registry, selection, clock, facts, sidebar, _calls, _count = _ready_environment(
        monkeypatch, source=observed_source(EQUIVALENT_BLOB)
    )
    before = set(sidebar._entries)
    prepared = _prepare(service, selection, "prepare.first")
    assert prepared["status"] == "ready"
    fingerprint = prepared["qualification_fingerprint"]
    assert registry.claim(fingerprint) is registry._current
    # Equal bytes from a replaced module or file: the wire cannot see it, currentness must.
    facts[1] = observed_source(EQUIVALENT_BLOB, origin="replaced-module")
    assert registry.claim(fingerprint) is None and registry._current is None
    facts[1] = observed_source(EQUIVALENT_BLOB)
    clock[0] += 0.5
    prepared = _prepare(service, selection, "prepare.origin")
    assert prepared["status"] == "ready"
    fingerprint = prepared["qualification_fingerprint"]
    # A benign byte edit: the new bytes are structurally admitted, the old receipt is not.
    facts[1] = observed_source("2" * 40)
    assert registry.claim(fingerprint) is None and registry._current is None
    clock[0] += 1
    again = _prepare(service, selection, "prepare.second")
    assert again["status"] == "ready" and again["qualification_fingerprint"] != fingerprint
    # A structurally changed source cannot be qualified afresh.
    facts[1] = observed_source("3" * 40, check=_CHANGED)
    assert _prepare(service, selection, "prepare.third")["status"] == "held"
    assert set(sidebar._entries) == before


@pytest.mark.parametrize("shape", ["segment_reads_only", "final_read_only"])
def test_source_drift_during_issuance_publishes_nothing_and_releases_the_producer(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    from comfyui_h3_context.adapters import comfyui_generation_profile
    from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkbenchError

    service, registry, selection, _clock, facts, sidebar, calls, _count = _ready_environment(
        monkeypatch, source=observed_source(EQUIVALENT_BLOB)
    )
    producer = registry._producer
    before = set(sidebar._entries)
    original, drifted = facts[1], observed_source("2" * 40)
    calls[0] = 0
    producer.observe(**selection)
    total = calls[0]  # the producer's original read, every segment's reads, the final recheck
    assert total >= 4

    def drifting() -> Any:
        calls[0] += 1
        if shape == "segment_reads_only":
            # A -> B -> A: only the per-segment comparison can see it; the final read agrees.
            changed = 1 < calls[0] < total
        else:
            # Only the final recheck sees new bytes; every segment agreed with the original.
            changed = calls[0] == total
        return composition_observation(facts[0], drifted if changed else original)

    calls[0] = 0
    monkeypatch.setattr(comfyui_generation_profile, "_observe_native_composition", drifting)
    with pytest.raises(ProductionWorkbenchError, match="qualification_changed"):
        producer.observe(**selection)
    # The per-segment check stops at the first segment; the final recheck is the last read.
    assert (calls[0] < total) if shape == "segment_reads_only" else (calls[0] == total)
    assert not producer._lock.locked()
    assert set(sidebar._entries) == before
    calls[0] = 0
    held = _prepare(service, selection, "prepare.drift")
    assert (held["status"], held["reason"]) == ("held", "qualification_changed")
    assert registry._current is None and set(sidebar._entries) == before
    monkeypatch.setattr(
        comfyui_generation_profile,
        "_observe_native_composition",
        lambda: composition_observation(facts[0], original),
    )
    assert _prepare(service, selection, "prepare.after")["status"] == "ready"


def test_issued_source_records_are_bounded_and_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    service, registry, selection, clock, _facts, _sidebar, _calls, _count = _ready_environment(
        monkeypatch, source=observed_source(EQUIVALENT_BLOB)
    )
    producer = registry._producer
    for index in range(producer_module._MAX_ISSUED_SOURCES + 3):
        clock[0] += 0.5
        prepared = _prepare(service, selection, f"prepare.bounded.{index}")
        assert prepared["status"] == "ready"
    assert len(producer._issued) <= producer_module._MAX_ISSUED_SOURCES
    clock[0] += 61
    assert _prepare(service, selection, "prepare.after.expiry")["status"] == "ready"
    assert len(producer._issued) == 1


def test_compiler_identity_covers_the_classifier_and_its_baseline_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sources = producer_module._COMPILER_SOURCES
    assert "core/native_t2va_structure.py" in sources
    assert "core/native_t2va_baseline.py" in sources
    assert len(set(sources)) == len(sources)
    package = Path(producer_module.__file__).resolve().parent.parent
    for relative in sources:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(package / relative, target)
    monkeypatch.setattr(
        producer_module, "__file__", str(tmp_path / "adapters" / "managed_mode_qualification.py")
    )
    original = producer_module.compiler_identity()
    for relative in ("core/native_t2va_structure.py", "core/native_t2va_baseline.py"):
        target = tmp_path / relative
        content = target.read_bytes()
        target.write_bytes(content + b"# changed rule\n")
        assert producer_module.compiler_identity() != original
        target.write_bytes(content)
        assert producer_module.compiler_identity() == original
    (tmp_path / "core/native_t2va_baseline.py").unlink()
    from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkbenchError

    with pytest.raises(ProductionWorkbenchError, match="qualification_compiler_unavailable"):
        producer_module.compiler_identity()


def test_fifteen_segment_structural_qualification_keeps_its_original_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, registry, selection, clock, _facts, sidebar, calls, count = _ready_environment(
        monkeypatch, source=observed_source(EQUIVALENT_BLOB), maximum_segments=True
    )
    assert count == 15
    before = set(sidebar._entries)
    calls[0] = 0
    prepared = _prepare(service, selection, "prepare.fifteen")
    assert prepared["status"] == "ready"
    qualification = registry._current
    assert len(qualification.composition_fingerprints) == 15
    assert (qualification.observed_at, qualification.expires_at) == (10.0, 70.0)
    # Each segment observes afresh at issuance and in its own currentness check.
    assert calls[0] >= 15 * 2
    clock[0] = 69.5
    assert registry.claim(prepared["qualification_fingerprint"]) is qualification
    assert registry._current.expires_at == 70.0
    clock[0] = 70.0
    assert registry.claim(prepared["qualification_fingerprint"]) is None
    assert set(sidebar._entries) == before
