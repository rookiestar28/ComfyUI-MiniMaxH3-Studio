import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { H3Sidebar } from "../src/components/H3Sidebar";
import type { SidebarWorkspaceState } from "../src/state/sidebarWorkspace";
import {
  validProductShell,
  validSidebarWorkspace,
} from "./sidebarWorkspaceFixture";

const shellState = {
  status: "projected",
  projection: validProductShell,
} as const;
const workspaceState: SidebarWorkspaceState = {
  status: "ready",
  projection: validSidebarWorkspace,
};

afterEach(cleanup);

describe("five-stage H3 sidebar workspace", () => {
  it("renders fixed accessible stages and backend-owned media candidates", () => {
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={workspaceState}
        onWorkspaceAction={vi.fn()}
      />,
    );
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.getAttribute("aria-label"))).toEqual([
      "Intent / Mode",
      "Media / Roles",
      "Understand / Plan",
      "Audit / Validate",
      "Execute / Export",
    ]);
    fireEvent.click(screen.getByRole("tab", { name: "Media / Roles" }));
    const panel = screen.getByRole("tabpanel");
    expect(within(panel).getByText("<Picture 1>")).toBeTruthy();
    expect(panel.textContent).not.toMatch(/filename|C:\\|https:\/\//i);
  });

  it("uses the backend Director candidate and requests a new revision", () => {
    const onAction = vi.fn();
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={workspaceState}
        onWorkspaceAction={onAction}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    const editor = screen.getByRole("textbox", { name: "Prompt revision" });
    fireEvent.change(editor, { target: { value: "Subject: @" } });
    const combobox = screen.getByRole("combobox", { name: "Reference token" });
    expect(combobox.getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(combobox, { key: "ArrowDown" });
    fireEvent.keyDown(combobox, { key: "Enter" });
    expect(onAction).toHaveBeenCalledWith({
      action: "stage_prompt",
      payload: {
        reason: "Insert backend reference <Picture 1>",
        prompt_text: "Subject: <Picture 1>",
      },
    });
  });

  it.each([
    ["start", "alpha", 0, 0, "<Subject 1> alpha", 11],
    ["middle", "alphabeta", 5, 5, "alpha <Subject 1> beta", 17],
    ["end", "alpha", 5, 5, "alpha <Subject 1>", 17],
    ["selection", "alpha beta", 0, 5, "<Subject 1> beta", 11],
    ["identity selection", "<Subject 1>", 0, 11, "<Subject 1>", 11],
  ] as const)(
    "inserts a subject chip at the %s caret and restores textarea ownership",
    (_case, input, start, end, expected, expectedCaret) => {
      const onAction = vi.fn();
      render(
        <H3Sidebar
          state={shellState}
          workspaceState={workspaceState}
          onWorkspaceAction={onAction}
        />,
      );
      fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
      const editor = screen.getByRole("textbox", {
        name: "Prompt revision",
      }) as HTMLTextAreaElement;
      fireEvent.change(editor, { target: { value: input } });
      editor.focus();
      editor.setSelectionRange(start, end);

      fireEvent.click(
        screen.getByRole("button", {
          name: "Insert backend reference <Subject 1>, Subject, the baker",
        }),
      );

      expect(onAction).toHaveBeenCalledTimes(1);
      expect(onAction).toHaveBeenCalledWith({
        action: "stage_prompt",
        payload: {
          reason: "Insert backend reference <Subject 1>",
          prompt_text: expected,
        },
      });
      expect(editor.value).toBe(expected);
      expect(document.activeElement).toBe(editor);
      expect(editor.selectionStart).toBe(expectedCaret);
      expect(editor.selectionEnd).toBe(expectedCaret);
    },
  );

  it("renders one ordered chip and compact option for every backend candidate", () => {
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={workspaceState}
        onWorkspaceAction={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    const toolbar = screen.getByRole("toolbar", {
      name: "Reference token toolbar",
    });
    expect(
      within(toolbar)
        .getAllByRole("button")
        .map((button) => button.getAttribute("data-token-label")),
    ).toEqual(["<Picture 1>", "<Subject 1>"]);

    const picker = screen.getByRole("combobox", {
      name: "Reference token picker",
    });
    expect(
      within(picker)
        .getAllByRole("option")
        .slice(1)
        .map((option) => option.getAttribute("data-token-label")),
    ).toEqual(["<Picture 1>", "<Subject 1>"]);
  });

  it("stages the compact native picker selection through the same action", () => {
    const onAction = vi.fn();
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={workspaceState}
        onWorkspaceAction={onAction}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    const editor = screen.getByRole("textbox", {
      name: "Prompt revision",
    }) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: "alpha" } });
    editor.setSelectionRange(0, 0);
    fireEvent.change(
      screen.getByRole("combobox", { name: "Reference token picker" }),
      { target: { value: "1" } },
    );
    expect(onAction).toHaveBeenCalledWith({
      action: "stage_prompt",
      payload: {
        reason: "Insert backend reference <Subject 1>",
        prompt_text: "<Subject 1> alpha",
      },
    });
  });

  it("retains the complete bounded candidate inventory in toolbar and select order", () => {
    const referenceCandidates = Array.from({ length: 18 }, (_, index) => ({
      asset_id: `image_${index + 1}`,
      kind: "image" as const,
      label: `<Picture ${index + 1}>`,
      ordinal: index + 1,
      paired_with: null,
    }));
    const subjectCandidates = Array.from({ length: 46 }, (_, index) => ({
      subject_id: `subject_${index + 1}`,
      ordinal: index + 1,
      label: `<Subject ${index + 1}>`,
      display: `subject ${index + 1}`,
    }));
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={{
          status: "ready",
          projection: {
            ...validSidebarWorkspace,
            reference_candidates: referenceCandidates,
            subject_candidates: subjectCandidates,
          },
        }}
        onWorkspaceAction={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    const toolbarLabels = within(
      screen.getByRole("toolbar", { name: "Reference token toolbar" }),
    )
      .getAllByRole("button")
      .map((button) => button.getAttribute("data-token-label"));
    const pickerLabels = within(
      screen.getByRole("combobox", { name: "Reference token picker" }),
    )
      .getAllByRole("option")
      .slice(1)
      .map((option) => option.getAttribute("data-token-label"));
    expect(toolbarLabels).toHaveLength(64);
    expect(pickerLabels).toEqual(toolbarLabels);
  });

  it("disables every one-tap control while stage_prompt is unavailable or busy", () => {
    const unavailable = {
      ...validSidebarWorkspace,
      actions: {
        ...validSidebarWorkspace.actions,
        stage_prompt: false,
      },
    };
    const view = render(
      <H3Sidebar
        state={shellState}
        workspaceState={{ status: "ready", projection: unavailable }}
        onWorkspaceAction={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    expect(
      screen
        .getByRole("button", {
          name: "Insert backend reference <Picture 1>, Image",
        })
        .hasAttribute("disabled"),
    ).toBe(true);
    expect(
      screen
        .getByRole("combobox", { name: "Reference token picker" })
        .hasAttribute("disabled"),
    ).toBe(true);

    view.rerender(
      <H3Sidebar
        state={shellState}
        workspaceState={{
          status: "loading",
          projection: validSidebarWorkspace,
          action: "stage_prompt",
        }}
        onWorkspaceAction={vi.fn()}
      />,
    );
    expect(
      screen
        .getByRole("button", {
          name: "Insert backend reference <Picture 1>, Image",
        })
        .hasAttribute("disabled"),
    ).toBe(true);
    expect(
      screen
        .getByRole("combobox", { name: "Reference token picker" })
        .hasAttribute("disabled"),
    ).toBe(true);
  });

  it("closes the backend-reference listbox on Escape", () => {
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={workspaceState}
        onWorkspaceAction={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Prompt revision" }), {
      target: { value: "Subject: @" },
    });
    const combobox = screen.getByRole("combobox", { name: "Reference token" });
    expect(combobox.getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(combobox, { key: "Escape" });
    expect(combobox.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(combobox);
  });

  it("windows a large backend reference list while preserving keyboard order", () => {
    const referenceCandidates = Array.from({ length: 100 }, (_, index) => ({
      asset_id: `image_${index + 1}`,
      kind: "image" as const,
      label: `<Picture ${index + 1}>`,
      ordinal: index + 1,
      paired_with: null,
    }));
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={{
          status: "ready",
          projection: {
            ...validSidebarWorkspace,
            reference_candidates: referenceCandidates,
          },
        }}
        onWorkspaceAction={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Prompt revision" }), {
      target: { value: "Subject: @" },
    });
    const combobox = screen.getByRole("combobox", { name: "Reference token" });
    expect(
      within(screen.getByRole("listbox")).getAllByRole("option"),
    ).toHaveLength(32);
    for (let index = 0; index < 65; index += 1)
      fireEvent.keyDown(combobox, { key: "ArrowDown" });
    expect(combobox.getAttribute("aria-activedescendant")).toMatch(
      /option-64$/,
    );
    expect(
      within(screen.getByRole("listbox")).getAllByRole("option"),
    ).toHaveLength(32);
    expect(
      within(screen.getByRole("listbox")).getByRole("option", {
        name: /<Picture 65>/,
      }),
    ).toBeTruthy();
    expect(screen.getByRole("listbox").getAttribute("aria-setsize")).toBe(
      "100",
    );
  });

  it("resets stale keyboard ownership when a large reference query narrows", () => {
    const onAction = vi.fn();
    const referenceCandidates = Array.from({ length: 100 }, (_, index) => ({
      asset_id: `image_${index + 1}`,
      kind: "image" as const,
      label: index === 0 ? "<Unique Reference>" : `<Picture ${index + 1}>`,
      ordinal: index + 1,
      paired_with: null,
    }));
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={{
          status: "ready",
          projection: {
            ...validSidebarWorkspace,
            reference_candidates: referenceCandidates,
          },
        }}
        onWorkspaceAction={onAction}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    const editor = screen.getByRole("textbox", { name: "Prompt revision" });
    fireEvent.change(editor, { target: { value: "Subject: @" } });
    const combobox = screen.getByRole("combobox", { name: "Reference token" });
    for (let index = 0; index < 65; index += 1)
      fireEvent.keyDown(combobox, { key: "ArrowDown" });
    expect(combobox.getAttribute("aria-activedescendant")).toMatch(
      /option-64$/,
    );

    fireEvent.change(editor, { target: { value: "Subject: @Unique" } });
    expect(
      within(screen.getByRole("listbox")).getAllByRole("option"),
    ).toHaveLength(1);
    expect(combobox.getAttribute("aria-activedescendant")).toBeNull();
    fireEvent.keyDown(combobox, { key: "Enter" });
    expect(onAction).not.toHaveBeenCalled();

    fireEvent.change(editor, { target: { value: "Subject: @Missing" } });
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.change(editor, { target: { value: "Subject: @Unique" } });
    fireEvent.keyDown(combobox, { key: "ArrowDown" });
    fireEvent.keyDown(combobox, { key: "Enter" });
    expect(onAction).toHaveBeenCalledWith({
      action: "stage_prompt",
      payload: {
        reason: "Insert backend reference <Unique Reference>",
        prompt_text: "Subject: <Unique Reference>",
      },
    });
  });

  it("renders hostile text literally and supports Traditional Chinese labels", () => {
    const hostile = {
      ...validSidebarWorkspace,
      prompt_text: '<img src=x onerror="globalThis.pwned=true">',
    };
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={{ status: "ready", projection: hostile }}
        onWorkspaceAction={vi.fn()}
        locale="zh-TW"
      />,
    );
    expect(screen.getByRole("tab", { name: "意圖／模式" })).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("就緒");
    expect(screen.getAllByText("完成")).toHaveLength(4);
    fireEvent.click(screen.getByRole("tab", { name: "意圖／模式" }));
    expect(screen.getByText("模式已明確指定。")).toBeTruthy();
    expect(screen.getByText("模式能力")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "稽核／驗證" }));
    expect(
      screen.getByDisplayValue('<img src=x onerror="globalThis.pwned=true">'),
    ).toBeTruthy();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByRole("button", { name: "驗證版本" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "複製提示詞" })).toBeTruthy();
  });

  it("shows explicit validate action and blocks export for a staged revision", () => {
    const staged = {
      ...validSidebarWorkspace,
      report_revision: 2,
      report_fingerprint: `sha256:${"c".repeat(64)}`,
      prompt_fingerprint: `sha256:${"d".repeat(64)}`,
      lifecycle: "stale" as const,
      validation_status: "not_run" as const,
      bindings: [],
      proposal: {
        ...validSidebarWorkspace.proposal,
        changed: true,
        reason: "Manual edit",
        current_prompt_fingerprint: `sha256:${"d".repeat(64)}`,
      },
      stages: validSidebarWorkspace.stages.map((stage, index) => ({
        ...stage,
        status:
          index === 3
            ? ("active" as const)
            : index === 4
              ? ("blocked" as const)
              : ("complete" as const),
      })),
      actions: {
        stage_prompt: true,
        import_prompt: true,
        validate: true,
        export: false,
        copy_prompt: false,
      },
    };
    const onAction = vi.fn();
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={{ status: "ready", projection: staged }}
        onWorkspaceAction={onAction}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Validate revision" }));
    expect(onAction).toHaveBeenCalledWith({ action: "validate", payload: {} });
    expect(
      screen
        .getByRole("button", { name: "Export JSON" })
        .hasAttribute("disabled"),
    ).toBe(true);
    expect(
      screen
        .getByRole("button", { name: "Copy prompt" })
        .hasAttribute("disabled"),
    ).toBe(true);
    expect(
      screen.getByText(
        "The backend workspace has a staged revision; validation is required before export.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText("Validation or media receipt is required before export"),
    ).toBeTruthy();
  });

  it("synchronizes the editor to a newly issued backend revision", () => {
    const { rerender } = render(
      <H3Sidebar
        state={shellState}
        workspaceState={workspaceState}
        onWorkspaceAction={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Audit / Validate" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Prompt revision" }), {
      target: { value: "unsent browser draft" },
    });
    const next = {
      ...validSidebarWorkspace,
      report_revision: validSidebarWorkspace.report_revision + 1,
      report_fingerprint: `sha256:${"c".repeat(64)}`,
      prompt_fingerprint: `sha256:${"d".repeat(64)}`,
      prompt_text: "backend-issued revision",
      proposal: {
        ...validSidebarWorkspace.proposal,
        changed: true,
        reason: "Backend revision",
        current_prompt_fingerprint: `sha256:${"d".repeat(64)}`,
      },
    };
    rerender(
      <H3Sidebar
        state={shellState}
        workspaceState={{ status: "ready", projection: next }}
        onWorkspaceAction={vi.fn()}
      />,
    );
    expect(screen.getByDisplayValue("backend-issued revision")).toBeTruthy();
    expect(screen.queryByDisplayValue("unsent browser draft")).toBeNull();
  });

  it("announces a backend action error without presenting it as ready", () => {
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={{
          status: "error",
          reason: "request_failed",
          projection: validSidebarWorkspace,
        }}
        onWorkspaceAction={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert").textContent).toContain("request_failed");
  });

  it("renders the bounded planning, capability, receipt, timeline and diff values", () => {
    const inspectable = {
      ...validSidebarWorkspace,
      lifecycle: "blocked" as const,
      planning: {
        ...validSidebarWorkspace.planning,
        creative_additions_status: "user_authored" as const,
        creative_additions: ["Hold the final composition for one beat."],
      },
      capabilities: {
        ...validSidebarWorkspace.capabilities,
        timed_reference_limits: {
          ...validSidebarWorkspace.capabilities.timed_reference_limits,
          status: "unverified" as const,
          limitation: "Timed reference duration authority is unavailable.",
        },
      },
      media_receipt: {
        ...validSidebarWorkspace.media_receipt,
        status: "unverified" as const,
        queue_ready: false,
      },
      proposal: {
        ...validSidebarWorkspace.proposal,
        changed: true,
        reason: "User-authored timing edit",
        diff: {
          status: "changed" as const,
          lines: ["- Subject: original", "+ Subject: revised"],
        },
      },
      actions: {
        ...validSidebarWorkspace.actions,
        export: false,
        copy_prompt: false,
      },
    };
    render(
      <H3Sidebar
        state={shellState}
        workspaceState={{ status: "ready", projection: inspectable }}
        onWorkspaceAction={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Intent / Mode" }));
    expect(screen.getByText("available")).toBeTruthy();
    expect(screen.getByText("t2va, i2va, fl2va, l2va, ref2va")).toBeTruthy();
    expect(screen.getByText("STRING")).toBeTruthy();
    expect(screen.getByText("4–15")).toBeTruthy();
    expect(screen.getByText("within_limit")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Understand / Plan" }));
    expect(screen.getByText("deterministic_manual")).toBeTruthy();
    expect(screen.getByText(/5\.166/)).toBeTruthy();
    expect(screen.getByText("124")).toBeTruthy();
    expect(screen.getByText("seconds")).toBeTruthy();
    expect(
      screen.getByText("Hold the final composition for one beat."),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Media / Roles" }));
    expect(screen.getAllByText("unverified")).toHaveLength(2);
    expect(screen.getByText("9 / 3 / 3 / 12")).toBeTruthy();
    expect(
      screen.getByText("Timed reference duration authority is unavailable."),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Execute / Export" }));
    expect(screen.getByText("changed")).toBeTruthy();
    expect(screen.getByText("- Subject: original")).toBeTruthy();
    expect(screen.getByText("+ Subject: revised")).toBeTruthy();
  });

  it("surfaces clipboard and import failures and ignores late file completion", async () => {
    const onFailure = vi.fn();
    const onAction = vi.fn();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    const view = render(
      <H3Sidebar
        state={shellState}
        workspaceState={workspaceState}
        onWorkspaceAction={onAction}
        onWorkspaceFailure={onFailure}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy prompt" }));
    await vi.waitFor(() => expect(onFailure).toHaveBeenCalled());

    const input = screen.getByLabelText("Import JSON") as HTMLInputElement;
    const oversized = new File(["x".repeat(70_001)], "large.json", {
      type: "application/json",
    });
    fireEvent.change(input, { target: { files: [oversized] } });
    expect(onFailure).toHaveBeenCalledTimes(2);

    let resolveText: ((value: string) => void) | undefined;
    const pending = new File(["{}"], "pending.json", {
      type: "application/json",
    });
    Object.defineProperty(pending, "text", {
      value: () =>
        new Promise<string>((resolve) => {
          resolveText = resolve;
        }),
    });
    fireEvent.change(input, { target: { files: [pending] } });
    view.unmount();
    resolveText?.("{}");
    await Promise.resolve();
    expect(onAction).not.toHaveBeenCalled();
  });

  it("invalidates pending client completions when workspace authority changes", async () => {
    const onFailure = vi.fn();
    const onAction = vi.fn();
    let rejectClipboard: ((reason?: unknown) => void) | undefined;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: () =>
          new Promise<void>((_resolve, reject) => {
            rejectClipboard = reject;
          }),
      },
    });
    const view = render(
      <H3Sidebar
        state={shellState}
        workspaceState={workspaceState}
        onWorkspaceAction={onAction}
        onWorkspaceFailure={onFailure}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy prompt" }));

    let resolveText: ((value: string) => void) | undefined;
    const pending = new File(["{}"], "pending.json", {
      type: "application/json",
    });
    Object.defineProperty(pending, "text", {
      value: () =>
        new Promise<string>((resolve) => {
          resolveText = resolve;
        }),
    });
    fireEvent.change(screen.getByLabelText("Import JSON"), {
      target: { files: [pending] },
    });

    const freshAuthority = {
      ...validSidebarWorkspace,
      workspace_id: "ws_freshauthority0123456789abcdefghijkl",
      correlation: {
        prompt_id: "prompt-2",
        execution_node_id: "18",
      },
    };
    view.rerender(
      <H3Sidebar
        state={shellState}
        workspaceState={{ status: "ready", projection: freshAuthority }}
        onWorkspaceAction={onAction}
        onWorkspaceFailure={onFailure}
      />,
    );
    resolveText?.("{}");
    rejectClipboard?.(new Error("late denial"));
    await Promise.resolve();
    await Promise.resolve();
    expect(onAction).not.toHaveBeenCalled();
    expect(onFailure).not.toHaveBeenCalled();
  });
});
