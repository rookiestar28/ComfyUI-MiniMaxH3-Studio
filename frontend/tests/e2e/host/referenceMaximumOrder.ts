import { expect, type Page } from "./fixture";
import {
  openH3AppModeTab,
  setSupportedH3Language,
  waitForH3Registration,
  waitForHostGraphSettled,
} from "./environment";

type Prompt = Record<
  string,
  { class_type: string; inputs: Record<string, unknown> }
>;
type Sources = { images: string[]; videos: string[]; audios: string[] };
export async function sidebarReferenceOrder(
  page: Page,
  media: {
    image: string;
    video: string;
    audio: string;
    images?: string[];
    videos?: string[];
  },
  counts: {
    images: number;
    videos: number;
    audios: number;
    soundtrack: "included" | "excluded";
  },
): Promise<Sources> {
  // IMPORTANT: sidebar remounts retain drafts. Each independent maximum needs a fresh session.
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await waitForHostGraphSettled(page);
  const sources: Sources = {
    images: Array.from(
      { length: counts.images },
      (_, i) => media.images?.[i % 3] ?? media.image,
    ),
    videos: Array.from(
      { length: counts.videos },
      (_, i) => media.videos?.[i % 2] ?? media.video,
    ),
    audios: Array.from({ length: counts.audios }, () => media.audio),
  };
  await page.evaluate(async (sources) => {
    const prompt = Object.fromEntries(
      (["images", "videos", "audios"] as const).flatMap((kind, group) =>
        sources[kind].map((locator, i) => [
          String(500 + group * 100 + i),
          {
            class_type:
              kind === "images"
                ? "LoadImage"
                : kind === "videos"
                  ? "LoadVideo"
                  : "LoadAudio",
            inputs: {
              [kind === "images"
                ? "image"
                : kind === "videos"
                  ? "file"
                  : "audio"]: locator,
            },
          },
        ]),
      ),
    );
    await (window as any).comfyAPI.app.app.loadApiJson(
      prompt,
      "ordered-reference-maxima",
    );
  }, sources);
  await setSupportedH3Language(page, "en");
  const container = await openH3AppModeTab(page, "h3-context-reference-maxima");
  await container.getByLabel("Task mode").selectOption("ref2va");
  await container
    .getByRole("textbox", { name: "Intent" })
    .fill("Keep selected reference order.");
  await container
    .getByRole("spinbutton", { name: "Clip duration (seconds)" })
    .fill("5");
  const selected: Sources = { images: [], videos: [], audios: [] };
  for (const [kind, label] of [
    ["images", "Reference images"],
    ["videos", "Reference videos"],
    ["audios", "Reference audio"],
  ] as const) {
    const select = container.getByRole("combobox", {
      name: label,
      exact: true,
    });
    for (let i = 0; i < counts[kind]; i++) {
      await expect
        .poll(() => select.locator("option").count())
        .toBeGreaterThan(1);
      const id = await select.locator("option").last().getAttribute("value");
      if (!id) throw new Error("The ordered source choice is missing");
      selected[kind].push(id);
      await select.selectOption(id);
      if (kind === "videos" && i === 0)
        await container
          .getByRole("combobox", {
            name: "Reference video soundtrack",
            exact: true,
          })
          .selectOption(counts.soundtrack);
    }
  }
  const before = (await page.evaluate((selected) => {
    const app = (window as any).comfyAPI.app.app;
    return Object.fromEntries(
      (["images", "videos", "audios"] as const).map((kind) => [
        kind,
        selected[kind].map((id) => {
          const node = app.graph.getNodeById(Number(id));
          return node.widgets.find(
            (widget: any) =>
              widget.name ===
              (kind === "images"
                ? "image"
                : kind === "videos"
                  ? "file"
                  : "audio"),
          ).value;
        }),
      ]),
    );
  }, selected)) as Sources;
  let queues = 0;
  const refuseQueue = (route: import("@playwright/test").Route) => {
    queues++;
    return route.abort("blockedbyclient");
  };
  // CRITICAL: maxima are model-free. Refuse an unexpected browser queue before it can load weights.
  await page.route("**/prompt", refuseQueue);
  try {
    const submit = container.locator('button[type="submit"]');
    await expect(submit).toBeEnabled();
    await submit.click();
    await expect
      .poll(
        () =>
          page.evaluate(
            () =>
              (window as any).comfyAPI.app.app.graph
                .serialize()
                .nodes.filter(
                  (node: any) =>
                    node.type ===
                    "comfyui_h3_context.H3Context.ReferenceRegistry",
                ).length,
          ),
        { timeout: 120_000 },
      )
      .toBe(1);
    expect(queues).toBe(0);
    const output = (await page.evaluate(
      async () =>
        (await (window as any).comfyAPI.app.app.graphToPrompt()).output,
    )) as Prompt;
    const registries = Object.values(output).filter(
      (node) =>
        node.class_type === "comfyui_h3_context.H3Context.ReferenceRegistry",
    );
    const natives = Object.values(output).filter(
      (node) => node.class_type === "MiniMaxH3ReferenceToVideo",
    );
    expect(registries).toHaveLength(1);
    expect(natives).toHaveLength(1);
    for (const [kind, group, stem, nativeGroup, nativeStem] of [
      ["images", "images", "image", "ref_images", "ref_image_"],
      ["videos", "videos", "video", "ref_videos", "ref_video_"],
      ["audios", "audios", "audio", "ref_audios", "ref_audio_"],
    ] as const) {
      const inputs = registries[0]!.inputs;
      expect(
        Object.keys(inputs)
          .filter((name) => name === group || name.startsWith(group + "."))
          .sort(),
      ).toEqual(
        Array.from(
          { length: counts[kind] },
          (_, i) => `${group}.${stem}${i}`,
        ).sort(),
      );
      expect(
        Object.keys(natives[0]!.inputs)
          .filter(
            (name) =>
              name === nativeGroup || name.startsWith(nativeGroup + "."),
          )
          .sort(),
      ).toEqual(
        Array.from(
          { length: counts[kind] },
          (_, i) => `${nativeGroup}.${nativeStem}${i}`,
        ).sort(),
      );
      const locators = Array.from({ length: counts[kind] }, (_, i) => {
        const link = inputs[`${group}.${stem}${i}`] as [
          string | number,
          number,
        ];
        expect(link[1]).toBe(0);
        const native = natives[0]!.inputs[
          `${nativeGroup}.${nativeStem}${i}`
        ] as [string | number, number];
        expect(native[1]).toBe(0);
        if (kind === "videos") {
          const component = output[String(native[0])]!;
          expect(component.class_type).toBe("GetVideoComponents");
          expect(component.inputs.video).toEqual(link);
          if (counts.soundtrack === "included") {
            const pair = [native[0], 1];
            expect(inputs[`paired_audios.paired_audio${i}`]).toEqual(pair);
            expect(
              natives[0]!.inputs[`ref_video_audios.ref_video_audio_${i}`],
            ).toEqual(pair);
          }
        } else expect(native).toEqual(link);
        return output[String(link[0])]!.inputs[
          kind === "images" ? "image" : kind === "videos" ? "file" : "audio"
        ];
      });
      expect(locators).toEqual(before[kind]);
    }
    const pairs = counts.soundtrack === "included" ? counts.videos : 0;
    expect(
      Object.keys(registries[0]!.inputs).filter((name) =>
        name.startsWith("paired_audios."),
      ),
    ).toHaveLength(pairs);
    expect(
      Object.keys(natives[0]!.inputs).filter((name) =>
        name.startsWith("ref_video_audios."),
      ),
    ).toHaveLength(pairs);
    return before;
  } finally {
    await page.unroute("**/prompt", refuseQueue);
  }
}
