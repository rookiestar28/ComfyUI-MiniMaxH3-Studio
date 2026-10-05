"""Which browser origin may act on this server, decided per request from trusted facts.

An owned route used to accept exactly one origin, `http://127.0.0.1:8188`, so the same page served
at `localhost`, another port, IPv6 loopback, a directly served interface address or behind a proxy
was refused as foreign. Widening that one constant would not have helped: each deployment is only
legitimate for its *own* page, and a page on one trusted alias must never gain authority over
another. The rule here is therefore relative, never a list:

1. the request's `Host` names the authority the browser addressed;
2. that authority must be one this server actually serves -- either an exact origin the operator
   configured (a proxy, a port mapping or a custom hostname), or the connection's own local
   endpoint, observed from the socket that accepted it (an IP literal equal to that address, or
   `localhost` when the address is loopback), on the port that socket is bound to;
3. the one target that authority selects is the only origin the request may carry.

CRITICAL: a well-formed `Host` that is not a trusted authority is refused, never echoed back as a
target. Echoing it is the DNS-rebinding hole: a hostile page whose name resolves to this machine
sends a `Host` and an `Origin` that agree with each other. No name is resolved here, and an
arbitrary DNS name is never trusted from the socket alone.

This module is pure: it parses and compares values handed to it. Reading headers, the transport
and the environment belongs to `adapters/comfyui_route_seam.py`.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import Enum

#: The operator's list of browser origins served through a proxy, a port mapping or a custom
#: hostname. Read by the route seam once per process; changing it needs a ComfyUI restart.
PUBLIC_ORIGINS_VARIABLE = "H3_CONTEXT_PUBLIC_ORIGINS"
MAX_PUBLIC_ORIGINS = 8
MAX_PUBLIC_ORIGINS_LENGTH = 2048
#: An authority is a 253-character name, a colon and a five-digit port; the IPv6 form is shorter.
MAX_AUTHORITY_LENGTH = 262

_DEFAULT_PORTS = {"http": 80, "https": 443}
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_DOTTED_QUAD = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}\Z")
#: A final label a browser's URL parser would read as a number. `http://1.2.3/` is parsed to
#: `1.2.0.3` and `http://0x7f.1/` to `127.0.0.1`, so a `Host` spelled this way was never produced
#: by a browser and is refused rather than compared as a name.
_NUMERIC_LABEL = re.compile(r"(?:[0-9]+|0x[0-9a-f]*)\Z")
#: No leading zero: a browser serializes the port as a plain decimal, so `:08188` is never its.
_PORT = re.compile(r"[1-9][0-9]{0,4}\Z")
_ORIGIN = re.compile(r"(https?)://(.+)\Z")
_CONFIG_WHITESPACE = " \t"


class AdmissionReason(Enum):
    """Why a request was refused. A closed set of categories; none carries a request value."""

    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    HOST_INVALID = "host_invalid"
    HOST_UNTRUSTED = "host_untrusted"
    ORIGIN_MISSING = "origin_missing"
    ORIGIN_INVALID = "origin_invalid"
    ORIGIN_MISMATCH = "origin_mismatch"
    FETCH_SITE_REJECTED = "fetch_site_rejected"


class OriginRequirement(Enum):
    """What a route requires of the `Origin` header once the target is known."""

    #: Present and equal to the target. Every mutation.
    REQUIRED = "required"
    #: Equal to the target, or absent: browsers omit it on a same-origin GET.
    OPTIONAL = "optional"
    #: As `OPTIONAL`, and `Sec-Fetch-Site` absent or exactly `same-origin`, for capability-bearing
    #: reads a native navigation reaches without an `Origin`.
    OPTIONAL_SAME_ORIGIN_FETCH = "optional_same_origin_fetch"


class PublicOriginsStatus(Enum):
    UNSET = "unset"
    VALID = "valid"
    EMPTY = "empty"
    MALFORMED = "malformed"
    DUPLICATE = "duplicate"
    TOO_MANY = "too_many"
    TOO_LONG = "too_long"


@dataclass(frozen=True, slots=True)
class WebOrigin:
    """A normalized `scheme://host[:port]`, with the scheme's default port made explicit."""

    scheme: str
    host: str
    port: int

    def serialize(self) -> str:
        if self.port == _DEFAULT_PORTS[self.scheme]:
            return f"{self.scheme}://{self.host}"
        return f"{self.scheme}://{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class LocalEndpoint:
    """The address and port the host accepted this connection on, and whether it is native TLS."""

    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    port: int
    secure: bool


@dataclass(frozen=True, slots=True)
class PublicOrigins:
    """The operator's configured targets. Anything but `VALID` configures none."""

    status: PublicOriginsStatus
    origins: tuple[WebOrigin, ...] = ()
    entries: int = 0


NO_PUBLIC_ORIGINS = PublicOrigins(PublicOriginsStatus.UNSET)


def _hostname(value: str) -> str | None:
    """A normalized hostname, or `None` for anything a browser would not have sent."""

    if value.startswith("["):
        if not value.endswith("]") or "%" in value:
            return None
        try:
            address = ipaddress.IPv6Address(value[1:-1])
        except ValueError:
            return None
        return f"[{address.compressed}]"
    if _DOTTED_QUAD.match(value):
        try:
            return str(ipaddress.IPv4Address(value))
        except ValueError:
            # A quad with a leading zero or an octet over 255: a browser never sends one.
            return None
    if len(value) > 253:
        return None
    labels = value.split(".")
    if not all(_LABEL.match(label) for label in labels):
        return None
    if _NUMERIC_LABEL.match(labels[-1]):
        return None
    return value


def parse_authority(value: object) -> tuple[str, int | None] | None:
    """`(hostname, explicit port or None)` for a strict `host[:port]`, else `None`."""

    if type(value) is not str or not value or len(value) > MAX_AUTHORITY_LENGTH:
        return None
    if not value.isascii() or not value.isprintable() or any(c.isspace() for c in value):
        return None
    lowered = value.lower()
    port: int | None = None
    if lowered.startswith("["):
        close = lowered.find("]")
        if close < 0:
            return None
        host_part, rest = lowered[: close + 1], lowered[close + 1 :]
        if rest:
            if not rest.startswith(":"):
                return None
            port_text = rest[1:]
        else:
            port_text = None
    else:
        host_part, separator, port_text_or_empty = lowered.partition(":")
        port_text = port_text_or_empty if separator else None
    if port_text is not None:
        if not _PORT.match(port_text):
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
    hostname = _hostname(host_part)
    if hostname is None:
        return None
    return hostname, port


def parse_origin(value: object) -> WebOrigin | None:
    """A strict serialized origin: `http|https://host[:port]`, nothing after it, never `null`."""

    if type(value) is not str:
        return None
    match = _ORIGIN.match(value.lower()) if value.isascii() else None
    if match is None:
        return None
    scheme, authority_text = match.group(1), match.group(2)
    authority = parse_authority(authority_text)
    if authority is None:
        return None
    hostname, port = authority
    return WebOrigin(scheme, hostname, _DEFAULT_PORTS[scheme] if port is None else port)


def local_endpoint(sockname: object, secure: bool) -> LocalEndpoint | None:
    """The connection's local endpoint from a transport `sockname`, or `None` when unusable."""

    if type(sockname) is not tuple or len(sockname) not in (2, 4):
        return None
    address_text, port = sockname[0], sockname[1]
    if type(address_text) is not str or type(port) is not int or not 1 <= port <= 65535:
        return None
    try:
        address = ipaddress.ip_address(address_text.partition("%")[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return LocalEndpoint(address, port, secure is True)


def parse_public_origins(raw: object) -> PublicOrigins:
    """The operator's `H3_CONTEXT_PUBLIC_ORIGINS`. Invalid configuration configures nothing."""

    if raw is None:
        return NO_PUBLIC_ORIGINS
    if type(raw) is not str:
        return PublicOrigins(PublicOriginsStatus.MALFORMED)
    if len(raw) > MAX_PUBLIC_ORIGINS_LENGTH:
        return PublicOrigins(PublicOriginsStatus.TOO_LONG)
    if not raw.strip(_CONFIG_WHITESPACE):
        return PublicOrigins(PublicOriginsStatus.EMPTY)
    entries = [entry.strip(_CONFIG_WHITESPACE) for entry in raw.split(",")]
    if len(entries) > MAX_PUBLIC_ORIGINS:
        return PublicOrigins(PublicOriginsStatus.TOO_MANY, entries=len(entries))
    origins: list[WebOrigin] = []
    for entry in entries:
        origin = parse_origin(entry)
        # CRITICAL: an origin must round-trip to its own serialization. `https://h:443` parses to
        # the same origin as `https://h`, but a trailing `/`, a path or a wildcard never parses,
        # and a partially valid list configures nothing rather than the parts that parsed.
        if origin is None:
            return PublicOrigins(PublicOriginsStatus.MALFORMED, entries=len(entries))
        origins.append(origin)
    if len(set(origins)) != len(origins):
        return PublicOrigins(PublicOriginsStatus.DUPLICATE, entries=len(entries))
    return PublicOrigins(PublicOriginsStatus.VALID, tuple(origins), len(entries))


def _is_ip_literal(hostname: str) -> bool:
    return hostname.startswith("[") or _DOTTED_QUAD.match(hostname) is not None


def resolve_target(
    hosts: object, local: LocalEndpoint | None, public: PublicOrigins
) -> WebOrigin | AdmissionReason:
    """The one origin this request may act for, or why it has none."""

    if type(hosts) is not list or len(hosts) != 1:
        return AdmissionReason.HOST_INVALID
    authority = parse_authority(hosts[0])
    if authority is None:
        return AdmissionReason.HOST_INVALID
    hostname, explicit_port = authority
    if public.status is PublicOriginsStatus.VALID:
        for origin in public.origins:
            port = _DEFAULT_PORTS[origin.scheme] if explicit_port is None else explicit_port
            if origin.host == hostname and origin.port == port:
                return origin
    if local is None:
        return AdmissionReason.AUTHORITY_UNAVAILABLE
    scheme = "https" if local.secure else "http"
    port = _DEFAULT_PORTS[scheme] if explicit_port is None else explicit_port
    if port != local.port:
        return AdmissionReason.HOST_UNTRUSTED
    if hostname == "localhost":
        trusted = local.address.is_loopback
    elif _is_ip_literal(hostname):
        trusted = ipaddress.ip_address(hostname.strip("[]")) == local.address
    else:
        # CRITICAL: never trust a DNS name from the socket alone -- see the module docstring.
        trusted = False
    if not trusted:
        return AdmissionReason.HOST_UNTRUSTED
    return WebOrigin(scheme, hostname, port)


def admission_refusal(
    requirement: OriginRequirement,
    *,
    hosts: object,
    origins: object,
    fetch_sites: object,
    local: LocalEndpoint | None,
    public: PublicOrigins,
) -> AdmissionReason | None:
    """`None` when the request may act for its target, otherwise the one reason it may not.

    CRITICAL: the target is resolved for every requirement, including the ones that admit an
    absent `Origin`. A same-origin GET carries no `Origin`, so for those reads the `Host` check is
    the only thing standing between a rebinding page and this server's responses.
    """

    target = resolve_target(hosts, local, public)
    if isinstance(target, AdmissionReason):
        return target
    if type(origins) is not list:
        return AdmissionReason.ORIGIN_INVALID
    if not origins:
        if requirement is OriginRequirement.REQUIRED:
            return AdmissionReason.ORIGIN_MISSING
    elif len(origins) != 1:
        return AdmissionReason.ORIGIN_INVALID
    else:
        origin = parse_origin(origins[0])
        if origin is None:
            return AdmissionReason.ORIGIN_INVALID
        if origin != target:
            return AdmissionReason.ORIGIN_MISMATCH
    if requirement is OriginRequirement.OPTIONAL_SAME_ORIGIN_FETCH:
        if type(fetch_sites) is not list or fetch_sites not in ([], ["same-origin"]):
            return AdmissionReason.FETCH_SITE_REJECTED
    return None


__all__ = [
    "MAX_PUBLIC_ORIGINS",
    "MAX_PUBLIC_ORIGINS_LENGTH",
    "NO_PUBLIC_ORIGINS",
    "PUBLIC_ORIGINS_VARIABLE",
    "AdmissionReason",
    "LocalEndpoint",
    "OriginRequirement",
    "PublicOrigins",
    "PublicOriginsStatus",
    "WebOrigin",
    "admission_refusal",
    "local_endpoint",
    "parse_authority",
    "parse_origin",
    "parse_public_origins",
    "resolve_target",
]
