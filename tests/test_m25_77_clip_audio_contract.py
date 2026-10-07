"""The clip wire's audio member -- its shape, its canonical identity and its admission.

The member carries the gain, mute and fades of a video clip's embedded audio. Its identity value
is never written: a clip without the member has identity audio, and an explicit identity object is
refused, so every composition made before the member existed keeps its bytes and its public
fingerprint. A value other than the identity is admitted only on a clip whose asset has bound
audio. The shape rows (`tests/fixtures/m25_77_clip_audio_wire_rows_v1.json`) and the admission rows
(`tests/fixtures/m25_77_clip_audio_admission_rows_v1.json`) are shared with the browser decoders,
so both ends answer one table.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from comfyui_h3_context.core import composition_contract as contract
from comfyui_h3_context.core.composition_contract import (
    PUBLIC_SNAPSHOT_SCHEMA,
    CompositionContractError,
    _clip,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.nle_authoring_contract import (
    NLE_AUTHORING_SCHEMA,
    decode_nle_authoring_state,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
COMPOSITION = json.loads(
    (FIXTURES / "m25_10_composition_contract_v1.json").read_text(encoding="utf-8")
)["snapshot"]
ROWS = json.loads((FIXTURES / "m25_77_clip_audio_wire_rows_v1.json").read_text(encoding="utf-8"))[
    "rows"
]
ADMISSION = json.loads(
    (FIXTURES / "m25_77_clip_audio_admission_rows_v1.json").read_text(encoding="utf-8")
)
SURFACE = json.loads(
    (ROOT / "comfyui_h3_context" / "contracts" / "cross_language_surface_v1.json").read_text(
        encoding="utf-8"
    )
)


def _clip_wire(clip_id: str = "clip-main") -> dict[str, object]:
    clips = COMPOSITION["clips"]
    assert isinstance(clips, list)
    return copy.deepcopy(next(clip for clip in clips if clip["clip_id"] == clip_id))


def _with_audio(row: dict[str, object]) -> dict[str, object]:
    wire = _clip_wire()
    wire["duration_frames"] = row["duration_frames"]
    if not row.get("absent"):
        wire["audio"] = copy.deepcopy(row["audio"])
    return wire


def _audio_wire(audio: contract.ClipAudio) -> dict[str, object]:
    return {
        "gain_mb": audio.gain_mb,
        "muted": audio.muted,
        "fade_in_frames": audio.fade_in_frames,
        "fade_out_frames": audio.fade_out_frames,
    }


@pytest.mark.parametrize("row", ROWS, ids=[str(row["name"]) for row in ROWS])
def test_the_clip_decoder_answers_the_shared_row_table(row: dict[str, object]) -> None:
    wire = _with_audio(row)
    if "refusal" in row:
        with pytest.raises(CompositionContractError) as raised:
            _clip(wire, 0)
        assert raised.value.code == row["refusal"]
        return
    clip = _clip(wire, 0)
    assert _audio_wire(clip.audio) == row["result"]
    # The decoded clip writes back exactly the wire it was given: the member when it carries a
    # value, nothing when it is the identity.
    assert clip.to_wire() == wire


def test_the_table_has_a_row_for_every_bound_and_both_sides_of_it() -> None:
    names = {str(row["name"]) for row in ROWS}
    for expected in (
        "absent",
        "explicit_identity",
        "gain_lower_bound",
        "gain_upper_bound",
        "gain_below_lower_bound",
        "gain_above_upper_bound",
        "fade_in_upper_bound",
        "fade_out_upper_bound",
        "fade_in_above_upper_bound",
        "fade_out_above_upper_bound",
        "fade_in_negative",
        "fade_out_negative",
        "fades_fill_the_clip",
        "fades_exceed_the_clip",
        "missing_member",
        "extra_member",
    ):
        assert expected in names


@pytest.mark.parametrize("value", [None, 7, "audio", ["audio"]])
def test_a_clip_that_is_not_an_object_is_refused_before_its_members_are_read(
    value: object,
) -> None:
    # Whether the optional member is present is asked of an object only: asked of anything else
    # it would raise before the decoder's own refusal.
    with pytest.raises(CompositionContractError) as raised:
        _clip(value, 0)
    assert raised.value.code == "invalid_contract"


def test_the_member_value_is_four_frozen_fields_in_wire_order() -> None:
    names = ("gain_mb", "muted", "fade_in_frames", "fade_out_frames")
    assert tuple(field.name for field in dataclasses.fields(contract.ClipAudio)) == names
    # Like every value of the contract: slots, so no instance dictionary, and no attribute write.
    assert contract.ClipAudio.__slots__ == names
    with pytest.raises(dataclasses.FrozenInstanceError):
        contract.IDENTITY_CLIP_AUDIO.gain_mb = -600  # type: ignore[misc]


def test_the_identity_value_is_never_written_and_any_other_value_is() -> None:
    clip = _clip(_clip_wire(), 0)
    assert clip.audio == contract.IDENTITY_CLIP_AUDIO
    assert "audio" not in clip.to_wire()
    adjusted = contract.ClipAudio(-600, False, 12, 0)
    wire = _clip_wire()
    wire["audio"] = _audio_wire(adjusted)
    decoded = _clip(wire, 0)
    assert decoded.audio == adjusted
    assert decoded.to_wire()["audio"] == _audio_wire(adjusted)
    assert contract.IDENTITY_CLIP_AUDIO == contract.ClipAudio(0, False, 0, 0)


def _documents(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        if value.get("schema") in (PUBLIC_SNAPSHOT_SCHEMA, NLE_AUTHORING_SCHEMA):
            yield value
        for member in value.values():
            yield from _documents(member)
    elif isinstance(value, list):
        for member in value:
            yield from _documents(member)


def test_every_committed_composition_keeps_its_bytes_and_decodes_to_identity_audio() -> None:
    decoded = 0
    for path in sorted(FIXTURES.rglob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for wire in _documents(document):
            decode = (
                decode_public_snapshot
                if wire["schema"] == PUBLIC_SNAPSHOT_SCHEMA
                else decode_nle_authoring_state
            )
            try:
                value = decode(wire)
            except ContractValidationError:
                # A fixture may hold a refusal vector on purpose; what it refuses is its own
                # suite's business. What matters here is that nothing that decoded changed.
                continue
            assert value.to_wire() == wire, path.name
            assert all(clip.audio == contract.IDENTITY_CLIP_AUDIO for clip in value.clips), (
                path.name
            )
            clips = wire["clips"]
            assert isinstance(clips, list)
            assert all("audio" not in clip for clip in clips), path.name
            decoded += 1
    # The accepted composition fixture and both semantic corpus bases at least.
    assert decoded >= 3


def _admission_wire(row: dict[str, object], *, with_audio: bool) -> dict[str, object]:
    wire: dict[str, object] = copy.deepcopy(COMPOSITION)
    clips = wire["clips"]
    assets = wire["assets"]
    assert isinstance(clips, list) and isinstance(assets, list)
    clip = next(item for item in clips if item["clip_id"] == row["clip_id"])
    if "asset_id" in row:
        clip["asset_id"] = row["asset_id"]
    if "embedded_audio" in row:
        asset = next(item for item in assets if item["asset_id"] == clip["asset_id"])
        asset["embedded_audio"] = row["embedded_audio"]
    if with_audio:
        clip["audio"] = copy.deepcopy(ADMISSION["audio"])
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return wire


@pytest.mark.parametrize(
    "row", ADMISSION["rows"], ids=[str(row["name"]) for row in ADMISSION["rows"]]
)
def test_a_member_is_admitted_only_on_a_clip_whose_asset_has_bound_audio(
    row: dict[str, object],
) -> None:
    # The same composition without the member is admitted, so a refusal is the member's own.
    decode_public_snapshot(_admission_wire(row, with_audio=False))
    wire = _admission_wire(row, with_audio=True)
    if row["result"] != "accepted":
        with pytest.raises(CompositionContractError) as raised:
            decode_public_snapshot(wire)
        assert raised.value.code == row["result"]
        return
    snapshot = decode_public_snapshot(wire)
    adjusted = next(clip for clip in snapshot.clips if clip.clip_id == row["clip_id"])
    assert _audio_wire(adjusted.audio) == ADMISSION["audio"]
    assert snapshot.to_wire() == wire


def test_the_admission_table_has_both_outcomes_and_every_kind_of_clip() -> None:
    rows = ADMISSION["rows"]
    assert {row["result"] for row in rows} == {"accepted", "invalid_contract"}
    # Track placement does not decide it: an overlay clip of a source with bound audio is admitted.
    assert {row["clip_id"] for row in rows if row["result"] == "accepted"} == {
        "clip-main",
        "clip-video-overlay",
    }
    assert {"clip-image", "clip-title"} <= {row["clip_id"] for row in rows}
    assert {row.get("embedded_audio") for row in rows} >= {"absent", "unavailable"}


def test_each_clip_is_admitted_on_its_own_asset_whatever_clip_precedes_it() -> None:
    """A clip without bound audio is refused even when the clip read just before it has some.

    The decoder reads the clips in wire order and decides each one's admission from that clip's own
    asset; nothing it found for the clip before may stand in for the next one's.
    """

    wire = copy.deepcopy(COMPOSITION)
    clips = wire["clips"]
    main = next(clip for clip in clips if clip["clip_id"] == "clip-main")
    title = next(clip for clip in clips if clip["clip_id"] == "clip-title")
    clips.remove(title)
    clips.insert(clips.index(main) + 1, title)
    main["audio"] = copy.deepcopy(ADMISSION["audio"])
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    # The order alone, and the member on the clip with bound audio, are admitted.
    decode_public_snapshot(copy.deepcopy(wire))
    title["audio"] = copy.deepcopy(ADMISSION["audio"])
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    with pytest.raises(CompositionContractError) as raised:
        decode_public_snapshot(wire)
    assert raised.value.code == "invalid_contract"


def test_the_generated_surface_declares_the_member_and_its_shape() -> None:
    shapes = {shape["export_name"]: shape for shape in SURFACE["shapes"]}
    assert shapes["compositionClipKeys"]["optional_keys"] == ["audio"]
    assert "audio" not in shapes["compositionClipKeys"]["required_keys"]
    assert shapes["clipAudioKeys"]["required_keys"] == [
        "fade_in_frames",
        "fade_out_frames",
        "gain_mb",
        "muted",
    ]
    assert shapes["clipAudioKeys"]["class_name"] == "ClipAudio"
