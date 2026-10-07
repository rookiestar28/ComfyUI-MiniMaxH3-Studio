"""Current v6 connection qualification through the product's chosen-model session.

Hermetic mode owns no socket and cannot mint live evidence. Live mode reads a masked key only
after fixed policy, send ceiling, clean candidate and exclusive output guards. Neither mode edits
the catalogue. Model ids are explicit inputs and are checked against the provider before chat.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
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
    ModelChoice,
    PromptModelContractError,
    PromptModelFamily,
    PromptModelProfile,
    admit_egress_destination,
    load_prompt_model_catalog,
)
from comfyui_h3_context.core.prompt_model_session import (  # noqa: E402
    OLLAMA_DRAFT_SCHEMA_ID,
    PromptModelMessage,
    PromptModelRole,
    PromptModelSessionRequest,
)
from comfyui_h3_context.core.provider_connection_qualification import (  # noqa: E402
    CONNECTION_QUALIFICATION_RECEIPT_SCHEMA,
    CONNECTION_QUALIFICATION_SCHEMA,
    connection_observation_basis,
)
from comfyui_h3_context.core.provider_setup import ProviderConsentStatus  # noqa: E402
from comfyui_h3_context.core.remote_prompt_model import (  # noqa: E402
    RemoteConsentRecord,
    RuntimeCredential,
)
from comfyui_h3_context.core.remote_provider_policy import policy_for_profile  # noqa: E402

_SYSTEM = (
    "Return only JSON with schema h3.prompt_model.draft_json.v1 and a nonempty prompt_text string."
)
_USER = "Rewrite this synthetic phrase without adding facts: quiet street, static camera."
ExchangeFactory = Callable[[PromptModelProfile, ModelChoice, RuntimeCredential], object]
CandidateIdentityReader = Callable[[], tuple[str, str]]


def _fail(message: str) -> NoReturn:
    sys.stderr.write("prompt_model_qualification: " + message + "\n")
    raise SystemExit(2)


def _current_candidate_identity() -> tuple[str, str]:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if status.returncode != 0 or status.stdout:
        _fail("live mode requires a clean committed candidate")
    identities: list[str] = []
    for ref in ("HEAD", "HEAD^{tree}"):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        value = result.stdout.strip()
        if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
            _fail("candidate identity is unavailable")
        identities.append(value)
    return identities[0], identities[1]


def _request(profile: PromptModelProfile, model: ModelChoice) -> PromptModelSessionRequest:
    decision = plan_prompt_model_request(
        capabilities=profile.capabilities,
        ladder=ContextProfileLadder(
            steps=(ContextProfileStep(context_tokens=8192, memory_bytes=1024**3),)
        ),
        workload=PromptModelWorkload(
            characters=len(_SYSTEM) + len(_USER),
            wide_characters=0,
            messages=2,
            visual_inputs=0,
            request_bytes=2048,
            requested_output_tokens=128,
        ),
        requested_step=0,
        available_memory_bytes=8 * 1024**3,
    )
    if decision.plan is None:
        _fail("synthetic request does not fit this connection")
    return PromptModelSessionRequest(
        profile=profile,
        model=model,
        destination=admit_egress_destination(profile.family, profile.endpoint),
        plan=decision.plan,
        messages=(
            PromptModelMessage(PromptModelRole.SYSTEM, _SYSTEM),
            PromptModelMessage(PromptModelRole.USER, _USER),
        ),
    )


class _HermeticExchange:
    def __init__(self, profile: PromptModelProfile, model: ModelChoice) -> None:
        self.profile, self.model = profile, model
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
        policy = policy_for_profile(self.profile)
        self.calls.append((method, path))
        if method == "GET" and path == policy.discovery_route and payload is None:
            identifier = (
                "models/" + self.model.model_id
                if policy.provider_id == "google_gemini"
                else self.model.model_id
            )
            if policy.provider_id == "google_gemini":
                return {
                    "models": [
                        {"name": identifier, "supportedGenerationMethods": ["generateContent"]}
                    ]
                }
            return {
                "data": [{"id": identifier}],
                **(
                    {"has_more": False}
                    if self.profile.family is PromptModelFamily.REMOTE_ANTHROPIC
                    else {}
                ),
            }
        if (
            method != "POST"
            or path != policy.chat_route
            or payload is None
            or payload.get("model") != self.model.model_id
        ):
            _fail("hermetic exchange received an invalid request")
        text = json.dumps(
            {"schema": OLLAMA_DRAFT_SCHEMA_ID, "prompt_text": "quiet street, static camera"}
        )
        self.metrics = RemoteExchangeMetrics(
            http_status=200,
            request_bytes=len(json.dumps(dict(payload)).encode()),
            response_bytes=256,
            prompt_tokens=96,
            completion_tokens=32,
            usage_present=True,
        )
        if self.profile.family is PromptModelFamily.REMOTE_ANTHROPIC:
            return {
                "type": "message",
                "role": "assistant",
                "model": self.model.model_id,
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 96, "output_tokens": 32},
            }
        return {
            "model": self.model.model_id,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ],
            "usage": {"prompt_tokens": 96, "completion_tokens": 32},
        }


def _closed_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate draft key")
        result[key] = value
    return result


def _closed_draft(text: str) -> bool:
    try:
        value = json.loads(text, object_pairs_hook=_closed_pairs)
    except ValueError:
        return False
    return (
        isinstance(value, dict)
        and set(value) == {"schema", "prompt_text"}
        and value["schema"] == OLLAMA_DRAFT_SCHEMA_ID
        and type(value["prompt_text"]) is str
        and 1 <= len(value["prompt_text"].strip()) <= 4096
    )


def _run(
    request: PromptModelSessionRequest,
    *,
    mode: str,
    credential: RuntimeCredential,
    candidate: tuple[str, str] | None,
    exchange_factory: ExchangeFactory | None,
) -> dict[str, object]:
    profile, model = request.profile, request.model
    if model is None:
        _fail("qualification requires an explicit chosen model")
    policy = policy_for_profile(profile)
    exchange = (
        exchange_factory(profile, model, credential)
        if exchange_factory is not None
        else _HermeticExchange(profile, model)
        if mode == "hermetic"
        else RemoteHttpsExchange(
            admit_egress_destination(profile.family, profile.endpoint), credential, policy=policy
        )
    )
    result = run_remote_prompt_model_session(
        request,
        exchange,
        selected_profile_id=profile.profile_id,
        consent=RemoteConsentRecord(
            profile_id=profile.profile_id,
            status=ProviderConsentStatus.GRANTED,
            network_permitted=True,
            media_upload_consented=False,
        ),
        credential=credential,
    )
    schema_valid = result.answer is not None and _closed_draft(result.answer.text)
    receipt = result.receipt
    receipt_valid = receipt is not None and (
        receipt.profile_id == profile.profile_id
        and receipt.provider_id == policy.provider_id
        and receipt.model_id == model.model_id
        and receipt.policy_sha256 == policy.fingerprint
        and receipt.host == admit_egress_destination(profile.family, profile.endpoint).host
        and receipt.request_bytes > 0
        and receipt.response_bytes > 0
        and receipt.credential_last_four == ""
    )
    projected = (
        None
        if receipt is None
        else {
            "schema": CONNECTION_QUALIFICATION_RECEIPT_SCHEMA,
            **{
                key: value
                for key, value in receipt.to_wire().items()
                if key not in {"schema", "host", "credential_last_four"}
            },
        }
    )
    observed = getattr(exchange, "transmission_count", None)
    basis = connection_observation_basis(
        outcome_id=result.outcome.outcome_id.value,
        schema_valid=schema_valid,
        receipt_valid=receipt_valid,
        receipt=projected,
        observed_transmissions=observed,
        max_transmissions=policy.max_transmissions,
    )
    return {
        "schema": CONNECTION_QUALIFICATION_SCHEMA,
        "mode": mode,
        "status": "PASS" if basis is not None else "FAIL",
        "profile_id": profile.profile_id,
        "provider_id": policy.provider_id,
        "family": profile.family.value,
        "model_id": model.model_id,
        "adapter_version": profile.adapter_version,
        "parser_version": profile.parser_version,
        "policy_sha256": policy.fingerprint,
        "max_transmissions": policy.max_transmissions,
        "observed_transmissions": observed,
        "observed_on": date.today().isoformat(),
        "repository_commit": None if candidate is None else candidate[0],
        "repository_tree": None if candidate is None else candidate[1],
        "outcome_id": result.outcome.outcome_id.value,
        "basis": basis,
        "schema_valid": schema_valid,
        "receipt_valid": receipt_valid,
        "receipt": projected,
        "catalog_promotion_performed": False,
    }


def _write(path: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # IMPORTANT: the preflight exists check does not own the path. Exclusive creation must
        # refuse a later race after provider I/O without overwriting immutable evidence.
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
    except FileExistsError:
        _fail("evidence output already exists")


def main(
    argv: Sequence[str] | None = None,
    *,
    exchange_factory: ExchangeFactory | None = None,
    credential_reader: Callable[[str], str] = getpass.getpass,
    candidate_identity_reader: CandidateIdentityReader = _current_candidate_identity,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("hermetic", "live"), required=True)
    parser.add_argument(
        "--profile", choices=("openai.remote", "gemini.remote", "anthropic.remote"), required=True
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--authorize-policy-sha256")
    parser.add_argument("--authorize-max-transmissions", type=int)
    parser.add_argument("--candidate-commit")
    parser.add_argument("--candidate-tree")
    args = parser.parse_args(argv)
    if args.output.exists():
        _fail("evidence output already exists")
    profile = load_prompt_model_catalog().require(args.profile)
    policy = policy_for_profile(profile)
    try:
        model = ModelChoice(
            profile.profile_id,
            args.model,
            "sha256:" + sha256((profile.profile_id + "\n" + args.model).encode()).hexdigest(),
            time.monotonic(),
        )
        request = _request(profile, model)
    except PromptModelContractError:
        _fail("invalid model or request contract")
    candidate = None
    authority = (
        args.authorize_policy_sha256,
        args.authorize_max_transmissions,
        args.candidate_commit,
        args.candidate_tree,
    )
    if args.mode == "live":
        if exchange_factory is not None:
            _fail("live mode forbids injected transports")
        if (
            args.authorize_policy_sha256 != policy.fingerprint
            or args.authorize_max_transmissions != policy.max_transmissions
            or any(
                not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None
                for value in (args.candidate_commit, args.candidate_tree)
            )
        ):
            _fail("live mode requires current policy, send bound and candidate commit/tree")
        candidate = candidate_identity_reader()
        if candidate != (args.candidate_commit, args.candidate_tree):
            _fail("authorization does not match the clean checked-out candidate")
        secret = credential_reader(f"API key for {policy.provider_id} (input hidden): ")
    else:
        if any(value is not None for value in authority):
            _fail("live authorization is invalid in hermetic mode")
        secret = profile.profile_id
    try:
        credential = RuntimeCredential(secret)
    except PromptModelContractError:
        _fail("invalid credential")
    payload = _run(
        request,
        mode=args.mode,
        credential=credential,
        candidate=candidate,
        exchange_factory=exchange_factory,
    )
    _write(args.output, payload)
    sys.stdout.write(
        json.dumps(
            {"mode": payload["mode"], "profile_id": profile.profile_id, "status": payload["status"]}
        )
        + "\n"
    )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
