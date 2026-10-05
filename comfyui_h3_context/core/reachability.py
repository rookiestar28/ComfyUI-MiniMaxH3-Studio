"""Provider-free public producer and task-mode reachability matrix.

The matrix is deliberately separate from the node implementation and capability maturity
manifest.  It answers a narrower question: can a normal public workflow reach a field or task
mode through a named producer, or must the route fail closed?  Injected test/host seams are
recorded explicitly and are never promoted to normal-user support.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .contracts import TaskMode
from .errors import ContractValidationError

if TYPE_CHECKING:
    from .capability_manifest import BindingManifest

REACHABILITY_SCHEMA = "h3.context.reachability.v1"
MAX_REACHABILITY_ENTRIES = 256
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")


class ReachabilityStage(str, Enum):
    """Independent capability stages whose support must not be conflated."""

    CONTEXT_IR = "context_ir"
    COMPILER = "compiler"
    NATIVE_GENERATION = "native_generation"


class ReachabilityDisposition(str, Enum):
    """Normal public support, injected-only seam, or an explicit unsupported route."""

    REACHABLE = "reachable"
    INJECTED_ONLY = "injected_only"
    EXPLICIT_UNAVAILABLE = "explicit_unavailable"
    UNSUPPORTED = "unsupported"


class ReachabilitySource(str, Enum):
    """Origin of a named path; injected sources are never normal producers."""

    PUBLIC_NODE = "public_node"
    HOST_INPUT = "host_input"
    INJECTED_FIXTURE = "injected_fixture"
    NONE = "none"


class ReachabilityError(ContractValidationError):
    """Raised when a matrix is incomplete or a caller requests an unsupported path."""


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ReachabilityError(f"{field} must be a bounded identifier")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value) is None:
        raise ReachabilityError(f"{field} must be a lower-case bounded code")
    return value


def _text(value: object, field: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ReachabilityError(f"{field} must be a bounded non-empty string")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ReachabilityError(f"{field} contains a control character")
    return value


@dataclass(frozen=True, slots=True)
class ProducerPath:
    """Named public or injected producer path for one reachability entry."""

    node_id: str
    socket_name: str
    source: ReachabilitySource = ReachabilitySource.PUBLIC_NODE

    def __post_init__(self) -> None:
        _identifier(self.node_id, "producer node_id")
        _code(self.socket_name, "producer socket_name")
        if not isinstance(self.source, ReachabilitySource):
            raise ReachabilityError("producer source must be a ReachabilitySource")

    def to_wire(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "socket_name": self.socket_name,
            "source": self.source.value,
        }


@dataclass(frozen=True, slots=True)
class ReachabilityEntry:
    """One stable binding or task-mode disposition."""

    entry_id: str
    stage: ReachabilityStage
    disposition: ReachabilityDisposition
    description: str
    task_mode: TaskMode | None = None
    binding_node_id: str | None = None
    binding_port_name: str | None = None
    producer: ProducerPath | None = None
    failure_code: str | None = None
    normal_use: bool = True

    def __post_init__(self) -> None:
        _code(self.entry_id, "reachability entry_id")
        if not isinstance(self.stage, ReachabilityStage):
            raise ReachabilityError("reachability stage must be a ReachabilityStage")
        if not isinstance(self.disposition, ReachabilityDisposition):
            raise ReachabilityError("reachability disposition must be a ReachabilityDisposition")
        _text(self.description, "reachability description")
        if self.task_mode is not None and not isinstance(self.task_mode, TaskMode):
            raise ReachabilityError("reachability task_mode must be a TaskMode or None")
        if self.binding_node_id is None:
            if self.binding_port_name is not None:
                raise ReachabilityError("binding_port_name requires binding_node_id")
        else:
            _identifier(self.binding_node_id, "binding node_id")
            if self.binding_port_name is None:
                raise ReachabilityError("binding_node_id requires binding_port_name")
            _code(self.binding_port_name, "binding port_name")
        if self.producer is not None and not isinstance(self.producer, ProducerPath):
            raise ReachabilityError("producer must be a ProducerPath or None")
        if self.failure_code is not None:
            _code(self.failure_code, "reachability failure_code")
        if not isinstance(self.normal_use, bool):
            raise ReachabilityError("reachability normal_use must be a boolean")

        if self.disposition is ReachabilityDisposition.REACHABLE:
            if self.producer is None or self.producer.source not in {
                ReachabilitySource.PUBLIC_NODE,
                ReachabilitySource.HOST_INPUT,
            }:
                raise ReachabilityError("reachable entries require a public or host-input producer")
            if self.failure_code is not None or not self.normal_use:
                raise ReachabilityError("reachable entries cannot carry failure or disabled state")
        elif self.disposition is ReachabilityDisposition.INJECTED_ONLY:
            if (
                self.producer is None
                or self.producer.source is not ReachabilitySource.INJECTED_FIXTURE
            ):
                raise ReachabilityError("injected-only entries require an injected producer")
            if self.failure_code is not None or self.normal_use:
                raise ReachabilityError("injected-only entries are never normal use")
        elif self.disposition is ReachabilityDisposition.EXPLICIT_UNAVAILABLE:
            if self.producer is None or self.producer.source is not ReachabilitySource.PUBLIC_NODE:
                raise ReachabilityError(
                    "explicit-unavailable entries require a named public producer"
                )
            if self.failure_code is None or self.normal_use:
                raise ReachabilityError(
                    "explicit-unavailable entries require failure and disabled normal use"
                )
        else:
            if self.failure_code is None:
                raise ReachabilityError("unsupported entries require a failure_code")
            if self.producer is not None and self.producer.source is ReachabilitySource.PUBLIC_NODE:
                raise ReachabilityError("unsupported entries cannot claim a public producer")
            if self.normal_use:
                raise ReachabilityError("unsupported entries must disable normal use")

    def to_wire(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "stage": self.stage.value,
            "disposition": self.disposition.value,
            "description": self.description,
            "task_mode": None if self.task_mode is None else self.task_mode.value,
            "binding_node_id": self.binding_node_id,
            "binding_port_name": self.binding_port_name,
            "producer": None if self.producer is None else self.producer.to_wire(),
            "failure_code": self.failure_code,
            "normal_use": self.normal_use,
        }


@dataclass(frozen=True, slots=True)
class ReachabilityManifest:
    """Immutable matrix for current public bindings and H3 task-mode stages."""

    entries: tuple[ReachabilityEntry, ...]
    schema: str = REACHABILITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != REACHABILITY_SCHEMA:
            raise ReachabilityError("unsupported reachability schema")
        if not isinstance(self.entries, tuple) or not self.entries:
            raise ReachabilityError("reachability entries must be a non-empty tuple")
        if len(self.entries) > MAX_REACHABILITY_ENTRIES:
            raise ReachabilityError("reachability entries exceed the finite bound")
        if not all(isinstance(item, ReachabilityEntry) for item in self.entries):
            raise ReachabilityError("reachability entries contain an invalid value")
        entry_ids = tuple(item.entry_id for item in self.entries)
        if len(entry_ids) != len(set(entry_ids)):
            raise ReachabilityError("reachability entry IDs must be unique")

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_wire(include_fingerprint=False),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.schema,
            "entries": [item.to_wire() for item in self.entries],
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value

    def for_binding(self, node_id: str, port_name: str) -> ReachabilityEntry:
        _identifier(node_id, "binding node_id")
        _code(port_name, "binding port_name")
        matches = tuple(
            item
            for item in self.entries
            if item.binding_node_id == node_id and item.binding_port_name == port_name
        )
        if len(matches) != 1:
            raise ReachabilityError(
                f"reachability matrix must contain exactly one entry for {node_id}.{port_name}"
            )
        return matches[0]

    def require_normal(self, entry_id: str) -> ReachabilityEntry:
        _code(entry_id, "entry_id")
        for item in self.entries:
            if item.entry_id == entry_id:
                if item.disposition is not ReachabilityDisposition.REACHABLE:
                    raise ReachabilityError(item.failure_code or "reachability_not_supported")
                return item
        raise ReachabilityError(f"unknown reachability entry: {entry_id!r}")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _entry_id(prefix: str, value: str) -> str:
    slug = _slug(value)
    if len(slug) > 48:
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        slug = f"{slug[:37]}_{suffix}"
    return _code(f"{prefix}.{slug}", "reachability entry_id")


def _binding_entry(
    *,
    node_id: str,
    port_name: str,
    stage: ReachabilityStage,
    disposition: ReachabilityDisposition,
    description: str,
    producer: ProducerPath | None = None,
    failure_code: str | None = None,
    normal_use: bool = True,
) -> ReachabilityEntry:
    return ReachabilityEntry(
        entry_id=_entry_id("binding", f"{node_id}.{port_name}"),
        stage=stage,
        disposition=disposition,
        description=description,
        binding_node_id=node_id,
        binding_port_name=port_name,
        producer=producer,
        failure_code=failure_code,
        normal_use=normal_use,
    )


def _mode_entry(
    *,
    stage: ReachabilityStage,
    mode: TaskMode,
    disposition: ReachabilityDisposition,
    description: str,
    producer: ProducerPath | None = None,
    failure_code: str | None = None,
    normal_use: bool = True,
    entry_key: str | None = None,
) -> ReachabilityEntry:
    return ReachabilityEntry(
        entry_id=_entry_id("mode", entry_key or f"{stage.value}.{mode.value}"),
        stage=stage,
        task_mode=mode,
        disposition=disposition,
        description=description,
        producer=producer,
        failure_code=failure_code,
        normal_use=normal_use,
    )


_TYPED_INPUT_FAILURES = {
    "H3_CONTEXT_IR_MEDIA": "context_ir_media_producer_unavailable",
}

_EXPLICIT_UNAVAILABLE_INPUTS = {
    "H3_FULL_REFERENCE_TIMELINE": (
        "comfyui_h3_context.H3Context.FullReferenceTimelineProducer",
        "timeline",
        "perception_profile_unavailable",
    ),
}


def _producer_for_input(node_id: str, socket_type: str, port_name: str) -> ProducerPath | None:
    """Resolve a producer without importing the node module or a host runtime."""

    if socket_type in _TYPED_INPUT_FAILURES:
        return None
    report_producers = {
        (
            "comfyui_h3_context.H3Context.DirectiveAuthorityProducer",
            "hard_constraints_report",
        ): ("comfyui_h3_context.H3Context.HardConstraintProducer", "producer_report"),
        (
            "comfyui_h3_context.H3Context.DirectiveAuthorityProducer",
            "intent_report",
        ): ("comfyui_h3_context.H3Context.IntentGraphProducer", "producer_report"),
        (
            "comfyui_h3_context.H3Context.FullReferenceTimelineProducer",
            "evidence_report",
        ): ("comfyui_h3_context.H3Context.EvidenceFusionProducer", "producer_report"),
        (
            "comfyui_h3_context.H3Context.FullReferenceTimelineProducer",
            "cross_reference_report",
        ): ("comfyui_h3_context.H3Context.CrossReferenceProducer", "producer_report"),
        (
            "comfyui_h3_context.H3Context.FullReferenceTimelineProducer",
            "directive_report",
        ): ("comfyui_h3_context.H3Context.DirectiveAuthorityProducer", "producer_report"),
        (
            "comfyui_h3_context.H3Context.FullReferenceTimelineProducer",
            "intent_report",
        ): ("comfyui_h3_context.H3Context.IntentGraphProducer", "producer_report"),
    }
    report_producer = report_producers.get((node_id, port_name))
    if socket_type == "H3_DOWNSTREAM_PRODUCER_REPORT":
        return None if report_producer is None else ProducerPath(*report_producer)
    upstream = {
        "H3_CONTEXT_REQUEST": (
            "comfyui_h3_context.H3Context.Request",
            "request",
        ),
        "H3_REFERENCE_REGISTRY": (
            "comfyui_h3_context.H3Context.ReferenceRegistry",
            "reference_registry",
        ),
        "H3_CONTEXT_PLAN": (
            "comfyui_h3_context.H3Context.Plan",
            "plan",
        ),
        "H3_PROMPT_DOCUMENT": (
            "comfyui_h3_context.H3Context.Compiler",
            "prompt_document",
        ),
        "H3_CONTEXT_REPORT": (
            "comfyui_h3_context.H3Context.Validator",
            "validated_report",
        ),
        "H3_MEDIA_PRODUCER_RESULT": (
            "comfyui_h3_context.H3Context.MediaAdmissionProducer",
            "media",
        ),
        "H3_HARD_CONSTRAINTS": (
            "comfyui_h3_context.H3Context.HardConstraintProducer",
            "hard_constraints",
        ),
        "H3_INTENT_GRAPH": (
            "comfyui_h3_context.H3Context.IntentGraphProducer",
            "intent_graph",
        ),
        "H3_UNIFIED_EVIDENCE_GRAPH": (
            "comfyui_h3_context.H3Context.EvidenceFusionProducer",
            "evidence_graph",
        ),
        "H3_CROSS_REFERENCE_GRAPH": (
            "comfyui_h3_context.H3Context.CrossReferenceProducer",
            "cross_reference_graph",
        ),
        "H3_DIRECTIVE_AUTHORITY": (
            "comfyui_h3_context.H3Context.DirectiveAuthorityProducer",
            "directive_authority",
        ),
    }
    selected = upstream.get(socket_type)
    if selected is not None:
        return ProducerPath(*selected)
    if socket_type in {"IMAGE", "VIDEO", "AUDIO"}:
        return ProducerPath(
            "comfyui.host.UpstreamMedia",
            socket_type.casefold(),
            ReachabilitySource.HOST_INPUT,
        )
    # Scalar widgets are themselves named, deterministic public producers.
    if socket_type in {"STRING", "FLOAT", "INT", "BOOLEAN", "COMBO"}:
        return ProducerPath(node_id, port_name)
    return None


def build_default_reachability_manifest(
    binding_manifest: BindingManifest | None = None,
) -> ReachabilityManifest:
    """Build the current matrix from the backend-owned binding manifest.

    The import is lazy to keep this module independent from capability-manifest construction.
    """

    if binding_manifest is None:
        from .capability_manifest import build_default_binding_manifest

        binding_manifest = build_default_binding_manifest()
    if not hasattr(binding_manifest, "bindings"):
        raise ReachabilityError("binding_manifest must expose bindings")

    entries: list[ReachabilityEntry] = []
    role_producers = {
        "first_frame": "explicit first-frame image role",
        "last_frame": "explicit last-frame image role",
        "paired_audios": "positional paired-video-audio role",
        "images": "generic reference image role",
        "videos": "generic reference video role",
        "audios": "standalone audio-source role",
    }
    for binding in binding_manifest.bindings:
        if binding.binding_kind.value != "input":
            continue
        socket_type = binding.socket_type
        failure = _TYPED_INPUT_FAILURES.get(socket_type)
        unavailable = _EXPLICIT_UNAVAILABLE_INPUTS.get(socket_type)
        if binding.node_id.endswith("ReferenceRegistry") and binding.port_name in role_producers:
            role_producer = ProducerPath(binding.node_id, binding.port_name)
            entries.append(
                _binding_entry(
                    node_id=binding.node_id,
                    port_name=binding.port_name,
                    stage=ReachabilityStage.COMPILER,
                    disposition=ReachabilityDisposition.REACHABLE,
                    description=role_producers[binding.port_name],
                    producer=role_producer,
                )
            )
            continue
        if unavailable is not None:
            entries.append(
                _binding_entry(
                    node_id=binding.node_id,
                    port_name=binding.port_name,
                    stage=ReachabilityStage.COMPILER,
                    disposition=ReachabilityDisposition.EXPLICIT_UNAVAILABLE,
                    description=(
                        "A public producer exposes the current typed unavailable state without "
                        "claiming a renderable timeline."
                    ),
                    producer=ProducerPath(unavailable[0], unavailable[1]),
                    failure_code=unavailable[2],
                    normal_use=False,
                )
            )
            continue
        if failure is not None:
            entries.append(
                _binding_entry(
                    node_id=binding.node_id,
                    port_name=binding.port_name,
                    stage=(
                        ReachabilityStage.CONTEXT_IR
                        if socket_type == "H3_CONTEXT_IR_MEDIA"
                        else ReachabilityStage.COMPILER
                    ),
                    disposition=ReachabilityDisposition.UNSUPPORTED,
                    description=(
                        "No normal public producer is shipped; injected values remain an explicit "
                        "test/host seam only."
                    ),
                    failure_code=failure,
                    normal_use=False,
                )
            )
            continue
        input_producer = _producer_for_input(binding.node_id, socket_type, binding.port_name)
        if input_producer is None:
            entries.append(
                _binding_entry(
                    node_id=binding.node_id,
                    port_name=binding.port_name,
                    stage=ReachabilityStage.COMPILER,
                    disposition=ReachabilityDisposition.UNSUPPORTED,
                    description="The public binding has no named deterministic producer.",
                    failure_code="producer_unavailable",
                    normal_use=False,
                )
            )
        else:
            entries.append(
                _binding_entry(
                    node_id=binding.node_id,
                    port_name=binding.port_name,
                    stage=ReachabilityStage.COMPILER,
                    disposition=ReachabilityDisposition.REACHABLE,
                    description="Named public producer path",
                    producer=input_producer,
                )
            )

    entries.extend(
        _mode_entry(
            stage=ReachabilityStage.CONTEXT_IR,
            mode=mode,
            disposition=ReachabilityDisposition.INJECTED_ONLY,
            description=(
                "Official Context-IR mapping is typed but requires an injected transport "
                "and media seam."
            ),
            producer=ProducerPath(
                "comfyui_h3_context.H3Context.OfficialContextIR",
                "execute",
                ReachabilitySource.INJECTED_FIXTURE,
            ),
            normal_use=False,
        )
        for mode in TaskMode
    )
    entries.extend(
        _mode_entry(
            stage=ReachabilityStage.COMPILER,
            mode=mode,
            disposition=ReachabilityDisposition.REACHABLE,
            description=(
                "Deterministic compiler path is available through Request, Registry, Plan, "
                "and Compiler."
            ),
            producer=ProducerPath("comfyui_h3_context.H3Context.Compiler", "prompt"),
        )
        for mode in TaskMode
    )
    native_supported = {TaskMode.T2VA, TaskMode.FL2VA, TaskMode.REF2VA}
    entries.extend(
        _mode_entry(
            stage=ReachabilityStage.NATIVE_GENERATION,
            mode=mode,
            disposition=(
                ReachabilityDisposition.REACHABLE
                if mode in native_supported
                else ReachabilityDisposition.UNSUPPORTED
            ),
            description=(
                "Declarative native H3 mapping with direct media flow"
                if mode in native_supported
                else "Pinned native H3 adapter has no mapping for this task mode"
            ),
            producer=(
                ProducerPath("comfyui_h3_context.H3Context.NativeH3Adapter", "native_h3_wiring")
                if mode in native_supported
                else None
            ),
            failure_code=None if mode in native_supported else "unsupported_native_task_mode",
            normal_use=mode in native_supported,
        )
        for mode in TaskMode
    )
    # IMPORTANT: a mode route describes mapping reachability, not every asset composition's
    # runtime qualification. Audio references use the same native Ref2VA route.
    return ReachabilityManifest(tuple(entries))


def validate_binding_coverage(
    binding_manifest: BindingManifest,
    reachability: ReachabilityManifest,
) -> None:
    """Fail closed if a public input lacks exactly one reachability disposition."""

    if not isinstance(reachability, ReachabilityManifest):
        raise ReachabilityError("reachability must be a ReachabilityManifest")
    for binding in binding_manifest.bindings:
        if binding.binding_kind.value != "input":
            continue
        reachability.for_binding(binding.node_id, binding.port_name)


def require_task_mode(
    manifest: ReachabilityManifest,
    stage: ReachabilityStage,
    mode: TaskMode,
) -> ReachabilityEntry:
    """Return a normal task-mode route or raise its declared failure code."""

    if not isinstance(manifest, ReachabilityManifest):
        raise ReachabilityError("manifest must be a ReachabilityManifest")
    if not isinstance(stage, ReachabilityStage) or not isinstance(mode, TaskMode):
        raise ReachabilityError("stage and mode must use their closed enums")
    matches = tuple(
        item
        for item in manifest.entries
        if item.stage is stage and item.task_mode is mode and item.binding_node_id is None
    )
    if len(matches) != 1:
        raise ReachabilityError("reachability matrix has no unique task-mode entry")
    selected = matches[0]
    if selected.disposition is not ReachabilityDisposition.REACHABLE:
        raise ReachabilityError(selected.failure_code or "task_mode_unsupported")
    return selected


__all__ = [
    "MAX_REACHABILITY_ENTRIES",
    "ProducerPath",
    "ReachabilityDisposition",
    "ReachabilityEntry",
    "ReachabilityError",
    "ReachabilityManifest",
    "ReachabilitySource",
    "ReachabilityStage",
    "REACHABILITY_SCHEMA",
    "build_default_reachability_manifest",
    "require_task_mode",
    "validate_binding_coverage",
]
