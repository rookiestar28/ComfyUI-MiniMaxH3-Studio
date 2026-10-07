import {
  OFFICIAL_ASSET_MANIFEST,
  type OfficialAssetInventory,
} from "../../src/host/officialAssetResolution";
import { syntheticTemplate, type Json } from "./templateFixture";

/**
 * Official-asset inventory fixtures (M17-29), shared by the resolution rows and
 * the M23-37 host-registration rows: a host whose public node-definition
 * registry carries loader inventories, and a template whose loaders resolve
 * against it.
 */

export type OfficialAssetFamily = "image_to_video" | "reference_to_video";

export function familySpecs(family: OfficialAssetFamily) {
  const slots = OFFICIAL_ASSET_MANIFEST.materializationFamilies[family];
  return slots.map((slot) => {
    const spec = OFFICIAL_ASSET_MANIFEST.slots.find(
      (entry) => entry.slot === slot,
    );
    if (spec === undefined) throw new Error("fixture manifest drift");
    return spec;
  });
}

export function installedInventory(
  family: OfficialAssetFamily,
  omit: readonly string[] = [],
): { inventory: OfficialAssetInventory; chosen: Map<string, string> } {
  const inventory: Record<string, string[]> = {};
  const chosen = new Map<string, string>();
  for (const spec of familySpecs(family)) {
    if (omit.includes(spec.slot)) continue;
    const basename =
      spec.acceptedBasenames.find((entry) => entry !== spec.templateDefault) ??
      spec.templateDefault;
    const exact = `Official\\${spec.slot}\\${basename}`;
    (inventory[spec.folderCategory] ??= []).push(exact);
    chosen.set(spec.slot, exact);
  }
  return { inventory, chosen };
}

export function templateWithDirectLoaders(): Json {
  const workflow = syntheticTemplate("video_minimax_h3_t2v");
  const nodes = workflow.nodes as Json[];
  for (const [index, spec] of familySpecs("image_to_video").entries()) {
    nodes.push({
      id: 200 + index,
      type: spec.loaderType,
      inputs: [{ name: spec.widgetName, type: "COMBO" }],
      widgets_values: [spec.templateDefault],
      widgets_values_named: { [spec.widgetName]: spec.templateDefault },
    });
  }
  return workflow;
}

export function nodeDefinitionPayloads(
  inventory: OfficialAssetInventory,
): Record<string, unknown> {
  const byType: Record<
    string,
    { input: { required: Record<string, unknown> } }
  > = {};
  for (const spec of OFFICIAL_ASSET_MANIFEST.slots) {
    const type = (byType[spec.loaderType] ??= { input: { required: {} } });
    type.input.required[spec.widgetName] = [
      inventory[spec.folderCategory] ?? [],
    ];
  }
  return byType;
}

export function nodeDefinitions(
  inventory: OfficialAssetInventory,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(nodeDefinitionPayloads(inventory)).map(
      ([loaderType, nodeData]) => [
        `loaders/${loaderType}`,
        { nodeData: { name: loaderType, ...(nodeData as object) } },
      ],
    ),
  );
}
