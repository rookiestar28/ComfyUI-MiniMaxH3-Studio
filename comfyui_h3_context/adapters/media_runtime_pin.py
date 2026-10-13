"""Data-only display and recovery pins for the supported user-provided media tools."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.av_reconstruction import qualified_ffmpeg_capability
from .media_runtime_resolution import MANAGED_PROFILE_COMPONENT

RELEASE_TAG = "2026-02-26-git-6695528af6"
RELEASE_PAGE_URL = f"https://github.com/GyanD/codexffmpeg/releases/tag/{RELEASE_TAG}"
SOURCE_LABEL = "Gyan.dev FFmpeg full build"
LICENSE_NAME = "GPL-3.0-or-later"


@dataclass(frozen=True, slots=True)
class RuntimeMember:
    published_name: str
    sha256: str
    executable: bool


@dataclass(frozen=True, slots=True)
class MediaRuntimePin:
    profile_component: str
    release_page_url: str
    source_label: str
    license_name: str
    archive_filename: str
    archive_bytes: int
    archive_sha256: str
    retained: tuple[RuntimeMember, ...]


def media_runtime_pin() -> MediaRuntimePin:
    capability = qualified_ffmpeg_capability()
    return MediaRuntimePin(
        MANAGED_PROFILE_COMPONENT,
        RELEASE_PAGE_URL,
        SOURCE_LABEL,
        LICENSE_NAME,
        f"ffmpeg-{RELEASE_TAG}-full_build.zip",
        246_558_061,
        "ad18ecdedfca6d51c28cf12f2c96287c"  # pragma: allowlist secret
        "e1150686578b75bff2efcd06ff34bb30",  # pragma: allowlist secret
        (
            RuntimeMember("bin/ffmpeg.exe", capability.ffmpeg_sha256.casefold(), True),
            RuntimeMember("bin/ffprobe.exe", capability.ffprobe_sha256.casefold(), True),
            RuntimeMember("LICENSE.txt", "", False),
            RuntimeMember("README.txt", "", False),
        ),
    )
