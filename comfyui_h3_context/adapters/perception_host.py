"""Revocable host composition and bounded, private local perception execution."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

from ..core.audio_analysis import (
    AUDIO_ANALYSIS_SCHEMA,
    AudioAnalysisBatch,
    AudioAnalysisStatus,
    AudioObservation,
    AudioObservationKind,
)
from ..core.constraints import TimePoint
from ..core.contracts import (
    AssetDescriptor,
    AssetRole,
    EvidenceLevel,
    MediaKind,
    ProviderIdentity,
    TaskMode,
    ValidationSeverity,
)
from ..core.errors import ContractValidationError
from ..core.evidence import (
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSource,
    EvidenceSourceKind,
    Provenance,
    SupportStatus,
    Uncertainty,
    UncertaintyKind,
)
from ..core.normalization import RawContextRequest, normalize_request
from ..core.perception_execution import (
    AUDIO_PROFILE,
    PROFILE_MODELS,
    VISUAL_PROFILE,
    QualifiedPerceptionResult,
    _issue_execution,
)
from ..core.perception_producer import PerceptionProducerResult, ProducerDisposition, ProducerKind
from ..core.reference_window import ReferenceConditioningWindow, reference_conditioning_window
from ..core.video_analysis import (
    VIDEO_ANALYSIS_SCHEMA,
    VideoAnalysisBatch,
    VideoAnalysisDiagnostic,
    VideoAnalysisStatus,
    VideoKeyframe,
    VideoObservation,
    VideoObservationKind,
    VideoShot,
)
from .perception_worker import MAX_VISUAL_DESCRIPTIONS as MAX_VISUAL_DESCRIPTIONS
from .perception_worker import OLLAMA_VERSION

MAX_SECONDS = 180.0
MAX_RSS = 20 * 1024**3
MAX_OUTPUT = 65536
_ADMISSION = threading.BoundedSemaphore(1)
_LOCK = threading.RLock()


class PerceptionExecutionError(ContractValidationError):
    """A content-free refusal; no raw runtime exception crosses the node boundary."""


@dataclass(frozen=True)
class DecodedCfrVideo:
    """Integration value for already-decoded CPU frames; never a file decoder."""

    images: object = field(repr=False)
    frame_rate: Decimal


@dataclass(frozen=True)
class PerceptionHostSettings:
    python_executable: Path = field(repr=False)
    temporary_root: Path = field(repr=False)
    weight_path: Path | None = field(default=None, repr=False)
    processor_dir: Path | None = field(default=None, repr=False)
    cancelled: Callable[[], bool] = field(default=lambda: False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for value in (
            self.python_executable,
            self.temporary_root,
            self.weight_path,
            self.processor_dir,
        ):
            if value is not None and (not isinstance(value, Path) or not value.is_absolute()):
                raise ContractValidationError("host paths must be absolute operator configuration")
        if not callable(self.cancelled):
            raise ContractValidationError("host cancellation probe must be callable")


@dataclass(frozen=True, eq=False)
class PerceptionHostBinding:
    settings: PerceptionHostSettings = field(repr=False)


_BINDING: PerceptionHostBinding | None = None
_SETTINGS: WeakKeyDictionary[PerceptionHostBinding, tuple[Any, ...]] = WeakKeyDictionary()


def _configuration(binding: PerceptionHostBinding) -> tuple[Any, ...]:
    settings = binding.settings
    return (
        id(settings),
        settings.python_executable,
        settings.temporary_root,
        settings.weight_path,
        settings.processor_dir,
        id(settings.cancelled),
    )


def _is_current(binding: PerceptionHostBinding) -> bool:
    # CRITICAL: frozen dataclasses alone do not attest nested configuration. Pin the
    # registration snapshot so changing paths/cancellation cannot reuse old authority.
    return _BINDING is binding and _SETTINGS.get(binding) == _configuration(binding)


def configure_perception_host(settings: PerceptionHostSettings) -> PerceptionHostBinding:
    if type(settings) is not PerceptionHostSettings:
        raise ContractValidationError("host settings must be exact")
    settings.__post_init__()
    binding = PerceptionHostBinding(settings)
    global _BINDING
    with _LOCK:
        _SETTINGS[binding] = _configuration(binding)
        _BINDING = binding
    return binding


def clear_perception_host(binding: PerceptionHostBinding) -> bool:
    global _BINDING
    with _LOCK:
        if _BINDING is not binding:
            return False
        _BINDING = None
        return True


def perception_configured(profile_id: str) -> bool:
    with _LOCK:
        binding = _BINDING
    return (
        binding is not None
        and _is_current(binding)
        and profile_id in PROFILE_MODELS
        and (
            profile_id != AUDIO_PROFILE
            or (
                binding.settings.weight_path is not None
                and binding.settings.processor_dir is not None
            )
        )
    )


def _lease(binding: PerceptionHostBinding) -> None:
    with _LOCK:
        current = _is_current(binding)
    if not current:
        raise PerceptionExecutionError("configuration_revoked")
    try:
        cancelled = binding.settings.cancelled()
    except Exception:
        raise PerceptionExecutionError("cancellation_probe_failed") from None
    # CRITICAL: callbacks run outside the lock and can revoke/reconfigure their own lease.
    with _LOCK:
        current = _is_current(binding)
    if not current:
        raise PerceptionExecutionError("configuration_revoked")
    if type(cancelled) is not bool or cancelled:
        raise PerceptionExecutionError("execution_cancelled")


@dataclass(frozen=True)
class _Snapshot:
    digest: str
    duration: Decimal
    indices: tuple[int, ...]
    fps: Decimal
    job: dict[str, Any] = field(repr=False)
    sample_count: int = 0
    admitted_frame_count: int = 0
    conditioning_window: ReferenceConditioningWindow | None = None


def _snapshot(
    media: PerceptionProducerResult,
    *,
    encode: bool = True,
    effective_frame_count: int | None = None,
) -> _Snapshot:
    torch = import_module("torch")

    media.assert_current()
    if (
        media.kind is not ProducerKind.MEDIA
        or media.disposition is not ProducerDisposition.COMPLETE
    ):
        raise PerceptionExecutionError("source_not_admitted")
    value = media.runtime_payload
    evidence = media.admission_evidence
    if media.media_kind == "audio":
        if type(value) is not dict or set(value) != {"waveform", "sample_rate"}:
            raise PerceptionExecutionError("unsupported_media")
        tensor, rate = value["waveform"], value["sample_rate"]
        if (
            type(tensor) is not torch.Tensor
            or tensor.device.type != "cpu"
            or tensor.dtype is not torch.float32
            or tensor.ndim != 3
            or tensor.shape[0] != 1
            or tensor.shape[1] not in (1, 2)
            or type(rate) is not int
            or not 8000 <= rate <= 48000
            or not 0 < tensor.shape[2] <= 15 * rate
        ):
            raise PerceptionExecutionError("unsupported_media")
        tensor = tensor.detach().clone().contiguous()
        if not torch.isfinite(tensor).all() or tensor.abs().max() > 1:
            raise PerceptionExecutionError("unsupported_media")
        duration = Decimal(tensor.shape[2]) / Decimal(rate)
        if (
            evidence.sample_rate_hz != rate
            or evidence.channel_count != tensor.shape[1]
            or abs(Decimal(str(evidence.duration_seconds)) - duration) > Decimal("0.000001")
        ):
            raise PerceptionExecutionError("source_metadata_mismatch")
        raw = tensor.numpy().tobytes()
        digest = hashlib.sha256(repr((tensor.shape, rate)).encode() + raw).hexdigest()
        mono = tensor.mean(dim=1)[0]
        if mono.abs().max() < 0.0001:
            raise PerceptionExecutionError("empty_audio")
        return _Snapshot(
            digest,
            duration,
            (),
            Decimal(0),
            {
                "sample_rate": rate,
                "samples": base64.b64encode(mono.numpy().tobytes()).decode() if encode else "",
            },
            tensor.shape[2],
        )
    if media.media_kind == "video":
        if type(value) is DecodedCfrVideo:
            tensor, fps = value.images, value.frame_rate
        else:
            try:
                VideoFromComponents = import_module(
                    "comfy_api.latest._input_impl.video_types"
                ).VideoFromComponents
            except ImportError:
                raise PerceptionExecutionError("unsupported_media") from None
            # CRITICAL: generic VIDEO metadata access can eagerly decode an unbounded file.
            if type(value) is not VideoFromComponents:
                raise PerceptionExecutionError("unsupported_media")
            components = getattr(  # noqa: B009 -- exact host type; avoid mypy narrowing to object
                VideoFromComponents, "get_components"
            )(value)
            if components.audio is not None or components.alpha is not None:
                raise PerceptionExecutionError("unsupported_media")
            rate = components.frame_rate
            if type(rate) is not Fraction or rate.denominator <= 0:
                raise PerceptionExecutionError("unsupported_media")
            tensor, fps = components.images, Decimal(rate.numerator) / Decimal(rate.denominator)
        if type(fps) is not Decimal or not fps.is_finite() or not 1 <= fps <= 60:
            raise PerceptionExecutionError("unsupported_media")
    elif media.media_kind == "image":
        tensor, fps = value, Decimal(0)
    else:
        raise PerceptionExecutionError("unsupported_media")
    if (
        type(tensor) is not torch.Tensor
        or tensor.device.type != "cpu"
        or tensor.dtype is not torch.float32
        or tensor.ndim != 4
        or tensor.shape[3] != 3
        or not 1 <= tensor.shape[0] <= 300
        or not 1 <= tensor.shape[1] <= 1024
        or not 1 <= tensor.shape[2] <= 1024
        or tensor.numel() * 4 > 256 * 1024**2
        or (media.media_kind == "image" and tensor.shape[0] != 1)
    ):
        raise PerceptionExecutionError("unsupported_media")
    duration = Decimal(0) if not fps else Decimal(tensor.shape[0]) / fps
    if (
        duration > 15
        or (evidence.width_pixels, evidence.height_pixels) != (tensor.shape[2], tensor.shape[1])
        or abs(Decimal(str(evidence.duration_seconds)) - duration) > Decimal("0.000001")
    ):
        raise PerceptionExecutionError("source_metadata_mismatch")
    tensor = tensor.detach().clone().contiguous()
    if not torch.isfinite(tensor).all() or tensor.min() < 0 or tensor.max() > 1:
        raise PerceptionExecutionError("unsupported_media")
    # CRITICAL: hash the entire admitted source before truncation; discarded-tail mutations
    # must still revoke qualification even though those frames never enter the prompt.
    digest = hashlib.sha256(
        repr((tensor.shape, str(fps))).encode() + tensor.numpy().tobytes()
    ).hexdigest()
    window = None
    if effective_frame_count is not None:
        if media.media_kind != "video":
            raise PerceptionExecutionError("unsupported_media")
        try:
            window = reference_conditioning_window(effective_frame_count, tensor.shape[0])
        except ContractValidationError as error:
            reason = (
                "reference_too_short"
                if str(error) == "reference_too_short"
                else "invalid_reference_window"
            )
            raise PerceptionExecutionError(reason) from None
        schedule = window.sample_indices
        count = min(MAX_VISUAL_DESCRIPTIONS, len(schedule))
        indices = (
            (schedule[0],)
            if count == 1
            else tuple(
                schedule[index * (len(schedule) - 1) // (count - 1)] for index in range(count)
            )
        )
    else:
        indices = tuple(sorted({0, tensor.shape[0] // 2, tensor.shape[0] - 1}))
    frames = []
    if encode:
        Image = import_module("PIL.Image")

        for index in indices:
            image = Image.fromarray((tensor[index].numpy() * 255).round().astype("uint8"))
            image.thumbnail((512, 512))
            output = io.BytesIO()
            image.save(output, format="PNG")
            frames.append(base64.b64encode(output.getvalue()).decode())
    return _Snapshot(
        digest,
        duration,
        indices,
        fps,
        {"frames": frames},
        admitted_frame_count=tensor.shape[0],
        conditioning_window=window,
    )


def _run_worker(job: dict[str, Any], binding: PerceptionHostBinding) -> list[str]:
    psutil = import_module("psutil")

    if job.get("profile_id") == VISUAL_PROFILE and (
        type(job.get("max_visual_descriptions")) is not int
        or job["max_visual_descriptions"] != MAX_VISUAL_DESCRIPTIONS
        or type(job.get("frames")) is not list
        or not 1 <= len(job["frames"]) <= MAX_VISUAL_DESCRIPTIONS
    ):
        raise PerceptionExecutionError("input_budget")
    _lease(binding)
    if not _ADMISSION.acquire(blocking=False):
        raise PerceptionExecutionError("execution_busy")
    try:
        root = binding.settings.temporary_root
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="h3-perception-", dir=root) as directory:
            owned = Path(directory)
            raw = json.dumps(job).encode()
            if len(raw) > 16 * 1024**2:
                raise PerceptionExecutionError("input_budget")
            (owned / "job.json").write_bytes(raw)
            environment = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC"}
            }
            environment.update(
                {
                    key: str(owned)
                    for key in ("TMP", "TEMP", "TMPDIR", "HF_HOME", "TORCH_HOME", "XDG_CACHE_HOME")
                }
            )
            environment.update(
                HF_HUB_OFFLINE="1",
                TRANSFORMERS_OFFLINE="1",
                PYTHONDONTWRITEBYTECODE="1",
                TOKENIZERS_PARALLELISM="false",
            )
            with (owned / "output").open("wb") as stdout, (owned / "errors").open("wb") as stderr:
                process = subprocess.Popen(  # noqa: S603 -- trusted host executable, owned worker
                    [
                        str(binding.settings.python_executable),
                        "-I",
                        str(Path(__file__).with_name("perception_worker.py")),
                        str(owned / "job.json"),
                    ],
                    cwd=owned,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000004)
                    if os.name == "nt"
                    else 0,
                )
                start = time.monotonic()
                owned_processes: dict[int, Any] = {}
                windows_job = None
                try:
                    if os.name == "nt":
                        from .perception_process import OwnedWindowsJob

                        # CRITICAL: assign the suspended launcher before it can create children.
                        windows_job = OwnedWindowsJob(process)
                    while process.poll() is None:
                        _lease(binding)
                        if time.monotonic() - start > MAX_SECONDS:
                            raise PerceptionExecutionError("execution_timeout")
                        try:
                            root_process = psutil.Process(process.pid)
                            family = [root_process, *root_process.children(recursive=True)]
                            total_rss = 0
                            for member in family:
                                owned_processes[member.pid] = (member, member.create_time())
                                total_rss += member.memory_info().rss
                            if total_rss > MAX_RSS:
                                raise PerceptionExecutionError("memory_budget")
                        except psutil.NoSuchProcess:
                            pass
                        if (owned / "output").stat().st_size > MAX_OUTPUT or (
                            owned / "errors"
                        ).stat().st_size > MAX_OUTPUT:
                            raise PerceptionExecutionError("output_budget")
                        time.sleep(0.05)
                finally:
                    try:
                        if windows_job is not None:
                            windows_job.close()
                    finally:
                        # CRITICAL: Windows venv launchers have an owned interpreter child. Reap the
                        # complete captured family before deleting media; killing only the launcher
                        # leaks inference and inherited file handles. Never kill a reused PID.
                        try:
                            root_process = psutil.Process(process.pid)
                            for member in root_process.children(recursive=True):
                                owned_processes[member.pid] = (member, member.create_time())
                        except psutil.NoSuchProcess:
                            pass
                        for member, created in reversed(list(owned_processes.values())):
                            try:
                                if member.create_time() == created:
                                    member.kill()
                                    member.wait(timeout=5)
                            except psutil.NoSuchProcess:
                                pass
                        if process.poll() is None:
                            process.kill()
                        process.wait()
            _lease(binding)
            output = (owned / "output").read_bytes()
            if process.returncode != 0 or len(output) > MAX_OUTPUT:
                raise PerceptionExecutionError("invalid_runtime_response")
            response = json.loads(output)
            if type(response) is not dict or response.get("ok") is not True:
                reason = response.get("reason") if type(response) is dict else None
                allowed = {
                    "runtime_drift",
                    "runtime_busy",
                    "runtime_unavailable",
                    "invalid_runtime_response",
                    "incomplete_output",
                    "empty_or_unbounded_output",
                    "empty_audio",
                    "unsupported_media",
                }
                raise PerceptionExecutionError(reason if reason in allowed else "execution_failed")
            descriptions = response.get("descriptions")
            if (
                type(descriptions) is not list
                or not 1
                <= len(descriptions)
                <= (MAX_VISUAL_DESCRIPTIONS if job.get("profile_id") == VISUAL_PROFILE else 3)
                or any(
                    type(text) is not str or not text.strip() or len(text) > 4096
                    for text in descriptions
                )
            ):
                raise PerceptionExecutionError("invalid_runtime_response")
            return descriptions
    finally:
        _ADMISSION.release()


def _point(value: Decimal) -> TimePoint:
    return TimePoint.from_text(format(value, "f"))


def execute_perception(
    media: PerceptionProducerResult,
    *,
    profile_id: str,
    local_service_consent: bool,
    request: RawContextRequest | None = None,
) -> QualifiedPerceptionResult:
    with _LOCK:
        binding = _BINDING
    if binding is None or not perception_configured(profile_id):
        raise PerceptionExecutionError("profile_unconfigured")
    _lease(binding)
    if profile_id == VISUAL_PROFILE and local_service_consent is not True:
        raise PerceptionExecutionError("local_service_consent_required")
    if (profile_id == VISUAL_PROFILE and media.media_kind not in {"video", "image"}) or (
        profile_id == AUDIO_PROFILE and media.media_kind != "audio"
    ):
        raise PerceptionExecutionError("unsupported_media")
    try:
        effective_frame_count = None
        if request is not None:
            from ..core.downstream_producer import _validate_exact_graph

            if profile_id != VISUAL_PROFILE or type(request) is not RawContextRequest:
                raise PerceptionExecutionError("invalid_request")
            try:
                _validate_exact_graph(request)
            except ContractValidationError:
                raise PerceptionExecutionError("invalid_request") from None
            if request.mode == TaskMode.REF2VA and media.media_kind == "video":
                view = request
                if not request.assets and not request.reference_registry.assets:
                    # Duration-only request nodes have no registry; use only the connected admitted
                    # source in this temporary view, never overwrite authored reference selections.
                    view = replace(
                        request,
                        assets=(
                            AssetDescriptor(media.asset_id, MediaKind.VIDEO, AssetRole.REFERENCE),
                        ),
                    )
                normalized = normalize_request(view)
                if normalized.request is None:
                    raise PerceptionExecutionError("invalid_request")
                if not any(
                    asset.asset_id == media.asset_id and asset.kind is MediaKind.VIDEO
                    for asset in normalized.request.assets
                ):
                    raise PerceptionExecutionError("request_source_mismatch")
                effective_frame_count = normalized.request.effective_frame_count
        snapshot = _snapshot(media, effective_frame_count=effective_frame_count)
        job = dict(snapshot.job, profile_id=profile_id)
        if profile_id == VISUAL_PROFILE:
            job["max_visual_descriptions"] = MAX_VISUAL_DESCRIPTIONS
        if profile_id == AUDIO_PROFILE:
            job.update(
                weight_path=str(binding.settings.weight_path),
                processor_dir=str(binding.settings.processor_dir),
            )
        descriptions = _run_worker(job, binding)
        if len(descriptions) != (len(snapshot.indices) if profile_id == VISUAL_PROFILE else 1):
            raise PerceptionExecutionError("invalid_runtime_response")
        uncertainty = Uncertainty(
            UncertaintyKind.LOW_CONFIDENCE,
            "Model observation is uncertain; sampled frames do not establish "
            "continuity or exact timing."
            if profile_id == VISUAL_PROFILE
            else "Whole-clip English ASR candidate; "
            "no exact dialogue, word timing or speaker authority.",
        )
        records = []
        for index, text in enumerate(descriptions):
            start = Decimal(snapshot.indices[index]) / snapshot.fps if snapshot.fps else Decimal(0)
            end = snapshot.duration if profile_id == AUDIO_PROFILE else start
            record = EvidenceRecord(
                f"{media.asset_id}.perception.{index}",
                text,
                EvidenceOrigin.OBSERVED,
                SupportStatus.UNCERTAIN,
                Provenance(
                    EvidenceSource(
                        EvidenceSourceKind.MEDIA_ASSET,
                        f"{media.asset_id}.decoded",
                        media.asset_id,
                        start=_point(start),
                        end=_point(end),
                    ),
                    ProviderIdentity.LOCAL,
                    EvidenceLevel.EXPERIMENTAL,
                    f"ollama{OLLAMA_VERSION}"
                    if profile_id == VISUAL_PROFILE
                    else "transformers4.57.6",
                    PROFILE_MODELS[profile_id],
                ),
                uncertainties=(uncertainty,),
            )
            records.append(record)
        analysis: VideoAnalysisBatch | AudioAnalysisBatch
        if profile_id == VISUAL_PROFILE:
            keyframes = tuple(
                VideoKeyframe(
                    f"{media.asset_id}.frame.{index}",
                    media.asset_id,
                    f"{media.asset_id}.decoded",
                    _point(
                        Decimal(snapshot.indices[index]) / snapshot.fps
                        if snapshot.fps
                        else Decimal(0)
                    ),
                    snapshot.indices[index] + 1,
                    uncertainties=(uncertainty,),
                )
                for index, record in enumerate(records)
            )
            observations = tuple(
                VideoObservation(
                    record.evidence_id, media.asset_id, VideoObservationKind.SCENE, record
                )
                for record in records
            )
            shots = (
                ()
                if not snapshot.fps
                else (
                    VideoShot(
                        f"{media.asset_id}.sampling_window",
                        media.asset_id,
                        f"{media.asset_id}.decoded",
                        _point(Decimal(0)),
                        _point(
                            Decimal(snapshot.conditioning_window.window_frame_count) / snapshot.fps
                            if snapshot.conditioning_window is not None
                            else snapshot.duration
                        ),
                        tuple(frame.keyframe_id for frame in keyframes),
                        tuple(item.observation_id for item in observations),
                        (uncertainty,),
                    ),
                )
            )
            analysis = VideoAnalysisBatch(
                f"{media.asset_id}.visual",
                VIDEO_ANALYSIS_SCHEMA,
                VideoAnalysisStatus.PARTIAL,
                (media.asset_id,),
                len(keyframes),
                keyframes,
                observations,
                shots,
                diagnostics=(
                    (
                        VideoAnalysisDiagnostic(
                            code="reference_frame_rate_approximate",
                            message=(
                                "Native reference conditioning counts frames at 24 fps; "
                                "timings from this clip are approximate."
                            ),
                            severity=ValidationSeverity.WARNING,
                        ),
                    )
                    if snapshot.fps and snapshot.fps != 24
                    else ()
                ),
                admitted_frame_count=snapshot.admitted_frame_count,
                frame_rate=snapshot.fps,
                conditioning_window=snapshot.conditioning_window,
            )
        else:
            analysis = AudioAnalysisBatch(
                f"{media.asset_id}.audio",
                AUDIO_ANALYSIS_SCHEMA,
                AudioAnalysisStatus.PARTIAL,
                (media.asset_id,),
                snapshot.sample_count,
                (
                    AudioObservation(
                        records[0].evidence_id,
                        media.asset_id,
                        AudioObservationKind.TRANSCRIPT,
                        records[0],
                        "en",
                    ),
                ),
            )

        def current() -> str:
            _lease(binding)
            return _snapshot(media, encode=False).digest

        return _issue_execution(profile_id, snapshot.digest, media, analysis, current)
    except PerceptionExecutionError:
        raise
    except Exception:
        raise PerceptionExecutionError("execution_failed") from None
