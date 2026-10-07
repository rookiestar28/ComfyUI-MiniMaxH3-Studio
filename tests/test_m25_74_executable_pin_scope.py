"""One media operation verifies each executable once.

Hashing a 200 MB tool before every spawn cost several whole-file reads per derivative. A pin scope
keeps the first verified descriptor for the length of one operation. These tests fix what that
buys, what it must still refuse, and that nothing is held once the operation ends.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from test_m25_45_lease_cross_boundary import lease_request

import comfyui_h3_context.adapters.comfyui_authoring_media_leases as route_module
import comfyui_h3_context.adapters.comfyui_media_runtime as runtime_module
import comfyui_h3_context.adapters.executable_admission as admission
from comfyui_h3_context.adapters.executable_admission import (
    ExecutableAdmissionError,
    executable_pin_scope,
    pin_exact_executable,
)
from comfyui_h3_context.adapters.media_runtime_manager import MediaRuntimeBusy

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the exact pin is the Windows admission")


def _tool(directory: Path, name: str = "tool.exe") -> tuple[Path, str]:
    directory.mkdir(exist_ok=True)
    path = directory / name
    body = name.encode() + b" qualified bytes"
    path.write_bytes(body)
    return path.resolve(), hashlib.sha256(body).hexdigest()


@pytest.fixture
def opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every open of an executable is one whole-file hash of it."""

    seen: list[str] = []
    original = admission.windows_open_read_pin

    def counted(path: Path) -> int:
        seen.append(Path(path).name)
        return original(path)

    monkeypatch.setattr(admission, "windows_open_read_pin", counted)
    return seen


def _writable(path: Path) -> bool:
    try:
        with open(path, "r+b"):
            return True
    except PermissionError:
        return False


Pins = dict[tuple[str, str], tuple[int, tuple[int, int, int, int]]]


def _held() -> Pins:
    pins = getattr(admission._SCOPE, "pins", None)
    assert pins is not None
    return cast(Pins, pins)


@contextmanager
def _refused() -> Iterator[None]:
    """A refused pin, by its one closed code exactly: a longer code is another code."""

    with pytest.raises(ExecutableAdmissionError) as raised:
        yield
    assert raised.value.code == "adapter_capability_changed"


def test_without_a_scope_every_pin_hashes(tmp_path: Path, opens: list[str]) -> None:
    path, digest = _tool(tmp_path / "tools")
    for _ in range(3):
        with pin_exact_executable(path, digest):
            assert not _writable(path)
        assert _writable(path)
    assert opens == ["tool.exe"] * 3


def test_a_scope_hashes_each_file_once(tmp_path: Path, opens: list[str]) -> None:
    first, first_digest = _tool(tmp_path / "tools", "ffmpeg.exe")
    second, second_digest = _tool(tmp_path / "tools", "ffprobe.exe")
    with executable_pin_scope():
        identities = set()
        for _ in range(3):
            with pin_exact_executable(first, first_digest) as identity:
                identities.add(identity)
        for _ in range(2):
            with pin_exact_executable(second, second_digest):
                pass
        assert len(identities) == 1
    assert opens == ["ffmpeg.exe", "ffprobe.exe"]


def test_a_held_pin_is_not_charged_again(tmp_path: Path) -> None:
    path, digest = _tool(tmp_path / "tools")
    charged: list[int] = []
    with executable_pin_scope():
        for _ in range(2):
            with pin_exact_executable(path, digest, charge=charged.append):
                pass
    assert sum(charged) == path.stat().st_size


def test_a_scope_holds_the_file_against_writers_and_releases_it(tmp_path: Path) -> None:
    path, digest = _tool(tmp_path / "tools")
    with executable_pin_scope():
        with pin_exact_executable(path, digest):
            pass
        # Between two pins of one operation nobody can change the verified bytes.
        ((descriptor, _identity),) = _held().values()
        assert not _writable(path)
        with pytest.raises(PermissionError):
            path.unlink()
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert _writable(path)
    path.unlink()


def test_a_wrong_digest_is_refused_inside_a_scope_after_a_correct_pin(
    tmp_path: Path, opens: list[str]
) -> None:
    path, digest = _tool(tmp_path / "tools")
    with executable_pin_scope():
        with pin_exact_executable(path, digest):
            pass
        with _refused():
            with pin_exact_executable(path, "0" * 64):
                pass
        # The refusal kept nothing, and the verified pin is still the one in use.
        assert len(_held()) == 1
        with pin_exact_executable(path, digest):
            pass
    assert opens == ["tool.exe", "tool.exe"]
    assert _writable(path)


def test_a_failed_first_pin_keeps_nothing(tmp_path: Path) -> None:
    path, _digest = _tool(tmp_path / "tools")
    with executable_pin_scope():
        with _refused():
            with pin_exact_executable(path, "0" * 64):
                pass
        assert _held() == {}
        assert _writable(path)


def test_a_held_descriptor_that_no_longer_matches_its_path_fails_closed(tmp_path: Path) -> None:
    path, digest = _tool(tmp_path / "tools")
    with executable_pin_scope():
        with pin_exact_executable(path, digest):
            pass
        ((key, (descriptor, identity)),) = _held().items()
        # The path now names a file other than the one the held descriptor verified.
        _held()[key] = (descriptor, (identity[0], identity[1] + 1, identity[2], identity[3]))
        entered: list[int] = []
        with _refused():
            with pin_exact_executable(path, digest):
                entered.append(1)
        # It stays refused for the rest of the operation; it is not silently hashed again.
        with _refused():
            with pin_exact_executable(path, digest):
                entered.append(2)
        # Refused before the caller's block runs: nothing is spawned from an unverified path and
        # then reported as a failure afterwards.
        assert entered == []
    assert _writable(path)


def test_a_held_slot_that_answers_for_another_file_is_refused_before_the_block(
    tmp_path: Path,
) -> None:
    path, digest = _tool(tmp_path / "tools")
    other, _ = _tool(tmp_path / "elsewhere", "other.exe")
    with executable_pin_scope():
        with pin_exact_executable(path, digest):
            pass
        ((key, (descriptor, identity)),) = _held().items()
        # The path still names the verified file; the held descriptor names another one. Only the
        # descriptor's own look at its file can tell, so the path's identity does not answer for it.
        stranger = admission.windows_open_read_pin(other)
        try:
            _held()[key] = (stranger, identity)
            entered: list[int] = []
            with _refused():
                with pin_exact_executable(path, digest):
                    entered.append(1)
            assert entered == []
        finally:
            _held()[key] = (descriptor, identity)
            os.close(stranger)
    assert _writable(path) and _writable(other)


def test_two_files_with_the_same_bytes_are_two_pins(tmp_path: Path, opens: list[str]) -> None:
    current, digest = _tool(tmp_path / "current")
    replacement, same = _tool(tmp_path / "replacement")
    assert digest == same and current != replacement
    with executable_pin_scope():
        # A held pin answers for its own file only: the digest alone does not name a file.
        for path in (current, replacement, current, replacement):
            with pin_exact_executable(path, digest):
                pass
        assert len(_held()) == 2
    assert opens == ["tool.exe", "tool.exe"]


def test_a_scope_holds_one_pin_for_a_file_named_in_another_case(
    tmp_path: Path, opens: list[str]
) -> None:
    path, digest = _tool(tmp_path / "tools")
    shouted = Path(str(path).upper())
    if not shouted.exists() or not os.path.samefile(shouted, path):
        pytest.skip("this directory tells names apart by their case")
    with executable_pin_scope():
        with pin_exact_executable(path, digest) as first:
            pass
        # The same file under another spelling the file system takes for it, and its digest in
        # the other case a tool may print it in: still one file, verified once.
        with pin_exact_executable(shouted, digest) as second:
            pass
        with pin_exact_executable(path, digest.upper()) as third:
            pass
        assert first == second == third
        assert len(_held()) == 1
    assert opens == ["tool.exe"]


def test_a_scope_closes_every_descriptor_although_one_close_fails(tmp_path: Path) -> None:
    first, first_digest = _tool(tmp_path / "tools", "ffmpeg.exe")
    second, second_digest = _tool(tmp_path / "tools", "ffprobe.exe")
    with executable_pin_scope():
        with pin_exact_executable(first, first_digest):
            pass
        with pin_exact_executable(second, second_digest):
            pass
        (closed, _), (other, _) = _held().values()
        # Closed behind the scope's back: the scope's own close of it fails.
        os.close(closed)
    # One failed close is not a reason to keep the next file held. A descriptor left open blocks
    # the replacement of the runtime's own files for as long as the process runs.
    with pytest.raises(OSError):
        os.fstat(other)
    assert _writable(first) and _writable(second)


def _ran() -> None:
    raise AssertionError("the caller's block ran for a file that was not admitted")


def test_a_file_refused_before_it_is_opened_is_refused_by_its_code(tmp_path: Path) -> None:
    path, digest = _tool(tmp_path / "tools")
    os.link(path, path.with_name("other.exe"))
    # Nothing was opened, so there is nothing to close: the refusal reaches the caller as the
    # closed code and not as an error of the cleanup.
    with _refused():
        with pin_exact_executable(path, digest):
            _ran()
    assert _writable(path)


def test_a_descriptor_that_is_not_the_named_file_is_refused_before_it_is_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, digest = _tool(tmp_path / "tools")
    twin, same = _tool(tmp_path / "elsewhere")
    assert same == digest
    pin = admission.windows_open_read_pin
    monkeypatch.setattr(admission, "windows_open_read_pin", lambda _path: pin(twin))
    charged: list[int] = []
    # The open reached another file than the one that was looked at, with the same bytes. It is
    # refused as soon as it is seen, before the caller's budget is charged for hashing it.
    with _refused():
        with pin_exact_executable(path, digest, charge=charged.append):
            _ran()
    assert charged == []
    assert _writable(twin)


def test_a_file_that_got_a_second_name_before_it_was_opened_is_refused_before_it_is_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, digest = _tool(tmp_path / "tools")
    pin = admission.windows_open_read_pin

    def named_again_then_opened(target: Path) -> int:
        os.link(target, target.with_name("other.exe"))
        return pin(target)

    monkeypatch.setattr(admission, "windows_open_read_pin", named_again_then_opened)
    charged: list[int] = []
    with _refused():
        with pin_exact_executable(path, digest, charge=charged.append):
            _ran()
    assert charged == []
    assert _writable(path)


def test_a_file_of_exactly_the_largest_admitted_size_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, digest = _tool(tmp_path / "tools")
    monkeypatch.setattr(admission, "MAX_EXECUTABLE_BYTES", path.stat().st_size)
    charged: list[int] = []
    with pin_exact_executable(path, digest, charge=charged.append):
        pass
    assert sum(charged) == path.stat().st_size


def test_a_descriptor_that_yields_more_than_the_largest_admitted_size_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, digest = _tool(tmp_path / "tools")
    size = path.stat().st_size
    monkeypatch.setattr(admission, "MAX_EXECUTABLE_BYTES", size)
    pin = admission.windows_open_read_pin
    read = os.read
    pinned: list[int] = []
    more = [b"x"]

    def remembered(target: Path) -> int:
        pinned.append(pin(target))
        return pinned[-1]

    def longer(descriptor: int, count: int) -> bytes:
        chunk = read(descriptor, count)
        if chunk or descriptor not in pinned or not more:
            return chunk
        return more.pop()

    monkeypatch.setattr(admission, "windows_open_read_pin", remembered)
    charged: list[int] = []
    with monkeypatch.context() as patched:
        patched.setattr(os, "read", longer)
        # The pin denies writers, so a real descriptor yields what was looked at. The bound in
        # the read loop is for one that does not: it refuses at the first byte past the largest
        # admitted size, before that byte is charged or hashed.
        with _refused():
            with pin_exact_executable(path, digest, charge=charged.append):
                _ran()
    assert charged == [size]
    assert more == []
    assert _writable(path)


def test_a_second_name_given_to_the_file_while_it_is_hashed_fails_the_pin(tmp_path: Path) -> None:
    path, digest = _tool(tmp_path / "tools")
    other = path.with_name("other.exe")

    def name_again(_size: int) -> None:
        if not other.exists():
            os.link(path, other)

    # The pin denies writers, not a second name for the same bytes. A file that has two names
    # when its hash is complete is not the single file that was looked at.
    with _refused():
        with pin_exact_executable(path, digest, charge=name_again):
            _ran()
    assert other.exists()
    assert _writable(path)


def test_a_file_touched_while_it_is_hashed_fails_the_pin(tmp_path: Path) -> None:
    path, digest = _tool(tmp_path / "tools")
    touched: list[int] = []

    def touch(_size: int) -> None:
        if not touched:
            touched.append(1)
            _touch(path)

    with _refused():
        with pin_exact_executable(path, digest, charge=touch):
            _ran()
    assert touched == [1]
    assert _writable(path)


@pytest.mark.parametrize("pinned", ["without-a-scope", "held-by-its-scope"])
def test_a_second_name_given_to_the_file_while_the_callers_block_runs_fails_the_pin(
    tmp_path: Path, pinned: str
) -> None:
    path, digest = _tool(tmp_path / "tools")
    other = path.with_name("other.exe")

    def named_again_during_the_block() -> None:
        with _refused():
            with pin_exact_executable(path, digest):
                os.link(path, other)

    if pinned == "without-a-scope":
        named_again_during_the_block()
    else:
        with executable_pin_scope():
            with pin_exact_executable(path, digest):
                pass
            named_again_during_the_block()
    assert other.exists()
    assert _writable(path)


def _touch(path: Path) -> None:
    before = path.stat()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))


@pytest.mark.parametrize("pinned", ["without-a-scope", "first-in-its-scope", "held-by-its-scope"])
def test_a_file_touched_while_the_callers_block_runs_fails_the_pin(
    tmp_path: Path, pinned: str
) -> None:
    path, digest = _tool(tmp_path / "tools")

    def touched_during_the_block() -> None:
        # The pin denies writers, not a change of the file's times. The tool then ran from a file
        # that no longer reads as the one that was verified: the pin fails although the block
        # ended normally.
        with _refused():
            with pin_exact_executable(path, digest):
                _touch(path)

    if pinned == "without-a-scope":
        touched_during_the_block()
    else:
        with executable_pin_scope():
            if pinned == "held-by-its-scope":
                with pin_exact_executable(path, digest):
                    pass
            touched_during_the_block()
            # For the rest of the operation the file is refused before a block runs.
            entered: list[int] = []
            with _refused():
                with pin_exact_executable(path, digest):
                    entered.append(1)
            assert entered == []
    assert _writable(path)


@pytest.mark.parametrize("pinned", ["first-in-its-scope", "held-by-its-scope"])
def test_a_descriptor_closed_while_the_callers_block_runs_fails_the_pin(
    tmp_path: Path, pinned: str
) -> None:
    path, digest = _tool(tmp_path / "tools")
    with executable_pin_scope():
        if pinned == "held-by-its-scope":
            with pin_exact_executable(path, digest):
                pass
        # The descriptor is what kept the verified bytes from changing. Once it is gone nothing
        # proves which bytes the tool ran from, although the path still reads as it did.
        with _refused():
            with pin_exact_executable(path, digest):
                ((descriptor, _identity),) = _held().values()
                os.close(descriptor)
    assert _writable(path)


def test_scopes_nest_and_only_the_outermost_releases(tmp_path: Path, opens: list[str]) -> None:
    path, digest = _tool(tmp_path / "tools")
    with executable_pin_scope():
        with pin_exact_executable(path, digest):
            pass
        with executable_pin_scope():
            with pin_exact_executable(path, digest):
                pass
        assert len(_held()) == 1 and not _writable(path)
        with pin_exact_executable(path, digest):
            pass
    assert opens == ["tool.exe"]
    assert getattr(admission._SCOPE, "pins", None) is None
    assert _writable(path)


def test_an_error_inside_a_scope_still_releases(tmp_path: Path) -> None:
    path, digest = _tool(tmp_path / "tools")
    with pytest.raises(KeyError):
        with executable_pin_scope():
            with pin_exact_executable(path, digest):
                pass
            raise KeyError("operation failed")
    assert getattr(admission._SCOPE, "pins", None) is None
    assert _writable(path)


def test_a_scope_belongs_to_the_thread_that_entered_it(tmp_path: Path, opens: list[str]) -> None:
    path, digest = _tool(tmp_path / "tools")
    seen: list[bool] = []

    def other() -> None:
        seen.append(getattr(admission._SCOPE, "pins", None) is None)
        with pin_exact_executable(path, digest):
            pass

    with executable_pin_scope():
        with pin_exact_executable(path, digest):
            pass
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive()
        assert len(_held()) == 1
    assert seen == [True]
    assert opens == ["tool.exe", "tool.exe"]
    assert _writable(path)


class _Manager:
    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
        self.leases = 0
        self.scope_closed_before_release: list[bool] = []

    @contextmanager
    def lease(self) -> Iterator[None]:
        if self.busy:
            raise MediaRuntimeBusy()
        self.leases += 1
        try:
            yield
        finally:
            # The scope has already closed its descriptors when the lease is given back.
            self.scope_closed_before_release.append(getattr(admission._SCOPE, "pins", None) is None)
            self.leases -= 1


def test_the_media_runtime_lease_is_one_pin_scope(
    tmp_path: Path, opens: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, digest = _tool(tmp_path / "tools")
    manager = _Manager()
    monkeypatch.setattr(runtime_module, "_manager", lambda: manager)
    with runtime_module.media_runtime_lease():
        assert manager.leases == 1
        for _ in range(4):
            with pin_exact_executable(path, digest):
                pass
    assert opens == ["tool.exe"]
    assert manager.scope_closed_before_release == [True]
    assert getattr(admission._SCOPE, "pins", None) is None
    assert _writable(path)


def test_a_refused_media_runtime_lease_opens_no_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module, "_manager", lambda: _Manager(busy=True))
    with pytest.raises(MediaRuntimeBusy):
        with runtime_module.media_runtime_lease():
            raise AssertionError("a busy runtime grants no lease")
    assert getattr(admission._SCOPE, "pins", None) is None


class _RecordingAuthority:
    """Stands in for the lease authority: records what holds while a create runs."""

    def __init__(self, manager: _Manager) -> None:
        self._manager = manager
        self.during_create: list[tuple[int, bool]] = []

    def create(self, _request: object, *, cancellation: object) -> tuple[dict[str, object], str]:
        self.during_create.append(
            (self._manager.leases, getattr(admission._SCOPE, "pins", None) is not None)
        )
        return {"leaseId": "lease-" + "0" * 32}, "0" * 64

    def close(self) -> None:
        pass


def test_the_lease_routes_create_runs_inside_one_runtime_lease_and_its_pin_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _Manager()
    monkeypatch.setattr(runtime_module, "_manager", lambda: manager)
    authority = _RecordingAuthority(manager)
    service = route_module.MediaLeaseRouteService(cast(Any, authority))
    try:
        result = service._work(lease_request("video_proxy"), "", route_module._Cancellation())
    finally:
        service.close()
    assert result.capability == "0" * 64
    # The create that generates a derivative is the media operation: one lease, one scope.
    assert authority.during_create == [(1, True)]
    assert manager.leases == 0 and manager.scope_closed_before_release == [True]
    assert getattr(admission._SCOPE, "pins", None) is None
