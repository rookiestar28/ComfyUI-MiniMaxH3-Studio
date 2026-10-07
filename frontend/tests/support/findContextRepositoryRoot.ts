import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

export const CONTEXT_REPOSITORY_MARKER = join(
  "governance",
  "contracts",
  "prompt_fidelity_v2.schema.json",
);

export function findContextRepositoryRoot(startDirectory: string): string {
  let current = resolve(startDirectory);
  for (;;) {
    if (existsSync(join(current, CONTEXT_REPOSITORY_MARKER))) return current;
    const parent = dirname(current);
    if (parent === current)
      throw new Error("cannot locate the repository root");
    current = parent;
  }
}
