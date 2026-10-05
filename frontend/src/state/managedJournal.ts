import type { QueueSeamObservation } from "../host/queueSeam";
import type { ShellState } from "./shellState";
import type { SurroundingsDiffReport } from "../host/surroundingsDiff";
import {
  APP_MODE_EFFECT_OWNERS,
  APP_MODE_EFFECT_TYPES,
  APP_MODE_EVENT_TYPES,
  APP_MODE_STATE_VALUES,
  type AppModeEffect,
  type AppModeEvent,
  type AppModeStateValue,
} from "../lifecycle/appModeMachine";

export const MANAGED_JOURNAL_STORAGE_KEY =
  "h3-context.managed-journal.v1" as const;

const STORAGE_SCHEMA = "h3.context.managed_journal.v1" as const;
const MAX_RUNS = 4;
const MAX_ENTRIES_PER_RUN = 64;
const MAX_STORAGE_BYTES = 32 * 1024;
const MAX_PATHS_PER_BUCKET = 8;
const MAX_PATH_LENGTH = 240;
const MAX_SURROUNDINGS_CHANGES = 512;
const MAX_QUEUE_SEAM_ARITY = 16;
const SAFE_QUEUE_SEAM_FUNCTION_NAME = /^[A-Za-z_$][A-Za-z0-9_$]{0,63}$/;

export const MANAGED_DIAGNOSTIC_STAGES = [
  "bootstrap_started",
  "bootstrap_waiter_installed",
  "bootstrap_prompt_bound",
  "bootstrap_projection_buffered",
  "bootstrap_projection_resolved",
  "bootstrap_buffer_resolved",
  "bootstrap_projection_returned",
  "bootstrap_terminal_seen",
  "managed_bootstrap_returned",
  "managed_bootstrap_stale",
  "aggregate_prepare_invoked",
  "aggregate_prepare_returned",
  "candidate_host_load_before_compile",
  "refusal_reason_unknown",
  "host_lost",
  "host_reconnecting",
  "host_reconnected",
  "reconcile_started",
  "reconcile_continued",
  "reconcile_terminalized",
  "reconcile_refused",
  "reconcile_unavailable",
] as const;
export type ManagedDiagnosticStage = (typeof MANAGED_DIAGNOSTIC_STAGES)[number];

const shellStates = [
  "interactive",
  "working",
  "projected",
  "editing_setup",
  "error",
  "host_unavailable",
] as const;
const interactiveReasons = [
  "empty_canvas",
  "canvas_ready",
  "pending_capability",
  "native_preference",
  "cancelled",
  "dirty_graph",
  "ambiguous_graph",
  "incompatible_graph",
  "malformed_graph",
  "unavailable",
] as const;
const hostAvailabilityPhases = ["lost", "reconnecting"] as const;
const workingPhases = [
  "materializing",
  "compiling",
  "preparing_context",
  "queueing",
  "generating",
  "verifying_output",
] as const;
const shellErrorCodes = [
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
] as const;
const appModeErrorCodes = [
  "incompatible_seam",
  "incompatible_graph",
  "dirty_graph",
  "invalid_request",
  "compile_failed",
  "queue_failed",
  "execution_failed",
  "execution_interrupted",
  "ambiguous_host_ownership",
  "cancelled",
  "stale_graph",
  "rollback_failed",
] as const;
const appModeSources = [
  "request",
  "seam",
  "graph",
  "compile",
  "queue",
  "transaction",
] as const;
const refusalReasons = [
  "anchor_missing",
  "connect_missing_first_frame",
  "connect_missing_last_frame",
  "generation_admission_refused",
  "queue_rejected",
  "queue_response_invalid",
  "source_image_changed",
  "template_unavailable",
] as const;
const coordinatorCategories = [
  "artifact_content_invalid",
  "artifact_locator_rejected",
  "artifact_authority_mismatch",
  "artifact_store_unavailable",
  "run_authority_mismatch",
  "unsupported_failure",
  "internal_failure",
] as const;
const capabilityNames = [
  "missing_load_graph_data",
  "missing_graph_to_prompt",
  "missing_detached_graph_constructor",
  "missing_queue_prompt",
  "missing_workflow_store",
  "missing_load_api_json",
  "profile_unavailable",
] as const;
const surroundingsNames = ["within_run", "consecutive_start"] as const;
const surroundingsBuckets = [
  "presentation",
  "host_metadata",
  "queue_wrapper",
  "user_parameter",
  "foreign_extension",
  "owned",
] as const;

type ShellStateName = (typeof shellStates)[number];
type ShellStateReason =
  | (typeof interactiveReasons)[number]
  | (typeof workingPhases)[number]
  | (typeof hostAvailabilityPhases)[number];
type ShellErrorCode = (typeof shellErrorCodes)[number];
type AppModeErrorCode = (typeof appModeErrorCodes)[number];
type AppModeSource = (typeof appModeSources)[number];
type RefusalReason = (typeof refusalReasons)[number];
type CoordinatorCategory = (typeof coordinatorCategories)[number];
type CapabilityName = (typeof capabilityNames)[number];
type SurroundingsName = (typeof surroundingsNames)[number];
type SurroundingsBucket = (typeof surroundingsBuckets)[number];

type JournalBase = Readonly<{
  seq: number;
  run: number;
  /**
   * Milliseconds since the run started (rounded to a thousandth), never
   * seconds: a `t` of 300 is 0.3 s. Misread as seconds once (2026-08-28), which
   * turned an instant refusal into a phantom hang.
   */
  t: number;
  kind:
    | "stage"
    | "state"
    | "refusal"
    | "error"
    | "capability"
    | "transition"
    | "effect"
    | "queue_seam"
    | "surroundings";
  name: string;
}>;

export type ManagedJournalEntry =
  | (JournalBase &
      Readonly<{
        kind: "stage";
        name: ManagedDiagnosticStage;
      }>)
  | (JournalBase &
      Readonly<{
        kind: "state";
        name: ShellStateName;
        code?: ShellErrorCode;
        reason?: ShellStateReason;
      }>)
  | (JournalBase &
      Readonly<{
        kind: "error" | "refusal";
        name: "app_mode" | "sequence_coordinator";
        code?: AppModeErrorCode;
        reason?: RefusalReason;
        category?: AppModeSource | CoordinatorCategory;
      }>)
  | (JournalBase &
      Readonly<{
        kind: "capability";
        name: CapabilityName;
        code: "true" | "false";
      }>)
  | (JournalBase &
      Readonly<{
        kind: "transition";
        name: "app_mode";
        event: AppModeEvent["type"];
        from: AppModeStateValue;
        to: AppModeStateValue;
      }>)
  | (JournalBase &
      Readonly<{
        kind: "effect";
        name: AppModeEffect["type"];
        owner: AppModeEffect["owner"];
      }>)
  | (JournalBase &
      Readonly<{
        kind: "queue_seam";
        name: "queue_prompt";
        function_name: string;
        arity: number | null;
        changed_since_controller_creation: boolean;
      }>)
  | (JournalBase &
      Readonly<{
        kind: "surroundings";
        name: SurroundingsName;
        counts: Readonly<Record<SurroundingsBucket, number>>;
        paths: Readonly<Record<SurroundingsBucket, readonly string[]>>;
      }>);

export type ManagedJournalStorage = Readonly<{
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}>;

export type ManagedDiagnosticsMetadata = Readonly<{
  packageVersion: string;
  locale: unknown;
  hostFrontendVersion?: string;
  buildProvenance?: Readonly<{
    sourceCommit: string;
    bundleSha256: string;
    bundleMatchesRecord: boolean;
  }>;
  fingerprints?: Readonly<{
    report?: string;
    prompt?: string;
    basePrompt?: string;
    currentPrompt?: string;
  }>;
}>;

type JournalOptions = Readonly<{ now?: () => number }>;
type ManagedJournalDraft = ManagedJournalEntry extends infer Entry
  ? Entry extends ManagedJournalEntry
    ? Omit<Entry, "seq" | "run" | "t">
    : never
  : never;

const stageSet = new Set<string>(MANAGED_DIAGNOSTIC_STAGES);
const shellStateSet = new Set<string>(shellStates);
const interactiveReasonSet = new Set<string>(interactiveReasons);
const workingPhaseSet = new Set<string>(workingPhases);
const shellErrorCodeSet = new Set<string>(shellErrorCodes);
const appModeErrorCodeSet = new Set<string>(appModeErrorCodes);
const appModeSourceSet = new Set<string>(appModeSources);
const refusalReasonSet = new Set<string>(refusalReasons);
const coordinatorCategorySet = new Set<string>(coordinatorCategories);
const capabilityNameSet = new Set<string>(capabilityNames);
const appModeStateSet = new Set<string>(APP_MODE_STATE_VALUES);
const appModeEventSet = new Set<string>(APP_MODE_EVENT_TYPES);
const appModeEffectSet = new Set<string>(APP_MODE_EFFECT_TYPES);
const appModeEffectOwnerSet = new Set<string>(APP_MODE_EFFECT_OWNERS);
const surroundingsNameSet = new Set<string>(surroundingsNames);
const surroundingsBucketSet = new Set<string>(surroundingsBuckets);

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function exactKeys(
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[] = [],
): boolean {
  const keys = Object.keys(value);
  const allowed = new Set([...required, ...optional]);
  return (
    required.every((key) => Object.hasOwn(value, key)) &&
    keys.every((key) => allowed.has(key))
  );
}

function member<T extends string>(
  values: ReadonlySet<string>,
  value: unknown,
): T | undefined {
  return typeof value === "string" && values.has(value)
    ? (value as T)
    : undefined;
}

function safeRun(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) > 0;
}

function safeNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function safeQueueSeamFunctionName(value: unknown): value is string {
  return typeof value === "string" && SAFE_QUEUE_SEAM_FUNCTION_NAME.test(value);
}

function safeQueueSeamArity(value: unknown): value is number | null {
  return (
    value === null ||
    (Number.isSafeInteger(value) &&
      Number(value) >= 0 &&
      Number(value) <= MAX_QUEUE_SEAM_ARITY)
  );
}

const surroundingsPathFields = new Set([
  "id",
  "revision",
  "last_node_id",
  "last_link_id",
  "groups",
  "config",
  "extra",
  "version",
  "definitions",
  "floatingLinks",
  "frontendVersion",
  "ds",
  "pos",
  "size",
  "flags",
  "order",
  "mode",
  "type",
  "inputs",
  "outputs",
  "link",
  "widgets_values",
  "properties",
  "cnr_id",
  "ver",
  "aux_id",
  "widget_idx_map",
  "seed_widgets",
  "scale",
  "offset",
  "length",
]);

function safePath(value: unknown): value is string {
  if (
    typeof value !== "string" ||
    !value.startsWith("$graph") ||
    value.length > MAX_PATH_LENGTH ||
    /[\u0000-\u001f\u007f\\/]/.test(value)
  )
    return false;
  let remainder = value.slice("$graph".length);
  const selector = remainder.match(
    /^\.(?:nodes|links)\[id=(?:\d{1,20}|<redacted>)\]/,
  )?.[0];
  if (selector !== undefined) remainder = remainder.slice(selector.length);
  while (remainder.length > 0) {
    if (remainder.startsWith('["Node name for S&R"]')) {
      remainder = remainder.slice('["Node name for S&R"]'.length);
      continue;
    }
    const index = remainder.match(/^\[\d+\]/)?.[0];
    if (index !== undefined) {
      remainder = remainder.slice(index.length);
      continue;
    }
    const field = remainder.match(/^\.([A-Za-z_][A-Za-z0-9_]*|<name>)/);
    if (
      field === null ||
      (field[1] !== "<name>" && !surroundingsPathFields.has(field[1]!))
    )
      return false;
    remainder = remainder.slice(field[0].length);
  }
  return true;
}

function cloneSurroundings(
  report: unknown,
):
  | Pick<
      Extract<ManagedJournalEntry, { kind: "surroundings" }>,
      "counts" | "paths"
    >
  | undefined {
  const value = record(report);
  if (
    value === undefined ||
    !exactKeys(value, ["schema", "total", "counts", "paths"]) ||
    value.schema !== "h3.context.surroundings_diff_evidence.v1" ||
    !safeNumber(value.total) ||
    value.total > MAX_SURROUNDINGS_CHANGES
  )
    return undefined;
  const rawCounts = record(value.counts);
  const rawPaths = record(value.paths);
  if (
    rawCounts === undefined ||
    rawPaths === undefined ||
    Object.keys(rawCounts).length !== surroundingsBuckets.length ||
    Object.keys(rawPaths).length !== surroundingsBuckets.length ||
    !Object.keys(rawCounts).every((key) => surroundingsBucketSet.has(key)) ||
    !Object.keys(rawPaths).every((key) => surroundingsBucketSet.has(key))
  )
    return undefined;
  const counts = {} as Record<SurroundingsBucket, number>;
  const paths = {} as Record<SurroundingsBucket, readonly string[]>;
  let counted = 0;
  for (const bucket of surroundingsBuckets) {
    const count = rawCounts[bucket];
    const bucketPaths = rawPaths[bucket];
    if (
      !Number.isSafeInteger(count) ||
      Number(count) < 0 ||
      Number(count) > MAX_SURROUNDINGS_CHANGES ||
      !Array.isArray(bucketPaths) ||
      !bucketPaths.every(safePath)
    )
      return undefined;
    counts[bucket] = Number(count);
    counted += Number(count);
    paths[bucket] = Object.freeze(
      [...new Set(bucketPaths)].sort().slice(0, MAX_PATHS_PER_BUCKET),
    );
  }
  if (counted !== value.total) return undefined;
  return {
    counts: Object.freeze(counts),
    paths: Object.freeze(paths),
  };
}

function freezeEntry(entry: ManagedJournalEntry): ManagedJournalEntry {
  if (entry.kind === "surroundings")
    return Object.freeze({
      ...entry,
      counts: Object.freeze({ ...entry.counts }),
      paths: Object.freeze(
        Object.fromEntries(
          surroundingsBuckets.map((bucket) => [
            bucket,
            Object.freeze([...entry.paths[bucket]]),
          ]),
        ) as Record<SurroundingsBucket, readonly string[]>,
      ),
    });
  return Object.freeze({ ...entry });
}

function decodeEntry(value: unknown): ManagedJournalEntry | undefined {
  const entry = record(value);
  if (
    entry === undefined ||
    !exactKeys(
      entry,
      ["seq", "run", "t", "kind", "name"],
      [
        "code",
        "reason",
        "category",
        "function_name",
        "arity",
        "changed_since_controller_creation",
        "counts",
        "paths",
        "event",
        "from",
        "to",
        "owner",
      ],
    ) ||
    !safeRun(entry.seq) ||
    !safeRun(entry.run) ||
    !safeNumber(entry.t)
  )
    return undefined;
  const base = { seq: entry.seq, run: entry.run, t: entry.t };
  if (entry.kind === "stage") {
    const name = member<ManagedDiagnosticStage>(stageSet, entry.name);
    if (
      name === undefined ||
      !exactKeys(entry, ["seq", "run", "t", "kind", "name"])
    )
      return undefined;
    return freezeEntry({ ...base, kind: "stage", name });
  }
  if (entry.kind === "state") {
    const name = member<ShellStateName>(shellStateSet, entry.name);
    const code = member<ShellErrorCode>(shellErrorCodeSet, entry.code);
    const reason = member<ShellStateReason>(
      new Set([
        ...interactiveReasons,
        ...workingPhases,
        ...hostAvailabilityPhases,
      ]),
      entry.reason,
    );
    if (
      !exactKeys(
        entry,
        ["seq", "run", "t", "kind", "name"],
        ["code", "reason"],
      ) ||
      name === undefined ||
      (entry.code !== undefined && code === undefined) ||
      (entry.reason !== undefined && reason === undefined) ||
      (name === "error" && (code === undefined || reason !== undefined)) ||
      (name === "interactive" &&
        (reason === undefined || !interactiveReasonSet.has(reason))) ||
      (name === "working" &&
        (reason === undefined || !workingPhaseSet.has(reason))) ||
      (name !== "error" && code !== undefined) ||
      (!["error", "interactive", "working"].includes(name) &&
        (code !== undefined || reason !== undefined)) ||
      entry.category !== undefined ||
      entry.counts !== undefined ||
      entry.paths !== undefined
    )
      return undefined;
    return freezeEntry({
      ...base,
      kind: "state",
      name,
      ...(code === undefined ? {} : { code }),
      ...(reason === undefined ? {} : { reason }),
    });
  }
  if (entry.kind === "error" || entry.kind === "refusal") {
    if (entry.name === "sequence_coordinator") {
      const category = member<CoordinatorCategory>(
        coordinatorCategorySet,
        entry.category,
      );
      if (
        !exactKeys(entry, ["seq", "run", "t", "kind", "name", "category"]) ||
        entry.kind !== "error" ||
        category === undefined ||
        entry.code !== undefined ||
        entry.reason !== undefined ||
        entry.counts !== undefined ||
        entry.paths !== undefined
      )
        return undefined;
      return freezeEntry({
        ...base,
        kind: "error",
        name: "sequence_coordinator",
        category,
      });
    }
    if (entry.name !== "app_mode") return undefined;
    const code = member<AppModeErrorCode>(appModeErrorCodeSet, entry.code);
    const category = member<AppModeSource>(appModeSourceSet, entry.category);
    const reason = member<RefusalReason>(refusalReasonSet, entry.reason);
    if (
      !exactKeys(
        entry,
        ["seq", "run", "t", "kind", "name", "code", "category"],
        ["reason"],
      ) ||
      code === undefined ||
      category === undefined ||
      (entry.reason !== undefined && reason === undefined) ||
      entry.kind !== (reason === undefined ? "error" : "refusal") ||
      entry.counts !== undefined ||
      entry.paths !== undefined
    )
      return undefined;
    return freezeEntry({
      ...base,
      kind: entry.kind,
      name: "app_mode",
      code,
      category,
      ...(reason === undefined ? {} : { reason }),
    });
  }
  if (entry.kind === "capability") {
    const name = member<CapabilityName>(capabilityNameSet, entry.name);
    if (
      !exactKeys(entry, ["seq", "run", "t", "kind", "name", "code"]) ||
      name === undefined ||
      (entry.code !== "true" && entry.code !== "false") ||
      entry.reason !== undefined ||
      entry.category !== undefined ||
      entry.counts !== undefined ||
      entry.paths !== undefined
    )
      return undefined;
    return freezeEntry({
      ...base,
      kind: "capability",
      name,
      code: entry.code,
    });
  }
  if (entry.kind === "transition") {
    const event = member<AppModeEvent["type"]>(appModeEventSet, entry.event);
    const from = member<AppModeStateValue>(appModeStateSet, entry.from);
    const to = member<AppModeStateValue>(appModeStateSet, entry.to);
    if (
      !exactKeys(entry, [
        "seq",
        "run",
        "t",
        "kind",
        "name",
        "event",
        "from",
        "to",
      ]) ||
      entry.name !== "app_mode" ||
      event === undefined ||
      from === undefined ||
      to === undefined
    )
      return undefined;
    return freezeEntry({
      ...base,
      kind: "transition",
      name: "app_mode",
      event,
      from,
      to,
    });
  }
  if (entry.kind === "effect") {
    const name = member<AppModeEffect["type"]>(appModeEffectSet, entry.name);
    const owner = member<AppModeEffect["owner"]>(
      appModeEffectOwnerSet,
      entry.owner,
    );
    if (
      !exactKeys(entry, ["seq", "run", "t", "kind", "name", "owner"]) ||
      name === undefined ||
      owner === undefined
    )
      return undefined;
    return freezeEntry({ ...base, kind: "effect", name, owner });
  }
  if (entry.kind === "queue_seam") {
    if (
      !exactKeys(entry, [
        "seq",
        "run",
        "t",
        "kind",
        "name",
        "function_name",
        "arity",
        "changed_since_controller_creation",
      ]) ||
      entry.name !== "queue_prompt" ||
      !safeQueueSeamFunctionName(entry.function_name) ||
      !safeQueueSeamArity(entry.arity) ||
      typeof entry.changed_since_controller_creation !== "boolean"
    )
      return undefined;
    return freezeEntry({
      ...base,
      kind: "queue_seam",
      name: "queue_prompt",
      function_name: entry.function_name,
      arity: entry.arity,
      changed_since_controller_creation:
        entry.changed_since_controller_creation,
    });
  }
  if (entry.kind === "surroundings") {
    const name = member<SurroundingsName>(surroundingsNameSet, entry.name);
    const report = cloneSurroundings({
      schema: "h3.context.surroundings_diff_evidence.v1",
      total:
        record(entry.counts) === undefined
          ? -1
          : surroundingsBuckets.reduce(
              (sum, bucket) => sum + Number(record(entry.counts)?.[bucket]),
              0,
            ),
      counts: entry.counts,
      paths: entry.paths,
    });
    if (
      !exactKeys(entry, [
        "seq",
        "run",
        "t",
        "kind",
        "name",
        "counts",
        "paths",
      ]) ||
      name === undefined ||
      report === undefined ||
      entry.code !== undefined ||
      entry.reason !== undefined ||
      entry.category !== undefined
    )
      return undefined;
    return freezeEntry({
      ...base,
      kind: "surroundings",
      name,
      ...report,
    });
  }
  return undefined;
}

function byteLength(text: string): number {
  return new TextEncoder().encode(text).byteLength;
}

function safeVersion(value: unknown): string | undefined {
  return typeof value === "string" &&
    /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/.test(value)
    ? value
    : undefined;
}

function safeFingerprint(value: unknown): string | undefined {
  return typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value)
    ? value
    : undefined;
}

function safeCommit(value: unknown): string | undefined {
  return typeof value === "string" && /^[0-9a-f]{40}$/.test(value)
    ? value
    : undefined;
}

export function createManagedJournal(options: JournalOptions = {}) {
  const now = options.now ?? (() => performance.now());
  let entries: ManagedJournalEntry[] = [];
  let sequence = 0;
  let initialized = false;
  let storage: ManagedJournalStorage | undefined;
  const runStarts = new Map<number, number>();
  const runTimes = new Map<number, number>();

  const persist = (): void => {
    if (storage === undefined) return;
    try {
      let mirror = entries;
      let text = JSON.stringify({ schema: STORAGE_SCHEMA, entries: mirror });
      while (mirror.length > 0 && byteLength(text) > MAX_STORAGE_BYTES) {
        mirror = mirror.slice(1);
        text = JSON.stringify({ schema: STORAGE_SCHEMA, entries: mirror });
      }
      if (byteLength(text) <= MAX_STORAGE_BYTES)
        storage.setItem(MANAGED_JOURNAL_STORAGE_KEY, text);
    } catch {
      // CRITICAL: diagnostics are never allowed to break the managed run path.
    }
  };

  const enforceBounds = (): void => {
    const perRun = new Map<number, number>();
    const keep = new Array(entries.length).fill(true) as boolean[];
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      const run = entries[index]!.run;
      const count = perRun.get(run) ?? 0;
      if (count >= MAX_ENTRIES_PER_RUN) keep[index] = false;
      else perRun.set(run, count + 1);
    }
    entries = entries.filter((_, index) => keep[index]);
    const retainedRuns = new Set<number>();
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      retainedRuns.add(entries[index]!.run);
      if (retainedRuns.size === MAX_RUNS) break;
    }
    entries = entries.filter((entry) => retainedRuns.has(entry.run));
    for (const run of runStarts.keys())
      if (!retainedRuns.has(run)) runStarts.delete(run);
    for (const run of runTimes.keys())
      if (!retainedRuns.has(run)) runTimes.delete(run);
  };

  const relativeTime = (run: number): number => {
    const current = now();
    const start = runStarts.get(run) ?? current;
    if (!runStarts.has(run)) runStarts.set(run, start);
    const measured = Number.isFinite(current)
      ? Math.max(0, Math.round((current - start) * 1000) / 1000)
      : 0;
    const monotonic = Math.max(runTimes.get(run) ?? 0, measured);
    runTimes.set(run, monotonic);
    return monotonic;
  };

  const insert = (run: number, entry: ManagedJournalDraft): void => {
    try {
      if (!safeRun(run)) return;
      if (entry.kind === "surroundings")
        entries = entries.filter(
          (current) =>
            !(
              current.run === run &&
              current.kind === "surroundings" &&
              current.name === entry.name
            ),
        );
      sequence += 1;
      const decoded = decodeEntry({
        ...entry,
        seq: sequence,
        run,
        t: relativeTime(run),
      });
      if (decoded === undefined) return;
      entries.push(decoded);
      enforceBounds();
      persist();
    } catch {
      // CRITICAL: a broken diagnostics input remains a no-op, never a run failure.
    }
  };

  return Object.freeze({
    initialize(nextStorage?: ManagedJournalStorage): void {
      if (initialized) return;
      initialized = true;
      storage = nextStorage;
      if (storage === undefined) return;
      try {
        const text = storage.getItem(MANAGED_JOURNAL_STORAGE_KEY);
        if (text === null) return;
        if (byteLength(text) > MAX_STORAGE_BYTES) throw new Error("oversized");
        const wire = record(JSON.parse(text));
        if (
          wire === undefined ||
          !exactKeys(wire, ["schema", "entries"]) ||
          wire.schema !== STORAGE_SCHEMA ||
          !Array.isArray(wire.entries)
        )
          throw new Error("invalid");
        const decoded = wire.entries.map(decodeEntry);
        if (decoded.some((entry) => entry === undefined))
          throw new Error("invalid entry");
        entries = decoded as ManagedJournalEntry[];
        enforceBounds();
        sequence = entries.reduce(
          (maximum, entry) => Math.max(maximum, entry.seq),
          0,
        );
      } catch {
        entries = [];
        sequence = 0;
        try {
          storage.removeItem(MANAGED_JOURNAL_STORAGE_KEY);
        } catch {
          // The in-memory journal remains available even when cleanup is blocked.
        }
      }
    },
    beginRun(run: number): void {
      try {
        if (!safeRun(run)) return;
        const current = now();
        runStarts.set(run, Number.isFinite(current) ? current : 0);
        runTimes.set(run, 0);
      } catch {
        runStarts.set(run, 0);
        runTimes.set(run, 0);
      }
    },
    recordStage(run: number, stage: ManagedDiagnosticStage): void {
      const name = member<ManagedDiagnosticStage>(stageSet, stage);
      if (name !== undefined) insert(run, { kind: "stage", name });
    },
    recordState(
      run: number,
      value: Readonly<{
        name: ShellStateName;
        code?: ShellErrorCode;
        reason?: ShellStateReason;
      }>,
    ): void {
      const wire = record(value);
      if (wire === undefined || !exactKeys(wire, ["name"], ["code", "reason"]))
        return;
      const name = member<ShellStateName>(shellStateSet, wire.name);
      const code = member<ShellErrorCode>(shellErrorCodeSet, wire.code);
      const reason = member<ShellStateReason>(
        new Set([
          ...interactiveReasons,
          ...workingPhases,
          ...hostAvailabilityPhases,
        ]),
        wire.reason,
      );
      if (
        name === undefined ||
        (wire.code !== undefined && code === undefined) ||
        (wire.reason !== undefined && reason === undefined) ||
        (name === "error" && code === undefined) ||
        (name === "interactive" &&
          (reason === undefined || !interactiveReasonSet.has(reason))) ||
        (name === "working" &&
          (reason === undefined || !workingPhaseSet.has(reason))) ||
        (name !== "error" && code !== undefined) ||
        (!["error", "interactive", "working"].includes(name) &&
          (code !== undefined || reason !== undefined))
      )
        return;
      insert(run, {
        kind: "state",
        name,
        ...(code === undefined ? {} : { code }),
        ...(reason === undefined ? {} : { reason }),
      });
    },
    recordTransition(
      run: number,
      value: Readonly<{
        event: AppModeEvent["type"];
        from: AppModeStateValue;
        to: AppModeStateValue;
      }>,
    ): void {
      const wire = record(value);
      if (wire === undefined || !exactKeys(wire, ["event", "from", "to"]))
        return;
      const event = member<AppModeEvent["type"]>(appModeEventSet, wire.event);
      const from = member<AppModeStateValue>(appModeStateSet, wire.from);
      const to = member<AppModeStateValue>(appModeStateSet, wire.to);
      if (event === undefined || from === undefined || to === undefined) return;
      insert(run, { kind: "transition", name: "app_mode", event, from, to });
    },
    recordEffect(
      run: number,
      value: Readonly<{
        name: AppModeEffect["type"];
        owner: AppModeEffect["owner"];
      }>,
    ): void {
      const wire = record(value);
      if (wire === undefined || !exactKeys(wire, ["name", "owner"])) return;
      const name = member<AppModeEffect["type"]>(appModeEffectSet, wire.name);
      const owner = member<AppModeEffect["owner"]>(
        appModeEffectOwnerSet,
        wire.owner,
      );
      if (name === undefined || owner === undefined) return;
      insert(run, { kind: "effect", name, owner });
    },
    recordAppModeError(
      run: number,
      value: Readonly<{
        code: AppModeErrorCode;
        source: AppModeSource;
        reason?: RefusalReason;
      }>,
    ): void {
      const wire = record(value);
      if (
        wire === undefined ||
        !exactKeys(wire, ["code", "source"], ["reason"])
      )
        return;
      const code = member<AppModeErrorCode>(appModeErrorCodeSet, wire.code);
      const category = member<AppModeSource>(appModeSourceSet, wire.source);
      const reason = member<RefusalReason>(refusalReasonSet, wire.reason);
      if (
        code === undefined ||
        category === undefined ||
        (wire.reason !== undefined && reason === undefined)
      )
        return;
      insert(run, {
        kind: reason === undefined ? "error" : "refusal",
        name: "app_mode",
        code,
        category,
        ...(reason === undefined ? {} : { reason }),
      });
    },
    recordCoordinatorError(run: number, value: CoordinatorCategory): void {
      const category = member<CoordinatorCategory>(
        coordinatorCategorySet,
        value,
      );
      if (category !== undefined)
        insert(run, {
          kind: "error",
          name: "sequence_coordinator",
          category,
        });
    },
    recordCapability(
      run: number,
      value: CapabilityName,
      missing: boolean,
    ): void {
      const name = member<CapabilityName>(capabilityNameSet, value);
      if (name !== undefined && typeof missing === "boolean")
        insert(run, {
          kind: "capability",
          name,
          code: missing ? "true" : "false",
        });
    },
    recordQueueSeam(run: number, value: QueueSeamObservation): void {
      const wire = record(value);
      if (
        wire === undefined ||
        !exactKeys(wire, [
          "functionName",
          "arity",
          "changedSinceControllerCreation",
        ]) ||
        !safeQueueSeamFunctionName(wire.functionName) ||
        !safeQueueSeamArity(wire.arity) ||
        typeof wire.changedSinceControllerCreation !== "boolean"
      )
        return;
      // IMPORTANT: persist only this closed observation. Function source or a
      // thrown wrapper value would leak host-extension internals into support data.
      insert(run, {
        kind: "queue_seam",
        name: "queue_prompt",
        function_name: wire.functionName,
        arity: wire.arity,
        changed_since_controller_creation: wire.changedSinceControllerCreation,
      });
    },
    recordSurroundings(
      run: number,
      value: SurroundingsName,
      report: SurroundingsDiffReport,
    ): void {
      const name = member<SurroundingsName>(surroundingsNameSet, value);
      const safe = cloneSurroundings(report);
      if (name !== undefined && safe !== undefined)
        insert(run, { kind: "surroundings", name, ...safe });
    },
    snapshot(): readonly ManagedJournalEntry[] {
      return Object.freeze(entries.map(freezeEntry));
    },
    reset(): void {
      entries = [];
      sequence = 0;
      runStarts.clear();
      runTimes.clear();
      try {
        storage?.removeItem(MANAGED_JOURNAL_STORAGE_KEY);
      } catch {
        // Reset remains complete in memory even if browser storage is blocked.
      }
    },
    compose(metadata: ManagedDiagnosticsMetadata): string {
      const version = safeVersion(metadata.packageVersion) ?? "unknown";
      const locale = ["en", "zh-TW", "zh-CN"].includes(String(metadata.locale))
        ? String(metadata.locale)
        : "unknown";
      const host = safeVersion(metadata.hostFrontendVersion) ?? "unavailable";
      const sourceCommit = safeCommit(metadata.buildProvenance?.sourceCommit);
      const bundleSha256 = safeFingerprint(
        metadata.buildProvenance?.bundleSha256,
      );
      const lines = [
        "H3 Context managed diagnostics",
        `package=${version}`,
        `locale=${locale}`,
        `host_frontend=${host}`,
        `build.commit=${sourceCommit ?? "unavailable"}`,
        `build.bundle=${bundleSha256 ?? "unavailable"}`,
        `build.bundle_matches_record=${
          sourceCommit !== undefined &&
          bundleSha256 !== undefined &&
          typeof metadata.buildProvenance?.bundleMatchesRecord === "boolean"
            ? String(metadata.buildProvenance.bundleMatchesRecord)
            : "unavailable"
        }`,
      ];
      const fingerprintRows = [
        ["report", metadata.fingerprints?.report],
        ["prompt", metadata.fingerprints?.prompt],
        ["base_prompt", metadata.fingerprints?.basePrompt],
        ["current_prompt", metadata.fingerprints?.currentPrompt],
      ] as const;
      for (const [name, value] of fingerprintRows) {
        const safe = safeFingerprint(value);
        lines.push(`fingerprint.${name}=${safe ?? "unavailable"}`);
      }
      lines.push("entries:");
      for (const entry of entries) {
        let line = `${entry.seq} run=${entry.run} t=${entry.t} ${entry.kind} ${entry.name}`;
        if ("code" in entry && entry.code !== undefined)
          line += ` code=${entry.code}`;
        if ("reason" in entry && entry.reason !== undefined)
          line += ` reason=${entry.reason}`;
        if ("category" in entry && entry.category !== undefined)
          line += ` category=${entry.category}`;
        if (entry.kind === "transition")
          line += ` event=${entry.event} from=${entry.from} to=${entry.to}`;
        if (entry.kind === "effect") line += ` owner=${entry.owner}`;
        if (entry.kind === "queue_seam")
          line += ` function_name=${entry.function_name} arity=${entry.arity ?? "unknown"} changed_since_controller_creation=${entry.changed_since_controller_creation}`;
        lines.push(line);
        if (entry.kind === "surroundings")
          for (const bucket of surroundingsBuckets) {
            lines.push(
              `  ${bucket}=${entry.counts[bucket]} paths=${entry.paths[bucket].join(",") || "none"}`,
            );
          }
      }
      return lines.join("\n");
    },
  });
}

export const managedJournal = createManagedJournal();

/**
 * The identity a shell state has for diagnostic purposes.
 *
 * CRITICAL: the recorder drops a state whose signature repeats, so every distinguishing field of a
 * state has to appear here. `host_unavailable` carries its phase, and ComfyUI's ordinary sequence
 * is `status(null)` then `reconnecting` -- signing on the status name alone would deduplicate the
 * reconnect attempt away and leave the journal claiming the host never tried to come back.
 */
export function diagnosticShellStateSignature(state: ShellState): string {
  if (state.status === "working") return `${state.status}:${state.phase}`;
  if (state.status === "interactive") return `${state.status}:${state.reason}`;
  if (state.status === "error") return `${state.status}:${state.code}`;
  if (state.status === "host_unavailable")
    return `${state.status}:${state.phase}`;
  return state.status;
}
