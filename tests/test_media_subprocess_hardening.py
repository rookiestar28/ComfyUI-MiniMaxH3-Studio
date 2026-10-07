"""HC-03 real media subprocess lifecycle regressions."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import patch

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from comfyui_h3_context.adapters.media_subprocess import (
    MediaProcessInvocation,
    OwnedOutputLease,
    ProcessCapture,
    ProcessStatus,
    SubprocessMediaRunner,
    _terminate_process,
    build_ffmpeg_invocation,
    create_output_lease,
)
from comfyui_h3_context.core.errors import MediaProcessError
from comfyui_h3_context.core.media_admission import MediaAllowlist, MediaLimits


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


class _CountingCancellation:
    def __init__(self, cancel_at: int) -> None:
        self.calls = 0
        self.cancel_at = cancel_at

    def is_cancelled(self) -> bool:
        self.calls += 1
        return self.calls >= self.cancel_at


class _RaisingCancellation:
    def is_cancelled(self) -> bool:
        raise RuntimeError("private cancellation failure")


class _NeverReapedProcess:
    """Boundary fake for the post-signal non-reap classification branch."""

    def __init__(self) -> None:
        self.pid = 4242
        self.stdin = None
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = None

    def poll(self) -> None:
        return None

    def wait(self, timeout: float) -> int:
        raise subprocess.TimeoutExpired(("ffmpeg",), timeout)


class _ExitedProcess:
    def __init__(self) -> None:
        self.pid = 4243
        self.stdin = None
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = 0

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float) -> int:
        return 0


class _StuckReaderThread:
    def start(self) -> None:
        return

    def join(self, timeout: float) -> None:
        return

    def is_alive(self) -> bool:
        return True


class MediaSubprocessHardeningTests(unittest.TestCase):
    def test_issued_lease_identity_cannot_be_reassigned_to_caller_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            lease = create_output_lease(base / "owned", suffix=".bin")
            foreign_directory = base / "caller-owned"
            foreign_directory.mkdir()
            foreign_file = foreign_directory / "keep.bin"
            foreign_file.write_bytes(b"keep")

            with self.assertRaises(AttributeError):
                lease.root = base  # type: ignore[misc]
            with self.assertRaises(AttributeError):
                lease.path = foreign_file  # type: ignore[misc]

            lease.release()
            self.assertEqual(foreign_file.read_bytes(), b"keep")
            self.assertTrue(foreign_directory.is_dir())

    def test_non_reaped_process_cannot_report_cleanup_success(self) -> None:
        process = _NeverReapedProcess()
        invocation = self._invocation(("ffmpeg",), timeout="0.01")
        with (
            patch(
                "comfyui_h3_context.adapters.media_subprocess.subprocess.Popen",
                return_value=process,
            ),
            patch(
                "comfyui_h3_context.adapters.media_subprocess._terminate_process",
                return_value=True,
            ),
        ):
            capture = SubprocessMediaRunner().run(invocation)

        self.assertEqual(capture.status, ProcessStatus.TIMED_OUT)
        self.assertFalse(capture.reaped)
        self.assertFalse(capture.cleanup_succeeded)
        self.assertGreaterEqual(capture.cleanup_errors, 1)

    @unittest.skipUnless(os.name == "nt", "Windows system executable boundary")
    def test_windows_termination_ignores_path_poisoned_taskkill(self) -> None:
        process = _NeverReapedProcess()
        with tempfile.TemporaryDirectory() as temporary:
            poison = (Path(temporary) / "taskkill.exe").resolve()
            poison.write_bytes(b"attacker-controlled executable")
            with (
                patch.dict(os.environ, {"PATH": str(poison.parent)}, clear=False),
                patch(
                    "comfyui_h3_context.adapters.media_subprocess.subprocess.run",
                    return_value=subprocess.CompletedProcess(("taskkill",), 0),
                ) as run,
            ):
                self.assertEqual(Path(shutil.which("taskkill") or "").resolve(), poison)
                self.assertTrue(_terminate_process(cast(subprocess.Popen[bytes], process)))

            argv = run.call_args.args[0]
            selected = Path(argv[0]).resolve()
            self.assertTrue(selected.is_absolute())
            self.assertEqual(selected.name.casefold(), "taskkill.exe")
            self.assertNotEqual(selected, poison)
            self.assertIn("system32", tuple(part.casefold() for part in selected.parts))

    def test_stuck_reader_cannot_report_cleanup_success(self) -> None:
        process = _ExitedProcess()
        invocation = self._invocation(("ffmpeg",))
        with (
            patch(
                "comfyui_h3_context.adapters.media_subprocess.subprocess.Popen",
                return_value=process,
            ),
            patch(
                "comfyui_h3_context.adapters.media_subprocess.threading.Thread",
                side_effect=lambda **_kwargs: _StuckReaderThread(),
            ),
        ):
            capture = SubprocessMediaRunner().run(invocation)

        self.assertEqual(capture.status, ProcessStatus.PIPE_FAILED)
        self.assertTrue(capture.reaped)
        self.assertFalse(capture.reader_threads_joined)
        self.assertFalse(capture.cleanup_succeeded)
        self.assertGreaterEqual(capture.cleanup_errors, 1)

    def test_capture_invariant_rejects_false_cleanup_success(self) -> None:
        with self.assertRaises(MediaProcessError):
            ProcessCapture(
                status=ProcessStatus.TIMED_OUT,
                cleanup_succeeded=True,
                reaped=False,
            )

    def test_process_schema_validates_closed_invocation_and_capture_variants(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "governance"
            / "contracts"
            / "media_process_v1.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        variants = schema.get("oneOf")
        self.assertIsInstance(variants, list)
        self.assertEqual(len(variants), 2)

        validator = Draft202012Validator(schema)
        Draft202012Validator.check_schema(schema)
        invocation = self._invocation(("ffmpeg",)).to_public_dict()
        capture = ProcessCapture(status=ProcessStatus.SUCCEEDED).to_public_dict()
        validator.validate(invocation)
        validator.validate(capture)

        for invalid in (
            {**invocation, "unexpected": True},
            {**invocation, "shell": True},
            {**invocation, "argv_count": "one"},
            {**capture, "status": "plausible_success"},
            {**capture, "cleanup_succeeded": "yes"},
            {"schema": "h3.media.process.v1"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    validator.validate(invalid)

        matching = [variant for variant in variants if set(variant["required"]) == set(capture)]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            matching[0]["properties"]["status"]["enum"],
            [status.value for status in ProcessStatus],
        )

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if os.name == "nt":
            import ctypes

            # CRITICAL: Linux Python stubs omit the Windows-only windll attribute; this branch
            # is reached only by the Windows process-liveness assertion.
            windll = getattr(ctypes, "windll")  # noqa: B009
            handle = windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259
            finally:
                windll.kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _require_ffmpeg(self) -> None:
        if shutil.which("ffmpeg") is None:
            self.fail("HC-03 real-process lane requires allowlisted ffmpeg")

    def _sleep_argv(self) -> tuple[str, ...]:
        return (
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=16x16:rate=30",
            "-f",
            "null",
            "-",
        )

    def _invocation(
        self, argv: tuple[str, ...], *, lease: OwnedOutputLease | None = None, timeout: str = "2"
    ) -> MediaProcessInvocation:
        return MediaProcessInvocation(
            tool="ffmpeg",
            argv=argv,
            protocol_whitelist=("file",),
            format_whitelist=("mp4",),
            codec_whitelist=("h264",),
            timeout_seconds=Decimal(timeout),
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
            start_new_session=os.name == "posix",
            output_leases=() if lease is None else (lease,),
            max_owned_output_bytes=1024,
        )

    def test_output_lease_is_root_owned_unique_and_no_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = create_output_lease(root, suffix=".mkv")
            second = create_output_lease(root, suffix=".mkv")
            self.assertNotEqual(first.path, second.path)
            self.assertFalse(first.path.exists())
            self.assertTrue(first.path.parent.is_dir())
            invocation = build_ffmpeg_invocation(
                "input.mp4", first, limits=limits(), allowlist=allowlist()
            )
            self.assertIn("-n", invocation.argv)
            self.assertNotIn("-y", invocation.argv)
            with self.assertRaises(MediaProcessError):
                build_ffmpeg_invocation(
                    "input.mp4",
                    root / "caller.mkv",  # type: ignore[arg-type]
                    limits=limits(),
                    allowlist=allowlist(),
                )
            first.release()
            first.release()
            second.release()

    def test_explicit_executable_is_absolute_exact_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = (Path(temporary) / "ffprobe.exe").resolve()
            executable.write_bytes(b"synthetic executable identity")
            invocation = MediaProcessInvocation(
                tool="ffprobe",
                executable=str(executable),
                argv=(str(executable), "-version"),
                protocol_whitelist=("file",),
                format_whitelist=("mp4",),
                codec_whitelist=("h264",),
                timeout_seconds=Decimal("1"),
                max_stdout_bytes=1024,
                max_stderr_bytes=1024,
            )

            public = json.dumps(invocation.to_public_dict(), sort_keys=True)
            self.assertNotIn(str(executable), public)
            self.assertEqual(invocation.argv[0], str(executable))
            with self.assertRaises(MediaProcessError):
                replace(invocation, argv=("ffprobe", "-version"))
            with self.assertRaises(MediaProcessError):
                replace(invocation, executable="ffprobe", argv=("ffprobe", "-version"))

    def test_pre_cancelled_invocation_does_not_spawn(self) -> None:
        with patch(
            "comfyui_h3_context.adapters.media_subprocess.subprocess.Popen",
            side_effect=AssertionError("pre-cancelled invocation must not spawn"),
        ) as popen:
            capture = SubprocessMediaRunner().run(
                self._invocation(("ffmpeg", "-version")),
                cancellation=_CountingCancellation(1),
            )

        self.assertEqual(capture.status, ProcessStatus.CANCELLED)
        self.assertEqual(capture.poll_observations, 0)
        popen.assert_not_called()

    def test_output_lease_rejects_symlink_or_reparse_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "target"
            target.mkdir()
            link = base / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    self.fail("HC-03 symlink safety lane could not create its fixture")
                result = subprocess.run(
                    [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(link), str(target)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, "could not create Windows junction fixture")
            with self.assertRaises(MediaProcessError):
                create_output_lease(link, suffix=".bin")
            link.unlink() if link.is_symlink() else link.rmdir()

    def test_real_process_success_handoff_is_usable_until_explicit_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._require_ffmpeg()
            lease = create_output_lease(root / "owned", suffix=".mkv")
            argv = (
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=16x16:d=0.05",
                "-frames:v",
                "1",
                "-c:v",
                "ffv1",
                "-n",
                str(lease.path),
            )
            invocation = self._invocation(argv, lease=lease)
            capture = SubprocessMediaRunner().run(invocation)
            self.assertEqual(capture.status, ProcessStatus.SUCCEEDED)
            self.assertTrue(capture.reaped)
            self.assertEqual(capture.poll_observations >= 1, True)
            self.assertEqual(capture.artifacts, (lease,))
            self.assertTrue(lease.path.is_file())
            self.assertGreater(lease.path.stat().st_size, 0)
            capture.release_artifacts()
            capture.release_artifacts()
            self.assertFalse(lease.path.exists())

    def test_real_polling_cancellation_timeout_and_probe_exception_reap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            del temporary
            self._require_ffmpeg()
            sleep_argv = self._sleep_argv()
            cancel = _CountingCancellation(3)
            cancelled = SubprocessMediaRunner().run(
                self._invocation(sleep_argv), cancellation=cancel
            )
            timed_out = SubprocessMediaRunner().run(self._invocation(sleep_argv, timeout="0.05"))
            probe_failed = SubprocessMediaRunner().run(
                self._invocation(sleep_argv), cancellation=_RaisingCancellation()
            )
            output_limited = SubprocessMediaRunner().run(
                replace(
                    self._invocation(("ffmpeg", "-hide_banner", "-h")),
                    max_stdout_bytes=1,
                    max_stderr_bytes=1,
                )
            )
            self.assertEqual(cancelled.status, ProcessStatus.CANCELLED)
            self.assertGreaterEqual(cancel.calls, 3)
            self.assertGreaterEqual(cancelled.poll_observations, 2)
            self.assertTrue(cancelled.reaped)
            self.assertEqual(timed_out.status, ProcessStatus.TIMED_OUT)
            self.assertTrue(timed_out.reaped)
            self.assertEqual(probe_failed.status, ProcessStatus.CANCELLATION_FAILED)
            self.assertTrue(probe_failed.reaped)
            self.assertNotIn("private cancellation failure", str(probe_failed.to_public_dict()))
            self.assertEqual(output_limited.status, ProcessStatus.OUTPUT_LIMIT)
            self.assertTrue(output_limited.reaped)

    def test_real_descendant_process_tree_is_terminated_and_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            child_code = "import time; time.sleep(30)"
            parent_code = (
                "import pathlib,subprocess,sys,time;"
                f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                # IMPORTANT: existence must publish a complete PID; direct write_text exposes
                # an empty file before its write and races the fixture's readiness check.
                "pid=pathlib.Path(sys.argv[1]);pending=pid.with_suffix('.pending');"
                "pending.write_text(str(p.pid),encoding='ascii');pending.replace(pid);"
                "time.sleep(30)"
            )
            if os.name == "nt":
                parent = subprocess.Popen(
                    [sys.executable, "-c", parent_code, str(pid_file)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            else:
                parent = subprocess.Popen(
                    [sys.executable, "-c", parent_code, str(pid_file)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    start_new_session=True,
                )
            try:
                deadline = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(pid_file.exists(), "descendant PID fixture was not created")
                child_pid = int(pid_file.read_text(encoding="ascii"))
                self.assertTrue(self._pid_exists(child_pid))
                self.assertTrue(_terminate_process(parent))
                parent.wait(timeout=3)
                deadline = time.monotonic() + 3
                while self._pid_exists(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(self._pid_exists(child_pid), "descendant process escaped cleanup")
            finally:
                if parent.poll() is None:
                    _terminate_process(parent)
                parent.wait(timeout=3)

    def test_cleanup_failure_does_not_overwrite_primary_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lease = create_output_lease(Path(temporary), suffix=".bin")
            invocation = self._invocation(("ffmpeg",), lease=lease)
            failed = replace(
                invocation,
                argv=("ffmpeg",),
            )
            with (
                patch(
                    "comfyui_h3_context.adapters.media_subprocess.subprocess.Popen",
                    side_effect=OSError("spawn failed"),
                ),
                patch.object(OwnedOutputLease, "release", side_effect=OSError("locked")),
            ):
                capture = SubprocessMediaRunner().run(failed)
            self.assertEqual(capture.status, ProcessStatus.SPAWN_FAILED)
            self.assertFalse(capture.cleanup_succeeded)
            self.assertEqual(capture.cleanup_errors, 1)
            self.assertNotEqual(capture.status, ProcessStatus.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
