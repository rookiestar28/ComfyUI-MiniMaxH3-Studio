"""Audio composition qualification crosses real mapping, admission and product seams."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from native_source_doubles import composition_observation, observed_source
from test_generation_profile import observation
from test_native_h3_adapter import _audio_only_reference_report, _report
from test_native_mode_qualification import _edges, _surface_artifact

from comfyui_h3_context.core import ExecutionCorrelation, canonical_fingerprint
from comfyui_h3_context.core.context_reporting import ContextReport
from comfyui_h3_context.core.errors import NativeH3AdapterError, ProductShellError
from comfyui_h3_context.core.generation_profile import AssetSlot, HostObservation
from comfyui_h3_context.core.native_composition import (
    NativeCompositionDisposition,
    NativeCompositionQualification,
)
from comfyui_h3_context.core.native_composition import (
    qualify_native_composition as qualify_snapshot,
)
from comfyui_h3_context.core.native_h3 import (
    NATIVE_H3_SOURCE_BLOB,
    NativeH3Wiring,
    build_native_h3_wiring,
)
from comfyui_h3_context.core.native_mode_matrix import (
    NativeModeMatrixError,
    qualify_native_mode_surface,
)
from comfyui_h3_context.core.perception_producer import (
    MediaAdmissionEvidence,
    PerceptionProducerResult,
    admit_host_media,
)
from comfyui_h3_context.core.product_shell import (
    build_product_shell_projection,
    validate_product_shell_wire,
)
from comfyui_h3_context.core.sidebar_workspace import build_sidebar_workspace_projection
from scripts.hc_09_host_seam_test_double import host_nodes_module


def qualify_native_composition(
    report: ContextReport, wiring: NativeH3Wiring, **kwargs: Any
) -> NativeCompositionQualification:
    # Unit scenarios inject an observable model-free host; production uses the host adapter.
    kwargs.setdefault(
        "observe_current",
        lambda: composition_observation(kwargs.get("host"), kwargs.get("source")),
    )
    return qualify_snapshot(report, wiring, **kwargs)


def admitted_audio(
    *, asset_id: str = "audio_1", order: int = 0, payload: object = None
) -> PerceptionProducerResult:
    return admit_host_media(
        media_kind="audio",
        asset_id=asset_id,
        admission_evidence=MediaAdmissionEvidence(
            "a" * 64,
            None,
            None,
            5.0,
            32000,
            1,
            "reference",
            order,
        ),
        audio={"waveform": SimpleNamespace(shape=(1, 1, 160000)), "sample_rate": 32000}
        if payload is None
        else payload,
    )


def test_audio_mapping_executes_an_owned_synthetic_consumer_and_qualified_product_projection() -> (
    None
):
    report = _audio_only_reference_report()
    wiring = build_native_h3_wiring(report)
    media = admitted_audio()
    qualification = qualify_native_composition(
        report,
        wiring,
        host=observation(),
        source=observed_source(),
        media=(media,),
    )
    assert qualification.qualified
    qualification.assert_current(report, wiring)
    # Execute a model-free consumer of the actual mapping. Original AUDIO goes directly to
    # its native slot; no dummy visual, copied media, codec action or Queue call is needed.
    received: dict[str, Any] = {}

    def consume_native(**inputs: Any) -> int:
        received.update(inputs)
        return cast(int, inputs["ref_audios"]["ref_audio_0"]["sample_rate"])

    inputs: dict[str, dict[str, object]] = {}
    for binding in wiring.bindings:
        inputs.setdefault(binding.native_input, {})[binding.native_slot] = media.runtime_payload
    assert consume_native(**inputs) == 32000
    assert set(received) == {"ref_audios"}
    assert received["ref_audios"]["ref_audio_0"] is media.runtime_payload
    shell = build_product_shell_projection(
        report,
        wiring,
        ExecutionCorrelation("prompt-1", "17"),
        composition=qualification,
    )
    assert shell.native_queue_ready and shell.prompt_export_ready
    assert (
        validate_product_shell_wire(
            shell.to_wire(),
            report,
            wiring,
            ExecutionCorrelation("prompt-1", "17"),
            composition=qualification,
        )
        == shell
    )
    with pytest.raises(ProductShellError, match="semantic_drift"):
        validate_product_shell_wire(
            shell.to_wire(),
            report,
            wiring,
            ExecutionCorrelation("prompt-1", "17"),
        )
    assert qualification.to_wire()["schema"] == "h3.native_composition_qualification.v1"
    assert "waveform" not in str(qualification.to_wire())
    edges = _edges(wiring)
    artifact = _surface_artifact(report, wiring, edges, artifact_payload=shell.to_wire())
    with pytest.raises(NativeModeMatrixError, match="native_audio_composition_unqualified"):
        qualify_native_mode_surface(report, wiring, edges, artifact=artifact)
    surface = qualify_native_mode_surface(
        report,
        wiring,
        edges,
        artifact=artifact,
        composition=qualification,
    )
    assert surface.queue_ready and surface.wiring_fingerprint == canonical_fingerprint(
        wiring.to_wire()
    )
    other = qualify_native_composition(
        report,
        wiring,
        host=observation(),
        source=observed_source(),
        media=(admitted_audio(),),
    )
    other_surface = qualify_native_mode_surface(
        report,
        wiring,
        edges,
        artifact=artifact,
        composition=other,
    )
    assert other_surface.identity_fingerprint != surface.identity_fingerprint


@pytest.mark.parametrize("change", ["host", "source", "source_origin", "waveform", "in_place"])
def test_currentness_checks_live_host_source_and_inner_waveform_identity(change: str) -> None:
    report = _audio_only_reference_report()
    wiring = build_native_h3_wiring(report)
    media = admitted_audio()
    payload = cast(dict[str, Any], media.runtime_payload)
    payload["waveform"]._version = 0
    facts: list[Any] = [observation(), observed_source()]
    receipt = qualify_native_composition(
        report,
        wiring,
        host=facts[0],
        source=facts[1],
        media=(media,),
        observe_current=lambda: composition_observation(facts[0], facts[1]),
    )
    receipt.assert_current(report, wiring)
    if change == "host":
        facts[0] = observation(present=set(AssetSlot) - {AssetSlot.AUDIO_VAE})
    elif change == "source":
        facts[1] = observed_source("0" * 40)
    elif change == "source_origin":
        # Equal bytes from another registered module or file are a different observation.
        facts[1] = observed_source(origin="replaced-module")
    elif change == "waveform":
        payload["waveform"] = SimpleNamespace(shape=(1, 1, 160000), _version=0)
    else:
        payload["waveform"]._version += 1
    with pytest.raises(NativeH3AdapterError, match="native_composition_receipt_stale"):
        receipt.assert_current(report, wiring)


def test_static_host_snapshot_cannot_claim_runtime_currentness() -> None:
    report = _audio_only_reference_report()
    result = qualify_snapshot(
        report,
        build_native_h3_wiring(report),
        host=observation(),
        source=observed_source(),
        media=(admitted_audio(),),
    )
    assert not result.qualified
    assert result.reason == "native_host_currentness_unqualified"


@pytest.mark.parametrize(
    "host,source,media,reason",
    [
        (None, NATIVE_H3_SOURCE_BLOB, True, "native_host_unqualified"),
        (observation(anchors=frozenset()), NATIVE_H3_SOURCE_BLOB, True, "native_host_unsupported"),
        (observation(capabilities=()), NATIVE_H3_SOURCE_BLOB, True, "native_template_drift"),
        (observation(), "0" * 40, True, "native_source_identity_mismatch"),
        (observation(), NATIVE_H3_SOURCE_BLOB, False, "native_audio_source_unqualified"),
    ],
)
def test_unqualified_runtime_has_precise_no_effect_product_hold(
    host: HostObservation | None, source: str, media: bool, reason: str
) -> None:
    report = _audio_only_reference_report()
    wiring = build_native_h3_wiring(report)
    qualification = qualify_native_composition(
        report,
        wiring,
        host=host,
        source=observed_source(source),
        media=(admitted_audio(),) if media else (),
    )
    assert not qualification.qualified
    assert qualification.reason == reason
    shell = build_product_shell_projection(
        report,
        wiring,
        ExecutionCorrelation("prompt-1", "17"),
        composition=qualification,
    )
    assert shell.prompt_export_ready and not shell.native_queue_ready
    assert reason in shell.limitations


def test_default_audio_product_and_sidebar_are_held_without_observed_runtime() -> None:
    report = _audio_only_reference_report()
    wiring = build_native_h3_wiring(report)
    correlation = ExecutionCorrelation("prompt-1", "17")
    shell = build_product_shell_projection(report, wiring, correlation)
    assert not shell.native_queue_ready and "native_host_unqualified" in shell.limitations
    sidebar = build_sidebar_workspace_projection(
        report,
        wiring,
        correlation,
        workspace_id="ws_0123456789abcdefghijklmnopqrstuv",
        base_prompt_fingerprint=canonical_fingerprint(report.prompt_document.text),
    )
    assert sidebar.lifecycle == "ready"
    assert sidebar.actions.export and sidebar.actions.copy_prompt
    assert not sidebar.media_receipt["queue_ready"]


def test_legacy_visual_receipt_and_changed_audio_source_cannot_authorize_audio_composition() -> (
    None
):
    report = _audio_only_reference_report()
    wiring = build_native_h3_wiring(report)
    media = admitted_audio()
    qualification = qualify_native_composition(
        report,
        wiring,
        host=observation(),
        source=observed_source(),
        media=(media,),
    )
    other = _report()
    with pytest.raises(NativeH3AdapterError, match="native_composition_receipt_stale"):
        qualification.assert_current(other, build_native_h3_wiring(other))
    forged = replace(qualification, subject_fingerprint="sha256:" + "0" * 64)
    with pytest.raises(NativeH3AdapterError, match="native_composition_receipt_stale"):
        forged.assert_current(report, wiring)
    cast(dict[str, object], media.runtime_payload)["sample_rate"] = 16000
    with pytest.raises(NativeH3AdapterError, match="native_composition_receipt_stale"):
        qualification.assert_current(report, wiring)


@pytest.mark.parametrize(
    "media",
    [
        admitted_audio(asset_id="foreign"),
        admitted_audio(order=1),
        admitted_audio(payload={"codec_ok": True}),
        admitted_audio(
            payload={"waveform": SimpleNamespace(shape=(1, 1, 1)), "sample_rate": 32000}
        ),
    ],
)
def test_foreign_reordered_or_non_decoded_audio_never_qualifies(
    media: PerceptionProducerResult,
) -> None:
    report = _audio_only_reference_report()
    result = qualify_native_composition(
        report,
        build_native_h3_wiring(report),
        host=observation(),
        source=observed_source(),
        media=(media,),
    )
    assert result.disposition is NativeCompositionDisposition.UNQUALIFIED
    assert result.reason == "native_audio_source_unqualified"


def test_host_producer_uses_loaded_source_bytes_and_never_promotes_package_presence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from comfyui_h3_context.adapters import comfyui_generation_profile as adapter

    report = _audio_only_reference_report()
    wiring = build_native_h3_wiring(report)
    monkeypatch.setattr(adapter, "observe_host", observation)
    source = tmp_path / "nodes_minimax_h3.py"
    source.write_text("# synthetic source, never imported\n", encoding="utf-8")
    module = SimpleNamespace(__file__=str(source))
    host_nodes = host_nodes_module()
    mappings = host_nodes.NODE_CLASS_MAPPINGS
    for name in (
        "EmptyMiniMaxH3LatentAV",
        "MiniMaxH3ImageToVideo",
        "MiniMaxH3ReferenceToVideo",
        "MiniMaxH3SigmaShift",
    ):
        node = type(name, (), {"__module__": "native_fixture"})
        setattr(module, name, node)
        mappings[name] = node
    modules = {"nodes": host_nodes, "native_fixture": module}
    monkeypatch.setattr(adapter, "_host_module", modules.get)
    result = adapter.build_native_composition_qualification(
        report, wiring, media=(admitted_audio(),)
    )
    assert result.reason == "native_source_identity_mismatch"
    # Audio bindings never borrow structural T2VA compatibility, even from a readable source.
    assert result.detail == "receipt_absent"
    assert not result.qualified
    source.write_bytes(b" " * 262145)
    with pytest.raises(NativeH3AdapterError, match="native_composition_receipt_stale"):
        result.assert_current(report, wiring)
    oversized = adapter.build_native_composition_qualification(
        report, wiring, media=(admitted_audio(),)
    )
    assert oversized.reason == "native_source_identity_mismatch"
    assert oversized.observed_source() is None and oversized.detail == "source_unavailable"
    monkeypatch.setattr(adapter, "_host_module", lambda name: None)
    absent = adapter.build_native_composition_qualification(
        report, wiring, media=(admitted_audio(),)
    )
    assert not absent.qualified and absent.observed_source() is None
