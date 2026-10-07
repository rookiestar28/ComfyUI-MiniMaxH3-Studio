import { createHash } from "node:crypto";
import { test, expect } from "@playwright/test";
import {
  decodeOutputStatus,
  type OutputStatus,
} from "../../../src/contracts/authoringOutputCodec";

// Explicit real-artifact loopback qualification. Never counted by the ordinary fixture suite.
test("qualified renderer artifact reaches a real browser preview and native download", async ({
  page,
}) => {
  test.skip(
    !process.env.H3_OUTPUT_FIXTURE_TARGET,
    "explicit real artifact fixture is required",
  );
  let succeeded: OutputStatus | null = null;
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (
      path.startsWith("/h3-context/v1/authoring/render") &&
      response.status() === 200
    )
      void response
        .json()
        .then((wire) => {
          const status = decodeOutputStatus(wire);
          if (status.phase === "succeeded") succeeded = status;
        })
        .catch(() => undefined);
  });
  await page.addInitScript(() => {
    const counts = { created: 0, revoked: 0, originalFetch: 0 };
    Object.defineProperty(window, "outputNativeCounts", { value: counts });
    const fetch = window.fetch.bind(window),
      create = URL.createObjectURL.bind(URL),
      revoke = URL.revokeObjectURL.bind(URL);
    window.fetch = (input, init) => {
      if (String(input).includes("/download")) counts.originalFetch++;
      return fetch(input, init);
    };
    URL.createObjectURL = (blob) => {
      counts.created++;
      return create(blob);
    };
    URL.revokeObjectURL = (url) => {
      counts.revoked++;
      revoke(url);
    };
  });
  await page.goto("/authoringOutput.html?qualified=1");
  await page.getByRole("button", { name: "Render final video" }).click();
  await expect(page.getByText("Video ready", { exact: true })).toBeVisible();
  await expect.poll(() => succeeded !== null).toBe(true);
  const first = succeeded as unknown as OutputStatus;
  const resources = () =>
    page.evaluate(async () => {
      const response = await fetch("/__output_fixture/resources");
      if (!response.ok) throw new Error("fixture resource observation failed");
      return (await response.json()) as {
        active_responses: number;
        native_preview_pending: boolean;
      };
    });
  // Observe real native derivative admission before unmounting; no renderer/status double.
  await page.getByRole("button", { name: "Preview output" }).click();
  await expect
    .poll(async () => (await resources()).native_preview_pending)
    .toBe(true);
  await page.getByRole("button", { name: "Toggle leaf" }).click();
  await expect
    .poll(resources)
    .toEqual({ active_responses: 0, native_preview_pending: false });
  const retained = decodeOutputStatus(
    await page.evaluate(
      async ({ job, workspace }) => {
        const response = await fetch(
          `/h3-context/v1/authoring/render/${job}?workspace_handle=${encodeURIComponent(workspace)}`,
        );
        if (!response.ok)
          throw new Error("completed job unavailable after preview abort");
        return response.json();
      },
      { job: first.job_handle, workspace: first.workspace_handle },
    ),
  );
  expect(retained).toEqual(first);
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { outputNativeCounts: object })
          .outputNativeCounts,
    ),
  ).toEqual({ created: 0, revoked: 0, originalFetch: 0 });
  succeeded = null;
  await page.getByRole("button", { name: "Toggle leaf" }).click();
  await page.getByRole("button", { name: "Render final video" }).click();
  await expect(page.getByText("Video ready", { exact: true })).toBeVisible();
  await expect
    .poll(() => succeeded !== null && succeeded.job_handle !== first.job_handle)
    .toBe(true);
  const status = succeeded as unknown as OutputStatus;
  expect(status.output?.audio_streams).toBe(
    Number(process.env.H3_OUTPUT_FIXTURE_AUDIO),
  );
  await page.getByRole("button", { name: "Preview output" }).click();
  const video = page.getByLabel("Final video preview");
  await expect(video).toHaveAttribute("src", /^blob:/);
  await expect
    .poll(() => video.evaluate((node) => (node as HTMLVideoElement).readyState))
    .toBeGreaterThanOrEqual(1);
  expect(
    await video.evaluate((node) => (node as HTMLVideoElement).duration),
  ).toBeCloseTo(1, 2);
  // This fixture mutates the real workspace reference revision, not a status double.
  expect(
    await page.evaluate(async () => {
      const response = await fetch("/__output_fixture/revise", {
        method: "POST",
      });
      return response.status;
    }),
  ).toBe(200);
  await page.getByRole("button", { name: "Refresh status" }).click();
  await expect(
    page.getByText("Output from an earlier revision", { exact: true }),
  ).toBeVisible();
  await expect.poll(() => succeeded?.currency).toBe("old_revision");
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download original" }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("authoring-final.mp4");
  expect(await download.failure()).toBeNull();
  const stream = await download.createReadStream(),
    hash = createHash("sha256");
  let length = 0;
  for await (const chunk of stream!) {
    length += chunk.length;
    hash.update(chunk);
  }
  expect(length).toBe(status.output?.byte_length);
  expect("sha256:" + hash.digest("hex")).toBe(
    status.output?.output_fingerprint,
  );
  await download.delete();
  await page.getByRole("button", { name: "Toggle leaf" }).click();
  await expect(video).toHaveCount(0);
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { outputNativeCounts: object })
          .outputNativeCounts,
    ),
  ).toEqual({ created: 1, revoked: 1, originalFetch: 0 });
});
