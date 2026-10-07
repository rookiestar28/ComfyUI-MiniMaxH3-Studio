"""The M19 modularity chain's closeout matrix: what moved, what held, and what that is evidence of.

Five items decomposed four large modules and added two structural guards. Each one wrote a record
full of numbers. This type holds those numbers **re-derived from the committed artifacts** rather
than restated, because a record's prose is not evidence -- M18-06's closeout log records the one
metric it copied from prose being wrong, contradicted by its own evidence line by 400 bytes.

A row is not just a reading. It carries an expectation, so the matrix is a check rather than a
table:

* ``CONSTANT`` -- this must not have changed since the checkpoint that introduced it. A public
  surface digest, a host-observable fingerprint, a forbidden-import count. If it moved, the chain
  broke something and the row says so.
* ``INFORMATIONAL`` -- this was expected to change and the value is reported without a verdict.
  Module count, largest module, the shipped bundle's hash.

``ABSENT`` is a real value, not a zero. An artifact that did not exist yet at a checkpoint has no
reading there, and reporting that as ``0`` would turn "not created yet" into "we measured none".

This module reads no file and imports no host: it is a pure function of already-read values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from comfyui_h3_context.core.canonical import canonical_bytes
from comfyui_h3_context.core.errors import ContractValidationError

M19_CLOSEOUT_SCHEMA = "h3-context-m19-closeout/1"

MAX_CHECKPOINTS = 16
MAX_ROWS = 64
MAX_TEXT = 200

#: The literal reported when an artifact does not exist at a checkpoint.
ABSENT = "absent"

_IDENTITY = re.compile(r"[A-Za-z][A-Za-z0-9_.:/@+-]{0,199}\Z")
_ITEM = re.compile(r"M[0-9]{2}-[0-9]{2}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_RELATIVE_PATH = re.compile(r"[A-Za-z0-9._-](?:[A-Za-z0-9._/-]*[A-Za-z0-9._-])?\Z")
#: A reading is a count, a digest, a boolean or the absent literal. Never free text, and never a
#: path or a value read out of the repository's content.
_VALUE = re.compile(r"(?:[0-9]{1,12}|sha256:[0-9a-f]{64}|true|false|absent)\Z")


class M19CloseoutError(ContractValidationError):
    """Raised when a closeout row claims more than its evidence supports."""


class RowExpectation(str, Enum):
    """What the chain was supposed to do to this metric."""

    #: Must not move after the checkpoint that introduced it.
    CONSTANT = "constant"
    #: Expected to move; reported without a verdict.
    INFORMATIONAL = "informational"


class RowVerdict(str, Enum):
    """What the readings say."""

    #: A ``CONSTANT`` row that did not move.
    HELD = "held"
    #: A ``CONSTANT`` row that moved. The chain changed something it should not have.
    BROKEN = "broken"
    #: An ``INFORMATIONAL`` row. No verdict is claimed.
    REPORTED = "reported"


def _text(value: object, field_name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        raise M19CloseoutError(f"{field_name} must be bounded non-empty text")
    if pattern.fullmatch(value) is None:
        raise M19CloseoutError(f"{field_name} is not in the accepted form: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """One accepted item in the chain, and the commit its evidence is read from."""

    item: str
    commit: str
    summary: str

    def __post_init__(self) -> None:
        _text(self.item, "item", _ITEM)
        _text(self.commit, "commit", _COMMIT)
        if not isinstance(self.summary, str) or not 0 < len(self.summary) <= MAX_TEXT:
            raise M19CloseoutError("summary must be bounded non-empty text")

    def to_wire(self) -> dict[str, str]:
        return {"commit": self.commit, "item": self.item, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class Reading:
    """One metric at one checkpoint."""

    item: str
    value: str

    def __post_init__(self) -> None:
        _text(self.item, "item", _ITEM)
        _text(self.value, "value", _VALUE)

    @property
    def present(self) -> bool:
        return self.value != ABSENT

    def to_wire(self) -> dict[str, str]:
        return {"item": self.item, "value": self.value}


@dataclass(frozen=True, slots=True)
class CloseoutRow:
    """One metric across the chain, with the expectation that decides its verdict."""

    domain: str
    metric: str
    source: str
    introduced_by: str
    expectation: RowExpectation
    readings: tuple[Reading, ...]

    def __post_init__(self) -> None:
        _text(self.domain, "domain", _IDENTITY)
        _text(self.metric, "metric", _IDENTITY)
        _text(self.source, "source", _RELATIVE_PATH)
        _text(self.introduced_by, "introduced_by", _ITEM)
        if not isinstance(self.expectation, RowExpectation):
            raise M19CloseoutError("expectation must be a RowExpectation")
        if not isinstance(self.readings, tuple) or not self.readings:
            raise M19CloseoutError("a row must carry at least one reading")
        if len(self.readings) > MAX_CHECKPOINTS:
            raise M19CloseoutError("readings must be bounded")
        items = [reading.item for reading in self.readings]
        if len(set(items)) != len(items):
            raise M19CloseoutError("a checkpoint may be read once per row")
        # A row cannot claim to have been introduced after its first real reading: that would let a
        # value that moved be excused by moving the introduction point instead.
        present = [reading.item for reading in self.readings if reading.present]
        if present and present[0] != self.introduced_by:
            raise M19CloseoutError(
                f"{self.metric} first reads at {present[0]} but claims {self.introduced_by}"
            )
        if not present:
            raise M19CloseoutError(f"{self.metric} has no reading at any checkpoint")

    @property
    def observed(self) -> tuple[str, ...]:
        return tuple(reading.value for reading in self.readings if reading.present)

    @property
    def verdict(self) -> RowVerdict:
        if self.expectation is RowExpectation.INFORMATIONAL:
            return RowVerdict.REPORTED
        return RowVerdict.HELD if len(set(self.observed)) == 1 else RowVerdict.BROKEN

    def to_wire(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "expectation": self.expectation.value,
            "introduced_by": self.introduced_by,
            "metric": self.metric,
            "readings": [reading.to_wire() for reading in self.readings],
            "source": self.source,
            "verdict": self.verdict.value,
        }


@dataclass(frozen=True, slots=True)
class M19Closeout:
    """The whole matrix."""

    checkpoints: tuple[Checkpoint, ...]
    rows: tuple[CloseoutRow, ...]
    schema: str = M19_CLOSEOUT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != M19_CLOSEOUT_SCHEMA:
            raise M19CloseoutError(f"unsupported schema {self.schema!r}")
        if not self.checkpoints or len(self.checkpoints) > MAX_CHECKPOINTS:
            raise M19CloseoutError("checkpoints must be a bounded non-empty tuple")
        if not self.rows or len(self.rows) > MAX_ROWS:
            raise M19CloseoutError("rows must be a bounded non-empty tuple")
        known = [checkpoint.item for checkpoint in self.checkpoints]
        if len(set(known)) != len(known):
            raise M19CloseoutError("an item may appear once")
        if known != sorted(known):
            raise M19CloseoutError("checkpoints must be in chain order")
        for row in self.rows:
            if [reading.item for reading in row.readings] != known:
                raise M19CloseoutError(
                    f"{row.metric} must read every checkpoint, in order, or report it absent"
                )

    @property
    def broken(self) -> tuple[CloseoutRow, ...]:
        return tuple(row for row in self.rows if row.verdict is RowVerdict.BROKEN)

    @property
    def held(self) -> tuple[CloseoutRow, ...]:
        return tuple(row for row in self.rows if row.verdict is RowVerdict.HELD)

    @property
    def fingerprint(self) -> str:
        import hashlib

        payload = {
            "checkpoints": [checkpoint.to_wire() for checkpoint in self.checkpoints],
            "rows": [row.to_wire() for row in self.rows],
            "schema": self.schema,
        }
        return "sha256:" + hashlib.sha256(canonical_bytes(payload)).hexdigest()

    def to_wire(self) -> dict[str, object]:
        return {
            "checkpoints": [checkpoint.to_wire() for checkpoint in self.checkpoints],
            "fingerprint": self.fingerprint,
            "rows": [row.to_wire() for row in self.rows],
            "schema": self.schema,
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "checkpoints": len(self.checkpoints),
            "rows": len(self.rows),
            "held": len(self.held),
            "broken": [row.metric for row in self.broken],
            "fingerprint": self.fingerprint,
        }


__all__ = [
    "ABSENT",
    "M19_CLOSEOUT_SCHEMA",
    "Checkpoint",
    "CloseoutRow",
    "M19Closeout",
    "M19CloseoutError",
    "Reading",
    "RowExpectation",
    "RowVerdict",
]
