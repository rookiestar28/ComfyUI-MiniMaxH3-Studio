// IMPORTANT: keep the JSON import attribute. Vite accepts the bare form, but
// the supported-host Playwright lane loads appMode through Node ESM as well.
import rawManifest from "../../../comfyui_h3_context/contracts/official_h3_assets_v2.json" with { type: "json" };

import type {
  GenerationAssetSlot,
  GenerationModeFamily,
} from "../contracts/generationProfileCodec";
import { HOST_NODE_DEFINITION_CEILING } from "./hostSeamContract";

type Json = Record<string, unknown>;

const SLOT_ORDER: readonly GenerationAssetSlot[] = [
  "video_unet",
  "reference_unet",
  "text_encoder",
  "video_vae",
  "audio_vae",
];
export type MaterializationAssetRole =
  GenerationAssetSlot | "image_turbo_lora" | "reference_turbo_lora";
const MATERIALIZATION_ROLE_ORDER: readonly MaterializationAssetRole[] = [
  ...SLOT_ORDER,
  "image_turbo_lora",
  "reference_turbo_lora",
];
const FAMILY_ORDER: readonly GenerationModeFamily[] = [
  "image_to_video",
  "reference_to_video",
];
const IMAGE_TO_VIDEO_SLOTS: readonly GenerationAssetSlot[] = Object.freeze([
  "video_unet",
  "text_encoder",
  "video_vae",
  "audio_vae",
]);
const REFERENCE_TO_VIDEO_SLOTS: readonly GenerationAssetSlot[] = Object.freeze([
  "reference_unet",
  "text_encoder",
  "video_vae",
  "audio_vae",
]);
const PROFILE_FAMILY_SLOTS: Readonly<
  Record<GenerationModeFamily, readonly GenerationAssetSlot[]>
> = Object.freeze({
  image_to_video: IMAGE_TO_VIDEO_SLOTS,
  reference_to_video: REFERENCE_TO_VIDEO_SLOTS,
});
const MATERIALIZATION_FAMILY_SLOTS: Readonly<
  Record<GenerationModeFamily, readonly MaterializationAssetRole[]>
> = Object.freeze({
  image_to_video: Object.freeze<MaterializationAssetRole[]>([
    ...IMAGE_TO_VIDEO_SLOTS,
    "image_turbo_lora",
  ]),
  reference_to_video: Object.freeze<MaterializationAssetRole[]>([
    ...REFERENCE_TO_VIDEO_SLOTS,
    "reference_turbo_lora",
  ]),
});
const MANIFEST_SCHEMA = "h3.context.official_assets.v2";
export const OFFICIAL_ASSET_POLICY_ID =
  "template_default_then_manifest_order_v1" as const;
const MAX_INVENTORY_ENTRIES = 50_000;
const MAX_INVENTORY_ENTRY_LENGTH = 4_096;
const MAX_TEMPLATE_NODES = 1_024;
const MAX_SUBGRAPH_DEFINITIONS = 16;

export type OfficialAssetSpec = Readonly<{
  slot: MaterializationAssetRole;
  folderCategory: string;
  loaderType: string;
  widgetName: string;
  templateDefault: string;
  acceptedBasenames: readonly string[];
}>;

export type OfficialAssetManifest = Readonly<{
  schema: typeof MANIFEST_SCHEMA;
  policyId: typeof OFFICIAL_ASSET_POLICY_ID;
  profileFamilies: Readonly<
    Record<GenerationModeFamily, readonly GenerationAssetSlot[]>
  >;
  materializationFamilies: Readonly<
    Record<GenerationModeFamily, readonly MaterializationAssetRole[]>
  >;
  slots: readonly OfficialAssetSpec[];
}>;

export type OfficialAssetInventory = Readonly<
  Record<string, readonly string[]>
>;

export type OfficialAssetSelectionReason =
  "template_default" | "relocated_default" | "official_variant";

export type OfficialAssetResolutionErrorCode =
  | "invalid_manifest"
  | "invalid_host_inventory"
  | "missing_loader_seam"
  | "ambiguous_loader_seam"
  | "malformed_loader_seam";

export class OfficialAssetResolutionError extends Error {
  readonly code: OfficialAssetResolutionErrorCode;

  constructor(code: OfficialAssetResolutionErrorCode) {
    // Host inventory and widget values are private. Nothing derived from either
    // is allowed to enter an exception that a shell or log might retain.
    super("official asset resolution rejected");
    this.name = "OfficialAssetResolutionError";
    this.code = code;
  }
}

function fail(code: OfficialAssetResolutionErrorCode): never {
  throw new OfficialAssetResolutionError(code);
}

function record(value: unknown): Json | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Json)
    : undefined;
}

function boundedName(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > 255 ||
    !/^[A-Za-z0-9_.:-]+$/.test(value)
  )
    fail("invalid_manifest");
  return value;
}

export function parseOfficialAssetManifest(
  value: unknown,
): OfficialAssetManifest {
  const root = record(value);
  if (
    root?.schema !== MANIFEST_SCHEMA ||
    root.policy_id !== OFFICIAL_ASSET_POLICY_ID ||
    !Array.isArray(root.slots) ||
    root.slots.length !== MATERIALIZATION_ROLE_ORDER.length
  )
    fail("invalid_manifest");
  const seen = new Set<MaterializationAssetRole>();
  const slots = root.slots.map((raw, index): OfficialAssetSpec => {
    const row = record(raw);
    const slot = row?.slot as MaterializationAssetRole;
    if (slot !== MATERIALIZATION_ROLE_ORDER[index] || seen.has(slot))
      fail("invalid_manifest");
    seen.add(slot);
    const accepted = row?.accepted_basenames;
    if (
      !Array.isArray(accepted) ||
      accepted.length === 0 ||
      accepted.length > 32
    )
      fail("invalid_manifest");
    const acceptedBasenames = accepted.map(boundedName);
    if (new Set(acceptedBasenames).size !== acceptedBasenames.length)
      fail("invalid_manifest");
    const templateDefault = boundedName(row?.template_default);
    if (!acceptedBasenames.includes(templateDefault)) fail("invalid_manifest");
    return Object.freeze({
      slot,
      folderCategory: boundedName(row?.folder_category),
      loaderType: boundedName(row?.loader_type),
      widgetName: boundedName(row?.widget_name),
      templateDefault,
      acceptedBasenames: Object.freeze(acceptedBasenames),
    });
  });
  if (seen.size !== MATERIALIZATION_ROLE_ORDER.length) fail("invalid_manifest");
  const parseFamilies = <T extends string>(
    value: unknown,
    roleOrder: readonly T[],
    expected: Readonly<Record<GenerationModeFamily, readonly T[]>>,
  ): Readonly<Record<GenerationModeFamily, readonly T[]>> => {
    const rawFamilies = record(value);
    if (
      rawFamilies === undefined ||
      Object.keys(rawFamilies).length !== FAMILY_ORDER.length ||
      FAMILY_ORDER.some((family) => !Object.hasOwn(rawFamilies, family))
    )
      fail("invalid_manifest");
    const parsed = {} as Record<GenerationModeFamily, readonly T[]>;
    for (const family of FAMILY_ORDER) {
      const rawSlots = rawFamilies[family];
      if (!Array.isArray(rawSlots) || rawSlots.length === 0)
        fail("invalid_manifest");
      const declared = rawSlots.map((slot) => {
        if (!roleOrder.includes(slot as T)) fail("invalid_manifest");
        return slot as T;
      });
      if (
        new Set(declared).size !== declared.length ||
        declared.length !== expected[family].length ||
        declared.some((slot, index) => slot !== expected[family][index])
      )
        fail("invalid_manifest");
      parsed[family] = Object.freeze(declared);
    }
    return Object.freeze(parsed);
  };
  const profileFamilies = parseFamilies(
    root.profile_families,
    SLOT_ORDER,
    PROFILE_FAMILY_SLOTS,
  );
  const materializationFamilies = parseFamilies(
    root.materialization_families,
    MATERIALIZATION_ROLE_ORDER,
    MATERIALIZATION_FAMILY_SLOTS,
  );
  return Object.freeze({
    schema: MANIFEST_SCHEMA,
    policyId: OFFICIAL_ASSET_POLICY_ID,
    profileFamilies,
    materializationFamilies,
    slots: Object.freeze(slots),
  });
}

export const OFFICIAL_ASSET_MANIFEST = parseOfficialAssetManifest(rawManifest);

function safeInventoryEntry(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > MAX_INVENTORY_ENTRY_LENGTH ||
    /[\u0000-\u001f\u007f]/.test(value) ||
    value.startsWith("/") ||
    value.startsWith("\\") ||
    /^[A-Za-z][A-Za-z0-9+.-]*:/.test(value)
  )
    fail("invalid_host_inventory");
  const segments = value.replaceAll("\\", "/").split("/");
  if (
    segments.some(
      (segment) =>
        segment.length === 0 ||
        segment === "." ||
        segment === ".." ||
        segment.includes(":"),
    )
  )
    fail("invalid_host_inventory");
  return value;
}

function basename(value: string): string {
  return value.replaceAll("\\", "/").split("/").at(-1)!.toLowerCase();
}

function inventoryRank(left: string, right: string): number {
  const canonical = (value: string): string =>
    value.replaceAll("\\", "/").toLowerCase();
  const leftCanonical = canonical(left);
  const rightCanonical = canonical(right);
  const depth = (value: string): number => value.split("/").length - 1;
  return (
    depth(leftCanonical) - depth(rightCanonical) ||
    leftCanonical.localeCompare(rightCanonical, "en") ||
    left.localeCompare(right, "en")
  );
}

function ranked(
  inventory: readonly string[],
  wantedBasename: string,
): string | undefined {
  const wanted = wantedBasename.toLowerCase();
  return inventory
    .filter((entry) => basename(entry) === wanted)
    .sort(inventoryRank)[0];
}

/** Select one exact host string without ever admitting a non-official basename. */
export function selectOfficialAsset(
  spec: OfficialAssetSpec,
  inventory: readonly string[],
):
  | Readonly<{ value: string; reason: OfficialAssetSelectionReason }>
  | undefined {
  if (inventory.length > MAX_INVENTORY_ENTRIES) fail("invalid_host_inventory");
  const safeInventory = inventory.map(safeInventoryEntry);
  if (safeInventory.includes(spec.templateDefault))
    return Object.freeze({
      value: spec.templateDefault,
      reason: "template_default",
    });
  const relocatedDefault = ranked(safeInventory, spec.templateDefault);
  if (relocatedDefault !== undefined)
    return Object.freeze({
      value: relocatedDefault,
      reason: "relocated_default",
    });
  for (const accepted of spec.acceptedBasenames) {
    if (accepted === spec.templateDefault) continue;
    const candidate = ranked(safeInventory, accepted);
    if (candidate !== undefined)
      return Object.freeze({
        value: candidate,
        reason: "official_variant",
      });
  }
  return undefined;
}

function hostComboValues(
  nodeDefinitions: Json,
  loaderType: string,
  widgetName: string,
): readonly string[] {
  const loader = record(nodeDefinitions[loaderType]);
  const input = record(loader?.input);
  const required = record(input?.required);
  const combo = required?.[widgetName];
  if (!Array.isArray(combo) || !Array.isArray(combo[0]))
    fail("invalid_host_inventory");
  const values = combo[0];
  if (values.length > MAX_INVENTORY_ENTRIES) fail("invalid_host_inventory");
  return Object.freeze(values.map(safeInventoryEntry));
}

/** Read only the three host combo arrays the shared manifest names. */
export function readOfficialAssetInventory(
  value: unknown,
): OfficialAssetInventory {
  const nodeDefinitions = record(value);
  if (nodeDefinitions === undefined) fail("invalid_host_inventory");
  const result: Record<string, readonly string[]> = {};
  const observed = new Map<string, readonly string[]>();
  for (const spec of OFFICIAL_ASSET_MANIFEST.slots) {
    const key = `${spec.loaderType}:${spec.widgetName}`;
    const values =
      observed.get(key) ??
      hostComboValues(nodeDefinitions, spec.loaderType, spec.widgetName);
    observed.set(key, values);
    const existing = result[spec.folderCategory];
    if (
      existing !== undefined &&
      (existing.length !== values.length ||
        existing.some((entry, index) => entry !== values[index]))
    )
      fail("invalid_host_inventory");
    result[spec.folderCategory] = values;
  }
  return Object.freeze(result);
}

function registeredNodeData(value: unknown): Json | undefined {
  if (
    value === null ||
    (typeof value !== "object" && typeof value !== "function")
  )
    return undefined;
  return record((value as { nodeData?: unknown }).nodeData);
}

/** Read the object-info records retained on the host's loaded node classes. */
export function readOfficialAssetInventoryFromNodeDefinitions(
  value: unknown,
): OfficialAssetInventory {
  const registry = record(value);
  if (registry === undefined) fail("invalid_host_inventory");
  const entries = Object.values(registry);
  if (entries.length === 0 || entries.length > HOST_NODE_DEFINITION_CEILING)
    fail("invalid_host_inventory");
  const nodeDefinitions: Json = {};
  for (const loaderType of new Set(
    OFFICIAL_ASSET_MANIFEST.slots.map((spec) => spec.loaderType),
  )) {
    const matches = entries
      .map(registeredNodeData)
      .filter((nodeData) => nodeData?.name === loaderType);
    if (matches.length !== 1) fail("invalid_host_inventory");
    nodeDefinitions[loaderType] = matches[0]!;
  }
  return readOfficialAssetInventory(nodeDefinitions);
}

type LoaderLocation = {
  readonly owner: Json;
  readonly widgetIndex: number;
  readonly namedWidgetKey: string;
};

function nodes(value: unknown): Json[] {
  if (!Array.isArray(value) || value.length > MAX_TEMPLATE_NODES)
    fail("malformed_loader_seam");
  const result = value.map(record);
  if (result.some((entry) => entry === undefined))
    fail("malformed_loader_seam");
  return result as Json[];
}

function widgetValues(node: Json): unknown[] {
  if (!Array.isArray(node.widgets_values)) fail("malformed_loader_seam");
  return node.widgets_values;
}

function sameId(left: unknown, right: unknown): boolean {
  return (
    (typeof left === "string" || typeof left === "number") &&
    (typeof right === "string" || typeof right === "number") &&
    String(left) === String(right)
  );
}

function namedWidgetIndex(
  node: Json,
  widgetName: string,
  templateDefault: string,
): number | undefined {
  const values = widgetValues(node);
  const named = record(node.widgets_values_named);
  if (named !== undefined) {
    if (!Object.hasOwn(named, widgetName)) return undefined;
    const index = Object.keys(named).indexOf(widgetName);
    const value = named[widgetName];
    if (
      index < 0 ||
      index >= values.length ||
      values[index] !== value ||
      typeof value !== "string"
    )
      fail("malformed_loader_seam");
    return basename(value) === templateDefault.toLowerCase()
      ? index
      : undefined;
  }
  const matching = values
    .map((value, index) => ({ value, index }))
    .filter(
      ({ value }) =>
        typeof value === "string" &&
        basename(value) === templateDefault.toLowerCase(),
    );
  if (matching.length > 1) fail("malformed_loader_seam");
  return matching[0]?.index;
}

function directLocations(
  workflow: Json,
  spec: OfficialAssetSpec,
): LoaderLocation[] {
  const found: LoaderLocation[] = [];
  for (const owner of nodes(workflow.nodes).filter(
    (node) => node.type === spec.loaderType,
  )) {
    const widgetIndex = namedWidgetIndex(
      owner,
      spec.widgetName,
      spec.templateDefault,
    );
    if (widgetIndex !== undefined)
      found.push({ owner, widgetIndex, namedWidgetKey: spec.widgetName });
  }
  return found;
}

function nestedLocations(
  workflow: Json,
  spec: OfficialAssetSpec,
): LoaderLocation[] {
  const definitions = record(workflow.definitions);
  const rawSubgraphs = definitions?.subgraphs ?? [];
  if (
    !Array.isArray(rawSubgraphs) ||
    rawSubgraphs.length > MAX_SUBGRAPH_DEFINITIONS
  )
    fail("malformed_loader_seam");
  const topLevel = nodes(workflow.nodes);
  const found: LoaderLocation[] = [];
  for (const rawDefinition of rawSubgraphs) {
    const definition = record(rawDefinition);
    if (definition === undefined) fail("malformed_loader_seam");
    const inputs = nodes(definition.inputs);
    const links = nodes(definition.links);
    const innerNodes = nodes(definition.nodes);
    const widgetInputs = inputs.filter((input) => input.type !== "IMAGE");
    const instances = topLevel.filter((node) =>
      sameId(node.type, definition.id),
    );
    for (const inner of innerNodes.filter(
      (node) =>
        node.type === spec.loaderType &&
        namedWidgetIndex(node, spec.widgetName, spec.templateDefault) !==
          undefined,
    )) {
      const innerInputs = nodes(inner.inputs);
      const matchingInputs = innerInputs.filter(
        (entry) => entry.name === spec.widgetName,
      );
      if (matchingInputs.length !== 1) fail("malformed_loader_seam");
      const input = matchingInputs[0]!;
      const targetSlot = innerInputs.indexOf(input);
      const matchingLinks = links.filter(
        (entry) =>
          sameId(entry.id, input.link) &&
          sameId(entry.target_id, inner.id) &&
          entry.target_slot === targetSlot,
      );
      if (matchingLinks.length !== 1) fail("malformed_loader_seam");
      const link = matchingLinks[0]!;
      if (
        link.origin_id !== -10 ||
        !Number.isSafeInteger(link.origin_slot) ||
        (link.origin_slot as number) < 0
      )
        fail("malformed_loader_seam");
      const promoted = inputs[link.origin_slot as number];
      if (promoted === undefined || typeof promoted.name !== "string")
        fail("malformed_loader_seam");
      for (const owner of instances) {
        const namedIndex = namedWidgetIndex(
          owner,
          promoted.name,
          spec.templateDefault,
        );
        const fallbackIndex = widgetInputs.indexOf(promoted);
        const widgetIndex = namedIndex ?? fallbackIndex;
        if (widgetIndex < 0) fail("malformed_loader_seam");
        const current = widgetValues(owner)[widgetIndex];
        if (
          typeof current !== "string" ||
          basename(current) !== spec.templateDefault.toLowerCase()
        )
          fail("malformed_loader_seam");
        found.push({
          owner,
          widgetIndex,
          namedWidgetKey: promoted.name,
        });
      }
    }
  }
  return found;
}

function loaderLocation(
  workflow: Json,
  spec: OfficialAssetSpec,
): LoaderLocation {
  const locations = [
    ...directLocations(workflow, spec),
    ...nestedLocations(workflow, spec),
  ];
  if (locations.length === 0) fail("missing_loader_seam");
  if (locations.length > 1) fail("ambiguous_loader_seam");
  return locations[0]!;
}

export type OfficialAssetResolution = Readonly<{
  workflow: Json;
  policyId: typeof OFFICIAL_ASSET_POLICY_ID;
  changedSlots: readonly MaterializationAssetRole[];
  unresolvedSlots: readonly MaterializationAssetRole[];
  selections: readonly Readonly<{
    slot: MaterializationAssetRole;
    reason: OfficialAssetSelectionReason;
  }>[];
  bindings: readonly Readonly<{
    slot: MaterializationAssetRole;
    loaderType: string;
    widgetName: string;
    value: string;
  }>[];
}>;

/** Assist known official roles on a clone; ComfyUI validates model names at queue time. */
export function resolveOfficialAssets(
  template: Json,
  family: GenerationModeFamily,
  inventory: OfficialAssetInventory,
): OfficialAssetResolution {
  const required = OFFICIAL_ASSET_MANIFEST.materializationFamilies[family];
  if (required === undefined) fail("invalid_manifest");
  const workflow = structuredClone(template) as Json;
  const changedSlots: MaterializationAssetRole[] = [];
  const unresolvedSlots: MaterializationAssetRole[] = [];
  const selections: Array<{
    slot: MaterializationAssetRole;
    reason: OfficialAssetSelectionReason;
  }> = [];
  const bindings: Array<{
    slot: MaterializationAssetRole;
    loaderType: string;
    widgetName: string;
    value: string;
  }> = [];
  for (const slot of required) {
    const spec = OFFICIAL_ASSET_MANIFEST.slots.find(
      (candidate) => candidate.slot === slot,
    );
    if (spec === undefined) fail("invalid_manifest");
    let location: LoaderLocation;
    try {
      location = loaderLocation(workflow, spec);
    } catch (error) {
      if (!(error instanceof OfficialAssetResolutionError)) throw error;
      // CRITICAL: a template revision or user rename can defeat this optional
      // name-based locator. Preserve its values for ComfyUI queue validation;
      // treating an unresolved role as a broken graph prevents App Mode startup.
      unresolvedSlots.push(slot);
      continue;
    }
    const choice = selectOfficialAsset(
      spec,
      inventory[spec.folderCategory] ?? [],
    );
    if (choice === undefined) {
      unresolvedSlots.push(slot);
      continue;
    }
    const values = [...widgetValues(location.owner)];
    if (values[location.widgetIndex] !== choice.value) changedSlots.push(slot);
    values[location.widgetIndex] = choice.value;
    location.owner.widgets_values = values;
    const named = record(location.owner.widgets_values_named);
    if (named !== undefined) {
      if (!Object.hasOwn(named, location.namedWidgetKey))
        fail("malformed_loader_seam");
      location.owner.widgets_values_named = {
        ...named,
        [location.namedWidgetKey]: choice.value,
      };
    }
    selections.push({ slot, reason: choice.reason });
    bindings.push({
      slot,
      loaderType: spec.loaderType,
      widgetName: spec.widgetName,
      value: choice.value,
    });
  }
  const frozenSelections: readonly Readonly<{
    slot: MaterializationAssetRole;
    reason: OfficialAssetSelectionReason;
  }>[] = selections.map((selection) => Object.freeze({ ...selection }));
  const frozenBindings = bindings.map((binding) =>
    Object.freeze({ ...binding }),
  );
  return Object.freeze({
    workflow,
    policyId: OFFICIAL_ASSET_POLICY_ID,
    changedSlots: Object.freeze(changedSlots),
    unresolvedSlots: Object.freeze(unresolvedSlots),
    selections: Object.freeze(frozenSelections),
    bindings: Object.freeze(frozenBindings),
  });
}
