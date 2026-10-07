import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { H3Sidebar } from "../src/components/H3Sidebar";
import { initialShellState } from "../src/state/shellState";

const readyAppMode = {
  capability: { status: "ready" as const },
  onStart: () => undefined,
};

function NativePreferenceHarness() {
  const [state, setState] = useState<
    | { status: "blocked"; reason: "dirty_graph" }
    | { status: "node-only"; reason: "explicit_native_choice" }
  >({ status: "blocked", reason: "dirty_graph" });
  return (
    <H3Sidebar
      state={state}
      appMode={readyAppMode}
      onChooseNodeOnly={() =>
        setState({ status: "node-only", reason: "explicit_native_choice" })
      }
    />
  );
}

describe("M15-12 open-box RED contract", () => {
  afterEach(() => {
    cleanup();
  });

  it("RED-M15-12-01: opens an interactive form on an empty canvas", () => {
    render(<H3Sidebar state={initialShellState} appMode={readyAppMode} />);
    expect(screen.getByRole("textbox", { name: /intent/i })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /start h3 app mode/i }),
    ).toBeTruthy();
  });

  it("RED-M15-12-02: keeps the H3 entry reversible after native preference", () => {
    render(<NativePreferenceHarness />);
    fireEvent.click(
      screen.getByRole("button", { name: /continue with native nodes/i }),
    );
    expect(screen.getByRole("textbox", { name: /intent/i })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /return to h3 app mode/i }),
    ).toBeTruthy();
  });

  it("RED-M15-12-03: removes fallback filler from the normal shell", () => {
    render(<H3Sidebar state={initialShellState} appMode={readyAppMode} />);
    expect(screen.queryByText(/prompt export awaiting projection/i)).toBeNull();
    expect(
      screen.queryByText(/assisted reconstruction unavailable/i),
    ).toBeNull();
  });

  it("RED-M15-12-04: does not expose a false one-option mode selector", () => {
    render(
      <H3Sidebar
        state={{ status: "app-mode", reason: "awaiting_input" }}
        appMode={readyAppMode}
      />,
    );
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("RED-M15-12-05: treats cancellation as recoverable status, not an alert", () => {
    render(
      <H3Sidebar
        state={{ status: "app-mode", reason: "cancelled" }}
        appMode={readyAppMode}
      />,
    );
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
    expect(
      screen.getByRole("button", { name: /restart h3 app mode/i }),
    ).toBeTruthy();
  });

  it("RED-M15-12-06: exposes an explicit keep-current-canvas decision", () => {
    render(
      <H3Sidebar
        state={{ status: "blocked", reason: "dirty_graph" }}
        appMode={readyAppMode}
        onChooseNodeOnly={() => undefined}
      />,
    );
    expect(
      screen.getByRole("button", { name: /keep current canvas/i }),
    ).toBeTruthy();
  });

  it("RED-M15-12-07: separates the five stage purposes in the interactive shell", () => {
    render(<H3Sidebar state={initialShellState} appMode={readyAppMode} />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(5);
    for (const tab of tabs) {
      fireEvent.click(tab);
      expect(tab.getAttribute("aria-selected")).toBe("true");
      const panel = screen.getByRole("tabpanel");
      expect(panel.getAttribute("aria-labelledby")).toBe(tab.id);
      expect(within(panel).getByText(/purpose:/i)).toBeTruthy();
      expect(within(panel).getByRole("button")).toBeTruthy();
    }
  });

  it("RED-M15-12-08: gives a real error exactly one retry action", () => {
    render(
      <H3Sidebar
        state={{ status: "app-mode", reason: "error" }}
        appMode={{ ...readyAppMode, error: "queue_failed" }}
      />,
    );
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", { name: /retry h3 app mode/i }),
    ).toBeTruthy();
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  it("RED-M15-12-09: binds responsive reflow to the mounted panel width", () => {
    const css = readFileSync(
      resolve(process.cwd(), "src/styles/tokens.css"),
      "utf8",
    );
    expect(css).toMatch(/container-type:\s*inline-size/);
    expect(css).toMatch(/@container/);
    expect(css).not.toMatch(/@media\s*\(max-width:\s*480px\)/);
  });
});
