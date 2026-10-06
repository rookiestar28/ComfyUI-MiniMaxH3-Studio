import {
  decodeSidebarWorkspaceProjection,
  type SidebarWorkspaceProjection,
} from "../contracts/sidebarWorkspaceCodec";
import type { WorkspaceActionRequest } from "../components/SidebarStages";
import type { SemanticProposalReviewRequest } from "../components/SemanticProposalReview";
import {
  decodeSemanticProposalActionResult,
  type SemanticProposalActionResult,
  type SemanticProposalReviewHandle,
  type SemanticProposalReviewProjection,
} from "../contracts/semanticProposalReviewCodec";
import {
  decodeAssistedPromptProposal,
  decodeAssistedSidebarResult,
  validateAssistedActionRequest,
  type AssistedActionRequest,
  type AssistedPromptProposalProjection,
  type AssistedSidebarResult,
} from "../contracts/assistedPromptProposalCodec";
import {
  isProviderSessionHandle,
  PROVIDER_SESSION_HEADER,
} from "./providerSettingsActions";

const route = "/h3-context/v1/sidebar/action";
const assistedRoute = "/h3-context/v1/sidebar/assisted";
const fingerprint = /^sha256:[0-9a-f]{64}$/;
const transferKeys = [
  "schema",
  "report_id",
  "report_revision",
  "report_fingerprint",
  "prompt_fingerprint",
  "task_mode",
  "profile",
  "prompt_text",
] as const;

export type SemanticProposalActionIdentity = Readonly<{
  workspace_id: string;
  report_revision: number;
  report_fingerprint: string;
}>;

export type SidebarTransfer = {
  schema: "h3.context.sidebar.transfer.v1";
  report_id: string;
  report_revision: number;
  report_fingerprint: string;
  prompt_fingerprint: string;
  task_mode: string;
  profile: string;
  prompt_text: string;
};

type FetchResponse = {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
};

function record(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error("sidebar response must be an object");
  return value as Record<string, unknown>;
}

function decodeTransfer(value: unknown): SidebarTransfer {
  const wire = record(value);
  const actual = Object.keys(wire);
  if (
    actual.some((key) => !transferKeys.includes(key as never)) ||
    transferKeys.some((key) => !Object.hasOwn(wire, key)) ||
    wire.schema !== "h3.context.sidebar.transfer.v1" ||
    typeof wire.report_id !== "string" ||
    !Number.isInteger(wire.report_revision) ||
    typeof wire.report_fingerprint !== "string" ||
    !fingerprint.test(wire.report_fingerprint) ||
    typeof wire.prompt_fingerprint !== "string" ||
    !fingerprint.test(wire.prompt_fingerprint) ||
    typeof wire.task_mode !== "string" ||
    typeof wire.profile !== "string" ||
    typeof wire.prompt_text !== "string" ||
    wire.prompt_text.length > 65_536
  )
    throw new Error("sidebar transfer response is incompatible");
  return wire as SidebarTransfer;
}

export function createSidebarActionClient({
  fetchApi,
  providerSessionHandle,
}: {
  fetchApi(path: string, init: RequestInit): Promise<FetchResponse>;
  providerSessionHandle?(): string | undefined;
}) {
  if (typeof fetchApi !== "function")
    throw new Error("supported same-origin action seam is absent");
  return {
    async send(
      projection: SidebarWorkspaceProjection,
      request: WorkspaceActionRequest,
      signal?: AbortSignal,
    ): Promise<
      | { kind: "workspace"; projection: SidebarWorkspaceProjection }
      | { kind: "transfer"; transfer: SidebarTransfer }
    > {
      const response = await fetchApi(route, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          schema: "h3.context.sidebar.action.v2",
          workspace_id: projection.workspace_id,
          expected_revision: projection.report_revision,
          expected_report_fingerprint: projection.report_fingerprint,
          action: request.action,
          payload: request.payload,
        }),
        signal,
      });
      if (!response.ok)
        throw new Error(`sidebar action failed with status ${response.status}`);
      const value = await response.json();
      const wire = record(value);
      if (wire.schema === "h3.context.sidebar.workspace.v2") {
        const next = decodeSidebarWorkspaceProjection(wire);
        if (
          next.workspace_id !== projection.workspace_id ||
          next.correlation.prompt_id !== projection.correlation.prompt_id ||
          next.correlation.execution_node_id !==
            projection.correlation.execution_node_id ||
          next.report_revision < projection.report_revision
        )
          throw new Error(
            "sidebar action response is stale or cross-workspace",
          );
        return { kind: "workspace", projection: next };
      }
      return { kind: "transfer", transfer: decodeTransfer(wire) };
    },
    async sendAssisted(
      projection: SidebarWorkspaceProjection,
      request: AssistedActionRequest,
      signal?: AbortSignal,
    ): Promise<
      | { kind: "workspace"; projection: SidebarWorkspaceProjection }
      | {
          kind: "assisted";
          result: AssistedSidebarResult;
          proposal: AssistedPromptProposalProjection | null;
        }
    > {
      validateAssistedActionRequest(request);
      const session = providerSessionHandle?.();
      if (!isProviderSessionHandle(session))
        throw new Error("provider session is unavailable");
      const response = await fetchApi(assistedRoute, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "content-type": "application/json",
          [PROVIDER_SESSION_HEADER]: session,
        },
        body: JSON.stringify({
          schema: "h3.context.sidebar.action.v2",
          workspace_id: projection.workspace_id,
          expected_revision: projection.report_revision,
          expected_report_fingerprint: projection.report_fingerprint,
          action: request.action,
          payload: request.payload,
        }),
        signal,
      });
      if (!response.ok)
        throw new Error(
          `assisted sidebar action failed with status ${response.status}`,
        );
      const value = await response.json();
      const wire = record(value);
      if (wire.schema === "h3.context.sidebar.workspace.v2") {
        const next = decodeSidebarWorkspaceProjection(wire);
        if (
          next.workspace_id !== projection.workspace_id ||
          next.correlation.prompt_id !== projection.correlation.prompt_id ||
          next.correlation.execution_node_id !==
            projection.correlation.execution_node_id ||
          next.report_revision <= projection.report_revision
        )
          throw new Error("assisted response is stale or cross-workspace");
        return { kind: "workspace", projection: next };
      }
      if (wire.schema === "h3.context.assisted_prompt_proposal.v1") {
        const proposal = decodeAssistedPromptProposal(wire);
        if (
          proposal.workspace_id !== projection.workspace_id ||
          proposal.report_revision !== projection.report_revision ||
          proposal.report_fingerprint !== projection.report_fingerprint
        )
          throw new Error("assisted proposal is stale or cross-workspace");
        return {
          kind: "assisted",
          result: {
            schema: "h3.context.assisted_sidebar_result.v1",
            state:
              proposal.state === "rejected"
                ? "failed"
                : proposal.state === "cancelled"
                  ? "cancelled"
                  : "proposal",
            proposal: proposal.state === "active" ? proposal : null,
          },
          proposal,
        };
      }
      const result = decodeAssistedSidebarResult(wire);
      if (
        result.proposal !== null &&
        (result.proposal.workspace_id !== projection.workspace_id ||
          result.proposal.report_revision !== projection.report_revision ||
          result.proposal.report_fingerprint !== projection.report_fingerprint)
      )
        throw new Error("assisted proposal is stale or cross-workspace");
      return { kind: "assisted", result, proposal: result.proposal };
    },
    async sendProposal(
      sidebar: SemanticProposalActionIdentity,
      handle: SemanticProposalReviewHandle,
      current: SemanticProposalReviewProjection | undefined,
      request: { action: "proposal_read" } | SemanticProposalReviewRequest,
      signal?: AbortSignal,
    ): Promise<SemanticProposalActionResult> {
      const transactionFingerprint =
        current?.transaction_fingerprint ?? handle.transaction_fingerprint;
      const workspaceFingerprint =
        current?.workspace_fingerprint ?? handle.workspace_fingerprint;
      const payload: Record<string, unknown> = {
        review_id: handle.review_id,
        expected_transaction_fingerprint: transactionFingerprint,
        expected_workspace_fingerprint: workspaceFingerprint,
      };
      if (request.action === "proposal_resolve")
        payload.resolutions = request.resolutions;
      const response = await fetchApi(route, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          schema: "h3.context.sidebar.action.v2",
          workspace_id: sidebar.workspace_id,
          expected_revision: sidebar.report_revision,
          expected_report_fingerprint: sidebar.report_fingerprint,
          action: request.action,
          payload,
        }),
        signal,
      });
      if (!response.ok)
        throw new Error(
          `semantic proposal action failed with status ${response.status}`,
        );
      const result = decodeSemanticProposalActionResult(await response.json());
      const expectedOutcome = {
        proposal_read: "read",
        proposal_resolve: "resolved",
        proposal_accept: "accepted",
        proposal_reject: "rejected",
        proposal_cancel: "cancelled",
      }[request.action];
      if (
        result.outcome !== expectedOutcome ||
        result.review.review_id !== handle.review_id ||
        result.review.report_fingerprint !== handle.report_fingerprint ||
        result.review.correlation.prompt_id !== handle.correlation.prompt_id ||
        result.review.correlation.execution_node_id !==
          handle.correlation.execution_node_id ||
        (current === undefined &&
          (result.review.transaction_fingerprint !==
            handle.transaction_fingerprint ||
            result.review.workspace_fingerprint !==
              handle.workspace_fingerprint)) ||
        (current !== undefined &&
          (result.review.workspace_id !== current.workspace_id ||
            result.review.attempt !== current.attempt ||
            result.review.revision < current.revision ||
            result.review.workspace_revision < current.workspace_revision))
      )
        throw new Error("semantic proposal response is stale or cross-review");
      return result;
    },
  };
}
