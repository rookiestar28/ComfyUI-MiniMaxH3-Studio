"""Historical sealed-v5 compatibility tool.

Current model-free connection qualification uses prompt_model_qualification.py. These legacy
results cannot qualify or activate v6 connections.

M22-15 native Anthropic qualification with hermetic and separately authorized live modes.

Hermetic mode exercises the exact Models/Messages request, parser, consent and receipt path
against in-process fixtures. Live mode uses the same product path only after the caller repeats the
exact policy fingerprint, call/token bounds and candidate Git identities. Legacy billing fields
and flags are inert. The API key is
accepted only through a masked prompt and never from argv, environment, files or evidence.

Neither mode edits the catalog. A hermetic PASS is not live qualification, and a live PASS remains
evidence for a later reviewed catalog promotion rather than promotion authority by itself.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date
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
PROFILE_ID = "anthropic.claude_sonnet_4_6.remote"
MODEL_ID = "claude-sonnet-4-6"
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
CandidateIdentityReader = Callable[[], tuple[str, str]]


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"m22_15_anthropic_qualification: {message}\n")
    raise SystemExit(2)


def _current_candidate_identity() -> tuple[str, str]:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0 or status.stdout:
        _fail("live mode requires a clean committed candidate")

    values: list[str] = []
    for revision in ("HEAD", "HEAD^{tree}"):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", revision],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        value = result.stdout.strip().lower()
        if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
            _fail("the current candidate Git identity could not be verified")
        values.append(value)
    return values[0], values[1]


def _profile(*, dialect_revision: int = 1) -> PromptModelProfile:
    matching = tuple(
        item
        for item in load_prompt_model_catalog().profiles
        if item.family is PromptModelFamily.REMOTE_ANTHROPIC
    )
    if len(matching) != 1 or matching[0].profile_id != PROFILE_ID:
        _fail("the curated Anthropic catalog inventory does not match this tool")
    profile = matching[0]
    if not isinstance(profile, PromptModelProfile):
        _fail("historical profile contract is invalid")
    profile = replace(
        profile,
        qualification_state=PromptModelQualificationState.CATALOG_ONLY,
        qualification_evidence=None,
    )
    policy_for_profile(profile)
    if dialect_revision == 2:
        # IMPORTANT: candidate qualification must not borrow the historical profile's live claim.
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


class _HermeticExchange:
    """Exact native Models/Messages fixtures with no socket-owning member or callback."""

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
            return {
                "data": [{"id": self.profile.model_id, "display_name": "fixture"}],
                "first_id": self.profile.model_id,
                "last_id": self.profile.model_id,
                "has_more": False,
            }
        if method != "POST" or path != self.policy.chat_route or not isinstance(payload, Mapping):
            _fail("hermetic fixture received an unrecognized request")
        if (
            payload.get("model") != self.profile.model_id
            or payload.get("system") != _SYSTEM
            or payload.get("stream") is not False
            or payload.get("max_tokens") != 128
            or set(payload)
            != {"model", "system", "messages", "max_tokens", "stream", "output_config"}
        ):
            _fail("hermetic fixture received a drifted Messages payload")
        answer = json.dumps(
            {
                "schema": OLLAMA_DRAFT_SCHEMA_ID,
                "prompt_text": "quiet street, static camera",
            },
            separators=(",", ":"),
        )
        self.metrics = RemoteExchangeMetrics(
            http_status=200,
            request_bytes=len(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            response_bytes=256,
            prompt_tokens=96,
            completion_tokens=32,
            usage_present=True,
        )
        return {
            "id": "msg_fixture",
            "type": "message",
            "role": "assistant",
            "model": self.profile.model_id,
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": answer}],
            "usage": {"input_tokens": 96, "output_tokens": 32},
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


def _receipt_wire(receipt: RemoteUsageReceipt | None) -> dict[str, object] | None:
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
    if mode not in {"hermetic", "live"}:
        _fail("qualification mode is invalid")
    if mode == "live" and exchange_factory is not None:
        # SECURITY: an injected fixture can prove protocol behavior, never a live provider call.
        _fail("live mode forbids an injected exchange")
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
    accepted = (
        result.outcome.outcome_id is PromptModelOutcomeId.OK
        and schema_valid
        and receipt_valid
        and type(transmissions) is int
        and 2 <= transmissions <= policy.max_transmissions
    )
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
        "receipt": _receipt_wire(result.receipt),
        "catalog_promotion_performed": False,
    }


def _write(path: Path, payload: dict[str, object]) -> None:
    if path.exists():
        _fail("the output path already exists; qualification evidence is never overwritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    )
    try:
        # SECURITY: the preflight is user-friendly, but only exclusive creation closes the race.
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
    except FileExistsError:
        _fail("the output path already exists; qualification evidence is never overwritten")


def main(
    argv: Sequence[str] | None = None,
    *,
    exchange_factory: ExchangeFactory | None = None,
    credential_reader: CredentialReader = getpass.getpass,
    candidate_identity_reader: CandidateIdentityReader = _current_candidate_identity,
) -> int:
    parser = argparse.ArgumentParser(
        description="Hermetically verify or separately authorize exact native Anthropic."
    )
    parser.add_argument("--mode", required=True, choices=("hermetic", "live"))
    parser.add_argument("--profile", required=True, choices=(PROFILE_ID,))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dialect-revision", type=int, choices=(1, 2), default=1)
    parser.add_argument("--authorize-policy-sha256")
    parser.add_argument("--authorize-max-transmissions", type=int)
    parser.add_argument("--authorize-max-input-tokens", type=int)
    parser.add_argument("--authorize-max-output-tokens", type=int)
    parser.add_argument(
        "--authorize-max-cost-micro-usd",
        type=int,
        help="Deprecated compatibility option; ignored (no repository billing gate).",
    )
    parser.add_argument("--candidate-commit")
    parser.add_argument("--candidate-tree")
    arguments = parser.parse_args(argv)
    if arguments.output.exists():
        # IMPORTANT: discover an unusable evidence target before asking for a credential or
        # spending either transmission. `_write` repeats this check against a later race.
        _fail("the output path already exists; qualification evidence is never overwritten")
    if arguments.mode == "live" and exchange_factory is not None:
        _fail("live mode forbids an injected exchange")

    profile = _profile(dialect_revision=arguments.dialect_revision)
    policy = policy_for_profile(profile)
    historical = load_prompt_model_catalog().require(profile.profile_id).qualification_evidence
    if not isinstance(historical, RemotePromptModelQualificationEvidence):
        _fail("historical qualification contract is invalid")
    observed_on = date.today()
    authority_values = (
        arguments.authorize_policy_sha256,
        arguments.authorize_max_transmissions,
        arguments.authorize_max_input_tokens,
        arguments.authorize_max_output_tokens,
        arguments.candidate_commit,
        arguments.candidate_tree,
    )
    if arguments.mode == "live":
        if (
            arguments.authorize_policy_sha256 != policy.fingerprint
            or arguments.authorize_max_transmissions != policy.max_transmissions
            or arguments.authorize_max_input_tokens != historical.max_input_tokens
            or arguments.authorize_max_output_tokens != historical.max_output_tokens
            or not isinstance(arguments.candidate_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", arguments.candidate_commit) is None
            or not isinstance(arguments.candidate_tree, str)
            or re.fullmatch(r"[0-9a-f]{40}", arguments.candidate_tree) is None
        ):
            _fail(
                "live mode requires the exact current policy fingerprint, call/token "
                "bounds and candidate commit/tree"
            )
        candidate_identity = candidate_identity_reader()
        if (
            type(candidate_identity) is not tuple
            or len(candidate_identity) != 2
            or any(re.fullmatch(r"[0-9a-f]{40}", value) is None for value in candidate_identity)
            or candidate_identity != (arguments.candidate_commit, arguments.candidate_tree)
        ):
            _fail("authorized commit/tree do not match the clean checked-out candidate")
        secret = credential_reader("API key for Anthropic (input hidden): ")
    else:
        if any(value is not None for value in authority_values):
            _fail("live authorization flags are invalid in hermetic mode")
        # Public identity satisfies the credential type gate; the hermetic exchange constructs no
        # header and owns no socket, so no secret-shaped fixture is invented.
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
