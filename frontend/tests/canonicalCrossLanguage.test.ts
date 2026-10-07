/**
 * M18-02: the browser half of the one cross-language identity in this repository.
 *
 * `sidebarHost.ts` fingerprints the workspace prompt here and compares the result against the
 * backend-produced value, so the two canonicalizers must agree or that comparison is meaningless.
 * This suite reads the same committed vectors the Python suite reads
 * (`tests/fixtures/canonical_cross_language_vectors.json`), so the runtimes are pinned to one
 * artifact rather than to each other's prose.
 *
 * The fixture records only what Python measured. Nothing here restates the browser guard: the
 * shipped module is imported and asked. A planning measurement that reimplemented the guard as
 * `Array.from(s).some(ch => ch.charCodeAt(0) ...)` reported a divergence that did not exist --
 * `Array.from` iterates code points while `charCodeAt(0)` returns the leading surrogate -- and that
 * is the mistake this file is shaped to make impossible.
 */
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { canonicalStringFingerprint } from "../src/contracts/canonicalFingerprint";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..");

interface Segment {
  readonly code_points: readonly number[];
  readonly repeat?: number;
}

interface Vector {
  readonly name: string;
  readonly description: string;
  readonly segments: readonly Segment[];
  readonly python_accepted: boolean;
  readonly python_fingerprint?: string;
}

interface VectorDocument {
  readonly schema: string;
  readonly canonical_string_limit: number;
  readonly vectors: readonly Vector[];
}

const document = JSON.parse(
  readFileSync(
    join(
      REPO_ROOT,
      "tests",
      "fixtures",
      "canonical_cross_language_vectors.json",
    ),
    "utf8",
  ),
) as VectorDocument;

/**
 * Rebuild a vector from its code points, exactly as the Python suite does.
 *
 * `String.fromCodePoint` is the counterpart of Python's `chr`. It is spread over one segment at a
 * time because a limit vector expands to tens of thousands of characters and a single spread of
 * that size overflows the call stack.
 */
const vectorText = (vector: Vector): string =>
  vector.segments
    .map((segment) =>
      String.fromCodePoint(...segment.code_points).repeat(segment.repeat ?? 1),
    )
    .join("");

const browserFingerprint = (text: string): string | null => {
  try {
    return canonicalStringFingerprint(text);
  } catch {
    return null;
  }
};

describe("canonical string identity across Python and the browser", () => {
  it("reads the committed vectors the Python suite reads", () => {
    expect(document.schema).toBe(
      "h3-context-canonical-cross-language-vectors/1",
    );
    expect(document.canonical_string_limit).toBe(65_536);
    expect(document.vectors.length).toBeGreaterThan(40);
  });

  it("accepts exactly the vectors Python accepts", () => {
    const split = document.vectors
      .filter(
        (vector) =>
          (browserFingerprint(vectorText(vector)) !== null) !==
          vector.python_accepted,
      )
      .map((vector) => vector.name);
    expect(split).toEqual([]);
  });

  it("produces the same digest as Python wherever both accept", () => {
    const diverged = document.vectors
      .filter((vector) => vector.python_accepted)
      .filter(
        (vector) =>
          browserFingerprint(vectorText(vector)) !== vector.python_fingerprint,
      )
      .map((vector) => vector.name);
    expect(diverged).toEqual([]);
  });

  it("refuses a string past the limit in UTF-16 units but not in code points", () => {
    // The M18-02 split, from the browser's side: this was always refused here, and Python used to
    // accept it and hand sidebarHost.ts a fingerprint this runtime could not reproduce.
    const astral = String.fromCodePoint(0x1f600).repeat(65_536 / 2 + 1);
    expect(astral.length).toBeGreaterThan(65_536);
    expect(Array.from(astral).length).toBeLessThanOrEqual(65_536);
    expect(browserFingerprint(astral)).toBeNull();

    const atLimit = String.fromCodePoint(0x1f600).repeat(65_536 / 2);
    expect(atLimit.length).toBe(65_536);
    expect(browserFingerprint(atLimit)).not.toBeNull();
  });

  it("keeps the identity the suite pinned before this item", () => {
    expect(canonicalStringFingerprint("hello")).toBe(
      "sha256:5aa762ae383fbb727af3c7a36d4940a5b8c40a989452d2304fc958ff3f354e7a",
    );
  });
});
