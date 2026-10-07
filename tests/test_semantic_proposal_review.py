from __future__ import annotations

import importlib
import json
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

import comfyui_h3_context.adapters.comfyui_sidebar_workspace as sidebar_adapter
import comfyui_h3_context.product_shell_node as product_shell_node
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    SIDEBAR_ACTION_SCHEMA,
    SidebarWorkspaceRegistry,
    _ProposalReviewCoordination,
    decode_sidebar_action_json,
)
from comfyui_h3_context.core.errors import SidebarWorkspaceError
from comfyui_h3_context.core.semantic_proposal_producer import (
    SemanticProposalProducerError,
    SemanticProposalReviewAuthority,
    SemanticProposalReviewBundle,
    _begin_semantic_proposal_authority,
    _commit_semantic_proposal_authority,
    _SemanticProposalAuthorityOwner,
    claim_semantic_proposal_review_authority,
)
from comfyui_h3_context.core.semantic_proposal_review import (
    MAX_SEMANTIC_REVIEW_TEXT,
    SEMANTIC_PROPOSAL_ACTION_RESULT_SCHEMA,
    SEMANTIC_PROPOSAL_REVIEW_SCHEMA,
    SemanticProposalReviewError,
    SemanticProposalReviewGroup,
    SemanticProposalReviewItem,
)
from comfyui_h3_context.core.semantic_proposal_transaction import (
    SemanticProposalAction,
    SemanticProposalTransactionState,
)
from comfyui_h3_context.core.ui_projection import ExecutionCorrelation
from comfyui_h3_context.product_shell_node import (
    H3ContextProductShellNode,
    ProductShellNodeError,
)

_PRODUCER_FIXTURES = importlib.import_module(
    Path(__file__).with_name("test_semantic_proposal_producer.py").stem
)
_provider_setup = cast(Callable[[], Any], _PRODUCER_FIXTURES.__dict__["_provider_setup"])
_qualified_product = cast(
    Callable[[], tuple[Any, Any]],
    _PRODUCER_FIXTURES.__dict__["_qualified_product"],
)


def _local_review() -> tuple[
    _SemanticProposalAuthorityOwner,
    Any,
    Any,
    Any,
]:
    source, product = _qualified_product()
    owner = _SemanticProposalAuthorityOwner(clock=lambda: 100.0)
    reservation = owner.begin(
        source,
        _provider_setup(),
        "prompt.review.local",
        "node.semantic.local",
    )
    authority = owner.commit(reservation, product)
    return owner, authority, source, product


def _action(
    workspace: Any,
    *,
    kind: str,
    review_id: str,
    transaction_fingerprint: str,
    proposal_workspace_fingerprint: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    wire = workspace.to_wire()
    payload: dict[str, Any] = {
        "review_id": review_id,
        "expected_transaction_fingerprint": transaction_fingerprint,
        "expected_workspace_fingerprint": proposal_workspace_fingerprint,
    }
    if extra:
        payload.update(extra)
    return {
        "schema": SIDEBAR_ACTION_SCHEMA,
        "workspace_id": wire["workspace_id"],
        "expected_revision": wire["report_revision"],
        "expected_report_fingerprint": wire["report_fingerprint"],
        "action": kind,
        "payload": payload,
    }


def _published_review(
    *,
    coordination: _ProposalReviewCoordination | None = None,
) -> tuple[Any, Any, Any, SidebarWorkspaceRegistry, Any, Any]:
    owner, authority, source, product = _local_review()
    registry = SidebarWorkspaceRegistry(
        clock=lambda: 100.0,
        proposal_coordination=coordination or _ProposalReviewCoordination(clock=lambda: 100.0),
    )
    correlation = ExecutionCorrelation("prompt.review.shared", "node.review.shared")
    sidebar = registry.publish(source.report, source.wiring, correlation)
    with owner.claim(authority) as claim:
        handle = registry.publish_semantic_proposal_review(
            claim.bundle,
            source.wiring,
            correlation,
            sidebar,
        )
        claim.commit()
    return source, product, sidebar, registry, handle, correlation


def _proposal_dispatch(
    registry: SidebarWorkspaceRegistry,
    action: dict[str, Any],
) -> dict[str, Any]:
    result = registry.dispatch(action)
    assert type(result) is dict
    return cast(dict[str, Any], result)


def test_product_shell_claims_exact_authority_and_emits_content_free_handle() -> None:
    source, product = _qualified_product()
    reservation = _begin_semantic_proposal_authority(
        source,
        _provider_setup(),
        "prompt.product.review",
        "node.semantic.product.review",
    )
    authority = _commit_semantic_proposal_authority(reservation, product)

    emitted = cast(
        dict[str, Any],
        H3ContextProductShellNode().emit(
            source.report,
            source.wiring,
            prompt_id="prompt.product.shell",
            execution_node_id="node.product.shell",
            semantic_proposal_review_authority=authority,
        ),
    )

    handle = emitted["ui"]["semantic_proposal_review"][0]
    assert set(handle) == {
        "schema",
        "review_id",
        "transaction_fingerprint",
        "workspace_fingerprint",
        "report_fingerprint",
        "correlation",
        "available",
        "reason",
    }
    serialized = json.dumps(handle, sort_keys=True).lower()
    assert "warm morning light" not in serialized
    assert "candidate_graph" not in serialized
    with pytest.raises(SemanticProposalProducerError, match="review_authority_inactive"):
        with claim_semantic_proposal_review_authority(authority):
            pass


def test_product_shell_absence_is_compatible_and_forged_authority_rejects() -> None:
    source, _product = _qualified_product()
    emitted = cast(
        dict[str, Any],
        H3ContextProductShellNode().emit(
            source.report,
            source.wiring,
            prompt_id="prompt.product.absent",
            execution_node_id="node.product.absent",
        ),
    )
    assert "semantic_proposal_review" not in emitted["ui"]

    forged = object.__new__(SemanticProposalReviewAuthority)
    object.__setattr__(forged, "_lineage_id", "forged")
    object.__setattr__(forged, "_expires_at_ms", 1)
    object.__setattr__(forged, "_fingerprint", "sha256:" + "0" * 64)
    with pytest.raises(ProductShellNodeError, match="projection_failed"):
        H3ContextProductShellNode().emit(
            source.report,
            source.wiring,
            prompt_id="prompt.product.forged",
            execution_node_id="node.product.forged",
            semantic_proposal_review_authority=forged,
        )


def test_product_shell_publication_failure_aborts_predecessor_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, product = _qualified_product()
    reservation = _begin_semantic_proposal_authority(
        source,
        _provider_setup(),
        "prompt.product.rollback",
        "node.semantic.product.rollback",
    )
    authority = _commit_semantic_proposal_authority(reservation, product)

    def fail_publication(*_args: object) -> None:
        raise SidebarWorkspaceError("proposal_capacity", "synthetic publication failure")

    monkeypatch.setattr(
        product_shell_node,
        "stage_semantic_proposal_review",
        fail_publication,
    )
    with pytest.raises(ProductShellNodeError, match="projection_failed"):
        H3ContextProductShellNode().emit(
            source.report,
            source.wiring,
            prompt_id="prompt.product.rollback.shell",
            execution_node_id="node.product.rollback.shell",
            semantic_proposal_review_authority=authority,
        )
    with claim_semantic_proposal_review_authority(authority):
        pass


def test_product_shell_commit_publication_failure_keeps_predecessor_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, product = _qualified_product()
    reservation = _begin_semantic_proposal_authority(
        source,
        _provider_setup(),
        "prompt.product.commit.rollback",
        "node.semantic.product.commit.rollback",
    )
    authority = _commit_semantic_proposal_authority(reservation, product)

    def fail_commit(_publication: object) -> None:
        raise RuntimeError("synthetic private publication failure")

    monkeypatch.setattr(
        product_shell_node,
        "commit_semantic_proposal_review",
        fail_commit,
    )
    with pytest.raises(ProductShellNodeError, match="projection_failed") as captured:
        H3ContextProductShellNode().emit(
            source.report,
            source.wiring,
            prompt_id="prompt.product.commit.rollback.shell",
            execution_node_id="node.product.commit.rollback.shell",
            semantic_proposal_review_authority=authority,
        )
    assert "synthetic" not in str(captured.value)
    with claim_semantic_proposal_review_authority(authority):
        pass


def test_review_read_accept_and_duplicate_terminal_are_process_wide() -> None:
    owner, authority, source, product = _local_review()
    registry = SidebarWorkspaceRegistry(
        clock=lambda: 100.0,
        proposal_coordination=_ProposalReviewCoordination(clock=lambda: 100.0),
    )
    correlation = ExecutionCorrelation("prompt.sidebar.review", "node.product.review")
    sidebar = registry.publish(source.report, source.wiring, correlation)
    with owner.claim(authority) as claim:
        handle = registry.publish_semantic_proposal_review(
            claim.bundle,
            source.wiring,
            correlation,
            sidebar,
        )
        claim.commit()

    read = _proposal_dispatch(
        registry,
        _action(
            sidebar,
            kind="proposal_read",
            review_id=handle.review_id,
            transaction_fingerprint=product.transaction.fingerprint,
            proposal_workspace_fingerprint=source.workspace.fingerprint,
        ),
    )
    assert read["schema"] == SEMANTIC_PROPOSAL_ACTION_RESULT_SCHEMA
    assert read["outcome"] == "read"
    assert read["review"]["schema"] == SEMANTIC_PROPOSAL_REVIEW_SCHEMA
    assert read["review"]["state"] == "ready_for_review"
    assert read["review"]["actions"] == {
        "proposal_read": True,
        "proposal_resolve": False,
        "proposal_accept": True,
        "proposal_reject": True,
        "proposal_cancel": True,
        "edit": False,
        "regenerate": False,
    }
    item = read["review"]["groups"][0]["items"][0]
    assert set(item) == {
        "target_id",
        "summary",
        "change_kind",
        "reference_labels",
        "constraint_labels",
        "uncertainty_codes",
        "reason_code",
    }
    assert item["constraint_labels"] == ["scene:scene_1"]
    assert item["reason_code"] == "candidate_modified"

    accept_action = _action(
        sidebar,
        kind="proposal_accept",
        review_id=handle.review_id,
        transaction_fingerprint=product.transaction.fingerprint,
        proposal_workspace_fingerprint=source.workspace.fingerprint,
    )
    accepted = _proposal_dispatch(registry, accept_action)
    replay = _proposal_dispatch(registry, accept_action)
    assert accepted == replay
    assert accepted["outcome"] == "accepted"
    assert accepted["review"]["state"] == "accepted"
    assert accepted["review"]["workspace_revision"] == source.workspace.revision + 1


def test_proposal_action_decoder_is_closed_and_bounded() -> None:
    _owner, _authority, source, product = _local_review()
    registry = SidebarWorkspaceRegistry(
        clock=lambda: 100.0,
        proposal_coordination=_ProposalReviewCoordination(clock=lambda: 100.0),
    )
    sidebar = registry.publish(
        source.report,
        source.wiring,
        ExecutionCorrelation("prompt.decode.review", "node.decode.review"),
    )
    action = _action(
        sidebar,
        kind="proposal_resolve",
        review_id="review_" + "a" * 32,
        transaction_fingerprint=product.transaction.fingerprint,
        proposal_workspace_fingerprint=source.workspace.fingerprint,
        extra={"resolutions": ["bounded clarification"]},
    )
    encoded = json.dumps(action, separators=(",", ":")).encode("utf-8")
    assert decode_sidebar_action_json(encoded) == action

    for mutated in (
        {**action, "unknown": True},
        {**action, "action": "proposal_edit"},
        {
            **action,
            "payload": {**action["payload"], "resolutions": ["x"] * 33},
        },
        {
            **action,
            "payload": {**action["payload"], "resolutions": ["x" * 4097]},
        },
    ):
        with pytest.raises(ValueError):
            decode_sidebar_action_json(json.dumps(mutated, separators=(",", ":")).encode("utf-8"))


@pytest.mark.parametrize(
    ("kind", "outcome", "state"),
    (
        ("proposal_reject", "rejected", "rejected"),
        ("proposal_cancel", "cancelled", "cancelled"),
    ),
)
def test_reject_and_cancel_are_exact_terminal_transitions(
    kind: str,
    outcome: str,
    state: str,
) -> None:
    source, product, sidebar, registry, handle, _correlation = _published_review()
    action = _action(
        sidebar,
        kind=kind,
        review_id=handle.review_id,
        transaction_fingerprint=product.transaction.fingerprint,
        proposal_workspace_fingerprint=source.workspace.fingerprint,
    )
    result = _proposal_dispatch(registry, action)
    assert result["outcome"] == outcome
    assert result["review"]["state"] == state
    assert _proposal_dispatch(registry, action) == result


def test_resolution_installs_successor_alias_and_original_handle_can_reread() -> None:
    source, product = _qualified_product()
    transaction = replace(
        product.transaction,
        state=SemanticProposalTransactionState.CLARIFICATION_REQUIRED,
        last_action=SemanticProposalAction.CLARIFY,
        clarification_ids=("clarification.safe",),
        transaction_fingerprint=None,
    )
    coordination = _ProposalReviewCoordination(clock=lambda: 100.0)
    registry = SidebarWorkspaceRegistry(
        clock=lambda: 100.0,
        proposal_coordination=coordination,
    )
    correlation = ExecutionCorrelation("prompt.review.resolve", "node.review.resolve")
    sidebar = registry.publish(source.report, source.wiring, correlation)
    handle = registry.publish_semantic_proposal_review(
        SemanticProposalReviewBundle(source.report, source.workspace, transaction),
        source.wiring,
        correlation,
        sidebar,
    )
    clarification = _proposal_dispatch(
        registry,
        _action(
            sidebar,
            kind="proposal_read",
            review_id=handle.review_id,
            transaction_fingerprint=transaction.fingerprint,
            proposal_workspace_fingerprint=source.workspace.fingerprint,
        ),
    )["review"]
    assert clarification["clarifications"] == [
        {
            "clarification_id": "clarification.safe",
            "label": "clarification.safe",
            "reason_code": "resolution_required",
        }
    ]
    assert clarification["uncertainty_codes"] == ["clarification_required"]
    assert clarification["reason_code"] == "clarification_required"
    resolved = _proposal_dispatch(
        registry,
        _action(
            sidebar,
            kind="proposal_resolve",
            review_id=handle.review_id,
            transaction_fingerprint=transaction.fingerprint,
            proposal_workspace_fingerprint=source.workspace.fingerprint,
            extra={"resolutions": ["Synthetic bounded resolution"]},
        ),
    )
    assert resolved["outcome"] == "resolved"
    assert resolved["review"]["revision"] == transaction.revision
    assert resolved["review"]["transaction_fingerprint"] != transaction.fingerprint
    assert resolved["review"]["state"] == "ready_for_review"

    with pytest.raises(SidebarWorkspaceError, match="proposal authority changed"):
        _proposal_dispatch(
            registry,
            _action(
                sidebar,
                kind="proposal_accept",
                review_id=handle.review_id,
                transaction_fingerprint=transaction.fingerprint,
                proposal_workspace_fingerprint=source.workspace.fingerprint,
            ),
        )

    reread = _proposal_dispatch(
        registry,
        _action(
            sidebar,
            kind="proposal_read",
            review_id=handle.review_id,
            transaction_fingerprint=transaction.fingerprint,
            proposal_workspace_fingerprint=source.workspace.fingerprint,
        ),
    )
    assert (
        reread["review"]["transaction_fingerprint"] == resolved["review"]["transaction_fingerprint"]
    )


def test_shared_mutation_claim_rejects_concurrent_action_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, product, sidebar, registry, handle, _correlation = _published_review()
    action = _action(
        sidebar,
        kind="proposal_accept",
        review_id=handle.review_id,
        transaction_fingerprint=product.transaction.fingerprint,
        proposal_workspace_fingerprint=source.workspace.fingerprint,
    )
    entered = threading.Event()
    release = threading.Event()
    original = cast(
        Callable[..., Any],
        sidebar_adapter.__dict__["accept_semantic_proposal_transaction"],
    )

    def blocked_accept(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        sidebar_adapter,
        "accept_semantic_proposal_transaction",
        blocked_accept,
    )
    results: list[dict[str, Any]] = []
    failures: list[BaseException] = []

    def run_owner() -> None:
        try:
            results.append(_proposal_dispatch(registry, action))
        except BaseException as exc:  # pragma: no cover - assertion reports thread failure
            failures.append(exc)

    owner = threading.Thread(target=run_owner)
    owner.start()
    assert entered.wait(timeout=5)
    with pytest.raises(SidebarWorkspaceError, match="proposal review is busy"):
        _proposal_dispatch(registry, action)
    release.set()
    owner.join(timeout=5)
    assert not owner.is_alive()
    assert failures == []
    assert results[0]["outcome"] == "accepted"
    assert _proposal_dispatch(registry, action) == results[0]


def test_review_group_exact_item_limit_and_required_review_facts() -> None:
    items = tuple(
        SemanticProposalReviewItem(
            f"scene.{index}",
            "Synthetic safe summary",
            "modified",
            ("<Picture 1>",),
            (f"scene:scene.{index}",),
            ("clarification_required",),
            "candidate_modified",
        )
        for index in range(64)
    )
    group = SemanticProposalReviewGroup("scenes", items)
    assert len(cast(list[object], group.to_wire()["items"])) == 64
    assert cast(list[object], group.to_wire()["items"])[0] == {
        "target_id": "scene.0",
        "summary": "Synthetic safe summary",
        "change_kind": "modified",
        "reference_labels": ["<Picture 1>"],
        "constraint_labels": ["scene:scene.0"],
        "uncertainty_codes": ["clarification_required"],
        "reason_code": "candidate_modified",
    }
    with pytest.raises(SemanticProposalReviewError, match="review_group_items"):
        SemanticProposalReviewGroup(
            "scenes",
            items + (SemanticProposalReviewItem("scene.overflow", "Synthetic"),),
        )


def test_ttl_boundary_clears_content_without_resurrecting_lineage() -> None:
    now = [100.0]
    source, product = _qualified_product()
    coordination = _ProposalReviewCoordination(
        clock=lambda: now[0],
        ttl_seconds=900,
    )
    registry = SidebarWorkspaceRegistry(
        clock=lambda: now[0],
        ttl_seconds=86_400,
        proposal_coordination=coordination,
    )
    correlation = ExecutionCorrelation("prompt.review.ttl", "node.review.ttl")
    sidebar = registry.publish(source.report, source.wiring, correlation)
    bundle = SemanticProposalReviewBundle(source.report, source.workspace, product.transaction)
    handle = registry.publish_semantic_proposal_review(
        bundle,
        source.wiring,
        correlation,
        sidebar,
    )
    read = _action(
        sidebar,
        kind="proposal_read",
        review_id=handle.review_id,
        transaction_fingerprint=product.transaction.fingerprint,
        proposal_workspace_fingerprint=source.workspace.fingerprint,
    )
    now[0] = 999.999
    assert _proposal_dispatch(registry, read)["outcome"] == "read"
    now[0] = 1_899.999
    with pytest.raises(Exception, match="proposal review expired"):
        _proposal_dispatch(registry, read)
    with pytest.raises(Exception, match="proposal review expired"):
        registry.publish_semantic_proposal_review(
            bundle,
            source.wiring,
            correlation,
            sidebar,
        )
    entry = next(iter(coordination._entries.values()))
    assert entry.report is None
    assert entry.workspace is None
    assert entry.transaction is None
    assert entry.terminal_result is None


def test_ttl_clears_terminal_projection_but_preserves_terminal_ledger() -> None:
    now = [100.0]
    source, product = _qualified_product()
    coordination = _ProposalReviewCoordination(clock=lambda: now[0], ttl_seconds=900)
    registry = SidebarWorkspaceRegistry(
        clock=lambda: now[0],
        ttl_seconds=86_400,
        proposal_coordination=coordination,
    )
    correlation = ExecutionCorrelation("prompt.review.terminal", "node.review.terminal")
    sidebar = registry.publish(source.report, source.wiring, correlation)
    handle = registry.publish_semantic_proposal_review(
        SemanticProposalReviewBundle(source.report, source.workspace, product.transaction),
        source.wiring,
        correlation,
        sidebar,
    )
    action = _action(
        sidebar,
        kind="proposal_accept",
        review_id=handle.review_id,
        transaction_fingerprint=product.transaction.fingerprint,
        proposal_workspace_fingerprint=source.workspace.fingerprint,
    )
    assert _proposal_dispatch(registry, action)["outcome"] == "accepted"
    entry = next(iter(coordination._entries.values()))
    assert entry.terminal_action == "proposal_accept"
    assert entry.terminal_result is not None
    now[0] = 1_000.0
    with pytest.raises(Exception, match="proposal review expired"):
        _proposal_dispatch(registry, action)
    assert entry.state == "expired"
    assert entry.terminal_action == "proposal_accept"
    assert entry.terminal_result is None


def test_exact_lineage_capacity_and_duplicate_publication_are_process_wide() -> None:
    source, product = _qualified_product()
    coordination = _ProposalReviewCoordination(
        clock=lambda: 100.0,
        max_lineages=64,
    )
    first = SidebarWorkspaceRegistry(
        clock=lambda: 100.0,
        proposal_coordination=coordination,
    )
    second = SidebarWorkspaceRegistry(
        clock=lambda: 100.0,
        proposal_coordination=coordination,
    )
    correlation = ExecutionCorrelation("prompt.review.capacity", "node.review.capacity")
    sidebar = first.publish(source.report, source.wiring, correlation)
    first_handle = None
    for index in range(64):
        workspace = replace(
            source.workspace,
            workspace_id=f"workspace.capacity.{index}",
            workspace_fingerprint=None,
        )
        transaction = replace(
            product.transaction,
            transaction_id=f"transaction.capacity.{index}",
            workspace_id=workspace.workspace_id,
            workspace_fingerprint=workspace.fingerprint,
            transaction_fingerprint=None,
        )
        bundle = SemanticProposalReviewBundle(source.report, workspace, transaction)
        handle = first.publish_semantic_proposal_review(
            bundle,
            source.wiring,
            correlation,
            sidebar,
        )
        if index == 0:
            first_handle = handle
            assert (
                second.publish_semantic_proposal_review(
                    bundle,
                    source.wiring,
                    correlation,
                    sidebar,
                )
                is handle
            )
    assert first_handle is not None
    workspace = replace(
        source.workspace,
        workspace_id="workspace.capacity.overflow",
        workspace_fingerprint=None,
    )
    transaction = replace(
        product.transaction,
        transaction_id="transaction.capacity.overflow",
        workspace_id=workspace.workspace_id,
        workspace_fingerprint=workspace.fingerprint,
        transaction_fingerprint=None,
    )
    with pytest.raises(Exception, match="proposal capacity is exhausted"):
        second.publish_semantic_proposal_review(
            SemanticProposalReviewBundle(source.report, workspace, transaction),
            source.wiring,
            correlation,
            sidebar,
        )


def test_display_text_limits_and_sensitive_locator_fail_closed() -> None:
    assert SemanticProposalReviewItem("scene.safe", "x" * MAX_SEMANTIC_REVIEW_TEXT)
    for value in (
        "A quiet studio with soft light",
        "Scene: quiet studio with an A grade",
        "Relative motion stays natural.",
    ):
        assert SemanticProposalReviewItem("scene.safe", value)
    for value in (
        "x" * (MAX_SEMANTIC_REVIEW_TEXT + 1),
        "file:///private/media.png",
        r"\\server\private\media.png",
        r"C:\private\media.png",
        r"C:private\media.png",
        "/etc/passwd",
        "../private/media.png",
        r".\private\media.png",
        "s3://private-bucket/key",
        "private/media.png",
        r"private\media.png",
        "foo/bar",
        "mailto:user@example.invalid",
        "data:text/plain,private",
        "urn:example:private",
        "s3:bucket",
    ):
        with pytest.raises(SemanticProposalReviewError):
            SemanticProposalReviewItem("scene.safe", value)
    for value in ("private/media.png", "mailto:user@example.invalid"):
        with pytest.raises(SemanticProposalReviewError):
            SemanticProposalReviewItem(
                "scene.safe",
                "Synthetic summary",
                reference_labels=(value,),
            )
    with pytest.raises(SemanticProposalReviewError, match="review_constraint_label"):
        SemanticProposalReviewItem(
            "scene.safe",
            "Synthetic summary",
            constraint_labels=("mailto:user@example.invalid",),
        )


def test_publication_claim_rollback_cannot_delete_an_existing_lineage() -> None:
    source, product = _qualified_product()
    coordination = _ProposalReviewCoordination(clock=lambda: 100.0)
    registry = SidebarWorkspaceRegistry(
        clock=lambda: 100.0,
        proposal_coordination=coordination,
    )
    correlation = ExecutionCorrelation("prompt.review.stage", "node.review.stage")
    sidebar = registry.publish(source.report, source.wiring, correlation)
    bundle = SemanticProposalReviewBundle(source.report, source.workspace, product.transaction)

    staged = registry.stage_semantic_proposal_review(
        bundle,
        source.wiring,
        correlation,
        sidebar,
    )
    with pytest.raises(KeyError):
        _proposal_dispatch(
            registry,
            _action(
                sidebar,
                kind="proposal_read",
                review_id=staged.handle.review_id,
                transaction_fingerprint=product.transaction.fingerprint,
                proposal_workspace_fingerprint=source.workspace.fingerprint,
            ),
        )
    registry.rollback_semantic_proposal_review(staged)
    assert coordination._entries == {}

    committed = registry.publish_semantic_proposal_review(
        bundle,
        source.wiring,
        correlation,
        sidebar,
    )
    duplicate = registry.stage_semantic_proposal_review(
        bundle,
        source.wiring,
        correlation,
        sidebar,
    )
    assert duplicate.handle is committed
    registry.rollback_semantic_proposal_review(duplicate)
    assert (
        _proposal_dispatch(
            registry,
            _action(
                sidebar,
                kind="proposal_read",
                review_id=committed.review_id,
                transaction_fingerprint=product.transaction.fingerprint,
                proposal_workspace_fingerprint=source.workspace.fingerprint,
            ),
        )["outcome"]
        == "read"
    )
