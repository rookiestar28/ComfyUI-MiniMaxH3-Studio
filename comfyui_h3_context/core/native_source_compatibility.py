"""Structural compatibility of the loaded native source for binding-free T2VA only.

The admission is computed from the loaded bytes at qualification time (`native_t2va_structure`),
never from a host version, revision or blob allowlist. The raw blob stays a currentness identity:
any byte change invalidates an issued receipt, and only a fresh observation may admit again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .canonical import canonical_fingerprint
from .contracts import TaskMode
from .native_h3 import NATIVE_H3_HOST_REVISION, NATIVE_H3_SOURCE_BLOB, NativeH3Wiring
from .native_t2va_structure import (
    PRODUCTION_MANIFEST,
    NativeT2VAStructureVerdict,
    is_production_admission,
)


@dataclass(frozen=True, slots=True)
class NativeSourceObservation:
    """One bounded read of the registered native module.

    The blob and the structural verdict derive from the same byte buffer. `_origin` holds the
    registered module, class objects and source location for in-process identity comparison;
    it is never serialized, hashed into a wire or shown to a browser.
    """

    source_blob: str
    structure: NativeT2VAStructureVerdict
    _origin: tuple[object, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class NativeSourceCompatibilityV2:
    """A receipt bound to the ORIGINAL observation it was minted from, never re-minted."""

    _source: NativeSourceObservation = field(repr=False)

    def admits(self, source: NativeSourceObservation | None, wiring: NativeH3Wiring) -> bool:
        # CRITICAL: only the binding-free T2VA branch is structurally proven. Media branches
        # changed conditioning between revisions; they must never borrow this receipt.
        return (
            type(self) is NativeSourceCompatibilityV2
            and type(source) is NativeSourceObservation
            and type(self._source) is NativeSourceObservation
            and source == self._source
            and source.source_blob != NATIVE_H3_SOURCE_BLOB
            and is_production_admission(source.structure)
            and wiring.task_mode is TaskMode.T2VA
            and not wiring.bindings
            and wiring.native_source_blob == NATIVE_H3_SOURCE_BLOB
            and wiring.host_revision == NATIVE_H3_HOST_REVISION
        )

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "schema": "h3.native_source_compatibility.v2",
                "classifier_schema": self._source.structure.classifier_schema,
                "manifest_fingerprint": self._source.structure.manifest_fingerprint,
                "source_blob": self._source.source_blob,
                "baseline_blob": NATIVE_H3_SOURCE_BLOB,
                "scope": "structural_t2va_without_bindings",
            }
        )


def qualify_native_source_compatibility(
    source: NativeSourceObservation | None, wiring: NativeH3Wiring
) -> NativeSourceCompatibilityV2 | None:
    """Mint a receipt for a structurally admitted non-baseline source; the baseline needs none."""
    if type(source) is not NativeSourceObservation or source.source_blob == NATIVE_H3_SOURCE_BLOB:
        return None
    receipt = NativeSourceCompatibilityV2(source)
    return receipt if receipt.admits(source, wiring) else None


__all__ = [
    "PRODUCTION_MANIFEST",
    "NativeSourceCompatibilityV2",
    "NativeSourceObservation",
    "qualify_native_source_compatibility",
]
