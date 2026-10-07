import { describe, expect, it } from "vitest";

import type { AppModeRefusalReason } from "../src/host/appMode";
import {
  SUPPORTED_LOCALES,
  appModeRefusalCopy,
  sidebarCopy,
} from "../src/i18n/catalog";

describe("M23-22 typed refusal copy", () => {
  const reasons: readonly AppModeRefusalReason[] = [
    {
      kind: "anchor_missing",
      requiredNode: "MiniMaxH3ImageToVideo",
    },
    {
      kind: "generation_admission_refused",
      admissionReason: "missing_asset",
      unsatisfiedSlots: ["video_unet", "audio_vae"],
    },
    { kind: "connect_missing_first_frame" },
    { kind: "connect_missing_last_frame" },
    { kind: "source_image_changed" },
    { kind: "template_unavailable" },
    {
      kind: "queue_rejected",
      classTypes: ["CLIPLoader"],
      errorTypes: ["value_not_in_list"],
    },
    { kind: "queue_response_invalid" },
  ];

  it.each(SUPPORTED_LOCALES)(
    "maps every reason to distinct catalog-only %s copy",
    (locale) => {
      const copy = sidebarCopy(locale);
      const sentences = reasons.map((reason) =>
        appModeRefusalCopy(locale, reason),
      );

      expect(sentences.every((sentence) => sentence.trim().length > 0)).toBe(
        true,
      );
      expect(new Set(sentences)).toHaveLength(reasons.length);
      expect(sentences).not.toContain(copy.incompatible);
      expect(sentences).not.toContain(copy.errorMessages.incompatible_seam);
      expect(sentences.join(" ")).not.toMatch(/\{(?:node|roles|detail)\}/);
    },
  );

  it.each(SUPPORTED_LOCALES)(
    "renders only bounded safe queue rejection vocabulary in %s",
    (locale) => {
      const rendered = appModeRefusalCopy(locale, {
        kind: "queue_rejected",
        classTypes: [
          "CLIPLoader",
          "Value not in list: C:\\private\\model.safetensors",
        ],
        errorTypes: ["prompt_outputs_failed_validation", "private message"],
      });

      expect(rendered).toContain("CLIPLoader");
      expect(rendered).toContain("prompt_outputs_failed_validation");
      expect(rendered).not.toContain("private");
      expect(rendered).not.toContain("model.safetensors");
      expect(rendered).not.toContain("Value not in list");
      expect(rendered).not.toMatch(/\{(?:detail|values)\}/);

      const generic = appModeRefusalCopy(locale, {
        kind: "queue_rejected",
        classTypes: ["C:\\private\\model.safetensors"],
        errorTypes: ["private message"],
      });
      expect(generic.trim().length).toBeGreaterThan(0);
      expect(generic).not.toContain("private");
    },
  );
});
