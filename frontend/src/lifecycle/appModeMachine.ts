export const APP_MODE_TERMINAL_STATES = [
  "terminal.done",
  "terminal.refused",
  "terminal.failed",
  "terminal.cancelled",
] as const;

export const APP_MODE_STATE_VALUES = [
  "idle",
  "census",
  "deciding",
  "validating",
  "writing",
  "preparing",
  "queued",
  "running",
  "closing",
  "host_unavailable",
  ...APP_MODE_TERMINAL_STATES,
] as const;

export const APP_MODE_EVENT_TYPES = [
  "START",
  "CENSUS_RESOLVED",
  "DECISION_ACCEPTED",
  "VALIDATED",
  "WRITTEN",
  "PREPARATION_READY",
  "CANVAS_READY",
  "QUEUE_ACCEPTED",
  "EXECUTION_STARTED",
  "EXECUTION_FINISHED",
  "OUTPUT_VERIFIED",
  "REFUSED",
  "FAILED",
  "CANCEL",
  "HOST_LOST",
  "HOST_RECONNECTING",
  "HOST_RESTORED",
  "RESET",
] as const;

export const APP_MODE_EFFECT_TYPES = [
  "queue",
  "write",
  "restore",
  "detach",
  "readProjection",
  "journal",
  "present",
] as const;

export const APP_MODE_EFFECT_OWNERS = [
  "AppModeQualification",
  "GraphWriteTransaction",
  "ManagedPreparation",
  "QueueSubmissionTransaction",
  "ExecutionCorrelator",
  "ManagedSequenceRunner",
  "ManagedDiagnostics",
] as const;

export type AppModeTerminalState = (typeof APP_MODE_TERMINAL_STATES)[number];
export type AppModeStateValue =
  | "idle"
  | "census"
  | "deciding"
  | "validating"
  | "writing"
  | "preparing"
  | "queued"
  | "running"
  | "closing"
  | "host_unavailable"
  | AppModeTerminalState;

export type AppModeRoute = "new" | "existing" | "replace" | "connect";
export type AppModeDecision =
  "empty" | "existing" | "dirty" | "ambiguous" | "incompatible" | "malformed";

export type AppModeFailure = Readonly<{
  code: string;
  recovery: "retry" | "retry_output_verification" | "inspect" | "use_native";
}>;

export type AppModeMachineContext = Readonly<{
  run: number;
  route?: AppModeRoute;
  existingGraph: boolean;
  decision?: AppModeDecision;
  ownedNodeIds: readonly string[];
  ownedLinkIds: readonly string[];
  ownedProjectionFingerprint?: string;
  executionIdentity?: string;
  surroundingsDigest?: string;
  promptId?: string;
  failure?: AppModeFailure;
  refusal?: string;
  hostPhase?: "lost" | "reconnecting";
}>;

export type AppModeSnapshot = Readonly<{
  value: AppModeStateValue;
  context: AppModeMachineContext;
  suspended?: AppModeSnapshot;
}>;

export type AppModeEvent =
  | Readonly<{
      type: "START";
      run: number;
      route: AppModeRoute;
      existingGraph: boolean;
    }>
  | Readonly<{ type: "CENSUS_RESOLVED"; decision: AppModeDecision }>
  | Readonly<{ type: "DECISION_ACCEPTED" }>
  | Readonly<{ type: "VALIDATED"; executionIdentity: string }>
  | Readonly<{
      type: "WRITTEN";
      ownedNodeIds: readonly string[];
      ownedLinkIds: readonly string[];
      ownedProjectionFingerprint: string;
      surroundingsDigest?: string;
    }>
  | Readonly<{ type: "PREPARATION_READY" }>
  | Readonly<{
      type: "CANVAS_READY";
      ownedNodeIds: readonly string[];
      ownedLinkIds: readonly string[];
      ownedProjectionFingerprint: string;
    }>
  | Readonly<{ type: "QUEUE_ACCEPTED"; promptId: string }>
  | Readonly<{ type: "EXECUTION_STARTED" }>
  | Readonly<{ type: "EXECUTION_FINISHED" }>
  | Readonly<{ type: "OUTPUT_VERIFIED" }>
  | Readonly<{ type: "REFUSED"; reason: string }>
  | Readonly<{
      type: "FAILED";
      code: string;
      recovery: AppModeFailure["recovery"];
    }>
  | Readonly<{ type: "CANCEL" }>
  | Readonly<{ type: "HOST_LOST" }>
  | Readonly<{ type: "HOST_RECONNECTING" }>
  | Readonly<{ type: "HOST_RESTORED" }>
  | Readonly<{ type: "RESET" }>;

export type AppModeEffectOwner =
  | "AppModeQualification"
  | "GraphWriteTransaction"
  | "ManagedPreparation"
  | "QueueSubmissionTransaction"
  | "ExecutionCorrelator"
  | "ManagedSequenceRunner"
  | "ManagedDiagnostics";

export type AppModeEffect = Readonly<{
  type:
    | "queue"
    | "write"
    | "restore"
    | "detach"
    | "readProjection"
    | "journal"
    | "present";
  owner: AppModeEffectOwner;
  name: string;
}>;

export type AppModeTransition = Readonly<{
  accepted: boolean;
  snapshot: AppModeSnapshot;
  effects: readonly AppModeEffect[];
}>;

const EMPTY_CONTEXT: AppModeMachineContext = Object.freeze({
  run: 0,
  existingGraph: false,
  ownedNodeIds: Object.freeze([]),
  ownedLinkIds: Object.freeze([]),
});

const INITIAL_SNAPSHOT: AppModeSnapshot = Object.freeze({
  value: "idle",
  context: EMPTY_CONTEXT,
});

const terminalSet = new Set<AppModeStateValue>(APP_MODE_TERMINAL_STATES);

export function isAppModeTerminal(
  value: AppModeStateValue,
): value is AppModeTerminalState {
  return terminalSet.has(value);
}

function effect(
  type: AppModeEffect["type"],
  owner: AppModeEffectOwner,
  name: string,
): AppModeEffect {
  return Object.freeze({ type, owner, name });
}

function freezeContext(context: AppModeMachineContext): AppModeMachineContext {
  return Object.freeze({
    ...context,
    ownedNodeIds: Object.freeze([...context.ownedNodeIds]),
    ownedLinkIds: Object.freeze([...context.ownedLinkIds]),
  });
}

function snapshot(
  value: AppModeStateValue,
  context: AppModeMachineContext,
  suspended?: AppModeSnapshot,
): AppModeSnapshot {
  return Object.freeze({
    value,
    context: freezeContext(context),
    ...(suspended === undefined ? {} : { suspended }),
  });
}

function accepted(
  previous: AppModeSnapshot,
  event: AppModeEvent,
  next: AppModeSnapshot,
  invoked: readonly AppModeEffect[] = [],
): AppModeTransition {
  const transitionName = `${previous.value}->${next.value}:${event.type}`;
  return Object.freeze({
    accepted: true,
    snapshot: next,
    effects: Object.freeze([
      effect("journal", "ManagedDiagnostics", transitionName),
      ...invoked,
      effect("present", "ManagedDiagnostics", next.value),
    ]),
  });
}

function ignored(current: AppModeSnapshot): AppModeTransition {
  return Object.freeze({
    accepted: false,
    snapshot: current,
    effects: Object.freeze([]),
  });
}

function safeRun(value: number): boolean {
  return Number.isSafeInteger(value) && value > 0;
}

function safeIdentity(value: string): boolean {
  return value.length > 0 && value.length <= 256;
}

function canCancel(value: AppModeStateValue): boolean {
  return ![
    "idle",
    "queued",
    "running",
    "closing",
    "host_unavailable",
    ...APP_MODE_TERMINAL_STATES,
  ].includes(value as never);
}

function transition(
  current: AppModeSnapshot,
  event: AppModeEvent,
): AppModeTransition {
  if (event.type === "RESET") return accepted(current, event, INITIAL_SNAPSHOT);

  if (event.type === "START") {
    if (isAppModeTerminal(current.value) || !safeRun(event.run))
      return ignored(current);
    const next = snapshot("census", {
      run: event.run,
      route: event.route,
      existingGraph: event.existingGraph,
      ownedNodeIds: [],
      ownedLinkIds: [],
    });
    return accepted(current, event, next, [
      effect("readProjection", "AppModeQualification", "inspect_graph"),
    ]);
  }

  if (event.type === "HOST_LOST") {
    if (current.value === "idle" || isAppModeTerminal(current.value))
      return ignored(current);
    if (current.value === "host_unavailable")
      return accepted(
        current,
        event,
        snapshot(
          "host_unavailable",
          { ...current.context, hostPhase: "lost" },
          current.suspended,
        ),
      );
    return accepted(
      current,
      event,
      snapshot(
        "host_unavailable",
        { ...current.context, hostPhase: "lost" },
        current,
      ),
      [effect("detach", "ManagedSequenceRunner", "stop_serial_successors")],
    );
  }
  if (
    event.type === "HOST_RECONNECTING" &&
    current.value === "host_unavailable"
  )
    return accepted(
      current,
      event,
      snapshot(
        "host_unavailable",
        { ...current.context, hostPhase: "reconnecting" },
        current.suspended,
      ),
    );
  if (event.type === "HOST_RESTORED" && current.value === "host_unavailable") {
    if (current.suspended === undefined) return ignored(current);
    return accepted(current, event, current.suspended, [
      effect("restore", "ExecutionCorrelator", "restore_interrupted_run"),
    ]);
  }

  if (event.type === "REFUSED" && !isAppModeTerminal(current.value))
    return accepted(
      current,
      event,
      snapshot("terminal.refused", {
        ...current.context,
        refusal: event.reason,
      }),
    );
  if (event.type === "FAILED" && !isAppModeTerminal(current.value))
    return accepted(
      current,
      event,
      snapshot("terminal.failed", {
        ...current.context,
        failure: Object.freeze({ code: event.code, recovery: event.recovery }),
      }),
    );
  if (event.type === "CANCEL" && canCancel(current.value))
    return accepted(
      current,
      event,
      snapshot("terminal.cancelled", current.context),
    );

  if (current.value === "census" && event.type === "CENSUS_RESOLVED")
    return accepted(
      current,
      event,
      snapshot("deciding", { ...current.context, decision: event.decision }),
    );
  if (current.value === "deciding" && event.type === "DECISION_ACCEPTED")
    return accepted(current, event, snapshot("validating", current.context));
  if (
    current.value === "validating" &&
    event.type === "VALIDATED" &&
    safeIdentity(event.executionIdentity)
  )
    return accepted(
      current,
      event,
      snapshot("writing", {
        ...current.context,
        executionIdentity: event.executionIdentity,
      }),
      [effect("write", "GraphWriteTransaction", "commit_owned_projection")],
    );
  if (
    current.value === "writing" &&
    event.type === "WRITTEN" &&
    safeIdentity(event.ownedProjectionFingerprint)
  )
    return accepted(
      current,
      event,
      snapshot("preparing", {
        ...current.context,
        ownedNodeIds: event.ownedNodeIds,
        ownedLinkIds: event.ownedLinkIds,
        ownedProjectionFingerprint: event.ownedProjectionFingerprint,
        ...(event.surroundingsDigest === undefined
          ? {}
          : { surroundingsDigest: event.surroundingsDigest }),
      }),
      [effect("restore", "ManagedPreparation", "prepare_bootstrap")],
    );
  // IMPORTANT: canvas readiness terminalizes preparation without a queue effect.
  if (
    current.value === "writing" &&
    event.type === "CANVAS_READY" &&
    safeIdentity(event.ownedProjectionFingerprint)
  )
    return accepted(
      current,
      event,
      snapshot("terminal.done", {
        ...current.context,
        existingGraph: true,
        ownedNodeIds: event.ownedNodeIds,
        ownedLinkIds: event.ownedLinkIds,
        ownedProjectionFingerprint: event.ownedProjectionFingerprint,
      }),
    );
  if (current.value === "preparing" && event.type === "PREPARATION_READY")
    return accepted(current, event, snapshot("queued", current.context), [
      effect("queue", "QueueSubmissionTransaction", "submit_exact_prompt"),
    ]);
  // IMPORTANT: the pinned ComfyUI frontend may deliver `execution_start` over the WebSocket
  // before the queue HTTP response has been stored, so the prompt binding can land while the
  // machine is already `running`. Record it in whichever of the two states it arrives, once per
  // run (a second, different prompt id would mean a second submission and is ignored). Restricting
  // this to `queued` silently drops the binding for that ordering.
  if (
    (current.value === "queued" || current.value === "running") &&
    event.type === "QUEUE_ACCEPTED" &&
    safeIdentity(event.promptId) &&
    current.context.promptId === undefined
  )
    return accepted(
      current,
      event,
      snapshot(current.value, { ...current.context, promptId: event.promptId }),
    );
  if (
    (current.value === "queued" || current.value === "running") &&
    event.type === "EXECUTION_STARTED"
  )
    return accepted(current, event, snapshot("running", current.context));
  if (current.value === "running" && event.type === "EXECUTION_FINISHED")
    return accepted(current, event, snapshot("closing", current.context), [
      effect("readProjection", "ExecutionCorrelator", "verify_output"),
    ]);
  if (current.value === "closing" && event.type === "OUTPUT_VERIFIED")
    return accepted(current, event, snapshot("terminal.done", current.context));

  return ignored(current);
}

export type AppModeMachine = Readonly<{
  initial: AppModeSnapshot;
  transition(current: AppModeSnapshot, event: AppModeEvent): AppModeTransition;
}>;

export function createAppModeMachine(): AppModeMachine {
  return Object.freeze({ initial: INITIAL_SNAPSHOT, transition });
}

type GraphEdge = Readonly<{
  from: AppModeStateValue;
  to: AppModeStateValue;
  event: AppModeEvent["type"];
}>;

const graphEdges: readonly GraphEdge[] = Object.freeze([
  { from: "idle", to: "census", event: "START" },
  { from: "census", to: "deciding", event: "CENSUS_RESOLVED" },
  { from: "deciding", to: "validating", event: "DECISION_ACCEPTED" },
  { from: "validating", to: "writing", event: "VALIDATED" },
  { from: "writing", to: "preparing", event: "WRITTEN" },
  { from: "preparing", to: "queued", event: "PREPARATION_READY" },
  { from: "writing", to: "terminal.done", event: "CANVAS_READY" },
  { from: "queued", to: "running", event: "EXECUTION_STARTED" },
  { from: "running", to: "closing", event: "EXECUTION_FINISHED" },
  { from: "closing", to: "terminal.done", event: "OUTPUT_VERIFIED" },
  { from: "census", to: "terminal.refused", event: "REFUSED" },
  { from: "validating", to: "terminal.failed", event: "FAILED" },
  { from: "deciding", to: "terminal.cancelled", event: "CANCEL" },
]);

export type AppModeGraphReport = Readonly<{
  reachableStates: readonly AppModeStateValue[];
  unreachableTerminals: readonly AppModeTerminalState[];
  forbiddenPaths: readonly string[];
}>;

function hasPath(
  from: AppModeStateValue,
  to: AppModeStateValue,
  excluded: ReadonlySet<AppModeStateValue> = new Set(),
): boolean {
  const pending = [from];
  const visited = new Set<AppModeStateValue>();
  while (pending.length > 0) {
    const current = pending.shift()!;
    if (current === to) return true;
    if (visited.has(current) || excluded.has(current)) continue;
    visited.add(current);
    for (const edge of graphEdges)
      if (edge.from === current) pending.push(edge.to);
  }
  return false;
}

export function analyzeAppModeGraph(): AppModeGraphReport {
  const reachable = new Set<AppModeStateValue>(["idle"]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const edge of graphEdges)
      if (reachable.has(edge.from) && !reachable.has(edge.to)) {
        reachable.add(edge.to);
        changed = true;
      }
  }
  const forbidden: string[] = [];
  if (
    graphEdges.some(
      (edge) => edge.to === "writing" && edge.from !== "validating",
    )
  )
    forbidden.push("writing_without_validating");
  if (hasPath("idle", "queued", new Set(["writing"])))
    forbidden.push("queued_without_writing");
  if (
    graphEdges.some(
      (edge) => isAppModeTerminal(edge.from) && edge.event !== "RESET",
    )
  )
    forbidden.push("terminal_has_non_reset_exit");
  return Object.freeze({
    reachableStates: Object.freeze([...reachable].sort()),
    unreachableTerminals: Object.freeze(
      APP_MODE_TERMINAL_STATES.filter((value) => !reachable.has(value)),
    ),
    forbiddenPaths: Object.freeze(forbidden),
  });
}

export function replayAppModeEvents(events: readonly AppModeEvent[]): Readonly<{
  snapshot: AppModeSnapshot;
  effects: readonly AppModeEffect[];
}> {
  const machine = createAppModeMachine();
  let current = machine.initial;
  const effects: AppModeEffect[] = [];
  for (const event of events) {
    const result = machine.transition(current, event);
    current = result.snapshot;
    effects.push(...result.effects);
  }
  return Object.freeze({ snapshot: current, effects: Object.freeze(effects) });
}
