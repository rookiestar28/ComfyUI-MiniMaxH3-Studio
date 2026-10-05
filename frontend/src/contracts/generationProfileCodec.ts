import {
  familyProfileKeys as familyKeys,
  generationProfileKeys as rootKeys,
} from "./generatedSurface";

/**
 * The capability projection App Mode gates on, decoded from the backend route.
 *
 * M17-20 D1 puts the decision in the backend on purpose: the browser cannot see
 * the host's node classes, its installed weights or the template bytes it
 * serves, so anything it concluded about generation would be a guess. What
 * arrives here is already a decision -- per materialization basis, a disposition
 * and a remediation -- and this module's only job is to refuse to believe a
 * payload that is not one.
 *
 * The projection is content-free by construction. It names model *roles*
 * (`video_unet`, `text_encoder`) and never a filename, a path or the host's
 * inventory, so nothing decoded here can carry private host state into the UI.
 */

export const GENERATION_PROFILE_SCHEMA =
  "h3.context.generation_profile.v1" as const;

export type GenerationAssetSlot =
  "video_unet" | "reference_unet" | "text_encoder" | "video_vae" | "audio_vae";
export type GenerationModeFamily = "image_to_video" | "reference_to_video";
export type GenerationFamilyDisposition =
  | "available"
  // M17-28: every required role is installed, but at least one is not under the
  // name the template materializes with. Distinct from `missing_asset` because
  // the two send the user to different actions.
  | "asset_relocated"
  | "missing_asset"
  | "template_drift"
  | "unsupported_host";
export type GenerationRemediation =
  | "none"
  | "select_installed_asset_on_canvas"
  | "requalify_template"
  | "upgrade_host";

export type GenerationFamilyProfile = Readonly<{
  family: GenerationModeFamily;
  disposition: GenerationFamilyDisposition;
  remediation: GenerationRemediation;
  anchorNodeType: string;
  templateName: string;
  unsatisfiedSlots: readonly GenerationAssetSlot[];
  taskModes: readonly string[];
}>;

export type GenerationProfile = Readonly<{
  schema: typeof GENERATION_PROFILE_SCHEMA;
  templateRevision: string;
  families: readonly GenerationFamilyProfile[];
}>;

const slots: readonly GenerationAssetSlot[] = [
  "video_unet",
  "reference_unet",
  "text_encoder",
  "video_vae",
  "audio_vae",
];
const families: readonly GenerationModeFamily[] = [
  "image_to_video",
  "reference_to_video",
];
/**
 * The remediation each disposition is allowed to carry.
 *
 * This is a contract check, not a second decision: the backend already paired
 * them, and a payload that pairs them differently is one this shell has not
 * qualified. Refusing it is what keeps a tampered or half-upgraded projection
 * from rendering a remediation that would send the user to fix the wrong thing.
 */
const remediations: Readonly<
  Record<GenerationFamilyDisposition, GenerationRemediation>
> = Object.freeze({
  available: "none",
  asset_relocated: "select_installed_asset_on_canvas",
  missing_asset: "select_installed_asset_on_canvas",
  template_drift: "requalify_template",
  unsupported_host: "upgrade_host",
});

const revisionPattern = /^[0-9a-f]{40}$/;
const namePattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/;
const taskModePattern = /^[a-z][a-z0-9]{1,15}$/;
const MAX_FAMILIES = 8;
const MAX_TASK_MODES = 16;

export class GenerationProfileDecodeError extends Error {
  constructor() {
    // The payload is host state; nothing from it crosses into the message.
    super("generation profile projection rejected");
    this.name = "GenerationProfileDecodeError";
  }
}

function invalid(): never {
  throw new GenerationProfileDecodeError();
}

function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    invalid();
  return value as Record<string, unknown>;
}

function closed(value: Record<string, unknown>, keys: readonly string[]): void {
  if (Object.keys(value).sort().join() !== keys.join()) invalid();
}

function name(value: unknown): string {
  if (typeof value !== "string" || !namePattern.test(value)) invalid();
  return value;
}

function decodeFamily(value: unknown): GenerationFamilyProfile {
  const wire = object(value);
  closed(wire, familyKeys);
  const family = wire.family as GenerationModeFamily;
  if (!families.includes(family)) invalid();
  const disposition = wire.disposition as GenerationFamilyDisposition;
  if (!Object.hasOwn(remediations, disposition)) invalid();
  const remediation = wire.remediation as GenerationRemediation;
  if (remediations[disposition] !== remediation) invalid();

  const unsatisfiedWire = wire.unsatisfied_slots;
  if (!Array.isArray(unsatisfiedWire) || unsatisfiedWire.length > slots.length)
    invalid();
  const unsatisfiedSlots = unsatisfiedWire.map((slot) => {
    if (!slots.includes(slot as GenerationAssetSlot)) invalid();
    return slot as GenerationAssetSlot;
  });
  if (new Set(unsatisfiedSlots).size !== unsatisfiedSlots.length) invalid();
  // Only a slot-level disposition names slots. A drifted template or an
  // unqualified host is not a slot problem, and reporting one for it would be a
  // lie the user acts on.
  const namesSlots =
    disposition === "missing_asset" || disposition === "asset_relocated";
  if (namesSlots !== unsatisfiedSlots.length > 0) invalid();

  const taskModesWire = wire.task_modes;
  if (
    !Array.isArray(taskModesWire) ||
    taskModesWire.length === 0 ||
    taskModesWire.length > MAX_TASK_MODES
  )
    invalid();
  const taskModes = taskModesWire.map((mode) => {
    if (typeof mode !== "string" || !taskModePattern.test(mode)) invalid();
    return mode;
  });
  if (new Set(taskModes).size !== taskModes.length) invalid();

  return Object.freeze({
    family,
    disposition,
    remediation,
    anchorNodeType: name(wire.anchor_node_type),
    templateName: name(wire.template_name),
    unsatisfiedSlots: Object.freeze(unsatisfiedSlots),
    taskModes: Object.freeze(taskModes),
  });
}

export function decodeGenerationProfile(value: unknown): GenerationProfile {
  const wire = object(value);
  closed(wire, rootKeys);
  if (wire.schema !== GENERATION_PROFILE_SCHEMA) invalid();
  if (
    typeof wire.template_revision !== "string" ||
    !revisionPattern.test(wire.template_revision)
  )
    invalid();
  const familiesWire = wire.families;
  if (
    !Array.isArray(familiesWire) ||
    familiesWire.length === 0 ||
    familiesWire.length > MAX_FAMILIES
  )
    invalid();
  const decoded = familiesWire.map(decodeFamily);
  // An entry is identified by its template, not by its family: `t2va` and `i2va`
  // share an anchor and a slot set while materializing from different bytes, so
  // two entries may legitimately carry the same family. Two entries carrying the
  // same template would be two answers about one basis.
  if (
    new Set(decoded.map((entry) => entry.templateName)).size !== decoded.length
  )
    invalid();
  // A task mode belongs to exactly one materialization basis. Two families
  // claiming the same mode would make the admission answer depend on iteration
  // order, so it is rejected rather than resolved.
  const claimed = decoded.flatMap((entry) => entry.taskModes);
  if (new Set(claimed).size !== claimed.length) invalid();
  return Object.freeze({
    schema: GENERATION_PROFILE_SCHEMA,
    templateRevision: wire.template_revision,
    families: Object.freeze(decoded),
  });
}

/**
 * The family that materializes a task mode, taken from the projection itself.
 *
 * The pairing is deliberately not a table in this bundle. `TASK_MODE_FAMILY`
 * lives in the backend contract, and a shell that kept its own copy would be a
 * second authority that can disagree with the host it is gating on.
 */
export function familyProfileForTaskMode(
  profile: GenerationProfile,
  taskMode: string,
): GenerationFamilyProfile | undefined {
  return profile.families.find((entry) => entry.taskModes.includes(taskMode));
}
