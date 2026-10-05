import { diagnosticCopy } from "./catalog";
import type { SidebarMessage } from "../contracts/sidebarWorkspaceCodec";

/**
 * M21-03 AC-14 — compose a diagnostic sentence from its identity and its typed
 * parameters instead of rendering the backend's English `message`.
 *
 * The rules this module holds are all fail-visible in the same direction: when
 * the catalog cannot produce a complete sentence, the backend `message` is
 * rendered instead. That is never blank, because the contract requires a
 * non-empty `message` on every diagnostic, so an identity this build has never
 * seen degrades to English rather than to nothing.
 */

/** Parameters whose values are a closed backend vocabulary worth localising. */
const ENUMERATED_PARAMETERS = ["band"] as const;

const PLACEHOLDER = /\{([a-z][a-z0-9_]*)\}/g;

function template(locale: unknown, code: string): string | undefined {
  let value: unknown = diagnosticCopy(locale);
  for (const part of code.split(".")) {
    if (value === null || typeof value !== "object") return undefined;
    if (!Object.prototype.hasOwnProperty.call(value, part)) return undefined;
    value = (value as Record<string, unknown>)[part];
  }
  return typeof value === "string" ? value : undefined;
}

function enumerated(
  locale: unknown,
  name: string,
  value: string | number,
): string {
  if (typeof value !== "string") return String(value);
  const table = (
    diagnosticCopy(locale) as unknown as Record<
      string,
      Record<string, string> | undefined
    >
  )[name];
  return table?.[value] ?? value;
}

/** The localised severity word, or the backend token when it is unknown. */
export function severityLabel(locale: unknown, severity: string): string {
  const table: Record<string, string> = diagnosticCopy(locale).severity;
  return table[severity] ?? severity;
}

/**
 * The sentence to show for one diagnostic.
 *
 * Returns the backend `message` when the identity has no catalog entry, and
 * also when the entry names a parameter the payload did not carry — a
 * half-substituted sentence is worse than an English one.
 */
export function diagnosticSentence(
  locale: unknown,
  item: SidebarMessage,
): string {
  const pattern = template(locale, item.code);
  if (pattern === undefined) return item.message;
  const parameters = item.parameters ?? {};
  let complete = true;
  const composed = pattern.replace(PLACEHOLDER, (_match, name: string) => {
    if (!Object.prototype.hasOwnProperty.call(parameters, name)) {
      complete = false;
      return "";
    }
    const value = parameters[name];
    return (ENUMERATED_PARAMETERS as readonly string[]).includes(name)
      ? enumerated(locale, name, value)
      : String(value);
  });
  return complete ? composed : item.message;
}
