"""Strict, content-free live evidence for model-independent remote connections."""

import json
import re
from collections.abc import Mapping
from hashlib import sha256

from .prompt_model_provider import (
    ConnectionQualificationEvidence,
    LegacyPromptModelProfile,
    PromptModelContractError,
    PromptModelOutcomeId,
    PromptModelProfile,
)
from .remote_provider_policy import policy_for_profile

CONNECTION_QUALIFICATION_SCHEMA = "h3.prompt_model.connection_qualification.v1"
CONNECTION_QUALIFICATION_RECEIPT_SCHEMA = "h3.prompt_model.connection_qualification_receipt.v1"
RESULT_KEYS = frozenset(
    {
        "schema",
        "mode",
        "status",
        "profile_id",
        "provider_id",
        "model_id",
        "family",
        "adapter_version",
        "parser_version",
        "policy_sha256",
        "max_transmissions",
        "observed_transmissions",
        "observed_on",
        "repository_commit",
        "repository_tree",
        "outcome_id",
        "basis",
        "schema_valid",
        "receipt_valid",
        "receipt",
        "catalog_promotion_performed",
    }
)
RECEIPT_KEYS = frozenset(
    {
        "schema",
        "profile_id",
        "provider_id",
        "model_id",
        "policy_sha256",
        "outcome_id",
        "http_status",
        "request_bytes",
        "response_bytes",
        "prompt_tokens",
        "completion_tokens",
        "duration_ms",
        "usage_present",
    }
)


def connection_observation_basis(
    *,
    outcome_id: str,
    schema_valid: object,
    receipt_valid: object,
    receipt: object,
    observed_transmissions: object,
    max_transmissions: int,
) -> str | None:
    """One verdict for tool output and strict reduction; reachability is never draft success."""
    if (
        type(schema_valid) is not bool
        or receipt_valid is not True
        or not isinstance(receipt, Mapping)
        or type(max_transmissions) is not int
        or not 2 <= max_transmissions <= 4
        or type(observed_transmissions) is not int
        or not 2 <= observed_transmissions <= max_transmissions
    ):
        return None
    for key in ("request_bytes", "response_bytes"):
        count = receipt.get(key)
        if type(count) is not int or count < 1:
            return None
    status = receipt.get("http_status")
    if type(status) is not int or receipt.get("outcome_id") != outcome_id:
        return None
    if outcome_id == PromptModelOutcomeId.OK.value and schema_valid is True and status == 200:
        return "completion"
    # IMPORTANT: a measured quota answer after identity proves the connection only. Missing
    # send metrics, auth/transport errors or a manufactured schema-success flag cannot qualify it.
    if (
        outcome_id == PromptModelOutcomeId.QUOTA.value
        and schema_valid is False
        and status in {402, 429}
    ):
        return "reachability"
    return None


def build_remote_connection_qualification_evidence(
    values: object,
    *,
    profile: PromptModelProfile,
) -> ConnectionQualificationEvidence:
    """Reduce a live observation; neither hermetic results nor legacy claims can qualify v6."""
    if (
        isinstance(profile, LegacyPromptModelProfile)
        or not isinstance(values, Mapping)
        or set(values) != RESULT_KEYS
    ):
        raise PromptModelContractError("connection_qualification_keys")
    policy = policy_for_profile(profile)
    expected = {
        "schema": CONNECTION_QUALIFICATION_SCHEMA,
        "mode": "live",
        "status": "PASS",
        "profile_id": profile.profile_id,
        "provider_id": policy.provider_id,
        "family": profile.family.value,
        "adapter_version": profile.adapter_version,
        "parser_version": profile.parser_version,
        "policy_sha256": policy.fingerprint,
    }
    if any(values[key] != value for key, value in expected.items()) or (
        type(values["schema_valid"]) is not bool
        or values["receipt_valid"] is not True
        or values["catalog_promotion_performed"] is not False
    ):
        raise PromptModelContractError("connection_qualification_identity")
    for key in ("repository_commit", "repository_tree"):
        if not isinstance(values[key], str) or re.fullmatch(r"[0-9a-f]{40}", values[key]) is None:
            raise PromptModelContractError("connection_qualification_candidate")
    ceiling, observed = values["max_transmissions"], values["observed_transmissions"]
    if (
        type(ceiling) is not int
        or ceiling != policy.max_transmissions
        or type(observed) is not int
        or not 2 <= observed <= ceiling
    ):
        raise PromptModelContractError("connection_qualification_transmissions")
    receipt = values["receipt"]
    if not isinstance(receipt, Mapping) or set(receipt) != RECEIPT_KEYS:
        raise PromptModelContractError("connection_qualification_receipt")
    if (
        receipt["schema"] != CONNECTION_QUALIFICATION_RECEIPT_SCHEMA
        or type(receipt["http_status"]) is not int
    ):
        raise PromptModelContractError("connection_qualification_receipt")
    for key in ("profile_id", "provider_id", "model_id", "policy_sha256", "outcome_id"):
        if receipt[key] != values[key]:
            raise PromptModelContractError("connection_qualification_receipt")
    if type(receipt["usage_present"]) is not bool:
        raise PromptModelContractError("connection_qualification_receipt")
    for key in (
        "request_bytes",
        "response_bytes",
        "prompt_tokens",
        "completion_tokens",
        "duration_ms",
    ):
        minimum = 1 if key in {"request_bytes", "response_bytes"} else 0
        if type(receipt[key]) is not int or not minimum <= receipt[key] <= 2_147_483_647:
            raise PromptModelContractError("connection_qualification_receipt")
    if receipt["usage_present"] is False and (
        receipt["prompt_tokens"] != 0 or receipt["completion_tokens"] != 0
    ):
        raise PromptModelContractError("connection_qualification_receipt")
    if not isinstance(values["model_id"], str) or not isinstance(values["observed_on"], str):
        raise PromptModelContractError("connection_qualification_field")
    if not isinstance(values["outcome_id"], str):
        raise PromptModelContractError("connection_qualification_field")
    basis = connection_observation_basis(
        outcome_id=values["outcome_id"],
        schema_valid=values["schema_valid"],
        receipt_valid=values["receipt_valid"],
        receipt=receipt,
        observed_transmissions=observed,
        max_transmissions=ceiling,
    )
    if basis is None or values["basis"] != basis:
        raise PromptModelContractError("connection_qualification_basis")
    # SECURITY: derive the evidence hash from this closed observation. A caller cannot splice
    # a historical digest, hermetic verdict or prompt-bearing object into new connection authority.
    fingerprint = (
        "sha256:"
        + sha256(
            json.dumps(
                dict(values), sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    )
    return ConnectionQualificationEvidence(
        profile_id=profile.profile_id,
        family=profile.family,
        observation_model_id=values["model_id"],
        adapter_version=profile.adapter_version,
        parser_version=profile.parser_version,
        observed_on=values["observed_on"],
        evidence_basis_sha256=fingerprint,
        max_transmissions=ceiling,
    )
