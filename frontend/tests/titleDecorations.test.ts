import { describe, expect, it } from "vitest";
import fixture from "../../tests/fixtures/m25_10_composition_contract_v1.json";
import {
  decodePublicCompositionSnapshot,
  publicCompositionFingerprint,
} from "../src/contracts/compositionCodec";
import { textStyleWire } from "../src/components/nle/nleCommandBuilders";
import { validateVisualText } from "../src/runtime/visualCompositor";

function snapshot() {
  const wire = structuredClone(fixture.snapshot);
  const text = wire.clips[3]!.text! as Record<string, unknown>;
  text.stroke = { rgba: [1, 2, 3, 128], width_px: 32 };
  text.shadow = { rgba: [4, 5, 6, 255], offset_x_px: 0, offset_y_px: 0 };
  text.background_rgba = [7, 8, 9, 128];
  text.background_padding_px = 64;
  wire.public_fingerprint = publicCompositionFingerprint(wire);
  return { wire, text };
}

describe("title decoration wire", () => {
  it("preserves complete decorations across the command builder", () => {
    const { wire, text } = snapshot();
    const clip = decodePublicCompositionSnapshot(wire).clips[3]!;
    const { content: _content, ...style } = text;
    expect(textStyleWire(clip)).toEqual(style);
  });
  it.each([0, 65, true, null])("refuses noncanonical padding %s", (padding) => {
    const { wire, text } = snapshot();
    text.background_padding_px = padding;
    wire.public_fingerprint = publicCompositionFingerprint(wire);
    expect(() => decodePublicCompositionSnapshot(wire)).toThrow();
  });
  it.each([
    ["stroke", null],
    ["stroke", { rgba: [0, 0, 0, 0], width_px: 1 }],
    ["stroke", { rgba: [0, 0, 0, 255], width_px: 0 }],
    ["stroke", { rgba: [0, 0, 0, 255], width_px: 33 }],
    ["stroke", { rgba: [0, 0, 0, 255], width_px: true }],
    ["stroke", { rgba: [0, 0, 0, 255], width_px: 1.5 }],
    ["stroke", { rgba: [0, 0, 0, 255], width_px: "1" }],
    ["stroke", { rgba: [0, 0, 0, 255] }],
    ["stroke", { rgba: [0, 0, 0, 256], width_px: 1 }],
    ["stroke", { rgba: [0, 0, 0, true], width_px: 1 }],
    ["stroke", { rgba: [0, 0, 0], width_px: 1 }],
    ["stroke", { rgba: [0, 0, 0, 255], width_px: 1, blur: 1 }],
    ["shadow", null],
    ["shadow", { rgba: [0, 0, 0, 0], offset_x_px: 0, offset_y_px: 0 }],
    ["shadow", { rgba: [0, 0, 0, 255], offset_x_px: -65, offset_y_px: 0 }],
    ["shadow", { rgba: [0, 0, 0, 255], offset_x_px: 0, offset_y_px: 65 }],
    ["shadow", { rgba: [0, 0, 0, 255], offset_x_px: true, offset_y_px: 0 }],
    ["shadow", { rgba: [0, 0, 0, 255], offset_x_px: 0, offset_y_px: 1.5 }],
    ["shadow", { rgba: [0, 0, 0, 255], offset_x_px: 0 }],
    [
      "shadow",
      { rgba: [0, 0, 0, 255], offset_x_px: 0, offset_y_px: 0, blur: 1 },
    ],
  ])("refuses a closed noncanonical %s decoration", (member, value) => {
    const { wire, text } = snapshot();
    text[member as string] = value;
    expect(() => {
      wire.public_fingerprint = publicCompositionFingerprint(wire);
      decodePublicCompositionSnapshot(wire);
    }).toThrow();
  });
  it("retains a terminal empty band without widening the admitted line budget", () => {
    expect(validateVisualText("H\n".repeat(32))).toHaveLength(33);
    expect(() => validateVisualText("H\n".repeat(33))).toThrow();
  });
});
