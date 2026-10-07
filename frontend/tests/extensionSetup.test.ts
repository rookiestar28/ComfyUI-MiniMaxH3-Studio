import { describe, expect, it, vi } from "vitest";

import { createExtensionSetupLifecycle } from "../src/lifecycle/extensionSetup";

describe("M17-00 entry setup ownership", () => {
  it("installs once before disposal and cleans up once", () => {
    const lifecycle = createExtensionSetupLifecycle();
    const install = vi.fn();
    const cleanup = vi.fn();

    expect(lifecycle.setup(install)).toBe(true);
    expect(lifecycle.setup(install)).toBe(false);
    expect(install).toHaveBeenCalledOnce();
    expect(lifecycle.dispose(cleanup)).toBe(true);
    expect(lifecycle.dispose(cleanup)).toBe(false);
    expect(cleanup).toHaveBeenCalledOnce();
    expect(lifecycle.setup(install)).toBe(false);
  });

  it("rolls back the one-shot guard when installation throws", () => {
    const lifecycle = createExtensionSetupLifecycle();
    expect(() =>
      lifecycle.setup(() => {
        throw new Error("installation failed");
      }),
    ).toThrow(/installation failed/);
    expect(lifecycle.getState()).toBe("idle");
    expect(lifecycle.setup(() => undefined)).toBe(true);
  });

  it("makes setup-time disposal terminal", () => {
    const lifecycle = createExtensionSetupLifecycle();
    const cleanup = vi.fn();
    lifecycle.setup(() => {
      lifecycle.dispose(cleanup);
    });
    expect(cleanup).toHaveBeenCalledOnce();
    expect(lifecycle.getState()).toBe("disposed");
    expect(lifecycle.setup(() => undefined)).toBe(false);
  });
});
