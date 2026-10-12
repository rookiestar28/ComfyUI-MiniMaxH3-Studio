import { expect, test } from "@playwright/test";
import { canonicalWorkspace, snapshot } from "../helpers/nleCanonical";
import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

test("Text inspector comparable visual checkpoint", async ({
  page,
}, testInfo) => {
  await routeGenericFixtureMedia(page);
  await canonicalWorkspace(page, undefined, undefined, "&media=1");
  const title = (await snapshot(page)).timelineSnapshot!.clips.find(
    (clip) => clip.text !== null,
  )!;
  await page
    .locator(
      `[data-h3-nle-clip="${title.clipId}"] [data-h3-nle-control="selection.set"]`,
    )
    .click();
  const inspector = page.locator('[data-h3-nle-area="inspector"]');
  await inspector.getByRole("tab", { name: "Text", exact: true }).click();
  await expect(inspector.getByLabel("Size (px)", { exact: true })).toHaveValue(
    String(title.text!.sizePx),
  );
  await inspector.screenshot({
    path: testInfo.outputPath("text-inspector.png"),
  });
});

test("decoration drafts survive reopen and complete edits, group reset and history preserve the other style", async ({
  page,
}, testInfo) => {
  await routeGenericFixtureMedia(page);
  const transactions = await canonicalWorkspace(
    page,
    (wire) => {
      const title = (
        wire.clips as Array<{ text: Record<string, unknown> | null }>
      ).find((clip) => clip.text !== null)!;
      Object.assign(title.text!, {
        stroke: { rgba: [0, 255, 0, 180], width_px: 2 },
        shadow: { rgba: [0, 0, 255, 128], offset_x_px: 0, offset_y_px: 0 },
        background_rgba: [255, 0, 0, 128],
        background_padding_px: 8,
      });
    },
    undefined,
    "&media=1&retention=1",
  );
  const before = (await snapshot(page)).timelineSnapshot!;
  const title = before.clips.find((clip) => clip.text !== null)!;
  await page
    .locator(
      `[data-h3-nle-clip="${title.clipId}"] [data-h3-nle-control="selection.set"]`,
    )
    .click();
  const inspector = page.locator('[data-h3-nle-area="inspector"]');
  await inspector.locator('[data-h3-nle-property-tab="text"]').click();
  const receiptCount = (await snapshot(page)).receipts;
  await inspector.getByLabel("Stroke width (px)", { exact: true }).fill("7");
  await page.locator('[data-h3-nle-action="close"]').click();
  await expect(page.locator('[data-h3-nle-surface="overlay_v1"]')).toHaveCount(
    0,
  );
  await page.locator('[data-h3-nle-entry="open"]').click();
  await expect(
    inspector.getByLabel("Stroke width (px)", { exact: true }),
  ).toHaveValue("7");
  expect((await snapshot(page)).receipts).toBe(receiptCount);
  await inspector.locator('[data-h3-nle-control="text.stroke"]').click();
  await expect
    .poll(async () => (await snapshot(page)).receipts)
    .toBe(receiptCount + 1);
  const changed = (await snapshot(page)).timelineSnapshot!.clips.find(
    (clip) => clip.clipId === title.clipId,
  )!;
  expect(changed).toEqual({
    ...title,
    text: { ...title.text!, stroke: { ...title.text!.stroke!, widthPx: 7 } },
  });
  expect(
    (transactions.at(-1) as { commands: unknown[] }).commands,
  ).toMatchObject([
    {
      kind: "set_text_style",
      payload: {
        style: {
          stroke: { rgba: [0, 255, 0, 180], width_px: 7 },
          shadow: { rgba: [0, 0, 255, 128], offset_x_px: 0, offset_y_px: 0 },
          background_padding_px: 8,
        },
      },
    },
  ]);
  await inspector
    .getByRole("button", { name: "Reset stroke", exact: true })
    .click();
  await expect
    .poll(async () => (await snapshot(page)).receipts)
    .toBe(receiptCount + 2);
  const reset = (await snapshot(page)).timelineSnapshot!.clips.find(
    (clip) => clip.clipId === title.clipId,
  )!;
  expect(reset.text!.stroke).toBeUndefined();
  expect(reset.text!.shadow).toEqual(title.text!.shadow);
  expect(reset.text!.backgroundPaddingPx).toBe(8);
  await page.locator('[data-h3-nle-control="history.undo"]').click();
  await expect
    .poll(async () => (await snapshot(page)).receipts)
    .toBe(receiptCount + 3);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.find(
      (clip) => clip.clipId === title.clipId,
    ),
  ).toEqual(changed);
  await page.locator('[data-h3-nle-control="history.redo"]').click();
  await expect
    .poll(async () => (await snapshot(page)).receipts)
    .toBe(receiptCount + 4);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.find(
      (clip) => clip.clipId === title.clipId,
    ),
  ).toEqual(reset);
  await inspector.screenshot({
    path: testInfo.outputPath("text-decorations-history.png"),
  });
});

for (const [locale, stroke, enable, width] of [
  ["en", "Stroke", "Enable stroke", "Stroke width (px)"],
  ["zh-TW", "描邊", "啟用描邊", "描邊寬度（px）"],
  ["zh-CN", "描边", "启用描边", "描边宽度（px）"],
] as const) {
  test(`localized decoration control synchronizes a real accepted style in ${locale}`, async ({
    page,
  }, testInfo) => {
    await routeGenericFixtureMedia(page);
    await canonicalWorkspace(
      page,
      undefined,
      undefined,
      `&media=1&locale=${locale}`,
    );
    const title = (await snapshot(page)).timelineSnapshot!.clips.find(
      (clip) => clip.text !== null,
    )!;
    await page
      .locator(
        `[data-h3-nle-clip="${title.clipId}"] [data-h3-nle-control="selection.set"]`,
      )
      .click();
    const inspector = page.locator('[data-h3-nle-area="inspector"]');
    await inspector.locator('[data-h3-nle-property-tab="text"]').click();
    await expect(
      inspector.getByRole("region", { name: stroke, exact: true }),
    ).toBeVisible();
    await inspector.getByLabel(enable, { exact: true }).check();
    await inspector.getByLabel(width, { exact: true }).fill("3");
    await inspector.getByLabel(width, { exact: true }).press("Enter");
    await expect
      .poll(
        async () =>
          (
            (await snapshot(page)).timelineSnapshot!.clips.find(
              (clip) => clip.clipId === title.clipId,
            )!.text as { stroke?: { widthPx: number } }
          ).stroke?.widthPx,
      )
      .toBe(3);
    await expect(inspector.getByLabel(width, { exact: true })).toHaveValue("3");
    await inspector.screenshot({
      path: testInfo.outputPath(`text-decorations-${locale}.png`),
    });
  });
}
