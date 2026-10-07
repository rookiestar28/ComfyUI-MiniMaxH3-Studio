// M25-33 AC33-03/04/05: the Media tools session reads only when a surface can use it, shares the
// one setup job, stops polling at a terminal state or on release, and runs a captured action once
// only after every identity it captured still holds.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import wireFixture from "./fixtures/media_runtime_wire_v3.json";
import {
  decodeMediaRuntimeJob,
  decodeMediaRuntimeStatus,
  type MediaRuntimeJob,
  type MediaRuntimeStatus,
} from "../src/contracts/mediaRuntimeCodec";
import type { MediaRuntimeClient } from "../src/host/mediaRuntimeClient";
import {
  MEDIA_RUNTIME_MAX_POLL_FAILURES,
  createMediaRuntimeSession,
} from "../src/lifecycle/mediaRuntimeSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
import type { MediaRuntimeIntent } from "../src/state/mediaRuntimeState";

type Raw = Record<string, unknown> & {
  features: Record<string, { state: string; reason: string | null }>;
};
const samples = wireFixture.samples as unknown as Record<string, Raw>;
const JOB = "abababababababababababababababab";

function status(name: string, mutate?: (raw: Raw) => void): MediaRuntimeStatus {
  const raw = JSON.parse(JSON.stringify(samples[name])) as Raw;
  mutate?.(raw);
  return decodeMediaRuntimeStatus(raw);
}

const READY = status("status_override");
const MISSING = status("status_setup_required");
const INSTALLING = status("status_installing");

function job(
  state: MediaRuntimeJob["state"],
  reason: string | null = null,
): MediaRuntimeJob {
  return decodeMediaRuntimeJob({
    ...samples.job_running,
    state,
    phase: state === "running" ? "downloading" : "done",
    reason,
  });
}

const IMPORT_INTENT: MediaRuntimeIntent = {
  kind: "production_import",
  productionWorkspaceHandle: "pw_handle",
  productionWorkspaceId: "workspace.1",
  segmentIds: ["segment.1"],
  outputHandles: ["out_ready"],
};

const RENDER_INTENT: MediaRuntimeIntent = {
  kind: "final_render",
  workspaceHandle: "aw_handle",
  workspaceRevision: 3,
  timelineRevision: 5,
  publicFingerprint: `sha256:${"f".repeat(64)}`,
  overlayGeneration: 2,
};

function subject() {
  const session = createShellSession();
  session.container = document.createElement("div");
  session.productionState = {
    status: "ready",
    projection: {
      workspaceHandle: "pw_handle",
      workspaceId: "workspace.1",
      workspaceRevision: 7,
      allowedActions: ["import_production_outputs_to_authoring"],
      selectedSegmentIds: ["segment.1"],
      outputs: [
        { segmentId: "segment.1", state: "ready", outputHandle: "out_ready" },
      ],
    },
  } as unknown as typeof session.productionState;
  session.authoringState = {
    status: "ready",
    timelineHistory: {
      snapshot: {
        workspaceHandle: "aw_handle",
        workspaceRevision: 3,
        timelineRevision: 5,
        publicFingerprint: `sha256:${"f".repeat(64)}`,
      },
    },
  } as unknown as typeof session.authoringState;
  session.nleWorkspace = {
    ...session.nleWorkspace,
    surface: {
      ...session.nleWorkspace.surface,
      status: "expanded",
      generation: 2,
    },
  };
  let page = "production";
  const client = {
    readStatus: vi.fn(async () => ({ ok: true, status: READY })),
    readJob: vi.fn(async () => ({ ok: true, job: job("running") })),
    send: vi.fn(async () => ({ ok: true, job: job("running") })),
  };
  const nleImportSelectedOutputs = vi.fn(async () => undefined);
  const nleReadRenderCapability = vi.fn(async () => undefined);
  const renderCurrent = vi.fn();
  const runtime = {
    session,
    deps: {
      mediaRuntimeClient: client as unknown as MediaRuntimeClient,
      pageRegistry: { getSnapshot: () => ({ selected: page }) },
    },
    actions: {
      renderCurrent,
      nleImportSelectedOutputs,
      nleReadRenderCapability,
    },
  } as unknown as ShellRuntime;
  const media = createMediaRuntimeSession(runtime);
  return {
    session,
    client,
    media,
    nleImportSelectedOutputs,
    nleReadRenderCapability,
    state: () => session.mediaRuntime,
    setPage: (next: string) => {
      page = next;
    },
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("status reads", () => {
  it("reads once on the first Settings open and never polls without a job", async () => {
    const s = subject();
    s.media.mediaRuntimeEnsureStatus();
    s.media.mediaRuntimeEnsureStatus();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(s.client.readStatus).toHaveBeenCalledTimes(1);
    expect(s.client.readJob).not.toHaveBeenCalled();
    expect(s.state()).toMatchObject({ status: "read", wire: READY });
  });

  it("fails closed to status unavailable and never keeps a stale ready", async () => {
    const s = subject();
    s.media.mediaRuntimeEnsureStatus();
    await vi.advanceTimersByTimeAsync(0);
    s.client.readStatus.mockResolvedValueOnce({
      ok: false,
      code: "malformed_response",
      outcomeUnknown: false,
    } as never);
    s.media.mediaRuntimeReport("import");
    await vi.advanceTimersByTimeAsync(0);
    expect(s.state()).toMatchObject({
      status: "unavailable",
      refusal: "malformed_response",
    });
  });

  it("never re-reads a status that already explains the refusal when a card remounts", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    s.media.mediaRuntimeReport("render");
    await vi.advanceTimersByTimeAsync(0);
    s.media.mediaRuntimeReport("render");
    s.media.mediaRuntimeReport("import");
    await vi.advanceTimersByTimeAsync(0);
    expect(s.client.readStatus).toHaveBeenCalledTimes(1);

    const failed = subject();
    failed.client.readStatus.mockResolvedValue({
      ok: false,
      code: "transport_failure",
      outcomeUnknown: false,
    } as never);
    failed.media.mediaRuntimeReport("preview");
    await vi.advanceTimersByTimeAsync(0);
    failed.media.mediaRuntimeReport("preview");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(failed.client.readStatus).toHaveBeenCalledTimes(1);
    expect(failed.state().status).toBe("unavailable");
  });

  it("re-reads the render capability once when the status says final output is ready", async () => {
    const s = subject();
    s.media.mediaRuntimeReport("render");
    await vi.advanceTimersByTimeAsync(0);
    expect(s.nleReadRenderCapability).toHaveBeenCalledTimes(1);
    expect(s.nleReadRenderCapability).toHaveBeenCalledWith(2);
    // The capability answered unsupported again and the card remounted: nothing reads again.
    s.media.mediaRuntimeReport("render");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(s.client.readStatus).toHaveBeenCalledTimes(1);
    expect(s.nleReadRenderCapability).toHaveBeenCalledTimes(1);
    // Import and preview reports never re-read the render capability.
    s.media.mediaRuntimeReport("import");
    await vi.advanceTimersByTimeAsync(0);
    expect(s.nleReadRenderCapability).toHaveBeenCalledTimes(1);
  });

  it("waits boundedly for a starting feature before explaining a render refusal", async () => {
    const s = subject();
    const starting = status("status_override", (raw) => {
      raw.features.render = { state: "unavailable", reason: "activating" };
    });
    s.client.readStatus
      .mockResolvedValueOnce({ ok: true, status: starting })
      .mockResolvedValueOnce({ ok: true, status: starting })
      .mockResolvedValue({ ok: true, status: READY });
    s.media.mediaRuntimeReport("render");
    await vi.advanceTimersByTimeAsync(0);
    expect(s.nleReadRenderCapability).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(s.client.readStatus).toHaveBeenCalledTimes(3);
    expect(s.nleReadRenderCapability).toHaveBeenCalledTimes(1);
  });

  it("shares a job another tab started instead of starting a second one", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: INSTALLING });
    s.media.mediaRuntimeReport("import");
    await vi.advanceTimersByTimeAsync(0);
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(s.client.send).not.toHaveBeenCalled();
    expect(s.client.readJob).toHaveBeenCalledWith(JOB);
    expect(s.state().pending).toEqual(IMPORT_INTENT);
  });
});

describe("install and continue", () => {
  async function installed(
    s: ReturnType<typeof subject>,
    intent: MediaRuntimeIntent,
  ) {
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(intent);
    s.client.readStatus.mockResolvedValue({ ok: true, status: READY });
    s.client.readJob.mockResolvedValueOnce({
      ok: true,
      job: job("succeeded", "installed"),
    });
    await vi.advanceTimersByTimeAsync(1_000);
  }

  it("continues the original import exactly once after a successful install", async () => {
    const s = subject();
    await installed(s, IMPORT_INTENT);
    expect(s.client.send).toHaveBeenCalledTimes(1);
    expect(s.nleImportSelectedOutputs).toHaveBeenCalledTimes(1);
    expect(s.nleImportSelectedOutputs).toHaveBeenCalledWith(["segment.1"]);
    expect(s.state()).toMatchObject({ pending: null, notice: "continued" });
    expect(s.state().resume).toMatchObject({ kind: "production_import" });
    // The poll stopped at the terminal state; nothing runs a second time.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(s.client.readJob).toHaveBeenCalledTimes(1);
    expect(s.nleImportSelectedOutputs).toHaveBeenCalledTimes(1);
  });

  it("re-reads the capability for a final render and never starts a render", async () => {
    const s = subject();
    await installed(s, RENDER_INTENT);
    expect(s.nleReadRenderCapability).toHaveBeenCalledWith(2);
    expect(s.nleImportSelectedOutputs).not.toHaveBeenCalled();
  });

  it("ignores a second click while the first install request is in flight", async () => {
    const s = subject();
    let resolveSend!: (value: unknown) => void;
    s.client.send.mockImplementationOnce(
      () => new Promise((resolve) => (resolveSend = resolve)) as never,
    );
    const first = s.media.mediaRuntimeInstall(IMPORT_INTENT);
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    expect(s.client.send).toHaveBeenCalledTimes(1);
    resolveSend({ ok: true, job: job("running") });
    await first;
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    expect(s.client.send).toHaveBeenCalledTimes(1);
  });

  it("adopts the running job after a lost install response without resending", async () => {
    const s = subject();
    s.client.send.mockResolvedValueOnce({
      ok: false,
      code: "transport_failure",
      outcomeUnknown: true,
    } as never);
    s.client.readStatus.mockResolvedValue({ ok: true, status: INSTALLING });
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(s.client.send).toHaveBeenCalledTimes(1);
    expect(s.client.readJob).toHaveBeenCalledWith(JOB);
    expect(s.state().notice).toBeNull();
  });

  it("says the outcome is unknown when a lost response left no running job", async () => {
    const s = subject();
    s.client.send.mockResolvedValueOnce({
      ok: false,
      code: "transport_failure",
      outcomeUnknown: true,
    } as never);
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    expect(s.client.send).toHaveBeenCalledTimes(1);
    expect(s.state().notice).toBe("outcome_unknown");
  });

  it("re-reads status when another setup action holds the slot", async () => {
    const s = subject();
    s.client.send.mockResolvedValueOnce({
      ok: false,
      code: "setup_busy",
      outcomeUnknown: false,
    } as never);
    await s.media.mediaRuntimeInstall(null);
    expect(s.state().refusal).toBe(null);
    expect(s.client.readStatus).toHaveBeenCalledTimes(1);
  });

  it("continues after an append advances the project revision but preserves the selected output", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    s.session.productionState = {
      status: "ready",
      projection: {
        ...(s.session.productionState as { projection: object }).projection,
        workspaceRevision: 8,
      },
    } as unknown as typeof s.session.productionState;
    s.client.readStatus.mockResolvedValue({ ok: true, status: READY });
    s.client.readJob.mockResolvedValueOnce({
      ok: true,
      job: job("succeeded", "installed"),
    });
    await vi.advanceTimersByTimeAsync(1_000);
    expect(s.nleImportSelectedOutputs).toHaveBeenCalledWith(["segment.1"]);
    expect(s.state()).toMatchObject({ pending: null, notice: "continued" });
  });

  it("drops the continuation when an appended project changes the captured output", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    s.session.productionState = {
      status: "ready",
      projection: {
        ...(s.session.productionState as { projection: object }).projection,
        workspaceRevision: 8,
        outputs: [
          {
            segmentId: "segment.1",
            state: "ready",
            outputHandle: "out_replaced",
          },
        ],
      },
    } as unknown as typeof s.session.productionState;
    s.client.readStatus.mockResolvedValue({ ok: true, status: READY });
    s.client.readJob.mockResolvedValueOnce({
      ok: true,
      job: job("succeeded", "installed"),
    });
    await vi.advanceTimersByTimeAsync(1_000);
    expect(s.nleImportSelectedOutputs).not.toHaveBeenCalled();
    expect(s.state()).toMatchObject({ pending: null, notice: "select_again" });
  });

  it("drops the continuation when the selection changed during install", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    s.session.productionState = {
      status: "ready",
      projection: {
        ...(s.session.productionState as { projection: object }).projection,
        selectedSegmentIds: ["segment.2"],
      },
    } as unknown as typeof s.session.productionState;
    s.client.readStatus.mockResolvedValue({ ok: true, status: READY });
    s.client.readJob.mockResolvedValueOnce({
      ok: true,
      job: job("succeeded", "installed"),
    });
    await vi.advanceTimersByTimeAsync(1_000);
    expect(s.nleImportSelectedOutputs).not.toHaveBeenCalled();
    expect(s.state().notice).toBe("select_again");
  });

  it("never continues an uncertain import, which keeps its own retained Retry", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    s.session.nleWorkspace = {
      ...s.session.nleWorkspace,
      import: { ...s.session.nleWorkspace.import, status: "uncertain" },
    };
    s.client.readStatus.mockResolvedValue({ ok: true, status: READY });
    s.client.readJob.mockResolvedValueOnce({
      ok: true,
      job: job("succeeded", "installed"),
    });
    await vi.advanceTimersByTimeAsync(1_000);
    expect(s.nleImportSelectedOutputs).not.toHaveBeenCalled();
    expect(s.state().notice).toBe("select_again");
  });

  it("drops a render continuation when its overlay generation closed", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(RENDER_INTENT);
    s.session.nleWorkspace = {
      ...s.session.nleWorkspace,
      surface: { ...s.session.nleWorkspace.surface, generation: 3 },
    };
    s.client.readStatus.mockResolvedValue({ ok: true, status: READY });
    s.client.readJob.mockResolvedValueOnce({
      ok: true,
      job: job("succeeded", "installed"),
    });
    await vi.advanceTimersByTimeAsync(1_000);
    expect(s.nleReadRenderCapability).not.toHaveBeenCalled();
    expect(s.state().notice).toBe("select_again");
  });

  it("waits boundedly for activation, then continues", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    const activating = status("status_override", (raw) => {
      raw.features.import = { state: "unavailable", reason: "activating" };
    });
    s.client.readStatus
      .mockResolvedValueOnce({ ok: true, status: activating })
      .mockResolvedValue({ ok: true, status: READY });
    s.client.readJob.mockResolvedValueOnce({
      ok: true,
      job: job("succeeded", "installed"),
    });
    await vi.advanceTimersByTimeAsync(1_000);
    expect(s.nleImportSelectedOutputs).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(s.nleImportSelectedOutputs).toHaveBeenCalledTimes(1);
  });

  it("reports a feature still not ready instead of continuing", async () => {
    const s2 = subject();
    s2.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s2.media.mediaRuntimeInstall(IMPORT_INTENT);
    s2.client.readJob.mockResolvedValueOnce({
      ok: true,
      job: job("succeeded", "installed"),
    });
    await vi.advanceTimersByTimeAsync(1_000);
    expect(s2.nleImportSelectedOutputs).not.toHaveBeenCalled();
    expect(s2.state()).toMatchObject({
      pending: null,
      notice: "feature_not_ready",
    });
  });

  it("keeps the continuation after a failed install and returns focus to the card", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    const focus = s.state().focusPrimary;
    s.client.readJob.mockResolvedValueOnce({
      ok: true,
      job: job("failed", "digest_mismatch"),
    });
    await vi.advanceTimersByTimeAsync(1_000);
    expect(s.state().pending).toEqual(IMPORT_INTENT);
    expect(s.state().focusPrimary).toBe(focus + 1);
    expect(s.nleImportSelectedOutputs).not.toHaveBeenCalled();
  });
});

describe("clearing a continuation", () => {
  it("cancel clears the continuation and never continues", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    s.client.send.mockResolvedValueOnce({
      ok: true,
      job: job("cancelled", "cancelled"),
    });
    await s.media.mediaRuntimeCancel();
    expect(s.client.send).toHaveBeenLastCalledWith("cancel_setup", {
      jobId: JOB,
    });
    expect(s.state()).toMatchObject({
      pending: null,
      notice: "continuation_cleared",
    });
    s.client.readJob.mockResolvedValue({
      ok: true,
      job: job("succeeded", "installed"),
    });
    await vi.advanceTimersByTimeAsync(2_000);
    expect(s.nleImportSelectedOutputs).not.toHaveBeenCalled();
  });

  it("leaving the page clears any continuation; leaving the overlay clears only its own", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    s.media.mediaRuntimeLeaveContext("overlay");
    expect(s.state().pending).toEqual(IMPORT_INTENT);
    s.media.mediaRuntimeLeaveContext("function");
    expect(s.state()).toMatchObject({
      pending: null,
      notice: "continuation_cleared",
    });

    const r = subject();
    await r.media.mediaRuntimeInstall(RENDER_INTENT);
    r.media.mediaRuntimeLeaveContext("function");
    expect(r.state().pending).toEqual(RENDER_INTENT);
    r.media.mediaRuntimeLeaveContext("page");
    expect(r.state().pending).toBeNull();
  });

  it("a different surface's install replaces the earlier continuation with a notice", async () => {
    const s = subject();
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    await s.media.mediaRuntimeInstall(RENDER_INTENT);
    expect(s.state()).toMatchObject({
      pending: RENDER_INTENT,
      notice: "continuation_replaced",
    });
    expect(s.client.send).toHaveBeenCalledTimes(1);
  });

  it("release stops polling and ignores a late job answer", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    s.media.releaseMediaRuntime();
    s.client.readJob.mockResolvedValue({
      ok: true,
      job: job("succeeded", "installed"),
    });
    await vi.advanceTimersByTimeAsync(5_000);
    expect(s.client.readJob).not.toHaveBeenCalled();
    expect(s.state()).toMatchObject({ status: "unread", pending: null });
    expect(s.nleImportSelectedOutputs).not.toHaveBeenCalled();
  });

  it("forgets a job the server no longer knows and follows the fresh status", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({ ok: true, status: MISSING });
    await s.media.mediaRuntimeInstall(null);
    s.client.readJob.mockResolvedValue({
      ok: false,
      code: "setup_job_not_found",
      outcomeUnknown: false,
    } as never);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(s.state()).toMatchObject({
      status: "read",
      wire: MISSING,
      job: null,
    });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(s.client.readJob).toHaveBeenCalledTimes(1);
  });

  it("gives up polling after the bounded failure count", async () => {
    const s = subject();
    await s.media.mediaRuntimeInstall(null);
    s.client.readJob.mockResolvedValue({
      ok: false,
      code: "transport_failure",
      outcomeUnknown: false,
    } as never);
    await vi.advanceTimersByTimeAsync(MEDIA_RUNTIME_MAX_POLL_FAILURES * 1_000);
    expect(s.client.readJob).toHaveBeenCalledTimes(
      MEDIA_RUNTIME_MAX_POLL_FAILURES,
    );
    expect(s.state().status).toBe("unavailable");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(s.client.readJob).toHaveBeenCalledTimes(
      MEDIA_RUNTIME_MAX_POLL_FAILURES,
    );
  });
});

describe("status actions", () => {
  it("sends the folder and restore actions against the read config revision", async () => {
    const s = subject();
    s.client.readStatus.mockResolvedValue({
      ok: true,
      status: status("status_local_selection"),
    });
    s.media.mediaRuntimeEnsureStatus();
    await vi.advanceTimersByTimeAsync(0);
    s.client.send.mockResolvedValue({ ok: true, status: READY } as never);
    s.media.mediaRuntimeUseLocalDirectory("D:/tools");
    await vi.advanceTimersByTimeAsync(0);
    expect(s.client.send).toHaveBeenLastCalledWith("use_local_directory", {
      directory: "D:/tools",
      expectedRevision: 4,
    });
    s.client.readStatus.mockResolvedValue({
      ok: true,
      status: status("status_local_selection"),
    });
    s.media.mediaRuntimeReport("import");
    await vi.advanceTimersByTimeAsync(0);
    s.media.mediaRuntimeRestoreAuto();
    await vi.advanceTimersByTimeAsync(0);
    expect(s.client.send).toHaveBeenLastCalledWith("restore_auto", {
      expectedRevision: 4,
    });
  });

  it("surfaces a reclaim refusal as a closed code", async () => {
    const s = subject();
    s.client.send.mockResolvedValueOnce({
      ok: false,
      code: "reclaim_unsafe",
      outcomeUnknown: false,
    } as never);
    s.media.mediaRuntimeReclaim();
    await vi.advanceTimersByTimeAsync(0);
    expect(s.client.send).toHaveBeenCalledWith("reclaim_parked_runtime", {});
    expect(s.state().refusal).toBe("reclaim_unsafe");
  });

  it("dismissing a notice keeps the continuation", async () => {
    const s = subject();
    await s.media.mediaRuntimeInstall(IMPORT_INTENT);
    await s.media.mediaRuntimeInstall(RENDER_INTENT);
    s.media.mediaToolsBinding().dismiss();
    expect(s.state()).toMatchObject({ notice: null, pending: RENDER_INTENT });
  });

  it("publishes stable binding functions across renders", () => {
    const s = subject();
    const first = s.media.mediaToolsBinding();
    const second = s.media.mediaToolsBinding();
    expect(second.install).toBe(first.install);
    expect(second.ensure).toBe(first.ensure);
    expect(second.report).toBe(first.report);
  });
});
