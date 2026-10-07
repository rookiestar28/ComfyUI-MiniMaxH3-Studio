import { describe, expect, it } from "vitest";

import {
  listAppModeImageSources,
  listAppModeMediaSources,
} from "../src/host/appModeCensus";

describe("App Mode canvas census", () => {
  it("returns sorted content-free loader identities and removes duplicates", () => {
    const graph = {
      nodes: [
        { id: 20, type: "LoadVideo", widgets_values: ["private.mp4"] },
        { id: 3, type: "LoadImage", widgets_values: ["private.png"] },
        { id: 3, type: "LoadImage", widgets_values: ["other.png"] },
        { id: 2, type: "LoadAudio", widgets_values: ["private.wav"] },
        { id: 99, type: "ForeignLoader" },
      ],
    };
    expect(listAppModeImageSources(graph)).toEqual([
      { node_id: "3", label: "Image node 3" },
    ]);
    expect(listAppModeMediaSources(graph)).toEqual([
      { node_id: "2", label: "Audio node 2", kind: "audio", output_slot: 0 },
      { node_id: "3", label: "Image node 3", kind: "image", output_slot: 0 },
      { node_id: "20", label: "Video node 20", kind: "video", output_slot: 0 },
    ]);
    expect(JSON.stringify(listAppModeMediaSources(graph))).not.toContain(
      "private",
    );
  });
});
