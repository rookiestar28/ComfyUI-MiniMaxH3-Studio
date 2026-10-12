import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PerceptionHostStatus } from "../src/components/PerceptionHostStatus";
import { SettingsPage } from "../src/components/SettingsPage";
import type { PerceptionHostStatus as Status } from "../src/contracts/perceptionHostCodec";
import type { PerceptionHostBinding } from "../src/state/perceptionHostState";
import { createManagedJournal } from "../src/state/managedJournal";

afterEach(cleanup);
const visual: Status = {
  schema: "h3.context.perception_host_status.v1",
  state: "configured",
  visual: true,
  audio: false,
  reason: "none",
  cancellation_observed: false,
};
function binding(status: Status | null = visual): PerceptionHostBinding {
  return {
    state: { status, busy: false, error: null },
    ensure: vi.fn(),
    leave: vi.fn(),
    refresh: vi.fn(async () => {}),
  };
}

describe("read-only local perception Settings block", () => {
  it.each([
    ["en", "Local perception", "Refresh status"],
    ["zh-TW", "本機感知", "重新整理狀態"],
    ["zh-CN", "本地感知", "刷新状态"],
  ] as const)(
    "uses %s user-facing copy with no executable or path editor",
    (locale, title, refresh) => {
      const { container } = render(
        <PerceptionHostStatus locale={locale} binding={binding()} />,
      );
      expect(screen.getByRole("heading", { name: title })).toBeTruthy();
      expect(screen.getByRole("button", { name: refresh })).toBeTruthy();
      expect(
        container.querySelectorAll("input, select, textarea"),
      ).toHaveLength(0);
    },
  );

  it("renders the visual/audio decisions and cancellation limitation separately", () => {
    render(<PerceptionHostStatus locale="en" binding={binding()} />);
    expect(
      screen.getByRole("status", { name: "Visual analysis" }).textContent,
    ).toBe("Ready");
    expect(
      screen.getByRole("status", { name: "Audio analysis" }).textContent,
    ).toBe("Unavailable");
    expect(screen.getByText("Host cancellation is not observed.")).toBeTruthy();
  });

  it.each([
    ["not_configured", "settings_missing", "Not configured"],
    ["refused", "path_refused", "Configuration refused"],
  ] as const)(
    "renders the finite %s disposition and a restart remedy",
    (state, reason, text) => {
      render(
        <PerceptionHostStatus
          locale="en"
          binding={binding({ ...visual, state, reason, visual: false })}
        />,
      );
      expect(screen.getByText(text)).toBeTruthy();
      expect(screen.getByText(/restart ComfyUI/)).toBeTruthy();
    },
  );

  it("requests one read on mount, refreshes explicitly and leaves on unmount", async () => {
    const state = binding();
    const { unmount } = render(
      <PerceptionHostStatus locale="en" binding={state} />,
    );
    expect(state.ensure).toHaveBeenCalledTimes(1);
    await userEvent.click(
      screen.getByRole("button", { name: "Refresh status" }),
    );
    expect(state.refresh).toHaveBeenCalledTimes(1);
    unmount();
    expect(state.leave).toHaveBeenCalledTimes(1);
  });

  it("disables duplicate refresh while busy and renders finite transport failure", () => {
    const state = binding(null);
    const { rerender } = render(
      <PerceptionHostStatus
        locale="en"
        binding={{ ...state, state: { ...state.state, busy: true } }}
      />,
    );
    expect(
      (
        screen.getByRole("button", {
          name: "Refresh status",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    rerender(
      <PerceptionHostStatus
        locale="en"
        binding={{
          ...state,
          state: { ...state.state, error: "transport_failure" },
        }}
      />,
    );
    expect(screen.getByText("Status unavailable")).toBeTruthy();
    expect(
      (
        screen.getByRole("button", {
          name: "Refresh status",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
  });

  it("is connected through the real Settings page", () => {
    const state = binding();
    const properties = {
      locale: "en" as const,
      snapshot: { status: "ready", value: "auto", pending: false } as const,
      onWrite: vi.fn(async () => true),
      perceptionHost: state,
    };
    render(<SettingsPage {...properties} />);
    expect(
      screen.getByRole("heading", { name: "Local perception" }),
    ).toBeTruthy();
    expect(state.ensure).toHaveBeenCalledTimes(1);
  });
});

describe("clipboard perception metadata", () => {
  it("copies only finite verified readiness without recording it in the run journal", () => {
    const journal = createManagedJournal();
    const before = journal.snapshot();
    const metadata = {
      packageVersion: "1.1.0",
      locale: "en",
      perceptionHost: visual,
    };
    const text = journal.compose(metadata);
    expect(text).toContain("perception.state=configured");
    expect(text).toContain("perception.visual=true");
    expect(text).toContain("perception.audio=false");
    expect(text).toContain("perception.cancellation_observed=false");
    expect(journal.snapshot()).toEqual(before);
  });

  it.each([
    { ...visual, python_executable: "untrusted_operator_value" },
    { ...visual, reason: "untrusted_operator_value" },
    { ...visual, cancellation_observed: "untrusted_operator_value" },
    { ...visual, visual: false },
  ])("omits hostile or inconsistent metadata entirely", (status) => {
    const metadata = {
      packageVersion: "1.1.0",
      locale: "en",
      perceptionHost: status,
    };
    const text = createManagedJournal().compose(metadata);
    expect(text).not.toContain("untrusted_operator_value");
    expect(text).not.toContain("perception.");
  });

  it("does not propagate an exception from hostile metadata access", () => {
    const hostile = { ...visual };
    Object.defineProperty(hostile, "reason", {
      enumerable: true,
      get() {
        throw new Error("untrusted_operator_value");
      },
    });
    const metadata = {
      packageVersion: "1.1.0",
      locale: "en",
      perceptionHost: hostile,
    };
    expect(() => createManagedJournal().compose(metadata)).not.toThrow();
    expect(createManagedJournal().compose(metadata)).not.toContain(
      "untrusted_operator_value",
    );
  });
});
