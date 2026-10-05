import type { SidebarWorkspaceProjection } from "../contracts/sidebarWorkspaceCodec";

export type SidebarWorkspaceActionName =
  "stage_prompt" | "import_prompt" | "validate" | "export";

export type SidebarWorkspaceState =
  | { status: "awaiting" }
  | { status: "ready"; projection: SidebarWorkspaceProjection }
  | {
      status: "editing";
      projection: SidebarWorkspaceProjection;
      promptText: string;
    }
  | {
      status: "loading";
      projection: SidebarWorkspaceProjection;
      action: SidebarWorkspaceActionName;
    }
  | {
      status: "error";
      reason: "request_failed" | "stale_response" | "incompatible_response";
      projection?: SidebarWorkspaceProjection;
    };

export type SidebarWorkspaceStateAction =
  | { type: "received"; projection: SidebarWorkspaceProjection }
  | { type: "host_execution"; projection: SidebarWorkspaceProjection }
  | { type: "edit"; promptText: string }
  | { type: "request"; action: SidebarWorkspaceActionName }
  | { type: "failed" }
  | { type: "reset" };

export const initialWorkspaceState: SidebarWorkspaceState = {
  status: "awaiting",
};

function projectionOf(
  state: SidebarWorkspaceState,
): SidebarWorkspaceProjection | undefined {
  return state.status === "awaiting" ? undefined : state.projection;
}

export function reduceWorkspaceState(
  state: SidebarWorkspaceState,
  action: SidebarWorkspaceStateAction,
): SidebarWorkspaceState {
  if (action.type === "reset") return initialWorkspaceState;
  if (action.type === "host_execution")
    return { status: "ready", projection: action.projection };
  if (action.type === "failed") {
    const projection = projectionOf(state);
    return projection === undefined
      ? { status: "error", reason: "request_failed" }
      : { status: "error", reason: "request_failed", projection };
  }
  if (action.type === "received") {
    const current = projectionOf(state);
    if (
      current !== undefined &&
      (action.projection.workspace_id !== current.workspace_id ||
        action.projection.correlation.prompt_id !==
          current.correlation.prompt_id ||
        action.projection.correlation.execution_node_id !==
          current.correlation.execution_node_id)
    ) {
      return {
        status: "error",
        reason: "incompatible_response",
        projection: current,
      };
    }
    if (
      state.status === "loading" &&
      action.projection.report_revision < state.projection.report_revision
    ) {
      return {
        status: "error",
        reason: "stale_response",
        projection: state.projection,
      };
    }
    return { status: "ready", projection: action.projection };
  }
  if (action.type === "edit") {
    const projection = projectionOf(state);
    if (projection === undefined || state.status === "loading") return state;
    return {
      status: "editing",
      projection,
      promptText: action.promptText,
    };
  }
  const projection = projectionOf(state);
  if (projection === undefined || state.status === "loading") return state;
  return { status: "loading", projection, action: action.action };
}
