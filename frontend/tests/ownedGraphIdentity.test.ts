import { describe, expect, it } from "vitest";

import {
  observeOwnedGraph,
  type OwnedGraphReference,
} from "../src/host/ownedGraphIdentity";

type Json = Record<string, any>;

const reference: OwnedGraphReference = Object.freeze({
  nodeIds: Object.freeze(["10", "11", "12"]),
  linkIds: Object.freeze(["1", "2", "3"]),
  anchorNodeId: "6",
  authoredWidgetNodeIds: Object.freeze(["12"]),
});

function workflow(): Json {
  return {
    last_node_id: 99,
    last_link_id: 7,
    nodes: [
      {
        id: 10,
        type: "comfyui_h3_context.H3Context.Request",
        pos: [10, 20],
        size: [420, 120],
        mode: 0,
        inputs: [{ name: "duration_seconds", link: 2 }],
        outputs: [{ links: [1] }],
        properties: {},
        widgets_values: ["i2va", "A bounded intent.", 8],
      },
      {
        id: 11,
        type: "comfyui_h3_context.H3Context.ProductShell",
        pos: [30, 40],
        size: [420, 120],
        mode: 0,
        inputs: [{ name: "request", link: 1 }],
        outputs: [{ links: [3] }],
        properties: {},
        widgets_values: [],
      },
      {
        id: 12,
        type: "PrimitiveFloat",
        pos: [50, 60],
        size: [210, 58],
        mode: 0,
        inputs: [],
        outputs: [{ links: [2, 7] }],
        properties: {},
        widgets_values: [8],
      },
      {
        id: 6,
        type: "MiniMaxH3ImageToVideo",
        mode: 0,
        inputs: [
          { name: "prompt", link: 3 },
          { name: "length", link: 7 },
        ],
        outputs: [],
        widgets_values: ["", 800, 1056, 192],
      },
      {
        id: 99,
        type: "ForeignSeedNode",
        pos: [70, 80],
        mode: 0,
        inputs: [],
        outputs: [],
        properties: {},
        widgets_values: [41],
      },
    ],
    links: [
      [1, 10, 0, 11, 0, "H3_CONTEXT_REQUEST"],
      [2, 12, 0, 10, 0, "FLOAT"],
      [3, 11, 0, 6, 0, "STRING"],
      [7, 12, 0, 6, 1, "FLOAT"],
    ],
    extra: {},
  };
}

describe("D12 repository-owned graph identity", () => {
  it("ignores presentation, host metadata, queue wrappers, extensions, and foreign seeds", () => {
    const before = workflow();
    const after = structuredClone(before);
    after.nodes[0].pos = [900, 901];
    after.nodes[0].size = [421, 121];
    after.nodes[0].properties.cnr_id = "fake-pack";
    after.nodes[0].widgets_values[1] = "A user-edited intent.";
    after.nodes[4].widgets_values = [987654321];
    after.extra.ds = { scale: 1.1, offset: [2, 3] };
    after.extra.someExt = { revision: 2 };
    after.widget_idx_map = { "99": { seed: 0 } };

    expect(observeOwnedGraph(after, reference).fingerprint).toBe(
      observeOwnedGraph(before, reference).fingerprint,
    );
  });

  it.each([
    [
      "authored duration",
      (graph: Json) => (graph.nodes[2].widgets_values[0] = 12),
    ],
    ["owned mode", (graph: Json) => (graph.nodes[1].mode = 4)],
    ["owned type", (graph: Json) => (graph.nodes[2].type = "ForeignFloat")],
    ["owned link", (graph: Json) => (graph.links[0][3] = 99)],
    [
      "anchor prompt binding",
      (graph: Json) => (graph.nodes[3].inputs[0].link = null),
    ],
  ])("changes when the %s changes", (_label, mutate) => {
    const before = workflow();
    const after = structuredClone(before);
    mutate(after);

    expect(observeOwnedGraph(after, reference).fingerprint).not.toBe(
      observeOwnedGraph(before, reference).fingerprint,
    );
  });

  it("normalizes identifiers without exposing node or widget values", () => {
    const observed = observeOwnedGraph(workflow(), {
      nodeIds: ["12", "10", "11", "10"],
      linkIds: ["3", "1", "2", "1"],
      anchorNodeId: "6",
      authoredWidgetNodeIds: ["12"],
    });

    expect(observed).toEqual({
      nodeIds: ["10", "11", "12"],
      linkIds: ["1", "2", "3"],
      anchorNodeId: "6",
      fingerprint: expect.stringMatching(/^sha256:[0-9a-f]{64}$/),
    });
    expect(JSON.stringify(observed)).not.toContain("A bounded intent");
  });
});

/**
 * M23-37 (D13 host behaviour, observed on ComfyUI frontend 1.51.9). After the one candidate
 * write the host re-serializes the repository-owned nodes from their node definitions: optional
 * inputs the splice never carried are appended unlinked, link inputs are ordered before widget
 * inputs (which renumbers link target slots), empty `widgets_values` arrays disappear and
 * `widgets_values_named` appears. None of that is a change to what the repository wrote.
 */
function hostNormalizedAfterWrite(): Json {
  const after = workflow();
  // Request: a widget input is inserted ahead of the linked input; the link's
  // target slot index moves with it.
  after.nodes[0].inputs.unshift({
    name: "task_mode",
    type: "COMBO",
    widget: { name: "task_mode" },
    link: null,
  });
  after.links[1][4] = 1;
  after.nodes[0].widgets_values_named = {
    task_mode: "i2va",
    user_intent: "A bounded intent.",
    duration_seconds: 8,
  };
  // ProductShell: optional inputs appended unlinked; empty widget values dropped.
  after.nodes[1].inputs.push(
    { name: "recompute_plan", type: "H3_RECOMPUTE_PLAN", link: null },
    {
      name: "pipeline_transaction",
      type: "H3_PIPELINE_TRANSACTION",
      link: null,
    },
  );
  delete after.nodes[1].widgets_values;
  // Output slot arrays gain a named entry the splice omitted.
  after.nodes[1].outputs.push({ name: "product_shell", links: [] });
  return after;
}

describe("M23-37 owned projection under host slot normalization", () => {
  it("is invariant under appended optional inputs, reordered inputs, moved slot indices and dropped empty widget values", () => {
    expect(
      observeOwnedGraph(hostNormalizedAfterWrite(), reference).fingerprint,
    ).toBe(observeOwnedGraph(workflow(), reference).fingerprint);
  });

  it("still changes when an owned link is rebound to a different input", () => {
    const rebound = hostNormalizedAfterWrite();
    // The same link id now feeds a different named input of the same node.
    rebound.nodes[0].inputs = [
      {
        name: "task_mode",
        type: "COMBO",
        widget: { name: "task_mode" },
        link: 2,
      },
      { name: "duration_seconds", link: null },
    ];
    rebound.links[1][4] = 0;
    expect(observeOwnedGraph(rebound, reference).fingerprint).not.toBe(
      observeOwnedGraph(workflow(), reference).fingerprint,
    );
  });

  it("still changes when an owned link's origin output moves", () => {
    const moved = hostNormalizedAfterWrite();
    moved.nodes[2].outputs = [
      { name: "other", links: [] },
      { name: "FLOAT", links: [2, 7] },
    ];
    moved.links[1][2] = 1;
    moved.links[3][2] = 1;
    expect(observeOwnedGraph(moved, reference).fingerprint).not.toBe(
      observeOwnedGraph(workflow(), reference).fingerprint,
    );
  });
});
