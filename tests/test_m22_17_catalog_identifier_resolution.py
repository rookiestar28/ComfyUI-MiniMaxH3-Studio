"""M22-17. A provider's catalog spelling of a model is not always its request spelling.

Google's OpenAI-compatibility surface takes the bare `gemini-3.7-flash` on its chat route and
answers `models/gemini-3.7-flash` on its listing route. Comparing the two by exact string equality
reported a published model as missing: the retained live receipt shows HTTP 200 with 8339 response
bytes on the discovery route and zero request bytes afterwards, so the session ended before it
asked the service for anything. These tests pin the join and, just as importantly, pin that it is
scoped to the declared Gemini compatibility and native listing routes.
"""

from __future__ import annotations

import unittest
from collections.abc import Mapping

from comfyui_h3_context.adapters.prompt_model_transport import (
    _CATALOG_IDENTIFIER_PREFIXES,
    PromptModelTransportError,
    request_spelling,
    resolve_live_identity,
)
from comfyui_h3_context.adapters.provider_readiness import _census
from comfyui_h3_context.core.prompt_model_provider import (
    PromptModelContractError,
    PromptModelFamily,
    PromptModelOutcomeId,
)
from comfyui_h3_context.core.prompt_model_session import (
    DiscoveryCandidate,
    DiscoveryRejection,
    LiveModelIdentity,
)

GEMINI_ROUTE = "/v1beta/openai/models"
OPENAI_ROUTE = "/v1/models"
GEMINI_MODEL = "gemini-3.7-flash"
OPENAI_MODEL = "gpt-5.6-terra"


class _ListingExchange:
    """The narrowest stub `resolve_live_identity` accepts: one GET that returns one listing."""

    def __init__(self, rows: object) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str) -> Mapping[str, object]:
        self.calls.append((method, path))
        return {"data": self.rows}


def _resolve(
    rows: object,
    *,
    model_id: str = GEMINI_MODEL,
    route: str = GEMINI_ROUTE,
    family: PromptModelFamily = PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
) -> LiveModelIdentity | None:
    return resolve_live_identity(family, model_id, _ListingExchange(rows), discovery_route=route)


class CatalogIdentifierResolutionTests(unittest.TestCase):
    def test_the_native_resource_name_resolves_to_the_request_identifier(self) -> None:
        identity = _resolve([{"id": f"models/{GEMINI_MODEL}"}])
        assert identity is not None
        self.assertEqual(GEMINI_MODEL, identity.model_id)
        # A remote catalog publishes no weight digest, so the weaker alias basis is preserved.
        self.assertIsNone(identity.digest)

    def test_a_bare_row_on_the_same_route_still_resolves(self) -> None:
        identity = _resolve([{"id": GEMINI_MODEL}])
        assert identity is not None
        self.assertEqual(GEMINI_MODEL, identity.model_id)

    def test_the_model_is_found_inside_a_full_catalog(self) -> None:
        rows = [{"id": f"models/{name}"} for name in ("gemini-2.5-flash", "gemini-1.5-pro-latest")]
        rows.insert(1, {"id": f"models/{GEMINI_MODEL}"})
        identity = _resolve(rows)
        assert identity is not None
        self.assertEqual(GEMINI_MODEL, identity.model_id)

    def test_an_absent_model_is_still_reported_missing(self) -> None:
        self.assertIsNone(_resolve([{"id": "models/gemini-2.5-flash"}]))

    def test_a_route_with_no_declared_prefix_keeps_exact_equality(self) -> None:
        # SECURITY: the join must not become a blanket prefix strip. An OpenAI listing that answered
        # with a Gemini-shaped name would be a different model, not the selected one.
        self.assertIsNone(
            _resolve([{"id": f"models/{OPENAI_MODEL}"}], model_id=OPENAI_MODEL, route=OPENAI_ROUTE)
        )
        identity = _resolve([{"id": OPENAI_MODEL}], model_id=OPENAI_MODEL, route=OPENAI_ROUTE)
        assert identity is not None
        self.assertEqual(OPENAI_MODEL, identity.model_id)

    def test_two_spellings_of_one_model_fail_closed(self) -> None:
        with self.assertRaises(PromptModelTransportError) as caught:
            _resolve([{"id": GEMINI_MODEL}, {"id": f"models/{GEMINI_MODEL}"}])
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)

    def test_a_listing_that_is_not_a_list_is_refused(self) -> None:
        with self.assertRaises(PromptModelTransportError) as caught:
            _resolve({"id": GEMINI_MODEL})
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)

    def test_the_anthropic_branch_does_not_inherit_the_prefix(self) -> None:
        self.assertIsNone(
            _resolve(
                [{"id": "models/claude-sonnet-4-6"}],
                model_id="claude-sonnet-4-6",
                route=OPENAI_ROUTE,
                family=PromptModelFamily.REMOTE_ANTHROPIC,
            )
        )

    def test_the_prefix_map_is_closed_to_the_two_gemini_catalog_routes(self) -> None:
        # IMPORTANT: the native entry includes its bounded page-size query; keep the exact map
        # rather than allowing arbitrary routes to normalize another provider's identifiers.
        self.assertEqual(
            {GEMINI_ROUTE: "models/", "/v1beta/models?pageSize=1000": "models/"},
            dict(_CATALOG_IDENTIFIER_PREFIXES),
        )

    def test_only_the_discovery_route_is_called(self) -> None:
        exchange = _ListingExchange([{"id": f"models/{GEMINI_MODEL}"}])
        resolve_live_identity(
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
            GEMINI_MODEL,
            exchange,
            discovery_route=GEMINI_ROUTE,
        )
        self.assertEqual([("GET", GEMINI_ROUTE)], exchange.calls)


class RequestSpellingTests(unittest.TestCase):
    def test_native_catalog_normalization_requires_the_exact_bounded_route(self) -> None:
        identifier = f"models/{GEMINI_MODEL}"
        self.assertEqual(GEMINI_MODEL, request_spelling(identifier, "/v1beta/models?pageSize=1000"))
        for route in (
            "/v1beta/models",
            "/v1beta/models?pageSize=999",
            "/v1beta/models?pageSize=1000&extra=1",
            OPENAI_ROUTE,
            None,
        ):
            with self.subTest(route=route):
                self.assertEqual(identifier, request_spelling(identifier, route))

    def test_a_declared_prefix_is_collapsed_and_nothing_else_is(self) -> None:
        self.assertEqual(GEMINI_MODEL, request_spelling(f"models/{GEMINI_MODEL}", GEMINI_ROUTE))
        self.assertEqual(GEMINI_MODEL, request_spelling(GEMINI_MODEL, GEMINI_ROUTE))
        self.assertEqual(
            f"models/{OPENAI_MODEL}", request_spelling(f"models/{OPENAI_MODEL}", OPENAI_ROUTE)
        )
        self.assertEqual(f"models/{GEMINI_MODEL}", request_spelling(f"models/{GEMINI_MODEL}", None))


class ReadinessCensusJoinTests(unittest.TestCase):
    """The census is the second reader of the same listing, and it judges by the same spelling."""

    def _judge(
        self, rows: list[dict[str, str]], route: str | None
    ) -> tuple[DiscoveryCandidate, ...]:
        # Deliberately the whole tuple, never a dict keyed by identifier. Two rows can carry one
        # identifier, and a dict silently keeps only the last verdict -- which is exactly why an
        # earlier version of this test could not fail while one of two duplicates was still
        # ADMITTED.
        return _census(
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
            {"data": rows},
            frozenset({GEMINI_MODEL}),
            discovery_route=route,
        )

    def _reasons(
        self, candidates: tuple[DiscoveryCandidate, ...], identifier: str
    ) -> set[DiscoveryRejection]:
        return {candidate.reason for candidate in candidates if candidate.identifier == identifier}

    def test_the_pinned_model_is_admitted_when_the_listing_prefixes_it(self) -> None:
        judged = self._judge(
            [{"id": "models/gemini-2.5-flash"}, {"id": f"models/{GEMINI_MODEL}"}], GEMINI_ROUTE
        )
        self.assertEqual({DiscoveryRejection.ADMITTED}, self._reasons(judged, GEMINI_MODEL))
        self.assertEqual({DiscoveryRejection.UNPINNED}, self._reasons(judged, "gemini-2.5-flash"))

    def test_without_the_join_the_same_listing_reports_the_model_as_unpinned(self) -> None:
        # The pre-M22-17 behavior, kept as an explicit statement of what the defect looked like.
        judged = self._judge([{"id": f"models/{GEMINI_MODEL}"}], None)
        self.assertEqual(
            {DiscoveryRejection.UNPINNED}, self._reasons(judged, f"models/{GEMINI_MODEL}")
        )

    def test_every_occurrence_of_a_repeated_identity_is_refused(self) -> None:
        for rows in (
            [{"id": GEMINI_MODEL}, {"id": f"models/{GEMINI_MODEL}"}],
            [{"id": f"models/{GEMINI_MODEL}"}, {"id": GEMINI_MODEL}],
        ):
            with self.subTest(first=rows[0]["id"]):
                judged = self._judge(rows, GEMINI_ROUTE)
                self.assertEqual(2, len(judged))
                # Not "at least one is ambiguous". Neither row may be admitted, in either order.
                self.assertEqual(
                    {DiscoveryRejection.AMBIGUOUS_FOLDER}, self._reasons(judged, GEMINI_MODEL)
                )

    def test_a_row_is_still_validated_in_the_spelling_it_arrived_in(self) -> None:
        # SECURITY: the join runs after validation, never before, so a traversal attempt cannot be
        # laundered into an acceptable identifier by stripping a declared prefix.
        with self.assertRaises(PromptModelContractError):
            self._judge([{"id": "models/../etc/passwd"}], GEMINI_ROUTE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
