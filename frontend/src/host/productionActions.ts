import {
  decodeProductionWorkbenchProjection,
  encodeProductionAction,
  encodeProductionDestinationAction,
  type ProductionAction,
  type ProductionActionInput,
  type ProductionDestinationAction,
  type ProductionDestinationInput,
  type ProductionWorkbenchProjection,
} from "../contracts/productionWorkbenchCodec";
import {
  decodeProductionAccumulatedProject,
  PRODUCTION_ACCUMULATION_ACTION_VERSION,
  type ProductionAccumulatedProject,
} from "../contracts/productionAccumulationCodec";

const route = "/h3-context/v1/production/action";

type FetchResponse = {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
};

const statusCodes = {
  400: "invalid_action",
  403: "origin_rejected",
  404: "workspace_unavailable",
  410: "workspace_gone",
  413: "request_too_large",
  415: "media_type_rejected",
  422: "action_rejected",
  423: "workspace_busy",
  429: "workspace_capacity",
  500: "internal_failure",
} as const;

export class ProductionClientError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, status: number) {
    super(code);
    this.name = "ProductionClientError";
    this.code = code;
    this.status = status;
  }
}

export type ProductionClientResult = Readonly<{
  status: 200 | 201 | 204 | 409;
  projection?: ProductionWorkbenchProjection;
}>;

/** Why a Start's destination was refused, decided before any queue call. */
export type ProductionDestinationRefusal =
  | "destination_busy"
  | "destination_changed"
  | "destination_unavailable"
  | "destination_capacity"
  | "accumulation_unsupported"
  | "destination_failed";

export class ProductionDestinationError extends Error {
  readonly reason: ProductionDestinationRefusal;
  readonly status: number;
  readonly projection: ProductionAccumulatedProject | undefined;

  constructor(
    reason: ProductionDestinationRefusal,
    status: number,
    projection?: ProductionAccumulatedProject,
  ) {
    super(reason);
    this.name = "ProductionDestinationError";
    this.reason = reason;
    this.status = status;
    this.projection = projection;
  }
}

export type ProductionDestinationResult =
  | Readonly<{
      status: 200 | 201;
      project: ProductionAccumulatedProject;
    }>
  | Readonly<{ status: 204 }>;

function destinationRefusal(status: number): ProductionDestinationRefusal {
  if (status === 409) return "destination_changed";
  if (status === 404 || status === 410) return "destination_unavailable";
  if (status === 429) return "destination_capacity";
  // IMPORTANT: a backend older than this frontend does not know the admission actions and
  // answers 400. Starting anyway would silently create a separate project per Start.
  if (status === 400) return "accumulation_unsupported";
  return "destination_failed";
}

export function createProductionActionClient({
  fetchApi,
}: {
  fetchApi(path: string, init: RequestInit): Promise<FetchResponse>;
}) {
  if (typeof fetchApi !== "function")
    throw new Error("supported same-origin Production seam is absent");
  return {
    async sendDestination(
      requestId: string,
      action: ProductionDestinationAction,
      input: ProductionDestinationInput,
      signal?: AbortSignal,
    ): Promise<ProductionDestinationResult> {
      const request = encodeProductionDestinationAction(
        requestId,
        action,
        input,
      );
      const response = await fetchApi(route, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(request),
        signal,
      });
      const target = "target" in input ? input.target : null;
      if (action === "release_generation_destination") {
        if (response.status === 204)
          return Object.freeze({ status: 204 as const });
        throw new ProductionDestinationError(
          destinationRefusal(response.status),
          response.status,
        );
      }
      if (
        response.status === 200 ||
        response.status === 201 ||
        response.status === 409
      ) {
        let body: unknown;
        try {
          body = await response.json();
        } catch {
          // Refusals other than busy carry no body.
          throw new ProductionDestinationError(
            destinationRefusal(response.status),
            response.status,
          );
        }
        const project = decodeProductionAccumulatedProject(body);
        // CRITICAL: an admission names one exact project. A projection for any other project is
        // a cross-authority answer and must never become the Start's destination.
        if (
          target !== null &&
          (project.workspaceHandle !== target.workspaceHandle ||
            project.workspaceId !== target.workspaceId)
        )
          throw new ProductionClientError("cross_workspace_response", 500);
        if (response.status === 409)
          throw new ProductionDestinationError(
            "destination_busy",
            409,
            project,
          );
        return Object.freeze({
          status: response.status as 200 | 201,
          project,
        });
      }
      throw new ProductionDestinationError(
        destinationRefusal(response.status),
        response.status,
      );
    },
    async readAccumulated(
      requestId: string,
      target: Readonly<{ workspaceHandle: string; workspaceId: string }>,
      signal?: AbortSignal,
    ): Promise<ProductionAccumulatedProject> {
      const response = await fetchApi(route, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          schema: "h3.context.production_workbench.action.v1",
          request_id: requestId,
          action: "read_accumulated_project",
          payload: {
            version: PRODUCTION_ACCUMULATION_ACTION_VERSION,
            workspace_handle: target.workspaceHandle,
            workspace_id: target.workspaceId,
          },
        }),
        signal,
      });
      if (!response.ok)
        throw new ProductionDestinationError(
          destinationRefusal(response.status),
          response.status,
        );
      const project = decodeProductionAccumulatedProject(await response.json());
      if (
        project.workspaceHandle !== target.workspaceHandle ||
        project.workspaceId !== target.workspaceId
      )
        throw new ProductionClientError("cross_workspace_response", 500);
      return project;
    },
    async send(
      requestId: string,
      action: ProductionAction,
      input: ProductionActionInput,
      signal?: AbortSignal,
    ): Promise<ProductionClientResult> {
      const request = encodeProductionAction(requestId, action, input);
      const response = await fetchApi(route, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(request),
        signal,
      });
      if (response.status === 204)
        return Object.freeze({ status: 204 as const });
      if (response.ok || response.status === 409) {
        const projection = decodeProductionWorkbenchProjection(
          await response.json(),
        );
        if (
          input.projection !== undefined &&
          (projection.workspaceHandle !== input.projection.workspaceHandle ||
            projection.workspaceRevision < input.projection.workspaceRevision)
        )
          throw new ProductionClientError("cross_workspace_response", 500);
        if (![200, 201, 409].includes(response.status))
          throw new ProductionClientError("unexpected_status", response.status);
        return Object.freeze({
          status: response.status as 200 | 201 | 409,
          projection,
        });
      }
      const code = statusCodes[response.status as keyof typeof statusCodes];
      throw new ProductionClientError(
        code ?? "internal_failure",
        response.status,
      );
    },
  };
}
