import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

describe("M15-13 legacy terminal surface guard", () => {
  it("does not ship the old terminal fallback bodies", () => {
    const source = [
      join(process.cwd(), "src/components/H3Sidebar.tsx"),
      join(process.cwd(), "src/entry.tsx"),
      join(process.cwd(), "src/host/sidebarHost.ts"),
    ]
      .map((path) => readFileSync(path, "utf8"))
      .join("\n");
    expect(source).not.toMatch(
      /node-only|Prompt export awaiting projection|Assisted reconstruction unavailable/i,
    );
  });
});
