// v2 replaced the mounted-view-only memory of v1 with bounded session
// retention. Every editable or view-state field of the five surfaces carries exactly one
// disposition; the retained and reconciled fields are the typed slots of
// `state/sidebarRetention.ts`.
//
// v3 uses one NLE. The `clip_editor` function hosts the launcher and a read-only project summary; the
// compact editing surface and its five `compact_*` rows are retired. The overlay opens at the
// viewport minus its margin with the four reference regions at every width, so `overlay_pane` is
// the media bin's tab and the three splitter positions are the reconciled `overlay_layout`.
export const SIDEBAR_EDITOR_UI_CONTRACT_V3 = {
  schema: "h3.context.sidebar_editor_ui.v3",
  topLevelPages: ["context", "production", "settings"],
  productionFunctions: ["production_workbench", "clip_editor"],
  initialProductionFunction: "production_workbench",
  persistence: "bounded_session_memory",
  selectors: {
    tabs: "v1",
    productionWorkbench: "production_workbench",
    clipEditor: "clip_editor",
  },
  clipEditorSurface: "launcher_and_summary",
  retention: {
    lifetime: "extension_session",
    restore: "same_authority_scope",
    transport: "paused",
    fullEditor: "explicit_open",
    surfaces: {
      navigation: {
        page: "retain",
        production_function: "retain",
        roving_focus: "reconcile",
        focus_identity: "retain",
        diagnostics_feedback: "clear",
      },
      context: {
        app_mode_draft: "retain",
        editing_stage: "retain",
        workspace_stage_drafts: "reconcile",
        anchor_designation: "clear",
        reference_picker: "clear",
        semantic_review: "clear",
      },
      production_workbench: {
        segment_selection: "retain",
        relation_draft: "reconcile",
        move_targets: "reconcile",
        segment_window: "reconcile",
        authority_expansion: "retain",
        release_confirmation: "clear",
        drag_gesture: "clear",
        media_preview: "clear",
        proposal_review: "clear",
      },
      clip_editor: {
        clip_selection: "retain",
        overlay_open: "clear",
        overlay_geometry: "reconcile",
        overlay_layout: "reconcile",
        overlay_pane: "retain",
        timeline_view: "retain",
        timeline_measurement: "clear",
        trim_gesture: "clear",
        playhead: "reconcile",
        playback: "clear",
        source_preview: "clear",
        track_draft: "reconcile",
        insert_draft: "reconcile",
        range_draft: "reconcile",
        clip_draft: "reconcile",
        summary_release_confirmation: "clear",
      },
      settings: {
        section_expansion: "retain",
        provider_selection: "retain",
        language: "retain",
        credential: "clear",
      },
    },
  },
} as const;

export type ProductionFunctionId =
  (typeof SIDEBAR_EDITOR_UI_CONTRACT_V3.productionFunctions)[number];
