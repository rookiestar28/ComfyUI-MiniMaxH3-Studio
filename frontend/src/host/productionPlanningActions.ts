import {
  decodeProductionPlanningResponse,
  encodeProductionPlanningAction,
  PRODUCTION_AUTOMATIC_PLAN_SCHEMA,
  PRODUCTION_PLANNING_PROJECTION_SCHEMA,
  type PlanningAction,
  type PlanningResponse,
} from "../contracts/productionPlanningCodec";

export const PRODUCTION_PLANNING_ROUTE =
  "/h3-context/v1/production/planning/action";

export class ProductionPlanningClientError extends Error {
  constructor(readonly status: number) {
    super("production planning action refused");
    this.name = "ProductionPlanningClientError";
  }
}

export function createProductionPlanningClient({
  fetchApi,
}: {
  fetchApi(
    path: string,
    init: RequestInit,
  ): Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;
}) {
  if (typeof fetchApi !== "function")
    throw new Error("same-origin planning transport is unavailable");
  return {
    async send(
      requestId: string,
      action: PlanningAction,
      payload: Record<string, unknown>,
      signal?: AbortSignal,
    ): Promise<PlanningResponse> {
      const request = encodeProductionPlanningAction(
        requestId,
        action,
        payload,
      );
      const response = await fetchApi(PRODUCTION_PLANNING_ROUTE, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(request),
        signal,
      });
      if (!response.ok || response.status !== 200)
        throw new ProductionPlanningClientError(response.status);
      const result = decodeProductionPlanningResponse(await response.json());
      const expectedSchema =
        action === "import_plan"
          ? PRODUCTION_AUTOMATIC_PLAN_SCHEMA
          : PRODUCTION_PLANNING_PROJECTION_SCHEMA;
      if (
        result.schema !== expectedSchema ||
        result.request_id !== requestId ||
        result.workspace_revision !==
          (payload.expected_workspace_revision as number) +
            (action === "import_plan" ? 1 : 0)
      )
        throw new ProductionPlanningClientError(500);
      if (result.schema === PRODUCTION_PLANNING_PROJECTION_SCHEMA) {
        if (
          result.workspace_handle !== payload.workspace_handle ||
          result.workspace_fingerprint !==
            payload.expected_workspace_fingerprint ||
          result.planning_revision !==
            (payload.expected_planning_revision as number) +
              (action === "read_plan" ? 0 : 1) ||
          (action === "prepare_context" &&
            (result.target_seconds !== payload.target_seconds ||
              result.policy !== payload.policy)) ||
          (action !== "prepare_context" &&
            result.planning_context_id !== payload.planning_context_id)
        )
          throw new ProductionPlanningClientError(500);
      } else if (result.proposal_id !== payload.proposal_id)
        throw new ProductionPlanningClientError(500);
      return result;
    },
  };
}
