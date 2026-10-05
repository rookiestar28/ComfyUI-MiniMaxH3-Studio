import type {
  SemanticProposalActionResult,
  SemanticProposalReviewHandle,
  SemanticProposalReviewProjection,
} from "../contracts/semanticProposalReviewCodec";

export type SemanticProposalMutation =
  | "proposal_resolve"
  | "proposal_accept"
  | "proposal_reject"
  | "proposal_cancel";

export type SemanticProposalReviewState =
  | { status: "unavailable" }
  | { status: "closed"; handle: SemanticProposalReviewHandle }
  | { status: "loading"; handle: SemanticProposalReviewHandle }
  | {
      status: "ready";
      handle: SemanticProposalReviewHandle;
      projection: SemanticProposalReviewProjection;
    }
  | {
      status: "mutating";
      handle: SemanticProposalReviewHandle;
      projection: SemanticProposalReviewProjection;
      action: SemanticProposalMutation;
    }
  | {
      status: "error";
      handle: SemanticProposalReviewHandle;
      reason: "request_failed" | "stale_response" | "incompatible_response";
    };

export const initialSemanticProposalReviewState: SemanticProposalReviewState = {
  status: "unavailable",
};

function sameHandle(
  left: SemanticProposalReviewHandle,
  right: SemanticProposalReviewHandle,
): boolean {
  return (
    left.review_id === right.review_id &&
    left.transaction_fingerprint === right.transaction_fingerprint &&
    left.workspace_fingerprint === right.workspace_fingerprint &&
    left.report_fingerprint === right.report_fingerprint &&
    left.correlation.prompt_id === right.correlation.prompt_id &&
    left.correlation.execution_node_id === right.correlation.execution_node_id
  );
}

function projectionOf(
  state: SemanticProposalReviewState,
): SemanticProposalReviewProjection | undefined {
  return state.status === "ready" || state.status === "mutating"
    ? state.projection
    : undefined;
}

function currentHandle(
  state: Exclude<SemanticProposalReviewState, { status: "unavailable" }>,
): SemanticProposalReviewHandle {
  const projection = projectionOf(state);
  return projection === undefined
    ? state.handle
    : {
        ...state.handle,
        transaction_fingerprint: projection.transaction_fingerprint,
        workspace_fingerprint: projection.workspace_fingerprint,
      };
}

export type SemanticProposalReviewStateAction =
  | { type: "host"; handle?: SemanticProposalReviewHandle }
  | { type: "open" }
  | { type: "close" }
  | { type: "request"; action: SemanticProposalMutation }
  | { type: "received"; result: SemanticProposalActionResult }
  | { type: "failed" }
  | { type: "reset" };

export function reduceSemanticProposalReviewState(
  state: SemanticProposalReviewState,
  action: SemanticProposalReviewStateAction,
): SemanticProposalReviewState {
  if (action.type === "reset") return initialSemanticProposalReviewState;
  if (action.type === "host") {
    if (action.handle === undefined) return initialSemanticProposalReviewState;
    if (
      state.status !== "unavailable" &&
      sameHandle(state.handle, action.handle)
    )
      return state;
    return { status: "closed", handle: action.handle };
  }
  if (state.status === "unavailable") return state;
  if (action.type === "close")
    return { status: "closed", handle: currentHandle(state) };
  if (action.type === "open")
    return state.status === "closed" ||
      state.status === "error" ||
      state.status === "ready"
      ? { status: "loading", handle: currentHandle(state) }
      : state;
  if (action.type === "request") {
    if (state.status !== "ready" || !state.projection.actions[action.action])
      return state;
    return {
      status: "mutating",
      handle: state.handle,
      projection: state.projection,
      action: action.action,
    };
  }
  if (action.type === "failed")
    return {
      status: "error",
      handle: currentHandle(state),
      reason: "request_failed",
    };
  if (state.status !== "loading" && state.status !== "mutating") return state;
  const next = action.result.review;
  const current = projectionOf(state);
  const compatible =
    next.review_id === state.handle.review_id &&
    next.report_fingerprint === state.handle.report_fingerprint &&
    next.correlation.prompt_id === state.handle.correlation.prompt_id &&
    next.correlation.execution_node_id ===
      state.handle.correlation.execution_node_id &&
    next.attempt >= 1 &&
    (current === undefined ||
      (next.workspace_id === current.workspace_id &&
        next.attempt === current.attempt &&
        next.revision >= current.revision &&
        next.workspace_revision >= current.workspace_revision));
  if (!compatible)
    return {
      status: "error",
      handle: currentHandle(state),
      reason: "incompatible_response",
    };
  if (
    current === undefined &&
    (next.transaction_fingerprint !== state.handle.transaction_fingerprint ||
      next.workspace_fingerprint !== state.handle.workspace_fingerprint)
  )
    return { status: "error", handle: state.handle, reason: "stale_response" };
  return { status: "ready", handle: state.handle, projection: next };
}
