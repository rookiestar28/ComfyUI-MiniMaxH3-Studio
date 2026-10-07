"""M25-21 ``NLE-STRESS-RENDER-V1``: the repository-local backend render companion.

The hardening plan (Section 14.1) separates the 600-second editing/preview fixture from a
60-second *rendering* companion, and Section 14.2 requires that the backend RSS measurement come
from "a dedicated repository-local backend fixture process and its owned child process tree ...
never the shared ComfyUI host process", sampled at 100 ms and reported as the peak increment over
the initialized idle baseline. This module is that fixture process.

What it actually does, in one process:

* builds the accepted render runtime exactly as the M25-20 render stage does -- the same
  digest-pinned renderer/probe pair, the same synthesized content-free sources, the same real
  ``RuntimeComfySourceFactory``/``ReferenceRegistry`` -- by importing that stage rather than
  re-implementing it (``scripts.nle_semantic_render``);
* extends the accepted base composition to the frozen companion shape: 1,440 frames at 24/1,
  320 x 180, with representative primary-video, video-overlay, image-overlay and text operations
  and the profile's embedded audio following the primary video;
* drives eight explicitly created jobs through one real ``AuthoringOutputRegistry`` -- create,
  status, download -- keeping at most one running and one queued, which is what the product's own
  ``RenderJobLimits`` admits;
* samples its own working set and that of its owned child process tree every 100 ms, and reports
  the peak increment over the baseline taken once the runtime is initialized and idle.

The measurement subject is this process: the job store and the executor that supervises the
renderer child. The child's own peak commit and the limits the product enforces on it are recorded
separately, never folded into the parent number.

Nothing here relaxes a product limit, and the script asserts no threshold: it reports what it
observed and the caller's report decides. A job that fails is reported as a failed job.

Run with ``--ffmpeg``/``--ffprobe`` (or ``H3_CONTEXT_AUTHORIZED_FFMPEG_PATH`` /
``H3_CONTEXT_AUTHORIZED_FFPROBE_PATH``) and ``--out``; the pair is digest-verified against the
packaged renderer qualification by the pinning code itself and is never discovered from ``PATH``.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.adapters.authoring_native_renderer import (  # noqa: E402
    NativeAuthoringRenderer,
)
from comfyui_h3_context.adapters.authoring_output_service import (  # noqa: E402
    AuthoringOutputRegistry,
)
from comfyui_h3_context.adapters.authoring_render_process import (  # noqa: E402
    RenderProcessObservation,
)
from comfyui_h3_context.adapters.authoring_render_service import (  # noqa: E402
    AuthoringRenderService,
)
from comfyui_h3_context.adapters.authoring_render_source import (  # noqa: E402
    PreparedAuthoringHistory,
    claim_render_source,
)
from comfyui_h3_context.adapters.authoring_render_store import RenderOutputStore  # noqa: E402
from comfyui_h3_context.core.authoring_output_protocol import (  # noqa: E402
    OUTPUT_CREATE_SCHEMA,
    AuthoringOutputCreate,
)
from comfyui_h3_context.core.authoring_render_jobs import RenderJobLimits  # noqa: E402
from comfyui_h3_context.core.composition_contract import (  # noqa: E402
    OUTPUT_PROFILE_ID,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.semantic_conformance_cases import (  # noqa: E402
    CORPUS_BASE,
    IMAGE_ASSET,
    normalize_base,
)
from comfyui_h3_context.core.semantic_conformance_media import profile_for  # noqa: E402
from scripts import nle_semantic_conformance as extractor  # noqa: E402
from scripts.nle_semantic_render import (  # noqa: E402
    BASE_VIDEO_ASSETS,
    RenderStageError,
    _build_runtime,
    _cleanup_scratch_root,
    _CorpusOutputWorkspace,
    _mint_row_receipt,
)

SCHEMA: Final = "h3.context.nle_stress_render_companion.v1"
FIXTURE: Final = "NLE-STRESS-RENDER-V1"

#: The frozen companion shape (plan Section 14.1): 60 seconds at 24/1.
COMPANION_FRAMES: Final = 1_440
#: Eight explicitly created jobs, in batches that keep one running and one queued.
COMPANION_JOBS: Final = 8
BATCH: Final = 2
#: Plan Section 14.2: sample process RSS at 100 ms.
SAMPLE_INTERVAL_S: Final = 0.1
TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled"})


class CompanionError(RuntimeError):
    """The companion could not run at all; a blocked run is never an observed zero."""


class CompanionRunFailure(CompanionError):
    """A failed run whose privacy-safe partial observation document must be retained."""

    def __init__(self, reason: str, document: dict[str, Any]) -> None:
        super().__init__(reason)
        self.document = document


class _ProcessBoundaryObserver:
    """Collect handle-bound renderer observations without exposing commands or private paths."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[RenderProcessObservation] = []

    def observe(self, row: RenderProcessObservation) -> None:
        if type(row) is not RenderProcessObservation:
            raise CompanionError("process_observation_invalid")
        with self._lock:
            self._rows.append(row)

    def document(self) -> dict[str, object]:
        with self._lock:
            rows = tuple(self._rows)
        invalid = [
            row
            for row in rows
            if not row.job_membership_verified
            or not row.working_set_valid
            or not row.limits_verified
        ]
        peak = max((row.peak_working_set_bytes for row in rows), default=0)
        holder = next((row for row in rows if row.peak_working_set_bytes == peak), None)
        return {
            "sampling_valid": bool(rows) and not invalid,
            "sampling_failure_reasons": sorted(
                {row.working_set_failure or "process_observation_invalid" for row in invalid}
            ),
            "completed_processes": len(rows),
            "measured_processes": sum(row.working_set_valid for row in rows),
            "unobserved_processes": sum(not row.working_set_valid for row in rows),
            "working_set_sample_count": sum(row.working_set_sample_count for row in rows),
            "peak_working_set_bytes": peak,
            "peak_working_set_holder": (
                ""
                if holder is None
                else f"{holder.executable_fingerprint[:23]}#{holder.process_id}@{holder.run_id}"
            ),
            "peak_committed_bytes": max((row.peak_committed_bytes for row in rows), default=0),
            "job_membership_verified": bool(rows)
            and all(row.job_membership_verified for row in rows),
            "limits_verified": bool(rows) and all(row.limits_verified for row in rows),
            "processes": [
                {
                    "session_id": row.session_id,
                    "run_id": row.run_id,
                    "process_id": row.process_id,
                    "executable_fingerprint": row.executable_fingerprint,
                    "job_membership_verified": row.job_membership_verified,
                    "working_set_valid": row.working_set_valid,
                    "working_set_failure": row.working_set_failure,
                    "working_set_sample_count": row.working_set_sample_count,
                    "peak_working_set_bytes": row.peak_working_set_bytes,
                    "peak_committed_bytes": row.peak_committed_bytes,
                    "limits_verified": row.limits_verified,
                }
                for row in rows
            ],
        }


# -------------------------------------------------------------------------------------------
# Process accounting
# -------------------------------------------------------------------------------------------


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = (
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    )


class _ProcessEntry32(ctypes.Structure):
    _fields_ = (
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_char * 260),
    )


class _FileTime(ctypes.Structure):
    _fields_ = (("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32))


_TH32CS_SNAPPROCESS: Final = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_PROCESS_VM_READ: Final = 0x0010
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def resolve_owned_descendants(
    parents: Mapping[int, int],
    owner: int,
    created: Callable[[int], int | None],
    *,
    max_depth: int = 8,
) -> tuple[frozenset[int], frozenset[int]]:
    """The processes genuinely descended from ``owner``.

    ``parents`` is ``{pid: parent_pid}`` for every visible process; ``created`` answers a pid's
    creation timestamp in any unit that orders correctly, or ``None`` when it cannot be read.

    Returns ``(owned, unverified)``: everything descended from ``owner``, and the subset whose
    creation time could not be read.

    GUARD (B-M2545-44), three invariants, plus an honest note on what is and is not established.

    **A parent link is rejected only when a READABLE creation time contradicts it.**
    ``th32ParentProcessID`` is recorded at creation and never cleared, and Windows reissues PIDs
    aggressively, so a dead ancestor's PID can be handed to a process this companion really did
    spawn, grafting an unrelated tree onto ours. The counter this feeds is ``PeakWorkingSetSize``,
    a LIFETIME high-water mark, so one mis-attributed sample would import a stranger's whole peak.

    That hazard is real in principle and **has never been observed here.** Four instrumented
    reproductions -- 11,230 samples, one standalone and three under a concurrent browser lane --
    show the parent-only rule this replaced selecting ZERO foreign processes, with both rules
    returning byte-identical maxima in every run. The 2,661,179,392-byte ceiling failure that
    prompted this code was not reproduced and is unattributed. Do not read this guard as its
    diagnosis; `.planning` holds the open entry.

    **An unreadable creation time must NOT drop the process.** This is what the capture did prove,
    against an earlier version of this very function. Requiring a readable timestamp silently
    excluded real children: ``ffprobe.exe`` and its ``conhost.exe`` exit within milliseconds, so
    ``OpenProcess`` answers ``ERROR_INVALID_PARAMETER`` and the timestamp is gone -- 40 such
    records across the four runs, every one genuinely ours. Excluding them under-counts, which is
    the direction that HIDES a breach, and it did so silently. Such a process is kept and returned
    in ``unverified`` so the caller can report it; it is never dropped and never becomes a zero.

    **The walk descends from the owner; it does not ascend from every process.** ``created`` costs
    an ``OpenProcess`` per call, and this runs ten times a second, so climbing from all few hundred
    processes on the machine would burn thousands of handle opens per second inside the very
    interval whose memory it is measuring. Descending touches the owner plus the candidate children
    of processes already owned, which on this fixture is a handful.
    """

    children_of: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        if pid != parent:
            children_of.setdefault(parent, []).append(pid)
    owned: set[int] = set()
    unverified: set[int] = set()
    owner_created = created(owner)
    # The owner is this process, so its own timestamp is always readable; if it is not, the whole
    # sample is unusable and says so rather than returning an empty, innocent-looking result.
    if owner_created is None:
        raise CompanionError("owner_identity_unavailable")
    frontier: list[tuple[int, int, int]] = [(owner, owner_created, 0)]
    while frontier:
        pid, pid_created, depth = frontier.pop()
        if depth >= max_depth:
            continue
        for child in children_of.get(pid, ()):
            if child == owner or child in owned:
                continue
            child_created = created(child)
            if child_created is None:
                # Already gone. Keep it -- the parent link is all the evidence that exists, and
                # discarding it under-counts our own short-lived children -- and KEEP DESCENDING,
                # carrying this process's own bound forward. `ffprobe.exe` spawns a `conhost.exe`,
                # so stopping here drops a real grandchild: measured, that left three of them
                # excluded per run after the first half of this repair. The ordering guard is not
                # weakened by continuing, because the bound handed down is the last timestamp that
                # was actually read.
                owned.add(child)
                unverified.add(child)
                frontier.append((child, pid_created, depth + 1))
                continue
            if child_created < pid_created:
                continue
            owned.add(child)
            frontier.append((child, child_created, depth + 1))
    return frozenset(owned), frozenset(unverified)


class _WindowsMemory:
    """Working-set reads for this process and the children it owns.

    GUARD: the descendant walk is re-taken on every sample rather than cached. A renderer child
    lives only for the span of one job, so a cached child list measures a process that has already
    exited (its handle reports its last values forever) and misses the next job's child entirely --
    which would silently report a child peak from whichever job happened to run first.
    """

    def __init__(self) -> None:
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self._pid = os.getpid()

    def _counters(self, handle: int) -> _ProcessMemoryCounters | None:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        ok = self._psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb
        )
        return counters if ok else None

    def own(self) -> int:
        handle = self._kernel.GetCurrentProcess()
        counters = self._counters(handle)
        if counters is None:
            raise CompanionError("own_working_set_unavailable")
        return int(counters.WorkingSetSize)

    def _created(self, pid: int) -> int | None:
        """The process's creation time as a FILETIME, or ``None`` when it cannot be read.

        A process whose creation time cannot be read is dropped rather than assumed: without it the
        recycled-PID check below has nothing to compare, and a process this companion cannot open
        is one whose memory it cannot read either, so nothing measurable is lost.
        """

        handle = self._kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            created = _FileTime()
            exited, kernel_time, user_time = _FileTime(), _FileTime(), _FileTime()
            ok = self._kernel.GetProcessTimes(
                ctypes.c_void_p(handle),
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            )
            if not ok:
                return None
            return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        finally:
            self._kernel.CloseHandle(ctypes.c_void_p(handle))

    def _snapshot(self) -> tuple[dict[int, int], dict[int, str]]:
        """``({pid: parent}, {pid: image name})``, straight from one process snapshot.

        No creation time is read here: that costs a handle open per process, and only the few pids
        the descent actually visits need one. See `resolve_owned_descendants`.
        """

        snapshot = self._kernel.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot in (0, _INVALID_HANDLE):
            raise CompanionError("process_snapshot_unavailable")
        try:
            entry = _ProcessEntry32()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32)
            if not self._kernel.Process32First(ctypes.c_void_p(snapshot), ctypes.byref(entry)):
                raise CompanionError("process_snapshot_unavailable")
            parents: dict[int, int] = {}
            names: dict[int, str] = {}
            while True:
                pid = int(entry.th32ProcessID)
                parents[pid] = int(entry.th32ParentProcessID)
                names[pid] = entry.szExeFile.decode("ascii", "replace")
                if not self._kernel.Process32Next(ctypes.c_void_p(snapshot), ctypes.byref(entry)):
                    break
            return parents, names
        finally:
            self._kernel.CloseHandle(ctypes.c_void_p(snapshot))

    def children(self) -> tuple[int, int, str, int]:
        """``(working set, peak working set, peak holder, unreadable)`` over the owned children.

        The peak holder is carried out because the peak is the number a ceiling is judged against,
        and a ceiling failure that does not say WHICH process produced it cannot be told apart from
        a mis-attribution -- which is exactly why `g10`'s 2,661,179,392 bytes can no longer be
        explained. It is an image name and a pid: no path, no command line, no argument.

        ``unreadable`` counts owned processes whose counters could not be read at all. It is
        reported rather than absorbed, because a child that could not be measured is missing data,
        not zero bytes.
        """

        parents, names = self._snapshot()
        # One creation-time read per pid per sample at most; the cache keeps a repeated visit free.
        cache: dict[int, int | None] = {}

        def created(pid: int) -> int | None:
            if pid not in cache:
                cache[pid] = self._created(pid)
            return cache[pid]

        owned, _unverified = resolve_owned_descendants(parents, self._pid, created)
        working = peak = unreadable = 0
        holder = ""
        for pid in owned:
            handle = self._kernel.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, False, pid
            )
            if not handle:
                unreadable += 1
                continue
            try:
                counters = self._counters(handle)
                if counters is None:
                    unreadable += 1
                    continue
                working += int(counters.WorkingSetSize)
                if int(counters.PeakWorkingSetSize) > peak:
                    peak = int(counters.PeakWorkingSetSize)
                    holder = f"{names.get(pid, 'unknown')}#{pid}"
            finally:
                self._kernel.CloseHandle(ctypes.c_void_p(handle))
        return working, peak, holder, unreadable


class _PosixMemory:
    """The same two reads on a POSIX host, from ``/proc``."""

    def __init__(self) -> None:
        self._pid = os.getpid()
        # `os.sysconf` exists only on the hosts where this class is ever constructed, and the
        # Windows stubs this repository type-checks against do not declare it.
        sysconf = cast(Callable[[str], int], os.sysconf)  # type: ignore[attr-defined]
        self._page = sysconf("SC_PAGE_SIZE")

    def _statm(self, pid: int) -> int:
        try:
            fields = Path(f"/proc/{pid}/statm").read_text(encoding="ascii").split()
        except OSError:
            return 0
        return int(fields[1]) * self._page

    def own(self) -> int:
        value = self._statm(self._pid)
        if value <= 0:
            raise CompanionError("own_working_set_unavailable")
        return value

    def children(self) -> tuple[int, int, str, int]:
        # `/proc/<pid>/task/*/children` is the kernel's own live child list, so it carries none of
        # the recycled-PID hazard the Windows snapshot does and needs no creation-time check.
        try:
            children = Path(f"/proc/{self._pid}/task").iterdir()
        except OSError:
            return 0, 0, "", 1
        working = 0
        holder = ""
        unreadable = 0
        for task in children:
            try:
                pids = (task / "children").read_text(encoding="ascii").split()
            except OSError:
                unreadable += 1
                continue
            for pid in pids:
                measured = self._statm(int(pid))
                if measured <= 0:
                    unreadable += 1
                    continue
                working += measured
                holder = holder or f"pid#{pid}"
        return working, working, holder, unreadable


def _memory_reader() -> _WindowsMemory | _PosixMemory:
    return _WindowsMemory() if sys.platform == "win32" else _PosixMemory()


class _Sampler:
    """Samples the fixture process and its owned children on a fixed interval."""

    def __init__(self, reader: _WindowsMemory | _PosixMemory) -> None:
        self._reader = reader
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.baseline = 0
        self.samples = 0
        self.own_peak = 0
        self.tree_peak = 0
        self.child_peak = 0
        self.child_peak_holder = ""
        self.unreadable_children = 0
        self.failure_reasons: list[str] = []
        self._failure: CompanionError | None = None

    def start(self, baseline: int) -> None:
        self.baseline = baseline
        self.own_peak = baseline
        self.tree_peak = baseline
        self._thread = threading.Thread(target=self._run, name="rss-sampler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.wait(SAMPLE_INTERVAL_S):
                own = self._reader.own()
                children, child_peak, holder, unreadable = self._reader.children()
                self.samples += 1
                self.unreadable_children += unreadable
                self.own_peak = max(self.own_peak, own)
                self.tree_peak = max(self.tree_peak, own + children)
                if child_peak > self.child_peak:
                    self.child_peak = child_peak
                    self.child_peak_holder = holder
        except BaseException as exc:
            reason = str(exc) if isinstance(exc, CompanionError) else type(exc).__name__
            # IMPORTANT (B-M2545-50): a daemon exception cannot leave old peaks looking like a
            # completed sample series. Retain the reason and make `stop` fail the caller.
            self.failure_reasons.append(reason)
            self._failure = CompanionError(reason)
            self._stop.set()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():
                raise CompanionError("sampler_stop_timeout")
        if self._failure is not None:
            raise self._failure


# -------------------------------------------------------------------------------------------
# The companion composition
# -------------------------------------------------------------------------------------------


def _source_frames(asset_id: str) -> int:
    profile = profile_for(asset_id)
    if profile is None:
        raise CompanionError("source_profile_undeclared")
    return profile.frame_count


def companion_wire(base: Mapping[str, Any]) -> dict[str, Any]:
    """The 1,440-frame companion composition, built from the accepted base wire.

    Every clip is a copy of the base fixture's own clip on that track, so transforms, crops,
    opacities, blends, text and effect payloads are the accepted ones; only the timeline extent,
    the clip identities and their placement change. Clips never exceed the frames their source
    actually has, so the render plan binds real decoded source rather than a silently padded tail.
    """

    wire: dict[str, Any] = copy.deepcopy(dict(base))
    normalize_base(wire)
    templates = {clip["track_id"]: clip for clip in wire["clips"]}
    clips: list[dict[str, Any]] = []

    def place(track: str, index: int, start: int, duration: int, source_start: int = 0) -> None:
        template = templates.get(track)
        if template is None:
            raise CompanionError("track_template_missing")
        clip = copy.deepcopy(template)
        clip["clip_id"] = f"stress-{track}-{index:03d}"
        clip["start_frame"] = start
        clip["duration_frames"] = duration
        clip["source_start_frame"] = source_start
        clips.append(clip)

    primary_frames = _source_frames("vid-primary")
    index = 0
    for start in range(0, COMPANION_FRAMES, primary_frames):
        place("track-primary", index, start, min(primary_frames, COMPANION_FRAMES - start))
        index += 1

    # GUARD: the overlays are representative, not dense, and that is a measured constraint rather
    # than a stylistic choice. The product caps a renderer child at 600 CPU seconds
    # (``RenderJobLimits.child_cpu_seconds``) across two threads, and a first calibration of this
    # companion with ten video, four image and six text overlay clips spent the whole ceiling on a
    # single job and was refused with ``resource_limit`` -- the product's limit doing exactly its
    # job. The plan freezes the companion's duration, frame rate, geometry and sources and asks
    # for "representative text/visual/embedded-audio operations", so one window of each overlay
    # kind is what this builds. Never densify these loops to make the composition look busier: it
    # buys nothing the rows measure and it costs the whole job.
    overlay_frames = _source_frames("vid-overlay")
    place("track-video", 0, 96, overlay_frames)
    place("track-image", 0, 480, 96)
    place("track-text", 0, 960, 48)

    wire["clips"] = clips
    wire["output"] = {**wire["output"], "duration_frames": COMPANION_FRAMES}
    wire["workspace_revision"] = int(wire["workspace_revision"]) + 1
    wire["timeline_revision"] = int(wire["timeline_revision"]) + 1
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return wire


# -------------------------------------------------------------------------------------------
# The workload
# -------------------------------------------------------------------------------------------


def _wait(
    registry: AuthoringOutputRegistry,
    handle: str,
    workspace: str,
    deadline: float,
    observe: Callable[[], None],
) -> dict[str, object]:
    """Wait for one job to reach a terminal phase, or report the phase it was stuck in.

    A deadline cancels the job and returns its last status rather than raising: a companion that
    ran out of time has still observed everything before that point, and a run that reports
    nothing at all cannot be told apart from one that never started.

    GUARD: ``observe`` runs on every poll, and the batch's running/queued maxima come from there
    rather than from a read taken right after ``create``. A job is still queued in the instant it
    is accepted, so sampling only at creation reports a maximum of zero running jobs -- a number
    that satisfies "at most one running" without ever having seen the lane run at all.
    """

    status = registry.status(handle, workspace)
    while status["phase"] not in TERMINAL:
        if time.monotonic() >= deadline:
            registry.cancel(handle, workspace)
            return {**dict(status), "failure": "companion_deadline"}
        time.sleep(0.2)
        observe()
        status = registry.status(handle, workspace)
    return dict(status)


def _failure_reason(error: BaseException) -> str:
    text = str(error)
    return text if re.fullmatch(r"[a-z0-9_]{1,128}", text) else type(error).__name__


def _companion_document(
    *,
    started: float,
    build_ms: int,
    jobs: list[dict[str, Any]],
    maxima: dict[str, int],
    sampler: _Sampler,
    process_observer: _ProcessBoundaryObserver,
    limits: RenderJobLimits,
    snapshot: Any | None,
    wire: Mapping[str, Any] | None,
    failure: BaseException | None,
) -> dict[str, Any]:
    succeeded = [job for job in jobs if job["phase"] == "succeeded"]
    observed_children = process_observer.document()
    return {
        "schema": SCHEMA,
        "fixture": FIXTURE,
        "status": ("PASS" if failure is None and jobs and len(succeeded) == len(jobs) else "FAIL"),
        "failure": (
            None
            if failure is None
            else {"reason": _failure_reason(failure), "type": type(failure).__name__}
        ),
        "interval_ms": int((time.monotonic() - started) * 1000),
        "runtime_build_ms": build_ms,
        "composition": {
            "duration_frames": COMPANION_FRAMES,
            "frame_rate": "24/1",
            "width": 320,
            "height": 180,
            "public_fingerprint": (None if snapshot is None else snapshot.public_fingerprint),
            "clips": 0 if wire is None else len(wire.get("clips", [])),
        },
        "jobs": {
            "created": len(jobs),
            "succeeded": len(succeeded),
            "running_max": maxima["running"],
            "queued_max": maxima["queued"],
            "detail": jobs,
        },
        "backend_rss": {
            "sampling_valid": not sampler.failure_reasons and sampler.samples > 0,
            "sampling_failure_reasons": list(sampler.failure_reasons),
            "baseline_bytes": sampler.baseline,
            "own_peak_bytes": sampler.own_peak,
            "increment_bytes": max(0, sampler.own_peak - sampler.baseline),
            "tree_peak_bytes": sampler.tree_peak,
            "tree_increment_bytes": max(0, sampler.tree_peak - sampler.baseline),
            "sample_count": sampler.samples,
            "interval_ms": int(SAMPLE_INTERVAL_S * 1000),
            "method": (
                "working set of this repository-local backend fixture process, sampled on a "
                "fixed 100 ms interval; the increment is the peak over the baseline taken once "
                "the render runtime and output registry were initialized and idle"
            ),
        },
        "child_process_tree": {
            **observed_children,
            "inferred_tree_peak_working_set_bytes": sampler.child_peak,
            "inferred_tree_peak_working_set_holder": sampler.child_peak_holder,
            "unreadable_child_reads": sampler.unreadable_children,
            "enforced_child_memory_bytes": limits.child_memory_bytes,
            "enforced_child_processes": limits.child_processes,
            "enforced_child_threads": limits.child_threads,
            "enforced_child_cpu_seconds": limits.child_cpu_seconds,
            "method": (
                "peak working set sampled from the exact process handle the product created "
                "inside its configured Windows job; process id, opaque run identity, executable "
                "fingerprint and job-membership check are retained per completed process. The "
                "100 ms parent-tree sampler remains a separate diagnostic and cannot substitute "
                "for this handle-bound admission. The enforced values are the product's own "
                "RenderJobLimits, which the same Windows job applies to each child. "
                "KNOWN WEAKNESS: the enforced limit bounds COMMITTED bytes (ProcessMemoryLimit / "
                "JobMemoryLimit) while this reports a WORKING SET, which are different quantities; "
                "measured on this fixture they track within 0.7%, and the comparison is left as it "
                "is rather than changed on evidence that does not call for it"
            ),
        },
    }


def run(
    ffmpeg: Path,
    ffprobe: Path,
    scratch_root: Path,
    timeout_seconds: float,
    job_count: int = COMPANION_JOBS,
) -> dict[str, Any]:
    # The pair is verified against the packaged renderer qualification by the same resolver the
    # M25-20 render stage uses; this script holds no tool path of its own and discovers none.
    qualification = json.loads(extractor.QUALIFICATION_PATH.read_text(encoding="utf-8"))
    build_started = time.monotonic()
    runtime = _build_runtime(
        extractor.resolve_binary(ffmpeg, "renderer"),
        extractor.resolve_binary(ffprobe, "probe"),
        str(qualification["renderer"]),
        str(qualification["probe"]),
        scratch_root,
    )
    build_ms = int((time.monotonic() - build_started) * 1000)
    reader = _memory_reader()
    sampler = _Sampler(reader)
    process_observer = _ProcessBoundaryObserver()
    limits = RenderJobLimits()
    started = time.monotonic()
    jobs: list[dict[str, Any]] = []
    maxima = {"running": 0, "queued": 0}
    wire: dict[str, Any] | None = None
    snapshot: Any | None = None
    failure: BaseException | None = None
    try:
        wire = companion_wire(runtime.base_wire)
        snapshot = decode_public_snapshot(wire)
        receipt = _mint_row_receipt(runtime)
        try:
            # GUARD: the companion stresses the accepted corpus base only, so it claims that
            # base's own sources. The registry is per base by construction -- `MAX_REFERENCE_VIDEOS`
            # is 3 and the accepted base already uses all three -- so claiming across bases here
            # would be refused by the product rather than merely wrong.
            claims = tuple(
                claim_render_source(receipt, asset_id)
                for asset_id in (IMAGE_ASSET, *BASE_VIDEO_ASSETS[CORPUS_BASE])
            )
            history = PreparedAuthoringHistory(snapshot, receipt.generation, claims, runtime.fonts)
            workspace = _CorpusOutputWorkspace(
                history, snapshot, time.monotonic() + timeout_seconds
            )
            job_root = runtime.render_dir / ("companion-" + secrets.token_hex(8))
            job_root.mkdir(parents=True, exist_ok=False)
            store = RenderOutputStore(job_root)
            service = AuthoringRenderService(
                store,
                backend=NativeAuthoringRenderer(
                    renderer_path=runtime.ffmpeg_path,
                    probe_path=runtime.ffprobe_path,
                    process_observer=process_observer.observe,
                ),
            )
            registry = AuthoringOutputRegistry(workspace=workspace, service=service, store=store)
            pending: list[str] = []

            def observe() -> None:
                """Sample the whole batch's phases, so the lane is observed while it runs."""

                phases = [
                    registry.status(handle, snapshot.workspace_handle)["phase"]
                    for handle in pending
                ]
                maxima["running"] = max(maxima["running"], phases.count("rendering"))
                maxima["queued"] = max(maxima["queued"], phases.count("queued"))

            # The idle baseline: the runtime is built, the sources exist, the registry is open,
            # and nothing has been rendered. Every increment below is measured from here.
            sampler.start(reader.own())
            try:
                deadline = time.monotonic() + timeout_seconds
                for batch_start in range(0, job_count, BATCH):
                    pending.clear()
                    for offset in range(min(BATCH, job_count - batch_start)):
                        status = registry.create(
                            AuthoringOutputCreate(
                                schema=OUTPUT_CREATE_SCHEMA,
                                workspace_handle=snapshot.workspace_handle,
                                workspace_revision=snapshot.workspace_revision,
                                timeline_revision=snapshot.timeline_revision,
                                snapshot_fingerprint=snapshot.public_fingerprint,
                                output_profile_id=OUTPUT_PROFILE_ID,
                                idempotency_key=(
                                    f"nle-stress-render-{batch_start + offset:02d}-"
                                    + secrets.token_hex(4)
                                ),
                            )
                        )
                        pending.append(str(status["job_handle"]))
                        observe()
                    for handle in pending:
                        job_started = time.monotonic()
                        final = _wait(
                            registry, handle, snapshot.workspace_handle, deadline, observe
                        )
                        summary = final.get("output")
                        jobs.append(
                            {
                                "elapsed_ms": int((time.monotonic() - job_started) * 1000),
                                "phase": final["phase"],
                                "failure": final.get("failure"),
                                "availability": final.get("availability"),
                                "verified": bool(
                                    isinstance(summary, Mapping) and summary.get("verified") is True
                                ),
                                "frame_count": (
                                    int(summary["frame_count"])
                                    if isinstance(summary, Mapping)
                                    and summary.get("frame_count") is not None
                                    else None
                                ),
                                "byte_length": (
                                    int(summary["byte_length"])
                                    if isinstance(summary, Mapping)
                                    and summary.get("byte_length") is not None
                                    else None
                                ),
                            }
                        )
                        if final["phase"] == "succeeded" and final.get("output_handle"):
                            lease = registry.open_download(
                                str(final["output_handle"]), snapshot.workspace_handle
                            )
                            with lease:
                                jobs[-1]["downloaded_bytes"] = sum(
                                    len(bytes(chunk)) for chunk in lease.chunks()
                                )
            finally:
                registry.close()
        finally:
            receipt.release()
    except (CompanionError, RenderStageError) as error:
        failure = error
    finally:
        # IMPORTANT (B-M2545-50): retain the samples and exact child-process observations before
        # propagating a sampler failure. Raising directly from `stop` discards the only document
        # that can distinguish a failed read from an observed zero.
        try:
            try:
                sampler.stop()
            except CompanionError as error:
                if failure is None:
                    failure = error
        finally:
            runtime.close()

    document = _companion_document(
        started=started,
        build_ms=build_ms,
        jobs=jobs,
        maxima=maxima,
        sampler=sampler,
        process_observer=process_observer,
        limits=limits,
        snapshot=snapshot,
        wire=wire,
        failure=failure,
    )
    if failure is not None:
        raise CompanionRunFailure(_failure_reason(failure), document) from failure
    return document


def _blocked_document(error: BaseException) -> dict[str, Any]:
    """Privacy-safe failure envelope for a run blocked before observers were constructed."""

    reason = _failure_reason(error)
    return {
        "schema": SCHEMA,
        "fixture": FIXTURE,
        "status": "FAIL",
        "failure": {"reason": reason, "type": type(error).__name__},
        "interval_ms": 0,
        "runtime_build_ms": 0,
        "composition": {
            "duration_frames": COMPANION_FRAMES,
            "frame_rate": "24/1",
            "width": 320,
            "height": 180,
            "public_fingerprint": None,
            "clips": 0,
        },
        "jobs": {
            "created": 0,
            "succeeded": 0,
            "running_max": 0,
            "queued_max": 0,
            "detail": [],
        },
        "backend_rss": {
            "sampling_valid": False,
            "sampling_failure_reasons": [reason],
            "baseline_bytes": 0,
            "own_peak_bytes": 0,
            "increment_bytes": 0,
            "tree_peak_bytes": 0,
            "tree_increment_bytes": 0,
            "sample_count": 0,
            "interval_ms": int(SAMPLE_INTERVAL_S * 1000),
        },
        "child_process_tree": {
            "sampling_valid": False,
            "sampling_failure_reasons": [reason],
            "completed_processes": 0,
            "measured_processes": 0,
            "unobserved_processes": 0,
            "working_set_sample_count": 0,
            "peak_working_set_bytes": 0,
            "peak_working_set_holder": "",
            "peak_committed_bytes": 0,
            "job_membership_verified": False,
            "limits_verified": False,
            "processes": [],
        },
    }


def _write_document(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path, default=None)
    parser.add_argument("--ffprobe", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, default=None)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    # Calibration only. The frozen companion is eight jobs; the document records how many were
    # actually created and the journey that owns the measurement asserts that number, so a
    # shortened run can never be mistaken for the fixture.
    parser.add_argument("--jobs", type=int, default=COMPANION_JOBS)
    args = parser.parse_args(argv)

    ffmpeg = args.ffmpeg or Path(os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH", ""))
    ffprobe = args.ffprobe or Path(os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH", ""))
    if not ffmpeg.is_file() or not ffprobe.is_file():
        _write_document(args.out, _blocked_document(CompanionError("renderer_pair_missing")))
        print("renderer pair not supplied", file=sys.stderr)
        return 2

    # GUARD (inherited from the render stage's B-68): keep this name short. The render store opens
    # its files without the extended-length prefix, so a staging path past 260 characters fails as
    # `store_write_failed` on a host with long paths disabled, and the product part of the path
    # below this root is already ~150 characters.
    scratch_root = args.scratch_root or (ROOT / ".tmp" / f"nsrc-{secrets.token_hex(4)}")
    scratch_root.mkdir(parents=True, exist_ok=True)
    try:
        document = run(ffmpeg, ffprobe, scratch_root, args.timeout_seconds, args.jobs)
    except CompanionRunFailure as error:
        _write_document(args.out, error.document)
        print(f"render companion blocked: {error}", file=sys.stderr)
        return 2
    except (CompanionError, RenderStageError) as error:
        _write_document(args.out, _blocked_document(error))
        print(f"render companion blocked: {error}", file=sys.stderr)
        return 2
    finally:
        _cleanup_scratch_root(scratch_root)
    _write_document(args.out, document)
    rss = document["backend_rss"]
    print(
        f"{FIXTURE}: {document['jobs']['succeeded']}/{document['jobs']['created']} jobs, "
        f"backend RSS increment {rss['increment_bytes']} bytes over {rss['sample_count']} "
        f"samples: {document['status']}"
    )
    return 0 if document["status"] == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover - console entry
    raise SystemExit(main())
