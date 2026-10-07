export function unavailableProductionAssemblyWire() {
  return {
    schema: "h3.context.production_assembly.projection.v1",
    state: "unavailable",
    progress: { completed: 0, total: 0 },
    capability_fingerprint: null,
    managed_sequence_fingerprint: null,
    artifact_receipt_fingerprints: [],
    cut_boundary_receipt_fingerprints: [],
    output_profile_id: "legacy_av_30fps_48khz_stereo",
    assembly_job_id: null,
    authorization_fingerprint: null,
    receipt_fingerprint: null,
    failure_code: "media_runtime_not_authorized",
    projection_fingerprint: `sha256:${"0".repeat(64)}`,
  };
}
