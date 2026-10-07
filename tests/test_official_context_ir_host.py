"""Ordinary registered-node composition with scoped host authority and no provider network."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import cast

import pytest
from test_official_context_ir_node import raw_request

from comfyui_h3_context.adapters.official_context_ir_host import (
    OfficialContextIRHostBinding,
    OfficialContextIRHostServices,
    clear_official_context_ir_host,
    configure_official_context_ir_host,
    official_context_ir_host_configured,
)
from comfyui_h3_context.core.context_reporting import ProviderReceipt
from comfyui_h3_context.core.errors import OfficialContextIRError
from comfyui_h3_context.core.official_context_ir import OfficialContextIRTransportResponse
from comfyui_h3_context.core.provider_policy import ProviderConsentNotice, ResolvedCredential
from comfyui_h3_context.h3_adapter_nodes import H3OfficialContextIRNode
from comfyui_h3_context.node_support import OfficialContextIRNodeError
from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS, OFFICIAL_CONTEXT_IR_NODE_ID


class FixtureServices:
    def __init__(self) -> None:
        self.active = True
        self.available = True
        self.resolutions = 0
        self.acquisitions = 0
        self.cleanups = 0
        self.calls: list[tuple[str, str]] = []
        self.after_create: Callable[[], None] = lambda: None
        self.after_query: Callable[[], None] = lambda: None
        self.on_cleanup: Callable[[], None] = lambda: None
        self.reference_override: str | None = None
        self.query_status = "succeeded"

    def resolve(self, reference: str) -> ResolvedCredential:
        self.resolutions += 1
        if not self.available:
            raise ValueError("fixture private resolver detail")
        return ResolvedCredential(self.reference_override or reference, "fixture-value")

    def create(
        self, payload: dict[str, object], credential: ResolvedCredential
    ) -> OfficialContextIRTransportResponse:
        self.calls.append(("create", credential.reference))
        self.after_create()
        return OfficialContextIRTransportResponse(202, {"task_id": "host-task-1"})

    def query(
        self, task_id: str, credential: ResolvedCredential
    ) -> OfficialContextIRTransportResponse:
        self.calls.append(("query", credential.reference))
        self.after_query()
        return OfficialContextIRTransportResponse(
            200,
            {
                "task": {
                    "status": self.query_status,
                    "task_type": "h3_context_ir",
                    "content": {"prompt": "fixture output"},
                }
            },
        )

    def factory(self, reference: str) -> OfficialContextIRHostServices:
        self.acquisitions += 1
        if reference != "runtime.session_one":
            raise ValueError("fixture private session detail")
        return OfficialContextIRHostServices(self, self, lambda: self.active, self.cleanup)

    def cleanup(self) -> None:
        self.cleanups += 1
        self.on_cleanup()


Configured = tuple[FixtureServices, OfficialContextIRHostBinding]


@pytest.fixture
def configured() -> Iterator[Configured]:
    services = FixtureServices()
    binding = configure_official_context_ir_host(services.factory)
    try:
        yield services, binding
    finally:
        clear_official_context_ir_host(binding)


def execute(node: H3OfficialContextIRNode) -> tuple[str, ProviderReceipt, ProviderConsentNotice]:
    return node.execute(
        raw_request(),
        ratio="16:9",
        upload_consent=True,
        network_allowed=True,
        credential_reference="runtime.session_one",
    )


def test_registered_default_constructor_uses_configured_host_without_workflow_secrets(
    configured: Configured,
) -> None:
    services, _ = configured
    node = cast("H3OfficialContextIRNode", NODE_CLASS_MAPPINGS[OFFICIAL_CONTEXT_IR_NODE_ID]())
    assert (
        node.VALIDATE_INPUTS(
            raw_request(),
            ratio="16:9",
            upload_consent=True,
            network_allowed=True,
            credential_reference="runtime.session_one",
        )
        is True
    )
    assert services.acquisitions == services.resolutions == 0
    prompt, receipt, notice = execute(node)
    assert prompt == "fixture output" and receipt.is_successful and notice.consent_granted
    assert services.calls == [("create", "runtime.session_one"), ("query", "runtime.session_one")]
    assert services.resolutions == 3 and services.cleanups == 1
    assert "fixture-value" not in json.dumps(receipt.to_wire())
    assert "fixture-value" not in repr(
        OfficialContextIRHostServices(services, services, lambda: True, services.cleanup)
    )


def test_missing_binding_and_denied_policy_do_no_service_work(configured: Configured) -> None:
    services, binding = configured
    with pytest.raises((OfficialContextIRError, OfficialContextIRNodeError)):
        H3OfficialContextIRNode().execute(raw_request())
    assert services.acquisitions == 0 and not services.calls
    assert clear_official_context_ir_host(binding)
    assert not official_context_ir_host_configured()
    assert H3OfficialContextIRNode.VALIDATE_INPUTS(None) is not True
    with pytest.raises((OfficialContextIRError, OfficialContextIRNodeError)):
        execute(H3OfficialContextIRNode())
    assert services.acquisitions == 0 and not services.calls


@pytest.mark.parametrize("point", ["before", "create", "query", "cleanup"])
def test_authority_revocation_cannot_publish_success_and_cleans_once(
    configured: Configured, point: str
) -> None:
    services, _ = configured
    if point == "before":
        services.active = False
    if point == "create":
        services.after_create = lambda: setattr(services, "active", False)
    if point == "query":
        services.after_query = lambda: setattr(services, "active", False)
    if point == "cleanup":
        services.on_cleanup = lambda: setattr(services, "active", False)
    node = H3OfficialContextIRNode()
    with pytest.raises((OfficialContextIRError, OfficialContextIRNodeError)):
        execute(node)
    assert node.last_result is None and services.cleanups == 1
    assert len(services.calls) == {"before": 0, "create": 1, "query": 2, "cleanup": 2}[point]


@pytest.mark.parametrize("point", ["before", "between"])
def test_credential_revocation_is_rechecked_before_every_io(
    configured: Configured, point: str
) -> None:
    services, _ = configured
    if point == "before":
        services.available = False
    else:
        services.after_create = lambda: setattr(services, "available", False)
    with pytest.raises((OfficialContextIRError, OfficialContextIRNodeError)):
        execute(H3OfficialContextIRNode())
    assert len(services.calls) == (0 if point == "before" else 1)
    assert services.cleanups == 1


def test_cross_session_factory_denial_is_content_free_and_does_not_call_transport(
    configured: Configured,
) -> None:
    services, _ = configured
    with pytest.raises((OfficialContextIRError, OfficialContextIRNodeError)) as captured:
        H3OfficialContextIRNode().execute(
            raw_request(),
            ratio="16:9",
            upload_consent=True,
            network_allowed=True,
            credential_reference="runtime.session_two",
        )
    assert not services.calls and services.cleanups == 0
    assert "private session detail" not in str(captured.value)


def test_stale_clear_cannot_remove_replacement_or_keep_old_execution_alive(
    configured: Configured,
) -> None:
    services, old = configured
    replacement = FixtureServices()
    newer = configure_official_context_ir_host(replacement.factory)
    try:
        assert not clear_official_context_ir_host(old)
        assert official_context_ir_host_configured()
        execute(H3OfficialContextIRNode())
        assert not services.calls and len(replacement.calls) == 2
    finally:
        assert clear_official_context_ir_host(newer)


def test_factory_replacement_during_create_prevents_query(configured: Configured) -> None:
    services, _ = configured
    replacements: list[OfficialContextIRHostBinding] = []
    services.after_create = lambda: replacements.append(
        configure_official_context_ir_host(FixtureServices().factory)
    )
    try:
        with pytest.raises(OfficialContextIRError):
            execute(H3OfficialContextIRNode())
        assert len(services.calls) == 1 and services.cleanups == 1
    finally:
        for binding in replacements:
            clear_official_context_ir_host(binding)


def test_explicit_constructor_services_do_not_mix_with_host_factory(configured: Configured) -> None:
    ambient, _ = configured
    direct = FixtureServices()
    node = H3OfficialContextIRNode(transport=direct, resolver=direct)
    execute(node)
    assert not ambient.acquisitions and len(direct.calls) == 2
    assert direct.resolutions == 1 and direct.cleanups == 0


def test_cancellation_timeout_and_cleanup_error_leave_no_stale_result(
    configured: Configured,
) -> None:
    services, _ = configured

    class Cancelled:
        def is_cancelled(self) -> bool:
            return True

    node = H3OfficialContextIRNode(cancellation_probe=Cancelled())
    with pytest.raises(OfficialContextIRError):
        execute(node)
    assert not services.calls and services.cleanups == 1
    services.query_status = "processing"
    node = H3OfficialContextIRNode(
        lifecycle=replace(node._lifecycle, max_polls=1), sleep=lambda _: None
    )
    with pytest.raises(OfficialContextIRError):
        execute(node)
    assert node.last_result is None and services.cleanups == 2
    services.query_status = "succeeded"
    services.on_cleanup = lambda: (_ for _ in ()).throw(
        ValueError("fixture private cleanup detail")
    )
    node = H3OfficialContextIRNode()
    with pytest.raises((OfficialContextIRError, OfficialContextIRNodeError)) as captured:
        execute(node)
    assert node.last_result is None and "private cleanup detail" not in str(captured.value)


def test_mismatched_resolver_reference_refuses_before_transport_and_cleans(
    configured: Configured,
) -> None:
    services, _ = configured
    services.reference_override = "runtime.other"
    with pytest.raises(OfficialContextIRNodeError):
        execute(H3OfficialContextIRNode())
    assert not services.calls and services.cleanups == 1


def test_factory_callback_does_not_hold_global_configuration_lock(configured: Configured) -> None:
    import threading

    services, old = configured
    replacements: list[OfficialContextIRHostBinding] = []

    def factory(reference: str) -> OfficialContextIRHostServices:
        worker = threading.Thread(
            target=lambda: replacements.append(configure_official_context_ir_host(services.factory))
        )
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        return services.factory(reference)

    intermediate = configure_official_context_ir_host(factory)
    try:
        with pytest.raises(OfficialContextIRNodeError):
            execute(H3OfficialContextIRNode())
        assert not services.calls and services.cleanups == 1
        assert not clear_official_context_ir_host(old)
    finally:
        clear_official_context_ir_host(intermediate)
        for binding in replacements:
            clear_official_context_ir_host(binding)


def test_prior_success_cannot_survive_later_request_failure(configured: Configured) -> None:
    node = H3OfficialContextIRNode()
    execute(node)
    assert node.last_result is not None
    with pytest.raises(OfficialContextIRNodeError):
        node.execute(raw_request())
    assert node.last_result is None


def test_replacement_inside_authority_callback_refuses_before_io(configured: Configured) -> None:
    services, _ = configured
    replacements: list[OfficialContextIRHostBinding] = []

    def current() -> bool:
        if not replacements:
            replacements.append(configure_official_context_ir_host(FixtureServices().factory))
        return True

    binding = configure_official_context_ir_host(
        lambda reference: OfficialContextIRHostServices(
            services, services, current, services.cleanup
        )
    )
    try:
        with pytest.raises(OfficialContextIRNodeError):
            execute(H3OfficialContextIRNode())
        assert not services.calls and services.cleanups == 1
    finally:
        clear_official_context_ir_host(binding)
        for replacement in replacements:
            clear_official_context_ir_host(replacement)
