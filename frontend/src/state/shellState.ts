import type { ProductShellProjection } from "../contracts/projectionCodecs";
import type { AppModeRefusalReason } from "../host/appMode";
import type { GraphInspection } from "../host/graphAdapter";
import type { AppModeSnapshot } from "../lifecycle/appModeMachine";

/** The sidebar owns a small, reversible state machine rather than a terminal fallback. */
export type ShellInteractiveReason =
  | "empty_canvas"
  | "canvas_ready"
  | "pending_capability"
  | "native_preference"
  | "cancelled"
  | "dirty_graph"
  | "ambiguous_graph"
  | "incompatible_graph"
  | "malformed_graph"
  | "unavailable";

export type ShellErrorCode =
  | "incompatible_seam"
  | "compile_failed"
  | "queue_failed"
  | "execution_failed"
  | "execution_interrupted"
  | "projection_missing"
  | "stale_graph"
  | "projection_mismatch"
  | "ambiguous_host_ownership"
  | "artifact_verification_failed"
  | "artifact_content_invalid"
  | "artifact_locator_rejected"
  | "artifact_authority_mismatch"
  | "artifact_store_unavailable"
  | "run_authority_mismatch"
  | "unsupported_failure"
  | "internal_failure"
  | "rollback_failed";

export type ShellWorkingPhase =
  | "materializing"
  | "compiling"
  | "preparing_context"
  | "queueing"
  | "generating"
  | "verifying_output";

export type ShellState =
  | {
      status: "interactive";
      reason: ShellInteractiveReason;
      existingGraph?: boolean;
      inspection?: GraphInspection;
      message?: string;
      refusalReason?: AppModeRefusalReason;
    }
  | {
      status: "working";
      phase: ShellWorkingPhase;
      transactionId: number;
      existingGraph?: boolean;
      graphFingerprint?: string;
    }
  | {
      status: "projected";
      projection: ProductShellProjection;
      transactionId?: number;
      graphFingerprint?: string;
      /** One host rescan may remove model-free generation after this projection. */
      modelFreeRescanExpected?: boolean;
      /** Backend-owned prompt hash independently checked against workspace text. */
      promptFingerprint?: string;
    }
  | {
      status: "editing_setup";
      prior: {
        projection: ProductShellProjection;
        anchorExecutionId: string;
        transactionId?: number;
        graphFingerprint: string;
        modelFreeRescanExpected?: boolean;
        promptFingerprint?: string;
      };
    }
  | {
      status: "error";
      code: ShellErrorCode;
      severity: "error";
      source: string;
      message: string;
      recovery:
        "retry" | "retry_output_verification" | "inspect" | "use_native";
      existingGraph?: boolean;
      transactionId?: number;
      refusalReason?: AppModeRefusalReason;
    }
  | {
      /**
       * The host's event socket is down. This is not an App Mode error: nothing the repository
       * owns failed, a run may still be executing on the host, and the interrupted state is
       * retained verbatim so a reconnect can restore or reconcile it.
       */
      status: "host_unavailable";
      phase: HostUnavailablePhase;
      /** The kind of work the drop interrupted, so the surface can name it. */
      priorStatus: RestorableShellState["status"];
      prior: RestorableShellState;
    };

export type HostUnavailablePhase = "lost" | "reconnecting";

/** Every shell state except the interruption itself; the wrapper never nests. */
export type RestorableShellState = Exclude<
  ShellState,
  { status: "host_unavailable" }
>;

/** Whether the current shell state retains explicit authority over a visible existing graph. */
export function hasExistingGraphAuthority(state: ShellState): boolean {
  // A lost host suspends the interrupted state; it never revokes the route authority that state
  // held, or a reconnected existing/Connect run would resume as a materialize-only refusal.
  if (state.status === "host_unavailable")
    return hasExistingGraphAuthority(state.prior);
  // IMPORTANT: working must preserve this route bit; dropping it makes accepted
  // existing/Connect queues render materialize-only refusals while generating.
  if (
    state.status === "interactive" ||
    state.status === "working" ||
    state.status === "error"
  )
    return state.existingGraph === true;
  // IMPORTANT: setup navigation retains the accepted graph fingerprint even
  // though the interactive presentation state is no longer visible.
  return (
    state.status === "editing_setup" && state.prior.graphFingerprint.length > 0
  );
}

/** Whether the visible or retained accepted projection owns this exact host prompt. */
export function retainedProjectionOwnsPrompt(
  state: ShellState,
  promptId: string,
): boolean {
  // CRITICAL: a terminal for the owning prompt can arrive after the socket returns. Reading
  // through the interruption is what keeps that late event owned instead of foreign.
  const owner = state.status === "host_unavailable" ? state.prior : state;
  const projection =
    owner.status === "projected"
      ? owner.projection
      : owner.status === "editing_setup"
        ? owner.prior.projection
        : undefined;
  return projection?.correlation.prompt_id === promptId;
}

export type ShellAction =
  | {
      type: "graph";
      inspection: GraphInspection;
      appModeAvailable?: boolean;
    }
  | {
      type: "projection";
      anchorExecutionId: string;
      projection: ProductShellProjection;
      transactionId?: number;
      graphFingerprint?: string;
      modelFreeRescanExpected?: boolean;
      backendPromptFingerprint?: string;
    }
  | { type: "edit_setup" }
  | {
      type: "cancel_edit";
      /** Fresh graph identity sampled by the host boundary before restore. */
      graphFingerprint?: string;
      /** When available, use the normal graph classifier after drift. */
      inspection?: GraphInspection;
    }
  | {
      type: "working";
      phase: ShellState & { status: "working" } extends never
        ? never
        : ShellWorkingPhase;
      transactionId: number;
      existingGraph?: boolean;
      graphFingerprint?: string;
    }
  | {
      type: "interactive";
      reason: ShellInteractiveReason;
      existingGraph?: boolean;
      inspection?: GraphInspection;
      message?: string;
      refusalReason?: AppModeRefusalReason;
    }
  | {
      type: "error";
      code: ShellErrorCode;
      source: string;
      message: string;
      recovery:
        "retry" | "retry_output_verification" | "inspect" | "use_native";
      existingGraph?: boolean;
      transactionId?: number;
      refusalReason?: AppModeRefusalReason;
    }
  | { type: "host_unavailable"; phase: HostUnavailablePhase }
  | { type: "host_restored" }
  | { type: "reset" };

export const initialShellState: ShellState = {
  status: "interactive",
  reason: "pending_capability",
};

/** Project the lifecycle authority into the stable Sidebar presentation contract. */
export function projectAppModeSnapshot(
  snapshot: AppModeSnapshot,
  fallback: ShellState,
): ShellState {
  const working = (
    phase: ShellWorkingPhase,
    graphFingerprint?: string,
  ): ShellState => ({
    status: "working",
    phase,
    transactionId: snapshot.context.run,
    existingGraph: snapshot.context.existingGraph,
    ...(graphFingerprint === undefined ? {} : { graphFingerprint }),
  });
  if (
    snapshot.value === "census" ||
    snapshot.value === "deciding" ||
    snapshot.value === "validating"
  )
    return working(
      snapshot.context.route === "new" ? "materializing" : "compiling",
    );
  if (snapshot.value === "writing") return working("materializing");
  if (snapshot.value.startsWith("preparing."))
    return working("preparing_context");
  if (snapshot.value === "queued")
    return working("queueing", snapshot.context.ownedProjectionFingerprint);
  if (snapshot.value === "running")
    return working("generating", snapshot.context.ownedProjectionFingerprint);
  if (snapshot.value === "closing")
    return working(
      "verifying_output",
      snapshot.context.ownedProjectionFingerprint,
    );
  if (snapshot.value === "terminal.cancelled")
    return { status: "interactive", reason: "cancelled" };
  if (snapshot.value === "terminal.refused")
    return {
      status: "interactive",
      reason: "unavailable",
      existingGraph: snapshot.context.existingGraph,
      message: snapshot.context.refusal,
    };
  if (snapshot.value === "terminal.failed") {
    const failure = snapshot.context.failure;
    const code =
      failure !== undefined && shellErrorCodesForProjection.has(failure.code)
        ? (failure.code as ShellErrorCode)
        : "internal_failure";
    return {
      status: "error",
      severity: "error",
      code,
      source: "app_mode",
      message: failure?.code ?? "internal_failure",
      recovery: failure?.recovery ?? "retry",
      existingGraph: snapshot.context.existingGraph,
      transactionId: snapshot.context.run,
    };
  }
  // `done` retains the independently verified ProductShell projection, while idle and host
  // interruption keep the existing shell classification/wrapper. The lifecycle machine never
  // fabricates either payload.
  return fallback;
}

const shellErrorCodesForProjection = new Set<string>([
  "incompatible_seam",
  "compile_failed",
  "queue_failed",
  "execution_failed",
  "execution_interrupted",
  "projection_missing",
  "stale_graph",
  "projection_mismatch",
  "ambiguous_host_ownership",
  "artifact_verification_failed",
  "artifact_content_invalid",
  "artifact_locator_rejected",
  "artifact_authority_mismatch",
  "artifact_store_unavailable",
  "run_authority_mismatch",
  "unsupported_failure",
  "internal_failure",
  "rollback_failed",
]);

function inspectionReason(inspection: GraphInspection): ShellInteractiveReason {
  if (inspection.status === "missing")
    return (inspection.nodeCount ?? 0) === 0 ? "empty_canvas" : "dirty_graph";
  if (inspection.status === "ambiguous") return "ambiguous_graph";
  const reason = String(inspection.reason ?? "");
  return reason.includes("malformed") || reason.includes("serialized")
    ? "malformed_graph"
    : "incompatible_graph";
}

export function reduceShellState(
  state: ShellState,
  action: ShellAction,
): ShellState {
  if (action.type === "reset") return initialShellState;
  if (action.type === "host_unavailable") {
    // CRITICAL: the wrapper must never nest. A second drop only advances the phase, so the state
    // the user was actually in stays recoverable through any number of retry cycles.
    if (state.status === "host_unavailable")
      return state.phase === action.phase
        ? state
        : { ...state, phase: action.phase };
    return {
      status: "host_unavailable",
      phase: action.phase,
      priorStatus: state.status,
      prior: state,
    };
  }
  if (action.type === "host_restored")
    return state.status === "host_unavailable" ? state.prior : state;
  if (action.type === "edit_setup") {
    if (state.status !== "projected" || state.graphFingerprint === undefined)
      return state;
    return {
      status: "editing_setup",
      prior: {
        projection: state.projection,
        anchorExecutionId: state.projection.correlation.execution_node_id,
        transactionId: state.transactionId,
        graphFingerprint: state.graphFingerprint,
        modelFreeRescanExpected: state.modelFreeRescanExpected,
        promptFingerprint: state.promptFingerprint,
      },
    };
  }
  if (action.type === "cancel_edit") {
    if (state.status !== "editing_setup") return state;
    if (action.graphFingerprint === state.prior.graphFingerprint)
      return {
        status: "projected",
        projection: state.prior.projection,
        transactionId: state.prior.transactionId,
        graphFingerprint: state.prior.graphFingerprint,
        modelFreeRescanExpected: state.prior.modelFreeRescanExpected,
        promptFingerprint: state.prior.promptFingerprint,
      };
    if (action.inspection !== undefined)
      return reduceShellState(state, {
        type: "graph",
        inspection: action.inspection,
      });
    return {
      status: "interactive",
      reason: "incompatible_graph",
      existingGraph: true,
      message:
        "The visible graph changed while editing App Mode setup; review it before another run.",
    };
  }
  if (action.type === "interactive") {
    return {
      status: "interactive",
      reason: action.reason,
      existingGraph: action.existingGraph,
      inspection: action.inspection,
      message: action.message,
      refusalReason: action.refusalReason,
    };
  }
  if (action.type === "working") {
    return {
      status: "working",
      phase: action.phase,
      transactionId: action.transactionId,
      existingGraph: action.existingGraph,
      graphFingerprint: action.graphFingerprint,
    };
  }
  if (action.type === "error") {
    return {
      status: "error",
      severity: "error",
      code: action.code,
      source: action.source,
      message: action.message,
      recovery: action.recovery,
      existingGraph: action.existingGraph,
      transactionId: action.transactionId,
      refusalReason: action.refusalReason,
    };
  }
  if (action.type === "projection") {
    if (
      action.anchorExecutionId !==
      action.projection.correlation.execution_node_id
    ) {
      return {
        status: "error",
        severity: "error",
        code: "projection_mismatch",
        source: "projection",
        message:
          "The Product Shell projection did not match the visible graph.",
        recovery: "inspect",
        transactionId: action.transactionId,
      };
    }
    // CRITICAL: a projection that arrives while the interruption is visible must still be checked
    // against the transaction it claims to complete. Comparing against the wrapper instead of the
    // suspended run would silently skip the whole transaction guard.
    const authority = state.status === "host_unavailable" ? state.prior : state;
    if (authority.status === "working") {
      const transactionMatches =
        action.transactionId === authority.transactionId &&
        authority.graphFingerprint !== undefined &&
        action.graphFingerprint === authority.graphFingerprint &&
        action.backendPromptFingerprint ===
          action.projection.prompt_fingerprint;
      if (!transactionMatches) {
        return {
          status: "error",
          severity: "error",
          code: "projection_mismatch",
          source: "projection",
          message:
            "The Product Shell projection did not match the active App Mode run.",
          recovery: "inspect",
          transactionId: authority.transactionId,
        };
      }
    }
    return {
      status: "projected",
      projection: action.projection,
      transactionId: action.transactionId,
      graphFingerprint: action.graphFingerprint,
      modelFreeRescanExpected: action.modelFreeRescanExpected === true,
      promptFingerprint: action.backendPromptFingerprint,
    };
  }
  if (
    state.status === "projected" &&
    state.modelFreeRescanExpected === true &&
    state.transactionId !== undefined &&
    state.graphFingerprint !== undefined &&
    action.inspection.status === "missing" &&
    action.inspection.reason === "missing_native_h3_core" &&
    action.inspection.anchors[0]?.executionId ===
      state.projection.correlation.execution_node_id
  )
    // The model-free host lane may remove the weight-backed native generation
    // node after ProductShell execution. Keep the verified result visible, but
    // never mark that incomplete graph as an existing queue target.
    return { ...state, modelFreeRescanExpected: false };
  // Host rescans can arrive while a transaction or classified recovery is
  // visible. Do not let an intermediate malformed/missing snapshot erase the
  // state that owns cancellation/retry; only an explicit action may advance it.
  if (state.status === "working") return state;
  // M23-25 validation errors precede the sole candidate write, so a later graph
  // census is not evidence that the refused candidate was repaired.
  if (state.status === "error") return state;
  // A canvas census during a host drop says nothing about the socket. Reclassifying here would
  // discard the interrupted state and present a normal surface while the host is still gone.
  if (state.status === "host_unavailable") return state;
  if (action.inspection.status !== "ready") {
    return {
      status: "interactive",
      reason: inspectionReason(action.inspection),
      existingGraph: (action.inspection.nodeCount ?? 0) > 0,
      inspection: action.inspection,
    };
  }
  if (state.status === "projected") {
    const anchor = action.inspection.anchors[0]?.executionId;
    const sameAnchor =
      anchor === state.projection.correlation.execution_node_id;
    return {
      status: "interactive",
      reason:
        action.inspection.existingGraphCompatible === true
          ? "native_preference"
          : "incompatible_graph",
      existingGraph: true,
      inspection: action.inspection,
      message: sameAnchor
        ? "The visible graph is ready for another run."
        : "The visible graph changed; review it before another run.",
    };
  }
  if (action.inspection.existingGraphCompatible !== true) {
    return {
      status: "interactive",
      reason: "incompatible_graph",
      existingGraph: true,
      inspection: action.inspection,
      message:
        "The visible H3 graph needs a Product Shell connection before binding.",
    };
  }
  return {
    status: "interactive",
    reason: "native_preference",
    existingGraph: true,
    inspection: action.inspection,
  };
}
