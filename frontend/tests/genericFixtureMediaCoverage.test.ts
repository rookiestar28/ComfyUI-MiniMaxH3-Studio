// D45-01 (plan section 11.2), registered as B-M2545-23. The generic `/nle-media/<member>` table
// and the workspace fixture are written in different files by different hands, and nothing tied
// them together: the fixture had placed an `img-still` clip on an overlay track since `57ea7cf`,
// and once `7ba938f` made every named asset a real request, the table's missing `image` member
// aborted it. That failure is silent at the route and surfaces as a disabled Play button in
// whichever shell journey runs next.
//
// This is the tie. Adding a kind to any fixture shape without serving it fails here, in the unit
// sweep, instead of hours later in a browser sweep that reports the row BLOCKED.
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, expect, test } from "vitest";

import { GENERIC_FIXTURE_MEDIA } from "./e2e/helpers/genericFixtureMedia";
import {
  NLE_STRESS_SHAPE,
  PORTRAIT_SHAPE,
  SMOKE_SHAPE,
  VIRTUALIZED_SHAPE,
  snapshotWire,
  snapshotFixture,
  type FixtureShape,
} from "./support/nleWorkspaceFixture";
import { resolveCompositionScene } from "../src/runtime/sceneResolver";

const SHAPES: ReadonlyArray<readonly [string, FixtureShape]> = [
  ["smoke", SMOKE_SHAPE],
  ["virtualized", VIRTUALIZED_SHAPE],
  ["portrait", PORTRAIT_SHAPE],
  ["stress", NLE_STRESS_SHAPE],
];

/**
 * The repository root the member paths are written against. The helper's own
 * `genericFixtureRoot()` cannot be reused here: it resolves `import.meta.url`, which is a real
 * file URL under the Playwright runner but an http URL once vite serves the module to vitest.
 */
const repositoryRoot = (): string => {
  let directory = process.cwd();
  while (!existsSync(join(directory, "pyproject.toml"))) {
    const parent = dirname(directory);
    if (parent === directory)
      throw new Error(`no repository root above ${process.cwd()}`);
    directory = parent;
  }
  return directory;
};

const kindsOf = (shape: FixtureShape): ReadonlySet<string> =>
  new Set(
    (snapshotWire(shape).assets as ReadonlyArray<{ kind: string }>).map(
      (asset) => asset.kind,
    ),
  );

describe("the generic fixture media table", () => {
  test("precise stress seeks resolve the real encoded source PTS at every tested frame", () => {
    const snapshot = snapshotFixture(NLE_STRESS_SHAPE);
    const primary = snapshot.tracks.find(
      ({ kind }) => kind === "primary_video",
    )!;
    const clip = snapshot.clips.find(
      (member) => member.trackId === primary.trackId && member.startFrame > 0,
    )!;
    const video = snapshot.assets.find(
      ({ assetId }) => assetId === clip.assetId,
    )!;
    expect(video.sourceFrameCount).toBe(48);
    expect(video.sourceSampleCount).toBe(96_000);
    for (const offset of [0, 1, 4, 11, 12, 23, 35, 47]) {
      const scene = resolveCompositionScene(snapshot, clip.startFrame + offset);
      expect(
        scene.layers.find((layer) => layer.clip_id === clip.clipId),
      ).toMatchObject({
        source_frame: offset,
        source_pts: offset * 512,
      });
    }
  });

  test("ordinary sparse geometry fixtures retain approximate landmark admission", () => {
    const snapshot = snapshotFixture(SMOKE_SHAPE);
    const video = snapshot.assets.find(({ kind }) => kind === "video")!;
    expect(video.landmarks.length).toBeLessThan(video.sourceFrameCount!);
    const scene = resolveCompositionScene(snapshot, 4);
    expect(
      scene.layers.find((layer) => layer.asset_id === video.assetId),
    ).toMatchObject({ source_pts: 0 });
  });
  test.each(SHAPES)(
    "serves every asset kind the %s shape emits",
    (_, shape) => {
      for (const kind of kindsOf(shape))
        expect(
          Object.keys(GENERIC_FIXTURE_MEDIA),
          `this shape emits a ${kind} asset, so the generic media table must serve it`,
        ).toContain(kind);
    },
  );

  test("serves an image, which is the member the regression removed", () => {
    expect(Object.keys(GENERIC_FIXTURE_MEDIA)).toContain("image");
  });

  test("declares a still whose real size matches the generic geometry the runtime assumes", () => {
    // `nleWorkspaceMedia.ts` declares 32 x 32 for any still it did not receive under the corpus
    // header, and images -- unlike video -- are not size-checked by `visualCompositionResources`.
    // A larger file here would therefore composite at four times its declared size in every
    // generic journey without anything failing, so the served bytes are checked against the
    // declared geometry directly.
    const image = GENERIC_FIXTURE_MEDIA.image!;
    expect(image.contentType).toBe("image/png");
    const png = readFileSync(join(repositoryRoot(), image.file));
    expect(png.subarray(0, 8)).toEqual(
      Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    );
    // IHDR width and height are the two big-endian 32-bit words at byte 16.
    expect(png.readUInt32BE(16)).toBe(32);
    expect(png.readUInt32BE(20)).toBe(32);
  });
});
