import type { SemanticProposalReviewRequest } from "../components/SemanticProposalReview";
import type { ProductionWorkbenchProjection } from "../contracts/productionWorkbenchCodec";
import type {
  SemanticProposalActionResult,
  SemanticProposalReviewHandle,
  SemanticProposalReviewProjection,
} from "../contracts/semanticProposalReviewCodec";
import {
  initialSemanticProposalReviewState,
  reduceSemanticProposalReviewState,
  type SemanticProposalReviewState,
} from "../state/semanticProposalReview";

const LIMIT = 64,
  TTL = 900_000,
  TIMEOUT = 30_000;
type Token = unknown;
type Action = { action: "proposal_read" } | SemanticProposalReviewRequest;
export type ProposalActionIdentity = Readonly<{
  workspace_id: string;
  report_revision: number;
  report_fingerprint: string;
}>;
type Source = Readonly<{
  action: ProposalActionIdentity;
  handle: SemanticProposalReviewHandle;
  observedAt: number;
}>;
export type ProductionProposalSourceClaim = Readonly<{ source: Source }>;
type Binding = {
  source: Source;
  workspaceHandle: string;
  workspaceId: string;
  workspaceRevision: number;
  workspaceFingerprint: string;
  segmentId: string;
};
type Status =
  | "not_issued"
  | "loading"
  | "ready"
  | "mutating"
  | "terminal"
  | "client_aborted"
  | "stale"
  | "failed"
  | "unavailable";
type Reason =
  | "expired"
  | "not_provided"
  | "request_failed"
  | "source_unavailable"
  | "stale"
  | "timeout";
type Target = {
  source?: Source;
  state: SemanticProposalReviewState;
  generation: number;
  abort?: AbortController;
  timeout?: Token;
  deadline?: number;
  status: Status;
  reason?: Reason;
};
type Row = Target & { binding?: Binding };
export type ProductionProposalRow = Readonly<{
  segmentId: string;
  ordinal: number;
  status: Status;
  actionable: boolean;
  reason?: "source_unavailable" | "unbound" | "capacity" | Reason;
  reviewState?: SemanticProposalReviewState;
}>;
type Send = (
  action: ProposalActionIdentity,
  handle: SemanticProposalReviewHandle,
  current: SemanticProposalReviewProjection | undefined,
  request: Action,
  signal?: AbortSignal,
) => Promise<SemanticProposalActionResult>;
type Bind =
  | Readonly<{
      action: "create";
      source?: ProductionProposalSourceClaim;
      after: ProductionWorkbenchProjection;
    }>
  | Readonly<{
      action: "add";
      source?: ProductionProposalSourceClaim;
      before: ProductionWorkbenchProjection;
      after: ProductionWorkbenchProjection;
    }>
  | Readonly<{
      action: "replace";
      source?: ProductionProposalSourceClaim;
      before: ProductionWorkbenchProjection;
      after: ProductionWorkbenchProjection;
      segmentId: string;
    }>;
type ReadBatch = {
  cancelled: boolean;
  generation: number;
  segmentIds: readonly string[];
};

const sameHandle = (
  a: SemanticProposalReviewHandle,
  b: SemanticProposalReviewHandle,
) =>
  a.review_id === b.review_id &&
  a.transaction_fingerprint === b.transaction_fingerprint &&
  a.workspace_fingerprint === b.workspace_fingerprint &&
  a.report_fingerprint === b.report_fingerprint &&
  a.correlation.prompt_id === b.correlation.prompt_id &&
  a.correlation.execution_node_id === b.correlation.execution_node_id;
const sameAction = (a: ProposalActionIdentity, b: ProposalActionIdentity) =>
  a.workspace_id === b.workspace_id &&
  a.report_revision === b.report_revision &&
  a.report_fingerprint === b.report_fingerprint;
const hasSegment = (p: ProductionWorkbenchProjection, id: string) =>
  p.segments.filter((s) => s.segmentId === id).length === 1;
const sameProduction = (b: Binding, p: ProductionWorkbenchProjection) =>
  b.workspaceHandle === p.workspaceHandle &&
  b.workspaceId === p.workspaceId &&
  b.workspaceRevision === p.workspaceRevision &&
  b.workspaceFingerprint === p.workspaceFingerprint &&
  hasSegment(p, b.segmentId);

export function createProductionProposalDispatcher({
  send,
  now = () => performance.now(),
  schedule = (callback, delay) => setTimeout(callback, delay),
  cancelSchedule = (token) =>
    clearTimeout(token as ReturnType<typeof setTimeout>),
  changed = () => undefined,
}: {
  send: Send;
  now?: () => number;
  schedule?: (callback: () => void, delay: number) => Token;
  cancelSchedule?: (token: Token) => void;
  changed?: () => void;
}) {
  let sources: Source[] = [],
    current: Source | undefined,
    expiry: Token | undefined,
    sourceCapacity = false,
    batchGeneration = 0,
    activeBatch: ReadBatch | undefined,
    disposed = false;
  const bindings = new Map<string, Binding>(),
    rows = new Map<string, Row>(),
    timers = new Set<Token>();
  const context: Target = {
    state: initialSemanticProposalReviewState,
    generation: 0,
    status: "unavailable",
  };
  const later = (callback: () => void, delay: number) => {
    let token: Token;
    token = schedule(
      () => {
        cancelSchedule(token);
        timers.delete(token);
        callback();
      },
      Math.max(0, delay),
    );
    timers.add(token);
    return token;
  };
  const cancel = (token?: Token) => {
    if (token !== undefined) {
      cancelSchedule(token);
      timers.delete(token);
    }
  };
  const stop = (target: Target, status: Status, reason?: Reason) => {
    const handle =
      target.source === undefined
        ? undefined
        : target.state.status === "unavailable"
          ? target.source?.handle
          : (() => {
              const closed = reduceSemanticProposalReviewState(target.state, {
                type: "close",
              });
              return closed.status === "unavailable"
                ? undefined
                : closed.handle;
            })();
    target.generation++;
    target.abort?.abort();
    cancel(target.timeout);
    target.abort = undefined;
    target.timeout = undefined;
    target.deadline = undefined;
    target.status = status;
    target.reason = reason;
    target.state =
      handle === undefined
        ? initialSemanticProposalReviewState
        : { status: "closed", handle };
  };
  const releaseIdentity = (target: Target, status: Status, reason?: Reason) => {
    // CRITICAL: expiry/invalidation must not leave review/action identities strongly referenced.
    stop(target, status, reason);
    target.source = undefined;
    target.state = initialSemanticProposalReviewState;
    if ("binding" in target) (target as Row).binding = undefined;
  };
  const cancelReadBatch = (preserveSegmentId?: string) => {
    const batch = activeBatch;
    if (batch === undefined || batch.cancelled) return;
    batch.cancelled = true;
    batchGeneration++;
    for (const id of batch.segmentIds) {
      if (id === preserveSegmentId) continue;
      const row = rows.get(id);
      if (
        row &&
        (row.status === "not_issued" ||
          row.status === "loading" ||
          row.status === "mutating")
      )
        stop(row, "client_aborted");
    }
  };
  const planExpiry = () => {
    cancel(expiry);
    expiry = undefined;
    if (disposed) return;
    const deadlines = [
      ...sources.map((s) => s.observedAt + TTL),
      ...[context, ...rows.values()].flatMap((t) =>
        t.deadline === undefined ? [] : [t.deadline],
      ),
    ];
    if (deadlines.length)
      expiry = later(
        () => {
          expiry = undefined;
          prune();
        },
        Math.min(...deadlines) - now(),
      );
  };
  const prune = () => {
    const time = now(),
      expired = new Set(sources.filter((s) => time >= s.observedAt + TTL));
    if (expired.size) {
      sources = sources.filter((s) => !expired.has(s));
      if (sources.length < LIMIT) sourceCapacity = false;
      if (current && expired.has(current)) current = undefined;
      if (context.source && expired.has(context.source))
        releaseIdentity(context, "unavailable", "expired");
      for (const [id, binding] of bindings)
        if (expired.has(binding.source)) {
          const row = rows.get(id);
          if (row) releaseIdentity(row, "unavailable", "expired");
          bindings.delete(id);
        }
    }
    for (const target of [context, ...rows.values()])
      if (target.deadline !== undefined && time >= target.deadline)
        releaseIdentity(target, "unavailable", "expired");
    planExpiry();
    if (expired.size) changed();
  };
  const install = (
    source: Source,
    p: ProductionWorkbenchProjection,
    segmentId: string,
  ) => {
    if (!hasSegment(p, segmentId)) return false;
    if (!bindings.has(segmentId) && bindings.size >= LIMIT) return false;
    while (!rows.has(segmentId) && rows.size >= LIMIT) {
      const identityFree = [...rows].find(
        ([, row]) => row.binding === undefined,
      );
      if (!identityFree) return false;
      // IMPORTANT: evict only content-free tombstones; live exact bindings are never displaced.
      rows.delete(identityFree[0]);
    }
    const old = rows.get(segmentId);
    if (old) stop(old, "stale", "stale");
    const binding: Binding = {
      source,
      workspaceHandle: p.workspaceHandle,
      workspaceId: p.workspaceId,
      workspaceRevision: p.workspaceRevision,
      workspaceFingerprint: p.workspaceFingerprint,
      segmentId,
    };
    bindings.set(segmentId, binding);
    rows.set(segmentId, {
      binding,
      source,
      state: { status: "closed", handle: source.handle },
      generation: (old?.generation ?? 0) + 1,
      status: "not_issued",
    });
    return true;
  };
  const installUnavailable = (
    p: ProductionWorkbenchProjection,
    segmentId: string,
    reason: "not_provided" | "source_unavailable",
  ) => {
    if (!hasSegment(p, segmentId)) return false;
    while (!rows.has(segmentId) && rows.size >= LIMIT) {
      const identityFree = [...rows].find(
        ([, row]) => row.binding === undefined,
      );
      if (!identityFree) return false;
      rows.delete(identityFree[0]);
    }
    const old = rows.get(segmentId);
    if (old) releaseIdentity(old, "unavailable", reason);
    bindings.delete(segmentId);
    rows.set(segmentId, {
      state: initialSemanticProposalReviewState,
      generation: (old?.generation ?? 0) + 1,
      status: "unavailable",
      reason,
    });
    return true;
  };
  const rowFor = (id: string, p: ProductionWorkbenchProjection) => {
    const row = rows.get(id);
    return row?.binding && sameProduction(row.binding, p) ? row : undefined;
  };
  const execute = async (
    target: Target,
    action: Action,
    production?: ProductionWorkbenchProjection,
  ) => {
    prune();
    const source = target.source;
    if (!source || !sources.includes(source)) return;
    const handle =
      target.state.status === "unavailable"
        ? source.handle
        : target.state.handle;
    const projection =
      target.state.status === "ready" || target.state.status === "mutating"
        ? target.state.projection
        : undefined;
    if (action.action !== "proposal_read" && !projection) return;
    if (target.abort) stop(target, "client_aborted");
    const generation = ++target.generation,
      abort = new AbortController(),
      started = now();
    target.abort = abort;
    target.status = action.action === "proposal_read" ? "loading" : "mutating";
    target.reason = undefined;
    target.state = reduceSemanticProposalReviewState(
      target.state,
      action.action === "proposal_read"
        ? { type: "open" }
        : { type: "request", action: action.action },
    );
    changed();
    if (
      action.action !== "proposal_read" &&
      target.state.status !== "mutating"
    ) {
      target.abort = undefined;
      return;
    }
    let wake!: () => void;
    const timedOut = new Promise<undefined>((resolve) => {
      wake = () => resolve(undefined);
    });
    target.timeout = later(wake, TIMEOUT);
    const result = await Promise.race([
      send(source.action, handle, projection, action, abort.signal).catch(
        () => null,
      ),
      timedOut,
    ]);
    if (
      target.generation !== generation ||
      target.abort !== abort ||
      target.source !== source
    )
      return;
    cancel(target.timeout);
    target.timeout = undefined;
    target.abort = undefined;
    if (
      target !== context &&
      production !== undefined &&
      !sameProduction((target as Row).binding!, production)
    ) {
      stop(target, "stale", "stale");
      changed();
      return;
    }
    if (result === undefined) {
      abort.abort();
      stop(target, "client_aborted", "timeout");
    } else if (result === null) {
      target.status = "failed";
      target.reason = "request_failed";
      target.state = reduceSemanticProposalReviewState(target.state, {
        type: "failed",
      });
      // CRITICAL: timer throttling cannot extend the absolute source-retention boundary.
    } else if (
      now() >= source.observedAt + TTL ||
      now() >= started + TTL ||
      !sources.includes(source)
    )
      stop(target, "unavailable", "expired");
    else {
      target.state = reduceSemanticProposalReviewState(target.state, {
        type: "received",
        result,
      });
      target.status =
        target.state.status === "error"
          ? "failed"
          : result.review.terminal === null
            ? "ready"
            : "terminal";
      target.reason =
        target.state.status === "error" ? "request_failed" : undefined;
      target.deadline = started + TTL;
      if (
        target === context &&
        action.action === "proposal_read" &&
        production &&
        result.review.workspace_fingerprint ===
          production.workspaceFingerprint &&
        hasSegment(production, result.review.segment_id)
      )
        install(source, production, result.review.segment_id);
    }
    planExpiry();
    changed();
  };
  const clearRows = (status: Status, reason?: Reason) => {
    for (const row of rows.values()) stop(row, status, reason);
    bindings.clear();
    rows.clear();
  };
  const invalidateWorkspace = () => {
    cancelReadBatch();
    for (const row of rows.values()) releaseIdentity(row, "stale", "stale");
    bindings.clear();
    releaseIdentity(context, "unavailable", "stale");
    sources = [];
    current = undefined;
    sourceCapacity = false;
    planExpiry();
  };
  const advanceIdentity = (
    before: ProductionWorkbenchProjection,
    after: ProductionWorkbenchProjection,
  ) => {
    const exactBefore = [...bindings.values()].every((binding) =>
      sameProduction(binding, before),
    );
    if (
      before.workspaceHandle !== after.workspaceHandle ||
      before.workspaceId !== after.workspaceId ||
      // IMPORTANT: a revision gap can hide a source replacement behind the same segment id.
      // Advance only one observed server transition or review actions can target old Context.
      after.workspaceRevision !== before.workspaceRevision + 1 ||
      !exactBefore
    ) {
      invalidateWorkspace();
      return false;
    }
    for (const [id, binding] of [...bindings]) {
      if (!hasSegment(after, id)) {
        const row = rows.get(id);
        if (row) stop(row, "stale", "stale");
        bindings.delete(id);
        rows.delete(id);
      } else {
        binding.workspaceRevision = after.workspaceRevision;
        binding.workspaceFingerprint = after.workspaceFingerprint;
      }
    }
    return true;
  };
  const rejectCurrent = () => {
    current = undefined;
    context.source = undefined;
    stop(context, "unavailable");
    changed();
  };

  return Object.freeze({
    observe(
      action: ProposalActionIdentity,
      handle: SemanticProposalReviewHandle,
    ) {
      prune();
      const identity = Object.freeze({
        workspace_id: action.workspace_id,
        report_revision: action.report_revision,
        report_fingerprint: action.report_fingerprint,
      });
      if (identity.report_fingerprint !== handle.report_fingerprint) {
        sourceCapacity = false;
        rejectCurrent();
        return "conflict" as const;
      }
      const prior = sources.find(
        (s) => s.handle.review_id === handle.review_id,
      );
      if (prior) {
        if (
          !sameHandle(prior.handle, handle) ||
          !sameAction(prior.action, identity)
        ) {
          rejectCurrent();
          return "conflict" as const;
        }
        current = prior;
        sourceCapacity = false;
        context.source = prior;
        context.state = reduceSemanticProposalReviewState(context.state, {
          type: "host",
          handle,
        });
        return "idempotent" as const;
      }
      if (sources.length >= LIMIT) {
        sourceCapacity = true;
        rejectCurrent();
        return "capacity" as const;
      }
      const source = Object.freeze({
        action: identity,
        handle,
        observedAt: now(),
      });
      sources.push(source);
      sourceCapacity = false;
      current = source;
      context.source = source;
      context.status = "not_issued";
      context.state = reduceSemanticProposalReviewState(context.state, {
        type: "host",
        handle,
      });
      planExpiry();
      return "admitted" as const;
    },
    captureCurrent() {
      prune();
      return current ? Object.freeze({ source: current }) : undefined;
    },
    capacityStatus: () => sourceCapacity,
    clearCurrentSource() {
      current = undefined;
      sourceCapacity = false;
      context.source = undefined;
      stop(context, "unavailable");
      changed();
    },
    contextState: () => context.state,
    runContext: (action: Action, production?: ProductionWorkbenchProjection) =>
      execute(context, action, production),
    closeContext() {
      stop(context, context.source ? "not_issued" : "unavailable");
      changed();
    },
    bind(request: Bind) {
      prune();
      const claimedSource = request.source?.source,
        source =
          claimedSource !== undefined && sources.includes(claimedSource)
            ? claimedSource
            : undefined,
        unavailableReason =
          claimedSource === undefined
            ? ("not_provided" as const)
            : ("source_unavailable" as const);
      let segmentId: string | undefined;
      if (request.action === "create") {
        if (request.after.segments.length !== 1) return false;
        clearRows("stale", "stale");
        segmentId = request.after.segments[0]?.segmentId;
      } else if (request.action === "add") {
        if (
          request.before.workspaceHandle !== request.after.workspaceHandle ||
          request.before.workspaceId !== request.after.workspaceId
        )
          return false;
        const before = new Set(request.before.segments.map((s) => s.segmentId)),
          added = request.after.segments.filter(
            (s) => !before.has(s.segmentId),
          );
        if (added.length !== 1) return false;
        if (!advanceIdentity(request.before, request.after)) return false;
        segmentId = added[0]?.segmentId;
      } else {
        if (!hasSegment(request.after, request.segmentId)) return false;
        if (!advanceIdentity(request.before, request.after)) return false;
        segmentId = request.segmentId;
      }
      if (!segmentId) return false;
      // IMPORTANT: an accepted no-source append still advances unrelated exact bindings;
      // a no-source replacement invalidates only its target instead of retargeting Context.
      return source === undefined
        ? installUnavailable(request.after, segmentId, unavailableReason)
        : install(source, request.after, segmentId);
    },
    rows(p: ProductionWorkbenchProjection): readonly ProductionProposalRow[] {
      prune();
      return p.segments.map((s) => {
        const retained = rows.get(s.segmentId),
          row = rowFor(s.segmentId, p);
        return row
          ? Object.freeze({
              segmentId: s.segmentId,
              ordinal: s.ordinal,
              status: row.status,
              actionable: true,
              reason: row.reason,
              reviewState: row.state,
            })
          : retained?.status === "stale"
            ? Object.freeze({
                segmentId: s.segmentId,
                ordinal: s.ordinal,
                status: "stale" as const,
                actionable: false,
                reason: "stale" as const,
                reviewState:
                  retained.state.status === "unavailable"
                    ? undefined
                    : retained.state,
              })
            : Object.freeze({
                segmentId: s.segmentId,
                ordinal: s.ordinal,
                status: "unavailable" as const,
                actionable: false,
                reason:
                  retained?.status === "unavailable" && retained.reason
                    ? retained.reason
                    : current === undefined
                      ? sourceCapacity
                        ? ("capacity" as const)
                        : ("source_unavailable" as const)
                      : ("unbound" as const),
              });
      });
    },
    async readSelected(
      p: ProductionWorkbenchProjection,
      selected: readonly string[],
    ) {
      prune();
      if (selected.length > LIMIT || new Set(selected).size !== selected.length)
        return;
      const chosen = new Set(selected),
        queue = p.segments
          .filter((s) => chosen.has(s.segmentId))
          .map((s) => s.segmentId);
      if (queue.length !== selected.length) return;
      // CRITICAL: one dispatcher-owned batch prevents overlapping calls from bypassing concurrency two.
      if (activeBatch !== undefined) return;
      const batch: ReadBatch = {
        cancelled: false,
        generation: ++batchGeneration,
        segmentIds: Object.freeze([...queue]),
      };
      activeBatch = batch;
      let cursor = 0;
      const worker = async () => {
        while (
          activeBatch === batch &&
          !batch.cancelled &&
          batch.generation === batchGeneration &&
          cursor < queue.length
        ) {
          const id = queue[cursor++],
            row = id ? rowFor(id, p) : undefined;
          if (row) {
            await execute(row, { action: "proposal_read" }, p);
            if (row.status === "client_aborted" && row.reason === "timeout") {
              cancelReadBatch(id);
              return;
            }
          }
        }
      };
      try {
        await Promise.all([worker(), worker()]);
      } finally {
        if (activeBatch === batch) activeBatch = undefined;
      }
    },
    cancelSelected(
      p: ProductionWorkbenchProjection,
      selected: readonly string[],
    ) {
      if (selected.length > LIMIT || new Set(selected).size !== selected.length)
        return;
      const chosen = new Set(selected);
      const canonical = p.segments.filter((s) => chosen.has(s.segmentId));
      if (canonical.length !== selected.length) return;
      if (activeBatch?.segmentIds.some((id) => chosen.has(id)) === true)
        cancelReadBatch();
      for (const segment of canonical) {
        const row = rowFor(segment.segmentId, p);
        if (row) stop(row, "client_aborted");
      }
      changed();
    },
    action(
      p: ProductionWorkbenchProjection,
      id: string,
      request: SemanticProposalReviewRequest,
    ) {
      const row = rowFor(id, p);
      return row ? execute(row, request, p) : Promise.resolve();
    },
    closeRow(id: string) {
      const row = rows.get(id);
      if (row) {
        stop(row, "not_issued");
        changed();
      }
    },
    refreshProjection(p: ProductionWorkbenchProjection) {
      // CRITICAL: unsolicited reads/conflicts may invalidate an exact binding but never retarget it.
      if ([...bindings.values()].some((binding) => !sameProduction(binding, p)))
        invalidateWorkspace();
      changed();
    },
    advanceProjection(
      before: ProductionWorkbenchProjection,
      after: ProductionWorkbenchProjection,
    ) {
      const advanced = advanceIdentity(before, after);
      changed();
      return advanced;
    },
    clearSensitive() {
      cancelReadBatch();
      stop(context, context.source ? "not_issued" : "unavailable");
      for (const row of rows.values())
        // IMPORTANT: navigation may clear review content, but content-free availability
        // tombstones must survive or optional no-source rows become false source failures.
        if (!(row.status === "unavailable" && row.binding === undefined))
          stop(row, "not_issued");
      planExpiry();
      changed();
    },
    resetWorkspace() {
      cancelReadBatch();
      clearRows("stale", "stale");
      planExpiry();
    },
    resetAll() {
      cancelReadBatch();
      stop(context, "unavailable", "stale");
      clearRows("unavailable", "stale");
      sources = [];
      current = undefined;
      sourceCapacity = false;
      context.source = undefined;
      context.state = initialSemanticProposalReviewState;
      planExpiry();
      changed();
    },
    dispose() {
      disposed = true;
      cancelReadBatch();
      stop(context, "client_aborted");
      clearRows("client_aborted");
      for (const token of [...timers]) cancel(token);
      sources = [];
      current = undefined;
      sourceCapacity = false;
      context.source = undefined;
      context.state = initialSemanticProposalReviewState;
    },
  });
}
