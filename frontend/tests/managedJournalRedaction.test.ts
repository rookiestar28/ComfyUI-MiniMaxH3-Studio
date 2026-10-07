import { describe, expect, it } from "vitest";

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

const forbidden = [
  "synthetic intent: fly over private garden",
  "Private Node Title",
  "family-video-secret.mp4",
  ["C:", "\\SyntheticPrivate", "\\clip.png"].join(""),
  ["", "synthetic", "private", "clip.png"].join("/"),
  `${["https:", "", "private.invalid", "signed"].join("/")}?${[
    "to",
    "ken",
  ].join("")}=secret`,
  "sk-synthetic-not-a-real-key-1234567890",
  "session_authority_synthetic_123",
  "synthetic raw Error.message",
  '{"nodes":[{"widgets_values":["private prompt"]}]}',
  "$graph.syntheticPrivatePrompt",
] as const;

describe("M23-21 managed journal redaction contract", () => {
  it("admits only closed fields and keeps forbidden classes out of memory, storage, and copy", () => {
    const storage = memoryStorage();
    const journal = createManagedJournal({ now: () => 10 });
    journal.initialize(storage);
    journal.beginRun(1);
    journal.recordStage(1, "bootstrap_started");
    journal.recordTransition(1, {
      event: "START",
      from: "idle",
      to: "census",
    });
    journal.recordEffect(1, {
      name: "readProjection",
      owner: "AppModeQualification",
    });
    journal.recordState(1, { name: "working", reason: "materializing" });
    journal.recordAppModeError(1, {
      code: "queue_failed",
      source: "queue",
      reason: "template_unavailable",
    });
    journal.recordCoordinatorError(1, "internal_failure");
    journal.recordCapability(1, "missing_queue_prompt", false);
    journal.recordSurroundings(1, "within_run", {
      schema: "h3.context.surroundings_diff_evidence.v1",
      total: 1,
      counts: {
        presentation: 0,
        host_metadata: 1,
        queue_wrapper: 0,
        user_parameter: 0,
        foreign_extension: 0,
        owned: 0,
      },
      paths: {
        presentation: [],
        host_metadata: ["$graph.extra.frontendVersion"],
        queue_wrapper: [],
        user_parameter: [],
        foreign_extension: [],
        owned: [],
      },
    });
    journal.recordSurroundings(1, "consecutive_start", {
      schema: "h3.context.surroundings_diff_evidence.v1",
      total: 1,
      counts: {
        presentation: 0,
        host_metadata: 0,
        queue_wrapper: 0,
        user_parameter: 0,
        foreign_extension: 1,
        owned: 0,
      },
      paths: {
        presentation: [],
        host_metadata: [],
        queue_wrapper: [],
        user_parameter: [],
        foreign_extension: ["$graph.syntheticPrivatePrompt"],
        owned: [],
      },
    });

    for (const value of forbidden) {
      (journal.recordStage as (run: number, stage: string) => void)(1, value);
      (journal.recordTransition as (run: number, value: unknown) => void)(1, {
        event: value,
        from: value,
        to: value,
        payload: value,
      });
      (journal.recordEffect as (run: number, value: unknown) => void)(1, {
        name: value,
        owner: value,
        payload: value,
      });
      (journal.recordState as (run: number, value: unknown) => void)(1, {
        name: value,
        code: value,
        reason: value,
        message: value,
        path: value,
      });
      (journal.recordAppModeError as (run: number, value: unknown) => void)(1, {
        code: value,
        source: value,
        reason: value,
        message: value,
      });
      (journal.recordCoordinatorError as (run: number, value: string) => void)(
        1,
        value,
      );
      (
        journal.recordCapability as (
          run: number,
          name: string,
          missing: boolean,
        ) => void
      )(1, value, true);
    }

    const entries = journal.snapshot();
    const allowedKeys = new Set([
      "seq",
      "run",
      "t",
      "kind",
      "name",
      "code",
      "reason",
      "category",
      "counts",
      "paths",
      "event",
      "from",
      "to",
      "owner",
    ]);
    for (const entry of entries)
      expect(Object.keys(entry).every((key) => allowedKeys.has(key))).toBe(
        true,
      );

    const payload = journal.compose({
      packageVersion: "0.1.0",
      locale: "en",
      hostFrontendVersion: "1.51.9",
      buildProvenance: {
        sourceCommit: "1".repeat(40),
        bundleSha256: `sha256:${"e".repeat(64)}`,
        bundleMatchesRecord: true,
      },
      fingerprints: {
        report: `sha256:${"a".repeat(64)}`,
        prompt: `sha256:${"b".repeat(64)}`,
        basePrompt: `sha256:${"c".repeat(64)}`,
        currentPrompt: `sha256:${"d".repeat(64)}`,
      },
    });
    const storagePayload =
      storage.values.get(MANAGED_JOURNAL_STORAGE_KEY) ?? "";
    const all = `${JSON.stringify(entries)}\n${storagePayload}\n${payload}`;
    for (const value of forbidden) expect(all).not.toContain(value);
    expect(payload).toContain("stage bootstrap_started");
    expect(payload).toContain("refusal app_mode code=queue_failed");
    expect(payload).toContain("category=internal_failure");
    expect(payload).toContain("host_frontend=1.51.9");
    expect(payload).toContain(`build.commit=${"1".repeat(40)}`);
    expect(payload).toContain(`build.bundle=sha256:${"e".repeat(64)}`);
    expect(payload).toContain("build.bundle_matches_record=true");
  });
});
