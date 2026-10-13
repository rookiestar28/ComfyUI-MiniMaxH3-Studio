import { afterEach, expect, it, vi } from "vitest";
import type { Page } from "@playwright/test";
import { instrumentHostGraphLoads } from "./e2e/host/managed";
// This unit exercises the public store lifecycle, without admitting a real host or bundle.
vi.mock("./e2e/host/candidate", () => ({ hostUrl: "http://127.0.0.1:8188" }));

afterEach(() => {
  delete (window as any).comfyAPI;
});

it.each([null, { attached: true }])(
  "captures cold workflow authority only after its real store contract is ready: %s",
  async (active) => {
    const app: any = {
      extensionManager: {},
      loadGraphData: vi.fn(async () => undefined),
    };
    (window as any).comfyAPI = { app: { app } };
    const waitForFunction = vi.fn(async (predicate: () => boolean) => {
      expect(predicate()).toBe(false);
      // Model the observed host lifecycle: registration exists before workflow initialization.
      app.extensionManager.workflow = {
        activeWorkflow: active,
        openWorkflows: active ? [active] : [],
      };
      expect(predicate()).toBe(true);
    });
    const page = {
      waitForFunction,
      evaluate: async (operation: () => unknown) => operation(),
    } as unknown as Page;
    await instrumentHostGraphLoads(page);
    expect(waitForFunction).toHaveBeenCalledOnce();
    expect((window as any).__h3GraphWorkflowAuthority).toBe(active);
    expect((window as any).__h3GraphWorkflowOpenCount).toBe(active ? 1 : 0);
    await app.loadGraphData({}, false, false, active);
    expect((window as any).__h3GraphWorkflowTargets).toEqual([true]);
  },
);
