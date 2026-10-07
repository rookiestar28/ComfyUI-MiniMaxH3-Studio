"""Run the deterministic M10-02 media admission/probe fixture without I/O or a decoder."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from typing import Any

from comfyui_h3_context.core.contracts import MediaKind
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
    SourceIdentity,
    admit_media_source,
    parse_ffprobe_manifest,
)
from comfyui_h3_context.core.security import (
    MediaSource,
    MediaSourceKind,
    MediaTransferPolicy,
    MediaTransferTimeouts,
)


def _fingerprint(letter: str) -> str:
    return "sha256:" + letter * 64


def build_fixture() -> dict[str, Any]:
    limits = MediaLimits(
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
    allowlist = MediaAllowlist(
        protocols=("file", "https"),
        containers=("mp4", "mov"),
        video_codecs=("h264",),
        audio_codecs=("aac",),
    )
    preprocessing = PreprocessingSpec(
        revision="prep-r1",
        operations=("rgb24", "source_pts"),
        target_pixel_format="rgb24",
        target_audio_sample_rate=16_000,
        target_audio_channels=1,
    )
    request = MediaAdmissionRequest(
        source=MediaSource(
            MediaSourceKind.REMOTE_URL,
            "https://media.example.test/assets/fixture.mp4",
            MediaKind.VIDEO,
            "video/mp4",
            1024,
            duration_seconds=0.1,
        ),
        expected_kind=MediaKind.VIDEO,
        transfer_policy=MediaTransferPolicy(
            allowed_hosts=("media.example.test",),
            allowed_url_path_prefixes=("/assets/",),
            allowed_local_roots=(),
            max_bytes=1_000_000,
            max_duration_seconds=60.0,
            max_references=4,
            timeouts=MediaTransferTimeouts(1.0, 2.0, 4.0),
            max_redirects=2,
        ),
        limits=limits,
        allowlist=allowlist,
        preprocessing=preprocessing,
    )
    identity = SourceIdentity(_fingerprint("a"), 1024, _fingerprint("b"))
    admission = admit_media_source(request)
    probe = parse_ffprobe_manifest(
        {
            "format": {"format_name": "mp4", "duration": "0.100"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 640,
                    "height": 360,
                    "avg_frame_rate": "30000/1001",
                    "r_frame_rate": "60000/1001",
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
        },
        request,
        identity,
    )
    timestamp_0 = PresentationTimestamp(0, 1, 90000)
    timestamp_1 = PresentationTimestamp(3000, 1, 90000)
    batch = CanonicalMediaBatch(
        source_identity=identity,
        preprocessing_fingerprint=preprocessing.fingerprint,
        frames=(
            DecodedFrameArtifact(
                "frame.0",
                identity.source_fingerprint,
                timestamp_0,
                640,
                360,
                "rgb24",
                _fingerprint("c"),
                preprocessing.fingerprint,
                sequence=0,
            ),
            DecodedFrameArtifact(
                "frame.1",
                identity.source_fingerprint,
                timestamp_1,
                640,
                360,
                "rgb24",
                _fingerprint("d"),
                preprocessing.fingerprint,
                sequence=1,
            ),
        ),
        audio_segments=(
            DecodedAudioSegment(
                "audio.0",
                identity.source_fingerprint,
                timestamp_0,
                timestamp_1,
                16_000,
                1,
                _fingerprint("e"),
                preprocessing.fingerprint,
            ),
        ),
    )
    return {
        "schema": MEDIA_ADMISSION_SCHEMA,
        "status": "PASS",
        "network": "disabled",
        "host_runtime": "not_started",
        "admission": admission.to_public_dict(),
        "probe_fingerprint": probe.fingerprint,
        "batch_fingerprint": batch.fingerprint,
        "source_fingerprint": identity.source_fingerprint,
        "preprocessing_fingerprint": preprocessing.fingerprint,
        "counts": {
            "video_streams": len(probe.video),
            "audio_streams": len(probe.audio),
            "frames": len(batch.frames),
            "audio_segments": len(batch.audio_segments),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    args = parser.parse_args()
    payload = build_fixture()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print("M10-02 fixture: PASS")
        print(f"probe={payload['probe_fingerprint']}")
        print(f"batch={payload['batch_fingerprint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
