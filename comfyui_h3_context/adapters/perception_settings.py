"""Stateless operator-file boundary; activation belongs to the host lifetime owner."""

from __future__ import annotations

import atexit
import json
import os
import re
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Any, cast

from ..core.errors import ContractValidationError
from ..core.perception_execution import AUDIO_PROFILE, PROFILE_MODELS, VISUAL_PROFILE
from ..core.private_storage_layout import private_file_path
from . import perception_host as host
from .media_runtime_resolution import ComfyHostRootPort, _overlaps
from .perception_host import PerceptionHostSettings

SETTINGS_SCHEMA = "h3-context-perception-host/1"
STATUS_SCHEMA = "h3.context.perception_host_status.v1"
MAX_SETTINGS_BYTES = 16384
SETTINGS_REASONS = frozenset(
    {
        "none",
        "settings_missing",
        "settings_too_large",
        "settings_invalid",
        "settings_unavailable",
        "path_refused",
        "unsupported_platform",
        "host_unavailable",
        "configuration_revoked",
        "binding_exists",
    }
)
_REQUIRED = frozenset({"schema", "python_executable", "temporary_root", "profiles"})
_OPTIONAL = frozenset({"weight_path", "processor_dir"})
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\Z", re.I)


class PerceptionSettingsError(ContractValidationError):
    """Finite content-free refusal at the operator-file boundary."""

    def __init__(self, code: str) -> None:
        self.code = (
            code
            if type(code) is str and code in SETTINGS_REASONS and code != "none"
            else "settings_unavailable"
        )
        super().__init__(self.code)


def _local_path(value: object) -> Path:
    if type(value) is not str or not value or "\x00" in value:
        raise PerceptionSettingsError("path_refused")
    pure = PureWindowsPath(value)
    # CRITICAL: reject Windows aliases/namespaces before filesystem normalization; accepting
    # drive-relative, device, stream or parent aliases could read a different operator target.
    if (
        re.fullmatch(r"[A-Za-z]:", pure.drive) is None
        or not pure.root
        or any(part in {".", ".."} for part in value.replace("\\", "/").split("/")[1:])
        or any(
            part.endswith((".", " "))
            or _RESERVED.fullmatch(part.split(".", 1)[0])
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
            for part in pure.parts[1:]
        )
    ):
        raise PerceptionSettingsError("path_refused")
    return Path(value)


def _identity(metadata: Any) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        getattr(metadata, "st_file_attributes", 0),
    )


def _file_identity(metadata: Any) -> tuple[int, ...]:
    # CRITICAL: Windows path/descriptor ctime semantics differ after a rewrite. Compare stable
    # identity, size and mtime; including ctime falsely refuses unchanged valid operator files.
    return (*_identity(metadata), metadata.st_size, metadata.st_mtime_ns)


def _inspect(
    path: Path, *, directory: bool, missing: str = "path_refused"
) -> tuple[tuple[int, ...], ...]:
    components = (*reversed(path.parents), path)
    proof = []
    for index, component in enumerate(components):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            raise PerceptionSettingsError(missing) from None
        except OSError:
            raise PerceptionSettingsError("path_refused") from None
        leaf = index == len(components) - 1
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (
                stat.S_ISDIR(metadata.st_mode)
                if directory or not leaf
                else stat.S_ISREG(metadata.st_mode)
            )
        ):
            raise PerceptionSettingsError("path_refused")
        proof.append(_file_identity(metadata) if leaf and not directory else _identity(metadata))
    return tuple(proof)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("settings_invalid")
        result[key] = value
    return result


def _non_finite(_value: str) -> None:
    raise ValueError("settings_invalid")


def load_perception_settings(
    path: Path, *, cancelled: Callable[[], bool]
) -> PerceptionHostSettings:
    if os.name != "nt":
        raise PerceptionSettingsError("unsupported_platform")
    if type(path) is not type(Path()) or not callable(cancelled):
        raise PerceptionSettingsError("settings_invalid")
    path = _local_path(str(path))
    before = _inspect(path, directory=False, missing="settings_missing")
    try:
        with path.open("rb") as stream:
            descriptor_before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(descriptor_before.st_mode)
                or _file_identity(descriptor_before) != before[-1]
            ):
                raise PerceptionSettingsError("path_refused")
            # CRITICAL: bound before UTF-8/JSON work, then recheck descriptor and every ancestor.
            # Publishing after a replaced/modified read would bind bytes from a stale file target.
            raw = stream.read(MAX_SETTINGS_BYTES + 1)
            if _file_identity(os.fstat(stream.fileno())) != before[-1]:
                raise PerceptionSettingsError("path_refused")
    except OSError:
        raise PerceptionSettingsError("settings_unavailable") from None
    if _inspect(path, directory=False) != before:
        raise PerceptionSettingsError("path_refused")
    if len(raw) > MAX_SETTINGS_BYTES:
        raise PerceptionSettingsError("settings_too_large")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_non_finite
        )
    except (UnicodeError, ValueError, RecursionError):
        raise PerceptionSettingsError("settings_invalid") from None
    if (
        type(value) is not dict
        or not _REQUIRED <= value.keys()
        or not value.keys() <= _REQUIRED | _OPTIONAL
        or value["schema"] != SETTINGS_SCHEMA
        or type(value["schema"]) is not str
        or any(
            type(value[name]) is not str
            for name in ("python_executable", "temporary_root", *(_OPTIONAL & value.keys()))
        )
    ):
        raise PerceptionSettingsError("settings_invalid")
    profiles = value["profiles"]
    if (
        type(profiles) is not list
        or not profiles
        or len(profiles) > len(PROFILE_MODELS)
        or any(type(profile) is not str or profile not in PROFILE_MODELS for profile in profiles)
        or len(set(profiles)) != len(profiles)
        or (AUDIO_PROFILE in profiles and not _OPTIONAL <= value.keys())
    ):
        raise PerceptionSettingsError("settings_invalid")
    paths = {
        name: _local_path(value[name])
        for name in ("python_executable", "temporary_root", *(_OPTIONAL & value.keys()))
    }
    inspections = {
        name: _inspect(target, directory=name in {"temporary_root", "processor_dir"})
        for name, target in paths.items()
    }
    try:
        settings = PerceptionHostSettings(
            paths["python_executable"],
            paths["temporary_root"],
            paths.get("weight_path"),
            paths.get("processor_dir"),
            cancelled,
            profiles=frozenset(profiles),
        )
    except ContractValidationError:
        raise PerceptionSettingsError("settings_invalid") from None
    if _inspect(path, directory=False) != before or any(
        _inspect(paths[name], directory=name in {"temporary_root", "processor_dir"}) != proof
        for name, proof in inspections.items()
    ):
        raise PerceptionSettingsError("path_refused")
    return settings


class _OperatorSettingsOwner:
    """Owns exactly one operator attempt and its binding; status reads never retry."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._attempted = False
        self._binding: host.PerceptionHostBinding | None = None
        self._reason = "host_unavailable"
        self._observed = False

    def __repr__(self) -> str:
        return "<OperatorPerceptionSettingsOwner>"

    def ensure_configured(self) -> bool:
        with self._lock:
            if self._attempted:
                with host._LOCK:
                    return self._binding is not None and host._is_current(self._binding)
            if os.name != "nt":
                self._attempted = True
                self._reason = "unsupported_platform"
                return False
            if sys.modules.get("folder_paths") is None:
                return False
            self._attempted = True
            with host._LOCK:
                if host._BINDING is not None:
                    self._reason = "binding_exists"
                    return False
            try:
                port = ComfyHostRootPort()
                private, served = port.private_root(), port.served_roots()
                if any(_overlaps(private, root) for root in served):
                    self._reason = "path_refused"
                    return False
                path = private_file_path(private, "perception_host.json")
            except Exception:
                self._reason = "host_unavailable"
                return False
            try:
                # CRITICAL: optional loaded modules can raise from attribute lookup. Keep the
                # lookup inside the cached finite-refusal guard or package activation can escape.
                observer = getattr(
                    sys.modules.get("comfy.model_management"), "processing_interrupted", None
                )
                observed = callable(observer)
                cancelled = cast(Callable[[], bool], observer) if observed else lambda: False
                settings = load_perception_settings(path, cancelled=cancelled)
                # CRITICAL: operator file IO runs outside the binding lock. Recheck at commit so
                # a manual binding registered during the read cannot be silently overwritten.
                with host._LOCK:
                    if host._BINDING is not None:
                        self._reason = "binding_exists"
                        return False
                    self._binding = host.configure_perception_host(settings)
                atexit.register(self.close)
            except PerceptionSettingsError as error:
                self._reason = error.code
                return False
            except Exception:
                if self._binding is not None:
                    host.clear_perception_host(self._binding)
                    self._binding = None
                self._reason = "settings_unavailable"
                return False
            self._observed = observed
            self._reason = "none"
            return True

    def status(self) -> dict[str, object]:
        with self._lock, host._LOCK:
            if os.name != "nt":
                reason = "unsupported_platform"
            elif host._BINDING is not None:
                binding = host._BINDING
                if host._is_current(binding):
                    visual, audio = (
                        host.perception_configured(VISUAL_PROFILE),
                        host.perception_configured(AUDIO_PROFILE),
                    )
                    if visual or audio:
                        return {
                            "schema": STATUS_SCHEMA,
                            "state": "configured",
                            "visual": visual,
                            "audio": audio,
                            "reason": "none",
                            "cancellation_observed": self._observed and binding is self._binding,
                        }
                    reason = "settings_invalid"
                else:
                    reason = "configuration_revoked"
            else:
                reason = "configuration_revoked" if self._binding is not None else self._reason
        return {
            "schema": STATUS_SCHEMA,
            "state": "not_configured"
            if reason in {"host_unavailable", "settings_missing"}
            else "refused",
            "visual": False,
            "audio": False,
            "reason": reason,
            "cancellation_observed": False,
        }

    def close(self) -> None:
        with self._lock:
            if self._binding is not None:
                host.clear_perception_host(self._binding)


_OWNER = _OperatorSettingsOwner()


def activate_operator_perception_host() -> bool:
    return _OWNER.ensure_configured()


def read_perception_host_status() -> dict[str, object]:
    return _OWNER.status()
