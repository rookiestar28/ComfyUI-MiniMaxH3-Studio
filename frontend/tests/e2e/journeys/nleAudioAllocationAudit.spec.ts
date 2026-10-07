import { expect, test } from "@playwright/test";

import { installNativeAudioAllocationAudit } from "../helpers/nativeAudioAllocationAudit";

test("native standalone allocation audit counts constructor, DOM and parser identities once", async ({
  page,
}) => {
  await page.addInitScript(installNativeAudioAllocationAudit);
  await page.goto("/");
  const counts = await page.evaluate(() => {
    const audit = window.__h3NativeAudioAudit!;
    const before = audit.snapshot().standaloneAudioElements;
    const first = new Audio();
    const second = document.createElement("audio");
    const root = document.createElement("div");
    root.innerHTML = "<audio></audio>";
    document.body.append(first, second, root);
    root.remove();
    first.remove();
    second.remove();
    return {
      before,
      after: audit.snapshot().standaloneAudioElements,
      repeated: audit.snapshot().standaloneAudioElements,
      branded: first instanceof HTMLAudioElement,
    };
  });
  expect(counts).toEqual({ before: 0, after: 3, repeated: 3, branded: true });
  expect(counts.after).not.toBe(0);
});

test("a muted video and one workspace AudioContext do not allocate standalone native audio", async ({
  page,
}) => {
  await page.addInitScript(installNativeAudioAllocationAudit);
  await page.goto("/");
  const evidence = await page.evaluate(async () => {
    const context = new AudioContext({ sampleRate: 48_000 });
    const video = document.createElement("video");
    video.muted = true;
    const standalone =
      window.__h3NativeAudioAudit!.snapshot().standaloneAudioElements;
    await context.close();
    return { standalone, contextState: context.state };
  });
  expect(evidence).toEqual({ standalone: 0, contextState: "closed" });
});
