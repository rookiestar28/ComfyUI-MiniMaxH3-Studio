"""Explicit-root process-local final artifacts with verified atomic publication.

Only the render service may use private stages and paths. Browser handles and byte-serving
authorization are intentionally absent; a store receipt alone does not qualify any renderer.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, SupportsIndex

from ..core.authoring_render_jobs import RenderJobLimits
from ..core.authoring_render_receipts import AuthoringRenderReceiptV1, MeasuredRenderOutput
from ..core.canonical import canonical_bytes
from ..core.safe_paths import UnsafePathError, validate_directory, validate_regular_file
from .segment_artifact_store import (
    ArtifactStoreError,
    _read_regular_bytes,
    _validated_directories,
    _write_new_file,
)

_DEFAULT_LIMITS = RenderJobLimits()
_JOB = re.compile(r"render-[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SESSION = re.compile(r"h3-render-[0-9a-f]{32}\Z")
_DIRECTORY = re.compile(r"(?:stage|artifact)-[0-9a-f]{32}\Z")
_INPUT = re.compile(r"[0-9a-f]{32}\.(?:bin|rgba|mp4|wav|txt|ffgraph)\Z")
_SUFFIXES = frozenset({".bin", ".rgba", ".mp4", ".wav", ".txt", ".ffgraph"})
_MAX_STAGE_FILES = 4096
_MAX_SESSIONS = 64
_MARKER_SCHEMA = "h3.authoring.render_store_session.v1"


class RenderStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in {
            "invalid_request",
            "store_closed",
            "store_unavailable",
            "stage_unavailable",
            "output_unavailable",
            "output_invalid",
            "receipt_invalid",
            "job_conflict",
            "resource_limit",
            "cancelled",
            "unsafe_store",
            "cleanup_failed",
        }:
            code = "store_unavailable"
        self.code = code
        super().__init__(code)


def _directory_identity(path: Path) -> tuple[int, int]:
    metadata = validate_directory(path).lstat()
    return metadata.st_dev, metadata.st_ino


def _file_names(directory: Path, maximum: int) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(names) >= maximum:
                raise RenderStoreError("resource_limit")
            names.append(entry.name)
    return tuple(names)


def _remove_directory(path: Path, identity: tuple[int, int]) -> None:
    # CRITICAL: cleanup is a flat closed inventory, never rmtree/resolve/glob traversal.
    # Unknown files or redirected directories are retained for explicit recovery, not followed.
    if _DIRECTORY.fullmatch(path.name) is None or _directory_identity(path) != identity:
        raise RenderStoreError("unsafe_store")
    with _validated_directories(path):
        names = _file_names(path, _MAX_STAGE_FILES + 2)
        if any(
            name not in {"output.mp4", "receipt.json"} and _INPUT.fullmatch(name) is None
            for name in names
        ):
            raise RenderStoreError("unsafe_store")
        for name in names:
            entry = validate_regular_file(path / name)
            if entry.lstat().st_nlink != 1:
                raise RenderStoreError("unsafe_store")
        for name in names:
            validate_regular_file(path / name).unlink()
    if _directory_identity(path) != identity:
        raise RenderStoreError("unsafe_store")
    path.rmdir()


def _lock_marker(path: Path) -> int:
    admitted = validate_regular_file(path, maximum_bytes=1024)
    before = admitted.lstat()
    descriptor = os.open(
        admitted, os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        opened = os.fstat(descriptor)
        if before.st_nlink != 1 or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise RenderStoreError("unsafe_store")
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _recover_orphans(root: Path) -> None:
    for name in _file_names(root, _MAX_SESSIONS):
        if _SESSION.fullmatch(name) is None:
            continue
        session = validate_directory(root / name)
        identity = _directory_identity(session)
        marker = session / "owner.json"
        try:
            descriptor = _lock_marker(marker)
        except (PermissionError, BlockingIOError):
            continue  # A live process retains its marker lock; never touch its artifacts.
        try:
            # Read through the descriptor that owns the byte lock. Reopening the locked
            # marker on Windows fails even in the same process and prevents orphan recovery.
            document = os.read(descriptor, 1025)
            if document != canonical_bytes({"schema": _MARKER_SCHEMA, "session": name}):
                raise RenderStoreError("unsafe_store")
            with _validated_directories(root, session):
                children = _file_names(session, _DEFAULT_LIMITS.max_jobs + 1)
                if any(
                    child != "owner.json" and _DIRECTORY.fullmatch(child) is None
                    for child in children
                ):
                    raise RenderStoreError("unsafe_store")
                for child in children:
                    if child != "owner.json":
                        directory = session / child
                        _remove_directory(directory, _directory_identity(directory))
        finally:
            os.close(descriptor)
        if _directory_identity(session) != identity:
            raise RenderStoreError("unsafe_store")
        validate_regular_file(marker, maximum_bytes=1024).unlink()
        session.rmdir()


class RenderStage:
    def __init__(
        self, store: RenderOutputStore, job_id: str, request_fingerprint: str, directory: Path
    ):
        self._store = store
        self.job_id = job_id
        self.request_fingerprint = request_fingerprint
        self._directory = directory
        self._identity = _directory_identity(directory)
        self._cancelled = threading.Event()
        self._committing = False
        self._files: set[str] = {"output.mp4"}

    def __repr__(self) -> str:
        return "<AuthoringRenderStage opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("render stages are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("render stages are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("render stages are not serializable")

    @property
    def output_path(self) -> Path:
        return self._directory / "output.mp4"

    def cancel(self) -> None:
        with self._store._lock:
            if self._store._stages.get(self.job_id) is self:
                self._cancelled.set()

    def write_input(self, payload: bytes, *, suffix: str = ".bin") -> Path:
        with self._store._lock:
            self._store._stage(self)
            if (
                type(payload) is not bytes
                or not payload
                or type(suffix) is not str
                or suffix not in _SUFFIXES
            ):
                raise RenderStoreError("invalid_request")
            if (
                len(self._files) >= _MAX_STAGE_FILES
                or self.check_budget() + len(payload) > self._store._limits.max_staging_bytes
            ):
                raise RenderStoreError("resource_limit")
            path = self._directory / (uuid.uuid4().hex + suffix)
            _write_new_file(path, payload)
            self._files.add(path.name)
            return path

    def check_budget(self) -> int:
        with self._store._lock:
            self._store._stage(self)
            total = 0
            for name in _file_names(self._directory, _MAX_STAGE_FILES + 2):
                if name not in self._files:
                    raise RenderStoreError("unsafe_store")
                path = validate_regular_file(self._directory / name)
                metadata = path.lstat()
                if metadata.st_nlink != 1:
                    raise RenderStoreError("unsafe_store")
                total += metadata.st_size
                if total > self._store._limits.max_staging_bytes:
                    raise RenderStoreError("resource_limit")
            return total


@dataclass(frozen=True, slots=True, repr=False)
class PrivateRenderArtifact:
    job_id: str
    receipt: AuthoringRenderReceiptV1
    _path: Path
    _identity: tuple[int, int]
    _expires: float

    def __repr__(self) -> str:
        return "<PrivateAuthoringRenderArtifact opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("render artifacts are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("render artifacts are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("render artifacts are not serializable")


class RenderOutputStore:
    def __init__(
        self,
        root: Path,
        *,
        limits: RenderJobLimits = _DEFAULT_LIMITS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(limits) is not RenderJobLimits or not callable(clock):
            raise RenderStoreError("invalid_request")
        self._lock = threading.RLock()
        self._limits = limits
        self._clock = clock
        self._closed = False
        self._stages: dict[str, RenderStage] = {}
        self._artifacts: dict[str, PrivateRenderArtifact] = {}
        self._marker_descriptor: int | None = None
        try:
            self._root = validate_directory(root)
            with _validated_directories(self._root):
                _recover_orphans(self._root)
                self._session = self._root / ("h3-render-" + uuid.uuid4().hex)
                self._session.mkdir(mode=0o700)
                self._session_identity = _directory_identity(self._session)
                marker = self._session / "owner.json"
                _write_new_file(
                    marker,
                    canonical_bytes({"schema": _MARKER_SCHEMA, "session": self._session.name}),
                )
                self._marker_descriptor = _lock_marker(marker)
        except (OSError, ValueError, ArtifactStoreError):
            raise RenderStoreError("store_unavailable") from None

    @property
    def staging_count(self) -> int:
        with self._lock:
            return len(self._stages)

    @property
    def retained_count(self) -> int:
        with self._lock:
            return len(self._artifacts)

    def _ready(self) -> None:
        if self._closed:
            raise RenderStoreError("store_closed")
        if _directory_identity(self._session) != self._session_identity:
            raise RenderStoreError("unsafe_store")

    def _stage(self, stage: RenderStage) -> None:
        self._ready()
        if type(stage) is not RenderStage or self._stages.get(stage.job_id) is not stage:
            raise RenderStoreError("stage_unavailable")
        if stage._cancelled.is_set():
            raise RenderStoreError("cancelled")
        if _directory_identity(stage._directory) != stage._identity:
            raise RenderStoreError("unsafe_store")

    def begin(self, job_id: str, request_fingerprint: str) -> RenderStage:
        with self._lock:
            self._ready()
            if (
                type(job_id) is not str
                or _JOB.fullmatch(job_id) is None
                or type(request_fingerprint) is not str
                or _DIGEST.fullmatch(request_fingerprint) is None
            ):
                raise RenderStoreError("invalid_request")
            if job_id in self._stages or job_id in self._artifacts:
                raise RenderStoreError("job_conflict")
            if len(self._stages) >= self._limits.running_jobs:
                raise RenderStoreError("resource_limit")
            with _validated_directories(self._root, self._session):
                directory = self._session / ("stage-" + uuid.uuid4().hex)
                directory.mkdir(mode=0o700)
                stage = RenderStage(self, job_id, request_fingerprint, directory)
                self._stages[job_id] = stage
                return stage

    def discard(self, stage: RenderStage) -> None:
        with self._lock:
            if self._stages.get(stage.job_id) is not stage:
                return
            stage.cancel()
            try:
                with _validated_directories(self._root, self._session):
                    _remove_directory(stage._directory, stage._identity)
            except (OSError, UnsafePathError, ArtifactStoreError):
                raise RenderStoreError("cleanup_failed") from None
            del self._stages[stage.job_id]

    def commit(
        self,
        stage: RenderStage,
        receipt: AuthoringRenderReceiptV1,
        *,
        verify_output: Callable[[Path], MeasuredRenderOutput],
        on_publish: Callable[[PrivateRenderArtifact], None] | None = None,
    ) -> PrivateRenderArtifact:
        with self._lock:
            if type(stage) is not RenderStage or self._stages.get(stage.job_id) is not stage:
                raise RenderStoreError("stage_unavailable")
            if stage._committing:
                raise RenderStoreError("stage_unavailable")
            stage._committing = True
        # Probe outside the publication lock: cancellation/status must not wait for media I/O.
        # Only the locked final stage check and map insertion decide whether cancel or publish wins.
        try:
            with self._lock:
                self._stage(stage)
                if (
                    type(receipt) is not AuthoringRenderReceiptV1
                    or receipt.request.fingerprint != stage.request_fingerprint
                ):
                    raise RenderStoreError("receipt_invalid")
                stage.check_budget()
            observed = verify_output(stage.output_path)
        except BaseException as exc:
            self.discard(stage)
            if isinstance(exc, RenderStoreError):
                raise
            if isinstance(exc, Exception):
                raise RenderStoreError("store_unavailable") from None
            raise
        with self._lock:
            destination: Path | None = None
            try:
                self._stage(stage)
                stage.check_budget()
                if type(observed) is not MeasuredRenderOutput or observed != receipt.observed:
                    raise RenderStoreError("output_invalid")
                self.prune()
                if (
                    len(self._artifacts) >= self._limits.retained_outputs
                    or sum(item.receipt.observed.byte_length for item in self._artifacts.values())
                    + observed.byte_length
                    > self._limits.retained_output_bytes
                ):
                    raise RenderStoreError("resource_limit")
                self._verify_body(stage.output_path, receipt)
                payload = canonical_bytes(receipt.to_wire())
                if len(payload) > self._limits.max_receipt_bytes:
                    raise RenderStoreError("receipt_invalid")
                with _validated_directories(self._root, self._session, stage._directory):
                    # Encoder return is not publication: flush/close the complete output and
                    # receipt, discard inputs, then rename only the verified pair atomically.
                    with stage.output_path.open("r+b") as stream:
                        os.fsync(stream.fileno())
                    _write_new_file(stage._directory / "receipt.json", payload)
                    for name in tuple(stage._files - {"output.mp4"}):
                        validate_regular_file(stage._directory / name).unlink()
                    stage._files = {"output.mp4", "receipt.json"}
                self._stage(stage)
                destination = self._session / ("artifact-" + uuid.uuid4().hex)
                with _validated_directories(self._root, self._session):
                    if destination.exists() or destination.is_symlink():
                        raise RenderStoreError("job_conflict")
                    os.replace(stage._directory, destination)
                    if _directory_identity(destination) != stage._identity:
                        raise RenderStoreError("unsafe_store")
                    self._verify_body(destination / "output.mp4", receipt)
                    if stage._cancelled.is_set():
                        raise RenderStoreError("cancelled")
                    now = self._clock()
                    if not math.isfinite(now):
                        raise RenderStoreError("invalid_request")
                    artifact = PrivateRenderArtifact(
                        stage.job_id,
                        receipt,
                        destination / "output.mp4",
                        stage._identity,
                        now + self._limits.retention_seconds,
                    )
                    self._artifacts[stage.job_id] = artifact
                    # CRITICAL: service terminalization shares this lock and transaction.
                    # A late cancel/invalid receipt must roll back the artifact, not expose
                    # a successful file for a failed or cancelled immutable job record.
                    if on_publish is not None:
                        on_publish(artifact)
                    del self._stages[stage.job_id]
                    return artifact
            except BaseException as exc:
                self._artifacts.pop(stage.job_id, None)
                # The map is published last. Even an interrupted rename cannot leave a late
                # success callback or a discoverable partial artifact behind.
                # CRITICAL: a name collision is not our renamed stage. Only the admitted
                # directory identity authorizes destination cleanup; preserve foreign entries.
                if (
                    destination is not None
                    and destination.exists()
                    and _directory_identity(destination) == stage._identity
                ):
                    with _validated_directories(self._root, self._session):
                        _remove_directory(destination, stage._identity)
                    self._stages.pop(stage.job_id, None)
                else:
                    self.discard(stage)
                if isinstance(exc, RenderStoreError):
                    raise
                if isinstance(exc, Exception):
                    raise RenderStoreError("store_unavailable") from None
                raise

    def _verify_body(self, path: Path, receipt: AuthoringRenderReceiptV1) -> bytes:
        body = _read_regular_bytes(path, maximum_bytes=self._limits.max_output_bytes)
        if (
            len(body) != receipt.observed.byte_length
            or "sha256:" + hashlib.sha256(body).hexdigest() != receipt.observed.output_fingerprint
        ):
            raise RenderStoreError("output_invalid")
        return body

    def read_output(self, job_id: str, request_fingerprint: str) -> bytes:
        with self._lock:
            self._ready()
            self.prune()
            if type(job_id) is not str or type(request_fingerprint) is not str:
                raise RenderStoreError("output_unavailable")
            artifact = self._artifacts.get(job_id)
            if artifact is None or artifact.receipt.request.fingerprint != request_fingerprint:
                raise RenderStoreError("output_unavailable")
            try:
                if _directory_identity(artifact._path.parent) != artifact._identity:
                    raise RenderStoreError("output_invalid")
                return self._verify_body(artifact._path, artifact.receipt)
            except (OSError, UnsafePathError, ArtifactStoreError):
                raise RenderStoreError("output_invalid") from None

    def prune(self) -> None:
        with self._lock:
            self._ready()
            now = self._clock()
            if not math.isfinite(now):
                raise RenderStoreError("invalid_request")
            for job_id, artifact in tuple(self._artifacts.items()):
                if now >= artifact._expires:
                    with _validated_directories(self._root, self._session):
                        _remove_directory(artifact._path.parent, artifact._identity)
                    del self._artifacts[job_id]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._ready()
            for stage in tuple(self._stages.values()):
                self.discard(stage)
            with _validated_directories(self._root, self._session):
                for artifact in tuple(self._artifacts.values()):
                    _remove_directory(artifact._path.parent, artifact._identity)
                self._artifacts.clear()
                if self._marker_descriptor is not None:
                    os.close(self._marker_descriptor)
                    self._marker_descriptor = None
                validate_regular_file(self._session / "owner.json", maximum_bytes=1024).unlink()
            if _directory_identity(self._session) != self._session_identity:
                raise RenderStoreError("unsafe_store")
            self._session.rmdir()
            self._closed = True
