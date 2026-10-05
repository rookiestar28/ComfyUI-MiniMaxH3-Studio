"""Validation, extraction and atomic publication of the one fixed managed media runtime.

The installer turns the pinned Gyan full-build archive into `media-runtime/runtime/<profile>/` under
the host's private H3 root. It never executes a downloaded file, never extracts a member it did not
name, and never publishes a tree whose executables fail the same exact pin every later spawn uses.

Order of work, each step refusing with one closed code before the next begins:

1. take the cross-process install lock and clean earlier jobs' known leftovers;
2. check free space, stream the archive into exclusive staging and compare its SHA-256;
3. validate the whole central directory, then stream only the retained members, hashing each;
4. pin both executables, then rename the finished tree into place, restoring any retired tree on
   failure.

Nothing here decides whether an install is wanted; the setup service does that from the resolver.
A `retired/` tree that a job could not restore or remove stays parked until the user explicitly
reclaims it, which happens only under the same lock and after the published pair is re-verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import zipfile
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.av_reconstruction import qualified_ffmpeg_capability
from ..core.safe_paths import UnsafePathError, ensure_directory, validate_directory
from .executable_admission import (
    ExecutableAdmissionError,
    FileIdentity,
    pin_exact_executable,
    windows_open_read_pin,
)
from .media_runtime_download import DownloadError, DownloadPort
from .media_runtime_resolution import MANAGED_PROFILE_COMPONENT, MediaRuntimePrivateLayout
from .segment_artifact_store import (
    ArtifactStoreError,
    _identity,
    _is_link_or_reparse,
    _safe_unlink,
    _validated_directories,
    _windows_open_existing_file,
    _windows_open_new_file,
    _write_new_file,
)

RELEASE_TAG = "2026-02-26-git-6695528af6"
SOURCE_URL = (
    "https://github.com/GyanD/codexffmpeg/releases/download/"
    f"{RELEASE_TAG}/ffmpeg-{RELEASE_TAG}-full_build.zip"
)
RELEASE_PAGE_URL = f"https://github.com/GyanD/codexffmpeg/releases/tag/{RELEASE_TAG}"
SOURCE_LABEL = "Gyan.dev FFmpeg full build"
LICENSE_NAME = "GPL-3.0-or-later"
SOURCE_NOTICE_SCHEMA = "h3.context.managed_media_runtime_source.v1"
MAX_ARCHIVE_MEMBERS = 64
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_MEMBER_NAME_CHARS = 260
SPACE_MARGIN_BYTES = 64 * 1024 * 1024
MAX_STAGING_ENTRIES = 64
MAX_SWEPT_JOBS = 16
CHUNK_BYTES = 1024 * 1024
STAGING_DIRECTORY = "staging"
LOCK_FILENAME = "install.lock"
ARCHIVE_FILENAME = "archive.zip.part"
TREE_DIRECTORY = "tree"
RETIRED_DIRECTORY = "retired"
SOURCE_NOTICE_FILENAME = "source.json"
JOB_TOKEN = re.compile(r"[0-9a-f]{32}")
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_FORBIDDEN_NAME_CHARACTERS = frozenset('<>"|?*')


class InstallerError(RuntimeError):
    """Closed, content-free installation failure."""

    CODES = frozenset(
        {
            "archive_invalid",
            "digest_mismatch",
            "insufficient_space",
            "permission_denied",
            "private_root_invalid",
            "publication_failed",
            "reclaim_unsafe",
            "runtime_busy",
            "setup_busy",
            "unsupported_platform",
            "write_failed",
        }
        | DownloadError.CODES
    )

    def __init__(self, code: str) -> None:
        if code not in self.CODES:
            raise ValueError("unknown installer error code")
        self.code = code
        super().__init__(code)


# --------------------------------------------------------------------------------------------
# Manifest


@dataclass(frozen=True, slots=True)
class RetainedMember:
    archive_name: str
    published_name: str
    size: int
    sha256: str
    executable: bool


@dataclass(frozen=True, slots=True)
class ManagedRuntimeManifest:
    profile_component: str
    source_url: str
    release_page_url: str
    source_label: str
    license_name: str
    archive_bytes: int
    archive_sha256: str
    member_count: int
    expanded_bytes: int
    top_directory: str
    retained: tuple[RetainedMember, ...]

    @property
    def retained_bytes(self) -> int:
        return sum(member.size for member in self.retained)


def managed_runtime_manifest() -> ManagedRuntimeManifest:
    """The one approved profile; executable digests come from the qualified capability."""

    capability = qualified_ffmpeg_capability()
    return ManagedRuntimeManifest(
        profile_component=MANAGED_PROFILE_COMPONENT,
        source_url=SOURCE_URL,
        release_page_url=RELEASE_PAGE_URL,
        source_label=SOURCE_LABEL,
        license_name=LICENSE_NAME,
        archive_bytes=246_558_061,
        archive_sha256=(
            "ad18ecdedfca6d51c28cf12f2c96287c"  # pragma: allowlist secret
            "e1150686578b75bff2efcd06ff34bb30"  # pragma: allowlist secret
        ),
        member_count=49,
        expanded_bytes=680_107_427,
        top_directory=f"ffmpeg-{RELEASE_TAG}-full_build/",
        retained=(
            RetainedMember(
                "bin/ffmpeg.exe",
                "bin/ffmpeg.exe",
                222_400_512,
                capability.ffmpeg_sha256.casefold(),
                True,
            ),
            RetainedMember(
                "bin/ffprobe.exe",
                "bin/ffprobe.exe",
                222_195_200,
                capability.ffprobe_sha256.casefold(),
                True,
            ),
            RetainedMember(
                "LICENSE",
                "LICENSE.txt",
                35_147,
                "8ceb4b9ee5adedde47b31e975c1d90c7"  # pragma: allowlist secret
                "3ad27b6b165a1dcd80c7c545eb65b903",  # pragma: allowlist secret
                False,
            ),
            RetainedMember(
                "README.txt",
                "README.txt",
                45_177,
                "4c76fc9cfe8ffe67b741331f7db9f1ab"  # pragma: allowlist secret
                "71e9804eefcb2928148edb9044bd59ca",  # pragma: allowlist secret
                False,
            ),
        ),
    )


# --------------------------------------------------------------------------------------------
# Archive validation


def _member_parts(name: object, top_directory: str) -> tuple[tuple[str, ...], bool] | None:
    """Split one archive name into safe components under the top directory, or refuse it."""

    if type(name) is not str or not name or len(name) > MAX_MEMBER_NAME_CHARS:
        return None
    if "\\" in name or ":" in name or name.startswith("/"):
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in name):
        return None
    directory = name.endswith("/")
    parts = name[:-1].split("/") if directory else name.split("/")
    for part in parts:
        if (
            part in {"", ".", ".."}
            or part != part.rstrip(" .")
            or part.split(".", 1)[0].casefold() in _RESERVED_NAMES
            or any(character in _FORBIDDEN_NAME_CHARACTERS for character in part)
        ):
            return None
    if f"{parts[0]}/" != top_directory:
        return None
    return tuple(parts), directory


def validate_archive_members(
    infos: Sequence[zipfile.ZipInfo], manifest: ManagedRuntimeManifest
) -> Mapping[str, zipfile.ZipInfo]:
    """Admit the whole central directory before any member byte is read.

    SECURITY: every member is judged, not only the retained ones. A traversal, link, duplicate or
    case-colliding name anywhere in the archive means the bytes are not the reviewed release, and
    an archive that is not the reviewed release is not partially trusted.
    """

    if len(infos) > MAX_ARCHIVE_MEMBERS or len(infos) != manifest.member_count:
        raise InstallerError("archive_invalid")
    declared = 0
    exact: set[str] = set()
    folded: set[str] = set()
    directories: set[str] = set()
    files: set[str] = set()
    admitted: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        split = _member_parts(info.filename, manifest.top_directory)
        if split is None:
            raise InstallerError("archive_invalid")
        parts, directory = split
        key = "/".join(parts)
        if key in exact or key.casefold() in folded:
            raise InstallerError("archive_invalid")
        exact.add(key)
        folded.add(key.casefold())
        mode = (info.external_attr >> 16) & 0xFFFF
        if (
            info.flag_bits & 0x1
            or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or stat.S_ISLNK(mode)
            or (directory and info.file_size != 0)
            or info.file_size < 0
        ):
            raise InstallerError("archive_invalid")
        declared += info.file_size
        if declared > MAX_EXPANDED_BYTES:
            raise InstallerError("archive_invalid")
        for depth in range(1, len(parts)):
            directories.add("/".join(parts[:depth]).casefold())
        if directory:
            directories.add(key.casefold())
        else:
            files.add(key.casefold())
        admitted[key] = info
    if declared != manifest.expanded_bytes or files & directories:
        raise InstallerError("archive_invalid")
    retained: dict[str, zipfile.ZipInfo] = {}
    for member in manifest.retained:
        key = manifest.top_directory + member.archive_name
        found = admitted.get(key)
        if found is None or found.is_dir() or found.file_size != member.size:
            raise InstallerError("archive_invalid")
        retained[member.archive_name] = found
    return retained


# --------------------------------------------------------------------------------------------
# Owned file writing


def _os_failure(exc: OSError) -> InstallerError:
    winerror = getattr(exc, "winerror", None)
    if winerror in {39, 112} or exc.errno == 28:
        return InstallerError("insufficient_space")
    if winerror in {32, 33}:
        return InstallerError("runtime_busy")
    if winerror == 5 or exc.errno in {1, 13}:
        return InstallerError("permission_denied")
    return InstallerError("write_failed")


def _rename_failure(exc: OSError) -> InstallerError:
    # A directory rename that Windows refuses with access-denied or a sharing violation almost
    # always means a file inside is open -- a running tool -- not a permission problem the user
    # could fix, so it is reported as busy.
    if getattr(exc, "winerror", None) in {5, 32, 33}:
        return InstallerError("runtime_busy")
    return _os_failure(exc)


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallerError("private_root_invalid") from exc


class _NewFileWriter:
    """One exclusively created regular file, streamed, hashed and bounded while written."""

    def __init__(self, path: Path, *, maximum_bytes: int, overflow_code: str) -> None:
        self.total = 0
        self._path = path
        self._maximum = maximum_bytes
        self._overflow = overflow_code
        self._digest = hashlib.sha256()
        self._descriptor: int | None = None
        self._created: tuple[int, int] | None = None
        try:
            parent = validate_directory(path.parent)
        except UnsafePathError as exc:
            raise InstallerError("private_root_invalid") from exc
        if parent != path.parent or path.name in {"", ".", ".."}:
            raise InstallerError("private_root_invalid")
        try:
            self._descriptor = (
                _windows_open_new_file(path)
                if os.name == "nt"
                else os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
                )
            )
            opened = os.fstat(self._descriptor)
            self._created = (opened.st_dev, opened.st_ino)
            current = path.lstat()
        except OSError as exc:
            self.discard()
            raise _os_failure(exc) from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _is_link_or_reparse(path, current)
            or (current.st_dev, current.st_ino) != self._created
        ):
            self.discard()
            raise InstallerError("private_root_invalid")

    def write(self, chunk: bytes) -> None:
        if self._descriptor is None:
            raise InstallerError("write_failed")
        self.total += len(chunk)
        if self.total > self._maximum:
            raise InstallerError(self._overflow)
        self._digest.update(chunk)
        view = memoryview(chunk)
        while view:
            try:
                written = os.write(self._descriptor, view)
            except OSError as exc:
                raise _os_failure(exc) from exc
            if written <= 0:
                raise InstallerError("write_failed")
            view = view[written:]

    def finish(self) -> tuple[FileIdentity, str]:
        descriptor = self._descriptor
        if descriptor is None:
            raise InstallerError("write_failed")
        try:
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            current = self._path.lstat()
        except OSError as exc:
            raise _os_failure(exc) from exc
        finally:
            self._descriptor = None
            os.close(descriptor)
        if (
            opened.st_nlink != 1
            or _is_link_or_reparse(self._path, current)
            or _identity(current) != _identity(opened)
        ):
            raise InstallerError("private_root_invalid")
        return _identity(opened), self._digest.hexdigest()

    def discard(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if self._created is None:
            return
        try:
            current = self._path.lstat()
        except OSError:
            return
        if (current.st_dev, current.st_ino) != self._created:
            return
        try:
            _safe_unlink(self._path, missing_ok=True, maximum_links=1)
        except ArtifactStoreError:
            pass


def _mkdir_owned(path: Path) -> None:
    try:
        path.mkdir()
        metadata = path.lstat()
    except OSError as exc:
        raise _os_failure(exc) from exc
    if _is_link_or_reparse(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise InstallerError("private_root_invalid")


def cleanup_job_directory(
    job_directory: Path, manifest: ManagedRuntimeManifest, *, include_retired: bool
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
        try:
            directory.rmdir()
        except OSError:
            pass


# --------------------------------------------------------------------------------------------
# Install lock


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
                raise InstallerError("setup_busy") from exc
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _is_link_or_reparse(path, current)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise InstallerError("private_root_invalid")
    except BaseException as exc:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(exc, ArtifactStoreError):
            raise InstallerError("private_root_invalid") from exc
        raise
    if descriptor is None:
        raise InstallerError("setup_busy")
    try:
        # CRITICAL: a byte-range lock, not a marker file. The OS releases it when the owning
        # process dies, so a crashed install can never leave every later install refused, and
        # a second ComfyUI process sharing this user directory cannot publish concurrently.
        locking(descriptor, getattr(msvcrt, "LK_NBLCK"), 1)  # noqa: B009
    except OSError as exc:
        os.close(descriptor)
        raise InstallerError("setup_busy") from exc
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


# --------------------------------------------------------------------------------------------
# Parked runtime recovery


def _plain_entry(path: Path, *, directory: bool) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if _is_link_or_reparse(path, metadata):
        return False
    return stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)


def _holds_named_member(retired: Path, manifest: ManagedRuntimeManifest) -> bool:
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
    layout: MediaRuntimePrivateLayout, manifest: ManagedRuntimeManifest
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


def reclaim_parked_runtimes(
    layout: MediaRuntimePrivateLayout,
    manifest: ManagedRuntimeManifest,
    *,
    pin: PinFunction = pin_exact_executable,
) -> int:
    """Remove parked trees' named members after verifying the published replacement.

    Returns how many parked job directories no longer hold a named member.

    SECURITY/CRITICAL: this runs only on an explicit user action, only under the install lock,
    and only once the published pair passes the same exact pins a spawn uses. A parked tree can
    be the only copy of the runtime (a publication whose restore failed); deleting it without a
    verified replacement leaves no runtime at all. Deletion keeps `cleanup_job_directory`'s
    named-member, no-recursion and link-refusal rules; nothing here deletes automatically.
    """

    if os.name != "nt":
        raise InstallerError("unsupported_platform")
    staging = layout.media_runtime / STAGING_DIRECTORY
    if _lstat_or_none(staging) is None:
        return 0
    if not _plain_entry(staging, directory=True):
        raise InstallerError("private_root_invalid")
    lock = _acquire_install_lock(staging)
    try:
        parked = parked_runtime_trees(layout, manifest)
        if not parked:
            return 0
        target = layout.runtime_root / manifest.profile_component
        if not _plain_entry(target, directory=True) or not _plain_entry(
            layout.managed_bin, directory=True
        ):
            raise InstallerError("reclaim_unsafe")
        for member in manifest.retained:
            if not member.executable:
                continue
            try:
                with pin(layout.managed_bin / Path(member.published_name).name, member.sha256):
                    pass
            except ExecutableAdmissionError as exc:
                raise InstallerError("reclaim_unsafe") from exc
        for job in parked:
            cleanup_job_directory(job, manifest, include_retired=True)
        remaining = set(parked_runtime_trees(layout, manifest))
        return sum(1 for job in parked if job not in remaining)
    finally:
        _release_install_lock(lock)


# --------------------------------------------------------------------------------------------
# Installer


ProgressCallback = Callable[[str, int, int], None]
PinFunction = Callable[..., Any]


class ManagedRuntimeInstaller:
    """One installation attempt. `run` either publishes the tree or raises `InstallerError`."""

    def __init__(
        self,
        layout: MediaRuntimePrivateLayout,
        *,
        manifest: ManagedRuntimeManifest,
        downloader: DownloadPort,
        job_token: str,
        cancelled: threading.Event,
        progress: ProgressCallback,
        disk_usage: Callable[[str], Any] = shutil.disk_usage,
        pin: PinFunction = pin_exact_executable,
    ) -> None:
        if type(job_token) is not str or JOB_TOKEN.fullmatch(job_token) is None:
            raise ValueError("job_token")
        self._layout = layout
        self._manifest = manifest
        self._downloader = downloader
        self._token = job_token
        self._cancelled = cancelled
        self._progress = progress
        self._disk_usage = disk_usage
        self._pin = pin

    def __repr__(self) -> str:
        return "<ManagedRuntimeInstaller>"

    @property
    def staging_root(self) -> Path:
        return self._layout.media_runtime / STAGING_DIRECTORY

    def run(self) -> None:
        if os.name != "nt":
            raise InstallerError("unsupported_platform")
        try:
            staging = ensure_directory(self.staging_root)
        except UnsafePathError as exc:
            raise InstallerError("private_root_invalid") from exc
        lock = self._acquire_lock(staging)
        try:
            self._sweep(staging)
            job_directory = staging / self._token
            try:
                with self._validated(staging):
                    _mkdir_owned(job_directory)
            except ArtifactStoreError as exc:
                raise InstallerError("private_root_invalid") from exc
            published = False
            try:
                self._check_space()
                identity = self._download(job_directory)
                self._extract(job_directory, identity)
                self._verify_tree(job_directory / TREE_DIRECTORY)
                self._publish(job_directory)
                published = True
            finally:
                cleanup_job_directory(job_directory, self._manifest, include_retired=published)
        finally:
            self._release_lock(lock)

    # -- steps -----------------------------------------------------------------------------

    @staticmethod
    def _validated(*directories: Path) -> Any:
        return _validated_directories(*directories)

    def _check_cancel(self) -> None:
        if self._cancelled.is_set():
            raise InstallerError("cancelled")

    def _acquire_lock(self, staging: Path) -> int:
        return _acquire_install_lock(staging)

    def _release_lock(self, descriptor: int) -> None:
        _release_install_lock(descriptor)

    def _sweep(self, staging: Path) -> None:
        """Clean earlier jobs' named leftovers; bounded, and only under the install lock."""

        candidates: list[Path] = []
        try:
            with os.scandir(staging) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_STAGING_ENTRIES or len(candidates) >= MAX_SWEPT_JOBS:
                        break
                    if entry.name == self._token or JOB_TOKEN.fullmatch(entry.name) is None:
                        continue
                    path = Path(entry.path)
                    metadata = path.lstat()
                    if _is_link_or_reparse(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
                        continue
                    candidates.append(path)
        except OSError as exc:
            raise InstallerError("private_root_invalid") from exc
        for candidate in candidates:
            cleanup_job_directory(candidate, self._manifest, include_retired=False)

    def _check_space(self) -> None:
        required = self._manifest.archive_bytes + self._manifest.retained_bytes + SPACE_MARGIN_BYTES
        try:
            usage = self._disk_usage(str(self._layout.media_runtime))
        except OSError as exc:
            raise InstallerError("private_root_invalid") from exc
        if int(getattr(usage, "free", 0)) < required:
            raise InstallerError("insufficient_space")

    def _download(self, job_directory: Path) -> FileIdentity:
        manifest = self._manifest
        self._check_cancel()
        self._progress("downloading", 0, manifest.archive_bytes)
        writer = _NewFileWriter(
            job_directory / ARCHIVE_FILENAME,
            maximum_bytes=manifest.archive_bytes,
            overflow_code="size_mismatch",
        )
        try:
            self._downloader.download(
                manifest.source_url,
                expected_bytes=manifest.archive_bytes,
                sink=writer.write,
                cancelled=self._cancelled,
                progress=lambda done: self._progress("downloading", done, manifest.archive_bytes),
            )
            identity, digest = writer.finish()
        except DownloadError as exc:
            writer.discard()
            raise InstallerError(exc.code) from exc
        except BaseException:
            writer.discard()
            raise
        if digest != manifest.archive_sha256:
            raise InstallerError("digest_mismatch")
        return identity

    def _extract(self, job_directory: Path, archive_identity: FileIdentity) -> None:
        manifest = self._manifest
        self._check_cancel()
        self._progress("extracting", 0, manifest.retained_bytes)
        archive = job_directory / ARCHIVE_FILENAME
        tree = job_directory / TREE_DIRECTORY
        try:
            with _validated_directories(job_directory):
                try:
                    descriptor = windows_open_read_pin(archive)
                except OSError as exc:
                    raise InstallerError("archive_invalid") from exc
                with os.fdopen(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if opened.st_nlink != 1 or _identity(opened) != archive_identity:
                        raise InstallerError("archive_invalid")
                    with zipfile.ZipFile(stream) as archive_file:
                        members = validate_archive_members(archive_file.infolist(), manifest)
                        _mkdir_owned(tree)
                        for directory in sorted(
                            {
                                Path(member.published_name).parent
                                for member in manifest.retained
                                if Path(member.published_name).parent != Path(".")
                            },
                            key=lambda value: len(value.parts),
                        ):
                            _mkdir_owned(tree / directory)
                        done = 0
                        for member in manifest.retained:
                            self._extract_member(
                                archive_file, members[member.archive_name], tree, member, done
                            )
                            done += member.size
                        self._write_source_notice(tree)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
            raise InstallerError("archive_invalid") from exc
        except ArtifactStoreError as exc:
            raise InstallerError("private_root_invalid") from exc
        except OSError as exc:
            raise _os_failure(exc) from exc

    def _extract_member(
        self,
        archive_file: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        tree: Path,
        member: RetainedMember,
        done_before: int,
    ) -> None:
        writer = _NewFileWriter(
            tree / member.published_name, maximum_bytes=member.size, overflow_code="archive_invalid"
        )
        try:
            with archive_file.open(info) as source:
                while True:
                    self._check_cancel()
                    chunk = source.read(min(CHUNK_BYTES, member.size + 1 - writer.total))
                    if not chunk:
                        break
                    writer.write(chunk)
                    self._progress(
                        "extracting", done_before + writer.total, self._manifest.retained_bytes
                    )
            _identity_unused, digest = writer.finish()
        except BaseException:
            writer.discard()
            raise
        if writer.total != member.size or digest != member.sha256:
            raise InstallerError("archive_invalid")

    def _write_source_notice(self, tree: Path) -> None:
        manifest = self._manifest
        payload = json.dumps(
            {
                "schema": SOURCE_NOTICE_SCHEMA,
                "profile": manifest.profile_component,
                "source_label": manifest.source_label,
                "release_page": manifest.release_page_url,
                "archive_sha256": manifest.archive_sha256,
                "license": manifest.license_name,
                "license_file": "LICENSE.txt",
                "source_notice_file": "README.txt",
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        try:
            _write_new_file(tree / SOURCE_NOTICE_FILENAME, payload + b"\n")
        except ArtifactStoreError as exc:
            raise InstallerError("write_failed") from exc

    def _admitted_pair(self, bin_directory: Path) -> bool:
        for member in self._manifest.retained:
            if not member.executable:
                continue
            try:
                with self._pin(bin_directory / Path(member.published_name).name, member.sha256):
                    pass
            except ExecutableAdmissionError:
                return False
        return True

    def _verify_tree(self, tree: Path) -> None:
        self._check_cancel()
        self._progress("verifying", 0, 0)
        for member in self._manifest.retained:
            if not member.executable:
                continue
            try:
                with self._pin(tree / member.published_name, member.sha256):
                    pass
            except ExecutableAdmissionError as exc:
                raise InstallerError("archive_invalid") from exc

    def _publish(self, job_directory: Path) -> None:
        self._check_cancel()
        self._progress("publishing", 0, 0)
        tree = job_directory / TREE_DIRECTORY
        retired = job_directory / RETIRED_DIRECTORY
        target = self._layout.runtime_root / self._manifest.profile_component
        try:
            runtime_root = ensure_directory(self._layout.runtime_root)
        except UnsafePathError as exc:
            raise InstallerError("private_root_invalid") from exc
        try:
            with _validated_directories(job_directory, runtime_root):
                existing = _lstat_or_none(target)
                if existing is not None:
                    if _is_link_or_reparse(target, existing) or not stat.S_ISDIR(existing.st_mode):
                        raise InstallerError("private_root_invalid")
                    if self._admitted_pair(target / "bin"):
                        # Someone already placed the exact pair; keep it and discard ours.
                        return
                    try:
                        os.rename(target, retired)
                    except OSError as exc:
                        raise _rename_failure(exc) from exc
                try:
                    # CRITICAL: publication is one directory rename inside pinned parents. A copy,
                    # or per-file moves, would expose a half-populated profile that the resolver
                    # can pick up between steps.
                    os.rename(tree, target)
                except OSError as exc:
                    if existing is not None:
                        try:
                            # CRITICAL: restore the retired tree before reporting. Returning with
                            # it still in staging would leave no runtime directory at all, and the
                            # next sweep is not allowed to bring it back.
                            os.rename(retired, target)
                        except OSError as restore_exc:
                            raise InstallerError("publication_failed") from restore_exc
                    raise _rename_failure(exc) from exc
        except ArtifactStoreError as exc:
            raise InstallerError("private_root_invalid") from exc


__all__ = [
    "LICENSE_NAME",
    "RELEASE_PAGE_URL",
    "SOURCE_LABEL",
    "SOURCE_URL",
    "InstallerError",
    "ManagedRuntimeInstaller",
    "ManagedRuntimeManifest",
    "RetainedMember",
    "cleanup_job_directory",
    "managed_runtime_manifest",
    "parked_runtime_trees",
    "reclaim_parked_runtimes",
    "validate_archive_members",
]
