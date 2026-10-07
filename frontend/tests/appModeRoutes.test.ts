import { describe, expect, it, vi } from "vitest";

import {
  compileDetachedCandidate,
  compileLoadedCandidate,
} from "../src/host/appModeRoutes";

describe("App Mode candidate compilation routes", () => {
  it("compiles the loaded root without substituting a detached graph", async () => {
    const compiled = { output: {}, workflow: { id: "loaded" } };
    const graphToPrompt = vi.fn(() => compiled);
    await expect(compileLoadedCandidate({ graphToPrompt })).resolves.toBe(
      compiled,
    );
    expect(graphToPrompt).toHaveBeenCalledWith();
  });

  it("snapshots a detached candidate before compiling it", async () => {
    const serialized = { nodes: [{ id: 1 }], links: [] };
    const detached = { id: "detached" };
    const createDetachedGraph = vi.fn((_candidate: unknown) => detached);
    const graphToPrompt = vi.fn((graph?: unknown) => ({
      output: {},
      workflow: graph,
    }));
    const result = await compileDetachedCandidate(
      { graphToPrompt },
      createDetachedGraph,
      serialized,
    );
    expect(createDetachedGraph).toHaveBeenCalledOnce();
    expect(createDetachedGraph.mock.calls[0]?.[0]).toEqual(serialized);
    expect(createDetachedGraph.mock.calls[0]?.[0]).not.toBe(serialized);
    expect(graphToPrompt).toHaveBeenCalledWith(detached);
    expect(result.workflow).toBe(detached);
  });
});
