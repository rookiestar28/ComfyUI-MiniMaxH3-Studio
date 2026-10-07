/**
 * M17-26 — the declared exceptions for the rendered-output locale sweep.
 *
 * The sweep renders every registered surface in `en` and `zh-TW` and treats a
 * byte-identical string as suspicious, because a translated product should not
 * produce the same words twice. Two kinds of string legitimately survive that
 * comparison, and they are kept apart on purpose:
 *
 * - **Permanent identifiers** are identical by design — codes, contract ids,
 *   version strings, the product name, and a locale's own endonym. They are
 *   matched by shape wherever possible so a new code needs no new entry.
 * - **Temporary violations** are defects. Each names where it is, why it is
 *   wrong, and the item that will remove it. Their count is ratcheted: it may
 *   fall, never rise.
 *
 * Adding a temporary violation therefore requires editing `RATCHET_CEILING`,
 * which is a visible act in review rather than a quiet append.
 */

/** Shapes that are the same in every locale because they are not prose. */
export const PERMANENT_IDENTIFIER_RULES: ReadonlyArray<
  Readonly<{ name: string; test: RegExp }>
> = [
  // Numbers, separators, arrows and typographic placeholders such as the em
  // dash. A glyph-only control is correct when its accessible name is
  // localised, which is how the M17-23 window pagination buttons are built:
  // `←` is the visible content and `aria-label={text.pagePrevious}` carries the
  // name.
  {
    name: "numeric-or-punctuation",
    test: /^[\d\s.,:;/\\×·—–@+%()[\]{}<>|←→↑↓‹›«»…-]+$/u,
  },
  // Backend vocabulary rendered as itself: `dirty_upstream`, `ref2va`,
  // `prepared`, `h3_full_reference`. The established pattern in this product is
  // that the label is copy and the value is backend truth — for example
  // `<dt>{text.state}</dt><dd>{projection.transaction.state}</dd>` — so a bare
  // lowercase token beside a translated label is data, not untranslated copy.
  //
  // The boundary is deliberate and it has a cost: a genuinely untranslated
  // single lowercase English word would be admitted here. That is accepted
  // because UI copy in this product is capitalised or multi-word, while backend
  // codes are always lowercase; the alternative is a guard that flags every
  // rendered state code and is therefore switched off. Capitalised single words
  // such as `Cancel` are still flagged, which is asserted in the sweep tests.
  { name: "backend-code-token", test: /^[a-z][a-z0-9_]*$/ },
  // Product scope and similar constants: `MANUAL_ONLY_SCOPED`.
  { name: "screaming-snake-code", test: /^[A-Z0-9]+(?:_[A-Z0-9]+)+$/ },
  // Contract and authority identifiers: `h3.context.segment_artifact_receipt.v1`.
  { name: "dotted-contract-id", test: /^[a-z0-9]+(?:\.[a-z0-9_]+)+$/ },
  // Canonical task-mode tags shown as tags, not as words.
  { name: "task-mode-tag", test: /^(?:T2VA|I2VA|FL2VA|L2VA|Ref2VA)$/ },
  { name: "semantic-version", test: /^v\d+\.\d+\.\d+(?:[-+][\w.]+)?$/ },
  { name: "fingerprint", test: /^sha256:[0-9a-f]{6,}(?:…|\.\.\.)?$/ },
  // The canonical prompt mini-language is grammar, not copy.
  {
    name: "prompt-mini-language",
    test: /^(?:<(?:Picture|Video|Audio|Subject)\s+\d+>|\[Shot\s+\d+\]|\(S\d+\))$/,
  },
];

/** Exact strings that are identical in every locale by design. */
export const PERMANENT_IDENTIFIERS: ReadonlySet<string> = new Set([
  "MiniMax H3 Studio", // product name
  // A language option is written in its own language in every locale, which is
  // the correct behaviour and the reason these are not violations.
  "English",
  "繁體中文",
  "简体中文",
]);

export type TemporaryViolation = Readonly<{
  /** The exact rendered string, for a defect with no interpolated part. */
  text?: string;
  /**
   * A pattern, for a defect whose rendered form depends on data — an English
   * word interpolated with a revision number, for example. Anchored patterns
   * only; a loose pattern would swallow future defects.
   */
  pattern?: RegExp;
  /** Where it comes from, precise enough to fix without searching. */
  where: string;
  /** Why it is a defect rather than an identifier. */
  reason: string;
  /** The roadmap item that removes it. */
  closer: string;
}>;

export const TEMPORARY_VIOLATIONS: readonly TemporaryViolation[] = [];

/**
 * Frozen on 2026-08-18 against `27050d5`, the commit that accepted `M17-23`,
 * with the sweep at its full surface coverage, at `5`. Lowered to `0` by
 * `M21-03`, which translated all five declared violations: the seeded intent
 * value, `sidebar.viewOnGithub` in both Chinese locales, and the Production
 * predecessor label, revision accessible name and conflict notice.
 *
 * This number may only decrease. Raising it is how a new untranslated string
 * would enter the product, so it must be argued for in review. At `0` the
 * allowlist admits nothing: a new identical string is a failure, full stop.
 */
export const RATCHET_CEILING = 0;

export type PendingSurface = Readonly<{
  surface: string;
  reason: string;
  closer: string;
  /** Violations already known to live behind this gap. */
  knownViolations: readonly string[];
}>;

/**
 * Surfaces the sweep does not yet render. A coverage gap is a hole in the
 * guard, so it is declared and ratcheted exactly like a violation rather than
 * left implicit.
 */
export const PENDING_SURFACES: readonly PendingSurface[] = [];

/**
 * Frozen on 2026-08-18 at `1`. May only decrease.
 *
 * The Production ready/pending/conflict surfaces were a pending gap until
 * `M17-23` landed at `27050d5` and its projection shape settled; they are now
 * swept, which is what surfaced three of the five declared violations. The last
 * gap, the semantic proposal review, was registered as a swept surface by
 * `M21-03`, so the ceiling is `0` and every surface the product renders is
 * compared across locales.
 */
export const PENDING_SURFACE_CEILING = 0;

/**
 * A latent risk that is deliberately NOT a violation, recorded so it is not
 * rediscovered as one.
 *
 * `ReferenceTokenCombobox`'s `labels` prop used to carry English defaults that
 * its only call site always overrode, so they were unreachable, no test could
 * fail on them, and a future second call site would have shipped English
 * silently. `M21-03` made the prop required, which removes the shape that made
 * the risk possible rather than the string that expressed it. The list is empty
 * and stays empty: an entry here is a defect with no failing test.
 */
export const LATENT_UNREACHABLE_DEFAULTS: readonly string[] = [];

/** True when a string is identical across locales by design. */
export function isPermanentIdentifier(text: string): boolean {
  if (PERMANENT_IDENTIFIERS.has(text)) return true;
  return PERMANENT_IDENTIFIER_RULES.some((rule) => rule.test.test(text));
}

export function matchesViolation(
  entry: TemporaryViolation,
  text: string,
): boolean {
  if (entry.text !== undefined) return entry.text === text;
  if (entry.pattern !== undefined) return entry.pattern.test(text);
  return false;
}

export function isDeclaredTemporaryViolation(text: string): boolean {
  return TEMPORARY_VIOLATIONS.some((entry) => matchesViolation(entry, text));
}
