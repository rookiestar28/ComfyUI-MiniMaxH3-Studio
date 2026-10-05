/**
 * M21-03 AC-09 — the one remediation mechanism.
 *
 * A remediation is declared against a backend-owned diagnostic *identity*, never
 * derived from its message. Message text is prose: it is localised, it changes
 * when wording improves, and matching on it would silently attach an action to
 * the wrong finding the first time a sentence was reworded.
 *
 * A remediation is also never invented. The closed `RemediationIntent` set below
 * contains only effects this build already performs, so a declared remediation
 * cannot promise something the product cannot do. An identity with no safe
 * effect is simply absent from the table, and the surface renders no action for
 * it -- which is the required behaviour, not a gap.
 *
 * `M22-06` adds provider-specific actions on top of this mechanism. There is
 * exactly one such mechanism in the frontend, and this is it.
 */

import type { TranslationKey } from "../i18n/catalog";

/** Every effect a declared remediation is allowed to have. */
export type RemediationIntent =
  /** Move to the Audit stage and put the caret in the prompt field. */
  "focus_prompt";

export type Remediation = Readonly<{
  intent: RemediationIntent;
  /** The catalog key of the action's label. Never a backend string. */
  label: TranslationKey;
}>;

/**
 * Diagnostic identity -> its single remediation.
 *
 * Every entry here is a prose-fidelity finding whose fix is an edit to the
 * prompt the user already owns, so the safe action is to take them to it. The
 * shot-timestamp family is deliberately absent: a malformed cut time is fixed
 * by re-rendering the document, not by hand-editing the prompt, and offering an
 * edit would invite a user to patch a symptom the renderer owns.
 */
const REMEDIATIONS: Readonly<Record<string, Remediation>> = {
  "fidelity.camera.unrequested_motion": {
    intent: "focus_prompt",
    label: "remediation.editPrompt",
  },
  "fidelity.camera.unrequested_cut": {
    intent: "focus_prompt",
    label: "remediation.editPrompt",
  },
  "fidelity.internal_vocabulary.present": {
    intent: "focus_prompt",
    label: "remediation.editPrompt",
  },
  "fidelity.constraint.excluded_source_trait": {
    intent: "focus_prompt",
    label: "remediation.editPrompt",
  },
  "fidelity.constraint.negated_technique_used": {
    intent: "focus_prompt",
    label: "remediation.editPrompt",
  },
};

/** The remediation declared for a diagnostic identity, or none. */
export function remediationFor(code: string): Remediation | undefined {
  return Object.prototype.hasOwnProperty.call(REMEDIATIONS, code)
    ? REMEDIATIONS[code]
    : undefined;
}

/** Every identity that declares one, for the parity guard. */
export const REMEDIATED_DIAGNOSTIC_IDS: readonly string[] = Object.freeze(
  Object.keys(REMEDIATIONS),
);
