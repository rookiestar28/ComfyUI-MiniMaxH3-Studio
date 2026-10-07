from __future__ import annotations

import socket
import socketserver
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, cast

import pytest

from comfyui_h3_context.adapters.ollama_native import SemanticDeadlineOllamaTransport
from comfyui_h3_context.core.errors import (
    LocalAdapterCancelledError,
    LocalAdapterTimeoutError,
    ModelTransportError,
)
from comfyui_h3_context.core.local_adapters import LocalCancellationProbe


class _RawHandler(socketserver.BaseRequestHandler):
    response_parts: tuple[tuple[bytes, float], ...] = ()
    requests: list[bytes] = []

    def handle(self) -> None:
        received = bytearray()
        self.request.settimeout(1.0)
        while b"\r\n\r\n" not in received:
            block = self.request.recv(4096)
            if not block:
                return
            received.extend(block)
        header, body = bytes(received).split(b"\r\n\r\n", 1)
        content_length = 0
        for line in header.split(b"\r\n")[1:]:
            if line.startswith(b"Content-Length: "):
                content_length = int(line.split(b": ", 1)[1])
        while len(body) < content_length:
            body += self.request.recv(content_length - len(body))
        type(self).requests.append(header + b"\r\n\r\n" + body)
        for part, delay in type(self).response_parts:
            if delay:
                time.sleep(delay)
            try:
                self.request.sendall(part)
            except OSError:
                return


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _RedirectSocket:
    def __init__(self, target: tuple[str, int], original: socket.socket) -> None:
        self._target = target
        self._socket = original

    def connect_ex(self, _: tuple[str, int]) -> int:
        return self._socket.connect_ex(self._target)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._socket, name)


@contextmanager
def _raw_server(
    monkeypatch: pytest.MonkeyPatch,
    *parts: tuple[bytes, float],
) -> Iterator[type[_RawHandler]]:
    handler = type("Handler", (_RawHandler,), {"response_parts": tuple(parts), "requests": []})
    server = _Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_socket = socket.socket

    def redirected(*args: Any, **kwargs: Any) -> _RedirectSocket:
        target = cast(tuple[str, int], server.server_address)
        return _RedirectSocket(target, original_socket(*args, **kwargs))

    monkeypatch.setattr(socket, "socket", redirected)
    try:
        yield handler
    finally:
        monkeypatch.setattr(socket, "socket", original_socket)
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def _transport() -> SemanticDeadlineOllamaTransport:
    return SemanticDeadlineOllamaTransport("qwen3.8:27b-bf16")


def _request(
    transport: SemanticDeadlineOllamaTransport,
    path: str = "/api/version",
    *,
    phase_seconds: float = 1.0,
    cancellation: LocalCancellationProbe | None = None,
) -> Mapping[str, object]:
    now = time.monotonic()
    return transport.request(
        "GET",
        path,
        None,
        cancellation=cancellation,
        absolute_deadline=now + phase_seconds,
        phase_timeout_seconds=phase_seconds,
    )


def test_content_length_and_chunked_frames_are_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
        b'Content-Length: 13\r\n\r\n{"models":[]}'
    )
    with _raw_server(monkeypatch, (fixed, 0)) as handler:
        assert _request(_transport(), "/api/ps") == {"models": []}
        assert handler.requests[0].startswith(b"GET /api/ps HTTP/1.1\r\n")

    chunked = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b'Transfer-Encoding: chunked\r\n\r\n7\r\n{"versi\r\n'
        b'8\r\non":"1"}\r\n0\r\n\r\n'
    )
    with _raw_server(monkeypatch, (chunked, 0)):
        assert _request(_transport()) == {"version": "1"}


@pytest.mark.parametrize(
    "response",
    (
        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\nContent-Type: application/json\nContent-Length: 2\n\n{}",
        b"HTTP/1.1 200 OK\r\nContent-Type:\tapplication/json\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 02\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n1;ext=x\r\n{\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\n{}",
    ),
)
def test_malformed_http_is_content_free(
    monkeypatch: pytest.MonkeyPatch,
    response: bytes,
) -> None:
    with _raw_server(monkeypatch, (response, 0)), pytest.raises(ModelTransportError) as caught:
        _request(_transport())
    assert str(caught.value) == "semantic_http_invalid"
    assert "HTTP" not in repr(caught.value)


def test_slow_drip_cannot_renew_cumulative_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 13\r\n\r\n"
    parts = [(prefix, 0.0)] + [(bytes([value]), 0.02) for value in b'{"models":[]}']
    with _raw_server(monkeypatch, *parts), pytest.raises(LocalAdapterTimeoutError) as caught:
        _request(_transport(), "/api/ps", phase_seconds=0.08)
    assert str(caught.value) == "semantic_http_timeout"


class _Cancelled:
    def __init__(self) -> None:
        self._started = time.monotonic()

    def is_cancelled(self) -> bool:
        return time.monotonic() - self._started >= 0.02


def test_cancellation_is_polled_inside_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    with _raw_server(monkeypatch, (b"", 1.0)), pytest.raises(LocalAdapterCancelledError):
        started = time.monotonic()
        _request(_transport(), cancellation=_Cancelled())
        assert time.monotonic() - started < 0.5


@pytest.mark.parametrize(
    ("method", "path", "payload", "phase_seconds"),
    (
        ("GET", "/api/version", None, 30.000001),
        ("POST", "/api/chat", {"model": "qwen3.8:27b-bf16"}, 120.000001),
        (
            "POST",
            "/api/generate",
            {"model": "qwen3.8:27b-bf16", "keep_alive": 0},
            5.000001,
        ),
        ("POST", "/api/generate", {"model": "qwen3.8:27b-bf16", "prompt": "x"}, 5.0),
    ),
)
def test_transport_owner_rejects_path_budget_or_cleanup_payload_before_contact(
    method: str,
    path: str,
    payload: dict[str, object] | None,
    phase_seconds: float,
) -> None:
    now = time.monotonic()
    with pytest.raises(ModelTransportError):
        _transport().request(
            method,
            path,
            payload,
            absolute_deadline=now + phase_seconds,
            phase_timeout_seconds=phase_seconds,
        )


def _direct_chat_payload() -> dict[str, object]:
    return {
        "model": "qwen3.8:27b-bf16",
        "messages": [{"role": "user", "content": "synthetic"}],
        "stream": False,
        "format": {"type": "object"},
        "options": {
            "temperature": 0.0,
            "top_k": 0,
            "top_p": 1.0,
            "min_p": 0.0,
            "repeat_penalty": 1.0,
            "presence_penalty": 0.0,
            "num_predict": 128,
            "seed": 0,
        },
        "keep_alive": "30s",
        "think": False,
    }


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value,
        lambda value: {**value, "tools": []},
        lambda value: {**value, "images": []},
        lambda value: {**value, "stream": True},
        lambda value: {**value, "messages": [{"role": "user", "content": "changed"}]},
        lambda value: {**value, "format": "json"},
        lambda value: {
            **value,
            "options": {**cast(dict[str, object], value["options"]), "seed": 1},
        },
        lambda value: {**value, "keep_alive": "5m"},
        lambda value: {**value, "think": True},
    ),
)
def test_direct_chat_payload_without_qualified_lease_never_contacts_socket(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
) -> None:
    response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
    payload = mutate(_direct_chat_payload())
    now = time.monotonic()
    with _raw_server(monkeypatch, (response, 0)) as handler:
        with pytest.raises(ModelTransportError, match="semantic_http_request"):
            _transport().request(
                "POST",
                "/api/chat",
                payload,
                absolute_deadline=now + 1.0,
                phase_timeout_seconds=1.0,
            )
        assert handler.requests == []
