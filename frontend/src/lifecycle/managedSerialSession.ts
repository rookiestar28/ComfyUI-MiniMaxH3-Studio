import type {
  ManagedSerialAppModeStart,
  ManagedSerialResolveChild,
  ManagedSerialResolveReuse,
  ManagedSerialSequenceStartIntent,
} from "../host/managedSequence";
import {
  ManagedSequenceClientError,
  ManagedSerialSequenceRunnerError,
  createManagedSerialSequenceRunner,
} from "../host/managedSequence";
import { ComfyPromptHistoryError } from "../host/comfyPromptHistory";
import { SequenceCoordinatorClientError } from "../host/sequenceCoordinator";
import type {
  ExecutionTerminalEvent,
  SaveVideoArtifactEvent,
} from "../host/sidebarHost";
import type { ShellRuntime } from "./shellSession";

export type ManagedSerialSequenceBindings = Readonly<{
  resolveChild: ManagedSerialResolveChild;
  resolveReuse?: ManagedSerialResolveReuse;
}>;

export function createManagedSerialSession(
  ctx: ShellRuntime,
  startChild: ManagedSerialAppModeStart,
) {
  const { session, deps, actions } = ctx;
  type SerialRunner = ReturnType<typeof createManagedSerialSequenceRunner>;
  let managedSerialRunner: SerialRunner | undefined;
  let managedSerialFailure: string | null = null;
  let managedSerialDetachRequested = true;
  let managedSerialStarts = 0;
  const managedSerialTasks = new Set<Promise<void>>();

  function safeManagedSerialFailure(error: unknown): string {
    return error instanceof ManagedSerialSequenceRunnerError ||
      error instanceof ManagedSequenceClientError ||
      error instanceof SequenceCoordinatorClientError ||
      error instanceof ComfyPromptHistoryError
      ? error.code
      : "managed_sequence_failed";
  }

  function trackManagedSerialEvent(operation: Promise<void>): void {
    let tracked: Promise<void>;
    tracked = operation
      .catch((error: unknown) => {
        managedSerialFailure = safeManagedSerialFailure(error);
      })
      .finally(() => managedSerialTasks.delete(tracked));
    managedSerialTasks.add(tracked);
  }

  function newManagedSerialRunner(
    bindings: ManagedSerialSequenceBindings,
  ): SerialRunner {
    return createManagedSerialSequenceRunner({
      parentClient: deps.managedSequenceClient,
      coordinatorClient: deps.sequenceCoordinator,
      reattachStore: deps.managedSequenceReattachStore,
      historyClient: deps.comfyPromptHistoryClient,
      startChild,
      resolveChild: bindings.resolveChild,
      resolveReuse: bindings.resolveReuse,
    });
  }

  function serialRunnerForStart(
    bindings: ManagedSerialSequenceBindings,
  ): SerialRunner {
    const current = managedSerialRunner?.snapshot();
    // IMPORTANT (B-M1605-SEQ-02): a runner marks itself started before its first request, so a
    // start refused before any parent existed (parentState still null) leaves a runner that
    // answers every later start with already_started until the page reloads. Replace it, but
    // never while a start is still in flight: that would authorize a second parent. The fresh
    // runner derives the same authorize request ID, so a parent the server did create is
    // replayed rather than duplicated.
    if (
      managedSerialRunner === undefined ||
      current?.parentState === "succeeded" ||
      current?.parentState === "cancelled" ||
      (current?.parentState === null && managedSerialStarts === 0)
    )
      managedSerialRunner = newManagedSerialRunner(bindings);
    return managedSerialRunner;
  }

  async function startManagedSerialSequence(
    intent: ManagedSerialSequenceStartIntent,
    bindings: ManagedSerialSequenceBindings,
  ): Promise<void> {
    managedSerialFailure = null;
    managedSerialDetachRequested = false;
    const runner = serialRunnerForStart(bindings);
    managedSerialStarts += 1;
    try {
      await runner.start(intent);
    } catch (error) {
      managedSerialFailure = safeManagedSerialFailure(error);
      throw error;
    } finally {
      managedSerialStarts -= 1;
    }
  }

  async function reattachManagedSerialSequence(
    bindings: ManagedSerialSequenceBindings,
  ) {
    managedSerialFailure = null;
    managedSerialDetachRequested = true;
    managedSerialRunner ??= newManagedSerialRunner(bindings);
    try {
      return await managedSerialRunner.reattach();
    } catch (error) {
      managedSerialFailure = safeManagedSerialFailure(error);
      throw error;
    }
  }

  function observeManagedSerialRunning(promptId: string): boolean {
    const runner = managedSerialRunner;
    if (runner === undefined) return false;
    const matched = runner.snapshot().activeQueuePromptId === promptId;
    trackManagedSerialEvent(runner.recordRunning(promptId));
    return matched;
  }

  function observeManagedSerialArtifact(
    event: SaveVideoArtifactEvent,
  ): boolean {
    const runner = managedSerialRunner;
    if (runner === undefined) return false;
    const matched = runner.snapshot().activeQueuePromptId === event.promptId;
    trackManagedSerialEvent(runner.recordArtifact(event));
    return matched;
  }

  function observeManagedSerialTerminal(
    event: ExecutionTerminalEvent,
  ): boolean {
    const runner = managedSerialRunner;
    if (runner === undefined) return false;
    const matched = runner.snapshot().activeQueuePromptId === event.promptId;
    trackManagedSerialEvent(runner.recordTerminal(event));
    return matched;
  }

  async function detachManagedSerialSequence(): Promise<void> {
    const runner = managedSerialRunner;
    // The runner revokes successor authority synchronously inside detach(). Avoid a second
    // best-effort backend signal when host loss, unmount and disposal collapse together.
    const parentState = runner?.snapshot().parentState;
    if (
      runner === undefined ||
      managedSerialDetachRequested ||
      parentState === "succeeded" ||
      parentState === "cancelled"
    )
      return;
    managedSerialDetachRequested = true;
    try {
      await runner.detach();
    } catch (error) {
      managedSerialFailure = safeManagedSerialFailure(error);
    }
  }

  async function cancelManagedSerialSequence(): Promise<void> {
    const runner = managedSerialRunner;
    if (runner === undefined)
      throw new ManagedSerialSequenceRunnerError("sequence_unavailable");
    managedSerialFailure = null;
    managedSerialDetachRequested = true;
    try {
      await runner.cancel();
    } catch (error) {
      managedSerialFailure = safeManagedSerialFailure(error);
      throw error;
    }
  }

  async function resumeManagedSerialSequence(identity: {
    activeWorkflowFingerprint: string;
    ownedProjectionFingerprint: string;
  }): Promise<void> {
    const runner = managedSerialRunner;
    if (runner === undefined)
      throw new ManagedSerialSequenceRunnerError("sequence_unavailable");
    managedSerialFailure = null;
    managedSerialDetachRequested = false;
    try {
      await runner.resume(identity);
    } catch (error) {
      managedSerialFailure = safeManagedSerialFailure(error);
      throw error;
    }
  }

  async function retryManagedSerialSequence(identity: {
    segmentId: string;
    activeWorkflowFingerprint: string;
    ownedProjectionFingerprint: string;
  }): Promise<void> {
    const runner = managedSerialRunner;
    if (runner === undefined)
      throw new ManagedSerialSequenceRunnerError("sequence_unavailable");
    managedSerialFailure = null;
    managedSerialDetachRequested = false;
    try {
      await runner.retry(identity);
    } catch (error) {
      managedSerialFailure = safeManagedSerialFailure(error);
      throw error;
    }
  }

  async function settleManagedSerialSequence(): Promise<void> {
    while (managedSerialTasks.size > 0)
      await Promise.all([...managedSerialTasks]);
  }

  function managedSerialSequenceSnapshot() {
    return Object.freeze({
      ...(managedSerialRunner?.snapshot() ?? {
        attached: false,
        parentSequenceId: null,
        parentState: null,
        parentRevision: null,
        activeSegmentId: null,
        activeQueuePromptId: null,
      }),
      failure: managedSerialFailure,
    });
  }

  /** The workflow label this session's runner bound its parent's children with, if any. */
  function managedSerialCanvasLabel() {
    return managedSerialRunner?.canvasLabel() ?? null;
  }

  return {
    cancelManagedSerialSequence,
    detachManagedSerialSequence,
    managedSerialCanvasLabel,
    managedSerialSequenceSnapshot,
    observeManagedSerialArtifact,
    observeManagedSerialRunning,
    observeManagedSerialTerminal,
    reattachManagedSerialSequence,
    resumeManagedSerialSequence,
    retryManagedSerialSequence,
    settleManagedSerialSequence,
    startManagedSerialSequence,
  };
}
