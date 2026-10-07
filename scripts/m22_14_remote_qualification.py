"""Historical sealed-v5 compatibility tool.

Current model-free connection qualification uses prompt_model_qualification.py. These legacy
results cannot qualify or activate v6 connections.

M22-14 exact remote-provider qualification, with hermetic and separately authorized live modes.

``hermetic`` executes the product session against package-owned fixtures and proves the route,
payload, parser, consent and content-free receipt without DNS or network access. ``live`` uses the
same request through the exact curated policy, but only after the caller repeats that policy's
fingerprint and transmission bound. Legacy billing fields/flags are inert. The API key is read
from a masked prompt into ``RuntimeCredential``;
it is never accepted from argv, an environment variable, a file or the output receipt.

Neither mode edits the catalog. A hermetic PASS is not provider qualification. A live PASS is only
evidence for a later reviewed catalog promotion and cannot activate a profile by itself.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.adapters.prompt_model_transport import (  # noqa: E402
    RemoteExchangeMetrics,
    RemoteHttpsExchange,
    run_remote_prompt_model_session,
)
from comfyui_h3_context.core.prompt_model_budget import (  # noqa: E402
    ContextProfileLadder,
    ContextProfileStep,
    PromptModelWorkload,
    plan_prompt_model_request,
)
from comfyui_h3_context.core.prompt_model_provider import (  # noqa: E402
    REMOTE_QUALIFICATION_RECEIPT_SCHEMA,
    REMOTE_QUALIFICATION_RESULT_SCHEMA,
    PromptModelFamily,
    PromptModelOutcomeId,
    PromptModelQualificationState,
    RemotePromptModelQualificationEvidence,
    admit_egress_destination,
)
from comfyui_h3_context.core.prompt_model_provider import (  # noqa: E402
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.prompt_model_provider import (  # noqa: E402
    load_legacy_prompt_model_catalog as load_prompt_model_catalog,
)
from comfyui_h3_context.core.prompt_model_session import (  # noqa: E402
    OLLAMA_DRAFT_SCHEMA_ID,
    PromptModelMessage,
    PromptModelRole,
    PromptModelSessionRequest,
)
from comfyui_h3_context.core.provider_setup import ProviderConsentStatus  # noqa: E402
from comfyui_h3_context.core.remote_prompt_model import (  # noqa: E402
    RemoteConsentRecord,
    RemoteUsageReceipt,
    RuntimeCredential,
)
from comfyui_h3_context.core.remote_provider_policy import (  # noqa: E402
    RemoteProviderPolicy,
    policy_for_profile,
)

QUALIFICATION_SCHEMA = REMOTE_QUALIFICATION_RESULT_SCHEMA
PROFILE_IDS = (
    "openai.gpt_5_6_terra.remote",
    "gemini.gemini_3_7_flash.remote",
)
_SYSTEM = (
    "Return one JSON object with schema h3.prompt_model.draft_json.v1 and prompt_text. "
    "Do not add prose or keys."
)
_USER = "Rewrite this synthetic phrase without adding facts: quiet street, static camera."
_LADDER = ContextProfileLadder(
    steps=(ContextProfileStep(context_tokens=8_192, memory_bytes=1024**3),)
)

ExchangeFactory = Callable[[PromptModelProfile, RuntimeCredential], object]
CredentialReader = Callable[[str], str]


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"m22_14_remote_qualification: {message}\n")
    raise SystemExit(2)


def _profile(profile_id: str, *, dialect_revision: int = 1) -> PromptModelProfile:
    profiles = {
        item.profile_id: item
        for item in load_prompt_model_catalog().profiles
        if item.family is PromptModelFamily.REMOTE_OPENAI_COMPATIBLE
    }
    profile = profiles.get(profile_id)
    if profile is None or tuple(profiles) != PROFILE_IDS:
        _fail("the curated remote catalog inventory does not match this tool")
    if not isinstance(profile, PromptModelProfile):
        _fail("historical profile contract is invalid")
    profile = replace(
        profile,
        qualification_state=PromptModelQualificationState.CATALOG_ONLY,
        qualification_evidence=None,
    )
    policy_for_profile(profile)
    if dialect_revision == 2:
        # IMPORTANT: old qualification cannot certify the new adapter/parser or action ceiling.
        # Candidate execution is explicit and never promotes or rewrites the shipped catalog.
        profile = replace(
            profile,
            adapter_version="1.1.0",
            parser_version="h3.prompt_model.draft_json.v2",
            max_calls_per_action=4,
            qualification_state=PromptModelQualificationState.CATALOG_ONLY,
            qualification_evidence=None,
        )
    elif dialect_revision != 1:
        _fail("dialect revision is invalid")
    policy_for_profile(profile)
    return profile


def _request(profile: PromptModelProfile) -> PromptModelSessionRequest:
    decision = plan_prompt_model_request(
        capabilities=profile.capabilities,
        ladder=_LADDER,
        workload=PromptModelWorkload(
            characters=len(_SYSTEM) + len(_USER),
            wide_characters=0,
            messages=2,
            visual_inputs=0,
            request_bytes=2_048,
            requested_output_tokens=128,
        ),
        requested_step=0,
        available_memory_bytes=8 * 1024**3,
    )
    if decision.plan is None:
        _fail("the fixed synthetic request did not fit the curated budget")
    return PromptModelSessionRequest(
        profile=profile,
        destination=admit_egress_destination(profile.family, profile.endpoint),
        plan=decision.plan,
        messages=(
            PromptModelMessage(role=PromptModelRole.SYSTEM, text=_SYSTEM),
            PromptModelMessage(role=PromptModelRole.USER, text=_USER),
        ),
    )


# M22-17. What each live service was observed to answer with on its listing route, recorded from the
# retained M22-14 qualification artifacts and the providers' own published interfaces. This is
# deliberately NOT derived from the adapter's `_CATALOG_IDENTIFIER_PREFIXES`: a fixture that returns
# whatever the comparison happens to search for asserts the assumption under test instead of
# exercising it, and that is exactly how the Gemini catalog spelling reached a live run undetected.
_OBSERVED_CATALOG_IDENTIFIERS: Mapping[str, str] = {
    "openai.gpt_5_6_terra.remote": "{model_id}",
    "gemini.gemini_3_7_flash.remote": "models/{model_id}",
    "anthropic.claude_sonnet_4_6.remote": "{model_id}",
}


def _observed_catalog_identifier(profile: object) -> str:
    """The exact string this provider's listing route puts in a row's `id`."""

    template = _OBSERVED_CATALOG_IDENTIFIERS.get(profile.profile_id, "{model_id}")  # type: ignore[attr-defined]
    return template.format(model_id=profile.model_id)  # type: ignore[attr-defined]


class _HermeticExchange:
    """The two official-shape fixtures. It has no socket-owning member or callback."""

    def __init__(self, profile: PromptModelProfile, policy: RemoteProviderPolicy) -> None:
        self.profile = profile
        self.policy = policy
        self.calls: list[tuple[str, str]] = []
        self.metrics: RemoteExchangeMetrics | None = None

    @property
    def transmission_count(self) -> int:
        return len(self.calls)

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        del timeout_seconds
        self.calls.append((method, path))
        if (method, path, payload) == ("GET", self.policy.discovery_route, None):
            return {"data": [{"id": _observed_catalog_identifier(self.profile)}]}
        if method != "POST" or path != self.policy.chat_route or not isinstance(payload, Mapping):
            _fail("hermetic fixture received an unrecognized request")
        self.metrics = RemoteExchangeMetrics(
            http_status=200,
            request_bytes=len(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            response_bytes=256,
            prompt_tokens=96,
            completion_tokens=32,
            usage_present=True,
        )
        return {
            "model": self.profile.model_id,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "schema": OLLAMA_DRAFT_SCHEMA_ID,
                                "prompt_text": "quiet street, static camera",
                            },
                            separators=(",", ":"),
                        ),
                    },
                }
            ],
            "usage": {"prompt_tokens": 96, "completion_tokens": 32},
        }


def _answer_is_closed_draft(text: str) -> bool:
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return (
        type(decoded) is dict
        and set(decoded) == {"schema", "prompt_text"}
        and decoded.get("schema") == OLLAMA_DRAFT_SCHEMA_ID
        and type(decoded.get("prompt_text")) is str
    )


def _receipt_is_qualification_evidence(
    receipt: object,
    profile: PromptModelProfile,
    policy: RemoteProviderPolicy,
) -> bool:
    if not isinstance(receipt, RemoteUsageReceipt):
        return False
    destination = admit_egress_destination(profile.family, profile.endpoint)
    # IMPORTANT: billing fields and optional usage are telemetry, not qualification authority.
    # Requiring positive costs would reject every current product receipt after billing removal.
    return (
        receipt.profile_id == profile.profile_id
        and receipt.host == destination.host
        and receipt.outcome_id is PromptModelOutcomeId.OK
        and receipt.http_status == 200
        and receipt.request_bytes > 0
        and receipt.response_bytes > 0
        and receipt.credential_last_four == ""
        and receipt.provider_id == policy.provider_id
        and receipt.model_id == profile.model_id
        and receipt.policy_sha256 == policy.fingerprint
    )


def _qualification_receipt_wire(receipt: RemoteUsageReceipt | None) -> dict[str, object] | None:
    """Project only content-free counts and policy identity into the durable evidence file."""

    if receipt is None:
        return None
    return {
        "schema": REMOTE_QUALIFICATION_RECEIPT_SCHEMA,
        "profile_id": receipt.profile_id,
        "outcome_id": receipt.outcome_id.value,
        "http_status": receipt.http_status,
        "request_bytes": receipt.request_bytes,
        "response_bytes": receipt.response_bytes,
        "prompt_tokens": receipt.prompt_tokens,
        "completion_tokens": receipt.completion_tokens,
        "duration_ms": receipt.duration_ms,
        "provider_id": receipt.provider_id,
        "model_id": receipt.model_id,
        "policy_sha256": receipt.policy_sha256,
        "price_basis_id": "",
        "maximum_cost_micro_usd": 0,
        "actual_cost_micro_usd": 0,
        "usage_present": receipt.usage_present,
    }


def basis_for(
    *,
    outcome_id: str,
    schema_valid: bool,
    receipt_valid: bool,
    transmissions: object,
    max_transmissions: int,
    receipt: Mapping[str, object] | None,
) -> str | None:
    """The single acceptance rule, applied to a fresh run and to a retained observation alike.

    M22-18. It is one function with two callers on purpose. A re-judgement that carried its own
    copy of these conditions could drift from what a live run would decide, and then a promotion
    would rest on a rule nothing else in the repository applies.
    """

    # Strict on type as well as value. A loose comparison accepts `2.0` from a hand-edited
    # artifact and reports PASS for something `build_remote_qualification_evidence` still refuses,
    # which tells a human the opposite of what the real gate would say.
    if (
        type(max_transmissions) is not int
        or max_transmissions not in {2, 4}
        or type(transmissions) is not int
        or not 2 <= transmissions <= max_transmissions
    ):
        return None
    if outcome_id == PromptModelOutcomeId.OK.value and schema_valid and receipt_valid:
        return "completion"
    sent = None if receipt is None else receipt.get("request_bytes")
    answered = None if receipt is None else receipt.get("response_bytes")
    if (
        outcome_id == PromptModelOutcomeId.QUOTA.value
        and type(sent) is int
        and type(answered) is int
        and sent >= 1
        and answered >= 1
    ):
        return "reachability"
    return None


def rejudge(values: Mapping[str, object]) -> dict[str, object]:
    """Re-decide a retained artifact under the current rule, without re-observing anything.

    The observation is a fact about the day it was made and does not change by being repeated.
    What changes is the rule applied to it, so an artifact whose status says FAIL because it was
    judged under the older, stricter bar is a good observation carrying a stale verdict. Only the
    verdict fields are recomputed here; every recorded observation is carried through untouched.
    """

    if values.get("schema") != QUALIFICATION_SCHEMA:
        _fail("re-judged artifact is not a remote qualification result")
    if values.get("mode") != "live":
        _fail("only a live observation may be re-judged")
    receipt = values.get("receipt")
    basis = basis_for(
        outcome_id=str(values.get("outcome_id")),
        schema_valid=values.get("schema_valid") is True,
        receipt_valid=values.get("receipt_valid") is True,
        transmissions=values.get("observed_transmissions"),
        max_transmissions=int(str(values.get("max_transmissions"))),
        receipt=receipt if isinstance(receipt, Mapping) else None,
    )
    return {**values, "status": "PASS" if basis is not None else "FAIL", "basis": basis}


def _run(
    profile: PromptModelProfile,
    *,
    mode: str,
    observed_on: date,
    repository_commit: str | None,
    repository_tree: str | None,
    credential: RuntimeCredential,
    exchange_factory: ExchangeFactory | None,
) -> dict[str, object]:
    policy = policy_for_profile(profile)
    if exchange_factory is not None:
        exchange = exchange_factory(profile, credential)
    elif mode == "hermetic":
        exchange = _HermeticExchange(profile, policy)
    else:
        exchange = RemoteHttpsExchange(
            admit_egress_destination(profile.family, profile.endpoint),
            credential,
            policy=policy,
            timeout_seconds=profile.request_timeout_seconds,
        )
    consent = RemoteConsentRecord(
        profile_id=profile.profile_id,
        status=ProviderConsentStatus.GRANTED,
        network_permitted=True,
        media_upload_consented=False,
    )
    result = run_remote_prompt_model_session(
        _request(profile),
        exchange,
        selected_profile_id=profile.profile_id,
        consent=consent,
        credential=credential,
        timeout_seconds=profile.request_timeout_seconds,
        on_date=observed_on,
    )
    schema_valid = result.answer is not None and _answer_is_closed_draft(result.answer.text)
    receipt_valid = _receipt_is_qualification_evidence(result.receipt, profile, policy)
    transmissions = getattr(exchange, "transmission_count", None)
    receipt = _qualification_receipt_wire(result.receipt)
    # M22-18. Two bars, and the artifact records which one was met rather than leaving a reader to
    # infer it. The completion bar is unchanged. A reachability acceptance additionally requires
    # that a content request was actually sent and answered -- both transmissions spent and bytes
    # on the wire in both directions -- so a run that fails at discovery cannot reach it.
    basis = basis_for(
        outcome_id=result.outcome.outcome_id.value,
        schema_valid=schema_valid,
        receipt_valid=receipt_valid,
        transmissions=transmissions,
        max_transmissions=policy.max_transmissions,
        receipt=receipt,
    )
    accepted = basis is not None
    original = load_prompt_model_catalog().require(profile.profile_id)
    historical = original.qualification_evidence
    if not isinstance(historical, RemotePromptModelQualificationEvidence):
        _fail("historical qualification contract is invalid")
    return {
        "schema": QUALIFICATION_SCHEMA,
        "mode": mode,
        "status": "PASS" if accepted else "FAIL",
        "provider_id": policy.provider_id,
        "profile_id": profile.profile_id,
        "model_id": profile.model_id,
        "adapter_version": profile.adapter_version,
        "parser_version": profile.parser_version,
        "policy_version": policy.policy_version,
        "policy_sha256": policy.fingerprint,
        "price_basis_id": historical.price_basis_id,
        "price_checked_on": historical.source_checked_on,
        "price_valid_through": historical.price_valid_through,
        "observed_on": observed_on.isoformat(),
        "repository_commit": repository_commit,
        "repository_tree": repository_tree,
        "max_transmissions": policy.max_transmissions,
        "observed_transmissions": transmissions,
        "max_input_tokens": historical.max_input_tokens,
        "max_output_tokens": historical.max_output_tokens,
        "max_cost_micro_usd": historical.max_cost_micro_usd,
        "outcome_id": result.outcome.outcome_id.value,
        "schema_valid": schema_valid,
        "receipt_valid": receipt_valid,
        "receipt": receipt,
        "catalog_promotion_performed": False,
        "basis": basis,
    }


def _write(path: Path, payload: dict[str, object]) -> None:
    if path.exists():
        _fail("the output path already exists; qualification evidence is never overwritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    exchange_factory: ExchangeFactory | None = None,
    credential_reader: CredentialReader = getpass.getpass,
) -> int:
    parser = argparse.ArgumentParser(
        description="Hermetically verify or separately authorize one exact curated remote profile."
    )
    parser.add_argument("--mode", required=True, choices=("hermetic", "live", "rejudge"))
    parser.add_argument("--profile", required=True, choices=PROFILE_IDS)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dialect-revision", type=int, choices=(1, 2), default=1)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--authorize-policy-sha256")
    parser.add_argument("--authorize-max-transmissions", type=int)
    parser.add_argument(
        "--authorize-max-cost-micro-usd",
        type=int,
        help="Deprecated compatibility option; ignored (no repository billing gate).",
    )
    parser.add_argument("--candidate-commit")
    parser.add_argument("--candidate-tree")
    arguments = parser.parse_args(argv)

    profile = _profile(arguments.profile, dialect_revision=arguments.dialect_revision)
    policy = policy_for_profile(profile)
    observed_on = date.today()
    if arguments.mode == "rejudge":
        if arguments.source is None:
            _fail("rejudge mode requires --source naming a retained live artifact")
        if any(
            value is not None
            for value in (
                arguments.authorize_policy_sha256,
                arguments.authorize_max_transmissions,
                arguments.candidate_commit,
                arguments.candidate_tree,
            )
        ):
            _fail("rejudge mode contacts no provider and takes no live authorization")
        source = Path(arguments.source)
        retained = json.loads(source.read_bytes().decode("utf-8"))
        if not isinstance(retained, dict):
            _fail("re-judged artifact is not an object")
        if retained.get("profile_id") != profile.profile_id:
            _fail("re-judged artifact belongs to a different profile")
        payload = rejudge(retained)
        _write(arguments.output, payload)
        sys.stdout.write(
            json.dumps(
                {
                    "mode": "rejudge",
                    "profile_id": payload["profile_id"],
                    "status": payload["status"],
                    "basis": payload["basis"],
                    "observed_on": payload["observed_on"],
                    "source_sha256": sha256(source.read_bytes()).hexdigest(),
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        return 0 if payload["status"] == "PASS" else 1
    if arguments.mode == "live":
        if (
            arguments.authorize_policy_sha256 != policy.fingerprint
            or arguments.authorize_max_transmissions != policy.max_transmissions
            or not isinstance(arguments.candidate_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", arguments.candidate_commit) is None
            or not isinstance(arguments.candidate_tree, str)
            or re.fullmatch(r"[0-9a-f]{40}", arguments.candidate_tree) is None
        ):
            _fail(
                "live mode requires the exact current policy fingerprint, transmission bound and "
                "candidate commit/tree"
            )
        secret = credential_reader(f"API key for {policy.provider_id} (input hidden): ")
    else:
        if any(
            value is not None
            for value in (
                arguments.authorize_policy_sha256,
                arguments.authorize_max_transmissions,
                arguments.candidate_commit,
                arguments.candidate_tree,
            )
        ):
            _fail("live authorization flags are invalid in hermetic mode")
        # A public profile id is sufficient for the type gate because the hermetic exchange never
        # constructs an authorization header. It avoids inventing secret-shaped fixture data.
        secret = profile.profile_id

    payload = _run(
        profile,
        mode=arguments.mode,
        observed_on=observed_on,
        repository_commit=arguments.candidate_commit,
        repository_tree=arguments.candidate_tree,
        credential=RuntimeCredential(secret),
        exchange_factory=exchange_factory,
    )
    _write(arguments.output, payload)
    sys.stdout.write(
        json.dumps(
            {
                "mode": payload["mode"],
                "profile_id": payload["profile_id"],
                "status": payload["status"],
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
