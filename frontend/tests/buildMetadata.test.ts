import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  H3_CONTEXT_BUILD_METADATA,
  H3_CONTEXT_DISPLAY_VERSION,
  H3_CONTEXT_VERSION,
} from "../src/buildMetadata";
import { resolveH3ContextBuildMetadata } from "../buildMetadata";

const repositoryUrl =
  "https://github.com/rookiestar28/ComfyUI-MiniMaxH3-Studio";

async function writeAuthorityFixture(
  root: string,
  versions: {
    project: string;
    package: string;
    python: string;
    installation: string;
  },
): Promise<void> {
  await mkdir(join(root, "frontend"), { recursive: true });
  await mkdir(join(root, "comfyui_h3_context", "contracts"), {
    recursive: true,
  });
  await writeFile(
    join(root, "pyproject.toml"),
    `[project]\nname = "minimax-h3-studio"\nversion = "${versions.project}"\n`,
  );
  await writeFile(
    join(root, "frontend", "package.json"),
    JSON.stringify({
      name: "@comfyui-h3-context/frontend",
      version: versions.package,
    }),
  );
  await writeFile(
    join(root, "comfyui_h3_context", "__init__.py"),
    `__version__ = "${versions.python}"\n`,
  );
  await writeFile(
    join(
      root,
      "comfyui_h3_context",
      "contracts",
      "installation_profiles_v1.json",
    ),
    JSON.stringify({
      profiles: [
        {
          profile_id: "core_manual",
          distribution: `minimax-h3-studio==${versions.installation}`,
        },
      ],
    }),
  );
}

describe("H3 build metadata", () => {
  it("exposes normalized installed-version metadata and the public repository URL", () => {
    expect(H3_CONTEXT_BUILD_METADATA).toEqual({
      version: H3_CONTEXT_VERSION,
      displayVersion: H3_CONTEXT_DISPLAY_VERSION,
      repositoryUrl,
    });
    expect(H3_CONTEXT_VERSION).toMatch(/^\d+\.\d+\.\d+$/);
    expect(H3_CONTEXT_DISPLAY_VERSION).toBe(`v${H3_CONTEXT_VERSION}`);
  });

  it("joins all package authorities without runtime fallback", () => {
    expect(resolveH3ContextBuildMetadata()).toEqual(H3_CONTEXT_BUILD_METADATA);
  });

  it("fails closed when an authority disagrees", async () => {
    const root = await mkdtemp(join(tmpdir(), "h3-m15-11-metadata-"));
    try {
      await writeAuthorityFixture(root, {
        project: "0.1.0",
        package: "0.1.0",
        python: "0.1.0",
        installation: "0.1.1",
      });
      expect(() => resolveH3ContextBuildMetadata(root)).toThrow(
        /version authorities disagree/i,
      );
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  it("fails closed when an authority is missing", async () => {
    const root = await mkdtemp(join(tmpdir(), "h3-m15-11-metadata-"));
    try {
      await writeAuthorityFixture(root, {
        project: "0.1.0",
        package: "0.1.0",
        python: "0.1.0",
        installation: "0.1.0",
      });
      await rm(join(root, "frontend", "package.json"));
      expect(() => resolveH3ContextBuildMetadata(root)).toThrow(
        /cannot read frontend package metadata/i,
      );
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
});
