"""Pure, locator-free M25-29 Production-output import contract.

The browser names only two current workspaces and opaque output rows.  Artifact receipts, source
fingerprints, media facts and filesystem locators remain adapter-owned and cannot cross this wire.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import cast

from .canonical import canonical_bytes, canonical_fingerprint

PRODUCTION_AUTHORING_IMPORT_ACTION = "import_production_outputs_to_authoring"
PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA = "h3.context.production_authoring_import.request.v1"
PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA = "h3.context.production_authoring_import.receipt.v1"
PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA = "h3.context.production_authoring_import.response.v1"
PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA_V2 = "h3.context.production_authoring_import.request.v2"
PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2 = "h3.context.production_authoring_import.receipt.v2"
PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA_V2 = (
    "h3.context.production_authoring_import.response.v2"
)
MAX_PRODUCTION_AUTHORING_IMPORT_BYTES = 65_536
MAX_PRODUCTION_AUTHORING_IMPORT_ENTRIES = 3

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PRODUCTION_HANDLE = re.compile(r"pw_[A-Za-z0-9_-]{32,96}\Z")
_AUTHORING_HANDLE = re.compile(r"authoring-[0-9a-f]{32}\Z")
_OUTPUT_HANDLE = re.compile(r"out_[0-9a-f]{40}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REQUEST_KEYS = {
    "schema",
    "action",
    "request_id",
    "production_workspace_handle",
    "production_workspace_id",
    "expected_production_workspace_revision",
    "expected_production_workspace_fingerprint",
    "authoring_workspace_handle",
    "expected_authoring_registry_fingerprint",
    "expected_authoring_reference_revision",
    "expected_authoring_timeline_revision",
    "expected_authoring_timeline_content_fingerprint",
    "expected_nle_workspace_revision",
    "expected_nle_timeline_revision",
    "expected_nle_timeline_fingerprint",
    "expected_nle_public_fingerprint",
    "entries",
}
_REQUEST_KEYS_V2 = _REQUEST_KEYS - {"expected_nle_public_fingerprint"} | {
    "authoring_schema",
    "profile_id",
    "expected_nle_authoring_fingerprint",
}


def _text(value: object, pattern: re.Pattern[str], name: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _revision(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ValueError(f"{name} is invalid")
    return value


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError("import request repeats an object member")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class ProductionAuthoringImportEntry:
    segment_id: str
    output_handle: str

    def __post_init__(self) -> None:
        _text(self.segment_id, _IDENTIFIER, "segment_id")
        _text(self.output_handle, _OUTPUT_HANDLE, "output_handle")

    def to_wire(self) -> dict[str, str]:
        return {"segment_id": self.segment_id, "output_handle": self.output_handle}


@dataclass(frozen=True, slots=True)
class ProductionAuthoringImportRequest:
    request_id: str
    production_workspace_handle: str
    production_workspace_id: str
    expected_production_workspace_revision: int
    expected_production_workspace_fingerprint: str
    authoring_workspace_handle: str
    expected_authoring_registry_fingerprint: str
    expected_authoring_reference_revision: int
    expected_authoring_timeline_revision: int
    expected_authoring_timeline_content_fingerprint: str
    expected_nle_workspace_revision: int
    expected_nle_timeline_revision: int
    expected_nle_timeline_fingerprint: str
    expected_nle_public_fingerprint: str
    entries: tuple[ProductionAuthoringImportEntry, ...]
    schema: str = PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA
    action: str = PRODUCTION_AUTHORING_IMPORT_ACTION

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA:
            raise ValueError("import request schema is unsupported")
        if self.action != PRODUCTION_AUTHORING_IMPORT_ACTION:
            raise ValueError("import request action is unsupported")
        _text(self.request_id, _IDENTIFIER, "request_id")
        _text(self.production_workspace_handle, _PRODUCTION_HANDLE, "production workspace handle")
        _text(self.production_workspace_id, _IDENTIFIER, "production workspace id")
        _revision(self.expected_production_workspace_revision, "production revision")
        _text(
            self.expected_production_workspace_fingerprint,
            _FINGERPRINT,
            "production fingerprint",
        )
        _text(self.authoring_workspace_handle, _AUTHORING_HANDLE, "authoring workspace handle")
        _text(
            self.expected_authoring_registry_fingerprint,
            _FINGERPRINT,
            "authoring registry fingerprint",
        )
        _revision(self.expected_authoring_reference_revision, "authoring reference revision")
        _revision(self.expected_authoring_timeline_revision, "authoring timeline revision")
        _text(
            self.expected_authoring_timeline_content_fingerprint,
            _FINGERPRINT,
            "authoring timeline fingerprint",
        )
        _revision(self.expected_nle_workspace_revision, "NLE workspace revision")
        _revision(self.expected_nle_timeline_revision, "NLE timeline revision")
        _text(self.expected_nle_timeline_fingerprint, _FINGERPRINT, "NLE timeline fingerprint")
        _text(self.expected_nle_public_fingerprint, _FINGERPRINT, "NLE public fingerprint")
        if (
            type(self.entries) is not tuple
            or not 1 <= len(self.entries) <= MAX_PRODUCTION_AUTHORING_IMPORT_ENTRIES
            or not all(type(row) is ProductionAuthoringImportEntry for row in self.entries)
        ):
            raise ValueError("import entries are invalid")
        if len({row.segment_id for row in self.entries}) != len(self.entries) or len(
            {row.output_handle for row in self.entries}
        ) != len(self.entries):
            raise ValueError("import entries contain duplicates")
        if len(canonical_bytes(self.to_wire())) > MAX_PRODUCTION_AUTHORING_IMPORT_BYTES:
            raise ValueError("import request exceeds its byte bound")

    @property
    def digest(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "action": self.action,
            "request_id": self.request_id,
            "production_workspace_handle": self.production_workspace_handle,
            "production_workspace_id": self.production_workspace_id,
            "expected_production_workspace_revision": (self.expected_production_workspace_revision),
            "expected_production_workspace_fingerprint": (
                self.expected_production_workspace_fingerprint
            ),
            "authoring_workspace_handle": self.authoring_workspace_handle,
            "expected_authoring_registry_fingerprint": (
                self.expected_authoring_registry_fingerprint
            ),
            "expected_authoring_reference_revision": self.expected_authoring_reference_revision,
            "expected_authoring_timeline_revision": self.expected_authoring_timeline_revision,
            "expected_authoring_timeline_content_fingerprint": (
                self.expected_authoring_timeline_content_fingerprint
            ),
            "expected_nle_workspace_revision": self.expected_nle_workspace_revision,
            "expected_nle_timeline_revision": self.expected_nle_timeline_revision,
            "expected_nle_timeline_fingerprint": self.expected_nle_timeline_fingerprint,
            "expected_nle_public_fingerprint": self.expected_nle_public_fingerprint,
            "entries": [row.to_wire() for row in self.entries],
        }

    @classmethod
    def from_wire(cls, value: object) -> ProductionAuthoringImportRequest:
        if type(value) is not dict or set(value) != _REQUEST_KEYS:
            raise ValueError("import request must be one closed object")
        wire = cast(dict[str, object], value)
        rows = wire["entries"]
        if type(rows) is not list or not 1 <= len(rows) <= MAX_PRODUCTION_AUTHORING_IMPORT_ENTRIES:
            raise ValueError("import entries are invalid")
        entries: list[ProductionAuthoringImportEntry] = []
        for index, row in enumerate(rows):
            if type(row) is not dict or set(row) != {"segment_id", "output_handle"}:
                raise ValueError(f"entries[{index}] is not closed")
            entries.append(
                ProductionAuthoringImportEntry(
                    segment_id=_text(
                        row["segment_id"], _IDENTIFIER, f"entries[{index}].segment_id"
                    ),
                    output_handle=_text(
                        row["output_handle"], _OUTPUT_HANDLE, f"entries[{index}].output_handle"
                    ),
                )
            )
        return cls(
            request_id=_text(wire["request_id"], _IDENTIFIER, "request_id"),
            production_workspace_handle=_text(
                wire["production_workspace_handle"], _PRODUCTION_HANDLE, "production handle"
            ),
            production_workspace_id=_text(
                wire["production_workspace_id"], _IDENTIFIER, "production workspace id"
            ),
            expected_production_workspace_revision=_revision(
                wire["expected_production_workspace_revision"], "production revision"
            ),
            expected_production_workspace_fingerprint=_text(
                wire["expected_production_workspace_fingerprint"],
                _FINGERPRINT,
                "production fingerprint",
            ),
            authoring_workspace_handle=_text(
                wire["authoring_workspace_handle"], _AUTHORING_HANDLE, "authoring handle"
            ),
            expected_authoring_registry_fingerprint=_text(
                wire["expected_authoring_registry_fingerprint"],
                _FINGERPRINT,
                "authoring registry fingerprint",
            ),
            expected_authoring_reference_revision=_revision(
                wire["expected_authoring_reference_revision"], "authoring reference revision"
            ),
            expected_authoring_timeline_revision=_revision(
                wire["expected_authoring_timeline_revision"], "authoring timeline revision"
            ),
            expected_authoring_timeline_content_fingerprint=_text(
                wire["expected_authoring_timeline_content_fingerprint"],
                _FINGERPRINT,
                "authoring timeline fingerprint",
            ),
            expected_nle_workspace_revision=_revision(
                wire["expected_nle_workspace_revision"], "NLE workspace revision"
            ),
            expected_nle_timeline_revision=_revision(
                wire["expected_nle_timeline_revision"], "NLE timeline revision"
            ),
            expected_nle_timeline_fingerprint=_text(
                wire["expected_nle_timeline_fingerprint"], _FINGERPRINT, "NLE timeline fingerprint"
            ),
            expected_nle_public_fingerprint=_text(
                wire["expected_nle_public_fingerprint"], _FINGERPRINT, "NLE public fingerprint"
            ),
            entries=tuple(entries),
            schema=str(wire["schema"]),
            action=str(wire["action"]),
        )


@dataclass(frozen=True, slots=True)
class ProductionAuthoringImportRequestV2:
    request_id: str
    production_workspace_handle: str
    production_workspace_id: str
    expected_production_workspace_revision: int
    expected_production_workspace_fingerprint: str
    authoring_workspace_handle: str
    expected_authoring_registry_fingerprint: str
    expected_authoring_reference_revision: int
    expected_authoring_timeline_revision: int
    expected_authoring_timeline_content_fingerprint: str
    expected_nle_workspace_revision: int
    expected_nle_timeline_revision: int
    expected_nle_timeline_fingerprint: str
    expected_nle_authoring_fingerprint: str
    authoring_schema: str
    profile_id: str
    entries: tuple[ProductionAuthoringImportEntry, ...]
    schema: str = PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA_V2
    action: str = PRODUCTION_AUTHORING_IMPORT_ACTION

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA_V2:
            raise ValueError("import request schema is unsupported")
        if self.action != PRODUCTION_AUTHORING_IMPORT_ACTION:
            raise ValueError("import request action is unsupported")
        _text(self.request_id, _IDENTIFIER, "request_id")
        _text(self.production_workspace_handle, _PRODUCTION_HANDLE, "production workspace handle")
        _text(self.production_workspace_id, _IDENTIFIER, "production workspace id")
        _revision(self.expected_production_workspace_revision, "production revision")
        _text(
            self.expected_production_workspace_fingerprint,
            _FINGERPRINT,
            "production fingerprint",
        )
        _text(self.authoring_workspace_handle, _AUTHORING_HANDLE, "authoring workspace handle")
        _text(
            self.expected_authoring_registry_fingerprint,
            _FINGERPRINT,
            "authoring registry fingerprint",
        )
        _revision(self.expected_authoring_reference_revision, "authoring reference revision")
        _revision(self.expected_authoring_timeline_revision, "authoring timeline revision")
        _text(
            self.expected_authoring_timeline_content_fingerprint,
            _FINGERPRINT,
            "authoring timeline fingerprint",
        )
        _revision(self.expected_nle_workspace_revision, "NLE workspace revision")
        _revision(self.expected_nle_timeline_revision, "NLE timeline revision")
        _text(self.expected_nle_timeline_fingerprint, _FINGERPRINT, "NLE timeline fingerprint")
        _text(
            self.expected_nle_authoring_fingerprint,
            _FINGERPRINT,
            "NLE authoring fingerprint",
        )
        if (self.authoring_schema, self.profile_id) != (
            "h3.context.nle_authoring_state.v1",
            "h3.authoring.nle_content_extent.v1",
        ):
            raise ValueError("NLE authoring profile is unsupported")
        if (
            type(self.entries) is not tuple
            or not 1 <= len(self.entries) <= MAX_PRODUCTION_AUTHORING_IMPORT_ENTRIES
            or not all(type(row) is ProductionAuthoringImportEntry for row in self.entries)
        ):
            raise ValueError("import entries are invalid")
        if len({row.segment_id for row in self.entries}) != len(self.entries) or len(
            {row.output_handle for row in self.entries}
        ) != len(self.entries):
            raise ValueError("import entries contain duplicates")
        if len(canonical_bytes(self.to_wire())) > MAX_PRODUCTION_AUTHORING_IMPORT_BYTES:
            raise ValueError("import request exceeds its byte bound")

    @property
    def digest(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "action": self.action,
            "request_id": self.request_id,
            "production_workspace_handle": self.production_workspace_handle,
            "production_workspace_id": self.production_workspace_id,
            "expected_production_workspace_revision": self.expected_production_workspace_revision,
            "expected_production_workspace_fingerprint": (
                self.expected_production_workspace_fingerprint
            ),
            "authoring_workspace_handle": self.authoring_workspace_handle,
            "expected_authoring_registry_fingerprint": self.expected_authoring_registry_fingerprint,
            "expected_authoring_reference_revision": self.expected_authoring_reference_revision,
            "expected_authoring_timeline_revision": self.expected_authoring_timeline_revision,
            "expected_authoring_timeline_content_fingerprint": (
                self.expected_authoring_timeline_content_fingerprint
            ),
            "expected_nle_workspace_revision": self.expected_nle_workspace_revision,
            "expected_nle_timeline_revision": self.expected_nle_timeline_revision,
            "expected_nle_timeline_fingerprint": self.expected_nle_timeline_fingerprint,
            "authoring_schema": self.authoring_schema,
            "profile_id": self.profile_id,
            "expected_nle_authoring_fingerprint": self.expected_nle_authoring_fingerprint,
            "entries": [row.to_wire() for row in self.entries],
        }

    @classmethod
    def from_wire(cls, value: object) -> ProductionAuthoringImportRequestV2:
        if type(value) is not dict or set(value) != _REQUEST_KEYS_V2:
            raise ValueError("import request v2 must be one closed object")
        wire = cast(dict[str, object], value)
        rows = wire["entries"]
        if type(rows) is not list or not 1 <= len(rows) <= MAX_PRODUCTION_AUTHORING_IMPORT_ENTRIES:
            raise ValueError("import entries are invalid")
        entries: list[ProductionAuthoringImportEntry] = []
        for index, row in enumerate(rows):
            if type(row) is not dict or set(row) != {"segment_id", "output_handle"}:
                raise ValueError(f"entries[{index}] is not closed")
            entries.append(
                ProductionAuthoringImportEntry(
                    segment_id=_text(
                        row["segment_id"], _IDENTIFIER, f"entries[{index}].segment_id"
                    ),
                    output_handle=_text(
                        row["output_handle"], _OUTPUT_HANDLE, f"entries[{index}].output_handle"
                    ),
                )
            )
        return cls(
            request_id=_text(wire["request_id"], _IDENTIFIER, "request_id"),
            production_workspace_handle=_text(
                wire["production_workspace_handle"], _PRODUCTION_HANDLE, "production handle"
            ),
            production_workspace_id=_text(
                wire["production_workspace_id"], _IDENTIFIER, "production workspace id"
            ),
            expected_production_workspace_revision=_revision(
                wire["expected_production_workspace_revision"], "production revision"
            ),
            expected_production_workspace_fingerprint=_text(
                wire["expected_production_workspace_fingerprint"],
                _FINGERPRINT,
                "production fingerprint",
            ),
            authoring_workspace_handle=_text(
                wire["authoring_workspace_handle"], _AUTHORING_HANDLE, "authoring handle"
            ),
            expected_authoring_registry_fingerprint=_text(
                wire["expected_authoring_registry_fingerprint"],
                _FINGERPRINT,
                "authoring registry fingerprint",
            ),
            expected_authoring_reference_revision=_revision(
                wire["expected_authoring_reference_revision"], "authoring reference revision"
            ),
            expected_authoring_timeline_revision=_revision(
                wire["expected_authoring_timeline_revision"], "authoring timeline revision"
            ),
            expected_authoring_timeline_content_fingerprint=_text(
                wire["expected_authoring_timeline_content_fingerprint"],
                _FINGERPRINT,
                "authoring timeline fingerprint",
            ),
            expected_nle_workspace_revision=_revision(
                wire["expected_nle_workspace_revision"], "NLE workspace revision"
            ),
            expected_nle_timeline_revision=_revision(
                wire["expected_nle_timeline_revision"], "NLE timeline revision"
            ),
            expected_nle_timeline_fingerprint=_text(
                wire["expected_nle_timeline_fingerprint"], _FINGERPRINT, "NLE timeline fingerprint"
            ),
            expected_nle_authoring_fingerprint=_text(
                wire["expected_nle_authoring_fingerprint"],
                _FINGERPRINT,
                "NLE authoring fingerprint",
            ),
            authoring_schema=_text(wire["authoring_schema"], _IDENTIFIER, "authoring schema"),
            profile_id=_text(wire["profile_id"], _IDENTIFIER, "authoring profile"),
            entries=tuple(entries),
            schema=str(wire["schema"]),
            action=str(wire["action"]),
        )


@dataclass(frozen=True, slots=True)
class ProductionAuthoringImportRow:
    segment_id: str
    output_handle: str
    asset_id: str
    source_kind: str
    disposition: str

    def __post_init__(self) -> None:
        _text(self.segment_id, _IDENTIFIER, "receipt segment_id")
        _text(self.output_handle, _OUTPUT_HANDLE, "receipt output_handle")
        _text(self.asset_id, _IDENTIFIER, "receipt asset_id")
        if self.source_kind != "video" or self.disposition not in {"created", "already_imported"}:
            raise ValueError("import receipt row is invalid")

    def to_wire(self) -> dict[str, str]:
        return {
            "segment_id": self.segment_id,
            "output_handle": self.output_handle,
            "asset_id": self.asset_id,
            "source_kind": self.source_kind,
            "disposition": self.disposition,
        }


@dataclass(frozen=True, slots=True)
class ProductionAuthoringImportReceipt:
    request_id: str
    disposition: str
    production_workspace_id: str
    production_workspace_revision: int
    production_workspace_fingerprint: str
    authoring_workspace_handle: str
    authoring_registry_fingerprint: str
    prior_reference_revision: int
    next_reference_revision: int
    prior_reference_fingerprint: str
    next_reference_fingerprint: str
    prior_timeline_revision: int
    next_timeline_revision: int
    prior_timeline_content_fingerprint: str
    next_timeline_content_fingerprint: str
    prior_nle_workspace_revision: int
    next_nle_workspace_revision: int
    prior_nle_workspace_fingerprint: str
    next_nle_workspace_fingerprint: str
    prior_nle_timeline_revision: int
    next_nle_timeline_revision: int
    prior_nle_timeline_fingerprint: str
    next_nle_timeline_fingerprint: str
    prior_nle_public_fingerprint: str
    next_nle_public_fingerprint: str
    rows: tuple[ProductionAuthoringImportRow, ...]
    authority_versions: tuple[str, ...]
    schema: str = PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        _text(self.request_id, _IDENTIFIER, "receipt request_id")
        if self.disposition not in {"created", "already_imported"}:
            raise ValueError("import receipt disposition is invalid")
        _text(self.production_workspace_id, _IDENTIFIER, "receipt production workspace id")
        _text(self.authoring_workspace_handle, _AUTHORING_HANDLE, "receipt authoring handle")
        for fingerprint_value in (
            self.production_workspace_fingerprint,
            self.authoring_registry_fingerprint,
            self.prior_reference_fingerprint,
            self.next_reference_fingerprint,
            self.prior_timeline_content_fingerprint,
            self.next_timeline_content_fingerprint,
            self.prior_nle_workspace_fingerprint,
            self.next_nle_workspace_fingerprint,
            self.prior_nle_timeline_fingerprint,
            self.next_nle_timeline_fingerprint,
            self.prior_nle_public_fingerprint,
            self.next_nle_public_fingerprint,
        ):
            _text(fingerprint_value, _FINGERPRINT, "receipt fingerprint")
        for revision_value in (
            self.production_workspace_revision,
            self.prior_reference_revision,
            self.next_reference_revision,
            self.prior_timeline_revision,
            self.next_timeline_revision,
            self.prior_nle_workspace_revision,
            self.next_nle_workspace_revision,
            self.prior_nle_timeline_revision,
            self.next_nle_timeline_revision,
        ):
            _revision(revision_value, "receipt revision")
        if (
            type(self.rows) is not tuple
            or not 1 <= len(self.rows) <= MAX_PRODUCTION_AUTHORING_IMPORT_ENTRIES
            or not all(type(row) is ProductionAuthoringImportRow for row in self.rows)
            or len({row.segment_id for row in self.rows}) != len(self.rows)
            or len({row.output_handle for row in self.rows}) != len(self.rows)
            or len({row.asset_id for row in self.rows}) != len(self.rows)
        ):
            raise ValueError("import receipt rows are invalid")
        changed = any(row.disposition == "created" for row in self.rows)
        if (self.disposition == "created") != changed:
            raise ValueError("import receipt disposition is inconsistent")
        if type(self.authority_versions) is not tuple or self.authority_versions != (
            PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA,
            PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA,
            "h3.context.segment_artifact_receipt.v1",
            "h3.context.authoring_source.generated.v1",
        ):
            raise ValueError("import authority versions are invalid")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "disposition": self.disposition,
            "production_workspace_id": self.production_workspace_id,
            "production_workspace_revision": self.production_workspace_revision,
            "production_workspace_fingerprint": self.production_workspace_fingerprint,
            "authoring_workspace_handle": self.authoring_workspace_handle,
            "authoring_registry_fingerprint": self.authoring_registry_fingerprint,
            "reference": {
                "prior_revision": self.prior_reference_revision,
                "next_revision": self.next_reference_revision,
                "prior_fingerprint": self.prior_reference_fingerprint,
                "next_fingerprint": self.next_reference_fingerprint,
            },
            "legacy_timeline": {
                "prior_revision": self.prior_timeline_revision,
                "next_revision": self.next_timeline_revision,
                "prior_content_fingerprint": self.prior_timeline_content_fingerprint,
                "next_content_fingerprint": self.next_timeline_content_fingerprint,
            },
            "nle": {
                "prior_workspace_revision": self.prior_nle_workspace_revision,
                "next_workspace_revision": self.next_nle_workspace_revision,
                "prior_workspace_fingerprint": self.prior_nle_workspace_fingerprint,
                "next_workspace_fingerprint": self.next_nle_workspace_fingerprint,
                "prior_timeline_revision": self.prior_nle_timeline_revision,
                "next_timeline_revision": self.next_nle_timeline_revision,
                "prior_timeline_fingerprint": self.prior_nle_timeline_fingerprint,
                "next_timeline_fingerprint": self.next_nle_timeline_fingerprint,
                "prior_public_fingerprint": self.prior_nle_public_fingerprint,
                "next_public_fingerprint": self.next_nle_public_fingerprint,
            },
            "rows": [row.to_wire() for row in self.rows],
            "authority_versions": list(self.authority_versions),
        }


@dataclass(frozen=True, slots=True)
class ProductionAuthoringImportResponse:
    receipt: ProductionAuthoringImportReceipt
    authoring_projection: dict[str, object]
    schema: str = PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA:
            raise ValueError("import response schema is unsupported")
        if type(self.receipt) is not ProductionAuthoringImportReceipt:
            raise TypeError("import response receipt is invalid")
        if type(self.authoring_projection) is not dict:
            raise TypeError("import response projection is invalid")
        if len(canonical_bytes(self.to_wire())) > 1_048_576:
            raise ValueError("import response exceeds its byte bound")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "receipt": self.receipt.to_wire(),
            "authoring_projection": self.authoring_projection,
        }


@dataclass(frozen=True, slots=True)
class ProductionAuthoringImportReceiptV2:
    request_id: str
    disposition: str
    production_workspace_id: str
    production_workspace_revision: int
    production_workspace_fingerprint: str
    authoring_workspace_handle: str
    authoring_registry_fingerprint: str
    prior_reference_revision: int
    next_reference_revision: int
    prior_reference_fingerprint: str
    next_reference_fingerprint: str
    prior_timeline_revision: int
    next_timeline_revision: int
    prior_timeline_content_fingerprint: str
    next_timeline_content_fingerprint: str
    authoring_schema: str
    profile_id: str
    prior_nle_workspace_revision: int
    next_nle_workspace_revision: int
    prior_nle_workspace_fingerprint: str
    next_nle_workspace_fingerprint: str
    prior_nle_timeline_revision: int
    next_nle_timeline_revision: int
    prior_nle_timeline_fingerprint: str
    next_nle_timeline_fingerprint: str
    prior_nle_authoring_fingerprint: str
    next_nle_authoring_fingerprint: str
    rows: tuple[ProductionAuthoringImportRow, ...]
    authority_versions: tuple[str, ...]
    schema: str = PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2:
            raise ValueError("import receipt schema is unsupported")
        if self.disposition not in {"created", "already_imported"}:
            raise ValueError("import receipt disposition is invalid")
        _text(self.request_id, _IDENTIFIER, "receipt request_id")
        _text(self.production_workspace_id, _IDENTIFIER, "receipt production workspace id")
        _text(self.authoring_workspace_handle, _AUTHORING_HANDLE, "receipt authoring handle")
        fingerprints = (
            self.production_workspace_fingerprint,
            self.authoring_registry_fingerprint,
            self.prior_reference_fingerprint,
            self.next_reference_fingerprint,
            self.prior_timeline_content_fingerprint,
            self.next_timeline_content_fingerprint,
            self.prior_nle_workspace_fingerprint,
            self.next_nle_workspace_fingerprint,
            self.prior_nle_timeline_fingerprint,
            self.next_nle_timeline_fingerprint,
            self.prior_nle_authoring_fingerprint,
            self.next_nle_authoring_fingerprint,
        )
        for value in fingerprints:
            _text(value, _FINGERPRINT, "receipt fingerprint")
        if (self.authoring_schema, self.profile_id) != (
            "h3.context.nle_authoring_state.v1",
            "h3.authoring.nle_content_extent.v1",
        ):
            raise ValueError("NLE authoring profile is unsupported")
        revisions = (
            self.production_workspace_revision,
            self.prior_reference_revision,
            self.next_reference_revision,
            self.prior_timeline_revision,
            self.next_timeline_revision,
            self.prior_nle_workspace_revision,
            self.next_nle_workspace_revision,
            self.prior_nle_timeline_revision,
            self.next_nle_timeline_revision,
        )
        for revision in revisions:
            _revision(revision, "receipt revision")
        if (
            type(self.rows) is not tuple
            or not 1 <= len(self.rows) <= MAX_PRODUCTION_AUTHORING_IMPORT_ENTRIES
            or not all(type(row) is ProductionAuthoringImportRow for row in self.rows)
            or len({row.segment_id for row in self.rows}) != len(self.rows)
            or len({row.output_handle for row in self.rows}) != len(self.rows)
            or len({row.asset_id for row in self.rows}) != len(self.rows)
        ):
            raise ValueError("import receipt rows are invalid")
        changed = any(row.disposition == "created" for row in self.rows)
        if (self.disposition == "created") != changed:
            raise ValueError("import receipt disposition is inconsistent")
        if type(self.authority_versions) is not tuple or self.authority_versions != (
            PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA_V2,
            PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2,
            "h3.context.segment_artifact_receipt.v1",
            "h3.context.authoring_source.generated.v1",
            self.authoring_schema,
            self.profile_id,
        ):
            raise ValueError("import authority versions are invalid")
        if (
            self.next_reference_revision != self.prior_reference_revision + int(changed)
            or self.next_timeline_revision != self.prior_timeline_revision
            or self.next_nle_workspace_revision != self.prior_nle_workspace_revision + int(changed)
            or self.next_nle_timeline_revision != self.prior_nle_timeline_revision
            or (self.prior_reference_fingerprint == self.next_reference_fingerprint) == changed
            or (self.prior_nle_workspace_fingerprint == self.next_nle_workspace_fingerprint)
            == changed
            or (self.prior_nle_authoring_fingerprint == self.next_nle_authoring_fingerprint)
            == changed
            or self.prior_timeline_content_fingerprint != self.next_timeline_content_fingerprint
            or self.prior_nle_timeline_fingerprint != self.next_nle_timeline_fingerprint
        ):
            raise ValueError("import receipt revision transition is invalid")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "disposition": self.disposition,
            "production_workspace_id": self.production_workspace_id,
            "production_workspace_revision": self.production_workspace_revision,
            "production_workspace_fingerprint": self.production_workspace_fingerprint,
            "authoring_workspace_handle": self.authoring_workspace_handle,
            "authoring_registry_fingerprint": self.authoring_registry_fingerprint,
            "reference": {
                "prior_revision": self.prior_reference_revision,
                "next_revision": self.next_reference_revision,
                "prior_fingerprint": self.prior_reference_fingerprint,
                "next_fingerprint": self.next_reference_fingerprint,
            },
            "legacy_timeline": {
                "prior_revision": self.prior_timeline_revision,
                "next_revision": self.next_timeline_revision,
                "prior_content_fingerprint": self.prior_timeline_content_fingerprint,
                "next_content_fingerprint": self.next_timeline_content_fingerprint,
            },
            "nle_authoring": {
                "authoring_schema": self.authoring_schema,
                "profile_id": self.profile_id,
                "prior_workspace_revision": self.prior_nle_workspace_revision,
                "next_workspace_revision": self.next_nle_workspace_revision,
                "prior_workspace_fingerprint": self.prior_nle_workspace_fingerprint,
                "next_workspace_fingerprint": self.next_nle_workspace_fingerprint,
                "prior_timeline_revision": self.prior_nle_timeline_revision,
                "next_timeline_revision": self.next_nle_timeline_revision,
                "prior_timeline_fingerprint": self.prior_nle_timeline_fingerprint,
                "next_timeline_fingerprint": self.next_nle_timeline_fingerprint,
                "prior_authoring_fingerprint": self.prior_nle_authoring_fingerprint,
                "next_authoring_fingerprint": self.next_nle_authoring_fingerprint,
            },
            "rows": [row.to_wire() for row in self.rows],
            "authority_versions": list(self.authority_versions),
        }


@dataclass(frozen=True, slots=True)
class ProductionAuthoringImportResponseV2:
    receipt: ProductionAuthoringImportReceiptV2
    authoring_projection: dict[str, object]
    history_projection: dict[str, object]
    schema: str = PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA_V2

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA_V2:
            raise ValueError("import response schema is unsupported")
        if type(self.receipt) is not ProductionAuthoringImportReceiptV2:
            raise TypeError("import response receipt is invalid")
        if type(self.authoring_projection) is not dict or type(self.history_projection) is not dict:
            raise TypeError("import response projection is invalid")
        try:
            encoded = json.dumps(
                self.to_wire(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ValueError("import response is not bounded JSON") from exc
        if len(encoded) > 1_048_576:
            raise ValueError("import response exceeds its byte bound")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "receipt": self.receipt.to_wire(),
            "authoring_projection": self.authoring_projection,
            "history_projection": self.history_projection,
        }


def decode_production_authoring_import_json(
    payload: bytes,
) -> ProductionAuthoringImportRequest | ProductionAuthoringImportRequestV2:
    if (
        type(payload) is not bytes
        or not payload
        or len(payload) > MAX_PRODUCTION_AUTHORING_IMPORT_BYTES
    ):
        raise ValueError("import request bytes are invalid")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("import request constant is invalid")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("import request JSON is invalid") from exc
    if (
        isinstance(value, dict)
        and value.get("schema") == PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA_V2
    ):
        return ProductionAuthoringImportRequestV2.from_wire(value)
    return ProductionAuthoringImportRequest.from_wire(value)


__all__ = [
    "MAX_PRODUCTION_AUTHORING_IMPORT_BYTES",
    "MAX_PRODUCTION_AUTHORING_IMPORT_ENTRIES",
    "PRODUCTION_AUTHORING_IMPORT_ACTION",
    "PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA",
    "PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA",
    "PRODUCTION_AUTHORING_IMPORT_RECEIPT_SCHEMA_V2",
    "PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA_V2",
    "PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA_V2",
    "PRODUCTION_AUTHORING_IMPORT_RESPONSE_SCHEMA",
    "ProductionAuthoringImportEntry",
    "ProductionAuthoringImportReceipt",
    "ProductionAuthoringImportReceiptV2",
    "ProductionAuthoringImportRequest",
    "ProductionAuthoringImportRequestV2",
    "ProductionAuthoringImportResponse",
    "ProductionAuthoringImportResponseV2",
    "ProductionAuthoringImportRow",
    "decode_production_authoring_import_json",
]
