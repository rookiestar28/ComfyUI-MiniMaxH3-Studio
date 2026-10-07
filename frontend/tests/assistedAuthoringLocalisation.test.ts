import { describe, expect, it } from "vitest";

import {
  SUPPORTED_LOCALES,
  diagnosticCopy,
  providerCopy,
} from "../src/i18n/catalog";
import {
  promptFidelityDiagnosticIds,
  promptModelOutcomeIds,
} from "../src/i18n/generatedBackendIdentities";

function nested(value: unknown, identity: string): unknown {
  for (const part of identity.split(".")) {
    if (value === null || typeof value !== "object") return undefined;
    if (!Object.prototype.hasOwnProperty.call(value, part)) return undefined;
    value = (value as Record<string, unknown>)[part];
  }
  return value;
}

describe("assisted-authoring localization closure", () => {
  it("keeps the locale set closed to the three shipped catalogs", () => {
    expect(SUPPORTED_LOCALES).toEqual(["en", "zh-TW", "zh-CN"]);
  });

  it.each(SUPPORTED_LOCALES)(
    "%s resolves every backend diagnostic and outcome identity",
    (locale) => {
      for (const identity of promptFidelityDiagnosticIds) {
        const translated = nested(diagnosticCopy(locale), identity);
        expect(translated, identity).toEqual(expect.any(String));
        expect((translated as string).trim().length, identity).toBeGreaterThan(
          0,
        );
      }
      for (const identity of promptModelOutcomeIds) {
        const suffix = identity.replace(/^prompt_model\./, "");
        const translated =
          providerCopy(locale).outcome[
            suffix as keyof ReturnType<typeof providerCopy>["outcome"]
          ];
        expect(translated, identity).toEqual(expect.any(String));
        expect((translated as string).trim().length, identity).toBeGreaterThan(
          0,
        );
      }
    },
  );

  it("uses generated backend identities rather than a frontend-owned subset", () => {
    expect(new Set(promptFidelityDiagnosticIds).size).toBe(
      promptFidelityDiagnosticIds.length,
    );
    expect(new Set(promptModelOutcomeIds).size).toBe(
      promptModelOutcomeIds.length,
    );
    expect(promptFidelityDiagnosticIds.length).toBeGreaterThan(0);
    expect(promptModelOutcomeIds.length).toBeGreaterThan(0);
  });
});
