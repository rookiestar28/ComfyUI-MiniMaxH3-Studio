"""M25-16 evidence layer 2: an authorized loopback real-application-service host.

The M25-16 plan (Section 9, "Two complementary evidence layers") requires a *causal*
browser-to-real-application-service path for the M26 planning integration: planning-context
preparation, admission/proposal, explicit review and atomic import, then explicit
readiness/currentness, before a separately authorized start -- all through the accepted service
code and routes, not injected fixtures.

This script hosts that real service on ``127.0.0.1`` only. It:

* installs the fixture-backed HC-09 host doubles from ``scripts/hc_09_host_seam_test_double.py``
  (``server.PromptServer.instance.routes``, ``folder_paths``, ``nodes``) so the real adapter modules
  import and register exactly as they do under a real ComfyUI host;
* registers every owned adapter route through the same registrar walk
  ``tests/test_m23_47_route_seam.py`` uses (``ensure_*_route_registered``), against a real
  ``aiohttp.web.RouteTableDef`` and the real ``aiohttp`` module already imported by this process;
* seeds exactly one synthetic, content-free Sidebar (H3 context) workspace through the identical
  pure-core call chain the real H3 context nodes use, so the Production-workspace-creation and
  planning/readiness routes downstream operate on a genuinely real, correctly wired workspace --
  see ``_seed_sidebar_context`` for why this one boundary is seeded in-process rather than reached
  by the browser;
* always refuses ``/prompt`` with 503 and counts the attempt, so no queue, provider or model
  effect can ever leave this process;
* exposes a content-free ``/__loopback/counters`` GET and ``/__loopback/context`` GET so a test
  harness can read call counts and the seeded context identity without inspecting process state.

The listener binds only loopback. An audit guard refuses outbound sockets and subprocesses;
controlled provider/model transport boundaries count and refuse execution attempts. The owned
planning, import and readiness services remain real. Optional media/render runtimes stay disabled;
their defensive scratch setting points inside the repository without allocating a per-run tree.

M25-45 adds one bounded exception, and only when ``H3_CONTEXT_E2E_SERVICE_LOOPBACK_MEDIA`` is
exactly ``"1"`` and both executables are supplied explicitly: the media runner may execute the
FFmpeg/FFprobe pair whose digests the packaged renderer qualification pins, so a journey can prove
a real derivative through the real route and the real codec rather than a route double. Nothing
else changes. Outbound sockets, ``os.system``, any other executable and any argument bearing a URL
scheme stay refused and counted, and with the variable absent the process behaves exactly as
before.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import pkgutil
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.hc_09_host_seam_test_double import (  # noqa: E402
    InstalledHostModules,
    host_folder_paths_module,
    host_nodes_module,
    host_prompt_server_module,
)

#: The only two adapter modules the HC-09 census permits to fail import outside a real host.
#: Mirrors tests/test_m23_47_route_seam.py's IMPORT_MAY_FAIL exactly -- widening this here would
#: let a route silently disappear from the loopback surface without the shared test noticing.
_IMPORT_MAY_FAIL = frozenset({"comfyui", "comfyui_execution"})


def _adapter_registrars() -> list[tuple[Any, str]]:
    """Every ``ensure_*_route_registered`` function this package publishes, walked by import."""

    import comfyui_h3_context.adapters as adapters_package
    from comfyui_h3_context.core.errors import OptionalDependencyError

    found: list[tuple[Any, str]] = []
    for info in pkgutil.iter_modules(adapters_package.__path__):
        try:
            module = importlib.import_module(f"{adapters_package.__name__}.{info.name}")
        except OptionalDependencyError:
            if info.name not in _IMPORT_MAY_FAIL:
                raise
            continue
        for name in dir(module):
            if name.startswith("ensure_") and name.endswith(
                ("_route_registered", "_routes_registered")
            ):
                found.append((module, name))
    found.sort(key=lambda pair: (pair[0].__name__, pair[1]))
    return found


#: The long-content seed: one 15 s clip whose author wrote the description as three shots,
#: opening with their own `[Shot 1]` the way a storyboard is naturally written.
MULTI_SHOT_INTENT = (
    "[Shot 1] A blue sphere rolls into a plain room. "
    "[Shot 2] At 00:05.000, the sphere stops beside a table. "
    "[Shot 3] At 00:10.000, the sphere rolls out of the room."
)


def _seed_sidebar_context(
    user_intent: str = "A blue sphere turns slowly.",
    duration_seconds: float = 10.0,
    correlation: str = "loopback_seed",
    *,
    include_prompt: bool = False,
) -> dict[str, object]:
    """Publish one synthetic, content-free H3 context exactly as the real node pipeline would.

    The browser cannot reach this boundary itself: publishing a Sidebar workspace for the first
    time is normally the side effect of a queued ComfyUI prompt executing the H3 context nodes
    (``comfyui_h3_context.nodes.H3Context{Plan,Compiler,Validator}Node``), and this loopback
    deliberately refuses ``/prompt`` -- see ``_prompt_handler`` below -- because evidence layer 2
    measures the pre-start boundary and any real queue effect here would let a test observe a
    false "planning caused generation" causality.

    Seeding through the identical pure-core call chain the real node uses, against the SAME
    composition-root registry the HTTP routes dispatch into (``get(SIDEBAR_WORKSPACE)``), keeps
    everything downstream (Production workspace creation, planning, readiness) a genuine call
    through the real adapters -- only the queue-execution boundary itself is out of scope for this
    evidence layer. This is not the Production workspace the plan's instruction 6 requires to be
    created through real routes; that one *is* created by the browser, over real fetch, in the
    Playwright journey.
    """

    from comfyui_h3_context.adapters.composition_root import SIDEBAR_WORKSPACE, get
    from comfyui_h3_context.core.native_h3 import build_native_h3_wiring
    from comfyui_h3_context.core.normalization import RawContextRequest
    from comfyui_h3_context.core.ui_projection import ExecutionCorrelation
    from comfyui_h3_context.nodes import (
        H3ContextCompilerNode,
        H3ContextPlanNode,
        H3ContextValidatorNode,
    )

    raw = RawContextRequest(
        mode="t2va",
        user_intent=user_intent,
        duration_seconds=duration_seconds,
    )
    plan = H3ContextPlanNode().build_plan(raw)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report = H3ContextValidatorNode().validate(plan, document)[1]
    if not (report.is_successful and report.validation.is_valid):
        raise RuntimeError("loopback seed context failed to validate")
    sidebar = get(SIDEBAR_WORKSPACE)
    source = sidebar.publish(
        report,
        build_native_h3_wiring(report),
        ExecutionCorrelation(correlation, f"{correlation}_node"),
    )
    seed: dict[str, object] = {
        "workspace_id": source.workspace_id,
        "report_revision": source.report_revision,
        "report_fingerprint": source.report_fingerprint,
    }
    if include_prompt:
        # The synthetic prompt this seed compiled to, so the harness can hand the planning section
        # the same text the Sidebar holds for a real Context. Only fixture text takes this path.
        seed["prompt_text"] = document.text
    return seed


def _read_loopback_seed(seed: dict[str, object], *, multi_shot: bool = False) -> dict[str, object]:
    from comfyui_h3_context.adapters.composition_root import SIDEBAR_WORKSPACE, get

    handle = seed["workspace_id"]
    if not isinstance(handle, str):
        raise RuntimeError("loopback seed identity unavailable")
    try:
        get(SIDEBAR_WORKSPACE).claim_production_seed(handle)
    except KeyError:
        # CRITICAL: broad suites reach this fixture after the real Context TTL. Publish a fresh
        # synthetic seed at fixture read, never lengthen the product TTL or retry its actions.
        fresh = (
            _seed_sidebar_context(
                MULTI_SHOT_INTENT, 15.0, "loopback_seed_multi_shot", include_prompt=True
            )
            if multi_shot
            else _seed_sidebar_context()
        )
        seed.clear()
        seed.update(fresh)
    return dict(seed)


#: Opt-in only. Absent or anything but "1" keeps the media runner refused like any other process.
MEDIA_RUNNER_ENV = "H3_CONTEXT_E2E_SERVICE_LOOPBACK_MEDIA"
#: A scheme-bearing argument is how a local transcode becomes a network fetch; ffmpeg accepts a URL
#: wherever it accepts a file. Refuse the spelling rather than trusting a protocol whitelist flag.
_SCHEME_MARKERS = ("://", "pipe:", "data:", "concat:", "subfile:", "async:", "cache:")


def _sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file, read in chunks.

    GUARD: not `hashlib.file_digest`, which exists only from Python 3.11 while `pyproject.toml`
    declares `requires-python = ">=3.10"`. The convenience call would raise `AttributeError` on a
    host at the declared floor -- and here that would abort the media allowance rather than refuse
    it, so a supported host would look like a tampered one. Chunked because the pinned pair is an
    encoder binary, not a fixture.
    """

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _solid_rgb_png(width: int, height: int, colour: tuple[int, int, int]) -> bytes:
    """Return a deterministic, content-free RGB PNG for the real browser codec row."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    scanline = b"\x00" + bytes(colour) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanline * height, level=9))
        + chunk(b"IEND", b"")
    )


def _pinned_media_pair() -> tuple[Path, Path] | None:
    """The explicitly supplied FFmpeg/FFprobe pair, or None when the allowance is not in force.

    CRITICAL: the digests come from the packaged renderer qualification, which is issued by an
    executed corpus rather than declared here, and both paths must be supplied explicitly. Reading
    either executable from `PATH`, or trusting a caller-supplied digest, would turn this one
    bounded allowance into arbitrary process execution from a network-capable service.
    """
    if os.environ.get(MEDIA_RUNNER_ENV) != "1":
        return None
    # Structure first, so a missing or malformed supply is named as that rather than as whatever
    # the qualification happens to say; the digests are the last word either way.
    supplied: list[Path] = []
    for variable in (
        "H3_CONTEXT_AUTHORIZED_FFMPEG_PATH",
        "H3_CONTEXT_AUTHORIZED_FFPROBE_PATH",
    ):
        value = os.environ.get(variable)
        if not value:
            raise RuntimeError("media_runner_pair_incomplete")
        candidate = Path(value)
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file():
            raise RuntimeError("media_runner_path_invalid")
        supplied.append(candidate)
    from comfyui_h3_context.adapters.authoring_renderer_qualification import (
        load_renderer_qualification,
    )

    identity = load_renderer_qualification()
    expected = (identity.renderer_fingerprint, identity.probe_fingerprint)
    for candidate, fingerprint in zip(supplied, expected, strict=True):
        digest = "sha256:" + _sha256_file(candidate)
        if digest != fingerprint:
            raise RuntimeError("media_runner_digest_mismatch")
    return supplied[0].resolve(), supplied[1].resolve()


def _command_text(arguments: object) -> str | None:
    """The whole command the audit event describes, however this platform describes it.

    CRITICAL: Windows reports one command-line *string* and `None` for the executable, while POSIX
    reports the resolved executable and an argv sequence. Reading only the sequence shape would
    make every invocation on Windows unrecognisable -- and therefore refused, which is safe -- but
    reading only `executable` would make every one of them unmatched against the pinned pair, which
    is the same thing said less clearly. Handle both, and refuse anything that is neither.
    """

    if isinstance(arguments, (str, bytes)):
        try:
            return os.fsdecode(arguments)
        except (TypeError, ValueError):
            return None
    if not isinstance(arguments, (list, tuple)):
        return None
    parts: list[str] = []
    for argument in arguments:
        try:
            parts.append(os.fsdecode(argument))
        except (TypeError, ValueError):
            return None
    return " ".join(parts)


def _leading_token(text: str) -> str:
    """The executable a Windows command line names, quoted or not."""

    stripped = text.lstrip()
    if stripped.startswith('"'):
        return stripped[1:].split('"', 1)[0]
    return stripped.split(" ", 1)[0]


def _is_pinned_media_invocation(
    pair: tuple[Path, Path] | None, executable: object, arguments: object
) -> bool:
    """True only for a local invocation of one pinned executable with no scheme-bearing argument."""
    if pair is None:
        return False
    text = _command_text(arguments)
    if text is None:
        return False
    named: object = executable
    if named is None:
        if isinstance(arguments, (list, tuple)):
            if not arguments:
                return False
            named = arguments[0]
        else:
            named = _leading_token(text)
    try:
        candidate = Path(os.fsdecode(named)).resolve()  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return False
    if candidate not in pair:
        return False
    # CRITICAL: M25-52's bounded decoder writes only to the final stdout destination `pipe:1`.
    # Remove exactly that terminal token before the scheme scan; allowing any other pipe spelling,
    # a non-terminal pipe or a second pipe would admit stdin/extra stream authority to loopback.
    marker_text = text
    if isinstance(arguments, (list, tuple)):
        try:
            parts = [os.fsdecode(argument) for argument in arguments]
        except (TypeError, ValueError):
            return False
        if parts and parts[-1] == "pipe:1":
            marker_text = " ".join(parts[:-1])
    else:
        stripped = text.rstrip()
        suffix = " pipe:1"
        if stripped.endswith(suffix):
            marker_text = stripped[: -len(suffix)]
    # The remaining whole command line, argv0 included: a pinned executable's own path can carry no
    # scheme, so scanning it closes the gap a per-argument scan leaves on Windows.
    lowered = marker_text.casefold()
    return not any(marker in lowered for marker in _SCHEME_MARKERS)


def _guard_external_effects(counters: dict[str, Any], scope: ExitStack) -> tuple[Path, Path] | None:
    """Refuse and count the external execution boundaries; leave owned services intact."""
    from comfyui_h3_context.adapters import prompt_model_transport

    def refused_provider(*_args: object, **_kwargs: object) -> Any:
        counters["provider_model_calls"] += 1
        raise RuntimeError("provider_model_disabled_in_loopback")

    for name in (
        "run_prompt_model_session",
        "run_remote_prompt_model_session",
        "probe_native_runtime",
    ):
        scope.enter_context(patch.object(prompt_model_transport, name, refused_provider))
    for exchange in (
        prompt_model_transport.LoopbackJsonExchange,
        prompt_model_transport.RemoteHttpsExchange,
    ):
        scope.enter_context(patch.object(exchange, "request", refused_provider))

    media_pair = _pinned_media_pair()

    def deny_egress(event: str, arguments: tuple[object, ...]) -> None:
        # CRITICAL: binding a server to loopback does not prevent its handlers making outbound
        # connections. This process only accepts requests; any outbound socket/process is a
        # forbidden effect, including a loopback model request. Install after loop construction.
        #
        # The one exception is the pinned media pair, and it is decided here rather than by the
        # caller: `subprocess.Popen` reports the executable the interpreter is about to run, so a
        # `shell=True` invocation arrives as the shell's own path and is refused with everything
        # else. Never widen this to a name match or a directory -- the digest-checked absolute
        # paths are the whole of the allowance.
        if event == "subprocess.Popen":
            if _is_pinned_media_invocation(media_pair, arguments[0], arguments[1]):
                counters["media_runner_calls"] += 1
                return
            counters["outbound_attempts"] += 1
            raise RuntimeError("outbound_execution_disabled_in_loopback")
        if event in {"socket.connect", "socket.getaddrinfo", "os.system"}:
            counters["outbound_attempts"] += 1
            raise RuntimeError("outbound_execution_disabled_in_loopback")

    sys.addaudithook(deny_egress)
    return media_pair


class _LoopbackMediaClaim:
    def __init__(self, fixture: dict[str, object]) -> None:
        self._fixture = fixture
        self.cache_key = str(fixture["derivative_fingerprint"])

    def current(self) -> bool:
        return True

    def confirm(self, _deadline: float) -> None:
        return None

    def generate(self, _deadline: float, cancellation: object) -> tuple[bytearray, Any]:
        checker = getattr(cancellation, "is_cancelled", None)
        if callable(checker) and checker():
            from comfyui_h3_context.core.authoring_media import MediaLeaseError

            raise MediaLeaseError("cancelled")
        return bytearray(cast(bytes, self._fixture["body"])), self._fixture["facts"]


def _audio_peaks_fixture_command(ffmpeg: Path, target: Path) -> list[str]:
    return [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x90:rate=24:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=1",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        # IMPORTANT: the closed production facts parser requires the qualified BT.709 fields;
        # omitting setparams makes this audio-bearing fixture fail before route qualification.
        "-vf",
        "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        "-c:v",
        "libx264",
        # IMPORTANT: keep decode and presentation order identical; libx264's default trailing
        # B-frames omit pkt_dts and make the closed production facts probe refuse the fixture.
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
        "1",
        "-shortest",
        "-movflags",
        "+faststart",
        "-y",
        str(target),
    ]


def _prepare_loopback_media(
    pair: tuple[Path, Path] | None, scope: ExitStack
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    """Create exact-ceiling MP4s, asset decorations and a real-route claim table.

    The size extension is a top-level ISO BMFF ``free`` box, not arbitrary trailing padding. Both
    the pinned probe and Chromium must parse the resulting container before it can count as media
    evidence.
    """

    if pair is None:
        return {}, {}
    from comfyui_h3_context.core.authoring_media import (
        DERIVATIVE_PROFILE_ID,
        MediaGeometry,
        VerifiedDerivative,
        derivative_byte_limit,
    )
    from comfyui_h3_context.core.composition_contract import (
        DERIVATIVE_MANIFEST_SCHEMA,
        PRIVATE_SOURCE_MANIFEST_SCHEMA,
        DerivativeManifest,
        PrivateSourceManifest,
    )

    root = Path(tempfile.mkdtemp(prefix="m25-45-media-", dir=ROOT / ".tmp")).resolve()
    if root.parent != (ROOT / ".tmp").resolve():
        raise RuntimeError("media_fixture_root_invalid")
    scope.callback(shutil.rmtree, root, True)
    ceiling = derivative_byte_limit("video_proxy")
    fixtures: dict[str, dict[str, object]] = {}
    public_assets: list[dict[str, object]] = []
    for index, colour in enumerate(("red", "blue", "green"), start=1):
        asset_id = f"loopback-video-{index}"
        target = root / f"{asset_id}.mp4"
        subprocess.run(
            [
                str(pair[0]),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colour}:s=1280x720:r=24:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-y",
                str(target),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        encoded = target.read_bytes()
        free_size = ceiling - len(encoded)
        if not 8 <= free_size <= 0xFFFFFFFF:
            raise RuntimeError("media_fixture_size_invalid")
        body = encoded + free_size.to_bytes(4, "big") + b"free" + bytes(free_size - 8)
        target.write_bytes(body)
        probe = subprocess.run(
            [
                str(pair[1]),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height",
                "-of",
                "json",
                str(target),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        stream = json.loads(probe.stdout)["streams"][0]
        if stream != {"codec_name": "h264", "width": 1280, "height": 720}:
            raise RuntimeError("media_fixture_probe_invalid")
        source_fingerprint = (
            "sha256:" + hashlib.sha256(f"{asset_id}:{colour}".encode("ascii")).hexdigest()
        )
        derivative_fingerprint = "sha256:" + hashlib.sha256(body).hexdigest()
        asset_fingerprint = (
            "sha256:" + hashlib.sha256(f"asset:{asset_id}".encode("ascii")).hexdigest()
        )
        generator_fingerprint = (
            "sha256:" + hashlib.sha256(b"m25-45-loopback-exact-ceiling-v1").hexdigest()
        )
        facts = VerifiedDerivative(
            PrivateSourceManifest(
                PRIVATE_SOURCE_MANIFEST_SCHEMA,
                asset_id,
                1,
                source_fingerprint,
                "m25-45-loopback",
            ),
            DerivativeManifest(
                DERIVATIVE_MANIFEST_SCHEMA,
                asset_id,
                source_fingerprint,
                DERIVATIVE_PROFILE_ID,
                derivative_fingerprint,
                1,
            ),
            "video_proxy",
            asset_fingerprint,
            generator_fingerprint,
            len(body),
            "absent",
            MediaGeometry(1280, 720, 1280, 720),
        )
        fixtures[asset_id] = {
            "body": body,
            "facts": facts,
            "derivative_fingerprint": derivative_fingerprint,
        }
        public_assets.append(
            {
                "assetId": asset_id,
                "assetFingerprint": asset_fingerprint,
                "byteCount": len(body),
                "derivativeFingerprint": derivative_fingerprint,
                "codec": stream["codec_name"],
                "width": stream["width"],
                "height": stream["height"],
            }
        )

    thumbnail_id = "loopback-thumbnail-asset"
    thumbnail_body = _solid_rgb_png(64, 36, (255, 192, 0))
    thumbnail_source_fingerprint = (
        "sha256:" + hashlib.sha256(b"loopback-thumbnail-source-v1").hexdigest()
    )
    thumbnail_derivative_fingerprint = "sha256:" + hashlib.sha256(thumbnail_body).hexdigest()
    thumbnail_asset_fingerprint = (
        "sha256:" + hashlib.sha256(b"asset:loopback-thumbnail-asset").hexdigest()
    )
    thumbnail_generator_fingerprint = (
        "sha256:" + hashlib.sha256(b"m25-48-loopback-thumbnail-v1").hexdigest()
    )
    thumbnail_facts = VerifiedDerivative(
        PrivateSourceManifest(
            PRIVATE_SOURCE_MANIFEST_SCHEMA,
            thumbnail_id,
            1,
            thumbnail_source_fingerprint,
            "m25-48-loopback",
        ),
        DerivativeManifest(
            DERIVATIVE_MANIFEST_SCHEMA,
            thumbnail_id,
            thumbnail_source_fingerprint,
            DERIVATIVE_PROFILE_ID,
            thumbnail_derivative_fingerprint,
            1,
        ),
        "thumbnail",
        thumbnail_asset_fingerprint,
        thumbnail_generator_fingerprint,
        len(thumbnail_body),
        "absent",
        MediaGeometry(64, 36, 64, 36),
    )
    fixtures[thumbnail_id] = {
        "body": thumbnail_body,
        "facts": thumbnail_facts,
        "derivative_fingerprint": thumbnail_derivative_fingerprint,
    }

    filmstrip_id = "loopback-filmstrip-asset"
    filmstrip_target = root / f"{filmstrip_id}.jpg"
    subprocess.run(
        [
            str(pair[0]),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=24:duration=1",
            "-vf",
            "fps=fps=4/1:round=near,scale=-2:48:flags=lanczos,"
            "tile=4x1:nb_frames=4:padding=0:margin=0",
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-c:v",
            "mjpeg",
            "-pix_fmt",
            "yuvj420p",
            "-q:v",
            "5",
            "-an",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-f",
            "image2",
            "-y",
            str(filmstrip_target),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    filmstrip_body = filmstrip_target.read_bytes()
    from comfyui_h3_context.adapters.authoring_derivative_jpeg import probe_filmstrip_jpeg

    filmstrip_width, filmstrip_height = probe_filmstrip_jpeg(
        filmstrip_body, maximum_bytes=derivative_byte_limit("filmstrip")
    )
    if (filmstrip_width, filmstrip_height) != (344, 48):
        raise RuntimeError("filmstrip_fixture_geometry_invalid")
    filmstrip_source_fingerprint = (
        "sha256:" + hashlib.sha256(b"loopback-filmstrip-source-v1").hexdigest()
    )
    filmstrip_derivative_fingerprint = "sha256:" + hashlib.sha256(filmstrip_body).hexdigest()
    filmstrip_asset_fingerprint = (
        "sha256:" + hashlib.sha256(b"asset:loopback-filmstrip-asset").hexdigest()
    )
    filmstrip_generator_fingerprint = (
        "sha256:" + hashlib.sha256(b"m25-49-loopback-filmstrip-v1").hexdigest()
    )
    filmstrip_facts = VerifiedDerivative(
        PrivateSourceManifest(
            PRIVATE_SOURCE_MANIFEST_SCHEMA,
            filmstrip_id,
            1,
            filmstrip_source_fingerprint,
            "m25-49-loopback",
        ),
        DerivativeManifest(
            DERIVATIVE_MANIFEST_SCHEMA,
            filmstrip_id,
            filmstrip_source_fingerprint,
            DERIVATIVE_PROFILE_ID,
            filmstrip_derivative_fingerprint,
            1,
        ),
        "filmstrip",
        filmstrip_asset_fingerprint,
        filmstrip_generator_fingerprint,
        len(filmstrip_body),
        "absent",
        MediaGeometry(160, 90, filmstrip_width, filmstrip_height),
    )
    fixtures[filmstrip_id] = {
        "body": filmstrip_body,
        "facts": filmstrip_facts,
        "derivative_fingerprint": filmstrip_derivative_fingerprint,
    }

    filmstrip_over_limit_id = "loopback-filmstrip-one-byte-over"
    filmstrip_over_limit_source_fingerprint = (
        "sha256:" + hashlib.sha256(b"loopback-filmstrip-over-limit-source-v1").hexdigest()
    )
    filmstrip_over_limit_asset_fingerprint = (
        "sha256:" + hashlib.sha256(b"asset:loopback-filmstrip-one-byte-over").hexdigest()
    )
    filmstrip_over_limit_facts = VerifiedDerivative(
        PrivateSourceManifest(
            PRIVATE_SOURCE_MANIFEST_SCHEMA,
            filmstrip_over_limit_id,
            1,
            filmstrip_over_limit_source_fingerprint,
            "m25-49-loopback",
        ),
        DerivativeManifest(
            DERIVATIVE_MANIFEST_SCHEMA,
            filmstrip_over_limit_id,
            filmstrip_over_limit_source_fingerprint,
            DERIVATIVE_PROFILE_ID,
            filmstrip_derivative_fingerprint,
            1,
        ),
        "filmstrip",
        filmstrip_over_limit_asset_fingerprint,
        filmstrip_generator_fingerprint,
        len(filmstrip_body),
        "absent",
        MediaGeometry(160, 90, filmstrip_width, filmstrip_height),
    )
    fixtures[filmstrip_over_limit_id] = {
        "body": filmstrip_body
        + bytes(derivative_byte_limit("filmstrip") + 1 - len(filmstrip_body)),
        "facts": filmstrip_over_limit_facts,
        "derivative_fingerprint": "sha256:"
        + hashlib.sha256(b"filmstrip-one-byte-over").hexdigest(),
    }

    audio_peaks_id = "loopback-audio-peaks-asset"
    audio_peaks_source = root / f"{audio_peaks_id}.mp4"
    subprocess.run(
        _audio_peaks_fixture_command(pair[0], audio_peaks_source),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    from comfyui_h3_context.adapters.authoring_derivative_generator import (
        AuthoringDerivativeGenerator,
    )
    from comfyui_h3_context.adapters.authoring_video_facts import (
        probe_authoring_video_facts,
    )
    from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
    from comfyui_h3_context.core.authoring_audio_peaks import decode_audio_peaks_envelope
    from comfyui_h3_context.core.composition_contract import composition_contract_fingerprint

    audio_adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=pair[0],
        ffprobe_path=pair[1],
        scratch_root=(root / "audio-probe-scratch").resolve(),
        clock_ms=lambda: 1,
    )
    audio_source_facts = probe_authoring_video_facts(
        audio_peaks_source.resolve(), audio_adapter, time.monotonic() + 120
    )
    if audio_source_facts.embedded_audio.disposition != "present_bound":
        raise RuntimeError("audio_peaks_fixture_audio_unavailable")
    canonical_sample_count = audio_source_facts.embedded_audio.canonical_sample_count
    if canonical_sample_count is None:
        raise RuntimeError("audio_peaks_fixture_coverage_invalid")
    audio_generator = AuthoringDerivativeGenerator(
        ffmpeg_path=pair[0],
        ffprobe_path=pair[1],
        scratch_root=(root / "audio-peaks-scratch").resolve(),
    )
    generated_audio_peaks = audio_generator.generate_video_audio_peaks(
        audio_peaks_source.resolve(),
        audio_source_facts,
        expected_source_fingerprint=audio_source_facts.content_fingerprint,
        deadline=time.monotonic() + 120,
    )
    try:
        audio_peaks_body = bytes(generated_audio_peaks.body)
        audio_peaks_generator_fingerprint = generated_audio_peaks.generator_fingerprint
    finally:
        generated_audio_peaks.clear()
    decoded_audio_peaks = decode_audio_peaks_envelope(audio_peaks_body)
    if abs(decoded_audio_peaks.sample_count * 6 - canonical_sample_count) > 6:
        raise RuntimeError("audio_peaks_fixture_coverage_invalid")
    audio_peaks_derivative_fingerprint = "sha256:" + hashlib.sha256(audio_peaks_body).hexdigest()
    audio_asset_wire = {
        "assetId": audio_peaks_id,
        "kind": "video",
        "sourceTimeBase": {
            "num": audio_source_facts.source_time_base.num,
            "den": audio_source_facts.source_time_base.den,
        },
        "sourceFrameCount": audio_source_facts.frame_count,
        "sourceSampleCount": canonical_sample_count,
        "embeddedAudio": "present_bound",
        "timestampPolicy": "nonnegative_monotonic_v1",
        "landmarks": [
            {
                "frameIndex": row.frame_index,
                "pts": row.pts,
                "dts": row.dts,
                "durationTicks": row.duration_ticks,
            }
            for row in audio_source_facts.landmarks
        ],
    }
    audio_peaks_asset_fingerprint = composition_contract_fingerprint(audio_asset_wire)
    audio_peaks_verified = VerifiedDerivative(
        PrivateSourceManifest(
            PRIVATE_SOURCE_MANIFEST_SCHEMA,
            audio_peaks_id,
            1,
            audio_source_facts.content_fingerprint,
            "m25-52-loopback",
        ),
        DerivativeManifest(
            DERIVATIVE_MANIFEST_SCHEMA,
            audio_peaks_id,
            audio_source_facts.content_fingerprint,
            DERIVATIVE_PROFILE_ID,
            audio_peaks_derivative_fingerprint,
            1,
        ),
        "audio_peaks",
        audio_peaks_asset_fingerprint,
        audio_peaks_generator_fingerprint,
        len(audio_peaks_body),
        "present_bound",
        None,
    )
    fixtures[audio_peaks_id] = {
        "body": audio_peaks_body,
        "facts": audio_peaks_verified,
        "derivative_fingerprint": audio_peaks_derivative_fingerprint,
    }

    audio_peaks_invalid_id = "loopback-audio-peaks-invalid"
    audio_peaks_invalid_body = bytearray(audio_peaks_body)
    struct.pack_into("bb", audio_peaks_invalid_body, 20, 120, -120)
    audio_peaks_invalid_fingerprint = (
        "sha256:" + hashlib.sha256(audio_peaks_invalid_body).hexdigest()
    )
    invalid_asset_wire = {**audio_asset_wire, "assetId": audio_peaks_invalid_id}
    audio_peaks_invalid_asset_fingerprint = composition_contract_fingerprint(invalid_asset_wire)
    audio_peaks_invalid_verified = VerifiedDerivative(
        PrivateSourceManifest(
            PRIVATE_SOURCE_MANIFEST_SCHEMA,
            audio_peaks_invalid_id,
            1,
            audio_source_facts.content_fingerprint,
            "m25-52-loopback",
        ),
        DerivativeManifest(
            DERIVATIVE_MANIFEST_SCHEMA,
            audio_peaks_invalid_id,
            audio_source_facts.content_fingerprint,
            DERIVATIVE_PROFILE_ID,
            audio_peaks_invalid_fingerprint,
            1,
        ),
        "audio_peaks",
        audio_peaks_invalid_asset_fingerprint,
        audio_peaks_generator_fingerprint,
        len(audio_peaks_invalid_body),
        "present_bound",
        None,
    )
    fixtures[audio_peaks_invalid_id] = {
        "body": bytes(audio_peaks_invalid_body),
        "facts": audio_peaks_invalid_verified,
        "derivative_fingerprint": audio_peaks_invalid_fingerprint,
    }

    audio_peaks_over_limit_id = "loopback-audio-peaks-one-byte-over"
    over_limit_asset_wire = {**audio_asset_wire, "assetId": audio_peaks_over_limit_id}
    audio_peaks_over_limit_asset_fingerprint = composition_contract_fingerprint(
        over_limit_asset_wire
    )
    audio_peaks_over_limit_verified = VerifiedDerivative(
        PrivateSourceManifest(
            PRIVATE_SOURCE_MANIFEST_SCHEMA,
            audio_peaks_over_limit_id,
            1,
            audio_source_facts.content_fingerprint,
            "m25-52-loopback",
        ),
        DerivativeManifest(
            DERIVATIVE_MANIFEST_SCHEMA,
            audio_peaks_over_limit_id,
            audio_source_facts.content_fingerprint,
            DERIVATIVE_PROFILE_ID,
            audio_peaks_derivative_fingerprint,
            1,
        ),
        "audio_peaks",
        audio_peaks_over_limit_asset_fingerprint,
        audio_peaks_generator_fingerprint,
        len(audio_peaks_body),
        "present_bound",
        None,
    )
    fixtures[audio_peaks_over_limit_id] = {
        "body": audio_peaks_body
        + bytes(derivative_byte_limit("audio_peaks") + 1 - len(audio_peaks_body)),
        "facts": audio_peaks_over_limit_verified,
        "derivative_fingerprint": "sha256:"
        + hashlib.sha256(b"audio-peaks-one-byte-over").hexdigest(),
    }

    over_limit_id = "loopback-thumbnail-one-byte-over"
    over_limit_source_fingerprint = (
        "sha256:" + hashlib.sha256(b"loopback-thumbnail-over-limit-source-v1").hexdigest()
    )
    over_limit_asset_fingerprint = (
        "sha256:" + hashlib.sha256(b"asset:loopback-thumbnail-one-byte-over").hexdigest()
    )
    over_limit_facts = VerifiedDerivative(
        PrivateSourceManifest(
            PRIVATE_SOURCE_MANIFEST_SCHEMA,
            over_limit_id,
            1,
            over_limit_source_fingerprint,
            "m25-48-loopback",
        ),
        DerivativeManifest(
            DERIVATIVE_MANIFEST_SCHEMA,
            over_limit_id,
            over_limit_source_fingerprint,
            DERIVATIVE_PROFILE_ID,
            thumbnail_derivative_fingerprint,
            1,
        ),
        "thumbnail",
        over_limit_asset_fingerprint,
        thumbnail_generator_fingerprint,
        len(thumbnail_body),
        "absent",
        MediaGeometry(64, 36, 64, 36),
    )
    fixtures[over_limit_id] = {
        "body": thumbnail_body
        + bytes(derivative_byte_limit("thumbnail") + 1 - len(thumbnail_body)),
        "facts": over_limit_facts,
        "derivative_fingerprint": "sha256:" + hashlib.sha256(b"one-byte-over").hexdigest(),
    }
    context = {
        "workspaceHandle": "m25-45-loopback-media",
        "workspaceRevision": 1,
        "timelineRevision": 1,
        "publicFingerprint": "sha256:" + hashlib.sha256(b"m25-45-public").hexdigest(),
        "manifestFingerprint": "sha256:" + hashlib.sha256(b"m25-45-manifest").hexdigest(),
        "profileFingerprint": (
            "sha256:f0a226902d89e48113905b43c09fd99c63b2307f8d39ae740330431e091861ff"
        ),
        "assets": public_assets,
        "decoration": {
            "assetId": thumbnail_id,
            "assetFingerprint": thumbnail_asset_fingerprint,
            "byteCount": len(thumbnail_body),
            "derivativeFingerprint": thumbnail_derivative_fingerprint,
            "width": 64,
            "height": 36,
        },
        "filmstrip": {
            "assetId": filmstrip_id,
            "assetFingerprint": filmstrip_asset_fingerprint,
            "byteCount": len(filmstrip_body),
            "derivativeFingerprint": filmstrip_derivative_fingerprint,
            "sourceFrameCount": 24,
            "sourceWidth": 160,
            "sourceHeight": 90,
            "width": filmstrip_width,
            "height": filmstrip_height,
            "tileCount": 4,
        },
        "filmstripOverLimit": {
            "assetId": filmstrip_over_limit_id,
            "assetFingerprint": filmstrip_over_limit_asset_fingerprint,
        },
        "audioPeaks": {
            "asset": audio_asset_wire,
            "assetFingerprint": audio_peaks_asset_fingerprint,
            "byteCount": len(audio_peaks_body),
            "derivativeFingerprint": audio_peaks_derivative_fingerprint,
            "sampleCount": decoded_audio_peaks.sample_count,
            "pairCount": len(decoded_audio_peaks.pairs),
        },
        "audioPeaksInvalid": {
            "asset": invalid_asset_wire,
            "assetFingerprint": audio_peaks_invalid_asset_fingerprint,
        },
        "audioPeaksOverLimit": {
            "asset": over_limit_asset_wire,
            "assetFingerprint": audio_peaks_over_limit_asset_fingerprint,
        },
        "overLimitAssetId": over_limit_id,
        "container": "ISO BMFF MP4 with a valid top-level free box",
        "exactCeilingBytes": ceiling,
    }
    return context, fixtures


def _build_app(
    counters: dict[str, Any],
    seed: dict[str, object],
    media_context: dict[str, object],
    media_fixtures: dict[str, dict[str, object]],
    scope: ExitStack,
    multi_shot_seed: dict[str, object] | None = None,
) -> tuple[Any, list[str], list[str]]:
    import aiohttp  # noqa: F401  (real module; installed into sys.modules by this import)
    from aiohttp import web

    routes = web.RouteTableDef()
    # CRITICAL: `register_owned_route` looks up `sys.modules["server"]` and `sys.modules["aiohttp"]`
    # lazily rather than importing either -- see comfyui_route_seam.host_web_and_routes. The real
    # `aiohttp` module is already installed above; only `server` needs the HC-09 double.
    installed = InstalledHostModules(
        server=host_prompt_server_module(routes),
        folder_paths=host_folder_paths_module(inventory={}),
        nodes=host_nodes_module(),
    )
    installed.__enter__()
    media_authority: Any = None
    if media_fixtures:
        from comfyui_h3_context.adapters import comfyui_authoring_media_leases as media_routes
        from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
        from comfyui_h3_context.core.authoring_media import MediaLeaseError, VerifiedDerivative

        def claim(request: Any) -> _LoopbackMediaClaim:
            fixture = media_fixtures.get(request.asset_id)
            if fixture is None:
                raise MediaLeaseError("authority_mismatch")
            facts = fixture.get("facts")
            if not isinstance(facts, VerifiedDerivative) or request.derivative_kind != facts.kind:
                raise MediaLeaseError("authority_mismatch")
            return _LoopbackMediaClaim(fixture)

        media_authority = MediaLeaseAuthority(claim)
        service = media_routes.MediaLeaseRouteService(media_authority)
        media_routes._SERVICE = service
        scope.callback(service.close)

    registered: list[str] = []
    skipped: list[str] = []
    for module, name in _adapter_registrars():
        label = f"{module.__name__}.{name}"
        try:
            ok = bool(getattr(module, name)())
        except Exception as error:  # pragma: no cover - reported to the caller, not swallowed
            skipped.append(f"{label}: {type(error).__name__}")
            continue
        (registered if ok else skipped).append(label)

    async def _count_same_origin(request: web.Request, handler: Any) -> Any:
        # "route family" == the exact registered path, per the plan's counters requirement: it
        # keeps two actions sharing one route (planning vs. readiness both post to
        # PRODUCTION_PLANNING_ROUTE) under one honestly-shared count instead of a fabricated split.
        if request.path.startswith("/h3-context/v1/"):
            table = counters["same_origin_calls_by_route"]
            table[request.path] = table.get(request.path, 0) + 1
        return await handler(request)

    app = web.Application(middlewares=[web.middleware(_count_same_origin)])
    app.add_routes(routes)

    async def _prompt_handler(_request: web.Request) -> web.Response:
        # CRITICAL: evidence layer 2 measures the pre-start boundary. Any real queue effect here
        # would let a test observe a false "planning caused generation" causality, so this handler
        # never queues anything -- it only counts the attempt and refuses. Do not wire this to the
        # real ComfyUI prompt queue even for a "just this one case" journey; that is exactly the
        # boundary M25-16 Section 9 requires to stay controlled/unavailable.
        counters["queue_calls"] += 1
        return web.json_response({"error": "queue_disabled_in_loopback"}, status=503)

    async def _system_stats_handler(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "system": {"comfyui_version": "m25-16-loopback", "python_version": sys.version},
                "devices": [],
            }
        )

    async def _counters_handler(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "queue_calls": counters["queue_calls"],
                "provider_model_calls": counters["provider_model_calls"],
                "outbound_attempts": counters["outbound_attempts"],
                "media_runner_calls": counters["media_runner_calls"],
                "same_origin_calls_by_route": dict(counters["same_origin_calls_by_route"]),
            }
        )

    async def _context_handler(_request: web.Request) -> web.Response:
        return web.json_response(_read_loopback_seed(seed))

    async def _multi_shot_context_handler(_request: web.Request) -> web.Response:
        if multi_shot_seed is None:
            return web.json_response({"error": "seed_unavailable"}, status=404)
        return web.json_response(_read_loopback_seed(multi_shot_seed, multi_shot=True))

    async def _media_context_handler(_request: web.Request) -> web.Response:
        if not media_context:
            return web.json_response({"error": "media_disabled"}, status=404)
        return web.json_response(dict(media_context))

    async def _media_state_handler(_request: web.Request) -> web.Response:
        if media_authority is None:
            return web.json_response({"error": "media_disabled"}, status=404)
        # Evidence-only, content-free accounting around the real authority. No capability, body,
        # request, path or private source identity crosses this endpoint.
        with media_authority._lock:
            return web.json_response(
                {
                    "leases": len(media_authority._entries),
                    "cacheEntries": len(media_authority._cache),
                    "cacheBytes": sum(len(entry.body) for entry in media_authority._cache.values()),
                }
            )

    app.router.add_post("/prompt", _prompt_handler)
    app.router.add_get("/system_stats", _system_stats_handler)
    app.router.add_get("/__loopback/counters", _counters_handler)
    app.router.add_get("/__loopback/context", _context_handler)
    app.router.add_get("/__loopback/multi-shot-context", _multi_shot_context_handler)
    app.router.add_get("/__loopback/media-context", _media_context_handler)
    app.router.add_get("/__loopback/media-state", _media_state_handler)

    return app, registered, skipped


def _watch_stdin_close(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """Detect the owning process closing our stdin, the primary shutdown signal on Windows.

    Windows delivers `child_process.kill()` as a forceful `TerminateProcess`, which never invokes
    a registered `signal.signal(SIGTERM, ...)` handler. Closing stdin and letting a blocking reader
    thread observe EOF is the one shutdown path that is reliable on both platforms; SIGTERM stays
    registered below purely for the POSIX case.
    """

    def _reader() -> None:
        try:
            while sys.stdin.readline():
                pass
        except (OSError, ValueError):
            # A broken/closed pipe has the same ownership meaning as EOF.
            loop.call_soon_threadsafe(stop_event.set)
            return
        loop.call_soon_threadsafe(stop_event.set)

    threading.Thread(target=_reader, daemon=True, name="loopback-stdin-watch").start()


async def _serve(port: int, scope: ExitStack) -> None:
    from aiohttp import web

    # Declared for any adapter that reads it defensively; the render/media runtime itself stays
    # OFF (H3_CONTEXT_AUTHORIZED_MEDIA_RUNTIME is left unset) so the optional media/provider
    # boundary this loopback must never cross stays truthfully unavailable, per the plan's
    # "external provider/model boundaries may remain controlled" allowance.
    os.environ["H3_CONTEXT_AUTHORIZED_MEDIA_SCRATCH_ROOT"] = str(ROOT / ".tmp")
    if os.environ.get(MEDIA_RUNNER_ENV) == "1":
        # The marker the explicit-override branch of media runtime resolution expects beside the
        # two supplied paths. The render runtime stays off either way: this allowance is about a
        # derivative the browser then presents, not about rendering an output.
        os.environ["H3_CONTEXT_AUTHORIZED_MEDIA_RUNTIME"] = "1"
    else:
        os.environ.pop("H3_CONTEXT_AUTHORIZED_MEDIA_RUNTIME", None)
    os.environ.pop("H3_CONTEXT_AUTHORIZED_RENDER_RUNTIME", None)

    counters: dict[str, Any] = {
        "queue_calls": 0,
        "provider_model_calls": 0,
        "outbound_attempts": 0,
        "media_runner_calls": 0,
        "same_origin_calls_by_route": {},
    }
    media_pair = _guard_external_effects(counters, scope)

    seed = _seed_sidebar_context()
    multi_shot_seed = _seed_sidebar_context(
        MULTI_SHOT_INTENT, 15.0, "loopback_seed_multi_shot", include_prompt=True
    )
    media_context, media_fixtures = _prepare_loopback_media(media_pair, scope)
    if media_context:
        # Fixture preparation is the only phase allowed to invoke the pinned media pair. Expose
        # its content-free count so browser evidence can prove that route use adds no native work
        # without baking an obsolete count into the journey each time the corpus gains a fixture.
        media_context["fixtureRunnerCalls"] = counters["media_runner_calls"]
    app, registered, skipped = _build_app(
        counters, seed, media_context, media_fixtures, scope, multi_shot_seed
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    ready = {
        "ready": True,
        "port": runner.addresses[0][1],
        "pid": os.getpid(),
        "registered_routes": registered,
        "skipped_routes": skipped,
        "seed_context": seed,
    }
    print(json.dumps(ready), flush=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
    except NotImplementedError:
        # ProactorEventLoop on Windows does not support add_signal_handler; stdin-close is the
        # remaining shutdown path there, wired unconditionally below.
        pass
    _watch_stdin_close(loop, stop_event)

    await stop_event.wait()
    await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        required=True,
        help="TCP port to bind on 127.0.0.1, or 0 to select a free one",
    )
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    (ROOT / ".tmp").mkdir(exist_ok=True)
    try:
        with ExitStack() as scope:
            asyncio.run(_serve(args.port, scope))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
