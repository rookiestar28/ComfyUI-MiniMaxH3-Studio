"""Read only the package's exact reviewed native-render qualification evidence.

This module does not qualify a renderer by inspecting its executable. The pinned evidence is
issued after actual corpus execution and independent observations; no client-supplied certificate
or runtime-generated self-assertion can replace it. Missing or changed evidence stays unavailable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final

from ..core.authoring_render_jobs import RenderJobLimits
from ..core.canonical import canonical_fingerprint
from ..core.composition_contract import NLE_OPERATION_IDS
from ..core.render_planner import (
    RENDER_PRIMITIVE_IDS,
    RENDERER_QUALIFICATION_ROW_IDS,
    required_unqualified_renderer_profile,
)
from .authoring_fonts import AuthoringFontError, load_packaged_font_manifest
from .authoring_render_service import RenderExecutionIdentity, RenderServiceError

# Exact executed corpus evidence; item acceptance still requires the native gate and review.
_ACCEPTED_EVIDENCE: Final[str | None] = (
    "sha256:26ccf5750cba59f4de3397459af0544ef1b500dbedc519b30e0ef283be5111fc"
)
_PACKAGE = Path(__file__).resolve().parents[1]
_EVIDENCE = _PACKAGE / "contracts" / "authoring_renderer_qualification_v2.json"
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


_COMPLETE_REPORT_FIELDS: Final = frozenset(
    {
        "schema",
        "renderer",
        "probe",
        "profile",
        "font_package",
        "implementation",
        "limits",
        "commands",
        "primitives",
        "qualification_rows",
        "runtime",
        "runtime_boundary",
        "source_rows",
        "raster_rows",
        "native_service_rows",
        "title_rendering",
        "native_service_mode",
        "generated_source_rows",
        "tabbed_text_layout",
    }
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _report_identity(
    report: Any, implementation: dict[str, str], *, complete: bool
) -> RenderExecutionIdentity:
    if type(report) is not dict:
        raise ValueError
    if complete:
        if set(report) != _COMPLETE_REPORT_FIELDS:
            raise ValueError
        maps = {
            "runtime": 2,
            "runtime_boundary": 4,
            "source_rows": 11,
            "raster_rows": 49,
            "title_rendering": 7,
        }
        if any(
            type(report[key]) is not dict or len(report[key]) != size for key, size in maps.items()
        ):
            raise ValueError
        if (
            type(report["native_service_rows"]) is not list
            or len(report["native_service_rows"]) != 8
            or type(report["generated_source_rows"]) is not list
            or len(report["generated_source_rows"]) != 1
            or any(
                type(report[key]) is not str
                for key in ("native_service_mode", "tabbed_text_layout")
            )
            or type(report["implementation"]) is not dict
            or set(report["implementation"]) != set(implementation)
            or any(
                type(value) is not str or _DIGEST.fullmatch(value) is None
                for value in report["implementation"].values()
            )
            or any(
                type(report[key]) is not dict
                for key in ("commands", "primitives", "qualification_rows")
            )
        ):
            raise ValueError
    if (
        report["schema"] != "h3.authoring.native_renderer_qualification.v1"
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
        qualification_fingerprint=canonical_fingerprint(report),
        profile_fingerprint=report["profile"],
    )


def _select_identity(document: Any) -> RenderExecutionIdentity:
    implementation = renderer_implementation_fingerprints()
    if type(document) is not dict:
        raise ValueError
    if document.get("schema") == "h3.authoring.native_renderer_qualification.v1":
        if document["implementation"] != implementation:
            raise ValueError
        return _report_identity(document, implementation, complete=False)
    if (
        set(document) != {"schema", "subjects"}
        or document["schema"] != "h3.authoring.native_renderer_qualification.v2"
    ):
        raise ValueError
    subjects = document["subjects"]
    if (
        type(subjects) is not dict
        or not 1 <= len(subjects) <= 2
        or not set(subjects) <= {"source", "public"}
    ):
        raise ValueError
    matches: list[RenderExecutionIdentity] = []
    for report in subjects.values():
        identity = _report_identity(report, implementation, complete=True)
        if report["implementation"] == implementation:
            matches.append(identity)
    # CRITICAL: match one whole executed subject; unioning individual file hashes would qualify
    # a mixed implementation that neither native corpus ever measured. Ambiguity also refuses.
    if len(matches) != 1:
        raise ValueError
    return matches[0]


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
        report = json.loads(body, object_pairs_hook=_pairs)
        # CRITICAL: check the reviewed evidence digest before trusting any report field.
        # A freshly re-signed, incomplete or forged report must not grant runtime capability.
        if canonical_fingerprint(report) != accepted_evidence:
            raise ValueError
        return _select_identity(report)
    # CRITICAL: font authority refusals must revoke capability through this closed boundary;
    # allowing the typed runtime exception to escape exposes a different failure to callers.
    except (OSError, ValueError, TypeError, KeyError, RecursionError, AuthoringFontError):
        raise RenderServiceError("runtime_unavailable") from None
