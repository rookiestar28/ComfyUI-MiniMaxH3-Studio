"""Boundary regressions for exact native audio composition qualification."""

from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest
from native_source_doubles import composition_observation, observed_source
from test_generation_profile import observation
from test_native_composition import admitted_audio
from test_native_h3_adapter import _audio_only_reference_report

from comfyui_h3_context.core.errors import NativeH3AdapterError
from comfyui_h3_context.core.generation_profile import AssetSlot, HostObservation, SlotDisposition
from comfyui_h3_context.core.native_composition import (
    NativeCompositionObservation,
    NativeCompositionQualification,
    qualify_native_composition,
)
from comfyui_h3_context.core.native_h3 import NATIVE_H3_SOURCE_BLOB, build_native_h3_wiring
from comfyui_h3_context.core.native_source_compatibility import NativeSourceObservation
from comfyui_h3_context.core.perception_producer import PerceptionProducerResult

CurrentObserver = Callable[[], NativeCompositionObservation]
_BASELINE_SOURCE = observed_source()


def _qualify(
    *,
    host: HostObservation | None = None,
    source: NativeSourceObservation | None = _BASELINE_SOURCE,
    media: PerceptionProducerResult | None = None,
    observer: CurrentObserver | None = None,
) -> NativeCompositionQualification:
    report = _audio_only_reference_report()
    wiring = build_native_h3_wiring(report)
    selected_host = observation() if host is None else host
    selected_media = admitted_audio() if media is None else media
    current = (
        observer if observer is not None else lambda: composition_observation(selected_host, source)
    )
    return qualify_native_composition(
        report,
        wiring,
        host=selected_host,
        source=source,
        media=(selected_media,),
        observe_current=current,
    )


@pytest.mark.parametrize(
    "missing",
    [
        AssetSlot.REFERENCE_UNET,
        AssetSlot.TEXT_ENCODER,
        AssetSlot.VIDEO_VAE,
        AssetSlot.AUDIO_VAE,
    ],
)
def test_audio_only_defers_missing_weight_names_to_comfyui(missing: AssetSlot) -> None:
    host = observation(present=set(AssetSlot) - {missing})
    result = _qualify(host=host)
    assert result.qualified
    assert result.reason == "qualified_structural_composition"


@pytest.mark.parametrize("disposition", [SlotDisposition.ABSENT, SlotDisposition.RELOCATED])
def test_every_weight_name_can_remain_unverified_without_an_asset_receipt(
    disposition: SlotDisposition,
) -> None:
    host = observation()
    host = replace(host, slots=tuple(replace(slot, disposition=disposition) for slot in host.slots))
    result = _qualify(host=host)
    assert result.qualified
    assert result.reason == "qualified_structural_composition"


@pytest.mark.parametrize("source_blob", [None, "0" * 40, NATIVE_H3_SOURCE_BLOB.upper()])
def test_audio_only_never_qualifies_without_the_exact_loaded_source_pin(
    source_blob: str | None,
) -> None:
    result = _qualify(source=observed_source(source_blob))
    assert not result.qualified
    assert result.reason == "native_source_identity_mismatch"


@pytest.mark.parametrize(
    "payload",
    [
        {"waveform": SimpleNamespace(shape=(2, 1, 80000)), "sample_rate": 32000},
        {"waveform": SimpleNamespace(shape=(1, 2, 80000)), "sample_rate": 32000},
        {"waveform": SimpleNamespace(shape=(1, 1, 159999)), "sample_rate": 32000},
        {"waveform": SimpleNamespace(shape=[1, 1, 160000]), "sample_rate": 32000},
        {
            "waveform": SimpleNamespace(shape=(1, 1, 160000)),
            "sample_rate": 32000,
            "codec_ok": True,
        },
    ],
)
def test_audio_only_rejects_noncanonical_decoded_audio_shapes(
    payload: dict[str, object],
) -> None:
    result = _qualify(media=admitted_audio(payload=payload))
    assert not result.qualified
    assert result.reason == "native_audio_source_unqualified"


def test_fresh_observation_does_not_turn_missing_names_into_a_structural_hold() -> None:
    stale_host = observation()
    current_host = observation(present=set(AssetSlot) - {AssetSlot.AUDIO_VAE})
    result = _qualify(
        host=stale_host,
        observer=lambda: composition_observation(current_host, observed_source()),
    )
    assert result.qualified
    assert result.reason == "qualified_structural_composition"


def test_noncallable_currentness_observer_is_rejected_before_qualification() -> None:
    with pytest.raises(NativeH3AdapterError, match="native_host_currentness_unqualified"):
        _qualify(observer=cast(CurrentObserver, True))
