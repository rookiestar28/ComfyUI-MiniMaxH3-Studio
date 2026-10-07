import {
  decodeGenerationProfile,
  GENERATION_PROFILE_SCHEMA,
  type GenerationProfile,
} from "../../src/contracts/generationProfileCodec";

/**
 * The wire payload `comfyui_h3_context.core.generation_profile` publishes for a
 * host where everything resolves.
 *
 * It is written out longhand rather than derived, so a change to the backend
 * contract shows up here as a decode failure instead of quietly propagating
 * through a shared builder.
 */
export const PINNED_TEMPLATE_REVISION =
  "5097de61ef09fe75466716ac0b200515f5ea078f"; // pragma: allowlist secret

export type FamilyOverride = {
  disposition: string;
  remediation: string;
  unsatisfied_slots?: string[];
};

/**
 * The three materialization bases the backend qualifies.
 *
 * Keyed by basis rather than by family on purpose: `t2va` and `i2va` share the
 * `image_to_video` family and its slots but load different template bytes, so a
 * fixture that collapsed them would make it impossible to write a row where one
 * basis drifts and the other does not.
 */
const BASES = {
  text: {
    family: "image_to_video",
    anchor_node_type: "MiniMaxH3ImageToVideo",
    template_name: "video_minimax_h3_t2v",
    task_modes: ["t2va"],
  },
  image: {
    family: "image_to_video",
    anchor_node_type: "MiniMaxH3ImageToVideo",
    template_name: "video_minimax_h3_i2v",
    task_modes: ["i2va", "fl2va", "l2va"],
  },
  reference: {
    family: "reference_to_video",
    anchor_node_type: "MiniMaxH3ReferenceToVideo",
    template_name: "video_minimax_h3_r2v",
    task_modes: ["ref2va"],
  },
} as const;

export type BasisName = keyof typeof BASES;
export type BasisOverrides = Partial<Record<BasisName, FamilyOverride>>;

function basis(
  name: BasisName,
  override?: FamilyOverride,
): Record<string, unknown> {
  return {
    ...BASES[name],
    task_modes: [...BASES[name].task_modes],
    disposition: override?.disposition ?? "available",
    remediation: override?.remediation ?? "none",
    unsatisfied_slots: override?.unsatisfied_slots ?? [],
  };
}

export function generationProfileWire(
  overrides?: BasisOverrides,
): Record<string, unknown> {
  return {
    schema: GENERATION_PROFILE_SCHEMA,
    template_revision: PINNED_TEMPLATE_REVISION,
    families: [
      basis("text", overrides?.text),
      basis("image", overrides?.image),
      basis("reference", overrides?.reference),
    ],
  };
}

/**
 * M17-28. Every required weight is installed; at least one is not under the name
 * the materialized workflow carries. It names slots like `MISSING_ASSET` does and
 * shares its remediation, which is exactly why the rows below check that the two
 * are still told apart.
 */
export const ASSET_RELOCATED: FamilyOverride = {
  disposition: "asset_relocated",
  remediation: "select_installed_asset_on_canvas",
  unsatisfied_slots: ["video_unet", "text_encoder"],
};
export const MISSING_ASSET: FamilyOverride = {
  disposition: "missing_asset",
  remediation: "select_installed_asset_on_canvas",
  unsatisfied_slots: ["video_unet", "audio_vae"],
};
export const TEMPLATE_DRIFT: FamilyOverride = {
  disposition: "template_drift",
  remediation: "requalify_template",
};
export const UNSUPPORTED_HOST: FamilyOverride = {
  disposition: "unsupported_host",
  remediation: "upgrade_host",
};

export function generationProfile(
  overrides?: BasisOverrides,
): GenerationProfile {
  return decodeGenerationProfile(generationProfileWire(overrides));
}

/** The loader App Mode is given when the host qualifies for every basis. */
export function loadAvailableProfile(): GenerationProfile {
  return generationProfile();
}
