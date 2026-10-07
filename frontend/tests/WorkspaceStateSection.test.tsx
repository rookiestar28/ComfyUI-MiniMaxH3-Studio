import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useReducer } from "react";
import { afterEach, describe, expect, it } from "vitest";
import fixture from "../../tests/fixtures/workspace_state_v1.json";
import { createWorkspaceStateClient } from "../src/host/workspaceStateClient";
import { createWorkspaceStateSession } from "../src/lifecycle/workspaceStateSession";
import type { Locale } from "../src/i18n/catalog";

import * as sectionModule from "../src/components/WorkspaceStateSection";
async function section() {
  return sectionModule;
}
const releases: (() => void)[] = [];
afterEach(() => {
  releases.splice(0).forEach((release) => release());
  cleanup();
});

async function harness(locale: Locale = "en") {
  const { WorkspaceStateSection } = await section();
  let changed = () => {};
  let wire = structuredClone(fixture);
  const calls: Record<string, unknown>[] = [];
  const client = createWorkspaceStateClient({
    fetchApi: async (_path, init) => {
      const action = JSON.parse(init.body as string) as Record<string, unknown>;
      calls.push(action);
      if (action.intent === "set_enabled") {
        wire.projection.enabled = action.enabled as boolean;
        wire.projection.current = action.enabled as boolean;
        wire.projection.sampler_active = action.enabled as boolean;
        wire.projection.save_state = action.enabled ? "saved" : "disabled";
        wire.projection.revision += 1;
      } else if (action.intent === "reset") {
        wire.projection = {
          ...wire.projection,
          enabled: false,
          count: 0,
          records: [],
          current: false,
          sampler_active: false,
          saved_at_ms: null as never,
          save_state: "disabled",
          revision: wire.projection.revision + 1,
        };
      }
      const result: Record<string, unknown> = structuredClone(wire);
      if (action.intent === "restore") {
        const row = wire.projection.records[0];
        result.recovered = {
          recovery_handle: "recovery_" + "a".repeat(43),
          record_id: row.record_id,
          kind: row.kind,
          state: row.state,
          source_status: "source_reauthorization_required",
          executable: false,
          segment_count: row.segment_count,
          revisions: row.revisions,
        };
      }
      return { status: 200, json: async () => result };
    },
  });
  const session = createWorkspaceStateSession({
    client,
    changed: () => changed(),
  });
  releases.push(session.release);
  function View() {
    const [, tick] = useReducer((value: number) => value + 1, 0);
    changed = tick;
    return (
      <WorkspaceStateSection locale={locale} binding={session.binding()} />
    );
  }
  render(<View />);
  await waitFor(() =>
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
      true,
    ),
  );
  return { session, calls };
}

describe("Settings recovery controls", () => {
  it("synchronizes disable/re-enable, finite selection and explicit read-only restore", async () => {
    const { session, calls } = await harness();
    const toggle = screen.getByRole("checkbox", {
      name: "Keep recovery metadata",
    });
    fireEvent.click(toggle);
    await waitFor(() =>
      expect((toggle as HTMLInputElement).checked).toBe(false),
    );
    expect(
      (
        screen.getByRole("combobox", {
          name: "Recovery record",
        }) as HTMLSelectElement
      ).disabled,
    ).toBe(true);
    fireEvent.click(toggle);
    await waitFor(() =>
      expect((toggle as HTMLInputElement).checked).toBe(true),
    );
    const select = screen.getByRole("combobox", { name: "Recovery record" });
    fireEvent.change(select, {
      target: { value: fixture.projection.records[0].record_id },
    });
    fireEvent.click(screen.getByRole("button", { name: "Restore metadata" }));
    await screen.findByText("Read-only recovery");
    expect(session.binding().state.recovered?.executable).toBe(false);
    expect(calls.filter((row) => row.intent === "restore")).toHaveLength(1);
    expect(document.body.textContent).not.toContain(
      "recovery_" + "a".repeat(43),
    );
  });

  it("requires clear confirmation, preserves cancel focus, and resets only after confirmation", async () => {
    const { calls } = await harness();
    const user = userEvent.setup();
    const clear = screen.getByRole("button", { name: "Clear metadata" });
    clear.focus();
    await user.keyboard("{Enter}");
    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(document.activeElement).toBe(cancel);
    await user.keyboard("{Enter}");
    expect(document.activeElement).toBe(clear);
    expect(calls.some((row) => row.intent === "reset")).toBe(false);
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: "Confirm clear" }));
    await waitFor(() =>
      expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
        false,
      ),
    );
    expect(calls.filter((row) => row.intent === "reset")).toHaveLength(1);
    expect(screen.getAllByText("No saved records").length).toBeGreaterThan(0);
  });

  for (const [locale, title, refresh] of [
    ["en", "Recovery metadata", "Refresh metadata"],
    ["zh-TW", "復原中繼資料", "重新整理中繼資料"],
    ["zh-CN", "恢复元数据", "刷新元数据"],
  ] as const)
    it(`localizes controls and stable focus identities for ${locale}`, async () => {
      await harness(locale);
      expect(screen.getByRole("heading", { name: title }).textContent).toBe(
        title,
      );
      expect(
        screen.getByRole("button", { name: refresh }).getAttribute("title"),
      ).toBe(refresh);
      const keys = Array.from(
        document.querySelectorAll<HTMLElement>("button, input, select"),
      ).map((node) => node.dataset.h3FocusKey);
      expect(
        keys.every(
          (key) =>
            typeof key === "string" && key.startsWith("settings-recovery-"),
        ),
      ).toBe(true);
      expect(new Set(keys).size).toBe(keys.length);
    });
});
