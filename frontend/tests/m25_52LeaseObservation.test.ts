import { describe, expect, it } from "vitest";

import {
  describeM25LeaseUrl,
  observeM25LeaseOperation,
  matchesM25LeaseRoute,
  m25WorkspaceResourcesReleased,
} from "./e2e/support/host/m25_52LeaseObservation";

import fontManifest from "../../comfyui_h3_context/fonts/font_manifest_v1.json";

describe("workspace cleanup with process-owned packaged fonts", () => {
  const fonts = fontManifest.font_assets.flatMap(({ faces }) =>
    faces.map((face) => ({
      sha256: face.file_sha256,
      byteCount: face.size_bytes,
    })),
  );
  const empty = {
    leases: 0,
    cacheEntries: 0,
    cacheBytes: 0,
    activeReads: 0,
    decorationLeases: 0,
    videoLeases: 0,
  };
  const font = fonts[0];
  const idleFont = { ...empty, cacheEntries: 1, cacheBytes: font.byteCount };

  it("accepts complete cleanup with the verified installation-owned font still idle", () => {
    expect(m25WorkspaceResourcesReleased(idleFont, [font], fonts)).toBe(true);
  });

  it("accepts an empty authority without claiming a font was used", () => {
    expect(m25WorkspaceResourcesReleased(empty, [], fonts)).toBe(true);
  });

  it.each([
    "leases",
    "activeReads",
    "decorationLeases",
    "videoLeases",
  ] as const)(
    "rejects a remaining %s even when all cached bytes are a verified font",
    (field) => {
      expect(
        m25WorkspaceResourcesReleased(
          { ...idleFont, [field]: 1 },
          [font],
          fonts,
        ),
      ).toBe(false);
    },
  );

  it("rejects a font-shaped body whose digest is not in the candidate installation", () => {
    expect(
      m25WorkspaceResourcesReleased(
        idleFont,
        [{ ...font, sha256: `sha256:${"0".repeat(64)}` }],
        fonts,
      ),
    ).toBe(false);
  });

  it("rejects a known font digest with a different byte count", () => {
    expect(
      m25WorkspaceResourcesReleased(
        { ...idleFont, cacheBytes: font.byteCount + 1 },
        [{ ...font, byteCount: font.byteCount + 1 }],
        fonts,
      ),
    ).toBe(false);
  });

  it("rejects any extra workspace body or byte after the verified font is accounted for", () => {
    expect(
      m25WorkspaceResourcesReleased(
        { ...idleFont, cacheEntries: 2 },
        [font],
        fonts,
      ),
    ).toBe(false);
    expect(
      m25WorkspaceResourcesReleased(
        { ...idleFont, cacheBytes: font.byteCount + 1 },
        [font],
        fonts,
      ),
    ).toBe(false);
    expect(m25WorkspaceResourcesReleased(idleFont, [], fonts)).toBe(false);
  });

  it("rejects duplicate font exemptions and impossible aggregate observations", () => {
    expect(
      m25WorkspaceResourcesReleased(
        { ...empty, cacheEntries: 2, cacheBytes: font.byteCount * 2 },
        [font, font],
        fonts,
      ),
    ).toBe(false);
    expect(m25WorkspaceResourcesReleased(empty, [font], fonts)).toBe(false);
  });

  it("rejects invalid numeric observations rather than treating them as empty", () => {
    expect(
      m25WorkspaceResourcesReleased({ ...empty, cacheBytes: -1 }, [], fonts),
    ).toBe(false);
    expect(
      m25WorkspaceResourcesReleased({ ...empty, leases: NaN }, [], fonts),
    ).toBe(false);
    expect(
      m25WorkspaceResourcesReleased({ ...empty, activeReads: 0.5 }, [], fonts),
    ).toBe(false);
  });
});

describe("M25-52 supplied-host lease observation", () => {
  it("matches the exported create/open routes with optional API prefix and query only", () => {
    const origin = "http://127.0.0.1:8188";
    expect(
      matchesM25LeaseRoute(
        `${origin}/h3-context/v1/authoring/media-source-leases`,
      ),
    ).toBe(true);
    expect(
      matchesM25LeaseRoute(
        `${origin}/api/h3-context/v1/authoring/media-source-leases?row=m25-52`,
      ),
    ).toBe(true);
    expect(
      matchesM25LeaseRoute(
        `${origin}/h3-context/v1/authoring/media-source-leases/open`,
      ),
    ).toBe(true);
    expect(
      matchesM25LeaseRoute(
        `${origin}/api/h3-context/v1/authoring/media-source-leases/open?row=m25-52`,
      ),
    ).toBe(true);
    expect(
      matchesM25LeaseRoute(
        `${origin}/h3-context/v1/authoring/media-source-leases/open/extra`,
      ),
    ).toBe(false);
    expect(
      matchesM25LeaseRoute(`${origin}/h3-context/v1/authoring/media/lease`),
    ).toBe(false);
    expect(
      describeM25LeaseUrl(
        `${origin}/api/h3-context/v1/authoring/media-source-leases/open?row=m25-52`,
      ),
    ).toEqual({
      scheme: "http",
      hostname: "127.0.0.1",
      port: "8188",
      pathAndQuery:
        "/api/h3-context/v1/authoring/media-source-leases/open?row=m25-52",
    });
  });

  it("carries create owner kind authority through open and release", () => {
    const owners = new Map<string, string>();
    const route =
      "http://127.0.0.1:8188/api/h3-context/v1/authoring/media-source-leases";
    expect(
      observeM25LeaseOperation(
        route,
        {
          operation: "create",
          ownerId: "nle-waveform-7-2",
          derivativeKind: "audio_peaks",
        },
        owners,
      ),
    ).toEqual({
      operation: "create",
      ownerId: "nle-waveform-7-2",
      kind: "audio_peaks",
    });
    expect(
      observeM25LeaseOperation(
        `${route}/open`,
        { operation: "open", ownerId: "nle-waveform-7-2" },
        owners,
      ),
    ).toEqual({
      operation: "open",
      ownerId: "nle-waveform-7-2",
      kind: "audio_peaks",
    });
    expect(
      observeM25LeaseOperation(
        route,
        { operation: "release", ownerId: "nle-waveform-7-2" },
        owners,
      ),
    ).toEqual({
      operation: "release",
      ownerId: "nle-waveform-7-2",
      kind: "audio_peaks",
    });
    expect(owners.has("nle-waveform-7-2")).toBe(false);
    expect(
      observeM25LeaseOperation(
        `${route}/open`,
        { operation: "open", ownerId: "nle-waveform-7-2" },
        owners,
      ),
    ).toEqual({
      operation: "open",
      ownerId: "nle-waveform-7-2",
      kind: "control",
    });
    expect(
      observeM25LeaseOperation(
        "http://127.0.0.1:8188/h3-context/v1/authoring/action",
        {
          operation: "create",
          ownerId: "unrelated",
          derivativeKind: "filmstrip",
        },
        owners,
      ),
    ).toBeNull();
    expect(owners.has("unrelated")).toBe(false);
  });
});
