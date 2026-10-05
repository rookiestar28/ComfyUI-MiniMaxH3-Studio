import type { ProductionWorkbenchProjection } from "../contracts/productionWorkbenchCodec";

export type ManagedProductionContextAuthority = Readonly<{
  contextWorkspaceId: string;
  taskMode: ProductionWorkbenchProjection["segments"][number]["taskMode"];
  effectiveDurationMilliseconds: number;
  frameCount: number;
}>;

export type ManagedProductionMemberAuthority = Readonly<{
  workspaceHandle: string;
  workspaceId: string;
  memberSegmentId: string;
  taskMode: ProductionWorkbenchProjection["segments"][number]["taskMode"];
  effectiveDurationMilliseconds: number;
  frameCount: number;
}>;

export function matchesCreatedProductionContext(
  production: ProductionWorkbenchProjection,
  authority: ManagedProductionContextAuthority,
): boolean {
  const segment = production.segments[0];
  // CRITICAL: Context `ws_...` and Production `workspace_...` are separate backend
  // namespaces. Bind them through the claimed seed fields, never identifier equality.
  // IMPORTANT: Production owns the frame-lattice duration. Comparing it to the
  // authored request rejects valid snapped subjects such as 5000 ms -> 5167 ms.
  return (
    production.workspaceId !== authority.contextWorkspaceId &&
    production.segments.length === 1 &&
    segment !== undefined &&
    production.selectedSegmentIds.length === 1 &&
    production.selectedSegmentIds[0] === segment.segmentId &&
    segment.relation === "independent" &&
    segment.predecessorSegmentId === null &&
    segment.taskMode === authority.taskMode &&
    segment.duration.requestedMilliseconds ===
      authority.effectiveDurationMilliseconds &&
    segment.duration.frameCount === authority.frameCount
  );
}

export function matchesAdmittedMemberProduction(
  production: ProductionWorkbenchProjection,
  authority: ManagedProductionMemberAuthority,
): boolean {
  const segment = production.segments.find(
    (candidate) => candidate.segmentId === authority.memberSegmentId,
  );
  // CRITICAL: the compact member response is not a project projection. Join it only through
  // the admitted project identity and the exact planned member returned by read_projection.
  return (
    production.workspaceHandle === authority.workspaceHandle &&
    production.workspaceId === authority.workspaceId &&
    segment !== undefined &&
    production.selectedSegmentIds.includes(segment.segmentId) &&
    segment.jobState === "planned" &&
    segment.taskMode === authority.taskMode &&
    segment.duration.requestedMilliseconds ===
      authority.effectiveDurationMilliseconds &&
    segment.duration.frameCount === authority.frameCount
  );
}
