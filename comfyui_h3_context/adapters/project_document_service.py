"""Paired editable-project transaction; portable data never constructs execution grants."""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

from ..core.canonical import canonical_fingerprint
from ..core.contracts import MediaKind, TaskMode
from ..core.nle_authoring_contract import materialize_render_snapshot
from ..core.project_document import (
    ProjectDocument,
    ProjectDocumentError,
    ProjectMediaReference,
    ProjectSegment,
    closed,
    decode_project_document,
    decode_project_value,
    encode_project_document,
    project_bytes,
)
from ..core.reference_set_authoring import AddSource
from ..core.reference_set_authoring import apply_command as apply_reference_command
from ..core.temporal_profile import build_temporal_profile
from ..core.timeline_authoring import (
    build_h3_timeline_limits,
    build_h3_timeline_profile,
    create_timeline,
    reference_view,
)
from ..core.timeline_history_v2 import TimelineHistoryStateV2
from .authoring_fonts import load_packaged_font_manifest
from .authoring_render_source import PreparedAuthoringHistory, claim_render_source
from .comfyui_authoring_workspace import (
    AuthoringWorkspaceRegistry,
    _AuthoringEntry,
    _seed_reference_state,
)
from .comfyui_production_workspace import (
    MAX_PRODUCTION_REGISTRY_BYTES,
    ProductionWorkspaceRegistry,
    _EditableProjectEntry,
    _ProjectAuthoringAnchor,
)
from .comfyui_sidebar_workspace import SidebarAuthoringSeed, SidebarAuthoringSource
from .project_source_binding import ProjectSourceBindingReceipt

OWNER_FIELDS = {
    "production_handle",
    "production_id",
    "production_revision",
    "production_fingerprint",
    "authoring_handle",
    "reference_revision",
    "legacy_timeline_revision",
    "workspace_revision",
    "timeline_revision",
    "authoring_fingerprint",
}


class ProjectDocumentService:
    def __init__(
        self,
        production: ProductionWorkspaceRegistry,
        authoring: AuthoringWorkspaceRegistry,
        *,
        retained: Any = None,
    ) -> None:
        self.production, self.authoring, self.retained = production, authoring, retained

    @staticmethod
    def _owner(
        production_handle: str | None,
        project_id: str | None,
        revision: int | None,
        fingerprint: str | None,
        authoring_handle: str,
        entry: _AuthoringEntry,
    ) -> dict[str, Any]:
        history = entry.timeline_history_v2
        if history is None or history.released:
            raise ProjectDocumentError("editor_not_initialized")
        state = history.authoring
        return {
            "production_handle": production_handle,
            "production_id": project_id,
            "production_revision": revision,
            "production_fingerprint": fingerprint,
            "authoring_handle": authoring_handle,
            "reference_revision": entry.reference.revision,
            "legacy_timeline_revision": entry.timeline.revision,
            "workspace_revision": state.workspace_revision,
            "timeline_revision": state.timeline_revision,
            "authoring_fingerprint": state.authoring_fingerprint,
        }

    def open_document(
        self,
        value: object,
        *,
        cancelled: Callable[[], bool] = lambda: False,
        recovery_history: object | None = None,
    ) -> dict[str, Any]:
        if cancelled():
            raise ProjectDocumentError("cancelled")
        document = (
            decode_project_document(value.encode("utf-8", errors="strict"))
            if type(value) is str
            else decode_project_value(value)
        )
        handle = "pw_" + secrets.token_urlsafe(32)
        project_id = "project_" + secrets.token_hex(16)
        authoring_handle = "authoring-" + secrets.token_hex(16)
        lineage = object()
        digest = canonical_fingerprint({"project": project_id})
        media = {row.asset_id: row for row in document.media}
        sources = tuple(
            SidebarAuthoringSource(
                row["source_id"],
                MediaKind(row["kind"]),
                row["duration_milliseconds"],
                None,
                index,
                media[row["source_id"]].content_fingerprint
                or canonical_fingerprint({"asset": row["source_id"]}),
            )
            for index, row in enumerate(document.reference["sources"])
        )
        seed = SidebarAuthoringSeed(project_id, TaskMode.T2VA, digest, sources, lineage)
        universe, unavailable, reference = _seed_reference_state(
            seed, source_binding_available=False
        )
        timeline = create_timeline(
            build_h3_timeline_profile(build_temporal_profile()),
            build_h3_timeline_limits(),
            reference_view(reference),
        )
        entry = _AuthoringEntry(
            seed,
            universe,
            unavailable,
            reference,
            timeline,
            0,
            self.authoring._clock(),
            None,
            None,
            timeline_history_v2=TimelineHistoryStateV2.initialize(
                document.fresh_authoring(project_id, authoring_handle)
            ),
            lineage_token=lineage,
            production_owner=(handle, project_id),
            project_created=True,
            document_reference_json=document.reference_json,
        )
        if recovery_history is not None:
            from ..core.editor_recovery import restore_history_data

            entry.timeline_history_v2 = restore_history_data(
                document, recovery_history, project_id=project_id, workspace_handle=authoring_handle
            )
        project = _EditableProjectEntry(
            project_id,
            document,
            len(encode_project_document(document)),
            self.production._clock(),
            lineage,
        )
        # CRITICAL: construct every potentially failing projection before either owner is published.
        production_projection = self.production._editable_projection(handle, project)
        projection = self.authoring._projection(authoring_handle, entry)
        if entry.timeline_history_v2 is None:
            raise ProjectDocumentError("editor_not_initialized")
        history_projection = self.authoring._history_projection(
            authoring_handle, entry.timeline_history_v2
        )
        owner = self._owner(handle, project_id, 1, project.fingerprint, authoring_handle, entry)
        response = {
            "schema": "h3.context.project_document.response.v1",
            "owner": owner,
            "production": None
            if production_projection is None
            else production_projection.to_wire(),
            "authoring": projection,
            "history": history_projection,
            "planning": document.planning,
            "title": document.title,
            "missing_media": [row.asset_id for row in document.media],
        }
        project_bytes(response, maximum_bytes=3 * 1024 * 1024)
        with self.authoring._lock:
            with self.production._lock:
                if cancelled():
                    raise ProjectDocumentError("cancelled")
                if (
                    len(self.authoring._entries) >= self.authoring._max_entries
                    or self.production._occupied_entry_slots() >= self.production._max_entries
                    or self.production._reserved_registry_bytes() + project.document_bytes
                    > MAX_PRODUCTION_REGISTRY_BYTES
                ):
                    raise ProjectDocumentError("project_capacity")
                if (
                    handle in self.production._entries
                    or handle in self.production._editable_projects
                    or handle in self.production._empty_projects
                    or handle in self.production._tombstones
                    or authoring_handle in self.authoring._entries
                    or authoring_handle in self.authoring._tombstones
                ):
                    raise ProjectDocumentError("project_conflict")
                self.production._editable_projects[handle] = project
                self.production._authoring_anchors[handle] = _ProjectAuthoringAnchor(
                    seed, authoring_handle
                )
                self.authoring._entries[authoring_handle] = entry
        return response

    def capture_recovery(self, owner: object, planning: object, title: object) -> dict[str, Any]:
        from ..core.editor_recovery import history_data

        if owner is None or closed(owner, OWNER_FIELDS)["authoring_handle"] is None:
            wire = self.snapshot_document(owner, planning, title)["document"]
            history = None
        else:
            # IMPORTANT: capture immutable document/history together; encode outside business locks.
            with self.authoring._lock:
                with self.production._lock:
                    _current, document, entry = self._capture(owner)
                    history = entry.timeline_history_v2
            wire = document.to_wire()
        wire["title"], wire["planning"] = title, planning
        document = decode_project_value(wire)
        return {"document": document.to_wire(), "history": history_data(history)}

    def restore_recovery(
        self, value: object, *, cancelled: Callable[[], bool] = lambda: False
    ) -> dict[str, Any]:
        data = closed(value, {"document", "history"})
        return self.open_document(
            data["document"], recovery_history=data["history"], cancelled=cancelled
        )

    def discard_open_response(self, response: dict[str, Any]) -> None:
        """An abandoned Open may discard only its untouched, newly issued data pair."""
        owner = response.get("owner")
        if type(owner) is not dict:
            return
        with self.authoring._lock:
            with self.production._lock:
                try:
                    _current, _document, entry = self._capture(owner)
                except ProjectDocumentError:
                    return
                if (
                    owner["production_handle"] not in self.production._editable_projects
                    or not entry.project_created
                    or entry.timeline_history_v2 is None
                ):
                    return
                # CRITICAL: exact pair CAS proves no later edit; restored history is initial data,
                # not evidence of a delivered edit. Never use an empty-undo check for cleanup.
                self.authoring._entries.pop(owner["authoring_handle"], None)
                self.production._editable_projects.pop(owner["production_handle"], None)
                self.production._authoring_anchors.pop(owner["production_handle"], None)
        cleanups = []
        if entry.initialized_sources is not None:
            cleanups.append(entry.initialized_sources.discard)
        if entry.source_binding is not None:
            cleanups.append(entry.source_binding.release)
            if self.retained is not None:
                cleanups.append(
                    lambda: self.retained.sync_recovery_references(owner["production_id"], ())
                )
        for cleanup in cleanups:
            try:
                cleanup()
            except Exception:
                logging.getLogger(__name__).warning("Abandoned project resource cleanup failed")

    def _capture(
        self, owner_value: object, *, allow_expired: bool = False, touch: bool = True
    ) -> tuple[dict[str, Any], ProjectDocument, _AuthoringEntry]:
        expected = closed(owner_value, OWNER_FIELDS)
        handle, authoring_handle = expected["production_handle"], expected["authoring_handle"]
        standalone = handle is None
        if (
            type(authoring_handle) is not str
            or (not standalone and type(handle) is not str)
            or (
                standalone
                and any(
                    expected[key] is not None
                    for key in OWNER_FIELDS
                    if key.startswith("production_")
                )
            )
        ):
            raise ProjectDocumentError("project_conflict")
        # CRITICAL: Authoring -> Production is the existing source-claim lock order. No source
        # currentness/probe/IO is called here; immutable data is copied before encoding outside.
        with self.authoring._lock:
            with self.production._lock:
                entry = self.authoring._entries.get(authoring_handle)
                editable = self.production._editable_projects.get(handle)
                live = self.production._entries.get(handle)
                if entry is None or (not standalone and editable is None and live is None):
                    raise ProjectDocumentError("project_unavailable")
                production_entry = editable if editable is not None else live
                if not allow_expired and (
                    self.authoring._clock() - entry.touched_at >= self.authoring._ttl_seconds
                    or (
                        production_entry is not None
                        and self.production._clock() - production_entry.touched_at
                        >= self.production._ttl_seconds
                    )
                ):
                    raise ProjectDocumentError("project_unavailable")
                project_id = (
                    editable.workspace_id
                    if editable is not None
                    else None
                    if live is None
                    else live.workspace.workspace_id
                )
                revision = (
                    editable.revision
                    if editable is not None
                    else None
                    if live is None
                    else live.workspace.revision
                )
                fingerprint = (
                    editable.fingerprint
                    if editable is not None
                    else None
                    if live is None
                    else live.workspace.fingerprint
                )
                if entry.production_owner != (None if standalone else (handle, project_id)):
                    raise ProjectDocumentError("project_conflict")
                current = self._owner(
                    handle, project_id, revision, fingerprint, authoring_handle, entry
                )
                if expected != current or any(
                    type(expected[key]) is not type(current[key]) for key in OWNER_FIELDS
                ):
                    raise ProjectDocumentError("project_conflict")
                if entry.timeline_history_v2 is None:
                    raise ProjectDocumentError("editor_not_initialized")
                state = entry.timeline_history_v2.authoring
                if entry.document_reference_json is not None:
                    reference = json.loads(entry.document_reference_json)
                else:
                    reference = {
                        "sources": [
                            {
                                "source_id": row.source_id,
                                "kind": row.kind.value,
                                "duration_milliseconds": row.duration_milliseconds,
                            }
                            for row in (
                                *entry.reference.images,
                                *entry.reference.videos,
                                *entry.reference.audios,
                            )
                        ],
                        "soundtracks": [row.to_wire() for row in entry.reference.relations],
                    }
                if editable is not None:
                    base = editable.document
                else:
                    reference_ids = {row["source_id"] for row in reference["sources"]}
                    segments = tuple(
                        ProjectSegment(
                            row.segment_id,
                            row.task_mode.value,
                            row.duration.duration_milliseconds,
                            row.relation.value,
                            row.predecessor_segment_id,
                            row.source_id if row.source_id in reference_ids else None,
                            tuple(key for key in row.reference_ids if key in reference_ids),
                        )
                        for row in (() if live is None else live.workspace.segments)
                    )
                    base = ProjectDocument(
                        "",
                        segments,
                        () if live is None else live.workspace.selected_segment_ids,
                        project_bytes(
                            {
                                "intent": "",
                                "script": "",
                                "target_seconds": 60,
                                "policy": "auto_storyboard",
                                "shots": [],
                            }
                        ).decode(),
                        state,
                        project_bytes(reference).decode(),
                        (),
                    )
                saved = {row.asset_id: row for row in base.media}
                prepared = entry.initialized_sources
                if prepared is not None:
                    for source in prepared.sources:
                        saved.setdefault(
                            source.asset.asset_id,
                            ProjectMediaReference(
                                source.asset.asset_id,
                                source.source_fingerprint,
                                source.byte_count,
                                None,
                            ),
                        )
                keys = {row.asset_id for row in state.assets if row.kind != "font"}
                keys.update(row["source_id"] for row in reference["sources"])
                for segment in base.segments:
                    keys.update(segment.reference_asset_ids)
                    if segment.source_asset_id is not None:
                        keys.add(segment.source_asset_id)
                ordered = [row.asset_id for row in base.media if row.asset_id in keys]
                ordered.extend(sorted(keys - set(ordered)))
                media = tuple(
                    saved.get(key, ProjectMediaReference(key, None, None, None)) for key in ordered
                )
                document = replace(
                    base,
                    editor=state,
                    reference_json=project_bytes(reference).decode(),
                    media=media,
                )
                if touch:
                    entry.touched_at = self.authoring._clock()
                    if editable is not None:
                        editable.touched_at = self.production._clock()
                    elif live is not None:
                        live.touched_at = self.production._clock()
                return current, document, entry

    def current_recovery_owner(self, owner: object) -> dict[str, Any] | None:
        """Resolve current facts for an already tracked pair, without renewing its TTL."""
        if owner is None:
            return None
        expected = closed(owner, OWNER_FIELDS)
        with self.authoring._lock:
            with self.production._lock:
                handle = expected["production_handle"]
                production = self.production._editable_projects.get(handle)
                live = self.production._entries.get(handle)
                if handle is not None and production is None and live is None:
                    raise ProjectDocumentError("project_unavailable")
                if production is not None:
                    project_id, revision, fingerprint = (
                        production.workspace_id,
                        production.revision,
                        production.fingerprint,
                    )
                elif live is not None:
                    project_id, revision, fingerprint = (
                        live.workspace.workspace_id,
                        live.workspace.revision,
                        live.workspace.fingerprint,
                    )
                else:
                    project_id, revision, fingerprint = None, None, None
                authoring_handle = expected["authoring_handle"]
                anchor = self.production._authoring_anchors.get(handle)
                if (
                    authoring_handle is None
                    and anchor is not None
                    and anchor.target_handle is not None
                ):
                    target = self.authoring._entries.get(anchor.target_handle)
                    if target is not None and target.timeline_history_v2 is not None:
                        authoring_handle = anchor.target_handle
                if authoring_handle is None:
                    return dict(
                        expected,
                        production_id=project_id,
                        production_revision=revision,
                        production_fingerprint=fingerprint,
                    )
                entry = self.authoring._entries.get(authoring_handle)
                if entry is None or entry.production_owner != (
                    None if handle is None else (handle, project_id)
                ):
                    raise ProjectDocumentError("project_unavailable")
                return self._owner(
                    handle, project_id, revision, fingerprint, authoring_handle, entry
                )

    def capture_latest_recovery(
        self, owner: object, planning: object, title: object
    ) -> tuple[dict[str, Any] | None, dict[str, Any], object]:
        from ..core.editor_recovery import history_data

        with self.authoring._lock:
            with self.production._lock:
                current = self.current_recovery_owner(owner)
                if current is not None and current["authoring_handle"] is not None:
                    current, document, entry = self._capture(
                        current, allow_expired=True, touch=False
                    )
                    history, binding = entry.timeline_history_v2, entry.source_binding
                else:
                    document, history, binding = None, None, None
        # CRITICAL: only the recovery service's tracked dirty owners call this internal port.
        # It does not renew owners or encode history/perform IO while holding business locks.
        wire = (
            self.snapshot_document(current, planning, title, allow_expired=True, touch=False)[
                "document"
            ]
            if document is None
            else document.to_wire()
        )
        wire["planning"], wire["title"] = planning, title
        return (
            current,
            {"document": decode_project_value(wire).to_wire(), "history": history_data(history)},
            binding,
        )

    def snapshot_document(
        self,
        owner: object,
        planning: object,
        title: object,
        *,
        allow_expired: bool = False,
        touch: bool = True,
    ) -> dict[str, Any]:
        if owner is None:
            document = decode_project_value(
                {
                    "format": "h3proj",
                    "schema_version": 1,
                    "title": title,
                    "planning": planning,
                    "production": {"segments": [], "selection": []},
                    "editor": None,
                    "media": [],
                }
            )
            return {
                "schema": "h3.context.project_document.export.v1",
                "owner": None,
                "document": document.to_wire(),
            }
        expected = closed(owner, OWNER_FIELDS)
        if expected["authoring_handle"] is None:
            authoring_fields = OWNER_FIELDS - {
                "production_handle",
                "production_id",
                "production_revision",
                "production_fingerprint",
            }
            if (
                any(expected[key] is not None for key in authoring_fields)
                or type(expected["production_handle"]) is not str
            ):
                raise ProjectDocumentError("project_conflict")
            with self.production._lock:
                handle = expected["production_handle"]
                live = self.production._entries.get(handle)
                anchor = self.production._authoring_anchors.get(handle)
                # IMPORTANT: omitting a bound Editor would silently lose its current accepted edits.
                if (
                    live is None
                    or (
                        not allow_expired
                        and self.production._clock() - live.touched_at
                        >= self.production._ttl_seconds
                    )
                    or (anchor is not None and anchor.target_handle is not None)
                ):
                    raise ProjectDocumentError("project_conflict")
                workspace = live.workspace
                actual = dict(
                    expected,
                    production_id=workspace.workspace_id,
                    production_revision=workspace.revision,
                    production_fingerprint=workspace.fingerprint,
                )
                if actual != expected or any(
                    type(actual[key]) is not type(expected[key]) for key in OWNER_FIELDS
                ):
                    raise ProjectDocumentError("project_conflict")
                inputs = set() if anchor is None else {row.asset_id for row in anchor.seed.sources}
                segments: list[dict[str, Any]] = [
                    ProjectSegment(
                        row.segment_id,
                        row.task_mode.value,
                        row.duration.duration_milliseconds,
                        row.relation.value,
                        row.predecessor_segment_id,
                        row.source_id if row.source_id in inputs else None,
                        tuple(key for key in row.reference_ids if key in inputs),
                    ).to_wire()
                    for row in workspace.segments
                ]
                selection = list(workspace.selected_segment_ids)
                if touch:
                    live.touched_at = self.production._clock()
            used = {key for row in segments for key in row["reference_asset_ids"]}
            used.update(
                row["source_asset_id"] for row in segments if row["source_asset_id"] is not None
            )
            document = decode_project_value(
                {
                    "format": "h3proj",
                    "schema_version": 1,
                    "title": title,
                    "planning": planning,
                    "production": {"segments": segments, "selection": selection},
                    "editor": None,
                    "media": [
                        ProjectMediaReference(key, None, None, None).to_wire()
                        for key in sorted(used)
                    ],
                }
            )
            return {
                "schema": "h3.context.project_document.export.v1",
                "owner": actual,
                "document": document.to_wire(),
            }
        current, document, _entry = self._capture(owner, allow_expired=allow_expired, touch=touch)
        wire = document.to_wire()
        wire["title"], wire["planning"] = title, planning
        document = decode_project_value(wire)
        return {
            "schema": "h3.context.project_document.export.v1",
            "owner": current,
            "document": document.to_wire(),
        }

    def relink_document(
        self,
        owner: object,
        asset_id: str,
        retained_id: str,
        *,
        deadline: float,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        from .retained_asset_source import check_retention_budget

        current, document, entry = self._capture(owner)
        if document.editor is None or entry.timeline_history_v2 is None:
            raise ProjectDocumentError("relink_unavailable")
        expected_asset = next(
            (row for row in document.editor.assets if row.asset_id == asset_id), None
        )
        expected_media = next((row for row in document.media if row.asset_id == asset_id), None)
        previous = entry.source_binding
        if (
            self.retained is None
            or entry.document_reference_json is None
            or expected_asset is None
            or expected_asset.kind != "video"
            or expected_media is None
            or (previous is not None and type(previous) is not ProjectSourceBindingReceipt)
        ):
            raise ProjectDocumentError("relink_unavailable")
        use, binding, committed = None, None, False
        try:
            check_retention_budget(deadline, cancelled)
            use = self.retained.restore_project_source(
                retained_id, deadline=deadline, cancelled=cancelled
            )
            facts = use.source.facts
            if use.source.public_asset(asset_id) != expected_asset or (
                expected_media.content_fingerprint is not None
                and (
                    facts.content_fingerprint != expected_media.content_fingerprint
                    or facts.byte_length != expected_media.byte_length
                )
            ):
                raise ProjectDocumentError("media_mismatch")
            uses = {} if previous is None else dict(previous.uses)
            superseded = uses.get(asset_id)
            uses[asset_id] = use
            binding = ProjectSourceBindingReceipt(uses, new_use=use, previous=previous)
            sources = tuple(
                claim_render_source(binding, row.asset_id)
                for row in document.editor.assets
                if row.kind != "font" and row.asset_id in uses
            )
            for source in sources:
                source.verify_currentness(deadline)
            fonts = load_packaged_font_manifest()
            reference = entry.reference
            unavailable = dict(entry.not_admissible)
            source_row = entry.universe.get(asset_id)
            if source_row is not None:
                if source_row.duration_milliseconds != use.source.duration_milliseconds:
                    raise ProjectDocumentError("media_mismatch")
                if reference.entry(asset_id) is None:
                    reference, _receipt = apply_reference_command(
                        reference, AddSource(reference.revision, source_row)
                    )
                unavailable.pop(asset_id, None)
            prepared = None
            if {row.asset_id for row in document.editor.assets if row.kind != "font"} == set(uses):
                prepared = PreparedAuthoringHistory(
                    materialize_render_snapshot(document.editor),
                    binding.generation,
                    sources,
                    fonts,
                    authoring_state=document.editor,
                    source_reference_revision=reference.revision,
                    source_timeline_revision=entry.timeline.revision,
                )
            candidate = replace(
                entry,
                source_binding=binding,
                initialized_sources=prepared,
                reference=reference,
                not_admissible=unavailable,
                touched_at=self.authoring._clock(),
            )
            if candidate.timeline_history_v2 is None:
                raise ProjectDocumentError("editor_not_initialized")
            handle = cast(str, current["authoring_handle"])
            projection = self.authoring._projection(handle, candidate)
            history_projection = self.authoring._history_projection(
                handle, candidate.timeline_history_v2
            )
            next_document = replace(
                document,
                media=tuple(
                    ProjectMediaReference(
                        row.asset_id, facts.content_fingerprint, facts.byte_length, use.asset_id
                    )
                    if row.asset_id == asset_id
                    else row
                    for row in document.media
                ),
            )
            with self.production._lock:
                project = self.production._editable_projects.get(
                    cast(str, current["production_handle"])
                )
                if project is None or project.revision != current["production_revision"]:
                    raise ProjectDocumentError("project_conflict")
                updated = replace(
                    project,
                    document=next_document,
                    document_bytes=len(encode_project_document(next_document)),
                    revision=project.revision + 1,
                    touched_at=self.production._clock(),
                )
            production_projection = self.production._editable_projection(
                cast(str, current["production_handle"]), updated
            )
            next_owner = self._owner(
                cast(str, current["production_handle"]),
                cast(str, current["production_id"]),
                updated.revision,
                updated.fingerprint,
                handle,
                candidate,
            )
            response = {
                "schema": "h3.context.project_document.relink.v1",
                "owner": next_owner,
                "production": None
                if production_projection is None
                else production_projection.to_wire(),
                "authoring": projection,
                "history": history_projection,
                "missing_media": [
                    row.asset_id for row in document.media if row.asset_id not in uses
                ],
            }
            project_bytes(response, maximum_bytes=3 * 1024 * 1024)
            self.retained.adopt_project_source(use, cast(str, current["production_id"]))
            check_retention_budget(deadline, cancelled)
            with self.authoring._lock:
                with self.production._lock:
                    latest, _document, captured = self._capture(owner)
                    if (
                        latest != current
                        or captured is not entry
                        or entry.source_binding is not previous
                    ):
                        raise ProjectDocumentError("project_conflict")
                    project = self.production._editable_projects.get(
                        cast(str, current["production_handle"])
                    )
                    if project is None:
                        raise ProjectDocumentError("project_conflict")
                    if (
                        self.production._reserved_registry_bytes()
                        - project.document_bytes
                        + updated.document_bytes
                        > MAX_PRODUCTION_REGISTRY_BYTES
                    ):
                        raise ProjectDocumentError("project_capacity")
                    # CRITICAL: binding metadata advances the paired revision before publication;
                    # unchanged timeline geometry must not let a stale Save acknowledge relink.
                    check_retention_budget(deadline, cancelled)
                    binding.activate(previous)
                    self.authoring._entries[handle] = candidate
                    self.authoring._entries.move_to_end(handle)
                    self.production._editable_projects[cast(str, current["production_handle"])] = (
                        updated
                    )
                    committed = True
                    self.production._notify_editor_recovery(cast(str, current["production_handle"]))
            # IMPORTANT: post-commit cleanup must not report the old pair as current after CAS.
            # The old prepared snapshot is revoked even if its best-effort resource cleanup fails.
            cleanups = []
            if entry.initialized_sources is not None:
                cleanups.append(entry.initialized_sources.discard)
            if superseded is not None:
                cleanups.append(superseded.release)
            for cleanup in cleanups:
                try:
                    cleanup()
                except Exception:
                    logging.getLogger(__name__).warning(
                        "Superseded project resource cleanup failed"
                    )
            return response
        except ProjectDocumentError:
            raise
        except Exception:
            raise ProjectDocumentError("relink_failed") from None
        finally:
            if not committed:
                if binding is not None:
                    binding.release()
                elif use is not None:
                    use.release()


def build_project_document_service(
    production: ProductionWorkspaceRegistry,
    authoring: AuthoringWorkspaceRegistry,
    retained: Any = None,
) -> ProjectDocumentService:
    return ProjectDocumentService(production, authoring, retained=retained)
