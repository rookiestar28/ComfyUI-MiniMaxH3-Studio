"""Request facts an owned route's admission reads, for tests that build requests by hand.

Since M23-57 an owned route admits a request only for the target its `Host` selects on the socket
that accepted it, so a faithful request double carries three things a real aiohttp request has: a
`Host` header, an `Origin` header, and a transport whose `get_extra_info("sockname")` names the
local endpoint (and `get_extra_info("sslcontext")` whether it is native TLS). A double without the
transport is refused as `authority_unavailable`, which would let a negative test pass for the wrong
reason -- so a test that expects a refusal should also assert which one.
"""

from __future__ import annotations

from typing import Any

#: The default ComfyUI listener, and the one deployment every pre-M23-57 fixture assumed.
LOOPBACK_HOST = "127.0.0.1:8188"
LOOPBACK_ORIGIN = "http://127.0.0.1:8188"
LOOPBACK_SOCKNAME = ("127.0.0.1", 8188)


class ListenerTransport:
    """The transport surface admission and the streaming edges read."""

    def __init__(
        self,
        sockname: object = LOOPBACK_SOCKNAME,
        *,
        secure: bool = False,
        closing: bool = False,
    ) -> None:
        self._extra: dict[str, object] = {
            "sockname": sockname,
            "sslcontext": object() if secure else None,
        }
        self._closing = closing
        self.aborted = False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._extra.get(name, default)

    def is_closing(self) -> bool:
        return self._closing

    def abort(self) -> None:
        self.aborted = True
        self._closing = True


def admission_headers(
    origins: list[str] | None = None,
    *,
    host: list[str] | None = None,
    fetch_sites: list[str] | None = None,
) -> dict[str, list[str]]:
    """The admission-relevant header lists for the default listener, overridable per case."""

    return {
        "Host": [LOOPBACK_HOST] if host is None else host,
        "Origin": [LOOPBACK_ORIGIN] if origins is None else origins,
        "Sec-Fetch-Site": [] if fetch_sites is None else fetch_sites,
    }


__all__ = [
    "LOOPBACK_HOST",
    "LOOPBACK_ORIGIN",
    "LOOPBACK_SOCKNAME",
    "ListenerTransport",
    "admission_headers",
]
