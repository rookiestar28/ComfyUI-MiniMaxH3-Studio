import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ProjectFileControls } from "../src/components/ProjectFileControls";
import type { ProjectFileBinding } from "../src/lifecycle/projectFileSession";
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
function binding(): ProjectFileBinding {
  return {
    state: {
      status: "idle",
      title: "Synthetic",
      dirty: true,
      error: null,
      missingMedia: [],
    },
    setTitle: vi.fn(),
    save: vi.fn(async () => undefined),
    open: vi.fn(async () => undefined),
    relink: vi.fn(async () => undefined),
    cancel: vi.fn(),
  };
}
describe("shared Project controls", () => {
  it.each([
    ["en", "Save project", "Open project", "Project title"],
    ["zh-TW", "儲存專案", "開啟專案", "專案名稱"],
    ["zh-CN", "保存项目", "打开项目", "项目名称"],
  ] as const)(
    "uses native controls and localized file operations in %s",
    (locale, save, open, title) => {
      const value = binding();
      vi.stubGlobal("showSaveFilePicker", undefined);
      vi.stubGlobal("confirm", () => false);
      render(<ProjectFileControls binding={value} locale={locale} />);
      fireEvent.change(screen.getByRole("textbox", { name: title }), {
        target: { value: "Edited" },
      });
      expect(value.setTitle).toHaveBeenCalledWith("Edited");
      fireEvent.click(screen.getByRole("button", { name: save }));
      expect(value.save).toHaveBeenCalledWith(undefined);
      fireEvent.click(screen.getByRole("button", { name: open }));
      expect(value.open).not.toHaveBeenCalled();
    },
  );
  it("links only the explicitly chosen current retained video", () => {
    const value = {
      ...binding(),
      state: { ...binding().state, missingMedia: ["video.synthetic"] },
      retained: [
        {
          asset_id: "asset_" + "a".repeat(32),
          frame_count: 24,
          width: 64,
          height: 64,
        },
      ],
      refreshRetained: vi.fn(async () => undefined),
    };
    render(<ProjectFileControls binding={value} locale="en" />);
    expect(value.relink).not.toHaveBeenCalled();
    const link = screen.getByRole("button", {
      name: "Relink media: video.synthetic",
    });
    expect((link as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: value.retained[0].asset_id },
    });
    expect((link as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(link);
    expect(value.relink).toHaveBeenCalledWith(
      "video.synthetic",
      value.retained[0].asset_id,
    );
  });
  it("keeps export fallback available beside a native picker", () => {
    const value = binding(),
      picker = vi.fn();
    vi.stubGlobal("showSaveFilePicker", picker);
    render(<ProjectFileControls binding={value} locale="en" compact />);
    fireEvent.click(
      screen.getByRole("button", { name: "Download project", hidden: true }),
    );
    expect(value.save).toHaveBeenCalledWith();
    expect(picker).not.toHaveBeenCalled();
  });
  it("Escape closes the Project menu and keeps focus inside the overlay", () => {
    const outer = vi.fn();
    const view = render(
      <div onKeyDown={outer}>
        <ProjectFileControls binding={binding()} locale="en" compact />
      </div>,
    );
    const menu = view.container.querySelector("details")!;
    menu.open = true;
    const title = screen.getByRole("textbox", { name: "Project title" });
    title.focus();
    fireEvent.keyDown(title, { key: "Escape" });
    expect(menu.open).toBe(false);
    expect(document.activeElement).toBe(menu.querySelector("summary"));
    expect(outer).not.toHaveBeenCalled();
    menu.open = true;
    fireEvent.keyDown(menu.querySelector("summary")!, { key: "Escape" });
    expect(menu.open).toBe(false);
    expect(outer).not.toHaveBeenCalled();
  });
});
