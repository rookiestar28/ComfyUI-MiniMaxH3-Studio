/**
 * GENERATED FILE -- DO NOT EDIT.
 *
 * Regenerate with `python scripts/cross_language_surface.py --write`, which derives
 * every key set below from the Python `to_wire` that produces it. The committed
 * record is `comfyui_h3_context/contracts/cross_language_surface_v1.json`; a frontend
 * test asserts this module reproduces it exactly, so an edit here fails the gate
 * rather than shipping.
 *
 * Key sets only. Every semantic, privacy, lifecycle and identity guard stays
 * handwritten in the codec that owns it -- those are refinements this file has no
 * authority over.
 *
 * Surface fingerprint: sha256:3e5de6fa127be4ed924e0fc7217cac4cedf31a572d91d96b99a095ed1a532c6b
 */

/** comfyui_h3_context/core/assisted_authoring_scope.py :: AssistedAuthoringState */
export const assistedAuthoringStateKeys: readonly string[] = [
  "authorized_for_this_action",
  "available",
  "defaulted",
  "ready",
  "selected",
];

/** comfyui_h3_context/core/composition_contract.py :: AudioExtension */
export const audioExtensionKeys: readonly string[] = [
  "command_members",
  "command_namespace",
  "embedded_renderer_variant",
  "final_render_edit_capability",
  "independent_audio_renderer_variant",
  "preview_edit_capability",
  "reason",
  "schema",
  "track_profile",
];

/** comfyui_h3_context/core/composition_contract.py :: CapabilityProfile */
export const capabilityProfileKeys: readonly string[] = [
  "active_video_limit",
  "audio_stream_rate_sentinel",
  "cancel_deadline_ms",
  "canvas_limit",
  "cross_origin_isolation_required",
  "decision_receipt_fingerprints",
  "embedded_audio_policy",
  "engine_profile_id",
  "frame_event_fallbacks",
  "frame_observer",
  "input_audio_codecs",
  "input_containers",
  "input_pixel_formats",
  "input_video_codecs",
  "media_transport",
  "mse_required",
  "network_policy",
  "observation_corpus_fingerprints",
  "observation_corpus_ids",
  "pending_rvfc_limit",
  "probe_audio_stream_keys",
  "probe_empty_array_keys",
  "probe_format_keys",
  "probe_root_keys",
  "probe_video_stream_keys",
  "teardown_deadline_ms",
  "visual_compositor",
  "warm_video_limit",
  "webcodecs_required",
];

/** comfyui_h3_context/core/composition_contract.py :: ClipAudio */
export const clipAudioKeys: readonly string[] = [
  "fade_in_frames",
  "fade_out_frames",
  "gain_mb",
  "muted",
];

/** comfyui_h3_context/core/composition_contract.py :: CompositionBlocker */
export const compositionBlockerKeys: readonly string[] = ["code", "subject_id"];

/** comfyui_h3_context/core/composition_contract.py :: CompositionClip */
export const compositionClipKeys: readonly string[] = [
  "asset_id",
  "audio",
  "blend",
  "clip_id",
  "crop",
  "duration_frames",
  "effect",
  "enabled",
  "opacity_bp",
  "source_start_frame",
  "start_frame",
  "text",
  "track_id",
  "transform",
  "transition",
];

/** comfyui_h3_context/core/composition_contract.py :: CompositionTrack */
export const compositionTrackKeys: readonly string[] = [
  "enabled",
  "kind",
  "locked",
  "order",
  "track_id",
];

/** comfyui_h3_context/core/composition_contract.py :: Crop */
export const cropKeys: readonly string[] = [
  "bottom_bp",
  "left_bp",
  "right_bp",
  "top_bp",
];

/** comfyui_h3_context/core/prompt_model_session.py :: DiscoveryCandidate */
export const discoveryCandidateKeys: readonly string[] = [
  "identifier",
  "reason",
];

/** comfyui_h3_context/core/composition_contract.py :: Effect */
export const effectKeys: readonly string[] = [
  "brightness_permille",
  "contrast_permille",
  "kind",
  "saturation_permille",
];

/** comfyui_h3_context/core/composition_contract.py :: EmbeddedAudioSpan */
export const embeddedAudioSpanKeys: readonly string[] = [
  "asset_id",
  "clip_id",
  "output_end_sample",
  "output_start_sample",
  "source_end_sample",
  "source_start_sample",
];

/** comfyui_h3_context/core/ui_projection.py :: ExecutionCorrelation */
export const executionCorrelationKeys: readonly string[] = [
  "execution_node_id",
  "prompt_id",
];

/** comfyui_h3_context/core/generation_profile.py :: FamilyProfile */
export const familyProfileKeys: readonly string[] = [
  "anchor_node_type",
  "disposition",
  "family",
  "remediation",
  "task_modes",
  "template_name",
  "unsatisfied_slots",
];

/** comfyui_h3_context/core/generation_profile.py :: GenerationProfile */
export const generationProfileKeys: readonly string[] = [
  "families",
  "schema",
  "template_revision",
];

/** comfyui_h3_context/core/generation_sequence.py :: GenerationSequenceProgress */
export const generationSequenceProgressKeys: readonly string[] = [
  "artifact_output_fingerprint",
  "artifact_receipt_fingerprint",
  "attempt",
  "failure_code",
  "host_owner_id",
  "job_id",
  "ordinal",
  "queue_prompt_id",
  "segment_id",
  "state",
  "transaction_id",
];

/** comfyui_h3_context/core/generation_sequence.py :: GenerationSequenceProjection */
export const generationSequenceProjectionKeys: readonly string[] = [
  "cancellation_requested",
  "complete",
  "correlation",
  "eligible_commands",
  "max_concurrency",
  "progress",
  "schema",
  "sequence_fingerprint",
  "sequence_id",
  "state_fingerprint",
  "workspace_fingerprint",
  "workspace_id",
  "workspace_revision",
];

/** comfyui_h3_context/core/composition_contract.py :: OutputProfile */
export const outputProfileKeys: readonly string[] = [
  "audio_codec",
  "audio_policy",
  "channels",
  "color_policy",
  "container",
  "duration_frames",
  "final_impulse_tolerance_samples",
  "frame_rate",
  "height",
  "pixel_aspect",
  "pixel_format",
  "preview_max_av_drift_samples",
  "profile_id",
  "sample_rate",
  "time_base",
  "video_codec",
  "width",
];

/** comfyui_h3_context/core/product_shell.py :: ProductShellBinding */
export const productShellBindingKeys: readonly string[] = [
  "asset_id",
  "kind",
  "native_child_path",
  "native_input",
  "presentation_label",
  "presentation_ordinal",
];

/** comfyui_h3_context/core/product_shell.py :: ProductShellHostProfile */
export const productShellHostProfileKeys: readonly string[] = [
  "core_revision",
  "core_version",
  "frontend_revision",
  "frontend_version",
  "node_api",
];

/** comfyui_h3_context/core/product_shell.py :: ProductShellProjection */
export const productShellProjectionKeys: readonly string[] = [
  "assisted_authoring",
  "assisted_ready",
  "bindings",
  "correlation",
  "field_ids",
  "host",
  "limitations",
  "native_node_id",
  "native_queue_ready",
  "product_scope",
  "profile",
  "prompt_export_ready",
  "prompt_fingerprint",
  "qualification_plan_fingerprint",
  "readiness_reason",
  "report_fingerprint",
  "report_id",
  "report_revision",
  "schema",
  "task_mode",
];

/** comfyui_h3_context/core/provider_settings.py :: ProviderConsentView */
export const providerConsentViewKeys: readonly string[] = [
  "cost_policy_sha256",
  "max_cost_micro_usd",
  "max_input_tokens",
  "max_output_tokens",
  "media_upload_consented",
  "network_permitted",
  "price_basis_id",
  "profile_id",
  "revision",
  "scope",
  "status",
];

/** comfyui_h3_context/core/provider_settings.py :: ProviderDiagnostic */
export const providerDiagnosticKeys: readonly string[] = [
  "outcome_id",
  "parameters",
  "remediation",
  "severity",
];

/** comfyui_h3_context/core/provider_settings.py :: ProviderIntentResult */
export const providerIntentResultKeys: readonly string[] = [
  "accepted",
  "projection",
  "rejection",
  "schema",
];

/** comfyui_h3_context/core/provider_settings.py :: ProviderProfileView */
export const providerProfileViewKeys: readonly string[] = [
  "adapter_version",
  "cost_class",
  "family",
  "host",
  "license_id",
  "limitations",
  "model_digest",
  "model_id",
  "parser_version",
  "port",
  "profile_id",
  "provider_label",
  "qualification_state",
  "retention_policy",
  "usage_receipt_required",
  "wire_dialect",
];

/** comfyui_h3_context/core/provider_settings.py :: ProviderSettingsProjection */
export const providerSettingsProjectionKeys: readonly string[] = [
  "assisted_authoring",
  "candidates",
  "candidates_truncated",
  "catalog_empty",
  "consent",
  "consent_required",
  "credential_last_four",
  "credential_present",
  "credential_required",
  "diagnostic",
  "disclosure",
  "profiles",
  "reachability_observed",
  "readiness",
  "revision",
  "schema",
  "selected_model_id",
  "selected_profile_id",
];

/** comfyui_h3_context/core/composition_contract.py :: PublicAsset */
export const publicAssetKeys: readonly string[] = [
  "asset_id",
  "embedded_audio",
  "kind",
  "landmarks",
  "source_frame_count",
  "source_sample_count",
  "source_time_base",
  "timestamp_policy",
];

/** comfyui_h3_context/core/composition_contract.py :: PublicCompositionSnapshot */
export const publicCompositionSnapshotKeys: readonly string[] = [
  "assets",
  "audio_extension",
  "blockers",
  "capability",
  "clips",
  "operation_profile_id",
  "output",
  "profile_id",
  "project_id",
  "public_fingerprint",
  "render_vocabulary",
  "schema",
  "timeline_fingerprint",
  "timeline_revision",
  "tracks",
  "workspace_fingerprint",
  "workspace_handle",
  "workspace_revision",
];

/** comfyui_h3_context/core/composition_contract.py :: Rational */
export const rationalKeys: readonly string[] = ["den", "num"];

/** comfyui_h3_context/core/composition_contract.py :: RenderVocabulary */
export const renderVocabularyKeys: readonly string[] = [
  "job_schema",
  "reasons",
  "receipt_schema",
  "request_schema",
  "schema",
  "states",
  "terminal_states",
];

/** comfyui_h3_context/core/composition_contract.py :: ResolvedLayer */
export const resolvedLayerKeys: readonly string[] = [
  "asset_id",
  "blend",
  "clip_id",
  "crop",
  "effect",
  "opacity_bp",
  "operation_ids",
  "source_frame",
  "source_pts",
  "text",
  "track_id",
  "transform",
  "transition_elapsed_frames",
];

/** comfyui_h3_context/core/composition_contract.py :: ResolvedScene */
export const resolvedSceneKeys: readonly string[] = [
  "audio_span",
  "blockers",
  "frame",
  "layers",
  "profile_id",
  "public_fingerprint",
  "schema",
];

/** comfyui_h3_context/core/segment_workspace.py :: SegmentDuration */
export const segmentDurationKeys: readonly string[] = [
  "delivered_milliseconds",
  "duration_milliseconds",
  "frame_count",
  "snapped",
];

/** comfyui_h3_context/core/semantic_proposal_review.py :: SemanticProposalClarification */
export const semanticProposalClarificationKeys: readonly string[] = [
  "clarification_id",
  "label",
  "reason_code",
];

/** comfyui_h3_context/core/semantic_proposal_review.py :: SemanticProposalReviewGroup */
export const semanticProposalReviewGroupKeys: readonly string[] = [
  "collection",
  "items",
];

/** comfyui_h3_context/core/semantic_proposal_review.py :: SemanticProposalReviewHandle */
export const semanticProposalReviewHandleKeys: readonly string[] = [
  "available",
  "correlation",
  "reason",
  "report_fingerprint",
  "review_id",
  "schema",
  "transaction_fingerprint",
  "workspace_fingerprint",
];

/** comfyui_h3_context/core/semantic_proposal_review.py :: SemanticProposalReviewItem */
export const semanticProposalReviewItemKeys: readonly string[] = [
  "change_kind",
  "constraint_labels",
  "reason_code",
  "reference_labels",
  "summary",
  "target_id",
  "uncertainty_codes",
];

/** comfyui_h3_context/core/semantic_proposal_review.py :: SemanticProposalReviewProjection */
export const semanticProposalReviewProjectionKeys: readonly string[] = [
  "action_reasons",
  "actions",
  "attempt",
  "changed_collections",
  "clarifications",
  "correlation",
  "groups",
  "reason_code",
  "report_fingerprint",
  "review_id",
  "revision",
  "schema",
  "segment_id",
  "state",
  "terminal",
  "transaction_fingerprint",
  "uncertainty_codes",
  "workspace_fingerprint",
  "workspace_id",
  "workspace_revision",
];

/** comfyui_h3_context/core/sidebar_workspace.py :: SidebarWorkspaceProjection */
export const sidebarWorkspaceProjectionKeys: readonly string[] = [
  "actions",
  "assisted_authoring",
  "base_prompt_fingerprint",
  "bindings",
  "capabilities",
  "comparison",
  "correlation",
  "diagnostics",
  "evidence",
  "exact_text",
  "guide_conformance",
  "lifecycle",
  "limitations",
  "media_receipt",
  "plan_steps",
  "planning",
  "product_scope",
  "profile",
  "prompt_fingerprint",
  "prompt_text",
  "prompt_text_redacted",
  "proposal",
  "receipt",
  "reference_candidates",
  "report_fingerprint",
  "report_id",
  "report_revision",
  "resources",
  "schema",
  "stages",
  "subject_candidates",
  "task_mode",
  "validation_status",
  "workspace_id",
];

/** comfyui_h3_context/core/composition_contract.py :: TextStyle */
export const textStyleKeys: readonly string[] = [
  "align",
  "background_rgba",
  "content",
  "fill_rgba",
  "font_asset_id",
  "line_height_bp",
  "size_px",
  "style",
  "weight",
];

/** comfyui_h3_context/core/composition_contract.py :: TimingLandmark */
export const timingLandmarkKeys: readonly string[] = [
  "dts",
  "duration_ticks",
  "frame_index",
  "pts",
];

/** comfyui_h3_context/core/transaction_transparency.py :: TransactionTransparencyProjection */
export const transactionTransparencyProjectionKeys: readonly string[] = [
  "actions",
  "correlation",
  "decisions",
  "guidance",
  "mandatory_segment_ids",
  "missing_required_segment_ids",
  "recompute_plan_fingerprint",
  "requested_segment_ids",
  "requires_full_recompute",
  "schema",
  "selection_safe",
  "transaction",
  "workspace_fingerprint",
  "workspace_id",
  "workspace_revision",
];

/** comfyui_h3_context/core/transaction_transparency.py :: TransactionTransparencySnapshot */
export const transactionTransparencySnapshotKeys: readonly string[] = [
  "attempt",
  "cancellation_requested",
  "compiled_prompt_fingerprint",
  "graph_fingerprint",
  "host_owner_id",
  "queue_prompt_id",
  "result_fingerprint",
  "state",
  "transaction_fingerprint",
  "transaction_id",
];

/** comfyui_h3_context/core/composition_contract.py :: Transform2D */
export const transform2DKeys: readonly string[] = [
  "anchor_x_bp",
  "anchor_y_bp",
  "position_x_bp",
  "position_y_bp",
  "rotation_mdeg",
  "scale_x_bp",
  "scale_y_bp",
];

/** comfyui_h3_context/core/composition_contract.py :: Transition */
export const transitionKeys: readonly string[] = ["duration_frames", "kind"];

/** comfyui_h3_context/core/provider_settings.py :: TransmissionDisclosure */
export const transmissionDisclosureKeys: readonly string[] = [
  "accepted_media",
  "consent_required",
  "consent_scope",
  "destination",
  "family",
  "local_only",
  "max_cost_micro_usd",
  "max_input_tokens",
  "max_output_tokens",
  "preflight_required",
  "price_basis_id",
  "price_valid_through",
  "provider_id",
  "requires_credential",
  "retention_policy",
  "transfer_boundary",
  "transmits_media",
];
