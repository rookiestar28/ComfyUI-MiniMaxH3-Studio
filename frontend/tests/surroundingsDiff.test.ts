import { describe, expect, it } from "vitest";

import { diffGraphSurroundings } from "./support/surroundingsDiff";

type Json = Record<string, any>;

function graph(): Json {
  return {
    last_node_id: 99,
    last_link_id: 7,
    nodes: [
      {
        id: 10,
        type: "comfyui_h3_context.H3Context.Request",
        mode: 0,
        pos: [10, 20],
        size: [300, 120],
        inputs: [{ name: "duration_seconds", link: 2 }],
        outputs: [{ links: [1] }],
        properties: {},
        widgets_values: ["i2va", "private prompt value", 8],
      },
      {
        id: 12,
        type: "PrimitiveFloat",
        mode: 0,
        pos: [30, 40],
        inputs: [],
        outputs: [{ links: [2] }],
        properties: {},
        widgets_values: [8],
      },
      {
        id: 6,
        type: "MiniMaxH3ImageToVideo",
        mode: 0,
        inputs: [{ name: "prompt", link: 3 }],
        outputs: [],
        properties: {},
        widgets_values: ["private-input.jpg", 192],
      },
      {
        id: 99,
        type: "ForeignSeed",
        mode: 0,
        pos: [50, 60],
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
    ],
    groups: [],
    config: {},
    extra: {},
    version: 0.4,
  };
}

const reference = {
  ownedNodeIds: ["10", "12"],
  ownedLinkIds: ["1", "2", "3"],
  anchorNodeId: "6",
  authoredWidgetNodeIds: ["12"],
};

describe("D12 surroundings evidence", () => {
  it("buckets noisy-host changes without promoting surroundings to owned identity", () => {
    const before = graph();
    const after = structuredClone(before);
    after.nodes[0].pos[0] = 900;
    after.nodes[0].properties.cnr_id = "fake-pack";
    after.nodes[0].properties.opaque_extension_field = "ignored value";
    after.nodes[0].widgets_values[1] = "a different private prompt";
    after.nodes[3].widgets_values[0] = 987654321;
    after.extra.ds = { scale: 1.1, offset: [2, 3] };
    after.extra.frontendVersion = "9.9.9";
    after.extra.someExtension = { revision: 2 };
    after.widget_idx_map = { "99": { seed: 0 } };
    after.seed_widgets = { "99": 0 };

    const report = diffGraphSurroundings({
      beforeValue: before,
      afterValue: after,
      reference: { ...reference, ownedProjectionEqual: true },
    });

    expect(report.counts).toMatchObject({
      presentation: expect.any(Number),
      host_metadata: 2,
      queue_wrapper: 2,
      user_parameter: 2,
      foreign_extension: 2,
      owned: 0,
    });
    expect(report.counts.presentation).toBeGreaterThan(0);
    expect(report.paths.presentation).toContain("$graph.nodes[id=10].pos[0]");
    expect(report.paths.host_metadata).toContain(
      "$graph.nodes[id=10].properties.cnr_id",
    );
    expect(report.paths.foreign_extension).toContain("$graph.extra.<name>");
  });

  it.each([
    [
      "authored duration",
      (value: Json) => (value.nodes[1].widgets_values[0] = 12),
    ],
    ["owned link", (value: Json) => (value.links[1][3] = 99)],
    [
      "anchor prompt binding",
      (value: Json) => (value.nodes[2].inputs[0].link = null),
    ],
  ])("classifies an %s identity change as owned", (_label, mutate) => {
    const before = graph();
    const after = structuredClone(before);
    mutate(after);

    const report = diffGraphSurroundings({
      beforeValue: before,
      afterValue: after,
      reference: { ...reference, ownedProjectionEqual: false },
    });

    expect(report.counts.owned).toBeGreaterThan(0);
  });

  it("never returns prompts, locators, values, or dynamic private keys", () => {
    const before = graph();
    before.dynamic_private_container = { id: "before" };
    before.extra.someExtension = { id: "before" };
    const after = structuredClone(before);
    after.extra["C:\\Users\\Private\\secret.jpg"] = {
      prompt: "do not disclose this prompt",
    };
    after.nodes[3].id = "family-video-secret.mp4";
    after.nodes[3].widgets_values[0] = 123456789;
    after.dynamic_private_container.id = "after";
    after.extra.someExtension.id = "after";

    const serialized = JSON.stringify(
      diffGraphSurroundings({
        beforeValue: before,
        afterValue: after,
        reference: { ...reference, ownedProjectionEqual: true },
      }),
    );

    expect(serialized).not.toContain("Users");
    expect(serialized).not.toContain("secret.jpg");
    expect(serialized).not.toContain("family-video-secret.mp4");
    expect(serialized).not.toContain("do not disclose");
    expect(serialized).not.toContain("123456789");
    expect(serialized).toContain("$graph.extra.<name>");
    expect(serialized).toContain("$graph.extra.<name>.<name>");
    expect(serialized).toContain("$graph.<name>.<name>");
    expect(serialized).not.toContain("$graph.extra.<name>.id");
    expect(serialized).not.toContain("$graph.<name>.id");
    expect(serialized).toContain("$graph.nodes[id=<redacted>]");
  });
});
