"""M10-02 media admission, probe, timestamp, and subprocess-boundary tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from comfyui_h3_context.adapters.media_subprocess import (
    MediaProcessInvocation,
    ProcessCapture,
    ProcessStatus,
    SubprocessMediaRunner,
    build_ffmpeg_invocation,
    build_ffprobe_invocation,
    create_output_lease,
)
from comfyui_h3_context.core.contracts import MediaKind
from comfyui_h3_context.core.errors import MediaAdmissionError, MediaProcessError
from comfyui_h3_context.core.media_admission import (
    MEDIA_ADMISSION_SCHEMA,
    CanonicalMediaBatch,
    DecodedAudioSegment,
    DecodedFrameArtifact,
    MediaAdmissionRequest,
    MediaAllowlist,
    MediaLimits,
    PreprocessingSpec,
    PresentationTimestamp,
    ProbeManifest,
    RedirectTrace,
    SourceIdentity,
    admit_media_source,
    parse_ffprobe_manifest,
    validate_media_batch_limits,
)
from comfyui_h3_context.core.security import (
    MediaSource,
    MediaSourceKind,
    MediaTransferPolicy,
    MediaTransferTimeouts,
)

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(letter: str) -> str:
    return "sha256:" + letter * 64


def limits() -> MediaLimits:
    return MediaLimits(
        max_bytes=1_000_000,
        max_duration_seconds=Decimal("60"),
        max_width=4096,
        max_height=4096,
        max_frame_rate=Decimal("120"),
        max_frames=512,
        max_sample_rate=96_000,
        max_channels=8,
        max_references=4,
        max_temp_bytes=2_000_000,
        max_decoded_bytes=4_000_000,
        max_probe_stdout_bytes=64_000,
        max_probe_stderr_bytes=8_000,
        max_wall_time_seconds=Decimal("10"),
        max_redirects=2,
    )


def allowlist() -> MediaAllowlist:
    return MediaAllowlist(
        protocols=("file", "https"),
        containers=("mp4", "mov", "png", "wav"),
        video_codecs=("h264", "png"),
        audio_codecs=("aac", "pcm_s16le"),
    )


def transfer_policy(root: Path) -> MediaTransferPolicy:
    return MediaTransferPolicy(
        allowed_hosts=("media.example.test",),
        allowed_url_path_prefixes=("/assets/",),
        allowed_local_roots=(root,),
        max_bytes=1_000_000,
        max_duration_seconds=60.0,
        max_references=4,
        timeouts=MediaTransferTimeouts(1.0, 2.0, 4.0),
        max_redirects=2,
    )


def preprocess(revision: str = "prep-r1") -> PreprocessingSpec:
    return PreprocessingSpec(
        revision=revision,
        operations=("exif_orientation", "rgb24", "source_pts"),
        target_pixel_format="rgb24",
        target_audio_sample_rate=16_000,
        target_audio_channels=1,
    )


def local_request(root: Path, *, size: int = 1024) -> MediaAdmissionRequest:
    source = MediaSource(
        MediaSourceKind.LOCAL_PATH,
        root / "clip.mp4",
        MediaKind.VIDEO,
        "video/mp4",
        size,
        duration_seconds=2.0,
    )
    return MediaAdmissionRequest(
        source=source,
        expected_kind=MediaKind.VIDEO,
        transfer_policy=transfer_policy(root),
        limits=limits(),
        allowlist=allowlist(),
        preprocessing=preprocess(),
    )


class MediaAdmissionTests(unittest.TestCase):
    def test_schema_and_public_admission_are_locator_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "clip.mp4").write_bytes(b"fixture")
            decision = admit_media_source(local_request(root))
            self.assertEqual(decision.schema, MEDIA_ADMISSION_SCHEMA)
            public = json.dumps(decision.to_public_dict(), sort_keys=True)
            self.assertNotIn(str(root), public)
            self.assertNotIn("clip.mp4", public)
            self.assertEqual(decision.status, "admitted")

    def test_size_redirect_and_open_identity_limits_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "clip.mp4").write_bytes(b"fixture")
            request = local_request(root, size=1_000_001)
            with self.assertRaises(MediaAdmissionError):
                admit_media_source(request)

            request = local_request(root)
            identity = SourceIdentity(fingerprint("a"), 1024, fingerprint("b"))
            with self.assertRaises(MediaAdmissionError):
                admit_media_source(request, opened_identity=identity)

            request_with_identity = replace(request, expected_identity=identity)
            admitted = admit_media_source(request_with_identity, opened_identity=identity)
            self.assertEqual(admitted.source_fingerprint, identity.source_fingerprint)

            redirect_request = replace(
                request,
                redirect_trace=RedirectTrace(
                    hop_fingerprints=(fingerprint("c"), fingerprint("d"), fingerprint("e")),
                    policy_match=True,
                ),
            )
            with self.assertRaises(MediaAdmissionError):
                admit_media_source(redirect_request)

            policy_drift_request = replace(
                request,
                redirect_trace=RedirectTrace(
                    hop_fingerprints=(fingerprint("c"),),
                    policy_match=False,
                ),
            )
            with self.assertRaises(MediaAdmissionError):
                admit_media_source(policy_drift_request)

    def test_probe_manifest_preserves_vfr_pts_and_audio_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "clip.mp4").write_bytes(b"fixture")
            request = local_request(root)
            identity = SourceIdentity(fingerprint("a"), 7, fingerprint("b"))
            payload = {
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "0.100"},
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 640,
                        "height": 360,
                        "avg_frame_rate": "30000/1001",
                        "r_frame_rate": "30000/1001",
                        "time_base": "1/90000",
                        "nb_frames": "3",
                        "duration": "0.100",
                    },
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "sample_rate": "16000",
                        "channels": 1,
                        "time_base": "1/16000",
                        "duration": "0.100",
                    },
                ],
            }
            manifest = parse_ffprobe_manifest(payload, request, identity)
            self.assertEqual(manifest.video[0].time_base, (1, 90000))
            self.assertEqual(manifest.video[0].average_frame_rate, Decimal("30000") / 1001)
            self.assertEqual(manifest.audio[0].sample_rate, 16000)
            self.assertEqual(manifest.to_public_dict()["container"], "mov")

    def test_probe_rejects_malformed_or_overlarge_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "clip.mp4").write_bytes(b"fixture")
            request = local_request(root)
            identity = SourceIdentity(fingerprint("a"), 7, fingerprint("b"))
            payloads: tuple[dict[str, object], ...] = (
                {"format": {}, "streams": []},
                {"format": {"format_name": "avi"}, "streams": []},
                {"format": {"format_name": "mp4"}, "streams": [{"codec_type": "video"}]},
            )
            for payload in payloads:
                with self.subTest(payload=payload), self.assertRaises(MediaAdmissionError):
                    parse_ffprobe_manifest(payload, request, identity)

            malformed_numeric: dict[str, object] = {
                "format": {"format_name": "mp4"},
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 640,
                        "height": 360,
                        "avg_frame_rate": "bad/ratio",
                        "time_base": "1/90000",
                    }
                ],
            }
            with self.assertRaises(MediaAdmissionError):
                parse_ffprobe_manifest(malformed_numeric, request, identity)

            with self.assertRaises(MediaAdmissionError):
                ProbeManifest.from_json_bytes(b"{" + b"x" * 70_000, request, identity)

    def test_timestamped_artifacts_retain_pts_and_preprocessing_identity(self) -> None:
        source = SourceIdentity(fingerprint("a"), 7, fingerprint("b"))
        first = PresentationTimestamp(100, 1, 1000)
        second = PresentationTimestamp(175, 1, 1000)
        frame = DecodedFrameArtifact(
            "frame.1",
            source.source_fingerprint,
            first,
            640,
            360,
            "rgb24",
            fingerprint("c"),
            preprocess().fingerprint,
            sequence=0,
        )
        audio = DecodedAudioSegment(
            "audio.1",
            source.source_fingerprint,
            first,
            second,
            16_000,
            1,
            fingerprint("d"),
            preprocess().fingerprint,
        )
        batch = CanonicalMediaBatch(source, preprocess().fingerprint, (frame,), (audio,))
        self.assertEqual(batch.frames[0].timestamp.ticks, 100)
        self.assertNotEqual(
            batch.fingerprint,
            CanonicalMediaBatch(source, preprocess("prep-r2").fingerprint, (), ()).fingerprint,
        )
        validate_media_batch_limits(batch, limits(), temporary_bytes=128)
        oversized = replace(frame, payload_bytes=limits().max_decoded_bytes + 1)
        with self.assertRaises(MediaAdmissionError):
            validate_media_batch_limits(
                CanonicalMediaBatch(source, preprocess().fingerprint, (oversized,), ()), limits()
            )
        with self.assertRaises(MediaAdmissionError):
            validate_media_batch_limits(
                batch, limits(), temporary_bytes=limits().max_temp_bytes + 1
            )

    def test_non_monotone_pts_and_host_fixed_fps_repair_are_rejected(self) -> None:
        source = SourceIdentity(fingerprint("a"), 7, fingerprint("b"))
        frame_a = DecodedFrameArtifact(
            "frame.a",
            source.source_fingerprint,
            PresentationTimestamp(20, 1, 1000),
            32,
            32,
            "rgb24",
            fingerprint("c"),
            preprocess().fingerprint,
            sequence=0,
        )
        frame_b = DecodedFrameArtifact(
            "frame.b",
            source.source_fingerprint,
            PresentationTimestamp(10, 1, 1000),
            32,
            32,
            "rgb24",
            fingerprint("d"),
            preprocess().fingerprint,
            sequence=1,
        )
        with self.assertRaises(MediaAdmissionError):
            CanonicalMediaBatch(source, preprocess().fingerprint, (frame_a, frame_b), ())


class MediaSubprocessTests(unittest.TestCase):
    def test_ffprobe_invocation_is_argument_array_and_redacts_locator(self) -> None:
        invocation = build_ffprobe_invocation(
            "/private/fixture.mp4", limits=limits(), allowlist=allowlist()
        )
        self.assertIsInstance(invocation, MediaProcessInvocation)
        self.assertFalse(invocation.shell)
        self.assertEqual(invocation.argv[0], "ffprobe")
        self.assertIn("-protocol_whitelist", invocation.argv)
        self.assertIn("file,https", invocation.argv)
        self.assertIn("-format_whitelist", invocation.argv)
        public = json.dumps(invocation.to_public_dict(), sort_keys=True)
        self.assertNotIn("/private/fixture.mp4", public)

        with tempfile.TemporaryDirectory() as temporary:
            lease = create_output_lease(Path(temporary), suffix=".mkv")
            decode = build_ffmpeg_invocation(
                "/private/input.mp4", lease, limits=limits(), allowlist=allowlist()
            )
            self.assertIn("-codec_whitelist", decode.argv)
            self.assertIn("-n", decode.argv)
            self.assertEqual(
                decode.max_owned_output_bytes,
                min(limits().max_temp_bytes, limits().max_decoded_bytes),
            )
            lease.release()

    def test_process_invocation_rejects_shell_and_unknown_tool(self) -> None:
        with self.assertRaises(MediaProcessError):
            MediaProcessInvocation(
                tool="sh",
                argv=("sh", "-c", "echo unsafe"),
                protocol_whitelist=("file",),
                format_whitelist=("mp4",),
                codec_whitelist=("h264",),
                timeout_seconds=Decimal("1"),
                max_stdout_bytes=10,
                max_stderr_bytes=10,
            )
        invocation = build_ffprobe_invocation(
            "/private/fixture.mp4", limits=limits(), allowlist=allowlist()
        )
        with self.assertRaises(MediaProcessError):
            replace(invocation, shell=True)  # noqa: S604

    def test_fake_process_runner_reports_handoff_and_does_not_publish_payload(self) -> None:
        class FakeStream:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def read(self, _size: int = -1) -> bytes:
                payload, self.payload = self.payload, b""
                return payload

            def close(self) -> None:
                return None

        class FakeProcess:
            pid = 1234
            returncode = 0

            def __init__(self) -> None:
                self.stdin = FakeStream(b"")
                self.stdout = FakeStream(b"private output")
                self.stderr = FakeStream(b"private error")

            def poll(self) -> int:
                return 0

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                return b"private output", b"private error"

            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary:
            lease = create_output_lease(Path(temporary), suffix=".bin")
            invocation = build_ffmpeg_invocation(
                "/private/input.mp4", lease, limits=limits(), allowlist=allowlist()
            )

            def spawn(*_args: object, **_kwargs: object) -> FakeProcess:
                lease.path.write_bytes(b"temporary")
                return FakeProcess()

            with patch(
                "comfyui_h3_context.adapters.media_subprocess.subprocess.Popen",
                side_effect=spawn,
            ):
                capture = SubprocessMediaRunner().run(invocation)
            self.assertEqual(capture.status, ProcessStatus.SUCCEEDED)
            self.assertTrue(capture.cleanup_succeeded)
            self.assertTrue(lease.path.exists())
            self.assertEqual(capture.owned_output_bytes, len(b"temporary"))
            self.assertNotIn("private output", json.dumps(capture.to_public_dict()))
            self.assertTrue(capture.release_artifacts())
            self.assertFalse(lease.path.exists())

    def test_process_runner_rejects_owned_output_bomb(self) -> None:
        class FakeStream:
            def read(self, _size: int = -1) -> bytes:
                return b""

            def close(self) -> None:
                return None

        class FakeProcess:
            pid = 1234
            returncode = 0
            stdin = FakeStream()
            stdout = FakeStream()
            stderr = FakeStream()

            def poll(self) -> int:
                return 0

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary:
            lease = create_output_lease(Path(temporary), suffix=".bin")
            invocation = replace(
                build_ffmpeg_invocation(
                    "/private/input.mp4", lease, limits=limits(), allowlist=allowlist()
                ),
                max_owned_output_bytes=1,
            )

            def spawn(*_args: object, **_kwargs: object) -> FakeProcess:
                lease.path.write_bytes(b"too large")
                return FakeProcess()

            with patch(
                "comfyui_h3_context.adapters.media_subprocess.subprocess.Popen",
                side_effect=spawn,
            ):
                capture = SubprocessMediaRunner().run(invocation)
            self.assertEqual(capture.status, ProcessStatus.OUTPUT_LIMIT)
            self.assertTrue(capture.cleanup_succeeded)
            self.assertFalse(lease.path.exists())

    def test_offline_fixture_cli_is_deterministic_and_network_free(self) -> None:
        command = [sys.executable, str(ROOT / "scripts" / "m10_02_media_fixture.py"), "--json"]
        first = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=10
        )
        second = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=10
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["network"], "disabled")
        self.assertEqual(payload["host_runtime"], "not_started")
        self.assertNotIn("media.example.test", first.stdout)

    def test_media_schemas_are_pinned_and_parseable(self) -> None:
        for name, schema_id in (
            (
                "media_admission_v1.schema.json",
                "comfyui-h3-context://contracts/media_admission_v1.schema.json",
            ),
            (
                "media_process_v1.schema.json",
                "comfyui-h3-context://contracts/media_process_v1.schema.json",
            ),
        ):
            with self.subTest(name=name):
                schema = json.loads(
                    (ROOT / "governance" / "contracts" / name).read_text(encoding="utf-8")
                )
                self.assertEqual(schema["$id"], schema_id)
                self.assertIn("$defs" if name.startswith("media_admission") else "oneOf", schema)

    def test_process_capture_exposes_only_bounded_status(self) -> None:
        capture = ProcessCapture(
            status=ProcessStatus.CANCELLED,
            stdout=b"private output",
            stderr=b"private error",
            exit_code=None,
            cleanup_succeeded=True,
        )
        public = capture.to_public_dict()
        self.assertEqual(public["status"], "cancelled")
        self.assertNotIn("private output", json.dumps(public))
        self.assertNotIn("private error", json.dumps(public))


if __name__ == "__main__":
    unittest.main()
