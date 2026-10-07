"""The actual planning owner behind the shared origin/body/registration boundary."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

# IMPORTANT: discovery uses top-level test modules; a tests. prefix duplicates mypy identities.
from test_m23_47_route_seam import _ReadableContent, _register_everything, _request
from test_m26_03_planning_service import action, environment, selectors

from comfyui_h3_context.adapters.composition_root import PRODUCTION_PLANNING, substituted
from comfyui_h3_context.adapters.production_planning_service import PRODUCTION_PLANNING_ROUTE


def test_actual_registered_route_traverses_owned_context_admission_proposal_import() -> None:
    service, sidebar, _, source, prepare, _ = environment()
    routes = _register_everything()
    (handler,) = [row.handler for row in routes if row.path == PRODUCTION_PLANNING_ROUTE]

    def send(kind: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        request = _request(["http://127.0.0.1:8188"])
        body = json.dumps(action(kind, payload, request_id)).encode()
        request.content_length = len(body)
        request.content = _ReadableContent(body)
        response = asyncio.run(handler(request))
        assert response.status == 200
        assert isinstance(response.body, dict)
        return cast(dict[str, Any], response.body)

    with substituted(PRODUCTION_PLANNING, service):
        prepared = send("prepare_context", prepare, "prepare")
        admitted = send(
            "admit_storyboard",
            {
                **selectors(prepared),
                "source_kind": "canonical_optimized_prompt",
                "typed_rows": [],
                "user_reviewed": False,
            },
            "admit",
        )
        proposed = send(
            "propose", {**selectors(admitted), "admission_id": admitted["admission_id"]}, "propose"
        )
        imported = send(
            "import_plan",
            {**selectors(proposed), "proposal_id": proposed["proposal"]["proposal_id"]},
            "import",
        )
    assert len(imported["segment_ids"]) == 2
    assert set(sidebar._entries) == {source.workspace_id}


@pytest.mark.parametrize(
    "origins", [[], ["http://foreign.invalid"], ["http://127.0.0.1:8188", "http://127.0.0.1:8188"]]
)
def test_planning_origin_refusal_precedes_body_or_service(origins: list[str]) -> None:
    (handler,) = [
        row.handler for row in _register_everything() if row.path == PRODUCTION_PLANNING_ROUTE
    ]
    response = asyncio.run(handler(_request(origins)))
    assert response.status == 403 and response.body is None
