import { describe, expect, it } from "vitest";

import {
  saveVideoArtifactFromExecuted,
  saveVideoArtifactFromOutput,
} from "../src/host/saveVideoArtifact";
import { saveVideoArtifactFromExecuted as reexported } from "../src/host/sidebarHost";

const locator = {
  filename: "managed_00001_.mp4",
  subfolder: "video/h3-context",
  type: "output",
} as const;
// The entry ComfyUI sends after v0.39.0: media metadata always, the asset id when assets are on.
const mediaMetadata = {
  kind: "video",
  width: 1280,
  height: 720,
  duration: 8,
  fps: 24,
  frame_count: 192,
};
const expected = {
  promptId: "prompt.model.1",
  outputNodeId: "43",
  locator,
};

function executed(entry: unknown, output: Record<string, unknown> = {}) {
  return {
    prompt_id: "prompt.model.1",
    node: "43",
    display_node: "43",
    output: { images: [entry], animated: [true], ...output },
  };
}

describe("SaveVideo artifact decoding across host entry shapes", () => {
  it.each([
    ["stock three-key entry", locator],
    ["entry with host media metadata", { ...locator, metadata: mediaMetadata }],
    [
      "entry with host media metadata and asset id",
      { ...locator, metadata: mediaMetadata, id: "asset-managed-00001" },
    ],
    [
      "entry with a later unknown host annotation",
      { ...locator, metadata: mediaMetadata, preview_hint: { any: 1 } },
    ],
  ])("admits the %s and forwards only the locator", (_name, entry) => {
    const artifact = saveVideoArtifactFromExecuted(executed(entry));
    expect(artifact).toEqual(expected);
    expect(Object.keys(artifact!.locator).sort()).toEqual([
      "filename",
      "subfolder",
      "type",
    ]);
    expect(Object.isFrozen(artifact)).toBe(true);
    expect(Object.isFrozen(artifact!.locator)).toBe(true);
    expect(JSON.stringify(artifact)).not.toContain("asset-managed");
    expect(JSON.stringify(artifact)).not.toContain("frame_count");
  });

  it("keeps the sidebar host export as the same decoder", () => {
    expect(reexported).toBe(saveVideoArtifactFromExecuted);
  });

  it.each([
    ["two saved entries", { images: [locator, locator], animated: [true] }],
    ["no animated flag", { images: [locator] }],
    ["animated false", { images: [locator], animated: [false] }],
    [
      "an extra output member",
      { images: [locator], animated: [true], gifs: [locator] },
    ],
    [
      "an input preview",
      {
        images: [{ ...locator, type: "input", metadata: mediaMetadata }],
        animated: [true],
      },
    ],
    [
      "a temporary preview",
      { images: [{ ...locator, type: "temp" }], animated: [true] },
    ],
    [
      "a missing subfolder",
      {
        images: [{ filename: locator.filename, type: "output" }],
        animated: [true],
      },
    ],
    [
      "an empty filename",
      { images: [{ ...locator, filename: "" }], animated: [true] },
    ],
    [
      "an oversized filename",
      { images: [{ ...locator, filename: "a".repeat(256) }], animated: [true] },
    ],
    [
      "a non-string subfolder",
      { images: [{ ...locator, subfolder: 3 }], animated: [true] },
    ],
    [
      "an oversized subfolder",
      {
        images: [{ ...locator, subfolder: "s".repeat(513) }],
        animated: [true],
      },
    ],
    ["an entry that is not an object", { images: ["x.mp4"], animated: [true] }],
  ])("still refuses %s", (_name, output) => {
    expect(
      saveVideoArtifactFromOutput("prompt.model.1", "43", output),
    ).toBeUndefined();
  });

  it.each([
    ["an empty prompt id", "", "43"],
    ["a prompt id with a path separator", "prompt/1", "43"],
    ["an oversized node id", "prompt.model.1", "n".repeat(257)],
    ["a non-string node id", "prompt.model.1", 43],
  ])("refuses %s", (_name, promptId, nodeId) => {
    expect(
      saveVideoArtifactFromOutput(promptId, nodeId, {
        images: [locator],
        animated: [true],
      }),
    ).toBeUndefined();
  });

  it("refuses an executed detail that is not an object", () => {
    expect(saveVideoArtifactFromExecuted(null)).toBeUndefined();
    expect(saveVideoArtifactFromExecuted([executed(locator)])).toBeUndefined();
  });
});
