"""Bounded native model metadata readers; no transport, key, price or inferred capabilities."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import NoReturn

from ..prompt_model_provider import (
    MAX_DISCOVERY_ROWS,
    ModelMetadata,
    PromptModelContractError,
    PromptModelFamily,
    normalize_ollama_digest,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
GEMINI_DISCOVERY_ROUTE = "/v1beta/models?pageSize=1000"
ANTHROPIC_DISCOVERY_ROUTE = "/v1/models?limit=1000"


def _fail() -> NoReturn:
    raise PromptModelContractError("readiness_census")


def _supported(capabilities: object, name: str) -> bool | None:
    if capabilities is None:
        return None
    if not isinstance(capabilities, Mapping):
        _fail()
    value = capabilities.get(name)
    if value is None:
        return None
    if not isinstance(value, Mapping) or type(value.get("supported")) is not bool:
        _fail()
    return bool(value["supported"])


def _created(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        _fail()
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            _fail()
        return int(stamp.timestamp())
    except (ValueError, OverflowError, OSError):
        _fail()
    return None


def read_native_models(
    family: PromptModelFamily, listing: object, route: str | None
) -> tuple[tuple[str, ModelMetadata], ...]:
    if not isinstance(listing, Mapping):
        _fail()
    gemini = route == GEMINI_DISCOVERY_ROUTE
    anthropic = family is PromptModelFamily.REMOTE_ANTHROPIC
    # IMPORTANT: a continuation is an incomplete census, never choice authority. Check the
    # raw bound before filtering, so unsupported models cannot hide an oversized response.
    if anthropic and listing.get("has_more") is not False:
        _fail()
    if gemini and listing.get("nextPageToken") not in (None, ""):
        _fail()
    entries = listing.get("models" if gemini or family is PromptModelFamily.OLLAMA else "data")
    if (
        not isinstance(entries, Sequence)
        or isinstance(entries, str | bytes)
        or len(entries) > MAX_DISCOVERY_ROWS
    ):
        _fail()
    rows = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            _fail()
        raw = entry.get("name") if gemini else entry.get("id")
        if family is PromptModelFamily.OLLAMA:
            raw = entry.get("name") or entry.get("model")
        if not isinstance(raw, str) or _IDENTIFIER.fullmatch(raw) is None or ".." in raw:
            _fail()
        identifier = raw.removeprefix("models/") if gemini else raw
        if identifier in seen:
            _fail()
        seen.add(identifier)
        if gemini:
            if not raw.startswith("models/"):
                _fail()
            methods = entry.get("supportedGenerationMethods")
            if (
                not isinstance(methods, list)
                or len(methods) > 32
                or any(
                    not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{1,64}", value)
                    for value in methods
                )
            ):
                _fail()
            thinking = entry.get("thinking")
            if thinking is not None and type(thinking) is not bool:
                _fail()
            metadata = ModelMetadata(
                display_name=entry.get("displayName"),
                max_input_tokens=entry.get("inputTokenLimit"),
                max_output_tokens=entry.get("outputTokenLimit"),
                reasoning_mandatory=False if thinking is False else None,
                locality="remote",
                moving_alias=raw.endswith("-latest"),
            )
            if "generateContent" not in methods:
                continue
            raw = raw.removeprefix("models/")
        elif anthropic:
            caps = entry.get("capabilities")
            thinking = _supported(caps, "thinking")
            metadata = ModelMetadata(
                display_name=entry.get("display_name"),
                created=_created(entry.get("created_at")),
                max_input_tokens=entry.get("max_input_tokens"),
                max_output_tokens=entry.get("max_tokens"),
                structured_output=_supported(caps, "structured_outputs"),
                reasoning_mandatory=False if thinking is False else None,
                locality="remote",
            )
        elif family is PromptModelFamily.OLLAMA:
            cloud = any(entry.get(key) not in (None, "") for key in ("remote_host", "remote_model"))
            details = entry.get("details")
            if details is not None and not isinstance(details, Mapping):
                _fail()
            metadata = ModelMetadata(
                model_digest=None if cloud else normalize_ollama_digest(entry.get("digest")),
                locality="cloud" if cloud else "unknown",
                family=details.get("family") if isinstance(details, Mapping) else None,
                parameter_size=details.get("parameter_size")
                if isinstance(details, Mapping)
                else None,
                quantization=details.get("quantization_level")
                if isinstance(details, Mapping)
                else None,
            )
        else:
            metadata = ModelMetadata(
                created=entry.get("created"),
                shutdown_date=entry.get("shutdown_date"),
                locality="remote"
                if family is PromptModelFamily.REMOTE_OPENAI_COMPATIBLE
                else "local",
            )
        rows.append((raw, metadata))
    return tuple(rows)
