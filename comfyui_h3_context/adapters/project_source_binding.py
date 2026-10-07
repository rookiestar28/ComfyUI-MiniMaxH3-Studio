"""Fresh exact retained sources may alias portable asset IDs after backend qualification."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..core.registry import ReferenceRegistry
from .authoring_source_binding import (
    AuthoringSourceBindingError,
    AuthoringSourceBindingReceipt,
    RuntimeVideoCapability,
    claim_transferred_authoring_source,
)
from .retained_asset_use import RetainedAssetUse, RetainedVideoSource


class ProjectSourceBindingReceipt(AuthoringSourceBindingReceipt):
    def __init__(
        self,
        uses: Mapping[str, RetainedAssetUse],
        *,
        new_use: RetainedAssetUse,
        previous: ProjectSourceBindingReceipt | None,
    ) -> None:
        if (
            any(type(use) is not RetainedAssetUse for use in uses.values())
            or type(new_use) is not RetainedAssetUse
        ):
            raise AuthoringSourceBindingError("source_origin_unregistered")
        # SECURITY: each use is freshly issued against a new exact registry; file IDs only alias
        # that receipt. No copied fingerprint, handle, former Context or user object can bind it.
        for use in uses.values():
            source = claim_transferred_authoring_source(use.receipt, use.source_id)
            if type(source) is not RetainedVideoSource or source is not use.source:
                raise AuthoringSourceBindingError("source_origin_unregistered")
        super().__init__(
            exact_registry=ReferenceRegistry.empty(),
            generation=1 if previous is None else previous.generation + 1,
        )
        self.uses = MappingProxyType(dict(uses))
        self._owns = False
        self._new_use: RetainedAssetUse | None = new_use

    def activate(self, previous: ProjectSourceBindingReceipt | None) -> None:
        if previous is not None:
            previous._owns, previous._new_use = False, None
            previous.release()
        self._owns, self._new_use = True, None

    def _claim_source(self, source_id: str) -> object:
        use = self.uses.get(source_id)
        if use is None:
            raise AuthoringSourceBindingError("source_not_found")
        return claim_transferred_authoring_source(use.receipt, use.source_id)

    def _capability_for(self, source_id: str) -> RuntimeVideoCapability:
        use = self.uses.get(source_id)
        if use is None:
            raise AuthoringSourceBindingError("source_not_found")
        return (
            RuntimeVideoCapability.AVAILABLE
            if use.source.current()
            else RuntimeVideoCapability.STALE
        )

    def _duration_for(self, source_id: str) -> int | None:
        use = self.uses.get(source_id)
        return None if use is None or not use.source.current() else use.source.duration_milliseconds

    def _release_sources(self) -> None:
        uses = (
            tuple(self.uses.values())
            if self._owns
            else (() if self._new_use is None else (self._new_use,))
        )
        first_error = None
        for use in uses:
            try:
                use.release()
            except Exception as error:
                first_error = first_error or error
        if first_error is not None:
            raise first_error
