"""Bounded CPU/loopback H0-H2 driver for the pinned ComfyUI host.

The driver talks to ComfyUI only through its public HTTP API. It never imports the host into the
project process, never installs dependencies, and never loads a model or provider. The default
workflow fixtures contain native H3 output nodes; the model-free queue projection removes those
weight-backed nodes and terminates the canonical context pipeline at host ``PreviewAny`` nodes so
that prompt/report execution is still exercised.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

# IMPORTANT: isolated host Python starts this file with only scripts/ on sys.path; keep the pure
# contract import bound to the exact clean repository snapshot paired with the tested artifact.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.capability_manifest import (  # noqa: E402
    PUBLIC_NODE_IDS,
    HostCanaryOutcome,
    HostCanarySeam,
    build_host_canary_report,
)
from comfyui_h3_context.core.workflow_migration import (  # noqa: E402
    MigrationDisposition,
    audit_workflow_migration,
)
from scripts.build_gate_report import BuildGateReportError, _extract_plain_members  # noqa: E402

DEFAULT_HOST_ROOT = ROOT / "reference" / "ComfyUI"
EXPECTED_HOST_REVISION = "b323a345bbbfb2f3a95b5b73b68eb7919a26515e"  # pragma: allowlist secret
EXPECTED_HOST_VERSION = "0.32.0"
LATEST_COMPATIBLE_HOST_REVISION = (
    "34744cd29eacea9bbdec17e628a81c2ce0737d16"  # pragma: allowlist secret
)
LATEST_COMPATIBLE_HOST_VERSION = "0.31.0"
FIXTURE_SCHEMA = "h3-context-workflow-fixture/1"
ASSISTANT_FIXTURE_SCHEMA = "h3-context-workflow-fixture/2"
SUPPORTED_FIXTURE_SCHEMAS = frozenset({FIXTURE_SCHEMA, ASSISTANT_FIXTURE_SCHEMA})
PROJECT_NODE_IDS = frozenset(
    {
        "comfyui_h3_context.H3Context.Request",
        "comfyui_h3_context.H3Context.Plan",
        "comfyui_h3_context.H3Context.Compiler",
        "comfyui_h3_context.H3Context.Validator",
        "comfyui_h3_context.H3Context.NativeH3Adapter",
    }
)
PRODUCT_SHELL_NODE_ID = "comfyui_h3_context.H3Context.ProductShell"
REFERENCE_NODE_ID = "comfyui_h3_context.H3Context.ReferenceRegistry"
PREVIEW_NODE_ID = "comfyui_h3_context.H3Context.Preview"
PROVIDER_TRANSPARENCY_NODE_ID = "comfyui_h3_context.H3Context.ProviderTransparency"
RELIABILITY_NODE_ID = "comfyui_h3_context.H3Context.Reliability"
MEDIA_ADMISSION_NODE_ID = "comfyui_h3_context.H3Context.MediaAdmissionProducer"
VISUAL_PERCEPTION_NODE_ID = "comfyui_h3_context.H3Context.VisualPerceptionProducer"
AUDIO_PERCEPTION_NODE_ID = "comfyui_h3_context.H3Context.AudioPerceptionProducer"
DOWNSTREAM_PRODUCER_NODE_IDS = frozenset(
    {
        "comfyui_h3_context.H3Context.HardConstraintProducer",
        "comfyui_h3_context.H3Context.IntentGraphProducer",
        "comfyui_h3_context.H3Context.EvidenceFusionProducer",
        "comfyui_h3_context.H3Context.CrossReferenceProducer",
        "comfyui_h3_context.H3Context.DirectiveAuthorityProducer",
        "comfyui_h3_context.H3Context.FullReferenceTimelineProducer",
    }
)
H2_CANARY_NODE_ID = "comfyui_h3_context.H3Context.MediaLifecycleCanary"
HOST_REPORT_SCHEMA = "h3-context-host-e2e/1"
FULL_REFERENCE_NODE_ID = "comfyui_h3_context.H3Context.FullReference"
NATIVE_OUTPUT_NODE_IDS = frozenset({"MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"})
NATIVE_NODE_IDS = NATIVE_OUTPUT_NODE_IDS | {"MiniMaxH3SigmaShift"}
EXPECTED_NATIVE_SOURCE_BLOB = "0b1840e851c248f89e9920159c3c8237fa2e7186"  # pragma: allowlist secret
LATEST_COMPATIBLE_NATIVE_SOURCE_BLOB = (
    "0b1840e851c248f89e9920159c3c8237fa2e7186"  # pragma: allowlist secret
)
MAX_NATIVE_SOURCE_BYTES = 131_072
SUPPORTED_FIXTURE_MODES = (
    "base",
    "reference",
    "full_reference",
    "audit_override",
    "provider_transparency",
    "reliability",
    "perception_producers",
    "downstream_producers",
    "local_reconstruction",
    "product_shell_base",
    "product_shell_reference",
    "assistant_base",
    "assistant_reference",
    "media_lifecycle",
    "native_mode_contract",
)
M7_FIXTURE_OBSERVER_COUNTS = {
    "m7-03-audit-override": 6,
    "m7-04-provider-transparency": 6,
    "m7-05-reliability": 7,
}
MAX_LOG_BYTES = 16_384
MAX_BROWSER_PERFORMANCE_RECEIPT_BYTES = 8_192
MAX_JSON_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60.0
PROJECT_SDIST_ROOT_PATTERN = re.compile(r"^minimax_h3_context-[0-9]+\.[0-9]+\.[0-9]+$")
HTTP_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.25
PRIVATE_MARKERS = (
    "authorization",
    "bearer ",
    "api_key",
    "password",
    "secret",
    "token=",
    "sig=",
    "http://",
    "https://",
)
HOST_REQUIRED_MODULES = (
    "torch",
    "torchaudio",
    "aiohttp",
    "numpy",
    "PIL",
    "transformers",
    "safetensors",
    "av",
    "psutil",
    "comfy_aimdo",
    "comfy_kitchen",
    "sqlalchemy",
    "yaml",
    "einops",
    "simpleeval",
    "blake3",
)
HOST_DEPENDENCY_DISTRIBUTIONS = {
    "torch": "torch",
    "torchaudio": "torchaudio",
    "aiohttp": "aiohttp",
    "numpy": "numpy",
    "PIL": "Pillow",
    "transformers": "transformers",
    "safetensors": "safetensors",
    "av": "av",
    "psutil": "psutil",
    "comfy_aimdo": "comfy-aimdo",
    "comfy_kitchen": "comfy-kitchen",
    "sqlalchemy": "SQLAlchemy",
    "yaml": "PyYAML",
    "einops": "einops",
    "simpleeval": "simpleeval",
    "blake3": "blake3",
}

JsonObject = dict[str, Any]


class HostDriverError(RuntimeError):
    """A host assertion or bounded lifecycle operation failed."""

    def __init__(self, message: str, *, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.evidence = dict(evidence or {})


class HostBlockedError(HostDriverError):
    """The named host prerequisite is unavailable without an unsafe fallback."""


class _OwnedHostTemporaryRoot:
    """Delete exactly one driver-owned host root and fail closed if deletion is incomplete."""

    def __init__(self, planning_root: Path) -> None:
        self._planning_root = planning_root.resolve()
        self._path = Path(tempfile.mkdtemp(prefix="m3-08-host-", dir=self._planning_root)).resolve()
        created = os.stat(self._path, follow_symlinks=False)
        self._identity = (created.st_dev, created.st_ino)
        # CRITICAL: filesystems may immediately reuse a directory inode after replacement;
        # a private owner marker closes that false-negative cleanup path without treating normal
        # child-file activity as an identity change.
        self._owner_marker = self._path / ".h3-context-owned-root"
        self._owner_token = secrets.token_hex(32)
        self._owner_marker.write_text(self._owner_token, encoding="ascii")

    @property
    def path(self) -> Path:
        return self._path

    def __enter__(self) -> Path:
        return self._path

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> Literal[False]:
        del exc_type, traceback
        cleanup_failure: str | None = None
        cleanup_failure_code: str | None = None
        cleanup_cause: OSError | None = None
        try:
            current = os.stat(self._path, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        except OSError as identity_error:
            current = None
            cleanup_cause = identity_error
            cleanup_failure = (
                f"owned temp root identity check raised {type(identity_error).__name__}"
            )
            cleanup_failure_code = type(identity_error).__name__

        if cleanup_failure is None:
            if current is None:
                return False
            if (
                self._path.parent != self._planning_root
                or self._path.is_symlink()
                or (current.st_dev, current.st_ino) != self._identity
                or not self._owner_marker_is_intact()
            ):
                cleanup_failure = "owned temp root identity changed before cleanup"
                cleanup_failure_code = "identity_changed"
            else:
                try:
                    shutil.rmtree(self._path)
                except OSError as cleanup_error:
                    cleanup_cause = cleanup_error
                    cleanup_failure = (
                        f"owned temp root cleanup raised {type(cleanup_error).__name__}"
                    )
                    cleanup_failure_code = type(cleanup_error).__name__
                else:
                    if self._path.exists():
                        cleanup_failure = "owned temp root remained after cleanup"
                        cleanup_failure_code = "retained_root"

        if cleanup_failure is None:
            return False

        evidence = dict(exc.evidence) if isinstance(exc, HostDriverError) else {}
        evidence.update(
            {
                "cleanup": "FAIL",
                "cleanup_failure": cleanup_failure_code,
            }
        )
        parts = []
        if exc is not None:
            parts.append(f"first_failure={exc}")
        parts.append(f"cleanup_failure={cleanup_failure}")
        # CRITICAL: cleanup denial must not be hidden or replace the first execution failure.
        raise HostDriverError("; ".join(parts), evidence=evidence) from (exc or cleanup_cause)

    def _owner_marker_is_intact(self) -> bool:
        try:
            metadata = os.stat(self._owner_marker, follow_symlinks=False)
            return (
                stat.S_ISREG(metadata.st_mode)
                and not self._owner_marker.is_symlink()
                and self._owner_marker.read_text(encoding="ascii") == self._owner_token
            )
        except (OSError, UnicodeError):
            return False


def _project_version(subject_root: Path) -> str:
    """Read the deployed subject version without importing untrusted host/runtime code."""

    project_file = subject_root / "pyproject.toml"
    if not project_file.is_file():
        raise HostBlockedError("deployed subject omits pyproject.toml")
    match = re.search(
        r'(?m)^version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$',
        project_file.read_text(encoding="utf-8"),
    )
    if match is None:
        raise HostBlockedError("deployed subject version is unavailable")
    return match.group(1)


def build_exact_host_canary_report(
    host_version: str,
    host_revision: str,
    *,
    registered_project_ids: int,
    browser_supported: bool = False,
    expected_version: str = EXPECTED_HOST_VERSION,
    expected_revision: str = EXPECTED_HOST_REVISION,
    authority: str = "supported_b323",
) -> JsonObject:
    """Bind a selected real-browser observation to one exact live host run."""

    if host_version != expected_version or host_revision != expected_revision:
        raise HostDriverError("frontend canary requires the selected exact host pin")
    if authority not in {"supported_b323", "latest_compatible_observation"}:
        raise HostDriverError("frontend canary authority is invalid")
    expected_backend_count = len(_expected_registration_ids() - NATIVE_NODE_IDS)
    if registered_project_ids != expected_backend_count:
        raise HostDriverError("frontend canary requires complete node-only registration")
    if browser_supported:
        details = {
            seam: (HostCanaryOutcome.SUPPORTED, "exact supported frontend browser probe passed")
            for seam in HostCanarySeam
        }
    else:
        # IMPORTANT: an unselected browser lane is not evidence that the extension is absent.
        details = {
            seam: (
                HostCanaryOutcome.NOT_RUN,
                "browser frontend seam was not probed by the selected host lane",
            )
            for seam in HostCanarySeam
        }
    profile = f"comfyui-{host_version}@{host_revision}"
    report = cast(JsonObject, build_host_canary_report(profile, details).to_wire())
    report["authority"] = authority
    return report


def _frontend_fallback_dispositions(browser_result: JsonObject | None) -> JsonObject:
    """Report only browser execution states proven by the retained browser assertions."""

    assertions = browser_result.get("assertions", []) if browser_result is not None else []
    if not isinstance(assertions, list):
        assertions = []
    return {
        "node_only": "supported",
        "app_mode": (
            "executed_live" if "app_mode_execution_parity" in assertions else "not_probed"
        ),
        "subgraph": (
            "executed_live" if "subgraph_execution_parity" in assertions else "not_probed"
        ),
    }


def _deploy_subject(*, subject_path: Path, staging_path: Path, artifact_path: Path) -> str:
    """Validate and deploy one exact source distribution without a privileged symlink."""

    if not artifact_path.is_file() or not artifact_path.name.endswith(".tar.gz"):
        raise HostBlockedError("exact host artifact must be an available source distribution")
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    with tarfile.open(artifact_path, "r:gz") as archive:
        members = archive.getmembers()
        normalized = [member.name.replace("\\", "/") for member in members]
        if any(name.startswith("/") or name == ".." or "../" in name for name in normalized):
            raise HostBlockedError("exact host artifact contains an unsafe path")
        if any(
            member.issym() or member.islnk() or member.isdev() or member.isfifo()
            for member in members
        ):
            raise HostBlockedError("exact host artifact contains an unsafe member type")
        roots = {name.split("/", 1)[0] for name in normalized if name}
        if len(roots) != 1:
            raise HostBlockedError("exact host artifact has an unexpected root")
        artifact_root = next(iter(roots))
        if not PROJECT_SDIST_ROOT_PATTERN.fullmatch(artifact_root):
            raise HostBlockedError("exact host artifact has an unexpected root")
        try:
            _extract_plain_members(archive, members, staging_path)
        except BuildGateReportError as error:
            raise HostBlockedError("exact host artifact failed safe extraction") from error
    extracted = staging_path / artifact_root
    if not extracted.is_dir():
        raise HostBlockedError("exact host artifact did not produce the expected root")
    shutil.copytree(extracted, subject_path)
    return artifact_sha256


class HostHTTPError(HostDriverError):
    """An HTTP request returned a non-success status."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"host HTTP {status}: {body}")


def _as_object(value: object, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return cast(JsonObject, value)


def _join_parts(value: object, field: str) -> str:
    if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
        raise ValueError(f"{field} must be a list of string parts")
    return "".join(cast(list[str], value))


def load_fixture(path: Path) -> JsonObject:
    """Load one accepted JSON fixture without executing any values from it."""

    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load workflow fixture {path.name}") from exc
    return _as_object(value, "fixture")


def validate_fixture(
    fixture: Mapping[str, Any],
    *,
    expected_version: str = EXPECTED_HOST_VERSION,
    expected_revision: str = EXPECTED_HOST_REVISION,
) -> None:
    """Validate the bounded, pinned, model-free fixture envelope."""

    fixture_schema = fixture.get("schema")
    if fixture_schema not in SUPPORTED_FIXTURE_SCHEMAS:
        raise ValueError("unsupported workflow fixture schema")
    if fixture.get("fixture_status") != "static_model_free_contract":
        raise ValueError("workflow fixture is not a static model-free contract")
    if fixture.get("workflow_format") != "comfyui_api_prompt_v1":
        raise ValueError("unsupported workflow fixture format")
    host = _as_object(fixture.get("host"), "fixture.host")
    if host.get("version") != expected_version:
        raise ValueError("workflow fixture host version is not pinned")
    if _join_parts(host.get("revision_parts"), "fixture.host.revision_parts") != expected_revision:
        raise ValueError("workflow fixture host revision is not pinned")
    prompt = _as_object(fixture.get("prompt"), "fixture.prompt")
    node_types = {
        _as_object(node, f"fixture.prompt.{node_id}").get("class_type")
        for node_id, node in prompt.items()
    }
    if fixture.get("fixture_id") == "m15-01-perception-producers":
        required = {
            "LoadImage",
            "LoadAudio",
            MEDIA_ADMISSION_NODE_ID,
            VISUAL_PERCEPTION_NODE_ID,
            AUDIO_PERCEPTION_NODE_ID,
        }
        if not required.issubset(node_types):
            raise ValueError("perception fixture omits its public producer path")
        class_types = [
            _as_object(node, f"fixture.prompt.{node_id}").get("class_type")
            for node_id, node in prompt.items()
        ]
        if class_types.count(MEDIA_ADMISSION_NODE_ID) != 2:
            raise ValueError("perception fixture requires two media admissions")
        expected = _as_object(fixture.get("expected"), "fixture.expected")
        if (
            expected.get("media_disposition") != "complete"
            or expected.get("visual_disposition") != "unavailable"
            or expected.get("audio_disposition") != "unavailable"
            or expected.get("automatic_fallback") is not False
            or expected.get("fingerprint_claim") != "caller_declared_unverified"
        ):
            raise ValueError("perception fixture expected projection is invalid")
        serialized = json.dumps(fixture, sort_keys=True).casefold()
        if any(marker in serialized for marker in PRIVATE_MARKERS):
            raise ValueError("workflow fixture contains a private/provider marker")
        return
    if fixture.get("fixture_id") == "m15-02-downstream-producers":
        required = {
            "LoadImage",
            "comfyui_h3_context.H3Context.Request",
            "comfyui_h3_context.H3Context.ReferenceRegistry",
            MEDIA_ADMISSION_NODE_ID,
            *DOWNSTREAM_PRODUCER_NODE_IDS,
        }
        if not required.issubset(node_types):
            raise ValueError("downstream fixture omits its public producer path")
        expected = _as_object(fixture.get("expected"), "fixture.expected")
        if (
            expected.get("prebuilt_python_results") is not False
            or expected.get("full_reference_timeline") != "explicit_unavailable"
            or expected.get("automatic_fallback") is not False
            or expected.get("evidence_claim") != "caller_declared_unverified"
        ):
            raise ValueError("downstream fixture expected projection is invalid")
        serialized = json.dumps(fixture, sort_keys=True).casefold()
        if any(marker in serialized for marker in PRIVATE_MARKERS):
            raise ValueError("workflow fixture contains a private/provider marker")
        return
    if fixture.get("fixture_id") == "m13-10-local-reconstruction":
        required = {
            "LoadImage",
            "comfyui_h3_context.H3Context.Request",
            "comfyui_h3_context.H3Context.ReferenceRegistry",
            MEDIA_ADMISSION_NODE_ID,
            *(
                DOWNSTREAM_PRODUCER_NODE_IDS
                - {"comfyui_h3_context.H3Context.FullReferenceTimelineProducer"}
            ),
            "comfyui_h3_context.H3Context.Plan",
            "comfyui_h3_context.H3Context.SourceProfiledRenderer",
            "comfyui_h3_context.H3Context.Validator",
            "comfyui_h3_context.H3Context.NativeH3Adapter",
            "comfyui_h3_context.H3Context.LocalReconstruction",
        }
        if not required.issubset(node_types):
            raise ValueError("local reconstruction fixture omits its public route")
        expected = _as_object(fixture.get("expected"), "fixture.expected")
        if (
            expected.get("prebuilt_python_results") is not False
            or expected.get("route") != "deterministic_manual"
            or expected.get("disposition") != "complete"
            or expected.get("automatic_fallback") is not False
            or expected.get("provider_calls") != 0
            or expected.get("model_loads") != 0
            or expected.get("uploads") != 0
        ):
            raise ValueError("local reconstruction fixture expected projection is invalid")
        serialized = json.dumps(fixture, sort_keys=True).casefold()
        if any(marker in serialized for marker in PRIVATE_MARKERS):
            raise ValueError("workflow fixture contains a private/provider marker")
        return
    if not PROJECT_NODE_IDS.issubset(node_types):
        raise ValueError("workflow fixture omits a canonical H3 pipeline node")
    if fixture_schema == ASSISTANT_FIXTURE_SCHEMA and not str(
        fixture.get("fixture_id", "")
    ).startswith("m15-09-assistant-"):
        raise ValueError("schema 2 is reserved for the M15-09 Assistant fixtures")
    if fixture.get("fixture_id") == "m7-03-audit-override" and (
        "comfyui_h3_context.H3Context.AuditOverride" not in node_types
    ):
        raise ValueError("audit override fixture omits the AuditOverride node")
    if fixture.get("fixture_id") == "m7-04-provider-transparency" and (
        PROVIDER_TRANSPARENCY_NODE_ID not in node_types
    ):
        raise ValueError("provider transparency fixture omits the ProviderTransparency node")
    if fixture.get("fixture_id") == "m7-05-reliability" and RELIABILITY_NODE_ID not in node_types:
        raise ValueError("reliability fixture omits the Reliability node")
    expected = _as_object(fixture.get("expected"), "fixture.expected")
    projection = _as_object(expected.get("output_projection"), "fixture.expected.output_projection")
    native_node = projection.get("native_node")
    if not isinstance(native_node, str) or native_node not in prompt:
        raise ValueError("workflow fixture native output projection is invalid")
    native_type = _as_object(prompt[native_node], "fixture native node").get("class_type")
    if native_type not in NATIVE_OUTPUT_NODE_IDS:
        raise ValueError("workflow fixture does not point to a pinned native H3 node")
    validator_id = _node_id(prompt, "comfyui_h3_context.H3Context.Validator")
    validated_report = [validator_id, 1]
    for class_type in (PREVIEW_NODE_ID, "comfyui_h3_context.H3Context.NativeH3Adapter"):
        consumer_id = _node_id(prompt, class_type)
        consumer = _as_object(prompt[consumer_id], f"fixture {class_type} node")
        inputs = _as_object(consumer.get("inputs"), f"fixture {class_type} inputs")
        if inputs.get("report") != validated_report:
            raise ValueError(f"{class_type} must consume the Validator validated report")
    if projection.get("report") != validated_report:
        raise ValueError("workflow output projection must expose the Validator validated report")
    product_shell_ids = [
        node_id
        for node_id, node in prompt.items()
        if _as_object(node, f"prompt.{node_id}").get("class_type") == PRODUCT_SHELL_NODE_ID
    ]
    if product_shell_ids:
        if len(product_shell_ids) != 1:
            raise ValueError("workflow fixture must contain exactly one ProductShell node")
        shell_id = product_shell_ids[0]
        shell_inputs = _as_object(prompt[shell_id].get("inputs"), "fixture ProductShell inputs")
        adapter_id = _node_id(prompt, "comfyui_h3_context.H3Context.NativeH3Adapter")
        if shell_inputs != {
            "report": validated_report,
            "native_h3_wiring": [adapter_id, 1],
        }:
            raise ValueError("ProductShell must consume exact report and native wiring")
        native_inputs = _as_object(prompt[native_node].get("inputs"), "fixture native inputs")
        if native_inputs.get("prompt") != [shell_id, 0]:
            raise ValueError("native node must consume the ProductShell standard STRING")
    serialized = json.dumps(fixture, sort_keys=True).casefold()
    if any(marker in serialized for marker in PRIVATE_MARKERS):
        raise ValueError("workflow fixture contains a private/provider marker")


def _node_id(prompt: Mapping[str, Any], class_type: str) -> str:
    matches = [
        node_id
        for node_id, node in prompt.items()
        if _as_object(node, f"prompt.{node_id}").get("class_type") == class_type
    ]
    if len(matches) != 1:
        raise ValueError(f"fixture must contain exactly one {class_type} node")
    return matches[0]


def build_model_free_prompt(fixture: Mapping[str, Any]) -> JsonObject:
    """Replace weight-backed native outputs with inspectable host output nodes.

    The canonical H3 nodes, links, request values, and local reference loaders remain unchanged.
    Only native H3 execution is removed because it would require absent weights/encoders. The
    prompt/report/validation outputs are observed through the pinned host's ``PreviewAny`` node.
    """

    validate_fixture(fixture)
    if fixture.get("fixture_id") == "m15-01-perception-producers":
        return build_model_free_perception_prompt(fixture)
    if fixture.get("fixture_id") == "m15-02-downstream-producers":
        return build_model_free_downstream_prompt(fixture)
    if fixture.get("fixture_id") == "m13-10-local-reconstruction":
        return build_model_free_local_reconstruction_prompt(fixture)
    source_prompt = _as_object(fixture.get("prompt"), "fixture.prompt")
    prompt: JsonObject = {
        str(node_id): dict(_as_object(node, f"fixture.prompt.{node_id}"))
        for node_id, node in source_prompt.items()
    }
    native_ids = {
        node_id
        for node_id, node in prompt.items()
        if _as_object(node, f"prompt.{node_id}").get("class_type") in NATIVE_NODE_IDS
    }
    for node_id in native_ids:
        del prompt[node_id]

    adapter_id = _node_id(prompt, "comfyui_h3_context.H3Context.NativeH3Adapter")
    compiler_id = _node_id(prompt, "comfyui_h3_context.H3Context.Compiler")
    validator_id = _node_id(prompt, "comfyui_h3_context.H3Context.Validator")
    override_ids = [
        node_id
        for node_id, node in prompt.items()
        if _as_object(node, f"prompt.{node_id}").get("class_type")
        == "comfyui_h3_context.H3Context.AuditOverride"
    ]
    report_source = (override_ids[0], 1) if len(override_ids) == 1 else (compiler_id, 1)
    numeric_ids = [int(node_id) for node_id in prompt if node_id.isdecimal()]
    next_id = max(numeric_ids, default=0) + 1

    preview_ids = [
        node_id
        for node_id, node in prompt.items()
        if _as_object(node, f"prompt.{node_id}").get("class_type") == PREVIEW_NODE_ID
    ]
    if len(preview_ids) > 1:
        raise ValueError("fixture must contain at most one H3 Context Preview node")
    if preview_ids:
        preview_id = preview_ids[0]
    else:
        preview_id = str(next_id)
        prompt[preview_id] = {
            "class_type": PREVIEW_NODE_ID,
            "inputs": {"report": list(report_source)},
        }
        next_id += 1

    transparency_ids = [
        node_id
        for node_id, node in prompt.items()
        if _as_object(node, f"prompt.{node_id}").get("class_type") == PROVIDER_TRANSPARENCY_NODE_ID
    ]
    reliability_ids = [
        node_id
        for node_id, node in prompt.items()
        if _as_object(node, f"prompt.{node_id}").get("class_type") == RELIABILITY_NODE_ID
    ]

    observed_sources: tuple[tuple[str, int], ...] = (
        (adapter_id, 0),
        (preview_id, 1),
        (validator_id, 0),
    )
    product_shell_ids = [
        node_id
        for node_id, node in prompt.items()
        if _as_object(node, f"prompt.{node_id}").get("class_type") == PRODUCT_SHELL_NODE_ID
    ]
    if product_shell_ids:
        if len(product_shell_ids) != 1:
            raise ValueError("fixture must contain at most one ProductShell node")
        observed_sources += ((product_shell_ids[0], 0), (product_shell_ids[0], 1))
    if len(override_ids) > 1:
        raise ValueError("fixture must contain at most one AuditOverride node")
    if override_ids:
        override_id = override_ids[0]
        observed_sources += ((override_id, 0), (override_id, 1), (override_id, 2))
    if len(transparency_ids) > 1:
        raise ValueError("fixture must contain at most one ProviderTransparency node")
    if transparency_ids:
        transparency_id = transparency_ids[0]
        observed_sources += (
            (transparency_id, 0),
            (transparency_id, 1),
            (transparency_id, 2),
        )
    if len(reliability_ids) > 1:
        raise ValueError("fixture must contain at most one Reliability node")
    if reliability_ids:
        reliability_id = reliability_ids[0]
        observed_sources += (
            (reliability_id, 0),
            (reliability_id, 1),
            (reliability_id, 2),
            (reliability_id, 3),
        )
    for source_id, source_output in observed_sources:
        prompt[str(next_id)] = {
            "class_type": "PreviewAny",
            "inputs": {"source": [source_id, source_output]},
        }
        next_id += 1
    return prompt


def build_model_free_perception_prompt(fixture: Mapping[str, Any]) -> JsonObject:
    """Expose every M15-01 result through source-bound host observers."""

    validate_fixture(fixture)
    source_prompt = _as_object(fixture.get("prompt"), "fixture.prompt")
    prompt: JsonObject = {
        str(node_id): dict(_as_object(node, f"fixture.prompt.{node_id}"))
        for node_id, node in source_prompt.items()
    }
    numeric_ids = [int(node_id) for node_id in prompt if node_id.isdecimal()]
    next_id = max(numeric_ids, default=0) + 1
    for source in (("2", 0), ("3", 0), ("5", 0), ("6", 0)):
        prompt[str(next_id)] = {
            "class_type": "PreviewAny",
            "inputs": {"source": [source[0], source[1]]},
        }
        next_id += 1
    return prompt


def build_model_free_downstream_prompt(fixture: Mapping[str, Any]) -> JsonObject:
    """Expose every M15-02 stage through source-bound host observers."""

    validate_fixture(fixture)
    source_prompt = _as_object(fixture.get("prompt"), "fixture.prompt")
    prompt: JsonObject = {
        str(node_id): dict(_as_object(node, f"fixture.prompt.{node_id}"))
        for node_id, node in source_prompt.items()
    }
    numeric_ids = [int(node_id) for node_id in prompt if node_id.isdecimal()]
    next_id = max(numeric_ids, default=0) + 1
    observed_sources = (
        ("4", 0),
        ("4", 1),
        ("5", 0),
        ("5", 1),
        ("6", 0),
        ("7", 0),
        ("7", 1),
        ("8", 0),
        ("8", 1),
        ("9", 0),
        ("9", 1),
        ("10", 0),
        ("10", 1),
    )
    for source_id, source_output in observed_sources:
        prompt[str(next_id)] = {
            "class_type": "PreviewAny",
            "inputs": {"source": [source_id, source_output]},
        }
        next_id += 1
    return prompt


def build_model_free_local_reconstruction_prompt(
    fixture: Mapping[str, Any],
) -> JsonObject:
    """Expose the complete deterministic local-reconstruction route to the pinned host."""

    validate_fixture(fixture)
    source_prompt = _as_object(fixture.get("prompt"), "fixture.prompt")
    prompt: JsonObject = {
        str(node_id): dict(_as_object(node, f"fixture.prompt.{node_id}"))
        for node_id, node in source_prompt.items()
    }
    numeric_ids = [int(node_id) for node_id in prompt if node_id.isdecimal()]
    next_id = max(numeric_ids, default=0) + 1
    observed_sources = (
        ("11", 0),
        ("11", 1),
        ("11", 2),
        ("12", 0),
        ("12", 1),
        ("13", 1),
        ("15", 0),
        ("16", 0),
        ("17", 0),
        ("17", 1),
        ("14", 0),
        ("14", 1),
        ("14", 2),
    )
    for source_id, source_output in observed_sources:
        prompt[str(next_id)] = {
            "class_type": "PreviewAny",
            "inputs": {"source": [source_id, source_output]},
        }
        next_id += 1
    return prompt


def build_model_free_reference_prompt(fixture: Mapping[str, Any]) -> JsonObject:
    """Build a host-valid two-reference projection without reading fixture media files.

    The static M3-07 API fixture records the intended two direct native links, but a V1
    ``INPUT_IS_LIST`` input cannot be represented by a nested list of link pairs in the pinned
    host API.  This projection uses the host's model-free ``EmptyImage`` and
    ``SplitImageToTileList`` nodes to produce two ordered IMAGE outputs behind one valid
    ``OUTPUT_IS_LIST`` link.  The canonical pipeline and report assertions remain unchanged.
    """

    validate_fixture(fixture)
    if _as_object(fixture.get("expected"), "fixture.expected").get("task_mode") != "ref2va":
        raise ValueError("reference host projection requires the ref2va fixture")
    prompt = build_model_free_prompt(fixture)
    prompt["2"] = {
        "class_type": "EmptyImage",
        "inputs": {"width": 128, "height": 64, "batch_size": 1, "color": 0},
    }
    prompt["3"] = {
        "class_type": "SplitImageToTileList",
        "inputs": {
            "image": ["2", 0],
            "tile_width": 64,
            "tile_height": 64,
            "overlap": 0,
        },
    }
    prompt["4"]["inputs"] = {"images": ["3", 0]}
    return prompt


def build_model_free_full_reference_prompt(fixture: Mapping[str, Any]) -> JsonObject:
    """Remove only weight-backed output from the M6-07 complex media graph.

    Unlike the M3 reference projection, this path preserves LoadVideo/GetVideoComponents and
    LoadAudio so the pinned host executes the original media-to-native edge topology before the
    native conditioning node is removed.  The V1 API cannot encode multiple links directly in an
    ``INPUT_IS_LIST`` socket (it interprets a nested list as an unhashable link), so the two image
    loaders are batched and then split into a real ``OUTPUT_IS_LIST`` using the pinned host's
    ``RebatchImages`` node.  This preserves both source images and their deterministic order while
    keeping the generated prompt host-valid.
    """

    validate_fixture(fixture)
    if _as_object(fixture.get("expected"), "fixture.expected").get("task_mode") != "ref2va":
        raise ValueError("full-reference host projection requires the ref2va fixture")
    prompt = build_model_free_prompt(fixture)
    reference_id = _node_id(prompt, REFERENCE_NODE_ID)
    numeric_ids = [int(node_id) for node_id in prompt if node_id.isdecimal()]
    next_id = max(numeric_ids, default=0) + 1

    image_batch_id = str(next_id)
    next_id += 1
    image_list_id = str(next_id)
    prompt[image_batch_id] = {
        "class_type": "ImageBatch",
        "inputs": {
            "image1": ["2", 0],
            "image2": ["3", 0],
        },
    }
    prompt[image_list_id] = {
        "class_type": "RebatchImages",
        "inputs": {
            "images": [image_batch_id, 0],
            "batch_size": 1,
        },
    }
    reference_inputs = dict(
        _as_object(prompt[reference_id].get("inputs"), f"prompt.{reference_id}.inputs")
    )
    reference_inputs["images"] = [image_list_id, 0]
    # A single V1 link is the valid representation for a one-item INPUT_IS_LIST socket.  The
    # static fixture keeps its declarative ``[[node, slot]]`` form for graph auditing, but sending
    # that nested shape to the host makes validation treat the inner list as an unhashable link.
    reference_inputs["videos"] = ["4", 0]
    reference_inputs["audios"] = ["5", 0]
    prompt[reference_id]["inputs"] = reference_inputs
    return prompt


def build_host_command(
    *,
    host_python: Path,
    host_root: Path,
    base_root: Path,
    port: int,
) -> list[str]:
    """Build a fixed, CPU-only, loopback-only ComfyUI command."""

    return [
        str(host_python),
        str(host_root / "main.py"),
        "--listen",
        "127.0.0.1",
        "--port",
        str(port),
        "--base-directory",
        str(base_root),
        "--models-directory",
        str(base_root / "models"),
        "--input-directory",
        str(base_root / "input"),
        "--output-directory",
        str(base_root / "output"),
        "--temp-directory",
        str(base_root / "temp"),
        "--user-directory",
        str(base_root / "user"),
        "--cpu",
        "--disable-auto-launch",
        "--disable-manager",
        "--disable-api-nodes",
        "--disable-metadata",
        "--log-stdout",
    ]


def redact_text(value: str, private_roots: Iterable[Path] = ()) -> str:
    """Remove credentials and private root strings from retained diagnostics."""

    redacted = value
    for root in private_roots:
        for spelling in {str(root), root.as_posix()}:
            redacted = redacted.replace(spelling, "[PRIVATE_ROOT]")
        try:
            resolved = root.resolve()
            for spelling in {str(resolved), resolved.as_posix()}:
                redacted = redacted.replace(spelling, "[PRIVATE_ROOT]")
        except OSError:
            pass
    redacted = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(token|sig|api[_-]?key|password|secret)\s*=\s*[^\s,;]+",
        r"\1=[REDACTED]",
        redacted,
    )
    return redacted


def _run_private_roots(
    *,
    host_python: Path | None,
    host_root: Path,
    artifact_path: Path | None = None,
    report_path: Path | None = None,
    runtime_root: Path | None = None,
) -> tuple[Path, ...]:
    """Collect every execution-owned or environment-specific root retained diagnostics may see."""

    roots = [ROOT, Path(sys.executable).parent, host_root]
    for path in (host_python, artifact_path, report_path):
        if path is not None:
            roots.append(path.parent)
    if runtime_root is not None:
        roots.append(runtime_root)
    return tuple(dict.fromkeys(root.resolve() for root in roots))


def select_unused_port() -> int:
    """Reserve an ephemeral loopback port long enough to select it for the child process."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def missing_host_dependencies(host_python: Path) -> tuple[str, ...]:
    """Probe declared host modules without importing the host or mutating its environment."""

    names = json.dumps(HOST_REQUIRED_MODULES)
    probe = (
        "import importlib.util, json; "
        f"names = json.loads({names!r}); "
        "print('\\n'.join(name + '=' + ('ok' if importlib.util.find_spec(name) else 'missing') "
        "for name in names))"
    )
    result = subprocess.run(
        [str(host_python), "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise HostBlockedError("host dependency probe failed")
    missing = tuple(
        line.split("=", 1)[0]
        for line in result.stdout.splitlines()
        if line.endswith("=missing") and "=" in line
    )
    return missing


def _host_dependency_versions(host_python: Path) -> dict[str, str]:
    """Return exact installed distribution versions from the selected host interpreter."""

    mapping = json.dumps(HOST_DEPENDENCY_DISTRIBUTIONS, sort_keys=True)
    probe = (
        "import importlib.metadata, json; "
        f"mapping = json.loads({mapping!r}); "
        "print(json.dumps({module: importlib.metadata.version(distribution) "
        "for module, distribution in mapping.items()}, sort_keys=True))"
    )
    result = subprocess.run(
        [str(host_python), "-I", "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise HostBlockedError("host dependency version probe failed")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HostBlockedError("host dependency version probe returned invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value) != set(HOST_REQUIRED_MODULES)
        or not all(isinstance(version, str) and version for version in value.values())
    ):
        raise HostBlockedError("host dependency version probe returned an incomplete inventory")
    return cast(dict[str, str], value)


def _http_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> Any:
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    if not url.startswith("http://127.0.0.1:"):
        raise HostDriverError("host driver permits only loopback HTTP")
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(  # noqa: S310
        url, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read(MAX_JSON_RESPONSE_BYTES + 1)
            if len(raw) > MAX_JSON_RESPONSE_BYTES:
                raise HostDriverError(
                    f"host JSON response exceeds {MAX_JSON_RESPONSE_BYTES} bytes for {path}"
                )
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
        raise HostHTTPError(exc.code, redact_text(body)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HostDriverError(f"host request failed for {path}: {type(exc).__name__}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostDriverError(f"host returned non-JSON data for {path}") from exc


def _wait_for_json(
    base_url: str,
    path: str,
    *,
    timeout: float,
    process: subprocess.Popen[str] | None = None,
    readiness_evidence: JsonObject | None = None,
) -> Any:
    started = time.monotonic()
    deadline = time.monotonic() + timeout
    first_error: str | None = None
    last_error: str | None = None
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        if process is not None and process.poll() is not None:
            raise HostDriverError(
                f"host exited before {path} readiness (code {process.returncode}); "
                f"first={first_error or 'none'} last={last_error or 'none'}"
            )
        try:
            value = _http_json(base_url, path)
            if readiness_evidence is not None:
                readiness_evidence.update(
                    {
                        "endpoint": path,
                        "attempts": attempts,
                        "duration_seconds": round(time.monotonic() - started, 6),
                        "first_error": first_error,
                        "last_error": last_error,
                    }
                )
            return value
        except HostDriverError as exc:
            message = redact_text(str(exc))
            first_error = first_error or message
            last_error = message
            time.sleep(POLL_INTERVAL_SECONDS)
    raise HostDriverError(
        f"timeout waiting for {path}; first={first_error or 'none'} last={last_error or 'none'}"
    )


def _port_is_released(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _png_bytes(rgb: tuple[int, int, int]) -> bytes:
    rows = b"\x00" + bytes(rgb) + bytes(rgb)
    raw = rows + b"\x00" + bytes(rgb) + bytes(rgb)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _write_reference_images(input_root: Path) -> None:
    input_root.mkdir(parents=True, exist_ok=True)
    (input_root / "h3_context_fixture_image_1.png").write_bytes(_png_bytes((220, 40, 40)))
    (input_root / "h3_context_fixture_image_2.png").write_bytes(_png_bytes((40, 80, 220)))


def _write_perception_media(input_root: Path) -> None:
    """Create bounded image/audio inputs for the M15-01 model-free host workflow."""

    import wave

    _write_reference_images(input_root)
    samples = bytes(8_000)
    with wave.open(str(input_root / "h3_context_fixture_audio.wav"), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(8_000)
        handle.writeframes(samples)


def _write_full_reference_media(input_root: Path) -> None:
    """Create bounded workspace-owned media for the model-free complex host fixture."""

    _write_reference_images(input_root)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise HostBlockedError("full-reference fixture requires the local ffmpeg executable")
    import wave

    sample_rate = 8_000
    samples = bytearray()
    for index in range(sample_rate // 2):
        value = int(8_000 * (1 if (index // 20) % 2 else -1))
        samples.extend(struct.pack("<h", value))
    audio_path = input_root / "h3_context_fixture_audio.wav"
    with wave.open(str(audio_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(samples))
    video_path = input_root / "h3_context_fixture_video.mp4"
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=24",
            "-t",
            "0.5",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0 or not video_path.is_file():
        raise HostDriverError("full-reference fixture video generation failed")


def _read_log_tail(log_path: Path, private_roots: Iterable[Path]) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"log unavailable: {type(exc).__name__}"
    return redact_text(text[-MAX_LOG_BYTES:], private_roots)


def _start_host(
    *,
    host_python: Path,
    host_root: Path,
    base_root: Path,
    port: int,
    enable_h2_canary: bool = False,
    isolate_coinstallation: bool = False,
) -> tuple[subprocess.Popen[str], Path]:
    command = build_host_command(
        host_python=host_python,
        host_root=host_root,
        base_root=base_root,
        port=port,
    )
    if isolate_coinstallation:
        command.extend(["--database-url", f"sqlite:///{(base_root / 'comfyui.sqlite').as_posix()}"])
    log_path = base_root / "host.log"
    environment = os.environ.copy()
    environment.update({"PYTHONUNBUFFERED": "1", "CUDA_VISIBLE_DEVICES": ""})
    if isolate_coinstallation:
        openclaw_state = base_root / "openclaw-state"
        doctor_state = base_root / "doctor-state"
        doctor_logs = doctor_state / "logs"
        for directory in (openclaw_state, doctor_state, doctor_logs):
            directory.mkdir(parents=True, exist_ok=True)
        environment.update(
            {
                "OPENCLAW_STATE_DIR": str(openclaw_state),
                "MOLTBOT_STATE_DIR": str(openclaw_state),
                "DOCTOR_STATE_DIR": str(doctor_state),
                "COMFYUI_DOCTOR_LOG_DIR": str(doctor_logs),
                "OPENCLAW_SCHEDULER_STARTUP_JITTER_SEC": "0",
                # CRITICAL: keep the explicit loopback --listen argument without weakening the
                # pinned OpenClaw security gate; this token is process-local and never retained.
                "OPENCLAW_ADMIN_TOKEN": secrets.token_urlsafe(32),
            }
        )
    if enable_h2_canary:
        environment.update(
            {
                "H3_CONTEXT_HOST_H2_CANARY": "1",
                "H3_CONTEXT_H2_CANARY_ROOT": str(base_root / "h2-canary"),
            }
        )
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=host_root,
            env=environment,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
            ),
        )
    return process, log_path


def _stop_host(process: subprocess.Popen[str], timeout: float) -> bool:
    if process.poll() is not None:
        return True
    if os.name == "nt":
        # CRITICAL: Windows Popen does not support SIGINT; terminate the verified host tree.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=timeout,
            )
            process.wait(timeout=timeout)
        except (AttributeError, OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=timeout)
            except (AttributeError, OSError, subprocess.TimeoutExpired):
                return False
        return process.poll() is not None
    try:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=timeout)
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        try:
            process.terminate()
            process.wait(timeout=timeout)
        except (AttributeError, OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=timeout)
            except (AttributeError, OSError, subprocess.TimeoutExpired):
                return False
    return process.poll() is not None


def _host_cycle_error(
    primary_failure: str | None,
    lifecycle_cycles: list[JsonObject],
) -> HostDriverError | None:
    """Preserve the first execution error while attaching structured cleanup evidence."""

    cleanup_failures = [
        str(cycle.get("cleanup_failure"))
        for cycle in lifecycle_cycles
        if cycle.get("cleanup") == "FAIL" and cycle.get("cleanup_failure")
    ]
    if primary_failure is None and not cleanup_failures:
        return None
    parts: list[str] = []
    if primary_failure is not None:
        parts.append(f"first_failure={primary_failure}")
    if cleanup_failures:
        parts.append("cleanup_failure=" + " | ".join(cleanup_failures))
    cleanup_status = (
        "PASS"
        if lifecycle_cycles and all(cycle.get("cleanup") == "PASS" for cycle in lifecycle_cycles)
        else "FAIL"
    )
    port_status = (
        "PASS"
        if lifecycle_cycles
        and all(cycle.get("port_released") is True for cycle in lifecycle_cycles)
        else "FAIL"
    )
    return HostDriverError(
        "; ".join(parts),
        evidence={
            "cleanup": cleanup_status,
            "port_release": port_status,
            "loopback_cycles": list(lifecycle_cycles),
        },
    )


def _host_metadata(host_root: Path) -> tuple[str, str]:
    if not host_root.is_dir() or not (host_root / "main.py").is_file():
        raise HostBlockedError("pinned ComfyUI checkout is unavailable")
    version_text = (host_root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', version_text)
    if match is None:
        raise HostDriverError("pinned host version is missing")
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=host_root,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise HostDriverError("cannot resolve pinned host revision")
    return match.group(1), result.stdout.strip()


def _expected_registration_ids(*, include_h2_canary: bool = False) -> frozenset[str]:
    # CRITICAL: H0 covers the backend-owned complete public namespace, not only nodes referenced
    # by historical workflow fixtures. Otherwise a new or currently non-executable node can vanish
    # from host registration while the supported-host gate still reports PASS.
    expected = frozenset(PUBLIC_NODE_IDS) | NATIVE_NODE_IDS
    return expected | ({H2_CANARY_NODE_ID} if include_h2_canary else set())


def _native_schema_evidence(info: Mapping[str, Any]) -> JsonObject:
    """Pin native H3 socket order and V3 Autogrow paths without loading model weights."""

    image_id = "MiniMaxH3ImageToVideo"
    reference_id = "MiniMaxH3ReferenceToVideo"
    sigma_id = "MiniMaxH3SigmaShift"
    image = _as_object(info.get(image_id), f"object_info.{image_id}")
    reference = _as_object(info.get(reference_id), f"object_info.{reference_id}")
    sigma = _as_object(info.get(sigma_id), f"object_info.{sigma_id}")
    expected_orders = {
        image_id: {
            "required": ["clip", "vae", "prompt", "width", "height", "length"],
            "optional": ["first_frame", "last_frame"],
        },
        reference_id: {
            "required": [
                "clip",
                "vae",
                "audio_vae",
                "prompt",
                "width",
                "height",
                "length",
                "ref_image_size",
            ],
            "optional": ["ref_images", "ref_videos", "ref_video_audios", "ref_audios"],
        },
    }
    for node_id, node in ((image_id, image), (reference_id, reference)):
        input_order = _as_object(node.get("input_order"), f"object_info.{node_id}.input_order")
        if input_order != expected_orders[node_id]:
            raise HostDriverError(f"native H3 input order drifted for {node_id}")
        if node.get("output") != ["CONDITIONING", "LATENT"]:
            raise HostDriverError(f"native H3 outputs drifted for {node_id}")
        required = _as_object(
            _as_object(node.get("input"), f"object_info.{node_id}.input").get("required"),
            f"object_info.{node_id}.input.required",
        )
        prompt = required.get("prompt")
        if not isinstance(prompt, list) or not prompt or prompt[0] != "STRING":
            raise HostDriverError(f"native H3 prompt socket drifted for {node_id}")

    required_socket_specs = {
        image_id: {
            "clip": "CLIP",
            "vae": "VAE",
            "prompt": "STRING",
            "width": "INT",
            "height": "INT",
            "length": "INT",
        },
        reference_id: {
            "clip": "CLIP",
            "vae": "VAE",
            "audio_vae": "VAE",
            "prompt": "STRING",
            "width": "INT",
            "height": "INT",
            "length": "INT",
            "ref_image_size": "COMBO",
        },
    }
    required_socket_types: dict[str, JsonObject] = {}
    required_by_node = {
        image_id: _as_object(
            _as_object(image.get("input"), f"object_info.{image_id}.input").get("required"),
            f"object_info.{image_id}.input.required",
        ),
        reference_id: _as_object(
            _as_object(reference.get("input"), f"object_info.{reference_id}.input").get("required"),
            f"object_info.{reference_id}.input.required",
        ),
    }
    for node_id, expected in required_socket_specs.items():
        observed: JsonObject = {}
        for field, socket_type in expected.items():
            descriptor = required_by_node[node_id].get(field)
            if not isinstance(descriptor, list) or len(descriptor) != 2:
                raise HostDriverError(f"native H3 required socket drifted for {node_id}.{field}")
            if socket_type == "COMBO":
                if descriptor[0] != "COMBO":
                    raise HostDriverError(f"native H3 ref_image_size socket drifted for {node_id}")
            elif descriptor[0] != socket_type:
                raise HostDriverError(f"native H3 required socket drifted for {node_id}.{field}")
            observed[field] = socket_type
        required_socket_types[node_id] = observed

    numeric_inputs: JsonObject = {
        "width": {"default": 1344, "min": 32, "max": 16384, "step": 32},
        "height": {"default": 768, "min": 32, "max": 16384, "step": 32},
        "length": {"default": 124, "min": 5, "max": 3600, "step": 17},
    }
    for field, expected in numeric_inputs.items():
        for node_id in (image_id, reference_id):
            descriptor = required_by_node[node_id][field]
            if not isinstance(descriptor, list) or descriptor[0] != "INT":
                raise HostDriverError(f"native H3 numeric socket drifted for {node_id}.{field}")
            observed_options = _as_object(
                descriptor[1], f"object_info.{node_id}.input.required.{field}"
            )
            if any(observed_options.get(key) != value for key, value in expected.items()):
                raise HostDriverError(f"native H3 numeric bounds drifted for {node_id}.{field}")
    ref_image_descriptor = required_by_node[reference_id]["ref_image_size"]
    ref_image_options = _as_object(
        ref_image_descriptor[1], f"object_info.{reference_id}.input.required.ref_image_size"
    )
    if (
        ref_image_options.get("options") != ["match", "max"]
        or ref_image_options.get("default") != "match"
        or ref_image_options.get("multiselect") is not False
    ):
        raise HostDriverError("native H3 ref_image_size options/default drifted")

    sigma_order = _as_object(sigma.get("input_order"), f"object_info.{sigma_id}.input_order")
    if sigma_order != {"required": ["model", "shift_video", "shift_audio"]}:
        raise HostDriverError("native H3 SigmaShift input order drifted")
    if sigma.get("output") != ["MODEL"]:
        raise HostDriverError("native H3 SigmaShift output drifted")
    sigma_required = _as_object(
        _as_object(sigma.get("input"), f"object_info.{sigma_id}.input").get("required"),
        f"object_info.{sigma_id}.input.required",
    )
    for field, default in (("shift_video", 12.0), ("shift_audio", 3.0)):
        descriptor = sigma_required.get(field)
        if (
            not isinstance(descriptor, list)
            or len(descriptor) != 2
            or descriptor[0] != "FLOAT"
            or not isinstance(descriptor[1], dict)
            or descriptor[1] != {"default": default, "min": 0.01, "max": 100.0, "step": 0.01}
        ):
            raise HostDriverError(f"native H3 SigmaShift {field} default drifted")

    image_input = _as_object(image.get("input"), f"object_info.{image_id}.input")
    image_optional = _as_object(
        image_input.get("optional"), f"object_info.{image_id}.input.optional"
    )
    for keyframe in ("first_frame", "last_frame"):
        descriptor = image_optional.get(keyframe)
        if not isinstance(descriptor, list) or not descriptor or descriptor[0] != "IMAGE":
            raise HostDriverError(f"native H3 keyframe socket drifted for {keyframe}")

    reference_input = _as_object(reference.get("input"), f"object_info.{reference_id}.input")
    optional = _as_object(reference_input.get("optional"), f"object_info.{reference_id}.optional")
    autogrow_specs = {
        "ref_images": ("IMAGE", "ref_image", "ref_image_", 9),
        "ref_videos": ("IMAGE", "ref_video", "ref_video_", 3),
        "ref_video_audios": ("AUDIO", "ref_video_audio", "ref_video_audio_", 3),
        "ref_audios": ("AUDIO", "ref_audio", "ref_audio_", 3),
    }
    autogrow_paths: list[str] = []
    bounded_autogrow: dict[str, Any] = {}
    for group, (socket_type, child_name, prefix, maximum) in autogrow_specs.items():
        descriptor = optional.get(group)
        if not isinstance(descriptor, list) or len(descriptor) != 2:
            raise HostDriverError(f"native H3 Autogrow descriptor drifted for {group}")
        if descriptor[0] != "COMFY_AUTOGROW_V3":
            raise HostDriverError(f"native H3 Autogrow socket drifted for {group}")
        options = _as_object(descriptor[1], f"object_info.{reference_id}.{group}")
        template = _as_object(
            options.get("template"), f"object_info.{reference_id}.{group}.template"
        )
        template_input = _as_object(
            template.get("input"), f"object_info.{reference_id}.{group}.template.input"
        )
        template_required = _as_object(
            template_input.get("required"),
            f"object_info.{reference_id}.{group}.template.input.required",
        )
        child = template_required.get(child_name)
        autogrow_expected: dict[str, object] = {
            "prefix": prefix,
            "min": 0,
            "max": maximum,
        }
        if any(template.get(key) != value for key, value in autogrow_expected.items()):
            raise HostDriverError(f"native H3 Autogrow bounds drifted for {group}")
        if not isinstance(child, list) or not child or child[0] != socket_type:
            raise HostDriverError(f"native H3 Autogrow child drifted for {group}")
        autogrow_paths.append(f"{group}.{prefix}0")
        bounded_autogrow[group] = {
            "socket_type": socket_type,
            "child": child_name,
            **autogrow_expected,
        }

    bounded = {
        "input_order": expected_orders,
        "outputs": {
            image_id: image["output"],
            reference_id: reference["output"],
        },
        "autogrow": bounded_autogrow,
        "prompt_sockets": {image_id: "STRING", reference_id: "STRING"},
        "sigma_shift": {
            "node_id": sigma_id,
            "input_order": sigma_order,
            "output": sigma["output"],
            "defaults": {"video": 12.0, "audio": 3.0},
        },
        "required_socket_types": required_socket_types,
        "numeric_inputs": numeric_inputs,
        "ref_image_size": {
            "options": ["match", "max"],
            "default": "match",
            "multiselect": False,
        },
    }
    canonical = json.dumps(bounded, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema": "h3-context-native-schema-evidence/1",
        "status": "PASS",
        "keyframe_inputs": ["first_frame", "last_frame"],
        "autogrow_paths": autogrow_paths,
        "autogrow": bounded_autogrow,
        "prompt_sockets": {image_id: "STRING", reference_id: "STRING"},
        "sigma_shift_node": sigma_id,
        "sigma_shift_defaults": {"video": 12.0, "audio": 3.0},
        "required_socket_types": required_socket_types,
        "numeric_inputs": numeric_inputs,
        "ref_image_size": {
            "options": ["match", "max"],
            "default": "match",
            "multiselect": False,
        },
        "schema_sha256": "sha256:" + hashlib.sha256(canonical).hexdigest(),
    }


def _native_source_semantics(
    source: str, blob: str, *, profile: str = "supported-b323"
) -> JsonObject:
    """Validate the exact sampler seam for the selected host-source authority."""

    expected_blob = (
        EXPECTED_NATIVE_SOURCE_BLOB
        if profile == "supported-b323"
        else LATEST_COMPATIBLE_NATIVE_SOURCE_BLOB
        if profile == "latest-compatible"
        else None
    )
    if expected_blob is None:
        raise HostDriverError("native H3 source profile is invalid")
    if blob != expected_blob:
        raise HostDriverError("native H3 supported source blob drifted")
    required_markers: tuple[str, ...] = (
        "comfy.model_sampling.ModelSamplingAV",
        "model_sampling.set_parameters(shift=shift_video, audio_shift=shift_audio)",
        'to["minimax_h3_sigma_shift_video"] = shift_video',
        'to["minimax_h3_sigma_shift_audio"] = shift_audio',
    )
    if not all(marker in source for marker in required_markers):
        raise HostDriverError("native H3 supported sampler semantics drifted")
    if profile == "latest-compatible":
        return {
            "schema": "h3-context-native-source-evidence/1",
            "status": "PASS",
            "qualification": "latest_compatible_observation",
            "supported_authority": "non_substituting_b323",
            "observed_blob": f"gitblob:{blob}",
            "observed_sampler": "ModelSamplingAV",
            "observed_scheduler": "model_sampling_av_video_audio",
            "observed_shift_application": "set_parameters.video_and_audio",
        }
    return {
        "schema": "h3-context-native-source-evidence/1",
        "status": "PASS",
        "supported_blob": f"gitblob:{blob}",
        "supported_sampler": "ModelSamplingAV",
        "supported_scheduler": "model_sampling_av_video_audio",
        "supported_video_shift_application": "set_parameters.shift_video",
        "supported_audio_shift_application": "set_parameters.audio_shift",
        "current_disposition": "REQUALIFIED_EXACT",
    }


def _native_source_evidence(host_root: Path, *, profile: str = "supported-b323") -> JsonObject:
    source_path = host_root / "comfy_extras" / "nodes_minimax_h3.py"
    if not source_path.is_file() or source_path.is_symlink():
        raise HostDriverError("native H3 supported source is unavailable")
    payload = source_path.read_bytes()
    if len(payload) > MAX_NATIVE_SOURCE_BYTES:
        raise HostDriverError("native H3 supported source exceeds the bound")
    try:
        source = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HostDriverError("native H3 supported source is not strict UTF-8") from exc
    header = f"blob {len(payload)}\0".encode()
    blob = hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()
    return _native_source_semantics(source, blob, profile=profile)


def _native_contract_probe_evidence(
    probe: Mapping[str, Any], native_schema: Mapping[str, Any]
) -> JsonObject:
    """Validate an isolated real-class probe without accepting model-free substitution."""

    expected_top = {
        "schema",
        "status",
        "host_version",
        "host_revision",
        "native_source_blob",
        "native_schema_sha256",
        "execution_projection",
        "class_records",
        "execution_boundary",
        "mode_receipts",
    }
    if set(probe) != expected_top:
        raise HostDriverError("native contract probe fields drifted")
    if (
        probe.get("schema") != "h3-context-native-contract-probe/1"
        or probe.get("status") != "PASS"
        or probe.get("host_version") != EXPECTED_HOST_VERSION
        or probe.get("host_revision") != EXPECTED_HOST_REVISION
        or probe.get("native_source_blob") != EXPECTED_NATIVE_SOURCE_BLOB
    ):
        raise HostDriverError("native contract probe host/source identity drifted")
    if (
        native_schema.get("schema") != "h3-context-native-schema-evidence/1"
        or native_schema.get("status") != "PASS"
        or probe.get("native_schema_sha256") != native_schema.get("schema_sha256")
    ):
        raise HostDriverError("native contract probe schema identity drifted")
    if probe.get("execution_projection") != "real_native_weight_callable_stub_only":
        raise HostDriverError("native contract execution projection is not real-native")

    expected_classes = (
        "MiniMaxH3ImageToVideo",
        "MiniMaxH3ReferenceToVideo",
        "MiniMaxH3SigmaShift",
    )
    records = probe.get("class_records")
    if not isinstance(records, list) or len(records) != len(expected_classes):
        raise HostDriverError("native contract class inventory is incomplete")
    retained_records: list[JsonObject] = []
    for raw, node_id in zip(records, expected_classes, strict=True):
        record = _as_object(raw, f"native contract class {node_id}")
        identity = f"comfy_extras.nodes_minimax_h3.{node_id}"
        if set(record) != {
            "node_id",
            "module",
            "qualname",
            "class_identity",
            "schema_owner_identity",
            "registry_source_identity_verified",
            "class_retained",
        }:
            raise HostDriverError("native contract class fields drifted")
        if (
            record.get("node_id") != node_id
            or record.get("module") != "comfy_extras.nodes_minimax_h3"
            or record.get("qualname") != node_id
            or record.get("class_identity") != identity
            or record.get("schema_owner_identity") != identity
            or record.get("registry_source_identity_verified") is not True
            or record.get("class_retained") is not True
        ):
            raise HostDriverError(f"native contract class identity drifted for {node_id}")
        retained_records.append(dict(record))

    boundary = _as_object(probe.get("execution_boundary"), "native contract execution boundary")
    if boundary != {
        "patched_attributes": ["execute"],
        "schema_observed_before_patch": True,
        "patch_scope": "isolated_host_process",
        "restored_after_probe": True,
    }:
        raise HostDriverError("native contract must patch execute only and restore it")

    expected_modes = ("t2va", "i2va", "fl2va", "l2va", "ref2va")
    receipts = probe.get("mode_receipts")
    if not isinstance(receipts, list) or len(receipts) != len(expected_modes):
        raise HostDriverError("native contract mode inventory is incomplete")
    retained_receipts: list[JsonObject] = []
    receipt_fields = {
        "task_mode",
        "native_node_id",
        "prompt_socket",
        "real_class_retained",
        "real_schema_retained",
        "native_node_queued",
        "graph_admitted",
        "weight_callable_stubbed",
        "stub_invoked",
        "result_identity",
        "weights_loaded",
        "model_output_generated",
        "provider_calls",
        "media_opened",
    }
    for raw, mode in zip(receipts, expected_modes, strict=True):
        receipt = _as_object(raw, f"native contract mode {mode}")
        node_id = "MiniMaxH3ReferenceToVideo" if mode == "ref2va" else "MiniMaxH3ImageToVideo"
        if set(receipt) != receipt_fields or (
            receipt.get("task_mode") != mode
            or receipt.get("native_node_id") != node_id
            or receipt.get("prompt_socket") != "STRING"
            or receipt.get("result_identity") != f"native-contract:{mode}"
        ):
            raise HostDriverError(f"native contract mode identity drifted for {mode}")
        if any(
            receipt.get(field) is not True
            for field in (
                "real_class_retained",
                "real_schema_retained",
                "native_node_queued",
                "graph_admitted",
                "weight_callable_stubbed",
                "stub_invoked",
            )
        ):
            raise HostDriverError(f"native contract mode {mode} did not retain the real seam")
        if any(
            receipt.get(field) is not False
            for field in (
                "weights_loaded",
                "model_output_generated",
                "provider_calls",
                "media_opened",
            )
        ):
            raise HostDriverError(
                f"native contract weight/media/provider boundary failed for {mode}"
            )
        retained_receipts.append(dict(receipt))
    return {
        "schema": "h3-context-native-contract-evidence/1",
        "status": "PASS",
        "qualification_scope": "structural_native_contract_only",
        "execution_projection": "real_native_weight_callable_stub_only",
        "native_schema_sha256": probe["native_schema_sha256"],
        "real_native_classes_retained": True,
        "real_native_schema_retained": True,
        "weight_callable_stub_only": True,
        "stripped_native_qualifies": False,
        "model_free_qualifies": False,
        "class_records": retained_records,
        "mode_receipts": retained_receipts,
        "weights_loaded": False,
        "model_output_generated": False,
        "provider_calls": False,
        "media_opened": False,
    }


def _native_contract_canary_source() -> str:
    """Return the test-only host extension that patches only real native execute callables."""

    return (
        """
import json
import inspect
import nodes as host_nodes
from pathlib import Path
from comfy_extras import nodes_minimax_h3 as native

def _registry_class(node_id, expected):
    cls = host_nodes.NODE_CLASS_MAPPINGS.get(node_id)
    observed_source = None if cls is None else inspect.getsourcefile(cls)
    expected_source = inspect.getsourcefile(expected)
    if (
        cls is None
        or cls.__name__ != expected.__name__
        or cls.__qualname__ != expected.__qualname__
        or observed_source is None
        or expected_source is None
        or Path(observed_source).resolve(strict=True)
        != Path(expected_source).resolve(strict=True)
    ):
        raise RuntimeError("native registry source identity drifted for " + node_id)
    return cls

_IMAGE = _registry_class("MiniMaxH3ImageToVideo", native.MiniMaxH3ImageToVideo)
_REFERENCE = _registry_class("MiniMaxH3ReferenceToVideo", native.MiniMaxH3ReferenceToVideo)
_SIGMA = _registry_class("MiniMaxH3SigmaShift", native.MiniMaxH3SigmaShift)
_ORIGINALS = {
    _IMAGE: _IMAGE.__dict__["execute"],
    _REFERENCE: _REFERENCE.__dict__["execute"],
}
_INVOKED = set()

def _json_output(payload):
    return {"ui": {"text": [payload]}, "result": (payload,)}

def _class_record(cls):
    identity = "comfy_extras.nodes_minimax_h3." + cls.__name__
    return {
        "node_id": cls.__name__,
        "module": "comfy_extras.nodes_minimax_h3",
        "qualname": cls.__name__,
        "class_identity": identity,
        "schema_owner_identity": identity,
        "registry_source_identity_verified": True,
        "class_retained": True,
    }

def _receipt(mode, node_id):
    if mode not in _INVOKED:
        raise RuntimeError("native contract stub was not invoked")
    return {
        "task_mode": mode,
        "native_node_id": node_id,
        "prompt_socket": "STRING",
        "real_class_retained": True,
        "real_schema_retained": True,
        "native_node_queued": True,
        "graph_admitted": True,
        "weight_callable_stubbed": True,
        "stub_invoked": True,
        "result_identity": "native-contract:" + mode,
        "weights_loaded": False,
        "model_output_generated": False,
        "provider_calls": False,
        "media_opened": False,
    }

def _image_execute(
    cls, clip, vae, prompt, width, height, length, first_frame=None, last_frame=None
):
    mode = "fl2va" if first_frame is not None and last_frame is not None else (
        "i2va" if first_frame is not None else ("l2va" if last_frame is not None else "t2va")
    )
    _INVOKED.add(mode)
    return native.io.NodeOutput("native-contract:" + mode, "native-contract-latent:" + mode)

def _reference_execute(cls, clip, vae, audio_vae, prompt, width, height, length,
                       ref_image_size="match", ref_images=None, ref_videos=None,
                       ref_video_audios=None, ref_audios=None):
    _INVOKED.add("ref2va")
    return native.io.NodeOutput("native-contract:ref2va", "native-contract-latent:ref2va")

_IMAGE.execute = classmethod(_image_execute)
_REFERENCE.execute = classmethod(_reference_execute)

class H3NativeContractValue:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("CLIP", "VAE", "IMAGE")
    RETURN_NAMES = ("clip", "vae", "image")
    FUNCTION = "emit"
    CATEGORY = "H3 Context/Test Only"
    def emit(self):
        sentinel = object()
        return (sentinel, sentinel, sentinel)

class H3NativeContractReceipt:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "conditioning": ("CONDITIONING",),
            "task_mode": (["t2va", "i2va", "fl2va", "l2va", "ref2va"],),
        }}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "emit"
    OUTPUT_NODE = True
    CATEGORY = "H3 Context/Test Only"
    def emit(self, conditioning, task_mode):
        node_id = "MiniMaxH3ReferenceToVideo" if task_mode == "ref2va" else "MiniMaxH3ImageToVideo"
        payload = json.dumps(_receipt(task_mode, node_id), sort_keys=True)
        return _json_output(payload)

class H3NativeContractRestore:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "restore"
    OUTPUT_NODE = True
    CATEGORY = "H3 Context/Test Only"
    def restore(self):
        for cls, execute in _ORIGINALS.items():
            cls.execute = execute
        payload = {
            "patched_attributes": ["execute"],
            "schema_observed_before_patch": True,
            "patch_scope": "isolated_host_process",
            "restored_after_probe": True,
            "class_records": [
                _class_record(_IMAGE),
                _class_record(_REFERENCE),
                _class_record(_SIGMA),
            ],
        }
        return _json_output(json.dumps(payload, sort_keys=True))

NODE_CLASS_MAPPINGS = {
    "H3NativeContractValue": H3NativeContractValue,
    "H3NativeContractReceipt": H3NativeContractReceipt,
    "H3NativeContractRestore": H3NativeContractRestore,
}
NODE_DISPLAY_NAME_MAPPINGS = {key: key for key in NODE_CLASS_MAPPINGS}
""".strip()
        + "\n"
    )


def _native_contract_prompts() -> dict[str, JsonObject]:
    """Build five bounded prompts that retain the real native nodes in host execution."""

    prompts: dict[str, JsonObject] = {}
    for mode in ("t2va", "i2va", "fl2va", "l2va"):
        native_inputs: JsonObject = {
            "clip": ["1", 0],
            "vae": ["1", 1],
            "prompt": f"native contract {mode}",
            "width": 1344,
            "height": 768,
            "length": 124,
        }
        if mode in {"i2va", "fl2va"}:
            native_inputs["first_frame"] = ["1", 2]
        if mode in {"l2va", "fl2va"}:
            native_inputs["last_frame"] = ["2", 2]
        prompts[mode] = {
            "1": {"class_type": "H3NativeContractValue", "inputs": {}},
            "2": {"class_type": "H3NativeContractValue", "inputs": {}},
            "10": {"class_type": "MiniMaxH3ImageToVideo", "inputs": native_inputs},
            "20": {
                "class_type": "H3NativeContractReceipt",
                "inputs": {"conditioning": ["10", 0], "task_mode": mode},
            },
        }
    prompts["ref2va"] = {
        "1": {"class_type": "H3NativeContractValue", "inputs": {}},
        "2": {"class_type": "H3NativeContractValue", "inputs": {}},
        "10": {
            "class_type": "MiniMaxH3ReferenceToVideo",
            "inputs": {
                "clip": ["1", 0],
                "vae": ["1", 1],
                "audio_vae": ["2", 1],
                "prompt": "native contract ref2va",
                "width": 1344,
                "height": 768,
                "length": 124,
                "ref_image_size": "match",
                "ref_images": [["1", 2]],
            },
        },
        "20": {
            "class_type": "H3NativeContractReceipt",
            "inputs": {"conditioning": ["10", 0], "task_mode": "ref2va"},
        },
    }
    return prompts


def _write_native_contract_canary(custom_nodes_root: Path) -> None:
    """Install the non-shipping execute-only canary in the owned host temporary root."""

    canary_root = custom_nodes_root / "zz_h3_native_contract_canary"
    if canary_root.exists():
        raise HostDriverError("native contract canary target already exists")
    canary_root.mkdir()
    (canary_root / "__init__.py").write_text(
        _native_contract_canary_source(), encoding="utf-8", newline="\n"
    )


def _queue_json_output_prompt(
    base_url: str,
    prompt: Mapping[str, Any],
    *,
    output_node_id: str,
    timeout: float,
) -> JsonObject:
    response = _as_object(
        _http_json(base_url, "/prompt", method="POST", payload={"prompt": prompt}),
        "native contract prompt response",
    )
    prompt_id = response.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise HostDriverError("native contract prompt was not admitted")
    deadline = time.monotonic() + timeout
    history: JsonObject | None = None
    while time.monotonic() < deadline:
        history_response = _as_object(
            _http_json(base_url, f"/history/{prompt_id}"),
            "native contract history response",
        )
        entry = history_response.get(prompt_id)
        if isinstance(entry, dict):
            history = cast(JsonObject, entry)
            status = history.get("status")
            if isinstance(status, dict) and status.get("completed"):
                break
        time.sleep(POLL_INTERVAL_SECONDS)
    if history is None:
        raise HostDriverError("native contract host history is unavailable")
    status = _as_object(history.get("status"), "native contract history.status")
    if status.get("status_str") != "success" or status.get("completed") is not True:
        raise HostDriverError("native contract native-node queue execution failed")
    outputs = _as_object(history.get("outputs"), "native contract history.outputs")
    texts = _extract_texts(outputs.get(output_node_id))
    if len(texts) != 1 or len(texts[0].encode("utf-8")) > 16_384:
        raise HostDriverError("native contract output is absent or exceeds the bound")
    try:
        value = json.loads(texts[0])
    except json.JSONDecodeError as exc:
        raise HostDriverError("native contract output is not JSON") from exc
    return _as_object(value, "native contract output")


def _queue_native_contract_probe(
    base_url: str,
    *,
    timeout: float,
    native_schema: Mapping[str, Any],
) -> JsonObject:
    """Queue all five real native nodes, then restore the isolated execute patch."""

    for node_id in (
        "H3NativeContractValue",
        "H3NativeContractReceipt",
        "H3NativeContractRestore",
    ):
        info = _as_object(
            _http_json(base_url, f"/object_info/{node_id}"),
            f"native contract object_info.{node_id}",
        )
        if node_id not in info:
            raise HostDriverError(f"native contract canary node is not registered: {node_id}")
    receipts = [
        _queue_json_output_prompt(
            base_url,
            prompt,
            output_node_id="20",
            timeout=timeout,
        )
        for prompt in _native_contract_prompts().values()
    ]
    restored = _queue_json_output_prompt(
        base_url,
        {"30": {"class_type": "H3NativeContractRestore", "inputs": {}}},
        output_node_id="30",
        timeout=timeout,
    )
    class_records = restored.get("class_records")
    boundary = dict(restored)
    boundary.pop("class_records", None)
    probe: JsonObject = {
        "schema": "h3-context-native-contract-probe/1",
        "status": "PASS",
        "host_version": EXPECTED_HOST_VERSION,
        "host_revision": EXPECTED_HOST_REVISION,
        "native_source_blob": EXPECTED_NATIVE_SOURCE_BLOB,
        "native_schema_sha256": native_schema.get("schema_sha256"),
        "execution_projection": "real_native_weight_callable_stub_only",
        "class_records": class_records,
        "execution_boundary": boundary,
        "mode_receipts": receipts,
    }
    return _native_contract_probe_evidence(probe, native_schema)


def _native_graph_qualification(
    fixture: Mapping[str, Any], native_schema: Mapping[str, Any]
) -> JsonObject:
    """Join the unprojected native graph edges to the declared runtime wiring manifest."""

    prompt = _as_object(fixture.get("prompt"), "fixture.prompt")
    expected = _as_object(fixture.get("expected"), "fixture.expected")
    projection = _as_object(expected.get("output_projection"), "fixture.output_projection")
    native_node_id = projection.get("native_node")
    if not isinstance(native_node_id, str) or native_node_id not in prompt:
        raise HostDriverError("native node is absent from qualification graph")
    native_node = _as_object(prompt[native_node_id], "fixture native node")
    native_type = native_node.get("class_type")
    if native_type not in NATIVE_OUTPUT_NODE_IDS:
        raise HostDriverError("native node type is not supported")
    native_inputs = _as_object(native_node.get("inputs"), "fixture native inputs")
    shell_id = _node_id(prompt, PRODUCT_SHELL_NODE_ID)
    if native_inputs.get("prompt") != [shell_id, 0]:
        raise HostDriverError("native graph prompt ownership drifted")
    direct_links = expected.get("direct_media_links")
    if not isinstance(direct_links, list) or not direct_links:
        raise HostDriverError("native graph has no declared direct media links")
    registry_id = _node_id(prompt, REFERENCE_NODE_ID)
    registry_node = _as_object(prompt[registry_id], "fixture reference registry")
    registry_inputs = _as_object(registry_node.get("inputs"), "fixture reference registry inputs")
    registry_images = registry_inputs.get("images")
    if not isinstance(registry_images, list) or len(registry_images) != len(direct_links):
        raise HostDriverError("native graph source authority is unavailable")
    autogrow = _as_object(native_schema.get("autogrow"), "native schema autogrow")
    bindings: list[JsonObject] = []
    seen_edges: set[tuple[str, int, str]] = set()
    seen_sources: set[tuple[str, int]] = set()
    for index, raw_link in enumerate(direct_links):
        link = _as_object(raw_link, f"direct_media_links.{index}")
        source = link.get("source")
        source_output = link.get("source_output")
        target = link.get("target")
        parent = link.get("target_input")
        if (
            not isinstance(source, str)
            or isinstance(source_output, bool)
            or not isinstance(source_output, int)
            or target != native_node_id
            or not isinstance(parent, str)
        ):
            raise HostDriverError("native graph direct media link ownership drifted")
        values = native_inputs.get(parent)
        if not isinstance(values, list) or index >= len(values):
            raise HostDriverError("native graph direct media link order is incomplete")
        if values[index] != [source, source_output]:
            raise HostDriverError("native graph direct media link order drifted")
        source_identity = (source, source_output)
        if source_identity in seen_sources:
            raise HostDriverError("native graph wiring manifest is ambiguous")
        seen_sources.add(source_identity)
        # CRITICAL: the native edge and declarative link cannot authenticate each other; retain the
        # separately visible Reference Registry input as the asset-to-source endpoint authority.
        if registry_images[index] != [source, source_output]:
            raise HostDriverError("native graph source authority association drifted")
        raw_spec = autogrow.get(parent)
        if not isinstance(raw_spec, dict):
            raise HostDriverError(f"native schema autogrow.{parent} is unavailable")
        spec = cast(JsonObject, raw_spec)
        prefix = spec.get("prefix")
        maximum = spec.get("max")
        if not isinstance(prefix, str) or not isinstance(maximum, int) or index >= maximum:
            raise HostDriverError("native graph child path is outside the real schema")
        relative_path = f"{parent}.{prefix}{index}"
        full_path = f"{native_type}.{relative_path}"
        asset_id = f"image_{index + 1}"
        label = f"<Picture {index + 1}>"
        if (
            link.get("target_path") != relative_path
            or link.get("asset_id") != asset_id
            or link.get("presentation_label") != label
            or link.get("presentation_ordinal") != index + 1
        ):
            raise HostDriverError("native graph wiring manifest identity/order drifted")
        edge = (source, source_output, relative_path)
        if edge in seen_edges:
            raise HostDriverError("native graph wiring manifest is ambiguous")
        seen_edges.add(edge)
        bindings.append(
            {
                "asset_id": asset_id,
                "presentation_label": label,
                "presentation_ordinal": index + 1,
                "source_node": source,
                "source_output": source_output,
                "native_node": native_node_id,
                "native_node_type": native_type,
                "native_child_path": full_path,
            }
        )
    if len(native_inputs.get(str(direct_links[0]["target_input"]), [])) != len(bindings):
        raise HostDriverError("native graph contains undeclared or ambiguous media edges")
    return {
        "schema": "h3-context-native-graph-qualification/1",
        "status": "PASS",
        "original_native_graph_retained": True,
        "execution_projection": "model_free_non_native_only",
        "native_node_queued": False,
        "weights_loaded": False,
        "identity_order_ownership_verified": True,
        "source_authority_verified": True,
        "runtime_manifest_verified": False,
        "bindings": bindings,
    }


def _assert_native_graph_runtime_qualification(
    observed: Mapping[tuple[str, int], tuple[str, ...]],
    shell_id: str,
    qualification: Mapping[str, Any],
) -> JsonObject:
    texts = observed.get((shell_id, 1), ())
    joined = "\n".join(texts)
    bindings = qualification.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        raise HostDriverError("native graph qualification has no bindings")
    last_binding_position = 0

    def field_position(field: str, value: str | int, start: int) -> int:
        if isinstance(value, str):
            markers = (
                f"{field}='{value}'",
                f'"{field}": "{value}"',
                f'"{field}":"{value}"',
            )
        else:
            markers = (f"{field}={value}", f'"{field}": {value}', f'"{field}":{value}')
        positions = [
            position for marker in markers if (position := joined.find(marker, start)) >= 0
        ]
        return min(positions, default=-1)

    for raw_binding in bindings:
        binding = _as_object(raw_binding, "native graph qualification binding")
        for field in ("asset_id", "presentation_label", "native_child_path"):
            marker = binding.get(field)
            if not isinstance(marker, str) or marker not in joined:
                raise HostDriverError(f"runtime native wiring lacks {field}")
        positions: list[int] = []
        cursor = last_binding_position
        for field in (
            "asset_id",
            "presentation_label",
            "presentation_ordinal",
            "native_child_path",
        ):
            value = binding.get(field)
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise HostDriverError("runtime native wiring binding contract drifted")
            cursor = field_position(field, value, cursor)
            positions.append(cursor)
            if cursor < 0:
                raise HostDriverError("runtime native wiring tuple association drifted")
        if positions != sorted(set(positions)):
            raise HostDriverError("runtime native wiring tuple association drifted")
        last_binding_position = positions[-1] + 1
    result = dict(qualification)
    result["runtime_manifest_verified"] = True
    result["joined_graph_runtime_verified"] = True
    return result


def _assert_registration(object_info: object, *, include_h2_canary: bool = False) -> dict[str, Any]:
    info = _as_object(object_info, "object_info")
    project_ids = _expected_registration_ids(include_h2_canary=include_h2_canary) - NATIVE_NODE_IDS
    missing_project = sorted(project_ids - set(info))
    missing_native = sorted(NATIVE_NODE_IDS - set(info))
    if missing_project or missing_native:
        raise HostDriverError(
            f"host registration missing project={missing_project} native={missing_native}"
        )
    for node_id in project_ids | NATIVE_NODE_IDS:
        node_info = _as_object(info.get(node_id), f"object_info.{node_id}")
        if node_info.get("name") != node_id:
            raise HostDriverError(f"host object_info name mismatch for {node_id}")
    # IMPORTANT: native V3 socket drift must block before any model-backed prompt can be queued.
    _native_schema_evidence(info)
    return info


def _registration_evidence(
    info: Mapping[str, Any], *, include_h2_canary: bool = False
) -> JsonObject:
    """Create a bounded exact-ID manifest from already validated public object-info."""

    expected = _expected_registration_ids(include_h2_canary=include_h2_canary)
    if not expected.issubset(info):
        raise HostDriverError("registration evidence is missing an expected node ID")
    selected = {node_id: info[node_id] for node_id in sorted(expected)}
    canonical = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    project_ids = expected - NATIVE_NODE_IDS
    return {
        "schema": "h3-context-registration-evidence/1",
        "registered_ids": sorted(expected),
        "project_ids": sorted(project_ids),
        "native_ids": sorted(NATIVE_NODE_IDS),
        "observed_object_info_count": len(info),
        "object_info_sha256": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "native_schema": _native_schema_evidence(info),
    }


def _run_registration_safety_probe(host_python: Path, subject_root: Path) -> JsonObject:
    """Exercise artifact registration idempotence and collision atomicity in host Python."""

    probe = f"""
import json
import sys
sys.path.insert(0, {str(subject_root)!r})
from comfyui_h3_context.core.errors import RegistrationConflictError
from comfyui_h3_context.registration import NODE_CLASS_MAPPINGS, register_nodes

foreign = object()
nodes = {{"Foreign.Sentinel": foreign}}
displays = {{"Foreign.Sentinel": "Foreign Sentinel"}}
register_nodes(nodes, displays)
first = dict(nodes)
register_nodes(nodes, displays)
idempotent = all(nodes[key] is value for key, value in first.items())
foreign_preserved = nodes["Foreign.Sentinel"] is foreign

node_id = sorted(NODE_CLASS_MAPPINGS)[0]
collision = object()
collision_nodes = {{node_id: collision}}
collision_displays = {{}}
before_nodes = dict(collision_nodes)
before_displays = dict(collision_displays)
collision_failed_closed = False
try:
    register_nodes(collision_nodes, collision_displays)
except RegistrationConflictError:
    collision_failed_closed = True
collision_atomic = (
    collision_nodes.keys() == before_nodes.keys()
    and all(collision_nodes[key] is value for key, value in before_nodes.items())
    and collision_displays == before_displays
)
checks = (idempotent, foreign_preserved, collision_failed_closed, collision_atomic)
result = {{
    "schema": "h3-context-registration-safety/1",
    "status": "PASS" if all(checks) else "FAIL",
    "idempotent": idempotent,
    "foreign_preserved": foreign_preserved,
    "collision_failed_closed": collision_failed_closed,
    "collision_atomic": collision_atomic,
}}
print(json.dumps(result, sort_keys=True))
"""
    result = subprocess.run(
        [str(host_python), "-I", "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise HostDriverError("host-interpreter registration safety probe failed")
    try:
        value = _as_object(json.loads(result.stdout), "registration safety probe")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HostDriverError("registration safety probe returned invalid JSON") from exc
    required_true = (
        "idempotent",
        "foreign_preserved",
        "collision_failed_closed",
        "collision_atomic",
    )
    if (
        value.get("schema") != "h3-context-registration-safety/1"
        or value.get("status") != "PASS"
        or any(value.get(field) is not True for field in required_true)
    ):
        raise HostDriverError("registration safety probe did not pass every assertion")
    return value


def _fetch_registration(
    base_url: str,
    *,
    timeout: float,
    process: subprocess.Popen[str] | None = None,
    include_h2_canary: bool = False,
) -> dict[str, Any]:
    """Read bounded registration data, falling back to the fixed node allowlist if needed."""

    try:
        return _assert_registration(
            _wait_for_json(base_url, "/object_info", timeout=timeout, process=process),
            include_h2_canary=include_h2_canary,
        )
    except HostDriverError as exc:
        if "JSON response exceeds" not in str(exc):
            raise
    info: dict[str, Any] = {}
    for node_id in sorted(_expected_registration_ids(include_h2_canary=include_h2_canary)):
        node_info = _wait_for_json(
            base_url,
            f"/object_info/{node_id}",
            timeout=timeout,
            process=process,
        )
        bounded = _as_object(node_info, f"object_info.{node_id}")
        info.update(bounded)
    return _assert_registration(info, include_h2_canary=include_h2_canary)


def _extract_texts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _extract_texts(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _extract_texts(child)]
    return []


def _collect_observer_texts(
    prompt: Mapping[str, Any], outputs: object
) -> dict[tuple[str, int], tuple[str, ...]]:
    """Map each PreviewAny source edge to its bounded text output."""

    output_map = _as_object(outputs, "history.outputs")
    observed: dict[tuple[str, int], tuple[str, ...]] = {}
    for observer_id, node in prompt.items():
        node_object = _as_object(node, f"prompt.{observer_id}")
        if node_object.get("class_type") != "PreviewAny":
            continue
        inputs = _as_object(node_object.get("inputs"), f"prompt.{observer_id}.inputs")
        source = inputs.get("source")
        if (
            not isinstance(source, list)
            or len(source) != 2
            or not isinstance(source[0], str)
            or isinstance(source[1], bool)
            or not isinstance(source[1], int)
        ):
            raise HostDriverError(f"PreviewAny observer {observer_id} has an invalid source")
        source_key = (source[0], source[1])
        if source_key in observed:
            raise HostDriverError(f"duplicate PreviewAny observer for source {source_key!r}")
        texts = tuple(_extract_texts(output_map.get(observer_id, {})))
        if not texts:
            raise HostDriverError(f"PreviewAny observer {observer_id} produced no text output")
        observed[source_key] = texts
    return observed


def _assert_source_markers(
    observed: Mapping[tuple[str, int], tuple[str, ...]],
    *,
    required_sources: Iterable[tuple[str, int]],
    markers: Iterable[str],
    label: str,
) -> None:
    """Require every marker on the explicitly observed source outputs, not global history text."""

    source_texts: list[str] = []
    for source in required_sources:
        texts = observed.get(source)
        if not texts:
            raise HostDriverError(f"{label} source {source!r} was not observed")
        source_texts.extend(texts)
    joined = "\n".join(source_texts).casefold()
    for marker in markers:
        if marker.casefold() not in joined:
            raise HostDriverError(f"{label} source outputs lack marker {marker!r}")


def _assert_audit_override_observations(
    observed: Mapping[tuple[str, int], tuple[str, ...]],
    *,
    override_id: str,
    validator_id: str,
    preview_id: str,
    edited_text: str,
    base_report_fingerprint: str,
    prompt_fingerprint: str,
    validated_report_fingerprint: str,
) -> None:
    """Bind audit evidence to the public outputs that actually expose each fact."""

    edited_outputs = observed.get((override_id, 0))
    if edited_outputs is None or edited_text not in edited_outputs:
        raise HostDriverError("audit override edited-prompt output lacks the exact manual text")
    _assert_source_markers(
        observed,
        required_sources=((override_id, 1),),
        markers=("audit_override_applied",),
        label="audit override updated report",
    )
    _assert_source_markers(
        observed,
        required_sources=((override_id, 2),),
        markers=(base_report_fingerprint,),
        label="audit override record",
    )
    _assert_source_markers(
        observed,
        required_sources=((validator_id, 0),),
        markers=("ValidationStatus.PASSED", "h3-context-m10-05"),
        label="audit override validator",
    )
    _assert_source_markers(
        observed,
        required_sources=((preview_id, 1),),
        markers=(prompt_fingerprint, validated_report_fingerprint),
        label="audit override validated preview",
    )


def _queue_fixture(
    base_url: str,
    fixture: Mapping[str, Any],
    *,
    timeout: float,
    native_qualification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expected = _as_object(fixture.get("expected"), "fixture.expected")
    fixture_id = fixture.get("fixture_id")
    if fixture_id == "m7-03-audit-override":
        prompt = build_model_free_prompt(fixture)
    elif fixture_id == "m6-07-full-reference-expanded":
        prompt = build_model_free_full_reference_prompt(fixture)
    elif expected.get("task_mode") == "ref2va":
        prompt = build_model_free_reference_prompt(fixture)
    else:
        prompt = build_model_free_prompt(fixture)
    response = _http_json(base_url, "/prompt", method="POST", payload={"prompt": prompt})
    response_object = _as_object(response, "prompt response")
    prompt_id = response_object.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise HostDriverError("host accepted a prompt without a prompt_id")
    deadline = time.monotonic() + timeout
    history: JsonObject | None = None
    while time.monotonic() < deadline:
        raw_history = _http_json(base_url, f"/history/{prompt_id}")
        history_object = _as_object(raw_history, "history response")
        entry = history_object.get(prompt_id)
        if isinstance(entry, dict):
            history = cast(JsonObject, entry)
            status = history.get("status")
            if isinstance(status, dict) and status.get("completed"):
                break
        time.sleep(POLL_INTERVAL_SECONDS)
    if history is None:
        raise HostDriverError("host did not produce history for queued prompt")
    status = _as_object(history.get("status"), "history.status")
    if status.get("status_str") != "success" or not status.get("completed"):
        raise HostDriverError(f"host prompt execution failed: {status.get('status_str')}")
    outputs = history.get("outputs")
    texts = _extract_texts(outputs)
    observed = _collect_observer_texts(prompt, outputs)
    if fixture_id == "m15-01-perception-producers":
        _assert_source_markers(
            observed,
            required_sources=(("2", 0), ("5", 0)),
            markers=("complete", "host_media_admitted", "caller_declared_unverified"),
            label="media admission",
        )
        _assert_source_markers(
            observed,
            required_sources=(("3", 0), ("6", 0)),
            markers=("unavailable", "profile_unavailable"),
            label="perception unavailable",
        )
        joined = "\n".join(texts).casefold()
        if "fallback" in joined or any(marker in joined for marker in PRIVATE_MARKERS):
            raise HostDriverError("perception workflow output contains a forbidden claim")
        return {
            "fixture_id": fixture_id,
            "prompt_id": prompt_id,
            "status": "PASS",
            "text_output_count": len(texts),
            "media_admission": "complete",
            "visual_disposition": "unavailable",
            "audio_disposition": "unavailable",
            "automatic_fallback": False,
        }
    if fixture_id == "m15-02-downstream-producers":
        _assert_source_markers(
            observed,
            required_sources=(("4", 0),),
            markers=("red umbrella", "watermark", "00:01.000"),
            label="hard constraints",
        )
        _assert_source_markers(
            observed,
            required_sources=(("4", 1),),
            markers=("hard_constraint", "manual_component_built"),
            label="hard-constraint report",
        )
        _assert_source_markers(
            observed,
            required_sources=(("5", 0),),
            markers=("traveler", "crosses the room", "manual.segment.1"),
            label="manual intent graph",
        )
        _assert_source_markers(
            observed,
            required_sources=(("5", 1),),
            markers=("intent_graph", "manual_component_built"),
            label="intent report",
        )
        _assert_source_markers(
            observed,
            required_sources=(("6", 0),),
            markers=("complete", "caller_declared_unverified"),
            label="media admission",
        )
        _assert_source_markers(
            observed,
            required_sources=(("7", 0),),
            markers=("partial", "uncertain"),
            label="evidence fusion graph",
        )
        _assert_source_markers(
            observed,
            required_sources=(("7", 1),),
            markers=("partial", "caller_declared_unverified"),
            label="evidence fusion report",
        )
        _assert_source_markers(
            observed,
            required_sources=(("8", 0),),
            markers=("empty", "image_1"),
            label="cross-reference graph",
        )
        _assert_source_markers(
            observed,
            required_sources=(("8", 1),),
            markers=("partial", "caller_declared_unverified"),
            label="cross-reference report",
        )
        _assert_source_markers(
            observed,
            required_sources=(("9", 0),),
            markers=("complete", "manual.directive.1"),
            label="directive authority bundle",
        )
        _assert_source_markers(
            observed,
            required_sources=(("9", 1),),
            markers=("complete", "manual_component_built"),
            label="directive authority report",
        )
        _assert_source_markers(
            observed,
            required_sources=(("10", 0),),
            markers=("perception_profile_unavailable",),
            label="timeline unavailable",
        )
        _assert_source_markers(
            observed,
            required_sources=(("10", 1),),
            markers=("full_reference_timeline", "perception_profile_unavailable"),
            label="timeline report",
        )
        joined = "\n".join(texts).casefold()
        if "fallback" in joined or any(marker in joined for marker in PRIVATE_MARKERS):
            raise HostDriverError("downstream workflow output contains a forbidden claim")
        return {
            "fixture_id": fixture_id,
            "prompt_id": prompt_id,
            "status": "PASS",
            "text_output_count": len(texts),
            "manual_route": "complete",
            "timeline_disposition": "explicit_unavailable",
            "automatic_fallback": False,
        }
    if fixture_id == "m13-10-local-reconstruction":
        _assert_source_markers(
            observed,
            required_sources=(("11", 0),),
            markers=('Narrator: "Enter now."', "GATE 7", "traveler"),
            label="source-profiled prompt",
        )
        _assert_source_markers(
            observed,
            required_sources=(("12", 0),),
            markers=("passed",),
            label="semantic validation",
        )
        _assert_source_markers(
            observed,
            required_sources=(("13", 1),),
            markers=("direct_to_native", "native_only"),
            label="native H3 wiring",
        )
        _assert_source_markers(
            observed,
            required_sources=(("14", 0),),
            markers=(
                "deterministic_manual",
                "complete",
                "caller_declared_media_fingerprint_unverified",
            ),
            label="local reconstruction acceptance",
        )
        joined = "\n".join(texts).casefold()
        public_source_removed = joined.replace(
            "https://huggingface.co/minimaxai/minimax-h3/blob/", ""
        )
        if (
            "fallback" in joined
            or any(marker in joined for marker in PRIVATE_MARKERS[:-2])
            or "http://" in public_source_removed
            or "https://" in public_source_removed
        ):
            raise HostDriverError("local reconstruction output contains a forbidden claim")
        return {
            "fixture_id": fixture_id,
            "prompt_id": prompt_id,
            "status": "PASS",
            "text_output_count": len(texts),
            "route": "deterministic_manual",
            "disposition": "complete",
            "source_profiled": True,
            "validation": "passed",
            "native_wiring": True,
            "provider_calls": 0,
            "model_loads": 0,
            "uploads": 0,
            "automatic_fallback": False,
        }
    expected_observer_count = M7_FIXTURE_OBSERVER_COUNTS.get(str(fixture_id))
    if expected_observer_count is not None and len(observed) != expected_observer_count:
        raise HostDriverError(
            f"{fixture_id} expected {expected_observer_count} source-bound observers, "
            f"got {len(observed)}"
        )
    fixture_prompt = _as_object(fixture.get("prompt"), "fixture.prompt")
    request_id = _node_id(fixture_prompt, "comfyui_h3_context.H3Context.Request")
    request = _as_object(fixture_prompt[request_id], "fixture request")
    request_inputs = _as_object(request.get("inputs"), "fixture request inputs")
    intent = request_inputs.get("user_intent")
    if not isinstance(intent, str) or not any(intent in text for text in texts):
        raise HostDriverError(
            "host history does not contain the exact user intent in prompt output"
        )
    joined = "\n".join(texts).casefold()
    for marker in PRIVATE_MARKERS:
        if marker in joined:
            raise HostDriverError(f"host output contains forbidden marker {marker!r}")
    required_report_markers = ("fingerprints", "limitations", "validation")
    if not all(marker in joined for marker in required_report_markers):
        raise HostDriverError("host history lacks the inspectable preview/report projection")
    required_reference_labels: tuple[str, ...] = ()
    joined_native_qualification: JsonObject | None = None
    if fixture_id in {
        "m15-03-product-shell-base",
        "m15-03-product-shell-reference",
        "m15-09-assistant-base",
        "m15-09-assistant-reference",
    }:
        shell_id = _node_id(fixture_prompt, PRODUCT_SHELL_NODE_ID)
        _assert_source_markers(
            observed,
            required_sources=((shell_id, 0),),
            markers=(intent,),
            label="product-shell standard STRING",
        )
        _assert_source_markers(
            observed,
            required_sources=((shell_id, 1),),
            markers=("manual_only_scoped", "0.32.0", "1.48.7"),
            label="product-shell backend projection",
        )
        if fixture_id in {"m15-03-product-shell-reference", "m15-09-assistant-reference"}:
            if native_qualification is None:
                raise HostDriverError("native graph qualification was not supplied")
            joined_native_qualification = _assert_native_graph_runtime_qualification(
                observed, shell_id, native_qualification
            )
    if fixture_id == "m6-07-full-reference-expanded":
        required_reference_labels = ("<Picture 1>", "<Picture 2>", "<Video 1>", "<Audio 1>")
        if not all(label.casefold() in joined for label in required_reference_labels):
            raise HostDriverError("host history lacks the ordered full-reference labels")
    if fixture_id == "m7-03-audit-override":
        override_id = _node_id(
            fixture_prompt,
            "comfyui_h3_context.H3Context.AuditOverride",
        )
        override_inputs = _as_object(
            fixture_prompt[override_id].get("inputs"), "fixture override inputs"
        )
        edited_text = override_inputs.get("prompt_text")
        base_report_fingerprint = override_inputs.get("base_report_fingerprint")
        expected_override = _as_object(expected.get("override_projection"), "override projection")
        expected_validated = _as_object(
            expected.get("validated_projection"), "validated projection"
        )
        prompt_fingerprint = expected_override.get("prompt_fingerprint")
        validated_report_fingerprint = expected_validated.get("report_fingerprint")
        if not all(
            isinstance(value, str)
            for value in (
                edited_text,
                base_report_fingerprint,
                prompt_fingerprint,
                validated_report_fingerprint,
            )
        ):
            raise HostDriverError("audit override fixture lacks its exact public projections")
        validator_id = _node_id(fixture_prompt, "comfyui_h3_context.H3Context.Validator")
        preview_id = _node_id(fixture_prompt, PREVIEW_NODE_ID)
        _assert_audit_override_observations(
            observed,
            override_id=override_id,
            validator_id=validator_id,
            preview_id=preview_id,
            edited_text=cast(str, edited_text),
            base_report_fingerprint=cast(str, base_report_fingerprint),
            prompt_fingerprint=cast(str, prompt_fingerprint),
            validated_report_fingerprint=cast(str, validated_report_fingerprint),
        )
    if fixture_id == "m7-04-provider-transparency":
        transparency_id = _node_id(fixture_prompt, PROVIDER_TRANSPARENCY_NODE_ID)
        transparency_inputs = _as_object(
            fixture_prompt[transparency_id].get("inputs"), "fixture transparency inputs"
        )
        if transparency_inputs.get("provider") != "remote_custom":
            raise HostDriverError("fixture does not select remote_custom explicitly")
        expected_transparency = _as_object(
            expected.get("transparency_projection"), "transparency projection"
        )
        _assert_source_markers(
            observed,
            required_sources=tuple((transparency_id, slot) for slot in range(3)),
            markers=(
                "provider_defined",
                "provider_pricing",
                "caller_selected_only",
                "execution_allowed",
                "upload_consent_required",
                "policy_valid",
            ),
            label="provider transparency",
        )
        for field in ("provider", "policy_valid", "execution_allowed"):
            value = expected_transparency.get(field)
            if value is None or str(value).casefold() not in joined:
                raise HostDriverError(f"host history lacks transparency projection {field!r}")
    if fixture_id == "m7-05-reliability":
        reliability_id = _node_id(fixture_prompt, RELIABILITY_NODE_ID)
        reliability_inputs = _as_object(
            fixture_prompt[reliability_id].get("inputs"), "fixture reliability inputs"
        )
        if reliability_inputs.get("cancel_requested") is not True:
            raise HostDriverError("fixture does not request cancellation explicitly")
        expected_reliability = _as_object(
            expected.get("reliability_projection"), "reliability projection"
        )
        _assert_source_markers(
            observed,
            required_sources=tuple((reliability_id, slot) for slot in range(4)),
            markers=(
                "progress=3/10",
                "cancel_requested=yes",
                "action=cancelled",
                "execution_allowed=no",
                "recovery.checkpoint_incompatible",
            ),
            label="reliability",
        )
        for field in ("stage", "state", "action", "execution_allowed"):
            value = expected_reliability.get(field)
            if value is None or str(value).casefold() not in joined:
                raise HostDriverError(f"host history lacks reliability projection {field!r}")
    result: JsonObject = {
        "fixture_id": fixture.get("fixture_id"),
        "prompt_id": prompt_id,
        "status": "PASS",
        "text_output_count": len(texts),
        "prompt_intent_preserved": True,
        "reference_labels": list(required_reference_labels),
        "report_projection_markers": list(required_report_markers),
    }
    if joined_native_qualification is not None:
        result["native_graph_qualification"] = joined_native_qualification
    return result


def _queue_h2_canary(base_url: str, *, timeout: float) -> JsonObject:
    scenarios = ("success", "cancellation", "timeout")
    prompt: JsonObject = {}
    for index, scenario in enumerate(scenarios, start=1):
        node_id = f"h2_{index}"
        observer_id = f"h2_preview_{index}"
        prompt[node_id] = {
            "class_type": H2_CANARY_NODE_ID,
            "inputs": {"scenario": scenario},
        }
        prompt[observer_id] = {
            "class_type": "PreviewAny",
            "inputs": {"source": [node_id, 0]},
        }
    response = _as_object(
        _http_json(base_url, "/prompt", method="POST", payload={"prompt": prompt}),
        "H2 prompt response",
    )
    prompt_id = response.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise HostDriverError("host accepted the H2 prompt without a prompt_id")
    deadline = time.monotonic() + timeout
    history: JsonObject | None = None
    while time.monotonic() < deadline:
        history_response = _as_object(
            _http_json(base_url, f"/history/{prompt_id}"),
            "H2 history response",
        )
        entry = history_response.get(prompt_id)
        if isinstance(entry, dict):
            history = cast(JsonObject, entry)
            status = history.get("status")
            if isinstance(status, dict) and status.get("completed"):
                break
        time.sleep(POLL_INTERVAL_SECONDS)
    if history is None:
        raise HostDriverError("host did not produce H2 history")
    status = _as_object(history.get("status"), "H2 history.status")
    if status.get("status_str") != "success" or not status.get("completed"):
        raise HostDriverError(f"host H2 execution failed: {status.get('status_str')}")
    observed = _collect_observer_texts(prompt, history.get("outputs"))
    expected_status = {
        "success": "succeeded",
        "cancellation": "cancelled",
        "timeout": "timed_out",
    }
    results: dict[str, JsonObject] = {}
    for index, scenario in enumerate(scenarios, start=1):
        texts = observed.get((f"h2_{index}", 0), ())
        if len(texts) != 1:
            raise HostDriverError(f"host H2 {scenario} produced an invalid result count")
        try:
            result = _as_object(json.loads(texts[0]), f"H2 {scenario} result")
        except json.JSONDecodeError as exc:
            raise HostDriverError(f"host H2 {scenario} returned invalid JSON") from exc
        if result.get("status") != expected_status[scenario]:
            raise HostDriverError(f"host H2 {scenario} reached the wrong terminal status")
        if not all(
            result.get(field) is True
            for field in ("cleanup_succeeded", "reaped", "reader_threads_joined")
        ):
            raise HostDriverError(f"host H2 {scenario} did not clean up")
        if any(marker in texts[0].casefold() for marker in PRIVATE_MARKERS):
            raise HostDriverError(f"host H2 {scenario} result was not redacted")
        results[scenario] = result
    if results["success"].get("artifact_released") is not True:
        raise HostDriverError("host H2 success artifact was not released")
    return {
        "fixture_id": "hc03-media-lifecycle-h2",
        "fixture_version": "1",
        "prompt_id": prompt_id,
        "status": "PASS",
        "scenarios": {scenario: results[scenario]["status"] for scenario in scenarios},
        "assertions": [
            "bounded_polling",
            "cancellation",
            "timeout",
            "reaped",
            "owned_output",
            "redaction",
            "cleanup",
        ],
    }


def _queue_invalid_prompt(base_url: str) -> None:
    invalid_prompt = {
        "1": {
            "class_type": "comfyui_h3_context.H3Context.Request",
            "inputs": {"task_mode": "t2va"},
        },
        "2": {"class_type": "PreviewAny", "inputs": {"source": ["1", 0]}},
    }
    try:
        _http_json(base_url, "/prompt", method="POST", payload={"prompt": invalid_prompt})
    except HostHTTPError as exc:
        if exc.status != 400 or "required_input_missing" not in exc.body:
            raise HostDriverError(
                "invalid prompt failed without an actionable typed error"
            ) from exc
        return
    raise HostDriverError("invalid prompt unexpectedly produced a success response")


def _git_snapshot() -> tuple[str, bool]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain=v2", "--untracked-files=all"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if head.returncode != 0 or status.returncode != 0:
        raise HostDriverError("cannot resolve the implementation snapshot")
    return head.stdout.strip(), not bool(status.stdout.strip())


def _python_version(executable: Path) -> str:
    result = subprocess.run(
        [str(executable), "-c", "import sys; print(sys.version.split()[0])"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise HostBlockedError("cannot identify a selected interpreter")
    return result.stdout.strip()


def _write_host_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _validate_browser_performance_receipt(receipt: Mapping[str, Any]) -> None:
    """Validate the content-free browser receipt before retaining it as host evidence."""

    expected = {
        "schema",
        "source",
        "mount_ms",
        "refresh_ms",
        "decode_ms",
        "render_ms",
        "projection_peak_bytes",
        "projection_update_count",
        "graph_event_count",
        "refresh_count",
        "coalesced_event_count",
        "cleanup_verified",
        "diagnostic_codes",
    }
    if type(receipt) is not dict or set(receipt) != expected:
        raise HostDriverError("browser performance receipt members are not closed")
    if (
        receipt.get("schema") != "h3.frontend.performance.v1"
        or receipt.get("source") != "browser_user_timing"
    ):
        raise HostDriverError("browser performance receipt identity is invalid")
    for field in ("mount_ms", "refresh_ms", "decode_ms", "render_ms"):
        value = receipt.get(field)
        if type(value) not in {int, float} or not 0 <= float(cast(int | float, value)) <= 60_000:
            raise HostDriverError("browser performance timing is unavailable or invalid")
    integer_bounds = {
        "projection_peak_bytes": (1, 131_072),
        "projection_update_count": (1, 10_000),
        "graph_event_count": (0, 10_000),
        "refresh_count": (1, 10_000),
        "coalesced_event_count": (0, 10_000),
    }
    for field, (minimum, maximum) in integer_bounds.items():
        value = receipt.get(field)
        if type(value) is not int or not minimum <= value <= maximum:
            raise HostDriverError("browser performance counter is outside its bound")
    if cast(int, receipt["coalesced_event_count"]) < 1:
        raise HostDriverError("browser performance receipt did not observe graph-event coalescing")
    if cast(int, receipt["coalesced_event_count"]) > cast(int, receipt["graph_event_count"]):
        raise HostDriverError("browser performance coalescing inventory is inconsistent")
    if receipt.get("cleanup_verified") is not True:
        raise HostDriverError("browser performance cleanup is not verified")
    if receipt.get("diagnostic_codes") != []:
        raise HostDriverError("browser performance receipt contains unresolved diagnostics")


def _decode_browser_performance_receipt(payload: bytes) -> JsonObject:
    """Decode one bounded content-free receipt without normalizing hostile wire bytes."""

    if (
        type(payload) is not bytes
        or not payload
        or len(payload) > MAX_BROWSER_PERFORMANCE_RECEIPT_BYTES
    ):
        raise HostDriverError("browser performance receipt wire size is invalid")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise HostDriverError("browser performance receipt is not strict UTF-8") from None

    def closed_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if type(key) is not str or key in value:
                raise ValueError("duplicate or invalid JSON member")
            value[key] = item
        return value

    def reject_nonfinite(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        decoded = json.loads(
            text,
            object_pairs_hook=closed_pairs,
            parse_constant=reject_nonfinite,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        # CRITICAL: browser evidence must reject duplicate/non-finite JSON before validation.
        raise HostDriverError("browser performance receipt JSON is invalid") from None
    return _as_object(decoded, "browser performance receipt")


def _run_browser_e2e(
    base_url: str,
    *,
    timeout: float,
    output_root: Path,
    expected_sidebar_ids: tuple[str, ...] = (),
) -> JsonObject:
    """Run the real browser against the live exact host with no external network allowance."""

    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise HostBlockedError("pnpm is unavailable for the supported-host browser probe")
    command = (
        pnpm,
        "--dir",
        str(ROOT / "frontend"),
        "exec",
        "playwright",
        "test",
        "--config",
        str(ROOT / "frontend" / "playwright.config.ts"),
    )
    environment = os.environ.copy()
    environment["H3_CONTEXT_HOST_URL"] = base_url
    environment["H3_CONTEXT_PLAYWRIGHT_OUTPUT"] = str(output_root)
    environment["H3_CONTEXT_EXPECTED_SIDEBAR_IDS"] = json.dumps(expected_sidebar_ids)
    try:
        result = subprocess.run(
            command,
            cwd=ROOT / "frontend",
            env=environment,
            check=False,
            capture_output=True,
            timeout=min(timeout, 240.0),
        )
    except subprocess.TimeoutExpired as exc:
        raise HostDriverError("supported-host browser probe timed out") from exc
    if type(result.stdout) is not bytes or type(result.stderr) is not bytes:
        raise HostDriverError("supported-host browser output type is invalid")
    combined_wire = (result.stdout + b"\n" + result.stderr)[-MAX_LOG_BYTES:]
    try:
        combined = combined_wire.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise HostDriverError("supported-host browser output is not strict UTF-8") from None
    if result.returncode != 0:
        raise HostDriverError(f"supported-host browser probe failed: {combined}")
    matches = re.findall(rb"H3_CONTEXT_PERFORMANCE_RECEIPT=(\{[^\r\n]+\})", combined_wire)
    if len(matches) != 1:
        raise HostDriverError("supported-host browser performance receipt is missing or ambiguous")
    performance_receipt = _decode_browser_performance_receipt(matches[0])
    _validate_browser_performance_receipt(performance_receipt)
    return {
        "status": "PASS",
        "assertions": [
            "supported_pair_loaded",
            "tab_registered",
            "projection_rendered",
            "app_mode_execution_parity",
            "app_mode_cancellation",
            "subgraph_execution_parity",
            "graph_rescan",
            "graph_refresh_coalescing",
            "frontend_performance_receipt",
            "projection_byte_budget",
            "unregister_destroy",
            "network_silence",
            "cleanup",
        ],
        "runner": "playwright",
        "output_retention": "driver_owned_temporary",
        "performance_receipt": performance_receipt,
        "coinstallation_sidebar_ids": list(expected_sidebar_ids),
    }


def run_supported_host(
    *,
    host_python: Path,
    host_root: Path = DEFAULT_HOST_ROOT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    modes: tuple[str, ...] = ("base", "reference"),
    artifact_path: Path | None = None,
    exact_command: tuple[str, ...] = (),
    browser_e2e: bool = False,
    expected_host_version: str = EXPECTED_HOST_VERSION,
    expected_host_revision: str = EXPECTED_HOST_REVISION,
    native_source_profile: str = "supported-b323",
    host_authority: str = "supported_b323",
    coinstall_exports: tuple[tuple[str, Path, JsonObject], ...] = (),
) -> dict[str, Any]:
    """Run selected H0/H1/H2 lanes twice and return a redacted summary."""

    started_at_utc = datetime.now(timezone.utc).isoformat()
    started_at_local = datetime.now().astimezone().isoformat()
    implementation_commit, public_worktree_clean = _git_snapshot()
    if timeout <= 0 or timeout > 240:
        raise ValueError("timeout must be between 0 and 240 seconds")
    # IMPORTANT: the child changes cwd, so all execution identities must be absolute.
    host_python = host_python.resolve()
    host_root = host_root.resolve()
    if artifact_path is not None:
        artifact_path = artifact_path.resolve()
    private_roots = _run_private_roots(
        host_python=host_python,
        host_root=host_root,
        artifact_path=artifact_path,
    )
    if not host_python.is_file():
        raise HostBlockedError("selected host interpreter is unavailable")
    missing = missing_host_dependencies(host_python)
    if missing:
        raise HostBlockedError(
            "host interpreter is missing declared dependencies: " + ", ".join(missing)
        )
    host_dependency_versions = _host_dependency_versions(host_python)
    version, revision = _host_metadata(host_root)
    if version != expected_host_version or revision != expected_host_revision:
        raise HostBlockedError(
            f"host pin mismatch (expected {expected_host_version}/{expected_host_revision[:12]})"
        )
    native_source = _native_source_evidence(host_root, profile=native_source_profile)
    unknown_modes = set(modes) - set(SUPPORTED_FIXTURE_MODES)
    if unknown_modes:
        raise ValueError(f"unsupported fixture mode(s): {sorted(unknown_modes)}")

    fixture_paths = {
        "base": ROOT / "workflows" / "m3_07_h3_context_base.json",
        "reference": ROOT / "workflows" / "m3_07_h3_context_reference.json",
        "full_reference": ROOT / "workflows" / "m6_07_h3_context_full_reference.json",
        "audit_override": ROOT / "workflows" / "m7_03_h3_context_audit_override.json",
        "provider_transparency": ROOT / "workflows" / "m7_04_h3_context_provider_transparency.json",
        "reliability": ROOT / "workflows" / "m7_05_h3_context_reliability.json",
        "perception_producers": ROOT / "workflows" / "m15_01_perception_producers.json",
        "downstream_producers": ROOT / "workflows" / "m15_02_downstream_producers.json",
        "local_reconstruction": ROOT / "workflows" / "m13_10_local_reconstruction.json",
        "product_shell_base": ROOT / "workflows" / "m15_03_product_shell_base.json",
        "product_shell_reference": ROOT / "workflows" / "m15_03_product_shell_reference.json",
        "assistant_base": ROOT / "workflows" / "m15_09_assistant_base.json",
        "assistant_reference": ROOT / "workflows" / "m15_09_assistant_reference.json",
    }
    fixture_modes = tuple(
        mode for mode in modes if mode not in {"media_lifecycle", "native_mode_contract"}
    )
    h2_enabled = "media_lifecycle" in modes
    native_contract_enabled = "native_mode_contract" in modes
    fixtures = {mode: load_fixture(fixture_paths[mode]) for mode in fixture_modes}
    for fixture in fixtures.values():
        validate_fixture(fixture)
    migration_audits: dict[str, JsonObject] = {}
    for mode in (
        "product_shell_base",
        "product_shell_reference",
        "assistant_base",
        "assistant_reference",
    ):
        audit = audit_workflow_migration(load_fixture(fixture_paths[mode]))
        if audit.disposition is not MigrationDisposition.ACCEPTED:
            raise HostDriverError(f"workflow migration audit rejected {mode}: {audit.reason_code}")
        migration_audits[mode] = audit.to_wire()
    fixture_sha256 = {
        mode: hashlib.sha256(fixture_paths[mode].read_bytes()).hexdigest() for mode in fixture_modes
    }

    planning_root = ROOT / ".planning"
    planning_root.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    lifecycle_cycles: list[JsonObject] = []
    browser_result: JsonObject | None = None
    native_contract_evidence: JsonObject | None = None
    with _OwnedHostTemporaryRoot(planning_root) as base_root:
        roots = {
            "models": base_root / "models",
            "input": base_root / "input",
            "output": base_root / "output",
            "temp": base_root / "temp",
            "user": base_root / "user",
            "custom_nodes": base_root / "custom_nodes",
        }
        for directory in roots.values():
            directory.mkdir(parents=True, exist_ok=True)
        # IMPORTANT: ComfyUI 0.31 initializes its SQLite database below the named user profile;
        # create only the driver-owned profile so latest-host startup cannot fall back elsewhere.
        (roots["user"] / "default").mkdir()
        if h2_enabled:
            canary_root = base_root / "h2-canary"
            canary_root.mkdir()
            (canary_root / ".h3-context-owned-h2-canary").write_text(
                "h3-context-h2-canary/1\n",
                encoding="utf-8",
            )
        if "full_reference" in modes:
            _write_full_reference_media(roots["input"])
        elif "perception_producers" in modes:
            _write_perception_media(roots["input"])
        else:
            _write_reference_images(roots["input"])
        subject_link = roots["custom_nodes"] / "ComfyUI-H3-Context"
        artifact_sha256: str | None = None
        if artifact_path is None:
            subject_link.symlink_to(ROOT, target_is_directory=True)
        else:
            artifact_sha256 = _deploy_subject(
                subject_path=subject_link,
                staging_path=base_root / "artifact",
                artifact_path=artifact_path,
            )
        coinstallation_evidence: list[JsonObject] = []
        sidebar_id_by_label = {
            "openclaw": "comfyui-openclaw",
            "doctor": "comfyui-doctor",
        }
        for label, export_root, export_evidence in coinstall_exports:
            if label not in sidebar_id_by_label or not export_root.is_dir():
                raise HostBlockedError("qualified co-installation export is unavailable")
            destination = roots["custom_nodes"] / f"ComfyUI-{label.title()}"
            if destination.exists():
                raise HostDriverError("co-installation destination already exists")
            shutil.copytree(export_root, destination)
            retained = dict(export_evidence)
            retained["sidebar_id"] = sidebar_id_by_label[label]
            coinstallation_evidence.append(retained)
        artifact_version = _project_version(subject_link)
        if artifact_version != "0.1.0":
            raise HostBlockedError("exact host artifact version is not 0.1.0")
        registration_safety = _run_registration_safety_probe(host_python, subject_link)
        if native_contract_enabled:
            _write_native_contract_canary(roots["custom_nodes"])

        registered_backend_ids: int | None = None
        registration_manifests: list[JsonObject] = []

        for cycle in ("fresh", "reload"):
            port = select_unused_port()
            readiness: JsonObject = {}
            cycle_started = time.monotonic()
            process, log_path = _start_host(
                host_python=host_python,
                host_root=host_root,
                base_root=base_root,
                port=port,
                enable_h2_canary=h2_enabled,
                isolate_coinstallation=bool(coinstall_exports),
            )
            base_url = f"http://127.0.0.1:{port}"
            shutdown_ok = False
            primary_failure: str | None = None
            try:
                system_stats = _wait_for_json(
                    base_url,
                    "/system_stats",
                    timeout=timeout,
                    process=process,
                    readiness_evidence=readiness,
                )
                info = _fetch_registration(
                    base_url,
                    timeout=timeout,
                    process=process,
                    include_h2_canary=h2_enabled,
                )
                registration_manifest = _registration_evidence(info, include_h2_canary=h2_enabled)
                registration_manifest["cycle"] = cycle
                registration_manifests.append(registration_manifest)
                if (
                    len(registration_manifests) > 1
                    and registration_manifests[0]["object_info_sha256"]
                    != registration_manifest["object_info_sha256"]
                ):
                    raise HostDriverError("object-info manifest drifted across reload")
                if cycle == "fresh":
                    if native_contract_enabled:
                        native_contract_evidence = _queue_native_contract_probe(
                            base_url,
                            timeout=timeout,
                            native_schema=registration_manifest["native_schema"],
                        )
                        summaries.append(
                            {
                                "fixture_id": "m15-05-native-mode-contract",
                                "status": "PASS",
                                "execution_projection": ("real_native_weight_callable_stub_only"),
                                "qualified_modes": [
                                    item["task_mode"]
                                    for item in native_contract_evidence["mode_receipts"]
                                ],
                            }
                        )
                    for mode in fixture_modes:
                        native_qualification = None
                        if mode in {"product_shell_reference", "assistant_reference"}:
                            native_qualification = _native_graph_qualification(
                                fixtures[mode], registration_manifest["native_schema"]
                            )
                        summaries.append(
                            _queue_fixture(
                                base_url,
                                fixtures[mode],
                                timeout=timeout,
                                native_qualification=native_qualification,
                            )
                        )
                    if h2_enabled:
                        summaries.append(_queue_h2_canary(base_url, timeout=timeout))
                    _queue_invalid_prompt(base_url)
                    if browser_e2e:
                        browser_result = _run_browser_e2e(
                            base_url,
                            timeout=timeout,
                            output_root=base_root / "browser-output",
                            expected_sidebar_ids=tuple(
                                sidebar_id_by_label[label] for label, _, _ in coinstall_exports
                            ),
                        )
                else:
                    summaries.append({"fixture_id": "reload-registration", "status": "PASS"})
                if not isinstance(system_stats, dict):
                    raise HostDriverError("host system_stats response is not an object")
                backend_ids = _expected_registration_ids() - NATIVE_NODE_IDS
                cycle_backend_count = len(backend_ids & set(info))
                if registered_backend_ids not in (None, cycle_backend_count):
                    raise HostDriverError("backend registration count drifted across reload")
                registered_backend_ids = cycle_backend_count
                summaries.append(
                    {
                        "cycle": cycle,
                        "status": "PASS",
                        "registered_project_ids": len(
                            (
                                _expected_registration_ids(include_h2_canary=h2_enabled)
                                - NATIVE_NODE_IDS
                            )
                            & set(info)
                        ),
                        "registered_native_ids": len(NATIVE_NODE_IDS),
                        "host_version": version,
                        "host_revision": revision,
                        "object_info_count": len(info),
                        "object_info_sha256": registration_manifest["object_info_sha256"],
                    }
                )
            except HostDriverError as exc:
                log_tail = _read_log_tail(
                    log_path,
                    (*private_roots, base_root),
                )
                primary_failure = f"{exc}; redacted_log_tail={log_tail}"
            finally:
                cleanup_failures: list[str] = []
                try:
                    shutdown_ok = _stop_host(process, timeout=min(timeout, 10.0))
                except (OSError, subprocess.TimeoutExpired) as exc:
                    shutdown_ok = False
                    cleanup_failures.append(f"host shutdown raised {type(exc).__name__}")
                if not shutdown_ok:
                    cleanup_failures.append("owned host process did not shut down")
                port_released = False
                try:
                    release_deadline = time.monotonic() + min(timeout, 10.0)
                    while not _port_is_released(port) and time.monotonic() < release_deadline:
                        time.sleep(POLL_INTERVAL_SECONDS)
                    port_released = _port_is_released(port)
                except OSError as exc:
                    cleanup_failures.append(f"port release probe raised {type(exc).__name__}")
                if not port_released:
                    cleanup_failures.append("host port was not released after shutdown")
                lifecycle_cycles.append(
                    {
                        "cycle": cycle,
                        "loopback_address": "127.0.0.1",
                        "port": port,
                        "readiness": readiness,
                        "shutdown": "PASS" if shutdown_ok else "FAIL",
                        "port_released": port_released,
                        "cleanup": "FAIL" if cleanup_failures else "PASS",
                        "cleanup_failure": (
                            " | ".join(cleanup_failures) if cleanup_failures else None
                        ),
                        "duration_seconds": round(time.monotonic() - cycle_started, 6),
                    }
                )
            cycle_error = _host_cycle_error(primary_failure, lifecycle_cycles)
            if cycle_error is not None:
                raise cycle_error
    ended_at_utc = datetime.now(timezone.utc).isoformat()
    ended_at_local = datetime.now().astimezone().isoformat()
    if registered_backend_ids is None:
        raise HostDriverError("host registration evidence is unavailable")
    frontend_canary = build_exact_host_canary_report(
        version,
        revision,
        registered_project_ids=registered_backend_ids,
        browser_supported=browser_result is not None,
        expected_version=expected_host_version,
        expected_revision=expected_host_revision,
        authority=host_authority,
    )
    lanes = ["H0"]
    if fixture_modes or native_contract_enabled:
        lanes.append("H1")
    if h2_enabled:
        lanes.append("H2")
    return {
        "schema": HOST_REPORT_SCHEMA,
        "status": "PASS",
        "exact_command": list(exact_command) if exact_command else ["direct_api_call"],
        "started_at_utc": started_at_utc,
        "started_at_local": started_at_local,
        "ended_at_utc": ended_at_utc,
        "ended_at_local": ended_at_local,
        "implementation_commit": implementation_commit,
        "public_worktree_clean": public_worktree_clean,
        "driver_interpreter": {
            "executable": redact_text(sys.executable, private_roots),
            "version": sys.version.split()[0],
        },
        "host_interpreter": {
            "executable": redact_text(str(host_python), private_roots),
            "version": _python_version(host_python),
        },
        "host_dependency_versions": host_dependency_versions,
        "execution_platform": {
            "os_name": os.name,
            "sys_platform": sys.platform,
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "lanes": lanes,
        "selected_modes": list(modes),
        "host_version": version,
        "host_revision": revision,
        "artifact_version": artifact_version,
        "fixture_sha256": fixture_sha256,
        "frontend_canary": frontend_canary,
        "browser_e2e": browser_result,
        "frontend_fallbacks": _frontend_fallback_dispositions(browser_result),
        "registration_safety": registration_safety,
        "native_source": native_source,
        "coinstallation": coinstallation_evidence,
        "coinstallation_runtime_isolation": (
            {
                "state_root": "driver_owned_temporary",
                "ephemeral_auth": True,
                "external_user_state": False,
            }
            if coinstall_exports
            else None
        ),
        "native_contract": native_contract_evidence,
        "registration_manifests": registration_manifests,
        "migration_audits": migration_audits,
        "cycles": ["fresh", "reload"],
        "fixtures": summaries,
        "loopback_cycles": lifecycle_cycles,
        "first_failure": None,
        "port_release": "PASS",
        "cleanup": "PASS",
        "models_loaded": False,
        "provider_calls": False,
        "media_uploaded": False,
        "artifact_sha256": artifact_sha256,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host-python",
        type=Path,
        default=(
            Path(os.environ["H3_CONTEXT_HOST_PYTHON"])
            if os.environ.get("H3_CONTEXT_HOST_PYTHON")
            else None
        ),
        help="explicit ComfyUI host interpreter (or H3_CONTEXT_HOST_PYTHON)",
    )
    parser.add_argument("--host-root", type=Path, default=DEFAULT_HOST_ROOT)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--mode", choices=SUPPORTED_FIXTURE_MODES, action="append")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--browser-e2e", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(raw_args)
    private_roots = _run_private_roots(
        host_python=args.host_python,
        host_root=args.host_root,
        artifact_path=args.artifact,
        report_path=args.report,
    )
    exact_command = (
        redact_text(sys.executable, private_roots),
        "scripts/m3_08_host_e2e.py",
        *(redact_text(value, private_roots) for value in raw_args),
    )
    began_utc = datetime.now(timezone.utc).isoformat()
    began_local = datetime.now().astimezone().isoformat()
    if args.host_python is None:
        result: JsonObject = {
            "schema": HOST_REPORT_SCHEMA,
            "status": "BLOCKED",
            "reason": "host interpreter is not configured",
            "first_failure": {
                "stage": "preflight",
                "kind": "host_interpreter_not_configured",
            },
            "exact_command": list(exact_command),
            "started_at_utc": began_utc,
            "started_at_local": began_local,
            "ended_at_utc": datetime.now(timezone.utc).isoformat(),
            "ended_at_local": datetime.now().astimezone().isoformat(),
        }
        if args.report is not None:
            _write_host_report(args.report, result)
        print(json.dumps(result, sort_keys=True))
        return 2
    try:
        result = run_supported_host(
            host_python=args.host_python,
            host_root=args.host_root,
            timeout=args.timeout,
            modes=tuple(args.mode or ("base", "reference")),
            artifact_path=args.artifact,
            exact_command=exact_command,
            browser_e2e=args.browser_e2e,
        )
    except HostBlockedError as exc:
        result = {
            "schema": HOST_REPORT_SCHEMA,
            "status": "BLOCKED",
            "reason": redact_text(str(exc), private_roots),
            "first_failure": {"stage": "host_preflight", "kind": "blocked"},
            "exact_command": list(exact_command),
            "started_at_utc": began_utc,
            "started_at_local": began_local,
            "ended_at_utc": datetime.now(timezone.utc).isoformat(),
            "ended_at_local": datetime.now().astimezone().isoformat(),
        }
        if args.report is not None:
            _write_host_report(args.report, result)
        print(json.dumps(result, sort_keys=True))
        return 2
    except (HostDriverError, ValueError) as exc:
        result = {
            "schema": HOST_REPORT_SCHEMA,
            "status": "FAIL",
            "reason": redact_text(str(exc), private_roots),
            "first_failure": {"stage": "host_execution", "kind": "assertion_or_lifecycle"},
            "exact_command": list(exact_command),
            "started_at_utc": began_utc,
            "started_at_local": began_local,
            "ended_at_utc": datetime.now(timezone.utc).isoformat(),
            "ended_at_local": datetime.now().astimezone().isoformat(),
        }
        if isinstance(exc, HostDriverError):
            for field in ("cleanup", "cleanup_failure", "port_release", "loopback_cycles"):
                if field in exc.evidence:
                    result[field] = exc.evidence[field]
        if args.report is not None:
            _write_host_report(args.report, result)
        print(json.dumps(result, sort_keys=True))
        return 1
    if args.report is not None:
        _write_host_report(args.report, result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
