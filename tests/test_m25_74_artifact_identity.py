"""The cheap and the proving tier of a generated source's stored artifact.

A full inspection reads and hashes the whole artifact. Currentness is asked many times per lease,
so it is answered from the identity the last full inspection recorded, and the proof is paid where
authority is granted. These tests fix what the identity sees, what only the proof sees, and which
question of a generated source uses which.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_segment_artifact_store import _fingerprint, _partial, _policy

import comfyui_h3_context.adapters.segment_artifact_store as store_module
from comfyui_h3_context.adapters.authoring_generated_source import GeneratedAuthoringVideoSource
from comfyui_h3_context.adapters.authoring_render_source import _read_generated_source_fingerprint
from comfyui_h3_context.adapters.authoring_source_binding import AuthoringSourceBindingError
from comfyui_h3_context.adapters.segment_artifact_store import (
    ArtifactIdentity,
    ArtifactInspectionStatus,
    ArtifactStoreError,
    PrivateSegmentArtifactStore,
)
from comfyui_h3_context.core.segment_artifacts import SegmentArtifactReceipt

BODY = b"generated-segment"
windows_only = pytest.mark.skipif(os.name != "nt", reason="the pinned media read is Windows-only")

Stored = tuple[PrivateSegmentArtifactStore, SegmentArtifactReceipt, Path, list[int]]


@pytest.fixture
def stored(tmp_path: Path) -> Stored:
    clock = [100]
    root = tmp_path / "private-store"
    store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: clock[0])
    receipt = store.commit(store.begin(_partial()), BODY)
    # IMPORTANT: admit a whole-second mtime so DrvFS replacement tests preserve stat identity
    # and reach the real hash proof; fractional utime restoration changes the cheap identity.
    os.utime(_artifact(root), ns=(1_700_000_000_000_000_000,) * 2)
    return store, receipt, root, clock


def _artifact(root: Path) -> Path:
    return root / "artifacts" / "artifact.1.bin"


def _receipt(root: Path) -> Path:
    return root / "receipts" / "artifact.1.json"


def _rewrite_keeping_identity(path: Path, payload: bytes) -> None:
    before = path.stat()
    assert len(payload) == before.st_size
    path.write_bytes(payload)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ), "same-identity replacement must preserve the admitted metadata"


@pytest.fixture
def content_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    original = store_module._read_regular_bytes

    def counted(path: Path, *, maximum_bytes: int) -> bytes:
        reads.append(path.parent.name)
        return original(path, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(store_module, "_read_regular_bytes", counted)
    return reads


def test_identity_comes_from_a_full_inspection_and_answers_without_reading(
    stored: Stored, content_reads: list[str]
) -> None:
    store, receipt, root, _ = stored
    inspection, identity = store.inspect_with_identity(receipt)
    assert inspection.status is ArtifactInspectionStatus.REUSABLE
    assert type(identity) is ArtifactIdentity
    assert content_reads == ["receipts", "artifacts"]
    # The identity discloses neither the store's location nor the artifact's.
    assert repr(identity) == "<ArtifactIdentity opaque>"
    members = [getattr(identity, name) for name in ArtifactIdentity.__slots__]
    assert all(
        root.name not in str(member) and "artifact.1" not in str(member) for member in members
    )

    del content_reads[:]
    assert store.identity_current(receipt, identity)
    assert content_reads == []
    # It is a value. Nothing can make it answer for more than the inspection it came from saw.
    with pytest.raises(AttributeError):
        identity._expires_at_ms = receipt.expires_at_ms + 1  # type: ignore[misc]


class _OwnedLock:
    """Stands in for a lock and knows whether the calling thread holds it."""

    def __init__(self, lock: Any) -> None:
        self._lock = lock
        self._holder: int | None = None
        self._depth = 0

    def __enter__(self) -> None:
        self._lock.acquire()
        self._holder = threading.get_ident()
        self._depth += 1

    def __exit__(self, *_exc: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._holder = None
        self._lock.release()

    def held(self) -> bool:
        return self._holder == threading.get_ident()


def test_the_store_takes_an_identity_and_answers_from_one_under_its_lock(
    stored: Stored, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, receipt, _, _ = stored
    lock = _OwnedLock(store._lock)
    store._lock = lock  # type: ignore[assignment]
    held: list[bool] = []
    original = PrivateSegmentArtifactStore._plain_file_identity

    def look(path: Path) -> tuple[int, int, int, int] | None:
        held.append(lock.held())
        return original(path)

    monkeypatch.setattr(PrivateSegmentArtifactStore, "_plain_file_identity", staticmethod(look))
    # The looks before and after the verified read and the read itself are one locked section:
    # nothing the store does to its own files falls between them.
    _, identity = store.inspect_with_identity(receipt)
    assert identity is not None
    assert held == [True] * 4
    del held[:]
    assert store.identity_current(receipt, identity)
    assert held == [True] * 2


def test_an_inspection_that_is_not_reusable_yields_no_identity(stored: Stored) -> None:
    store, receipt, root, _ = stored
    _artifact(root).write_bytes(b"tampered-segment!")
    inspection, identity = store.inspect_with_identity(receipt)
    assert inspection.status is ArtifactInspectionStatus.TAMPERED
    assert identity is None


def test_an_inspection_with_identity_refuses_what_a_full_inspection_refuses(
    stored: Stored,
) -> None:
    store, receipt, _, _ = stored
    # It is the full inspection with one more return value, its closed refusals included.
    with pytest.raises(ArtifactStoreError) as refused:
        store.inspect_with_identity(cast(Any, object()))
    assert refused.value.code == "receipt_type"
    with pytest.raises(ArtifactStoreError) as refused:
        store.inspect(cast(Any, object()))
    assert refused.value.code == "receipt_type"
    with pytest.raises(ArtifactStoreError, match="read_limit"):
        store.inspect_with_identity(receipt, maximum_bytes=len(BODY) - 1)
    inspection, identity = store.inspect_with_identity(receipt, maximum_bytes=len(BODY))
    assert inspection.status is ArtifactInspectionStatus.REUSABLE
    assert identity is not None


def _remove_artifact(store: Any, root: Path, clock: list[int], receipt: Any) -> None:
    _artifact(root).unlink()


def _remove_receipt(store: Any, root: Path, clock: list[int], receipt: Any) -> None:
    _receipt(root).unlink()


def _expire(store: Any, root: Path, clock: list[int], receipt: Any) -> None:
    clock[0] = receipt.expires_at_ms


def _disable(store: Any, root: Path, clock: list[int], receipt: Any) -> None:
    store.disable(purge=False)


def _grow_artifact(store: Any, root: Path, clock: list[int], receipt: Any) -> None:
    _artifact(root).write_bytes(BODY + b"!")


def _touch_artifact(store: Any, root: Path, clock: list[int], receipt: Any) -> None:
    before = _artifact(root).stat()
    os.utime(_artifact(root), ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))


def _touch_receipt(store: Any, root: Path, clock: list[int], receipt: Any) -> None:
    before = _receipt(root).stat()
    os.utime(_receipt(root), ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))


def _link_artifact(store: Any, root: Path, clock: list[int], receipt: Any) -> None:
    try:
        os.link(_artifact(root), root.parent / "second-name.bin")
    except OSError:
        pytest.skip("hard links are unavailable on this filesystem")


@pytest.mark.parametrize(
    ("change", "status"),
    [
        (_remove_artifact, ArtifactInspectionStatus.MISSING),
        (_remove_receipt, ArtifactInspectionStatus.MISSING),
        (_expire, ArtifactInspectionStatus.EXPIRED),
        (_disable, ArtifactInspectionStatus.DISABLED),
        (_grow_artifact, ArtifactInspectionStatus.TAMPERED),
        (_link_artifact, ArtifactInspectionStatus.UNSAFE),
        # A changed timestamp over unchanged bytes is not a revocation: the identity stops
        # answering and the full inspection that follows finds the artifact reusable. Either of
        # the two verified files does this, the receipt as much as the artifact.
        (_touch_artifact, ArtifactInspectionStatus.REUSABLE),
        (_touch_receipt, ArtifactInspectionStatus.REUSABLE),
    ],
)
def test_identity_stops_answering_when_the_stored_files_change(
    stored: Stored,
    change: Callable[[Any, Path, list[int], Any], None],
    status: ArtifactInspectionStatus,
) -> None:
    store, receipt, root, clock = stored
    _, identity = store.inspect_with_identity(receipt)
    assert identity is not None and store.identity_current(receipt, identity)
    change(store, root, clock, receipt)
    assert not store.identity_current(receipt, identity)
    assert store.inspect(receipt).status is status


def test_identity_answers_only_for_the_receipt_it_was_recorded_for(stored: Stored) -> None:
    store, receipt, _, _ = stored
    _, identity = store.inspect_with_identity(receipt)
    assert identity is not None
    other = replace(receipt, model_fingerprint=_fingerprint("d"), receipt_fingerprint=None)
    assert not store.identity_current(other, identity)
    assert not store.identity_current(receipt, cast(Any, object()))
    assert not store.identity_current(cast(Any, object()), identity)


def test_a_same_size_replacement_with_a_restored_timestamp_is_seen_only_by_the_proof(
    stored: Stored,
) -> None:
    store, receipt, root, _ = stored
    _, identity = store.inspect_with_identity(receipt)
    assert identity is not None
    _rewrite_keeping_identity(_artifact(root), b"X" * len(BODY))

    # This is exactly what the cheap tier cannot see, and why it never stands in for the proof.
    assert store.identity_current(receipt, identity)
    assert store.inspect(receipt).status is ArtifactInspectionStatus.TAMPERED
    assert store.inspect_with_identity(receipt)[1] is None


def test_no_identity_is_issued_for_a_file_replaced_during_the_verified_read(
    stored: Stored, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, receipt, root, _ = stored
    original = store_module._read_regular_bytes
    replaced: list[Path] = []

    def read_then_replace(path: Path, *, maximum_bytes: int) -> bytes:
        payload = original(path, maximum_bytes=maximum_bytes)
        if path.parent.name == "artifacts" and not replaced:
            # Replaced the moment after it was read and hashed. The inspection's verdict is about
            # the earlier bytes; an identity taken now would describe bytes nothing verified.
            path.write_bytes(BODY + b"!")
            replaced.append(path)
        return payload

    monkeypatch.setattr(store_module, "_read_regular_bytes", read_then_replace)
    inspection, identity = store.inspect_with_identity(receipt)
    assert replaced == [_artifact(root)]
    assert inspection.status is ArtifactInspectionStatus.REUSABLE
    assert identity is None
    assert store.inspect(receipt).status is ArtifactInspectionStatus.TAMPERED


Swap = tuple[Callable[[], None], Callable[[], None]]


def _a_second_name(file: Path, aside: Path) -> Swap:
    """The file has a second name, except while it is read."""

    second = aside / "second-name.bin"
    try:
        os.link(file, second)
    except OSError:
        pytest.skip("hard links are unavailable on this filesystem")
    return second.unlink, lambda: os.link(file, second)


def _a_directory(file: Path, aside: Path) -> Swap:
    """A directory holds the file's name, except while the file is read."""

    file_aside = aside / "file-aside.bin"
    directory_aside = aside / "directory-aside"
    file.rename(file_aside)
    file.mkdir()

    def file_in_place() -> None:
        file.rename(directory_aside)
        file_aside.rename(file)

    def directory_in_place() -> None:
        file.rename(file_aside)
        directory_aside.rename(file)

    return file_in_place, directory_in_place


@pytest.mark.parametrize("not_plain", [_a_second_name, _a_directory])
@pytest.mark.parametrize("which", [_artifact, _receipt])
def test_no_identity_is_issued_for_a_file_that_is_plain_only_during_the_verified_read(
    stored: Stored,
    monkeypatch: pytest.MonkeyPatch,
    which: Callable[[Path], Path],
    not_plain: Callable[[Path, Path], Swap],
) -> None:
    store, receipt, root, _ = stored
    file = which(root)
    make_plain, unmake_plain = not_plain(file, root.parent)
    original = store_module._read_regular_bytes

    def read_while_plain(path: Path, *, maximum_bytes: int) -> bytes:
        if path != file:
            return original(path, maximum_bytes=maximum_bytes)
        # For exactly the verified read the file is one plain file, so the inspection finds the
        # artifact reusable. The looks before and after it agree with each other: both saw the
        # same thing, and it was not a plain file. Either of the two verified files does this,
        # the receipt as much as the artifact.
        make_plain()
        try:
            return original(path, maximum_bytes=maximum_bytes)
        finally:
            unmake_plain()

    monkeypatch.setattr(store_module, "_read_regular_bytes", read_while_plain)
    inspection, identity = store.inspect_with_identity(receipt)
    assert inspection.status is ArtifactInspectionStatus.REUSABLE
    # An identity of what the looks saw would answer for a missing, linked or replaced artifact
    # ever after.
    assert identity is None


def _refuse_the_look(monkeypatch: pytest.MonkeyPatch, refused: Path) -> None:
    original = Path.lstat

    def lstat(self: Path) -> os.stat_result:
        if self == refused:
            raise PermissionError(13, "access denied")
        return original(self)

    monkeypatch.setattr(Path, "lstat", lstat)


def _refuse_the_link_check(monkeypatch: pytest.MonkeyPatch, refused: Path) -> None:
    original = store_module._is_link_or_reparse

    def is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
        # What `Path.is_symlink` raises before Python 3.13 when its own look at the file is refused.
        if path == refused:
            raise PermissionError(13, "access denied")
        return original(path, metadata)

    monkeypatch.setattr(store_module, "_is_link_or_reparse", is_link_or_reparse)


@pytest.mark.parametrize("refuse", [_refuse_the_look, _refuse_the_link_check])
@pytest.mark.parametrize("which", [_artifact, _receipt])
def test_identity_answers_no_when_the_file_system_refuses_the_question(
    stored: Stored,
    monkeypatch: pytest.MonkeyPatch,
    refuse: Callable[[pytest.MonkeyPatch, Path], None],
    which: Callable[[Path], Path],
) -> None:
    store, receipt, root, _ = stored
    _, identity = store.inspect_with_identity(receipt)
    assert identity is not None and store.identity_current(receipt, identity)
    # Currentness is asked per chunk and by the reaper's thread: a refusal is an answer there,
    # never an error that leaves the store, and the answer is the boolean a caller compares.
    refuse(monkeypatch, which(root))
    assert store.identity_current(receipt, identity) is False


@pytest.mark.parametrize("which", [_artifact, _receipt])
def test_a_file_the_stores_link_check_flags_has_no_identity(
    stored: Stored, monkeypatch: pytest.MonkeyPatch, which: Callable[[Path], Path]
) -> None:
    store, receipt, root, _ = stored
    _, identity = store.inspect_with_identity(receipt)
    assert identity is not None and store.identity_current(receipt, identity)
    flagged = which(root)
    original = store_module._is_link_or_reparse

    # A regular file that carries a reparse tag cannot be made by this test's user, so the store's
    # own check is told that it sees one.
    def is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
        return path == flagged or original(path, metadata)

    monkeypatch.setattr(store_module, "_is_link_or_reparse", is_link_or_reparse)
    assert not store.identity_current(receipt, identity)
    # The full inspection refuses such a file too, so nothing records an identity for it.
    inspection, again = store.inspect_with_identity(receipt)
    assert inspection.status is ArtifactInspectionStatus.UNSAFE
    assert again is None


class _Lease:
    """The private scratch copy as the source sees it: present, with the probed length."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.released = False

    def inspect_artifact(self, _limit: int) -> tuple[str, int]:
        return ("ok", self.path.stat().st_size) if not self.released else ("missing", 0)

    def release(self) -> None:
        self.released = True


@pytest.fixture
def source(stored: Stored, tmp_path: Path) -> GeneratedAuthoringVideoSource:
    store, receipt, _, _ = stored
    copy = tmp_path / "scratch.bin"
    copy.write_bytes(BODY)
    return GeneratedAuthoringVideoSource(
        cast(Any, _Lease(copy)),
        cast(Any, SimpleNamespace(byte_length=len(BODY))),
        receipt,
        store,
        lambda: True,
    )


@pytest.fixture
def full_inspections(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    calls: list[int] = []
    original = PrivateSegmentArtifactStore.inspect

    def counted(self: PrivateSegmentArtifactStore, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PrivateSegmentArtifactStore, "inspect", counted)
    yield calls


def test_a_generated_source_asks_about_its_artifact_with_its_lock_held(
    stored: Stored, tmp_path: Path
) -> None:
    store, receipt, _, _ = stored
    copy = tmp_path / "scratch.bin"
    copy.write_bytes(BODY)
    held: list[bool] = []

    def production_is_current() -> bool:
        held.append(lock.held())
        return True

    source = GeneratedAuthoringVideoSource(
        cast(Any, _Lease(copy)),
        cast(Any, SimpleNamespace(byte_length=len(BODY))),
        receipt,
        store,
        production_is_current,
    )
    lock = _OwnedLock(source._lock)
    source._lock = lock  # type: ignore[assignment]
    # The owner's flag, the borrower count and the recorded identity change under this lock.
    # Every question that reads them and then the artifact is one locked section, the three
    # that prove as much as the one that answers from the identity.
    assert source.current() and source.verify_artifact()
    borrower = source.borrow_for_render()
    assert borrower.current()
    assert held == [True] * 4


def test_currentness_proves_once_and_then_answers_from_identity(
    source: GeneratedAuthoringVideoSource, full_inspections: list[int]
) -> None:
    assert source.current()
    assert len(full_inspections) == 1
    assert all(source.current() for _ in range(25))
    assert source.claim() is source
    assert len(full_inspections) == 1


def test_a_verification_always_proves(
    source: GeneratedAuthoringVideoSource, full_inspections: list[int]
) -> None:
    assert source.current()
    assert source.verify_artifact() and source.verify_artifact()
    assert len(full_inspections) == 3


@windows_only
def test_a_confirmation_read_proves_the_stored_artifact_exactly_once(
    source: GeneratedAuthoringVideoSource, full_inspections: list[int]
) -> None:
    assert source.current()
    del full_inspections[:]
    _read_generated_source_fingerprint(source, time.monotonic() + 30)
    assert len(full_inspections) == 1


def test_a_replacement_the_identity_cannot_see_ends_the_source_at_its_next_verification(
    stored: Stored, source: GeneratedAuthoringVideoSource
) -> None:
    _, _, root, _ = stored
    assert source.current()
    _rewrite_keeping_identity(_artifact(root), b"X" * len(BODY))

    assert source.current()  # the documented cheap-tier residual ...
    with pytest.raises(AuthoringSourceBindingError, match="source_stale"):
        _read_generated_source_fingerprint(source, time.monotonic() + 30)
    assert not source.verify_artifact()
    assert not source.current()  # ... which ends with the first proof, and stays ended.


@pytest.mark.parametrize("change", [_remove_artifact, _expire, _disable, _grow_artifact])
def test_a_change_the_identity_sees_ends_the_source_at_once(
    stored: Stored,
    source: GeneratedAuthoringVideoSource,
    change: Callable[[Any, Path, list[int], Any], None],
) -> None:
    store, receipt, root, clock = stored
    assert source.current()
    change(store, root, clock, receipt)
    assert not source.current()
    assert not source.verify_artifact()


def test_a_timestamp_change_costs_one_proof_and_keeps_the_source(
    stored: Stored, source: GeneratedAuthoringVideoSource, full_inspections: list[int]
) -> None:
    store, receipt, root, clock = stored
    assert source.current()
    _touch_artifact(store, root, clock, receipt)
    assert source.current() and source.current()
    assert len(full_inspections) == 2


def test_production_revocation_and_release_end_the_source_without_reading(
    stored: Stored, tmp_path: Path, full_inspections: list[int]
) -> None:
    store, receipt, _, _ = stored
    copy = tmp_path / "scratch.bin"
    copy.write_bytes(BODY)
    authority = [True]
    source = GeneratedAuthoringVideoSource(
        cast(Any, _Lease(copy)),
        cast(Any, SimpleNamespace(byte_length=len(BODY))),
        receipt,
        store,
        lambda: authority[0],
    )
    assert source.current()
    del full_inspections[:]
    authority[0] = False
    assert not source.current() and not source.verify_artifact()
    authority[0] = True
    source.release()
    assert not source.current() and not source.verify_artifact()
    assert full_inspections == []


def test_a_render_borrower_keeps_the_proof_for_every_question(
    source: GeneratedAuthoringVideoSource, full_inspections: list[int]
) -> None:
    assert source.current()
    del full_inspections[:]
    borrower = source.borrow_for_render()
    assert len(full_inspections) == 1
    assert borrower.current() and borrower.current()
    assert len(full_inspections) == 3
    # The borrower outlives the workspace owner, which has no further say over its reads. The
    # borrower's lease keeps the scratch copy in place, and that gives the released owner nothing:
    # neither its currentness nor a verification answers for it again.
    source.release()
    assert not source.current()
    assert not source.verify_artifact()
    assert borrower.current()
    borrower.release()
    assert not borrower.current()


def test_a_released_owner_lends_nothing_while_an_earlier_borrower_keeps_the_artifact(
    source: GeneratedAuthoringVideoSource,
) -> None:
    borrower = source.borrow_for_render()
    source.release()
    # The earlier borrower keeps the scratch copy in place, so the artifact itself still proves.
    # The owner is gone all the same, and a source without its owner lends to nobody new.
    assert borrower.current()
    with pytest.raises(AuthoringSourceBindingError, match="source_stale"):
        source.borrow_for_render()
    borrower.release()
    assert not borrower.current()


def test_the_source_answers_a_borrowers_question_only_while_it_has_a_borrower(
    source: GeneratedAuthoringVideoSource, full_inspections: list[int]
) -> None:
    assert source.current()
    del full_inspections[:]
    assert not source.current_for_borrower()
    assert full_inspections == []
    borrower = source.borrow_for_render()
    assert source.current_for_borrower()
    borrower.release()
    del full_inspections[:]
    assert not source.current_for_borrower()
    assert full_inspections == []


def test_a_proof_that_ends_in_an_error_leaves_no_identity_to_answer_from(
    source: GeneratedAuthoringVideoSource,
    full_inspections: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert source.current()
    proof = PrivateSegmentArtifactStore.inspect_with_identity

    def unreadable(self: PrivateSegmentArtifactStore, *args: Any, **kwargs: Any) -> Any:
        raise OSError("the store could not be read")

    monkeypatch.setattr(PrivateSegmentArtifactStore, "inspect_with_identity", unreadable)
    assert not source.verify_artifact()
    monkeypatch.setattr(PrivateSegmentArtifactStore, "inspect_with_identity", proof)
    del full_inspections[:]
    # The identity is forgotten before the proof that replaces it starts. A proof that ended in
    # an error therefore leaves none behind, and the next question pays for a proof of its own
    # instead of answering from what an earlier one saw.
    assert source.current()
    assert len(full_inspections) == 1
