import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { H3Sidebar, type SidebarNleBinding } from "../src/components/H3Sidebar";
import { SIDEBAR_EDITOR_UI_CONTRACT_V3 } from "../src/contracts/sidebarEditorUiContract";
import { projectionFixture } from "./support/authoringFixture";
import { authoringReady, SMOKE_SHAPE } from "./support/nleWorkspaceFixture";
import { initialShellState } from "../src/state/shellState";
import {
  initialNleImportState,
  initialNleSurfaceState,
} from "../src/state/nleWorkspaceState";
import { createSidebarRetention } from "../src/state/sidebarRetention";

afterEach(cleanup);

const pages = [
  { id: "context" as const },
  { id: "production" as const },
  { id: "settings" as const },
];

function nleBinding(
  requested: SidebarNleBinding["requestedFunction"],
  onFunctionChange = vi.fn(),
): SidebarNleBinding {
  return {
    surface: initialNleSurfaceState,
    supported: true,
    onOpen: vi.fn(),
    onFunctionChange,
    requestedFunction: requested,
    importAction: {
      state: initialNleImportState,
      selectionState: () => ({ eligible: false, reason: null, busy: false }),
      onImport: vi.fn(),
      onRetry: vi.fn(),
      onOpen: vi.fn(),
    },
  };
}

function selectedFunction(): string | null {
  return (
    screen
      .getAllByRole("tab")
      .find((tab) => tab.getAttribute("aria-selected") === "true")
      ?.getAttribute("data-h3-director-function") ?? null
  );
}

describe("M25 sidebar editor UI contract", () => {
  it("freezes the one-page-shell, two-function and retention identities", () => {
    expect(SIDEBAR_EDITOR_UI_CONTRACT_V3).toEqual({
      schema: "h3.context.sidebar_editor_ui.v3",
      topLevelPages: ["context", "production", "settings"],
      productionFunctions: ["production_workbench", "clip_editor"],
      initialProductionFunction: "production_workbench",
      persistence: "bounded_session_memory",
      selectors: {
        tabs: "v1",
        productionWorkbench: "production_workbench",
        clipEditor: "clip_editor",
      },
      clipEditorSurface: "launcher_and_summary",
      retention: {
        lifetime: "extension_session",
        restore: "same_authority_scope",
        transport: "paused",
        fullEditor: "explicit_open",
        surfaces: {
          navigation: {
            page: "retain",
            production_function: "retain",
            roving_focus: "reconcile",
            focus_identity: "retain",
            diagnostics_feedback: "clear",
          },
          context: {
            app_mode_draft: "retain",
            editing_stage: "retain",
            workspace_stage_drafts: "reconcile",
            anchor_designation: "clear",
            reference_picker: "clear",
            semantic_review: "clear",
          },
          production_workbench: {
            segment_selection: "retain",
            relation_draft: "reconcile",
            move_targets: "reconcile",
            segment_window: "reconcile",
            authority_expansion: "retain",
            release_confirmation: "clear",
            drag_gesture: "clear",
            media_preview: "clear",
            proposal_review: "clear",
          },
          clip_editor: {
            clip_selection: "retain",
            overlay_open: "clear",
            overlay_geometry: "reconcile",
            overlay_layout: "reconcile",
            overlay_pane: "retain",
            timeline_view: "retain",
            timeline_measurement: "clear",
            trim_gesture: "clear",
            playhead: "reconcile",
            playback: "clear",
            source_preview: "clear",
            track_draft: "reconcile",
            insert_draft: "reconcile",
            range_draft: "reconcile",
            clip_draft: "reconcile",
            summary_release_confirmation: "clear",
          },
          settings: {
            section_expansion: "retain",
            provider_selection: "retain",
            language: "retain",
            credential: "clear",
          },
        },
      },
    });
  });

  it("uses roving focus with manual activation and no hidden panel work", () => {
    const onProductionIntent = vi.fn();
    const onAuthoringIntent = vi.fn();
    const openPreview = vi.fn();
    const view = render(
      <H3Sidebar
        state={initialShellState}
        pageRegistry={{ selected: "production", pages }}
        productionState={{ status: "absent" }}
        authoringState={{ status: "absent" }}
        onProductionIntent={onProductionIntent}
        onAuthoringIntent={onAuthoringIntent}
        onAuthoringMediaPreview={openPreview}
      />,
    );

    const tablist = screen.getByRole("tablist", {
      name: "Production functions",
    });
    expect(tablist.getAttribute("data-h3-director-function-tabs")).toBe("v1");
    const production = screen.getByRole("tab", { name: "Production" });
    const editor = screen.getByRole("tab", { name: "Clip editor" });
    expect(production.getAttribute("aria-selected")).toBe("true");
    expect(production.tabIndex).toBe(0);
    expect(editor.tabIndex).toBe(-1);
    expect(
      view.container.querySelectorAll("[data-h3-director-panel]"),
    ).toHaveLength(1);
    expect(
      view.container.querySelector(
        "[data-h3-director-panel='production_workbench']",
      ),
    ).not.toBeNull();
    expect(
      screen.queryByRole("button", {
        name: "Start authoring from this context",
      }),
    ).toBeNull();

    production.focus();
    fireEvent.keyDown(production, { key: "ArrowRight" });
    expect(document.activeElement).toBe(editor);
    expect(editor.getAttribute("aria-selected")).toBe("false");
    expect(screen.queryByText(/No authoring workspace/)).toBeNull();
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(editor.getAttribute("aria-selected")).toBe("true");
    expect(
      view.container.querySelector("[data-h3-director-panel='clip_editor']"),
    ).not.toBeNull();
    expect(
      screen.getByRole("button", {
        name: "Start authoring from this context",
      }),
    ).toBeDefined();
    // M25-44 (one NLE): the tab holds the summary, never a second editing surface.
    expect(
      screen.getByRole("region", { name: "Clip editor project" }),
    ).toBeDefined();
    expect(
      screen.queryByRole("region", { name: "Reference & timeline authoring" }),
    ).toBeNull();
    expect(onProductionIntent).not.toHaveBeenCalled();
    expect(onAuthoringIntent).not.toHaveBeenCalled();
    expect(openPreview).not.toHaveBeenCalled();

    fireEvent.keyDown(editor, { key: "Home" });
    expect(document.activeElement).toBe(production);
    expect(editor.getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(production, { key: " " });
    expect(production.getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(production, { key: "End" });
    expect(document.activeElement).toBe(editor);
    fireEvent.keyDown(editor, { key: " " });
    expect(editor.getAttribute("aria-selected")).toBe("true");
    expect(onProductionIntent).not.toHaveBeenCalled();
    expect(onAuthoringIntent).not.toHaveBeenCalled();
  });

  it("persists for top-level round trips and localizes", () => {
    const view = render(
      <H3Sidebar
        state={initialShellState}
        locale="zh-TW"
        pageRegistry={{ selected: "production", pages }}
      />,
    );
    const clip = screen.getByRole("tab", { name: "剪輯器" });
    fireEvent.click(clip);
    view.rerender(
      <H3Sidebar
        state={initialShellState}
        locale="zh-TW"
        pageRegistry={{ selected: "context", pages }}
      />,
    );
    expect(screen.queryByRole("tab", { name: "剪輯器" })).toBeNull();
    view.rerender(
      <H3Sidebar
        state={initialShellState}
        locale="zh-CN"
        pageRegistry={{ selected: "production", pages }}
      />,
    );
    expect(
      screen.getByRole("tab", { name: "剪辑器" }).getAttribute("aria-selected"),
    ).toBe("true");
  });

  // M25-21 section 14.4 supersedes M25-16's mounted-view-only reset: the selected function
  // survives view destroy/remount through the session store and resets only on full disposal.
  it("retains the function across view destroy and resets it only on full disposal", () => {
    const retention = createSidebarRetention();
    const onFunctionChange = vi.fn();
    const first = render(
      <H3Sidebar
        state={initialShellState}
        pageRegistry={{ selected: "production", pages }}
        nle={nleBinding({ id: "clip_editor", generation: 0 }, onFunctionChange)}
        retention={retention}
      />,
    );
    const editor = screen.getByRole("tab", { name: "Clip editor" });
    editor.focus();
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(selectedFunction()).toBe("clip_editor");
    expect(onFunctionChange).toHaveBeenCalledExactlyOnceWith("clip_editor");
    first.unmount();

    const second = render(
      <H3Sidebar
        state={initialShellState}
        pageRegistry={{ selected: "production", pages }}
        nle={nleBinding({ id: "clip_editor", generation: 0 }, onFunctionChange)}
        retention={retention}
      />,
    );
    expect(selectedFunction()).toBe("clip_editor");
    // The roving tab stop follows the restored selection, and restoring issued no switch.
    expect(screen.getByRole("tab", { name: "Clip editor" }).tabIndex).toBe(0);
    expect(screen.getByRole("tab", { name: "Production" }).tabIndex).toBe(-1);
    expect(onFunctionChange).toHaveBeenCalledTimes(1);
    second.unmount();

    retention.dispose();
    render(
      <H3Sidebar
        state={initialShellState}
        pageRegistry={{ selected: "production", pages }}
        retention={retention}
      />,
    );
    expect(selectedFunction()).toBe("production_workbench");
    expect(retention.size()).toBe(0);
  });

  it("M25-44 summary starts, refreshes and releases the Authoring workspace without editing", () => {
    const onAuthoringIntent = vi.fn();
    const onStartAuthoring = vi.fn();
    const clipEditor = { id: "clip_editor" as const, generation: 1 };
    const binding = {
      ...nleBinding(clipEditor),
      onStartAuthoring,
      planning: {
        state: {} as never,
        contextAvailable: true,
        actions: {} as never,
      },
    };
    const view = render(
      <H3Sidebar
        state={initialShellState}
        pageRegistry={{ selected: "production", pages }}
        authoringState={{ status: "absent" }}
        onAuthoringIntent={onAuthoringIntent}
        nle={binding}
      />,
    );
    const start = screen.getByRole("button", {
      name: "Start authoring from this context",
    }) as HTMLButtonElement;
    expect(start.disabled).toBe(false);
    fireEvent.click(start);
    expect(onStartAuthoring).toHaveBeenCalledOnce();
    expect(onAuthoringIntent).not.toHaveBeenCalled();

    // A workspace without a loaded timeline refreshes its projection.
    view.rerender(
      <H3Sidebar
        state={initialShellState}
        pageRegistry={{ selected: "production", pages }}
        authoringState={{ status: "ready", projection: projectionFixture() }}
        onAuthoringIntent={onAuthoringIntent}
        nle={binding}
      />,
    );
    expect(screen.getByRole("status").textContent).toContain(
      "Open the full editor",
    );
    fireEvent.click(screen.getByRole("button", { name: "Refresh workspace" }));
    expect(onAuthoringIntent).toHaveBeenLastCalledWith({
      action: "read_projection",
    });

    // With a timeline the summary counts it, refreshes the history and releases in two steps.
    view.rerender(
      <H3Sidebar
        state={initialShellState}
        pageRegistry={{ selected: "production", pages }}
        authoringState={authoringReady(SMOKE_SHAPE)}
        onAuthoringIntent={onAuthoringIntent}
        nle={binding}
      />,
    );
    expect(
      view.container.querySelector("[data-h3-nle-summary-counts]")?.textContent,
    ).toMatch(/^\d+ tracks, \d+ clips · Duration \d\d:\d\d:\d\d:\d\d$/u);
    fireEvent.click(screen.getByRole("button", { name: "Refresh workspace" }));
    expect(onAuthoringIntent).toHaveBeenLastCalledWith({
      action: "read_timeline_history",
    });
    const calls = onAuthoringIntent.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Release workspace" }));
    fireEvent.click(screen.getByRole("button", { name: "Keep workspace" }));
    expect(onAuthoringIntent).toHaveBeenCalledTimes(calls);
    fireEvent.click(screen.getByRole("button", { name: "Release workspace" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm release" }));
    expect(onAuthoringIntent).toHaveBeenLastCalledWith({
      action: "release_workspace",
    });
    // No editing control of any kind is rendered in the tab.
    expect(view.container.querySelector("[data-h3-nle-control]")).toBeNull();
    expect(view.container.querySelector('[role="slider"]')).toBeNull();
  });

  it("never replays an applied one-shot function request on remount", () => {
    const retention = createSidebarRetention();
    const requested = { id: "clip_editor" as const, generation: 1 };
    const first = render(
      <H3Sidebar
        state={initialShellState}
        pageRegistry={{ selected: "production", pages }}
        nle={nleBinding(requested)}
        retention={retention}
      />,
    );
    expect(selectedFunction()).toBe("clip_editor");
    fireEvent.click(screen.getByRole("tab", { name: "Production" }));
    expect(selectedFunction()).toBe("production_workbench");
    first.unmount();

    const second = render(
      <H3Sidebar
        state={initialShellState}
        pageRegistry={{ selected: "production", pages }}
        nle={nleBinding(requested)}
        retention={retention}
      />,
    );
    expect(selectedFunction()).toBe("production_workbench");
    // A request issued after the remount is new and still applies exactly once.
    second.rerender(
      <H3Sidebar
        state={initialShellState}
        pageRegistry={{ selected: "production", pages }}
        nle={nleBinding({ id: "clip_editor", generation: 2 })}
        retention={retention}
      />,
    );
    expect(selectedFunction()).toBe("clip_editor");
  });
});
