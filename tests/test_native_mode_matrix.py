"""M15-05 closed five-mode native qualification matrix tests."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from comfyui_h3_context.core import TaskMode
from comfyui_h3_context.core import native_mode_matrix as api

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "governance" / "contracts" / "native_mode_matrix_v1.schema.json"
FIXTURE = ROOT / "tests" / "fixtures" / "m15_05_native_mode_matrix.json"


def test_native_mode_matrix_contract_module_exists() -> None:
    spec = importlib.util.find_spec("comfyui_h3_context.core.native_mode_matrix")
    assert spec is not None, "M15-05 native mode matrix contract is not implemented"


def test_native_mode_matrix_api_is_explicit() -> None:
    required = {
        "NATIVE_MODE_MATRIX_SCHEMA",
        "NATIVE_MODE_MATRIX_VERSION",
        "NativeModeDisposition",
        "NativeModeMatrix",
        "NativeModeMatrixError",
        "NativeModeQualification",
        "NativeGraphEdge",
        "NativeGraphSourceAuthority",
        "NativeSurfaceArtifact",
        "NativeSurfaceQualification",
        "NativeSocketSpec",
        "assert_native_surface_equivalence",
        "build_default_native_mode_matrix",
        "decode_native_mode_matrix_json",
        "qualify_native_mode_surface",
        "validate_native_mode_matrix_wire",
    }
    assert not (required - set(dir(api))), f"missing API: {sorted(required - set(dir(api)))}"


def test_host_identity_is_typed_provenance_not_a_matrix_gate() -> None:
    matrix = replace(
        api.build_default_native_mode_matrix(),
        host_version="0.33.0",
        host_revision="1" * 40,
        native_source_blob="2" * 40,
    )
    jsonschema.validate(matrix.to_wire(), json.loads(SCHEMA.read_text(encoding="utf-8")))

    with pytest.raises(api.NativeModeMatrixError, match="host provenance"):
        replace(api.build_default_native_mode_matrix(), host_version=f"{'9' * 65}.0")


def test_native_mode_matrix_api_is_exported_from_its_leaf_module() -> None:
    for name in (
        "NativeModeMatrix",
        "NativeModeMatrixError",
        "NativeGraphEdge",
        "NativeGraphSourceAuthority",
        "NativeSurfaceArtifact",
        "NativeSurfaceQualification",
        "assert_native_surface_equivalence",
        "build_default_native_mode_matrix",
        "decode_native_mode_matrix_json",
        "qualify_native_mode_surface",
    ):
        assert name in api.__all__, f"leaf export {name} is missing"
        assert hasattr(api, name), f"leaf attribute {name} is missing"


def test_matrix_is_closed_over_exact_five_mode_order_and_claim_ceiling() -> None:
    matrix = api.build_default_native_mode_matrix()
    assert matrix.schema == "h3.native_mode_matrix.v1"
    assert matrix.version == "1.0.0"
    assert matrix.product_scope == "MANUAL_ONLY_SCOPED"
    assert matrix.assisted_profiles == (
        "ollama.local",
        "openai.remote",
        "gemini.remote",
        "anthropic.remote",
    )
    assert matrix.assisted_authoring.to_wire() == {
        "available": True,
        "selected": False,
        "ready": False,
        "authorized_for_this_action": False,
        "defaulted": False,
    }
    assert matrix.claim_scope == "structural_native_contract_only"
    assert matrix.model_executions == ()
    assert matrix.provider_executions == ()
    assert matrix.media_executions == ()
    assert tuple(entry.task_mode for entry in matrix.modes) == (
        TaskMode.T2VA,
        TaskMode.I2VA,
        TaskMode.FL2VA,
        TaskMode.L2VA,
        TaskMode.REF2VA,
    )
    assert all(
        entry.disposition is api.NativeModeDisposition.STRUCTURALLY_QUALIFIED_NATIVE
        for entry in matrix.modes
    )
    assert matrix.fingerprint == api.build_default_native_mode_matrix().fingerprint


def test_mode_entries_pin_profiles_roles_cardinality_and_native_paths() -> None:
    entries = {entry.task_mode: entry for entry in api.build_default_native_mode_matrix().modes}
    expected = {
        TaskMode.T2VA: (
            "h3_base",
            "MiniMaxH3ImageToVideo",
            (),
            0,
            0,
            (),
        ),
        TaskMode.I2VA: (
            "h3_base",
            "MiniMaxH3ImageToVideo",
            ("first_frame",),
            1,
            1,
            ("MiniMaxH3ImageToVideo.first_frame",),
        ),
        TaskMode.FL2VA: (
            "h3_base",
            "MiniMaxH3ImageToVideo",
            ("first_frame", "last_frame"),
            2,
            2,
            (
                "MiniMaxH3ImageToVideo.first_frame",
                "MiniMaxH3ImageToVideo.last_frame",
            ),
        ),
        TaskMode.L2VA: (
            "h3_base",
            "MiniMaxH3ImageToVideo",
            ("last_frame",),
            1,
            1,
            ("MiniMaxH3ImageToVideo.last_frame",),
        ),
        TaskMode.REF2VA: (
            "h3_full_reference",
            "MiniMaxH3ReferenceToVideo",
            ("reference", "audio_source"),
            1,
            12,
            (
                "MiniMaxH3ReferenceToVideo.ref_images.ref_image_{index}",
                "MiniMaxH3ReferenceToVideo.ref_videos.ref_video_{index}",
                "MiniMaxH3ReferenceToVideo.ref_video_audios.ref_video_audio_{video_index}",
                "MiniMaxH3ReferenceToVideo.ref_audios.ref_audio_{index}",
            ),
        ),
    }
    for mode, wanted in expected.items():
        entry = entries[mode]
        assert (
            entry.prompt_profile.value,
            entry.native_node_id,
            entry.asset_roles,
            entry.minimum_assets,
            entry.maximum_assets,
            entry.native_paths,
        ) == wanted
        assert entry.prompt_socket == "STRING"
        assert entry.readiness == "current_passed_report_and_trusted_wiring"
    assert entries[TaskMode.REF2VA].dynamic_reference_boundary == "parent_graph_visible"
    assert entries[TaskMode.T2VA].dynamic_reference_boundary == "not_applicable"


def test_exact_native_socket_types_defaults_enums_and_sigma_semantics_are_pinned() -> None:
    matrix = api.build_default_native_mode_matrix()
    image = matrix.modes[0]
    reference = matrix.modes[-1]
    image_required = {item.name: item for item in image.required_inputs}
    reference_required = {item.name: item for item in reference.required_inputs}
    assert {name: item.socket_type for name, item in image_required.items()} == {
        "clip": "CLIP",
        "vae": "VAE",
        "prompt": "STRING",
        "width": "INT",
        "height": "INT",
        "length": "INT",
    }
    assert reference_required["audio_vae"].socket_type == "VAE"
    assert reference_required["ref_image_size"].options == ("match", "max")
    assert reference_required["ref_image_size"].default == "match"
    for name, default, minimum, maximum, step in (
        ("width", 1344, 32, "MAX_RESOLUTION", 32),
        ("height", 768, 32, "MAX_RESOLUTION", 32),
        ("length", 124, 5, 3600, 17),
    ):
        spec = image_required[name]
        assert (spec.default, spec.minimum, spec.maximum, spec.step) == (
            default,
            minimum,
            maximum,
            step,
        )
    sigma = matrix.sigma_shift
    assert sigma.node_id == "MiniMaxH3SigmaShift"
    assert sigma.input_types == (
        ("model", "MODEL"),
        ("shift_video", "FLOAT"),
        ("shift_audio", "FLOAT"),
    )
    assert sigma.defaults == (("shift_video", 12.0), ("shift_audio", 3.0))
    assert sigma.bounds == (("minimum", 0.01), ("maximum", 100.0), ("step", 0.01))
    assert sigma.sampler == "ModelSamplingDiscreteFlow"
    assert sigma.scheduler == "video_discrete_flow_audio_internal_shift"


def test_surface_and_graph_parity_contract_is_closed_and_non_escalating() -> None:
    matrix = api.build_default_native_mode_matrix()
    assert matrix.surfaces == (
        "direct_node",
        "fixed_app_mode",
        "fixed_subgraph",
        "sidebar_normal_queue",
        "api_headless",
    )
    assert matrix.graph_binding_fields == (
        "asset_id",
        "kind",
        "presentation_label",
        "presentation_ordinal",
        "source_node",
        "source_output",
        "native_node",
        "native_child_path",
        "connection_order",
        "ownership",
    )
    assert matrix.presentation_label_basis == "one_based"
    assert matrix.autogrow_path_basis == "fully_qualified_zero_based"
    assert matrix.paired_audio_basis == "same_zero_based_video_suffix"
    assert matrix.host_contract_lane == "real_classes_real_schema_weight_callable_stub_only"
    assert matrix.stripped_native_qualifies is False
    assert matrix.model_free_qualifies is False


def test_matrix_rejects_semantic_contradictions_and_derived_types() -> None:
    matrix = api.build_default_native_mode_matrix()
    with __import__("pytest").raises(api.NativeModeMatrixError, match="product scope"):
        replace(matrix, product_scope="ASSISTED_PROFILE_QUALIFIED")
    with __import__("pytest").raises(api.NativeModeMatrixError, match="mode inventory"):
        replace(matrix, modes=matrix.modes[:-1])
    with __import__("pytest").raises(api.NativeModeMatrixError, match="assisted"):
        replace(matrix, assisted_profiles=("automatic_context_recovery",))

    class Spoof(api.NativeModeMatrix):
        pass

    with __import__("pytest").raises(api.NativeModeMatrixError, match="exact contract"):
        Spoof(**{field: getattr(matrix, field) for field in matrix.__dataclass_fields__})


def test_strict_decoder_rejects_duplicates_invalid_utf8_oversize_and_subclasses() -> None:
    matrix = api.build_default_native_mode_matrix()
    encoded = json.dumps(matrix.to_wire(), ensure_ascii=False).encode("utf-8")
    assert api.decode_native_mode_matrix_json(encoded) == matrix
    assert api.decode_native_mode_matrix_json(bytearray(encoded)) == matrix
    assert api.decode_native_mode_matrix_json(encoded.decode("utf-8")) == matrix
    hostile = '{"schema":"h3.native_mode_matrix.v1","schema":"h3.native_mode_matrix.v1"}'
    for payload in (hostile, b"\xff", "{", "NaN", b" " * 65_537, 123):
        with __import__("pytest").raises(api.NativeModeMatrixError):
            api.decode_native_mode_matrix_json(payload)  # type: ignore[arg-type]

    class SpoofStr(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            return encoded

    class SpoofBytes(bytes):
        pass

    class SpoofBytearray(bytearray):
        pass

    for subclass_payload in (
        SpoofStr("x" * 70_000),
        SpoofBytes(encoded),
        SpoofBytearray(encoded),
    ):
        with __import__("pytest").raises(api.NativeModeMatrixError, match="text or bytes"):
            api.decode_native_mode_matrix_json(subclass_payload)


def test_schema_fixture_and_fingerprint_match_canonical_contract() -> None:
    matrix = api.build_default_native_mode_matrix()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert schema["$id"] == "comfyui-h3-context://contracts/native_mode_matrix_v1.schema.json"
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(fixture["matrix"], schema)
    assert fixture["matrix"] == matrix.to_wire()
    assert fixture["fingerprint"] == matrix.fingerprint


@pytest.mark.parametrize("mutation", ("legacy", "extra", "reordered"))
def test_schema_and_factory_reject_noncanonical_assisted_profiles(mutation: str) -> None:
    matrix = api.build_default_native_mode_matrix()
    profiles = list(matrix.assisted_profiles)
    if mutation == "legacy":
        profiles[0] = "ollama.qwen3_8_27b.local"
    elif mutation == "extra":
        profiles.append("unreviewed.remote")
    else:
        profiles.reverse()
    wire = {**matrix.to_wire(), "assisted_profiles": profiles}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(wire, json.loads(SCHEMA.read_text(encoding="utf-8")))
    with pytest.raises(api.NativeModeMatrixError, match="assisted profiles"):
        replace(matrix, assisted_profiles=tuple(profiles))


def test_wire_contains_no_execution_or_sensitive_payload_authority() -> None:
    wire = api.build_default_native_mode_matrix().to_wire()
    serialized = json.dumps(wire, sort_keys=True).casefold()
    for forbidden in (
        "api_key",
        "authorization",
        "bearer ",
        "cookie",
        "password",
        "private_media",
        "signed_url",
        "token=",
        '"model_output_generated": true',
    ):
        assert forbidden not in serialized
