"""Explicit test-only supported-host canary for the real media subprocess boundary."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

from .adapters.media_subprocess import (
    MediaProcessInvocation,
    OwnedOutputLease,
    ProcessStatus,
    SubprocessMediaRunner,
    create_output_lease,
)
from .core.errors import MediaProcessError

H2_CANARY_NODE_ID = "comfyui_h3_context.H3Context.MediaLifecycleCanary"
_CANARY_MARKER = ".h3-context-owned-h2-canary"


class _CancelAfterPolling:
    def __init__(self) -> None:
        self.calls = 0

    def is_cancelled(self) -> bool:
        self.calls += 1
        return self.calls >= 2


def _owned_root() -> Path:
    if os.environ.get("H3_CONTEXT_HOST_H2_CANARY") != "1":
        raise MediaProcessError("host H2 canary is not enabled")
    value = os.environ.get("H3_CONTEXT_H2_CANARY_ROOT")
    if not value:
        raise MediaProcessError("host H2 canary root is not configured")
    root = Path(value)
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MediaProcessError("host H2 canary root is unavailable") from exc
    marker = resolved / _CANARY_MARKER
    if root.is_symlink() or not resolved.is_dir() or not marker.is_file():
        raise MediaProcessError("host H2 canary root is not explicitly owned")
    if marker.read_text(encoding="utf-8") != "h3-context-h2-canary/1\n":
        raise MediaProcessError("host H2 canary ownership marker is invalid")
    return resolved


def _invocation(scenario: str, root: Path) -> MediaProcessInvocation:
    output_leases: tuple[OwnedOutputLease, ...] = ()
    argv: tuple[str, ...]
    timeout = Decimal("5")
    if scenario == "success":
        lease = create_output_lease(root / "outputs", suffix=".mkv")
        output_leases = (lease,)
        argv = (
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:r=10",
            "-t",
            "0.1",
            "-an",
            "-c:v",
            "ffv1",
            "-n",
            str(lease.path),
        )
    else:
        argv = (
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
        if scenario == "timeout":
            timeout = Decimal("0.05")
    return MediaProcessInvocation(
        tool="ffmpeg",
        argv=argv,
        protocol_whitelist=("file",),
        format_whitelist=("matroska",),
        codec_whitelist=("ffv1",),
        timeout_seconds=timeout,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        start_new_session=os.name == "posix",
        output_leases=output_leases,
        max_owned_output_bytes=1_000_000,
    )


class H3MediaLifecycleCanaryNode:
    """Run bounded synthetic success/cancel/timeout paths only in the explicit host test lane."""

    __h3_context_node_id__ = H2_CANARY_NODE_ID
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("result",)
    FUNCTION = "execute"
    CATEGORY = "h3_context/internal"
    EXPERIMENTAL = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, ...]]]:
        return {"required": {"scenario": (("success", "cancellation", "timeout"),)}}

    def execute(self, scenario: str) -> tuple[str]:
        if scenario not in {"success", "cancellation", "timeout"}:
            raise MediaProcessError("unsupported host H2 canary scenario")
        invocation = _invocation(scenario, _owned_root())
        cancellation = _CancelAfterPolling() if scenario == "cancellation" else None
        capture = SubprocessMediaRunner().run(invocation, cancellation=cancellation)
        expected = {
            "success": ProcessStatus.SUCCEEDED,
            "cancellation": ProcessStatus.CANCELLED,
            "timeout": ProcessStatus.TIMED_OUT,
        }[scenario]
        if capture.status is not expected:
            raise MediaProcessError("host H2 canary reached an unexpected terminal status")
        public = capture.to_public_dict()
        public["scenario"] = scenario
        public["artifact_released"] = capture.release_artifacts()
        return (json.dumps(public, sort_keys=True, separators=(",", ":")),)


__all__ = ["H2_CANARY_NODE_ID", "H3MediaLifecycleCanaryNode"]
