"""Explicit host configuration with request-scoped, revocable official provider services.

Installing a factory performs no provider I/O and discovers no credentials. The host supplies
its own existing transport and resolver, scoped to the opaque reference of each execution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock

from ..core.errors import SecurityPolicyError
from ..core.official_context_ir import (
    OfficialContextIRTransport,
    OfficialContextIRTransportResponse,
)
from ..core.provider_policy import CredentialResolver, ResolvedCredential


@dataclass(frozen=True, slots=True)
class OfficialContextIRHostServices:
    transport: OfficialContextIRTransport = field(repr=False)
    resolver: CredentialResolver = field(repr=False)
    is_current: Callable[[], bool] = field(repr=False)
    cleanup: Callable[[], None] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.transport, OfficialContextIRTransport):
            raise TypeError("host transport does not implement the official protocol")
        if not isinstance(self.resolver, CredentialResolver):
            raise TypeError("host resolver does not implement the credential protocol")
        if not callable(self.is_current) or not callable(self.cleanup):
            raise TypeError("host authority and cleanup callbacks are required")


@dataclass(frozen=True, slots=True, eq=False)
class OfficialContextIRHostBinding:
    """Opaque identity used only to clear the exact configuration its caller installed."""

    _factory: Callable[[str], OfficialContextIRHostServices] = field(repr=False)


_LOCK = RLock()
_BINDING: OfficialContextIRHostBinding | None = None


def configure_official_context_ir_host(
    factory: Callable[[str], OfficialContextIRHostServices],
) -> OfficialContextIRHostBinding:
    """Install trusted host configuration; replacement revokes all older request leases."""

    if not callable(factory):
        raise TypeError("official host factory must be callable")
    binding = OfficialContextIRHostBinding(factory)
    global _BINDING
    with _LOCK:
        _BINDING = binding
    return binding


def clear_official_context_ir_host(binding: OfficialContextIRHostBinding) -> bool:
    """A stale owner cannot remove a replacement's configuration."""

    global _BINDING
    with _LOCK:
        if _BINDING is not binding:
            return False
        _BINDING = None
        return True


def official_context_ir_host_configured() -> bool:
    with _LOCK:
        return _BINDING is not None


class OfficialContextIRHostLease:
    """One execution; neither services nor a resolved credential enter workflow state."""

    def __init__(
        self,
        binding: OfficialContextIRHostBinding,
        reference: str,
        services: OfficialContextIRHostServices,
    ) -> None:
        self._binding = binding
        self._reference = reference
        self._services = services
        self._closed = False
        self.resolver = _LeaseResolver(self)
        self.transport = _LeaseTransport(self)

    def ensure_current(self) -> None:
        with _LOCK:
            current = _BINDING is self._binding
        try:
            authorized = self._services.is_current() if current else False
        except Exception:
            raise SecurityPolicyError("official host authority unavailable") from None
        # The callback may itself replace configuration; its earlier snapshot is not authority.
        with _LOCK:
            current = current and _BINDING is self._binding
        if authorized is not True or not current:
            raise SecurityPolicyError("official host authority revoked")

    def resolve(self, reference: str) -> ResolvedCredential:
        self.ensure_current()
        if self._closed or reference != self._reference:
            raise SecurityPolicyError("official host credential scope rejected")
        try:
            value = self._services.resolver.resolve(reference)
        except Exception:
            raise SecurityPolicyError("official host credential unavailable") from None
        self.ensure_current()
        if not isinstance(value, ResolvedCredential) or value.reference != reference:
            raise SecurityPolicyError("official host credential scope rejected")
        return value

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._services.cleanup()
        except Exception:
            raise SecurityPolicyError("official host cleanup failed") from None


class _LeaseResolver:
    def __init__(self, lease: OfficialContextIRHostLease) -> None:
        self._lease = lease

    def resolve(self, reference: str) -> ResolvedCredential:
        return self._lease.resolve(reference)


class _LeaseTransport:
    def __init__(self, lease: OfficialContextIRHostLease) -> None:
        self._lease = lease

    def create(
        self, payload: dict[str, object], credential: ResolvedCredential
    ) -> OfficialContextIRTransportResponse:
        # CRITICAL: the core retains its credential across polls; resolve fresh at every I/O.
        current = self._lease.resolve(credential.reference)
        result = self._lease._services.transport.create(payload, current)
        self._lease.ensure_current()
        return result

    def query(
        self, task_id: str, credential: ResolvedCredential
    ) -> OfficialContextIRTransportResponse:
        current = self._lease.resolve(credential.reference)
        result = self._lease._services.transport.query(task_id, current)
        self._lease.ensure_current()
        return result


def acquire_official_context_ir_host(reference: str) -> OfficialContextIRHostLease:
    with _LOCK:
        binding = _BINDING
    if binding is None:
        raise SecurityPolicyError("official host transport is unconfigured")
    # CRITICAL: host callbacks must run outside the configuration lock, including cleanup.
    try:
        services = binding._factory(reference)
    except Exception:
        raise SecurityPolicyError("official host service unavailable") from None
    if type(services) is not OfficialContextIRHostServices:
        raise SecurityPolicyError("official host service invalid")
    lease = OfficialContextIRHostLease(binding, reference, services)
    try:
        lease.ensure_current()
    except BaseException:
        lease.close()
        raise
    return lease


__all__ = [
    "OfficialContextIRHostBinding",
    "OfficialContextIRHostServices",
    "clear_official_context_ir_host",
    "configure_official_context_ir_host",
]
