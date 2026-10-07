"""Read only the package's exact reviewed native-render qualification evidence.

This module does not qualify a renderer by inspecting its executable. The pinned evidence is
issued after actual corpus execution and independent observations; no client-supplied certificate
or runtime-generated self-assertion can replace it. Missing or changed evidence stays unavailable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Final

from ..core.authoring_render_jobs import RenderJobLimits
from ..core.canonical import canonical_fingerprint
from ..core.composition_contract import NLE_OPERATION_IDS
from ..core.render_planner import (
    RENDER_PRIMITIVE_IDS,
    RENDERER_QUALIFICATION_ROW_IDS,
    required_unqualified_renderer_profile,
)
from .authoring_fonts import load_packaged_font_manifest
from .authoring_render_service import RenderExecutionIdentity, RenderServiceError

# Exact executed corpus evidence; item acceptance still requires the native gate and review.
_ACCEPTED_EVIDENCE: Final[str | None] = (
    "sha256:ad225dec772b348e4a2235a9bcabcd54e7cc0f350161c785c4563f7f66f1ada5"
)
_PACKAGE = Path(__file__).resolve().parents[1]
_EVIDENCE = _PACKAGE / "contracts" / "authoring_renderer_qualification_v1.json"
_IMPLEMENTATION_PATHS: Final = (
    "core/canonical.py",
    "core/composition_contract.py",
    "core/timeline_authoring.py",
    # CRITICAL: V2 content extent becomes the real output duration here;
    # keep it in the native subject.
    "core/nle_authoring_contract.py",
    "core/safe_paths.py",
    "core/render_planner.py",
    "core/authoring_render_jobs.py",
    "core/authoring_render_receipts.py",
    # CRITICAL: each audio run's gain and fades enter the graph's FFmpeg program from here; a
    # change to this arithmetic must revoke the qualification as a change to the graph does.
    "core/clip_audio.py",
    "adapters/authoring_native_renderer.py",
    "adapters/authoring_render_executor.py",
    "adapters/authoring_render_graph.py",
    "adapters/authoring_render_process.py",
    "adapters/authoring_render_probe.py",
    "adapters/authoring_render_service.py",
    "adapters/authoring_render_store.py",
    "adapters/authoring_render_leases.py",
    "adapters/authoring_render_source.py",
    "adapters/authoring_source_binding.py",
    # CRITICAL: generated-source currentness and borrower lifetime affect actual render reads;
    # omitting this origin would keep qualification valid after its authority checks change.
    "adapters/authoring_generated_source.py",
    # CRITICAL: retained facts, currentness and borrower lifetime govern actual render reads;
    # leaving this authority unbound would admit changed sources under the old qualification.
    "adapters/retained_asset_use.py",
    "adapters/authoring_video_facts.py",
    "adapters/authoring_image_source.py",
    "adapters/authoring_fonts.py",
    "adapters/av_reconstruction_media.py",
    "adapters/segment_artifact_store.py",
)


def renderer_implementation_fingerprints() -> dict[str, str]:
    """Exact owned implementation subject, independent of checkout newline convention."""
    result: dict[str, str] = {}
    try:
        for relative in _IMPLEMENTATION_PATHS:
            with (_PACKAGE / relative).open("rb") as stream:
                body = stream.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise RenderServiceError("runtime_unavailable")
            module_id = relative.replace("/", "_").replace(".", "_")
            result[module_id] = "sha256:" + hashlib.sha256(body.replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        raise RenderServiceError("runtime_unavailable") from None
    return result


def load_renderer_qualification() -> RenderExecutionIdentity:
    """Return identity only for exact packaged evidence and the still-matching implementation."""
    accepted_evidence = _ACCEPTED_EVIDENCE
    if accepted_evidence is None:
        raise RenderServiceError("runtime_unavailable")
    try:
        with _EVIDENCE.open("rb") as stream:
            body = stream.read(65_537)
        if len(body) > 65_536:
            raise ValueError
        report = json.loads(body)
        # CRITICAL: check the reviewed evidence digest before trusting any report field.
        # A freshly re-signed, incomplete or forged report must not grant runtime capability.
        if canonical_fingerprint(report) != accepted_evidence:
            raise ValueError
        if (
            report["schema"] != "h3.authoring.native_renderer_qualification.v1"
            or report["implementation"] != renderer_implementation_fingerprints()
            or report["limits"] != asdict(RenderJobLimits())
            or report["profile"] != required_unqualified_renderer_profile().profile_fingerprint
            or report["font_package"] != load_packaged_font_manifest().package_fingerprint
            or set(report["commands"]) != set(NLE_OPERATION_IDS)
            or set(report["primitives"]) != set(RENDER_PRIMITIVE_IDS)
            or set(report["qualification_rows"]) != set(RENDERER_QUALIFICATION_ROW_IDS)
        ):
            raise ValueError
        return RenderExecutionIdentity(
            renderer_fingerprint=report["renderer"],
            probe_fingerprint=report["probe"],
            qualification_fingerprint=accepted_evidence,
            profile_fingerprint=report["profile"],
        )
    except (OSError, ValueError, TypeError, KeyError):
        raise RenderServiceError("runtime_unavailable") from None
