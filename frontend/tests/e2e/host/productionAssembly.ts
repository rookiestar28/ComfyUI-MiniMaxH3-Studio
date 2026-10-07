export const qualificationFingerprint = `sha256:${"a".repeat(64)}`;

export type CandidateProductionProjection = Readonly<{
  workspaceHandle: string;
  workspaceId: string;
  workspaceRevision: number;
  workspaceFingerprint: string;
  selectedSegmentIds: readonly string[];
  allowedActions: readonly string[];
  authorityVersions: readonly unknown[];
  outputs: readonly unknown[];
  assembly: Readonly<{
    state: string;
    capabilityFingerprint: string | null;
    managedSequenceFingerprint: string | null;
    artifactReceiptFingerprints: readonly string[];
    cutBoundaryReceiptFingerprints: readonly string[];
    outputProfileId: string;
    assemblyJobId: string | null;
    authorizationFingerprint: string | null;
    receiptFingerprint: string | null;
    failureCode: string | null;
    projectionFingerprint: string;
  }>;
}>;

export function unavailableAssemblyFacts(
  projection: CandidateProductionProjection,
) {
  return {
    state: projection.assembly.state,
    capabilityFingerprint: projection.assembly.capabilityFingerprint,
    managedSequenceFingerprint: projection.assembly.managedSequenceFingerprint,
    artifactReceiptCount:
      projection.assembly.artifactReceiptFingerprints.length,
    cutBoundaryReceiptCount:
      projection.assembly.cutBoundaryReceiptFingerprints.length,
    assemblyJobId: projection.assembly.assemblyJobId,
    authorizationFingerprint: projection.assembly.authorizationFingerprint,
    receiptFingerprint: projection.assembly.receiptFingerprint,
    failureCode: projection.assembly.failureCode,
    projectionFingerprint: projection.assembly.projectionFingerprint,
    assemblyAdvertised: projection.allowedActions.includes("assemble_sequence"),
  };
}

export function rawAssemblyPayload(
  projection: CandidateProductionProjection,
): Record<string, unknown> {
  return {
    workspace_handle: projection.workspaceHandle,
    workspace_id: projection.workspaceId,
    expected_workspace_revision: projection.workspaceRevision,
    expected_workspace_fingerprint: projection.workspaceFingerprint,
    managed_sequence_fingerprint: qualificationFingerprint,
    artifact_receipt_fingerprints: [qualificationFingerprint],
    cut_boundary_receipt_fingerprints: [],
    assembly_capability_fingerprint: qualificationFingerprint,
    output_profile_id: projection.assembly.outputProfileId,
  };
}
