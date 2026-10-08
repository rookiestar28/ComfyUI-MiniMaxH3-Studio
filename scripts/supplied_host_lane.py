"""Portable supplied-host inspection, exact row execution and explicit maintenance.

The lane is intentionally explicit: ``run`` never synchronizes or restarts a host, and maintenance
never runs without a separately selected subcommand and a pinned process identity.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

MAX_CANDIDATE_FILES = 512
MAX_REPORT_STRING = 400
MAX_HTTP_BYTES = 2 * 1_048_576
MAX_M25_52_TIMELINE_CAPTURE_BYTES = 192 * 1_024
ELIGIBLE_SUFFIXES = frozenset({".py", ".json", ".typed", ".ttf", ".js", ".css"})
ROW_CONTROL_ALLOWLIST = frozenset(
    {
        "H3_CONTEXT_DEPLOYMENT_URL",
        "H3_CONTEXT_EMBEDDED_AUDIO_HOST_FIXTURE",
        "H3_CONTEXT_EMBEDDED_AUDIO_OBSERVER",
        "H3_CONTEXT_EXPECTED_SIDEBAR_IDS",
        "H3_CONTEXT_HOST_HEADED",
        "H3_CONTEXT_M25_34_EXPECTED_SOURCE",
        "H3_CONTEXT_M25_34_MEDIA_JOURNEY",
        "H3_CONTEXT_M25_34_OBSERVER_FFMPEG",
        "H3_CONTEXT_M25_34_OBSERVER_FFPROBE",
        "H3_CONTEXT_M25_34_RENDER",
        "H3_CONTEXT_M25_34_SOURCE_MEDIA",
        "H3_CONTEXT_M25_43_EVIDENCE",
        "H3_CONTEXT_M25_43_HOST",
        "H3_CONTEXT_M25_45_HOST",
        "H3_CONTEXT_M25_46_HOST",
        "H3_CONTEXT_M25_47_HOST",
        "H3_CONTEXT_M25_48_ARTIFACT_LOCATOR",
        "H3_CONTEXT_M25_48_HOST",
        "H3_CONTEXT_M25_49_HOST",
        "H3_CONTEXT_M25_52_HOST",
        "H3_CONTEXT_M25_52_RESOURCE_RECEIPT",
        "H3_CONTEXT_M25_53_HOST",
        "H3_CONTEXT_M25_53_EXPECTATION",
        "H3_CONTEXT_M25_53_OBSERVER_FFMPEG",
        "H3_CONTEXT_M25_53_OBSERVER_FFPROBE",
        "H3_CONTEXT_M25_53_SCHEDULER_TRACE",
        "H3_CONTEXT_M25_55_HOST",
        "H3_CONTEXT_M25_56_UX14",
        "H3_CONTEXT_M25_57_HOST",
        "H3_CONTEXT_M25_59_HOST",
        "H3_CONTEXT_NLE_WORKSPACE_HOST",
        "H3_CONTEXT_NLE_WORKSPACE_FIXTURE",
        "H3_CONTEXT_WEIGHT_SAMPLE",
    }
)
#: Row controls that are switches: admitted only with the value "1".
_SWITCH_ROW_CONTROLS = frozenset(
    {
        "H3_CONTEXT_HOST_HEADED",
        "H3_CONTEXT_M25_34_RENDER",
        "H3_CONTEXT_M25_53_SCHEDULER_TRACE",
        "H3_CONTEXT_M25_56_UX14",
        "H3_CONTEXT_WEIGHT_SAMPLE",
    }
)
DENIED_EXECUTION_FLAGS = frozenset(
    {
        "H3_CONTEXT_M23_19_REAL_I2VA",
        "H3_CONTEXT_M23_19_HOST_PYTHON",
        "H3_CONTEXT_M23_32_LATE_ARTIFACT_LOCATOR",
        "H3_CONTEXT_M23_32_TERMINAL_WAITING",
        "H3_CONTEXT_M23_39_RUNTIME_FAILURE",
    }
)
RECEIPT_SCHEMA = "h3.context.host_log_scan_receipt.v1"
VERDICT_SCHEMA = "h3.context.supplied_host_lane_result.v1"
M25_52_SPEC = "tests/e2e/journeys/host/m25_52EmbeddedAudioWaveform.spec.ts"
M25_52_TITLE = "M25-52 proves the real peaks route and mixed-load playback on the supplied host"
M25_52_EVIDENCE_ATTACHMENT = "m25-52-real-route-mixed-load"
M25_52_EVIDENCE_SCHEMA = "h3.context.m25_52.real_route_mixed_load.v1"
M25_52_FAILURE_ATTACHMENT = "m25-52-full-route-failure-facts"
M25_52_FAILURE_SCHEMA = "h3.context.m25_52.full_route_failure_facts.v1"
M25_53_SPEC = "tests/e2e/journeys/host/m25_53ReferenceFidelity.spec.ts"
M25_53_TITLE = "M25-53 reference fidelity editing through verified export"
M25_53_EVIDENCE_ATTACHMENT = "m25-53-reference-fidelity"
M25_53_EVIDENCE_SCHEMA = "h3.context.m25_53.reference_fidelity.v1"
M25_56_SPEC = "tests/e2e/journeys/host/productionAuthoringImportHost.spec.ts"
M25_56_TITLE = (
    "M25-56 supplied host: a real Production import decodes, plays, pauses and releases its "
    "monitor source"
)
M25_56_EVIDENCE_ATTACHMENT = "m25-56-ux14-host"
M25_56_EVIDENCE_SCHEMA = "h3.context.m25_56.ux14_host_evidence.v1"
M25_56_MINIMUM_DECODED_PIXEL_SAMPLES = 16


class AdmissionError(RuntimeError):
    """An explicit host, candidate, selection or result admission check failed."""


@dataclass(frozen=True)
class RowSelection:
    spec: str
    title: str


@dataclass(frozen=True)
class ExpectedProcess:
    pid: int
    created: float
    executable: str
    cwd: str
    entrypoint: str
    port: int


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    created: float
    executable: str
    cwd: str
    entrypoint: str
    port: int
    listening: bool


@dataclass(frozen=True)
class CandidateFileChange:
    relative_path: str
    source: Path
    destination: Path
    destination_existed: bool


@dataclass(frozen=True)
class CandidateChanges:
    source_root: Path
    destination_root: Path
    file_count: int
    changed: tuple[CandidateFileChange, ...]
    extras: tuple[str, ...]
    inventory_sha256: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def _resolved(value: str | Path) -> Path:
    return Path(value).resolve(strict=True)


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(str(Path(left).resolve(strict=False))) == os.path.normcase(
        str(Path(right).resolve(strict=False))
    )


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (callable(is_junction) and is_junction())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_host_url(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("supplied host URL must be an explicit loopback HTTP origin")
    if parsed.path not in ("", "/") or parsed.hostname is None or parsed.port is None:
        raise ValueError("supplied host URL must be an explicit loopback HTTP origin with a port")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as error:
        raise ValueError("supplied host URL must use a numeric loopback address") from error
    if not address.is_loopback:
        raise ValueError("supplied host URL must use a loopback address")
    return parsed.hostname, parsed.port


def parse_row(value: str) -> RowSelection:
    spec, separator, title = value.partition("::")
    normalized = spec.replace("\\", "/")
    if (
        separator == ""
        or not re.fullmatch(r"tests/e2e/journeys/host/[A-Za-z0-9_.-]+\.spec\.ts", normalized)
        or not 1 <= len(title) <= 300
        or any(character in title for character in "\r\n\0")
    ):
        raise ValueError("row must be an exact host spec and full title separated by ::")
    return RowSelection(normalized, title)


def parse_row_environment(values: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, content = value.partition("=")
        if separator == "" or name not in ROW_CONTROL_ALLOWLIST:
            raise ValueError(f"{name or '<empty>'} is not an admitted row control")
        if (
            name in result
            or len(content) > 2048
            or any(character in content for character in "\r\n\0")
        ):
            raise ValueError(f"invalid value for admitted row control {name}")
        if name == "H3_CONTEXT_M25_34_MEDIA_JOURNEY" and content not in {"ready", "install"}:
            raise ValueError(f"invalid value for admitted row control {name}")
        if name == "H3_CONTEXT_M25_34_EXPECTED_SOURCE" and content not in {
            "explicit_override",
            "local_selection",
            "managed",
            "python_prefix",
            "system_path",
        }:
            raise ValueError(f"invalid value for admitted row control {name}")
        # IMPORTANT: the weight-sample control enables a real, weight-backed generation in the
        # registration lifecycle row (the E2E SOP first-start and second-start rows). It is
        # admitted only as an explicit `--row-env`, only as "1", and never inherited. The same
        # closed switch rule applies to the other explicitly admitted execution controls.
        if name in _SWITCH_ROW_CONTROLS and content != "1":
            raise ValueError(f"invalid value for admitted row control {name}")
        result[name] = content
    return result


def _report_specs(report: Mapping[str, object]) -> list[Mapping[str, object]]:
    found: list[Mapping[str, object]] = []

    def visit(suites: object) -> None:
        if not isinstance(suites, list):
            return
        for suite in suites:
            if not isinstance(suite, dict):
                continue
            specs = suite.get("specs")
            if isinstance(specs, list):
                found.extend(item for item in specs if isinstance(item, dict))
            visit(suite.get("suites"))

    visit(report.get("suites"))
    return found


def _canonical_report_file(value: object) -> str:
    normalized = str(value).replace("\\", "/")
    marker = "tests/e2e/"
    if marker in normalized:
        return marker + normalized.rsplit(marker, 1)[1]
    if normalized.startswith("journeys/host/"):
        return "tests/e2e/" + normalized
    return normalized


def admit_collection(
    requested: Sequence[RowSelection], report: Mapping[str, object]
) -> dict[str, RowSelection]:
    if not requested or len(set(requested)) != len(requested):
        raise AdmissionError("selection must contain distinct explicit rows")
    collected: dict[tuple[str, str], str] = {}
    for spec in _report_specs(report):
        identity = (_canonical_report_file(spec.get("file")), str(spec.get("title", "")))
        test_id = spec.get("id")
        if not isinstance(test_id, str) or test_id == "" or identity in collected:
            raise AdmissionError("structured selection is malformed or duplicated")
        collected[identity] = test_id
    expected = {(row.spec, row.title) for row in requested}
    if set(collected) != expected:
        raise AdmissionError("structured selection is not exact")
    return {collected[(row.spec, row.title)]: row for row in requested}


def _decode_json_attachment(
    attachment: Mapping[str, object], *, name: str, maximum_bytes: int
) -> Mapping[str, object] | None:
    if attachment.get("name") != name or attachment.get("contentType") != "application/json":
        return None
    body = attachment.get("body")
    if not isinstance(body, str) or len(body) > maximum_bytes * 2:
        return None
    try:
        raw = base64.b64decode(body, validate=True)
        if len(raw) > maximum_bytes:
            return None
        decoded = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _decode_receipt(attachment: Mapping[str, object]) -> Mapping[str, object] | None:
    return _decode_json_attachment(attachment, name="h3_host_log_scan", maximum_bytes=128 * 1024)


def _decode_m25_52_evidence(
    attachments: Sequence[Mapping[str, object]],
) -> Mapping[str, object] | None:
    matches = [value for value in attachments if value.get("name") == M25_52_EVIDENCE_ATTACHMENT]
    if len(matches) != 1:
        return None
    decoded = _decode_json_attachment(
        matches[0], name=M25_52_EVIDENCE_ATTACHMENT, maximum_bytes=256 * 1024
    )
    if decoded is None or decoded.get("schema") != M25_52_EVIDENCE_SCHEMA:
        return None
    candidate = decoded.get("candidate")
    playback = decoded.get("playback")
    if not isinstance(candidate, dict) or not isinstance(playback, dict):
        return None
    bundle = candidate.get("bundleSha256")
    installed_inventory = candidate.get("installedInventorySha256")
    backend_inventory = candidate.get("backendInventorySha256")
    frames = playback.get("frameSamples")
    if (
        not isinstance(bundle, str)
        or re.fullmatch(r"[0-9a-f]{64}", bundle) is None
        or not isinstance(installed_inventory, str)
        or re.fullmatch(r"[0-9a-f]{64}", installed_inventory) is None
        or not isinstance(backend_inventory, str)
        or re.fullmatch(r"[0-9a-f]{64}", backend_inventory) is None
        or not isinstance(frames, list)
        or not 2 <= len(frames) <= 5_000
        or playback.get("frameSampleCount") != len(frames)
        or any(type(frame) is not int or not 0 <= frame <= 100_000 for frame in frames)
        or any(current != prior + 1 for prior, current in zip(frames, frames[1:], strict=False))
    ):
        return None
    return decoded


def _decode_m25_52_failure_facts(
    attachments: Sequence[Mapping[str, object]],
) -> Mapping[str, object] | None:
    matches = [value for value in attachments if value.get("name") == M25_52_FAILURE_ATTACHMENT]
    if len(matches) != 1:
        return None
    decoded = _decode_json_attachment(
        matches[0], name=M25_52_FAILURE_ATTACHMENT, maximum_bytes=256 * 1024
    )
    if (
        decoded is None
        or decoded.get("schema") != M25_52_FAILURE_SCHEMA
        or not isinstance(decoded.get("stage"), str)
    ):
        return None
    return decoded


def _decode_m25_53_evidence(
    attachments: Sequence[Mapping[str, object]],
) -> Mapping[str, object] | None:
    matches = [value for value in attachments if value.get("name") == M25_53_EVIDENCE_ATTACHMENT]
    if len(matches) != 1:
        return None
    decoded = _decode_json_attachment(
        matches[0], name=M25_53_EVIDENCE_ATTACHMENT, maximum_bytes=512 * 1024
    )
    if decoded is None or decoded.get("schema") != M25_53_EVIDENCE_SCHEMA:
        return None
    candidate = decoded.get("candidate")
    sources = decoded.get("sources")
    export = decoded.get("export")
    observations = export.get("observations") if isinstance(export, dict) else None
    observer = export.get("observer") if isinstance(export, dict) else None
    asset_ids = sources.get("assetIds") if isinstance(sources, dict) else None
    if (
        not isinstance(candidate, dict)
        or not isinstance(export, dict)
        or not isinstance(observations, dict)
        or not isinstance(observer, dict)
        or not isinstance(asset_ids, list)
        or not 3 <= len(asset_ids) <= 12
        or any(
            not isinstance(asset_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", asset_id) is None
            for asset_id in asset_ids
        )
        or len(set(asset_ids)) != len(asset_ids)
        or re.fullmatch(r"[0-9a-f]{64}", str(export.get("downloadedSha256", ""))) is None
        or type(export.get("downloadedBytes")) is not int
        or int(export["downloadedBytes"]) < 1
        or observations.get("independent") is not True
        or observations.get("clipOrderMatched") is not True
        or observations.get("sourceRangesMatched") is not True
        or observations.get("transitionMatched") is not True
        or observations.get("audioMatched") is not True
        or observations.get("visualEditsMatched") is not True
    ):
        return None
    landmarks = observer.get("landmarkSamples")
    opening_silence = observer.get("openingSilence")
    audio_onsets = observer.get("audioOnsets")
    content_tail = observer.get("contentTail")
    if (
        not isinstance(landmarks, list)
        or len(landmarks) != 6
        or any(
            not isinstance(sample, dict)
            or sample.get("assetId") not in asset_ids
            or type(sample.get("sourceFrame")) is not int
            or type(sample.get("outputFrame")) is not int
            or type(sample.get("exactFrameIndexError")) not in (int, float)
            or type(sample.get("adjacentFrameIndexError")) not in (int, float)
            or not math.isfinite(sample["exactFrameIndexError"])
            or not math.isfinite(sample["adjacentFrameIndexError"])
            or sample["exactFrameIndexError"] > 8
            or sample["adjacentFrameIndexError"] - sample["exactFrameIndexError"] <= 2
            or any(
                not isinstance(sample.get(field), dict)
                or any(
                    type(sample[field].get(channel)) is not int
                    for channel in ("red", "green", "blue")
                )
                for field in ("sourceMarker", "outputMarker", "sourceOther", "outputOther")
            )
            for sample in landmarks
        )
        or not isinstance(opening_silence, list)
        or len(opening_silence) != 3
        or any(
            not isinstance(sample, dict)
            or sample.get("assetId") not in asset_ids
            or type(sample.get("sourceRms")) not in (int, float)
            or type(sample.get("outputRms")) not in (int, float)
            or not math.isfinite(sample["sourceRms"])
            or not math.isfinite(sample["outputRms"])
            or sample["sourceRms"] >= 0.003
            or sample["outputRms"] >= 0.008
            for sample in opening_silence
        )
        or not isinstance(audio_onsets, list)
        or len(audio_onsets) != 3
        or any(
            not isinstance(sample, dict)
            or sample.get("assetId") not in asset_ids
            or any(
                type(sample.get(field)) is not int
                for field in (
                    "expectedSourceOnset",
                    "sourceOnset",
                    "expectedOutputOnset",
                    "outputOnset",
                )
            )
            or abs(sample["sourceOnset"] - sample["expectedSourceOnset"]) > 2_048
            or abs(sample["outputOnset"] - sample["expectedOutputOnset"]) > 2_048
            for sample in audio_onsets
        )
        or type(observer.get("tailToneRms")) not in (int, float)
        or not math.isfinite(observer["tailToneRms"])
        or observer["tailToneRms"] <= 0.005
        or type(export.get("frameCount")) is not int
        or export["frameCount"] < 1
        or not isinstance(content_tail, dict)
        or type(content_tail.get("finalVideoFrame")) is not int
        or content_tail["finalVideoFrame"] != export["frameCount"] - 1
        or type(content_tail.get("finalDecodedAudioWindowRms")) not in (int, float)
        or not math.isfinite(content_tail["finalDecodedAudioWindowRms"])
        or content_tail["finalDecodedAudioWindowRms"] <= 0.005
        or content_tail["finalDecodedAudioWindowRms"] != observer["tailToneRms"]
        or type(export.get("decodedAudioSamples")) is not int
        or type(export.get("expectedAudioSamples")) is not int
        or type(export.get("expectedAudioExtentSamples")) is not int
        # The fixed host export profile is 48 kHz at 24 fps, or exactly 2,000 playable samples
        # per output frame. This binds both media streams to the same final placed-content edge.
        or export["expectedAudioSamples"] != export["frameCount"] * 2_000
        # IMPORTANT: the candidate-bound decoder honors the container's final AAC discard
        # padding. Admit the playable timeline extent, not the coded-block ceiling; restoring
        # 1,024-sample rounding rejects correct exports whose timelines end mid-block.
        or export["expectedAudioExtentSamples"] != export["expectedAudioSamples"]
        or abs(export["decodedAudioSamples"] - export["expectedAudioExtentSamples"]) > 1
        or type(observer.get("transitionAlpha")) not in (int, float)
        or not math.isfinite(observer["transitionAlpha"])
        or abs(observer["transitionAlpha"] - 0.5) > 0.03
        or type(observer.get("titleBefore")) is not int
        or type(observer.get("titleDuring")) is not int
        or observer["titleBefore"] >= 10
        or observer["titleDuring"] <= 100
    ):
        return None
    for key in ("bundleSha256", "installedInventorySha256", "backendInventorySha256"):
        if re.fullmatch(r"[0-9a-f]{64}", str(candidate.get(key, ""))) is None:
            return None
    return decoded


def _decode_m25_56_evidence(
    attachments: Sequence[Mapping[str, object]],
) -> Mapping[str, object] | None:
    matches = [value for value in attachments if value.get("name") == M25_56_EVIDENCE_ATTACHMENT]
    if len(matches) != 1:
        return None
    decoded = _decode_json_attachment(
        matches[0], name=M25_56_EVIDENCE_ATTACHMENT, maximum_bytes=128 * 1024
    )
    if decoded is None or decoded.get("schema") != M25_56_EVIDENCE_SCHEMA:
        return None
    candidate = decoded.get("candidate")
    original = decoded.get("originalState")
    current = decoded.get("currentState")
    source = decoded.get("input")
    leases = decoded.get("sourceLease")
    if (
        not isinstance(candidate, dict)
        or re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("bundleSha256", ""))) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("installedInventorySha256", ""))) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("backendInventorySha256", ""))) is None
        or not isinstance(original, dict)
        or not isinstance(current, dict)
        or not isinstance(source, dict)
        or not isinstance(leases, list)
        or not 3 <= len(leases) <= 32
        or decoded.get("disposition") != "not_reproduced"
        or decoded.get("closed") is not True
        or original
        != {
            "monitor": "source_unavailable",
            "audio": "suspended",
            "recoverOffered": True,
        }
        or source.get("kind") != "production_output"
        or type(source.get("selectedOutputs")) is not int
        or not 1 <= source["selectedOutputs"] <= 16
        or source.get("importedAssetIdPresent") is not True
        or type(current.get("beforePlay")) is not int
        or type(current.get("advancedTo")) is not int
        or type(current.get("pausedAt")) is not int
        or not 0 <= current["beforePlay"] <= 10_000_000
        or not 0 <= current["advancedTo"] <= 10_000_000
        or not 0 <= current["pausedAt"] <= 10_000_000
        or current["advancedTo"] <= current["beforePlay"]
        or current["pausedAt"] < current["advancedTo"]
        or current.get("recoverOffered") is not False
        or not isinstance(current.get("audioState"), str)
        or current["audioState"] not in {"silent", "following", "seeking", "suspended"}
        or type(current.get("decodedPixelSamplesAfterPause")) is not int
        or current["decodedPixelSamplesAfterPause"] < M25_56_MINIMUM_DECODED_PIXEL_SAMPLES
        or current.get("monitorStatus") != "Monitor paused."
    ):
        return None
    expected_leases: list[dict[str, object]] = [
        {
            "operation": "create",
            "phase": "before_close",
            "leaseAlias": "monitor-playback-1",
            "scope": "clip",
            "clipIdPresent": True,
            "derivativeKind": "video_proxy",
            "ownerMatchesClip": True,
            "status": 200,
        },
        {
            "operation": "open",
            "phase": "before_close",
            "leaseAlias": "monitor-playback-1",
            "scope": None,
            "clipIdPresent": False,
            "derivativeKind": None,
            "ownerMatchesClip": True,
            "status": 200,
        },
        {
            "operation": "release",
            "phase": "after_close",
            "leaseAlias": "monitor-playback-1",
            "scope": None,
            "clipIdPresent": False,
            "derivativeKind": None,
            "ownerMatchesClip": True,
            "status": 200,
        },
    ]
    if len(leases) != len(expected_leases):
        return None
    for lease, expected in zip(leases, expected_leases, strict=True):
        if not isinstance(lease, dict):
            return None
        if (
            lease.keys() != expected.keys()
            or type(lease.get("clipIdPresent")) is not bool
            or type(lease.get("ownerMatchesClip")) is not bool
            or type(lease.get("status")) is not int
            or lease != expected
        ):
            return None
    return decoded


def _bounded_error_summaries(result: Mapping[str, object] | None) -> list[dict[str, object]]:
    errors = result.get("errors") if result is not None else None
    if not isinstance(errors, list):
        return []
    summaries: list[dict[str, object]] = []
    for error in errors[:4]:
        if not isinstance(error, dict):
            summaries.append({"message": sanitize_text(str(error))})
            continue
        summary: dict[str, object] = {}
        message = error.get("message")
        if isinstance(message, str):
            summary["message"] = sanitize_text(message)
        location = error.get("location")
        if isinstance(location, dict):
            safe_location: dict[str, object] = {}
            filename = location.get("file")
            if isinstance(filename, str):
                safe_location["file"] = sanitize_text(filename)
            for key in ("line", "column"):
                value = location.get(key)
                if type(value) is int and value >= 0:
                    safe_location[key] = value
            if safe_location:
                summary["location"] = safe_location
        summaries.append(summary or {"message": "unstructured Playwright error"})
    return summaries


def admit_results(
    selected: Mapping[str, RowSelection],
    report: Mapping[str, object],
    *,
    expected_bundle_sha256: str | None = None,
    expected_inventory_sha256: str | None = None,
    capture_evidence_dir: Path | None = None,
    capture_attempt: str | None = None,
) -> dict[str, object]:
    specs: dict[str, Mapping[str, object]] = {}
    malformed_result_count = 0
    for spec in _report_specs(report):
        test_id = spec.get("id")
        if not isinstance(test_id, str) or not test_id or test_id in specs:
            malformed_result_count += 1
            continue
        specs[test_id] = spec
    counts = {
        "selected": len(selected),
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "incomplete": 0,
    }
    rows: list[dict[str, object]] = []
    report_errors = report.get("errors")
    global_error_count = (
        len(report_errors)
        if isinstance(report_errors, list)
        else (0 if report_errors is None else 1)
    )
    counts["incomplete"] += global_error_count + malformed_result_count
    extra_ids = sorted(set(specs) - set(selected))
    if extra_ids:
        counts["incomplete"] += len(extra_ids)
    for test_id, row in selected.items():
        reported = specs.get(test_id)
        status = "missing"
        reasons: list[str] = []
        receipt: Mapping[str, object] | None = None
        evidence: Mapping[str, object] | None = None
        failure_facts: Mapping[str, object] | None = None
        result: Mapping[str, object] | None = None
        if reported is None:
            reasons.append("missing_result")
        else:
            if (
                _canonical_report_file(reported.get("file")) != row.spec
                or str(reported.get("title", "")) != row.title
            ):
                reasons.append("result_identity_mismatch")
            tests = reported.get("tests")
            test = (
                tests[0]
                if isinstance(tests, list) and len(tests) == 1 and isinstance(tests[0], dict)
                else None
            )
            results = test.get("results") if isinstance(test, dict) else None
            if (
                not isinstance(results, list)
                or len(results) != 1
                or not isinstance(results[0], dict)
            ):
                reasons.append("missing_or_multiple_attempts")
            else:
                result = results[0]
                status = str(result.get("status", "missing"))
                if status == "skipped":
                    counts["skipped"] += 1
                    reasons.append("skipped")
                else:
                    counts["executed"] += 1
                if status != "passed":
                    reasons.append("test_not_passed")
                if result.get("retry") != 0:
                    reasons.append("retry_not_zero")
                if isinstance(test, dict) and test.get("expectedStatus") != "passed":
                    reasons.append("nonordinary_expected_status")
                attachments = result.get("attachments")
                attachment_rows = [
                    value
                    for value in (attachments if isinstance(attachments, list) else [])
                    if isinstance(value, dict)
                ]
                decoded = [
                    item
                    for item in (_decode_receipt(value) for value in attachment_rows)
                    if item is not None
                ]
                if len(decoded) != 1:
                    reasons.append("missing_or_duplicate_scan_receipt")
                else:
                    receipt = decoded[0]
                    if (
                        receipt.get("schema") != RECEIPT_SCHEMA
                        or receipt.get("test_id") != test_id
                        or receipt.get("retry") != 0
                    ):
                        reasons.append("scan_receipt_identity_mismatch")
                    if (
                        receipt.get("status") != "clean"
                        or receipt.get("complete") is not True
                        or receipt.get("owned_count") != 0
                    ):
                        reasons.append("scan_not_complete_clean")
                if (row.spec, row.title) == (M25_52_SPEC, M25_52_TITLE):
                    failure_facts = _decode_m25_52_failure_facts(attachment_rows)
                    evidence = _decode_m25_52_evidence(attachment_rows)
                    evidence_candidate = evidence.get("candidate") if evidence is not None else None
                    if (
                        evidence is None
                        or not isinstance(evidence_candidate, dict)
                        or (
                            expected_bundle_sha256 is not None
                            and evidence_candidate.get("bundleSha256") != expected_bundle_sha256
                        )
                        or (
                            expected_inventory_sha256 is not None
                            and evidence_candidate.get("installedInventorySha256")
                            != expected_inventory_sha256
                        )
                    ):
                        reasons.append("missing_or_invalid_row_evidence")
                    elif capture_evidence_dir is not None and capture_attempt is not None:
                        try:
                            evidence = retain_composited_timeline_capture(
                                evidence,
                                evidence_dir=capture_evidence_dir,
                                attempt=capture_attempt,
                            )
                        except (AdmissionError, OSError):
                            evidence = None
                            reasons.append("missing_or_invalid_composited_timeline_capture")
                    elif (capture_evidence_dir is None) != (capture_attempt is None):
                        reasons.append("missing_or_invalid_composited_timeline_capture")
                elif (row.spec, row.title) == (M25_53_SPEC, M25_53_TITLE):
                    evidence = _decode_m25_53_evidence(attachment_rows)
                    evidence_candidate = evidence.get("candidate") if evidence is not None else None
                    if (
                        evidence is None
                        or not isinstance(evidence_candidate, dict)
                        or (
                            expected_bundle_sha256 is not None
                            and evidence_candidate.get("bundleSha256") != expected_bundle_sha256
                        )
                        or (
                            expected_inventory_sha256 is not None
                            and evidence_candidate.get("installedInventorySha256")
                            != expected_inventory_sha256
                        )
                    ):
                        reasons.append("missing_or_invalid_row_evidence")
                elif (row.spec, row.title) == (M25_56_SPEC, M25_56_TITLE):
                    evidence = _decode_m25_56_evidence(attachment_rows)
                    evidence_candidate = evidence.get("candidate") if evidence is not None else None
                    if (
                        evidence is None
                        or not isinstance(evidence_candidate, dict)
                        or (
                            expected_bundle_sha256 is not None
                            and evidence_candidate.get("bundleSha256") != expected_bundle_sha256
                        )
                        or (
                            expected_inventory_sha256 is not None
                            and evidence_candidate.get("installedInventorySha256")
                            != expected_inventory_sha256
                        )
                    ):
                        reasons.append("missing_or_invalid_row_evidence")
        if reasons:
            if "skipped" not in reasons:
                counts["failed"] += 1
            if any("scan" in reason or "missing" in reason for reason in reasons):
                counts["incomplete"] += 1
        else:
            counts["passed"] += 1
        rows.append(
            {
                "test_id": test_id,
                "spec": row.spec,
                "title": row.title,
                "test_status": status,
                "scan_status": receipt.get("status") if receipt is not None else "missing",
                "foreign_count": receipt.get("foreign_count", 0) if receipt is not None else 0,
                "log_scan_receipt": receipt,
                "evidence": evidence,
                "failure_facts": failure_facts,
                "reasons": reasons,
                "error_count": len(result.get("errors", []))
                if isinstance(result, dict) and isinstance(result.get("errors"), list)
                else 0,
                "error_summaries": _bounded_error_summaries(result),
            }
        )
    verdict = (
        "PASS"
        if counts["passed"] == counts["selected"]
        and not extra_ids
        and global_error_count == 0
        and malformed_result_count == 0
        else "FAIL"
    )
    return {
        "verdict": verdict,
        "counts": counts,
        "rows": rows,
        "extra_result_count": len(extra_ids),
        "global_error_count": global_error_count,
        "malformed_result_count": malformed_result_count,
    }


def retain_composited_timeline_capture(
    evidence: Mapping[str, object], *, evidence_dir: Path, attempt: str
) -> Mapping[str, object]:
    """Retain the original bounded M25-52 timeline PNG outside the sanitized JSON receipt."""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", attempt) is None:
        raise AdmissionError("timeline capture attempt label is invalid")
    decorations = evidence.get("decorations")
    if not isinstance(decorations, dict):
        raise AdmissionError("composited timeline decorations are missing")
    capture = decorations.get("compositedTimelineCapture")
    if not isinstance(capture, dict):
        raise AdmissionError("composited timeline capture is missing")
    encoded = capture.get("pngBase64")
    maximum_encoded_length = ((MAX_M25_52_TIMELINE_CAPTURE_BYTES + 2) // 3) * 4
    if (
        capture.get("source") != 'Playwright composed crop of [data-h3-nle-area="timeline"]'
        or capture.get("contentType") != "image/png"
        or not isinstance(encoded, str)
        or not 1 <= len(encoded) <= maximum_encoded_length
        or type(capture.get("byteLength")) is not int
    ):
        raise AdmissionError("composited timeline capture metadata is invalid")
    try:
        png = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise AdmissionError("composited timeline capture encoding is invalid") from error
    declared_length = capture.get("byteLength")
    width = capture.get("cropWidthPx")
    height = capture.get("cropHeightPx")
    viewport = capture.get("viewport")
    if (
        not 24 <= len(png) <= MAX_M25_52_TIMELINE_CAPTURE_BYTES
        or type(declared_length) is not int
        or declared_length != len(png)
        or capture.get("sha256") != hashlib.sha256(png).hexdigest()
        or png[:8] != b"\x89PNG\r\n\x1a\n"
        or int.from_bytes(png[8:12], "big") != 13
        or png[12:16] != b"IHDR"
        or type(width) is not int
        or type(height) is not int
        or not 1 <= width <= 4_096
        or not 1 <= height <= 4_096
        or struct.unpack(">II", png[16:24]) != (width, height)
        or not isinstance(viewport, dict)
        or viewport.get("width") != 1402
        or viewport.get("height") != 868
    ):
        raise AdmissionError("composited timeline capture integrity check failed")

    root = _assert_safe_root(evidence_dir)
    captures_dir = root / "captures"
    if _is_link(captures_dir):
        raise AdmissionError("timeline capture evidence directory is unsafe")
    captures_dir.mkdir(exist_ok=True)
    resolved_captures_dir = captures_dir.resolve(strict=True)
    try:
        resolved_captures_dir.relative_to(root)
    except ValueError as error:
        raise AdmissionError("timeline capture evidence directory is unsafe") from error
    target = resolved_captures_dir / f"{attempt}-timeline.png"
    if target.exists() or _is_link(target):
        raise AdmissionError("timeline capture evidence already exists")
    try:
        with target.open("xb") as stream:
            stream.write(png)
    except OSError as error:
        raise AdmissionError("timeline capture evidence could not be retained") from error

    retained = dict(evidence)
    retained_decorations = dict(decorations)
    retained_capture = dict(capture)
    retained_capture.pop("pngBase64", None)
    retained_capture["evidencePath"] = f"captures/{attempt}-timeline.png"
    retained_decorations["compositedTimelineCapture"] = retained_capture
    retained["decorations"] = retained_decorations
    return retained


def validate_process_snapshot(expected: ExpectedProcess, observed: ProcessSnapshot) -> None:
    if observed.pid != expected.pid:
        raise AdmissionError("process PID mismatch")
    if abs(observed.created - expected.created) > 0.01:
        raise AdmissionError("process creation identity mismatch")
    if not _same_path(observed.executable, expected.executable):
        raise AdmissionError("process executable mismatch")
    if not _same_path(observed.cwd, expected.cwd):
        raise AdmissionError("process working directory mismatch")
    if not _same_path(observed.entrypoint, expected.entrypoint):
        raise AdmissionError("process entrypoint mismatch")
    if observed.port != expected.port or not observed.listening:
        raise AdmissionError("process listener mismatch")


def capture_process_snapshot(expected: ExpectedProcess) -> ProcessSnapshot:
    # The supplied ComfyUI interpreter already carries psutil as part of the host runtime. Run the
    # bounded identity probe there so a clean project checkout needs no new driver dependency.
    helper = """
import json
import pathlib
import psutil
import sys

p = psutil.Process(int(sys.argv[1]))
argv = p.cmdline()
cwd = pathlib.Path(p.cwd())
entry = pathlib.Path(argv[1]) if len(argv) > 1 else pathlib.Path('.')
if not entry.is_absolute():
    entry = cwd / entry
port = int(sys.argv[2])
listening = any(
    connection.status == psutil.CONN_LISTEN
    and connection.laddr
    and connection.laddr.port == port
    for connection in p.net_connections(kind='tcp')
)
print(json.dumps({
    'pid': p.pid,
    'created': p.create_time(),
    'executable': p.exe(),
    'cwd': str(cwd),
    'entrypoint': str(entry),
    'port': port,
    'listening': listening,
}))
"""
    completed = subprocess.run(  # noqa: S603 - explicit supplied interpreter, fixed helper
        [expected.executable, "-I", "-c", helper, str(expected.pid), str(expected.port)],
        cwd=expected.cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise AdmissionError("supplied host process identity probe failed")
    try:
        wire = json.loads(completed.stdout)
        return ProcessSnapshot(**wire)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise AdmissionError("supplied host process identity probe was malformed") from error


def validate_idle_queue(value: object) -> None:
    if (
        not isinstance(value, dict)
        or value.get("queue_running") != []
        or value.get("queue_pending") != []
    ):
        raise AdmissionError("supplied host queue is occupied or malformed")


def _assert_safe_root(root: Path) -> Path:
    lexical = root.absolute()
    if _is_link(lexical) or not lexical.is_dir():
        raise AdmissionError("candidate root is unsafe")
    return lexical.resolve(strict=True)


def _assert_safe_destination(root: Path, destination: Path) -> None:
    if _is_link(destination):
        raise AdmissionError("installed candidate destination is unsafe")
    resolved_root = root.resolve(strict=True)
    cursor = destination
    while cursor != root and cursor != cursor.parent:
        if cursor.exists() and _is_link(cursor):
            raise AdmissionError("installed candidate destination is unsafe")
        cursor = cursor.parent
    resolved = destination.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise AdmissionError("installed candidate destination is unsafe") from error


def _eligible_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        retained_directories: list[str] = []
        for name in directories:
            child = current_path / name
            if _is_link(child):
                raise AdmissionError("candidate inventory is unsafe")
            if name != "__pycache__":
                retained_directories.append(name)
        directories[:] = retained_directories
        for name in names:
            path = current_path / name
            if _is_link(path):
                raise AdmissionError("candidate inventory is unsafe")
            if path.is_file() and path.suffix.lower() in ELIGIBLE_SUFFIXES:
                files.append(path)
    files.sort()
    if not 1 <= len(files) <= MAX_CANDIDATE_FILES:
        raise AdmissionError("candidate inventory exceeds the 1..512 file bound")
    return files


def candidate_changes(source_root: Path, destination_root: Path) -> CandidateChanges:
    source = _assert_safe_root(source_root)
    destination = _assert_safe_root(destination_root)
    files = _eligible_files(source)
    digest = hashlib.sha256()
    changed: list[CandidateFileChange] = []
    expected: set[str] = set()
    for path in files:
        if _is_link(path):
            raise AdmissionError("candidate inventory is unsafe")
        relative = path.relative_to(source).as_posix()
        expected.add(relative)
        destination_path = destination / Path(relative)
        _assert_safe_destination(destination, destination_path)
        source_sha = _sha256(path)
        digest.update(relative.encode("utf-8") + b"\0" + source_sha.encode("ascii") + b"\n")
        existed = destination_path.is_file()
        if not existed or _sha256(destination_path) != source_sha:
            changed.append(CandidateFileChange(relative, path, destination_path, existed))
    extras = tuple(
        sorted(
            path.relative_to(destination).as_posix()
            for path in _eligible_files(destination)
            if path.relative_to(destination).as_posix() not in expected
        )
    )
    return CandidateChanges(
        source,
        destination,
        len(files),
        tuple(changed),
        extras,
        digest.hexdigest(),
    )


def _write_temporary(source: Path, destination: Path) -> None:
    with destination.open("xb") as stream:
        with source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, stream, length=1024 * 1024)


def synchronize_candidate(
    changes: CandidateChanges,
    backup_root: Path,
    *,
    write_temporary: Callable[[Path, Path], None] = _write_temporary,
) -> tuple[str, ...]:
    if changes.extras:
        raise AdmissionError("installed candidate has unknown extra files")
    if backup_root.exists():
        raise AdmissionError("maintenance backup destination already exists")
    backup_root.mkdir(parents=True)
    written: list[CandidateFileChange] = []
    try:
        for item in changes.changed:
            _assert_safe_destination(changes.destination_root, item.destination)
            if item.destination_existed:
                backup = backup_root / Path(item.relative_path)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item.destination, backup)
            item.destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = item.destination.with_name(
                item.destination.name + ".supplied-host-lane.tmp"
            )
            if temporary.exists():
                raise AdmissionError("maintenance temporary path already exists")
            try:
                write_temporary(item.source, temporary)
                os.replace(temporary, item.destination)
            finally:
                temporary.unlink(missing_ok=True)
            written.append(item)
            if _sha256(item.source) != _sha256(item.destination):
                raise AdmissionError("installed candidate digest mismatch after replacement")
    except BaseException:
        rollback_error: BaseException | None = None
        for item in reversed(written):
            try:
                backup = backup_root / Path(item.relative_path)
                if item.destination_existed:
                    shutil.copyfile(backup, item.destination)
                else:
                    item.destination.unlink(missing_ok=True)
            except BaseException as error:  # pragma: no cover - catastrophic and reported below
                rollback_error = error
        if rollback_error is not None:
            raise AdmissionError(
                "candidate synchronization and rollback both failed"
            ) from rollback_error
        raise
    return tuple(item.relative_path for item in written)


def restore_candidate(
    changes: CandidateChanges, backup_root: Path, written_paths: Iterable[str]
) -> None:
    by_name = {item.relative_path: item for item in changes.changed}
    failures = 0
    for relative_path in reversed(tuple(written_paths)):
        item = by_name[relative_path]
        try:
            _assert_safe_destination(changes.destination_root, item.destination)
            backup = backup_root / Path(relative_path)
            if item.destination_existed:
                if not backup.is_file():
                    raise AdmissionError("maintenance rollback backup is missing")
                shutil.copyfile(backup, item.destination)
            else:
                item.destination.unlink(missing_ok=True)
        except (OSError, AdmissionError):
            failures += 1
    if failures:
        raise AdmissionError(f"candidate rollback failed for {failures} admitted file(s)")


_URL = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"']+")
_UNC_PATH = re.compile(r"\\\\[^\s\"']+")
_TOKEN = re.compile(r"(?i)\b(token|signature|api[_-]?key|authorization)=([^\s&,;]+)")


def sanitize_text(value: str) -> str:
    text = _URL.sub("<url>", value)
    text = _WINDOWS_PATH.sub("<path>", text)
    text = _UNC_PATH.sub("<path>", text)
    text = _TOKEN.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    if len(text) > MAX_REPORT_STRING:
        text = text[:MAX_REPORT_STRING] + "..."
    return text


def sanitize_tree(value: object, *, _key: str | None = None) -> object:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        if (
            _key == "frameSamples"
            and len(value) <= 5_000
            and all(type(item) is int and 0 <= item <= 100_000 for item in value)
        ):
            return list(value)
        return [sanitize_tree(item) for item in value[:100]]
    if isinstance(value, tuple):
        return [sanitize_tree(item) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(key)[:80]: sanitize_tree(item, _key=str(key))
            for key, item in list(value.items())[:100]
        }
    return value if value is None or isinstance(value, (bool, int, float)) else type(value).__name__


def _http_json(host_url: str, path: str, timeout: float = 10.0) -> object:
    request = urllib.request.Request(  # noqa: S310 - caller supplies a validated loopback origin
        host_url.rstrip("/") + path, method="GET"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback validated
        payload = response.read(MAX_HTTP_BYTES + 1)
    if len(payload) > MAX_HTTP_BYTES:
        raise AdmissionError("supplied host response exceeded the read bound")
    return json.loads(payload)


def _candidate_subject(root: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        encoding="utf-8",
        errors="strict",
    ).strip()


def _expected_process(arguments: argparse.Namespace) -> ExpectedProcess:
    _, port = validate_host_url(arguments.host_url)
    return ExpectedProcess(
        arguments.pid,
        arguments.created,
        str(_resolved(arguments.host_python)),
        str(_resolved(arguments.host_root)),
        str(_resolved(arguments.host_entrypoint)),
        port,
    )


def _preflight(arguments: argparse.Namespace, *, require_exact: bool) -> dict[str, object]:
    expected = _expected_process(arguments)
    snapshot = capture_process_snapshot(expected)
    validate_process_snapshot(expected, snapshot)
    validate_idle_queue(_http_json(arguments.host_url, "/queue"))
    candidate_root = _resolved(arguments.candidate_root)
    actual_subject = _candidate_subject(candidate_root)
    if actual_subject != arguments.candidate_subject:
        raise AdmissionError("candidate subject mismatch")
    bundle = _resolved(arguments.bundle)
    if bundle.stat().st_size <= 0 or _sha256(bundle) != arguments.bundle_sha256:
        raise AdmissionError("candidate bundle identity mismatch")
    log = _resolved(arguments.host_log)
    if not log.is_file():
        raise AdmissionError("host log is not an existing readable file")
    package = candidate_root / "comfyui_h3_context"
    installed = (
        _resolved(arguments.host_root) / "custom_nodes" / arguments.pack_name / "comfyui_h3_context"
    )
    drift = candidate_changes(package, installed)
    if require_exact and (drift.changed or drift.extras):
        raise AdmissionError("installed candidate drift prevents exact row execution")
    system = _http_json(arguments.host_url, "/system_stats")
    wire = system if isinstance(system, dict) else {}
    system_product = wire.get("system")
    product: Mapping[str, object] = system_product if isinstance(system_product, dict) else {}
    return {
        "expected": expected,
        "snapshot": snapshot,
        "candidate_root": candidate_root,
        "bundle": bundle,
        "log": log,
        "drift": drift,
        "versions": {
            "comfyui": product.get("comfyui_version"),
            "frontend": product.get("required_frontend_version"),
        },
    }


def _terminate_owned_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    else:  # pragma: no cover - exercised by Linux CI
        # IMPORTANT: retain group signaling on POSIX; Windows stubs omit these native APIs.
        killpg = cast(Callable[[int, int], None], getattr(os, "killpg"))  # noqa: B009
        try:
            killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                killpg(process.pid, int(getattr(signal, "SIGKILL")))  # noqa: B009
    if process.poll() is None:
        process.kill()
    process.wait(timeout=15)


def process_exists(pid: int) -> bool:
    if os.name == "nt":
        kernel = getattr(ctypes, "windll").kernel32  # noqa: B009
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        kernel.CloseHandle(handle)
        return True
    try:  # pragma: no cover - exercised by Linux CI
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def run_bounded(
    command: Sequence[str], *, cwd: Path, environment: Mapping[str, str], timeout: float
) -> CommandResult:
    creationflags = (
        int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP")) if os.name == "nt" else 0  # noqa: B009
    )
    process = subprocess.Popen(  # noqa: S603 - argv only, no shell
        list(command),
        cwd=cwd,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return CommandResult(process.returncode, stdout, stderr, False)
    except subprocess.TimeoutExpired:
        _terminate_owned_process_tree(process)
        stdout, stderr = process.communicate()
        return CommandResult(process.returncode or 124, stdout, stderr, True)


def _parse_json_output(result: CommandResult, kind: str) -> Mapping[str, object]:
    if result.timed_out:
        raise AdmissionError(f"Playwright {kind} timed out")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AdmissionError(f"Playwright {kind} report is missing or malformed") from error
    if not isinstance(report, dict):
        raise AdmissionError(f"Playwright {kind} report is malformed")
    return report


def _playwright_command(
    candidate_root: Path, rows: Sequence[RowSelection], *, listing: bool
) -> list[str]:
    cli = candidate_root / "frontend/node_modules/@playwright/test/cli.js"
    if not cli.is_file():
        raise AdmissionError("the checkout-local Playwright CLI is unavailable")
    pattern = "(?:" + "|".join(re.escape(row.title) for row in rows) + ")$"
    command = [
        shutil.which("node") or "node",
        str(cli),
        "test",
        "--config",
        "playwright.host.config.ts",
        *sorted({row.spec for row in rows}),
        "--grep",
        pattern,
        "--reporter=json",
        "--workers=1",
        "--retries=0",
    ]
    if listing:
        command.append("--list")
    return command


def _run_environment(
    arguments: argparse.Namespace, preflight: Mapping[str, object]
) -> dict[str, str]:
    environment = dict(os.environ)
    # CRITICAL: row authority is supplied only through the admitted allowlist. Inheriting any
    # H3_CONTEXT_* toggle can silently enable real generation or a fault path in a selected spec.
    for name in tuple(environment):
        if name.startswith("H3_CONTEXT_") or name in DENIED_EXECUTION_FLAGS:
            environment.pop(name, None)
    environment.update(parse_row_environment(arguments.row_env))
    candidate_root = preflight["candidate_root"]
    bundle = preflight["bundle"]
    log = preflight["log"]
    drift = preflight["drift"]
    if (
        not isinstance(candidate_root, Path)
        or not isinstance(bundle, Path)
        or not isinstance(log, Path)
        or not isinstance(drift, CandidateChanges)
    ):
        raise AdmissionError("internal preflight path contract is invalid")
    environment.update(
        {
            "H3_CONTEXT_HOST_ROOT": str(_resolved(arguments.host_root)),
            "H3_CONTEXT_HOST_URL": arguments.host_url,
            "H3_CONTEXT_CANDIDATE_BACKEND_MODE": "exact",
            "H3_CONTEXT_CANDIDATE_BUNDLE_PATH": str(bundle),
            "H3_CONTEXT_CANDIDATE_BUNDLE_SHA256": arguments.bundle_sha256,
            # IMPORTANT: this is the driver's complete installed-candidate identity. Keep it
            # distinct from the browser helper's backend-only inventory or valid rows fail closed.
            "H3_CONTEXT_CANDIDATE_INVENTORY_SHA256": drift.inventory_sha256,
            "H3_CONTEXT_HOST_LOG": str(log),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _evidence_path(arguments: argparse.Namespace) -> Path:
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", arguments.attempt) is None:
        raise ValueError("attempt must be a lowercase bounded label")
    root = Path(arguments.evidence_dir).resolve(strict=True)
    target = root / f"{arguments.attempt}.json"
    if target.exists():
        raise AdmissionError("attempt report already exists")
    return target


def _write_report(path: Path, value: Mapping[str, object]) -> None:
    safe = sanitize_tree(value)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(safe, stream, indent=2, sort_keys=True, ensure_ascii=False)
        stream.write("\n")


def inspect_operation(arguments: argparse.Namespace) -> dict[str, object]:
    report_path = _evidence_path(arguments)
    state = _preflight(arguments, require_exact=False)
    drift = state["drift"]
    snapshot = state["snapshot"]
    if not isinstance(drift, CandidateChanges) or not isinstance(snapshot, ProcessSnapshot):
        raise AdmissionError("internal preflight inspection contract is invalid")
    result = {
        "schema": VERDICT_SCHEMA,
        "operation": "inspect",
        "verdict": "PASS",
        "process": {
            "pid": snapshot.pid,
            "created": snapshot.created,
            "listener": snapshot.listening,
        },
        "candidate": {
            "subject": arguments.candidate_subject,
            "inventory_sha256": drift.inventory_sha256,
            "files": drift.file_count,
            "changed_count": len(drift.changed),
            "extra_count": len(drift.extras),
            "changed": [item.relative_path for item in drift.changed[:100]],
            "extras": list(drift.extras[:100]),
        },
        "versions": state["versions"],
    }
    _write_report(report_path, result)
    return result


def run_operation(arguments: argparse.Namespace) -> dict[str, object]:
    report_path = _evidence_path(arguments)
    rows = [parse_row(value) for value in arguments.row]
    state = _preflight(arguments, require_exact=True)
    candidate_root = state["candidate_root"]
    drift = state["drift"]
    if not isinstance(candidate_root, Path) or not isinstance(drift, CandidateChanges):
        raise AdmissionError("internal preflight candidate contract is invalid")
    environment = _run_environment(arguments, state)
    with tempfile.TemporaryDirectory(prefix="h3-supplied-host-lane-") as temporary:
        environment["H3_CONTEXT_PLAYWRIGHT_OUTPUT"] = str(Path(temporary) / "output")
        collection_result = run_bounded(
            _playwright_command(candidate_root, rows, listing=True),
            cwd=candidate_root / "frontend",
            environment=environment,
            timeout=min(arguments.timeout, 300.0),
        )
        collection = _parse_json_output(collection_result, "collection")
        collection_errors = collection.get("errors")
        if collection_result.returncode != 0 or (
            collection_errors is not None
            and (not isinstance(collection_errors, list) or collection_errors)
        ):
            raise AdmissionError("Playwright collection failed")
        selected = admit_collection(rows, collection)
        execution_result = run_bounded(
            _playwright_command(candidate_root, rows, listing=False),
            cwd=candidate_root / "frontend",
            environment=environment,
            timeout=arguments.timeout,
        )
        execution = _parse_json_output(execution_result, "execution")
    # Retain the PNG before the generic report sanitizer clips long evidence strings.
    admission = admit_results(
        selected,
        execution,
        expected_bundle_sha256=arguments.bundle_sha256,
        expected_inventory_sha256=drift.inventory_sha256,
        capture_evidence_dir=report_path.parent,
        capture_attempt=arguments.attempt,
    )
    final_state = _preflight(arguments, require_exact=True)
    if final_state["versions"] != state["versions"]:
        raise AdmissionError("host version identity changed during execution")
    if execution_result.returncode != 0 and admission["verdict"] == "PASS":
        raise AdmissionError("Playwright exit code and structured result disagree")
    verdict = {
        "schema": VERDICT_SCHEMA,
        "operation": "run",
        "attempt": arguments.attempt,
        "verdict": admission["verdict"],
        "candidate": {
            "subject": arguments.candidate_subject,
            "bundle_sha256": arguments.bundle_sha256,
            "inventory_sha256": drift.inventory_sha256,
        },
        "process": {"pid": arguments.pid, "created": arguments.created, "unchanged": True},
        "versions": state["versions"],
        "playwright_exit": execution_result.returncode,
        **admission,
    }
    _write_report(report_path, verdict)
    return verdict


def _restart_verified(
    arguments: argparse.Namespace, expected: ExpectedProcess
) -> tuple[int, float]:
    validate_process_snapshot(expected, capture_process_snapshot(expected))
    validate_idle_queue(_http_json(arguments.host_url, "/queue"))
    helper = """
import json
import pathlib
import psutil
import subprocess
import sys

pid = int(sys.argv[1])
created = float(sys.argv[2])
expected_exe = pathlib.Path(sys.argv[3]).resolve()
expected_cwd = pathlib.Path(sys.argv[4]).resolve()
expected_entry = pathlib.Path(sys.argv[5]).resolve()
log = pathlib.Path(sys.argv[6]).resolve()
p = psutil.Process(pid)
if abs(p.create_time() - created) > 0.01:
    raise RuntimeError('process_creation_mismatch')
if pathlib.Path(p.exe()).resolve() != expected_exe:
    raise RuntimeError('process_executable_mismatch')
if pathlib.Path(p.cwd()).resolve() != expected_cwd:
    raise RuntimeError('process_cwd_mismatch')
argv = p.cmdline()
entry = pathlib.Path(argv[1])
entry = entry if entry.is_absolute() else expected_cwd / entry
if entry.resolve() != expected_entry:
    raise RuntimeError('process_entry_mismatch')
environment = p.environ()
tail = argv[2:]
p.terminate()
p.wait(timeout=60)
flags = 0
if sys.platform == 'win32':
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
with log.open('ab', buffering=0) as stream:
    child = subprocess.Popen(
        [str(expected_exe), str(expected_entry), *tail],
        cwd=expected_cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=stream,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        start_new_session=sys.platform != 'win32',
    )
print(json.dumps({
    'pid': child.pid,
    'created': psutil.Process(child.pid).create_time(),
}))
"""
    completed = subprocess.run(  # noqa: S603 - explicit host interpreter, fixed bounded helper
        [
            expected.executable,
            "-I",
            "-c",
            helper,
            str(expected.pid),
            str(expected.created),
            expected.executable,
            expected.cwd,
            expected.entrypoint,
            str(_resolved(arguments.host_log)),
        ],
        cwd=expected.cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise AdmissionError("verified supplied host restart failed")
    try:
        wire = json.loads(completed.stdout)
        pid, created = int(wire["pid"]), float(wire["created"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AdmissionError("verified supplied host restart result was malformed") from error
    # Readiness and final identity use the fixed helper; raw argv/environment is never emitted.
    deadline = time.monotonic() + arguments.ready_timeout
    new_expected = ExpectedProcess(
        pid, created, expected.executable, expected.cwd, expected.entrypoint, expected.port
    )
    while time.monotonic() < deadline:
        try:
            validate_process_snapshot(new_expected, capture_process_snapshot(new_expected))
            validate_idle_queue(_http_json(arguments.host_url, "/queue", timeout=2.0))
            return pid, created
        except (OSError, ValueError, AdmissionError, subprocess.SubprocessError):
            time.sleep(1.0)
    _terminate_exact_host(expected.executable, pid, created, expected.cwd)
    raise AdmissionError("restarted supplied host did not become ready")


def _terminate_exact_host(host_python: str, pid: int, created: float, host_cwd: str) -> None:
    helper = """
import psutil
import sys

p = psutil.Process(int(sys.argv[1]))
if abs(p.create_time() - float(sys.argv[2])) > 0.01:
    raise RuntimeError('process_creation_mismatch')
p.terminate()
try:
    p.wait(timeout=30)
except psutil.TimeoutExpired:
    p.kill()
    p.wait(timeout=15)
"""
    completed = subprocess.run(  # noqa: S603 - exact supplied interpreter/PID/create identity
        [host_python, "-I", "-c", helper, str(pid), str(created)],
        cwd=host_cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise AdmissionError("restarted host cleanup failed; exact child may remain alive")


def maintenance_operation(arguments: argparse.Namespace) -> dict[str, object]:
    report_path = _evidence_path(arguments)
    state = _preflight(arguments, require_exact=arguments.operation == "restart")
    drift = state["drift"]
    expected = state["expected"]
    if not isinstance(drift, CandidateChanges) or not isinstance(expected, ExpectedProcess):
        raise AdmissionError("internal preflight maintenance contract is invalid")
    changed: tuple[str, ...] = ()
    backup: Path | None = None
    if arguments.operation == "sync-restart":
        backup = Path(arguments.evidence_dir).resolve(strict=True) / f"{arguments.attempt}-backup"
        changed = synchronize_candidate(drift, backup)
        verified = candidate_changes(drift.source_root, drift.destination_root)
        if verified.changed or verified.extras:
            restore_candidate(drift, backup, changed)
            raise AdmissionError("candidate drift remains after synchronization")
    try:
        pid, created = _restart_verified(arguments, expected)
    except BaseException:
        if backup is not None:
            restore_candidate(drift, backup, changed)
        raise
    result = {
        "schema": VERDICT_SCHEMA,
        "operation": arguments.operation,
        "attempt": arguments.attempt,
        "verdict": "PASS",
        "prior_process": {"pid": expected.pid, "created": expected.created},
        "process": {"pid": pid, "created": created},
        "candidate": {
            "subject": arguments.candidate_subject,
            "inventory_sha256": drift.inventory_sha256,
            "changed_count": len(changed),
            "changed": list(changed[:100]),
        },
    }
    _write_report(report_path, result)
    return result


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host-url", required=True)
    parser.add_argument("--host-root", required=True)
    parser.add_argument("--host-python", required=True)
    parser.add_argument("--host-entrypoint", required=True)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--created", required=True, type=float)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--candidate-subject", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--host-log", required=True)
    parser.add_argument("--pack-name", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--attempt", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    inspect = commands.add_parser(
        "inspect", help="read-only supplied-host and candidate inspection"
    )
    _add_common(inspect)
    run = commands.add_parser(
        "run", help="run exact selected supplied-host rows without maintenance"
    )
    _add_common(run)
    run.add_argument("--row", action="append", required=True)
    run.add_argument("--row-env", action="append", default=[])
    run.add_argument("--timeout", type=float, default=2400.0)
    for name in ("restart", "sync-restart"):
        maintenance = commands.add_parser(name, help=f"explicit {name} maintenance operation")
        _add_common(maintenance)
        maintenance.add_argument("--ready-timeout", type=float, default=300.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", arguments.pack_name):
            raise ValueError("pack name is invalid")
        if not re.fullmatch(r"[0-9a-f]{40}", arguments.candidate_subject):
            raise ValueError("candidate subject must be a full commit OID")
        if not re.fullmatch(r"[0-9a-f]{64}", arguments.bundle_sha256):
            raise ValueError("bundle SHA-256 is invalid")
        if arguments.operation == "inspect":
            result = inspect_operation(arguments)
        elif arguments.operation == "run":
            result = run_operation(arguments)
        else:
            result = maintenance_operation(arguments)
        print(json.dumps(sanitize_tree(result), indent=2, sort_keys=True, ensure_ascii=False))
        return 0 if result.get("verdict") == "PASS" else 4
    except (AdmissionError, OSError, ValueError, subprocess.SubprocessError) as error:
        failure = {
            "schema": VERDICT_SCHEMA,
            "operation": arguments.operation,
            "verdict": "FAIL",
            "reason": type(error).__name__,
            "message": sanitize_text(str(error)),
        }
        if hasattr(arguments, "evidence_dir") and hasattr(arguments, "attempt"):
            try:
                directory = Path(arguments.evidence_dir).resolve(strict=True)
                if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", arguments.attempt):
                    target = directory / f"{arguments.attempt}.json"
                    if not target.exists():
                        _write_report(target, failure)
            except (OSError, ValueError, AdmissionError):
                pass
        print(json.dumps(failure, indent=2, sort_keys=True), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
