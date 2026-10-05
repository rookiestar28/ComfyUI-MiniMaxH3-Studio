"""Pure, fail-closed security policy primitives for future adapters.

This module deliberately performs no I/O. URL admission does not resolve DNS, local-path
admission does not read media, and receipt values exclude credentials and private payloads.
Network, media, provider, cache, and cancellation implementations remain owned by later
adapters and contracts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from ipaddress import ip_address
from math import isfinite
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from .contracts import MediaKind
from .errors import SecurityPolicyError

ProviderName = Literal["manual", "local", "remote_custom", "official_minimax"]
PrivacyMode = Literal["local_only", "explicit_remote"]
ReceiptStatus = Literal["submitted", "succeeded", "failed", "cancelled"]

_PROVIDERS = frozenset({"manual", "local", "remote_custom", "official_minimax"})
_REMOTE_PROVIDERS = frozenset({"remote_custom", "official_minimax"})
_PRIVACY_MODES = frozenset({"local_only", "explicit_remote"})
_RECEIPT_STATUSES = frozenset({"submitted", "succeeded", "failed", "cancelled"})
_PUBLIC_IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_FINGERPRINT = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_MAX_ALLOWLIST_ITEMS = 64
_MEDIA_TYPES = {
    MediaKind.IMAGE: frozenset(
        {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
    ),
    MediaKind.VIDEO: frozenset({"video/mp4", "video/quicktime"}),
    MediaKind.AUDIO: frozenset({"audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3"}),
}


@dataclass(frozen=True, slots=True)
class RemoteExecutionPolicy:
    """Require an explicit provider, privacy mode, and upload-consent decision."""

    provider: ProviderName
    privacy_mode: PrivacyMode
    upload_consent: bool

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDERS:
            raise SecurityPolicyError(f"unknown provider: {self.provider!r}")
        if self.privacy_mode not in _PRIVACY_MODES:
            raise SecurityPolicyError(f"unknown privacy mode: {self.privacy_mode!r}")
        if not isinstance(self.upload_consent, bool):
            raise SecurityPolicyError("upload_consent must be a boolean")

        is_remote = self.provider in _REMOTE_PROVIDERS
        if is_remote and self.privacy_mode != "explicit_remote":
            raise SecurityPolicyError("remote providers require explicit_remote privacy mode")
        if is_remote and not self.upload_consent:
            raise SecurityPolicyError("remote providers require explicit media-upload consent")
        if not is_remote and self.privacy_mode != "local_only":
            raise SecurityPolicyError("local providers require local_only privacy mode")
        if not is_remote and self.upload_consent:
            raise SecurityPolicyError("local providers cannot declare remote upload consent")


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Bound resource declarations shared by future media/provider adapters.

    Retry count and cache TTL may be zero to explicitly disable those behaviors; all other
    bounds must be strictly positive. Enforcing these values is owned by the executing adapter.
    """

    max_bytes: int
    max_duration_seconds: float
    max_width: int
    max_height: int
    max_frames: int
    max_sample_rate: int
    max_references: int
    max_concurrency: int
    max_memory_bytes: int
    max_wall_time_seconds: float
    max_retries: int
    max_cache_ttl_seconds: float

    def __post_init__(self) -> None:
        positive_integers = (
            "max_bytes",
            "max_width",
            "max_height",
            "max_frames",
            "max_sample_rate",
            "max_references",
            "max_concurrency",
            "max_memory_bytes",
        )
        for name in positive_integers:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise SecurityPolicyError(f"{name} must be a positive integer")

        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int):
            raise SecurityPolicyError("max_retries must be a non-negative integer")
        if self.max_retries < 0:
            raise SecurityPolicyError("max_retries must be a non-negative integer")

        non_negative_floats = (
            "max_duration_seconds",
            "max_wall_time_seconds",
            "max_cache_ttl_seconds",
        )
        for name in non_negative_floats:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SecurityPolicyError(f"{name} must be a finite non-negative number")
            if not isfinite(float(value)) or float(value) <= 0 and name != "max_cache_ttl_seconds":
                raise SecurityPolicyError(f"{name} must be a finite positive number")
            if name == "max_cache_ttl_seconds" and float(value) < 0:
                raise SecurityPolicyError(f"{name} must be a finite non-negative number")


@dataclass(frozen=True, slots=True)
class MediaTransferTimeouts:
    """Finite deadlines a concrete media transport must enforce."""

    connect_seconds: float
    read_seconds: float
    total_seconds: float

    def __post_init__(self) -> None:
        values = (
            (self.connect_seconds, "connect_seconds"),
            (self.read_seconds, "read_seconds"),
            (self.total_seconds, "total_seconds"),
        )
        for value, field_name in values:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or float(value) <= 0
            ):
                raise SecurityPolicyError(f"{field_name} must be a finite positive number")
        if float(self.total_seconds) < max(float(self.connect_seconds), float(self.read_seconds)):
            raise SecurityPolicyError("total_seconds must cover connect and read deadlines")

    def to_public_dict(self) -> dict[str, float]:
        return {
            "connect_seconds": float(self.connect_seconds),
            "read_seconds": float(self.read_seconds),
            "total_seconds": float(self.total_seconds),
        }


class MediaSourceKind(str, Enum):
    """Explicit source ownership; no source kind is inferred from a locator."""

    REMOTE_URL = "remote_url"
    LOCAL_PATH = "local_path"


def _bounded_string_tuple(
    values: Iterable[str], field_name: str, *, allow_empty: bool = True
) -> tuple[str, ...]:
    try:
        result = tuple(values)
    except (TypeError, ValueError) as exc:
        raise SecurityPolicyError(f"{field_name} must be a bounded string tuple") from exc
    if len(result) > _MAX_ALLOWLIST_ITEMS or (not allow_empty and not result):
        raise SecurityPolicyError(f"{field_name} must contain at most {_MAX_ALLOWLIST_ITEMS} items")
    if not all(isinstance(item, str) for item in result):
        raise SecurityPolicyError(f"{field_name} must contain only strings")
    if len(result) != len(set(result)):
        raise SecurityPolicyError(f"{field_name} must not contain duplicates")
    return result


def _normalise_path_prefix(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.startswith("/")
        or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value)
        or "\\" in value
    ):
        raise SecurityPolicyError("URL path prefixes must be absolute and safe")
    decoded = unquote(value)
    if "\\" in decoded or any(segment in {".", ".."} for segment in decoded.split("/")):
        raise SecurityPolicyError("URL path prefixes must not contain traversal segments")
    return value.rstrip("/") or "/"


@dataclass(frozen=True, slots=True)
class MediaTransferPolicy:
    """Runtime-only admission and resource policy for one media transfer batch."""

    allowed_hosts: tuple[str, ...]
    allowed_url_path_prefixes: tuple[str, ...]
    allowed_local_roots: tuple[str | Path, ...] = field(repr=False, compare=False)
    max_bytes: int = 0
    max_duration_seconds: float = 0.0
    max_references: int = 0
    timeouts: MediaTransferTimeouts = field(
        default_factory=lambda: MediaTransferTimeouts(1.0, 1.0, 1.0)
    )
    max_redirects: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_hosts, tuple):
            raise SecurityPolicyError("allowed_hosts must be a tuple")
        if not isinstance(self.allowed_url_path_prefixes, tuple):
            raise SecurityPolicyError("allowed_url_path_prefixes must be a tuple")
        if not isinstance(self.allowed_local_roots, tuple):
            raise SecurityPolicyError("allowed_local_roots must be a tuple")
        hosts = _bounded_string_tuple(self.allowed_hosts, "allowed_hosts")
        prefixes = _bounded_string_tuple(
            self.allowed_url_path_prefixes, "allowed_url_path_prefixes"
        )
        roots = tuple(self.allowed_local_roots)
        if len(roots) > _MAX_ALLOWLIST_ITEMS:
            raise SecurityPolicyError(
                f"allowed_local_roots must contain at most {_MAX_ALLOWLIST_ITEMS} items"
            )
        if not all(isinstance(root, (str, Path)) and str(root) for root in roots):
            raise SecurityPolicyError("allowed_local_roots must contain non-empty paths")
        normalised_hosts = tuple(_normalise_host(host) for host in hosts)
        normalised_prefixes = tuple(_normalise_path_prefix(prefix) for prefix in prefixes)
        if len(normalised_hosts) != len(set(normalised_hosts)):
            raise SecurityPolicyError("allowed_hosts must not contain duplicate hosts")
        if len(normalised_prefixes) != len(set(normalised_prefixes)):
            raise SecurityPolicyError("allowed_url_path_prefixes must not contain duplicates")
        if (
            not isinstance(self.max_bytes, int)
            or isinstance(self.max_bytes, bool)
            or self.max_bytes <= 0
        ):
            raise SecurityPolicyError("max_bytes must be a positive integer")
        if (
            isinstance(self.max_duration_seconds, bool)
            or not isinstance(self.max_duration_seconds, (int, float))
            or not isfinite(float(self.max_duration_seconds))
            or float(self.max_duration_seconds) <= 0
        ):
            raise SecurityPolicyError("max_duration_seconds must be a finite positive number")
        if (
            not isinstance(self.max_references, int)
            or isinstance(self.max_references, bool)
            or self.max_references <= 0
        ):
            raise SecurityPolicyError("max_references must be a positive integer")
        if not isinstance(self.timeouts, MediaTransferTimeouts):
            raise SecurityPolicyError("timeouts must be a MediaTransferTimeouts value")
        if (
            not isinstance(self.max_redirects, int)
            or isinstance(self.max_redirects, bool)
            or not 0 <= self.max_redirects <= 5
        ):
            raise SecurityPolicyError("max_redirects must be an integer between 0 and 5")
        object.__setattr__(self, "allowed_hosts", normalised_hosts)
        object.__setattr__(self, "allowed_url_path_prefixes", normalised_prefixes)
        object.__setattr__(self, "allowed_local_roots", roots)

    def to_public_dict(self) -> dict[str, object]:
        """Return budgets only; hosts, roots, and runtime locators are not portable fields."""

        return {
            "max_bytes": self.max_bytes,
            "max_duration_seconds": float(self.max_duration_seconds),
            "max_references": self.max_references,
            "timeouts": self.timeouts.to_public_dict(),
            "max_redirects": self.max_redirects,
        }


@dataclass(frozen=True, slots=True)
class MediaSource:
    """Runtime-only media locator and declared metadata supplied to admission checks."""

    source_kind: MediaSourceKind
    locator: str | Path = field(repr=False, compare=False)
    media_kind: MediaKind
    media_type: str
    size_bytes: int
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, MediaSourceKind):
            raise SecurityPolicyError("source_kind must be a MediaSourceKind")
        if not isinstance(self.locator, (str, Path)) or not str(self.locator):
            raise SecurityPolicyError("media locator must be non-empty")
        if self.source_kind is MediaSourceKind.REMOTE_URL and not isinstance(self.locator, str):
            raise SecurityPolicyError("remote media locators must be strings")
        if not isinstance(self.media_kind, MediaKind):
            raise SecurityPolicyError("media_kind must be a MediaKind")
        if (
            not isinstance(self.media_type, str)
            or not self.media_type
            or self.media_type != self.media_type.strip().casefold()
            or any(char.isspace() or ord(char) < 0x20 for char in self.media_type)
            or self.media_type not in _MEDIA_TYPES[self.media_kind]
        ):
            raise SecurityPolicyError("media_type is not approved for the media kind")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes <= 0
        ):
            raise SecurityPolicyError("size_bytes must be a positive integer")
        if self.duration_seconds is not None and (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not isfinite(float(self.duration_seconds))
            or float(self.duration_seconds) <= 0
        ):
            raise SecurityPolicyError("duration_seconds must be finite and positive")
        if self.media_kind in {MediaKind.VIDEO, MediaKind.AUDIO} and self.duration_seconds is None:
            raise SecurityPolicyError("video and audio sources require duration_seconds")
        object.__setattr__(self, "media_type", self.media_type.casefold())

    def to_public_dict(self) -> dict[str, object]:
        """Return safe metadata without the raw URL or local path."""

        return {
            "source_kind": self.source_kind.value,
            "media_kind": self.media_kind.value,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class ValidatedMediaSource:
    """Admission result retaining a hidden runtime locator for an injected uploader."""

    source_kind: MediaSourceKind
    locator: str | Path = field(repr=False, compare=False)
    media_kind: MediaKind
    media_type: str
    size_bytes: int
    duration_seconds: float | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "source_kind": self.source_kind.value,
            "media_kind": self.media_kind.value,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class ValidatedRemoteURL:
    """A URL admitted by an exact-host, HTTPS-only, network-free policy."""

    scheme: Literal["https"]
    hostname: str
    port: int | None
    path: str


def _normalise_host(host: str) -> str:
    if not isinstance(host, str) or not host:
        raise SecurityPolicyError("URL host must be non-empty")
    try:
        normalised = host.encode("idna").decode("ascii").casefold().rstrip(".")
    except UnicodeError as exc:
        raise SecurityPolicyError("URL host is not valid IDNA") from exc
    if not normalised or any(char in normalised for char in "/\\@"):
        raise SecurityPolicyError("URL host contains forbidden characters")
    return normalised


def normalise_url_host(host: str) -> str:
    """Normalise a URL host once, for every admission path that has to compare hosts."""

    return _normalise_host(host)


def validate_remote_url(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    allowed_path_prefixes: Iterable[str] | None = None,
) -> ValidatedRemoteURL:
    """Validate an allowlisted HTTPS URL without DNS or network side effects."""

    if not isinstance(url, str) or not url or url != url.strip():
        raise SecurityPolicyError("URL must be a non-empty string without surrounding whitespace")
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise SecurityPolicyError("URL contains whitespace or control characters")
    if "\\" in url:
        raise SecurityPolicyError("URL backslashes are not accepted")

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise SecurityPolicyError("URL is malformed") from exc

    if parsed.scheme.casefold() != "https":
        raise SecurityPolicyError("only HTTPS provider URLs are accepted")
    if parsed.username is not None or parsed.password is not None:
        raise SecurityPolicyError("URL userinfo is not accepted")
    if hostname is None:
        raise SecurityPolicyError("URL host is missing")
    if parsed.query or parsed.fragment:
        raise SecurityPolicyError("URL query and fragment data are not accepted")
    if port is not None and not 1 <= port <= 65535:
        raise SecurityPolicyError("URL port must be between 1 and 65535")

    normalised_host = _normalise_host(hostname)
    allowed = frozenset(_normalise_host(value) for value in allowed_hosts)
    if normalised_host not in allowed:
        raise SecurityPolicyError("URL host is not in the explicit provider allowlist")

    try:
        literal = ip_address(normalised_host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise SecurityPolicyError("URL literal address is not globally reachable")

    path = parsed.path or "/"
    decoded_path = unquote(path)
    if "\\" in decoded_path or any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise SecurityPolicyError("URL path contains traversal segments")
    if allowed_path_prefixes is not None:
        prefixes = tuple(_normalise_path_prefix(prefix) for prefix in allowed_path_prefixes)
        if not prefixes:
            raise SecurityPolicyError("URL path is not in the explicit provider allowlist")
        if not any(
            prefix == "/" or path == prefix or path.startswith(prefix.rstrip("/") + "/")
            for prefix in prefixes
        ):
            raise SecurityPolicyError("URL path is not in the explicit provider allowlist")

    return ValidatedRemoteURL(
        scheme="https",
        hostname=normalised_host,
        port=port,
        path=path,
    )


def validate_local_path(
    candidate: str | Path,
    allowed_root: str | Path,
    *,
    require_file: bool = True,
    require_exists: bool = False,
) -> Path:
    """Resolve a local candidate beneath an explicit root without reading its contents."""

    try:
        root = Path(allowed_root).resolve(strict=False)
        if not root.is_dir():
            raise SecurityPolicyError("allowed_root must be an existing directory")
        raw_candidate = Path(candidate)
        joined = raw_candidate if raw_candidate.is_absolute() else root / raw_candidate
        cursor = joined
        while True:
            if cursor.is_symlink():
                raise SecurityPolicyError("symlink path components are not accepted")
            parent = cursor.parent
            if cursor == parent or cursor == root:
                break
            cursor = parent
        resolved = joined.resolve(strict=False)
    except (OSError, RuntimeError, TypeError) as exc:
        raise SecurityPolicyError("local path cannot be resolved safely") from exc

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SecurityPolicyError("local path escapes the allowed root") from exc

    exists = resolved.exists()
    if require_exists and not exists:
        raise SecurityPolicyError("local path must identify an existing file")
    if require_file and exists and not resolved.is_file():
        raise SecurityPolicyError("local path must resolve to a regular file")
    return resolved


def _validate_media_source(
    source: MediaSource,
    policy: MediaTransferPolicy,
) -> ValidatedMediaSource:
    if source.size_bytes > policy.max_bytes:
        raise SecurityPolicyError("media source exceeds the declared byte limit")
    if source.duration_seconds is not None and (
        source.duration_seconds > policy.max_duration_seconds
    ):
        raise SecurityPolicyError("media source exceeds the declared duration limit")

    if source.source_kind is MediaSourceKind.REMOTE_URL:
        if not isinstance(source.locator, str):
            raise SecurityPolicyError("remote media locators must be strings")
        validate_remote_url(
            source.locator,
            allowed_hosts=policy.allowed_hosts,
            allowed_path_prefixes=policy.allowed_url_path_prefixes,
        )
        locator: str | Path = source.locator
    else:
        if not policy.allowed_local_roots:
            raise SecurityPolicyError("local media requires an explicit allowed root")
        resolved: Path | None = None
        for root in policy.allowed_local_roots:
            try:
                resolved = validate_local_path(
                    source.locator,
                    root,
                    require_file=True,
                    require_exists=True,
                )
            except SecurityPolicyError:
                continue
            break
        if resolved is None:
            raise SecurityPolicyError("local media is outside every allowed root")
        locator = resolved

    return ValidatedMediaSource(
        source_kind=source.source_kind,
        locator=locator,
        media_kind=source.media_kind,
        media_type=source.media_type,
        size_bytes=source.size_bytes,
        duration_seconds=source.duration_seconds,
    )


def validate_media_sources(
    sources: Iterable[MediaSource],
    policy: MediaTransferPolicy,
) -> tuple[ValidatedMediaSource, ...]:
    """Validate a bounded batch of explicit media sources without opening or uploading them."""

    if not isinstance(policy, MediaTransferPolicy):
        raise SecurityPolicyError("policy must be a MediaTransferPolicy")
    validated: list[ValidatedMediaSource] = []
    try:
        iterator = iter(sources)
    except TypeError as exc:
        raise SecurityPolicyError("sources must be iterable") from exc
    for source in iterator:
        if len(validated) >= policy.max_references:
            raise SecurityPolicyError("media source count exceeds the declared reference limit")
        if not isinstance(source, MediaSource):
            raise SecurityPolicyError("sources must contain MediaSource values")
        validated.append(_validate_media_source(source, policy))
    if not validated:
        raise SecurityPolicyError("at least one media source is required")
    return tuple(validated)


def _check_public_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or _PUBLIC_IDENTIFIER.fullmatch(value) is None:
        raise SecurityPolicyError(f"{field} must be a bounded public identifier")


def _check_fingerprint(value: str, field: str) -> None:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise SecurityPolicyError(f"{field} must be a SHA-256 fingerprint")


@dataclass(frozen=True, slots=True)
class RedactedReceipt:
    """Portable provider receipt fields that intentionally exclude sensitive payloads."""

    provider: ProviderName
    status: ReceiptStatus
    task_id: str | None = None
    request_fingerprint: str | None = None
    output_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDERS:
            raise SecurityPolicyError(f"unknown provider: {self.provider!r}")
        if self.status not in _RECEIPT_STATUSES:
            raise SecurityPolicyError(f"unknown receipt status: {self.status!r}")
        if self.task_id is not None:
            _check_public_identifier(self.task_id, "task_id")
        if self.request_fingerprint is not None:
            _check_fingerprint(self.request_fingerprint, "request_fingerprint")
        if self.output_fingerprint is not None:
            _check_fingerprint(self.output_fingerprint, "output_fingerprint")

    def to_public_dict(self) -> dict[str, str | None]:
        """Return only the fields approved for portable, non-secret reporting."""

        return {
            "provider": self.provider,
            "status": self.status,
            "task_id": self.task_id,
            "request_fingerprint": self.request_fingerprint,
            "output_fingerprint": self.output_fingerprint,
        }


__all__ = [
    "PrivacyMode",
    "ProviderName",
    "ReceiptStatus",
    "RedactedReceipt",
    "RemoteExecutionPolicy",
    "ResourceLimits",
    "MediaSource",
    "MediaSourceKind",
    "MediaTransferPolicy",
    "MediaTransferTimeouts",
    "SecurityPolicyError",
    "ValidatedMediaSource",
    "ValidatedRemoteURL",
    "validate_local_path",
    "validate_media_sources",
    "validate_remote_url",
]
