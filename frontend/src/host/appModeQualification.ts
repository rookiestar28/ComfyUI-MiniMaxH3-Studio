// Read-only host qualification shared by canvas preparation and explicit generation.
import type {
  AppModeApp,
  AppModeApi,
  AppModeCapability,
  AppModeDetachedGraphFactory,
} from "./appModeContract";
import {
  probeLoadApiJson,
  probeGraphToPrompt,
  probeGraphConstructor,
} from "./hostSeams";
import { probeQueueCallable } from "./queueSeam";
import { probeGraphWriter, probeWorkflowStore } from "./canvasOwnedWrite";

export function qualifyAppMode(
  app: AppModeApp,
  api: AppModeApi,
  createDetachedGraph?: AppModeDetachedGraphFactory,
): AppModeCapability {
  if (probeLoadApiJson(app).status !== "ready")
    return { status: "unavailable", reason: "missing_load_api_json" };
  if (probeGraphToPrompt(app).status !== "ready")
    return { status: "unavailable", reason: "missing_graph_to_prompt" };
  if (probeQueueCallable(api).status !== "ready")
    return { status: "unavailable", reason: "missing_queue_prompt" };
  // M17-20 D11 option (b): materialization loads a spliced template workflow
  // rather than expanding it into an API prompt, so the graph-load seam is now a
  // capability rather than only the rollback path. A host without it is
  // MANUAL_ONLY_SCOPED, which is an honest unavailable state -- the alternative
  // would be silently falling back to the truncated graph this item replaces.
  if (probeGraphWriter(app).status !== "ready")
    return { status: "unavailable", reason: "missing_load_graph_data" };
  if (probeWorkflowStore(app).status !== "ready")
    return { status: "unavailable", reason: "missing_workflow_store" };
  if (
    typeof createDetachedGraph !== "function" &&
    probeGraphConstructor(app).status !== "ready"
  )
    return {
      status: "unavailable",
      reason: "missing_detached_graph_constructor",
    };
  return { status: "ready" };
}
