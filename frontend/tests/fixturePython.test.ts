import {
  mkdtempSync,
  mkdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { join, resolve } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { fixturePython } from "../e2e/fixturePython";

const roots: string[] = [];
function root() {
  const scratch = resolve(process.cwd(), "../.tmp");
  mkdirSync(scratch, { recursive: true });
  const value = mkdtempSync(join(scratch, "fixture-python-"));
  roots.push(value);
  return value;
}
function interpreter(base: string, name: string, platform: NodeJS.Platform) {
  const prefix = join(base, name);
  const file = join(
    prefix,
    platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  mkdirSync(join(prefix, platform === "win32" ? "Scripts" : "bin"), {
    recursive: true,
  });
  writeFileSync(file, "");
  writeFileSync(join(prefix, "pyvenv.cfg"), "home = base\n");
  return file;
}
afterEach(() => {
  for (const value of roots.splice(0))
    rmSync(value, { recursive: true, force: true });
});

describe("portable fixture Python", () => {
  it("rejects a linked environment even when the executable exists", () => {
    const base = root();
    const foreign = join(base, "foreign");
    mkdirSync(foreign);
    interpreter(foreign, ".venv", process.platform);
    symlinkSync(join(foreign, ".venv"), join(base, ".venv"), "junction");
    expect(() => fixturePython(base, {})).toThrow("project-local");
  });
  it.each(["win32", "linux"] as const)(
    "keeps the original .venv default on %s",
    (platform) => {
      const base = root();
      const selected = interpreter(base, ".venv", platform);
      expect(fixturePython(base, {}, platform)).toBe(selected);
    },
  );
  it("accepts an explicit Linux-local venv and rejects ambient/global paths", () => {
    const base = root();
    const selected = interpreter(base, ".venv-wsl", "linux");
    expect(
      fixturePython(base, { H3_CONTEXT_E2E_PYTHON: selected }, "linux"),
    ).toBe(selected);
    for (const value of [
      "python",
      "/usr/bin/python",
      join(base, "other/python"),
    ]) {
      expect(() =>
        fixturePython(base, { H3_CONTEXT_E2E_PYTHON: value }, "linux"),
      ).toThrow("project-local");
    }
    expect(() =>
      fixturePython(base, { H3_CONTEXT_E2E_PYTHON: selected }, "win32"),
    ).toThrow("project-local");
  });
  it("rejects a missing explicit environment before any fixture spawn", () => {
    const base = root();
    expect(() =>
      fixturePython(
        base,
        { H3_CONTEXT_E2E_PYTHON: join(base, ".venv/bin/python") },
        "linux",
      ),
    ).toThrow("project-local");
  });
});
