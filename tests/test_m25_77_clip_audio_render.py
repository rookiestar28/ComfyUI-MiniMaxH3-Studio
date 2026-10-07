"""The one definition of a clip's audio adjustments, and the final render's statement of it.

`core/clip_audio.py` states the factor a clip's audio is multiplied by at each output sample of
the clip; the render graph turns it into `volume` and `afade` filters for each audio run of the
clip. The table both languages are tested against is generated from the Python statement
(`scripts/m25_77_clip_audio_envelope_fixture.py`). Here the statement is held to an exact one, the
filters are held to the statement through the per-sample gains FFmpeg's filters were measured to
apply on the pinned build, and a composition without adjustments keeps its plan and its program
byte for byte.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from dataclasses import FrozenInstanceError, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import pytest
from test_m25_authoring_render_executor import process_control, stores
from test_m25_authoring_render_jobs import _plan, long_plan
from test_m25_render_planner import _currentness, _font_facts, _snapshot_wire, _source_manifest

from comfyui_h3_context.adapters.authoring_render_graph import (
    RenderAudioRun,
    RenderGraphError,
    _clip_audio_filters,
    build_render_graph,
)
from comfyui_h3_context.core import render_planner
from comfyui_h3_context.core.authoring_render_jobs import (
    render_plan_fingerprint,
    semantic_render_fingerprint,
)
from comfyui_h3_context.core.canonical import canonical_bytes, canonical_fingerprint
from comfyui_h3_context.core.clip_audio import (
    SAMPLES_PER_FRAME,
    ClipAudioRun,
    clip_audio_factor,
    clip_audio_gain,
    clip_audio_run,
)
from comfyui_h3_context.core.composition_contract import (
    NLE_OPERATION_IDS,
    ClipAudio,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.render_planner import (
    PlanClipAudio,
    RenderPlanV1,
    plan_render,
    required_unqualified_renderer_profile,
)
from scripts.m25_77_clip_audio_envelope_fixture import TARGET_PATH, build, main

ROOT = Path(__file__).resolve().parents[1]

# Measured on the commit the clip audio member was added to (057a8c71): a plan, and a program, of a
# composition without adjustments reproduces each of these byte for byte while the renderer profile
# names the base's command list (`_base_operation_list`). The command itself moves the profile's
# fingerprint and the three fingerprints that bind it, and nothing else of such a plan
# (`test_the_command_reaches_an_unadjusted_plan_only_through_the_renderer_profile`).
BASE_IDENTITY = {
    "renderer_capability_profile_fingerprint": (
        "sha256:a0503df2eddd79f8c97f079f1f1c67c49336ff578d1a9625e5fa31206536d369"
    ),
    "unit_plan.plan_wire_without_chunks": (
        "27a7be69bb271fe08df33517ace5db9b449ed285db13c469b949b7b56015b748"  # noqa: E501  # pragma: allowlist secret
    ),
    "unit_plan.render_plan_fingerprint": (
        "sha256:5b7b693d7ab0d960963facd073c052058fadc6dfb114f4d9c3e761b31e31b9d3"
    ),
    "unit_plan.idempotency_fingerprint": (
        "sha256:6469875f62797630ef06451b1ba1a55aad63637eed8a07bb3d58a47ead5756b6"
    ),
    "long_plan.plan_wire_without_chunks": (
        "fc3734e59994c2f41125d40e0145d3a6ee79a53d39afff18f50bcab637da128c"  # noqa: E501  # pragma: allowlist secret
    ),
    "long_plan.render_plan_fingerprint": (
        "sha256:84691009f201d2531625abf0773015cb372a31ce208cca2cdcfcfe3d4e1a84ec"
    ),
    "long_plan.idempotency_fingerprint": (
        "sha256:3a09b8d0d358a69f0f58f27c272889cbafc51fd8b0e99177daca4e171e546521"
    ),
    "bound_video_plan.plan_wire_without_chunks": (
        "27e9bb0befe71c455c54a109285a3c582b8f52da284635c6f73dcc3d8a455990"  # noqa: E501  # pragma: allowlist secret
    ),
    "bound_video_plan.render_plan_fingerprint": (
        "sha256:8a64416a4fcbba4a70a14b40f31bc25c6c550b1f9c1b43df22e3ab330c87dc60"
    ),
    "bound_video_plan.idempotency_fingerprint": (
        "sha256:245efc6ad395d3a727e06b627f22b1d5cc8e737637ae09465ece727452ce2135"
    ),
    "bound_video_plan.filter_graph": (
        "4f4dd23850ce392149140208c98ff170e501aec147785185a333dd9b5e5546cb"  # noqa: E501  # pragma: allowlist secret
    ),
    "unit_plan.semantic_render_fingerprint": (
        "sha256:5a1e2fd9fe4715b66bd1fe5be499a8a497b9e952dfe598d92a7545c6a5c7b311"
    ),
    "long_plan.semantic_render_fingerprint": (
        "sha256:cbfca53d49070657ccf26ef74a8abedd29f70ff2ae4fde513af95c8b6c278dca"
    ),
    "bound_video_plan.semantic_render_fingerprint": (
        "sha256:577521e4ce044e431eee85ac190b42bda7e8c143d784f7ac28139bab4abcec75"
    ),
}


def _sha(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _base_operation_list() -> tuple[str, ...]:
    """The closed operation list as it was at the base: the current one without `set_clip_audio`."""

    return tuple(name for name in NLE_OPERATION_IDS if name != "set_clip_audio")


@pytest.fixture
def base_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plans made under the renderer profile of the base.

    The profile names the closed operation list (`semantic_command_ids`) and every plan binds the
    profile's fingerprint, so adding a command moves that fingerprint for every plan; the plan's
    idempotency, plan and semantic render fingerprints bind it in turn. Under this fixture a
    composition without adjustments must reproduce the base's bytes exactly.
    """

    monkeypatch.setattr(render_planner, "NLE_OPERATION_IDS", _base_operation_list())


def _exact(audio: ClipAudio, duration_frames: int, k: int) -> float:
    """The definition again, in exact fractions where it is rational."""

    if audio.muted:
        return 0.0
    length = duration_frames * SAMPLES_PER_FRAME
    rising = Fraction(1)
    falling = Fraction(1)
    if audio.fade_in_frames:
        rising = min(Fraction(1), Fraction(k, audio.fade_in_frames * SAMPLES_PER_FRAME))
    if audio.fade_out_frames:
        falling = min(Fraction(1), Fraction(length - k, audio.fade_out_frames * SAMPLES_PER_FRAME))
    return math.pow(10.0, audio.gain_mb / 2000) * float(rising * falling)


def test_the_envelope_table_is_what_its_generator_writes() -> None:
    # Byte for byte, by the generator's own check.
    assert main(["--check"]) == 0
    assert json.loads(TARGET_PATH.read_text(encoding="utf-8")) == build()


def test_the_statement_equals_the_exact_definition_at_every_table_row() -> None:
    table = build()
    assert table["samples_per_frame"] == SAMPLES_PER_FRAME
    rows = 0
    for clip in cast(list[dict[str, Any]], table["clips"]):
        audio = ClipAudio(**clip["audio"])
        for k, factor in clip["factors"]:
            assert clip_audio_factor(audio, clip["duration_frames"], k) == factor
            assert factor == pytest.approx(
                _exact(audio, clip["duration_frames"], k), rel=1e-12, abs=1e-15
            )
            rows += 1
    # Every edge, neighbour and midpoint the generator marks on its twelve clips.
    assert rows == 96


@pytest.mark.parametrize("k", [-1, 2 * SAMPLES_PER_FRAME])
def test_a_sample_outside_the_clip_has_no_factor(k: int) -> None:
    with pytest.raises(ValueError, match="outside the clip"):
        clip_audio_factor(ClipAudio(-600, False, 1, 0), 2, k)


@pytest.mark.parametrize(("k0", "k1"), [(-1, 10), (0, 2 * SAMPLES_PER_FRAME + 1), (5, 5), (6, 5)])
def test_a_run_outside_the_clip_has_no_filters(k0: int, k1: int) -> None:
    with pytest.raises(ValueError, match="outside the clip"):
        clip_audio_run(ClipAudio(-600, False, 1, 0), 2, k0, k1)


def test_the_filters_of_a_run_are_one_frozen_value() -> None:
    audio = ClipAudio(-600, False, 1, 1)
    applied = clip_audio_run(audio, 2, 0, 2 * SAMPLES_PER_FRAME)
    assert applied == ClipAudioRun(clip_audio_gain(audio), (2_000, 0.0), (2_000, 2_000, 1.0))
    with pytest.raises(FrozenInstanceError):
        applied.volume = 1.0  # type: ignore[misc]
    assert ClipAudioRun.__slots__ == ("volume", "fade_in", "fade_out")


def test_a_table_row_is_one_frozen_value_built_by_name() -> None:
    audio = ClipAudio(-600, False, 0, 0)
    row = PlanClipAudio(clip_id="c", start_frame=0, duration_frames=2, audio=audio)
    with pytest.raises(FrozenInstanceError):
        row.audio = ClipAudio(0, True, 0, 0)  # type: ignore[misc]
    # Its order is the wire's (`to_wire`), not its fields'.
    assert set(PlanClipAudio.__slots__) == {"clip_id", "start_frame", "duration_frames", "audio"}
    with pytest.raises(TypeError):
        PlanClipAudio("c", 0, 2, audio)  # type: ignore[call-arg]


def _float32(value: float) -> float:
    return float(struct.unpack("<f", struct.pack("<f", value))[0])


_FILTER = re.compile(
    r",(?:volume=(?P<volume>[^,]+)"
    r"|afade=t=in:ss=0:ns=(?P<in_n>\d+):silence=(?P<silence>[^,]+)"
    r"|afade=t=out:ss=(?P<out_s>\d+):ns=(?P<out_n>\d+)(?::unity=(?P<unity>[^,]+))?)"
)


def _gains(chain: str, samples: int) -> list[float]:
    """The per-sample gain FFmpeg applies for `chain`, as measured on the pinned build.

    `volume=v` multiplies by float32(v). A fade-in from sample 0 over N samples with `silence=s`
    gives s + (1 - s) * min(1, i / N); a fade-out from S over N with `unity=u` (1 when absent)
    gives u before S, u * (S + N - i) / N up to S + N and 0 after.
    """

    gains = [1.0] * samples
    position = 0
    for match in _FILTER.finditer(chain):
        assert match.start() == position, chain
        position = match.end()
        if match["volume"] is not None:
            value = _float32(float(match["volume"]))
            gains = [gain * value for gain in gains]
        elif match["in_n"] is not None:
            count, silence = int(match["in_n"]), float(match["silence"])
            gains = [
                gain * (silence + (1 - silence) * min(1.0, i / count))
                for i, gain in enumerate(gains)
            ]
        else:
            start, count = int(match["out_s"]), int(match["out_n"])
            unity = 1.0 if match["unity"] is None else float(match["unity"])
            gains = [
                gain * unity * min(1.0, max(0.0, (start + count - i) / count))
                for i, gain in enumerate(gains)
            ]
    assert position == len(chain), chain
    return gains


#: (duration frames, audio) for the per-sample check; short, so every sample is compared.
_CLIPS = [
    (6, ClipAudio(-600, False, 2, 2)),
    (6, ClipAudio(0, False, 3, 3)),
    (6, ClipAudio(0, False, 2, 0)),
    (6, ClipAudio(0, False, 0, 2)),
    (6, ClipAudio(300, False, 0, 0)),
    (6, ClipAudio(-1200, True, 2, 2)),
    (1, ClipAudio(0, False, 1, 0)),
    (1, ClipAudio(-6000, False, 0, 1)),
]


def _runs(duration: int, audio: ClipAudio) -> list[tuple[int, int]]:
    """The whole clip, and runs that start or end before, inside, at and after each ramp."""

    length = duration * SAMPLES_PER_FRAME
    fade_in = audio.fade_in_frames * SAMPLES_PER_FRAME
    fade_out = length - audio.fade_out_frames * SAMPLES_PER_FRAME
    marks = sorted(
        {
            0,
            1,
            fade_in // 2,
            fade_in,
            (fade_in + fade_out) // 2,
            fade_out,
            (fade_out + length) // 2,
            length - 1,
            length,
        }
    )
    runs = {(0, length), (length - 1, length), (0, 1)}
    for start in marks:
        for end in marks:
            if 0 <= start < end <= length:
                runs.add((start, end))
    return sorted(runs)


@pytest.mark.parametrize(("duration", "audio"), _CLIPS)
def test_the_filters_of_every_run_give_the_definition_at_every_sample(
    duration: int, audio: ClipAudio
) -> None:
    origin = 7  # a clip that does not start at the timeline's first frame
    entry = PlanClipAudio(
        clip_id="clip-a", start_frame=origin, duration_frames=duration, audio=audio
    )
    for k0, k1 in _runs(duration, audio):
        start = origin * SAMPLES_PER_FRAME
        run = RenderAudioRun(start + k0, start + k1, "asset-a", "clip-a", 0)
        gains = _gains(_clip_audio_filters(entry, run), k1 - k0)
        for i, gain in enumerate(gains):
            expected = clip_audio_factor(audio, duration, k0 + i)
            # float32 of the gain, and the graph's twelve significant digits of a ramp's start.
            assert gain == pytest.approx(expected, rel=2e-7, abs=1e-11), (k0, k1, i)


def test_a_run_that_starts_inside_the_fade_out_resumes_the_clip_envelope() -> None:
    # A clip of 48 frames with a fade-out of 12 is owned again from sample 80000 after another
    # clip ended inside it: the ramp continues from where the clip's own envelope stands.
    entry = PlanClipAudio(
        clip_id="c", start_frame=0, duration_frames=48, audio=ClipAudio(0, False, 0, 12)
    )
    assert _clip_audio_filters(entry, RenderAudioRun(80_000, 96_000, "a", "c", 0)) == (
        ",afade=t=out:ss=0:ns=16000:unity=0.666666666667"
    )
    # A run that reaches the ramp from before it, or starts exactly at it: the ramp starts inside
    # the run, at full level.
    assert _clip_audio_filters(entry, RenderAudioRun(60_000, 80_000, "a", "c", 0)) == (
        ",afade=t=out:ss=12000:ns=24000"
    )
    assert _clip_audio_filters(entry, RenderAudioRun(72_000, 96_000, "a", "c", 0)) == (
        ",afade=t=out:ss=0:ns=24000"
    )
    assert _clip_audio_filters(entry, RenderAudioRun(0, 60_000, "a", "c", 0)) == ""


def test_a_run_that_starts_inside_the_fade_in_continues_it() -> None:
    entry = PlanClipAudio(
        clip_id="c", start_frame=2, duration_frames=48, audio=ClipAudio(-600, False, 12, 0)
    )
    assert _clip_audio_filters(
        entry, RenderAudioRun(4_000 + 6_000, 4_000 + 96_000, "a", "c", 0)
    ) == (",volume=0.501187233627,afade=t=in:ss=0:ns=18000:silence=0.25")


def test_a_muted_clip_is_silenced_by_volume_alone() -> None:
    entry = PlanClipAudio(
        clip_id="c", start_frame=0, duration_frames=48, audio=ClipAudio(-600, True, 12, 12)
    )
    assert _clip_audio_filters(entry, RenderAudioRun(0, 96_000, "a", "c", 0)) == ",volume=0"


def test_a_run_outside_the_clip_its_table_row_names_is_refused() -> None:
    entry = PlanClipAudio(
        clip_id="c", start_frame=1, duration_frames=2, audio=ClipAudio(-600, False, 0, 0)
    )
    with pytest.raises(RenderGraphError, match="plan_mismatch"):
        _clip_audio_filters(entry, RenderAudioRun(0, 4_000, "a", "c", 0))


def _identity(name: str, plan: Any) -> dict[str, str]:
    wire = plan.to_wire()
    wire.pop("resolved_operations")
    return {
        f"{name}.plan_wire_without_chunks": _sha(canonical_bytes(wire)),
        f"{name}.render_plan_fingerprint": render_plan_fingerprint(plan),
        f"{name}.idempotency_fingerprint": str(wire["idempotency_fingerprint"]),
        f"{name}.semantic_render_fingerprint": semantic_render_fingerprint(plan),
    }


@pytest.mark.usefixtures("base_profile")
def test_a_plan_without_adjustments_keeps_the_bytes_and_fingerprints_of_the_base() -> None:
    plans = {"unit_plan": _plan(), "long_plan": long_plan()}
    for name, plan in plans.items():
        assert (
            plan.renderer_capability_profile_fingerprint
            == (BASE_IDENTITY["renderer_capability_profile_fingerprint"])
        )
        assert plan.clip_audio == ()
        assert "clip_audio" not in plan.to_wire()
        for key, value in _identity(name, plan).items():
            assert value == BASE_IDENTITY[key], key


def test_the_command_reaches_an_unadjusted_plan_only_through_the_renderer_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The one exception to the base's bytes, stated exactly: the closed list gains `set_clip_audio`
    # after `set_effect` and nothing else, so the profile's fingerprint moves, and with it the
    # idempotency fingerprint that binds it; every other member of the plan is the base's.
    base_list = _base_operation_list()
    after = base_list.index("set_effect") + 1
    assert NLE_OPERATION_IDS == (*base_list[:after], "set_clip_audio", *base_list[after:])
    current = {"unit_plan": _plan(), "long_plan": long_plan()}
    monkeypatch.setattr(render_planner, "NLE_OPERATION_IDS", base_list)
    base = {"unit_plan": _plan(), "long_plan": long_plan()}
    monkeypatch.undo()
    profile = required_unqualified_renderer_profile().profile_fingerprint
    assert profile != BASE_IDENTITY["renderer_capability_profile_fingerprint"]
    for name, plan in current.items():
        now, then = plan.to_wire(), base[name].to_wire()
        assert sorted(key for key in now if now[key] != then[key]) == [
            "idempotency_fingerprint",
            "renderer_capability_profile_fingerprint",
        ], name
        assert plan.renderer_capability_profile_fingerprint == profile
        assert plan.idempotency_fingerprint == _restated_idempotency_fingerprint(plan, None)
        restated = replace(
            plan,
            renderer_capability_profile_fingerprint=(
                base[name].renderer_capability_profile_fingerprint
            ),
        )
        assert (
            _restated_idempotency_fingerprint(restated, None)
            == (BASE_IDENTITY[f"{name}.idempotency_fingerprint"])
        )


def test_the_table_is_written_and_bound_only_when_a_clip_carries_an_adjustment() -> None:
    plan = _plan()
    row = PlanClipAudio(
        clip_id="clip-main", start_frame=0, duration_frames=48, audio=ClipAudio(-600, False, 12, 12)
    )
    adjusted = replace(plan, clip_audio=(row,))
    assert adjusted.to_wire()["clip_audio"] == [
        {
            "clip_id": "clip-main",
            "start_frame": 0,
            "duration_frames": 48,
            "audio": {"gain_mb": -600, "muted": False, "fade_in_frames": 12, "fade_out_frames": 12},
        }
    ]
    assert render_plan_fingerprint(adjusted) != render_plan_fingerprint(plan)
    assert replace(adjusted, clip_audio=()).to_wire() == plan.to_wire()


@pytest.mark.usefixtures("base_profile")
def test_the_semantic_identity_of_a_render_tells_its_audio_adjustments_apart() -> None:
    # The semantic fingerprint states which media a render's inputs produce. An adjustment changes
    # the audio, so it changes that identity; without one, the identity is the base's (under the
    # base's renderer profile, which the fingerprint binds).
    plan = _plan()
    row = PlanClipAudio(
        clip_id="clip-main", start_frame=0, duration_frames=48, audio=ClipAudio(-600, False, 0, 0)
    )
    muted = replace(row, audio=ClipAudio(-600, True, 0, 0))
    identities = {
        semantic_render_fingerprint(plan),
        semantic_render_fingerprint(replace(plan, clip_audio=(row,))),
        semantic_render_fingerprint(replace(plan, clip_audio=(muted,))),
    }
    assert len(identities) == 3
    assert semantic_render_fingerprint(
        replace(plan, clip_audio=(row,), snapshot_revision=plan.snapshot_revision + 1)
    ) == semantic_render_fingerprint(replace(plan, clip_audio=(row,)))
    assert (
        semantic_render_fingerprint(plan) == BASE_IDENTITY["unit_plan.semantic_render_fingerprint"]
    )


ADJUSTED_ROW = {
    "clip_id": "clip-main",
    "start_frame": 0,
    "duration_frames": 48,
    "audio": {"gain_mb": -600, "muted": False, "fade_in_frames": 12, "fade_out_frames": 12},
}


def _adjusted_plan() -> RenderPlanV1:
    """The planner's own composition with `ADJUSTED_ROW` on `clip-main`, through the decoder."""

    wire = _snapshot_wire()
    clip = next(item for item in wire["clips"] if item["clip_id"] == "clip-main")
    clip["audio"] = dict(cast(dict[str, object], ADJUSTED_ROW["audio"]))
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    snapshot = decode_public_snapshot(wire)
    manifest = _source_manifest(snapshot)
    return plan_render(
        snapshot=snapshot,
        source_manifest=manifest,
        currentness=_currentness(manifest),
        font_facts=_font_facts(snapshot),
    )


def _restated_idempotency_fingerprint(
    plan: RenderPlanV1, table: list[dict[str, object]] | None
) -> str:
    """A plan's idempotency material restated from its own fields, with `table` under its key."""

    material: dict[str, object] = {
        "schema_version": plan.schema_version,
        "snapshot_revision": plan.snapshot_revision,
        "snapshot_fingerprint": plan.snapshot_fingerprint,
        "source_manifest_fingerprint": plan.source_manifest_fingerprint,
        "font_manifest_fingerprint": plan.font_manifest_fingerprint,
        "font_package_fingerprint": plan.font_package_fingerprint,
        "font_facts_fingerprint": plan.font_facts_fingerprint,
        "renderer_capability_profile_fingerprint": plan.renderer_capability_profile_fingerprint,
        "resolved_operation_chunk_fingerprints": [
            chunk.chunk_fingerprint for chunk in plan.resolved_operations
        ],
        "output_profile": plan.output_profile.to_wire(),
        "source_currentness_claim": plan.source_currentness_claim.to_wire(),
        "limits": plan.limits.to_wire(),
        "emits_audio_stream": plan.emits_audio_stream,
        "unavailable_disposition": plan.unavailable_disposition,
    }
    if table is not None:
        material["clip_audio"] = table
    return canonical_fingerprint(material)


def test_a_decoded_adjusted_snapshot_plans_its_table_and_binds_it() -> None:
    plan = _adjusted_plan()
    assert plan.clip_audio == (
        PlanClipAudio(
            clip_id="clip-main",
            start_frame=0,
            duration_frames=48,
            audio=ClipAudio(-600, False, 12, 12),
        ),
    )
    assert plan.to_wire()["clip_audio"] == [ADJUSTED_ROW]
    # The idempotency material binds the table under its key; the plan without adjustments keeps
    # its material without the key.
    assert plan.idempotency_fingerprint == _restated_idempotency_fingerprint(plan, [ADJUSTED_ROW])
    assert plan.idempotency_fingerprint != _restated_idempotency_fingerprint(plan, None)
    base = _plan()
    assert base.idempotency_fingerprint == _restated_idempotency_fingerprint(base, None)
    assert plan.idempotency_fingerprint != base.idempotency_fingerprint


def _bound_graph(
    tmp_path: Path, clip_audio: tuple[PlanClipAudio, ...]
) -> tuple[str, str, dict[str, str]]:
    """The program of the bound video plan with `clip_audio`, its fingerprint, and the identity of
    the plan it was made from."""

    from test_m25_render_job_leases import acquire, video_bound

    from comfyui_h3_context.adapters.authoring_render_executor import prepare_render_assets

    (tmp_path / "source").mkdir()
    _, receipt, bound = video_bound(tmp_path / "source")
    plan = replace(bound.plan, clip_audio=clip_audio)
    sources = acquire(bound)
    (tmp_path / "store").mkdir()
    store = stores().RenderOutputStore(tmp_path / "store")
    try:
        stage = store.begin("render-" + "d" * 32, "sha256:" + "d" * 64)
        prepared = prepare_render_assets(
            plan=plan, sources=sources, stage=stage, control=process_control()
        )
        return (
            build_render_graph(plan=plan, prepared=prepared).filter_graph,
            render_plan_fingerprint(plan),
            _identity("bound_video_plan", bound.plan),
        )
    finally:
        sources.release()
        receipt.release()
        store.close()


def test_an_unadjusted_program_is_the_base_program_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "current").mkdir()
    (tmp_path / "base").mkdir()
    # The program reads no renderer profile: it is the base's under the current command list too.
    graph, _, _ = _bound_graph(tmp_path / "current", ())
    assert _sha(graph) == BASE_IDENTITY["bound_video_plan.filter_graph"]
    # Under the base's profile the plan it is made from is the base's as well.
    monkeypatch.setattr(render_planner, "NLE_OPERATION_IDS", _base_operation_list())
    graph, _, identity = _bound_graph(tmp_path / "base", ())
    assert identity == {key: BASE_IDENTITY[key] for key in identity}
    assert _sha(graph) == BASE_IDENTITY["bound_video_plan.filter_graph"]


def test_an_adjusted_clip_gets_its_filters_on_its_run_and_nothing_else_changes(
    tmp_path: Path,
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    identity, _, _ = _bound_graph(tmp_path / "a", ())
    row = PlanClipAudio(
        clip_id="clip-main", start_frame=0, duration_frames=48, audio=ClipAudio(-600, False, 12, 12)
    )
    graph, _, _ = _bound_graph(tmp_path / "b", (row,))
    chain = (
        ",volume=0.501187233627,afade=t=in:ss=0:ns=24000:silence=0,afade=t=out:ss=72000:ns=24000"
    )
    # After the run's own trim and timestamps, before its label: the ramps count from the run's
    # first sample.
    assert graph.count(chain) == 1
    assert "asetpts=N/SR/TB" + chain + "[audio0]" in graph
    assert graph.replace(chain, "") == identity


def test_a_table_that_names_a_clip_twice_is_refused(tmp_path: Path) -> None:
    row = PlanClipAudio(
        clip_id="clip-main", start_frame=0, duration_frames=48, audio=ClipAudio(-600, False, 0, 0)
    )
    with pytest.raises(RenderGraphError, match="plan_mismatch"):
        _bound_graph(tmp_path, (row, row))


def test_a_table_row_whose_clip_does_not_cover_its_run_is_refused(tmp_path: Path) -> None:
    row = PlanClipAudio(
        clip_id="clip-main", start_frame=1, duration_frames=48, audio=ClipAudio(-600, False, 0, 0)
    )
    with pytest.raises(RenderGraphError, match="plan_mismatch"):
        _bound_graph(tmp_path, (row,))
