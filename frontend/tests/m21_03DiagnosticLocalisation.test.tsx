/**
 * M21-03 — diagnostics are composed here, not translated by the backend.
 *
 * `M21-01` shipped eleven prose-fidelity identities and `M24-05` now owns eleven
 * guide-readiness ones, plus four language identities, for twenty-six in total. Before localisation they reached the
 * sidebar as backend English rendered verbatim, which is the defect this file
 * guards against returning.
 *
 * The registry is read from the `M21-01` contract rather than restated here. A
 * hand-maintained list would pass forever after a twelfth identity was added,
 * which is the exact failure AC-M21-03-15 exists to prevent.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  SidebarStages,
  initialSidebarStagesDraft,
} from "../src/components/SidebarStages";
import type { SidebarMessage } from "../src/contracts/sidebarWorkspaceCodec";
import { decodeSidebarWorkspaceProjection } from "../src/contracts/sidebarWorkspaceCodec";
import { SUPPORTED_LOCALES, diagnosticCopy } from "../src/i18n/catalog";
import type { Locale } from "../src/i18n/catalog";
import { diagnosticSentence, severityLabel } from "../src/i18n/diagnostics";

import { validSidebarWorkspace } from "./sidebarWorkspaceFixture";
import {
  CONTEXT_REPOSITORY_MARKER,
  findContextRepositoryRoot,
} from "./support/findContextRepositoryRoot";

const REPO_ROOT = findContextRepositoryRoot(
  dirname(fileURLToPath(import.meta.url)),
);

const REGISTRY: readonly string[] = ((): readonly string[] => {
  const schema = JSON.parse(
    readFileSync(join(REPO_ROOT, CONTEXT_REPOSITORY_MARKER), "utf8"),
  ) as {
    $defs: {
      diagnostic: { properties: { diagnostic_id: { enum: string[] } } };
    };
  };
  return schema.$defs.diagnostic.properties.diagnostic_id.enum;
})();

/**
 * The identities whose sentence is deliberately content-free: they state a fact
 * about the typed plan and carry none of the user's words, so their templates
 * interpolate nothing. This list is pinned, not derived, so that admitting a
 * further identity to it is a decision somebody makes on purpose.
 */
const CONTENT_FREE: readonly string[] = ["fidelity.soundscape.unspecified"];

/** The parameter names one identity's template asks for, in one locale. */
function placeholders(locale: Locale, code: string): readonly string[] {
  let value: unknown = diagnosticCopy(locale);
  for (const part of code.split(".")) {
    if (value === null || typeof value !== "object") return [];
    value = (value as Record<string, unknown>)[part];
  }
  if (typeof value !== "string") return [];
  return [...value.matchAll(/\{([a-z][a-z0-9_]*)\}/g)].map((match) => match[1]);
}

/** A payload that satisfies every placeholder the English template names. */
function completeParameters(code: string): Record<string, string | number> {
  const result: Record<string, string | number> = {};
  for (const name of placeholders("en", code))
    result[name] =
      name === "band"
        ? "below_target"
        : name.endsWith("_index")
          ? 2
          : name === "characters"
            ? 321
            : `value-${name}`;
  return result;
}

const FALLBACK = "backend english fallback";

const message = (
  code: string,
  parameters?: Record<string, string | number>,
): SidebarMessage => ({
  code,
  severity: "warning",
  message: FALLBACK,
  ...(parameters === undefined ? {} : { parameters }),
});

afterEach(cleanup);

describe("M21-03 diagnostic registry parity", () => {
  it("reads a non-empty closed registry from the M21-01 contract", () => {
    expect(REGISTRY.length).toBe(27);
    expect(new Set(REGISTRY).size).toBe(REGISTRY.length);
    expect(REGISTRY.every((code) => code.startsWith("fidelity."))).toBe(true);
  });

  it.each(SUPPORTED_LOCALES)(
    "gives every registry identity a non-empty entry in %s",
    (locale) => {
      for (const code of REGISTRY) {
        const sentence = diagnosticSentence(
          locale,
          message(code, completeParameters(code)),
        );
        expect(sentence.trim().length, code).toBeGreaterThan(0);
        expect(sentence, code).not.toBe(FALLBACK);
      }
    },
  );

  it("asks for the same parameters in every locale", () => {
    // A translated template that dropped a placeholder would render a sentence
    // missing the value that makes it actionable, and no other row would see it.
    //
    // GUARD: naming no parameter is legitimate for exactly the identities in
    // CONTENT_FREE and for nobody else, so the exemption is an enumerated
    // allowlist rather than a lowered bar. M24-05's readiness reasons state a
    // fact about the typed plan and carry none of the user's words, so their
    // templates interpolate nothing; every other identity must still name at
    // least one parameter in English and the same set in all three locales.
    //
    // Relaxing this to "some identity somewhere has a placeholder" is the
    // specific mistake to avoid: a parameterised identity that lost its
    // placeholder from all three catalogs at once would satisfy both the parity
    // comparison, which sees three empty sets agreeing, and a registry-wide
    // count, which other identities keep satisfying. A new content-free
    // identity must be added to CONTENT_FREE deliberately, which is the point.
    for (const code of CONTENT_FREE) expect(REGISTRY).toContain(code);
    for (const code of REGISTRY) {
      const english = [...placeholders("en", code)].sort();
      if (CONTENT_FREE.includes(code)) expect(english, code).toEqual([]);
      else expect(english.length, code).toBeGreaterThan(0);
      for (const locale of SUPPORTED_LOCALES)
        expect(
          [...placeholders(locale, code)].sort(),
          `${locale} ${code}`,
        ).toEqual(english);
    }
  });

  it("fails when an entry is removed", () => {
    // Non-vacuity: the guard must be able to fail. A code the catalog has never
    // heard of is exactly the state a removed entry leaves behind.
    expect(
      diagnosticSentence("en", message("fidelity.camera.removed_entry", {})),
    ).toBe(FALLBACK);
  });
});

describe("M21-03 diagnostic composition", () => {
  it("places every typed parameter into the localised sentence", () => {
    for (const code of REGISTRY) {
      const parameters = completeParameters(code);
      for (const locale of SUPPORTED_LOCALES) {
        const sentence = diagnosticSentence(locale, message(code, parameters));
        expect(sentence, `${locale} ${code}`).not.toContain("{");
        for (const [name, value] of Object.entries(parameters)) {
          // `band` is a closed backend vocabulary and is localised, not shown.
          if (name === "band") continue;
          expect(sentence, `${locale} ${code}.${name}`).toContain(
            String(value),
          );
        }
      }
    }
  });

  it("localises an enumerated parameter value rather than showing its token", () => {
    const code = "fidelity.description.length_band";
    const item = message(code, {
      band: "below_target",
      characters: 321,
      words: 42,
    });
    expect(diagnosticSentence("en", item)).toContain("below the usual target");
    expect(diagnosticSentence("zh-TW", item)).toContain("低於常用目標長度");
    expect(diagnosticSentence("zh-CN", item)).toContain("低于常用目标长度");
    for (const locale of SUPPORTED_LOCALES)
      expect(diagnosticSentence(locale, item)).not.toContain("below_target");
  });

  it("falls back to the backend message, never to blank", () => {
    // An identity this build has never seen, and an identity whose payload is
    // missing a value its template names: both degrade to English rather than
    // to a half-substituted sentence or an empty line.
    const unknown = message("fidelity.future.identity");
    const partial = message("fidelity.shot_timestamp.malformed", {
      shot_index: 2,
    });
    for (const locale of SUPPORTED_LOCALES) {
      expect(diagnosticSentence(locale, unknown)).toBe(FALLBACK);
      expect(diagnosticSentence(locale, partial)).toBe(FALLBACK);
    }
  });

  it("localises severity and keeps an unknown severity visible", () => {
    expect(severityLabel("en", "warning")).toBe("Warning");
    expect(severityLabel("zh-TW", "warning")).toBe("警告");
    expect(severityLabel("zh-CN", "error")).toBe("错误");
    expect(severityLabel("zh-TW", "unheard_of")).toBe("unheard_of");
  });
});

describe("M21-03 the audit stage renders the composed sentence", () => {
  const stage = (diagnostics: readonly unknown[], locale: Locale) => {
    const projection = decodeSidebarWorkspaceProjection({
      ...validSidebarWorkspace,
      diagnostics,
    });
    return render(
      <SidebarStages
        projection={projection}
        locale={locale}
        draft={{
          ...initialSidebarStagesDraft(projection),
          activeStage: "audit",
        }}
        busy={false}
        onDraftChange={vi.fn()}
        onAction={vi.fn()}
        onClientFailure={vi.fn()}
      />,
    );
  };

  it("shows the localised sentence and severity, not the backend English", () => {
    stage(
      [
        {
          code: "fidelity.camera.unrequested_motion",
          severity: "warning",
          message: "the prompt adds camera motion the request did not ask for",
          parameters: { term: "pan right" },
        },
      ],
      "zh-TW",
    );
    const entry = document.querySelector(
      '[data-code="fidelity.camera.unrequested_motion"]',
    );
    expect(entry).not.toBeNull();
    expect(entry?.textContent).toContain("警告");
    expect(entry?.textContent).toContain("pan right");
    expect(entry?.textContent).not.toContain(
      "the prompt adds camera motion the request did not ask for",
    );
  });

  it("renders a non-fidelity diagnostic exactly as the backend wrote it", () => {
    // The catalog owns the fidelity family only. Widening it silently would put
    // this surface back in the business of restating backend prose.
    stage(
      [
        {
          code: "prompt.section_body_missing",
          severity: "error",
          message: "a required section body is missing",
        },
      ],
      "zh-TW",
    );
    expect(screen.getByText(/a required section body is missing/)).toBeTruthy();
  });
});
