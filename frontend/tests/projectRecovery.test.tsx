import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ProjectRecoveryControls } from "../src/components/ProjectRecoveryControls";
import type { ProjectRecoveryBinding } from "../src/lifecycle/projectRecoverySession";
import { createProjectRecoverySession } from "../src/lifecycle/projectRecoverySession";
import { decodeRecoveryStatus } from "../src/contracts/projectRecoveryCodec";
import type { ProjectPlanning } from "../src/contracts/projectDocumentCodec";

const project = "project_" + "a".repeat(32);
const planning: ProjectPlanning = {
  intent: "Synthetic draft",
  script: "",
  target_seconds: 8,
  policy: "auto_storyboard",
  shots: [],
};
const status = (enabled = false, generation = 1) =>
  decodeRecoveryStatus({
    schema: "h3.context.project_recovery.status.v1",
    supported: true,
    enabled,
    include_video: false,
    revision: enabled ? 2 : 0,
    charged_bytes: 0,
    records: [],
    writer_active: enabled,
    current: enabled
      ? {
          project_id: project,
          generation,
          saved_generation: generation,
          saved_revision: 2,
          saved_at_ms: 1000,
          saved_owner: null,
          state: "saved",
          reason: null,
        }
      : null,
  });
afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

async function fixture() {
  let enabled = false,
    title = "Synthetic",
    generation = 1;
  const client = {
    send: vi.fn(async (action: Record<string, unknown>) => {
      if (action.intent === "settings") enabled = action.enabled as boolean;
      if (action.intent === "watch") generation++;
      return status(enabled, generation);
    }),
    restore: vi.fn(),
  };
  const controller = createProjectRecoverySession({
    client,
    capture: () => ({ owner: null, planning, title }),
    changed: vi.fn(),
    adopt: vi.fn(),
    discard: vi.fn(),
    visible: () => true,
  });
  return {
    controller,
    client,
    edit: () => {
      title = "Synthetic changed";
    },
  };
}
describe("opted-in recovery session", () => {
  it("never transmits draft content before the explicit setting", async () => {
    const { controller, client } = await fixture();
    await controller.refresh();
    controller.observe();
    await Promise.resolve();
    expect(controller.binding().state.status).toBe("off");
    expect(
      client.send.mock.calls.every(([action]) => action.intent === "status"),
    ).toBe(true);
    controller.dispose();
  });
  it("a later local draft cannot be cleared by an old save reply", async () => {
    const { controller, client, edit } = await fixture();
    await controller.refresh();
    await controller.settings(true, false);
    let complete!: (value: ReturnType<typeof status>) => void;
    client.send.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    const saving = controller.saveNow();
    await Promise.resolve();
    edit();
    controller.observe();
    complete(status(true, 2));
    await saving;
    expect(controller.binding().state.status).not.toBe("saved");
    controller.dispose();
  });
  it("failed saving keeps dirty and blocks replacement without explicit discard", async () => {
    const { controller, client, edit } = await fixture();
    await controller.refresh();
    await controller.settings(true, false);
    edit();
    controller.observe();
    client.send.mockRejectedValue(new Error("storage_unavailable"));
    expect(await controller.beforeReplace(false)).toBe(false);
    expect(controller.binding().state.status).toBe("error");
    expect(controller.binding().state.dirty).toBe(true);
    controller.dispose();
  });
  it("leaving during an old request admits and flushes a later draft once", async () => {
    const { controller, client, edit } = await fixture();
    await controller.refresh();
    await controller.settings(true, false);
    let complete!: (value: ReturnType<typeof status>) => void;
    client.send.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    const saving = controller.saveNow();
    await Promise.resolve();
    edit();
    controller.leave();
    complete(status(true, 2));
    await saving;
    await vi.waitFor(() =>
      expect(client.send).toHaveBeenCalledWith(
        expect.objectContaining({
          intent: "watch",
          title: "Synthetic changed",
        }),
      ),
    );
    await vi.waitFor(() =>
      expect(controller.binding().state.status).toBe("saved"),
    );
    expect(
      client.send.mock.calls.filter(([action]) => action.intent === "flush"),
    ).toHaveLength(2);
    controller.dispose();
  });
  it("rejects corrupt or authority-bearing status DTOs", () => {
    expect(() =>
      decodeRecoveryStatus({ ...status(), title: "private" }),
    ).toThrow();
    expect(() =>
      decodeRecoveryStatus({
        ...status(true),
        current: { ...status(true).current!, saved_generation: 0 },
      }),
    ).toThrow();
    expect(() =>
      decodeRecoveryStatus({ ...status(), records: Array(17).fill({}) }),
    ).toThrow();
  });
  it.each([
    [
      "en",
      "Save draft recovery",
      "Include retained video links",
      "Save recovery now",
      "Select recovery",
      "Restore as a new project",
      "Clear recovery",
    ],
    [
      "zh-TW",
      "儲存草稿恢復資料",
      "包含保留影片連結",
      "立即儲存恢復資料",
      "選擇恢復資料",
      "恢復為新專案",
      "清除恢復資料",
    ],
    [
      "zh-CN",
      "保存草稿恢复数据",
      "包含保留视频链接",
      "立即保存恢复数据",
      "选择恢复数据",
      "恢复为新项目",
      "清除恢复数据",
    ],
  ] as const)(
    "%s controls synchronize opt-ins, selection and confirmed clear",
    (locale, enabled, video, save, choose, restore, clear) => {
      const binding: ProjectRecoveryBinding = {
        state: {
          enabled: false,
          includeVideo: false,
          busy: false,
          dirty: false,
          status: "off",
          error: null,
          savedAt: null,
          records: [
            {
              project_id: project,
              revision: 2,
              saved_at_ms: 1000,
              closed_at_ms: 1001,
              active: false,
            },
          ],
        },
        settings: vi.fn(async () => undefined),
        saveNow: vi.fn(async () => undefined),
        refresh: vi.fn(async () => undefined),
        restore: vi.fn(async () => undefined),
        clear: vi.fn(async () => undefined),
        beforeReplace: vi.fn(async () => true),
      };
      const view = render(
        <ProjectRecoveryControls binding={binding} locale={locale} />,
      );
      expect(
        (screen.getByRole("checkbox", { name: enabled }) as HTMLInputElement)
          .checked,
      ).toBe(false);
      expect(
        (screen.getByRole("checkbox", { name: video }) as HTMLInputElement)
          .disabled,
      ).toBe(true);
      fireEvent.click(screen.getByRole("checkbox", { name: enabled }));
      expect(binding.settings).toHaveBeenCalledWith(true, false);
      view.rerender(
        <ProjectRecoveryControls
          binding={{
            ...binding,
            state: {
              ...binding.state,
              enabled: true,
              status: "dirty",
              dirty: true,
            },
          }}
          locale={locale}
        />,
      );
      expect(
        (screen.getByRole("checkbox", { name: enabled }) as HTMLInputElement)
          .checked,
      ).toBe(true);
      fireEvent.click(screen.getByRole("checkbox", { name: video }));
      expect(binding.settings).toHaveBeenCalledWith(true, true);
      fireEvent.click(screen.getByRole("button", { name: save }));
      expect(binding.saveNow).toHaveBeenCalledOnce();
      fireEvent.change(screen.getByRole("combobox", { name: choose }), {
        target: { value: project },
      });
      fireEvent.click(screen.getByRole("button", { name: restore }));
      expect(binding.restore).toHaveBeenCalledWith(project);
      vi.spyOn(globalThis, "confirm").mockReturnValue(false);
      fireEvent.click(screen.getByRole("button", { name: clear }));
      expect(binding.clear).not.toHaveBeenCalled();
      vi.spyOn(globalThis, "confirm").mockReturnValue(true);
      fireEvent.click(screen.getByRole("button", { name: clear }));
      expect(binding.clear).toHaveBeenCalledWith(project);
      view.rerender(
        <ProjectRecoveryControls
          binding={{ ...binding, state: { ...binding.state, busy: true } }}
          locale={locale}
        />,
      );
      expect(
        (screen.getByRole("button", { name: restore }) as HTMLButtonElement)
          .disabled,
      ).toBe(true);
      view.rerender(
        <ProjectRecoveryControls
          binding={{
            ...binding,
            state: {
              ...binding.state,
              status: "error",
              error: "recovery_unavailable",
            },
          }}
          locale={locale}
        />,
      );
      const failureCopy = {
        en: "Recovery unavailable; edits are not confirmed saved",
        "zh-TW": "恢復儲存無法使用；編輯尚未確認儲存",
        "zh-CN": "恢复保存不可用；编辑尚未确认保存",
      };
      expect(screen.getByRole("alert").textContent).toBe(failureCopy[locale]);
    },
  );
});
