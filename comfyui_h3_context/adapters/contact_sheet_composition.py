"""Explicit injected contact-sheet composition seam for M21-02.

Like `adapters/video_decode.py`, this adapter is a qualification fixture boundary rather than a
compositor implementation.  A later caller may replace the producer with a bounded image pipeline
after host and media gates pass; the contract checks below do not change when it does.

The seam exists so the failure discipline lives in one place.  A composer either returns a sheet
that matches the plan it was given, byte for byte and fingerprint for fingerprint, or the caller
gets a typed refusal and no value at all.  There is no partial sheet, and a failed composition
cannot replace a sheet the caller already holds.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from comfyui_h3_context.core.contact_sheet import (
    MAX_CONTACT_SHEET_BYTES,
    ContactSheetDocument,
    ContactSheetPlan,
)
from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.errors import ContactSheetError
from comfyui_h3_context.core.local_adapters import (
    LocalAdapterDescriptor,
    LocalAdapterKind,
    LocalBudgetGuard,
    LocalDeviceKind,
    LocalResourceBudget,
)
from comfyui_h3_context.core.vlm_observation import ContactSheetPayload

ContactSheetProducer = Callable[
    [ContactSheetPlan, LocalBudgetGuard], tuple[ContactSheetDocument, bytes]
]


class InjectedContactSheetComposer:
    """Return a caller-injected composed sheet without opening media or importing a compositor."""

    def __init__(
        self,
        producer: ContactSheetProducer,
        *,
        adapter_id: str = "injected_contact_sheet",
    ) -> None:
        if not callable(producer):
            raise ContactSheetError("injected contact sheet producer must be callable")
        self._producer = producer
        self._descriptor = LocalAdapterDescriptor(
            adapter_id=adapter_id,
            kind=LocalAdapterKind.PERCEPTION,
            adapter_version="1.0.0",
            minimum_version="1.0.0",
            maximum_version="1.0.0",
            supported_task_modes=frozenset({TaskMode.REF2VA}),
            supported_media=frozenset({MediaKind.VIDEO}),
            supported_devices=frozenset(LocalDeviceKind),
            optional_dependencies=(),
            output_schema="h3.contact.sheet.v1",
            limits=LocalResourceBudget(2_048 * 1024 * 1024, 30.0, 3, MAX_CONTACT_SHEET_BYTES, 1, 1),
            supports_determinism=True,
            supports_seed=False,
            supports_cancellation=True,
        )

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._descriptor

    def compose(self, plan: ContactSheetPlan, guard: LocalBudgetGuard) -> ContactSheetPayload:
        """Compose one sheet for one plan, or refuse; never return a partial or unbound result."""

        if not isinstance(plan, ContactSheetPlan):
            raise ContactSheetError("composition requires a ContactSheetPlan")
        if not isinstance(guard, LocalBudgetGuard):
            raise ContactSheetError("composition requires a LocalBudgetGuard")
        try:
            produced = self._producer(plan, guard)
        except ContactSheetError:
            raise
        except Exception as exc:  # noqa: BLE001 - a foreign composer failure is a typed refusal
            raise ContactSheetError("contact sheet composition failed") from exc
        if not isinstance(produced, tuple) or len(produced) != 2:
            raise ContactSheetError("contact sheet producer must return a document and its bytes")
        document, payload = produced
        if not isinstance(document, ContactSheetDocument):
            raise ContactSheetError("contact sheet producer returned an invalid document")
        if not isinstance(payload, bytes) or not payload or len(payload) > MAX_CONTACT_SHEET_BYTES:
            raise ContactSheetError("composed sheet bytes exceed the bounded limit")
        # The composer is untrusted like any other producer: it may not substitute a different
        # plan, and its declared fingerprint must actually be the fingerprint of what it returned.
        if document.plan != plan:
            raise ContactSheetError("composed sheet does not carry the requested plan")
        if document.content_fingerprint != "sha256:" + hashlib.sha256(payload).hexdigest():
            raise ContactSheetError("composed sheet fingerprint does not match its bytes")
        return ContactSheetPayload(sheet=document, payload=payload)


__all__ = ["ContactSheetProducer", "InjectedContactSheetComposer"]
