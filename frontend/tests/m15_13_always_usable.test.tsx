import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { H3Sidebar } from "../src/components/H3Sidebar";
import { initialShellState } from "../src/state/shellState";

const readyAppMode = {
  durationResolution: {
    status: "resolved" as const,
    resolution: {
      schema: "h3.context.duration_resolution.v1" as const,
      requested_seconds: 5,
      requested_milliseconds: 5000,
      effective_milliseconds: 5167,
      frame_count: 124,
      snapped: true,
    },
  },
  capability: { status: "ready" as const },
  onStart: vi.fn(),
  onCancel: vi.fn(),
};

describe("M15-13 always-usable Base shell", () => {
  afterEach(cleanup);

  it("renders an interactive Base form and functional stage tabs on an empty first paint", () => {
    render(
      <H3Sidebar
        state={initialShellState}
        appMode={readyAppMode}
        onChooseNative={vi.fn()}
      />,
    );

    expect(screen.getByRole("textbox", { name: /intent/i })).toBeTruthy();
    expect(
      screen.getByRole("spinbutton", { name: /duration \(seconds\)/i }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /start h3 app mode/i }),
    ).toBeTruthy();
    const taskMode = screen.getByRole("combobox", { name: /task mode/i });
    expect(taskMode).toBeTruthy();
    expect(taskMode.querySelectorAll("option")).toHaveLength(5);
    expect(
      screen.queryByText(/assisted reconstruction unavailable/i),
    ).toBeNull();

    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(5);
    fireEvent.click(screen.getByRole("tab", { name: /media/i }));
    expect(screen.getByRole("tabpanel").textContent).toMatch(/media/i);
    expect(screen.getByRole("tabpanel").querySelector("button")).toBeTruthy();
  });

  it("keeps the Base form available without repeating an achieved native action", () => {
    const onChooseNative = vi.fn();
    render(
      <H3Sidebar
        state={{ status: "interactive", reason: "native_preference" }}
        appMode={readyAppMode}
        onChooseNative={onChooseNative}
      />,
    );

    expect(screen.queryByRole("button", { name: /native nodes/i })).toBeNull();
    expect(onChooseNative).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: /intent/i })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /start h3 app mode/i }),
    ).toBeTruthy();
  });

  it("makes dirty-graph decisions explicit without queueing or mutating", () => {
    render(
      <H3Sidebar
        state={{
          status: "interactive",
          reason: "dirty_graph",
          existingGraph: true,
        }}
        appMode={{ ...readyAppMode, existingGraph: true }}
        onChooseNative={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: /replace canvas and start/i }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", {
        name: /keep canvas and exit h3 app mode|native nodes/i,
      }),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: /queue current h3 graph/i }),
    ).toBeNull();
  });

  it("uses one safe alert for classified errors while preserving retry", () => {
    render(
      <H3Sidebar
        state={{
          status: "error",
          code: "queue_failed",
          severity: "error",
          source: "queue",
          message: "The normal queue rejected this run.",
          recovery: "retry",
        }}
        appMode={readyAppMode}
      />,
    );

    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /retry/i })).toBeTruthy();
    expect(screen.getAllByRole("status").length).toBeGreaterThan(0);
  });

  it("shows only the classified recovery action for an App Mode error", () => {
    render(
      <H3Sidebar
        state={{
          status: "error",
          code: "queue_failed",
          severity: "error",
          source: "queue",
          message: "The normal queue rejected this run.",
          recovery: "retry",
        }}
        appMode={readyAppMode}
        onChooseNative={vi.fn()}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.queryByRole("textbox", { name: /intent/i })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /start h3 app mode/i }),
    ).toBeNull();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: /retry/i })).toHaveLength(1);
  });

  it("does not stack a workspace alert on the shell recovery alert", () => {
    render(
      <H3Sidebar
        state={{
          status: "error",
          code: "queue_failed",
          severity: "error",
          source: "queue",
          message: "The normal queue rejected this run.",
          recovery: "retry",
        }}
        workspaceState={{ status: "error", reason: "request_failed" }}
        appMode={readyAppMode}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /retry/i })).toBeTruthy();
  });

  it("disables every stage tab while App Mode is working", () => {
    render(
      <H3Sidebar
        state={{ status: "working", phase: "materializing", transactionId: 3 }}
        appMode={readyAppMode}
      />,
    );
    expect(
      screen.getAllByRole("tab").every((tab) => tab.hasAttribute("disabled")),
    ).toBe(true);
  });
});
