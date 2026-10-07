import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsPage } from "../src/components/SettingsPage";

afterEach(cleanup);

describe("M17-12 sole local Settings page", () => {
  it("renders exactly one language row and commits only through the adapter", async () => {
    const onWrite = vi.fn(async () => true);
    const { container } = render(
      <SettingsPage
        locale="en"
        snapshot={{ status: "ready", value: "auto", pending: false }}
        onWrite={onWrite}
      />,
    );
    expect(container.querySelectorAll("select")).toHaveLength(1);
    await userEvent.selectOptions(screen.getByLabelText("Language"), "zh-TW");
    expect(onWrite).toHaveBeenCalledWith("zh-TW");
    expect(
      container.querySelector('[data-setting-id="H3.Context.Language"]'),
    ).not.toBeNull();
  });

  it("disables writes and exposes bounded storage failure", () => {
    render(
      <SettingsPage
        locale="en"
        snapshot={{
          status: "setting_storage_unavailable",
          value: undefined,
          pending: false,
        }}
        onWrite={vi.fn()}
      />,
    );
    expect(
      (screen.getByLabelText("Language") as HTMLSelectElement).disabled,
    ).toBe(true);
    expect(screen.getByRole("status").textContent).toMatch(/unavailable/i);
  });
});
