"""M10-03 native ComfyUI and explicit Ollama model-adapter tests."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from comfyui_h3_context.adapters.comfyui_native import ComfyUINativeCLIPAdapter
from comfyui_h3_context.adapters.ollama_native import (
    LoopbackOllamaTransport,
    OllamaNativeAdapter,
    preflight_ollama,
    run_ollama_provider_preflight,
)
from comfyui_h3_context.core import (
    EvidenceLevel,
    LocalAdapterExecutionRequest,
    LocalCancellationProbe,
    LocalDeviceKind,
    LocalDeviceSpec,
    LocalResourceBudget,
    MediaKind,
    ModelBackendFamily,
    ModelCapability,
    ModelCapabilityState,
    ModelGenerationRequest,
    ModelGenerationResult,
    ModelManifest,
    ModelProbeReport,
    ModelProbeStatus,
    ModelRuntimeProfile,
    OllamaModelObservation,
    OllamaProcessObservation,
    OllamaServerObservation,
    TaskMode,
    build_model_result,
    qualify_ollama_manifest,
    run_local_adapter,
)
from comfyui_h3_context.core.contracts import ProviderIdentity
from comfyui_h3_context.core.errors import (
    LocalAdapterCancelledError,
    LocalAdapterCapabilityError,
    LocalAdapterTimeoutError,
    ModelManifestError,
    ModelOutputError,
    ModelTransportError,
)
from comfyui_h3_context.core.provider_policy import ProviderExecutionPolicy, ProviderPrivacyMode
from comfyui_h3_context.core.provider_setup import (
    ProviderConsentAuthority,
    ProviderLocalBackend,
    ProviderPreflightReceipt,
    ProviderPreflightStatus,
    ProviderSetup,
    build_ollama_provider_preflight,
    build_provider_consent_authority,
    build_provider_setup,
)

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(letter: str) -> str:
    return "sha256:" + letter * 64


def runtime() -> ModelRuntimeProfile:
    return ModelRuntimeProfile(
        device=LocalDeviceSpec(LocalDeviceKind.CPU),
        dtype="float16",
        offload="host_managed",
        max_context_tokens=8192,
        max_output_tokens=512,
        max_media_items=8,
        limits=LocalResourceBudget(
            max_memory_bytes=2_000_000,
            max_wall_time_seconds=10.0,
            max_references=8,
            max_output_bytes=32_000,
            max_output_items=2,
            max_concurrency=1,
        ),
    )


def native_manifest(*, approved: bool = True) -> ModelManifest:
    return ModelManifest(
        manifest_id="native.qwen3_vl_8b",
        backend_family=ModelBackendFamily.COMFYUI_NATIVE,
        adapter_id="comfyui_native",
        adapter_version="1.0.0",
        model_id="qwen3-vl-8b",
        model_digest=fingerprint("a"),
        checkpoint_fingerprint=fingerprint("b"),
        detected_family="qwen3_vl_8b",
        clip_type="qwen_vl",
        tokenizer_processor="qwen3_vl",
        generation_weights_complete=True,
        capabilities=frozenset(
            {
                ModelCapability.TEXT_GENERATION,
                ModelCapability.VISION,
                ModelCapability.STRUCTURED_OUTPUT,
            }
        ),
        structured_output_schema="h3.model.typed_output.v1",
        parser_path="json_object_v1",
        runtime=runtime(),
        cancellation=ModelCapabilityState.UNQUALIFIED,
        license="apache-2.0",
        host_profile="comfyui.c44dea",
        evidence_level=EvidenceLevel.EXPERIMENTAL,
        approved=approved,
        notes="fixture-qualified only",
    )


def ollama_manifest(*, approved: bool = True) -> ModelManifest:
    return ModelManifest(
        manifest_id="ollama.qwen3_vl_8b",
        backend_family=ModelBackendFamily.OLLAMA,
        adapter_id="ollama",
        adapter_version="1.0.0",
        model_id="qwen3-vl:8b",
        model_digest=fingerprint("d"),
        checkpoint_fingerprint=fingerprint("e"),
        detected_family="qwen3_vl",
        clip_type=None,
        tokenizer_processor="ollama_native",
        generation_weights_complete=True,
        capabilities=frozenset(
            {
                ModelCapability.TEXT_GENERATION,
                ModelCapability.VISION,
                ModelCapability.STRUCTURED_OUTPUT,
            }
        ),
        structured_output_schema="h3.model.typed_output.v1",
        parser_path="json_object_v1",
        runtime=runtime(),
        cancellation=ModelCapabilityState.UNQUALIFIED,
        license="apache-2.0",
        host_profile="ollama.loopback",
        evidence_level=EvidenceLevel.EXPERIMENTAL,
        approved=approved,
        notes="fixture-qualified only",
        server_version="0.9.0",
    )


def generation_request() -> ModelGenerationRequest:
    return ModelGenerationRequest(
        prompt="Return a JSON object describing the visible subject.",
        max_tokens=64,
        do_sample=False,
        media_fingerprints=(fingerprint("f"),),
        image=object(),
    )


def ollama_authorities(
    report: ModelProbeReport, *, revision: int = 1
) -> tuple[ProviderSetup, ProviderConsentAuthority, ProviderPreflightReceipt]:
    setup = build_provider_setup(
        ProviderExecutionPolicy(
            provider=ProviderIdentity.LOCAL,
            privacy_mode=ProviderPrivacyMode.LOCAL_ONLY,
            offline=False,
            network_allowed=True,
            upload_consent=False,
        ),
        local_backend=ProviderLocalBackend.OLLAMA,
        revision=revision,
    )
    preflight = build_ollama_provider_preflight(
        setup,
        report,
        endpoint_fingerprint=fingerprint("e"),
        expected_manifest_id="ollama.qwen3_vl_8b",
        expected_model_digest=fingerprint("d"),
        expected_server_version="0.9.0",
    )
    return setup, build_provider_consent_authority(setup), preflight


def execution_request(value: ModelGenerationRequest) -> LocalAdapterExecutionRequest:
    return LocalAdapterExecutionRequest(
        adapter_id="comfyui_native" if value.image is not None else "ollama",
        task_mode=TaskMode.T2VA,
        media_kinds=(MediaKind.IMAGE,),
        device=LocalDeviceSpec(LocalDeviceKind.CPU),
        deterministic_required=True,
        input_value=value,
    )


class FakeClip:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def tokenize(self, prompt: str, **kwargs: object) -> object:
        self.calls.append("tokenize")
        self.prompt = prompt
        self.tokenize_kwargs = kwargs
        return {"tokens": [1, 2]}

    def generate(self, tokens: object, **kwargs: object) -> object:
        self.calls.append("generate")
        self.generate_tokens = tokens
        self.generate_kwargs = kwargs
        return [3, 4]

    def decode(self, generated_ids: object) -> str:
        self.calls.append("decode")
        self.decode_ids = generated_ids
        return '{"objects":[{"label":"person"}]}'


class FakeOllamaTransport:
    def __init__(self, *, digest: str | None = None, done: bool = True) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.digest = fingerprint("d") if digest is None else digest
        self.done = done

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        cancellation: LocalCancellationProbe | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        del cancellation, timeout_seconds
        self.calls.append((method, path, payload))
        if path == "/api/version":
            return {"version": "0.9.0", "cloud": False}
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": "qwen3-vl:8b",
                        "digest": self.digest,
                        "size": 1024,
                        "details": {"family": "qwen3"},
                    }
                ]
            }
        if path == "/api/show":
            return {
                "details": {"family": "qwen3", "quantization_level": "Q4_K_M"},
                "capabilities": ["completion", "vision"],
                "model_info": {"context_length": 8192},
            }
        if path == "/api/ps":
            return {"models": []}
        if path == "/api/chat":
            return {
                "done": self.done,
                "done_reason": "stop" if self.done else "length",
                "message": {"content": '{"objects":[]}'},
            }
        raise AssertionError(path)


class ModelManifestTests(unittest.TestCase):
    def test_manifest_is_bounded_and_schema_projection_is_locator_free(self) -> None:
        manifest = native_manifest()
        public = manifest.to_public_dict()
        self.assertEqual(public["backend_family"], "comfyui_native")
        self.assertNotIn("/", json.dumps(public))
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "model_manifest_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/model_manifest_v1.schema.json"
        )
        self.assertEqual(set(schema["required"]), set(public))

    def test_conditioning_only_h3_checkpoint_is_rejected_for_generation(self) -> None:
        with self.assertRaises(ModelManifestError):
            replace(
                native_manifest(),
                detected_family="minimax_h3_qwen3vl_32b_conditioning",
            )

    def test_incomplete_generation_weights_are_not_capability(self) -> None:
        with self.assertRaises(ModelManifestError):
            replace(native_manifest(), generation_weights_complete=False)

    def test_json_parser_rejects_partial_or_scalar_model_output(self) -> None:
        with self.assertRaises(ModelOutputError):
            build_model_result(native_manifest(), generation_request(), "{partial")
        with self.assertRaises(ModelOutputError):
            build_model_result(native_manifest(), generation_request(), "[]")
        with self.assertRaises(ModelOutputError):
            build_model_result(native_manifest(), generation_request(), '{"x":1,"x":2}')

    def test_request_budget_is_bound_before_model_execution(self) -> None:
        with self.assertRaises(ModelOutputError):
            build_model_result(
                native_manifest(),
                replace(generation_request(), max_tokens=runtime().max_output_tokens + 1),
                '{"objects":[]}',
            )


class NativeAdapterTests(unittest.TestCase):
    def test_native_adapter_uses_host_owned_clip_call_chain(self) -> None:
        clip = FakeClip()
        adapter = ComfyUINativeCLIPAdapter(clip, native_manifest())
        value = generation_request()
        result = run_local_adapter(adapter, execution_request(value))
        typed = cast(ModelGenerationResult, result.value)
        self.assertEqual(clip.calls, ["tokenize", "generate", "decode"])
        self.assertEqual(typed.parsed_output["objects"], [{"label": "person"}])
        self.assertNotIn(value.prompt, json.dumps(typed.to_public_dict()))
        self.assertEqual(result.output_items, 1)

    def test_native_adapter_does_not_accept_unapproved_manifest(self) -> None:
        with self.assertRaises(LocalAdapterCapabilityError):
            ComfyUINativeCLIPAdapter(FakeClip(), native_manifest(approved=False))


class OllamaAdapterTests(unittest.TestCase):
    def test_manifest_rejects_all_documented_cloud_model_name_forms(self) -> None:
        for model_id in ("qwen3-vl:8b:cloud", "qwen3-vl:8b-cloud", "gpt-oss:120b-cloud"):
            with self.subTest(model_id=model_id), self.assertRaises(ModelManifestError):
                replace(ollama_manifest(), model_id=model_id)

    def test_preflight_binds_version_tags_show_and_ps_to_digest(self) -> None:
        transport = FakeOllamaTransport()
        manifest = ollama_manifest()
        report = preflight_ollama(transport, manifest)
        self.assertEqual(report.status, ModelProbeStatus.SUPPORTED)
        self.assertEqual(
            [(method, path) for method, path, _ in transport.calls],
            [
                ("GET", "/api/version"),
                ("GET", "/api/tags"),
                ("POST", "/api/show"),
                ("GET", "/api/ps"),
            ],
        )
        self.assertIsNotNone(report.observed_model)
        assert report.observed_model is not None
        self.assertEqual(report.observed_model.digest, manifest.model_digest)

    def test_preflight_digest_mismatch_is_unsupported_without_fallback(self) -> None:
        transport = FakeOllamaTransport(digest=fingerprint("c"))
        report = preflight_ollama(transport, ollama_manifest())
        self.assertEqual(report.status, ModelProbeStatus.UNSUPPORTED)
        self.assertIn("model_digest_mismatch", report.diagnostics)

    def test_ollama_adapter_requires_preflight_and_emits_typed_result(self) -> None:
        transport = FakeOllamaTransport()
        manifest = ollama_manifest()
        report = preflight_ollama(transport, manifest)
        setup, consent, operational = ollama_authorities(report)
        adapter = OllamaNativeAdapter(
            transport,
            manifest,
            report,
            setup=setup,
            consent=consent,
            provider_preflight=operational,
        )
        value = generation_request()
        value = replace(value, image=None, image_payloads=(b"image-bytes",))
        request = execution_request(value)
        request = replace(request, adapter_id="ollama")
        result = run_local_adapter(adapter, request)
        typed = cast(ModelGenerationResult, result.value)
        self.assertEqual(typed.backend_family, ModelBackendFamily.OLLAMA)
        chat = next(payload for _, path, payload in transport.calls if path == "/api/chat")
        assert isinstance(chat, dict)
        self.assertFalse(chat["stream"])
        self.assertEqual(chat["keep_alive"], "0")

    def test_ollama_adapter_rejects_supported_preflight_for_another_manifest(self) -> None:
        transport = FakeOllamaTransport()
        manifest = ollama_manifest()
        report = replace(preflight_ollama(transport, manifest), manifest_id="ollama.other")
        setup, consent, operational = ollama_authorities(preflight_ollama(transport, manifest))
        with self.assertRaises(LocalAdapterCapabilityError):
            OllamaNativeAdapter(
                transport,
                manifest,
                report,
                setup=setup,
                consent=consent,
                provider_preflight=operational,
            )

    def test_ollama_partial_result_is_terminal_failure(self) -> None:
        transport = FakeOllamaTransport(done=False)
        manifest = ollama_manifest()
        report = preflight_ollama(transport, manifest)
        setup, consent, operational = ollama_authorities(report)
        adapter = OllamaNativeAdapter(
            transport,
            manifest,
            report,
            setup=setup,
            consent=consent,
            provider_preflight=operational,
        )
        value = ModelGenerationRequest(prompt="Return JSON")
        request = LocalAdapterExecutionRequest(
            adapter_id="ollama",
            task_mode=TaskMode.T2VA,
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
            input_value=value,
        )
        with self.assertRaises(ModelOutputError):
            run_local_adapter(adapter, request)

    def test_ollama_adapter_rejects_stale_operational_authority(self) -> None:
        transport = FakeOllamaTransport()
        manifest = ollama_manifest()
        report = preflight_ollama(transport, manifest)
        setup, consent, operational = ollama_authorities(report, revision=1)
        stale_setup, _, _ = ollama_authorities(report, revision=2)
        with self.assertRaises(LocalAdapterCapabilityError):
            OllamaNativeAdapter(
                transport,
                manifest,
                report,
                setup=stale_setup,
                consent=consent,
                provider_preflight=operational,
            )

    def test_ollama_provider_preflight_maps_cancel_timeout_and_transport_without_execution(
        self,
    ) -> None:
        setup, _, _ = ollama_authorities(preflight_ollama(FakeOllamaTransport(), ollama_manifest()))

        class Cancelled:
            def is_cancelled(self) -> bool:
                return True

        cancelled = run_ollama_provider_preflight(
            setup,
            FakeOllamaTransport(),
            ollama_manifest(),
            endpoint_fingerprint=fingerprint("e"),
            cancellation=Cancelled(),
        )
        self.assertIs(cancelled.status, ProviderPreflightStatus.CANCELLED)
        self.assertFalse(cancelled.network_attempted)

        class FailingTransport:
            def __init__(self, error: Exception) -> None:
                self.error = error

            def request(
                self,
                method: str,
                path: str,
                payload: Mapping[str, object] | None = None,
                *,
                cancellation: LocalCancellationProbe | None = None,
                timeout_seconds: float | None = None,
            ) -> Mapping[str, object]:
                del method, path, payload, cancellation, timeout_seconds
                raise self.error

        for error, status in (
            (LocalAdapterTimeoutError("private timeout"), ProviderPreflightStatus.TIMEOUT),
            (ModelTransportError("private endpoint"), ProviderPreflightStatus.TRANSPORT),
        ):
            with self.subTest(status=status):
                receipt = run_ollama_provider_preflight(
                    setup,
                    FailingTransport(error),
                    ollama_manifest(),
                    endpoint_fingerprint=fingerprint("e"),
                )
                self.assertIs(receipt.status, status)
                self.assertTrue(receipt.network_attempted)
                self.assertFalse(receipt.media_transferred)
                self.assertFalse(receipt.model_executed)
                self.assertFalse(receipt.download_attempted)
                self.assertFalse(receipt.admin_attempted)

    def test_loopback_transport_rejects_redirects_cloud_and_admin_paths(self) -> None:
        class EndpointSubclass(str):
            pass

        with self.assertRaises(ModelTransportError):
            LoopbackOllamaTransport("http://0.0.0.0:11434/api")
        with self.assertRaises(ModelTransportError):
            LoopbackOllamaTransport("http://192.168.1.3:11434/api")
        with self.assertRaises(ModelTransportError):
            LoopbackOllamaTransport(EndpointSubclass("http://127.0.0.1:11434/api"))
        for endpoint in (
            "http://localhost:11434/api",
            " http://127.0.0.1:11434/api",
            "\nhttp://127.0.0.1:11434/api",
            "http://127.0.0.1:11434/api\r\n",
            "http://127.0.0.1:0/api",
            "http://127.0.0.1:00000/api",
            "http://127.0.0.1:/api",
            "http://[::1/api",
            "http://[::1]]/api",
            "http://[gggg::1]/api",
            "http://127.0.0.1：11434/api",
            "http://127.0.0.1／api",
            "http://user＠127.0.0.1:11434/api",
            "http://127.0.0.1？query/api",
            "http://127.0.0.1＃fragment/api",
        ):
            with self.subTest(endpoint=repr(endpoint)), self.assertRaises(ModelTransportError):
                LoopbackOllamaTransport(endpoint)
        for timeout in (True, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ModelTransportError):
                LoopbackOllamaTransport(timeout_seconds=timeout)
        with self.assertRaises(ModelTransportError):
            LoopbackOllamaTransport(max_response_bytes=True)
        transport = LoopbackOllamaTransport()
        with self.assertRaises(ModelTransportError):
            transport.request("POST", "/api/pull", {})
        with self.assertRaises(ModelTransportError):
            transport.request("POST", "/api/show", {"value": float("nan")})

    def test_loopback_transport_redacts_parser_failures(self) -> None:
        for endpoint, expected_message in (
            ("http://[::1/api", "Ollama endpoint syntax is invalid"),
            ("http://[::1]]/api", "Ollama endpoint syntax is invalid"),
            ("http://[gggg::1]/api", "Ollama endpoint syntax is invalid"),
            ("http://127.0.0.1：11434/api", "Ollama endpoint syntax is invalid"),
            ("http://127.0.0.1／api", "Ollama endpoint syntax is invalid"),
            ("http://user＠127.0.0.1:11434/api", "Ollama endpoint syntax is invalid"),
            ("http://127.0.0.1？query/api", "Ollama endpoint syntax is invalid"),
            ("http://127.0.0.1＃fragment/api", "Ollama endpoint syntax is invalid"),
            (
                "http://private-host.invalid:11434/api",
                "Ollama endpoint host must be a loopback literal",
            ),
            ("http://127.0.0.1:secret/api", "Ollama endpoint port is invalid"),
        ):
            with self.subTest(endpoint=repr(endpoint)):
                with self.assertRaises(ModelTransportError) as captured:
                    LoopbackOllamaTransport(endpoint)
                self.assertEqual(str(captured.exception), expected_message)
                self.assertIsNone(captured.exception.__cause__)
                self.assertIsNone(captured.exception.__context__)

    def test_concrete_loopback_transport_owns_headers_limits_timeout_cancel_and_cleanup(
        self,
    ) -> None:
        class Response:
            status = 200

            def __init__(self, raw: bytes = b'{"version":"0.9.0"}') -> None:
                self.raw = raw

            def read(self, maximum: int) -> bytes:
                self.maximum = maximum
                return self.raw

        class Connection:
            def __init__(self, host: str, port: int, *, timeout: float) -> None:
                self.args = (host, port, timeout)
                self.closed = False
                self.response = Response()

            def request(
                self,
                method: str,
                path: str,
                *,
                body: bytes | None,
                headers: Mapping[str, str],
            ) -> None:
                self.request_args = (method, path, body, dict(headers))

            def getresponse(self) -> Response:
                return self.response

            def close(self) -> None:
                self.closed = True

        connection = Connection("unused", 1, timeout=1.0)
        with patch(
            "comfyui_h3_context.adapters.ollama_native.http.client.HTTPConnection",
            return_value=connection,
        ) as constructor:
            value = LoopbackOllamaTransport(timeout_seconds=3.0).request("GET", "/api/version")
        self.assertEqual(value, {"version": "0.9.0"})
        constructor.assert_called_once_with("127.0.0.1", 11434, timeout=3.0)
        self.assertEqual(connection.request_args[0:3], ("GET", "/api/version", None))
        self.assertEqual(
            connection.request_args[3],
            {"Accept": "application/json", "Content-Type": "application/json"},
        )
        self.assertTrue(connection.closed)

        class TimeoutConnection(Connection):
            def request(
                self,
                method: str,
                path: str,
                *,
                body: bytes | None,
                headers: Mapping[str, str],
            ) -> None:
                del method, path, body, headers
                raise TimeoutError("private endpoint")

        timed = TimeoutConnection("unused", 1, timeout=1.0)
        with (
            patch(
                "comfyui_h3_context.adapters.ollama_native.http.client.HTTPConnection",
                return_value=timed,
            ),
            self.assertRaises(LocalAdapterTimeoutError),
        ):
            LoopbackOllamaTransport().request("GET", "/api/version")
        self.assertTrue(timed.closed)

        class CancelAfterResponse:
            def __init__(self) -> None:
                self.calls = 0

            def is_cancelled(self) -> bool:
                self.calls += 1
                return self.calls > 1

        cancelled_connection = Connection("unused", 1, timeout=1.0)
        with (
            patch(
                "comfyui_h3_context.adapters.ollama_native.http.client.HTTPConnection",
                return_value=cancelled_connection,
            ),
            self.assertRaises(LocalAdapterCancelledError),
        ):
            LoopbackOllamaTransport().request(
                "GET", "/api/version", cancellation=CancelAfterResponse()
            )
        self.assertTrue(cancelled_connection.closed)

        for raw in (b'{"version":"0.9.0","version":"forged"}', b'{"version":NaN}'):
            hostile_connection = Connection("unused", 1, timeout=1.0)
            hostile_connection.response = Response(raw)
            with (
                patch(
                    "comfyui_h3_context.adapters.ollama_native.http.client.HTTPConnection",
                    return_value=hostile_connection,
                ),
                self.assertRaises(ModelTransportError),
            ):
                LoopbackOllamaTransport().request("GET", "/api/version")
            self.assertTrue(hostile_connection.closed)

    def test_ollama_qualification_rejects_cloud_or_process_identity_drift(self) -> None:
        manifest = ollama_manifest()
        model = OllamaModelObservation(
            manifest.model_id,
            manifest.model_digest,
            1024,
            "qwen3",
            ("text_generation", "vision"),
        )
        report = qualify_ollama_manifest(
            manifest,
            server=OllamaServerObservation("0.9.0", cloud_enabled=True),
            model=model,
            process=OllamaProcessObservation("qwen3-vl:8b", fingerprint("c")),
        )
        self.assertEqual(report.status, ModelProbeStatus.UNSUPPORTED)
        self.assertIn("cloud_route", report.diagnostics)
        self.assertIn("process_identity_mismatch", report.diagnostics)

    def test_offline_model_fixture_is_deterministic_and_no_network(self) -> None:
        command = [sys.executable, str(ROOT / "scripts" / "m10_03_model_fixture.py"), "--json"]
        first = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=10
        )
        second = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=10
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["network"], "disabled")
        self.assertEqual(payload["host_runtime"], "not_started")
        self.assertFalse(payload["automatic_fallback"])


if __name__ == "__main__":
    unittest.main()
