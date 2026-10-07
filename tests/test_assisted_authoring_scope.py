"""Opt-in assisted-authoring product truth is explicit and never defaulted."""

from __future__ import annotations

from dataclasses import replace

import pytest

from comfyui_h3_context.core.assisted_authoring_scope import (
    AssistedAuthoringScopeError,
    AssistedAuthoringState,
)


def test_available_catalog_is_not_selection_readiness_or_action_authority() -> None:
    state = AssistedAuthoringState(
        available=True,
        selected=False,
        ready=False,
        authorized_for_this_action=False,
        defaulted=False,
    )

    assert state.to_wire() == {
        "available": True,
        "selected": False,
        "ready": False,
        "authorized_for_this_action": False,
        "defaulted": False,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"available": False, "selected": True},
        {"selected": False, "ready": True},
        {"ready": False, "authorized_for_this_action": True},
        {"defaulted": True},
    ],
)
def test_impossible_or_defaulted_state_is_rejected(changes: dict[str, bool]) -> None:
    baseline = AssistedAuthoringState(
        available=True,
        selected=True,
        ready=True,
        authorized_for_this_action=True,
        defaulted=False,
    )

    with pytest.raises(AssistedAuthoringScopeError):
        replace(baseline, **changes)
