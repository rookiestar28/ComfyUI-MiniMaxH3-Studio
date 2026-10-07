import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthoringPreviewMonitor } from "../src/components/AuthoringPreviewMonitor";
import type { AuthoringPreviewRequest } from "../src/contracts/authoringPreviewCodec";
import type { AuthoringPreviewIdentity } from "../src/host/authoringFrameCoordinator";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const identity = (overrides: Partial<AuthoringPreviewIdentity> = {}) => ({
  workspaceHandle: "authoring-1",
  referenceRevision: 4,
  timelineRevision: 2,
  timelineContentFingerprint: "sha256:" + "a".repeat(64),
  clipId: "clip-1",
  startFrame: 10,
  frames: 24,
  sourceStartFrame: 30,
  fps: 24,
  ...overrides,
});

describe("authoring preview monitor", () => {
  it("keeps one muted warm player, promotes at the boundary, and follows only bound embedded audio", async () => {
    vi.spyOn(URL, "createObjectURL")
      .mockReturnValueOnce("blob:active")
      .mockReturnValueOnce("blob:warm");
    const revoke = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    const onFrameObserved = vi.fn();
    render(
      <AuthoringPreviewMonitor
        locale="en"
        identity={identity()}
        warmIdentity={identity({
          clipId: "clip-2",
          startFrame: 34,
          sourceStartFrame: 0,
        })}
        continuityRelation="contiguous"
        openPreview={async (request: AuthoringPreviewRequest) => ({
          blob: new Blob([request.clipId]),
          audioDisposition:
            request.clipId === "clip-1" ? "present_bound" : "absent",
        })}
        returnFocus={null}
        onClose={vi.fn()}
        onFrameObserved={onFrameObserved}
      />,
    );

    const active = (await screen.findByLabelText(
      "Preview clip-1",
    )) as HTMLVideoElement;
    const warm = document.querySelector(
      "video[data-h3-preview-role=warm]",
    ) as HTMLVideoElement | null;
    await waitFor(() => expect(warm?.src).toContain("blob:warm"));
    expect(warm?.muted).toBe(true);
    expect(warm?.tabIndex).toBe(-1);
    expect(warm?.getAttribute("aria-hidden")).toBe("true");
    fireEvent.loadedMetadata(active);
    expect(active.controls).toBe(false);
    expect(active.muted).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Play preview" }));
    fireEvent.play(active);
    expect(active.muted).toBe(false);
    expect(
      document.querySelector("[data-h3-embedded-audio=present_bound]"),
    ).not.toBeNull();

    fireEvent.ended(active);
    await screen.findByLabelText("Preview clip-2");
    expect(onFrameObserved).toHaveBeenCalledWith(34);
    expect(revoke).toHaveBeenCalledWith("blob:active");
    expect(
      document.querySelector("video[data-h3-preview-role=warm]"),
    ).toBeNull();
  });

  it("retains the active clip and reports single-clip fallback when warm opening fails", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:active");
    render(
      <AuthoringPreviewMonitor
        locale="en"
        identity={identity()}
        warmIdentity={identity({ clipId: "clip-2", startFrame: 34 })}
        continuityRelation="contiguous"
        openPreview={async (request: AuthoringPreviewRequest) => {
          if (request.clipId === "clip-2")
            throw Object.assign(new Error("private"), {
              disposition: "unsupported",
            });
          return {
            blob: new Blob(["active"]),
            audioDisposition: "absent" as const,
          };
        }}
        returnFocus={null}
        onClose={vi.fn()}
      />,
    );
    expect(await screen.findByLabelText("Preview clip-1")).toBeDefined();
    await waitFor(() =>
      expect(
        document.querySelector("[data-h3-continuity=single_clip_fallback]"),
      ).not.toBeNull(),
    );
    // IMPORTANT: keep this as a string; a slash-delimited form resembles a concrete path to the offline audit.
    expect(screen.queryByText("private")).toBeNull();
  });

  it("seeks the owned player from a controlled frame and reports observation", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
    const onFrameObserved = vi.fn();
    render(
      <AuthoringPreviewMonitor
        locale="en"
        identity={identity()}
        requestedFrame={22}
        onFrameObserved={onFrameObserved}
        openPreview={async () => ({
          blob: new Blob(["mp4"]),
          audioDisposition: "absent",
        })}
        returnFocus={null}
        onClose={vi.fn()}
      />,
    );
    const video = (await screen.findByLabelText(
      "Preview clip-1",
    )) as HTMLVideoElement;
    await waitFor(() => expect(video.currentTime).toBe(0.5));
    await waitFor(() => expect(onFrameObserved).toHaveBeenCalledWith(22));
  });

  it("pauses gaps without changing time or substituting the selected player", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => undefined);
    const props = {
      locale: "en" as const,
      identity: identity(),
      openPreview: async () => ({
        blob: new Blob(["mp4"]),
        audioDisposition: "absent" as const,
      }),
      returnFocus: null,
      onClose: vi.fn(),
    };
    const view = render(
      <AuthoringPreviewMonitor {...props} requestedFrame={22} />,
    );
    const video = (await screen.findByLabelText(
      "Preview clip-1",
    )) as HTMLVideoElement;
    await waitFor(() => expect(video.currentTime).toBe(0.5));
    view.rerender(
      <AuthoringPreviewMonitor
        {...props}
        requestedFrame={40}
        outsideSelectedClip
      />,
    );
    await waitFor(() => expect(pause).toHaveBeenCalled());
    expect(video.currentTime).toBe(0.5);
    expect(
      screen.getByText("Frame 40 is outside selected clip clip-1"),
    ).toBeDefined();
    expect(screen.queryByRole("button", { name: "Play preview" })).toBeNull();
    expect(screen.getByLabelText("Preview clip-1")).toBe(video);

    view.rerender(<AuthoringPreviewMonitor {...props} requestedFrame={23} />);
    await waitFor(() => expect(video.currentTime).toBeCloseTo(13 / 24));
    expect(screen.getByLabelText("Preview clip-1")).toBe(video);
  });

  it.each([
    [
      "en",
      "Preview clip-1",
      "Play preview",
      "Pause preview",
      "Close preview",
      "Preview playing",
    ],
    ["zh-TW", "預覽 clip-1", "播放預覽", "暫停預覽", "關閉預覽", "預覽播放中"],
    ["zh-CN", "预览 clip-1", "播放预览", "暂停预览", "关闭预览", "预览播放中"],
  ] as const)(
    "keeps the owned visual transport names exact in %s",
    async (locale, preview, play, pause, close, announcement) => {
      vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
      render(
        <AuthoringPreviewMonitor
          locale={locale}
          identity={identity()}
          openPreview={async () => ({
            blob: new Blob(["mp4"]),
            audioDisposition: "absent",
          })}
          returnFocus={null}
          onClose={vi.fn()}
        />,
      );
      const video = await screen.findByLabelText(preview);
      expect(screen.getByRole("button", { name: play })).toBeDefined();
      expect(screen.getByRole("button", { name: close })).toBeDefined();
      fireEvent.play(video);
      expect(screen.getByRole("button", { name: pause })).toBeDefined();
      expect(
        document.querySelector("[data-h3-preview-announcement]")?.textContent,
      ).toBe(announcement);
    },
  );

  it.each(["present_bound", "absent", "unavailable"] as const)(
    "renders one muted native player for %s audio and observes fallback media time",
    async (audioDisposition) => {
      vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
      const revoke = vi
        .spyOn(URL, "revokeObjectURL")
        .mockImplementation(() => undefined);
      render(
        <AuthoringPreviewMonitor
          locale="en"
          identity={identity()}
          openPreview={async () => ({
            blob: new Blob(["mp4"]),
            audioDisposition,
          })}
          returnFocus={null}
          onClose={vi.fn()}
        />,
      );
      const video = await screen.findByLabelText("Preview clip-1");
      expect(video.tagName).toBe("VIDEO");
      expect((video as HTMLVideoElement).controls).toBe(false);
      expect((video as HTMLVideoElement).muted).toBe(true);
      expect((video as HTMLVideoElement).playsInline).toBe(true);
      expect(video.getAttribute("preload")).toBe("metadata");
      expect(
        screen.getByRole("button", { name: "Play preview" }),
      ).toBeDefined();
      expect(screen.queryByRole("button", { name: /volume|mute/i })).toBeNull();
      Object.defineProperty(video, "currentTime", {
        value: 0.5,
        writable: true,
      });
      fireEvent.timeUpdate(video);
      expect(screen.getByText("Paused at frame 22")).toBeDefined();
      fireEvent.volumeChange(video);
      expect((video as HTMLVideoElement).muted).toBe(true);
      cleanup();
      expect(revoke).toHaveBeenCalledWith("blob:preview");
    },
  );

  it("uses one owned Play/Pause control and closes on play rejection", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
    const revoke = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);
    const play = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue(undefined);
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => undefined);
    render(
      <AuthoringPreviewMonitor
        locale="en"
        identity={identity()}
        openPreview={async () => ({
          blob: new Blob(["mp4"]),
          audioDisposition: "absent",
        })}
        returnFocus={null}
        onClose={vi.fn()}
      />,
    );
    const video = (await screen.findByLabelText(
      "Preview clip-1",
    )) as HTMLVideoElement;
    fireEvent.click(screen.getByRole("button", { name: "Play preview" }));
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
    fireEvent.play(video);
    fireEvent.click(screen.getByRole("button", { name: "Pause preview" }));
    expect(pause).toHaveBeenCalledTimes(1);

    play.mockRejectedValueOnce(new DOMException("blocked", "NotAllowedError"));
    fireEvent.pause(video);
    await userEvent.click(screen.getByRole("button", { name: "Play preview" }));
    expect(
      await screen.findByText(
        "Preview unavailable: the browser blocked playback. Press Play again.",
      ),
    ).toBeDefined();
    expect(screen.queryByLabelText("Preview clip-1")).toBeNull();
    expect(revoke).toHaveBeenCalledWith("blob:preview");
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Close preview" }),
    );
  });

  it("announces transport states without streaming observed frame values", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
    render(
      <AuthoringPreviewMonitor
        locale="en"
        identity={identity()}
        openPreview={async () => ({
          blob: new Blob(["mp4"]),
          audioDisposition: "absent",
        })}
        returnFocus={null}
        onClose={vi.fn()}
      />,
    );
    const video = (await screen.findByLabelText(
      "Preview clip-1",
    )) as HTMLVideoElement;
    const announcement = document.querySelector(
      "[data-h3-preview-announcement]",
    );
    expect(announcement?.textContent).toBe("Preview ready");
    Object.defineProperty(video, "currentTime", {
      value: 0.5,
      writable: true,
    });
    Object.defineProperty(video, "paused", {
      value: false,
      configurable: true,
    });
    fireEvent.play(video);
    expect(announcement?.textContent).toBe("Preview playing");
    fireEvent.timeUpdate(video);
    expect(announcement?.textContent).toBe("Preview playing");
    expect(announcement?.textContent).not.toContain("22");
  });

  it("prefers presented-frame callbacks and cancels the owned callback", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
    let presented:
      ((now: number, metadata: { mediaTime: number }) => void) | undefined;
    const request = vi.fn(
      (callback: (now: number, metadata: { mediaTime: number }) => void) => {
        presented = callback;
        return 17;
      },
    );
    const cancel = vi.fn();
    const prototype = HTMLVideoElement.prototype as unknown as Record<
      string,
      unknown
    >;
    const originalRequest = Object.getOwnPropertyDescriptor(
      prototype,
      "requestVideoFrameCallback",
    );
    const originalCancel = Object.getOwnPropertyDescriptor(
      prototype,
      "cancelVideoFrameCallback",
    );
    Object.defineProperties(HTMLVideoElement.prototype, {
      requestVideoFrameCallback: { configurable: true, value: request },
      cancelVideoFrameCallback: { configurable: true, value: cancel },
    });
    try {
      const opener = async () => ({
        blob: new Blob(["mp4"]),
        audioDisposition: "absent" as const,
      });
      const ObservedFrameHarness = () => {
        const [requestedFrame, setRequestedFrame] = useState(10);
        return (
          <AuthoringPreviewMonitor
            locale="en"
            identity={identity()}
            requestedFrame={requestedFrame}
            onFrameObserved={setRequestedFrame}
            openPreview={opener}
            returnFocus={null}
            onClose={vi.fn()}
          />
        );
      };
      render(<ObservedFrameHarness />);
      const video = await screen.findByLabelText("Preview clip-1");
      // IMPORTANT: the video can render before the separate passive effects subscribe and apply
      // the initial requested frame; an optional callback invocation would silently drop the frame
      // observation, and a native clock set before that flush is overwritten by the initial seek.
      await waitFor(() => expect(request).toHaveBeenCalled());
      Object.defineProperty(video, "currentTime", {
        value: 0.75,
        writable: true,
      });
      fireEvent.timeUpdate(video);
      expect(screen.queryByText("Paused at frame 28")).toBeNull();
      expect(presented).toBeTypeOf("function");
      act(() => presented!(0, { mediaTime: 0.5 }));
      expect(screen.getByText("Paused at frame 22")).toBeDefined();
      // The browser's current clock may already be ahead of the presented-frame
      // metadata. Returning observed frame 22 to the controlled prop is an ack,
      // not a command to seek the native clock backward to 0.5 seconds.
      expect((video as HTMLVideoElement).currentTime).toBe(0.75);
      cleanup();
      expect(cancel).toHaveBeenCalledWith(17);
    } finally {
      if (originalRequest === undefined)
        Reflect.deleteProperty(prototype, "requestVideoFrameCallback");
      else
        Object.defineProperty(
          prototype,
          "requestVideoFrameCallback",
          originalRequest,
        );
      if (originalCancel === undefined)
        Reflect.deleteProperty(prototype, "cancelVideoFrameCallback");
      else
        Object.defineProperty(
          prototype,
          "cancelVideoFrameCallback",
          originalCancel,
        );
    }
  });

  it("revokes and reports a content-free native media failure", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
    const revoke = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);
    render(
      <AuthoringPreviewMonitor
        locale="en"
        identity={identity()}
        openPreview={async () => ({
          blob: new Blob(["mp4"]),
          audioDisposition: "absent",
        })}
        returnFocus={null}
        onClose={vi.fn()}
      />,
    );
    fireEvent.error(await screen.findByLabelText("Preview clip-1"));
    expect(await screen.findByRole("alert")).toBeDefined();
    expect(screen.getByText("Preview failed")).toBeDefined();
    expect(screen.queryByLabelText("Preview clip-1")).toBeNull();
    expect(revoke).toHaveBeenCalledWith("blob:preview");
    expect(screen.getByRole("button", { name: "Close preview" })).toBeDefined();
  });

  it("closes idempotently, revokes, and restores the exact local trigger", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
    const revoke = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);
    const trigger = document.createElement("button");
    document.body.append(trigger);
    const focus = vi.spyOn(trigger, "focus");
    const onClose = vi.fn();
    render(
      <AuthoringPreviewMonitor
        locale="en"
        identity={identity()}
        openPreview={async () => ({
          blob: new Blob(["mp4"]),
          audioDisposition: "absent",
        })}
        returnFocus={trigger}
        onClose={onClose}
      />,
    );
    await screen.findByLabelText("Preview clip-1");
    fireEvent.click(screen.getByRole("button", { name: "Close preview" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(revoke).toHaveBeenCalledTimes(1);
    expect(focus).toHaveBeenCalledTimes(1);
    trigger.remove();
  });

  it("shows a bounded unavailable state without hiding close", async () => {
    render(
      <AuthoringPreviewMonitor
        locale="en"
        identity={identity()}
        openPreview={async () => {
          throw Object.assign(new Error("private detail"), {
            disposition: "stale",
          });
        }}
        returnFocus={null}
        onClose={vi.fn()}
      />,
    );
    expect((await screen.findByRole("status")).textContent).toBe(
      "Preview unavailable: the clip changed. Reopen the preview.",
    );
    expect(screen.queryByText(/private detail/)).toBeNull();
    expect(screen.getByRole("button", { name: "Close preview" })).toBeDefined();
  });
});
