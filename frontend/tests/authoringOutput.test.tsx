import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthoringOutput } from "../src/components/AuthoringOutput";
import {
  OUTPUT_CAPABILITY,
  type OutputBinding,
} from "../src/contracts/authoringOutputCodec";
import type { OutputClient } from "../src/host/authoringOutputActions";
import type { OutputPreview } from "../src/host/authoringOutputPreview";

afterEach(() => cleanup());
beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(
    () => undefined,
  );
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(
    () => undefined,
  );
});
const binding: OutputBinding = {
  workspace_handle: `authoring-${"a".repeat(32)}`,
  workspace_revision: 1,
  timeline_revision: 2,
  snapshot_fingerprint: `sha256:${"b".repeat(64)}`,
};
const status = {
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
    output_profile_id: OUTPUT_CAPABILITY.output_profile_id,
    verified: true,
  },
} as const;
function dependencies() {
  const client: OutputClient = {
    create: vi.fn(async () => status),
    status: vi.fn(async () => status),
    cancel: vi.fn(async () => status),
  };
  const lease = { url: "blob:preview", close: vi.fn() };
  const preview: OutputPreview = {
    open: vi.fn(async () => lease),
    close: vi.fn(),
  };
  return { client, preview, lease };
}
async function component() {
  return AuthoringOutput;
}
describe("unmounted final-output leaf", () => {
  it("performs no calls without exact supported capability and workspace", async () => {
    const Component = await component(),
      deps = dependencies();
    const view = render(
      <Component capability={null} binding={binding} locale="en" {...deps} />,
    );
    expect(view.container.childElementCount).toBe(0);
    view.rerender(
      <Component
        capability={OUTPUT_CAPABILITY}
        binding={binding}
        locale="en"
        {...deps}
      />,
    );
    expect(view.container.childElementCount).toBe(0);
    expect(deps.client.create).not.toHaveBeenCalled();
  });
  it("renders by keyboard, labels old output and exposes native download only", async () => {
    const Component = await component(),
      deps = dependencies(),
      user = userEvent.setup();
    const props = {
      capability: { ...OUTPUT_CAPABILITY, supported: true },
      binding,
      locale: "en" as const,
      ...deps,
    };
    const view = render(<Component {...props} />);
    expect(deps.client.create).not.toHaveBeenCalled();
    await user.tab();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Render final video" }),
    );
    await user.keyboard("{Enter}");
    const link = await screen.findByRole("link", { name: "Download original" });
    expect(link.getAttribute("download")).toBe("authoring-final.mp4");
    expect(link.getAttribute("href")).toContain(
      "/download?workspace_handle=authoring-",
    );
    expect(link.getAttribute("href")).not.toContain("blob:");
    view.rerender(
      <Component {...props} binding={{ ...binding, timeline_revision: 3 }} />,
    );
    expect(screen.getByText("Output from an earlier revision")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Preview output" }));
    expect(
      (await screen.findByLabelText("Final video preview")).getAttribute("src"),
    ).toBe("blob:preview");
    view.unmount();
    expect(deps.lease.close).toHaveBeenCalled();
    expect(deps.client.cancel).not.toHaveBeenCalled();
  });
  it("steps the preview position itself when a page-wide hotkey cancels its native default", async () => {
    // B-M2605-SEEK-02: ComfyUI-Easy-Use binds the arrows through hotkeys-js on `document`, which
    // cancels the native step of every range input, so the preview seek lost its arrow keys.
    const Component = await component(),
      deps = dependencies(),
      user = userEvent.setup();
    const foreign = vi.fn((event: KeyboardEvent) => event.preventDefault());
    render(
      <Component
        capability={{ ...OUTPUT_CAPABILITY, supported: true }}
        binding={binding}
        locale="en"
        {...deps}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Render final video" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "Preview output" }),
    );
    const player = await screen.findByLabelText<HTMLVideoElement>(
      "Final video preview",
    );
    Object.defineProperty(player, "duration", {
      configurable: true,
      value: 2,
    });
    fireEvent.loadedMetadata(player);
    const seek = screen.getByRole<HTMLInputElement>("slider", {
      name: "Preview position",
    });
    document.addEventListener("keydown", foreign);
    try {
      const press = (key: string, init: KeyboardEventInit = {}) =>
        fireEvent.keyDown(seek, { key, ...init });
      expect(press("ArrowRight")).toBe(false);
      expect(seek.value).toBe("0.1");
      expect(player.currentTime).toBe(0.1);
      press("PageUp");
      expect(seek.value).toBe("0.3");
      press("End");
      expect(seek.value).toBe("2");
      expect(player.currentTime).toBe(2);
      press("Home");
      expect(seek.value).toBe("0");
      expect(foreign).not.toHaveBeenCalled();
      // Browser and OS shortcuts keep their meaning.
      press("ArrowRight", { metaKey: true });
      expect(seek.value).toBe("0");
      expect(foreign).toHaveBeenCalledTimes(1);
    } finally {
      document.removeEventListener("keydown", foreign);
    }
  });
  it("aborts pending creation on workspace replacement without implicitly cancelling job", async () => {
    const Component = await component(),
      deps = dependencies();
    let signal: AbortSignal | undefined;
    deps.client = {
      ...deps.client,
      create: vi.fn<OutputClient["create"]>((_request, _capability, value) => {
        signal = value;
        return new Promise(() => undefined);
      }),
    };
    const props = {
      capability: { ...OUTPUT_CAPABILITY, supported: true },
      binding,
      locale: "zh-TW" as const,
      ...deps,
    };
    const view = render(<Component {...props} />);
    await userEvent.click(screen.getByRole("button", { name: "算繪最終影片" }));
    await waitFor(() => expect(signal).toBeDefined());
    view.rerender(
      <Component
        {...props}
        binding={{
          ...binding,
          workspace_handle: `authoring-${"f".repeat(32)}`,
        }}
      />,
    );
    expect(signal!.aborted).toBe(true);
    expect(deps.client.cancel).not.toHaveBeenCalled();
  });
});
