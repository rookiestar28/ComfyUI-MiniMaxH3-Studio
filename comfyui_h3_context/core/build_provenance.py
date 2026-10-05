"""Strict, dependency-free build-provenance and served-bundle observations."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any, cast

BUILD_PROVENANCE_SCHEMA = "h3-context-build-provenance/1"
BUILD_IDENTITY_SCHEMA = "h3-context-build-identity/1"
BUILD_PROVENANCE_RESPONSE_SCHEMA = "h3.context.build_provenance_response.v1"
BUILD_PROVENANCE_PATH = (
    Path(__file__).resolve().parents[1] / "contracts" / "build_provenance_v1.json"
)
SERVED_BUNDLE_PATH = Path(__file__).resolve().parents[1] / "web" / "h3-context-sidebar.js"
MAX_PROVENANCE_BYTES = 32_768
MAX_BUNDLE_BYTES = 16 * 1024 * 1024

_OID = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,199}$")
_BUNDLE_RELATIVE_PATH = "comfyui_h3_context/web/h3-context-sidebar.js"
_BUILDER_ID = "scripts/frontend_build_report.py"
_BUILD_TYPE = "h3-context-frontend-offline/1"

JsonObject = dict[str, Any]


class BuildProvenanceError(RuntimeError):
    """Raised when provenance or the observed served artifact is not closed and safe."""


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")


def _digest_value(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value)).hexdigest()}"


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BuildProvenanceError("duplicate provenance member")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    raise BuildProvenanceError("invalid provenance constant")


def _object(value: object, keys: set[str], label: str) -> JsonObject:
    if type(value) is not dict or set(cast(dict[object, object], value)) != keys:
        raise BuildProvenanceError(f"invalid {label}")
    return cast(JsonObject, value)


def _string(value: object, pattern: re.Pattern[str], label: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise BuildProvenanceError(f"invalid {label}")
    return value


def _dependency(value: object) -> JsonObject:
    if type(value) is not dict:
        raise BuildProvenanceError("invalid resolved dependency")
    dependency = cast(JsonObject, value)
    uri = _string(dependency.get("uri"), _TOKEN, "dependency uri")
    if set(dependency) == {"uri", "digest"}:
        digest = _string(dependency.get("digest"), _DIGEST, "dependency digest")
        return {"uri": uri, "digest": digest}
    if set(dependency) == {"uri", "version"}:
        version = _string(dependency.get("version"), _TOKEN, "dependency version")
        return {"uri": uri, "version": version}
    raise BuildProvenanceError("invalid resolved dependency")


def decode_build_provenance(payload: str | bytes | bytearray) -> JsonObject:
    """Decode the one closed provenance wire shape without executing schema tooling."""

    if type(payload) is str:
        try:
            raw = payload.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise BuildProvenanceError("provenance is not strict UTF-8") from exc
    elif type(payload) is bytes:
        raw = payload
    elif type(payload) is bytearray:
        raw = bytes(payload)
    else:
        raise BuildProvenanceError("provenance requires exact text or bytes")
    if not raw or len(raw) > MAX_PROVENANCE_BYTES:
        raise BuildProvenanceError("provenance exceeds the byte bound")
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except BuildProvenanceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BuildProvenanceError("provenance is not strict JSON") from exc

    record = _object(
        value,
        {
            "schema",
            "builder",
            "build_type",
            "external_parameters",
            "resolved_dependencies",
            "invocation",
            "bundle",
            "embedded_identity",
        },
        "provenance record",
    )
    if record["schema"] != BUILD_PROVENANCE_SCHEMA:
        raise BuildProvenanceError("invalid provenance schema")
    builder = _object(record["builder"], {"id"}, "builder")
    builder_id = _string(builder["id"], _TOKEN, "builder id")
    build_type = _string(record["build_type"], _TOKEN, "build type")
    if builder_id != _BUILDER_ID or build_type != _BUILD_TYPE:
        raise BuildProvenanceError("invalid build authority")
    external = _object(
        record["external_parameters"],
        {"source_commit", "source_tree", "source_inputs_sha256"},
        "external parameters",
    )
    source_commit = _string(external["source_commit"], _OID, "source commit")
    source_tree = _string(external["source_tree"], _OID, "source tree")
    source_inputs = _string(external["source_inputs_sha256"], _DIGEST, "source-input digest")
    dependencies_value = record["resolved_dependencies"]
    if type(dependencies_value) is not list or not 1 <= len(dependencies_value) <= 32:
        raise BuildProvenanceError("invalid resolved dependencies")
    dependencies = [_dependency(item) for item in dependencies_value]
    dependency_uris = [item["uri"] for item in dependencies]
    if dependency_uris != sorted(dependency_uris) or len(set(dependency_uris)) != len(
        dependency_uris
    ):
        raise BuildProvenanceError("resolved dependencies are duplicated or unordered")
    invocation = _object(record["invocation"], {"id", "started_on", "finished_on"}, "invocation")
    invocation_id = _string(invocation["id"], _DIGEST, "invocation id")
    started_on = _string(invocation["started_on"], _TIMESTAMP, "started timestamp")
    finished_on = _string(invocation["finished_on"], _TIMESTAMP, "finished timestamp")
    if started_on != finished_on:
        raise BuildProvenanceError("non-reproducible invocation timestamps")
    bundle = _object(record["bundle"], {"path", "sha256", "size"}, "bundle")
    if bundle["path"] != _BUNDLE_RELATIVE_PATH:
        raise BuildProvenanceError("invalid bundle path")
    bundle_digest = _string(bundle["sha256"], _DIGEST, "bundle digest")
    bundle_size = bundle["size"]
    if type(bundle_size) is not int or not 1 <= bundle_size <= MAX_BUNDLE_BYTES:
        raise BuildProvenanceError("invalid bundle size")
    identity = _object(
        record["embedded_identity"],
        {
            "schema",
            "builder_id",
            "source_commit",
            "source_tree",
            "source_inputs_sha256",
            "resolved_dependencies_sha256",
        },
        "embedded identity",
    )
    if identity["schema"] != BUILD_IDENTITY_SCHEMA:
        raise BuildProvenanceError("invalid embedded identity schema")
    if (
        identity["builder_id"] != builder_id
        or identity["source_commit"] != source_commit
        or identity["source_tree"] != source_tree
        or identity["source_inputs_sha256"] != source_inputs
    ):
        raise BuildProvenanceError("embedded identity disagrees with provenance")
    dependency_digest = _string(
        identity["resolved_dependencies_sha256"],
        _DIGEST,
        "resolved-dependency digest",
    )
    normalized_identity = {
        "schema": BUILD_IDENTITY_SCHEMA,
        "builder_id": builder_id,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "source_inputs_sha256": source_inputs,
        "resolved_dependencies_sha256": dependency_digest,
    }
    normalized_bundle = {
        "path": _BUNDLE_RELATIVE_PATH,
        "sha256": bundle_digest,
        "size": bundle_size,
    }
    # CRITICAL: format validation alone is not provenance validation. Recompute both content
    # bindings so a well-shaped but internally substituted record cannot reach the route.
    if dependency_digest != _digest_value(dependencies):
        raise BuildProvenanceError("resolved-dependency digest disagrees with provenance")
    if invocation_id != _digest_value(
        {"identity": normalized_identity, "bundle": normalized_bundle}
    ):
        raise BuildProvenanceError("invocation id disagrees with provenance")
    return {
        "schema": BUILD_PROVENANCE_SCHEMA,
        "builder": {"id": builder_id},
        "build_type": build_type,
        "external_parameters": {
            "source_commit": source_commit,
            "source_tree": source_tree,
            "source_inputs_sha256": source_inputs,
        },
        "resolved_dependencies": dependencies,
        "invocation": {
            "id": invocation_id,
            "started_on": started_on,
            "finished_on": finished_on,
        },
        "bundle": normalized_bundle,
        "embedded_identity": normalized_identity,
    }


def load_build_provenance(path: Path = BUILD_PROVENANCE_PATH) -> JsonObject:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_PROVENANCE_BYTES:
            raise BuildProvenanceError("provenance path is not a bounded regular file")
        return decode_build_provenance(path.read_bytes())
    except BuildProvenanceError:
        raise
    except OSError as exc:
        raise BuildProvenanceError("provenance record is unavailable") from exc


def observe_served_bundle(record: JsonObject, path: Path = SERVED_BUNDLE_PATH) -> JsonObject:
    """Hash only the exact regular served entry and compare it with the package record."""

    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= MAX_BUNDLE_BYTES:
            raise BuildProvenanceError("served bundle is not a bounded regular file")
        payload = path.read_bytes()
    except BuildProvenanceError:
        raise
    except OSError as exc:
        raise BuildProvenanceError("served bundle is unavailable") from exc
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    expected = cast(JsonObject, record.get("bundle"))
    return {
        "path": _BUNDLE_RELATIVE_PATH,
        "size": len(payload),
        "sha256": digest,
        "matches_record": expected.get("sha256") == digest and expected.get("size") == len(payload),
    }


__all__ = [
    "BUILD_IDENTITY_SCHEMA",
    "BUILD_PROVENANCE_PATH",
    "BUILD_PROVENANCE_RESPONSE_SCHEMA",
    "BUILD_PROVENANCE_SCHEMA",
    "SERVED_BUNDLE_PATH",
    "BuildProvenanceError",
    "decode_build_provenance",
    "load_build_provenance",
    "observe_served_bundle",
]
