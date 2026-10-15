"""Complete pinned subjects are selected whole; synthetic envelopes grant no native evidence."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from comfyui_h3_context.adapters import authoring_renderer_qualification as qualification
from comfyui_h3_context.adapters.authoring_render_service import RenderServiceError
from comfyui_h3_context.core.canonical import canonical_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_public_subject_matches_current_public_redaction() -> None:
    from scripts.public_redaction import redact_blobs

    envelope = json.loads(qualification._EVIDENCE.read_bytes())
    paths = {
        "comfyui_h3_context/" + relative: relative
        for relative in qualification._IMPLEMENTATION_PATHS
    }
    public = redact_blobs({path: (ROOT / path).read_bytes() for path in paths})
    expected = {
        relative.replace("/", "_").replace(".", "_"): "sha256:"
        + hashlib.sha256(public[path].replace(b"\r\n", b"\n")).hexdigest()
        for path, relative in paths.items()
    }
    # CRITICAL: Source admission can stay valid while the redacted renderer is unavailable;
    # check the whole Public subject before shipping, including comment-only byte changes.
    assert envelope["subjects"]["public"]["implementation"] == expected


def _envelope() -> dict[str, Any]:
    source = json.loads(
        (
            ROOT / "comfyui_h3_context/contracts/authoring_renderer_qualification_v1.json"
        ).read_bytes()
    )
    public = copy.deepcopy(source)
    public["implementation"][next(iter(public["implementation"]))] = canonical_fingerprint(
        {"unit-only-public-input": 1}
    )
    return {
        "schema": "h3.authoring.native_renderer_qualification.v2",
        "subjects": {"source": source, "public": public},
    }


def _install(document: dict[str, Any], path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(qualification, "_EVIDENCE", path)
    monkeypatch.setattr(qualification, "_ACCEPTED_EVIDENCE", canonical_fingerprint(document))


@pytest.mark.parametrize("subject", ["source", "public"])
def test_complete_subject_identity_is_report_fingerprint_not_envelope_pin(
    subject: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = _envelope()
    _install(envelope, tmp_path / "unit-envelope.json", monkeypatch)
    report = envelope["subjects"][subject]
    monkeypatch.setattr(
        qualification, "renderer_implementation_fingerprints", lambda: report["implementation"]
    )
    identity = qualification.load_renderer_qualification()
    assert identity.qualification_fingerprint == canonical_fingerprint(report)
    assert identity.qualification_fingerprint != qualification._ACCEPTED_EVIDENCE
    assert identity.renderer_fingerprint == report["renderer"]
    assert identity.probe_fingerprint == report["probe"]
    other = "public" if subject == "source" else "source"
    assert identity.qualification_fingerprint != canonical_fingerprint(envelope["subjects"][other])


@pytest.mark.parametrize(
    "change",
    [
        "unknown_subject",
        "extra_envelope_field",
        "empty",
        "duplicate_subject",
        "missing_observations",
        "extra_report_field",
        "partial_map",
        "mixed_map",
        "other_limits",
        "other_fonts",
        "unknown_source",
    ],
)
def test_invalid_complete_subject_admission_is_closed(
    change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = _envelope()
    actual = copy.deepcopy(envelope["subjects"]["source"]["implementation"])
    if change == "unknown_subject":
        envelope["subjects"]["foreign"] = envelope["subjects"]["public"]
    elif change == "extra_envelope_field":
        envelope["note"] = "unit-only"
    elif change == "empty":
        envelope["subjects"] = {}
    elif change == "duplicate_subject":
        envelope["subjects"]["public"] = copy.deepcopy(envelope["subjects"]["source"])
    elif change == "missing_observations":
        envelope["subjects"]["public"].pop("title_rendering")
    elif change == "extra_report_field":
        envelope["subjects"]["public"]["note"] = "unit-only"
    elif change == "partial_map":
        envelope["subjects"]["public"]["implementation"].pop(next(iter(actual)))
    elif change == "mixed_map":
        for index, key in enumerate(actual):
            envelope["subjects"]["source" if index % 2 else "public"]["implementation"][key] = (
                canonical_fingerprint({"unit-drift": index})
            )
    elif change == "other_limits":
        envelope["subjects"]["public"]["limits"]["max_output_bytes"] += 1
    elif change == "other_fonts":
        envelope["subjects"]["public"]["font_package"] = canonical_fingerprint(
            {"unit-font-drift": 1}
        )
    elif change == "unknown_source":
        actual[next(iter(actual))] = canonical_fingerprint({"unit-unknown": 1})
    _install(envelope, tmp_path / "unit-envelope.json", monkeypatch)
    monkeypatch.setattr(qualification, "renderer_implementation_fingerprints", lambda: actual)
    with pytest.raises(RenderServiceError, match="runtime_unavailable"):
        qualification.load_renderer_qualification()


def test_duplicate_json_key_cannot_hide_in_reviewed_canonical_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _envelope()["subjects"]["source"]
    path = tmp_path / "duplicate-key.json"
    _install(source, path, monkeypatch)
    body = path.read_text(encoding="utf-8")
    path.write_text(
        '{"schema":"h3.authoring.native_renderer_qualification.v1",' + body[1:], encoding="utf-8"
    )
    monkeypatch.setattr(
        qualification, "renderer_implementation_fingerprints", lambda: source["implementation"]
    )
    with pytest.raises(RenderServiceError, match="runtime_unavailable"):
        qualification.load_renderer_qualification()


def test_missing_v2_does_not_fall_back_to_retained_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = _envelope()
    monkeypatch.setattr(qualification, "_EVIDENCE", tmp_path / "missing-v2.json")
    monkeypatch.setattr(qualification, "_ACCEPTED_EVIDENCE", canonical_fingerprint(envelope))
    with pytest.raises(RenderServiceError, match="runtime_unavailable"):
        qualification.load_renderer_qualification()


def test_source_receipt_currentness_cannot_be_relabelled_as_public_subject() -> None:
    from test_m25_authoring_render_jobs import _plan
    from test_m25_authoring_render_receipts import _facts

    from comfyui_h3_context.core.authoring_render_jobs import request_for_render_plan
    from comfyui_h3_context.core.authoring_render_receipts import (
        RenderReceiptError,
        make_render_receipt,
        verify_render_receipt,
    )

    envelope = _envelope()
    source = envelope["subjects"]["source"]
    public = envelope["subjects"]["public"]
    plan = _plan()
    arguments: dict[str, Any] = dict(
        request=request_for_render_plan(
            plan, idempotency_key="request-00000001", timeout_ms=30_000
        ),
        plan=plan,
        observed=_facts(plan),
        renderer_artifact_fingerprint=source["renderer"],
        probe_artifact_fingerprint=source["probe"],
        publication_currentness=plan.source_currentness_claim,
    )
    receipt = make_render_receipt(
        **arguments, qualification_fingerprint=canonical_fingerprint(source)
    )
    verify_render_receipt(
        receipt, **arguments, qualification_fingerprint=canonical_fingerprint(source)
    )
    with pytest.raises(RenderReceiptError, match="receipt_mismatch"):
        verify_render_receipt(
            receipt, **arguments, qualification_fingerprint=canonical_fingerprint(public)
        )
