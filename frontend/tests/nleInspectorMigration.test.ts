import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const NLE = join(__dirname, "..", "src", "components", "nle");

describe("M25-50 legacy inspector migration", () => {
  it("removes the legacy mount and gives every migrated owner one canonical source control", () => {
    expect(existsSync(join(NLE, "NleInspector.tsx"))).toBe(false);
    const workspace = readFileSync(join(NLE, "NleWorkspace.tsx"), "utf8");
    expect(workspace).not.toContain('NleInspector"');
    expect(workspace).toContain("NleInspectorTabs");

    const source = [
      "NleInspectorTabs.tsx",
      "NleTimelineMenus.tsx",
      "NleTimeline.tsx",
      // M25-62: the track toggles moved into the extracted header.
      "NleTrackHeader.tsx",
    ]
      .map((file) => readFileSync(join(NLE, file), "utf8"))
      .join("\n");
    const owners = [
      "track.add",
      "track.remove",
      "track.reorder",
      "track.enabled",
      "track.locked",
      "asset.replace",
      "clip.merge",
      "clip.slip",
      "clip.slide",
      "clip.enabled",
      "visual.transform",
      "visual.opacity_blend",
      "visual.crop",
      "visual.effect",
      "text.content",
      "text.style",
      "boundary.transition",
    ];
    for (const owner of owners) {
      const marker = `data-h3-nle-control="${owner}"`;
      expect(source.split(marker)).toHaveLength(2);
    }
  });
});
