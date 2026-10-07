import { describe, expect, it } from "vitest";

import { inspectVisibleH3Graph } from "../src/host/graphAdapter";

const manifest = {
  fields: [
    {
      field_id:
        "h3.comfyui_h3_context_h3context_productshell.output.product_shell",
      node_id: "comfyui_h3_context.H3Context.ProductShell",
      port_name: "product_shell",
    },
  ],
};

const canonical = [
  "comfyui_h3_context.H3Context.Request",
  "comfyui_h3_context.H3Context.Plan",
  "comfyui_h3_context.H3Context.Compiler",
  "comfyui_h3_context.H3Context.Validator",
  "comfyui_h3_context.H3Context.NativeH3Adapter",
  "comfyui_h3_context.H3Context.ProductShell",
  "comfyui_h3_context.H3Context.Preview",
  "MiniMaxH3ImageToVideo",
].map((type, index) => ({ id: index + 1, type }));

describe("M15-13 graph decision gate", () => {
  it("does not present ProductShell-only as a compatible run target", () => {
    expect(
      inspectVisibleH3Graph(
        {
          nodes: [{ id: 8, type: "comfyui_h3_context.H3Context.ProductShell" }],
        },
        manifest,
      ),
    ).toMatchObject({ status: "incompatible", reason: "incomplete_h3_flow" });
  });

  it("fails closed for a dangling serialized link before queue decisions", () => {
    expect(
      inspectVisibleH3Graph(
        {
          nodes: canonical,
          links: [[1, 1, 0, 999, 0, "H3"]],
        },
        manifest,
      ),
    ).toMatchObject({ status: "incompatible" });
  });
});
