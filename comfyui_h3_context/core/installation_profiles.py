"""Closed installation and ownership profiles for the public package and optional runtimes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

# CRITICAL: keep this relative; ComfyUI loads custom nodes under isolated package aliases.
from .safe_paths import UnsafePathError, read_regular_file_bytes

INSTALLATION_PROFILE_SCHEMA = "h3-context-installation-profiles/1"
INSTALLATION_PROFILE_VERSION = 1
MAX_INSTALLATION_PROFILE_WIRE_BYTES = 32_768
MAX_INSTALLATION_PROFILE_JSON_DEPTH = 16
MAX_INSTALLATION_PROFILE_JSON_ITEMS = 512
INSTALLATION_PROFILE_PATH = (
    Path(__file__).resolve().parents[1] / "contracts" / "installation_profiles_v1.json"
)


class InstallationProfileError(ValueError):
    """Raised when installation-profile bytes or semantics are outside the closed contract."""


@dataclass(frozen=True, slots=True)
class InstallationCompatibilityResult:
    """Typed result for Python support and well-formed host provenance."""

    status: str
    diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InstallationProfile:
    """One explicit package, host, external-runtime, or bundled-frontend surface."""

    profile_id: str
    owner: str
    distribution: str
    compatibility: tuple[str, ...]
    required: bool
    install_mode: str
    download_policy: str
    storage_owner: str
    model_management: str
    runtime_entries: tuple[str, ...]
    removal: str
    rollback: str
    fallback: str
    end_user_node_lifecycle: str

    def to_wire(self) -> dict[str, object]:
        """Return the exact JSON-safe profile projection."""

        return {
            "profile_id": self.profile_id,
            "owner": self.owner,
            "distribution": self.distribution,
            "compatibility": list(self.compatibility),
            "required": self.required,
            "install_mode": self.install_mode,
            "download_policy": self.download_policy,
            "storage_owner": self.storage_owner,
            "model_management": self.model_management,
            "runtime_entries": list(self.runtime_entries),
            "removal": self.removal,
            "rollback": self.rollback,
            "fallback": self.fallback,
            "end_user_node_lifecycle": self.end_user_node_lifecycle,
        }


@dataclass(frozen=True, slots=True)
class InstallationProfileManifest:
    """Versioned closed inventory of supported installation surfaces."""

    schema: str
    version: int
    profiles: tuple[InstallationProfile, ...]

    def to_wire(self) -> dict[str, object]:
        """Return the exact JSON-safe manifest projection."""

        return {
            "schema": self.schema,
            "version": self.version,
            "profiles": [profile.to_wire() for profile in self.profiles],
        }

    def to_wire_bytes(self) -> bytes:
        """Return deterministic strict-UTF-8 JSON bytes with one final newline."""

        return (
            json.dumps(
                self.to_wire(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")


def build_default_installation_profiles() -> InstallationProfileManifest:
    """Build the accepted package, host, frontend and external-ownership contract."""

    return InstallationProfileManifest(
        schema=INSTALLATION_PROFILE_SCHEMA,
        version=INSTALLATION_PROFILE_VERSION,
        profiles=(
            InstallationProfile(
                profile_id="core_manual",
                owner="package",
                # IMPORTANT: match package and manifest versions; strict decoding rejects drift.
                distribution="minimax-h3-studio==1.0.2",
                compatibility=("python>=3.10",),
                required=True,
                install_mode="artifact",
                download_policy="artifact_only",
                storage_owner="package",
                model_management="none",
                runtime_entries=("comfyui_h3_context",),
                removal="remove_package_owned_files",
                rollback="reinstall_previous_verified_artifact",
                fallback="none",
                end_user_node_lifecycle="forbidden",
            ),
            InstallationProfile(
                profile_id="comfyui_native",
                owner="host",
                distribution="host-provided",
                compatibility=(
                    "comfyui-native-node-capabilities-v1",
                    "comfyui-frontend-extension-api-v1",
                ),
                required=True,
                install_mode="host_reuse",
                download_policy="forbidden",
                storage_owner="host",
                model_management="host_only",
                runtime_entries=("comfyui_h3_context",),
                removal="leave_host_runtime_unchanged",
                rollback="restore_previous_verified_node_artifact",
                fallback="none",
                end_user_node_lifecycle="forbidden",
            ),
            InstallationProfile(
                profile_id="external_ollama",
                owner="user",
                distribution="external-user-managed",
                compatibility=("h3-context-ollama-qualification/1",),
                required=False,
                install_mode="external_runtime",
                download_policy="forbidden",
                storage_owner="user",
                model_management="external_only",
                runtime_entries=(),
                removal="leave_external_runtime_unchanged",
                rollback="user_managed_external_rollback",
                fallback="none",
                end_user_node_lifecycle="forbidden",
            ),
            InstallationProfile(
                profile_id="external_openai_compatible_loopback",
                owner="user",
                distribution="external-user-managed",
                compatibility=("openai-compatible-model-list-and-chat-v1",),
                required=False,
                install_mode="external_runtime",
                download_policy="forbidden",
                storage_owner="user",
                model_management="external_only",
                runtime_entries=(),
                removal="leave_external_runtime_unchanged",
                rollback="user_managed_external_rollback",
                fallback="none",
                end_user_node_lifecycle="forbidden",
            ),
            InstallationProfile(
                profile_id="remote_openai_compatible_service",
                owner="provider",
                distribution="third-party-service",
                compatibility=("openai-compatible-model-list-and-chat-v1",),
                required=False,
                install_mode="remote_service",
                download_policy="forbidden",
                storage_owner="provider",
                model_management="provider_managed",
                runtime_entries=(),
                removal="leave_remote_service_unchanged",
                rollback="revoke_session_consent_and_clear_selection",
                fallback="none",
                end_user_node_lifecycle="forbidden",
            ),
            InstallationProfile(
                profile_id="remote_anthropic_service",
                owner="provider",
                distribution="third-party-service",
                compatibility=("anthropic-models-and-messages-v1",),
                required=False,
                install_mode="remote_service",
                download_policy="forbidden",
                storage_owner="provider",
                model_management="provider_managed",
                runtime_entries=(),
                removal="leave_remote_service_unchanged",
                rollback="revoke_session_consent_and_clear_selection",
                fallback="none",
                end_user_node_lifecycle="forbidden",
            ),
            InstallationProfile(
                profile_id="frontend_extension",
                owner="package",
                distribution="bundled-with-minimax-h3-studio",
                compatibility=("comfyui-frontend-extension-api-v1",),
                required=True,
                install_mode="bundled_asset",
                download_policy="artifact_only",
                storage_owner="package",
                model_management="none",
                runtime_entries=("comfyui_h3_context/web/h3-context-sidebar.js",),
                removal="remove_package_owned_bundle",
                rollback="reinstall_previous_verified_artifact",
                fallback="none",
                end_user_node_lifecycle="forbidden",
            ),
        ),
    )


def _numeric_version(value: object) -> tuple[int, int, int] | None:
    if (
        type(value) is not str
        or len(value) > 64
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value) is None
    ):
        return None
    major, minor, patch = value.split(".")
    try:
        return int(major), int(minor), int(patch)
    except ValueError:
        # SECURITY: hostile public version text must become a diagnostic, never an exception.
        return None


def qualify_installation_environment(
    *, python_version: str, comfyui_version: str, frontend_version: str
) -> InstallationCompatibilityResult:
    """Validate provenance syntax without turning host identity into capability."""

    diagnostics: list[str] = []
    python = _numeric_version(python_version)
    comfyui = _numeric_version(comfyui_version)
    frontend = _numeric_version(frontend_version)
    if python is None:
        diagnostics.append("invalid_python_version")
    elif python < (3, 10, 0):
        diagnostics.append("python_incompatible")
    if comfyui is None:
        diagnostics.append("invalid_comfyui_version")
    if frontend is None:
        diagnostics.append("invalid_frontend_version")
    return InstallationCompatibilityResult(
        status="supported" if not diagnostics else "incompatible",
        diagnostics=tuple(diagnostics),
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InstallationProfileError(
                f"installation profile JSON contains duplicate member {key!r}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise InstallationProfileError(f"installation profile JSON contains invalid constant {value!r}")


def _validate_resources(value: object, *, depth: int = 0, count: list[int]) -> None:
    if depth > MAX_INSTALLATION_PROFILE_JSON_DEPTH:
        raise InstallationProfileError("installation profile JSON exceeds the depth limit")
    count[0] += 1
    if count[0] > MAX_INSTALLATION_PROFILE_JSON_ITEMS:
        raise InstallationProfileError("installation profile JSON exceeds the item limit")
    if type(value) is dict:
        for key, child in cast(dict[str, object], value).items():
            if type(key) is not str or len(key) > 128:
                raise InstallationProfileError("installation profile member name is unbounded")
            _validate_resources(child, depth=depth + 1, count=count)
    elif type(value) is list:
        for child in cast(list[object], value):
            _validate_resources(child, depth=depth + 1, count=count)
    elif value is None or type(value) in {str, int, bool}:
        if type(value) is str and len(value) > 1_024:
            raise InstallationProfileError("installation profile text is unbounded")
    else:
        raise InstallationProfileError("installation profile JSON contains an unsupported type")


def validate_installation_profiles_wire(value: object) -> InstallationProfileManifest:
    """Validate one JSON-safe value against the exact accepted profile inventory."""

    _validate_resources(value, count=[0])
    expected = build_default_installation_profiles()
    try:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise InstallationProfileError("installation profile wire is not canonical JSON") from exc
    if encoded != expected.to_wire_bytes():
        raise InstallationProfileError(
            "installation profile wire does not match the frozen contract"
        )
    return expected


def decode_installation_profiles_json(
    payload: str | bytes | bytearray,
) -> InstallationProfileManifest:
    """Decode strict UTF-8 JSON with exact type, duplicate, and resource closure."""

    # CRITICAL: exact built-in types prevent hostile subclasses from bypassing byte limits.
    if type(payload) is str:
        try:
            encoded = str.encode(payload, "utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise InstallationProfileError("installation profile JSON is not strict UTF-8") from exc
    elif type(payload) is bytes:
        encoded = payload
    elif type(payload) is bytearray:
        encoded = bytes(payload)
    else:
        raise InstallationProfileError("installation profile JSON requires exact text or bytes")
    if not encoded or len(encoded) > MAX_INSTALLATION_PROFILE_WIRE_BYTES:
        raise InstallationProfileError("installation profile JSON exceeds the bounded byte limit")
    try:
        value = json.loads(
            encoded.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except InstallationProfileError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InstallationProfileError("installation profile JSON is not strict JSON") from exc
    return validate_installation_profiles_wire(value)


def load_installation_profiles(
    path: Path = INSTALLATION_PROFILE_PATH,
) -> InstallationProfileManifest:
    """Load the package-owned regular-file profile manifest through the strict decoder."""

    try:
        payload = read_regular_file_bytes(
            path,
            maximum_bytes=MAX_INSTALLATION_PROFILE_WIRE_BYTES,
        )
        return decode_installation_profiles_json(payload)
    except UnsafePathError as exc:
        raise InstallationProfileError(
            "installation profile manifest contains an unsafe link or reparse path"
        ) from exc
    except OSError as exc:
        raise InstallationProfileError("installation profile manifest cannot be read") from exc


__all__ = [
    "INSTALLATION_PROFILE_PATH",
    "INSTALLATION_PROFILE_SCHEMA",
    "INSTALLATION_PROFILE_VERSION",
    "MAX_INSTALLATION_PROFILE_JSON_DEPTH",
    "MAX_INSTALLATION_PROFILE_JSON_ITEMS",
    "MAX_INSTALLATION_PROFILE_WIRE_BYTES",
    "InstallationProfile",
    "InstallationCompatibilityResult",
    "InstallationProfileError",
    "InstallationProfileManifest",
    "build_default_installation_profiles",
    "decode_installation_profiles_json",
    "load_installation_profiles",
    "qualify_installation_environment",
    "validate_installation_profiles_wire",
]
