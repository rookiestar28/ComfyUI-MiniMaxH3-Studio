"""Explicit, bounded qualification of the retained canonical Production plan."""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..core.canonical import canonical_fingerprint
from ..core.contracts import TaskMode
from ..core.generation_profile import FamilyDisposition, materialization_bases
from ..core.guide_conformance import evaluate_guide_conformance
from ..core.managed_sequence import ManagedModeQualificationV1, ManagedModeQualificationV2
from ..core.native_composition import NativeCompositionDisposition
from ..core.native_mode_matrix import build_default_native_mode_matrix
from ..core.native_source_compatibility import NativeSourceObservation
from ..core.production_import import ProductionAutomaticPlanAuthorityV2
from . import comfyui_generation_profile as profile_adapter
from .comfyui_generation_profile import build_generation_profile
from .comfyui_production_workspace import ProductionWorkbenchError, ProductionWorkspaceRegistry
from .managed_asset_resolution import (
    build_managed_asset_resolution,
    build_managed_native_composition,
)
from .production_context_materializer import claim_canonical_production_materialization

QUALIFICATION_TTL_SECONDS = 60
#: Issued qualifications whose loaded-source observation is retained for later currentness checks.
_MAX_ISSUED_SOURCES = 8
_LOGGER = logging.getLogger(__name__)
_NOT_ISSUED = object()
_COMPILER_SOURCES = (
    "core/audit_override.py",
    "core/canonical.py",
    "core/canonical_context_pipeline.py",
    "core/constraints.py",
    "core/context_reporting.py",
    "core/contracts.py",
    "core/guide_conformance.py",
    "core/intent_graph.py",
    "core/linting.py",
    "core/managed_sequence.py",
    "core/native_composition.py",
    "core/native_asset_resolution.py",
    "core/native_source_compatibility.py",
    "core/native_t2va_structure.py",
    "core/native_t2va_baseline.py",
    "core/native_h3.py",
    "core/native_mode_matrix.py",
    "core/normalization.py",
    "core/generation_profile.py",
    "core/production_duration.py",
    "core/production_import.py",
    "core/production_semantics.py",
    "core/production_storyboard.py",
    "core/profiles.py",
    "core/prompt_fidelity.py",
    "core/registry.py",
    "core/rendering.py",
    "core/validation_lifecycle.py",
    "adapters/comfyui_generation_profile.py",
    "adapters/production_context_materializer.py",
    "adapters/managed_mode_qualification.py",
    "adapters/managed_asset_resolution.py",
)


def compiler_identity() -> str:
    """Fingerprint bounded installed implementation bytes, never browser-supplied claims."""
    root = Path(__file__).resolve().parent.parent
    digests = []
    try:
        for relative in _COMPILER_SOURCES:
            with (root / relative).open("rb") as stream:
                content = stream.read(262_145)
            if len(content) > 262_144:
                raise ValueError("compiler source capacity")
            digests.append([relative, hashlib.sha256(content).hexdigest()])
    except (OSError, ValueError):
        raise ProductionWorkbenchError("qualification_compiler_unavailable", 409) from None
    return canonical_fingerprint(digests)


class ManagedQualificationProducer:
    """One preparation at a time; each ephemeral Context is released before its successor."""

    def __init__(
        self,
        production_registry: ProductionWorkspaceRegistry,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._production = production_registry
        self._clock = clock
        self._lock = threading.Lock()
        self._issued_lock = threading.Lock()
        # Fingerprint -> (expiry, loaded-source observation at issuance). In-process only.
        self._issued: dict[str, tuple[float, NativeSourceObservation | None]] = {}

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ProductionWorkbenchError("qualification_clock", 503)
        return float(value)

    def _remember(
        self, qualification: ManagedModeQualificationV2, source: NativeSourceObservation | None
    ) -> None:
        now = self._now()
        with self._issued_lock:
            live = {
                key: value
                for key, value in self._issued.items()
                if value[0] > now and key != qualification.fingerprint
            }
            # Oldest entries go first; an evicted qualification simply fails closed later.
            while len(live) >= _MAX_ISSUED_SOURCES:
                live.pop(next(iter(live)))
            live[qualification.fingerprint] = (qualification.expires_at, source)
            self._issued = live

    def _issued_source(self, qualification: ManagedModeQualificationV2) -> object:
        """The source observed at issuance, or a sentinel equal to no observation."""
        with self._issued_lock:
            issued = self._issued.get(qualification.fingerprint)
        return _NOT_ISSUED if issued is None else issued[1]

    @contextmanager
    def guard_current(
        self,
        qualification: ManagedModeQualificationV2,
        *,
        workspace_handle: str,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        expected_plan_fingerprint: str,
    ) -> Iterator[None]:
        if (
            build_generation_profile().fingerprint() != qualification.host_profile_fingerprint
            or compiler_identity() != qualification.baseline.compiler_fingerprint
            or build_managed_asset_resolution().inventory_fingerprint
            != qualification.asset_resolution_fingerprint
            # CRITICAL: a FRESH observation must equal the one recorded at issuance. Never
            # refresh authority from newer bytes; a benign file edit still invalidates it.
            or self._issued_source(qualification)
            != profile_adapter._observe_native_composition().source
            or self._now() >= qualification.expires_at
        ):
            raise ProductionWorkbenchError("qualification_changed", 409)
        with self._production.guard_automatic_plan_authority(
            workspace_handle,
            expected_workspace_revision=expected_workspace_revision,
            expected_workspace_fingerprint=expected_workspace_fingerprint,
            expected_plan_fingerprint=expected_plan_fingerprint,
        ):
            yield

    def observe(
        self,
        *,
        workspace_handle: str,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        expected_plan_fingerprint: str,
        previous: ManagedModeQualificationV2 | None = None,
    ) -> ManagedModeQualificationV2:
        if not self._lock.acquire(blocking=False):
            raise ProductionWorkbenchError("qualification_busy", 423)
        try:
            now = self._now()
            if previous is not None and now >= previous.expires_at:
                raise ProductionWorkbenchError("qualification_expired", 409)
            authority = self._production.claim_automatic_plan_authority(
                workspace_handle,
                expected_workspace_revision=expected_workspace_revision,
                expected_workspace_fingerprint=expected_workspace_fingerprint,
                expected_plan_fingerprint=expected_plan_fingerprint,
            )
            if type(authority) is not ProductionAutomaticPlanAuthorityV2:
                raise ProductionWorkbenchError("qualification_plan_unqualified", 409)
            profile = build_generation_profile()
            assets = build_managed_asset_resolution()
            # IMPORTANT: the loaded native source is observed once as the original fact; every
            # segment's fresh observation and the final recheck must equal it.
            source = profile_adapter._observe_native_composition().source
            if previous is not None and self._issued_source(previous) != source:
                raise ProductionWorkbenchError("qualification_changed", 409)
            admission: str | None = None
            modes = []
            for template, _family, names in materialization_bases():
                basis = profile.basis(template)
                if basis.disposition not in (
                    FamilyDisposition.AVAILABLE,
                    FamilyDisposition.ASSET_RELOCATED,
                    FamilyDisposition.MISSING_ASSET,
                ):
                    raise ProductionWorkbenchError("qualification_host_unqualified", 409)
                for name in names:
                    if profile.for_task_mode(name) is not basis:
                        raise ProductionWorkbenchError("qualification_modes_incomplete", 409)
                    modes.append(TaskMode(name))
            compiler = compiler_identity()
            matrix = build_default_native_mode_matrix()
            if set(modes) != {row.task_mode for row in matrix.modes}:
                raise ProductionWorkbenchError("qualification_modes_incomplete", 409)
            compositions = []
            guide_readiness = []
            receipts = []
            for segment in authority.proposal.segments:
                claim, receipt = self._production.materialize_automatic_plan_segment(
                    authority, segment.segment_id
                )
                try:
                    if receipt.capability_fingerprint != matrix.fingerprint:
                        raise ProductionWorkbenchError("qualification_capability_changed", 409)
                    snapshot = claim_canonical_production_materialization(claim)
                    composition = build_managed_native_composition(
                        snapshot.report, snapshot.wiring, assets
                    )
                    composition.assert_current(snapshot.report, snapshot.wiring)
                    if composition.disposition is not NativeCompositionDisposition.QUALIFIED:
                        # Closed identifiers only: no source text, path or exception detail.
                        _LOGGER.info(
                            "H3 Context managed qualification held: composition=%s detail=%s",
                            composition.reason,
                            composition.detail,
                        )
                        raise ProductionWorkbenchError("qualification_composition_unqualified", 409)
                    if composition.observed_source() != source:
                        raise ProductionWorkbenchError("qualification_changed", 409)
                    admission = composition.detail
                    # IMPORTANT: semantic asset IDs do not name owned ComfyUI loader nodes.
                    # Match the shipped prepared-child resolver; otherwise readiness authorizes
                    # a parent whose first child is certain to fail before it can bind a graph.
                    if (
                        snapshot.report.request.task_mode is not TaskMode.T2VA
                        or snapshot.wiring.bindings
                        or snapshot.canonical_lowering is None
                    ):
                        raise ProductionWorkbenchError(
                            "qualification_host_bindings_unavailable", 409
                        )
                    guide = evaluate_guide_conformance(
                        snapshot.report.plan, snapshot.report.prompt_document
                    )
                    # IMPORTANT: native execution admission does not grant official-guide READY.
                    # Preserve the independent verdict explicitly instead of upgrading a draft.
                    guide_readiness.append(guide.readiness)
                    snapshot.assert_current()
                    compositions.append(
                        canonical_fingerprint(
                            {"composition": composition.to_wire(), "guide": guide.to_wire()}
                        )
                    )
                    receipts.append(receipt.fingerprint)
                finally:
                    # CRITICAL: the accepted materializer owns one scratch lease. Holding it
                    # across segments prevents the next segment and can exhaust Sidebar capacity.
                    claim.release()
            current = self._production.claim_automatic_plan_authority(
                workspace_handle,
                expected_workspace_revision=expected_workspace_revision,
                expected_workspace_fingerprint=expected_workspace_fingerprint,
                expected_plan_fingerprint=expected_plan_fingerprint,
            )
            if (
                current is not authority
                or build_generation_profile() != profile
                or compiler_identity() != compiler
                or profile_adapter._observe_native_composition().source != source
            ):
                raise ProductionWorkbenchError("qualification_changed", 409)
            assets.assert_current()
            result = ManagedModeQualificationV2(
                ManagedModeQualificationV1(
                    matrix.fingerprint,
                    compiler,
                    tuple(modes),
                    tuple(receipts),
                    now if previous is None else previous.observed_at,
                    now + QUALIFICATION_TTL_SECONDS if previous is None else previous.expires_at,
                ),
                authority.fingerprint,
                tuple(compositions),
                tuple(guide_readiness),
                profile.fingerprint(),
                assets.inventory_fingerprint,
            )
            if self._now() >= result.expires_at:
                raise ProductionWorkbenchError("qualification_expired", 409)
            if previous is not None and result != previous:
                raise ProductionWorkbenchError("qualification_changed", 409)
            if previous is None:
                self._remember(result, source)
                _LOGGER.info(
                    "H3 Context managed qualification issued: segments=%d source=%s detail=%s",
                    len(compositions),
                    "none" if source is None else source.source_blob,
                    admission,
                )
            return result
        except ProductionWorkbenchError:
            raise
        except Exception:
            raise ProductionWorkbenchError("qualification_observation_failed", 409) from None
        finally:
            self._lock.release()
