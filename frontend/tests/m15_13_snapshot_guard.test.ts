import { describe, expect, it, vi } from "vitest";

import {
  createAppModeController,
  type AppModeInputs,
} from "../src/host/appMode";
import referenceAssistant from "../../subgraphs/H3 Context Assistant - Reference.json";
import { fixtureBackedAppModeHost } from "./support/hostSeamTestDouble";

const inputs: AppModeInputs = {
  task_mode: "t2va",
  user_intent: "A bounded snapshot guard probe.",
  duration_milliseconds: 5167,
  frame_count: 124,
};

describe("M15-13 App Mode snapshot guard", () => {
  it("fails closed before replacement when the public graph cannot be snapshotted", async () => {
    const serialized: Record<string, unknown> = { nodes: [] };
    serialized.self = serialized;
    serialized.uncloneable = () => undefined;
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph: { serialize: () => serialized },
      },
      { queuePrompt: vi.fn() },
    );

    await expect(
      createAppModeController(app, api).start(inputs),
    ).rejects.toMatchObject({ code: "incompatible_seam" });
    expect(app.loadApiJson).not.toHaveBeenCalled();
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(app.graphToPrompt).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("fails closed before binding an existing graph when its snapshot is unavailable", async () => {
    const graph = structuredClone(referenceAssistant) as Record<
      string,
      unknown
    >;
    const nodes = graph.nodes as Array<Record<string, unknown>>;
    nodes[0]!.uncloneable = () => undefined;
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph: { serialize: () => graph },
      },
      { queuePrompt: vi.fn() },
    );

    await expect(
      createAppModeController(app, api).start(inputs, { useExisting: true }),
    ).rejects.toMatchObject({ code: "incompatible_seam" });
    expect(app.loadApiJson).not.toHaveBeenCalled();
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(app.graphToPrompt).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("fails closed when a cyclic snapshot has no canonical identity", async () => {
    const graph: Record<string, unknown> = { nodes: [] };
    graph.self = graph;
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph: { serialize: () => graph },
      },
      { queuePrompt: vi.fn() },
    );

    await expect(
      createAppModeController(app, api).start(inputs),
    ).rejects.toMatchObject({ code: "incompatible_seam" });
    expect(app.loadApiJson).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });

  it("classifies a throwing public serialize seam without leaking the cause", async () => {
    const { app, api } = fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn(),
        graph: {
          serialize: () => {
            throw new Error("private graph detail");
          },
        },
      },
      { queuePrompt: vi.fn() },
    );

    await expect(
      createAppModeController(app, api).start(inputs),
    ).rejects.toMatchObject({ code: "incompatible_seam" });
    await expect(
      createAppModeController(app, api).start(inputs),
    ).rejects.not.toThrow(/private graph detail/);
  });
});
