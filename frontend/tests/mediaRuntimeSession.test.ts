import { describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/media_runtime_wire_v4.json";
import { decodeMediaRuntimeStatus } from "../src/contracts/mediaRuntimeCodec";
import { createMediaRuntimeSession } from "../src/lifecycle/mediaRuntimeSession";
import {
  createShellSession,
  type ShellRuntime,
} from "../src/lifecycle/shellSession";
const ready = decodeMediaRuntimeStatus(fixture.samples.status_ready_recovery);
function subject() {
  const session = createShellSession();
  session.mediaRuntime = {
    ...session.mediaRuntime,
    status: "read",
    wire: decodeMediaRuntimeStatus(fixture.samples.status_setup_required),
  };
  const client = {
    readStatus: vi.fn(async () => ({ ok: true, status: ready })),
    send: vi.fn(async () => ({ ok: true, status: ready })),
  };
  const actions = {
    renderCurrent: vi.fn(),
    nleImportSelectedOutputs: vi.fn(),
    nleReadRenderCapability: vi.fn(),
  };
  const media = createMediaRuntimeSession({
    session,
    deps: { mediaRuntimeClient: client },
    actions,
  } as unknown as ShellRuntime);
  return { session, client, actions, media };
}
const flush = async () => {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
};
describe("manual runtime session", () => {
  it("an older GET cannot overwrite the revision returned by folder selection", async () => {
    const s = subject();
    s.session.mediaRuntime = { ...s.session.mediaRuntime, status: "unread" };
    let finish: (
      value: Awaited<ReturnType<typeof s.client.readStatus>>,
    ) => void = () => {};
    s.client.readStatus.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    s.media.mediaRuntimeEnsureStatus();
    s.media.mediaRuntimeUseLocalDirectory("C:/tools/bin");
    await flush();
    expect(s.session.mediaRuntime.wire).toBe(ready);
    finish({
      ok: true,
      status: decodeMediaRuntimeStatus(fixture.samples.status_setup_required),
    });
    await flush();
    expect(s.session.mediaRuntime.wire).toBe(ready);
    expect(s.session.mediaRuntime.status).toBe("read");
  });
  it("selection carries CAS and never retries the original media action", async () => {
    const s = subject();
    s.media.mediaRuntimeUseLocalDirectory("C:/tools/bin");
    await flush();
    expect(s.client.send).toHaveBeenCalledWith("use_local_directory", {
      directory: "C:/tools/bin",
      expectedRevision: 0,
    });
    expect(s.session.mediaRuntime.notice).toBe("retry_action");
    expect(s.actions.nleImportSelectedOutputs).not.toHaveBeenCalled();
    expect("resume" in s.session.mediaRuntime).toBe(false);
  });
  it("coalesces a double click and ignores late results after release", async () => {
    const s = subject();
    let finish: (
      value: Awaited<ReturnType<typeof s.client.send>>,
    ) => void = () => {};
    s.client.send.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }) as ReturnType<typeof s.client.send>,
    );
    s.media.mediaRuntimeRescan();
    s.media.mediaRuntimeRescan();
    expect(s.client.send).toHaveBeenCalledTimes(1);
    s.media.releaseMediaRuntime();
    finish({ ok: true, status: ready });
    await flush();
    expect(s.session.mediaRuntime.status).toBe("unread");
    expect(s.session.mediaRuntime.wire).toBeNull();
  });
  it("a lost mutation rereads once and is never resubmitted", async () => {
    const s = subject();
    s.client.send.mockResolvedValue({
      ok: false,
      code: "transport_failure",
      outcomeUnknown: true,
    } as never);
    s.media.mediaRuntimeRescan();
    await flush();
    await flush();
    expect(s.client.send).toHaveBeenCalledTimes(1);
    expect(s.client.readStatus).toHaveBeenCalledTimes(1);
    expect(s.actions.nleImportSelectedOutputs).not.toHaveBeenCalled();
  });
  it("keeps a stale config refusal and permits an explicit retry", async () => {
    const s = subject();
    s.client.send.mockResolvedValue({
      ok: false,
      code: "config_conflict",
      outcomeUnknown: false,
    } as never);
    s.media.mediaRuntimeRestoreAuto();
    await flush();
    expect(s.session.mediaRuntime.refusal).toBe("config_conflict");
    expect(s.client.readStatus).not.toHaveBeenCalled();
  });
  it("a contextual remount does not poll an already explained refusal", async () => {
    const s = subject();
    s.media.mediaRuntimeReport("preview");
    s.media.mediaRuntimeReport("preview");
    await flush();
    expect(s.client.readStatus).not.toHaveBeenCalled();
  });
});
