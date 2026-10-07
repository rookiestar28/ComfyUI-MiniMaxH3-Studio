"""Opt-in retained media has its own charged namespace, owner lease and atomic catalog."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from ..core.composition_contract import composition_contract_fingerprint
from ..core.durable_workspace_state import (
    MAX_OWNER_REVISION,
    DurableStateError,
    json_bytes,
    require_owner,
    strict_json,
)
from ..core.private_storage_layout import MIB, private_subtree_path
from ..core.retained_assets import (
    CLOSED_RETENTION_MS,
    MAX_CATALOG_BYTES,
    MAX_GLOBAL_ASSETS,
    MAX_OWNER_ASSETS,
    MAX_RETAINED_ASSET_BYTES,
    RETAINED_MEDIA_PROFILE,
    RetainedAsset,
    RetainedAssetError,
    RetainedCatalog,
    decode_asset,
    decode_catalog,
    encode_catalog,
    require_asset_id,
    require_project_reference,
)
from ..core.safe_paths import (
    UnsafePathError,
    ensure_directory,
    validate_directory,
    validate_regular_file,
)
from .authoring_source_binding import AuthoringSourceBindingError
from .authoring_video_facts import AuthoringVideoFactsError, probe_authoring_video_facts
from .av_reconstruction_media import QualifiedAVMediaAdapter
from .durable_state_store import _delete, _exists, _read, _replace_manifest
from .managed_artifact_scopes import _acquire_lock, _create_or_admit
from .media_runtime_resolution import _link_free_existing_prefix
from .media_subprocess import create_output_lease
from .retained_asset_source import check_retention_budget, require_retention_source
from .retained_asset_use import RetainedAssetUse, _mint_retained_use
from .segment_artifact_store import (
    ArtifactStoreError,
    _copy_regular_file_into_new,
    _directory_identity,
    _identity,
    _read_regular_bytes,
    _validated_directories,
    _write_new_file,
)

_FAMILY = ".h3-retained-assets-v1"
_GLOBAL_LOCK = ".catalog.lock"
_OWNER_LOCK = ".owner.lock"
_CATALOG = "manifest.json"
_PENDING = "manifest.next.json"
_FAMILY_BYTES = json_bytes({"schema": "h3.context.retained_asset_family.v1"})
_GLOBAL_BYTES = json_bytes({"schema": "h3.context.retained_asset_catalog_lock.v1"})
_FILE = re.compile(r"(asset_[0-9a-f]{32})\.mp4\Z")
_PROOF = re.compile(r"(intent|verified|garbage)-(asset_[0-9a-f]{32})\.json\Z")
_PROOF_BYTES = 2048
_USE = re.compile(r"retained_[0-9a-f]{32}\Z")
_USE_TTL = 15 * 60


@dataclass(frozen=True, slots=True)
class RetainedStoreLimits:
    max_owner_assets: int = MAX_OWNER_ASSETS
    max_global_assets: int = MAX_GLOBAL_ASSETS
    max_owners: int = 16
    max_owner_bytes: int = 256 * MIB
    max_total_bytes: int = 1024 * MIB

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.max_owner_assets, MAX_OWNER_ASSETS),
            (self.max_global_assets, MAX_GLOBAL_ASSETS),
            (self.max_owners, 16),
            (self.max_owner_bytes, 256 * MIB),
            (self.max_total_bytes, 1024 * MIB),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise RetainedAssetError("limits_invalid")


@dataclass(frozen=True, slots=True)
class RetainedCleanup:
    catalog: RetainedCatalog
    removed: int
    protected: int


@dataclass(frozen=True, slots=True)
class RetainedInventory:
    catalog: RetainedCatalog
    charged_bytes: int
    remnant_count: int
    protected: int


@dataclass(frozen=True, slots=True)
class _AssetProof:
    asset: RetainedAsset
    file_identity: tuple[int, int, int, int] | None
    metadata: os.stat_result


@dataclass(frozen=True, slots=True)
class _CancellationProbe:
    cancelled: Callable[[], bool]

    def is_cancelled(self) -> bool:
        value = self.cancelled()
        if type(value) is not bool:
            raise RetainedAssetError("source_unavailable")
        return value


def _owner_bytes(owner: str) -> bytes:
    return json_bytes({"schema": "h3.context.retained_asset_owner_lock.v1", "owner_id": owner})


def _revision(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_OWNER_REVISION:
        raise RetainedAssetError("revision_invalid")
    return value


def _proof_bytes(
    kind: str, owner: str, asset: RetainedAsset, identity: os.stat_result | None = None
) -> bytes:
    return json_bytes(
        {
            "schema": "h3.context.retained_asset_" + kind + ".v1",
            "owner_id": owner,
            "asset": asset.to_wire(),
            "file_identity": None if identity is None else list(_identity(identity)),
        }
    )


class RetainedAssetStore:
    """Callers must obtain the private root/owner from the qualified server owner port."""

    def __init__(
        self,
        private_root: Path,
        owner_id: str,
        *,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        limits: RetainedStoreLimits | None = None,
    ) -> None:
        try:
            self.owner_id = require_owner(owner_id)
        except DurableStateError as error:
            raise RetainedAssetError(str(error)) from None
        if type(private_root) not in (Path, type(Path())) or not private_root.is_absolute():
            raise RetainedAssetError("storage_unsafe")
        self.private_root = private_root
        # IMPORTANT: dead-process volatile sweeping owns managed-artifacts only. Never place
        # retained media in that scope or let its sweep infer ownership of this durable subtree.
        self.root = private_subtree_path(private_root, "recovery-assets") / "v1"
        self.owner_directory = self.root / self.owner_id
        self.limits = limits or RetainedStoreLimits()
        if type(self.limits) is not RetainedStoreLimits or not callable(clock_ms):
            raise RetainedAssetError("limits_invalid")
        self.clock_ms = clock_ms
        self._lock = threading.RLock()
        self._lease: int | None = None
        self._owner_identity: tuple[int, int] | None = None
        self._leased_assets: set[str] = set()
        self._uses: dict[str, RetainedAssetUse] = {}
        self._project_uses: dict[str, str] = {}
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            failed = False
            for use in tuple(self._uses.values()):
                try:
                    use.release()
                except AuthoringSourceBindingError:
                    failed = True
            self._close_idle_owner()
            if failed:
                # IMPORTANT: one failed fresh-copy cleanup must not strand every later use.
                # Keep failed bytes and their owner lease protected until a retry closes them.
                raise RetainedAssetError("storage_unavailable")

    def _close_idle_owner(self) -> None:
        if self._closed and not self._uses:
            if self._lease is not None:
                os.close(self._lease)
                self._lease = None
                self._owner_identity = None

    def _check_owner_lease(self, identity: tuple[int, int]) -> None:
        if self._lease is None or self._owner_identity != identity:
            raise RetainedAssetError("storage_unsafe")
        path = self.owner_directory / _OWNER_LOCK
        before = validate_regular_file(path, maximum_bytes=1024).lstat()
        opened = os.fstat(self._lease)
        expected = _owner_bytes(self.owner_id)
        if (
            before.st_nlink != 1
            or opened.st_nlink != 1
            or _identity(opened) != _identity(before)
            or opened.st_size != len(expected)
        ):
            raise RetainedAssetError("storage_unsafe")
        # CRITICAL: the owner payload must be read through the retained locked FD on Windows.
        # Reopening byte zero conflicts with our own lease; stat alone misses payload substitution.
        os.lseek(self._lease, 0, os.SEEK_SET)
        if (
            os.read(self._lease, len(expected) + 1) != expected
            or _identity(os.fstat(self._lease)) != _identity(opened)
            or _identity(path.lstat()) != _identity(opened)
        ):
            raise RetainedAssetError("storage_unsafe")

    def _create_family(self) -> None:
        initial = len(_FAMILY_BYTES) + len(_GLOBAL_BYTES) + len(_owner_bytes(self.owner_id))
        if (
            initial > self.limits.max_total_bytes
            or len(_owner_bytes(self.owner_id)) > self.limits.max_owner_bytes
        ):
            raise RetainedAssetError("quota_bytes")
        existed = _exists(self.root)
        ensure_directory(self.root)
        if not _exists(self.root / _FAMILY):
            if existed:
                with os.scandir(self.root) as entries:
                    if next(entries, None) is not None:
                        raise RetainedAssetError("storage_unverified")
            _create_or_admit(self.root / _FAMILY, _FAMILY_BYTES)
        if _read_regular_bytes(self.root / _FAMILY, maximum_bytes=1024) != _FAMILY_BYTES:
            raise RetainedAssetError("storage_unsafe")
        if not _exists(self.root / _GLOBAL_LOCK):
            with os.scandir(self.root) as entries:
                if any(entry.name != _FAMILY for entry in entries):
                    raise RetainedAssetError("storage_unverified")
            _create_or_admit(self.root / _GLOBAL_LOCK, _GLOBAL_BYTES)

    @contextmanager
    def _operation(self, *, create: bool = False) -> Iterator[bool]:
        with self._lock:
            guard = None
            try:
                if self._closed:
                    raise RetainedAssetError("service_closed")
                if not _link_free_existing_prefix(self.root):
                    raise RetainedAssetError("storage_unsafe")
                if not _exists(self.root) and not create:
                    yield False
                    return
                if create:
                    self._create_family()
                with _validated_directories(self.private_root, self.root.parent, self.root):
                    if (
                        _read_regular_bytes(self.root / _FAMILY, maximum_bytes=1024)
                        != _FAMILY_BYTES
                    ):
                        raise RetainedAssetError("storage_unsafe")
                    try:
                        guard = _acquire_lock(self.root / _GLOBAL_LOCK, _GLOBAL_BYTES)
                    except ArtifactStoreError as error:
                        if str(error) == "scope_busy":
                            raise RetainedAssetError("catalog_busy") from None
                        raise
                    if not _exists(self.owner_directory):
                        if not create:
                            yield False
                            return
                        if len(self._owners()) >= self.limits.max_owners:
                            raise RetainedAssetError("quota_owners")
                        self._reserve(len(_owner_bytes(self.owner_id)))
                        ensure_directory(self.owner_directory)
                    with _validated_directories(self.owner_directory):
                        identity = _directory_identity(self.owner_directory.lstat())
                        if self._lease is None:
                            if not _exists(self.owner_directory / _OWNER_LOCK):
                                if not create or self._files(self.owner_directory):
                                    raise RetainedAssetError("storage_unverified")
                                self._reserve(len(_owner_bytes(self.owner_id)))
                            _create_or_admit(
                                self.owner_directory / _OWNER_LOCK, _owner_bytes(self.owner_id)
                            )
                            try:
                                self._lease = _acquire_lock(
                                    self.owner_directory / _OWNER_LOCK, _owner_bytes(self.owner_id)
                                )
                            except ArtifactStoreError as error:
                                if str(error) == "scope_busy":
                                    raise RetainedAssetError("owner_busy") from None
                                raise
                            self._owner_identity = identity
                        self._check_owner_lease(identity)
                        yield True
            except RetainedAssetError:
                raise
            except DurableStateError as error:
                raise RetainedAssetError(str(error)) from None
            except (UnsafePathError, ArtifactStoreError, OSError) as error:
                # SECURITY: native lock identity/link refusals are unsafe, not transient IO.
                raise RetainedAssetError(
                    "storage_unsafe"
                    if isinstance(error, UnsafePathError)
                    or str(error) in {"unsafe_store_entry", "unsafe_scope_lock"}
                    else "storage_unavailable"
                ) from None
            finally:
                if guard is not None:
                    os.close(guard)

    def _owners(self) -> tuple[Path, ...]:
        result = []
        with os.scandir(self.root) as entries:
            for count, entry in enumerate(entries, 1):
                if count > 18:
                    raise RetainedAssetError("quota_directory")
                if entry.name not in {_FAMILY, _GLOBAL_LOCK}:
                    try:
                        require_owner(entry.name)
                    except DurableStateError:
                        raise RetainedAssetError("storage_unverified") from None
                    result.append(validate_directory(self.root / entry.name))
        return tuple(result)

    def _files(self, owner: Path) -> tuple[tuple[Path, os.stat_result], ...]:
        result = []
        with os.scandir(owner) as entries:
            for count, entry in enumerate(entries, 1):
                if count > 128:
                    raise RetainedAssetError("quota_directory")
                path = owner / entry.name
                metadata = validate_regular_file(path).lstat()
                if metadata.st_nlink != 1:
                    raise RetainedAssetError("storage_unsafe")
                result.append((path, metadata))
        return tuple(result)

    def _catalog(self, owner: Path | None = None) -> RetainedCatalog:
        directory = self.owner_directory if owner is None else owner
        path = directory / _CATALOG
        if not _exists(path):
            return RetainedCatalog(directory.name)
        return decode_catalog(_read(path, MAX_CATALOG_BYTES)[0], expected_owner=directory.name)

    def read(self) -> RetainedCatalog:
        with self._operation() as admitted:
            return self._catalog() if admitted else RetainedCatalog(self.owner_id)

    def inventory(self) -> RetainedInventory:
        """Observe charged owner files without implicit deletion or source use/probing."""
        with self._operation() as admitted:
            if not admitted:
                return RetainedInventory(RetainedCatalog(self.owner_id), 0, 0, 0)
            catalog = self._catalog()
            known = {_CATALOG, _OWNER_LOCK} | {row.asset_id + ".mp4" for row in catalog.assets}
            files = self._files(self.owner_directory)
            return RetainedInventory(
                catalog,
                sum(metadata.st_size for _, metadata in files),
                sum(path.name not in known for path, _ in files),
                sum(
                    bool(row.project_references or row.asset_id in self._leased_assets)
                    for row in catalog.assets
                ),
            )

    def _reserve(self, extra_bytes: int = 0, *, new_id: str | None = None) -> None:
        owners = self._owners()
        total = sum((self.root / name).lstat().st_size for name in (_FAMILY, _GLOBAL_LOCK))
        own_total = 0
        all_ids: dict[str, set[str]] = {}
        unverified = False
        for owner in owners:
            ids: set[str] = set()
            owner_total = 0
            for path, metadata in self._files(owner):
                owner_total += metadata.st_size
                media, proof = _FILE.fullmatch(path.name), _PROOF.fullmatch(path.name)
                if media is not None:
                    ids.add(media.group(1))
                elif proof is not None:
                    ids.add(proof.group(2))
                elif path.name in {_CATALOG, _PENDING}:
                    catalog = decode_catalog(
                        _read(path, MAX_CATALOG_BYTES)[0], expected_owner=owner.name
                    )
                    ids.update(asset.asset_id for asset in catalog.assets)
                elif path.name != _OWNER_LOCK or metadata.st_size > 1024:
                    unverified = True
            total += owner_total
            if owner.name == self.owner_id:
                own_total = owner_total
            if owner_total > self.limits.max_owner_bytes:
                raise RetainedAssetError("quota_bytes")
            all_ids[owner.name] = ids
        # CRITICAL: charge physical media, intents, garbage, old/pending catalogs and foreign
        # locked owners before reserving a copy. Visible catalog rows alone undercount crash data.
        own_ids = all_ids.setdefault(self.owner_id, set())
        if new_id is not None:
            own_ids.add(new_id)
        if (
            any(len(ids) > self.limits.max_owner_assets for ids in all_ids.values())
            or sum(len(ids) for ids in all_ids.values()) > self.limits.max_global_assets
        ):
            raise RetainedAssetError("quota_assets")
        if (
            total + extra_bytes > self.limits.max_total_bytes
            or own_total + extra_bytes > self.limits.max_owner_bytes
        ):
            raise RetainedAssetError("quota_bytes")
        if len(owners) > self.limits.max_owners:
            raise RetainedAssetError("quota_owners")
        if unverified:
            raise RetainedAssetError("storage_unverified")

    def _cas(self, catalog: RetainedCatalog, expected: int) -> None:
        if catalog.revision != _revision(expected):
            raise RetainedAssetError("revision_conflict")
        if catalog.revision == MAX_OWNER_REVISION:
            raise RetainedAssetError("revision_exhausted")

    def _publish(self, catalog: RetainedCatalog) -> None:
        payload = encode_catalog(catalog)
        self._reserve(len(payload))
        pending = self.owner_directory / _PENDING
        _write_new_file(pending, payload)
        _replace_manifest(pending, self.owner_directory / _CATALOG, maximum_bytes=MAX_CATALOG_BYTES)

    def _verify(self, asset: RetainedAsset) -> os.stat_result:
        try:
            body, metadata = _read(
                self.owner_directory / (asset.asset_id + ".mp4"), asset.byte_length
            )
        except FileNotFoundError:
            raise RetainedAssetError("asset_unavailable") from None
        except (ArtifactStoreError, UnsafePathError):
            raise RetainedAssetError("asset_changed") from None
        if (
            len(body) != asset.byte_length
            or "sha256:" + hashlib.sha256(body).hexdigest() != asset.content_fingerprint
        ):
            raise RetainedAssetError("asset_changed")
        return metadata

    def _decode_proof(self, path: Path, kind: str, asset_id: str) -> _AssetProof:
        payload, metadata = _read(path, _PROOF_BYTES)
        try:
            value = strict_json(payload, maximum_bytes=_PROOF_BYTES, maximum_depth=4)
        except DurableStateError as error:
            raise RetainedAssetError(str(error)) from None
        if (
            type(value) is not dict
            or set(value) != {"schema", "owner_id", "asset", "file_identity"}
            or value["schema"] != "h3.context.retained_asset_" + kind + ".v1"
            or value["owner_id"] != self.owner_id
        ):
            raise RetainedAssetError("storage_unverified")
        asset = decode_asset(value["asset"])
        if asset.asset_id != asset_id or asset.project_references:
            raise RetainedAssetError("storage_unverified")
        raw = value["file_identity"]
        identity: tuple[int, int, int, int] | None = None
        if raw is not None:
            if (
                type(raw) is not list
                or len(raw) != 4
                or any(type(part) is not int or not 0 <= part < 1 << 128 for part in raw)
                or raw[2] != asset.byte_length
            ):
                raise RetainedAssetError("storage_unverified")
            identity = (raw[0], raw[1], raw[2], raw[3])
        if (kind == "intent" and identity is not None) or (kind == "verified" and identity is None):
            raise RetainedAssetError("storage_unverified")
        return _AssetProof(asset, identity, metadata)

    @staticmethod
    def _same_media_row(first: RetainedAsset, second: RetainedAsset) -> bool:
        return (
            replace(
                first,
                project_references=second.project_references,
                closed_at_ms=second.closed_at_ms,
            )
            == second
        )

    def _clean_remnants(self, catalog: RetainedCatalog) -> None:
        pending = self.owner_directory / _PENDING
        if _exists(pending):
            body, metadata = _read(pending, MAX_CATALOG_BYTES)
            candidate = decode_catalog(body, expected_owner=self.owner_id)
            if candidate.revision not in {catalog.revision, catalog.revision + 1}:
                raise RetainedAssetError("storage_unverified")
            _delete(pending, metadata)
        visible = {asset.asset_id: asset for asset in catalog.assets}
        groups: dict[str, list[tuple[Path, _AssetProof]]] = {}
        for path, _ in self._files(self.owner_directory):
            match = _PROOF.fullmatch(path.name)
            if match is None:
                continue
            proof = self._decode_proof(path, match.group(1), match.group(2))
            groups.setdefault(proof.asset.asset_id, []).append((path, proof))
        for asset_id, proofs in groups.items():
            asset = proofs[0][1].asset
            if any(not self._same_media_row(proof.asset, asset) for _, proof in proofs):
                raise RetainedAssetError("storage_unverified")
            if asset_id in visible and not self._same_media_row(visible[asset_id], asset):
                raise RetainedAssetError("storage_unverified")
            identities = {
                proof.file_identity for _, proof in proofs if proof.file_identity is not None
            }
            if len(identities) > 1:
                raise RetainedAssetError("storage_unverified")
            media = self.owner_directory / (asset_id + ".mp4")
            if _exists(media):
                # CRITICAL: intent plus a matching digest cannot prove a completed copy's owner.
                # Require its recorded native identity too; preserve complete-but-unproven or
                # same-content substituted remnants rather than guessing from filename/age.
                if not identities:
                    continue
                try:
                    media_identity = self._verify(asset)
                except RetainedAssetError as error:
                    if error.code in {"asset_changed", "asset_unavailable"}:
                        continue
                    raise
                if _identity(media_identity) not in identities:
                    continue
                if asset_id not in visible:
                    if asset_id in self._leased_assets:
                        continue
                    _delete(media, media_identity)
            for path, proof in proofs:
                _delete(path, proof.metadata)

    def set_enabled(self, enabled: bool, *, expected_revision: int) -> RetainedCatalog:
        if type(enabled) is not bool:
            raise RetainedAssetError("intent_invalid")
        with self._operation(create=enabled) as admitted:
            catalog = self._catalog() if admitted else RetainedCatalog(self.owner_id)
            self._cas(catalog, expected_revision)
            if not admitted:
                return catalog
            self._clean_remnants(catalog)
            updated = replace(catalog, revision=catalog.revision + 1, enabled=enabled)
            self._publish(updated)
            return updated

    def admit(
        self,
        source: object,
        *,
        expected_revision: int,
        deadline: float,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> tuple[RetainedCatalog, RetainedAsset]:
        with self._operation() as admitted:
            catalog = self._catalog() if admitted else RetainedCatalog(self.owner_id)
            self._cas(catalog, expected_revision)
            if not admitted or not catalog.enabled:
                raise RetainedAssetError("recovery_disabled")
            live = require_retention_source(source, owner_id=self.owner_id)
            check_retention_budget(deadline, cancelled)
            self._clean_remnants(catalog)
            facts = live.facts
            now = self.clock_ms()
            # IMPORTANT: qualified facts allow 512 timing rows. The general 256-item hash path
            # fails for valid longer clips; use the existing bounded composition bytes without
            # truncating landmarks or changing the general canonicalizer's limits.
            asset = RetainedAsset(
                "asset_" + uuid.uuid4().hex,
                facts.content_fingerprint,
                facts.byte_length,
                RETAINED_MEDIA_PROFILE,
                facts.width,
                facts.height,
                facts.frame_count,
                live.duration_milliseconds,
                composition_contract_fingerprint(facts.to_wire()),
                now,
                now,
            )
            self._reserve(new_id=asset.asset_id)
            updated = replace(
                catalog, revision=catalog.revision + 1, assets=(*catalog.assets, asset)
            )
            intent_body = _proof_bytes("intent", self.owner_id, asset)
            self._reserve(
                asset.byte_length + len(intent_body) + _PROOF_BYTES + len(encode_catalog(updated)),
                new_id=asset.asset_id,
            )
            intent = self.owner_directory / ("intent-" + asset.asset_id + ".json")
            _write_new_file(intent, intent_body)
            live.copy_into(
                self.owner_directory / (asset.asset_id + ".mp4"),
                deadline=deadline,
                cancelled=cancelled,
            )
            copy_identity = self._verify(asset)
            verified = self.owner_directory / ("verified-" + asset.asset_id + ".json")
            _write_new_file(verified, _proof_bytes("verified", self.owner_id, asset, copy_identity))
            require_retention_source(live, owner_id=self.owner_id)
            check_retention_budget(deadline, cancelled)
            # CRITICAL: only closed, fully verified media precedes this atomic catalog CAS.
            # Publishing intent/partial media as a row would falsely acknowledge crash recovery.
            self._publish(updated)
            require_retention_source(live, owner_id=self.owner_id)
            check_retention_budget(deadline, cancelled)
            self._clean_remnants(updated)
            return updated, asset

    def set_project_reference(
        self,
        asset_id: str,
        project_id: str,
        present: bool,
        *,
        expected_revision: int,
    ) -> RetainedCatalog:
        """Internal owner-qualified port; routes must never accept arbitrary project IDs."""
        identifier, project = require_asset_id(asset_id), require_project_reference(project_id)
        if type(present) is not bool:
            raise RetainedAssetError("reference_invalid")
        with self._operation() as admitted:
            catalog = self._catalog() if admitted else RetainedCatalog(self.owner_id)
            self._cas(catalog, expected_revision)
            asset = next((row for row in catalog.assets if row.asset_id == identifier), None)
            if asset is None:
                raise RetainedAssetError("asset_unavailable")
            references = set(asset.project_references)
            if (project in references) == present:
                return catalog
            if present:
                references.add(project)
            else:
                references.discard(project)
            updated_asset = replace(
                asset,
                project_references=tuple(sorted(references)),
                closed_at_ms=None if references else max(asset.created_at_ms, self.clock_ms()),
            )
            if updated_asset == asset:
                return catalog
            self._clean_remnants(catalog)
            updated = replace(
                catalog,
                revision=catalog.revision + 1,
                assets=tuple(
                    updated_asset if row.asset_id == identifier else row for row in catalog.assets
                ),
            )
            self._publish(updated)
            return updated

    def _use_finalized(self, handle: str) -> None:
        with self._lock:
            self._project_uses.pop(handle, None)
            use = self._uses.pop(handle, None)
            if use is not None and not any(
                other.asset_id == use.asset_id for other in self._uses.values()
            ):
                self._leased_assets.discard(use.asset_id)
            self._close_idle_owner()

    def _use_current(self, handle: str, asset: RetainedAsset, identity: os.stat_result) -> bool:
        with self._lock:
            use = self._uses.get(handle)
            if (
                self._closed
                or use is None
                or (handle not in self._project_uses and time.monotonic() >= use.expires_at)
            ):
                return False
            try:
                if _read_regular_bytes(self.root / _FAMILY, maximum_bytes=1024) != _FAMILY_BYTES:
                    return False
                self._check_owner_lease(
                    _directory_identity(validate_directory(self.owner_directory).lstat())
                )
                catalog = self._catalog()
                current = next(
                    (row for row in catalog.assets if row.asset_id == asset.asset_id), None
                )
                if not catalog.enabled or current is None:
                    return False
                comparable = replace(
                    current,
                    project_references=asset.project_references,
                    closed_at_ms=asset.closed_at_ms,
                )
                return comparable == asset and _identity(self._verify(asset)) == _identity(identity)
            except Exception:
                return False

    def _expire_uses(self) -> None:
        failed = False
        for use in tuple(self._uses.values()):
            if use.use_handle not in self._project_uses and time.monotonic() >= use.expires_at:
                try:
                    use.release()
                except AuthoringSourceBindingError:
                    failed = True
        if failed:
            # IMPORTANT: expiry must retain failed bytes while attempting every other use.
            # Project the cleanup refusal through this owner's finite error contract.
            raise RetainedAssetError("storage_unavailable")

    def sync_recovery_references(
        self,
        project_id: str,
        asset_ids: tuple[str, ...],
        *,
        uses: tuple[RetainedAssetUse, ...] = (),
    ) -> None:
        """Internal recovery journal port; imported identifiers cannot mint a reference."""
        project = require_project_reference(project_id)
        if (
            type(asset_ids) is not tuple
            or len(asset_ids) > 128
            or len(set(asset_ids)) != len(asset_ids)
            or type(uses) is not tuple
        ):
            raise RetainedAssetError("reference_invalid")
        wanted = {require_asset_id(identifier) for identifier in asset_ids}
        with self._lock:
            catalog = self.read()
            by_id = {row.asset_id: row for row in catalog.assets}
            if not wanted <= by_id.keys():
                raise RetainedAssetError("asset_unavailable")
            for identifier in wanted:
                if project in by_id[identifier].project_references:
                    continue
                # SECURITY: only a fresh use issued by this exact store may create a new pin.
                # Known journal reconciliation may preserve, but never invent, an existing pin.
                authorized = any(
                    type(use) is RetainedAssetUse
                    and self._uses.get(use.use_handle) is use
                    and use.asset_id == identifier
                    and not use.receipt.released
                    and use.source.current()
                    for use in uses
                )
                if not authorized:
                    raise RetainedAssetError("lease_invalid")
            for identifier in sorted(wanted):
                catalog = self.set_project_reference(
                    identifier, project, present=True, expected_revision=catalog.revision
                )
            for row in catalog.assets:
                if row.asset_id not in wanted and project in row.project_references:
                    catalog = self.set_project_reference(
                        row.asset_id, project, present=False, expected_revision=catalog.revision
                    )

    def adopt_project_use(self, use: RetainedAssetUse, project_id: str) -> None:
        """Backend-only ownership transfer; a browser file never calls this port."""
        project = require_project_reference(project_id)
        with self._lock:
            if (
                type(use) is not RetainedAssetUse
                or self._uses.get(use.use_handle) is not use
                or use.receipt.released
                or self._closed
                or (use.use_handle not in self._project_uses and time.monotonic() >= use.expires_at)
            ):
                raise RetainedAssetError("lease_invalid")
            if self._project_uses.get(use.use_handle, project) != project:
                raise RetainedAssetError("reference_invalid")
            # IMPORTANT: a live project owns cleanup, not the transient Settings-use timer.
            # Auto-expiring its freshly bound source makes an otherwise editable project stale.
            self._project_uses[use.use_handle] = project

    def restore(
        self,
        asset_id: str,
        *,
        media_adapter: QualifiedAVMediaAdapter,
        scratch_root: Path,
        deadline: float,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> RetainedAssetUse:
        identifier = require_asset_id(asset_id)
        check_retention_budget(deadline, cancelled)
        if type(media_adapter) is not QualifiedAVMediaAdapter:
            raise RetainedAssetError("media_unqualified")
        with self._operation() as admitted:
            catalog = self._catalog() if admitted else RetainedCatalog(self.owner_id)
            if not admitted or not catalog.enabled:
                raise RetainedAssetError("recovery_disabled")
            self._expire_uses()
            if len(self._uses) >= 16:
                raise RetainedAssetError("lease_bound")
            asset = next((row for row in catalog.assets if row.asset_id == identifier), None)
            if asset is None or (
                asset.closed_at_ms is not None
                and self.clock_ms() - asset.closed_at_ms >= CLOSED_RETENTION_MS
            ):
                raise RetainedAssetError("asset_unavailable")
            path = self.owner_directory / (asset.asset_id + ".mp4")
            if not _exists(path):
                raise RetainedAssetError("asset_unavailable")
            original = validate_regular_file(path, maximum_bytes=MAX_RETAINED_ASSET_BYTES).lstat()
            lease = create_output_lease(scratch_root, suffix=".mp4")
            use: RetainedAssetUse | None = None
            try:
                size, digest = _copy_regular_file_into_new(
                    path,
                    lease.path,
                    maximum_bytes=asset.byte_length,
                    should_cancel=lambda: cancelled() or time.monotonic() >= deadline,
                )
                if (size, digest) != (asset.byte_length, asset.content_fingerprint):
                    raise RetainedAssetError("asset_changed")
                facts = probe_authoring_video_facts(
                    lease.path, media_adapter, deadline, cancellation=_CancellationProbe(cancelled)
                )
                last, base = facts.landmarks[-1], facts.source_time_base
                duration = (
                    (last.pts + last.duration_ticks) * base.num * 1000 + base.den - 1
                ) // base.den
                # IMPORTANT: the facts digest does not validate separately stored scalar fields.
                # Match every current scalar as well, or altered duration/geometry aliases it.
                if composition_contract_fingerprint(facts.to_wire()) != asset.facts_fingerprint or (
                    facts.content_fingerprint,
                    facts.byte_length,
                    facts.schema_version,
                    facts.width,
                    facts.height,
                    facts.frame_count,
                    duration,
                ) != (
                    asset.content_fingerprint,
                    asset.byte_length,
                    asset.media_profile,
                    asset.width,
                    asset.height,
                    asset.frame_count,
                    asset.duration_ms,
                ):
                    raise RetainedAssetError("media_unqualified")
                check_retention_budget(deadline, cancelled)
                # SECURITY: rehashed/reprobed bytes mint only fresh process-owned authority.
                # Never decode an old receipt or carry its registry/Production callback.
                use = _mint_retained_use(
                    asset_id=asset.asset_id,
                    lease=lease,
                    facts=facts,
                    duration=asset.duration_ms,
                    expires_at=time.monotonic() + _USE_TTL,
                    current=lambda: (
                        use is not None and self._use_current(use.use_handle, asset, original)
                    ),
                    finalized=lambda: (
                        self._use_finalized(use.use_handle) if use is not None else None
                    ),
                )
                self._uses[use.use_handle] = use
                self._leased_assets.add(asset.asset_id)
                if not use.source.current():
                    raise RetainedAssetError("asset_changed")
                return use
            except BaseException as error:
                if use is not None:
                    use.release()
                elif not lease.released:
                    lease.release()
                check_retention_budget(deadline, cancelled)
                if isinstance(error, AuthoringVideoFactsError):
                    raise RetainedAssetError("media_unqualified") from None
                if isinstance(error, ArtifactStoreError):
                    raise RetainedAssetError("asset_changed") from None
                raise

    def claim_use(self, use_handle: str) -> RetainedAssetUse:
        if type(use_handle) is not str or _USE.fullmatch(use_handle) is None:
            raise RetainedAssetError("lease_invalid")
        with self._lock:
            self._expire_uses()
            use = self._uses.get(use_handle)
            if self._closed or use is None or use.receipt.released:
                raise RetainedAssetError("lease_invalid")
            if not use.source.current():
                raise RetainedAssetError("source_stale")
            return use

    def release_use(self, use_handle: str) -> None:
        if type(use_handle) is not str or _USE.fullmatch(use_handle) is None:
            raise RetainedAssetError("lease_invalid")
        with self._lock:
            use = self._uses.get(use_handle)
            if use is not None:
                try:
                    use.release()
                except AuthoringSourceBindingError:
                    raise RetainedAssetError("storage_unavailable") from None

    def _cleanup(self, *, expected_revision: int, expired_only: bool) -> RetainedCleanup:
        with self._operation() as admitted:
            catalog = self._catalog() if admitted else RetainedCatalog(self.owner_id)
            self._cas(catalog, expected_revision)
            if not admitted:
                return RetainedCleanup(catalog, 0, 0)
            self._expire_uses()
            self._clean_remnants(catalog)
            protected = sum(
                bool(asset.project_references or asset.asset_id in self._leased_assets)
                for asset in catalog.assets
            )
            eligible = tuple(
                asset
                for asset in catalog.assets
                if not asset.project_references
                and asset.asset_id not in self._leased_assets
                and (
                    not expired_only
                    or (
                        asset.closed_at_ms is not None
                        and self.clock_ms() - asset.closed_at_ms >= CLOSED_RETENTION_MS
                    )
                )
            )
            if not eligible:
                return RetainedCleanup(catalog, 0, protected)
            identities = {
                asset.asset_id: self._verify(asset)
                if _exists(self.owner_directory / (asset.asset_id + ".mp4"))
                else None
                for asset in eligible
            }
            ids = {asset.asset_id for asset in eligible}
            updated = replace(
                catalog,
                revision=catalog.revision + 1,
                assets=tuple(asset for asset in catalog.assets if asset.asset_id not in ids),
            )
            proof_bodies = tuple(
                _proof_bytes("garbage", self.owner_id, asset, identities[asset.asset_id])
                for asset in eligible
            )
            self._reserve(sum(map(len, proof_bodies)) + len(encode_catalog(updated)))
            for asset, body in zip(eligible, proof_bodies, strict=True):
                _write_new_file(
                    self.owner_directory / ("garbage-" + asset.asset_id + ".json"), body
                )
            # IMPORTANT: an immutable deletion proof precedes catalog removal. Crash recovery
            # deletes only its exact verified unreferenced file, never an age-guessed orphan.
            self._publish(updated)
            self._clean_remnants(updated)
            return RetainedCleanup(updated, len(eligible), protected)

    def clear(self, *, expected_revision: int) -> RetainedCleanup:
        return self._cleanup(expected_revision=expected_revision, expired_only=False)

    def collect(self, *, expected_revision: int) -> RetainedCleanup:
        return self._cleanup(expected_revision=expected_revision, expired_only=True)
