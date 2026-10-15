"""The continuation route study canaries: gating, the verified buffer store, and route geometry."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from typing import Any

import pytest

torch = pytest.importorskip("torch")

from comfyui_h3_context import sequence_continuation_canary as canary  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def owned_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "canary"
    root.mkdir()
    (root / canary.CANARY_MARKER_NAME).write_text(canary.CANARY_MARKER_PAYLOAD, encoding="utf-8")
    monkeypatch.setenv(canary.CANARY_FLAG, "1")
    monkeypatch.setenv(canary.CANARY_ROOT_VARIABLE, str(root))
    return root


def _probe(enabled: bool) -> list[str]:
    root = str(ROOT).replace("\\", "\\\\")
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, "{root}")
        import comfyui_h3_context as package
        print("\\n".join(sorted(package.NODE_CLASS_MAPPINGS)))
        """
    )
    env = {key: value for key, value in os.environ.items() if key != canary.CANARY_FLAG}
    if enabled:
        env[canary.CANARY_FLAG] = "1"
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


def test_registration_is_absent_without_the_flag_and_complete_with_it() -> None:
    normal = _probe(enabled=False)
    gated = _probe(enabled=True)
    assert not set(canary.CANARY_NODES) & set(normal)
    assert set(gated) - set(normal) == set(canary.CANARY_NODES)
    for node in canary.CANARY_NODES.values():
        assert node.EXPERIMENTAL and node.CATEGORY == "h3_context/internal"
        assert node.__h3_context_node_id__ in canary.CANARY_DISPLAY_NAMES


def test_an_unowned_root_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(canary.CANARY_FLAG, raising=False)
    with pytest.raises(canary.SequenceContinuationCanaryError, match="not enabled"):
        canary._owned_root()
    monkeypatch.setenv(canary.CANARY_FLAG, "1")
    monkeypatch.delenv(canary.CANARY_ROOT_VARIABLE, raising=False)
    with pytest.raises(canary.SequenceContinuationCanaryError, match="not configured"):
        canary._owned_root()
    monkeypatch.setenv(canary.CANARY_ROOT_VARIABLE, str(tmp_path))
    with pytest.raises(canary.SequenceContinuationCanaryError, match="not explicitly owned"):
        canary._owned_root()
    (tmp_path / canary.CANARY_MARKER_NAME).write_text("other\n", encoding="utf-8")
    with pytest.raises(canary.SequenceContinuationCanaryError, match="marker is invalid"):
        canary._owned_root()


def test_buffers_round_trip_exactly_and_refuse_tampering_and_overwrite(owned_root: Path) -> None:
    value = torch.arange(24, dtype=torch.float32).reshape(1, 2, 3, 4) / 7
    header = canary.write_buffer(owned_root, "latent-tails", "seg-01", "video", value)
    assert header["shape"] == [1, 2, 3, 4] and header["dtype"] == "float32"
    assert torch.equal(canary.read_buffer(owned_root, "latent-tails", "seg-01", "video"), value)
    with pytest.raises(FileExistsError):
        canary.write_buffer(owned_root, "latent-tails", "seg-01", "video", value)
    raw = owned_root / "latent-tails" / "seg-01" / "video.raw"
    data = bytearray(raw.read_bytes())
    data[0] ^= 1
    raw.write_bytes(bytes(data))
    with pytest.raises(canary.SequenceContinuationCanaryError, match="does not verify"):
        canary.read_buffer(owned_root, "latent-tails", "seg-01", "video")
    for key in ("", "Upper", "../escape", "a" * 65):
        with pytest.raises(canary.SequenceContinuationCanaryError, match="key is invalid"):
            canary.write_buffer(owned_root, "latent-tails", key, "video", value)
    with pytest.raises(canary.SequenceContinuationCanaryError, match="not admitted"):
        canary.write_buffer(owned_root, "latent-tails", "seg-02", "x", value.double())


def test_the_pin_window_is_twelve_tokens_thirty_nine_frames_sixty_five_steps() -> None:
    starts = canary.token_frame_starts()
    assert starts == [0, 1, 5, 9, 13, 17, 18, 22, 26, 30, 34, 35]
    assert starts[-1] + canary.FRAME_PER_TOKEN[11 % 5] == canary.PIN_FRAMES
    assert canary.audio_step_at_frame(canary.PIN_FRAMES) == canary.PIN_AUDIO_STEPS
    assert canary.trim_extents(39, 153) == (52_000, 204_000)
    assert canary.trim_extents(0, 153) == (0, 204_000)


def test_hard_and_tapered_masks_preserve_exactly_the_pin_window() -> None:
    video, audio = canary.mask_values(57, 320, taper=False)
    assert video == [0.0] * 12 + [1.0] * 45
    assert audio == [0.0] * 65 + [1.0] * 255
    video, audio = canary.mask_values(57, 320, taper=True)
    assert video[:8] == [0.0] * 8
    assert video[8:12] == pytest.approx([0.1, 0.325, 0.55, 0.775])
    assert video[12:] == [1.0] * 45
    # Token 8 starts at frame 26 (step 43); the taper ends exactly at the window's last step.
    assert audio[:43] == [0.0] * 43 and audio[43] == pytest.approx(0.1)
    assert audio[64] == pytest.approx(0.775) and audio[65:] == [1.0] * 255
    with pytest.raises(canary.SequenceContinuationCanaryError):
        canary.mask_values(11, 320, taper=False)


def _av(tokens: int = 57, steps: int = 320) -> tuple[Any, Any]:
    return torch.zeros(1, 24, tokens, 30, 54), torch.zeros(1, 32, 2, steps)


def test_head_write_and_dense_masks_have_the_host_layout() -> None:
    video, audio = _av()
    tail_video, tail_audio = canary.tail_of(
        torch.ones(1, 24, 57, 30, 54), torch.ones(1, 32, 2, 320)
    )
    assert tail_video.shape == (1, 24, 12, 30, 54) and tail_audio.shape == (1, 32, 2, 65)
    new_video, new_audio = canary.write_head(video, audio, tail_video, tail_audio)
    assert new_video[:, :, :12].eq(1).all() and new_video[:, :, 12:].eq(0).all()
    assert new_audio[..., :65].eq(1).all() and new_audio[..., 65:].eq(0).all()
    assert video.eq(0).all(), "the anchor latent must not be modified in place"
    video_mask, audio_mask = canary.mask_tensors(video, audio, taper=False)
    assert video_mask.shape == (1, 1, 57, 30, 54) and audio_mask.shape == (1, 1, 2, 320)
    assert video_mask[:, :, :12].eq(0).all() and video_mask[:, :, 12:].eq(1).all()
    with pytest.raises(canary.SequenceContinuationCanaryError, match="geometry"):
        canary.write_head(video, audio, tail_video[..., :10], tail_audio)


def test_keyframe_routes_place_entries_on_the_target_timeline() -> None:
    tail_video, tail_audio = torch.ones(1, 24, 12, 30, 54), torch.ones(1, 32, 2, 65)
    assert canary.keyframes_for("r_l", tail_video, tail_audio) == []
    (single,) = canary.keyframes_for("r_k1", tail_video, tail_audio)
    assert single["resolved_frame_index"] == 0 and single["latent"].shape[2] == 12
    assert single["audio_latent"].shape[-1] == 65
    entries = canary.keyframes_for("r_k12", tail_video, tail_audio)
    assert [entry["resolved_frame_index"] for entry in entries] == canary.token_frame_starts()
    assert all(entry["latent"].shape[2] == 1 for entry in entries)
    # The audio anchor stays one aligned block: only the frame-0 entry carries it.
    assert [("audio_latent" in entry) for entry in entries] == [True] + [False] * 11
    assert entries[0]["audio_latent"].shape[-1] == 65


def _nested_module() -> types.ModuleType:
    module = types.ModuleType("comfy.nested_tensor")

    class NestedTensor:
        is_nested = True

        def __init__(self, tensors: tuple[Any, ...]) -> None:
            self.tensors = list(tensors)

    module.NestedTensor = NestedTensor  # type: ignore[attr-defined]
    return module


def _helpers_module() -> types.ModuleType:
    module = types.ModuleType("node_helpers")

    def conditioning_set_values(conditioning: Any, values: dict[str, Any]) -> Any:
        return [[item[0], {**item[1], **values}] for item in conditioning]

    module.conditioning_set_values = conditioning_set_values  # type: ignore[attr-defined]
    return module


def test_capture_then_compose_applies_each_route(
    owned_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = _nested_module()
    monkeypatch.setitem(sys.modules, "comfy", types.ModuleType("comfy"))
    monkeypatch.setitem(sys.modules, "comfy.nested_tensor", nested)
    monkeypatch.setitem(sys.modules, "node_helpers", _helpers_module())
    sampled = {"samples": nested.NestedTensor(_av()), "batch_index": [0]}
    sampled["samples"].tensors[0][:, :, -12:] = 2.0
    capture = canary.H3SequenceContinuationLatentTailCaptureCanaryNode()
    out = capture.execute(sampled, "a-01")
    assert out["result"][0] is sampled
    anchor = {"samples": nested.NestedTensor(_av())}
    positive: list[list[Any]] = [["text", {"minimax_keyframes": [{"resolved_frame_index": 4}]}]]
    compose = canary.H3SequenceContinuationTailComposeCanaryNode()

    keyed, latent = compose.execute(positive, anchor, "a-01", "r_k1")
    assert latent is anchor and len(keyed[0][1]["minimax_keyframes"]) == 2
    assert positive[0][1]["minimax_keyframes"] == [{"resolved_frame_index": 4}]

    unkeyed, masked = compose.execute(positive, anchor, "a-01", "r_l")
    assert unkeyed is positive and masked is not anchor
    assert masked["samples"].tensors[0][:, :, :12].eq(2.0).all()
    assert masked["noise_mask"].tensors[0][:, :, :12].eq(0).all()
    assert anchor["samples"].tensors[0].eq(0).all() and "noise_mask" not in anchor

    both, tapered = compose.execute(positive, anchor, "a-01", "r_c_taper")
    assert len(both[0][1]["minimax_keyframes"]) == 2
    assert float(tapered["noise_mask"].tensors[0][0, 0, 11, 0, 0]) == pytest.approx(0.775)
    with pytest.raises(canary.SequenceContinuationCanaryError, match="unknown"):
        compose.execute(positive, anchor, "a-01", "r_x")


def _decoded(frames: int, seconds_samples: int) -> tuple[Any, dict[str, Any]]:
    images = torch.linspace(0, 1, frames).reshape(frames, 1, 1, 1).expand(frames, 4, 6, 3)
    waveform = torch.arange(seconds_samples, dtype=torch.float32).reshape(1, 1, -1).expand(1, 2, -1)
    return images.contiguous(), {"waveform": waveform.contiguous(), "sample_rate": 32_000}


def test_window_clip_and_trim_are_sample_exact(owned_root: Path) -> None:
    images, audio = _decoded(192, 256_000)
    window = canary.H3SequenceContinuationTailWindowCanaryNode()
    window.execute(images, audio, "w-01", 153)
    clip_images, clip_audio = canary.H3SequenceContinuationTailClipCanaryNode().execute("w-01")
    assert clip_images.shape == (39, 4, 6, 3)
    assert clip_audio["waveform"].shape == (1, 2, 52_000)
    # The window ends at frame 153 = sample 204,000, so its first sample is 152,000.
    assert float(clip_audio["waveform"][0, 0, 0]) == 152_000.0
    assert float(clip_audio["waveform"][0, 0, -1]) == 203_999.0
    expected = (images[114:153] * 255).round() / 255
    assert torch.allclose(clip_images, expected, atol=1e-6)

    trimmed_images, trimmed_audio = canary.H3SequenceContinuationHeadTrimCanaryNode().execute(
        images, audio, 39, 153, "u-01"
    )
    assert trimmed_images.shape[0] == 153 and torch.equal(trimmed_images[0], images[39])
    assert trimmed_audio["waveform"].shape[-1] == 204_000
    assert float(trimmed_audio["waveform"][0, 0, 0]) == 52_000.0
    retained = canary.read_buffer(owned_root, "untrimmed", "u-01", "audio")
    assert retained.shape == (2, 256_000)
    with pytest.raises(canary.SequenceContinuationCanaryError, match="too few decoded frames"):
        canary.H3SequenceContinuationHeadTrimCanaryNode().execute(images, audio, 39, 154, "")
    with pytest.raises(canary.SequenceContinuationCanaryError, match="beyond the shown"):
        window.execute(images[:100], audio, "w-02", 153)


def _layout_model(*, ref_shifts_cursor: bool) -> types.SimpleNamespace:
    """A minimal stand-in that reproduces the host's documented placement rules."""

    spans = (1, 4, 4, 4, 4)
    rescale = 5.0 / 3.0

    class PackedLayout:
        def __init__(
            self,
            text_len: int,
            latent_t: int,
            h: int,
            w: int,
            audio_t: int,
            keyframes: Any,
            refs: Any,
        ) -> None:
            rows = (h // 2) * (w // 2)
            cursor = float(text_len) + (len(refs) if ref_shifts_cursor else 0)
            times: list[float] = [float(i) for i in range(text_len)]
            segments = []
            offset = text_len
            for entry in keyframes:
                start = cursor + rescale * entry["resolved_frame_index"]
                if entry.get("latent") is not None:
                    tokens = entry["latent"].shape[2]
                    for token in range(tokens):
                        times += [start + rescale * sum(spans[k % 5] for k in range(token))] * rows
                    segments.append((offset, offset + tokens * rows, "cond"))
                    offset += tokens * rows
                if entry.get("audio_latent") is not None:
                    steps = entry["audio_latent"].shape[-1]
                    times += [start + j for j in range(steps)] * 2
                    segments.append((offset, offset + steps * 2, "cond_audio"))
                    offset += steps * 2
            self.position_ids = torch.tensor(times, dtype=torch.float64).reshape(-1, 1)
            self.segments = segments

    return types.SimpleNamespace(
        FRAME_PER_TOKEN=spans, FRAME_RESCALE=rescale, PackedLayout=PackedLayout
    )


def test_layout_probe_detects_a_host_that_moves_the_offset() -> None:
    facts = canary.layout_facts(_layout_model(ref_shifts_cursor=True), torch)
    assert facts["layout_behaviour"] is True
    broken = canary.layout_facts(_layout_model(ref_shifts_cursor=False), torch)
    assert broken["reference_moves_cursor_not_offset"] is False
    assert broken["layout_behaviour"] is False
