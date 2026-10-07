import { describe, expect, it } from "vitest";

import { createShellSession } from "../src/lifecycle/shellSession";

describe("product shell session state", () => {
  it("creates isolated bounded correlation collections", () => {
    const first = createShellSession();
    const second = createShellSession();
    first.acceptedProjectionPromptIds.add("prompt-1");
    first.deferredAppModeProjections.set("prompt-2", {
      run: 1,
      executionNodeId: "node-1",
      accept() {},
    });
    expect(second.acceptedProjectionPromptIds.size).toBe(0);
    expect(second.deferredAppModeProjections.size).toBe(0);
    expect(first.acceptedProjectionPromptIds).not.toBe(
      second.acceptedProjectionPromptIds,
    );
    expect(first.ownedGraphConfigureDepth).toBe(0);
  });
});
