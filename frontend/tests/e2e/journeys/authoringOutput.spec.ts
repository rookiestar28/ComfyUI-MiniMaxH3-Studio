import { test, expect, type Page } from "@playwright/test";
import { OUTPUT_CAPABILITY } from "../../../src/contracts/authoringOutputCodec";
import { outputFixtureBody } from "../../../e2e/authoringOutputServer";

// These content-free transport doubles qualify the browser leaf only, not a render receipt.
const body = Buffer.from(outputFixtureBody);
const binding = {
  workspace_handle: `authoring-${"a".repeat(32)}`,
  workspace_revision: 1,
  timeline_revision: 2,
  snapshot_fingerprint: `sha256:${"b".repeat(64)}`,
};
const ready = {
  ...binding,
  schema: "h3.authoring.output_status.v1",
  job_handle: `arj_${"c".repeat(22)}`,
  output_handle: `aro_${"d".repeat(22)}`,
  state_version: 7,
  phase: "succeeded",
  progress_bp: 10000,
  failure: null,
  currency: "current",
  availability: "available",
  output: {
    output_fingerprint: `sha256:${"e".repeat(64)}`,
    byte_length: body.length,
    width: 64,
    height: 64,
    frame_count: 24,
    frame_rate_num: 24,
    frame_rate_den: 1,
    audio_streams: 0,
    output_profile_id: OUTPUT_CAPABILITY.output_profile_id,
    verified: true,
  },
};
const headers = {
  "Cache-Control": "private, no-store",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
};

async function install(page: Page, pending = false) {
  const counts = {
    create: 0,
    cancel: 0,
    preview: 0,
    original: 0,
    originalFetch: 0,
  };
  let cancelled = false;
  await page.addInitScript(() => {
    const counts = { created: 0, revoked: 0 };
    Object.defineProperty(window, "outputBlobCounts", { value: counts });
    const create = URL.createObjectURL.bind(URL),
      revoke = URL.revokeObjectURL.bind(URL);
    URL.createObjectURL = (blob) => {
      counts.created++;
      return create(blob);
    };
    URL.revokeObjectURL = (url) => {
      counts.revoked++;
      revoke(url);
    };
  });
  await page.route("**/h3-context/v1/authoring/**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname;
    if (path.endsWith("/download") || path.endsWith("/preview")) {
      const preview = path.endsWith("/preview");
      if (preview) counts.preview++;
      else {
        counts.original++;
        if (["fetch", "xhr"].includes(request.resourceType()))
          counts.originalFetch++;
      }
      await route.fulfill({
        status: 200,
        headers: {
          ...headers,
          "Content-Type": "video/mp4",
          "Content-Length": String(body.length),
          "Accept-Ranges": "bytes",
          "Content-Disposition": `${preview ? "inline" : "attachment"}; filename="authoring-${preview ? "preview" : "final"}.mp4"`,
        },
        body,
      });
      return;
    }
    if (request.method() === "POST") {
      if (path.endsWith("/cancel")) {
        counts.cancel++;
        cancelled = true;
      } else counts.create++;
    }
    const wire = pending
      ? {
          ...ready,
          phase: cancelled ? "cancelled" : "queued",
          failure: cancelled ? "cancelled" : null,
          output_handle: null,
          output: null,
          progress_bp: 0,
          availability: "gone",
          state_version: cancelled ? 2 : 1,
        }
      : ready;
    const json = JSON.stringify(wire);
    await route.fulfill({
      status: 200,
      headers: {
        ...headers,
        "Content-Type": "application/json",
        "Content-Length": String(Buffer.byteLength(json)),
      },
      body: json,
    });
  });
  await page.goto("/authoringOutput.html");
  return counts;
}
async function blobCounts(page: Page) {
  return page.evaluate(
    () =>
      (
        window as unknown as {
          outputBlobCounts: { created: number; revoked: number };
        }
      ).outputBlobCounts,
  );
}

test("explicit native download and bounded preview lifecycle at responsive widths", async ({
  page,
}) => {
  const counts = await install(page);
  const initialDownloads = (
    await (await page.request.get("/__output_fixture/downloads")).json()
  ).downloads;
  await expect(page.getByRole("region", { name: "Final video" })).toHaveCount(
    0,
  );
  expect(counts.create).toBe(0);
  await page.getByRole("button", { name: "Toggle capability" }).click();
  const render = page.getByRole("button", { name: "Render final video" });
  await render.focus();
  await page.keyboard.press("Enter");
  const original = page.getByRole("link", { name: "Download original" });
  await expect(original).toBeVisible();
  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const region = page.getByRole("region", { name: "Final video" });
    expect(
      await region.evaluate((node) => node.scrollWidth <= node.clientWidth),
    ).toBe(true);
    await expect(original).toBeInViewport();
  }
  await page.getByRole("button", { name: "Edit fixture revision" }).click();
  await expect(page.getByText("Output from an earlier revision")).toBeVisible();
  await page.getByRole("button", { name: "Preview output" }).click();
  await expect(page.getByLabel("Final video preview")).toHaveAttribute(
    "src",
    /^blob:/,
  );
  expect(await blobCounts(page)).toEqual({ created: 1, revoked: 0 });
  await page.getByRole("button", { name: "Close preview" }).click();
  await expect(
    page.getByRole("button", { name: "Preview output" }),
  ).toBeFocused();
  expect(await blobCounts(page)).toEqual({ created: 1, revoked: 1 });
  const downloadEvent = page.waitForEvent("download");
  await original.click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("authoring-final.mp4");
  expect(await download.failure()).toBeNull();
  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  expect(Buffer.concat(chunks)).toEqual(body);
  expect(counts.originalFetch).toBe(0);
  expect(
    (await (await page.request.get("/__output_fixture/downloads")).json())
      .downloads - initialDownloads,
  ).toBe(1);
  await download.delete();
  await page.getByRole("button", { name: "Preview output" }).click();
  await expect(page.getByLabel("Final video preview")).toHaveAttribute(
    "src",
    /^blob:/,
  );
  await page.getByRole("button", { name: "Toggle leaf" }).click();
  await expect(page.getByRole("region", { name: "Final video" })).toHaveCount(
    0,
  );
  expect(await blobCounts(page)).toEqual({ created: 2, revoked: 2 });
  expect(counts.cancel).toBe(0);
  await page.getByRole("button", { name: "Toggle leaf" }).click();
  await expect(render).toBeEnabled();
  await expect(original).toHaveCount(0);
});

test("queued output requires explicit cancellation and exposes no artifact", async ({
  page,
}) => {
  const counts = await install(page, true);
  await page.getByRole("button", { name: "Toggle capability" }).click();
  await page.getByRole("button", { name: "Render final video" }).click();
  await expect(
    page.getByRole("progressbar", { name: "Queued" }),
  ).toHaveJSProperty("value", 0);
  await expect(
    page.getByRole("link", { name: "Download original" }),
  ).toHaveCount(0);
  expect(counts.cancel).toBe(0);
  await page.getByRole("button", { name: "Cancel render" }).click();
  await expect(
    page.getByText("Render cancelled", { exact: true }),
  ).toBeVisible();
  expect(counts.cancel).toBe(1);
  expect(counts.original + counts.preview).toBe(0);
});
