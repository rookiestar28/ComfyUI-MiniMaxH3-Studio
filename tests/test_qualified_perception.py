"""Qualified producer source authority, resource lifecycle and timeline regressions."""

from __future__ import annotations

import json
import sys
from copy import copy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from comfyui_h3_context.adapters import perception_host as host
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.normalization import RawContextRequest
from comfyui_h3_context.core.perception_execution import AUDIO_PROFILE, VISUAL_PROFILE
from comfyui_h3_context.core.perception_producer import MediaAdmissionEvidence
from comfyui_h3_context.nodes import (
    H3AudioPerceptionProducerNode,
    H3CrossReferenceProducerNode,
    H3DirectiveAuthorityProducerNode,
    H3EvidenceFusionProducerNode,
    H3FullReferenceTimelineProducerNode,
    H3HardConstraintProducerNode,
    H3IntentGraphProducerNode,
    H3MediaAdmissionProducerNode,
    H3ReferenceRegistryNode,
    H3VisualPerceptionProducerNode,
)


def video_media(*, fps: Decimal = Decimal(2), count: int = 8) -> Any:
    import torch

    return H3MediaAdmissionProducerNode().admit(
        "video",
        "video_1",
        admission_evidence=MediaAdmissionEvidence(
            "b" * 64,
            32,
            32,
            float(Decimal(count) / fps),
            None,
            None,
            "reference",
            0,
        ),
        video=host.DecodedCfrVideo(torch.zeros((count, 32, 32, 3)), fps),
    )[0]


def timeline(
    media: Any, visual: Any, *, constraints_text: str = "", duration_seconds: float = 4.0
) -> Any:
    registry = H3ReferenceRegistryNode().build_registry(videos=[media.runtime_payload])[0]
    constraints, cr = H3HardConstraintProducerNode().produce(dialogue=constraints_text)
    request = RawContextRequest(
        TaskMode.REF2VA,
        "Use this reference.",
        duration_seconds,
        hard_constraints=constraints,
        reference_registry=registry,
    )
    intent, ir = H3IntentGraphProducerNode().produce(request, registry)
    evidence, er = H3EvidenceFusionProducerNode().produce(media)
    cross, xr = H3CrossReferenceProducerNode().produce(registry, media)
    directives, dr = H3DirectiveAuthorityProducerNode().produce(
        request, registry, constraints, cr, intent, ir
    )
    return H3FullReferenceTimelineProducerNode().produce(
        request,
        registry,
        media,
        evidence,
        er,
        cross,
        xr,
        directives,
        dr,
        intent,
        ir,
        visual_result=visual,
    )


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    binding = host.configure_perception_host(
        host.PerceptionHostSettings(
            Path(sys.executable),
            tmp_path,
            tmp_path / "weights",
            tmp_path / "processor",
        )
    )
    monkeypatch.setattr(
        host, "_run_worker", lambda job, binding: ["A red square."] * len(job.get("frames", [0]))
    )
    yield binding
    host.clear_perception_host(binding)


def visual(media: Any) -> Any:
    return H3VisualPerceptionProducerNode().produce(
        media, "ollama", VISUAL_PROFILE, local_service_consent=True
    )[0]


def test_reference_conditioning_discards_the_blue_tail(
    configured: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64
    import io

    import torch
    from PIL import Image

    media = video_media(fps=Decimal(24), count=288)
    media.runtime_payload.images[:124, :, :, 0] = 1
    media.runtime_payload.images[124:, :, :, 2] = 1

    def descriptions(job: Any, binding: Any) -> list[str]:
        colors = []
        for value in job["frames"]:
            pixel = Image.open(io.BytesIO(base64.b64decode(value))).getpixel((0, 0))
            colors.append("A red frame." if pixel[0] > pixel[2] else "A blue frame.")
        return colors

    monkeypatch.setattr(host, "_run_worker", descriptions)
    node = H3VisualPerceptionProducerNode()
    request = RawContextRequest(TaskMode.REF2VA, "Use this reference.", 5.0)
    # Exercise the actual pre-window node too: its discarded midpoint/tail is the causal RED.
    inputs: dict[str, Any] = (
        {"request": request} if "request" in node.INPUT_TYPES()["optional"] else {}
    )
    result = node.produce(media, "ollama", VISUAL_PROFILE, local_service_consent=True, **inputs)[0]
    assert all(frame.frame_index - 1 < 124 for frame in result.analysis.keyframes)
    assert all("blue" not in row.claim.casefold() for row in result.analysis.observations)
    assert result.analysis.conditioning_window.window_frame_count == 124
    assert [frame.frame_index - 1 for frame in result.analysis.keyframes] == [0, 24, 60, 84, 120]
    assert result.analysis.shots[0].end.seconds == Decimal(124) / 24
    assert result.analysis.admitted_frame_count == 288 and result.analysis.frame_rate == 24
    assert torch.all(media.runtime_payload.images[124:, :, :, 2] == 1)


def test_qualified_frames_produce_nonempty_partial_timeline(configured: Any) -> None:
    media = video_media()
    result = visual(media)
    assert [item.start.seconds for item in result.analysis.observations] == [
        Decimal(0),
        Decimal(2),
        Decimal("3.5"),
    ]
    assert all(item.start == item.end for item in result.analysis.observations)
    planned, report = timeline(media, result)
    assert planned.is_valid and planned.timeline.segments
    assert report.disposition.value == "partial"
    assert report.reason_code.value == "qualified_timeline_built"
    assert planned.timeline.source_spans[0].source_start.seconds == 0
    assert planned.timeline.source_spans[0].source_end.seconds == 4
    assert planned.timeline.segments[-1].end.seconds == Decimal(
        str(planned.plan.request.effective_duration_seconds)
    )
    assert all(item.evidence.support.value == "uncertain" for item in result.analysis.observations)
    assert all(item.evidence.confidence is None for item in result.analysis.observations)
    assert "red square" not in json.dumps(result.to_wire()).lower()
    assert "temporary_root" not in repr(result)
    import jsonschema

    schema = json.loads(
        (
            Path(__file__).parents[1] / "governance/contracts/downstream_producer_v1.schema.json"
        ).read_text()
    )
    jsonschema.validate(report.to_wire(), schema)


def test_parent_copy_source_mutation_and_nested_analysis_mutation_are_rejected(
    configured: Any,
) -> None:
    media = video_media()
    result = visual(media)
    with pytest.raises(ContractValidationError):
        result.assert_current(copy(media))
    with pytest.raises(ContractValidationError):
        copy(result).assert_current()
    media.runtime_payload.images[0, 0, 0, 0] = 1
    with pytest.raises(ContractValidationError):
        timeline(media, result)
    media.runtime_payload.images[0, 0, 0, 0] = 0
    object.__setattr__(result.analysis.observations[0].evidence, "claim", "forged")
    with pytest.raises(ContractValidationError):
        result.assert_current()


def test_revocation_rejects_previous_result_and_old_clear_preserves_successor(
    configured: Any, tmp_path: Path
) -> None:
    result = visual(video_media())
    successor = host.configure_perception_host(
        host.PerceptionHostSettings(Path(sys.executable), tmp_path)
    )
    assert host.clear_perception_host(configured) is False
    with pytest.raises(host.PerceptionExecutionError, match="configuration_revoked"):
        result.assert_current()
    assert host.clear_perception_host(successor)


@pytest.mark.parametrize(
    "profile,route,device,consent",
    [
        (VISUAL_PROFILE, "ollama", "auto", False),
        (VISUAL_PROFILE, "specialist", "auto", True),
        (VISUAL_PROFILE, "ollama", "cuda", True),
        (AUDIO_PROFILE, "comfyui_native", "auto", True),
    ],
)
def test_preflight_refuses_wrong_profile_route_device_consent(
    configured: Any, profile: str, route: str, device: str, consent: bool
) -> None:
    assert (
        H3VisualPerceptionProducerNode.VALIDATE_INPUTS(
            profile_id=profile, route=route, device=device, local_service_consent=consent
        )
        is not True
    )


@pytest.mark.parametrize("mutation", ["nan", "overrange", "metadata", "fps", "file"])
def test_unsupported_source_refuses_before_worker(
    configured: Any, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    media = video_media()
    if mutation == "nan":
        media.runtime_payload.images[0, 0, 0, 0] = float("nan")
    elif mutation == "overrange":
        media.runtime_payload.images[0, 0, 0, 0] = 2
    elif mutation == "metadata":
        object.__setattr__(media.admission_evidence, "width_pixels", 64)
    elif mutation == "fps":
        object.__setattr__(media.runtime_payload, "frame_rate", Decimal(120))
    else:
        object.__setattr__(media.runtime_payload, "images", object())
    monkeypatch.setattr(
        host, "_run_worker", lambda *args: pytest.fail("unsupported source reached worker")
    )
    with pytest.raises(ContractValidationError):
        visual(media)


def test_silent_audio_abstains_before_asr(configured: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import torch

    media = H3MediaAdmissionProducerNode().admit(
        "audio",
        "audio_1",
        admission_evidence=MediaAdmissionEvidence(
            "a" * 64,
            None,
            None,
            1.0,
            16000,
            1,
            "reference",
            0,
        ),
        audio={"waveform": torch.zeros((1, 1, 16000)), "sample_rate": 16000},
    )[0]
    monkeypatch.setattr(host, "_run_worker", lambda *args: pytest.fail("silence reached worker"))
    with pytest.raises(host.PerceptionExecutionError, match="empty_audio"):
        H3AudioPerceptionProducerNode().produce(media, "comfyui_native", AUDIO_PROFILE)


def test_busy_admission_refuses_without_creating_files(configured: Any, tmp_path: Path) -> None:
    assert host._ADMISSION.acquire(False)
    try:
        with pytest.raises(host.PerceptionExecutionError, match="execution_busy"):
            # Use the real runner, not the fixture's execution double.
            _REAL_RUNNER({}, configured)
        assert list(tmp_path.iterdir()) == []
    finally:
        host._ADMISSION.release()


_REAL_RUNNER = host._run_worker


def test_unbound_reference_is_filtered_by_timeline_and_tail_mutation_stays_revocable(
    configured: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = video_media(fps=Decimal(24), count=288)
    monkeypatch.setattr(
        host, "_run_worker", lambda job, binding: ["A red frame.", "A blue frame.", "A blue tail."]
    )
    result = visual(media)
    assert result.analysis.conditioning_window is None
    assert [frame.frame_index - 1 for frame in result.analysis.keyframes] == [0, 144, 287]
    planned, report = timeline(media, result, duration_seconds=5.0)
    assert planned.is_valid and report.disposition.value == "partial"
    assert planned.timeline.request.conditioning_window.window_frame_count == 124
    assert planned.timeline.segments[0].video_observation_ids == (
        result.analysis.observations[0].observation_id,
    )
    finding = next(
        item
        for item in planned.diagnostics
        if item.code == "perception_outside_conditioning_window"
    )
    assert finding.severity.value == "warning" and "2 visual observations" in finding.message
    assert any(
        item.code == finding.code and "2 visual observations" in item.message
        for item in planned.plan.limitations
    )
    assert all("blue" not in record.claim.casefold() for record in planned.plan.evidence.records)
    # The audit source is retained, but discarded pixels still revoke the qualified authority.
    assert "blue" in json.dumps(result.analysis.to_wire()).casefold()
    media.runtime_payload.images[250, 0, 0, 2] = 1
    with pytest.raises(ContractValidationError):
        result.assert_current()


def test_window_bound_reference_refuses_a_different_timeline_request(configured: Any) -> None:
    media = video_media(fps=Decimal(24), count=288)
    result = H3VisualPerceptionProducerNode().produce(
        media,
        "ollama",
        VISUAL_PROFILE,
        local_service_consent=True,
        request=RawContextRequest(TaskMode.REF2VA, "Use this reference.", 5.0),
    )[0]
    with pytest.raises(
        ContractValidationError,
        match="perception_window_mismatch: Run visual perception with the same request",
    ):
        timeline(media, result, duration_seconds=4.0)


def test_non_native_rate_warns_and_uses_frame_indices_without_resampling(configured: Any) -> None:
    result = H3VisualPerceptionProducerNode().produce(
        video_media(),
        "ollama",
        VISUAL_PROFILE,
        local_service_consent=True,
        request=RawContextRequest(TaskMode.REF2VA, "Use this reference.", 4.0),
    )[0]
    assert result.analysis.conditioning_window.window_frame_count == 5
    assert [frame.frame_index for frame in result.analysis.keyframes] == [1]
    assert result.analysis.shots[0].end.seconds == Decimal("2.5")
    assert any(
        item.code == "reference_frame_rate_approximate" and item.severity.value == "warning"
        for item in result.analysis.diagnostics
    )


def test_other_mode_and_image_requests_keep_legacy_visual_sampling(configured: Any) -> None:
    import torch

    request = RawContextRequest(TaskMode.T2VA, "Draw shapes.", 5.0)
    result = H3VisualPerceptionProducerNode().produce(
        video_media(), "ollama", VISUAL_PROFILE, local_service_consent=True, request=request
    )[0]
    assert result.analysis.conditioning_window is None
    assert [frame.frame_index - 1 for frame in result.analysis.keyframes] == [0, 4, 7]
    image = H3MediaAdmissionProducerNode().admit(
        "image",
        "image_1",
        declared_source_fingerprint="b" * 64,
        width_pixels=32,
        height_pixels=32,
        image=torch.zeros((1, 32, 32, 3)),
    )[0]
    result = H3VisualPerceptionProducerNode().produce(
        image,
        "ollama",
        VISUAL_PROFILE,
        local_service_consent=True,
        request=RawContextRequest(TaskMode.REF2VA, "Use this reference.", 5.0),
    )[0]
    assert result.analysis.conditioning_window is None
    assert result.analysis.admitted_frame_count == 1 and result.analysis.frame_rate == 0
    assert [frame.frame_index for frame in result.analysis.keyframes] == [1]
    assert not result.analysis.shots and not result.analysis.diagnostics


@pytest.mark.parametrize("count,reason", [(4, "reference_too_short"), (301, "unsupported_media")])
def test_windowing_does_not_bypass_whole_media_admission(
    configured: Any, monkeypatch: pytest.MonkeyPatch, count: int, reason: str
) -> None:
    monkeypatch.setattr(
        host, "_run_worker", lambda *args: pytest.fail("refused reference reached worker")
    )
    with pytest.raises(host.PerceptionExecutionError, match=reason):
        H3VisualPerceptionProducerNode().produce(
            video_media(fps=Decimal(24), count=count),
            "ollama",
            VISUAL_PROFILE,
            local_service_consent=True,
            request=RawContextRequest(TaskMode.REF2VA, "Use this reference.", 5.0),
        )


def test_visual_window_contract_is_closed_and_authority_pins_nested_metadata(
    configured: Any,
) -> None:
    import jsonschema

    result = H3VisualPerceptionProducerNode().produce(
        video_media(fps=Decimal(24), count=288),
        "ollama",
        VISUAL_PROFILE,
        local_service_consent=True,
        request=RawContextRequest(TaskMode.REF2VA, "Use this reference.", 5.0),
    )[0]
    schema = json.loads(
        (
            Path(__file__).parents[1] / "governance/contracts/video_analysis_v1.schema.json"
        ).read_text()
    )
    jsonschema.validate(result.analysis.to_wire(), schema)
    object.__setattr__(result.analysis.conditioning_window, "sample_indices", (0, 120))
    with pytest.raises(ContractValidationError):
        result.assert_current()


@pytest.mark.parametrize("bound,frames", [(None, 1), (True, 1), (4, 1), (5, 6)])
def test_parent_and_worker_reject_job_bound_drift(
    configured: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bound: int | None, frames: int
) -> None:
    from comfyui_h3_context.adapters import perception_worker as worker

    assert host.MAX_VISUAL_DESCRIPTIONS == worker.MAX_VISUAL_DESCRIPTIONS == 5
    job: dict[str, Any] = {
        "profile_id": VISUAL_PROFILE,
        "max_visual_descriptions": bound,
        "frames": ["synthetic"] * frames,
    }
    with pytest.raises(host.PerceptionExecutionError, match="input_budget"):
        _REAL_RUNNER(job, configured)
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(worker, "_ollama_identity", lambda: None)
    monkeypatch.setattr(
        worker,
        "_http",
        lambda path, body=None: (
            {"models": []} if path == "/api/ps" else pytest.fail("unbounded job reached model")
        ),
    )
    with pytest.raises(worker.Refusal, match="input_budget|unsupported_media"):
        worker._visual(job)


def test_worker_accepts_the_measured_five_frames_with_per_call_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters import perception_worker as worker

    calls = []
    monkeypatch.setattr(worker, "_ollama_identity", lambda: None)

    def http(path: str, body: Any = None) -> Any:
        if path == "/api/ps":
            return {"models": []}
        calls.append(body)
        return {
            "done": True,
            "done_reason": "stop",
            "message": {"content": '{"description":"A red square."}'},
        }

    monkeypatch.setattr(worker, "_http", http)
    assert len(worker._visual({"frames": ["synthetic"] * 5, "max_visual_descriptions": 5})) == 5
    assert len(calls) == 5 and all(call["keep_alive"] == 0 for call in calls)


@pytest.mark.parametrize("description_count,accepted", [(5, True), (6, False)])
def test_parent_accepts_five_and_refuses_six_descriptions_with_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, description_count: int, accepted: bool
) -> None:
    import subprocess

    fixture = tmp_path / "worker_fixture.py"
    fixture.write_text(
        "print("
        + repr(json.dumps({"ok": True, "descriptions": ["A red square."] * description_count}))
        + ")\n",
        encoding="utf-8",
    )
    real_popen = subprocess.Popen

    def spawn(argv: list[str], **kwargs: Any) -> Any:
        argv[2] = str(fixture)
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    owned = tmp_path / "owned"
    binding = host.configure_perception_host(
        host.PerceptionHostSettings(Path(sys.executable), owned)
    )
    try:
        job = {
            "profile_id": VISUAL_PROFILE,
            "frames": ["synthetic"] * 5,
            "max_visual_descriptions": 5,
        }
        if accepted:
            assert len(_REAL_RUNNER(job, binding)) == 5
        else:
            with pytest.raises(host.PerceptionExecutionError, match="invalid_runtime_response"):
                _REAL_RUNNER(job, binding)
        assert not list(owned.iterdir())
    finally:
        host.clear_perception_host(binding)


def test_callback_revocation_runs_outside_lock(tmp_path: Path) -> None:
    def cancelled() -> bool:
        assert host.clear_perception_host(binding)
        return False

    binding = host.configure_perception_host(
        host.PerceptionHostSettings(Path(sys.executable), tmp_path, cancelled=cancelled)
    )
    with pytest.raises(host.PerceptionExecutionError, match="configuration_revoked"):
        host._lease(binding)


def test_mutated_configuration_cannot_reuse_issued_authority(
    configured: Any, tmp_path: Path
) -> None:
    result = visual(video_media())
    object.__setattr__(configured.settings, "temporary_root", tmp_path / "replacement")
    assert not host.perception_configured(VISUAL_PROFILE)
    with pytest.raises(host.PerceptionExecutionError, match="configuration_revoked"):
        result.assert_current()


@pytest.mark.parametrize("fault", ["version", "model", "post_version", "post_model", "busy", "eos"])
def test_visual_worker_refuses_runtime_drift_and_incomplete_output(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from comfyui_h3_context.adapters import perception_worker as worker

    versions = tags = 0

    def http(path: str, body: Any = None) -> dict[str, Any]:
        nonlocal versions, tags
        if path == "/api/version":
            versions += 1
            drift = fault == "version" or (fault == "post_version" and versions == 2)
            return {"version": "other" if drift else worker.OLLAMA_VERSION}
        if path == "/api/tags":
            tags += 1
            drift = fault == "model" or (fault == "post_model" and tags == 2)
            return {
                "models": [{"name": worker.MODEL, "digest": "x" if drift else worker.MODEL_SHA}]
            }
        if path == "/api/ps":
            return {"models": [{"name": "another-owner"}] if fault == "busy" else []}
        return {
            "done": True,
            "done_reason": "length" if fault == "eos" else "stop",
            "message": {"content": '{"description":"A red square."}'},
        }

    monkeypatch.setattr(worker, "_http", http)
    reason = (
        "runtime_busy"
        if fault == "busy"
        else "incomplete_output"
        if fault == "eos"
        else "runtime_drift"
    )
    with pytest.raises(worker.Refusal, match=reason):
        worker._visual(
            {"frames": ["synthetic"], "max_visual_descriptions": worker.MAX_VISUAL_DESCRIPTIONS}
        )


@pytest.mark.parametrize(
    "tokens,accepted",
    [([1, 2, 9], True), ([1, 2], False), ([1] * 99 + [9], True), ([1] * 100 + [9], False)],
)
def test_asr_completion_checks_raw_generation_eos(tokens: list[int], accepted: bool) -> None:
    from types import SimpleNamespace

    import torch

    from comfyui_h3_context.adapters.perception_worker import Refusal, _completed_asr_sequences

    sequences = torch.tensor([tokens])
    output = SimpleNamespace(sequences=sequences)
    if accepted:
        assert _completed_asr_sequences(output, 9) is sequences
    else:
        with pytest.raises(Refusal, match="incomplete_output"):
            _completed_asr_sequences(output, 9)


def test_audio_worker_rejects_version_drift_before_import_or_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters import perception_worker as worker

    monkeypatch.setattr(worker, "version", lambda name: "drift")
    monkeypatch.setattr(worker, "import_module", lambda name: pytest.fail("drift reached runtime"))
    with pytest.raises(worker.Refusal, match="runtime_drift"):
        worker._audio({})


def test_manual_dialogue_is_preserved_without_asr_promotion(configured: Any) -> None:
    media = video_media()
    planned, _ = timeline(media, visual(media), constraints_text="Keep these exact words.")
    assert planned.plan.hard_constraints.constraints[0].text == "Keep these exact words."


@pytest.mark.parametrize("description", [[], [""], ["A"] * 4])
def test_empty_or_malformed_output_cannot_build_analysis(
    configured: Any, monkeypatch: pytest.MonkeyPatch, description: list[str]
) -> None:
    monkeypatch.setattr(host, "_run_worker", lambda *args: description)
    with pytest.raises(ContractValidationError):
        visual(video_media())


@pytest.mark.parametrize("description", ["", "   ", "x" * 513])
def test_visual_worker_keeps_finite_output_refusal(
    monkeypatch: pytest.MonkeyPatch, description: str
) -> None:
    from comfyui_h3_context.adapters import perception_worker as worker

    monkeypatch.setattr(worker, "_ollama_identity", lambda: None)
    monkeypatch.setattr(
        worker,
        "_http",
        lambda path, body=None: (
            {"models": []}
            if path == "/api/ps"
            else {
                "done": True,
                "done_reason": "stop",
                "message": {"content": json.dumps({"description": description})},
            }
        ),
    )
    with pytest.raises(worker.Refusal, match="empty_or_unbounded_output"):
        worker._visual(
            {"frames": ["synthetic"], "max_visual_descriptions": worker.MAX_VISUAL_DESCRIPTIONS}
        )


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("timeout", "execution_timeout"),
        ("rss", "memory_budget"),
        ("output", "output_budget"),
        ("cancel", "execution_cancelled"),
    ],
)
def test_real_owned_child_is_reaped_and_temp_removed(
    fault: str, reason: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import time

    fixture = tmp_path / "worker_fixture.py"
    fixture.write_text(
        "import time\nprint('x'*100000, flush=True)\ntime.sleep(5)\n"
        if fault == "output"
        else "import time\ntime.sleep(5)\n",
        encoding="utf-8",
    )
    owned = tmp_path / "owned"
    real_popen = subprocess.Popen
    children = []

    def spawn(argv: list[str], **kwargs: Any) -> Any:
        argv[2] = str(fixture)
        process = real_popen(argv, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    if fault == "timeout":
        monkeypatch.setattr(host, "MAX_SECONDS", 0.1)
    if fault == "rss":
        monkeypatch.setattr(host, "MAX_RSS", 1)
    deadline = time.monotonic() + 0.2
    binding = host.configure_perception_host(
        host.PerceptionHostSettings(
            Path(sys.executable),
            owned,
            cancelled=lambda: fault == "cancel" and time.monotonic() > deadline,
        )
    )
    try:
        with pytest.raises(host.PerceptionExecutionError, match=reason):
            _REAL_RUNNER({}, binding)
        assert len(children) == 1 and children[0].poll() is not None
        assert not list(owned.iterdir())
        assert host._ADMISSION.acquire(False)
        host._ADMISSION.release()
    finally:
        host.clear_perception_host(binding)
