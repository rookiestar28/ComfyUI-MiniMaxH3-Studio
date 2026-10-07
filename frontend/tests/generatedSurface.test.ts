/**
 * M18-03: the browser half of the one structural authority for cross-language wires.
 *
 * Seventeen key sets used to be written out by hand in six codecs, next to the Python `to_wire`
 * that actually decides them. They now come from `generatedSurface.ts`, which is generated from
 * `comfyui_h3_context/contracts/cross_language_surface_v1.json`. Nothing about that arrangement is
 * safe unless the generated module and the committed record cannot disagree, so this suite reads
 * the record and asks the shipped module -- it does not restate either.
 *
 * The interesting failure is not "the numbers differ". It is a key set that drifts by one key in
 * the direction of permissiveness, because a closed-shape check built from it would then accept a
 * payload the producer never emits. So the comparison is exact and unordered-insensitive both ways.
 *
 * The suite also holds the line the item is about: a codec may refine a shape, but it may not
 * restate one. `duplicate_shapes` enforces that on the Python side; here we assert the shipped
 * codecs actually import what they use, so the duplicate cannot come back as a local literal.
 *
 * No fixture here carries a prompt, a media value, a private path or a credential.
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import * as generated from "../src/contracts/generatedSurface";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..");
const CODEC_DIR = join(REPO_ROOT, "frontend", "src", "contracts");

interface Shape {
  readonly export_name: string;
  readonly module_path: string;
  readonly class_name: string;
  readonly required_keys: readonly string[];
  readonly optional_keys?: readonly string[];
}

interface SurfaceDocument {
  readonly schema: string;
  readonly shapes: readonly Shape[];
  readonly fingerprint: string;
}

const surface = JSON.parse(
  readFileSync(
    join(
      REPO_ROOT,
      "comfyui_h3_context",
      "contracts",
      "cross_language_surface_v1.json",
    ),
    "utf8",
  ),
) as SurfaceDocument;

const exported = generated as unknown as Record<
  string,
  readonly string[] | undefined
>;

const allKeys = (shape: Shape): readonly string[] =>
  [...shape.required_keys, ...(shape.optional_keys ?? [])].sort();

describe("generated cross-language surface", () => {
  it("declares the schema and a fingerprint the record agrees on", () => {
    expect(surface.schema).toBe("h3-context-cross-language-surface/1");
    expect(surface.fingerprint).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(surface.shapes.length).toBeGreaterThan(0);
  });

  it("exports exactly the shapes the record declares, and nothing else", () => {
    const recorded = surface.shapes.map((shape) => shape.export_name).sort();
    const shipped = Object.keys(exported)
      .filter((name) => name.endsWith("Keys"))
      .sort();
    expect(shipped).toEqual(recorded);
    expect(new Set(shipped).size).toBe(shipped.length);
  });

  it.each(surface.shapes.map((shape) => [shape.export_name, shape] as const))(
    "%s matches its Python authority exactly",
    (name, shape) => {
      const shipped = exported[name];
      expect(shipped, `${name} is not exported`).toBeDefined();
      const keys = [...(shipped ?? [])].sort();
      expect(keys).toEqual(allKeys(shape));
      expect(keys.length).toBe(new Set(keys).size);
      expect(shape.module_path.startsWith("comfyui_h3_context/")).toBe(true);
    },
  );

  it("carries key names only -- no value, locator, credential or private path", () => {
    const text = readFileSync(join(CODEC_DIR, "generatedSurface.ts"), "utf8");
    for (const key of surface.shapes.flatMap(allKeys)) {
      expect(key).toMatch(/^[a-z][a-z0-9_]*$/);
    }
    expect(text).not.toMatch(/[A-Za-z]:\\|https?:\/\/|sk-|Bearer /);
  });

  it("is the only place the shipped codecs get an owned key set from", () => {
    const owned = new Map(
      surface.shapes.map(
        (shape) => [allKeys(shape).join(","), shape.export_name] as const,
      ),
    );
    const offenders: string[] = [];
    for (const file of readdirSync(CODEC_DIR).filter((name) =>
      name.endsWith(".ts"),
    )) {
      if (file === "generatedSurface.ts") {
        continue;
      }
      const text = readFileSync(join(CODEC_DIR, file), "utf8");
      // IMPORTANT: whitespace is read once, before each literal, never on both sides of it. With
      // `(?:\s*"k",?\s*)+` every run of whitespace between two literals can be split two ways, so
      // a long literal array that does not close (a comment inside it) backtracks exponentially:
      // the worker spins at full CPU, no test timeout ends a synchronous match, and the whole
      // suite hangs instead of failing. Both forms accept exactly the same text.
      for (const [, body] of text.matchAll(
        /\[((?:\s*"[a-z][a-z0-9_]*",?)+\s*)\]/g,
      )) {
        const literal = [...body.matchAll(/"([a-z][a-z0-9_]*)"/g)]
          .map((match) => match[1])
          .sort();
        const owner = owned.get(literal.join(","));
        if (owner !== undefined) {
          offenders.push(`${file} restates ${owner}`);
        }
      }
      for (const [, joined] of text.matchAll(
        /"([a-z][a-z0-9_]*(?:,[a-z][a-z0-9_]*)+)"/g,
      )) {
        const owner = owned.get(joined.split(",").sort().join(","));
        if (owner !== undefined) {
          offenders.push(`${file} inlines ${owner}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
