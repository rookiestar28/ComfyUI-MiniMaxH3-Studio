"""M22-19. A hosted model catalogue is bigger than a local Ollama tag list.

The readiness census refused any listing past 64 rows, which is a sensible population for a handful
of pulled local models and simply false for a hosted catalogue: the retained M22-14 Gemini discovery
response was 8339 bytes, on the order of a hundred and fifty rows. No remote profile had ever
reached the product's readiness control before M22-17, so nothing had exercised the ceiling.

The second test class here is the one that matters most. A draft of this item proposed decoupling
the projection window from the ingest ceiling so that raising one could not move the other. That
would have re-opened the defect that
`test_an_exact_candidate_after_the_old_ui_window_remains_selectable` was written for -- rows
accepted into session state that the surface never shows, with the selected identity able to hide
among them. These tests pin the derivation so the next reader cannot make that mistake quietly.
"""

from __future__ import annotations

import unittest
from collections.abc import Mapping

from comfyui_h3_context.adapters.prompt_model_transport import (
    PromptModelTransportError,
    resolve_live_identity,
)
from comfyui_h3_context.adapters.provider_readiness import _census
from comfyui_h3_context.core.prompt_model_provider import (
    MAX_DISCOVERY_ROWS,
    PromptModelContractError,
    PromptModelFamily,
)
from comfyui_h3_context.core.prompt_model_session import (
    DiscoveryCandidate,
    DiscoveryRejection,
    LiveModelIdentity,
)
from comfyui_h3_context.core.provider_settings import (
    MAX_PROJECTED_CANDIDATES,
    ProviderSettingsState,
    ReadinessObservation,
)

GEMINI_ROUTE = "/v1beta/openai/models"
PINNED = "gemini-3.7-flash"
PINNED_ANTHROPIC = "claude-sonnet-4-6"

#: Past the ceiling this item replaces, comfortably inside the one it installs. A real hosted
#: catalogue sits in this gap, which is the whole point of the change.
HOSTED_CATALOGUE_ROWS = 200


def _hosted_listing(rows: int, *, include_pinned: bool = True) -> Mapping[str, object]:
    entries: list[dict[str, str]] = [
        {"id": f"models/gemini-filler-{index}"} for index in range(rows)
    ]
    if include_pinned:
        entries[rows // 2] = {"id": f"models/{PINNED}"}
    return {"data": entries}


class _ListingExchange:
    def __init__(self, listing: Mapping[str, object]) -> None:
        self.listing = listing

    def request(self, method: str, path: str) -> Mapping[str, object]:
        del method, path
        return self.listing


def _judge(rows: int, *, include_pinned: bool = True) -> tuple[DiscoveryCandidate, ...]:
    return _census(
        PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        _hosted_listing(rows, include_pinned=include_pinned),
        frozenset({PINNED}),
        discovery_route=GEMINI_ROUTE,
    )


class HostedCatalogueCensusTests(unittest.TestCase):
    def test_a_hosted_catalogue_past_the_old_ceiling_is_censused(self) -> None:
        # The item's regression proof, stated as the user-visible fact rather than as a constant:
        # a catalogue of this size used to be refused whole, so the model could not be found in it.
        self.assertGreater(HOSTED_CATALOGUE_ROWS, 64)
        judged = _judge(HOSTED_CATALOGUE_ROWS)
        self.assertEqual(HOSTED_CATALOGUE_ROWS, len(judged))
        admitted = [c.identifier for c in judged if c.reason is DiscoveryRejection.ADMITTED]
        self.assertEqual([PINNED], admitted)

    def test_the_ceiling_still_refuses_a_listing_past_it(self) -> None:
        with self.assertRaisesRegex(PromptModelContractError, "readiness_census"):
            _judge(MAX_DISCOVERY_ROWS + 1)

    def test_a_listing_exactly_at_the_ceiling_is_accepted(self) -> None:
        judged = _judge(MAX_DISCOVERY_ROWS)
        self.assertEqual(MAX_DISCOVERY_ROWS, len(judged))

    def test_an_absent_model_in_a_large_catalogue_is_still_not_admitted(self) -> None:
        judged = _judge(HOSTED_CATALOGUE_ROWS, include_pinned=False)
        self.assertEqual(HOSTED_CATALOGUE_ROWS, len(judged))
        self.assertNotIn(DiscoveryRejection.ADMITTED, {c.reason for c in judged})


class ProjectionWindowTracksIngestTests(unittest.TestCase):
    """SECURITY: the projection window may never be smaller than what ingest accepted."""

    def test_the_projection_window_is_not_smaller_than_the_ingest_ceiling(self) -> None:
        # A window smaller than the ceiling lets a row enter session state that the surface never
        # shows, and the selected identity or a duplicate can hide there. Replacing the derivation
        # with a literal is the specific mistake this pins against.
        self.assertGreaterEqual(MAX_PROJECTED_CANDIDATES, MAX_DISCOVERY_ROWS)

    def test_raising_the_ingest_ceiling_carries_the_projection_window_with_it(self) -> None:
        self.assertEqual(MAX_PROJECTED_CANDIDATES, MAX_DISCOVERY_ROWS)


class OneListingOneCeilingTests(unittest.TestCase):
    """SECURITY: both readers of a single provider listing must stop at the same row.

    `probe_provider_readiness` calls `resolve_live_identity` before `_census`, so a lower ceiling in
    the resolver is the one that decides and the census ceiling is never consulted. A separate
    `MAX_REMOTE_MODEL_ROWS = 256` lived in the transport and applied to the Anthropic branch alone,
    which kept exactly this item's outage alive for that family while the census was raised to 512.
    """

    @staticmethod
    def _resolve(rows: int) -> LiveModelIdentity | None:
        entries = [{"id": f"claude-filler-{index}"} for index in range(rows)]
        entries[rows // 2] = {"id": PINNED_ANTHROPIC}
        return resolve_live_identity(
            PromptModelFamily.REMOTE_ANTHROPIC,
            PINNED_ANTHROPIC,
            _ListingExchange({"data": entries}),
        )

    def test_a_catalogue_past_the_old_transport_ceiling_resolves(self) -> None:
        # 300 is past the removed 256 and inside the census ceiling. This must fail against the
        # pre-change transport, which refused it as a malformed response.
        self.assertGreater(300, 256)
        identity = self._resolve(300)
        assert identity is not None
        self.assertEqual(PINNED_ANTHROPIC, identity.model_id)

    def test_the_resolver_stops_at_the_same_row_as_the_census(self) -> None:
        self.assertIsNotNone(self._resolve(MAX_DISCOVERY_ROWS))
        with self.assertRaises(PromptModelTransportError):
            self._resolve(MAX_DISCOVERY_ROWS + 1)


class IngestSitesAcceptTheirFullQuotaTests(unittest.TestCase):
    """Every ingest site must accept exactly the ceiling, not one row short of it.

    The suite only ever proved the refusal side, at `MAX_DISCOVERY_ROWS + 1`. A ceiling that also
    refuses *at* the ceiling is off by one in the direction this item exists to fix, and it would
    not have shown: mutating `>` to `>=` at either site below left the whole focused set green.
    """

    @staticmethod
    def _quota() -> tuple[DiscoveryCandidate, ...]:
        return tuple(
            DiscoveryCandidate(identifier=f"candidate.{index}", reason=DiscoveryRejection.UNPINNED)
            for index in range(MAX_DISCOVERY_ROWS)
        )

    def test_an_observation_carrying_the_full_quota_is_accepted(self) -> None:
        observation = ReadinessObservation(reachable=True, candidates=self._quota())
        self.assertEqual(MAX_DISCOVERY_ROWS, len(observation.candidates))

    def test_the_full_quota_is_recorded_and_wholly_projected(self) -> None:
        # The derivation, proven end to end rather than by comparing two constants: what ingest
        # accepts is exactly what the surface is handed, with nothing dropped and no truncation.
        subject = ProviderSettingsState(profiles=())
        subject.record_candidates(self._quota())
        projection = subject.project()
        self.assertEqual(MAX_DISCOVERY_ROWS, len(projection.candidates))
        self.assertFalse(projection.candidates_truncated)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
