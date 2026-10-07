"""M25-31: one-time managed media setup and safe binary acquisition.

Synthetic archives under a substituted manifest are hermetic contract evidence for validation,
extraction and publication. Two rows use real inputs only when a caller supplies them explicitly:
the verified release archive (`H3_M25_31_MANAGED_ARCHIVE_PATH`) and one live download through the
production transport (`H3_M25_31_LIVE_DOWNLOAD=1`).
"""

from __future__ import annotations

import asyncio
import email.message
import functools
import hashlib
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from deployment_request_doubles import LOOPBACK_HOST, ListenerTransport

import comfyui_h3_context.adapters.comfyui_media_runtime_setup as routes_module
import comfyui_h3_context.adapters.media_runtime_download as download_module
import comfyui_h3_context.adapters.media_runtime_installer as installer_module
from comfyui_h3_context.adapters.comfyui_media_runtime_setup import (
    JOB_ROUTE,
    JOB_SCHEMA,
    REQUEST_SCHEMA,
    SETUP_ROUTE,
    STATUS_ROUTE,
    STATUS_SCHEMA,
    MediaRuntimeSetupError,
    MediaRuntimeSetupService,
    decode_setup_request,
    dispatch_setup_request,
    ensure_media_runtime_setup_route_registered,
)
from comfyui_h3_context.adapters.executable_admission import windows_open_read_pin
from comfyui_h3_context.adapters.media_runtime_discovery_worker import (
    DiscoveryRequest,
    DiscoveryResponse,
    admit_request,
)
from comfyui_h3_context.adapters.media_runtime_download import (
    ASSET_HOSTS,
    INITIAL_HOST,
    DownloadError,
    HttpsArchiveDownloader,
    admit_redirect,
    admit_source_url,
)
from comfyui_h3_context.adapters.media_runtime_installer import (
    SOURCE_URL,
    InstallerError,
    ManagedRuntimeInstaller,
    ManagedRuntimeManifest,
    RetainedMember,
    cleanup_job_directory,
    managed_runtime_manifest,
    validate_archive_members,
)
from comfyui_h3_context.adapters.media_runtime_resolution import (
    MANAGED_PROFILE_COMPONENT,
    HostRootError,
    MediaRuntimeConfig,
    MediaRuntimeConfigError,
    MediaRuntimeResolution,
    MediaRuntimeResolver,
    MediaRuntimeSelection,
    ResolutionReason,
    ResolutionState,
    RuntimeInputs,
    SourceKind,
    SubprocessDiscoveryWorker,
    private_layout,
)
from comfyui_h3_context.adapters.prompt_model_transport import PromptModelTransportError
from comfyui_h3_context.core.av_reconstruction import qualified_ffmpeg_capability
from comfyui_h3_context.core.prompt_model_provider import PromptModelOutcomeId
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows managed runtime installer")

TOP = "pkg-full_build/"
FFMPEG = b"MZ hermetic ffmpeg " * 4096
FFPROBE = b"MZ hermetic ffprobe " * 4096
LICENSE = b"GNU GENERAL PUBLIC LICENSE fixture\n" * 64
README = b"source notice fixture\n" * 64
JOB = "ab" * 16
OTHER_JOB = "cd" * 16
PLENTY = SimpleNamespace(free=10**12)
UNSAFE_TOKEN = "../escape"  # noqa: S105 - a path, not a credential


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------------------------
# Archive fixtures


def build_archive(
    *,
    extra: tuple[tuple[str, bytes | None], ...] = (),
    ffmpeg: bytes = FFMPEG,
    license_bytes: bytes = LICENSE,
) -> bytes:
    entries: list[tuple[str, bytes | None]] = [
        (TOP, None),
        (f"{TOP}bin/", None),
        (f"{TOP}bin/ffmpeg.exe", ffmpeg),
        (f"{TOP}bin/ffprobe.exe", FFPROBE),
        (f"{TOP}bin/ffplay.exe", b"not retained"),
        (f"{TOP}doc/", None),
        (f"{TOP}doc/manual.html", b"<html>not retained</html>"),
        (f"{TOP}LICENSE", license_bytes),
        (f"{TOP}README.txt", README),
        *extra,
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            if payload is None:
                archive.writestr(zipfile.ZipInfo(name), b"")
            else:
                archive.writestr(name, payload)
    return buffer.getvalue()


def manifest_for(archive: bytes, **changes: Any) -> ManagedRuntimeManifest:
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        infos = opened.infolist()
    values: dict[str, Any] = {
        "profile_component": MANAGED_PROFILE_COMPONENT,
        "source_url": SOURCE_URL,
        "release_page_url": "https://github.com/GyanD/codexffmpeg/releases/tag/fixture",
        "source_label": "fixture",
        "license_name": "GPL-3.0-or-later",
        "archive_bytes": len(archive),
        "archive_sha256": sha(archive),
        "member_count": len(infos),
        "expanded_bytes": sum(info.file_size for info in infos),
        "top_directory": TOP,
        "retained": (
            RetainedMember("bin/ffmpeg.exe", "bin/ffmpeg.exe", len(FFMPEG), sha(FFMPEG), True),
            RetainedMember("bin/ffprobe.exe", "bin/ffprobe.exe", len(FFPROBE), sha(FFPROBE), True),
            RetainedMember("LICENSE", "LICENSE.txt", len(LICENSE), sha(LICENSE), False),
            RetainedMember("README.txt", "README.txt", len(README), sha(README), False),
        ),
    }
    values.update(changes)
    return ManagedRuntimeManifest(**values)


def infos_for(archive: bytes) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        return opened.infolist()


class BytesDownloader:
    def __init__(
        self,
        payload: bytes,
        *,
        chunk: int = 32 * 1024,
        after_chunk: Callable[[int], None] | None = None,
        error: DownloadError | None = None,
    ) -> None:
        self.payload = payload
        self.chunk = chunk
        self.after_chunk = after_chunk
        self.error = error
        self.urls: list[str] = []
        self.aborted = 0

    def download(
        self,
        url: str,
        *,
        expected_bytes: int,
        sink: Callable[[bytes], None],
        cancelled: threading.Event,
        progress: Callable[[int], None],
    ) -> None:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        done = 0
        for start in range(0, len(self.payload), self.chunk):
            if cancelled.is_set():
                raise DownloadError("cancelled")
            piece = self.payload[start : start + self.chunk]
            sink(piece)
            done += len(piece)
            progress(done)
            if self.after_chunk is not None:
                self.after_chunk(done)

    def abort(self) -> None:
        self.aborted += 1


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int]] = []

    def __call__(self, phase: str, done: int, total: int) -> None:
        self.events.append((phase, done, total))

    def phases(self) -> list[str]:
        ordered: list[str] = []
        for phase, _done, _total in self.events:
            if not ordered or ordered[-1] != phase:
                ordered.append(phase)
        return ordered


def installer(
    tmp_path: Path,
    archive: bytes,
    *,
    manifest: ManagedRuntimeManifest | None = None,
    downloader: BytesDownloader | None = None,
    cancelled: threading.Event | None = None,
    disk: object = PLENTY,
    token: str = JOB,
) -> tuple[ManagedRuntimeInstaller, Any, BytesDownloader, Recorder]:
    layout = private_layout(tmp_path / "private")
    chosen = downloader or BytesDownloader(archive)
    recorder = Recorder()
    run = ManagedRuntimeInstaller(
        layout,
        manifest=manifest or manifest_for(archive),
        downloader=chosen,
        job_token=token,
        cancelled=cancelled or threading.Event(),
        progress=recorder,
        disk_usage=lambda _path: disk,
    )
    return run, layout, chosen, recorder


def tree_files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


# ---------------------------------------------------------------------------------------------
# AC31-01/05: fixed manifest


def test_the_manifest_pins_the_verified_release_and_the_qualified_pair() -> None:
    manifest = managed_runtime_manifest()
    capability = qualified_ffmpeg_capability()
    executables = {member.published_name: member for member in manifest.retained}

    assert manifest.profile_component == MANAGED_PROFILE_COMPONENT
    assert admit_source_url(manifest.source_url).host == INITIAL_HOST
    assert manifest.archive_bytes == 246_558_061
    assert manifest.member_count == 49
    assert manifest.expanded_bytes == 680_107_427
    assert executables["bin/ffmpeg.exe"].sha256 == capability.ffmpeg_sha256.casefold()
    assert executables["bin/ffprobe.exe"].sha256 == capability.ffprobe_sha256.casefold()
    assert {member.published_name for member in manifest.retained} == {
        "bin/ffmpeg.exe",
        "bin/ffprobe.exe",
        "LICENSE.txt",
        "README.txt",
    }
    assert manifest.license_name == capability.license_expression


# ---------------------------------------------------------------------------------------------
# AC31-02: transport


class FakeSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.shutdowns = 0

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def shutdown(self, _how: int) -> None:
        self.shutdowns += 1


class FakeResponse:
    def __init__(
        self,
        status: int,
        headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
        read_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.msg = email.message.Message()
        for name, value in headers:
            self.msg[name] = value
        self._body = body
        self._offset = 0
        self._read_error = read_error

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.msg.get(name, default)

    def read(self, amount: int) -> bytes:
        if self._read_error is not None and self._offset >= len(self._body) // 2:
            raise self._read_error
        piece = self._body[self._offset : self._offset + min(amount, 7919)]
        self._offset += len(piece)
        return piece


class FakeConnection:
    def __init__(self, host: str, script: FakeNetwork) -> None:
        self.host = host
        self.script = script
        self.sock = FakeSocket()
        self.headers: dict[str, str] = {}
        self.target = ""
        self.closed = False

    def connect(self) -> None:
        error = self.script.connect_errors.get(self.host)
        if error is not None:
            raise error

    def putrequest(self, method: str, target: str, skip_accept_encoding: bool = False) -> None:
        assert method == "GET"
        assert skip_accept_encoding
        self.target = target

    def putheader(self, name: str, value: str) -> None:
        self.headers[name] = value

    def endheaders(self) -> None:
        self.script.requests.append((self.host, self.target, dict(self.headers)))

    def getresponse(self) -> FakeResponse:
        return self.script.responses[self.host].pop(0)

    def close(self) -> None:
        self.closed = True


class FakeNetwork:
    def __init__(self) -> None:
        self.responses: dict[str, list[FakeResponse]] = {}
        self.connect_errors: dict[str, BaseException] = {}
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.connections: list[FakeConnection] = []

    def factory(self, host: str, port: int, timeout: float) -> FakeConnection:
        assert port == 443
        assert timeout > 0
        connection = FakeConnection(host, self)
        self.connections.append(connection)
        return connection


PAYLOAD = bytes(range(256)) * 400


def fetch(
    network: FakeNetwork,
    *,
    expected: int = len(PAYLOAD),
    cancelled: threading.Event | None = None,
    progress: Callable[[int], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> bytearray:
    received = bytearray()
    downloader = HttpsArchiveDownloader(
        connection_factory=network.factory, clock=clock or (lambda: 0.0)
    )
    downloader.download(
        SOURCE_URL,
        expected_bytes=expected,
        sink=received.extend,
        cancelled=cancelled or threading.Event(),
        progress=progress or (lambda _done: None),
    )
    return received


def redirect_to(location: str, status: int = 302) -> FakeResponse:
    return FakeResponse(status, (("Location", location),))


ASSET = "https://release-assets.githubusercontent.com/github-production-release-asset/1?sig=x"


def test_a_redirected_download_streams_exact_bytes_from_the_asset_host() -> None:
    network = FakeNetwork()
    network.responses = {
        INITIAL_HOST: [redirect_to(ASSET)],
        "release-assets.githubusercontent.com": [
            FakeResponse(200, (("Content-Length", str(len(PAYLOAD))),), PAYLOAD)
        ],
    }
    progress: list[int] = []

    received = fetch(network, progress=progress.append)

    assert bytes(received) == PAYLOAD
    assert progress[-1] == len(PAYLOAD)
    assert [host for host, _target, _headers in network.requests] == [
        INITIAL_HOST,
        "release-assets.githubusercontent.com",
    ]
    assert network.requests[1][1] == "/github-production-release-asset/1?sig=x"
    for _host, _target, headers in network.requests:
        assert headers["Accept-Encoding"] == "identity"
        assert {name.casefold() for name in headers} <= {
            "user-agent",
            "accept",
            "accept-encoding",
            "connection",
        }
    assert all(connection.closed for connection in network.connections)
    assert all(
        connection.sock.timeouts == [download_module.READ_IDLE_TIMEOUT_SECONDS]
        for connection in network.connections
    )


@pytest.mark.parametrize(
    "location",
    [
        "http://release-assets.githubusercontent.com/a",
        "https://evil.example/a",
        "https://release-assets.githubusercontent.com.evil.example/a",
        "https://release-assets.githubusercontent.com:8443/a",
        "https://user@release-assets.githubusercontent.com/a",
        "/relative/asset",
        "https://release-assets.githubusercontent.com\\a",
        "https://release-assets.githubusercontent.com/a b",
        "https://" + "a" * 9000,
        "https://github.com/another/path",
        "ftp://objects.githubusercontent.com/a",
    ],
)
def test_redirects_outside_the_asset_allowlist_are_refused_without_contact(location: str) -> None:
    network = FakeNetwork()
    network.responses = {INITIAL_HOST: [redirect_to(location)]}

    with pytest.raises(DownloadError, match="^redirect_refused$"):
        fetch(network)

    assert [host for host, _target, _headers in network.requests] == [INITIAL_HOST]


def test_an_asset_host_may_not_redirect_again() -> None:
    network = FakeNetwork()
    network.responses = {
        INITIAL_HOST: [redirect_to(ASSET)],
        "release-assets.githubusercontent.com": [
            redirect_to("https://objects.githubusercontent.com/a", 307)
        ],
    }

    with pytest.raises(DownloadError, match="^redirect_refused$"):
        fetch(network)

    assert "objects.githubusercontent.com" not in {host for host, _t, _h in network.requests}
    assert admit_redirect(ASSET, from_host=INITIAL_HOST).host in ASSET_HOSTS
    with pytest.raises(DownloadError):
        admit_redirect(ASSET, from_host="release-assets.githubusercontent.com")


@pytest.mark.parametrize("headers", [(), (("Location", ASSET), ("Location", ASSET))])
def test_a_redirect_without_exactly_one_location_is_refused(
    headers: tuple[tuple[str, str], ...],
) -> None:
    network = FakeNetwork()
    network.responses = {INITIAL_HOST: [FakeResponse(302, headers)]}

    with pytest.raises(DownloadError, match="^redirect_refused$"):
        fetch(network)


@pytest.mark.parametrize(
    "url",
    [
        "https://objects.githubusercontent.com/a.zip",
        "http://github.com/a.zip",
        "https://github.com:444/a.zip",
    ],
)
def test_the_source_url_must_name_the_release_host(url: str) -> None:
    with pytest.raises(DownloadError, match="^redirect_refused$"):
        admit_source_url(url)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (FakeResponse(404), "source_unavailable"),
        (FakeResponse(410), "source_unavailable"),
        (FakeResponse(500), "download_failed"),
        (FakeResponse(403), "download_failed"),
        (FakeResponse(200, (("Content-Encoding", "gzip"),), PAYLOAD), "download_failed"),
        (FakeResponse(200, (("Content-Length", "12"),), PAYLOAD), "size_mismatch"),
        (FakeResponse(200, (("Content-Length", "-1"),), PAYLOAD), "download_failed"),
        (FakeResponse(200, (), PAYLOAD + b"x"), "size_mismatch"),
        (FakeResponse(200, (), PAYLOAD[:-1]), "size_mismatch"),
        (FakeResponse(200, (), PAYLOAD, read_error=TimeoutError()), "network_timeout"),
        (FakeResponse(200, (), PAYLOAD, read_error=ConnectionResetError()), "network_unavailable"),
    ],
)
def test_statuses_lengths_and_read_failures_have_distinct_codes(
    response: FakeResponse, code: str
) -> None:
    network = FakeNetwork()
    network.responses = {INITIAL_HOST: [response]}
    delivered = bytearray()

    with pytest.raises(DownloadError, match=f"^{code}$"):
        downloader = HttpsArchiveDownloader(connection_factory=network.factory, clock=lambda: 0)
        downloader.download(
            SOURCE_URL,
            expected_bytes=len(PAYLOAD),
            sink=delivered.extend,
            cancelled=threading.Event(),
            progress=lambda _done: None,
        )

    assert len(delivered) <= len(PAYLOAD)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ssl.SSLCertVerificationError("certificate verify failed"), "tls_failed"),
        (ConnectionRefusedError(), "network_unavailable"),
        (TimeoutError(), "network_timeout"),
    ],
)
def test_connection_failures_are_typed(error: BaseException, code: str) -> None:
    network = FakeNetwork()
    network.connect_errors[INITIAL_HOST] = error

    with pytest.raises(DownloadError, match=f"^{code}$"):
        fetch(network)


def test_cancellation_and_abort_stop_the_stream() -> None:
    network = FakeNetwork()
    network.responses = {INITIAL_HOST: [FakeResponse(200, (), PAYLOAD)]}
    cancelled = threading.Event()
    downloader = HttpsArchiveDownloader(connection_factory=network.factory, clock=lambda: 0)

    def progress(done: int) -> None:
        if done > 10_000:
            cancelled.set()
            downloader.abort()

    with pytest.raises(DownloadError, match="^cancelled$"):
        downloader.download(
            SOURCE_URL,
            expected_bytes=len(PAYLOAD),
            sink=lambda _chunk: None,
            cancelled=cancelled,
            progress=progress,
        )

    assert network.connections[0].sock.shutdowns == 1


@pytest.mark.parametrize("phase", ["opening", "connecting"])
def test_a_cancel_before_the_socket_exists_sends_no_request(phase: str) -> None:
    network = FakeNetwork()
    network.responses = {INITIAL_HOST: [FakeResponse(200, (), PAYLOAD)]}
    cancelled = threading.Event()
    downloader = HttpsArchiveDownloader(clock=lambda: 0)
    connects: list[str] = []

    def factory(host: str, port: int, timeout: float) -> FakeConnection:
        connection = network.factory(host, port, timeout)
        original = connection.connect

        def connect() -> None:
            connects.append(host)
            original()
            if phase == "connecting":
                cancelled.set()

        connection.connect = connect  # type: ignore[method-assign]
        if phase == "opening":
            # The cancel lands while the address is resolving: abort finds no socket.
            cancelled.set()
            downloader.abort()
        return connection

    downloader._connection_factory = factory

    with pytest.raises(DownloadError, match="^cancelled$"):
        downloader.download(
            SOURCE_URL,
            expected_bytes=len(PAYLOAD),
            sink=lambda _chunk: None,
            cancelled=cancelled,
            progress=lambda _done: None,
        )

    assert network.requests == []
    assert connects == ([] if phase == "opening" else [INITIAL_HOST])
    assert network.connections[0].closed


def test_the_total_deadline_ends_a_slow_download() -> None:
    network = FakeNetwork()
    network.responses = {INITIAL_HOST: [FakeResponse(200, (), PAYLOAD)]}
    now = [0.0]

    def clock() -> float:
        now[0] += 400.0
        return now[0]

    with pytest.raises(DownloadError, match="^download_timeout$"):
        fetch(network, clock=clock)


def test_a_sink_failure_is_not_reported_as_a_network_outcome() -> None:
    network = FakeNetwork()
    network.responses = {INITIAL_HOST: [FakeResponse(200, (), PAYLOAD)]}
    downloader = HttpsArchiveDownloader(connection_factory=network.factory, clock=lambda: 0)

    def sink(_chunk: bytes) -> None:
        raise InstallerError("insufficient_space")

    with pytest.raises(InstallerError, match="^insufficient_space$"):
        downloader.download(
            SOURCE_URL,
            expected_bytes=len(PAYLOAD),
            sink=sink,
            cancelled=threading.Event(),
            progress=lambda _done: None,
        )


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (PromptModelOutcomeId.EGRESS_REFUSED, "egress_refused"),
        (PromptModelOutcomeId.TIMEOUT, "network_timeout"),
        (PromptModelOutcomeId.DESTINATION_UNRESOLVED, "network_unavailable"),
    ],
)
def test_the_production_path_resolves_through_the_killable_global_address_resolver(
    monkeypatch: pytest.MonkeyPatch, outcome: PromptModelOutcomeId, code: str
) -> None:
    seen: list[tuple[str, bool]] = []

    def refuse(host: str, *, timeout_seconds: float, loopback_required: bool) -> str:
        seen.append((host, loopback_required))
        assert 0 < timeout_seconds <= download_module.DNS_TIMEOUT_SECONDS
        raise PromptModelTransportError(outcome, "")

    monkeypatch.setattr(download_module, "resolve_pinned_address_bounded", refuse)

    with pytest.raises(DownloadError, match=f"^{code}$"):
        HttpsArchiveDownloader().download(
            SOURCE_URL,
            expected_bytes=10,
            sink=lambda _chunk: None,
            cancelled=threading.Event(),
            progress=lambda _done: None,
        )

    assert seen == [(INITIAL_HOST, False)]


def test_the_connection_is_pinned_to_the_admitted_address_with_verifying_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[tuple[str, int]] = []

    def create_connection(address: tuple[str, int], *_args: object) -> object:
        connected.append(address)
        raise ConnectionRefusedError()

    monkeypatch.setattr(socket, "create_connection", create_connection)
    downloader = HttpsArchiveDownloader(address_resolver=lambda _host: "203.0.113.9")

    connection = downloader._open(INITIAL_HOST, deadline=10**9)

    assert connection.host == INITIAL_HOST
    assert connection._context.verify_mode is ssl.CERT_REQUIRED
    assert connection._context.check_hostname is True
    with pytest.raises(ConnectionRefusedError):
        connection.connect()
    assert connected == [("203.0.113.9", 443)]


# ---------------------------------------------------------------------------------------------
# AC31-03: archive validation


def rename_member(infos: list[zipfile.ZipInfo], old: str, new: str) -> list[zipfile.ZipInfo]:
    for info in infos:
        if info.filename == old:
            info.filename = new
    return infos


def test_the_synthetic_archive_is_admitted_with_exactly_the_retained_members() -> None:
    archive = build_archive()

    retained = validate_archive_members(infos_for(archive), manifest_for(archive))

    assert set(retained) == {"bin/ffmpeg.exe", "bin/ffprobe.exe", "LICENSE", "README.txt"}


@pytest.mark.parametrize(
    "name",
    [
        f"{TOP}../evil.txt",
        f"{TOP}doc/../../evil.txt",
        f"/{TOP}doc/evil.txt",
        "C:/evil.txt",
        f"{TOP}doc\\evil.txt",
        f"{TOP}doc/evil.txt:stream",
        f"{TOP}doc/con.txt",
        f"{TOP}doc/LPT1",
        f"{TOP}doc/evil.",
        f"{TOP}doc/evil ",
        f"{TOP}doc/ev\x01il",
        f"{TOP}doc//evil.txt",
        f"{TOP}doc/./evil.txt",
        f"{TOP}doc/ev?il",
        "other_top/evil.txt",
        f"{TOP}doc/" + "a" * 300,
    ],
)
def test_unsafe_member_names_anywhere_refuse_the_archive(name: str) -> None:
    archive = build_archive()
    infos = rename_member(infos_for(archive), f"{TOP}doc/manual.html", name)

    with pytest.raises(InstallerError, match="^archive_invalid$"):
        validate_archive_members(infos, manifest_for(archive))


def mutate(field: str, value: Any) -> Callable[[list[zipfile.ZipInfo]], list[zipfile.ZipInfo]]:
    def apply(infos: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
        for info in infos:
            if info.filename == f"{TOP}doc/manual.html":
                setattr(info, field, value)
        return infos

    return apply


def test_duplicates_case_collisions_and_file_directory_collisions_are_refused() -> None:
    archive = build_archive(extra=((f"{TOP}doc/Manual.HTML", b"x"),))
    with pytest.raises(InstallerError, match="^archive_invalid$"):
        validate_archive_members(infos_for(archive), manifest_for(archive))

    archive = build_archive(extra=((f"{TOP}doc", b"file shadowing a directory"),))
    with pytest.raises(InstallerError, match="^archive_invalid$"):
        validate_archive_members(infos_for(archive), manifest_for(archive))

    archive = build_archive()
    infos = infos_for(archive)
    infos = rename_member(infos, f"{TOP}doc/manual.html", f"{TOP}README.txt")
    with pytest.raises(InstallerError, match="^archive_invalid$"):
        validate_archive_members(infos, manifest_for(archive))


@pytest.mark.parametrize(
    "change",
    [
        mutate("external_attr", (0o120777 << 16)),
        mutate("flag_bits", 0x1),
        mutate("compress_type", zipfile.ZIP_BZIP2),
    ],
    ids=["symlink", "encrypted", "unsupported_compression"],
)
def test_links_encryption_and_unsupported_compression_are_refused(
    change: Callable[[list[zipfile.ZipInfo]], list[zipfile.ZipInfo]],
) -> None:
    archive = build_archive()

    with pytest.raises(InstallerError, match="^archive_invalid$"):
        validate_archive_members(change(infos_for(archive)), manifest_for(archive))


def test_a_directory_entry_with_content_is_refused() -> None:
    archive = build_archive()
    infos = infos_for(archive)
    for info in infos:
        if info.filename == f"{TOP}doc/":
            info.file_size = 5
    manifest = manifest_for(archive, expanded_bytes=manifest_for(archive).expanded_bytes + 5)

    with pytest.raises(InstallerError, match="^archive_invalid$"):
        validate_archive_members(infos, manifest)


def test_count_and_expanded_size_must_match_and_stay_bounded() -> None:
    archive = build_archive()
    manifest = manifest_for(archive)

    with pytest.raises(InstallerError, match="^archive_invalid$"):
        validate_archive_members(infos_for(archive), manifest_for(archive, member_count=8))
    with pytest.raises(InstallerError, match="^archive_invalid$"):
        validate_archive_members(
            infos_for(archive), manifest_for(archive, expanded_bytes=manifest.expanded_bytes - 1)
        )
    oversized = infos_for(archive)
    oversized[6].file_size = installer_module.MAX_EXPANDED_BYTES + 1
    with pytest.raises(InstallerError, match="^archive_invalid$"):
        validate_archive_members(oversized, manifest)
    many = build_archive(extra=tuple((f"{TOP}doc/{index}.html", b"x") for index in range(64)))
    with pytest.raises(InstallerError, match="^archive_invalid$"):
        validate_archive_members(infos_for(many), manifest_for(many))


def test_retained_members_must_be_present_once_with_the_manifest_size() -> None:
    archive = build_archive()
    manifest = manifest_for(archive)
    wrong_size = manifest_for(
        archive,
        retained=(
            *manifest.retained[:3],
            RetainedMember("README.txt", "README.txt", len(README) + 1, sha(README), False),
        ),
    )
    missing = manifest_for(
        archive,
        retained=(
            *manifest.retained,
            RetainedMember("bin/absent.exe", "bin/absent.exe", 1, sha(b"x"), False),
        ),
    )

    with pytest.raises(InstallerError, match="^archive_invalid$"):
        validate_archive_members(infos_for(archive), wrong_size)
    with pytest.raises(InstallerError, match="^archive_invalid$"):
        validate_archive_members(infos_for(archive), missing)


# ---------------------------------------------------------------------------------------------
# AC31-03/04/06: installer


@windows_only
def test_install_publishes_only_the_retained_members_and_cleans_staging(tmp_path: Path) -> None:
    archive = build_archive()
    run, layout, downloader, recorder = installer(tmp_path, archive)

    run.run()

    target = layout.runtime_root / MANAGED_PROFILE_COMPONENT
    assert tree_files(target) == [
        "LICENSE.txt",
        "README.txt",
        "bin/ffmpeg.exe",
        "bin/ffprobe.exe",
        "source.json",
    ]
    assert (target / "bin" / "ffmpeg.exe").read_bytes() == FFMPEG
    assert (target / "LICENSE.txt").read_bytes() == LICENSE
    notice = json.loads((target / "source.json").read_text(encoding="utf-8"))
    assert notice["profile"] == MANAGED_PROFILE_COMPONENT
    assert "\\" not in json.dumps(notice)
    assert downloader.urls == [SOURCE_URL]
    staging = layout.media_runtime / "staging"
    assert sorted(path.name for path in staging.iterdir()) == ["install.lock"]
    assert recorder.phases() == ["downloading", "extracting", "verifying", "publishing"]
    assert not layout.config_file.exists()
    assert not layout.scratch_root.exists()


@windows_only
def test_a_digest_mismatch_publishes_nothing_and_cleans_staging(tmp_path: Path) -> None:
    archive = build_archive()
    flipped = bytearray(archive)
    flipped[len(archive) // 2] ^= 0x01
    tampered = bytes(flipped)
    run, layout, _downloader, recorder = installer(
        tmp_path, archive, downloader=BytesDownloader(tampered)
    )

    with pytest.raises(InstallerError, match="^digest_mismatch$"):
        run.run()

    assert not (layout.runtime_root / MANAGED_PROFILE_COMPONENT).exists()
    assert sorted(path.name for path in (layout.media_runtime / "staging").iterdir()) == [
        "install.lock"
    ]
    assert "extracting" not in recorder.phases()


@windows_only
def test_an_unsafe_member_refuses_before_any_extraction(tmp_path: Path) -> None:
    archive = build_archive(extra=((f"{TOP}../escape.txt", b"escape"),))
    run, layout, _downloader, _recorder = installer(tmp_path, archive)

    with pytest.raises(InstallerError, match="^archive_invalid$"):
        run.run()

    assert not list(tmp_path.rglob("escape.txt"))
    assert not list(tmp_path.rglob("ffmpeg.exe"))
    assert not (layout.runtime_root / MANAGED_PROFILE_COMPONENT).exists()


@windows_only
def test_a_retained_member_with_other_content_is_refused(tmp_path: Path) -> None:
    archive = build_archive()
    manifest = manifest_for(archive)
    lying = manifest_for(
        archive,
        retained=(
            *manifest.retained[:2],
            RetainedMember("LICENSE", "LICENSE.txt", len(LICENSE), sha(b"other"), False),
            manifest.retained[3],
        ),
    )
    run, layout, _downloader, _recorder = installer(tmp_path, archive, manifest=lying)

    with pytest.raises(InstallerError, match="^archive_invalid$"):
        run.run()

    assert not (layout.runtime_root / MANAGED_PROFILE_COMPONENT).exists()
    assert not list((layout.media_runtime / "staging").rglob("*.exe"))


@windows_only
def test_insufficient_space_refuses_before_download(tmp_path: Path) -> None:
    archive = build_archive()
    run, _layout, downloader, _recorder = installer(
        tmp_path, archive, disk=SimpleNamespace(free=1024)
    )

    with pytest.raises(InstallerError, match="^insufficient_space$"):
        run.run()

    assert downloader.urls == []


@windows_only
def test_cancellation_during_download_leaves_nothing_behind(tmp_path: Path) -> None:
    archive = build_archive()
    cancelled = threading.Event()
    downloader = BytesDownloader(archive, after_chunk=lambda _done: cancelled.set())
    run, layout, _downloader, _recorder = installer(
        tmp_path, archive, downloader=downloader, cancelled=cancelled
    )

    with pytest.raises(InstallerError, match="^cancelled$"):
        run.run()

    assert not (layout.runtime_root / MANAGED_PROFILE_COMPONENT).exists()
    assert sorted(path.name for path in (layout.media_runtime / "staging").iterdir()) == [
        "install.lock"
    ]


@windows_only
def test_a_download_failure_keeps_its_code(tmp_path: Path) -> None:
    archive = build_archive()
    run, _layout, _downloader, _recorder = installer(
        tmp_path, archive, downloader=BytesDownloader(archive, error=DownloadError("tls_failed"))
    )

    with pytest.raises(InstallerError, match="^tls_failed$"):
        run.run()


def broken_runtime(layout: Any) -> Path:
    target: Path = layout.runtime_root / MANAGED_PROFILE_COMPONENT
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "ffmpeg.exe").write_bytes(b"stale")
    (target / "bin" / "ffprobe.exe").write_bytes(b"stale")
    return target


@windows_only
def test_a_broken_runtime_is_retired_and_replaced(tmp_path: Path) -> None:
    archive = build_archive()
    run, layout, _downloader, _recorder = installer(tmp_path, archive)
    target = broken_runtime(layout)

    run.run()

    assert (target / "bin" / "ffmpeg.exe").read_bytes() == FFMPEG
    assert sorted(path.name for path in (layout.media_runtime / "staging").iterdir()) == [
        "install.lock"
    ]


@windows_only
def test_an_already_valid_runtime_is_kept(tmp_path: Path) -> None:
    archive = build_archive()
    run, layout, _downloader, _recorder = installer(tmp_path, archive)
    target = layout.runtime_root / MANAGED_PROFILE_COMPONENT
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "ffmpeg.exe").write_bytes(FFMPEG)
    (target / "bin" / "ffprobe.exe").write_bytes(FFPROBE)
    (target / "keep.txt").write_bytes(b"user note")

    run.run()

    assert (target / "keep.txt").read_bytes() == b"user note"
    assert not (target / "source.json").exists()


@windows_only
def test_a_linked_runtime_target_is_refused(tmp_path: Path) -> None:
    import _winapi

    archive = build_archive()
    run, layout, _downloader, _recorder = installer(tmp_path, archive)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_bytes(b"not ours")
    layout.runtime_root.mkdir(parents=True)
    _winapi.CreateJunction(str(elsewhere), str(layout.runtime_root / MANAGED_PROFILE_COMPONENT))

    with pytest.raises(InstallerError, match="^private_root_invalid$"):
        run.run()

    assert tree_files(elsewhere) == ["precious.txt"]


@windows_only
def test_a_linked_staging_directory_is_refused_before_any_download(tmp_path: Path) -> None:
    import _winapi

    archive = build_archive()
    run, layout, downloader, _recorder = installer(tmp_path, archive)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_bytes(b"not ours")
    layout.media_runtime.mkdir(parents=True)
    _winapi.CreateJunction(str(elsewhere), str(layout.media_runtime / "staging"))

    with pytest.raises(InstallerError, match="^private_root_invalid$"):
        run.run()

    assert downloader.urls == []
    assert tree_files(elsewhere) == ["precious.txt"]
    assert not (layout.runtime_root / MANAGED_PROFILE_COMPONENT).exists()


@windows_only
def test_a_failed_publication_restores_the_retired_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = build_archive()
    run, layout, _downloader, _recorder = installer(tmp_path, archive)
    target = broken_runtime(layout)
    real_rename = os.rename
    calls: list[tuple[str, str]] = []

    def rename(source: object, destination: object) -> None:
        calls.append((Path(str(source)).name, Path(str(destination)).name))
        if Path(str(source)).name == "tree":
            raise PermissionError(13, "denied", None, 5)
        real_rename(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", rename)

    with pytest.raises(InstallerError, match="^runtime_busy$"):
        run.run()

    assert calls == [
        (MANAGED_PROFILE_COMPONENT, "retired"),
        ("tree", MANAGED_PROFILE_COMPONENT),
        ("retired", MANAGED_PROFILE_COMPONENT),
    ]
    assert (target / "bin" / "ffmpeg.exe").read_bytes() == b"stale"
    assert not list((layout.media_runtime / "staging").rglob("retired"))


@windows_only
def test_a_failed_restore_keeps_the_retired_tree_through_later_sweeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = build_archive()
    run, layout, _downloader, _recorder = installer(tmp_path, archive)
    broken_runtime(layout)
    real_rename = os.rename

    def rename(source: object, destination: object) -> None:
        if Path(str(source)).name in {"tree", "retired"}:
            raise OSError(22, "failure", None, 87)
        real_rename(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(InstallerError, match="^publication_failed$"):
        run.run()
    monkeypatch.setattr(os, "rename", real_rename)
    retired = layout.media_runtime / "staging" / JOB / "retired" / "bin" / "ffmpeg.exe"
    assert retired.read_bytes() == b"stale"

    second, _layout, _downloader, _recorder = installer(tmp_path, archive, token=OTHER_JOB)
    second.run()

    assert retired.read_bytes() == b"stale"
    assert (
        layout.runtime_root / MANAGED_PROFILE_COMPONENT / "bin" / "ffmpeg.exe"
    ).read_bytes() == FFMPEG


@windows_only
def test_a_runtime_file_in_use_makes_publication_busy_and_keeps_the_old_tree(
    tmp_path: Path,
) -> None:
    archive = build_archive()
    run, layout, _downloader, _recorder = installer(tmp_path, archive)
    target = broken_runtime(layout)
    descriptor = windows_open_read_pin(target / "bin" / "ffmpeg.exe")
    try:
        with pytest.raises(InstallerError, match="^runtime_busy$"):
            run.run()
    finally:
        os.close(descriptor)

    assert (target / "bin" / "ffmpeg.exe").read_bytes() == b"stale"
    assert sorted(path.name for path in (layout.media_runtime / "staging").iterdir()) == [
        "install.lock"
    ]


@windows_only
def test_an_install_lock_held_by_another_installer_is_busy(tmp_path: Path) -> None:
    import msvcrt

    archive = build_archive()
    run, layout, downloader, _recorder = installer(tmp_path, archive)
    staging = layout.media_runtime / "staging"
    staging.mkdir(parents=True)
    holder = os.open(staging / "install.lock", os.O_RDWR | os.O_CREAT)
    msvcrt.locking(holder, msvcrt.LK_NBLCK, 1)
    try:
        with pytest.raises(InstallerError, match="^setup_busy$"):
            run.run()
    finally:
        msvcrt.locking(holder, msvcrt.LK_UNLCK, 1)
        os.close(holder)

    assert downloader.urls == []
    run.run()
    assert (layout.runtime_root / MANAGED_PROFILE_COMPONENT / "bin" / "ffprobe.exe").exists()


@windows_only
def test_the_sweep_removes_only_named_leftovers_of_earlier_jobs(tmp_path: Path) -> None:
    archive = build_archive()
    run, layout, _downloader, _recorder = installer(tmp_path, archive)
    staging = layout.media_runtime / "staging"
    leftover = staging / OTHER_JOB
    (leftover / "tree" / "bin").mkdir(parents=True)
    (leftover / "archive.zip.part").write_bytes(b"partial")
    (leftover / "tree" / "bin" / "ffmpeg.exe").write_bytes(b"partial")
    (leftover / "tree" / "unknown.dat").write_bytes(b"not named")
    unrelated = staging / "not-a-job"
    unrelated.mkdir()
    (unrelated / "archive.zip.part").write_bytes(b"left alone")

    run.run()

    assert tree_files(leftover) == ["tree/unknown.dat"]
    assert (unrelated / "archive.zip.part").read_bytes() == b"left alone"


def test_cleanup_is_not_recursive_and_ignores_missing_entries(tmp_path: Path) -> None:
    manifest = manifest_for(build_archive())
    job = tmp_path / JOB
    (job / "tree" / "bin" / "nested").mkdir(parents=True)
    (job / "tree" / "bin" / "nested" / "ffmpeg.exe").write_bytes(b"deeper than named")

    cleanup_job_directory(job, manifest, include_retired=True)

    assert (job / "tree" / "bin" / "nested" / "ffmpeg.exe").exists()


# The installer borrows segment_artifact_store's filesystem primitives. These rows pin the
# guarantees it relies on at its own seam, so a change made there for another caller cannot
# weaken them silently.


@windows_only
def test_cleanup_never_deletes_through_a_junctioned_tree(tmp_path: Path) -> None:
    import _winapi

    manifest = manifest_for(build_archive())
    elsewhere = tmp_path / "elsewhere"
    for name in (*(member.published_name for member in manifest.retained), "source.json"):
        (elsewhere / name).parent.mkdir(parents=True, exist_ok=True)
        (elsewhere / name).write_bytes(b"not ours")
    job = tmp_path / JOB
    job.mkdir()
    _winapi.CreateJunction(str(elsewhere), str(job / "tree"))

    cleanup_job_directory(job, manifest, include_retired=True)

    assert tree_files(elsewhere) == [
        "LICENSE.txt",
        "README.txt",
        "bin/ffmpeg.exe",
        "bin/ffprobe.exe",
        "source.json",
    ]


@windows_only
def test_the_sweep_leaves_a_junctioned_job_directory_untouched(tmp_path: Path) -> None:
    import _winapi

    archive = build_archive()
    run, layout, _downloader, _recorder = installer(tmp_path, archive)
    staging = layout.media_runtime / "staging"
    staging.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "archive.zip.part").write_bytes(b"not ours")
    _winapi.CreateJunction(str(elsewhere), str(staging / OTHER_JOB))

    run.run()

    assert (elsewhere / "archive.zip.part").read_bytes() == b"not ours"
    assert (layout.runtime_root / MANAGED_PROFILE_COMPONENT / "bin" / "ffmpeg.exe").exists()


@windows_only
def test_the_exclusive_writer_never_opens_an_existing_file(tmp_path: Path) -> None:
    existing = tmp_path / "archive.zip.part"
    existing.write_bytes(b"not ours")

    with pytest.raises(InstallerError, match="^write_failed$"):
        installer_module._NewFileWriter(existing, maximum_bytes=16, overflow_code="archive_invalid")

    assert existing.read_bytes() == b"not ours"


@pytest.mark.parametrize(
    ("errno", "winerror", "code"),
    [
        (28, 112, "insufficient_space"),
        (28, None, "insufficient_space"),
        (13, 5, "permission_denied"),
        (13, 32, "runtime_busy"),
        (13, None, "permission_denied"),
        (5, None, "write_failed"),
    ],
)
def test_os_failures_map_to_closed_codes(errno: int, winerror: int | None, code: str) -> None:
    error = OSError(errno, "injected failure")
    # IMPORTANT: POSIX ignores OSError's winerror constructor argument; inject Windows fixtures.
    if winerror is not None:
        error.winerror = winerror
    assert installer_module._os_failure(error).code == code


def test_the_installer_requires_a_job_token_shape(tmp_path: Path) -> None:
    archive = build_archive()
    with pytest.raises(ValueError, match="job_token"):
        ManagedRuntimeInstaller(
            private_layout(tmp_path),
            manifest=manifest_for(archive),
            downloader=BytesDownloader(archive),
            job_token=UNSAFE_TOKEN,
            cancelled=threading.Event(),
            progress=lambda *_args: None,
        )


# ---------------------------------------------------------------------------------------------
# AC31-06/08: setup service


LOCATED = MediaRuntimeResolution(
    ResolutionState.LOCATED,
    ResolutionReason.PAIR_ADMITTED,
    SourceKind.MANAGED,
    ffmpeg_path=Path("C:/private/bin/ffmpeg.exe"),
    ffprobe_path=Path("C:/private/bin/ffprobe.exe"),
    scratch_root=Path("C:/private/scratch"),
    ffmpeg_identity=(1, 2, 3, 4),
    ffprobe_identity=(1, 2, 3, 5),
)
MISSING = MediaRuntimeResolution(
    ResolutionState.UNAVAILABLE, ResolutionReason.SUPPORTED_PAIR_MISSING
)
EXPLICIT = MediaRuntimeResolution(
    ResolutionState.INVALID_CONFIG,
    ResolutionReason.OVERRIDE_UNSUPPORTED_PAIR,
    SourceKind.EXPLICIT_OVERRIDE,
)
LOCAL = MediaRuntimeResolution(
    ResolutionState.INVALID_CONFIG,
    ResolutionReason.LOCAL_SELECTION_INVALID_PATH,
    SourceKind.LOCAL_SELECTION,
)
CORRUPT = MediaRuntimeResolution(
    ResolutionState.INVALID_CONFIG, ResolutionReason.CONFIG_CORRUPT, SourceKind.LOCAL_SELECTION
)
TIMEOUT = MediaRuntimeResolution(ResolutionState.UNAVAILABLE, ResolutionReason.DISCOVERY_TIMEOUT)


class FakeResolver:
    def __init__(self, *results: MediaRuntimeResolution) -> None:
        self.results = list(results)
        self.rescans = 0
        self.invalidations = 0
        self.config: MediaRuntimeConfig | None = None
        self.config_error: MediaRuntimeConfigError | None = None
        self.write_error: MediaRuntimeConfigError | None = None
        self.writes: list[tuple[int, MediaRuntimeSelection, str | None]] = []
        self.layout = private_layout(Path("C:/private"))

    def resolve(self, *, rescan: bool = False) -> MediaRuntimeResolution:
        self.rescans += int(rescan)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def invalidate(self) -> None:
        self.invalidations += 1

    def read_config(self) -> MediaRuntimeConfig | None:
        if self.config_error is not None:
            raise self.config_error
        return self.config

    def write_config(
        self,
        *,
        expected_revision: int,
        selection: MediaRuntimeSelection,
        directory: str | None = None,
    ) -> MediaRuntimeConfig:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append((expected_revision, selection, directory))
        self.config = MediaRuntimeConfig(expected_revision + 1, selection, directory)
        return self.config

    def private_layout(self) -> Any:
        return self.layout


class FakeInstallers:
    def __init__(self, action: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.action = action

    def __call__(self, layout: object, **kwargs: Any) -> SimpleNamespace:
        self.calls.append({"layout": layout, **kwargs})

        def run() -> None:
            if self.action is not None:
                self.action(kwargs)

        return SimpleNamespace(run=run)


class Deferred:
    def __init__(self) -> None:
        self.targets: list[Callable[[], None]] = []

    def __call__(self, target: Callable[[], None]) -> None:
        self.targets.append(target)

    def run_all(self) -> None:
        while self.targets:
            self.targets.pop(0)()


def service(
    resolver: FakeResolver,
    *,
    installers: FakeInstallers | None = None,
    starter: Callable[[Callable[[], None]], None] | None = None,
    downloads: list[object] | None = None,
    ids: list[str] | None = None,
) -> MediaRuntimeSetupService:
    made = downloads if downloads is not None else []
    identities = ids if ids is not None else [JOB, OTHER_JOB, "a" * 32]

    def downloader() -> BytesDownloader:
        instance = BytesDownloader(b"")
        made.append(instance)
        return instance

    return MediaRuntimeSetupService(
        resolver=resolver,  # type: ignore[arg-type]
        downloader_factory=downloader,
        installer_factory=installers or FakeInstallers(),
        manifest_provider=lambda: manifest_for(build_archive()),
        thread_starter=starter or (lambda target: target()),
        job_id_factory=lambda: identities.pop(0),
        parked_scanner=lambda _layout, _manifest: (),
    )


def test_status_is_locator_free_and_offers_install_only_when_missing() -> None:
    downloads: list[object] = []
    status = service(FakeResolver(MISSING), downloads=downloads).status()

    assert set(status) == {
        "schema",
        "resolution",
        "config",
        "setup",
        "install",
        "features",
        "recovery",
        "actions",
    }
    assert status["schema"] == STATUS_SCHEMA == "h3.context.media_runtime_status.v3"
    assert status["recovery"] is None
    assert status["features"] == {
        feature: {"state": "setup_required", "reason": "supported_pair_missing"}
        for feature in ("import", "preview", "derivatives", "assembly", "render")
    }
    assert status["actions"] == ["install_supported", "rescan", "use_local_directory"]
    assert status["config"] == {"revision": 0, "selection": "auto"}
    assert status["setup"] is None
    install = cast(dict[str, object], status["install"])
    assert set(install) == {
        "profile",
        "source_label",
        "release_page",
        "license",
        "approximate_bytes",
    }
    encoded = json.dumps(status)
    assert "C:" not in encoded and "\\" not in encoded and ".zip" not in encoded
    assert downloads == []

    assert service(FakeResolver(LOCATED)).status()["actions"] == ["rescan", "use_local_directory"]
    assert service(FakeResolver(EXPLICIT)).status()["actions"] == ["rescan"]
    local = FakeResolver(LOCAL)
    local.config = MediaRuntimeConfig(3, MediaRuntimeSelection.LOCAL, "C:\\tools")
    assert service(local).status()["actions"] == ["rescan", "use_local_directory", "restore_auto"]
    unreadable = FakeResolver(CORRUPT)
    unreadable.config_error = MediaRuntimeConfigError("config_corrupt")
    corrupt_status = service(unreadable).status()
    assert corrupt_status["config"] is None
    assert corrupt_status["actions"] == ["rescan"]


def test_supported_tools_already_available_install_nothing() -> None:
    installers = FakeInstallers()
    downloads: list[object] = []
    setup = service(FakeResolver(LOCATED), installers=installers, downloads=downloads)

    job = setup.start_install()

    assert setup.job(cast(str, job["job_id"]))["reason"] == "already_available"
    assert setup.job(cast(str, job["job_id"]))["state"] == "succeeded"
    assert installers.calls == []
    assert downloads == []


@pytest.mark.parametrize(
    ("resolution", "reason"),
    [
        (EXPLICIT, "advanced_override_active"),
        (LOCAL, "local_selection_active"),
        (CORRUPT, "config_invalid"),
        (TIMEOUT, "discovery_timeout"),
    ],
)
def test_install_refuses_when_it_cannot_change_the_result(
    resolution: MediaRuntimeResolution, reason: str
) -> None:
    installers = FakeInstallers()
    downloads: list[object] = []
    setup = service(FakeResolver(resolution), installers=installers, downloads=downloads)

    job = setup.job(cast(str, setup.start_install()["job_id"]))

    assert (job["state"], job["reason"]) == ("failed", reason)
    assert installers.calls == []
    assert downloads == []


def test_a_successful_install_activates_through_a_rescan() -> None:
    resolver = FakeResolver(MISSING, LOCATED)
    phases: list[str] = []

    def act(kwargs: dict[str, Any]) -> None:
        kwargs["progress"]("downloading", 10, 20)
        phases.append("ran")

    installers = FakeInstallers(act)
    setup = service(resolver, installers=installers)

    job = setup.job(cast(str, setup.start_install()["job_id"]))

    assert (job["state"], job["reason"], job["phase"]) == ("succeeded", "installed", "done")
    assert phases == ["ran"]
    assert installers.calls[0]["job_token"] == JOB
    assert resolver.invalidations == 1
    assert resolver.rescans == 2


def test_an_install_that_does_not_locate_the_pair_fails_verification() -> None:
    setup = service(FakeResolver(MISSING, MISSING), installers=FakeInstallers())

    job = setup.job(cast(str, setup.start_install()["job_id"]))

    assert (job["state"], job["reason"]) == ("failed", "verification_failed")


@pytest.mark.parametrize(
    ("error", "state", "reason"),
    [
        (InstallerError("digest_mismatch"), "failed", "digest_mismatch"),
        (InstallerError("cancelled"), "cancelled", "cancelled"),
        (RuntimeError("boom C:\\private"), "failed", "internal_failure"),
    ],
)
def test_installer_failures_reach_a_terminal_content_free_state(
    error: BaseException, state: str, reason: str
) -> None:
    def fail(_kwargs: dict[str, Any]) -> None:
        raise error

    setup = service(FakeResolver(MISSING), installers=FakeInstallers(fail))

    job = setup.job(cast(str, setup.start_install()["job_id"]))

    assert (job["state"], job["reason"]) == (state, reason)
    assert "private" not in json.dumps(job)
    assert setup.rescan()["schema"] == STATUS_SCHEMA


def test_duplicate_installs_share_one_job_and_actions_are_busy_meanwhile() -> None:
    deferred = Deferred()
    resolver = FakeResolver(MISSING)
    setup = service(resolver, starter=deferred)

    first = setup.start_install()
    second = setup.start_install()

    assert first["job_id"] == second["job_id"] == JOB
    assert first["schema"] == JOB_SCHEMA
    assert setup.status()["actions"] == ["cancel_setup"]
    for action in (
        setup.rescan,
        lambda: setup.use_local_directory("C:\\tools", 0),
        lambda: setup.restore_auto(0),
    ):
        with pytest.raises(MediaRuntimeSetupError) as refused:
            action()
        assert (refused.value.code, refused.value.status) == ("setup_busy", 409)
    assert resolver.writes == []

    deferred.run_all()
    assert setup.job(JOB)["state"] == "failed"
    setup.use_local_directory("C:\\tools", 0)
    assert resolver.writes == [(0, MediaRuntimeSelection.LOCAL, "C:\\tools")]


def test_cancel_marks_the_running_job_and_aborts_its_download() -> None:
    started = threading.Event()
    release = threading.Event()
    downloads: list[object] = []

    def wait(kwargs: dict[str, Any]) -> None:
        started.set()
        assert release.wait(10)
        if kwargs["cancelled"].is_set():
            raise InstallerError("cancelled")

    threads: list[threading.Thread] = []

    def starter(target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target)
        threads.append(thread)
        thread.start()

    setup = service(
        FakeResolver(MISSING), installers=FakeInstallers(wait), starter=starter, downloads=downloads
    )
    job_id = cast(str, setup.start_install()["job_id"])
    assert started.wait(10)

    setup.cancel(job_id)
    release.set()
    threads[0].join(10)

    assert setup.job(job_id)["state"] == "cancelled"
    assert cast(BytesDownloader, downloads[0]).aborted == 1
    assert setup.cancel(job_id)["state"] == "cancelled"
    with pytest.raises(MediaRuntimeSetupError) as unknown:
        setup.cancel(OTHER_JOB)
    assert (unknown.value.code, unknown.value.status) == ("setup_job_not_found", 404)


def test_only_the_running_and_most_recent_jobs_are_retained() -> None:
    setup = service(FakeResolver(TIMEOUT))

    first = cast(str, setup.start_install()["job_id"])
    second = cast(str, setup.start_install()["job_id"])

    assert first != second
    assert setup.job(second)["reason"] == "discovery_timeout"
    with pytest.raises(MediaRuntimeSetupError):
        setup.job(first)


def test_a_thread_start_failure_releases_the_action_slot() -> None:
    def refuse(_target: Callable[[], None]) -> None:
        raise RuntimeError("no threads")

    resolver = FakeResolver(MISSING)
    setup = service(resolver, starter=refuse)

    with pytest.raises(RuntimeError):
        setup.start_install()

    assert setup.job(JOB)["reason"] == "internal_failure"
    setup.restore_auto(0)
    assert resolver.writes == [(0, MediaRuntimeSelection.AUTO, None)]


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("config_conflict", 409),
        ("selection_invalid_path", 422),
        ("selection_unsupported_pair", 422),
        ("unsupported_host", 503),
        ("discovery_timeout", 503),
        ("config_write_failed", 500),
    ],
)
def test_config_action_errors_map_to_statuses(code: str, status: int) -> None:
    resolver = FakeResolver(MISSING)
    resolver.write_error = MediaRuntimeConfigError(code)
    setup = service(resolver)

    with pytest.raises(MediaRuntimeSetupError) as refused:
        setup.use_local_directory("C:\\tools", 2)

    assert (refused.value.code, refused.value.status) == (code, status)
    resolver.write_error = None
    assert setup.restore_auto(2)["schema"] == STATUS_SCHEMA


# ---------------------------------------------------------------------------------------------
# AC31-07: wire


def request(action: str, **fields: object) -> bytes:
    return json.dumps({"schema": REQUEST_SCHEMA, "action": action, **fields}).encode("utf-8")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"{",
        b"\xff",
        b'{"schema":"a","schema":"b"}',
        b'{"schema":NaN}',
    ],
)
def test_the_decoder_refuses_malformed_or_ambiguous_json(raw: bytes) -> None:
    with pytest.raises(MediaRuntimeSetupError, match="^invalid_request$"):
        decode_setup_request(raw)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": "wrong", "action": "rescan"},
        {"schema": REQUEST_SCHEMA, "action": "reinstall"},
        {"schema": REQUEST_SCHEMA, "action": "rescan", "url": "https://evil.example"},
        {"schema": REQUEST_SCHEMA, "action": "install_supported", "profile": "latest"},
        {"schema": REQUEST_SCHEMA, "action": "cancel_setup"},
        {"schema": REQUEST_SCHEMA, "action": "cancel_setup", "job_id": "../x"},
        {"schema": REQUEST_SCHEMA, "action": "use_local_directory", "directory": "C:\\t"},
        {
            "schema": REQUEST_SCHEMA,
            "action": "use_local_directory",
            "directory": "",
            "expected_revision": 0,
        },
        {
            "schema": REQUEST_SCHEMA,
            "action": "use_local_directory",
            "directory": "C:\\" + "a" * 5000,
            "expected_revision": 0,
        },
        {"schema": REQUEST_SCHEMA, "action": "restore_auto", "expected_revision": True},
        {"schema": REQUEST_SCHEMA, "action": "restore_auto", "expected_revision": -1},
        {"schema": REQUEST_SCHEMA, "action": "restore_auto", "expected_revision": 1.0},
        {"schema": REQUEST_SCHEMA, "action": ["rescan"]},
    ],
)
def test_dispatch_refuses_anything_outside_the_closed_request(payload: dict[str, object]) -> None:
    setup = service(FakeResolver(MISSING), starter=Deferred())

    with pytest.raises(MediaRuntimeSetupError, match="^invalid_request$"):
        dispatch_setup_request(setup, payload)


def test_dispatch_statuses_follow_the_action() -> None:
    resolver = FakeResolver(MISSING)
    setup = service(resolver, starter=Deferred())

    status, job = dispatch_setup_request(setup, decode_setup_request(request("install_supported")))
    assert (status, job["schema"]) == (202, JOB_SCHEMA)
    status, cancelled = dispatch_setup_request(
        setup, decode_setup_request(request("cancel_setup", job_id=JOB))
    )
    assert (status, cancelled["job_id"]) == (200, JOB)


# ---------------------------------------------------------------------------------------------
# AC31-07: routes


class _Routes(list[SimpleNamespace]):
    def _decorator(self, method: str, path: str) -> Callable[[Callable[..., Any]], Any]:
        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.append(SimpleNamespace(method=method, path=path, handler=handler))
            return handler

        return decorate

    def get(self, path: str) -> Callable[[Callable[..., Any]], Any]:
        return self._decorator("GET", path)

    def post(self, path: str) -> Callable[[Callable[..., Any]], Any]:
        return self._decorator("POST", path)


class _Headers:
    def __init__(self, values: dict[str, list[str]]) -> None:
        self._values = values

    def getall(self, name: str, default: list[str]) -> list[str]:
        return self._values.get(name, [LOOPBACK_HOST] if name == "Host" else default)


class _Content:
    def __init__(self, body: bytes) -> None:
        self._chunks = [body, b""]
        self.reads = 0

    async def read(self, _limit: int) -> bytes:
        self.reads += 1
        return self._chunks.pop(0)


def http_request(
    body: bytes = b"",
    *,
    origin: list[str] | None = None,
    site: list[str] | None = None,
    content_type: str = "application/json",
    job_id: str | None = None,
) -> SimpleNamespace:
    headers = {"Origin": ["http://127.0.0.1:8188"] if origin is None else origin}
    if site is not None:
        headers["Sec-Fetch-Site"] = site
    return SimpleNamespace(
        content_type=content_type,
        content_length=len(body),
        content=_Content(body),
        headers=_Headers(headers),
        transport=ListenerTransport(),
        match_info={"job_id": job_id} if job_id is not None else {},
    )


def registered() -> dict[tuple[str, str], Callable[..., Any]]:
    routes = _Routes()
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(
        json_response=lambda value, status=200, headers=None: (status, value),
        Response=lambda status=200, body=None, headers=None: (status, body),
    )
    with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
        with patch.object(routes_module, "_ROUTE_REGISTERED", False):
            assert ensure_media_runtime_setup_route_registered()
            assert ensure_media_runtime_setup_route_registered()
    assert len(routes) == 3
    return {(route.method, route.path): route.handler for route in routes}


class RouteService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def status(self) -> dict[str, object]:
        self.calls.append("status")
        return {"schema": STATUS_SCHEMA}

    def job(self, job_id: str) -> dict[str, object]:
        self.calls.append(f"job:{job_id}")
        if job_id != JOB:
            raise MediaRuntimeSetupError("setup_job_not_found", 404)
        return {"schema": JOB_SCHEMA, "job_id": job_id}

    def start_install(self) -> dict[str, object]:
        self.calls.append("install")
        return {"schema": JOB_SCHEMA, "job_id": JOB}

    def rescan(self) -> dict[str, object]:
        raise OSError("C:\\private\\path leaked")


def test_the_three_routes_register_idempotently_through_the_owned_seam() -> None:
    handlers = registered()

    assert set(handlers) == {("GET", STATUS_ROUTE), ("POST", SETUP_ROUTE), ("GET", JOB_ROUTE)}
    for handler in handlers.values():
        assert handler.__module__ == routes_module.__name__


def test_a_foreign_route_on_any_path_leaves_all_three_unregistered() -> None:
    def foreign(_request: object) -> None:
        return None

    routes = _Routes([SimpleNamespace(method="GET", path=JOB_ROUTE, handler=foreign)])
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(json_response=lambda value, status=200: value)
    with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
        with patch.object(routes_module, "_ROUTE_REGISTERED", False):
            assert not ensure_media_runtime_setup_route_registered()

    assert len(routes) == 1


def test_routes_check_origin_before_work_and_answer_content_free() -> None:
    handlers = registered()
    fake = RouteService()

    async def exercise() -> None:
        status_route = handlers[("GET", STATUS_ROUTE)]
        setup_route = handlers[("POST", SETUP_ROUTE)]
        job_route = handlers[("GET", JOB_ROUTE)]

        assert await status_route(http_request(origin=[])) == (200, {"schema": STATUS_SCHEMA})
        assert await status_route(http_request(origin=["https://evil.example"])) == (
            403,
            {"error": "origin_rejected"},
        )
        assert await status_route(http_request(origin=[], site=["cross-site"])) == (
            403,
            {"error": "origin_rejected"},
        )

        foreign = http_request(request("install_supported"), origin=["https://evil.example"])
        assert await setup_route(foreign) == (403, {"error": "origin_rejected"})
        assert foreign.content.reads == 0
        assert await setup_route(http_request(b"{}", content_type="text/plain")) == (
            415,
            {"error": "media_type_rejected"},
        )
        oversized = http_request(b"x" * (routes_module.MAX_SETUP_REQUEST_BYTES + 1))
        assert await setup_route(oversized) == (413, {"error": "request_too_large"})
        assert await setup_route(http_request(b"{")) == (400, {"error": "invalid_request"})
        assert await setup_route(http_request(request("install_supported"))) == (
            202,
            {"schema": JOB_SCHEMA, "job_id": JOB},
        )
        leaked = await setup_route(http_request(request("rescan")))
        assert leaked == (500, {"error": "internal_failure"})

        assert await job_route(http_request(job_id="../../x")) == (
            400,
            {"error": "invalid_request"},
        )
        assert await job_route(http_request(job_id=OTHER_JOB)) == (
            404,
            {"error": "setup_job_not_found"},
        )
        assert await job_route(http_request(job_id=JOB)) == (
            200,
            {"schema": JOB_SCHEMA, "job_id": JOB},
        )

    with patch.object(routes_module, "media_runtime_setup", lambda: fake):
        asyncio.run(exercise())

    # Refused requests never reached the service: one admitted status read, one install.
    assert fake.calls.count("status") == 1
    assert fake.calls.count("install") == 1


def test_package_import_builds_no_setup_service_and_opens_no_connection() -> None:
    script = (
        "import socket, subprocess\n"
        "class Refuse(subprocess.Popen):\n"
        "    def __init__(self, *a, **k):\n"
        "        raise SystemExit('process spawned during import')\n"
        "subprocess.Popen = Refuse\n"
        "def refuse(*a, **k):\n"
        "    raise SystemExit('network used during import')\n"
        "socket.create_connection = refuse\n"
        "socket.getaddrinfo = refuse\n"
        "import comfyui_h3_context\n"
        "from comfyui_h3_context.adapters import composition_root as root\n"
        "import comfyui_h3_context.adapters.comfyui_media_runtime_setup\n"
        "assert root.installed(root.MEDIA_RUNTIME_SETUP) is None\n"
        "assert root.installed(root.MEDIA_RUNTIME_RESOLVER) is None\n"
        "print('inert')\n"
    )
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("H3_CONTEXT_")
    }
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(routes_module.__file__).resolve().parents[2],
        env=environment,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip().endswith("inert")


# ---------------------------------------------------------------------------------------------
# AC31-05: integrated chain with the real resolver


class FakeRoots:
    def __init__(self, root: Path) -> None:
        self.root = root

    def private_root(self) -> Path:
        return self.root

    def served_roots(self) -> tuple[Path, ...]:
        raise HostRootError(ResolutionReason.PRIVATE_ROOT_INVALID)


class InProcessWorker:
    def run(self, request: DiscoveryRequest, *, timeout_seconds: float) -> DiscoveryResponse:
        assert timeout_seconds > 0
        return admit_request(request, drive_type=lambda _path: 3)

    def cancel(self) -> None:
        return None


def no_path_inputs() -> RuntimeInputs:
    return RuntimeInputs(
        platform="win32",
        machine="AMD64",
        pointer_bits=64,
        python_prefix="C:\\h3-absent-prefix",
        path=None,
        legacy=(),
    )


@windows_only
def test_install_and_continue_locates_the_managed_pair_in_the_same_process(tmp_path: Path) -> None:
    archive = build_archive()
    root = tmp_path / "user" / "__h3_context"
    resolver = MediaRuntimeResolver(
        host_roots=FakeRoots(root),
        worker=InProcessWorker(),
        package_parent=tmp_path / "package",
        inputs_provider=no_path_inputs,
        ffmpeg_sha256=sha(FFMPEG),
        ffprobe_sha256=sha(FFPROBE),
    )
    downloads: list[BytesDownloader] = []

    def downloader() -> BytesDownloader:
        instance = BytesDownloader(archive)
        downloads.append(instance)
        return instance

    setup = MediaRuntimeSetupService(
        resolver=resolver,
        downloader_factory=downloader,
        installer_factory=functools.partial(
            ManagedRuntimeInstaller, disk_usage=lambda _path: PLENTY
        ),
        manifest_provider=lambda: manifest_for(archive),
        thread_starter=lambda target: target(),
        job_id_factory=lambda: JOB,
    )

    before = setup.status()
    assert cast(dict[str, str], before["resolution"])["reason"] == "supported_pair_missing"
    assert downloads == []

    job = setup.job(cast(str, setup.start_install()["job_id"]))
    after = setup.status()

    assert (job["state"], job["reason"]) == ("succeeded", "installed")
    assert after["resolution"] == {
        "schema": "h3.context.media_runtime_resolution.v1",
        "state": "located",
        "reason": "pair_admitted",
        "source_kind": "managed",
    }
    assert len(downloads) == 1
    assert "install_supported" not in cast(list[str], after["actions"])


def long_path_inputs() -> RuntimeInputs:
    # A real Windows PATH routinely holds more eligible directories than the candidate bound.
    return RuntimeInputs(
        platform="win32",
        machine="AMD64",
        pointer_bits=64,
        python_prefix="C:\\h3-absent-prefix",
        path=";".join(f"C:\\h3-missing-{index}" for index in range(40)),
        legacy=(),
    )


@windows_only
def test_a_long_path_that_truncates_discovery_still_offers_the_install(tmp_path: Path) -> None:
    # M25-34 B-M2534-01: on the supplied host a 118-entry PATH truncated discovery, and the missing
    # tools were reported as `discovery_limit` with no install offer, so Install and continue never
    # appeared. The managed directory is the first candidate and was searched, so installing it is
    # both safe and the one thing that resolves the search.
    archive = build_archive()
    root = tmp_path / "user" / "__h3_context"
    resolver = MediaRuntimeResolver(
        host_roots=FakeRoots(root),
        worker=InProcessWorker(),
        package_parent=tmp_path / "package",
        inputs_provider=long_path_inputs,
        ffmpeg_sha256=sha(FFMPEG),
        ffprobe_sha256=sha(FFPROBE),
    )
    setup = MediaRuntimeSetupService(
        resolver=resolver,
        downloader_factory=lambda: BytesDownloader(archive),
        installer_factory=functools.partial(
            ManagedRuntimeInstaller, disk_usage=lambda _path: PLENTY
        ),
        manifest_provider=lambda: manifest_for(archive),
        thread_starter=lambda target: target(),
        job_id_factory=lambda: JOB,
    )

    before = setup.status()
    assert cast(dict[str, str], before["resolution"])["reason"] == "discovery_limit"
    assert before["features"] == {
        feature: {"state": "setup_required", "reason": "discovery_limit"}
        for feature in ("import", "preview", "derivatives", "assembly", "render")
    }
    assert "install_supported" in cast(list[str], before["actions"])

    job = setup.job(cast(str, setup.start_install()["job_id"]))
    after = setup.status()

    assert (job["state"], job["reason"]) == ("succeeded", "installed")
    assert cast(dict[str, str], after["resolution"])["source_kind"] == "managed"
    assert "install_supported" not in cast(list[str], after["actions"])


@windows_only
def test_a_limit_reached_before_searching_the_managed_directory_offers_no_install(
    tmp_path: Path,
) -> None:
    # An oversized PATH is refused before enumeration: the managed directory was never searched
    # and may hold a valid runtime that publication would retire.
    def oversized() -> RuntimeInputs:
        return RuntimeInputs(
            platform="win32",
            machine="AMD64",
            pointer_bits=64,
            python_prefix="C:\\h3-absent-prefix",
            path="C:\\x;" * 7000,
            legacy=(),
        )

    root = tmp_path / "user" / "__h3_context"
    resolver = MediaRuntimeResolver(
        host_roots=FakeRoots(root),
        worker=InProcessWorker(),
        package_parent=tmp_path / "package",
        inputs_provider=oversized,
        ffmpeg_sha256=sha(FFMPEG),
        ffprobe_sha256=sha(FFPROBE),
    )
    downloads: list[BytesDownloader] = []

    def downloader() -> BytesDownloader:
        instance = BytesDownloader(b"")
        downloads.append(instance)
        return instance

    setup = MediaRuntimeSetupService(
        resolver=resolver,
        downloader_factory=downloader,
        thread_starter=lambda target: target(),
        job_id_factory=lambda: JOB,
    )

    status = setup.status()
    assert cast(dict[str, dict[str, str]], status["features"])["import"] == {
        "state": "unavailable",
        "reason": "discovery_limit",
    }
    assert "install_supported" not in cast(list[str], status["actions"])
    job = setup.job(cast(str, setup.start_install()["job_id"]))
    assert (job["state"], job["reason"]) == ("failed", "discovery_limit")
    assert downloads == []


@windows_only
def test_config_actions_commit_through_the_real_resolver_and_preserve_bytes_on_refusal(
    tmp_path: Path,
) -> None:
    local = tmp_path / "tools"
    local.mkdir()
    (local / "ffmpeg.exe").write_bytes(FFMPEG)
    (local / "ffprobe.exe").write_bytes(FFPROBE)
    root = tmp_path / "user" / "__h3_context"
    resolver = MediaRuntimeResolver(
        host_roots=FakeRoots(root),
        worker=InProcessWorker(),
        package_parent=tmp_path / "package",
        inputs_provider=no_path_inputs,
        ffmpeg_sha256=sha(FFMPEG),
        ffprobe_sha256=sha(FFPROBE),
    )
    setup = MediaRuntimeSetupService(resolver=resolver, thread_starter=lambda target: target())
    config_file = private_layout(root).config_file

    selected = setup.use_local_directory(str(local), 0)
    assert selected["config"] == {"revision": 1, "selection": "local"}
    assert cast(dict[str, str], selected["resolution"])["source_kind"] == "local_selection"
    assert selected["actions"] == ["rescan", "use_local_directory", "restore_auto"]
    assert str(local) not in json.dumps(selected)
    committed = config_file.read_bytes()

    absent = str(tmp_path / "absent")
    refusals: list[tuple[Callable[[], object], str, int]] = [
        (lambda: setup.restore_auto(0), "config_conflict", 409),
        (lambda: setup.use_local_directory("relative", 1), "selection_invalid_path", 422),
        (lambda: setup.use_local_directory(absent, 1), "selection_invalid_path", 422),
    ]
    for refused_action, code, status in refusals:
        with pytest.raises(MediaRuntimeSetupError) as refused:
            refused_action()
        assert (refused.value.code, refused.value.status) == (code, status)
        assert config_file.read_bytes() == committed

    restored = setup.restore_auto(1)
    assert restored["config"] == {"revision": 2, "selection": "auto"}
    assert cast(dict[str, str], restored["resolution"])["reason"] == "supported_pair_missing"
    assert "restore_auto" not in cast(list[str], restored["actions"])


# ---------------------------------------------------------------------------------------------
# Real inputs, only when explicitly supplied


@windows_only
@pytest.mark.skipif(
    not os.environ.get("H3_M25_31_MANAGED_ARCHIVE_PATH"),
    reason="the verified release archive was not explicitly supplied",
)
def test_the_verified_release_archive_installs_and_passes_the_production_pin(
    tmp_path: Path,
) -> None:
    source = Path(os.environ["H3_M25_31_MANAGED_ARCHIVE_PATH"])
    manifest = managed_runtime_manifest()

    class FileDownloader(BytesDownloader):
        def download(
            self,
            url: str,
            *,
            expected_bytes: int,
            sink: Callable[[bytes], None],
            cancelled: threading.Event,
            progress: Callable[[int], None],
        ) -> None:
            assert url == manifest.source_url
            assert expected_bytes == manifest.archive_bytes
            done = 0
            with source.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    sink(chunk)
                    done += len(chunk)
                    progress(done)

    root = tmp_path / "user" / "__h3_context"
    layout = private_layout(root)
    ManagedRuntimeInstaller(
        layout,
        manifest=manifest,
        downloader=FileDownloader(b""),
        job_token=JOB,
        cancelled=threading.Event(),
        progress=lambda *_args: None,
    ).run()

    published = layout.runtime_root / MANAGED_PROFILE_COMPONENT
    assert tree_files(published) == [
        "LICENSE.txt",
        "README.txt",
        "bin/ffmpeg.exe",
        "bin/ffprobe.exe",
        "source.json",
    ]
    resolver = MediaRuntimeResolver(
        host_roots=FakeRoots(root),
        worker=SubprocessDiscoveryWorker(),
        inputs_provider=no_path_inputs,
    )
    resolution = resolver.resolve()
    assert resolution.state is ResolutionState.LOCATED
    assert resolution.source_kind is SourceKind.MANAGED
    assert resolution.ffmpeg_path == layout.managed_bin / "ffmpeg.exe"


@pytest.mark.skipif(
    os.environ.get("H3_M25_31_LIVE_DOWNLOAD") != "1",
    reason="the live release download was not explicitly requested",
)
def test_the_production_transport_downloads_the_fixed_archive_live() -> None:
    manifest = managed_runtime_manifest()
    hosts: list[str] = []

    class RecordingDownloader(HttpsArchiveDownloader):
        def _open(self, host: str, deadline: float) -> Any:
            hosts.append(host)
            return super()._open(host, deadline)

    digest = hashlib.sha256()
    size = [0]

    def sink(chunk: bytes) -> None:
        digest.update(chunk)
        size[0] += len(chunk)

    RecordingDownloader().download(
        manifest.source_url,
        expected_bytes=manifest.archive_bytes,
        sink=sink,
        cancelled=threading.Event(),
        progress=lambda _done: None,
    )

    assert size[0] == manifest.archive_bytes
    assert digest.hexdigest() == manifest.archive_sha256
    assert hosts[0] == INITIAL_HOST
    assert set(hosts[1:]) <= ASSET_HOSTS
    evidence = os.environ.get("H3_M25_31_LIVE_EVIDENCE")
    if evidence:
        Path(evidence).write_text(
            json.dumps({"hosts": hosts, "bytes": size[0], "sha256": digest.hexdigest()}, indent=2),
            encoding="utf-8",
        )
