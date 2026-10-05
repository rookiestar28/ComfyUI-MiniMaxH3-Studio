"""Facades over the process media runtime manager for the existing media consumers.

M25-32 replaced the import-time environment activation with the composition-root manager
(`media_runtime_manager`). These names stay as the narrow seams consumers already call. None of
them reads the environment or discovers a locator itself; the M25-30 resolver owns both.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ..core.av_reconstruction import qualified_av_limits
from ..core.canonical import canonical_fingerprint
from ..core.m26_assembly import build_production_assembly_capability
from .av_reconstruction_media import QualifiedAVMediaAdapter
from .av_reconstruction_store import AVStorePolicy, PrivateAVReconstructionStore
from .executable_admission import executable_pin_scope
from .m26_production_assembly import M26ProductionAssemblyRuntime
from .segment_artifact_store import PrivateSegmentArtifactStore

if TYPE_CHECKING:
    from .authoring_derivative_generator import AuthoringDerivativeGenerator
    from .media_runtime_manager import MediaRuntimeManager

IMPORT_ACTIVATION_SECONDS = 30.0


def _manager() -> MediaRuntimeManager:
    from .media_runtime_manager import media_runtime_manager

    return media_runtime_manager()


def current_authorized_media_runtime() -> QualifiedAVMediaAdapter | None:
    """The shared adapter, activating on first use. Blocking: worker threads only."""

    binding = _manager().ensure(deadline=time.monotonic() + IMPORT_ACTIVATION_SECONDS)
    return None if binding is None else binding.adapter


def request_media_runtime_activation() -> None:
    """Ask for a background activation without waiting; safe on the host event loop."""

    _manager().request_activation()


@contextmanager
def media_runtime_lease() -> Iterator[None]:
    """Hold media work against a binding transition; raises `MediaRuntimeBusy` during one."""

    # One lease is one media operation, and one operation verifies each executable once. The pin
    # scope is entered inside the lease and never around it: a transition is refused while a
    # lease is held, so no held executable can stand in the way of the runtime's replacement.
    with _manager().lease(), executable_pin_scope():
        yield


def authorized_authoring_derivative_runtime() -> AuthoringDerivativeGenerator | None:
    """One derivative generator per active binding; no locator discovery or activation wait."""
    from .authoring_derivative_generator import AuthoringDerivativeGenerator

    manager = _manager()
    if manager.current() is None:
        manager.request_activation()
        return None

    def build(binding: object) -> object:
        from .media_runtime_manager import MediaRuntimeBinding

        bound = cast(MediaRuntimeBinding, binding)
        return AuthoringDerivativeGenerator(
            ffmpeg_path=bound.ffmpeg_path,
            ffprobe_path=bound.ffprobe_path,
            scratch_root=bound.scratch_root / "authoring-derivatives",
        )

    return cast("AuthoringDerivativeGenerator | None", manager.scoped("derivative", build))


def _m26_store(root: Path) -> PrivateAVReconstructionStore:
    limits = qualified_av_limits()
    policy = AVStorePolicy(
        max_member_bytes=limits.max_aggregate_output_bytes,
        max_total_bytes=limits.store_quota_bytes,
        max_transactions=16,
        max_members_per_transaction=limits.max_segments + 1,
        max_recovery_entries=2_048,
        max_concurrent_writes=1,
        transaction_ttl_ms=86_400_000,
    )
    return PrivateAVReconstructionStore(
        root,
        policy=policy,
        clock_ms=lambda: int(time.time() * 1000),
    )


def authorized_m26_assembly_runtime(
    artifact_store: PrivateSegmentArtifactStore,
) -> M26ProductionAssemblyRuntime | None:
    """One M26 runtime on the active binding; the single-writer store is kept per scratch root."""

    if type(artifact_store) is not PrivateSegmentArtifactStore:
        raise TypeError("artifact_store authority is invalid")
    manager = _manager()
    binding = manager.current()
    if binding is None:
        manager.request_activation()
        return None
    root = binding.scratch_root / "m26-production-reconstruction"
    # IMPORTANT: the store is keyed by its root, not by the binding. It is a single writer per
    # root with durable receipts; a second instance for the same root after a binding change
    # would be refused by the store's coordination, and dropping it would strand open work.
    store = cast(
        PrivateAVReconstructionStore,
        manager.store(str(root), lambda: _m26_store(root)),
    )
    store_identity = canonical_fingerprint(
        {
            "schema": "h3.context.m26.private_store_binding.v1",
            "binding": [
                str(binding.ffmpeg_path),
                str(binding.ffprobe_path),
                str(binding.scratch_root),
            ],
        }
    )
    return M26ProductionAssemblyRuntime(
        artifact_store=artifact_store,
        reconstruction_store=store,
        media_bridge=binding.adapter,
        low_level_adapter=binding.adapter,
        capability=build_production_assembly_capability(store_identity_fingerprint=store_identity),
    )


__all__ = [
    "IMPORT_ACTIVATION_SECONDS",
    "authorized_authoring_derivative_runtime",
    "authorized_m26_assembly_runtime",
    "current_authorized_media_runtime",
    "media_runtime_lease",
    "request_media_runtime_activation",
]
