// Route and terminal policies contain no host writes or queue submission.
import type {
  AppModeStartOptions,
  AppModeStartResult,
} from "../host/appModeContract";
import type { AppModeDecision } from "./appModeMachine";
import type { ShellRuntime } from "./shellSession";

export function routeDecision(
  route: "new" | "existing" | "replace" | "connect",
): AppModeDecision {
  if (route === "new") return "empty";
  if (route === "replace") return "dirty";
  return "existing";
}

export function requestedAppModeRoute(
  options?: AppModeStartOptions,
): AppModeStartResult["route"] {
  if (options?.connectExisting !== undefined) return "connect";
  if (options?.useExisting === true) return "existing";
  if (options?.replaceExisting === true) return "replace";
  return "new";
}

export function workingRouteRetainsExistingGraph(
  route: AppModeStartResult["route"],
): boolean {
  return route === "existing" || route === "connect";
}

export function createAppModeLifecycleTransitions({ deps }: ShellRuntime) {
  function completeAppModeLifecycle(run: number): void {
    if (!deps.appModeLifecycle.isCurrent(run)) return;
    if (deps.appModeLifecycle.getSnapshot().value === "queued")
      deps.appModeLifecycle.send({ type: "EXECUTION_STARTED" });
    if (deps.appModeLifecycle.getSnapshot().value === "running")
      deps.appModeLifecycle.send({ type: "EXECUTION_FINISHED" });
    if (deps.appModeLifecycle.getSnapshot().value === "closing")
      deps.appModeLifecycle.send({ type: "OUTPUT_VERIFIED" });
    if (!deps.appModeLifecycle.complete(run))
      deps.appModeLifecycle.invalidate();
  }

  function failAppModeLifecycle(
    run: number,
    code: string,
    recovery: "retry" | "retry_output_verification" | "inspect" | "use_native",
  ): void {
    if (!deps.appModeLifecycle.isCurrent(run)) return;
    deps.appModeLifecycle.send({ type: "FAILED", code, recovery });
    deps.appModeLifecycle.invalidate();
  }

  // IMPORTANT: qualify lazily on render/start. Eager qualification sees no root graph
  // on the supplied host and incorrectly disables controls during module evaluation.
  return {
    completeAppModeLifecycle,
    failAppModeLifecycle,
    currentAppModeCapability: () => deps.appModeController.capability(),
    currentAppModeRun: () => deps.appModeLifecycle.getRunSequence(),
    isCurrentAppModeRun: (run: number) => deps.appModeLifecycle.isCurrent(run),
  };
}
