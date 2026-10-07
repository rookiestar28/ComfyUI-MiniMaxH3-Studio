"""Structural source compatibility admits only binding-free T2VA and never outlives its bytes."""

import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from native_source_doubles import (
    EQUIVALENT_BLOB,
    composition_observation,
    observed_source,
    structure_verdict,
)
from test_generation_profile import observation
from test_native_h3_adapter import (
    _audio_only_reference_report,
    _keyframe_report,
    _report,
    _single_keyframe_report,
)

from comfyui_h3_context.adapters import comfyui_generation_profile as adapter
from comfyui_h3_context.core import ExecutionCorrelation
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.errors import NativeH3AdapterError
from comfyui_h3_context.core.native_composition import (
    NativeCompositionQualification,
    qualify_native_composition,
)
from comfyui_h3_context.core.native_h3 import build_native_h3_wiring
from comfyui_h3_context.core.native_source_compatibility import (
    NativeSourceCompatibilityV2,
    NativeSourceObservation,
    qualify_native_source_compatibility,
)
from comfyui_h3_context.core.native_t2va_structure import (
    NativeT2VAStructureVerdict,
    StructureReason,
    classify_native_t2va_source,
)
from comfyui_h3_context.core.product_shell import build_product_shell_projection
from scripts.hc_09_host_seam_test_double import host_nodes_module

_CHANGED = "definition:MiniMaxH3ImageToVideo.execute"
_NATIVE_CLASSES = (
    "EmptyMiniMaxH3LatentAV",
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
    "MiniMaxH3SigmaShift",
)


def _qualify(
    current: list[Any], *, report: Any = None, wiring: Any = None, receipt: Any = None
) -> NativeCompositionQualification:
    report = _report() if report is None else report
    wiring = build_native_h3_wiring(report) if wiring is None else wiring
    host = observation()
    return qualify_native_composition(
        report,
        wiring,
        host=host,
        source=current[0],
        observe_current=lambda: composition_observation(host, current[0]),
        source_compatibility=receipt,
    )


def test_structural_receipt_admits_t2va_and_binds_the_original_observation() -> None:
    report = _report()
    wiring = build_native_h3_wiring(report)
    source = observed_source(EQUIVALENT_BLOB)
    current: list[Any] = [source]
    # Without a receipt a structurally admitted non-baseline source is still never promoted.
    held = _qualify(current, report=report)
    assert (held.reason, held.detail) == ("native_source_identity_mismatch", "receipt_absent")
    receipt = qualify_native_source_compatibility(source, wiring)
    assert type(receipt) is NativeSourceCompatibilityV2
    result = _qualify(current, report=report, wiring=wiring, receipt=receipt)
    assert result.qualified and result.detail == "structural_receipt"
    wire = result.to_wire()
    assert wire["schema"] == "h3.native_composition_qualification.v4"
    assert wire["source_compatibility_fingerprint"] == receipt.fingerprint
    assert "registered-module" not in str(wire) and "structural_receipt" not in str(wire)
    result.assert_current(report, wiring)
    # A benign byte edit, the same bytes from another origin, or a changed structure each
    # invalidate the ORIGINAL receipt; assert_current never re-mints authority from them.
    for drifted in (
        observed_source("2" * 40),
        observed_source(EQUIVALENT_BLOB, origin="replaced-module"),
        observed_source(EQUIVALENT_BLOB, check=_CHANGED),
    ):
        current[0] = drifted
        with pytest.raises(NativeH3AdapterError, match="native_source_identity_mismatch"):
            result.assert_current(report, wiring)
    # A fresh observation of equal structure may qualify again with its own receipt.
    current[0] = observed_source("2" * 40)
    fresh = qualify_native_source_compatibility(current[0], wiring)
    assert fresh is not None and fresh.fingerprint != receipt.fingerprint
    assert _qualify(current, report=report, wiring=wiring, receipt=fresh).qualified


def test_changed_t2va_structure_has_a_bounded_internal_reason_only() -> None:
    report = _report()
    wiring = build_native_h3_wiring(report)
    current: list[Any] = [observed_source(EQUIVALENT_BLOB, check=_CHANGED)]
    assert qualify_native_source_compatibility(current[0], wiring) is None
    result = _qualify(current, report=report, wiring=wiring)
    assert not result.qualified
    assert (result.reason, result.detail) == ("native_t2va_structure_changed", _CHANGED)
    assert _CHANGED not in str(result.to_wire())
    shell = build_product_shell_projection(
        report, wiring, ExecutionCorrelation("prompt-1", "17"), composition=result
    )
    assert not shell.native_queue_ready
    assert "native_t2va_structure_changed" in shell.limitations
    assert _CHANGED not in str(shell.to_wire())


def test_baseline_blob_keeps_the_no_receipt_path_and_unavailable_source_stays_distinct() -> None:
    report = _report()
    wiring = build_native_h3_wiring(report)
    baseline = observed_source()
    assert qualify_native_source_compatibility(baseline, wiring) is None
    result = _qualify([baseline], report=report)
    assert result.qualified and result.detail == "baseline_blob"
    assert result.to_wire()["schema"] == "h3.native_composition_qualification.v1"
    # The exact baseline blob is admitted by identity, whatever its classification.
    assert _qualify([observed_source(check=_CHANGED)], report=report).qualified
    missing = _qualify([None], report=report)
    assert (missing.reason, missing.detail) == (
        "native_source_identity_mismatch",
        "source_unavailable",
    )


def test_receipt_fingerprint_is_deterministic_and_binds_structural_and_raw_identity() -> None:
    wiring = build_native_h3_wiring(_report())

    def fingerprint(source: NativeSourceObservation | None) -> str:
        receipt = qualify_native_source_compatibility(source, wiring)
        assert receipt is not None
        return receipt.fingerprint

    base = fingerprint(observed_source(EQUIVALENT_BLOB))
    assert base == fingerprint(observed_source(EQUIVALENT_BLOB))
    # Origin is an in-process currentness fact, never serialized into the fingerprint.
    assert base == fingerprint(observed_source(EQUIVALENT_BLOB, origin="other-module"))
    assert base != fingerprint(observed_source("2" * 40))
    report = _report()
    first = _qualify([observed_source(EQUIVALENT_BLOB)], report=report, receipt=None)
    second = _qualify([observed_source(EQUIVALENT_BLOB)], report=report, receipt=None)
    assert first.to_wire() == second.to_wire()


def test_unknown_changed_foreign_or_rebound_sources_get_no_receipt() -> None:
    wiring = build_native_h3_wiring(_report())
    assert qualify_native_source_compatibility(None, wiring) is None
    assert qualify_native_source_compatibility(EQUIVALENT_BLOB, wiring) is None  # type: ignore[arg-type]
    assert qualify_native_source_compatibility(observed_source(), wiring) is None
    for verdict in (
        structure_verdict(_CHANGED),
        NativeT2VAStructureVerdict(StructureReason.ADMITTED, "admitted", "sha256:" + "0" * 64),
        NativeT2VAStructureVerdict(
            StructureReason.ADMITTED,
            "admitted",
            structure_verdict().manifest_fingerprint,
            classifier_schema="h3.native_t2va_structure.v0",
        ),
    ):
        source = NativeSourceObservation(EQUIVALENT_BLOB, verdict, ("registered-module",))
        assert qualify_native_source_compatibility(source, wiring) is None
    for rebound in (
        replace(wiring, native_source_blob="2" * 40),
        replace(wiring, host_revision="3" * 40),
    ):
        assert (
            qualify_native_source_compatibility(observed_source(EQUIVALENT_BLOB), rebound) is None
        )
    # A directly constructed receipt still has to satisfy the same admission rule.
    forged = NativeSourceCompatibilityV2(observed_source())  # type: ignore[arg-type]
    assert not forged.admits(observed_source(), wiring)


@pytest.mark.parametrize("mode", [TaskMode.I2VA, TaskMode.L2VA, TaskMode.FL2VA])
def test_image_modes_cannot_borrow_structural_compatibility(mode: TaskMode) -> None:
    report = _keyframe_report() if mode is TaskMode.FL2VA else _single_keyframe_report(mode)
    wiring = build_native_h3_wiring(report)
    source = observed_source(EQUIVALENT_BLOB)
    assert qualify_native_source_compatibility(source, wiring) is None
    receipt = qualify_native_source_compatibility(source, build_native_h3_wiring(_report()))
    host = observation()
    with pytest.raises(NativeH3AdapterError, match="native_source_identity_mismatch"):
        qualify_native_composition(
            report,
            wiring,
            host=host,
            source=source,
            observe_current=lambda: composition_observation(host, source),
            source_compatibility=receipt,
        )
    # Media modes keep the pinned identity: a changed structure is not reported as T2VA drift.
    changed = observed_source(EQUIVALENT_BLOB, check=_CHANGED)
    held = qualify_native_composition(
        report,
        wiring,
        host=host,
        source=changed,
        observe_current=lambda: composition_observation(host, changed),
    )
    assert (held.reason, held.detail) == ("native_source_identity_mismatch", "receipt_absent")


def test_bindings_cannot_borrow_structural_compatibility() -> None:
    report = _audio_only_reference_report()
    wiring = build_native_h3_wiring(report)
    assert wiring.bindings
    assert qualify_native_source_compatibility(observed_source(EQUIVALENT_BLOB), wiring) is None


def _install_native_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: bytes
) -> tuple[Path, ModuleType, dict[str, Any]]:
    source = tmp_path / "nodes_minimax_h3.py"
    source.write_bytes(content)
    module_name = str(source.with_suffix(""))
    module = ModuleType(module_name)
    module.__file__ = str(source)
    host_nodes = host_nodes_module()
    mappings = host_nodes.NODE_CLASS_MAPPINGS
    for name in _NATIVE_CLASSES:
        cls = type(name, (), {"__module__": module_name})
        setattr(module, name, cls)
        mappings[name] = cls
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setitem(sys.modules, "nodes", host_nodes)
    monkeypatch.setattr(adapter, "observe_host", observation)
    return source, module, mappings


def _blob(content: bytes) -> str:
    header = b"blob " + str(len(content)).encode() + b"\0"
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def test_observer_hashes_and_classifies_one_registered_buffer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    content = b"# model-free source observer fixture\n"
    source, module, _mappings = _install_native_module(monkeypatch, tmp_path, content)
    observed = adapter._observe_native_composition().source
    assert observed is not None
    assert observed.source_blob == _blob(content)
    assert observed.structure == classify_native_t2va_source(content)
    assert observed.structure.reason is StructureReason.CHANGED
    assert module in observed._origin
    # P11: a write racing the classification cannot mix bytes; both facts share one buffer.
    received: list[bytes] = []

    def racing(buffer: bytes) -> NativeT2VAStructureVerdict:
        received.append(buffer)
        source.write_bytes(b"# rewritten while classifying\n")
        return classify_native_t2va_source(buffer)

    monkeypatch.setattr(adapter, "classify_native_t2va_source", racing)
    raced = adapter._observe_native_source()
    assert raced is not None and received == [content]
    assert raced.source_blob == _blob(received[0])


def test_observer_invalidates_on_byte_or_file_identity_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    content = b"# model-free source observer fixture\n"
    source, _module, _mappings = _install_native_module(monkeypatch, tmp_path, content)
    first = adapter._observe_native_source()
    assert first is not None and first == adapter._observe_native_source()
    source.write_bytes(content + b"# a benign comment\n")
    assert adapter._observe_native_source() != first
    source.write_bytes(content)
    # Identical bytes in a replacement file are a different observed file identity.
    replacement = tmp_path / "replacement.py"
    replacement.write_bytes(content)
    os.replace(replacement, source)
    again = adapter._observe_native_source()
    assert again is not None and again.source_blob == first.source_blob
    assert again != first


@pytest.mark.parametrize(
    "tamper", ["replaced_class", "missing_sigma_shift", "sigma_shift_elsewhere", "no_nodes"]
)
def test_observer_refuses_inactive_or_foreign_registrations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tamper: str
) -> None:
    _source, module, mappings = _install_native_module(monkeypatch, tmp_path, b"# fixture\n")
    assert adapter._observe_native_composition().source is not None
    if tamper == "replaced_class":
        mappings["MiniMaxH3ImageToVideo"] = type("Foreign", (), {"__module__": module.__name__})
    elif tamper == "missing_sigma_shift":
        del mappings["MiniMaxH3SigmaShift"]
    elif tamper == "sigma_shift_elsewhere":
        other = ModuleType("other_native_copy")
        copy = type("MiniMaxH3SigmaShift", (), {"__module__": "other_native_copy"})
        other.MiniMaxH3SigmaShift = copy  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "other_native_copy", other)
        mappings["MiniMaxH3SigmaShift"] = copy
    else:
        monkeypatch.delitem(sys.modules, "nodes")
    observed = adapter._observe_native_composition()
    assert observed.source is None and observed.host is not None


@pytest.mark.parametrize("tamper", ["basename", "oversized", "unreadable", "no_file"])
def test_observer_refuses_unbounded_or_unexpected_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tamper: str
) -> None:
    source, module, _mappings = _install_native_module(monkeypatch, tmp_path, b"# fixture\n")
    if tamper == "basename":
        renamed = tmp_path / "nodes_other.py"
        source.rename(renamed)
        module.__file__ = str(renamed)
    elif tamper == "oversized":
        source.write_bytes(b" " * 262_145)
    elif tamper == "unreadable":
        source.unlink()
    else:
        del module.__file__
    assert adapter._observe_native_source() is None
