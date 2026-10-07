import { SEQUENCE_COORDINATOR_ROUTE } from "../../../src/host/sequenceCoordinator";

export type M2508PostRouteClass =
  "queue" | "provider" | "generation" | "coordinator" | "other";

export type M2508CoordinatorActionClass =
  "prepare_managed_run" | "submit_managed_run" | "unexpected";

export function normalizeM2508HostApiPath(path: string): string {
  // IMPORTANT: ComfyUI 1.51.9 `fetchApi` adds exactly one `/api` transport prefix. Compare the
  // logical route after removing that prefix or coordinator and `/prompt` evidence is misbucketed.
  return path.startsWith("/api/") ? path.slice(4) : path;
}

export function classifyM2508PostRoute(path: string): M2508PostRouteClass {
  const logicalPath = normalizeM2508HostApiPath(path);
  if (
    logicalPath === "/prompt" ||
    logicalPath === "/queue" ||
    logicalPath.startsWith("/prompt/")
  )
    return "queue";
  if (logicalPath.includes("/provider/")) return "provider";
  // IMPORTANT: this exact authority/control-plane route contains `/generation/` but does not run
  // a model. Keep it before the catch-all or valid managed queues become false generation failures.
  if (logicalPath === SEQUENCE_COORDINATOR_ROUTE) return "coordinator";
  if (logicalPath.includes("/generation/")) return "generation";
  return "other";
}

export function classifyM2508CoordinatorAction(
  body: unknown,
): M2508CoordinatorActionClass {
  if (body === null || typeof body !== "object" || Array.isArray(body))
    return "unexpected";
  const action = (body as { action?: unknown }).action;
  return action === "prepare_managed_run" || action === "submit_managed_run"
    ? action
    : "unexpected";
}
