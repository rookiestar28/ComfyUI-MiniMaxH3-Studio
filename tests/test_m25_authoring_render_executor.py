"""Private-store behavior tests; synthetic observations do not qualify a renderer."""

from __future__ import annotations

import copy
import hashlib
import importlib
import os
import pickle
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_m25_authoring_render_receipts import _receipt
from test_m25_render_job_leases import image_bound as image_bound

from comfyui_h3_context.core.authoring_render_jobs import RenderJobLimits


def stores() -> Any:
    return importlib.import_module("comfyui_h3_context.adapters.authoring_render_store")


def executors() -> Any:
    return importlib.import_module("comfyui_h3_context.adapters.authoring_render_executor")


def process_control() -> Any:
    return SimpleNamespace(deadline=time.monotonic() + 30, is_cancelled=lambda: False)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only render enforcement")
def test_stage_input_pin_denies_write_delete_and_releases_after_failure(tmp_path: Path) -> None:
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage = store.begin("render-" + "b" * 32, "sha256:" + "b" * 64)
        payload = b"synthetic-private-input"
        path = stage.write_input(payload, suffix=".txt")
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        with pytest.raises(RuntimeError, match="synthetic failure"):
            with executors()._pin_stage_inputs(stage, {path: digest}, process_control()):
                assert path.read_bytes() == payload
                with pytest.raises(OSError):
                    path.write_bytes(b"replacement")
                with pytest.raises(OSError):
                    path.unlink()
                raise RuntimeError("synthetic failure")
        path.write_bytes(payload)
        assert path.read_bytes() == payload
    finally:
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only render enforcement")
def test_stage_input_pin_rejects_replaced_bytes_and_foreign_paths(tmp_path: Path) -> None:
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage = store.begin("render-" + "b" * 32, "sha256:" + "b" * 64)
        path = stage.write_input(b"same-size-original", suffix=".txt")
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        path.write_bytes(b"same-size-replaced")
        for candidate in (path, tmp_path / "foreign.txt"):
            with pytest.raises(executors().RenderPreparationError, match="source_invalid"):
                with executors()._pin_stage_inputs(stage, {candidate: digest}, process_control()):
                    pytest.fail("tampered or foreign input admitted")
    finally:
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only render enforcement")
def test_executor_failed_child_releases_inputs_without_publishing(
    tmp_path: Path, image_bound: Any
) -> None:
    from test_m25_authoring_render_process import Control, command_pin, processes
    from test_m25_render_job_leases import acquire

    bound = image_bound[5]
    sources = acquire(bound)
    store = stores().RenderOutputStore(tmp_path)
    control = Control()
    try:
        stage = store.begin("render-" + "c" * 32, "sha256:" + "c" * 64)
        prepared = executors().prepare_render_assets(
            plan=bound.plan, sources=sources, stage=stage, control=control
        )
        # The pinned non-renderer is a deliberate negative executable boundary, never
        # qualification evidence. Its failed child must release input pins and job slots.
        with command_pin(control) as renderer, processes().WindowsRenderProcessSession() as session:
            with pytest.raises(processes().RenderProcessError, match="process_failed"):
                executors().run_prepared_render(
                    plan=bound.plan,
                    prepared=prepared,
                    renderer=renderer,
                    session=session,
                    control=control,
                )
            assert session.active_processes == 0
        assert not stage.output_path.exists()
        assert store.retained_count == 0
        prepared.media[0].path.write_bytes(b"pins released")
    finally:
        sources.release()
        store.close()


def test_image_packing_preserves_float_code_values_and_channel_order() -> None:
    pixels = struct.pack("<6f", 0.1, 0.2, 0.3, 0.4, 0.5, 0.6)
    packed = executors().pack_planar_image(pixels, width=2, height=1, control=process_control())
    values = struct.unpack("<6f", packed)
    assert values == pytest.approx((0.2, 0.5, 0.3, 0.6, 0.1, 0.4))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.001, 1.001])
def test_image_packing_never_clamps_or_fills_invalid_pixels(value: float) -> None:
    pixels = struct.pack("<3f", value, 0.2, 0.3)
    with pytest.raises(executors().RenderPreparationError, match="source_invalid"):
        executors().pack_planar_image(pixels, width=1, height=1, control=process_control())


@pytest.mark.parametrize("dimensions", [(True, 1), (0, 1), (8193, 1), (4096, 4096)])
def test_image_packing_rejects_invalid_dimensions(dimensions: tuple[int, int]) -> None:
    with pytest.raises(executors().RenderPreparationError, match="source_invalid"):
        executors().pack_planar_image(
            b"\x00" * 12, width=dimensions[0], height=dimensions[1], control=process_control()
        )


@pytest.mark.parametrize("failure", ["cancelled", "deadline"])
def test_image_packing_checks_stop_before_processing(failure: str) -> None:
    control = process_control()
    if failure == "cancelled":
        control.is_cancelled = lambda: True
    else:
        control.deadline = time.monotonic() - 1
    with pytest.raises(Exception, match=failure):
        executors().pack_planar_image(b"\x00" * 12, width=1, height=1, control=control)


def test_image_packing_preserves_signed_zero_bits() -> None:
    pixels = struct.pack("<3I", 0x80000000, 0, 0x80000000)
    result = executors().pack_planar_image(pixels, width=1, height=1, control=process_control())
    assert struct.unpack("<3I", result) == (0, 0x80000000, 0x80000000)


def test_preparation_stages_only_exact_job_lease_and_carries_no_public_path(
    tmp_path: Path, image_bound: Any
) -> None:
    from test_m25_render_job_leases import acquire

    from comfyui_h3_context.core.authoring_render_jobs import request_for_render_plan

    bound = image_bound[5]
    request = request_for_render_plan(
        bound.plan, idempotency_key="prepare-image-01", timeout_ms=30000
    )
    store = stores().RenderOutputStore(tmp_path)
    sources = acquire(bound)
    try:
        stage = store.begin("render-" + "c" * 32, request.fingerprint)
        prepared = executors().prepare_render_assets(
            plan=bound.plan, sources=sources, stage=stage, control=process_control()
        )
        assert len(prepared.media) == 1
        item = prepared.media[0]
        assert item.origin == "runtime_image"
        assert item.asset_id == "img-overlay"
        assert item.path.parent == stage.output_path.parent
        assert len(item.path.read_bytes()) == 48
        assert str(tmp_path) not in repr(prepared)
        assert str(tmp_path) not in repr(item)
        for value in (prepared, item):
            for operation in (copy.copy, copy.deepcopy, pickle.dumps):
                with pytest.raises(TypeError, match="not (copyable|serializable)"):
                    operation(value)
        assert not stage.output_path.exists()
        sources.release()
        with pytest.raises(Exception, match="source_released"):
            executors().prepare_render_assets(
                plan=bound.plan, sources=sources, stage=stage, control=process_control()
            )
        assert len(list(stage.output_path.parent.iterdir())) == 1
    finally:
        sources.release()
        store.close()


def test_preparation_rejects_mismatched_source_join_before_writing(
    tmp_path: Path, image_bound: Any
) -> None:
    from test_m25_render_job_leases import acquire

    bound = image_bound[5]
    fact = replace(bound.plan.source_bindings[0], source_fingerprint="sha256:" + "0" * 64)
    plan = replace(bound.plan, source_bindings=(fact,))
    store = stores().RenderOutputStore(tmp_path)
    sources = acquire(bound)
    try:
        stage = store.begin("render-" + "d" * 32, "sha256:" + "d" * 64)
        with pytest.raises(executors().RenderPreparationError, match="plan_mismatch"):
            executors().prepare_render_assets(
                plan=plan, sources=sources, stage=stage, control=process_control()
            )
        assert list(stage.output_path.parent.iterdir()) == []
    finally:
        sources.release()
        store.close()


@pytest.mark.parametrize(
    "weight,style", [(400, "normal"), (700, "normal"), (400, "italic"), (700, "italic")]
)
def test_text_preparation_uses_exact_packaged_face_and_literal_utf8(
    tmp_path: Path, weight: int, style: str
) -> None:
    from test_m25_render_planner import _snapshot

    from comfyui_h3_context.adapters.authoring_fonts import (
        load_packaged_font_manifest,
        require_text_coverage,
    )

    title = next(clip.text for clip in _snapshot().clips if clip.text is not None)
    title = replace(title, weight=weight, style=style, content="Literal %{pts}: '[];,\\\nΩ Ж")
    manifest = load_packaged_font_manifest()
    face = require_text_coverage(manifest, title.font_asset_id, weight, style, title.content)
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage = store.begin("render-" + "e" * 32, "sha256:" + "e" * 64)
        prepared = executors().prepare_render_text(
            clip_id="title-01", style=title, fonts=manifest, stage=stage, control=process_control()
        )
        assert prepared.text_path.read_bytes() == title.content.encode("utf-8")
        assert prepared.font_path.read_bytes() == face.read_verified_bytes()
        assert prepared.font_fingerprint == face.file_fingerprint
        assert str(tmp_path) not in repr(prepared)
        assert title.content not in repr(prepared)
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(TypeError, match="not (copyable|serializable)"):
                operation(prepared)
    finally:
        store.close()


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_tabbed_text_keeps_literal_lines_and_pins_without_expanding_tabs(
    tmp_path: Path, align: str
) -> None:
    from test_m25_render_planner import _snapshot

    from comfyui_h3_context.adapters.authoring_fonts import load_packaged_font_manifest

    title = next(clip.text for clip in _snapshot().clips if clip.text is not None)
    title = replace(title, content="H\tH\nHHHHH\n", align=align)
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage = store.begin("render-" + "f" * 32, "sha256:" + "f" * 64)
        prepared = executors().prepare_render_text(
            clip_id="tabbed-title",
            style=title,
            fonts=load_packaged_font_manifest(),
            stage=stage,
            control=process_control(),
        )
        assert prepared.text_path.read_bytes() == title.content.encode()
        if align == "left":
            assert not prepared.lines
        else:
            assert [path.read_bytes() if path else b"" for path, _ in prepared.lines] == [
                b"H\tH",
                b"HHHHH",
                b"",
            ]
            for path, fingerprint in prepared.lines:
                assert (
                    fingerprint
                    == "sha256:" + hashlib.sha256(path.read_bytes() if path else b"").hexdigest()
                )
    finally:
        store.close()


def test_text_preparation_never_substitutes_an_unsupported_glyph(tmp_path: Path) -> None:
    from test_m25_render_planner import _snapshot

    from comfyui_h3_context.adapters.authoring_fonts import load_packaged_font_manifest

    title = next(clip.text for clip in _snapshot().clips if clip.text is not None)
    title = replace(title, content="漢")
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage = store.begin("render-" + "f" * 32, "sha256:" + "f" * 64)
        with pytest.raises(executors().RenderPreparationError, match="font_unavailable"):
            executors().prepare_render_text(
                clip_id="title-01",
                style=title,
                fonts=load_packaged_font_manifest(),
                stage=stage,
                control=process_control(),
            )
        assert list(stage.output_path.parent.iterdir()) == []
    finally:
        store.close()


@pytest.mark.parametrize("tabbed", [False, True])
def test_full_preparation_deduplicates_resolved_title_frames(
    tmp_path: Path, image_bound: Any, tabbed: bool
) -> None:
    from test_m25_render_job_leases import acquire
    from test_m25_render_planner import _snapshot_wire

    from comfyui_h3_context.adapters.authoring_render_source import prepare_bound_render_plan
    from comfyui_h3_context.core.composition_contract import (
        decode_public_snapshot,
        public_snapshot_fingerprint,
    )

    old_bound = image_bound[5]
    wire = old_bound._issued_snapshot.to_wire()
    template = _snapshot_wire()
    wire["assets"].extend(row for row in template["assets"] if row["kind"] == "font")
    wire["tracks"].extend(row for row in template["tracks"] if row["kind"] == "text_overlay")
    title = next(row for row in template["clips"] if row["text"] is not None)
    title["start_frame"] = 0
    title["duration_frames"] = 4
    if tabbed:
        title["text"].update(content="H\tH\nHHHHH", align="center")
    wire["clips"].append(title)
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    snapshot = decode_public_snapshot(wire)
    bound = prepare_bound_render_plan(old_bound._history, snapshot, lambda: True)
    sources = acquire(bound)
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage = store.begin("render-" + "a" * 32, "sha256:" + "a" * 64)
        prepared = executors().prepare_render_assets(
            plan=bound.plan, sources=sources, stage=stage, control=process_control()
        )
        assert len(prepared.media) == len(prepared.texts) == 1
        assert prepared.texts[0].clip_id == title["clip_id"]
        assert len(list(stage.output_path.parent.iterdir())) == (5 if tabbed else 3)
        if tabbed:
            from comfyui_h3_context.adapters.authoring_render_graph import build_render_graph

            graph = build_render_graph(plan=bound.plan, prepared=prepared).filter_graph
            assert graph.count("text_align=L:tabsize=4") == 2
            assert "text_align=C" not in graph and title["text"]["content"] not in graph
            for path, _ in prepared.texts[0].lines:
                assert path is not None and path.name in graph
    finally:
        sources.release()
        store.close()


def staged(store: Any, identifier: str = "a") -> tuple[Any, Any]:
    body = b"synthetic-complete-output"
    receipt = _receipt()
    receipt = replace(
        receipt,
        observed=replace(
            receipt.observed,
            byte_length=len(body),
            output_fingerprint="sha256:" + hashlib.sha256(body).hexdigest(),
        ),
    )
    stage = store.begin("render-" + identifier * 32, receipt.request.fingerprint)
    stage.output_path.write_bytes(body)
    return stage, receipt


def test_stage_is_not_addressable_until_independent_verification_and_atomic_commit(
    tmp_path: Path,
) -> None:
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage, receipt = staged(store)
        with pytest.raises(stores().RenderStoreError, match="output_unavailable"):
            store.read_output(stage.job_id, receipt.request.fingerprint)
        observed_paths: list[Path] = []

        def verify(path: Path) -> Any:
            assert path.read_bytes() == b"synthetic-complete-output"
            observed_paths.append(path)
            return receipt.observed

        artifact = store.commit(stage, receipt, verify_output=verify)
        assert len(observed_paths) == 1
        assert not stage.output_path.exists()
        assert artifact.receipt == receipt
        assert (
            store.read_output(stage.job_id, receipt.request.fingerprint)
            == b"synthetic-complete-output"
        )
        assert store.retained_count == 1
        assert store.staging_count == 0
    finally:
        store.close()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fault", ["probe", "digest", "request", "cancel"])
def test_failed_publication_never_creates_addressable_output(tmp_path: Path, fault: str) -> None:
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage, receipt = staged(store)
        measured = receipt.observed
        if fault == "probe":
            measured = replace(measured, frame_count=measured.frame_count - 1)
        elif fault == "digest":
            stage.output_path.write_bytes(b"x" * receipt.observed.byte_length)
        elif fault == "request":
            receipt = replace(
                receipt, request=replace(receipt.request, idempotency_key="different-request")
            )
        else:
            stage.cancel()
        with pytest.raises(stores().RenderStoreError):
            store.commit(stage, receipt, verify_output=lambda _path: measured)
        assert store.retained_count == 0
        assert store.staging_count == 0
        with pytest.raises(stores().RenderStoreError, match="output_unavailable"):
            store.read_output(stage.job_id, receipt.request.fingerprint)
    finally:
        store.close()
    assert list(tmp_path.iterdir()) == []


def test_store_binds_exact_stage_instance_and_never_overwrites_job(tmp_path: Path) -> None:
    left = stores().RenderOutputStore(tmp_path)
    right = stores().RenderOutputStore(tmp_path)
    try:
        stage, receipt = staged(left)
        with pytest.raises(stores().RenderStoreError, match="stage_unavailable"):
            right.commit(stage, receipt, verify_output=lambda _path: receipt.observed)
        assert left.staging_count == 1
        left.commit(stage, receipt, verify_output=lambda _path: receipt.observed)
        with pytest.raises(stores().RenderStoreError, match="job_conflict"):
            left.begin(stage.job_id, receipt.request.fingerprint)
        with pytest.raises(stores().RenderStoreError, match="output_unavailable"):
            left.read_output(stage.job_id, "sha256:" + "0" * 64)
    finally:
        left.close()
        right.close()


def test_expiry_and_limits_remove_only_exact_owned_outputs(tmp_path: Path) -> None:
    sentinel = tmp_path / "unrelated.txt"
    sentinel.write_bytes(b"preserve")
    now = [1.0]
    limits = RenderJobLimits(retained_outputs=1, retention_seconds=2)
    store = stores().RenderOutputStore(tmp_path, limits=limits, clock=lambda: now[0])
    try:
        stage, receipt = staged(store)
        store.commit(stage, receipt, verify_output=lambda _path: receipt.observed)
        second, second_receipt = staged(store, "b")
        with pytest.raises(stores().RenderStoreError, match="resource_limit"):
            store.commit(
                second, second_receipt, verify_output=lambda _path: second_receipt.observed
            )
        assert store.staging_count == 0
        now[0] = 3.0
        store.prune()
        assert store.retained_count == 0
        assert sentinel.read_bytes() == b"preserve"
    finally:
        store.close()
    assert list(tmp_path.iterdir()) == [sentinel]


def test_cancel_during_independent_probe_prevents_late_publication(tmp_path: Path) -> None:
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage, receipt = staged(store)

        def verify(_path: Path) -> Any:
            stage.cancel()
            return receipt.observed

        with pytest.raises(stores().RenderStoreError, match="cancelled"):
            store.commit(stage, receipt, verify_output=verify)
        assert store.retained_count == store.staging_count == 0
    finally:
        store.close()


def test_output_tampering_cannot_borrow_retained_receipt(tmp_path: Path) -> None:
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage, receipt = staged(store)
        artifact = store.commit(stage, receipt, verify_output=lambda _path: receipt.observed)
        metadata = artifact._path.stat()
        artifact._path.write_bytes(b"x" * receipt.observed.byte_length)
        os.utime(artifact._path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        with pytest.raises(stores().RenderStoreError, match="output_invalid"):
            store.read_output(stage.job_id, receipt.request.fingerprint)
    finally:
        store.close()


def test_cancel_from_another_thread_does_not_wait_for_probe(tmp_path: Path) -> None:
    store = stores().RenderOutputStore(tmp_path)
    entered = threading.Event()
    proceed = threading.Event()
    failures: list[str] = []
    stage, receipt = staged(store)

    def verify(_path: Path) -> Any:
        entered.set()
        assert proceed.wait(3)
        return receipt.observed

    def publish() -> None:
        try:
            store.commit(stage, receipt, verify_output=verify)
        except stores().RenderStoreError as exc:
            failures.append(exc.code)

    worker = threading.Thread(target=publish)
    worker.start()
    try:
        assert entered.wait(3)
        stage.cancel()
        assert worker.is_alive()
    finally:
        proceed.set()
        worker.join(3)
        store.close()
    assert not worker.is_alive()
    assert failures == ["cancelled"]
    assert list(tmp_path.iterdir()) == []


def test_real_process_crash_recovers_owned_staging_without_resuming_jobs(tmp_path: Path) -> None:
    sentinel = tmp_path / "unrelated.txt"
    sentinel.write_bytes(b"preserve")
    script = """
import os, sys
from pathlib import Path
from comfyui_h3_context.adapters.authoring_render_store import RenderOutputStore
store = RenderOutputStore(Path(sys.argv[1]))
stage = store.begin('render-' + 'a' * 32, 'sha256:' + 'a' * 64)
stage.write_input(b'synthetic input')
stage.output_path.write_bytes(b'incomplete output')
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=False,
        timeout=10,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert result.returncode == 0
    old_sessions = tuple(tmp_path.glob("h3-render-*"))
    assert len(old_sessions) == 1
    store = stores().RenderOutputStore(tmp_path)
    try:
        assert store.staging_count == store.retained_count == 0
        assert not old_sessions[0].exists()
        with pytest.raises(stores().RenderStoreError, match="output_unavailable"):
            store.read_output("render-" + "a" * 32, "sha256:" + "a" * 64)
    finally:
        store.close()
    assert list(tmp_path.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"preserve"


def test_failure_after_actual_rename_rolls_back_owned_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = stores().RenderOutputStore(tmp_path)
    stage, receipt = staged(store)
    rename = os.replace

    def interrupted_rename(source: Path, destination: Path) -> None:
        rename(source, destination)
        raise OSError("synthetic interrupted return")

    monkeypatch.setattr(stores().os, "replace", interrupted_rename)
    try:
        with pytest.raises(stores().RenderStoreError, match="store_unavailable"):
            store.commit(stage, receipt, verify_output=lambda _path: receipt.observed)
        assert store.staging_count == store.retained_count == 0
        assert not stage.output_path.exists()
    finally:
        store.close()
    assert list(tmp_path.iterdir()) == []


def test_publication_name_collision_preserves_foreign_directory_and_discards_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = stores().RenderOutputStore(tmp_path)
    stage, receipt = staged(store)
    collision_id = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    collision = stage.output_path.parent.parent / ("artifact-" + collision_id.hex)
    collision.mkdir()
    sentinel = collision / "unrelated.txt"
    sentinel.write_bytes(b"preserve")
    monkeypatch.setattr(stores().uuid, "uuid4", lambda: collision_id)
    try:
        with pytest.raises(stores().RenderStoreError, match="job_conflict"):
            store.commit(stage, receipt, verify_output=lambda _path: receipt.observed)
        assert store.staging_count == store.retained_count == 0
        assert not stage.output_path.exists()
        assert sentinel.read_bytes() == b"preserve"
    finally:
        sentinel.unlink()
        collision.rmdir()
        store.close()


@pytest.mark.parametrize("operation", [copy.copy, copy.deepcopy, pickle.dumps])
def test_private_artifact_cannot_copy_or_serialize_private_paths(
    tmp_path: Path, operation: Any
) -> None:
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage, receipt = staged(store)
        artifact = store.commit(stage, receipt, verify_output=lambda _path: receipt.observed)
        assert str(tmp_path) not in repr(artifact)
        with pytest.raises(TypeError, match="not (copyable|serializable)"):
            operation(artifact)
    finally:
        store.close()


def test_stage_rejects_malformed_suffix_without_disclosing_payload(tmp_path: Path) -> None:
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage, _receipt_value = staged(store)
        malformed: Any = []
        with pytest.raises(stores().RenderStoreError, match="invalid_request"):
            stage.write_input(b"synthetic input", suffix=malformed)
    finally:
        store.close()


def test_staging_byte_budget_is_checked_before_input_allocation(tmp_path: Path) -> None:
    store = stores().RenderOutputStore(
        tmp_path, limits=RenderJobLimits(max_staging_bytes=32, max_output_bytes=32)
    )
    try:
        stage, _receipt_value = staged(store)
        before = tuple(stage.output_path.parent.iterdir())
        with pytest.raises(stores().RenderStoreError, match="resource_limit"):
            stage.write_input(b"x" * 32)
        assert tuple(stage.output_path.parent.iterdir()) == before
    finally:
        store.close()


def test_publication_callback_failure_rolls_back_the_unpublished_transaction(
    tmp_path: Path,
) -> None:
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage, receipt = staged(store)

        def refuse(_artifact: Any) -> None:
            raise RuntimeError("synthetic service cancellation")

        with pytest.raises(stores().RenderStoreError, match="store_unavailable"):
            store.commit(
                stage, receipt, verify_output=lambda _path: receipt.observed, on_publish=refuse
            )
        assert store.retained_count == store.staging_count == 0
        with pytest.raises(stores().RenderStoreError, match="output_unavailable"):
            store.read_output(stage.job_id, receipt.request.fingerprint)
    finally:
        store.close()
