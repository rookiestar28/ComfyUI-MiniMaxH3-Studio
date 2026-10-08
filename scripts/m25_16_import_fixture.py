"""Bounded hermetic browser fixture service for the M25-16 Production-to-Authoring import journeys.

One JSON request per stdin line, one JSON reply per stdout line. Unlike the stateless timeline
oracle (`m25_16_timeline_fixture.py`), this process keeps the real `ProductionWorkspaceRegistry`
and `AuthoringWorkspaceRegistry` in memory so the accepted import ledger, consumed-claim
refusals, catalog refresh and timeline history all run through the real adapter code. The
Playwright helper (`frontend/tests/e2e/helpers/nleImportFixture.ts`) owns the process lifetime.

Only the media-facts probe is stubbed, exactly as `tests/test_m25_29_production_authoring_import.py`
does: the artifact bodies are synthetic, so there is no real container to probe. Everything else
(registries, import service, history, receipts) is the shipped implementation.

With `"media": "corpus"` (M25-20 B-65, the two `import_integration` corpus rows) nothing is
stubbed: the ready output's body is the real encoded imported source the render stage also
imports (`scripts/nle_semantic_import_scenario.py`), and the probe is the real qualified adapter
over the pinned `ffmpeg`/`ffprobe` (`H3_CONTEXT_AUTHORIZED_FFMPEG_PATH` / `_FFPROBE_PATH`), so the
frame table the shell's snapshot carries is what the product measured of those bytes. Without the
pinned tools that mode answers 503 rather than falling back to the stub.

Operations:
  {"op": "bootstrap", "segments": N,
   "media": "synthetic"|"corpus"|"m25_56_tone124"|"m25_56_impulse124"}
                                        -> {"status": 200, "context_handle", "production",
                                            "authoring", "history", "media",
                                            "source_fingerprint", "source_byte_length"}
  {"op": "authoring", "action": {...}}  -> {"status", "body"}  real authoring-action route semantics
  {"op": "production", "action": {...}} -> {"status", "body"}  real production route semantics
  {"op": "import", "request": {...}}    -> {"status", "body"}  real import route: bodiless refusals
  {"op": "touch_production"}            -> {"status", "revision"}  real concurrent selection change
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_m25_29_production_authoring_import as accepted  # noqa: E402

from comfyui_h3_context.adapters.authoring_fonts import (  # noqa: E402
    _ERROR_CODES as FONT_ERROR_CODES,
)
from comfyui_h3_context.adapters.authoring_generated_source import (  # noqa: E402
    GeneratedAuthoringVideoSource,
)
from comfyui_h3_context.adapters.authoring_native_renderer import (  # noqa: E402
    NativeAuthoringRenderer,
)
from comfyui_h3_context.adapters.authoring_output_service import (  # noqa: E402
    AuthoringOutputRegistry,
)
from comfyui_h3_context.adapters.authoring_render_leases import (  # noqa: E402
    RenderSourceLeaseError,
)
from comfyui_h3_context.adapters.authoring_render_service import (  # noqa: E402
    AuthoringRenderService,
)
from comfyui_h3_context.adapters.authoring_render_store import RenderOutputStore  # noqa: E402
from comfyui_h3_context.adapters.authoring_source_binding import (  # noqa: E402
    AuthoringSourceBindingError,
    claim_transferred_authoring_source,
)
from comfyui_h3_context.adapters.av_reconstruction_media import (  # noqa: E402
    AuthoringVideoProbePayload,
    AVMediaAdapterError,
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.comfyui_authoring_workspace import (  # noqa: E402
    AuthoringWorkbenchError,
    AuthoringWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_production_workspace import (  # noqa: E402
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.production_authoring_import_service import (  # noqa: E402
    ProductionAuthoringImportService,
)
from comfyui_h3_context.core.authoring_output_protocol import (  # noqa: E402
    OutputProtocolError,
    decode_output_create,
)
from comfyui_h3_context.core.production_authoring_import import (  # noqa: E402
    decode_production_authoring_import_json,
)
from comfyui_h3_context.core.production_workbench import (  # noqa: E402
    ProductionWorkbenchProjection,
)
from comfyui_h3_context.core.timeline_history_v2 import TimelineHistoryStateV2  # noqa: E402

MAX_LINE_BYTES = 2_097_152
MAX_IMPORTED_MEDIA_BYTES = 1_000_000
MAX_IMPORTED_MEDIA_BASE64_BYTES = 1_400_000
MAX_OUTPUT_MEDIA_BYTES = 4_000_000
MAX_SEGMENTS = 3
# The accepted fixture declares every segment at 124 frames; the shipped frontend codec refuses a
# ready output whose delivered frame count differs from its segment's declared duration, so the
# synthetic artifacts and the stubbed probe both report exactly that many frames.
FRAME_COUNT = 124
IMPORT_DEADLINE_SECONDS = 30.0
CONTEXT_HANDLE = "ws_" + "c" * 32


def _stub_probe_adapter(scratch: Path) -> QualifiedAVMediaAdapter:
    scratch.mkdir(parents=True, exist_ok=True)
    adapter = object.__new__(QualifiedAVMediaAdapter)
    adapter._scratch_root = scratch  # noqa: SLF001 - mirrors the accepted test adapter

    def probe(
        _self: QualifiedAVMediaAdapter,
        *,
        source_path: Path,
        deadline: float,
        cancellation: object | None = None,
    ) -> AuthoringVideoProbePayload:
        del deadline, cancellation
        body = source_path.read_bytes()
        return AuthoringVideoProbePayload(
            accepted._video_probe_wire(frame_count=FRAME_COUNT),  # noqa: SLF001
            "sha256:" + hashlib.sha256(body).hexdigest(),
            len(body),
        )

    # Same seam the accepted test module monkeypatches; this process is the only consumer.
    setattr(QualifiedAVMediaAdapter, "probe_authoring_video_source", probe)  # noqa: B010
    return adapter


def _refusal(error: BaseException) -> dict[str, Any]:
    status = getattr(error, "status", 400)
    projection = getattr(error, "projection", None)
    if status == 409 and projection is not None:
        body = projection.to_wire() if hasattr(projection, "to_wire") else projection
        return {"status": 409, "body": body}
    return {"status": status if isinstance(status, int) else 400, "body": None}


def _pinned_tool(variable: str) -> Path | None:
    value = os.environ.get(variable, "")
    if not value:
        return None
    path = Path(value)
    return path if path.is_file() else None


class _Service:
    def __init__(self) -> None:
        fixture_temp_root = ROOT / ".tmp"
        fixture_temp_root.mkdir(parents=True, exist_ok=True)
        self._root = Path(tempfile.mkdtemp(prefix="m25-16-import-fixture-", dir=fixture_temp_root))
        self._production: ProductionWorkspaceRegistry | None = None
        self._authoring: AuthoringWorkspaceRegistry | None = None
        self._import: ProductionAuthoringImportService | None = None
        self._output: AuthoringOutputRegistry | None = None
        self._relayed_outputs: set[str] = set()
        self._relay_paths: set[Path] = set()
        self._scenario: Any = None
        self._workspace_handle: str | None = None
        self._authoring_workspace_handle: str | None = None
        self._touches = 0

    def close(self) -> None:
        if self._output is not None:
            self._output.close()
        if self._scenario is not None:
            self._scenario.close()
        for path in self._relay_paths:
            path.unlink(missing_ok=True)
        shutil.rmtree(self._root, ignore_errors=True)

    def _output_registry(self) -> AuthoringOutputRegistry:
        if self._output is not None:
            return self._output
        if self._authoring is None:
            raise OutputProtocolError("runtime_unavailable")
        ffmpeg = _pinned_tool("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
        ffprobe = _pinned_tool("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
        if ffmpeg is None or ffprobe is None:
            raise OutputProtocolError("runtime_unavailable")
        output_root = self._root / "real-output"
        output_root.mkdir(parents=True, exist_ok=False)
        store = RenderOutputStore(output_root)
        service = AuthoringRenderService(
            store,
            backend=NativeAuthoringRenderer(renderer_path=ffmpeg, probe_path=ffprobe),
        )
        # IMPORTANT: output must share the exact live Authoring registry that owns imported
        # leases and edit history. Reconstructing a second workspace would make a successful
        # render unrelated to the editor journey under test.
        self._output = AuthoringOutputRegistry(
            workspace=self._authoring,
            service=service,
            store=store,
        )
        return self._output

    def _publish_output_relay(self, handle: str, workspace: str) -> None:
        if handle in self._relayed_outputs:
            return
        configured = os.environ.get("H3_CONTEXT_E2E_OUTPUT_RELAY_DIR", "")
        if not configured:
            return
        allowed = (ROOT / ".tmp").resolve()
        relay = Path(configured).resolve()
        try:
            relay.relative_to(allowed)
        except ValueError:
            raise OutputProtocolError("runtime_unavailable") from None
        relay.mkdir(parents=True, exist_ok=True)
        with self._output_registry().open_download(handle, workspace) as lease:
            body = b"".join(bytes(chunk) for chunk in lease.chunks())
        if not 1 <= len(body) <= MAX_OUTPUT_MEDIA_BYTES:
            raise OutputProtocolError("resource_limit")
        target = relay / f"{handle}.mp4"
        pending = relay / f"{handle}.pending"
        pending.write_bytes(body)
        pending.replace(target)
        self._relayed_outputs.add(handle)
        self._relay_paths.add(target)

    def bootstrap(self, segments: object, media: object = "synthetic") -> dict[str, Any]:
        if not isinstance(segments, int) or segments < 1 or segments > MAX_SEGMENTS:
            return {"status": 400, "body": None}
        if media == "corpus":
            return self._bootstrap_corpus_media(segments)
        if isinstance(media, str) and media in {
            "m25_56_tone124",
            "m25_56_impulse124",
        }:
            return self._bootstrap_m25_56_media(segments, media)
        if media != "synthetic":
            return {"status": 400, "body": None}
        production, projection, authoring, authoring_projection, history, _store = (
            accepted._registries_with_ready_output(  # noqa: SLF001
                self._root, segment_count=segments, frame_count=FRAME_COUNT
            )
        )
        adapter = _stub_probe_adapter(self._root / "authoring-scratch")
        self._production = production
        self._authoring = authoring
        self._workspace_handle = projection.workspace_handle
        self._authoring_workspace_handle = str(authoring_projection["workspace_handle"])
        self._import = ProductionAuthoringImportService(
            production_registry=production,
            authoring_registry=authoring,
            media_runtime=lambda: adapter,
        )
        return {
            "status": 200,
            "context_handle": CONTEXT_HANDLE,
            "production": projection.to_wire(),
            "authoring": authoring_projection,
            "history": history,
            "media": "synthetic",
            "source_fingerprint": None,
            "source_byte_length": None,
        }

    def _bootstrap_corpus_media(self, segments: int) -> dict[str, Any]:
        ffmpeg = _pinned_tool("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
        ffprobe = _pinned_tool("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
        if ffmpeg is None or ffprobe is None:
            return {"status": 503, "body": None, "error": "ffmpeg_unpinned"}
        # Lazy: only this mode pays for the scenario module (and, through it, the render
        # stage's imports) -- the synthetic mode every other journey uses stays as it was.
        from scripts import nle_semantic_import_scenario as scenario_module

        scenario = scenario_module.build_scenario(
            ffmpeg, ffprobe, root=self._root / "corpus", segments=segments
        )
        self._scenario = scenario
        self._production = scenario.production
        self._authoring = scenario.authoring
        self._workspace_handle = scenario.projection.workspace_handle
        self._authoring_workspace_handle = str(scenario.authoring_projection["workspace_handle"])
        self._import = scenario.service
        return {
            "status": 200,
            "context_handle": scenario.context_handle,
            "production": scenario.projection.to_wire(),
            "authoring": scenario.authoring_projection,
            "history": scenario.history,
            "media": "corpus",
            "source_fingerprint": scenario.source_fingerprint,
            "source_byte_length": scenario.source_byte_length,
        }

    def _bootstrap_m25_56_media(self, segments: int, media: str) -> dict[str, Any]:
        ffmpeg = _pinned_tool("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
        ffprobe = _pinned_tool("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
        if ffmpeg is None or ffprobe is None:
            return {"status": 503, "body": None, "error": "ffmpeg_unpinned"}
        fixture_paths = {
            "m25_56_tone124": ROOT / "tests" / "fixtures" / "m25_56_124f_tone_bt709.mp4",
            "m25_56_impulse124": ROOT / ".tmp" / "m25-56-media" / "m25-56-124f-impulses.mp4",
        }[media]
        frame_count = 124
        source = fixture_paths
        if not source.is_file():
            return {"status": 503, "body": None, "error": "m25_56_fixture_missing"}
        body = source.read_bytes()
        if not 1 <= len(body) <= MAX_IMPORTED_MEDIA_BYTES:
            return {"status": 413, "body": None, "error": "m25_56_fixture_size"}
        production, projection, authoring, authoring_projection, history, _store = (
            accepted._registries_with_ready_output(  # noqa: SLF001 - accepted real import ledger
                self._root,
                segment_count=segments,
                artifact_bodies=(body,) * segments,
                frame_count=frame_count,
                width=320,
                height=180,
            )
        )
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=self._root / "authoring-scratch",
            clock_ms=lambda: max(1, time.monotonic_ns() // 1_000_000),
        )
        self._production = production
        self._authoring = authoring
        self._workspace_handle = projection.workspace_handle
        self._authoring_workspace_handle = str(authoring_projection["workspace_handle"])
        self._import = ProductionAuthoringImportService(
            production_registry=production,
            authoring_registry=authoring,
            media_runtime=lambda: adapter,
        )
        return {
            "status": 200,
            "context_handle": CONTEXT_HANDLE,
            "production": projection.to_wire(),
            "authoring": authoring_projection,
            "history": history,
            "media": media,
            "source_fingerprint": "sha256:" + hashlib.sha256(body).hexdigest(),
            "source_byte_length": len(body),
        }

    def authoring(self, action: object) -> dict[str, Any]:
        if self._authoring is None:
            return {"status": 503, "body": None}
        try:
            decoded = cast(dict[str, object], action)
            result = self._authoring.dispatch(decoded, production_registry=self._production)
        except (AuthoringWorkbenchError, ValueError, KeyError, TypeError) as error:
            return _refusal(error)
        if decoded.get("action") in {
            "create_authoring_workspace",
            "ensure_authoring_from_production",
        }:
            body = result.body
            handle = body.get("workspace_handle") if isinstance(body, dict) else None
            if isinstance(handle, str) and handle:
                # IMPORTANT: absent-target journeys must follow the Production-anchored project
                # workspace returned by `ensure`; the bootstrap's unattached seed editor is stale.
                self._authoring_workspace_handle = handle
        return {"status": result.status, "body": result.body}

    def production(self, action: object) -> dict[str, Any]:
        if self._production is None:
            return {"status": 503, "body": None}
        try:
            result = self._production.dispatch(action)
        except Exception as error:  # noqa: BLE001 - route-equivalent refusal mapping
            return _refusal(error)
        body = None if result.projection is None else result.projection.to_wire()
        return {"status": result.status, "body": body}

    def touch_production(self) -> dict[str, Any]:
        """Advance the live Production revision through a real selection action (M25-40).

        A stubbed bodiless refusal leaves the backend untouched, so the browser's one bounded
        recapture correctly finds no changed identity and stops. The known-stale recovery journey
        needs a genuine concurrent change instead. The current selection is re-applied unchanged:
        the registry still advances the revision, while segments, outputs, receipts and selection
        membership (which import eligibility requires) stay exactly as captured.
        """
        if self._production is None or self._workspace_handle is None:
            return {"status": 503, "body": None}
        self._touches += 1
        current = self._production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": f"fixture.touch.read.{self._touches}",
                "action": "read_projection",
                "payload": {"workspace_handle": self._workspace_handle},
            }
        ).projection
        if not isinstance(current, ProductionWorkbenchProjection):
            return {"status": 503, "body": None}
        result = self._production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": f"fixture.touch.select.{self._touches}",
                "action": "set_selection",
                "payload": {
                    "workspace_handle": current.workspace_handle,
                    "expected_workspace_revision": current.workspace_revision,
                    "expected_workspace_fingerprint": current.workspace_fingerprint,
                    "segment_ids": list(current.selected_segment_ids),
                },
            }
        )
        touched = result.projection
        revision = (
            touched.workspace_revision
            if isinstance(touched, ProductionWorkbenchProjection)
            else None
        )
        return {"status": result.status, "body": None, "revision": revision}

    def import_outputs(self, request: object) -> dict[str, Any]:
        if self._import is None:
            return {"status": 503, "body": None}
        try:
            decoded = decode_production_authoring_import_json(
                json.dumps(request, separators=(",", ":")).encode()
            )
            response = self._import.dispatch(
                decoded, deadline=time.monotonic() + IMPORT_DEADLINE_SECONDS
            )
        except AuthoringWorkbenchError as error:
            return {"status": error.status, "body": None}
        except ValueError:
            return {"status": 400, "body": None}
        return {"status": 200, "body": response.to_wire()}

    def _read_imported_source(self, asset_id: str, deadline: float) -> bytes:
        """Read one real imported original without constructing an unrelated render job."""

        from comfyui_h3_context.adapters.av_reconstruction_media import _read_regular_media_body

        registry = self._authoring
        handle = self._authoring_workspace_handle
        if registry is None or handle is None:
            raise RenderSourceLeaseError("source_unavailable")
        with registry._lock:
            registry._prune(registry._clock())
            entry = registry._entry(handle)
            history = registry._history(entry)
            prepared = entry.initialized_sources
            binding = entry.source_binding
            if registry._render_snapshot(history) is None:
                raise AuthoringWorkbenchError(409, "render_snapshot_unavailable")
            assets = (
                history.authoring.assets
                if isinstance(history, TimelineHistoryStateV2)
                else history.snapshot.assets
            )
            asset = next((item for item in assets if item.asset_id == asset_id), None)
            claim = (
                None
                if prepared is None
                else next((item for item in prepared.sources if item.asset == asset), None)
            )
            if asset is None or prepared is None or claim is None:
                raise RenderSourceLeaseError("source_not_found")
            if binding is None:
                raise RenderSourceLeaseError("source_unavailable")

        def current() -> bool:
            with registry._lock:
                registry._prune(registry._clock())
                active = registry._entries.get(handle)
                return (
                    active is entry
                    and registry._history(active) is history
                    and active.initialized_sources is prepared
                    and active.source_binding is binding
                    and active.reference.revision == prepared.source_reference_revision
                    and active.timeline.revision == prepared.source_timeline_revision
                    and claim.current()
                )

        def confirm() -> None:
            if time.monotonic() >= deadline:
                raise RenderSourceLeaseError("source_expired")
            if not current():
                raise RenderSourceLeaseError("source_unavailable")

        borrower = None
        body: bytearray | None = None
        try:
            confirm()
            source = claim_transferred_authoring_source(binding, asset_id)
            if type(source) is not GeneratedAuthoringVideoSource:
                raise RenderSourceLeaseError("source_unavailable")
            if not 1 <= claim.byte_count <= MAX_IMPORTED_MEDIA_BYTES:
                raise AVMediaAdapterError("resource_limit")
            claim.verify_currentness(deadline)
            confirm()
            borrower = source.borrow_for_render()
            path = borrower.path
            if path is None:
                raise RenderSourceLeaseError("source_unavailable")
            body, fingerprint = _read_regular_media_body(path, claim.byte_count, deadline=deadline)
            # CRITICAL: an original read belongs to this exact live history, not a render job
            # that may outlive it. Never cache currentness or omit the post-read authority/hash
            # checks: equal stat metadata, an old valid digest, or a retained borrower cannot
            # authorize a replaced history, released workspace or revoked Production source.
            confirm()
            if (
                claim_transferred_authoring_source(binding, asset_id) is not source
                or not borrower.current()
                or fingerprint != claim.source_fingerprint
                or len(body) != claim.byte_count
            ):
                raise RenderSourceLeaseError("source_replaced")
            confirm()
            return bytes(body)
        finally:
            if body is not None:
                body.clear()
            if borrower is not None:
                borrower.release()

    def media_source(self, asset_id: object) -> dict[str, Any]:
        """Serve an owner-bound imported original through a bounded fixture-only read."""

        if (
            self._authoring is None
            or self._authoring_workspace_handle is None
            or type(asset_id) is not str
            or not asset_id
            or len(asset_id) > 256
        ):
            return {"status": 400, "error": "invalid_media_request"}
        try:
            body = self._read_imported_source(asset_id, time.monotonic() + 5.0)
            if not 1 <= len(body) <= MAX_IMPORTED_MEDIA_BYTES:
                return {"status": 413, "error": "media_resource_limit"}
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            encoded = base64.b64encode(body).decode("ascii")
            if len(encoded) > MAX_IMPORTED_MEDIA_BASE64_BYTES:
                return {"status": 413, "error": "media_resource_limit"}
            return {
                "status": 200,
                "asset_id": asset_id,
                "byte_length": len(body),
                "source_fingerprint": digest,
                "media_base64": encoded,
            }
        except AuthoringWorkbenchError as error:
            return {"status": error.status, "error": error.code}
        except RenderSourceLeaseError as error:
            status = 404 if error.code == "source_not_found" else 409
            return {"status": status, "error": error.code}
        except AuthoringSourceBindingError:
            return {"status": 409, "error": "source_unavailable"}
        except AVMediaAdapterError as error:
            if error.code == "resource_limit":
                return {"status": 413, "error": "media_resource_limit"}
            return {"status": 409, "error": "source_replaced"}

    def output_create(self, payload: object) -> dict[str, Any]:
        try:
            status = self._output_registry().create(decode_output_create(payload))
            return {"status": 200, "body": status}
        except OutputProtocolError as error:
            return {"status": error.status, "body": error.to_wire()}

    def output_status(self, handle: object, workspace: object) -> dict[str, Any]:
        try:
            if type(handle) is not str or type(workspace) is not str:
                raise OutputProtocolError()
            status = self._output_registry().status(handle, workspace)
            output_handle = status.get("output_handle")
            if status.get("availability") == "available" and type(output_handle) is str:
                # Native Chromium downloads bypass Playwright interception. Publish only the
                # exact verified body under its opaque public handle so the test server can serve
                # the same product output; no source path or caller-selected filename crosses.
                self._publish_output_relay(output_handle, workspace)
            return {"status": 200, "body": status}
        except OutputProtocolError as error:
            return {"status": error.status, "body": error.to_wire()}

    def output_cancel(self, handle: object, workspace: object) -> dict[str, Any]:
        try:
            if type(handle) is not str or type(workspace) is not str:
                raise OutputProtocolError()
            status = self._output_registry().cancel(handle, workspace)
            return {"status": 200, "body": status}
        except OutputProtocolError as error:
            return {"status": error.status, "body": error.to_wire()}

    def output_media(self, handle: object, workspace: object) -> dict[str, Any]:
        try:
            if type(handle) is not str or type(workspace) is not str:
                raise OutputProtocolError()
            with self._output_registry().open_download(handle, workspace) as lease:
                body = b"".join(bytes(chunk) for chunk in lease.chunks())
                if not 1 <= len(body) <= MAX_OUTPUT_MEDIA_BYTES:
                    raise OutputProtocolError("resource_limit")
                return {
                    "status": lease.selection.status,
                    "headers": lease.headers,
                    "byte_length": len(body),
                    "output_fingerprint": "sha256:" + hashlib.sha256(body).hexdigest(),
                    "media_base64": base64.b64encode(body).decode("ascii"),
                }
        except OutputProtocolError as error:
            return {"status": error.status, "body": error.to_wire()}


def unexpected_reply(operation: object, error: Exception) -> dict[str, Any]:
    # CRITICAL: bootstrap refusals must identify the operation without logging exception text,
    # payloads or paths. A regex alone still allows sensitive data disguised as a code.
    operations = {
        "bootstrap",
        "authoring",
        "production",
        "import",
        "media_source",
        "output_create",
        "output_status",
        "output_cancel",
        "output_media",
        "touch_production",
    }
    codes = {
        "request_invalid",
        "initialization_unavailable",
        "initialization_revision_conflict",
        "initialization_currentness_conflict",
        "timeline_history_profile_mismatch",
        "timeline_history_already_bound",
        "timeline_initialization_busy",
        "source_stale",
        "source_not_found",
        "source_unsupported",
        "registry_mismatch",
        "generation_mismatch",
        "render_sources_unavailable",
        "internal_invariant",
        "font_unavailable",
        "font_manifest_changed",
        "initialization_projection_invalid",
        "source_not_bound",
        "audio_editing_deferred",
    } | FONT_ERROR_CODES
    code = getattr(error, "code", None)
    return {
        "status": 500,
        "body": None,
        "error": "AuthoringWorkbenchError"
        if isinstance(error, AuthoringWorkbenchError)
        else "UnexpectedError",
        "operation": operation
        if isinstance(operation, str) and operation in operations
        else "decode",
        "code": code
        if isinstance(error, AuthoringWorkbenchError) and code in codes
        else "unexpected_error",
    }


def _serve() -> None:
    service = _Service()
    out = sys.stdout.buffer
    try:
        for raw in sys.stdin.buffer:
            if len(raw) > MAX_LINE_BYTES:
                reply: dict[str, Any] = {"status": 413, "body": None}
            else:
                op: object = None
                try:
                    message = json.loads(raw)
                    op = message.get("op")
                    if op == "bootstrap":
                        reply = service.bootstrap(
                            message.get("segments", 1), message.get("media", "synthetic")
                        )
                    elif op == "authoring":
                        reply = service.authoring(message.get("action"))
                    elif op == "production":
                        reply = service.production(message.get("action"))
                    elif op == "import":
                        reply = service.import_outputs(message.get("request"))
                    elif op == "media_source":
                        reply = service.media_source(message.get("asset_id"))
                    elif op == "output_create":
                        reply = service.output_create(message.get("payload"))
                    elif op == "output_status":
                        reply = service.output_status(
                            message.get("handle"), message.get("workspace_handle")
                        )
                    elif op == "output_cancel":
                        reply = service.output_cancel(
                            message.get("handle"), message.get("workspace_handle")
                        )
                    elif op == "output_media":
                        reply = service.output_media(
                            message.get("handle"), message.get("workspace_handle")
                        )
                    elif op == "touch_production":
                        reply = service.touch_production()
                    else:
                        reply = {"status": 400, "body": None}
                except Exception as error:  # noqa: BLE001 - keep the line protocol alive
                    reply = unexpected_reply(op, error)
            out.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
            out.flush()
    finally:
        service.close()


if __name__ == "__main__":
    _serve()
