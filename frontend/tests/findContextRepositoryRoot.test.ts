import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  CONTEXT_REPOSITORY_MARKER,
  findContextRepositoryRoot,
} from "./support/findContextRepositoryRoot";

describe("findContextRepositoryRoot", () => {
  it("prefers the nested checkout contract over an ancestor AGENTS file", () => {
    const workspaceRoot = mkdtempSync(join(tmpdir(), "h3-context-root-"));
    const repositoryRoot = join(workspaceRoot, "nested-checkout");
    const contractPath = join(repositoryRoot, CONTEXT_REPOSITORY_MARKER);

    try {
      mkdirSync(join(repositoryRoot, "frontend", "tests"), {
        recursive: true,
      });
      mkdirSync(dirname(contractPath), { recursive: true });
      writeFileSync(contractPath, "{}");
      writeFileSync(join(workspaceRoot, "AGENTS.md"), "parent instructions");

      expect(
        findContextRepositoryRoot(join(repositoryRoot, "frontend", "tests")),
      ).toBe(repositoryRoot);
    } finally {
      rmSync(workspaceRoot, { force: true, recursive: true });
    }
  });
});
