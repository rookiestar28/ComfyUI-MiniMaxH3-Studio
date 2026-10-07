import { describe, expect, it, vi } from "vitest";
import { createAuthoringSession } from "../src/host/authoringSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
import {
  initialSidebarStagesDraft,
  sidebarStagesAuthority,
} from "../src/components/SidebarStages";
import { validSidebarWorkspace } from "./sidebarWorkspaceFixture";

function pending() {
  let resolve!: (value: unknown) => void;
  const promise = new Promise<unknown>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function subject(sendAssisted: ReturnType<typeof vi.fn>) {
  const session = createShellSession();
  session.workspaceState = {
    status: "ready",
    projection: validSidebarWorkspace,
  };
  session.workspaceDraft = initialSidebarStagesDraft(validSidebarWorkspace);
  const controller = createAuthoringSession({
    session,
    deps: { actions: { sendAssisted } },
    actions: { renderCurrent: vi.fn() },
  } as unknown as ShellRuntime);
  return { session, controller };
}
const idle = {
  kind: "assisted",
  result: { state: "idle", proposal: null },
  proposal: null,
};

describe("assisted client completion ownership", () => {
  it("routes Refine through the assisted client and drops a changed report completion", async () => {
    const deferred = pending();
    const send = vi.fn(() => deferred.promise);
    const { session, controller } = subject(send);
    const action = {
      action: "refine_prompt" as const,
      payload: { instruction: "Improve lighting" },
    };
    const run = controller.runWorkspaceAction(action);
    expect(send).toHaveBeenCalledWith(
      validSidebarWorkspace,
      action,
      expect.any(AbortSignal),
    );
    session.workspaceState = {
      status: "ready",
      projection: { ...validSidebarWorkspace, report_revision: 2 },
    };
    deferred.resolve({
      ...idle,
      proposal: { state: "active", candidate_text: "Stale candidate" },
    });
    await run;
    expect(session.assistedProposal).toBeUndefined();
  });

  it("preserves a newer editor after an explicit acceptance response", async () => {
    const deferred = pending();
    const { session, controller } = subject(vi.fn(() => deferred.promise));
    const run = controller.runWorkspaceAction({
      action: "accept_assisted_proposal",
      payload: {
        proposal_id: `assist_${"a".repeat(32)}`,
        expected_proposal_revision: 1,
      },
    });
    session.workspaceDraft = {
      ...session.workspaceDraft!,
      promptText: "Newer local text",
      reason: "New reason",
    };
    const next = {
      ...validSidebarWorkspace,
      report_revision: 2,
      prompt_text: "Accepted candidate",
    };
    deferred.resolve({ kind: "workspace", projection: next });
    await run;
    expect(session.workspaceDraft).toMatchObject({
      authority: sidebarStagesAuthority(next),
      promptText: "Newer local text",
      reason: "New reason",
    });
  });

  it("cancel invalidates the pending response before acknowledging the server", async () => {
    const generation = pending();
    const cancellation = pending();
    const send = vi
      .fn()
      .mockReturnValueOnce(generation.promise)
      .mockReturnValueOnce(cancellation.promise);
    const { session, controller } = subject(send);
    const run = controller.runWorkspaceAction({
      action: "refine_prompt",
      payload: { instruction: "Improve lighting" },
    });
    const cancel = controller.runWorkspaceAction({
      action: "cancel_assisted_execution",
      payload: {},
    });
    generation.resolve({
      ...idle,
      proposal: { state: "active", candidate_text: "Cancelled candidate" },
    });
    await run;
    expect(session.assistedProposal).toBeUndefined();
    expect(session.assistedBusy).toBe(true);
    cancellation.resolve(idle);
    await cancel;
    expect(session.assistedBusy).toBe(false);
  });
});
