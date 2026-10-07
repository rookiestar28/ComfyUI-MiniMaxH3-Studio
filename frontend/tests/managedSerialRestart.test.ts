// B-M1605-SEQ-02: a start refused before any parent sequence exists must not poison the page
// session. The runner marks itself started before its first request, so reusing that runner for
// the next explicit Generate answered `already_started` until the page was reloaded.

import { describe, expect, it, vi } from "vitest";

import {
  ManagedSequenceClientError,
  type ManagedSequenceMutationResult,
  type ManagedSerialSequenceStartIntent,
} from "../src/host/managedSequence";
import { createManagedSerialSession } from "../src/lifecycle/managedSerialSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";

const fp = (digit: string) => `sha256:${digit.repeat(64)}`;

const intent: ManagedSerialSequenceStartIntent = {
  authorization: {
    workspace_handle: `pw_${"w".repeat(40)}`,
    expected_workspace_revision: 1,
    expected_workspace_fingerprint: fp("1"),
    expected_plan_fingerprint: fp("2"),
    generation_plan_fingerprint: fp("3"),
    compiler_fingerprint: fp("4"),
    host_capability_fingerprint: fp("5"),
    explicit_intent: "generate_approved_sequence",
  },
  qualificationFingerprint: fp("6"),
  segmentIds: ["segment.1"],
};

function subject(
  send: (
    requestId: string,
    action: string,
  ) => Promise<ManagedSequenceMutationResult>,
) {
  const parentClient = { send: vi.fn(send), read: vi.fn() };
  const runtime = {
    session: createShellSession(),
    deps: {
      managedSequenceClient: parentClient,
      sequenceCoordinator: { send: vi.fn() },
      managedSequenceReattachStore: {
        read: vi.fn(() => undefined),
        replace: vi.fn(() => true),
        clear: vi.fn(),
      },
      comfyPromptHistoryClient: { read: vi.fn() },
    },
    actions: {},
  } as unknown as ShellRuntime;
  const serial = createManagedSerialSession(runtime, vi.fn());
  const bindings = { resolveChild: vi.fn() };
  const authorizations = () =>
    parentClient.send.mock.calls.filter(
      ([, action]) => action === "authorize_sequence",
    ).length;
  return { serial, bindings, authorizations };
}

describe("B-M1605-SEQ-02 managed serial restart", () => {
  it("starts again with a fresh runner after a start refused before any parent existed", async () => {
    const ctx = subject(async () => {
      throw new ManagedSequenceClientError("managed_sequence_rejected", 409);
    });
    await expect(
      ctx.serial.startManagedSerialSequence(intent, ctx.bindings),
    ).rejects.toMatchObject({ code: "managed_sequence_rejected" });
    expect(ctx.serial.managedSerialSequenceSnapshot()).toMatchObject({
      parentState: null,
      failure: "managed_sequence_rejected",
    });
    await expect(
      ctx.serial.startManagedSerialSequence(intent, ctx.bindings),
    ).rejects.toMatchObject({ code: "managed_sequence_rejected" });
    expect(ctx.authorizations()).toBe(2);
  });

  it("keeps a start that is still in flight exclusive", async () => {
    let release: (() => void) | undefined;
    const ctx = subject(
      () =>
        new Promise((_resolve, reject) => {
          release = () =>
            reject(
              new ManagedSequenceClientError("managed_sequence_rejected", 409),
            );
        }),
    );
    const first = ctx.serial.startManagedSerialSequence(intent, ctx.bindings);
    const second = ctx.serial.startManagedSerialSequence(intent, ctx.bindings);
    await vi.waitFor(() => expect(release).toBeDefined());
    release!();
    await expect(first).rejects.toMatchObject({
      code: "managed_sequence_rejected",
    });
    await expect(second).rejects.toMatchObject({ code: "already_started" });
    expect(ctx.authorizations()).toBe(1);
  });
});
