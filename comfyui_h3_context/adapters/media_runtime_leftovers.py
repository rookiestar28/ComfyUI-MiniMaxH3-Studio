"""Explicit, bounded recovery of earlier media setup leftovers. Never acquires tools."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .executable_admission import ExecutableAdmissionError, pin_exact_executable
from .media_runtime_pin import MediaRuntimePin
from .media_runtime_resolution import MediaRuntimePrivateLayout
from .segment_artifact_store import (
    ArtifactStoreError,
    _is_link_or_reparse,
    _safe_unlink,
    _validated_directories,
    _windows_open_existing_file,
    _windows_open_new_file,
)

MAX_STAGING_ENTRIES = 64
MAX_SWEPT_JOBS = 16
STAGING_DIRECTORY = "staging"
LOCK_FILENAME = "install.lock"
ARCHIVE_FILENAME = "archive.zip.part"
TREE_DIRECTORY = "tree"
RETIRED_DIRECTORY = "retired"
SOURCE_NOTICE_FILENAME = "source.json"
JOB_TOKEN = re.compile(r"[0-9a-f]{32}")
PinFunction = Callable[..., Any]


class RecoveryError(RuntimeError):
    CODES = frozenset(
        {"private_root_invalid", "reclaim_unsafe", "setup_busy", "unsupported_platform"}
    )

    def __init__(self, code: str) -> None:
        if code not in self.CODES:
            raise ValueError("unknown recovery error code")
        self.code = code
        super().__init__(code)


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecoveryError("private_root_invalid") from exc


def cleanup_job_directory(
    job_directory: Path, manifest: MediaRuntimePin, *, include_retired: bool
) -> None:
    """Remove only the files an installer job names, then the directories it created.

    IMPORTANT: this is deliberately not a recursive delete. Unknown content is left where it is,
    and a `retired/` tree is removed only by the job that published its replacement: a job whose
    restore failed still holds the previous runtime there, and a later sweep must not erase it.
    """

    trees = [TREE_DIRECTORY, RETIRED_DIRECTORY] if include_retired else [TREE_DIRECTORY]
    names = [*(member.published_name for member in manifest.retained), SOURCE_NOTICE_FILENAME]
    files = [job_directory / ARCHIVE_FILENAME]
    for tree in trees:
        files.extend(job_directory / tree / name for name in names)
    for path in files:
        try:
            _safe_unlink(path, missing_ok=True, maximum_links=1)
        except ArtifactStoreError:
            pass
    directories: list[Path] = []
    for tree in trees:
        nested = sorted(
            {Path(name).parent for name in names if Path(name).parent != Path(".")},
            key=lambda value: len(value.parts),
            reverse=True,
        )
        directories.extend(job_directory / tree / directory for directory in nested)
        directories.append(job_directory / tree)
    directories.append(job_directory)
    for directory in directories:
        if not _plain_entry(directory, directory=True):
            continue
        try:
            directory.rmdir()
        except OSError:
            pass


def _acquire_install_lock(staging: Path) -> int:
    import msvcrt

    locking = getattr(msvcrt, "locking")  # noqa: B009
    path = staging / LOCK_FILENAME
    descriptor: int | None = None
    try:
        with _validated_directories(staging):
            # OSError must be translated inside the pinned block: the context manager would
            # otherwise re-code a sharing violation as an unsafe store entry.
            try:
                try:
                    descriptor = _windows_open_new_file(path)
                except OSError:
                    if _lstat_or_none(path) is None:
                        raise
                    descriptor = _windows_open_existing_file(path)
                opened = os.fstat(descriptor)
                current = path.lstat()
            except OSError as exc:
                raise RecoveryError("setup_busy") from exc
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _is_link_or_reparse(path, current)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise RecoveryError("private_root_invalid")
    except BaseException as exc:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(exc, ArtifactStoreError):
            raise RecoveryError("private_root_invalid") from exc
        raise
    if descriptor is None:
        raise RecoveryError("setup_busy")
    try:
        # CRITICAL: a byte-range lock, not a marker file. The OS releases it when the owning
        # process dies, so a crashed install can never leave every later install refused, and
        # a second ComfyUI process sharing this user directory cannot publish concurrently.
        locking(descriptor, getattr(msvcrt, "LK_NBLCK"), 1)  # noqa: B009
    except OSError as exc:
        os.close(descriptor)
        raise RecoveryError("setup_busy") from exc
    return descriptor


def _release_install_lock(descriptor: int) -> None:
    import msvcrt

    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        getattr(msvcrt, "locking")(descriptor, getattr(msvcrt, "LK_UNLCK"), 1)  # noqa: B009
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _plain_entry(path: Path, *, directory: bool) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if _is_link_or_reparse(path, metadata):
        return False
    return stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)


def _holds_named_member(retired: Path, manifest: MediaRuntimePin) -> bool:
    names = [*(member.published_name for member in manifest.retained), SOURCE_NOTICE_FILENAME]
    for name in names:
        parts = Path(name).parts
        current = retired
        # Every intermediate component must itself be a plain directory: a junctioned `bin`
        # would otherwise report a member that lives outside the private root.
        for part in parts[:-1]:
            current = current / part
            if not _plain_entry(current, directory=True):
                break
        else:
            if _plain_entry(current / parts[-1], directory=False):
                return True
    return False


def parked_runtime_trees(
    layout: MediaRuntimePrivateLayout, manifest: MediaRuntimePin
) -> tuple[Path, ...]:
    """Job directories whose `retired/` tree still holds a named runtime member. Read-only.

    A tree is parked when a publication could not restore it or its cleanup could not remove it.
    The scan is bounded by `MAX_STAGING_ENTRIES`, creates nothing and never follows a link or
    junction; an unreadable staging directory reports nothing rather than guessing.
    """

    staging = layout.media_runtime / STAGING_DIRECTORY
    if not _plain_entry(staging, directory=True):
        return ()
    parked: list[Path] = []
    try:
        with os.scandir(staging) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_STAGING_ENTRIES:
                    break
                if JOB_TOKEN.fullmatch(entry.name) is None:
                    continue
                job = Path(entry.path)
                retired = job / RETIRED_DIRECTORY
                if (
                    _plain_entry(job, directory=True)
                    and _plain_entry(retired, directory=True)
                    and _holds_named_member(retired, manifest)
                ):
                    parked.append(job)
    except OSError:
        return ()
    return tuple(sorted(parked))


def abandoned_setup_jobs(
    layout: MediaRuntimePrivateLayout, manifest: MediaRuntimePin
) -> tuple[Path, ...]:
    """Read-only scan of named temporary files; parked copies remain a separate recovery state."""
    staging = layout.media_runtime / STAGING_DIRECTORY
    if not _plain_entry(staging, directory=True):
        return ()
    jobs: list[Path] = []
    try:
        with os.scandir(staging) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_STAGING_ENTRIES:
                    break
                job = Path(entry.path)
                if JOB_TOKEN.fullmatch(entry.name) is None or not _plain_entry(job, directory=True):
                    continue
                if _plain_entry(job / ARCHIVE_FILENAME, directory=False) or (
                    _plain_entry(job / TREE_DIRECTORY, directory=True)
                    and _holds_named_member(job / TREE_DIRECTORY, manifest)
                ):
                    jobs.append(job)
    except OSError:
        return ()
    return tuple(sorted(jobs))


def reclaim_parked_runtimes(
    layout: MediaRuntimePrivateLayout,
    manifest: MediaRuntimePin,
    *,
    pin: PinFunction = pin_exact_executable,
) -> int:
    """Reclaim only known names; old parked tools need a verified managed replacement."""
    if os.name != "nt":
        raise RecoveryError("unsupported_platform")
    staging = layout.media_runtime / STAGING_DIRECTORY
    if _lstat_or_none(staging) is None:
        return 0
    if not _plain_entry(staging, directory=True):
        raise RecoveryError("private_root_invalid")
    lock = _acquire_install_lock(staging)
    try:
        parked = parked_runtime_trees(layout, manifest)
        abandoned = abandoned_setup_jobs(layout, manifest)
        # CRITICAL: share the earlier setup lock and hold both replacement pins through deletion.
        # A parked tree may be the only usable copy; an old process may still own an abandoned job.
        with ExitStack() as pins:
            if parked:
                target = layout.runtime_root / manifest.profile_component
                if not _plain_entry(target, directory=True) or not _plain_entry(
                    layout.managed_bin, directory=True
                ):
                    raise RecoveryError("reclaim_unsafe")
                for member in manifest.retained:
                    if member.executable:
                        try:
                            pins.enter_context(
                                pin(
                                    layout.managed_bin / Path(member.published_name).name,
                                    member.sha256,
                                )
                            )
                        except ExecutableAdmissionError as exc:
                            raise RecoveryError("reclaim_unsafe") from exc
            jobs = sorted(set(parked) | set(abandoned))[:MAX_SWEPT_JOBS]
            for job in jobs:
                cleanup_job_directory(job, manifest, include_retired=job in parked)
        remaining = set(parked_runtime_trees(layout, manifest)) | set(
            abandoned_setup_jobs(layout, manifest)
        )
        return sum(job not in remaining for job in jobs)
    finally:
        _release_install_lock(lock)
