"""M25-21: freeze the machine-readable NLE hardening coverage manifest.

The manifest is generated from what already exists, not authored, for the reason M25-20 gives for
its own: a hand-kept copy of 34 command rows and seven shell rows drifts the day either source
moves. Its membership joins

- the 34 canonical command rows of the M25-16 ``NleControlCoverageManifestV1``, keyed by the
  backend ``command`` (plan section 14.3), never by the display ``operation_id``;
- the seven frozen ``SidebarEditorUiInvariantV1`` rows, which stay non-command coverage;
- the non-command action and import inventory (transport, the embedded-audio follower, the deferred
  audio negative, the M25-19 output actions and the Production import);
- the owned recovery seams; and
- the frozen numeric measurements of plan sections 6 and 14.

Every row names the collected test that must record its observation for each dimension. The
manifest holds no result: the report (``scripts/nle_hardening_report.py``) is where observations
meet this membership, and a missing observation there is ``NOT_RUN``, never a pass.

Run with ``--write`` to refresh, ``--check`` to verify.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Final, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.composition_contract import NLE_OPERATION_IDS  # noqa: E402
from comfyui_h3_context.core.semantic_conformance import (  # noqa: E402
    NON_DESTROY_CLOSE_REASONS,
    SIDEBAR_UI_INVARIANT_IDS,
)

MANIFEST_SCHEMA: Final = "h3.context.nle_hardening_coverage_manifest.v1"
MANIFEST_PATH: Final = ROOT / "governance" / "contracts" / "nle_hardening_coverage_manifest_v1.json"
CONTROL_MANIFEST_PATH: Final = (
    ROOT / "governance" / "contracts" / "nle_control_coverage_manifest_v1.json"
)
SHELL_CONTRACT_PATH: Final = ROOT / "frontend" / "src" / "contracts" / "sidebarEditorUiContract.ts"

DIMENSIONS: Final = ("accessibility", "stress", "recovery")

COMMANDS_SPEC: Final = "e2e/journeys/nleHardeningCommands.spec.ts"
SHELL_SPEC: Final = "e2e/journeys/nleHardeningShell.spec.ts"
ACTIONS_SPEC: Final = "e2e/journeys/nleHardeningActions.spec.ts"
STRESS_SPEC: Final = "e2e/journeys/nleHardeningStress.spec.ts"
RENDER_SPEC: Final = "e2e/journeys/nleHardeningRender.spec.ts"

# The shared workload tests. Each records one identified observation per row it serves (plan
# section 14.5: one executed interaction may supply many assertions; 34 x 3 is not 102 runs).
EDIT_WORKLOAD: Final = (
    f"{STRESS_SPEC}::NLE-STRESS-V1 scheduled edit phase accepts and refuses every command "
    "within the edit budgets"
)
PLAYBACK_WORKLOAD: Final = (
    f"{STRESS_SPEC}::NLE-STRESS-V1 ten-minute decoded playback and seek workload stays within "
    "the runtime budgets"
)
SMOKE_WORKLOAD: Final = (
    f"{STRESS_SPEC}::M25-16-overlay-v1-smoke five-minute decoded playback stays within the "
    "smoke budgets"
)
UI_WORKLOAD: Final = (
    f"{STRESS_SPEC}::UI-CONTRACT-STRESS-V1 shell workload keeps every invariant with zero "
    "navigation effects"
)
SEAM_WORKLOAD: Final = (
    f"{STRESS_SPEC}::NLE-STRESS-V1 injected seam losses recover the accepted revision or fail "
    "closed"
)
REMOUNT_WORKLOAD: Final = (
    f"{STRESS_SPEC}::NLE-STRESS-V1 remount cycles restore retained state paused and release "
    "every owner"
)
OUTPUT_WORKLOAD: Final = (
    f"{STRESS_SPEC}::output status workload keeps at most two status clients and one running job"
)
IMPORT_WORKLOAD: Final = (
    f"{STRESS_SPEC}::import workload stays inside the selection, byte and cleanup bounds"
)
# The render companion is the one workload that is not a browser journey: plan section 14.2
# requires the backend RSS increment to come from a repository-local backend fixture process and
# its owned child tree, which no page can measure. It runs in this lane so the same gatherer
# collects it, and it is keyed to its own test rather than to the browser output workload -- a
# measurement is admitted only from the test that actually produced it. It lives in its own spec
# file deliberately: the shared stress spec's eight workloads take hours to execute, and a PASS
# binds the tree that produced it, so appending a ninth test there would invalidate all eight.
RENDER_COMPANION: Final = (
    f"{RENDER_SPEC}::NLE-STRESS-RENDER-V1 backend companion renders eight jobs inside the "
    "process resource ceilings"
)


def command_a11y_title(operation_id: str, command: str) -> str:
    return (
        f"hardening a11y {operation_id} is a named 44px touch target that issues one {command} "
        "and keeps focus in the editor"
    )


def command_recovery_title(operation_id: str, command: str) -> str:
    return (
        f"hardening recovery {operation_id} lost reply after the core accepted {command} "
        "reconciles read-only without replay"
    )


TRIM_SUBCASES: Final = (
    (
        "edge_trim_accessible_input",
        "accessibility",
        f"{COMMANDS_SPEC}::hardening a11y clip.trim edge grips are separate 44px touch targets "
        "with keyboard draft, commit and cancel",
    ),
    (
        "edge_trim_cancel_conflict",
        "recovery",
        f"{COMMANDS_SPEC}::hardening recovery clip.trim drafts cancel on capture loss, Escape and "
        "a changed view without a transaction",
    ),
    ("edge_trim_history", "stress", EDIT_WORKLOAD),
)

# Frozen identity of the seven shell rows (plan section 6). The focus targets are the close-reason
# matrix: Close/Escape use launcher -> clip_editor -> production; a function switch focuses the
# activated function; top-level navigation focuses the activated page; capability/mount failure
# returns to the launcher beside the unavailable status; view destroy delegates to the host.
LAUNCHER: Final = '[data-h3-focus-key="nle-open-overlay"]'
OVERLAY: Final = '[data-h3-nle-surface="overlay_v1"]'
ALL_SLOTS: Final = (
    "navigation.function",
    "production.view",
    "production.anchor",
    "production.draft",
    "nle.overlay",
    "nle.timeline",
    "nle.transport",
    "nle.track",
    "nle.insert",
    "nle.range",
    "nle.clip",
    "settings.view",
)
NLE_SLOTS: Final = (
    "nle.overlay",
    "nle.timeline",
    "nle.transport",
    "nle.track",
    "nle.insert",
    "nle.range",
    "nle.clip",
)

UI_ROWS: Final[dict[str, dict[str, Any]]] = {
    "global_shell_identity": {
        "owned_selector": 'nav.h3n[aria-label="H3 Context pages"]',
        "role": "navigation",
        "relationship": (
            "three page buttons with exactly one aria-current page; the Production page owns the "
            "two-function tablist"
        ),
        "lifecycle_before": "unchanged",
        "lifecycle_after": "unchanged",
        "close_reasons": [],
        "focus_targets": {"page_activation": '[data-h3-focus-key="page-{page}"]'},
        "fallback_vocabulary": [],
        "retention_slots": ["navigation.function", "settings.view"],
        "a11y": "pages are one navigation landmark of 44px buttons with one current page",
        "recovery": "the selected page and function survive a view destroy and remount",
    },
    "function_switch": {
        "owned_selector": "[data-h3-director-function-tabs]",
        "role": "tablist",
        "relationship": (
            "manual-activation tabs: the selected tab controls data-h3-director-panel and an "
            "inactive panel has no focusable descendant"
        ),
        "lifecycle_before": "compact_ready",
        "lifecycle_after": "compact_ready",
        "close_reasons": [],
        "focus_targets": {"activation": '[data-h3-director-function="{function}"]'},
        "fallback_vocabulary": [],
        # M25-44: the Clip editor function's draft lives in the full editor (`nle.track`); the
        # compact editor that wrote `authoring.view`/`authoring.draft` is no longer mounted.
        "retention_slots": [
            "navigation.function",
            "production.view",
            "production.anchor",
            "production.draft",
            "nle.track",
        ],
        "a11y": (
            "manual-activation tabs move focus without side effects and activate on Enter or tap"
        ),
        "recovery": "drafts on both functions survive switching and a remount without an action",
    },
    "overlay_open": {
        "owned_selector": OVERLAY,
        "role": "dialog",
        "relationship": (
            "aria-modal dialog in one owned root; initial focus on its heading; Tab contained; no "
            "foreign root made inert or hidden"
        ),
        "lifecycle_before": "compact_ready",
        "lifecycle_after": "expanded",
        "close_reasons": [],
        "focus_targets": {"open": f"{OVERLAY} .h3-nle-header h2"},
        "fallback_vocabulary": [],
        "retention_slots": list(NLE_SLOTS),
        "a11y": "dialog starts at its heading and contains Tab without touching the host",
        "recovery": (
            "an explicit reopen restores the retained editor state paused and replays nothing"
        ),
    },
    "duplicate_open": {
        "owned_selector": '[data-h3-nle-entry="open"]',
        "role": "button",
        "relationship": "a second open while expanded keeps one root and one generation",
        "lifecycle_before": "expanded",
        "lifecycle_after": "expanded",
        "close_reasons": [],
        "focus_targets": {"duplicate": f"{OVERLAY} .h3-nle-header h2"},
        "fallback_vocabulary": [],
        "retention_slots": ["nle.overlay"],
        "a11y": "the real open origin and a backdrop tap keep one dialog and its focus",
        "recovery": "an open racing a pending capability read settles on one generation",
    },
    "overlay_close_return_focus": {
        "owned_selector": '[data-h3-nle-action="close"]',
        "role": "button",
        "relationship": "a close records exactly one reason, then returns focus per the matrix",
        "lifecycle_before": "expanded",
        "lifecycle_after": "compact_ready",
        "close_reasons": list(NON_DESTROY_CLOSE_REASONS),
        "focus_targets": {
            "explicit_close": LAUNCHER,
            "escape": LAUNCHER,
            "function_switch": '[data-h3-director-function="production_workbench"]',
            "top_level_navigation": '[data-h3-focus-key="page-context"]',
            "capability_or_mount_failure": LAUNCHER,
        },
        "fallback_vocabulary": [],
        "retention_slots": list(NLE_SLOTS),
        "a11y": "every close reason lands on its frozen focus target",
        "recovery": "every close keeps valid drafts and releases every owner before a reopen",
    },
    # M25-44 (one NLE): an unsupported runtime no longer falls back to a compact editor; the
    # launcher's unavailable region carries the surface status alone.
    "overlay_unavailable_status": {
        "owned_selector": '[data-h3-nle-unavailable="overlay_v1"] [data-h3-nle-status="surface"]',
        "role": "status",
        "relationship": (
            "an unsupported runtime announces the surface status beside the launcher and mounts "
            "no dialog"
        ),
        "lifecycle_before": "compact_ready",
        "lifecycle_after": "compact_unsupported",
        "close_reasons": ["capability_or_mount_failure"],
        "focus_targets": {"refusal": LAUNCHER},
        "fallback_vocabulary": [
            "media_runtime_unavailable",
            "compact_unsupported",
        ],
        "retention_slots": ["nle.clip"],
        "a11y": "the unavailable status is announced and focus stays on the launcher",
        "recovery": "a refused open keeps the retained drafts and a later supported open works",
    },
    "view_destroy_cleanup": {
        "owned_selector": "[data-h3-nle-root]",
        "role": "dialog",
        "relationship": (
            "a native sidebar close records view_destroy once, removes the owned root and "
            "releases every owner"
        ),
        "lifecycle_before": "expanded",
        "lifecycle_after": "compact_ready",
        "close_reasons": ["view_destroy"],
        "focus_targets": {"view_destroy": "host"},
        "fallback_vocabulary": [],
        "retention_slots": list(ALL_SLOTS),
        "a11y": "a native close delegates focus to the host without detached targets",
        "recovery": "a late reply after a native close cannot revive the destroyed view",
    },
}

ZERO_EFFECTS: Final = {
    "command": 0,
    "revision": 0,
    "job": 0,
    "output": 0,
    "queue": 0,
    "provider": 0,
}

# Non-command action inventory. Transport and the follower come from the M25-16 manifest's
# non-command rows; the output actions are the M25-19 inventory the NLE render affordance exposes.
TRANSPORT_A11Y: Final = (
    f"{ACTIONS_SPEC}::hardening a11y transport controls are named 44px targets reachable by "
    "keyboard and touch"
)
SEEK_A11Y: Final = (
    f"{ACTIONS_SPEC}::hardening a11y transport.seek steps one frame and pages one output second"
)
TRANSPORT_RECOVERY: Final = (
    f"{ACTIONS_SPEC}::hardening recovery transport restores its position paused after a "
    "remount and a blocked monitor recovers"
)
AUDIO_A11Y: Final = (
    f"{ACTIONS_SPEC}::hardening a11y audio follower is announced read-only and no audio control "
    "takes focus"
)
AUDIO_RECOVERY: Final = (
    f"{ACTIONS_SPEC}::hardening recovery audio follower suspends on hide and follows the accepted "
    "primary video again"
)
OUTPUT_A11Y: Final = (
    f"{ACTIONS_SPEC}::hardening a11y output actions are named 44px targets with keyboard paths "
    "and an extension-owned preview"
)
OUTPUT_RECOVERY: Final = (
    f"{ACTIONS_SPEC}::hardening recovery output status loss and remount never replay a render"
)
IMPORT_A11Y: Final = (
    f"{ACTIONS_SPEC}::hardening a11y import is reachable by pointer, keyboard and touch and "
    "focuses the highlighted result"
)
IMPORT_RECOVERY: Final = (
    f"{ACTIONS_SPEC}::hardening recovery import lost reply is uncertain until an explicit exact "
    "retry"
)
IMPORT_REFUSAL: Final = (
    f"{ACTIONS_SPEC}::hardening recovery import refusals keep no optimistic state and no "
    "timeline change"
)


def _action(
    row_id: str,
    authority_class: str,
    selector: str | None,
    accessibility: str,
    stress: str,
    recovery: str,
    row_class: str = "action",
) -> dict[str, Any]:
    return {
        "row_id": row_id,
        "row_class": row_class,
        "authority_class": authority_class,
        "control_selector": selector,
        "evidence": {
            "accessibility_evidence_id": accessibility,
            "stress_evidence_id": stress,
            "recovery_evidence_id": recovery,
        },
    }


def _transport(operation_id: str) -> dict[str, Any]:
    return _action(
        f"action.{operation_id}",
        "local_transport",
        f'[data-h3-nle-control="{operation_id}"]',
        SEEK_A11Y if operation_id == "transport.seek" else TRANSPORT_A11Y,
        PLAYBACK_WORKLOAD,
        TRANSPORT_RECOVERY,
    )


ACTION_ROWS: Final = (
    *(
        _transport(operation_id)
        for operation_id in (
            "transport.play",
            "transport.pause",
            "transport.step_back",
            "transport.step_forward",
            "transport.seek",
            "transport.recover",
            "transport.zoom_in",
            "transport.zoom_out",
            "transport.scroll",
        )
    ),
    _action(
        "action.audio.embedded_primary_follow",
        "canonical_derived_effect",
        '[data-h3-nle-status="audio"]',
        AUDIO_A11Y,
        PLAYBACK_WORKLOAD,
        AUDIO_RECOVERY,
    ),
    _action(
        "action.audio.deferred",
        "deferred_unavailable",
        None,
        AUDIO_A11Y,
        PLAYBACK_WORKLOAD,
        AUDIO_RECOVERY,
    ),
    *(
        _action(
            f"action.output.{name}",
            "output_action",
            "[data-h3-nle-render]",
            OUTPUT_A11Y,
            OUTPUT_WORKLOAD,
            OUTPUT_RECOVERY,
        )
        for name in ("render", "status", "cancel", "final_preview", "download")
    ),
    _action(
        "import.production_outputs",
        "output_action",
        '[data-h3-nle-control="asset.import_production"]',
        IMPORT_A11Y,
        IMPORT_WORKLOAD,
        IMPORT_RECOVERY,
        row_class="import",
    ),
    _action(
        "import.production_outputs.refusal",
        "output_action",
        '[data-h3-nle-control="asset.import_production"]',
        IMPORT_A11Y,
        IMPORT_WORKLOAD,
        IMPORT_REFUSAL,
        row_class="import",
    ),
)

# Owned recovery seams (plan sections 8 and 14.2). Worker and WebGL are not selected APIs: their
# rows prove zero allocation instead of introducing the API to inject a fault into.
RECOVERY_ROWS: Final = (
    ("seam.native_media", "native_media_error", 10),
    ("seam.compositor", "canvas_context_loss", 10),
    ("seam.follower", "follower_suspension", 10),
    ("seam.client_response", "response_loss", 10),
    ("seam.lifecycle", "view_release_mid_flight", 10),
    ("seam.backend_restart", "backend_restart", 1),
    ("seam.worker", "non_allocation", 0),
    ("seam.webgl", "non_allocation", 0),
)

MIB: Final = 1024 * 1024

# Frozen measurements (plan sections 6 and 14). `op` is the comparison the observed value must
# satisfy against `value`. The report never widens these; changing one is a plan amendment.
MEASUREMENTS: Final = (
    # NLE-STRESS-V1 runtime budgets.
    ("stress.main_thread_busy_ratio", "NLE-STRESS-V1", "ratio", "<=", 0.5, PLAYBACK_WORKLOAD),
    ("stress.idle_long_task_max", "NLE-STRESS-V1", "ms", "<=", 50, PLAYBACK_WORKLOAD),
    ("stress.render_p95", "NLE-STRESS-V1", "ms", "<=", 16, PLAYBACK_WORKLOAD),
    # M25-45 AC45-04: the whole compositor, from the render call to the frame after it. The
    # row above is the overlay's React commit, which is a different quantity and stays where
    # it was; a larger preview is a claim about what the viewer waits for, so it is measured.
    ("stress.presentation_p95", "NLE-STRESS-V1", "ms", "<=", 50, PLAYBACK_WORKLOAD),
    ("stress.presentation_max", "NLE-STRESS-V1", "ms", "<=", 150, PLAYBACK_WORKLOAD),
    ("stress.handler_p95", "NLE-STRESS-V1", "ms", "<=", 50, EDIT_WORKLOAD),
    ("stress.rebase_presentation_p95", "NLE-STRESS-V1", "ms", "<=", 100, EDIT_WORKLOAD),
    ("stress.sidebar_commits_per_tick", "NLE-STRESS-V1", "count", "==", 0, PLAYBACK_WORKLOAD),
    ("stress.nle_commits_per_edit_max", "NLE-STRESS-V1", "count", "<=", 4, EDIT_WORKLOAD),
    ("stress.heap_growth", "NLE-STRESS-V1", "bytes", "<=", 256 * MIB, PLAYBACK_WORKLOAD),
    ("stress.mounted_clip_nodes_max", "NLE-STRESS-V1", "count", "<=", 96, PLAYBACK_WORKLOAD),
    ("stress.mounted_track_rows_max", "NLE-STRESS-V1", "count", "<=", 8, PLAYBACK_WORKLOAD),
    ("stress.row_ruler_nodes_max", "NLE-STRESS-V1", "count", "<=", 256, PLAYBACK_WORKLOAD),
    ("stress.nle_dom_nodes_max", "NLE-STRESS-V1", "count", "<=", 1500, PLAYBACK_WORKLOAD),
    (
        "stress.composition_canvas_surfaces",
        "NLE-STRESS-V1",
        "count",
        "==",
        1,
        PLAYBACK_WORKLOAD,
    ),
    (
        "stress.decoration_canvas_surfaces",
        "NLE-STRESS-V1",
        "count",
        "<=",
        1,
        PLAYBACK_WORKLOAD,
    ),
    ("stress.active_video_owners_max", "NLE-STRESS-V1", "count", "<=", 2, PLAYBACK_WORKLOAD),
    ("stress.warm_video_owners_max", "NLE-STRESS-V1", "count", "<=", 1, PLAYBACK_WORKLOAD),
    ("stress.retained_surfaces_max", "NLE-STRESS-V1", "count", "<=", 16, PLAYBACK_WORKLOAD),
    (
        "stress.retained_surface_bytes_max",
        "NLE-STRESS-V1",
        "bytes",
        "<=",
        256 * MIB,
        PLAYBACK_WORKLOAD,
    ),
    ("stress.worker_allocations", "NLE-STRESS-V1", "count", "==", 0, PLAYBACK_WORKLOAD),
    ("stress.blob_urls_max", "NLE-STRESS-V1", "count", "<=", 2, PLAYBACK_WORKLOAD),
    ("stress.follower_count_max", "NLE-STRESS-V1", "count", "<=", 1, PLAYBACK_WORKLOAD),
    ("stress.playback_advancing", "NLE-STRESS-V1", "ms", ">=", 600_000, PLAYBACK_WORKLOAD),
    ("stress.teardown_max", "NLE-STRESS-V1", "ms", "<=", 500, REMOUNT_WORKLOAD),
    ("stress.cancellation_max", "NLE-STRESS-V1", "ms", "<=", 250, SEAM_WORKLOAD),
    # NLE-STRESS-V1 action floors.
    ("floor.seeks", "NLE-STRESS-V1", "count", ">=", 600, PLAYBACK_WORKLOAD),
    ("floor.audio_seeks", "NLE-STRESS-V1", "count", ">=", 120, PLAYBACK_WORKLOAD),
    ("floor.edit_attempts", "NLE-STRESS-V1", "count", ">=", 240, EDIT_WORKLOAD),
    ("floor.conflict_rebase", "NLE-STRESS-V1", "count", ">=", 40, EDIT_WORKLOAD),
    ("floor.primary_video_geometry", "NLE-STRESS-V1", "count", ">=", 40, EDIT_WORKLOAD),
    ("floor.undo_redo_cycles", "NLE-STRESS-V1", "count", ">=", 20, EDIT_WORKLOAD),
    ("floor.source_replacements", "NLE-STRESS-V1", "count", ">=", 10, EDIT_WORKLOAD),
    ("floor.edge_drags", "NLE-STRESS-V1", "count", ">=", 20, EDIT_WORKLOAD),
    ("floor.edge_drags_per_direction", "NLE-STRESS-V1", "count", ">=", 5, EDIT_WORKLOAD),
    ("floor.cancelled_drafts", "NLE-STRESS-V1", "count", ">=", 10, EDIT_WORKLOAD),
    ("floor.trim_undo_redo", "NLE-STRESS-V1", "count", ">=", 10, EDIT_WORKLOAD),
    ("floor.move_drags", "NLE-STRESS-V1", "count", ">=", 20, EDIT_WORKLOAD),
    ("floor.group_move_drags", "NLE-STRESS-V1", "count", ">=", 5, EDIT_WORKLOAD),
    ("floor.cross_track_moves", "NLE-STRESS-V1", "count", ">=", 5, EDIT_WORKLOAD),
    ("floor.marquee_selections", "NLE-STRESS-V1", "count", ">=", 5, EDIT_WORKLOAD),
    ("floor.ruler_scrubs", "NLE-STRESS-V1", "count", ">=", 10, EDIT_WORKLOAD),
    ("floor.follower_injections", "NLE-STRESS-V1", "count", ">=", 10, SEAM_WORKLOAD),
    ("floor.remount_cycles", "NLE-STRESS-V1", "count", ">=", 25, REMOUNT_WORKLOAD),
    ("floor.edit_phase_wall", "NLE-STRESS-V1", "ms", "<=", 1_200_000, EDIT_WORKLOAD),
    # M25-16-overlay-v1-smoke inherited budgets.
    ("smoke.heap_growth", "M25-16-overlay-v1-smoke", "bytes", "<=", 128 * MIB, SMOKE_WORKLOAD),
    ("smoke.playback_advancing", "M25-16-overlay-v1-smoke", "ms", ">=", 300_000, SMOKE_WORKLOAD),
    ("smoke.nle_dom_nodes_max", "M25-16-overlay-v1-smoke", "count", "<=", 1500, SMOKE_WORKLOAD),
    ("smoke.sidebar_commits_per_tick", "M25-16-overlay-v1-smoke", "count", "==", 0, SMOKE_WORKLOAD),
    # UI-CONTRACT-STRESS-V1 floors and zero effects.
    ("ui.function_switches", "UI-CONTRACT-STRESS-V1", "count", ">=", 100, UI_WORKLOAD),
    ("ui.overlay_cycles", "UI-CONTRACT-STRESS-V1", "count", ">=", 25, UI_WORKLOAD),
    ("ui.duplicate_opens", "UI-CONTRACT-STRESS-V1", "count", ">=", 10, UI_WORKLOAD),
    ("ui.fallbacks", "UI-CONTRACT-STRESS-V1", "count", ">=", 10, UI_WORKLOAD),
    ("ui.navigation_closures", "UI-CONTRACT-STRESS-V1", "count", ">=", 10, UI_WORKLOAD),
    ("ui.view_destroy_cycles", "UI-CONTRACT-STRESS-V1", "count", ">=", 10, UI_WORKLOAD),
    ("ui.navigation_effects", "UI-CONTRACT-STRESS-V1", "count", "==", 0, UI_WORKLOAD),
    ("ui.hidden_playback_or_polling", "UI-CONTRACT-STRESS-V1", "count", "==", 0, UI_WORKLOAD),
    # Accessibility thresholds.
    ("a11y.frame_step", "M25-16-overlay-v1-smoke", "frames", "==", 1, SEEK_A11Y),
    ("a11y.page_step", "M25-16-overlay-v1-smoke", "frames", "==", 24, SEEK_A11Y),
    # Deferred audio negative evidence (plan section 6).
    ("audio.standalone_allocations", "NLE-STRESS-V1", "count", "==", 0, PLAYBACK_WORKLOAD),
    # The embedded PCM follower owns one lazy workspace context; this is a distinct quantity.
    ("audio.workspace_context_allocations", "NLE-STRESS-V1", "count", "<=", 1, PLAYBACK_WORKLOAD),
    # Render companion and backend resources (plan section 14).
    ("render.jobs", "NLE-STRESS-RENDER-V1", "count", ">=", 8, OUTPUT_WORKLOAD),
    ("render.running_max", "NLE-STRESS-RENDER-V1", "count", "<=", 1, OUTPUT_WORKLOAD),
    ("render.queued_max", "NLE-STRESS-RENDER-V1", "count", "<=", 1, OUTPUT_WORKLOAD),
    ("render.status_clients_max", "NLE-STRESS-RENDER-V1", "count", "<=", 2, OUTPUT_WORKLOAD),
    (
        "backend.rss_increment_max",
        "NLE-STRESS-RENDER-V1",
        "bytes",
        "<=",
        1024 * MIB,
        RENDER_COMPANION,
    ),
)


def sha256_of(path: Path) -> str:
    # `sha256:`-prefixed like every other fingerprint here: a bare 64-character hex string reads
    # to `detect-secrets` as a high-entropy credential.
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _control_manifest() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(CONTROL_MANIFEST_PATH.read_text(encoding="utf-8")))


def _command_rows(control: dict[str, Any]) -> list[dict[str, Any]]:
    by_command = {row["command"]: row for row in control["command_rows"]}
    # Exact-set join on the backend command inventory; a drifted control manifest fails here,
    # at generation, instead of producing a manifest that silently omits a command.
    if set(by_command) != set(NLE_OPERATION_IDS) or len(by_command) != len(control["command_rows"]):
        raise ValueError("control manifest command rows differ from NLE_OPERATION_IDS")
    rows: list[dict[str, Any]] = []
    for command in NLE_OPERATION_IDS:
        source = by_command[command]
        operation_id = source["operation_id"]
        row: dict[str, Any] = {
            "command": command,
            "operation_id": operation_id,
            "control_selector": source["control_selector"],
            "role": source["role"],
            "accessible_name": source["accessible_name"],
            "recovery_injection": "response_loss",
            "evidence": {
                "accessibility_evidence_id": (
                    f"{COMMANDS_SPEC}::{command_a11y_title(operation_id, command)}"
                ),
                "stress_evidence_id": EDIT_WORKLOAD,
                "recovery_evidence_id": (
                    f"{COMMANDS_SPEC}::{command_recovery_title(operation_id, command)}"
                ),
            },
        }
        if command == "trim_clip":
            row["subcases"] = [
                {"subcase_id": subcase, "dimension": dimension, "evidence_id": evidence}
                for subcase, dimension, evidence in TRIM_SUBCASES
            ]
        rows.append(row)
    return rows


def _ui_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for invariant in SIDEBAR_UI_INVARIANT_IDS:
        spec = UI_ROWS[invariant]
        rows.append(
            {
                "invariant_id": invariant,
                "owned_selector": spec["owned_selector"],
                "role": spec["role"],
                "relationship": spec["relationship"],
                "lifecycle_before": spec["lifecycle_before"],
                "lifecycle_after": spec["lifecycle_after"],
                "close_reasons": spec["close_reasons"],
                "focus_targets": spec["focus_targets"],
                "fallback_vocabulary": spec["fallback_vocabulary"],
                "expected_effects": dict(ZERO_EFFECTS),
                "retention_slots": spec["retention_slots"],
                "evidence": {
                    "accessibility_evidence_id": (
                        f"{SHELL_SPEC}::hardening a11y ui {invariant} {spec['a11y']}"
                    ),
                    "stress_evidence_id": UI_WORKLOAD,
                    "recovery_evidence_id": (
                        f"{SHELL_SPEC}::hardening recovery ui {invariant} {spec['recovery']}"
                    ),
                },
            }
        )
    return rows


def build() -> dict[str, Any]:
    control = _control_manifest()
    return {
        "schema": MANIFEST_SCHEMA,
        "version": 1,
        "surface": "overlay_v1",
        "joined_sources": {
            "control_coverage_manifest_sha256": sha256_of(CONTROL_MANIFEST_PATH),
            "sidebar_editor_ui_contract_sha256": sha256_of(SHELL_CONTRACT_PATH),
        },
        "fixtures": {
            "smoke": "M25-16-overlay-v1-smoke",
            "stress": "NLE-STRESS-V1",
            "render": "NLE-STRESS-RENDER-V1",
            "ui_stress": "UI-CONTRACT-STRESS-V1",
        },
        "dimensions": list(DIMENSIONS),
        "command_rows": _command_rows(control),
        "ui_rows": _ui_rows(),
        "action_rows": [dict(row) for row in ACTION_ROWS],
        "recovery_rows": [
            {
                "seam_id": seam,
                "injection": injection,
                "injections_min": floor,
                "evidence_id": SEAM_WORKLOAD,
            }
            for seam, injection, floor in RECOVERY_ROWS
        ],
        "measurements": [
            {
                "measurement_id": measurement,
                "scope": scope,
                "unit": unit,
                "threshold": {"op": op, "value": value},
                "evidence_id": evidence,
            }
            for measurement, scope, unit, op, value, evidence in MEASUREMENTS
        ],
    }


WORKLOADS: Final = frozenset(
    {
        EDIT_WORKLOAD,
        PLAYBACK_WORKLOAD,
        SMOKE_WORKLOAD,
        UI_WORKLOAD,
        SEAM_WORKLOAD,
        REMOUNT_WORKLOAD,
        OUTPUT_WORKLOAD,
        IMPORT_WORKLOAD,
    }
)


class ManifestError(ValueError):
    """The manifest's membership or identity departs from its frozen sources."""


def _exact(label: str, actual: list[str], expected: tuple[str, ...] | list[str]) -> None:
    duplicates = sorted({item for item in actual if actual.count(item) > 1})
    if duplicates:
        raise ManifestError(f"{label}: duplicate rows {duplicates}")
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        raise ManifestError(f"{label}: missing {missing}, extra {extra}")


def validate(manifest: dict[str, Any], control: dict[str, Any] | None = None) -> None:
    """Semantic checks beyond the JSON schema: exact sets, frozen identity and per-row evidence.

    The schema bounds shapes; this rejects a manifest whose rows are missing, duplicated, extra,
    covered only by a category-level test, or whose frozen identity drifted from its sources.
    """

    control = control if control is not None else _control_manifest()
    by_command = {row["command"]: row for row in control["command_rows"]}
    commands = [row["command"] for row in manifest["command_rows"]]
    _exact("command_rows", commands, NLE_OPERATION_IDS)
    per_test: dict[str, str] = {}
    for row in manifest["command_rows"]:
        source = by_command[row["command"]]
        for field in ("operation_id", "control_selector", "role", "accessible_name"):
            if row[field] != source[field]:
                raise ManifestError(f"{row['command']}: {field} differs from the control manifest")
        evidence = row["evidence"]
        # A per-command dimension must name this command's own case; a title that does not is a
        # category-level test standing in for a missing per-command assertion.
        for dimension in ("accessibility", "recovery"):
            title = evidence[f"{dimension}_evidence_id"]
            if f" {row['operation_id']} " not in title or f" {row['command']} " not in title:
                raise ManifestError(f"{row['command']}: {dimension} evidence is not per-command")
            owner = per_test.setdefault(title, row["command"])
            if owner != row["command"]:
                raise ManifestError(
                    f"{row['command']}: {dimension} evidence is shared with {owner}"
                )
        if evidence["stress_evidence_id"] != EDIT_WORKLOAD:
            raise ManifestError(f"{row['command']}: stress evidence is not the edit workload")
        subcases = [subcase["subcase_id"] for subcase in row.get("subcases", [])]
        expected_subcases = [subcase for subcase, _, _ in TRIM_SUBCASES]
        _exact(
            f"{row['command']} subcases",
            subcases,
            expected_subcases if row["command"] == "trim_clip" else [],
        )
    invariants = [row["invariant_id"] for row in manifest["ui_rows"]]
    _exact("ui_rows", invariants, SIDEBAR_UI_INVARIANT_IDS)
    if set(invariants) & set(commands):
        raise ManifestError("ui_rows: a UI invariant shares an identifier with a command")
    for row in manifest["ui_rows"]:
        for dimension in ("accessibility", "recovery"):
            if f" ui {row['invariant_id']} " not in row["evidence"][f"{dimension}_evidence_id"]:
                raise ManifestError(f"{row['invariant_id']}: {dimension} evidence is not per-row")
        if row["evidence"]["stress_evidence_id"] != UI_WORKLOAD:
            raise ManifestError(f"{row['invariant_id']}: stress evidence is not the UI workload")
        if set(row["focus_targets"]) != set(UI_ROWS[row["invariant_id"]]["focus_targets"]):
            raise ManifestError(f"{row['invariant_id']}: focus targets differ from the matrix")
        if row["close_reasons"] != UI_ROWS[row["invariant_id"]]["close_reasons"]:
            raise ManifestError(f"{row['invariant_id']}: close reasons differ from the matrix")
    action_ids = [row["row_id"] for row in manifest["action_rows"]]
    _exact("action_rows", action_ids, [row["row_id"] for row in ACTION_ROWS])
    operation_ids = {row["operation_id"] for row in manifest["command_rows"]}
    for row_id in action_ids:
        if row_id.removeprefix("action.") in operation_ids:
            raise ManifestError(f"{row_id}: a non-command row carries a command identity")
    for row in manifest["action_rows"]:
        if row["evidence"]["stress_evidence_id"] not in WORKLOADS:
            raise ManifestError(f"{row['row_id']}: stress evidence is not a named workload")
    _exact(
        "recovery_rows",
        [row["seam_id"] for row in manifest["recovery_rows"]],
        [seam for seam, _, _ in RECOVERY_ROWS],
    )
    frozen = {seam: (injection, floor) for seam, injection, floor in RECOVERY_ROWS}
    for row in manifest["recovery_rows"]:
        if (row["injection"], row["injections_min"]) != frozen[row["seam_id"]]:
            raise ManifestError(f"{row['seam_id']}: injection or floor differs from the plan")
    _exact(
        "measurements",
        [row["measurement_id"] for row in manifest["measurements"]],
        [measurement for measurement, *_ in MEASUREMENTS],
    )
    thresholds = {
        measurement: (scope, unit, op, value)
        for measurement, scope, unit, op, value, _ in MEASUREMENTS
    }
    for row in manifest["measurements"]:
        declared = (
            row["scope"],
            row["unit"],
            row["threshold"]["op"],
            row["threshold"]["value"],
        )
        # A threshold is a frozen plan value: widening it here to obtain a pass is exactly what
        # plan section 6 forbids, so any difference from the plan table is a rejection.
        if declared != thresholds[row["measurement_id"]]:
            raise ManifestError(f"{row['measurement_id']}: threshold differs from the plan")


def render() -> str:
    manifest = build()
    validate(manifest)
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="refresh the hardening manifest")
    group.add_argument("--check", action="store_true", help="verify the hardening manifest")
    args = parser.parse_args(argv)
    expected = render()
    if args.write:
        # Byte-exact LF output so `--check` holds identically on every platform.
        MANIFEST_PATH.write_bytes(expected.encode("utf-8"))
        print(f"wrote {MANIFEST_PATH.relative_to(ROOT).as_posix()}")
        return 0
    actual = MANIFEST_PATH.read_bytes().decode("utf-8") if MANIFEST_PATH.exists() else ""
    if actual != expected:
        print("hardening manifest drifted; rerun with --write", file=sys.stderr)
        return 1
    print("hardening manifest is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
