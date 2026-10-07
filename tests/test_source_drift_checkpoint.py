"""M14-09 bounded source/model/host drift checkpoint tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from comfyui_h3_context.core import (
    CheckpointDisposition,
    DriftDisposition,
    SourceDriftCheckpointError,
    SourceSurfaceGroup,
    build_default_source_drift_checkpoint,
    decode_source_drift_checkpoint_json,
    validate_source_drift_checkpoint_wire,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "governance" / "contracts" / "source_drift_checkpoint_v2.schema.json"
FIXTURE = ROOT / "tests" / "fixtures" / "m22_10_source_drift_checkpoint.json"


def test_default_checkpoint_is_bounded_and_preserves_claim_ceiling() -> None:
    checkpoint = build_default_source_drift_checkpoint()

    assert checkpoint.schema == "h3.source_drift_checkpoint.v2"
    assert checkpoint.version == "2.0.0"
    assert checkpoint.disposition is CheckpointDisposition.PASS_WITH_BLOCKED_DRIFT
    assert checkpoint.product_scope == "MANUAL_ONLY_SCOPED"
    assert checkpoint.non_equivalence_preserved is True
    assert checkpoint.assisted_defaults_authorized is False
    assert checkpoint.assisted_profiles == (
        "ollama.local",
        "openai.remote",
        "gemini.remote",
        "anthropic.remote",
    )
    assert checkpoint.assisted_authoring.to_wire() == {
        "available": True,
        "selected": False,
        "ready": False,
        "authorized_for_this_action": False,
        "defaulted": False,
    }
    assert checkpoint.paid_calls == ()
    assert checkpoint.model_executions == ()
    assert checkpoint.media_executions == ()
    assert checkpoint.provider_executions == ()
    assert checkpoint.spend_microusd == 0
    assert len(checkpoint.surfaces) == 26
    assert {surface.group for surface in checkpoint.surfaces} == set(SourceSurfaceGroup)
    assert len({surface.surface_id for surface in checkpoint.surfaces}) == len(checkpoint.surfaces)


def test_exact_pins_and_material_latest_native_drift_are_separate() -> None:
    checkpoint = build_default_source_drift_checkpoint()
    by_id = {surface.surface_id: surface for surface in checkpoint.surfaces}

    supported = by_id["comfyui.supported.core"]
    assert supported.expected_identity == ("git:c44dea18809e3ca0e12e25cbe6938c2c45a29c9d")
    assert supported.observed_identity == supported.expected_identity
    assert supported.disposition is DriftDisposition.EXACT_MATCH

    native = by_id["comfyui.latest.native_h3"]
    assert native.expected_identity == "gitblob:22bc91cd4570a071c664ec4848a5be748909e1e7"
    assert native.observed_identity == "gitblob:0b1840e851c248f89e9920159c3c8237fa2e7186"
    assert native.material is True
    assert native.disposition is DriftDisposition.BLOCKED_PENDING_REQUALIFICATION
    assert "native_sigma_sampler" in native.affected_seams
    assert "M15-05" in native.requalification_owners
    assert checkpoint.latest_host_qualified is False

    frontend = by_id["comfyui.supported.frontend"]
    assert frontend.observed_identity == ("git:f339c6b2ec0cc90e1c4fa717d9f7d6d565a8f6da")
    assert frontend.disposition is DriftDisposition.EXACT_MATCH


def test_minimax_guides_model_license_and_ollama_are_revision_bound() -> None:
    by_id = {
        surface.surface_id: surface for surface in build_default_source_drift_checkpoint().surfaces
    }

    assert by_id["minimax.guide.base"].observed_identity == (
        "sha256:2cfebc096a6e08370f288d468d90b60f7f9bcb938f94bf090816e910e48e75fc"
    )
    assert by_id["minimax.guide.reference"].observed_identity == (
        "sha256:1e574f356716ad55612247ffb7bbccbcdb484ad96599d63c7dca1af186b1fab7"
    )
    assert by_id["minimax.model.revision"].observed_identity == (
        "git:6818f6c32d12b210915e44ad56a4228c2608f160"
    )
    assert by_id["minimax.model.license"].observed_identity == (
        "sha256:59b99642b95ea21630e311198ddbfffbfe05aadba0c2f5d884cbdf4efcc90f44"
    )
    assert by_id["ollama.python_client"].observed_identity == (
        "git:25b93290d8cd07b0d00732641f812ee34fd4c989"
    )
    assert by_id["ollama.latest.server"].disposition is (
        DriftDisposition.BLOCKED_PENDING_REQUALIFICATION
    )
    assert by_id["ollama.docs.tags"].observed_identity == (
        "sha256:70b873546ce4cb19113f8de8ce3cd8fb22e36d130c80d2a753254f2a45e1b20f"
    )
    assert by_id["ollama.docs.show"].observed_identity == (
        "sha256:75e200fa9ec92986579c8fcc7319c38fd85dc1310e853ee20eeba1d1fb02d2ad"
    )
    assert by_id["ollama.docs.ps"].observed_identity == (
        "sha256:c7d9eb1ee9ecae4a275aa5b6a5ff209e4a303b32b3fc994d60aec853ef5fd8ee"
    )


def test_checkpoint_wire_schema_fingerprint_and_decoder_are_closed() -> None:
    checkpoint = build_default_source_drift_checkpoint()
    wire = checkpoint.to_wire()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    jsonschema.validate(wire, schema)
    decoded = validate_source_drift_checkpoint_wire(wire)
    assert decoded.to_wire() == wire
    assert decoded.fingerprint == checkpoint.fingerprint
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == wire

    with pytest.raises(SourceDriftCheckpointError):
        validate_source_drift_checkpoint_wire({**wire, "unexpected": True})

    changed = json.loads(json.dumps(wire))
    changed["surfaces"][0]["observed_identity"] = "git:" + ("0" * 40)
    with pytest.raises(SourceDriftCheckpointError):
        validate_source_drift_checkpoint_wire(changed)


@pytest.mark.parametrize("mutation", ("legacy", "extra", "reordered"))
def test_schema_and_factory_reject_noncanonical_assisted_profiles(mutation: str) -> None:
    checkpoint = build_default_source_drift_checkpoint()
    profiles = list(checkpoint.assisted_profiles)
    if mutation == "legacy":
        profiles[0] = "ollama.qwen3_8_27b.local"
    elif mutation == "extra":
        profiles.append("unreviewed.remote")
    else:
        profiles.reverse()
    wire = {**checkpoint.to_wire(), "assisted_profiles": profiles}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(wire, json.loads(SCHEMA.read_text(encoding="utf-8")))
    with pytest.raises(SourceDriftCheckpointError, match="assisted_profiles"):
        replace(checkpoint, assisted_profiles=tuple(profiles))


def test_checkpoint_json_decoder_is_strict_utf8_duplicate_aware_and_bounded() -> None:
    checkpoint = build_default_source_drift_checkpoint()
    payload = json.dumps(checkpoint.to_wire(), ensure_ascii=False, separators=(",", ":"))

    for encoded in (payload, payload.encode(), bytearray(payload.encode())):
        assert decode_source_drift_checkpoint_json(encoded) == checkpoint

    root_duplicate = payload.replace(
        '{"schema":"h3.source_drift_checkpoint.v2",',
        '{"schema":"h3.source_drift_checkpoint.v2","schema":"h3.source_drift_checkpoint.v2",',
        1,
    )
    nested_duplicate = payload.replace(
        '"surface_id":"minimax.repository",',
        '"surface_id":"minimax.repository","surface_id":"minimax.repository",',
        1,
    )
    for hostile in (root_duplicate, nested_duplicate):
        with pytest.raises(SourceDriftCheckpointError, match="duplicate"):
            decode_source_drift_checkpoint_json(hostile)

    with pytest.raises(SourceDriftCheckpointError, match="UTF-8"):
        decode_source_drift_checkpoint_json(b"\xff")
    with pytest.raises(SourceDriftCheckpointError, match="malformed"):
        decode_source_drift_checkpoint_json("{")
    with pytest.raises(SourceDriftCheckpointError, match="invalid constant"):
        decode_source_drift_checkpoint_json("NaN")
    with pytest.raises(SourceDriftCheckpointError, match="byte limit"):
        decode_source_drift_checkpoint_json(b" " * 65_537)
    with pytest.raises(SourceDriftCheckpointError, match="text or bytes"):
        decode_source_drift_checkpoint_json(123)  # type: ignore[arg-type]


def test_checkpoint_json_decoder_rejects_builtin_type_subclasses() -> None:
    payload = json.dumps(build_default_source_drift_checkpoint().to_wire())

    class SpoofStr(str):
        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
            return b"{}"

    class SpoofBytes(bytes):
        pass

    class SpoofBytearray(bytearray):
        pass

    hostile_values = (
        SpoofStr(payload + (" " * 70_000)),
        SpoofBytes(payload.encode()),
        SpoofBytearray(payload.encode()),
    )
    for hostile in hostile_values:
        with pytest.raises(SourceDriftCheckpointError, match="text or bytes"):
            decode_source_drift_checkpoint_json(hostile)


def test_checkpoint_decoder_rejects_hostile_url_path_and_claim_mutations() -> None:
    wire = build_default_source_drift_checkpoint().to_wire()
    mutations = []
    for unsafe_url in ("file:///tmp/source", "https://docs.ollama.com/.planning/secret"):
        changed = json.loads(json.dumps(wire))
        changed["surfaces"][0]["official_url"] = unsafe_url
        mutations.append(changed)
    escalated = json.loads(json.dumps(wire))
    escalated["assisted_defaults_authorized"] = True
    mutations.append(escalated)

    for changed in mutations:
        with pytest.raises(SourceDriftCheckpointError):
            decode_source_drift_checkpoint_json(json.dumps(changed))


def test_checkpoint_rejects_claim_or_execution_escalation() -> None:
    checkpoint = build_default_source_drift_checkpoint()

    with pytest.raises(SourceDriftCheckpointError):
        replace(checkpoint, assisted_defaults_authorized=True)
    with pytest.raises(SourceDriftCheckpointError):
        replace(checkpoint, non_equivalence_preserved=False)
    with pytest.raises(SourceDriftCheckpointError):
        replace(checkpoint, paid_calls=("call_1",))
    with pytest.raises(SourceDriftCheckpointError):
        replace(checkpoint, spend_microusd=1)


def test_only_reviewed_official_public_urls_enter_portable_wire() -> None:
    checkpoint = build_default_source_drift_checkpoint()

    for surface in checkpoint.surfaces:
        assert surface.official_url.startswith("https://")
        assert any(
            surface.official_url.startswith(prefix)
            for prefix in (
                "https://github.com/MiniMax-AI/",
                "https://huggingface.co/MiniMaxAI/",
                "https://github.com/Comfy-Org/",
                "https://docs.comfy.org/",
                "https://github.com/ollama/",
                "https://docs.ollama.com/",
            )
        )
    encoded = json.dumps(checkpoint.to_wire(), sort_keys=True)
    for forbidden in (".planning", "/reference/", ".tmp/", "token=", "password="):
        assert forbidden not in encoded
