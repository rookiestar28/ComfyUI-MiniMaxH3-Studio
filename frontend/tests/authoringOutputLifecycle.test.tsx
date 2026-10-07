import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAuthoringOutput } from "../src/components/useAuthoringOutput";
import {
  OUTPUT_CAPABILITY,
  type OutputBinding,
  type OutputStatus,
} from "../src/contracts/authoringOutputCodec";
import {
  OutputClientError,
  type OutputClient,
} from "../src/host/authoringOutputActions";
import type { OutputPreview } from "../src/host/authoringOutputPreview";

const capability = { ...OUTPUT_CAPABILITY, supported: true };
const binding: OutputBinding = {
  workspace_handle: `authoring-${"a".repeat(32)}`,
  workspace_revision: 1,
  timeline_revision: 2,
  snapshot_fingerprint: `sha256:${"b".repeat(64)}`,
};
const finished: OutputStatus = {
  ...binding,
  schema: "h3.authoring.output_status.v1",
  job_handle: `arj_${"c".repeat(22)}`,
  output_handle: `aro_${"d".repeat(22)}`,
  state_version: 7,
  phase: "succeeded",
  progress_bp: 10000,
  failure: null,
  currency: "current",
  availability: "available",
  output: {
    output_fingerprint: `sha256:${"e".repeat(64)}`,
    byte_length: 10000,
    width: 1280,
    height: 720,
    frame_count: 48,
    frame_rate_num: 24,
    frame_rate_den: 1,
    audio_streams: 1,
    output_profile_id: capability.output_profile_id,
    verified: true,
  },
};
function fixture() {
  const client: OutputClient = {
    create: vi.fn(async () => finished),
    status: vi.fn(async () => finished),
    cancel: vi.fn(async () => finished),
  };
  const preview: OutputPreview = {
    open: vi.fn(async () => ({ url: "blob:fixture", close() {} })),
    close: vi.fn(),
  };
  const hook = renderHook(() =>
    useAuthoringOutput(binding, capability, client, preview),
  );
  return { client, preview, ...hook };
}
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("output leaf polling and explicit action ordering", () => {
  it("lets explicit render supersede a pending background read without cancelling the old job", async () => {
    vi.useFakeTimers();
    const f = fixture();
    await act(async () => f.result.current.render());
    let pollSignal: AbortSignal | undefined;
    vi.mocked(f.client.status).mockImplementation(
      (_job, _binding, _cap, signal) => {
        pollSignal = signal;
        return new Promise(() => undefined);
      },
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(pollSignal?.aborted).toBe(false);
    await act(async () => f.result.current.render());
    expect(pollSignal?.aborted).toBe(true);
    expect(f.client.create).toHaveBeenCalledTimes(2);
    expect(f.client.cancel).not.toHaveBeenCalled();
  });

  it.each(["unavailable", "expired", "forbidden"] as const)(
    "stops automatic reads after %s while allowing deliberate refresh",
    async (code) => {
      vi.useFakeTimers();
      const f = fixture();
      await act(async () => f.result.current.render());
      vi.mocked(f.client.status).mockRejectedValue(new OutputClientError(code));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      expect(f.result.current.error).toBe(true);
      expect(f.result.current.url).toBeNull();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20000);
      });
      expect(f.client.status).toHaveBeenCalledTimes(1);
      await act(async () => f.result.current.refresh());
      expect(f.client.status).toHaveBeenCalledTimes(2);
      expect(f.client.cancel).not.toHaveBeenCalled();
    },
  );
});
