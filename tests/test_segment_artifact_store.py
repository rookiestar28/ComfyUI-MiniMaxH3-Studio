from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

import comfyui_h3_context.adapters.segment_artifact_store as artifact_store_module
from comfyui_h3_context.adapters.segment_artifact_store import (
    ArtifactInspectionStatus,
    ArtifactStoreError,
    ArtifactStorePolicy,
    PrivateSegmentArtifactStore,
)
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.pipeline_transaction import (
    PipelineTransaction,
    prepare_pipeline_transaction,
    record_pipeline_running,
    record_pipeline_submission,
)
from comfyui_h3_context.core.recompute_closure import plan_recompute
from comfyui_h3_context.core.segment_artifacts import (
    ArtifactLifecycleState,
    SegmentArtifactError,
    SegmentArtifactReceipt,
    begin_segment_artifact_receipt,
    decode_segment_artifact_receipt,
)
from comfyui_h3_context.core.segment_workspace import (
    AcceptedIntentAuthority,
    SegmentContextManifest,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    create_workspace,
    derive_segment_manifests,
    revise_workspace,
)


def _fingerprint(character: str) -> str:
    return "sha256:" + character * 64


def _running_transaction() -> tuple[SegmentContextManifest, PipelineTransaction]:
    initial_segment = SegmentDeclaration(
        segment_id="segment.1",
        task_mode=TaskMode.T2VA,
        source_id="source.1",
        reference_ids=(),
        duration=SegmentDuration.from_frame_count(124),
        relation=SegmentRelationKind.INDEPENDENT,
        predecessor_segment_id=None,
        accepted_intent_fingerprint=_fingerprint("a"),
        semantic_receipt_fingerprint=_fingerprint("b"),
        profile_fingerprint=_fingerprint("1"),
        reference_registry_fingerprint=_fingerprint("2"),
        native_binding_fingerprint=_fingerprint("3"),
        producer_settings_fingerprint=_fingerprint("4"),
    )
    initial = create_workspace(
        "workspace.alpha",
        (initial_segment,),
        accepted_intent_authorities=(AcceptedIntentAuthority("segment.1", _fingerprint("a")),),
    )
    revised_segment = replace(initial_segment, producer_settings_fingerprint=_fingerprint("5"))
    current = revise_workspace(
        initial,
        expected_workspace_fingerprint=initial.fingerprint,
        accepted_intent_authorities=(AcceptedIntentAuthority("segment.1", _fingerprint("a")),),
        segments=(revised_segment,),
    )
    manifests = derive_segment_manifests(current)
    recompute = plan_recompute(derive_segment_manifests(initial), manifests)
    prepared = prepare_pipeline_transaction(
        workspace=current,
        manifests=manifests,
        recompute_plan=recompute,
        transaction_id="transaction.1",
        graph_fingerprint=_fingerprint("6"),
        compiled_prompt_fingerprint=_fingerprint("7"),
    )
    submitted = record_pipeline_submission(
        prepared,
        expected_transaction_fingerprint=prepared.fingerprint,
        queue_prompt_id="prompt.1",
    )
    running = record_pipeline_running(
        submitted,
        expected_transaction_fingerprint=submitted.fingerprint,
        host_owner_id="host.execution.1",
    )
    return manifests[0], running


def _partial(*, artifact_id: str = "artifact.1", now_ms: int = 100) -> SegmentArtifactReceipt:
    manifest, transaction = _running_transaction()
    return begin_segment_artifact_receipt(
        manifest=manifest,
        transaction=transaction,
        artifact_id=artifact_id,
        model_fingerprint=_fingerprint("8"),
        runtime_fingerprint=_fingerprint("9"),
        execution_fingerprint=_fingerprint("c"),
        predecessor_artifact_fingerprint=None,
        format_label="opaque.video.v1",
        shape=(120, 1920, 1080, 3),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 1_000,
    )


def _policy(**overrides: int) -> ArtifactStorePolicy:
    values = {
        "max_artifact_bytes": 64,
        "max_total_bytes": 256,
        "max_entries": 8,
        "ttl_seconds": 10,
        "max_concurrent_writes": 1,
        "max_recovery_entries": 64,
    }
    values.update(overrides)
    return ArtifactStorePolicy(**values)


def test_receipt_binds_exact_manifest_transaction_and_public_projection_is_locator_free() -> None:
    manifest, transaction = _running_transaction()
    receipt = _partial()

    assert receipt.state is ArtifactLifecycleState.PARTIAL
    assert receipt.workspace_fingerprint == manifest.workspace_fingerprint
    assert receipt.manifest_fingerprint == manifest.fingerprint
    assert receipt.graph_fingerprint == transaction.graph_fingerprint
    assert receipt.native_binding_fingerprint == manifest.native_binding_fingerprint
    assert receipt.settings_fingerprint == manifest.producer_settings_fingerprint
    assert receipt.transaction_fingerprint == transaction.fingerprint
    projection = receipt.to_public_dict()
    assert set(projection) == {
        "schema",
        "artifact_kind",
        "artifact_id",
        "state",
        "format_label",
        "shape",
        "byte_length",
        "created_at_ms",
        "expires_at_ms",
        "receipt_token",
        "output_token",
    }
    assert projection["receipt_token"] != receipt.fingerprint
    public = str(projection).lower()
    for forbidden in ("path", "locator", "credential", "prompt", "http://", "\\\\"):
        assert forbidden not in public

    from comfyui_h3_context import core

    assert core.SegmentArtifactReceipt is SegmentArtifactReceipt
    assert core.begin_segment_artifact_receipt is begin_segment_artifact_receipt


def test_receipt_wire_rejects_duplicate_members_and_fingerprint_tamper() -> None:
    receipt = _partial()
    assert decode_segment_artifact_receipt(receipt.to_wire_bytes()) == receipt
    duplicate = receipt.to_wire_bytes().replace(
        b'{"artifact_id":',
        b'{"artifact_id":"artifact.duplicate","artifact_id":',
        1,
    )
    with pytest.raises(SegmentArtifactError, match="duplicate_receipt_member"):
        decode_segment_artifact_receipt(duplicate)
    with pytest.raises(SegmentArtifactError, match="receipt_fingerprint_mismatch"):
        replace(receipt, receipt_fingerprint=_fingerprint("f"))


def test_atomic_partial_to_complete_promotion_is_exact_reusable_and_no_clobber() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        clock = [100]
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(
            root,
            policy=_policy(),
            clock_ms=lambda: clock[0],
        )
        partial = store.begin(_partial())
        completed = store.commit(partial, b"generated-segment")

        assert completed.state is ArtifactLifecycleState.COMPLETE
        assert completed.byte_length == len(b"generated-segment")
        assert completed.output_fingerprint is not None
        assert completed.output_fingerprint.startswith("sha256:")
        inspection = store.inspect(completed)
        assert inspection.status is ArtifactInspectionStatus.REUSABLE
        assert store.read(completed) == b"generated-segment"
        with pytest.raises(ArtifactStoreError, match="duplicate_artifact"):
            store.begin(_partial())


def test_wrong_identity_and_tampered_payload_are_never_reusable() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        completed = store.commit(store.begin(_partial()), b"generated-segment")
        incompatible = replace(
            completed,
            model_fingerprint=_fingerprint("d"),
            receipt_fingerprint=None,
        )
        assert store.inspect(incompatible).status is ArtifactInspectionStatus.INCOMPATIBLE

        payload_path = root / "artifacts" / "artifact.1.bin"
        payload_path.write_bytes(b"tampered-segment!")
        assert store.inspect(completed).status is ArtifactInspectionStatus.TAMPERED
        with pytest.raises(ArtifactStoreError, match="artifact_not_reusable"):
            store.read(completed)


def test_quota_and_expiry_fail_with_typed_non_reuse_results() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        clock = [100]
        store = PrivateSegmentArtifactStore(
            Path(temporary) / "private-store",
            policy=_policy(max_artifact_bytes=4, max_total_bytes=6),
            clock_ms=lambda: clock[0],
        )
        partial = store.begin(_partial())
        with pytest.raises(ArtifactStoreError, match="artifact_too_large"):
            store.commit(partial, b"12345")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(
            root,
            policy=_policy(max_artifact_bytes=4, max_total_bytes=6),
            clock_ms=lambda: 100,
        )
        store.commit(store.begin(_partial()), b"1234")
        second = store.begin(_partial(artifact_id="artifact.2"))
        with pytest.raises(ArtifactStoreError, match="byte_quota_exceeded"):
            store.commit(second, b"123")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(
            root,
            policy=_policy(max_entries=1),
            clock_ms=lambda: 100,
        )
        store.commit(store.begin(_partial()), b"one")
        with pytest.raises(ArtifactStoreError, match="entry_quota_exceeded"):
            store.begin(_partial(artifact_id="artifact.2"))

    with tempfile.TemporaryDirectory() as temporary:
        clock = [100]
        store = PrivateSegmentArtifactStore(
            Path(temporary) / "private-store",
            policy=_policy(),
            clock_ms=lambda: clock[0],
        )
        completed = store.commit(store.begin(_partial()), b"ok")
        clock[0] = completed.expires_at_ms
        assert store.inspect(completed).status is ArtifactInspectionStatus.EXPIRED
        report = store.recover()
        assert report.expired_removed == 1
        assert store.inspect(completed).status is ArtifactInspectionStatus.MISSING


def test_policy_ttl_and_write_concurrency_are_enforced_before_publication() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(
            root,
            policy=_policy(ttl_seconds=1),
            clock_ms=lambda: 100,
        )
        too_long = replace(_partial(), expires_at_ms=1_101, receipt_fingerprint=None)
        with pytest.raises(ArtifactStoreError, match="receipt_ttl_exceeds_policy"):
            store.begin(too_long)

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(
            root,
            policy=_policy(max_concurrent_writes=1),
            clock_ms=lambda: 100,
        )
        partial = store.begin(_partial())
        assert store._write_slots.acquire(blocking=False)
        try:
            with pytest.raises(ArtifactStoreError, match="write_concurrency_limit"):
                store.commit(partial, b"generated-segment")
        finally:
            store._write_slots.release()
        assert not (root / "artifacts" / "artifact.1.bin").exists()


def test_receipt_publication_failure_rolls_back_payload_without_losing_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(
            root,
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        partial = store.begin(_partial())
        publish = store._atomic_publish_new

        def fail_receipt(final: Path, payload: bytes, *, parent: Path) -> None:
            if final.parent.name == "receipts":
                raise ArtifactStoreError("injected_receipt_failure")
            publish(final, payload, parent=parent)

        monkeypatch.setattr(store, "_atomic_publish_new", fail_receipt)
        with pytest.raises(ArtifactStoreError, match="injected_receipt_failure"):
            store.commit(partial, b"generated-segment")
        assert not (root / "artifacts" / "artifact.1.bin").exists()
        assert (root / "staging" / "artifact.1.partial.json").is_file()


def test_payload_write_failure_leaves_no_final_or_publish_temporary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(
            root,
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        partial = store.begin(_partial())

        def fail_write(descriptor: int, payload: bytes | memoryview) -> int:
            raise OSError("injected payload write failure")

        monkeypatch.setattr(os, "write", fail_write)
        with pytest.raises(ArtifactStoreError, match="store_write_failed"):
            store.commit(partial, b"generated-segment")

        assert not (root / "artifacts" / "artifact.1.bin").exists()
        assert (root / "staging" / "artifact.1.partial.json").is_file()
        assert sorted(path.name for path in (root / "staging").iterdir()) == [
            "artifact.1.partial.json"
        ]


def test_failed_lifecycle_is_retained_without_a_reusable_payload() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(
            root,
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        partial = store.begin(_partial())
        failed = store.fail(partial, failure_code="host_execution_failed")

        assert failed.state is ArtifactLifecycleState.FAILED
        assert failed.failure_code == "host_execution_failed"
        assert store.inspect(failed).status is ArtifactInspectionStatus.NOT_COMPLETE
        assert not (root / "artifacts" / "artifact.1.bin").exists()


def test_recovery_removes_only_owned_orphans_and_staging_files() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        foreign = Path(temporary) / "foreign.bin"
        foreign.write_bytes(b"keep")
        (root / "artifacts" / "orphan.bin").write_bytes(b"orphan")
        (root / "staging" / "crash.tmp").write_bytes(b"partial")

        report = store.recover()

        assert report.orphans_removed == 1
        assert report.staging_removed == 1
        assert foreign.read_bytes() == b"keep"
        assert root.is_dir()


def test_recovery_aggregate_admission_is_bounded_before_any_mutation() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(
            root,
            policy=_policy(max_entries=1, max_recovery_entries=2),
            clock_ms=lambda: 100,
        )
        first = root / "staging" / "first.tmp"
        second = root / "staging" / "second.tmp"
        orphan = root / "artifacts" / "orphan.bin"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        orphan.write_bytes(b"three")

        with pytest.raises(ArtifactStoreError, match="recovery_scan_limit"):
            store.recover()
        assert first.is_file() and second.is_file() and orphan.is_file()

        second.unlink()
        report = store.recover()
        assert report.staging_removed == 1
        assert report.orphans_removed == 1


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        result = subprocess.run(
            [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(link), str(target)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
            timeout=5,
        )
        assert result.returncode == 0


def _replace_with_directory_link(path: Path, target: Path) -> None:
    path.rmdir()
    target.mkdir()
    _make_directory_link(path, target)


def test_link_or_reparse_root_and_artifact_leaf_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        target = base / "target"
        target.mkdir()
        link = base / "linked-store"
        _make_directory_link(link, target)
        with pytest.raises(ArtifactStoreError, match="unsafe_store_root"):
            PrivateSegmentArtifactStore(link, policy=_policy(), clock_ms=lambda: 100)
        link.unlink() if link.is_symlink() else link.rmdir()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        completed = store.commit(store.begin(_partial()), b"generated-segment")
        payload = root / "artifacts" / "artifact.1.bin"
        payload.unlink()
        target = Path(temporary) / "foreign.bin"
        target.write_bytes(b"generated-segment")
        try:
            payload.symlink_to(target)
        except OSError:
            os.link(target, payload)
        assert store.inspect(completed).status is ArtifactInspectionStatus.UNSAFE
        payload.unlink()


@pytest.mark.parametrize("replaced_child", ["artifacts", "receipts"])
def test_post_init_child_junction_never_publishes_outside_private_root(
    replaced_child: str,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root = base / "private-store"
        store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        partial = store.begin(_partial())
        outside = base / f"outside-{replaced_child}"
        _replace_with_directory_link(root / replaced_child, outside)
        try:
            with pytest.raises(ArtifactStoreError, match="unsafe_store_entry"):
                store.commit(partial, b"private-generated-segment")
            assert list(outside.iterdir()) == []
            assert not (root / "artifacts" / "artifact.1.bin").exists()
        finally:
            link = root / replaced_child
            link.unlink() if link.is_symlink() else link.rmdir()


def test_post_init_staging_junction_blocks_read_and_cleanup_outside_root() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root = base / "private-store"
        store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        partial = store.begin(_partial())
        staged = root / "staging" / "artifact.1.partial.json"
        staged_payload = staged.read_bytes()
        staged.unlink()
        outside = base / "outside-staging"
        _replace_with_directory_link(root / "staging", outside)
        (outside / "artifact.1.partial.json").write_bytes(staged_payload)
        try:
            with pytest.raises(ArtifactStoreError, match="partial_receipt_invalid"):
                store.commit(partial, b"private-generated-segment")
            assert (outside / "artifact.1.partial.json").read_bytes() == staged_payload
            assert not (outside / "artifact.1.bin").exists()
        finally:
            (outside / "artifact.1.partial.json").unlink()
            link = root / "staging"
            link.unlink() if link.is_symlink() else link.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows no-delete-share regression")
def test_prewrite_admission_closes_validation_to_open_junction_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root = base / "private-store"
        outside = base / "outside-staging"
        store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        original_open = artifact_store_module._windows_open_new_file
        attempted = False

        def racing_open(path: Path) -> int:
            nonlocal attempted
            candidate = Path(path)
            if not attempted and candidate.parent == root / "staging":
                attempted = True
                _replace_with_directory_link(root / "staging", outside)
            return original_open(path)

        monkeypatch.setattr(artifact_store_module, "_windows_open_new_file", racing_open)
        try:
            with pytest.raises(ArtifactStoreError, match="unsafe_store_entry"):
                store.begin(_partial())
            assert attempted
            assert not outside.exists()
        finally:
            link = root / "staging"
            link.unlink() if link.is_symlink() else link.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows no-delete-share regression")
def test_prewrite_admission_closes_validation_to_publication_junction_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root = base / "private-store"
        outside = base / "outside-artifacts"
        store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        partial = store.begin(_partial())
        original_move = artifact_store_module._windows_move_new_file
        attempted = False

        def racing_move(source: Path, destination: Path) -> int:
            nonlocal attempted
            if not attempted and destination.parent == root / "artifacts":
                attempted = True
                _replace_with_directory_link(root / "artifacts", outside)
            return original_move(source, destination)

        monkeypatch.setattr(artifact_store_module, "_windows_move_new_file", racing_move)
        try:
            with pytest.raises(ArtifactStoreError, match="unsafe_store_entry"):
                store.commit(partial, b"private-generated-payload")
            assert attempted
            assert not outside.exists()
        finally:
            link = root / "artifacts"
            link.unlink() if link.is_symlink() else link.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows no-delete-share regression")
def test_open_payload_handle_blocks_junction_swap_while_private_bytes_are_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root = base / "private-store"
        outside = base / "outside-artifacts"
        store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        partial = store.begin(_partial())
        original_write = os.write
        attempted = False

        def racing_write(descriptor: int, payload: bytes | memoryview) -> int:
            nonlocal attempted
            if not attempted:
                attempted = True
                with pytest.raises(OSError):
                    _replace_with_directory_link(root / "artifacts", outside)
            return original_write(descriptor, payload)

        monkeypatch.setattr(os, "write", racing_write)
        completed = store.commit(partial, b"private-generated-payload")
        assert attempted
        assert store.read(completed) == b"private-generated-payload"
        assert not outside.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows no-clobber move regression")
def test_windows_publication_race_never_clobbers_existing_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        partial = store.begin(_partial())
        original_move = artifact_store_module._windows_move_new_file
        raced = False

        def racing_move(source: Path, destination: Path) -> int:
            nonlocal raced
            if not raced and destination.parent == root / "artifacts":
                raced = True
                destination.write_bytes(b"competing-publisher")
            return original_move(source, destination)

        monkeypatch.setattr(artifact_store_module, "_windows_move_new_file", racing_move)
        with pytest.raises(ArtifactStoreError, match="duplicate_artifact"):
            store.commit(partial, b"private-generated-payload")

        assert raced
        assert (root / "artifacts" / "artifact.1.bin").read_bytes() == b"competing-publisher"
        assert (root / "staging" / "artifact.1.partial.json").is_file()
        assert sorted(path.name for path in (root / "staging").iterdir()) == [
            "artifact.1.partial.json"
        ]


@pytest.mark.skipif(os.name != "nt", reason="Windows published-handle regression")
def test_windows_published_file_is_pinned_through_post_move_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        partial = store.begin(_partial())
        original_move = artifact_store_module._windows_move_new_file
        attempted = False

        def racing_move(source: Path, destination: Path) -> int:
            nonlocal attempted
            descriptor = original_move(source, destination)
            if not attempted and destination.parent == root / "artifacts":
                attempted = True
                with pytest.raises(OSError):
                    destination.unlink()
            return descriptor

        monkeypatch.setattr(artifact_store_module, "_windows_move_new_file", racing_move)
        completed = store.commit(partial, b"private-generated-payload")

        assert attempted
        assert store.read(completed) == b"private-generated-payload"


def test_disable_retain_and_disable_purge_are_persistent_and_bounded() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        completed = store.commit(store.begin(_partial()), b"generated-segment")
        inspection_projection = store.inspect(completed).to_public_dict()
        assert set(inspection_projection) == {"artifact_id", "status", "receipt_token"}
        assert inspection_projection["receipt_token"] != completed.fingerprint
        retained = store.disable(purge=False)
        assert retained.purged_entries == 0
        reopened = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        assert reopened.inspect(completed).status is ArtifactInspectionStatus.DISABLED
        with pytest.raises(ArtifactStoreError, match="store_disabled"):
            reopened.begin(_partial(artifact_id="artifact.2"))

        purged = reopened.disable(purge=True)
        assert purged.purged_entries == 1
        assert root.is_dir()
        assert not (root / "artifacts" / "artifact.1.bin").exists()


def test_future_receipt_and_cross_instance_lifecycle_quota_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        store = PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        future = replace(
            _partial(),
            created_at_ms=1_000_000,
            expires_at_ms=1_001_000,
            receipt_fingerprint=None,
        )
        with pytest.raises(ArtifactStoreError, match="receipt_created_in_future"):
            store.begin(future)

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        policy = _policy(max_entries=1)
        first_store = PrivateSegmentArtifactStore(root, policy=policy, clock_ms=lambda: 100)
        second_store = PrivateSegmentArtifactStore(root, policy=policy, clock_ms=lambda: 100)
        first = first_store.begin(_partial())
        with pytest.raises(ArtifactStoreError, match="entry_quota_exceeded"):
            second_store.begin(_partial(artifact_id="artifact.2"))
        failed = first_store.fail(first, failure_code="host_execution_failed")
        assert failed.state is ArtifactLifecycleState.FAILED
        with pytest.raises(ArtifactStoreError, match="entry_quota_exceeded"):
            second_store.begin(_partial(artifact_id="artifact.2"))


def test_cross_instance_byte_quota_concurrency_and_disable_truth_are_shared() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        policy = _policy(max_artifact_bytes=4, max_total_bytes=6, max_entries=2)
        first_store = PrivateSegmentArtifactStore(root, policy=policy, clock_ms=lambda: 100)
        second_store = PrivateSegmentArtifactStore(root, policy=policy, clock_ms=lambda: 100)
        first = first_store.begin(_partial())
        second = second_store.begin(_partial(artifact_id="artifact.2"))
        first_store.commit(first, b"1234")
        with pytest.raises(ArtifactStoreError, match="byte_quota_exceeded"):
            second_store.commit(second, b"123")

        assert first_store._write_slots.acquire(blocking=False)
        try:
            with pytest.raises(ArtifactStoreError, match="write_concurrency_limit"):
                second_store.commit(second, b"12")
        finally:
            first_store._write_slots.release()

        first_store.disable(purge=False)
        with pytest.raises(ArtifactStoreError, match="store_disabled"):
            second_store.begin(_partial(artifact_id="artifact.3"))


def test_current_store_migration_is_explicit_noop_and_foreign_root_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        root.mkdir()
        (root / "foreign.txt").write_text("keep", encoding="utf-8")
        with pytest.raises(ArtifactStoreError, match="foreign_store_root"):
            PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        assert (root / "foreign.txt").read_text(encoding="utf-8") == "keep"

    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateSegmentArtifactStore(
            Path(temporary) / "private-store",
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        migration = store.migrate()
        assert migration.status == "already_current"
        assert migration.from_schema == migration.to_schema


def test_reopen_rejects_policy_drift_for_the_same_private_root() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        PrivateSegmentArtifactStore(root, policy=_policy(), clock_ms=lambda: 100)
        with pytest.raises(ArtifactStoreError, match="store_policy_mismatch"):
            PrivateSegmentArtifactStore(
                root,
                policy=_policy(max_entries=7),
                clock_ms=lambda: 100,
            )


# ---------------------------------------------------------------------------
# M20-08: the chunked streaming primitive and stream_artifact_into
# ---------------------------------------------------------------------------


def _streaming_store(
    root: Path, payload: bytes
) -> tuple[PrivateSegmentArtifactStore, SegmentArtifactReceipt]:
    store = PrivateSegmentArtifactStore(
        root / "private-store",
        policy=_policy(max_artifact_bytes=4096, max_total_bytes=8192),
        clock_ms=lambda: 100,
    )
    return store, store.commit(store.begin(_partial()), payload)


def test_stream_artifact_into_copies_exactly_and_owns_its_admission() -> None:
    payload = b"generated-segment-stream" * 8
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        store, completed = _streaming_store(base, payload)
        destination = base / "staging" / "copy.media"
        destination.parent.mkdir()

        length, fingerprint = store.stream_artifact_into(completed, destination)

        assert (length, fingerprint) == (completed.byte_length, completed.output_fingerprint)
        assert destination.read_bytes() == payload

        with pytest.raises(ArtifactStoreError, match="store_configuration"):
            store.stream_artifact_into(completed, Path("relative/copy.media"))
        with pytest.raises(ArtifactStoreError, match="unsafe_store_entry|store_write_failed"):
            store.stream_artifact_into(completed, destination)
        assert destination.read_bytes() == payload


def test_stream_artifact_into_refuses_a_linked_or_tampered_artifact_leaf() -> None:
    payload = b"generated-segment-stream"
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        store, completed = _streaming_store(base, payload)
        leaf = base / "private-store" / "artifacts" / "artifact.1.bin"
        destination = base / "staging" / "copy.media"
        destination.parent.mkdir()

        # Same-length content tamper: the copy hashes en route and the divergent copy
        # must not survive.
        leaf.write_bytes(b"generated-segment-STREAM")
        with pytest.raises(ArtifactStoreError, match="artifact_not_reusable|unsafe_store_entry"):
            store.stream_artifact_into(completed, destination)
        assert not destination.exists()

        # Link replacement of the artifact leaf fails closed before any byte moves.
        leaf.unlink()
        foreign = base / "foreign.bin"
        foreign.write_bytes(payload)
        try:
            leaf.symlink_to(foreign)
        except OSError:
            os.link(foreign, leaf)
        with pytest.raises(ArtifactStoreError, match="artifact_not_reusable|unsafe_store_entry"):
            store.stream_artifact_into(completed, destination)
        assert not destination.exists()
        leaf.unlink()


def test_copy_primitive_admission_mirrors_the_pinned_read_and_write_rules() -> None:
    payload = b"streamed-primitive-payload"
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        source_root = base / "sources"
        source_root.mkdir()
        staging = base / "staging"
        staging.mkdir()
        source = source_root / "source.bin"
        source.write_bytes(payload)

        # Boundary: maximum_bytes == size succeeds; one byte less refuses and removes nothing.
        exact = artifact_store_module._copy_regular_file_into_new(
            source, staging / "exact.bin", maximum_bytes=len(payload)
        )
        assert exact == (len(payload), "sha256:" + hashlib.sha256(payload).hexdigest())
        with pytest.raises(ArtifactStoreError, match="unsafe_store_entry"):
            artifact_store_module._copy_regular_file_into_new(
                source, staging / "overlong.bin", maximum_bytes=len(payload) - 1
            )
        assert not (staging / "overlong.bin").exists()

        # A linked source (symlink where creatable, hardlink otherwise) fails closed.
        linked = source_root / "linked.bin"
        try:
            linked.symlink_to(source)
        except OSError:
            os.link(source, linked)
        with pytest.raises(ArtifactStoreError, match="unsafe_store_entry"):
            artifact_store_module._copy_regular_file_into_new(
                linked, staging / "linked-copy.bin", maximum_bytes=4096
            )
        assert not (staging / "linked-copy.bin").exists()
        linked.unlink()

        # A missing or reparse-point destination parent fails closed.
        with pytest.raises(ArtifactStoreError, match="unsafe_store_entry"):
            artifact_store_module._copy_regular_file_into_new(
                source, base / "missing-parent" / "copy.bin", maximum_bytes=4096
            )
        junction_target = base / "junction-target"
        junction = base / "junction-parent"
        junction_target.mkdir()
        _make_directory_link(junction, junction_target)
        with pytest.raises(ArtifactStoreError, match="unsafe_store_entry"):
            artifact_store_module._copy_regular_file_into_new(
                source, junction / "copy.bin", maximum_bytes=4096
            )
        assert tuple(junction_target.iterdir()) == ()
        junction.unlink() if junction.is_symlink() else junction.rmdir()

        # Cancellation at the first chunk removes the partial copy.
        with pytest.raises(ArtifactStoreError, match="stream_cancelled"):
            artifact_store_module._copy_regular_file_into_new(
                source,
                staging / "cancelled.bin",
                maximum_bytes=4096,
                should_cancel=lambda: True,
            )
        assert not (staging / "cancelled.bin").exists()
