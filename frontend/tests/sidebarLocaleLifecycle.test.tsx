import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  H3Sidebar,
  initialAppModeDraft,
  type AppModeDraft,
} from "../src/components/H3Sidebar";
import {
  initialSidebarStagesDraft,
  SidebarStages,
} from "../src/components/SidebarStages";
import { initialShellState } from "../src/state/shellState";
import { validSidebarWorkspace } from "./sidebarWorkspaceFixture";

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
};

afterEach(cleanup);

describe("M17-00 live locale and view lifecycle", () => {
  it.each(["en", "zh-TW", "zh-CN"] as const)(
    "keeps semantic keyboard order and visible labels in %s",
    (locale) => {
      const view = render(
        <H3Sidebar
          state={initialShellState}
          appMode={readyAppMode}
          locale={locale}
        />,
      );
      const navigation = screen.getByRole("navigation");
      expect(navigation.textContent?.trim().length).toBeGreaterThan(0);
      const tabs = screen.getAllByRole("tab");
      expect(tabs).toHaveLength(5);
      expect(
        tabs.every((tab) => (tab.getAttribute("aria-label") ?? "").length > 0),
      ).toBe(true);
      tabs[1]?.focus();
      expect(document.activeElement).toBe(tabs[1]);
      view.unmount();
    },
  );

  it("changes shell copy without losing React state or keyboard focus", () => {
    const view = render(
      <H3Sidebar
        state={initialShellState}
        appMode={readyAppMode}
        locale="en"
      />,
    );
    const intent = screen.getByRole("textbox", { name: "Intent" });
    fireEvent.change(intent, { target: { value: "Keep this private draft" } });
    intent.focus();

    view.rerender(
      <H3Sidebar
        state={initialShellState}
        appMode={readyAppMode}
        locale="zh-CN"
      />,
    );

    expect(
      (screen.getByRole("textbox", { name: "意图" }) as HTMLTextAreaElement)
        .value,
    ).toBe("Keep this private draft");
    expect(
      screen.getByRole("button", { name: "启动 H3 App Mode" }),
    ).toBeTruthy();
    expect(document.activeElement).toBe(intent);
  });

  it("localizes a stable App Mode error code at the final view boundary", () => {
    const state = {
      status: "error" as const,
      code: "queue_failed" as const,
      severity: "error" as const,
      source: "app_mode",
      message: "queue_failed",
      recovery: "retry" as const,
    };
    const view = render(<H3Sidebar state={state} locale="en" />);
    expect(
      screen.getByText("The normal ComfyUI queue rejected this run."),
    ).toBeTruthy();
    view.rerender(<H3Sidebar state={state} locale="zh-CN" />);
    expect(screen.getByText("常规 ComfyUI 队列拒绝了此次运行。")).toBeTruthy();
    expect(document.body.textContent).not.toContain("queue_failed");
  });

  it("restores transient intent and active stage after a view-only remount", () => {
    let draft: AppModeDraft = initialAppModeDraft;
    const onDraftChange = (next: AppModeDraft): void => {
      draft = next;
    };
    const props = () => ({
      state: initialShellState,
      appMode: readyAppMode,
      appModeDraft: draft,
      onAppModeDraftChange: onDraftChange,
    });
    const first = render(<H3Sidebar {...props()} />);
    fireEvent.change(screen.getByRole("textbox", { name: "Intent" }), {
      target: { value: "Persist across close" },
    });
    first.rerender(<H3Sidebar {...props()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Media / Roles" }));
    first.unmount();

    const second = render(<H3Sidebar {...props()} />);
    const media = screen.getByRole("tab", { name: "Media / Roles" });
    expect(media.getAttribute("aria-selected")).toBe("true");
    fireEvent.click(screen.getByRole("tab", { name: "Intent / Mode" }));
    second.rerender(<H3Sidebar {...props()} />);
    expect(
      (screen.getByRole("textbox", { name: "Intent" }) as HTMLTextAreaElement)
        .value,
    ).toBe("Persist across close");
  });

  it("restores the projected active stage and exact draft after a view-only remount", () => {
    let draft = initialSidebarStagesDraft(validSidebarWorkspace);
    const onDraftChange = (next: typeof draft): void => {
      draft = next;
    };
    const renderStages = () =>
      render(
        <SidebarStages
          projection={validSidebarWorkspace}
          locale="en"
          busy={false}
          onAction={() => undefined}
          onClientFailure={() => undefined}
          draft={draft}
          onDraftChange={onDraftChange}
        />,
      );

    const first = renderStages();
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    first.rerender(
      <SidebarStages
        projection={validSidebarWorkspace}
        locale="en"
        busy={false}
        onAction={() => undefined}
        onClientFailure={() => undefined}
        draft={draft}
        onDraftChange={onDraftChange}
      />,
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Prompt revision" }), {
      target: { value: "Private transient edit" },
    });
    first.rerender(
      <SidebarStages
        projection={validSidebarWorkspace}
        locale="en"
        busy={false}
        onAction={() => undefined}
        onClientFailure={() => undefined}
        draft={draft}
        onDraftChange={onDraftChange}
      />,
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Revision reason" }), {
      target: { value: "Private transient reason" },
    });
    first.unmount();

    renderStages();
    expect(
      screen
        .getByRole("tab", { name: "Audit / Validate" })
        .getAttribute("aria-selected"),
    ).toBe("true");
    expect(
      (
        screen.getByRole("textbox", {
          name: "Prompt revision",
        }) as HTMLTextAreaElement
      ).value,
    ).toBe("Private transient edit");
    expect(
      (
        screen.getByRole("textbox", {
          name: "Revision reason",
        }) as HTMLInputElement
      ).value,
    ).toBe("Private transient reason");
  });

  it("resets a transient projected draft when backend workspace authority changes", () => {
    const draft = {
      ...initialSidebarStagesDraft(validSidebarWorkspace),
      activeStage: "audit" as const,
      promptText: "Old local edit",
      reason: "Old local reason",
    };
    const nextProjection = {
      ...validSidebarWorkspace,
      report_revision: validSidebarWorkspace.report_revision + 1,
      report_fingerprint: `sha256:${"c".repeat(64)}` as const,
      prompt_fingerprint: `sha256:${"d".repeat(64)}` as const,
      prompt_text: "New backend prompt",
    };
    render(
      <SidebarStages
        projection={nextProjection}
        locale="en"
        busy={false}
        onAction={() => undefined}
        onClientFailure={() => undefined}
        draft={draft}
      />,
    );
    expect(
      screen
        .getByRole("tab", { name: "Audit / Validate" })
        .getAttribute("aria-selected"),
    ).toBe("true");
    expect(
      (
        screen.getByRole("textbox", {
          name: "Prompt revision",
        }) as HTMLTextAreaElement
      ).value,
    ).toBe("New backend prompt");
  });

  it("assigns a unique bounded focus key to every interactive control", () => {
    const assertFocusKeys = (root: HTMLElement): void => {
      const controls = Array.from(
        root.querySelectorAll<HTMLElement>("button, input, select, textarea"),
      );
      const keys = controls.map((control) => control.dataset.h3FocusKey ?? "");
      for (const [index, key] of keys.entries()) {
        expect(key, `${controls[index]?.tagName} focus identity`).toMatch(
          /^[a-z0-9-]{1,64}$/u,
        );
      }
      expect(new Set(keys).size).toBe(keys.length);
    };

    const appView = render(
      <H3Sidebar
        state={initialShellState}
        appMode={readyAppMode}
        locale="en"
      />,
    );
    assertFocusKeys(appView.container);
    appView.unmount();

    const projectedView = render(
      <SidebarStages
        projection={validSidebarWorkspace}
        locale="zh-CN"
        busy={false}
        onAction={() => undefined}
        onClientFailure={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "审核／验证" }));
    assertFocusKeys(projectedView.container);
    fireEvent.click(
      projectedView.container.querySelector<HTMLButtonElement>(
        'button[aria-controls="h3-refinement-instruction"]',
      )!,
    );
    assertFocusKeys(projectedView.container);
    fireEvent.click(
      projectedView.container.querySelector<HTMLButtonElement>(
        'button[aria-controls="h3-prompt-reader"]',
      )!,
    );
    assertFocusKeys(projectedView.container);
  });
});
