"""Authenticate an ephemeral Windows gate link census; invalid receipts never skip a check."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from scripts.workspace_link_guard import (
    LinkFinding,
    _io_path,
    find_links,
    is_reparse_point,
    worktree_roots,
)

RECEIPT_ENV = "H3_GATE_LINK_RECEIPT"
KEY_ENV = "H3_GATE_LINK_KEY"
PID_ENV = "H3_GATE_LINK_PID"
MAX_AGE = 7200
MAX_BYTES = 4096


def _process_identity(pid: int) -> str:
    if sys.platform != "win32":
        raise ValueError("Windows live gate identity is required")
    if pid <= 0:
        raise ValueError("Windows live gate identity is required")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ValueError("gate process is unavailable")
    try:
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        code = wintypes.DWORD()
        if not kernel.GetProcessTimes(handle, created, exited, kernel_time, user_time):
            raise ValueError("gate creation identity is unavailable")
        if not kernel.GetExitCodeProcess(handle, code) or code.value != 259:
            raise ValueError("gate process is no longer live")
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL, timeout=30)


def _source(root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(_git(root, "rev-parse", "HEAD"))
    index = Path(os.fsdecode(_git(root, "rev-parse", "--git-path", "index")).strip())
    digest.update(hashlib.sha256((root / index).read_bytes()).digest())
    names = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for raw in sorted(set(names.split(b"\0")) - {b""}):
        relative = os.fsdecode(raw)
        path = root / relative
        if not path.resolve().is_relative_to(root) or is_reparse_point(path):
            raise ValueError("source path is not an owned regular file")
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("source path is not a regular file")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _topology(root: Path, findings: list[LinkFinding]) -> str:
    roots = worktree_roots(root)
    if root not in roots:
        raise ValueError("registered workspace identity is unavailable")
    rows = []
    for finding in findings:
        try:
            Path(_io_path(finding.target)).stat()
            exists = True
        except (FileNotFoundError, NotADirectoryError):
            exists = False
        if finding.leaves_container and exists:
            raise ValueError("escaping live link cannot be attested")
        rows.append((str(finding.path), str(finding.target), str(finding.container), exists))
    return hashlib.sha256(_canonical([sorted(map(str, roots)), sorted(rows)])).hexdigest()


def _location(root: Path) -> Path:
    raw = os.environ[RECEIPT_ENV]
    path = Path(raw)
    base = root / ".tmp/gate-link-receipts"
    if not path.is_absolute() or path.parent.parent != base or path.name != "receipt.json":
        raise ValueError("receipt location is not a fresh gate directory")
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("receipt location escapes its workspace")
    current = path
    while current != root:
        if os.path.lexists(current) and is_reparse_point(current):
            raise ValueError("receipt ancestor is a reparse point")
        current = current.parent
    return path


def _key() -> bytes:
    value = os.environ[KEY_ENV]
    if len(value) != 64 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ValueError("receipt key has an invalid shape")
    return bytes.fromhex(value)


def issue(root: Path, findings: list[LinkFinding]) -> None:
    """Called only after the gate's strict link census has passed."""
    root = root.resolve()
    path = _location(root)
    pid = int(os.environ[PID_ENV])
    payload = dict(
        schema="gate-link-census/1",
        root=os.path.normcase(str(root)),
        source=_source(root),
        topology=_topology(root, findings),
        pid=pid,
        process=_process_identity(pid),
        run=path.parent.name,
        issued=time.time(),
    )
    envelope = dict(
        payload=payload, signature=hmac.new(_key(), _canonical(payload), "sha256").hexdigest()
    )
    path.parent.mkdir(parents=True, exist_ok=False)
    with path.open("xb") as stream:
        stream.write(_canonical(envelope))


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len(dict(pairs)) != len(pairs):
        raise ValueError("duplicate receipt keys")
    return dict(pairs)


def authentic_current_receipt(root: Path) -> bool:
    """False means run the original guard, never treat missing proof as permission."""
    try:
        root = root.resolve()
        path = _location(root)
        if path.stat().st_size > MAX_BYTES:
            return False
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return False
        value = json.loads(raw, object_pairs_hook=_closed_object)
        if type(value) is not dict or set(value) != {"payload", "signature"}:
            return False
        payload = value["payload"]
        fields = {"schema", "root", "source", "topology", "pid", "process", "run", "issued"}
        if type(payload) is not dict or set(payload) != fields:
            return False
        if type(value["signature"]) is not str or not hmac.compare_digest(
            value["signature"], hmac.new(_key(), _canonical(payload), "sha256").hexdigest()
        ):
            return False
        # CRITICAL: a signed old PASS alone cannot skip today's scan. Bind the live gate,
        # source and complete fresh topology, including ignored links and dangling targets.
        if payload["schema"] != "gate-link-census/1" or payload["root"] != os.path.normcase(
            str(root)
        ):
            return False
        if type(payload["pid"]) is not int or payload["pid"] != int(os.environ[PID_ENV]):
            return False
        if (
            type(payload["issued"]) not in (int, float)
            or not 0 <= time.time() - payload["issued"] <= MAX_AGE
        ):
            return False
        if payload["run"] != path.parent.name or payload["process"] != _process_identity(
            payload["pid"]
        ):
            return False
        if payload["source"] != _source(root):
            return False
        return bool(
            payload["topology"] == _topology(root, find_links(root, repo_root=root, strict=True))
        )
    except (OSError, ValueError, KeyError, TypeError, RecursionError, subprocess.SubprocessError):
        return False


def cleanup(root: Path, raw: str) -> None:
    previous = os.environ.get(RECEIPT_ENV)
    os.environ[RECEIPT_ENV] = raw
    try:
        path = _location(root.resolve())
        path.unlink(missing_ok=True)
        if path.parent.exists():
            path.parent.rmdir()
    finally:
        if previous is None:
            os.environ.pop(RECEIPT_ENV, None)
        else:
            os.environ[RECEIPT_ENV] = previous


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["cleanup"])
    parser.add_argument("path")
    args = parser.parse_args()
    try:
        cleanup(Path(__file__).resolve().parents[1], args.path)
        return 0
    except (OSError, ValueError):
        print("Gate receipt cleanup refused an unsafe or nonempty location.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
