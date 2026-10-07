"""The closed evidence record: status rules, required justifications, and privacy redaction.

The status vocabulary is small on purpose, and the two rules that matter are the ones a careless
harness collapses:

* **`supported` needs a basis.** Either applicable bounded live evidence, or an explicit recorded
  reason why live evidence is not applicable because the mechanism is fully determined by the exact
  source without loading weights. A `supported` row with neither is not a measurement, it is an
  opinion, and it degrades to `unqualified`.
* **`unsupported` needs exhaustiveness.** Absence is only affirmative evidence if the search was
  exhaustive over the pinned source and the live node inventory, and says so. Without that record it
  degrades to `unqualified`.

The distinction between those two failure states is the reason this item runs before `M20`:
`unsupported` sends a consumer branch to replan now, while `unqualified` only invites another run.
Silently reporting a confirmed absence as `unqualified` would waste the entire early-qualification
argument.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import EVIDENCE_SCHEMA, EVIDENCE_VERSION

#: The five rows, in the order the plan and the research authority name them.
REQUIRED_ROWS: tuple[str, ...] = (
    "temporal_profile",
    "joint_av_latent_descriptor",
    "completed_boundary_resume",
    "dual_domain_av_mask",
    "two_ended_av_bridge",
)

#: Which `M20` item each row gates. Recorded with the row so an accepted matrix cannot be read as
#: activating something it never measured.
ROW_CONSUMERS: Mapping[str, str] = {
    "temporal_profile": "M20-01",
    "joint_av_latent_descriptor": "M20-04",
    "completed_boundary_resume": "M20-05",
    "dual_domain_av_mask": "M20-06",
    "two_ended_av_bridge": "M20-07",
}

_PRIVATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("windows_path", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")),
    ("unc_path", re.compile(r"\\\\[A-Za-z0-9_.$-]+\\")),
    # An enumerated list of private prefixes is a blocklist, and a reviewer showed it was already
    # incomplete: an MSYS single-letter drive rendering, ordinary Linux install roots and a
    # forward-slash UNC share all walked through it. Evidence is supposed to carry opaque
    # identities, never locations, so the rule is inverted: anything shaped like an absolute path
    # is refused, whatever it names.
    ("absolute_path", re.compile(r"(?<![\w.~])/{1,2}[^/\s]+/")),
    ("file_url", re.compile(r"file://", re.IGNORECASE)),
    ("credential", re.compile(r"(?i)(?:api[_-]?key|secret|token|password|authorization)\s*[=:]")),
    ("url_userinfo", re.compile(r"https?://[^/\s]*@")),
)

_ALLOWED_HOST = re.compile(r"^http://127\.0\.0\.1:\d+/?$")


class RedactionError(ValueError):
    """An evidence value carried something that must never leave the machine."""


class RowStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNQUALIFIED = "unqualified"
    RETIRED = "retired"


@dataclass(frozen=True)
class Exhaustiveness:
    """Why an absence is affirmative rather than merely unobserved."""

    source_identity: str
    candidate_mechanisms: tuple[str, ...]
    live_node_inventory: tuple[str, ...]
    inventory_source: str

    def is_complete(self) -> bool:
        return bool(
            self.source_identity
            and self.candidate_mechanisms
            and self.live_node_inventory
            and self.inventory_source
        )

    def as_evidence(self) -> dict[str, Any]:
        return {
            "source_identity": self.source_identity,
            "candidate_mechanisms": list(self.candidate_mechanisms),
            "live_node_inventory": list(self.live_node_inventory),
            "inventory_source": self.inventory_source,
        }


@dataclass(frozen=True)
class Row:
    """One capability result, with the justification its status requires."""

    name: str
    status: RowStatus
    reason_code: str
    consumer: str
    source_evidence: Mapping[str, Any] = field(default_factory=dict)
    live_evidence: Mapping[str, Any] | None = None
    live_applicability: str | None = None
    exhaustiveness: Exhaustiveness | None = None
    findings: tuple[str, ...] = ()

    def as_evidence(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "row": self.name,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "consumer": self.consumer,
            "source_evidence": dict(self.source_evidence),
            "findings": list(self.findings),
        }
        if self.live_evidence is not None:
            record["live_evidence"] = dict(self.live_evidence)
        if self.live_applicability is not None:
            record["live_applicability"] = self.live_applicability
        if self.exhaustiveness is not None:
            record["exhaustiveness"] = self.exhaustiveness.as_evidence()
        return record


def enforce_status_rules(row: Row) -> Row:
    """Downgrade any row whose status is not carried by its own justification.

    This runs on every row before the matrix is assembled, so an over-claiming row cannot survive by
    being written carefully somewhere else.
    """
    if row.status is RowStatus.SUPPORTED:
        has_live = row.live_evidence is not None
        has_reason = bool(row.live_applicability)
        if not has_live and not has_reason:
            return Row(
                name=row.name,
                status=RowStatus.UNQUALIFIED,
                reason_code="supported_without_basis",
                consumer=row.consumer,
                source_evidence=row.source_evidence,
                findings=(
                    *row.findings,
                    "downgraded: a supported row needs live evidence or a recorded reason why "
                    "live evidence is not applicable",
                ),
            )
    if row.status is RowStatus.UNSUPPORTED:
        if row.exhaustiveness is None or not row.exhaustiveness.is_complete():
            return Row(
                name=row.name,
                status=RowStatus.UNQUALIFIED,
                reason_code="unsupported_without_exhaustiveness",
                consumer=row.consumer,
                source_evidence=row.source_evidence,
                exhaustiveness=row.exhaustiveness,
                findings=(
                    *row.findings,
                    "downgraded: an unsupported row needs an exhaustiveness record naming the "
                    "searched source, the candidate mechanisms and the live node inventory",
                ),
            )
    return row


def assert_redacted(payload: Any, *, path: str = "$") -> None:
    """Refuse to emit anything carrying a private path, credential or non-loopback endpoint."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            assert_redacted(value, path=f"{path}.{key}")
        return
    if isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            assert_redacted(value, path=f"{path}[{index}]")
        return
    if not isinstance(payload, str):
        return
    if _ALLOWED_HOST.match(payload):
        return
    for label, pattern in _PRIVATE_PATTERNS:
        if pattern.search(payload):
            raise RedactionError(f"{path}: evidence value matches {label}")
    if payload.startswith(("http://", "https://")):
        raise RedactionError(f"{path}: only the supplied loopback endpoint may appear in evidence")


def build_matrix(
    *,
    subject: Mapping[str, Any],
    rows: Sequence[Row],
    limitations: Sequence[str] = (),
) -> dict[str, Any]:
    """Assemble the closed matrix, enforcing row completeness and privacy before returning it."""
    enforced = [enforce_status_rules(row) for row in rows]
    by_name = {row.name: row for row in enforced}

    missing = [name for name in REQUIRED_ROWS if name not in by_name]
    if missing:
        raise ValueError(f"matrix is missing required rows: {', '.join(missing)}")
    unexpected = [name for name in by_name if name not in REQUIRED_ROWS]
    if unexpected:
        raise ValueError(f"matrix carries rows outside the closed set: {', '.join(unexpected)}")

    matrix = {
        "schema": EVIDENCE_SCHEMA,
        "version": EVIDENCE_VERSION,
        "subject": dict(subject),
        "rows": [by_name[name].as_evidence() for name in REQUIRED_ROWS],
        "limitations": list(limitations),
    }
    assert_redacted(matrix)
    return matrix


def supported_rows(matrix: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(row["row"] for row in matrix["rows"] if row["status"] == RowStatus.SUPPORTED.value)
