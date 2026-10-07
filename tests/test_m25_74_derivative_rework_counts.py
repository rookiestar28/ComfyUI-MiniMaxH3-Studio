"""What an accepted edit and a body read cost the media lease authority, counted.

The clip editor's monitor took seconds to tens of seconds to become playable again after every
accepted edit. The cause was media work repeated for content that had not changed: a derivative
regenerated although its cache key was the same, a stored artifact hashed for every currentness
question, source facts probed again, an executable hashed before every spawn.

These tests drive the accepted modules -- the workspace registries, the import service, the lease
authority, the derivative claim and generator, the pinned FFmpeg pair -- and count that work per
phase. They count instead of timing on purpose: a count cannot pass because a machine is fast, and
a failure names the work that came back. Each test compares one whole table, so a failure shows
every phase at once.
"""

from __future__ import annotations

import hashlib
import json
import subprocess  # noqa: S404 -- fixed argv, caller-supplied authorized executable, no shell
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from test_m25_29_production_authoring_import import (
    _authoring_action,
    _import_request,
    _insert_asset_transaction,
    _registries_with_ready_output,
)
from test_m25_authoring_audio_rate_compatibility import _qualified_tools

import comfyui_h3_context.adapters.executable_admission as admission_module
from comfyui_h3_context.adapters.authoring_derivative_generator import AuthoringDerivativeGenerator
from comfyui_h3_context.adapters.authoring_derivative_source import AuthoringDerivativeClaim
from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.adapters.executable_admission import executable_pin_scope
from comfyui_h3_context.adapters.media_subprocess import SubprocessMediaRunner
from comfyui_h3_context.adapters.segment_artifact_store import PrivateSegmentArtifactStore
from comfyui_h3_context.core.authoring_media import (
    REQUEST_SCHEMA,
    RETAINED_DERIVATIVE_IDLE_SECONDS,
    RUNTIME_PROFILE_FINGERPRINT,
    CreateLeaseRequest,
    DerivativeKind,
    LeaseCommand,
    decode_lease_request,
    public_asset_manifest_fingerprint,
    public_runtime_asset_wire,
)
from comfyui_h3_context.core.composition_contract import (
    ENGINE_PROFILE_ID,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)

WIDTH, HEIGHT, FRAMES = 640, 360, 48

Table = dict[str, dict[str, int]]

# One encode and one validation probe of its output per first use; each tool verified once.
_COLD_VIDEO = {
    "generate.video_proxy": 1,
    "spawn.ffmpeg": 1,
    "spawn.ffprobe": 1,
    "executable.ffmpeg": 1,
    "executable.ffprobe": 1,
}
_COLD_AUDIO = {
    "generate.audio_preview": 1,
    "spawn.ffmpeg": 1,
    "spawn.ffprobe": 0,
    "executable.ffmpeg": 1,
    "executable.ffprobe": 0,
}
# An accepted semantic edit changes the snapshot, not the media.
_AFTER_EDIT = {
    "generate.video_proxy": 0,
    "generate.audio_preview": 0,
    "spawn.ffmpeg": 0,
    "spawn.ffprobe": 0,
    "executable.ffmpeg": 0,
    "executable.ffprobe": 0,
}

GENERATED_ORIGIN: Table = {
    "cold video create": _COLD_VIDEO,
    # Opening a lease confirms its source: one proof of the stored artifact's content.
    "open": {"artifact.inspect": 1},
    # A reader observes its lease; it neither evaluates a claim nor hashes an artifact.
    "body": {"claim.current": 0, "artifact.inspect": 0},
    "cold audio create": _COLD_AUDIO,
    "one claim evaluation": {"artifact.inspect": 0},
    "one confirmation": {"artifact.inspect": 1},
    # A selection is not an edit of the media or of the picture: both leases stay.
    "reap after a selection": {"leases": 2, "generate.video_proxy": 0, "spawn.ffmpeg": 0},
    # Two leases, each admitted by one confirmation before and one after the cache lookup.
    "creates after an edit": {**_AFTER_EDIT, "artifact.inspect": 4},
}

PATH_BACKED_ORIGIN: Table = {
    "cold video create": _COLD_VIDEO,
    "cold audio create": _COLD_AUDIO,
    "creates after an edit": _AFTER_EDIT,
}


class _Counts:
    """Totals of the primitives under test, and what each named phase added to them."""

    def __init__(self) -> None:
        self.totals: Counter[str] = Counter()
        self.phases: dict[str, Counter[str]] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[Counter[str]]:
        before = Counter(self.totals)
        added: Counter[str] = Counter()
        try:
            yield added
        finally:
            added.update({key: value - before[key] for key, value in self.totals.items()})
            self.phases[name] = added

    def require(self, expected: Table) -> None:
        observed = {
            name: {key: self.phases[name][key] for key in keys} for name, keys in expected.items()
        }
        assert observed == expected, "observed work per phase:\n" + "\n".join(
            f"  {name}: {row}" for name, row in observed.items()
        )


def _counting(counts: _Counts, key: Callable[..., str], original: Callable[..., Any]) -> Any:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        counts.totals[key(*args, **kwargs)] += 1
        return original(*args, **kwargs)

    return wrapper


@pytest.fixture
def counts(monkeypatch: pytest.MonkeyPatch) -> _Counts:
    value = _Counts()
    monkeypatch.setattr(
        SubprocessMediaRunner,
        "run",
        _counting(
            value,
            lambda _self, invocation, *_a, **_k: f"spawn.{invocation.tool}",
            SubprocessMediaRunner.run,
        ),
    )
    # A full inspection reads and hashes the whole stored artifact.
    monkeypatch.setattr(
        PrivateSegmentArtifactStore,
        "inspect",
        _counting(value, lambda *_a, **_k: "artifact.inspect", PrivateSegmentArtifactStore.inspect),
    )
    # One open of an executable is one whole-file hash of it.
    monkeypatch.setattr(
        admission_module,
        "windows_open_read_pin",
        _counting(
            value,
            lambda path, *_a, **_k: f"executable.{Path(path).stem}",
            admission_module.windows_open_read_pin,
        ),
    )
    for method, key in (
        ("generate_video_proxy", "generate.video_proxy"),
        ("generate_video_audio_preview", "generate.audio_preview"),
    ):
        monkeypatch.setattr(
            AuthoringDerivativeGenerator,
            method,
            _counting(
                value,
                lambda *_a, _key=key, **_k: _key,
                getattr(AuthoringDerivativeGenerator, method),
            ),
        )
    monkeypatch.setattr(
        AuthoringDerivativeClaim,
        "current",
        _counting(value, lambda *_a, **_k: "claim.current", AuthoringDerivativeClaim.current),
    )
    return value


def _encode_source(
    ffmpeg: Path, target: Path, *, width: int = WIDTH, height: int = HEIGHT, frames: int = FRAMES
) -> None:
    """A source in the profile the Authoring facts probe admits as it is; two seconds by default."""

    duration = str(frames / 24)
    subprocess.run(  # noqa: S603 -- the caller-supplied authorized executable, fixed argv
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate=24:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=701:sample_rate=48000:duration={duration}",
            "-vf",
            "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709,setsar=0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-y",
            str(target),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _create_request(
    snapshot: Any, handle: str, asset_id: str, clip_id: str, kind: str, request_id: str
) -> CreateLeaseRequest:
    manifest = {
        "schema": "h3.context.public_media_asset_manifest.v1",
        "profileId": ENGINE_PROFILE_ID,
        "profileFingerprint": RUNTIME_PROFILE_FINGERPRINT,
        "publicFingerprint": snapshot.public_fingerprint,
        "workspaceRevision": snapshot.workspace_revision,
        "timelineRevision": snapshot.timeline_revision,
        "assets": [public_runtime_asset_wire(asset) for asset in snapshot.assets],
    }
    wire = {
        "schema": REQUEST_SCHEMA,
        "operation": "create",
        "requestId": request_id,
        "workspaceHandle": handle,
        "workspaceRevision": snapshot.workspace_revision,
        "timelineRevision": snapshot.timeline_revision,
        "publicFingerprint": snapshot.public_fingerprint,
        "manifestFingerprint": "sha256:" + hashlib.sha256(_json_bytes(manifest)).hexdigest(),
        "profileFingerprint": RUNTIME_PROFILE_FINGERPRINT,
        "scope": "clip",
        "clipId": clip_id,
        "assetId": asset_id,
        "derivativeKind": kind,
        "ownerId": clip_id,
        "runtimeEpoch": 1,
        "sourceStartFrame": 0,
        "sourceEndFrame": FRAMES,
    }
    request = decode_lease_request(_json_bytes(wire))
    assert isinstance(request, CreateLeaseRequest)
    return request


def _product_authority(claim: Callable[[CreateLeaseRequest], Any]) -> MediaLeaseAuthority:
    # The lease route composes its authority with this retention. The reaper thread is left out so
    # that every claim evaluation in a test happens where the test put it.
    return MediaLeaseAuthority(
        claim, retention_seconds=RETAINED_DERIVATIVE_IDLE_SECONDS, start_reaper=False
    )


def _create(
    authority: MediaLeaseAuthority, request: CreateLeaseRequest
) -> tuple[dict[str, object], str]:
    # The lease worker runs one create inside one media runtime lease, which is one pin scope.
    with executable_pin_scope():
        return authority.create(request)


def test_generated_source_edit_and_body_repeat_no_media_work(
    tmp_path: Path, counts: _Counts
) -> None:
    ffmpeg, ffprobe, adapter = _qualified_tools(tmp_path)
    encoded = (tmp_path / "original.mp4").resolve()
    _encode_source(ffmpeg, encoded)
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "proxy-scratch").resolve(),
    )
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path,
        frame_count=FRAMES,
        artifact_bodies=(encoded.read_bytes(),),
        width=WIDTH,
        height=HEIGHT,
    )
    import_request = _import_request(projection, before, history)
    response = authoring.import_production_outputs(
        import_request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 300,
    )
    handle = import_request.authoring_workspace_handle
    asset_id = response.receipt.rows[0].asset_id
    sequence = iter(range(1, 1000))

    def read_state() -> dict[str, Any]:
        reply = authoring.dispatch(
            _authoring_action(
                f"read.{next(sequence)}", "read_timeline_history", workspace_handle=handle
            )
        )
        assert reply.body is not None
        return cast(dict[str, Any], reply.body["authoring"])

    def apply(request_id: str, commands: list[dict[str, object]] | None = None) -> Any:
        state = read_state()
        if commands is None:
            primary = next(track for track in state["tracks"] if track["kind"] == "primary_video")
            payload = _insert_asset_transaction(
                authoring_state=state,
                asset_id=asset_id,
                track_id=primary["track_id"],
                request_id=request_id,
                duration_frames=FRAMES,
            )
        else:
            payload = _insert_asset_transaction(
                authoring_state=state, asset_id="unused", track_id="unused", request_id=request_id
            )
            payload["commands"] = commands
        action = _authoring_action(request_id, "apply_timeline_transaction")
        action["payload"] = payload
        reply = authoring.dispatch(action)
        assert reply.status == 200 and reply.body is not None, reply.body
        return decode_public_snapshot(reply.body["render_snapshot"])

    snapshot = apply("generated")
    clip_id = "clip.generated"

    def admit(request: CreateLeaseRequest) -> AuthoringDerivativeClaim:
        return authoring.admit_media_derivative(request, generator=generator, media_adapter=adapter)

    def request(issued: Any, kind: str, request_id: str) -> CreateLeaseRequest:
        return _create_request(issued, handle, asset_id, clip_id, kind, request_id)

    authority = _product_authority(admit)
    try:
        with counts.phase("cold video create"):
            video, video_capability = _create(authority, request(snapshot, "video_proxy", "v.1"))
        opening = LeaseCommand(
            "open",
            "open.v.1",
            cast(str, video["leaseId"]),
            cast(int, video["revision"]),
            cast(str, video["ownerId"]),
            cast(int, video["runtimeEpoch"]),
        )
        with counts.phase("open"):
            read = authority.open(opening, video_capability)
        total = 0
        with counts.phase("body"):
            while chunk := read.read_chunk(65536):
                total += len(chunk)
        read.discard()
        assert total == video["byteCount"]

        with counts.phase("cold audio create"):
            audio, _ = _create(authority, request(snapshot, "audio_preview", "a.1"))

        claim = admit(request(snapshot, "video_proxy", "probe.1"))
        with counts.phase("one claim evaluation"):
            assert claim.current()
        with counts.phase("one confirmation"):
            claim.confirm(time.monotonic() + 60)

        selected = apply("select", [{"kind": "select_clips", "payload": {"clip_ids": [clip_id]}}])
        assert selected.public_fingerprint == snapshot.public_fingerprint
        with counts.phase("reap after a selection") as kept:
            authority.reap()
            kept["leases"] = authority.resources()["leases"]

        edited = apply(
            "opacity",
            [
                {
                    "kind": "set_opacity_blend",
                    "payload": {"clip_id": clip_id, "opacity_bp": 9000, "blend": "normal"},
                }
            ],
        )
        assert edited.public_fingerprint != snapshot.public_fingerprint
        with counts.phase("creates after an edit"):
            video_again, _ = _create(authority, request(edited, "video_proxy", "v.2"))
            audio_again, _ = _create(authority, request(edited, "audio_preview", "a.2"))
        counts.require(GENERATED_ORIGIN)
        assert video_again["derivativeFingerprint"] == video["derivativeFingerprint"]
        assert audio_again["derivativeFingerprint"] == audio["derivativeFingerprint"]
    finally:
        authority.close()
    assert authority.resources() == {
        "leases": 0,
        "cacheEntries": 0,
        "cacheBytes": 0,
        "activeReads": 0,
    }


def test_path_backed_source_edit_repeats_no_media_work(
    tmp_path: Path, counts: _Counts, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_m25_composition_contract import fixture_snapshot
    from test_m25_render_source_currentness import TYPE_AUTHORITY, _file_video

    from comfyui_h3_context.adapters.authoring_fonts import load_packaged_font_manifest
    from comfyui_h3_context.adapters.authoring_render_source import (
        PreparedAuthoringHistory,
        claim_render_source,
    )
    from comfyui_h3_context.adapters.authoring_source_binding import (
        PathBackedComfyVideoFromFileV1Factory,
    )
    from comfyui_h3_context.core.contracts import AssetRole, MediaKind
    from comfyui_h3_context.core.registry import ReferenceAsset, build_reference_registry

    ffmpeg, ffprobe, adapter = _qualified_tools(tmp_path)
    root = (tmp_path / "input").resolve()
    root.mkdir()
    _encode_source(ffmpeg, root / "source.mp4")
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "proxy-scratch").resolve(),
    )
    monkeypatch.setattr(
        "comfyui_h3_context.adapters.comfyui_authoring_media_preview."
        "current_authoring_media_preview_adapter",
        lambda: adapter,
    )
    binding = PathBackedComfyVideoFromFileV1Factory(
        type_authority=TYPE_AUTHORITY,
        input_root_factory=lambda: root,
        duration_probe=lambda _source: FRAMES * 1000 // 24,
    ).capture(
        exact_registry=build_reference_registry(
            (ReferenceAsset("video_1", MediaKind.VIDEO, AssetRole.REFERENCE, 1),)
        ),
        generation=1,
        sources=(("video_1", MediaKind.VIDEO, _file_video("source.mp4")),),
    )
    source_claim = claim_render_source(binding, "video_1")

    def snapshot_with(opacity_bp: int) -> Any:
        wire = fixture_snapshot()
        wire["assets"] = [source_claim.asset.to_wire()]
        wire["tracks"] = [wire["tracks"][0]]
        clip = wire["clips"][0]
        clip.update(
            asset_id="video_1", duration_frames=1, source_start_frame=0, opacity_bp=opacity_bp
        )
        wire["clips"] = [clip]
        wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
        return decode_public_snapshot(wire)

    # The workspace registry is not part of this construction: `accepted` stands for the snapshot
    # the workspace currently holds, and replacing it is what an accepted edit does to a claim.
    accepted = [snapshot_with(10_000)]
    fonts = load_packaged_font_manifest()

    def admit(request: CreateLeaseRequest) -> AuthoringDerivativeClaim:
        issued = accepted[0]
        return AuthoringDerivativeClaim(
            request,
            issued,
            PreparedAuthoringHistory(issued, binding.generation, (source_claim,), fonts),
            binding,
            lambda: accepted[0].public_fingerprint == issued.public_fingerprint,
            generator,
            adapter,
        )

    def request(kind: DerivativeKind, request_id: str) -> CreateLeaseRequest:
        snapshot = accepted[0]
        clip_id = snapshot.clips[0].clip_id
        return CreateLeaseRequest(
            request_id,
            snapshot.workspace_handle,
            snapshot.workspace_revision,
            snapshot.timeline_revision,
            snapshot.public_fingerprint,
            public_asset_manifest_fingerprint(snapshot),
            RUNTIME_PROFILE_FINGERPRINT,
            "clip",
            clip_id,
            "video_1",
            kind,
            clip_id,
            1,
            0,
            FRAMES,
        )

    authority = _product_authority(admit)
    try:
        with counts.phase("cold video create"):
            video, _ = _create(authority, request("video_proxy", "path.v.1"))
        with counts.phase("cold audio create"):
            audio, _ = _create(authority, request("audio_preview", "path.a.1"))

        accepted[0] = snapshot_with(9_000)
        with counts.phase("creates after an edit"):
            video_again, _ = _create(authority, request("video_proxy", "path.v.2"))
            audio_again, _ = _create(authority, request("audio_preview", "path.a.2"))
        counts.require(PATH_BACKED_ORIGIN)
        assert video_again["derivativeFingerprint"] == video["derivativeFingerprint"]
        assert audio_again["derivativeFingerprint"] == audio["derivativeFingerprint"]
    finally:
        authority.close()
        binding.release()
