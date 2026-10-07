"""M22-20. A provider that answered is reachable, whatever its answer turned out to be.

`probe_provider_readiness` funnels every `PromptModelTransportError` from `resolve_live_identity`
into `_failed`, which means `reachable=False` -- "could not be reached". That is right for an error
raised before any response arrived and wrong for every error that function actually raises, because
each of them is a judgement about a body that was received and parsed.

The registered symptom was one family: `REMOTE_ANTHROPIC` carries a row ceiling inside
`resolve_live_identity`, so an oversized listing was refused there and reported unreachable, while
every other family had no ceiling in that function, fell through to the census, and was reported
reachable. One listing, two diagnostics, and the provider answered in both cases.

These cases pin the fixed rule from both sides: received-then-refused answers, and never-answered
fails, with the discriminator itself under test so a refactor cannot swap it for a proxy.
"""

from __future__ import annotations

import unittest

from historical_prompt_model_fixtures import (
    load_historical_direct_call_catalog as load_prompt_model_catalog,
)
from test_provider_readiness import ListingExchange, profile

from comfyui_h3_context.adapters.prompt_model_transport import PromptModelTransportError
from comfyui_h3_context.adapters.provider_readiness import probe_provider_readiness
from comfyui_h3_context.core.prompt_model_provider import (
    MAX_DISCOVERY_ROWS,
    PromptModelFamily,
    PromptModelOutcomeId,
)
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.provider_settings import ReadinessObservation

#: One row per family that reaches `resolve_live_identity`, so a claim about "every family" is
#: actually checked against every family rather than against the one that was reported.
REMOTE_PROFILE_IDS = (
    "anthropic.claude_sonnet_4_6.remote",
    "openai.gpt_5_6_terra.remote",
    "gemini.gemini_3_7_flash.remote",
)


def remote_profiles() -> tuple[PromptModelProfile, ...]:
    catalog = load_prompt_model_catalog()
    return tuple(catalog.require(profile_id) for profile_id in REMOTE_PROFILE_IDS)


def probe(target: PromptModelProfile, listing: object) -> ReadinessObservation:
    exchange = ListingExchange(listing)
    return probe_provider_readiness(
        target, exchange_factory=lambda _destination, _credential: exchange
    )


def shape(observation: ReadinessObservation) -> tuple[object, ...]:
    """Everything a caller can see, so "identical" means identical and not merely similar."""

    outcome = observation.outcome
    return (
        observation.reachable,
        None if outcome is None else outcome.outcome_id,
        None if outcome is None else outcome.parameters,
        observation.candidates,
        observation.identity,
    )


#: One row past the single ceiling every reader of one provider listing now shares.
ROWS_PAST_CEILING = MAX_DISCOVERY_ROWS + 1


#: Listings a provider can send that `resolve_live_identity` refuses after reading them. Each is
#: refused by the Anthropic branch specifically, which is the branch that used to report the
#: refusal as an unreachable provider.
def oversized() -> dict[str, object]:
    return {"data": [{"id": f"model-{index}"} for index in range(ROWS_PAST_CEILING)]}


def unreadable_row() -> dict[str, object]:
    return {"data": [{"id": "model-a"}, {"id": 17}]}


def duplicated(model_id: str) -> dict[str, object]:
    return {"data": [{"id": model_id}, {"id": model_id}]}


class AnAnsweredListingIsReachableTests(unittest.TestCase):
    def test_an_oversized_listing_is_answered_by_every_remote_family(self) -> None:
        for target in remote_profiles():
            with self.subTest(profile=target.profile_id, family=target.family.value):
                observation = probe(target, oversized())
                self.assertTrue(observation.reachable)
                assert observation.outcome is not None
                self.assertIs(
                    observation.outcome.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE
                )
                self.assertEqual((), observation.candidates)
                self.assertIsNone(observation.identity)

    def test_every_remote_family_reports_an_oversized_listing_identically(self) -> None:
        # AC-01, and the point of the item. Before the fix the Anthropic row differed from the
        # other two on `reachable` alone, which is exactly the sort of divergence a per-family
        # assertion misses and a cross-family comparison cannot.
        observed = {
            target.profile_id: shape(probe(target, oversized())) for target in remote_profiles()
        }
        self.assertEqual(1, len(set(observed.values())), observed)

    def test_an_unreadable_row_is_answered_identically_too(self) -> None:
        # The row ceiling was the registered symptom, not the whole defect. The Anthropic branch
        # refuses a listing holding one unreadable row as well, from the same raise, so it took the
        # same wrong path.
        observed = {
            target.profile_id: shape(probe(target, unreadable_row()))
            for target in remote_profiles()
        }
        self.assertEqual(1, len(set(observed.values())), observed)
        for target_id, seen in observed.items():
            with self.subTest(profile=target_id):
                self.assertIs(True, seen[0])

    def test_a_duplicate_identity_listing_is_answered_rather_than_unreachable(self) -> None:
        # Both branches raise here, for the same fail-closed reason, and both used to report a
        # provider that answered twice over as one that could not be reached at all.
        for target in remote_profiles():
            with self.subTest(profile=target.profile_id):
                observation = probe(target, duplicated(target.model_id))
                self.assertTrue(observation.reachable)
                assert observation.outcome is not None
                self.assertIs(
                    observation.outcome.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE
                )
                self.assertIsNone(observation.identity)

    def test_a_listing_whose_data_member_is_not_a_list_is_answered(self) -> None:
        for target in remote_profiles():
            for body in ({"data": "not-a-list"}, {"data": {"id": "model-a"}}, {}):
                with self.subTest(profile=target.profile_id, body=body):
                    observation = probe(target, body)
                    self.assertTrue(observation.reachable)
                    assert observation.outcome is not None
                    self.assertIs(
                        observation.outcome.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE
                    )

    def test_the_local_family_answers_from_the_same_rule(self) -> None:
        # Ollama raises from the same function for its own reasons -- a `models` member that is not
        # a list, and a digest that will not normalize -- so it is covered here rather than assumed.
        for body in (
            {"models": "not-a-list"},
            {},
            {"models": [{"name": "model-a", "digest": "not-a-digest"}]},
        ):
            with self.subTest(body=body):
                observation = probe(profile(), body)
                self.assertTrue(observation.reachable)
                assert observation.outcome is not None
                self.assertIs(
                    observation.outcome.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE
                )
                self.assertIsNone(observation.identity)

    def test_a_loopback_listing_past_the_ceiling_matches_the_remote_families(self) -> None:
        observation = probe(profile(PromptModelFamily.LOOPBACK_SERVER), oversized())
        remote = probe(remote_profiles()[0], oversized())
        self.assertEqual(shape(remote), shape(observation))

    def test_an_oversized_local_listing_matches_the_remote_families_too(self) -> None:
        # Found by the delta review. The cases above reach the ceiling by two different routes and
        # the local family takes the second one: `resolve_live_identity` has no row ceiling in its
        # Ollama branch at all, so an oversized but otherwise valid tag list returns from it
        # cleanly and is refused later, by `_census`. Asserting the equivalence for the family that
        # exercises the other route is the difference between checking the claim and restating it.
        local = probe(profile(), {"models": [{"name": f"m-{i}"} for i in range(ROWS_PAST_CEILING)]})
        remote = probe(remote_profiles()[0], oversized())
        self.assertEqual(shape(remote), shape(local))
        self.assertTrue(local.reachable)

    def test_a_listing_that_is_both_oversized_and_unreadable_is_answered_identically(self) -> None:
        # Also from the review. The Anthropic branch combines the two refusals in one `or`, so the
        # length decides first and the combination cannot differ from either half -- which is an
        # argument, and this is the check. A future edit that splits that condition, or reorders it
        # so an unreadable row is judged before the length, has to keep the answer the same.
        both: dict[str, object] = {
            "data": [{"id": f"model-{index}"} for index in range(ROWS_PAST_CEILING - 1)]
            + [{"id": 17}]
        }
        observed = {target.profile_id: shape(probe(target, both)) for target in remote_profiles()}
        observed["local"] = shape(
            probe(
                profile(),
                {
                    "models": [{"name": f"m-{i}"} for i in range(ROWS_PAST_CEILING - 1)]
                    + [{"name": 17}]
                },
            )
        )
        self.assertEqual(1, len(set(observed.values())), observed)
        self.assertEqual(shape(probe(remote_profiles()[0], oversized())), observed["local"])


class ANeverAnsweredProviderIsStillUnreachableTests(unittest.TestCase):
    """AC-02. The fix must not turn a silent host into a reachable one."""

    def test_an_exchange_that_raises_before_answering_is_unreachable(self) -> None:
        # `ListingExchange(failure=...)` raises inside `request`, so the recorder exists and has
        # recorded nothing. That is the state the discriminator is reading.
        for outcome_id in (
            PromptModelOutcomeId.BACKEND_ABSENT,
            PromptModelOutcomeId.TIMEOUT,
            PromptModelOutcomeId.AUTHENTICATION,
            PromptModelOutcomeId.QUOTA,
            PromptModelOutcomeId.TRANSPORT,
            PromptModelOutcomeId.MALFORMED_RESPONSE,
            PromptModelOutcomeId.PROVIDER_ERROR,
        ):
            for target in (profile(), *remote_profiles()):
                with self.subTest(outcome_id=outcome_id, profile=target.profile_id):
                    exchange = ListingExchange(failure=outcome_id)
                    observation = probe_provider_readiness(
                        target,
                        exchange_factory=lambda _destination, _credential, e=exchange: e,
                    )
                    self.assertFalse(observation.reachable)
                    assert observation.outcome is not None
                    self.assertIs(observation.outcome.outcome_id, outcome_id)

    def test_a_failure_before_the_recorder_exists_is_unreachable(self) -> None:
        # The recorder is bound before the block that can fail precisely so this path can be
        # answered rather than raising `UnboundLocalError` out of the handler. A factory that
        # raises fails earlier than any of the cases above.
        def factory(_destination: object, _credential: object) -> object:
            raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "")

        for target in (profile(), *remote_profiles()):
            with self.subTest(profile=target.profile_id):
                observation = probe_provider_readiness(target, exchange_factory=factory)
                self.assertFalse(observation.reachable)
                assert observation.outcome is not None
                self.assertIs(observation.outcome.outcome_id, PromptModelOutcomeId.TRANSPORT)

    def test_the_discriminator_is_the_recorded_answer_and_not_the_outcome_id(self) -> None:
        # One outcome id, two truths. `MALFORMED_RESPONSE` raised by the exchange means no answer
        # was received; raised after a listing came back it means the answer was refused. Keying
        # the handler on the outcome id would collapse these two into one, so they are asserted
        # side by side.
        target = remote_profiles()[0]

        never_answered = probe_provider_readiness(
            target,
            exchange_factory=lambda _destination, _credential: ListingExchange(
                failure=PromptModelOutcomeId.MALFORMED_RESPONSE
            ),
        )
        answered_badly = probe(target, oversized())

        self.assertFalse(never_answered.reachable)
        self.assertTrue(answered_badly.reachable)
        assert never_answered.outcome is not None and answered_badly.outcome is not None
        self.assertIs(never_answered.outcome.outcome_id, answered_badly.outcome.outcome_id)


class TheRefusedListingIsNeverSelectionAuthorityTests(unittest.TestCase):
    def test_a_refused_listing_contributes_no_candidates(self) -> None:
        # AC-03. Becoming reachable must not smuggle in a census of the very listing the identity
        # reader refused. A caller that reads candidates would otherwise be handed rows that were
        # never judged trustworthy.
        for target in remote_profiles():
            for body in (oversized(), unreadable_row(), duplicated(target.model_id)):
                with self.subTest(profile=target.profile_id):
                    observation = probe(target, body)
                    self.assertEqual((), observation.candidates)
                    self.assertIsNone(observation.identity)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
