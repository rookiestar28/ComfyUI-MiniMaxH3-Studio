/**
 * M17-26 — static reader for the locale-keyed copy tables that live in component
 * source rather than in `src/i18n/catalog.ts`.
 *
 * Why source parsing rather than importing the tables:
 *
 * 1. None of the four component tables is exported, so there is nothing to
 *    import. Exporting them would edit files other items are actively rewriting.
 * 2. A guard that depends on registration can be defeated by forgetting to
 *    register. Reading source lets `discoverLocaleTableFiles` rediscover tables
 *    independently, so an unregistered table is a test failure rather than a
 *    silent hole.
 *
 * The parser is deliberately narrow and fail-closed: it accepts string literals,
 * object literals, array literals and prettier's `+` string continuations, and
 * throws on anything else. A copy table is data; if one grows an expression the
 * guard stops rather than guessing.
 */

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * The frontend package root, resolved from this module rather than from
 * `process.cwd()`. Sibling static tests use `process.cwd()`, which is correct
 * only when the runner is started inside `frontend/`; resolving upward keeps
 * the guard working whichever directory invokes it, so a wrong cwd cannot
 * present itself as a missing copy table.
 */
const ROOT = ((): string => {
  let current = dirname(fileURLToPath(import.meta.url));
  for (;;) {
    if (existsSync(join(current, "package.json"))) return current;
    const parent = resolve(current, "..");
    if (parent === current)
      throw new Error("cannot locate the frontend package root");
    current = parent;
  }
})();

/** A locale-keyed table that cannot be imported and must be read from source. */
export type CopyTable = Readonly<{
  /** Path relative to the frontend package root. */
  file: string;
  /** The `const` identifier bound to the locale-keyed object literal. */
  identifier: string;
}>;

export const UNEXPORTED_COPY_TABLES: readonly CopyTable[] = [
  { file: "src/components/nle/nleCopy.ts", identifier: "copy" },
  { file: "src/components/nle/NleInspectorTabs.tsx", identifier: "COPY" },
  { file: "src/components/authoringOutputCopy.ts", identifier: "outputCopy" },
  {
    file: "src/components/AuthoringPreviewMonitor.tsx",
    identifier: "copy",
  },
  { file: "src/components/MediaToolsCard.tsx", identifier: "copy" },
  { file: "src/components/ProductionWorkbench.tsx", identifier: "copy" },
  { file: "src/components/ProjectFileControls.tsx", identifier: "COPY" },
  { file: "src/components/ProjectRecoveryControls.tsx", identifier: "COPY" },
  { file: "src/components/RetainedAssetsSection.tsx", identifier: "COPY" },
  { file: "src/components/SettingsPage.tsx", identifier: "copy" },
  {
    file: "src/components/TransactionTransparencyPanel.tsx",
    identifier: "copy",
  },
  { file: "src/components/WorkspaceStateSection.tsx", identifier: "COPY" },
  { file: "src/components/productionStateLabels.ts", identifier: "LABELS" },
];

/**
 * `src/i18n/catalog.ts` is a locale table too, but it is assembled from
 * references rather than literals and it is already validated at module load by
 * `validateCatalog`. It is excluded from source parsing and asserted at runtime.
 */
export const RUNTIME_VALIDATED_TABLE = "src/i18n/catalog.ts";

export const readSource = (file: string): string =>
  readFileSync(join(ROOT, file), "utf8");

/**
 * A locale key that opens an object literal. This distinguishes a copy table
 * from a file that merely mentions a locale value, such as `localeStore.ts`
 * (`return "zh-TW";`) or `languageSettings.ts` (a `Set` of preferences).
 */
const TABLE_SIGNATURE = /"zh-TW":\s*\{/;

/** Every file under `src/` that declares a locale-keyed object literal. */
export function discoverLocaleTableFiles(): readonly string[] {
  const found: string[] = [];
  const walk = (dir: string): void => {
    for (const name of readdirSync(dir).sort()) {
      const abs = join(dir, name);
      if (statSync(abs).isDirectory()) {
        walk(abs);
        continue;
      }
      if (!/\.tsx?$/.test(name)) continue;
      if (TABLE_SIGNATURE.test(readFileSync(abs, "utf8")))
        found.push(relative(ROOT, abs).split(sep).join("/"));
    }
  };
  walk(join(ROOT, "src"));
  return found.sort();
}

export type CopyNode = string | { readonly [key: string]: CopyNode };

type Cursor = { i: number };

function skipTrivia(src: string, c: Cursor): void {
  for (;;) {
    while (c.i < src.length && /\s/.test(src[c.i] as string)) c.i += 1;
    if (src.startsWith("//", c.i)) {
      const end = src.indexOf("\n", c.i);
      c.i = end < 0 ? src.length : end;
      continue;
    }
    if (src.startsWith("/*", c.i)) {
      const end = src.indexOf("*/", c.i);
      if (end < 0)
        throw new Error("unterminated block comment in a copy table");
      c.i = end + 2;
      continue;
    }
    return;
  }
}

/**
 * Reads one string literal. Escapes are unwrapped to their following character
 * rather than decoded, which is sufficient here: this reader feeds key-parity
 * and non-empty checks, never a rendered comparison.
 */
function readStringLiteral(src: string, c: Cursor): string {
  const quote = src[c.i];
  if (quote !== '"' && quote !== "'" && quote !== "`")
    throw new Error(`expected a string literal at offset ${c.i}`);
  c.i += 1;
  let out = "";
  while (c.i < src.length) {
    const ch = src[c.i] as string;
    if (ch === "\\") {
      out += src[c.i + 1] ?? "";
      c.i += 2;
      continue;
    }
    if (ch === quote) {
      c.i += 1;
      return out;
    }
    if (quote === "`" && ch === "$" && src[c.i + 1] === "{")
      throw new Error(
        "a copy-table value must be a literal; template interpolation is not readable as data",
      );
    out += ch;
    c.i += 1;
  }
  throw new Error("unterminated string literal in a copy table");
}

function readKey(src: string, c: Cursor): string {
  const ch = src[c.i];
  if (ch === '"' || ch === "'") return readStringLiteral(src, c);
  const match = /^[A-Za-z_$][\w$]*/.exec(src.slice(c.i));
  if (!match) throw new Error(`expected an object key at offset ${c.i}`);
  c.i += match[0].length;
  return match[0];
}

function readValue(src: string, c: Cursor): CopyNode {
  skipTrivia(src, c);
  const ch = src[c.i];
  if (ch === "{") return readObject(src, c);
  if (ch === "[") return readArray(src, c);
  if (ch === '"' || ch === "'" || ch === "`") {
    let text = readStringLiteral(src, c);
    // Prettier splits a long string across lines with `+`; rejoin the parts so
    // the value is compared as the single string the product renders.
    for (;;) {
      const save = c.i;
      skipTrivia(src, c);
      if (src[c.i] !== "+") {
        c.i = save;
        return text;
      }
      c.i += 1;
      skipTrivia(src, c);
      text += readStringLiteral(src, c);
    }
  }
  throw new Error(
    `unsupported copy-table value at offset ${c.i}: ${JSON.stringify(
      src.slice(c.i, c.i + 48),
    )}`,
  );
}

function readObject(src: string, c: Cursor): { [key: string]: CopyNode } {
  if (src[c.i] !== "{") throw new Error(`expected '{' at offset ${c.i}`);
  c.i += 1;
  const out: { [key: string]: CopyNode } = {};
  for (;;) {
    skipTrivia(src, c);
    const ch = src[c.i];
    if (ch === undefined) throw new Error("unterminated object literal");
    if (ch === "}") {
      c.i += 1;
      return out;
    }
    if (ch === ",") {
      c.i += 1;
      continue;
    }
    const key = readKey(src, c);
    skipTrivia(src, c);
    if (src[c.i] !== ":")
      throw new Error(`expected ':' after copy-table key ${key}`);
    c.i += 1;
    out[key] = readValue(src, c);
  }
}

function readArray(src: string, c: Cursor): { [key: string]: CopyNode } {
  if (src[c.i] !== "[") throw new Error(`expected '[' at offset ${c.i}`);
  c.i += 1;
  const out: { [key: string]: CopyNode } = {};
  let index = 0;
  for (;;) {
    skipTrivia(src, c);
    const ch = src[c.i];
    if (ch === undefined) throw new Error("unterminated array literal");
    if (ch === "]") {
      c.i += 1;
      return out;
    }
    if (ch === ",") {
      c.i += 1;
      continue;
    }
    out[String(index)] = readValue(src, c);
    index += 1;
  }
}

/** Parses the object literal bound to `identifier` in `source`. */
export function parseCopyTable(
  source: string,
  identifier: string,
): { readonly [key: string]: CopyNode } {
  const declaration = new RegExp(
    `(?:^|\\n)(?:export\\s+)?const\\s+${identifier}\\b`,
    "m",
  );
  const match = declaration.exec(source);
  if (match === null) throw new Error(`no 'const ${identifier}' declaration`);
  // Skip any type annotation between the identifier and its initializer.
  const assign = source.indexOf("=", match.index + match[0].length);
  if (assign < 0) throw new Error(`'const ${identifier}' has no initializer`);
  const cursor: Cursor = { i: assign + 1 };
  const value = readValue(source, cursor);
  if (typeof value === "string")
    throw new Error(`'const ${identifier}' is a string, not a copy table`);
  return value;
}

export function readCopyTable(table: CopyTable): {
  readonly [key: string]: CopyNode;
} {
  try {
    return parseCopyTable(readSource(table.file), table.identifier);
  } catch (cause) {
    throw new Error(
      `${table.file}: cannot read copy table '${table.identifier}': ${
        (cause as Error).message
      }`,
    );
  }
}

/** Every leaf string in a table, as `dotted.path -> value`. */
export function leafEntries(
  node: CopyNode,
  prefix = "",
): ReadonlyArray<readonly [string, string]> {
  if (typeof node === "string") return [[prefix, node]];
  const out: Array<readonly [string, string]> = [];
  for (const [key, child] of Object.entries(node))
    out.push(...leafEntries(child, prefix === "" ? key : `${prefix}.${key}`));
  return out;
}

export type ParityIssue = Readonly<{
  locale: string;
  kind: "missing-locale" | "missing-key" | "extra-key" | "blank-value";
  detail: string;
}>;

/**
 * Compares one table against a closed locale set, in both directions, and
 * rejects blank values — the same contract `validateCatalog` applies to the
 * catalog, applied to a table that cannot be imported.
 */
export function localeParityIssues(
  table: { readonly [key: string]: CopyNode },
  locales: readonly string[],
): readonly ParityIssue[] {
  const issues: ParityIssue[] = [];
  const present = Object.keys(table);

  for (const locale of locales)
    if (!present.includes(locale))
      issues.push({
        locale,
        kind: "missing-locale",
        detail: `table has no '${locale}' block`,
      });
  for (const locale of present)
    if (!locales.includes(locale))
      issues.push({
        locale,
        kind: "missing-locale",
        detail: `table declares unsupported locale '${locale}'`,
      });
  if (issues.length > 0) return issues;

  const reference = locales[0] as string;
  const referenceKeys = new Set(
    leafEntries(table[reference] as CopyNode).map(([path]) => path),
  );

  for (const locale of locales) {
    const entries = leafEntries(table[locale] as CopyNode);
    const keys = new Set(entries.map(([path]) => path));
    for (const path of referenceKeys)
      if (!keys.has(path))
        issues.push({ locale, kind: "missing-key", detail: path });
    for (const path of keys)
      if (!referenceKeys.has(path))
        issues.push({ locale, kind: "extra-key", detail: path });
    for (const [path, value] of entries)
      if (value.trim().length === 0)
        issues.push({ locale, kind: "blank-value", detail: path });
  }
  return issues;
}
