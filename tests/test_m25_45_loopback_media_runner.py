"""M25-45 AC45-12: the loopback service's media allowance admits one pinned pair and nothing else.

The allowance exists so a journey can prove a real derivative through the real route and the real
codec. It is the only way a process this service starts can ever run, so every rule that bounds it
is tested here: the default mode, an incomplete or wrong pair, a foreign executable, and an
argument that would turn a local transcode into a network fetch.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.m25_16_service_loopback import (
    MEDIA_RUNNER_ENV,
    _audio_peaks_fixture_command,
    _is_pinned_media_invocation,
    _pinned_media_pair,
)

FFMPEG_ENV = "H3_CONTEXT_AUTHORIZED_FFMPEG_PATH"
FFPROBE_ENV = "H3_CONTEXT_AUTHORIZED_FFPROBE_PATH"


def _pinned_digests() -> tuple[str, str]:
    from comfyui_h3_context.adapters.authoring_renderer_qualification import (
        load_renderer_qualification,
    )

    identity = load_renderer_qualification()
    return identity.renderer_fingerprint, identity.probe_fingerprint


def _supplied_pair() -> tuple[Path, Path] | None:
    """The pinned pair, when this invocation actually supplied it; never discovered from PATH."""
    ffmpeg, ffprobe = os.environ.get(FFMPEG_ENV), os.environ.get(FFPROBE_ENV)
    if not ffmpeg or not ffprobe:
        return None
    try:
        expected = _pinned_digests()
    except Exception:
        # No current packaged qualification, so there is no pinned pair to admit. The allowance is
        # unavailable rather than wrong, and the negatives below still run.
        return None
    paths = (Path(ffmpeg), Path(ffprobe))
    for path, digest in zip(paths, expected, strict=True):
        if not path.is_file():
            return None
        # Not `hashlib.file_digest`: it arrived in Python 3.11 and `pyproject.toml` declares a
        # 3.10 floor, so the convenience call would turn a supported host into a collection error.
        computed = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                computed.update(block)
        if "sha256:" + computed.hexdigest() != digest:
            return None
    return paths[0], paths[1]


def test_the_allowance_is_off_unless_the_variable_is_exactly_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value in (None, "", "0", "true", "1 "):
        if value is None:
            monkeypatch.delenv(MEDIA_RUNNER_ENV, raising=False)
        else:
            monkeypatch.setenv(MEDIA_RUNNER_ENV, value)
        assert _pinned_media_pair() is None


def test_an_incomplete_pair_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MEDIA_RUNNER_ENV, "1")
    monkeypatch.delenv(FFMPEG_ENV, raising=False)
    monkeypatch.delenv(FFPROBE_ENV, raising=False)
    with pytest.raises(RuntimeError, match="media_runner_pair_incomplete"):
        _pinned_media_pair()


def test_a_relative_or_missing_path_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(MEDIA_RUNNER_ENV, "1")
    monkeypatch.setenv(FFMPEG_ENV, "ffmpeg.exe")
    monkeypatch.setenv(FFPROBE_ENV, str(tmp_path / "ffprobe.exe"))
    with pytest.raises(RuntimeError, match="media_runner_path_invalid"):
        _pinned_media_pair()
    monkeypatch.setenv(FFMPEG_ENV, str(tmp_path / "absent.exe"))
    with pytest.raises(RuntimeError, match="media_runner_path_invalid"):
        _pinned_media_pair()


def test_an_executable_whose_digest_is_not_the_pinned_one_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    impostor = tmp_path / "ffmpeg.exe"
    impostor.write_bytes(b"not the qualified renderer")
    monkeypatch.setenv(MEDIA_RUNNER_ENV, "1")
    monkeypatch.setenv(FFMPEG_ENV, str(impostor))
    monkeypatch.setenv(FFPROBE_ENV, str(impostor))
    with pytest.raises(RuntimeError, match="media_runner_digest_mismatch"):
        _pinned_media_pair()


def test_only_the_pinned_executables_run_and_only_on_local_arguments(tmp_path: Path) -> None:
    ffmpeg, ffprobe = tmp_path / "ffmpeg.exe", tmp_path / "ffprobe.exe"
    for path in (ffmpeg, ffprobe):
        path.write_bytes(b"")
    pair = (ffmpeg.resolve(), ffprobe.resolve())
    source, target = tmp_path / "in.mp4", tmp_path / "out.mp4"
    local = [str(ffmpeg), "-i", str(source), "-vf", "scale=1280:-2", str(target)]

    assert _is_pinned_media_invocation(pair, str(ffmpeg), local)
    assert _is_pinned_media_invocation(pair, str(ffprobe), [str(ffprobe), "-show_streams"])
    stdout_pipe = [str(ffmpeg), "-i", str(source), "-f", "s16le", "pipe:1"]
    assert _is_pinned_media_invocation(pair, str(ffmpeg), stdout_pipe)
    assert _is_pinned_media_invocation(pair, None, subprocess.list2cmdline(stdout_pipe))
    # The allowance is not in force at all.
    assert not _is_pinned_media_invocation(None, str(ffmpeg), local)
    # A foreign executable, including the shell a `shell=True` invocation would report.
    assert not _is_pinned_media_invocation(pair, sys.executable, [sys.executable, "-c", "pass"])
    assert not _is_pinned_media_invocation(
        pair, str(tmp_path / "cmd.exe"), [str(tmp_path / "cmd.exe"), "/c", str(ffmpeg)]
    )
    # Any argument that names a protocol rather than a file.
    for hostile in (
        "https://example.invalid/source.mp4",
        "HTTP://Example.Invalid/source.mp4",
        "pipe:0",
        "concat:a.ts|b.ts",
        "data:application/octet-stream;base64,AAAA",
    ):
        assert not _is_pinned_media_invocation(pair, str(ffmpeg), [str(ffmpeg), "-i", hostile])
    assert not _is_pinned_media_invocation(
        pair, str(ffmpeg), [str(ffmpeg), "-i", "pipe:0", "-f", "s16le", "pipe:1"]
    )
    assert not _is_pinned_media_invocation(
        pair, str(ffmpeg), [str(ffmpeg), "-i", str(source), "pipe:1", str(target)]
    )
    assert not _is_pinned_media_invocation(
        pair,
        None,
        subprocess.list2cmdline([str(ffmpeg), "-i", "pipe:1", "-f", "s16le", "pipe:1"]),
    )
    # Windows describes the same invocation as one command-line string with no executable at all
    # (B-M2545-12). Every rule above must hold on that shape too, or the guard would refuse the
    # pinned pair on the only platform this lane runs on.
    assert _is_pinned_media_invocation(pair, None, subprocess.list2cmdline(local))
    assert _is_pinned_media_invocation(
        pair, None, subprocess.list2cmdline([str(ffprobe), "-version"])
    )
    assert not _is_pinned_media_invocation(
        pair, None, subprocess.list2cmdline([sys.executable, "-c", "pass"])
    )
    assert not _is_pinned_media_invocation(
        pair, None, subprocess.list2cmdline([str(ffmpeg), "-i", "https://example.invalid/a.mp4"])
    )
    # A bare name resolves against the working directory, which is not where the pinned pair lives.
    assert not _is_pinned_media_invocation(pair, None, "ffmpeg -i in.mp4")
    # A quoted executable is one token, not two: were the leading token split at the space, the
    # positive below would resolve to a directory and be refused.
    spaced_directory = tmp_path / "media tools"
    spaced_directory.mkdir()
    spaced = spaced_directory / "ffmpeg.exe"
    spaced.write_bytes(b"")
    quoted = subprocess.list2cmdline([str(spaced), "-i", str(source)])
    assert quoted.startswith('"')
    assert _is_pinned_media_invocation((spaced.resolve(), ffprobe.resolve()), None, quoted)
    assert not _is_pinned_media_invocation(pair, None, quoted)
    # A shape that is neither a command line nor an argument list, and an empty one.
    unreadable_shapes: tuple[object, ...] = (0, {"argv": local}, None, [])
    for unreadable in unreadable_shapes:
        assert not _is_pinned_media_invocation(pair, None, unreadable)


def test_audio_fixture_declares_the_closed_production_colour_profile(tmp_path: Path) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    target = tmp_path / "audio-bearing.mp4"
    command = _audio_peaks_fixture_command(ffmpeg, target)

    assert command[0] == str(ffmpeg)
    assert command[-1] == str(target)
    assert command[command.index("-vf") + 1] == (
        "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
    )
    assert command[command.index("-bf") + 1] == "0"
    for flag, value in (
        ("-color_range", "tv"),
        ("-colorspace", "bt709"),
        ("-color_primaries", "bt709"),
        ("-color_trc", "bt709"),
    ):
        assert command[command.index(flag) + 1] == value


@pytest.mark.skipif(
    _supplied_pair() is None,
    reason="requires the pinned FFmpeg/FFprobe pair supplied through the authorized path variables",
)
def test_the_installed_hook_admits_the_pinned_pair_and_nothing_else() -> None:
    # Audit hooks cannot be removed. Never install this guard in the shared pytest process.
    pair = _supplied_pair()
    assert pair is not None
    program = """
import json, os, subprocess, sys
from contextlib import ExitStack
from scripts.m25_16_service_loopback import _guard_external_effects
counters = {'provider_model_calls': 0, 'outbound_attempts': 0, 'media_runner_calls': 0}
reasons = []
with ExitStack() as scope:
    _guard_external_effects(counters, scope)
    admitted = subprocess.run(
        [os.environ['H3_CONTEXT_AUTHORIZED_FFPROBE_PATH'], '-version'],
        capture_output=True, timeout=30,
    ).returncode
    for attempt in (
        lambda: subprocess.Popen([sys.executable, '-c', 'raise SystemExit(99)']),
        lambda: subprocess.Popen(
            [os.environ['H3_CONTEXT_AUTHORIZED_FFMPEG_PATH'], '-i', 'https://example.invalid/a.mp4']
        ),
        lambda: os.system('cmd /c exit 0'),
    ):
        try:
            attempt()
        except RuntimeError as error:
            reasons.append(str(error))
        else:
            raise AssertionError('external attempt was allowed')
print(json.dumps({'counters': counters, 'reasons': reasons, 'admitted': admitted}))
"""
    environment = {
        **os.environ,
        MEDIA_RUNNER_ENV: "1",
        FFMPEG_ENV: str(pair[0]),
        FFPROBE_ENV: str(pair[1]),
    }
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=90,
        env=environment,
    )
    observed = json.loads(result.stdout)
    assert observed["admitted"] == 0
    assert observed["counters"]["media_runner_calls"] == 1
    assert observed["counters"]["outbound_attempts"] == 3
    assert observed["reasons"] == ["outbound_execution_disabled_in_loopback"] * 3
