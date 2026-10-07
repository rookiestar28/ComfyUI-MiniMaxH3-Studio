from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from fractions import Fraction
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from deployment_request_doubles import LOOPBACK_HOST, ListenerTransport

import comfyui_h3_context.adapters.av_reconstruction_media as media_module
import comfyui_h3_context.adapters.comfyui_authoring_media_preview as route_module
from comfyui_h3_context.adapters.av_reconstruction_media import (
    AVMediaAdapterError,
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.comfyui_authoring_media_preview import (
    AUTHORING_MEDIA_PREVIEW_AUDIO_HEADER,
    AUTHORING_MEDIA_PREVIEW_ROUTE,
    AuthoringMediaPreviewResult,
    clear_authoring_media_preview_adapter,
    current_authoring_media_preview_adapter,
    publish_authoring_media_preview_adapter,
)
from comfyui_h3_context.adapters.comfyui_authoring_workspace import AuthoringWorkbenchError
from comfyui_h3_context.core.authoring_preview_protocol import (
    AUTHORING_PREVIEW_REQUEST_SCHEMA,
    MAX_AUTHORING_PREVIEW_REQUEST_BYTES,
)
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

FP = "sha256:" + "a" * 64


def test_runtime_adapter_slot_is_exact_identity_and_content_free() -> None:
    adapter = object.__new__(QualifiedAVMediaAdapter)
    other = object.__new__(QualifiedAVMediaAdapter)
    clear_authoring_media_preview_adapter()
    assert current_authoring_media_preview_adapter() is None
    publish_authoring_media_preview_adapter(adapter)
    assert current_authoring_media_preview_adapter() is adapter
    with pytest.raises(RuntimeError, match="already published"):
        publish_authoring_media_preview_adapter(other)
    assert clear_authoring_media_preview_adapter(other) is False
    assert current_authoring_media_preview_adapter() is adapter
    assert clear_authoring_media_preview_adapter(adapter) is True
    assert current_authoring_media_preview_adapter() is None


def test_binary_result_contract_is_closed_and_bounded() -> None:
    result = AuthoringMediaPreviewResult(bytearray(b"normalized-mp4"), "present_bound")
    assert result.take() == bytearray(b"normalized-mp4")
    assert result.audio_disposition == "present_bound"
    assert AUTHORING_MEDIA_PREVIEW_ROUTE == "/h3-context/v1/authoring/media-preview"
    assert AUTHORING_MEDIA_PREVIEW_AUDIO_HEADER == "X-H3-Context-Embedded-Audio"


def _probe(audio: dict[str, object] | None = None) -> dict[str, object]:
    streams: list[dict[str, object]] = [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 512,
            "height": 512,
            "pix_fmt": "yuv420p",
            "avg_frame_rate": "24/1",
        }
    ]
    if audio is not None:
        streams.append(audio)
    return {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "5.000",
        },
        "streams": streams,
    }


def test_preview_probe_normalizes_only_empty_qualified_ffprobe_envelopes() -> None:
    closed = _probe()
    exact_wire = {
        "programs": [],
        "stream_groups": [],
        **closed,
    }

    assert media_module._normalize_preview_probe_wire(exact_wire) == closed
    assert set(media_module._normalize_preview_probe_wire(exact_wire)) == {
        "format",
        "streams",
    }

    for invalid in (
        {**exact_wire, "programs": [{"program_id": 1}]},
        {**exact_wire, "stream_groups": [{}]},
        {**exact_wire, "programs": {}},
        {**exact_wire, "unknown": []},
    ):
        with pytest.raises(AVMediaAdapterError, match="media_output_invalid"):
            media_module._normalize_preview_probe_wire(invalid)


def test_qualified_source_facts_preserve_only_eligible_embedded_audio() -> None:
    eligible = {
        "codec_type": "audio",
        "codec_name": "aac",
        "sample_rate": "48000",
        "channels": 2,
        "channel_layout": "stereo",
    }
    qualified_ffprobe_wire = {**eligible, "avg_frame_rate": "0/0"}
    assert QualifiedAVMediaAdapter._authoring_preview_source_facts(
        _probe(qualified_ffprobe_wire),
        source_fps=24,
        source_start_frame=30,
        frames=60,
    ) == (5000, "present_bound")
    assert QualifiedAVMediaAdapter._authoring_preview_source_facts(
        _probe(eligible), source_fps=24, source_start_frame=30, frames=60
    ) == (5000, "present_bound")
    for invalid_sentinel in ("1/1", "", 0, None):
        assert QualifiedAVMediaAdapter._authoring_preview_source_facts(
            _probe({**eligible, "avg_frame_rate": invalid_sentinel}),
            source_fps=24,
            source_start_frame=30,
            frames=60,
        ) == (5000, "unavailable")
    assert QualifiedAVMediaAdapter._authoring_preview_source_facts(
        _probe({**eligible, "avg_frame_rate": "0/0", "unknown": "value"}),
        source_fps=24,
        source_start_frame=30,
        frames=60,
    ) == (5000, "unavailable")
    assert QualifiedAVMediaAdapter._authoring_preview_source_facts(
        _probe(), source_fps=24, source_start_frame=30, frames=60
    ) == (5000, "absent")
    assert QualifiedAVMediaAdapter._authoring_preview_source_facts(
        _probe({**eligible, "codec_name": "private"}),
        source_fps=24,
        source_start_frame=30,
        frames=60,
    ) == (5000, "unavailable")
    with pytest.raises(AVMediaAdapterError, match="preview_source_unsupported"):
        QualifiedAVMediaAdapter._authoring_preview_source_facts(
            _probe(), source_fps=30, source_start_frame=30, frames=60
        )


def test_authoring_audio_trim_times_are_canonical_ffmpeg_seconds() -> None:
    assert media_module._preview_seconds_expression(0, 24) == "0"
    assert media_module._preview_seconds_expression(24, 24) == "1"
    assert media_module._preview_seconds_expression(1, 24) == "0.041666667"
    assert media_module._preview_seconds_expression(899, 30) == "29.966666667"
    for invalid in ((-1, 24), (1, 0), (1.0, 24), (1, 24.0)):
        with pytest.raises(AVMediaAdapterError, match="preview_source_unsupported"):
            media_module._preview_seconds_expression(*invalid)


def test_full_frame_preview_uses_counted_range_not_rounded_container_duration() -> None:
    value = _probe()
    format_value = value["format"]
    assert isinstance(format_value, dict)
    format_value["duration"] = "15.083333"
    assert QualifiedAVMediaAdapter._authoring_preview_source_facts(
        value, source_fps=24, source_start_frame=0, frames=362, counted_frames=362
    ) == (15083, "absent")
    assert QualifiedAVMediaAdapter._authoring_preview_source_facts(
        value, source_fps=24, source_start_frame=361, frames=1, counted_frames=362
    ) == (15083, "absent")
    for start, frames in ((0, 363), (362, 1), (361, 2)):
        with pytest.raises(AVMediaAdapterError, match="preview_source_unsupported"):
            QualifiedAVMediaAdapter._authoring_preview_source_facts(
                value, source_fps=24, source_start_frame=start, frames=frames, counted_frames=362
            )
    for count in (True, 0, -1, 1801, "362", None):
        with pytest.raises(AVMediaAdapterError, match="preview_source_unsupported"):
            QualifiedAVMediaAdapter._authoring_preview_source_facts(
                value,
                source_fps=24,
                source_start_frame=0,
                frames=362,
                counted_frames=cast(Any, count),
            )


def test_counted_frame_probe_is_closed_bounded_and_fails_without_actual_count() -> None:
    decode = getattr(media_module, "_authoring_preview_frame_count_wire", None)
    assert callable(decode), "closed decoded-frame projection is missing"
    assert decode({"streams": [{"nb_read_frames": "362"}]}) == 362
    assert (
        decode({"programs": [], "stream_groups": [], "streams": [{"nb_read_frames": "512"}]}) == 512
    )
    invalid_wires: tuple[dict[str, object], ...] = (
        {"streams": []},
        {"streams": [{"nb_read_frames": "1"}, {"nb_read_frames": "2"}]},
        {"streams": [{"nb_read_frames": "N/A"}]},
        {"streams": [{"nb_read_frames": 362}]},
        {"streams": [{"nb_read_frames": "0"}]},
        {"streams": [{"nb_read_frames": "-1"}]},
        {"streams": [{"nb_read_frames": "1801"}]},
        {"streams": [{"nb_read_frames": "362.0"}]},
        {"streams": [{"nb_read_frames": "362", "unknown": 1}]},
        {"streams": [{"nb_frames": "362"}]},
        {"streams": [{"nb_read_frames": "000362"}]},
        {"streams": [{"nb_read_frames": "362"}], "unknown": []},
        {"streams": [{"nb_read_frames": "362"}], "programs": [{}]},
        {"streams": [{"nb_read_frames": "362"}], "stream_groups": {}},
    )
    for wire in invalid_wires:
        with pytest.raises(AVMediaAdapterError, match="preview_source_unsupported"):
            decode(wire)


def test_authoring_preview_keeps_qualified_output_fractional_milliseconds() -> None:
    value: dict[str, object] = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "15.066667"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 64,
                "height": 64,
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "15/1",
            }
        ],
    }
    assert QualifiedAVMediaAdapter._validate_authoring_preview_output(
        value, audio_disposition="absent"
    ) == Fraction(15066667, 1000)


def test_executable_preview_refuses_source_profile_before_counting_decoded_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"synthetic-private-video")
    adapter = object.__new__(QualifiedAVMediaAdapter)
    adapter._scratch_root = (tmp_path / "scratch").resolve()
    adapter._scratch_root.mkdir()
    adapter._process_slots = threading.BoundedSemaphore(1)
    wire = _probe()
    format_value = wire["format"]
    assert isinstance(format_value, dict)
    format_value["duration"] = "31.000000"
    monkeypatch.setattr(adapter, "_preview_probe", lambda *args, **kwargs: wire)

    def forbidden_count(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("an unqualified source must not enter whole-file decode")

    monkeypatch.setattr(adapter, "_authoring_preview_frame_count", forbidden_count)
    with pytest.raises(AVMediaAdapterError, match="preview_source_too_long"):
        adapter.execute_authoring_preview(
            source_path=source,
            source_start_frame=0,
            frames=1,
            source_fps=24,
            deadline=time.monotonic() + 5.0,
        )
    assert list(adapter._scratch_root.iterdir()) == []


def test_duration_probe_stages_private_source_and_returns_only_bounded_milliseconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic-private-video")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    adapter = object.__new__(QualifiedAVMediaAdapter)
    adapter._scratch_root = scratch
    adapter._process_slots = threading.BoundedSemaphore(1)
    observed_paths: list[Path] = []
    observed_maximum_bytes: list[int] = []

    def probe(
        path: Path,
        *,
        maximum_bytes: int,
        deadline: float,
        cancellation: object | None,
    ) -> dict[str, object]:
        observed_paths.append(path)
        observed_maximum_bytes.append(maximum_bytes)
        assert path != source
        assert path.read_bytes() == source.read_bytes()
        assert deadline > time.monotonic()
        assert cancellation is None
        return _probe()

    monkeypatch.setattr(adapter, "_preview_probe", probe)
    duration = adapter.probe_authoring_source_duration(
        source_path=source,
        deadline=time.monotonic() + 5.0,
    )

    assert duration == 5_000
    assert len(observed_paths) == 1
    assert observed_maximum_bytes == [64 * 1024 * 1024]
    assert list(scratch.iterdir()) == []


def test_real_qualified_video_covers_probe_audio_trim_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    ffprobe_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
    if not ffmpeg_value or not ffprobe_value:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    ffmpeg = Path(ffmpeg_value).resolve(strict=True)
    ffprobe = Path(ffprobe_value).resolve(strict=True)
    source = (tmp_path / "privacy-safe-source.mp4").resolve()
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=24:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "24",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-movflags",
            "+faststart",
            "-shortest",
            "-y",
            str(source),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    scratch = (tmp_path / "scratch").resolve()
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=scratch,
        clock_ms=lambda: int(time.time() * 1000),
    )
    raw_probe_wires: list[dict[str, object]] = []
    ffmpeg_filters: list[str] = []
    original_normalize = media_module._normalize_preview_probe_wire
    original_run_ffmpeg = adapter._run_ffmpeg

    def capture_probe(value: object) -> dict[str, object]:
        raw_probe_wires.append(cast(dict[str, object], value))
        return original_normalize(value)

    def capture_ffmpeg(invocation: Any, cancellation: Any) -> None:
        argv = list(invocation.argv)
        filter_index = argv.index("-filter_complex")
        ffmpeg_filters.append(argv[filter_index + 1])
        original_run_ffmpeg(invocation, cancellation)

    monkeypatch.setattr(media_module, "_normalize_preview_probe_wire", capture_probe)
    monkeypatch.setattr(adapter, "_run_ffmpeg", capture_ffmpeg)

    assert (
        adapter.probe_authoring_source_duration(
            source_path=source,
            deadline=time.monotonic() + 30.0,
        )
        == 4_000
    )
    assert list(scratch.iterdir()) == []

    body, audio_disposition = adapter.execute_authoring_preview(
        source_path=source,
        source_start_frame=24,
        frames=48,
        source_fps=24,
        deadline=time.monotonic() + 30.0,
    )

    source_wire = raw_probe_wires[0]
    assert set(source_wire) == {"format", "programs", "stream_groups", "streams"}
    assert source_wire["programs"] == []
    assert source_wire["stream_groups"] == []
    source_streams = cast(list[dict[str, object]], source_wire["streams"])
    source_audio = next(row for row in source_streams if row["codec_type"] == "audio")
    assert source_audio["avg_frame_rate"] == "0/0"
    assert ffmpeg_filters == [
        "[0:v:0]trim=start_frame=24:end_frame=72,setpts=PTS-STARTPTS,"
        "scale=w='min(320,iw)':h='min(320,ih)':force_original_aspect_ratio=decrease,"
        "fps=15,format=yuv420p[v];"
        "[0:a:0]atrim=start=1:duration=2,asetpts=PTS-STARTPTS,"
        "aformat=sample_rates=48000:channel_layouts=mono,aresample=48000[a]"
    ]
    assert audio_disposition == "present_bound"
    assert 0 < len(body) <= media_module._PREVIEW_MAX_OUTPUT_BYTES
    assert bytes(body[4:8]) == b"ftyp"
    assert list(scratch.iterdir()) == []


class _Routes(list[SimpleNamespace]):
    def post(self, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.append(SimpleNamespace(method="POST", path=path, handler=handler))
            return handler

        return register


class _Response:
    def __init__(self, *, body=b"", status=200, headers=None):  # type: ignore[no-untyped-def]
        self.body = body
        self.status = status
        self.headers = {} if headers is None else headers


class _Content:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def read(self, _limit: int) -> bytes:
        body, self.body = self.body, b""
        return body


class _Headers:
    def __init__(
        self,
        *,
        origins: list[str] | None = None,
        ranges: list[str] | None = None,
    ) -> None:
        self.origins = ["http://127.0.0.1:8188"] if origins is None else origins
        self.ranges = [] if ranges is None else ranges

    def getall(self, name: str, default: list[str]) -> list[str]:
        if name == "Host":
            return [LOOPBACK_HOST]
        if name == "Origin":
            return self.origins
        if name == "Range":
            return self.ranges
        return default


def _request(**overrides: object) -> SimpleNamespace:
    body = json.dumps(
        {
            "schema": AUTHORING_PREVIEW_REQUEST_SCHEMA,
            "requestId": "request-1",
            "workspaceHandle": "authoring-1",
            "referenceRevision": 1,
            "timelineRevision": 1,
            "timelineContentFingerprint": FP,
            "clipId": "clip-1",
        }
    ).encode()
    request = SimpleNamespace(
        content_type="application/json",
        content_length=len(body),
        content=_Content(body),
        headers=_Headers(),
        query_string="",
        transport=ListenerTransport(),
    )
    for name, value in overrides.items():
        setattr(request, name, value)
    return request


def _handler() -> Callable[..., Any]:
    routes = _Routes()
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(Response=_Response)
    with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
        with patch.object(route_module, "_ROUTE_REGISTERED", False):
            assert route_module.ensure_authoring_media_preview_route_registered()
    return cast(Callable[..., Any], routes[0].handler)


def test_route_success_uses_exact_headers_and_late_cas() -> None:
    adapter = object.__new__(QualifiedAVMediaAdapter)
    claim = SimpleNamespace(render=lambda *_args, **_kwargs: (bytearray(b"normalized"), "absent"))

    class _ImmediateExecutor:
        def submit(self, function, *args):  # type: ignore[no-untyped-def]
            future: Future[object] = Future()
            future.set_result(function(*args))
            return future

    route_module.publish_authoring_media_preview_adapter(adapter)
    try:
        with (
            patch.object(route_module, "_EXECUTION_CLAIM", threading.Lock()),
            patch.object(route_module, "_executor", return_value=_ImmediateExecutor()),
            patch.object(route_module, "admit_authoring_media_preview", return_value=claim),
            patch.object(route_module, "authoring_media_preview_is_current", return_value=True),
            patch.object(time, "monotonic", wraps=time.monotonic),
        ):
            response = asyncio.run(_handler()(_request()))
    finally:
        route_module.clear_authoring_media_preview_adapter(adapter)
    assert (response.status, response.body) == (200, bytearray(b"normalized"))
    assert response.headers["Content-Type"] == "video/mp4"
    assert response.headers[AUTHORING_MEDIA_PREVIEW_AUDIO_HEADER] == "absent"
    assert response.headers["Content-Disposition"] == (
        'inline; filename="h3-authoring-preview.mp4"'
    )


def test_route_rejects_unpublished_adapter_before_workspace_lookup() -> None:
    current = route_module.current_authoring_media_preview_adapter()
    if current is not None:
        assert route_module.clear_authoring_media_preview_adapter(current)
    with patch.object(route_module, "admit_authoring_media_preview") as admit:
        response = asyncio.run(_handler()(_request()))
    assert response.status == 422
    assert json.loads(response.body) == {
        "schema": "h3.context.authoring_source_preview.error.v1",
        "requestId": "request-1",
        "reason": "unsupported",
    }
    admit.assert_not_called()


def test_route_published_adapter_reaches_workspace_lookup_before_render() -> None:
    adapter = object.__new__(QualifiedAVMediaAdapter)
    route_module.publish_authoring_media_preview_adapter(adapter)
    try:
        with patch.object(
            route_module,
            "admit_authoring_media_preview",
            side_effect=AuthoringWorkbenchError(404, "workspace_unavailable"),
        ) as admit:
            response = asyncio.run(_handler()(_request()))
    finally:
        route_module.clear_authoring_media_preview_adapter(adapter)
    assert response.status == 404
    assert json.loads(response.body) == {
        "schema": "h3.context.authoring_source_preview.error.v1",
        "requestId": "request-1",
        "reason": "authority_mismatch",
    }
    admit.assert_called_once()


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"content_type": "text/plain"}, 415),
        ({"headers": _Headers(origins=["http://attacker.invalid"])}, 403),
        ({"headers": _Headers(ranges=["bytes=0-1"])}, 400),
        ({"query_string": "range=0-1"}, 400),
        ({"content_length": MAX_AUTHORING_PREVIEW_REQUEST_BYTES + 1}, 413),
    ],
)
def test_route_rejects_hostile_transport_shape_before_lookup(
    overrides: dict[str, object], status: int
) -> None:
    with patch.object(route_module, "admit_authoring_media_preview") as admit:
        response = asyncio.run(_handler()(_request(**overrides)))
    assert response.status == status
    assert json.loads(response.body)["reason"] == "invalid_request"
    admit.assert_not_called()


def test_route_busy_and_late_stale_results_are_distinct_and_content_free() -> None:
    adapter = object.__new__(QualifiedAVMediaAdapter)
    claim = SimpleNamespace()
    occupied = threading.Lock()
    assert occupied.acquire(blocking=False)
    route_module.publish_authoring_media_preview_adapter(adapter)
    try:
        with (
            patch.object(route_module, "_EXECUTION_CLAIM", occupied),
            patch.object(route_module, "admit_authoring_media_preview", return_value=claim),
        ):
            busy = asyncio.run(_handler()(_request()))
        assert busy.status == 429
        assert json.loads(busy.body)["reason"] == "busy"
    finally:
        occupied.release()
        route_module.clear_authoring_media_preview_adapter(adapter)

    class _ImmediateExecutor:
        def submit(self, _function, *_args):  # type: ignore[no-untyped-def]
            future: Future[AuthoringMediaPreviewResult] = Future()
            future.set_result(result)
            return future

    result = AuthoringMediaPreviewResult(bytearray(b"stale-private-body"), "absent")
    route_module.publish_authoring_media_preview_adapter(adapter)
    try:
        with (
            patch.object(route_module, "_EXECUTION_CLAIM", threading.Lock()),
            patch.object(route_module, "_executor", return_value=_ImmediateExecutor()),
            patch.object(route_module, "admit_authoring_media_preview", return_value=claim),
            patch.object(route_module, "authoring_media_preview_is_current", return_value=False),
        ):
            stale = asyncio.run(_handler()(_request()))
    finally:
        route_module.clear_authoring_media_preview_adapter(adapter)
    assert stale.status == 409
    assert json.loads(stale.body)["reason"] == "stale"
    with pytest.raises(RuntimeError, match="already consumed"):
        result.take()


def test_abandoned_worker_discards_result_before_releasing_route_claim() -> None:
    execution_claim = threading.Lock()
    assert execution_claim.acquire(blocking=False)
    future: Future[AuthoringMediaPreviewResult] = Future()
    result = AuthoringMediaPreviewResult(bytearray(b"late"), "absent")
    with patch.object(route_module, "_EXECUTION_CLAIM", execution_claim):
        future.add_done_callback(route_module._finish_abandoned)
        future.set_result(result)
        assert execution_claim.acquire(blocking=False)
        execution_claim.release()
    with pytest.raises(RuntimeError, match="already consumed"):
        result.take()
