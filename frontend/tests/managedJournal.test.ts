import { describe, expect, it } from "vitest";

import { initialShellState, reduceShellState } from "../src/state/shellState";
import { diagnosticShellStateSignature } from "../src/state/managedJournal";
import {
  MANAGED_JOURNAL_STORAGE_KEY,
  createManagedJournal,
  type ManagedJournalStorage,
} from "../src/state/managedJournal";

function memoryStorage(): ManagedJournalStorage & {
  values: Map<string, string>;
} {
  const values = new Map<string, string>();
  return {
    values,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => void values.delete(key),
  };
}

describe("M23-21 managed-run journal", () => {
  it("round-trips only closed statechart transition and effect names", () => {
    const storage = memoryStorage();
    const journal = createManagedJournal({ now: () => 5 });
    journal.initialize(storage);
    journal.beginRun(8);
    journal.recordTransition(8, {
      event: "START",
      from: "idle",
      to: "census",
    });
    journal.recordEffect(8, {
      name: "readProjection",
      owner: "AppModeQualification",
    });
    (journal.recordTransition as (run: number, value: unknown) => void)(8, {
      event: "synthetic private prompt",
      from: "idle",
      to: "census",
      payload: "must not persist",
    });

    expect(journal.snapshot()).toMatchObject([
      { kind: "transition", event: "START", from: "idle", to: "census" },
      {
        kind: "effect",
        name: "readProjection",
        owner: "AppModeQualification",
      },
    ]);

    const restored = createManagedJournal({ now: () => 6 });
    restored.initialize(storage);
    expect(restored.snapshot()).toEqual(journal.snapshot());
  });

  it("keeps 64 entries per run and only the four newest runs", () => {
    const journal = createManagedJournal({ now: () => 100 });
    journal.beginRun(1);
    for (let index = 0; index < 65; index += 1)
      journal.recordStage(1, "bootstrap_started");

    expect(journal.snapshot()).toHaveLength(64);
    expect(journal.snapshot()[0]?.seq).toBe(2);

    journal.reset();
    for (let run = 1; run <= 5; run += 1) {
      journal.beginRun(run);
      journal.recordStage(run, "bootstrap_started");
    }
    expect([...new Set(journal.snapshot().map((entry) => entry.run))]).toEqual([
      2, 3, 4, 5,
    ]);
  });

  it("uses non-decreasing time relative to each run rather than wall clock", () => {
    let now = 1_000;
    const journal = createManagedJournal({ now: () => now });
    journal.beginRun(7);
    now = 1_005;
    journal.recordStage(7, "bootstrap_started");
    now = 1_003;
    journal.recordStage(7, "bootstrap_waiter_installed");
    now = 1_020;
    journal.recordStage(7, "bootstrap_prompt_bound");

    expect(journal.snapshot().map((entry) => entry.t)).toEqual([5, 5, 20]);
  });

  it("evicts the oldest persisted entries before the mirror exceeds 32 KiB", () => {
    const storage = memoryStorage();
    const journal = createManagedJournal({ now: () => 0 });
    const buckets = [
      "presentation",
      "host_metadata",
      "queue_wrapper",
      "user_parameter",
      "foreign_extension",
      "owned",
    ] as const;
    const paths = Object.fromEntries(
      buckets.map((bucket, bucketIndex) => [
        bucket,
        Array.from(
          { length: 8 },
          (_, pathIndex) =>
            `$graph.extra${".<name>".repeat(24)}[${bucketIndex * 8 + pathIndex}]`,
        ),
      ]),
    ) as Record<(typeof buckets)[number], string[]>;
    const counts = Object.fromEntries(
      buckets.map((bucket) => [bucket, 8]),
    ) as Record<(typeof buckets)[number], number>;

    journal.initialize(storage);
    for (let run = 1; run <= 4; run += 1) {
      journal.beginRun(run);
      for (const name of ["within_run", "consecutive_start"] as const)
        journal.recordSurroundings(run, name, {
          schema: "h3.context.surroundings_diff_evidence.v1",
          total: 48,
          counts,
          paths,
        });
    }

    const serialized = storage.values.get(MANAGED_JOURNAL_STORAGE_KEY) ?? "";
    const persisted = JSON.parse(serialized) as { entries: unknown[] };
    expect(new TextEncoder().encode(serialized).byteLength).toBeLessThanOrEqual(
      32 * 1024,
    );
    expect(persisted.entries.length).toBeLessThan(journal.snapshot().length);
  });

  it("round-trips one bounded mirror and resets corrupt storage", () => {
    const storage = memoryStorage();
    const writer = createManagedJournal({ now: () => 20 });
    writer.initialize(storage);
    writer.beginRun(1);
    writer.recordState(1, { name: "working", reason: "materializing" });

    const serialized = storage.values.get(MANAGED_JOURNAL_STORAGE_KEY);
    expect(serialized).toBeDefined();
    expect(new TextEncoder().encode(serialized).byteLength).toBeLessThanOrEqual(
      32 * 1024,
    );

    const reader = createManagedJournal({ now: () => 30 });
    reader.initialize(storage);
    expect(reader.snapshot()).toEqual(writer.snapshot());

    storage.values.set(MANAGED_JOURNAL_STORAGE_KEY, "{not-json");
    const corrupt = createManagedJournal({ now: () => 40 });
    expect(() => corrupt.initialize(storage)).not.toThrow();
    expect(corrupt.snapshot()).toEqual([]);
    expect(storage.values.has(MANAGED_JOURNAL_STORAGE_KEY)).toBe(false);
  });

  it("round-trips and composes only the bounded queue seam observation", () => {
    const storage = memoryStorage();
    const writer = createManagedJournal({ now: () => 20 });
    writer.initialize(storage);
    writer.beginRun(1);
    writer.recordQueueSeam(1, {
      functionName: "queuePrompt",
      arity: 2,
      changedSinceControllerCreation: true,
    });
    writer.recordAppModeError(1, {
      code: "queue_failed",
      source: "queue",
      reason: "queue_rejected",
    });

    expect(writer.snapshot()).toEqual([
      {
        seq: 1,
        run: 1,
        t: 0,
        kind: "queue_seam",
        name: "queue_prompt",
        function_name: "queuePrompt",
        arity: 2,
        changed_since_controller_creation: true,
      },
      {
        seq: 2,
        run: 1,
        t: 0,
        kind: "refusal",
        name: "app_mode",
        code: "queue_failed",
        category: "queue",
        reason: "queue_rejected",
      },
    ]);

    const reader = createManagedJournal({ now: () => 30 });
    reader.initialize(storage);
    expect(reader.snapshot()).toEqual(writer.snapshot());
    expect(
      reader.compose({
        packageVersion: "1.0.0",
        locale: "en",
        hostFrontendVersion: "1.51.9",
      }),
    ).toContain(
      "queue_seam queue_prompt function_name=queuePrompt arity=2 changed_since_controller_creation=true",
    );
  });

  it("rejects malformed or unbounded queue seam observations", () => {
    const storage = memoryStorage();
    storage.values.set(
      MANAGED_JOURNAL_STORAGE_KEY,
      JSON.stringify({
        schema: "h3.context.managed_journal.v1",
        entries: [
          {
            seq: 1,
            run: 1,
            t: 0,
            kind: "queue_seam",
            name: "queue_prompt",
            function_name: "queuePrompt\nprivate",
            arity: 17,
            changed_since_controller_creation: true,
          },
        ],
      }),
    );

    const reader = createManagedJournal({ now: () => 0 });
    reader.initialize(storage);
    expect(reader.snapshot()).toEqual([]);
    expect(storage.values.has(MANAGED_JOURNAL_STORAGE_KEY)).toBe(false);

    const writer = createManagedJournal({ now: () => 0 });
    writer.beginRun(1);
    writer.recordQueueSeam(1, {
      functionName: "x".repeat(65),
      arity: 17,
      changedSinceControllerCreation: false,
    });
    expect(writer.snapshot()).toEqual([]);
  });

  it("rejects impossible state and code combinations from persisted storage", () => {
    const storage = memoryStorage();
    storage.values.set(
      MANAGED_JOURNAL_STORAGE_KEY,
      JSON.stringify({
        schema: "h3.context.managed_journal.v1",
        entries: [
          {
            seq: 1,
            run: 1,
            t: 0,
            kind: "state",
            name: "interactive",
            code: "queue_failed",
            reason: "cancelled",
          },
        ],
      }),
    );

    const journal = createManagedJournal({ now: () => 0 });
    journal.initialize(storage);

    expect(journal.snapshot()).toEqual([]);
    expect(storage.values.has(MANAGED_JOURNAL_STORAGE_KEY)).toBe(false);
  });

  it("never lets unavailable or throwing browser storage affect a run", () => {
    const journal = createManagedJournal({ now: () => 0 });
    expect(() => journal.initialize(undefined)).not.toThrow();
    journal.beginRun(1);
    expect(() => journal.recordStage(1, "bootstrap_started")).not.toThrow();

    const throwing: ManagedJournalStorage = {
      getItem: () => {
        throw new DOMException("synthetic blocked storage", "SecurityError");
      },
      setItem: () => {
        throw new DOMException("synthetic quota", "QuotaExceededError");
      },
      removeItem: () => {
        throw new DOMException("synthetic blocked storage", "SecurityError");
      },
    };
    const guarded = createManagedJournal({ now: () => 0 });
    expect(() => guarded.initialize(throwing)).not.toThrow();
    guarded.beginRun(1);
    expect(() => guarded.recordStage(1, "bootstrap_started")).not.toThrow();
    expect(guarded.snapshot()).toHaveLength(1);
  });
});

describe("diagnostic shell state signature", () => {
  const working = reduceShellState(initialShellState, {
    type: "working",
    phase: "generating",
    transactionId: 4,
  });
  const verifying = reduceShellState(initialShellState, {
    type: "working",
    phase: "verifying_output",
    transactionId: 4,
  });
  const failed = reduceShellState(initialShellState, {
    type: "error",
    code: "execution_failed",
    source: "coordinator",
    message: "execution_failed",
    recovery: "retry",
  });
  const refused = reduceShellState(initialShellState, {
    type: "error",
    code: "run_authority_mismatch",
    source: "coordinator",
    message: "run_authority_mismatch",
    recovery: "inspect",
  });
  const cancelled = reduceShellState(initialShellState, {
    type: "interactive",
    reason: "cancelled",
  });
  const lost = reduceShellState(working, {
    type: "host_unavailable",
    phase: "lost",
  });
  const reconnecting = reduceShellState(lost, {
    type: "host_unavailable",
    phase: "reconnecting",
  });

  it("separates the two host interruption phases so neither deduplicates the other", () => {
    expect(diagnosticShellStateSignature(lost)).not.toBe(
      diagnosticShellStateSignature(reconnecting),
    );
  });

  it("keeps every state kind that carries a distinguishing field distinct", () => {
    expect(diagnosticShellStateSignature(working)).not.toBe(
      diagnosticShellStateSignature(verifying),
    );
    expect(diagnosticShellStateSignature(cancelled)).not.toBe(
      diagnosticShellStateSignature(initialShellState),
    );
    expect(diagnosticShellStateSignature(failed)).not.toBe(
      diagnosticShellStateSignature(refused),
    );
    expect(
      diagnosticShellStateSignature({
        status: "projected",
        projection: {} as never,
      }),
    ).toBe("projected");
  });
});
