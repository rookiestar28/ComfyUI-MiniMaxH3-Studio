import { describe, expect, it } from "vitest";

import {
  OFFICIAL_ASSET_MANIFEST,
  readOfficialAssetInventory,
  resolveOfficialAssets,
} from "../src/host/officialAssetResolution";

const suppliedHostUrl = process.env.H3_CONTEXT_HOST_URL;

function requiredLoopbackHost(): URL {
  if (suppliedHostUrl === undefined)
    throw new Error("an explicit supplied host URL is required");
  const url = new URL(suppliedHostUrl);
  if (
    url.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
    url.username.length > 0 ||
    url.password.length > 0 ||
    url.search.length > 0 ||
    url.hash.length > 0
  )
    throw new Error(
      "the supplied host URL is not an uncredentialed loopback URL",
    );
  return url;
}

describe.skipIf(suppliedHostUrl === undefined)(
  "supplied host official asset resolver compatibility",
  () => {
    it("allows advisory unresolved roles in all three served templates without retaining names", async () => {
      const base = requiredLoopbackHost();
      const objectInfoResponse = await fetch(new URL("/object_info", base));
      expect(objectInfoResponse.ok).toBe(true);
      const inventory = readOfficialAssetInventory(
        await objectInfoResponse.json(),
      );
      const routes = [
        ["video_minimax_h3_t2v", "image_to_video"],
        ["video_minimax_h3_i2v", "image_to_video"],
        ["video_minimax_h3_r2v", "reference_to_video"],
      ] as const;
      for (const [templateName, family] of routes) {
        const response = await fetch(
          new URL(`/templates/${templateName}.json`, base),
        );
        expect(response.ok).toBe(true);
        const template = (await response.json()) as Record<string, unknown>;
        const resolution = resolveOfficialAssets(template, family, inventory);
        const expectedRoles =
          OFFICIAL_ASSET_MANIFEST.materializationFamilies[family].length;
        expect(
          resolution.unresolvedSlots.length + resolution.selections.length,
        ).toBe(expectedRoles);
        // No known installed names must still preserve the complete served graph.
        const unknown = resolveOfficialAssets(template, family, {});
        expect(unknown.unresolvedSlots).toHaveLength(expectedRoles);
        expect(unknown.selections).toHaveLength(0);
        expect(
          JSON.stringify(unknown.workflow) === JSON.stringify(template),
        ).toBe(true);
      }
    });
  },
);
