import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";

import { genericFixtureRoot } from "../helpers/genericFixtureMedia";

type MeasurementCell = Readonly<{
  cell: Readonly<{ width: number; height: number; path: string }>;
  requestedPath: string;
  actualPath: string;
  firstPaintBackingWidth: number;
  firstPaintBackingHeight: number;
  backingWidth: number;
  backingHeight: number;
  deliveredWidth: number;
  deliveredHeight: number;
  backingStable: boolean;
  pictureWidth: number;
  pictureHeight: number;
  status: string;
  error: string | null;
}>;

test("a full-size product matrix cell is measured at the requested CSS and backing size", async ({
  page,
}) => {
  const root = genericFixtureRoot();
  await page.route("**/measurement-media/*", async (route) => {
    const name = new URL(route.request().url()).pathname.split("/").at(-1);
    if (name === "detail-720p.mp4") {
      await route.fulfill({
        body: readFileSync(
          join(
            root,
            "frontend/tests/fixtures/m25_20_semantic_media/vid-primary-hd.mp4",
          ),
        ),
        contentType: "video/mp4",
      });
      return;
    }
    if (name === "detail-720p.png") {
      await route.fulfill({
        body: readFileSync(
          join(
            root,
            "frontend/tests/fixtures/m25_20_semantic_media/img-overlay-hd.png",
          ),
        ),
        contentType: "image/png",
      });
      return;
    }
    await route.abort();
  });

  await page.goto("/previewPathMeasurement.html?willReadFrequently=1");
  await page.waitForFunction(
    () =>
      document.body.dataset.h3MeasurementReady === "true" ||
      document.body.dataset.h3MeasurementError !== undefined,
  );
  expect(
    await page.evaluate(() => document.body.dataset.h3MeasurementError ?? null),
  ).toBeNull();

  const result = await page.evaluate(async () => {
    const harness = (
      window as typeof window & {
        h3PreviewPathMeasurement: {
          run(request: unknown): Promise<{ cells: MeasurementCell[] }>;
        };
      }
    ).h3PreviewPathMeasurement;
    return await harness.run({
      cells: [
        {
          width: 640,
          height: 360,
          layers: 1,
          colorAdjust: false,
          path: "product",
          backingWidth: 640,
          backingHeight: 360,
        },
      ],
      warmup: 1,
      samples: 1,
    });
  });
  const cell = result.cells[0]!;
  expect(cell.status, cell.error ?? "measurement failed").toBe("ok");
  expect([cell.requestedPath, cell.actualPath]).toEqual(["product", "native"]);
  expect([cell.pictureWidth, cell.pictureHeight]).toEqual([640, 360]);
  expect([cell.firstPaintBackingWidth, cell.firstPaintBackingHeight]).toEqual([
    640, 360,
  ]);
  expect([cell.backingWidth, cell.backingHeight]).toEqual([640, 360]);
  expect([cell.deliveredWidth, cell.deliveredHeight]).toEqual([640, 360]);
  expect(cell.backingStable).toBe(true);
});
