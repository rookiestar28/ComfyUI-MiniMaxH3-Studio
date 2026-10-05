"""Pure model-family, qualification, and typed-output contracts for M10-03.

This module never discovers a checkpoint, imports ComfyUI/Torch/Ollama, opens a URL, or retains
model/media content. Runtime adapters pass bounded observations into the contracts and keep raw
objects, prompts, responses, paths, and endpoints outside portable receipts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import cast

from .canonical import canonical_fingerprint
from .contracts import EvidenceLevel
from .errors import ModelManifestError, ModelOutputError
from .local_adapters import LocalDeviceSpec, LocalResourceBudget

MODEL_MANIFEST_SCHEMA = "h3.model.manifest.v1"
MODEL_PROBE_SCHEMA = "h3.model.probe.v1"
MODEL_GENERATION_REQUEST_SCHEMA = "h3.model.generation.request.v1"
MODEL_GENERATION_RESULT_SCHEMA = "h3.model.generation.result.v1"
MAX_MODEL_CAPABILITIES = 16
MAX_MODEL_DIAGNOSTICS = 32
MAX_MODEL_PROMPT_CHARS = 32_768
MAX_MODEL_OUTPUT_CHARS = 131_072
MAX_MODEL_MEDIA_ITEMS = 16
MAX_MODEL_JSON_DEPTH = 12
MAX_MODEL_JSON_KEYS = 128

_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TEXT = re.compile(r"[^\x00-\x1f\x7f\ud800-\udfff]*\Z")


class ModelBackendFamily(str, Enum):
    """Explicit backend family; selection is never implicit."""

    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"


class ModelCapability(str, Enum):
    TEXT_GENERATION = "text_generation"
    VISION = "vision"
    VIDEO = "video"
    AUDIO = "audio"
    STRUCTURED_OUTPUT = "structured_output"


class ModelProbeStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    NOT_RUN = "not_run"


class ModelCapabilityState(str, Enum):
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"
    UNSUPPORTED = "unsupported"


def _code(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise ModelManifestError(f"{field_name} must be a bounded lower-case code")
    return value.casefold()


def _model_name(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _MODEL_NAME.fullmatch(value) is None:
        raise ModelManifestError(f"{field_name} must be a bounded model name")
    if any(marker in value.casefold() for marker in ("/", "\\", "token", "secret", "password")):
        raise ModelManifestError(f"{field_name} contains forbidden locator or secret material")
    return value


def _text(value: object, field_name: str, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or _TEXT.fullmatch(value) is None
    ):
        raise ModelManifestError(f"{field_name} must be bounded text")
    return value


def _version(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ModelManifestError(f"{field_name} must be a numeric version")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ModelManifestError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ModelManifestError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelManifestError(f"{field_name} must be a non-negative integer")
    return value


def _bounded_codes(
    values: object, field_name: str, maximum: int = MAX_MODEL_CAPABILITIES
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise ModelManifestError(f"{field_name} must be a bounded tuple")
    result = tuple(_code(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise ModelManifestError(f"{field_name} must not contain duplicates")
    return result


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    """Validate a bounded JSON tree before a schema or model response crosses the adapter."""

    if depth > MAX_MODEL_JSON_DEPTH:
        raise ModelOutputError("structured JSON exceeds the finite nesting depth")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if len(value) > MAX_MODEL_OUTPUT_CHARS:
            raise ModelOutputError("structured JSON string exceeds the finite length")
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_MODEL_JSON_KEYS:
            raise ModelOutputError("structured JSON object exceeds the finite key limit")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise ModelOutputError("structured JSON key is outside the finite limit")
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_MODEL_JSON_KEYS:
            raise ModelOutputError("structured JSON array exceeds the finite item limit")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    raise ModelOutputError("structured JSON contains a non-JSON value")


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class ModelRuntimeProfile:
    """Bounded execution profile without a framework/device object."""

    device: LocalDeviceSpec
    dtype: str
    offload: str
    max_context_tokens: int
    max_output_tokens: int
    max_media_items: int
    limits: LocalResourceBudget

    def __post_init__(self) -> None:
        if not isinstance(self.device, LocalDeviceSpec):
            raise ModelManifestError("runtime device must be LocalDeviceSpec")
        _code(self.dtype, "runtime dtype")
        _code(self.offload, "runtime offload")
        _positive_int(self.max_context_tokens, "max_context_tokens")
        _positive_int(self.max_output_tokens, "max_output_tokens")
        _positive_int(self.max_media_items, "max_media_items")
        if not isinstance(self.limits, LocalResourceBudget):
            raise ModelManifestError("runtime limits must be LocalResourceBudget")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "device": self.device.to_wire(),
            "dtype": self.dtype,
            "offload": self.offload,
            "max_context_tokens": self.max_context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_media_items": self.max_media_items,
            "limits": self.limits.to_public_dict(),
        }


@dataclass(frozen=True, slots=True)
class ModelManifest:
    """Approved model-family declaration; discovery alone cannot construct one."""

    manifest_id: str
    backend_family: ModelBackendFamily
    adapter_id: str
    adapter_version: str
    model_id: str
    model_digest: str
    checkpoint_fingerprint: str
    detected_family: str
    clip_type: str | None
    tokenizer_processor: str
    generation_weights_complete: bool
    capabilities: frozenset[ModelCapability]
    structured_output_schema: str
    parser_path: str
    runtime: ModelRuntimeProfile
    cancellation: ModelCapabilityState
    license: str
    host_profile: str
    evidence_level: EvidenceLevel
    approved: bool = False
    notes: str = ""
    schema: str = MODEL_MANIFEST_SCHEMA
    server_version: str | None = None

    def __post_init__(self) -> None:
        _code(self.manifest_id, "manifest_id")
        if not isinstance(self.backend_family, ModelBackendFamily):
            raise ModelManifestError("backend_family must be ModelBackendFamily")
        _code(self.adapter_id, "adapter_id")
        _version(self.adapter_version, "adapter_version")
        _model_name(self.model_id, "model_id")
        _fingerprint(self.model_digest, "model_digest")
        _fingerprint(self.checkpoint_fingerprint, "checkpoint_fingerprint")
        _code(self.detected_family, "detected_family")
        if self.clip_type is not None:
            _code(self.clip_type, "clip_type")
        _code(self.tokenizer_processor, "tokenizer_processor")
        if not isinstance(self.generation_weights_complete, bool):
            raise ModelManifestError("generation_weights_complete must be a boolean")
        if not isinstance(self.capabilities, frozenset) or not self.capabilities:
            raise ModelManifestError("capabilities must be a non-empty frozenset")
        if len(self.capabilities) > MAX_MODEL_CAPABILITIES or not all(
            isinstance(item, ModelCapability) for item in self.capabilities
        ):
            raise ModelManifestError("capabilities are outside the finite bound")
        _code(self.structured_output_schema, "structured_output_schema")
        _code(self.parser_path, "parser_path")
        if not isinstance(self.runtime, ModelRuntimeProfile):
            raise ModelManifestError("runtime must be ModelRuntimeProfile")
        if not isinstance(self.cancellation, ModelCapabilityState):
            raise ModelManifestError("cancellation must be ModelCapabilityState")
        _code(self.license, "license")
        _code(self.host_profile, "host_profile")
        if not isinstance(self.evidence_level, EvidenceLevel):
            raise ModelManifestError("evidence_level must be EvidenceLevel")
        if not isinstance(self.approved, bool):
            raise ModelManifestError("approved must be a boolean")
        if self.notes:
            _text(self.notes, "notes", 2048)
        if self.schema != MODEL_MANIFEST_SCHEMA:
            raise ModelManifestError("unsupported model manifest schema")
        if self.server_version is not None:
            _version(self.server_version, "server_version")
        if self.backend_family is ModelBackendFamily.COMFYUI_NATIVE and self.clip_type is None:
            raise ModelManifestError("native manifest requires an explicit CLIPType")
        if self.backend_family is ModelBackendFamily.OLLAMA and self.clip_type is not None:
            raise ModelManifestError("Ollama manifest must not claim a ComfyUI CLIPType")
        if ModelCapability.TEXT_GENERATION in self.capabilities:
            if not self.generation_weights_complete:
                raise ModelManifestError("text generation requires complete generation weights")
            if ModelCapability.STRUCTURED_OUTPUT not in self.capabilities:
                raise ModelManifestError("text generation requires structured-output capability")
            if "conditioning" in self.detected_family.casefold():
                raise ModelManifestError("conditioning-only model cannot claim text generation")
        # CRITICAL: loopback Ollama can route signed-in cloud models; reject documented cloud tags.
        if self.backend_family is ModelBackendFamily.OLLAMA and self.model_id.casefold().endswith(
            (":cloud", "-cloud", "/cloud", "_cloud")
        ):
            raise ModelManifestError("cloud-qualified Ollama models are not allowed")
        if (
            self.backend_family is ModelBackendFamily.OLLAMA
            and self.approved
            and self.server_version is None
        ):
            raise ModelManifestError("approved Ollama manifest requires a pinned server version")

    @property
    def supports_generation(self) -> bool:
        return ModelCapability.TEXT_GENERATION in self.capabilities

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "manifest_id": self.manifest_id,
            "backend_family": self.backend_family.value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "checkpoint_fingerprint": self.checkpoint_fingerprint,
            "detected_family": self.detected_family,
            "clip_type": self.clip_type,
            "tokenizer_processor": self.tokenizer_processor,
            "generation_weights_complete": self.generation_weights_complete,
            "capabilities": sorted(item.value for item in self.capabilities),
            "structured_output_schema": self.structured_output_schema,
            "parser_path": self.parser_path,
            "runtime": self.runtime.to_public_dict(),
            "cancellation": self.cancellation.value,
            "license": self.license,
            "host_profile": self.host_profile,
            "evidence_level": self.evidence_level.value,
            "approved": self.approved,
            "notes": self.notes,
            "server_version": self.server_version,
        }

    @property
    def fingerprint(self) -> str:
        payload = self.to_public_dict()
        runtime = cast(dict[str, object], payload["runtime"])
        limits = cast(dict[str, object], runtime["limits"])
        # The existing local-adapter wire shape is human-readable; canonical bytes require
        # floats to be represented explicitly rather than accepting a raw binary float.
        limits["max_wall_time_seconds"] = format(self.runtime.limits.max_wall_time_seconds, ".17g")
        return canonical_fingerprint(payload)


@dataclass(frozen=True, slots=True)
class OllamaModelObservation:
    """Redacted `/api/tags` + `/api/show` model identity observation."""

    name: str
    digest: str
    size_bytes: int
    family: str
    capabilities: tuple[str, ...]
    context_length: int | None = None
    quantization: str | None = None

    def __post_init__(self) -> None:
        _model_name(self.name, "Ollama model name")
        _fingerprint(self.digest, "Ollama model digest")
        _positive_int(self.size_bytes, "Ollama model size_bytes")
        _code(self.family, "Ollama model family")
        _bounded_codes(self.capabilities, "Ollama model capabilities")
        if self.context_length is not None:
            _positive_int(self.context_length, "Ollama context_length")
        if self.quantization is not None:
            _code(self.quantization, "Ollama quantization")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "family": self.family,
            "capabilities": list(self.capabilities),
            "context_length": self.context_length,
            "quantization": self.quantization,
        }


@dataclass(frozen=True, slots=True)
class OllamaServerObservation:
    version: str
    cloud_enabled: bool = False

    def __post_init__(self) -> None:
        _version(self.version, "Ollama server version")
        if not isinstance(self.cloud_enabled, bool):
            raise ModelManifestError("cloud_enabled must be a boolean")

    def to_public_dict(self) -> dict[str, object]:
        return {"version": self.version, "cloud_enabled": self.cloud_enabled}


@dataclass(frozen=True, slots=True)
class OllamaProcessObservation:
    model_name: str
    model_digest: str
    vram_bytes: int = 0
    context_length: int | None = None

    def __post_init__(self) -> None:
        _model_name(self.model_name, "Ollama process model_name")
        _fingerprint(self.model_digest, "Ollama process model_digest")
        _non_negative_int(self.vram_bytes, "Ollama process vram_bytes")
        if self.context_length is not None:
            _positive_int(self.context_length, "Ollama process context_length")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "model_name": self.model_name,
            "model_digest": self.model_digest,
            "vram_bytes": self.vram_bytes,
            "context_length": self.context_length,
        }


@dataclass(frozen=True, slots=True)
class ModelProbeReport:
    manifest_id: str
    status: ModelProbeStatus
    diagnostics: tuple[str, ...] = ()
    observed_model: OllamaModelObservation | None = None
    server: OllamaServerObservation | None = None
    process: OllamaProcessObservation | None = None
    schema: str = MODEL_PROBE_SCHEMA

    def __post_init__(self) -> None:
        _code(self.manifest_id, "probe manifest_id")
        if not isinstance(self.status, ModelProbeStatus):
            raise ModelManifestError("probe status must be ModelProbeStatus")
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > MAX_MODEL_DIAGNOSTICS:
            raise ModelManifestError("probe diagnostics exceed the finite bound")
        object.__setattr__(
            self,
            "diagnostics",
            _bounded_codes(self.diagnostics, "probe diagnostics", MAX_MODEL_DIAGNOSTICS),
        )
        if self.observed_model is not None and not isinstance(
            self.observed_model, OllamaModelObservation
        ):
            raise ModelManifestError("observed_model must be OllamaModelObservation")
        if self.server is not None and not isinstance(self.server, OllamaServerObservation):
            raise ModelManifestError("server must be OllamaServerObservation")
        if self.process is not None and not isinstance(self.process, OllamaProcessObservation):
            raise ModelManifestError("process must be OllamaProcessObservation")
        if self.schema != MODEL_PROBE_SCHEMA:
            raise ModelManifestError("unsupported model probe schema")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "manifest_id": self.manifest_id,
            "status": self.status.value,
            "diagnostics": list(self.diagnostics),
            "observed_model": None
            if self.observed_model is None
            else self.observed_model.to_public_dict(),
            "server": None if self.server is None else self.server.to_public_dict(),
            "process": None if self.process is None else self.process.to_public_dict(),
        }


@dataclass(frozen=True, slots=True)
class ModelGenerationRequest:
    """Runtime generation request; media/prompt content is never in public receipts."""

    prompt: str = field(repr=False, compare=False)
    max_tokens: int = 512
    output_schema: str = "h3.model.typed_output.v1"
    do_sample: bool = False
    temperature: float = 1.0
    top_k: int = 50
    top_p: float = 1.0
    min_p: float = 0.0
    repetition_penalty: float = 1.0
    presence_penalty: float = 0.0
    seed: int | None = None
    thinking: bool = False
    use_default_template: bool = True
    media_fingerprints: tuple[str, ...] = ()
    image: object = field(default=None, repr=False, compare=False)
    video: object = field(default=None, repr=False, compare=False)
    audio: object = field(default=None, repr=False, compare=False)
    image_payloads: tuple[bytes, ...] = field(default=(), repr=False, compare=False)
    structured_schema: Mapping[str, object] | None = field(default=None, repr=False, compare=False)
    schema: str = MODEL_GENERATION_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if (
            not isinstance(self.prompt, str)
            or not self.prompt
            or len(self.prompt) > MAX_MODEL_PROMPT_CHARS
        ):
            raise ModelManifestError("prompt is outside the bounded generation input")
        if any(ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in self.prompt):
            raise ModelManifestError("prompt contains an unsafe code point")
        _positive_int(self.max_tokens, "max_tokens")
        _code(self.output_schema, "output_schema")
        for value, field_name in (
            (self.temperature, "temperature"),
            (self.top_p, "top_p"),
            (self.min_p, "min_p"),
            (self.repetition_penalty, "repetition_penalty"),
            (self.presence_penalty, "presence_penalty"),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ModelManifestError(f"{field_name} must be non-negative")
        _non_negative_int(self.top_k, "top_k")
        if self.seed is not None and (
            isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0
        ):
            raise ModelManifestError("seed must be a non-negative integer or None")
        if (
            not isinstance(self.do_sample, bool)
            or not isinstance(self.thinking, bool)
            or not isinstance(self.use_default_template, bool)
        ):
            raise ModelManifestError("generation boolean controls must be booleans")
        if (
            not isinstance(self.media_fingerprints, tuple)
            or len(self.media_fingerprints) > MAX_MODEL_MEDIA_ITEMS
        ):
            raise ModelManifestError("media_fingerprints exceed the finite limit")
        for fingerprint_value in self.media_fingerprints:
            _fingerprint(fingerprint_value, "media fingerprint")
        if (
            not isinstance(self.image_payloads, tuple)
            or len(self.image_payloads) > MAX_MODEL_MEDIA_ITEMS
        ):
            raise ModelManifestError("image_payloads exceed the finite limit")
        for payload in self.image_payloads:
            if not isinstance(payload, bytes) or not payload or len(payload) > 16_000_000:
                raise ModelManifestError("image payload is outside the bounded runtime limit")
        if self.structured_schema is not None and not isinstance(self.structured_schema, Mapping):
            raise ModelManifestError("structured_schema must be a mapping or None")
        if self.structured_schema is not None:
            try:
                _validate_json_value(self.structured_schema)
            except ModelOutputError as exc:
                raise ModelManifestError(
                    "structured_schema exceeds its bounded JSON contract"
                ) from exc
        if self.schema != MODEL_GENERATION_REQUEST_SCHEMA:
            raise ModelManifestError("unsupported generation request schema")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "prompt_fingerprint": canonical_fingerprint({"prompt": self.prompt}),
            "prompt_chars": len(self.prompt),
            "max_tokens": self.max_tokens,
            "output_schema": self.output_schema,
            "do_sample": self.do_sample,
            "temperature": self.temperature,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "min_p": self.min_p,
            "repetition_penalty": self.repetition_penalty,
            "presence_penalty": self.presence_penalty,
            "seed": self.seed,
            "thinking": self.thinking,
            "use_default_template": self.use_default_template,
            "media_fingerprints": list(self.media_fingerprints),
            "image_count": len(self.image_payloads),
            "structured_schema_present": self.structured_schema is not None,
        }


@dataclass(frozen=True, slots=True)
class ModelGenerationResult:
    """Typed-output boundary; generated content stays runtime-only."""

    backend_family: ModelBackendFamily
    model_id: str
    model_digest: str
    output_fingerprint: str
    parsed_output: Mapping[str, object] = field(repr=False, compare=False)
    text: str = field(repr=False, compare=False)
    parser_path: str
    complete: bool = True
    diagnostics: tuple[str, ...] = ()
    schema: str = MODEL_GENERATION_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.backend_family, ModelBackendFamily):
            raise ModelOutputError("backend_family must be ModelBackendFamily")
        _model_name(self.model_id, "result model_id")
        _fingerprint(self.model_digest, "result model_digest")
        _fingerprint(self.output_fingerprint, "result output_fingerprint")
        if not isinstance(self.parsed_output, Mapping):
            raise ModelOutputError("parsed_output must be an object")
        if (
            not isinstance(self.text, str)
            or not self.text
            or len(self.text) > MAX_MODEL_OUTPUT_CHARS
        ):
            raise ModelOutputError("generated text is outside the bounded output limit")
        _code(self.parser_path, "result parser_path")
        if not isinstance(self.complete, bool) or not self.complete:
            raise ModelOutputError("model result must be complete")
        object.__setattr__(
            self,
            "diagnostics",
            _bounded_codes(self.diagnostics, "result diagnostics", MAX_MODEL_DIAGNOSTICS),
        )
        if self.schema != MODEL_GENERATION_RESULT_SCHEMA:
            raise ModelOutputError("unsupported model result schema")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "backend_family": self.backend_family.value,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "output_fingerprint": self.output_fingerprint,
            "output_chars": len(self.text),
            "parsed_keys": sorted(str(key) for key in self.parsed_output),
            "parser_path": self.parser_path,
            "complete": self.complete,
            "diagnostics": list(self.diagnostics),
        }


def parse_json_object_output(
    text: str, *, maximum_chars: int = MAX_MODEL_OUTPUT_CHARS
) -> dict[str, object]:
    """Parse one complete JSON object; partial/array/scalar output is never accepted."""

    if not isinstance(text, str) or not text or len(text) > maximum_chars:
        raise ModelOutputError("model output exceeds the bounded parser input")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ModelOutputError("model output is not complete valid JSON") from exc
    if not isinstance(value, dict):
        raise ModelOutputError("model output must be a JSON object")
    if len(value) > MAX_MODEL_JSON_KEYS:
        raise ModelOutputError("model output object exceeds the bounded key limit")
    _validate_json_value(value)
    return cast(dict[str, object], value)


def validate_model_generation_request(
    manifest: ModelManifest,
    request: ModelGenerationRequest,
) -> None:
    """Bind request-level token/media budgets to the selected model manifest."""

    if not isinstance(manifest, ModelManifest) or not manifest.approved:
        raise ModelOutputError("model manifest is not approved for generation")
    if not isinstance(request, ModelGenerationRequest):
        raise ModelOutputError("request must be ModelGenerationRequest")
    if request.max_tokens > manifest.runtime.max_output_tokens:
        raise ModelOutputError("requested output tokens exceed the model manifest limit")
    media_count = max(len(request.media_fingerprints), len(request.image_payloads))
    if media_count > manifest.runtime.max_media_items:
        raise ModelOutputError("requested media exceeds the model manifest limit")


def build_model_result(
    manifest: ModelManifest,
    request: ModelGenerationRequest,
    text: str,
) -> ModelGenerationResult:
    """Bind complete generated text to an approved manifest and local parser."""

    if (
        not isinstance(manifest, ModelManifest)
        or not manifest.approved
        or not manifest.supports_generation
    ):
        raise ModelOutputError("model manifest is not approved for generation")
    if not isinstance(request, ModelGenerationRequest):
        raise ModelOutputError("request must be ModelGenerationRequest")
    validate_model_generation_request(manifest, request)
    parsed = parse_json_object_output(text)
    return ModelGenerationResult(
        backend_family=manifest.backend_family,
        model_id=manifest.model_id,
        model_digest=manifest.model_digest,
        output_fingerprint=canonical_fingerprint({"text": text, "schema": request.output_schema}),
        parsed_output=parsed,
        text=text,
        parser_path=manifest.parser_path,
    )


def qualify_ollama_manifest(
    manifest: ModelManifest,
    *,
    server: OllamaServerObservation,
    model: OllamaModelObservation,
    process: OllamaProcessObservation | None = None,
) -> ModelProbeReport:
    """Reconcile local Ollama preflight observations with an approved manifest."""

    if (
        not isinstance(manifest, ModelManifest)
        or manifest.backend_family is not ModelBackendFamily.OLLAMA
    ):
        raise ModelManifestError("Ollama qualification requires an Ollama manifest")
    diagnostics: list[str] = []
    if server.cloud_enabled:
        diagnostics.append("cloud_route")
    if model.name != manifest.model_id:
        diagnostics.append("model_name_mismatch")
    if model.digest != manifest.model_digest:
        diagnostics.append("model_digest_mismatch")
    if manifest.server_version is not None and server.version != manifest.server_version:
        diagnostics.append("server_version_mismatch")
    required = {item.value for item in manifest.capabilities}
    if not required.issubset(set(model.capabilities) | {"structured_output"}):
        diagnostics.append("model_capability_mismatch")
    if process is not None and (
        process.model_name != model.name or process.model_digest != model.digest
    ):
        diagnostics.append("process_identity_mismatch")
    status = (
        ModelProbeStatus.SUPPORTED
        if not diagnostics and manifest.approved
        else ModelProbeStatus.UNSUPPORTED
    )
    return ModelProbeReport(
        manifest_id=manifest.manifest_id,
        status=status,
        diagnostics=tuple(diagnostics),
        observed_model=model,
        server=server,
        process=process,
    )


__all__ = [
    "MODEL_MANIFEST_SCHEMA",
    "MODEL_PROBE_SCHEMA",
    "MODEL_GENERATION_REQUEST_SCHEMA",
    "MODEL_GENERATION_RESULT_SCHEMA",
    "ModelBackendFamily",
    "ModelCapability",
    "ModelCapabilityState",
    "ModelManifest",
    "ModelProbeReport",
    "ModelProbeStatus",
    "ModelRuntimeProfile",
    "ModelGenerationRequest",
    "ModelGenerationResult",
    "OllamaModelObservation",
    "OllamaProcessObservation",
    "OllamaServerObservation",
    "build_model_result",
    "parse_json_object_output",
    "qualify_ollama_manifest",
    "validate_model_generation_request",
]
