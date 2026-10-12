import {
  act,
  cleanup,
  fireEvent,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/styles/tokens.css?inline", () => ({ default: ".h3c{}" }));
type Fixture = typeof import("./fixtures/entryHostModules");
const route = "/h3-context/v1/perception/status";
const visual = {
  schema: "h3.context.perception_host_status.v1",
  state: "configured",
  visual: true,
  audio: false,
  reason: "none",
  cancellation_observed: true,
};
let fixture: Fixture | undefined;
let ownedTab: ReturnType<Fixture["registeredTab"]> | undefined;
let ownedExtension: ReturnType<Fixture["registeredExtension"]> | undefined;

async function mount(handler: Parameters<Fixture["setFetchApiHandler"]>[0]) {
  vi.resetModules();
  fixture = await import("./fixtures/entryHostModules");
  fixture.setFetchApiHandler(handler);
  const values = new Map<string, unknown>([
    ["H3.Context.Language", "en"],
    ["Comfy.Locale", "en"],
  ]);
  const settings = Object.assign(new EventTarget(), {
    getSettingValue: (id: string) => values.get(id),
    setSettingValue: (id: string, value: unknown) => {
      values.set(id, value);
    },
    setSettingValueAsync: async (id: string, value: unknown) => {
      values.set(id, value);
    },
  });
  (
    fixture.app as typeof fixture.app & { ui?: { settings: typeof settings } }
  ).ui = { settings };
  fixture.setGraph(fixture.canonicalGraph(17));
  await import("../src/entry");
  ownedExtension = fixture.registeredExtension();
  act(() => ownedExtension!.setup?.());
  ownedTab = fixture.registeredTab();
  const panel = document.createElement("section");
  panel.className = "side-bar-panel";
  const content = document.createElement("div");
  content.className = "sidebar-content-container";
  const container = document.createElement("div");
  content.append(container);
  panel.append(content);
  document.body.append(panel);
  act(() => ownedTab!.render(container));
  return container;
}

afterEach(() => {
  act(() => {
    ownedTab?.destroy();
    ownedExtension?.h3DisposeExtension?.();
  });
  ownedTab = undefined;
  ownedExtension = undefined;
  fixture = undefined;
  cleanup();
  document.body.innerHTML = "";
  localStorage.clear();
  sessionStorage.clear();
});

describe("entry perception read ownership", () => {
  it("reads only on Settings open, refreshes real controls and conserves graph/queue state", async () => {
    const reads: RequestInit[] = [];
    let wire = visual;
    await mount(async (path, init) => {
      if (path !== route) return new Response("{}", { status: 503 });
      reads.push(init);
      return new Response(JSON.stringify(wire), { status: 200 });
    });
    expect(reads).toHaveLength(0);
    const sideEffects = fixture!.sideEffectCounts();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await waitFor(() => expect(reads).toHaveLength(1));
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "Visual analysis" }).textContent,
      ).toBe("Ready"),
    );
    expect(reads[0]?.method).toBe("GET");
    expect(reads[0]?.body).toBeUndefined();
    wire = { ...visual, visual: false, audio: true };
    fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
    await waitFor(() => expect(reads).toHaveLength(2));
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "Audio analysis" }).textContent,
      ).toBe("Ready"),
    );
    expect(
      screen.getByRole("status", { name: "Visual analysis" }).textContent,
    ).toBe("Unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Context" }));
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(reads).toHaveLength(2);
    expect(fixture!.sideEffectCounts()).toEqual(sideEffects);
  });

  it.each(["navigation", "destroy", "dispose"] as const)(
    "aborts the actual pending Settings read on %s",
    async (action) => {
      const reads: RequestInit[] = [];
      let complete: ((response: Response) => void) | undefined;
      await mount(async (path, init) => {
        if (path !== route) return new Response("{}", { status: 503 });
        reads.push(init);
        return new Promise<Response>((done) => {
          complete = done;
        });
      });
      fireEvent.click(screen.getByRole("button", { name: "Settings" }));
      await waitFor(() => expect(reads).toHaveLength(1));
      expect(reads[0]?.signal?.aborted).toBe(false);
      act(() => {
        if (action === "navigation")
          fireEvent.click(screen.getByRole("button", { name: "Context" }));
        else if (action === "destroy") ownedTab!.destroy();
        else ownedExtension!.h3DisposeExtension?.();
      });
      expect(reads[0]?.signal?.aborted).toBe(true);
      await act(async () => {
        complete!(new Response(JSON.stringify(visual), { status: 200 }));
      });
      expect(
        screen.queryByRole("heading", { name: "Local perception" }),
      ).toBeNull();
      expect(reads).toHaveLength(1);
    },
  );
});
