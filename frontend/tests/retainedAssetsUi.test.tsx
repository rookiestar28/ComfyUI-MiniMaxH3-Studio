import { useReducer, useState } from "react";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  RetainedAssetsSection,
  RetainOutputAction,
} from "../src/components/RetainedAssetsSection";
import { decodeRetainedAssetsResponse } from "../src/contracts/retainedAssetsCodec";
import type { RetainedAssetsBinding } from "../src/lifecycle/retainedAssetsSession";
import { createRetainedAssetsSession } from "../src/lifecycle/retainedAssetsSession";
import { createRetainedAssetsClient } from "../src/host/retainedAssetsClient";
import { productionProjection } from "./support/nleSequenceFixture";
import { SettingsPage } from "../src/components/SettingsPage";
import { ProductionWorkbench } from "../src/components/ProductionWorkbench";
import {
  assetId,
  retainedFailureWire,
  retainedResponse,
  retainedWire,
  restoredWire,
} from "./retainedAssetFixtures";

afterEach(cleanup);
function binding(): RetainedAssetsBinding {
  return {
    state: {
      projection: decodeRetainedAssetsResponse(retainedWire()).projection,
      selected: assetId,
      restored: null,
      previewUrl: null,
      busy: null,
      error: null,
      confirmed: true,
      retainedId: null,
      retainedKey: null,
      cleanup: null,
    },
    ensure: vi.fn(),
    leave: vi.fn(),
    refresh: vi.fn(async () => {}),
    setEnabled: vi.fn(async () => {}),
    select: vi.fn(),
    retain: vi.fn(async () => {}),
    restore: vi.fn(async () => {}),
    preview: vi.fn(async () => {}),
    releaseUse: vi.fn(async () => {}),
    clear: vi.fn(async () => {}),
    collect: vi.fn(async () => {}),
  };
}
const projection = productionProjection(undefined, {
  outputs: [
    {
      output_handle: "out_" + "c".repeat(43),
      ordinal: 1,
      state: "ready",
      segment_id: "segment_1",
      preview: true,
    },
  ],
  allowed_actions: [
    "read_projection",
    "release_workspace",
    "preview_output",
    "import_production_outputs_to_authoring",
  ],
});

describe("retained assets settings and single-output control", () => {
  it("keeps missing-copy recovery usable and clears only after refresh and user confirmation", async () => {
    const missing = retainedWire();
    missing.projection.charged_bytes = 100;
    const commands: unknown[] = [];
    const fetchApi = vi.fn(async (path: string, init: RequestInit) => {
      expect(path).toBe("/h3-context/retained-assets");
      const action = JSON.parse(init.body as string);
      commands.push(action);
      if (action.intent === "restore")
        return retainedResponse(retainedFailureWire("asset_unavailable"), 404);
      if (action.intent === "clear")
        return retainedResponse({
          ...missing,
          cleanup: { removed: 1, protected: 0 },
          projection: {
            ...missing.projection,
            revision: 3,
            count: 0,
            assets: [],
          },
        });
      return retainedResponse(missing);
    });
    function Surface() {
      const [, changed] = useReducer((version: number) => version + 1, 0);
      const [session] = useState(() =>
        createRetainedAssetsSession({
          client: createRetainedAssetsClient({ fetchApi }),
          changed,
        }),
      );
      return <RetainedAssetsSection locale="en" binding={session.binding()} />;
    }
    const allocate = vi.spyOn(URL, "createObjectURL");
    const mounted = render(<Surface />);
    const select = screen.getByRole("combobox", { name: "Retained asset" });
    await waitFor(() =>
      expect((select as HTMLSelectElement).disabled).toBe(false),
    );
    await userEvent.selectOptions(select, assetId);
    await userEvent.click(
      screen.getByRole("button", { name: "Restore retained media" }),
    );
    await screen.findByText("Relink required");
    const clear = screen.getByRole("button", { name: "Clear retained media" });
    expect(clear.getAttribute("aria-disabled")).toBe("true");
    await userEvent.click(clear);
    expect(screen.queryByRole("button", { name: "Confirm clear" })).toBe(null);
    expect(commands).toHaveLength(2);
    await userEvent.click(
      screen.getByRole("button", { name: "Refresh retained media" }),
    );
    await waitFor(() => expect(clear.getAttribute("aria-disabled")).toBe(null));
    expect(commands).toHaveLength(3);
    await userEvent.click(clear);
    expect(commands).toHaveLength(3);
    await userEvent.click(
      screen.getByRole("button", { name: "Confirm clear" }),
    );
    await screen.findByText("Removed: 1 / Protected: 0");
    expect(screen.getByText("Retained asset: 0 / 16")).toBeTruthy();
    expect(commands).toEqual([
      { intent: "status" },
      { intent: "restore", asset_id: assetId, expected_revision: 2 },
      { intent: "status" },
      { intent: "clear", expected_revision: 2 },
    ]);
    expect(allocate).not.toHaveBeenCalled();
    expect(mounted.container.querySelector("video")).toBe(null);
    mounted.unmount();
  });
  it.each([
    [
      "en",
      "Retained media",
      "Keep verified media",
      "Clear retained media",
      "Cancel",
      "Confirm clear",
    ],
    ["zh-TW", "保留媒體", "保留已驗證媒體", "清除保留媒體", "取消", "確認清除"],
    ["zh-CN", "保留媒体", "保留已验证媒体", "清除保留媒体", "取消", "确认清除"],
  ] as const)(
    "owns checkbox, confirmation and focus in %s",
    async (locale, heading, toggle, clear, cancel, confirm) => {
      const value = binding();
      const mounted = render(
        <RetainedAssetsSection locale={locale} binding={value} />,
      );
      expect(value.ensure).toHaveBeenCalledTimes(1);
      expect(screen.getByRole("heading", { name: heading })).toBeTruthy();
      await userEvent.click(screen.getByRole("checkbox", { name: toggle }));
      expect(value.setEnabled).toHaveBeenCalledWith(false);
      const opener = screen.getByRole("button", { name: clear });
      await userEvent.click(opener);
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: cancel }),
      );
      await userEvent.click(screen.getByRole("button", { name: cancel }));
      expect(document.activeElement).toBe(opener);
      expect(value.clear).not.toHaveBeenCalled();
      await userEvent.click(opener);
      await userEvent.click(screen.getByRole("button", { name: confirm }));
      expect(value.clear).toHaveBeenCalledTimes(1);
      mounted.unmount();
      expect(value.leave).toHaveBeenCalledTimes(1);
    },
  );
  it("restore and preview are separate, VFR capability is explicit and release stays available", async () => {
    const value = binding();
    const { rerender } = render(
      <RetainedAssetsSection locale="en" binding={value} />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Restore retained media" }),
    );
    expect(value.restore).toHaveBeenCalledTimes(1);
    expect(value.preview).not.toHaveBeenCalled();
    const restored = decodeRetainedAssetsResponse(restoredWire(false)).restored;
    rerender(
      <RetainedAssetsSection
        locale="en"
        binding={{
          ...value,
          state: { ...value.state, restored, busy: "preview" },
        }}
      />,
    );
    expect(screen.getByText("Preview unavailable")).toBeTruthy();
    await userEvent.click(
      screen.getByRole("button", { name: "Preview retained media" }),
    );
    expect(value.preview).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Release restored media" }),
    );
    expect(value.releaseUse).toHaveBeenCalledTimes(1);
  });
  it("shows charged remnants and protected cleanup without unsafe clearing", () => {
    const value = binding();
    const { container } = render(
      <RetainedAssetsSection
        locale="en"
        binding={{
          ...value,
          state: {
            ...value.state,
            projection: {
              ...value.state.projection!,
              remnant_count: 2,
              protected: 1,
            },
            cleanup: { removed: 0, protected: 1 },
          },
        }}
      />,
    );
    expect(within(container).getByText(/Unverified remnants: 2/)).toBeTruthy();
    expect(within(container).getByText(/Protected: 1/)).toBeTruthy();
    expect(container.textContent).not.toContain("source_id");
  });
  it("retains only one selected ready non-aggregate output with exact captured correlation", async () => {
    const value = binding();
    const { rerender } = render(
      <RetainOutputAction
        locale="en"
        binding={value}
        projection={projection}
        eligible
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Retain output" }),
    );
    expect(value.retain).toHaveBeenCalledWith({
      workspace_handle: projection.workspaceHandle,
      workspace_id: projection.workspaceId,
      expected_workspace_revision: projection.workspaceRevision,
      expected_workspace_fingerprint: projection.workspaceFingerprint,
      segment_id: "segment_1",
      output_handle: projection.outputs[0]!.outputHandle,
    });
    for (const changed of [
      { ...projection, selectedSegmentIds: ["segment_1", "segment.other"] },
      {
        ...projection,
        outputs: [{ ...projection.outputs[0]!, segmentId: null }],
      },
      {
        ...projection,
        outputs: [{ ...projection.outputs[0]!, state: "pending" as const }],
      },
      {
        ...projection,
        outputs: [
          projection.outputs[0]!,
          { ...projection.outputs[0]!, outputHandle: "out_other" },
        ],
      },
    ]) {
      rerender(
        <RetainOutputAction
          locale="en"
          binding={value}
          projection={changed}
          eligible
        />,
      );
      expect(
        screen
          .getByRole("button", { name: "Retain output" })
          .getAttribute("aria-disabled"),
      ).toBe("true");
      await userEvent.click(
        screen.getByRole("button", { name: "Retain output" }),
      );
    }
    expect(value.retain).toHaveBeenCalledTimes(1);
  });
  it("is wired into the actual Settings and Production views", () => {
    const value = binding();
    const settings = render(
      <SettingsPage
        locale="en"
        snapshot={{ status: "ready", value: "auto", pending: false }}
        onWrite={() => {}}
        retainedAssets={value}
      />,
    );
    expect(
      screen.getByRole("heading", { name: "Retained media" }),
    ).toBeTruthy();
    settings.unmount();
    render(
      <ProductionWorkbench
        locale="en"
        state={{ status: "ready", projection }}
        onIntent={() => {}}
        retainedAssets={value}
      />,
    );
    expect(
      screen
        .getByRole("button", { name: "Retain output" })
        .getAttribute("aria-disabled"),
    ).toBe(null);
  });
});
