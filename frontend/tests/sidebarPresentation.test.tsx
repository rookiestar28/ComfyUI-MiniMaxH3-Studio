import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SidebarStages } from "../src/components/SidebarStages";
import { H3Sidebar } from "../src/components/H3Sidebar";
import type { SidebarWorkspaceProjection } from "../src/contracts/sidebarWorkspaceCodec";
import { sidebarCopy } from "../src/i18n/catalog";
import { validSidebarWorkspace } from "./sidebarWorkspaceFixture";

describe("H3 sidebar presentation contract", () => {
  afterEach(() => {
    cleanup();
  });

  it("keeps five ordered accessible stages while exposing redundant stage accents", () => {
    render(
      <SidebarStages
        projection={validSidebarWorkspace}
        locale="en"
        busy={false}
        onAction={() => undefined}
        onClientFailure={() => undefined}
      />,
    );
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(5);
    expect(tabs.map((tab) => tab.getAttribute("data-stage"))).toEqual([
      "intent",
      "media",
      "understand",
      "audit",
      "execute",
    ]);
    for (const [index, stage] of [
      "intent",
      "media",
      "understand",
      "audit",
      "execute",
    ].entries()) {
      expect(tabs[index]?.classList.contains(`h3-stage-tab--${stage}`)).toBe(
        true,
      );
    }
    expect(screen.getByRole("tabpanel").getAttribute("data-stage")).toBe(
      "execute",
    );
    expect(tabs[4]?.getAttribute("aria-selected")).toBe("true");
  });

  it("retains blocked status as the authoritative override", () => {
    const workspace = validSidebarWorkspace as SidebarWorkspaceProjection;
    const blocked: SidebarWorkspaceProjection = {
      ...workspace,
      lifecycle: "blocked",
      stages: workspace.stages.map((stage, index) =>
        index === 2 ? { ...stage, status: "blocked" as const } : stage,
      ),
    };
    render(
      <SidebarStages
        projection={blocked}
        locale="en"
        busy={false}
        onAction={() => undefined}
        onClientFailure={() => undefined}
      />,
    );
    const blockedTab = screen.getByRole("tab", { name: "Understand / Plan" });
    expect(blockedTab.classList.contains("h3-stage-tab--understand")).toBe(
      true,
    );
    expect(blockedTab.classList.contains("h3-stage-tab--blocked")).toBe(true);
  });

  it("renders guide readiness separately from the structural workspace lifecycle", () => {
    const view = render(
      <SidebarStages
        projection={{
          ...validSidebarWorkspace,
          guide_conformance: {
            schema: "h3.context.guide_conformance.v2",
            readiness: "incomplete",
            reasons: ["fidelity.soundscape.unspecified"],
          },
        }}
        locale="en"
        busy={false}
        onAction={() => undefined}
        onClientFailure={() => undefined}
      />,
    );
    expect(
      view.container.querySelector('[data-guide-readiness="incomplete"]')
        ?.textContent,
    ).toContain("Guide readiness");
    expect(view.container.textContent).toContain("Incomplete");

    view.rerender(
      <SidebarStages
        projection={validSidebarWorkspace}
        locale="en"
        busy={false}
        onAction={() => undefined}
        onClientFailure={() => undefined}
      />,
    );
    expect(
      view.container.querySelector('[data-guide-readiness="ready"]')
        ?.textContent,
    ).toContain("Ready");
  });

  it("keeps a malformed existing graph on an explicit replacement path", () => {
    render(
      <H3Sidebar
        state={{
          status: "interactive",
          reason: "incompatible_graph",
          existingGraph: true,
        }}
        appMode={{
          capability: { status: "ready" },
          durationResolution: {
            status: "resolved",
            resolution: {
              schema: "h3.context.duration_resolution.v1",
              requested_seconds: 5,
              requested_milliseconds: 5000,
              effective_milliseconds: 5167,
              frame_count: 124,
              snapped: true,
            },
          },
          onStart: () => undefined,
        }}
        onChooseNative={() => undefined}
      />,
    );
    // M17-27: assert the verdict is announced, not that it contains the word
    // "incompatible". That word was a proxy for the reason code, and pinning it
    // stopped the copy from being able to say which route the verdict is about.
    expect(
      screen.getAllByText(sidebarCopy("en").incompatible).length,
    ).toBeGreaterThan(0);
    expect(
      screen
        .getByRole("button", { name: "Replace canvas and start H3 App Mode" })
        .getAttribute("disabled"),
    ).toBeNull();
  });

  it("exposes cancellation as an accessible next action", () => {
    render(
      <H3Sidebar
        state={{ status: "working", phase: "materializing", transactionId: 1 }}
        appMode={{
          capability: { status: "ready" },
          onStart: () => undefined,
          onCancel: () => undefined,
        }}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Cancel App Mode" }),
    ).toBeTruthy();
  });

  it("does not offer local cancellation after the host queue was submitted", () => {
    render(
      <H3Sidebar
        state={{ status: "working", phase: "queueing", transactionId: 2 }}
        appMode={{
          capability: { status: "ready" },
          onStart: () => undefined,
          onCancel: () => undefined,
        }}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "Cancel App Mode" }),
    ).toBeNull();
    expect(
      screen.getAllByText("Working on the visible Base graph.").length,
    ).toBeGreaterThan(0);
  });
});
