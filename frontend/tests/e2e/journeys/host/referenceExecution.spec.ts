import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { sidebarCopy } from "../../../../src/i18n/catalog";
import { referenceLabels } from "../../../../src/host/referenceSelection";
import { sidebarReferenceOrder } from "../../host/referenceMaximumOrder";

import { expect, test, type Page } from "../../host/fixture";
import {
  assertCandidateBundleInjection,
  candidateInjectionCount,
  expectCompleteHostAssetResolution,
  hostAssetResolutionReceipt,
  hostUrl,
  openH3AppModeTab,
  officialAssetManifest,
  repositoryRoot,
  setSupportedH3Language,
  waitForH3Registration,
  waitForHostGraphSettled,
} from "../../host/environment";

type Prompt = Record<
  string,
  { class_type: string; inputs: Record<string, unknown> }
>;
type Media = {
  image: string;
  video: string;
  audio: string;
  images?: string[];
  videos?: string[];
};

const candidateIdentity = () => ({
  bundleSha256: process.env.H3_CONTEXT_CANDIDATE_BUNDLE_SHA256,
  installedInventorySha256: process.env.H3_CONTEXT_CANDIDATE_INVENTORY_SHA256,
});

function readMedia(): Media {
  const path = process.env.H3_CONTEXT_REFERENCE_MEDIA_FIXTURE;
  if (!path)
    throw new Error(
      "An explicitly authorized reference media fixture is required",
    );
  const media = JSON.parse(readFileSync(path, "utf8")) as Media;
  for (const kind of ["image", "video", "audio"] as const) {
    if (
      typeof media[kind] !== "string" ||
      !media[kind] ||
      /[\r\n\0]/.test(media[kind])
    )
      throw new Error("The reference media fixture is malformed");
  }
  for (const [kind, count] of [
    ["images", 3],
    ["videos", 2],
  ] as const) {
    const values = media[kind];
    if (
      values !== undefined &&
      (!Array.isArray(values) ||
        values.length !== count ||
        new Set(values).size !== count ||
        values.some(
          (value) =>
            typeof value !== "string" || !value || /[\r\n\0]/.test(value),
        ))
    )
      throw new Error("The multi-reference fixture is malformed");
  }
  if ((media.images === undefined) !== (media.videos === undefined))
    throw new Error("Both multi-reference groups are required");
  return media;
}

function modelFreePrompt(file: string, media: Media): Prompt {
  const python = resolve(
    repositoryRoot,
    process.platform === "win32"
      ? ".venv/Scripts/python.exe"
      : ".venv/bin/python",
  );
  // IMPORTANT: the existing builder validates the unmodified shipped fixture first. Only its
  // loader locators change below; rewriting media edges would hide an invalid shipped topology.
  const program = [
    "import json,sys",
    "from pathlib import Path",
    "from scripts.m3_08_host_e2e import build_model_free_prompt",
    "fixture=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))",
    "print(json.dumps(build_model_free_prompt(fixture)))",
  ].join("\n");
  const prompt = JSON.parse(
    execFileSync(
      python,
      ["-B", "-c", program, resolve(repositoryRoot, "workflows", file)],
      {
        cwd: repositoryRoot,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
        windowsHide: true,
      },
    ),
  ) as Prompt;
  for (const node of Object.values(prompt)) {
    if (node.class_type === "LoadImage") node.inputs.image = media.image;
    if (node.class_type === "LoadVideo") node.inputs.file = media.video;
    if (node.class_type === "LoadAudio") node.inputs.audio = media.audio;
  }
  return prompt;
}

function strings(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(strings);
  if (value !== null && typeof value === "object")
    return Object.values(value).flatMap(strings);
  return [];
}

async function initialize(page: Page): Promise<void> {
  if (!hostUrl) throw new Error("H3_CONTEXT_HOST_URL is required");
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await waitForHostGraphSettled(page);
}

test("exact host executes migrated reference examples and refuses unknown linked children", async ({
  context,
  page,
  request,
}, testInfo) => {
  test.setTimeout(180_000);
  const injection = candidateInjectionCount(context);
  await initialize(page);
  await assertCandidateBundleInjection(page, context, injection);
  const media = readMedia();
  const evidence: Array<Record<string, unknown>> = [];
  const execute = async (
    prompt: Prompt,
    label: string,
    required: string[],
    ordered = false,
  ): Promise<void> => {
    const queued = await request.post(`${hostUrl}/prompt`, {
      data: { prompt },
    });
    expect(queued.ok(), "The real host must accept the reference prompt").toBe(
      true,
    );
    const queuedBody = await queued.json();
    expect(typeof queuedBody.prompt_id === "string").toBe(true);
    const id = queuedBody.prompt_id as string;
    let history: any;
    await expect
      .poll(
        async () => {
          const response = await request.get(`${hostUrl}/history/${id}`);
          history = (await response.json())[id];
          return history?.status?.completed === true;
        },
        { timeout: 60_000 },
      )
      .toBe(true);
    const status = history.status.status_str;
    if (status !== "success") {
      const errors = (history.status.messages ?? [])
        .filter((item: any[]) => item[0] === "execution_error")
        .map((item: any[]) => ({
          node_type: item[1]?.node_type,
          exception_type: item[1]?.exception_type,
        }));
      throw new Error(
        `Reference execution failed: ${JSON.stringify({ status, errors })}`,
      );
    }
    const text = strings(history.outputs).join("\n");
    expect(
      required.every((marker) => text.includes(marker)),
      "The executed terminal projection must carry its expected reference labels",
    ).toBe(true);
    const preview = Object.entries(prompt).find(
      ([, node]) => node.class_type === "comfyui_h3_context.H3Context.Preview",
    );
    expect(
      preview !== undefined && history.outputs[preview[0]] !== undefined,
      "The fixture's own Preview must execute",
    ).toBe(true);
    if (ordered) {
      const shell = Object.entries(prompt).find(
        ([, node]) =>
          node.class_type === "comfyui_h3_context.H3Context.ProductShell",
      );
      const bindings = history.outputs[shell?.[0] ?? ""]?.bindings?.[0];
      expect(
        Array.isArray(bindings),
        "The real ProductShell must expose ordered native bindings",
      ).toBe(true);
      expect(
        bindings.map((binding: any) => binding.presentation_label),
      ).toEqual(required);
    }
    evidence.push({
      label,
      success: true,
      required_label_count: required.length,
      preview_executed: true,
      ...(ordered ? { ordered_bindings: true } : {}),
    });
  };
  // Two real submissions are separate executions even when ComfyUI can reuse cached node values.
  const allGroups = modelFreePrompt("m3_07_h3_context_reference.json", media);
  allGroups["20"] = { class_type: "LoadVideo", inputs: { file: media.video } };
  allGroups["21"] = {
    class_type: "GetVideoComponents",
    inputs: { video: ["20", 0] },
  };
  allGroups["22"] = { class_type: "LoadAudio", inputs: { audio: media.audio } };
  const registry = Object.values(allGroups).find(
    (node) =>
      node.class_type === "comfyui_h3_context.H3Context.ReferenceRegistry",
  )!;
  registry.inputs["videos.video0"] = ["20", 0];
  registry.inputs["paired_audios.paired_audio0"] = ["21", 1];
  registry.inputs["audios.audio0"] = ["22", 0];
  for (let start = 1; start <= 2; start++)
    await execute(allGroups, `four-groups-start-${start}`, [
      "<Picture 1>",
      "<Picture 2>",
      "<Video 1>",
      "<Audio 1>",
      "<Audio 2>",
    ]);
  for (const file of [
    "m3_07_h3_context_reference.json",
    "m15_03_product_shell_reference.json",
    "m15_09_assistant_reference.json",
    "m6_07_h3_context_full_reference.json",
  ])
    await execute(
      modelFreePrompt(file, media),
      file,
      file.startsWith("m6_07")
        ? ["<Picture 1>", "<Picture 2>", "<Video 1>", "<Audio 1>"]
        : ["<Picture 1>", "<Picture 2>"],
    );
  const invalid = structuredClone(allGroups);
  for (const profile of [
    {
      name: "image-audio-max",
      images: 9,
      videos: 0,
      audios: 3,
      soundtrack: "excluded",
    },
    {
      name: "image-video-max",
      images: 9,
      videos: 3,
      audios: 0,
      soundtrack: "excluded",
    },
    {
      name: "paired-video-max",
      images: 6,
      videos: 3,
      audios: 0,
      soundtrack: "included",
    },
  ] as const) {
    const chosen = await sidebarReferenceOrder(page, media, profile);
    const prompt = modelFreePrompt(
      "m15_03_product_shell_reference.json",
      media,
    );
    const registry = Object.values(prompt).find(
      (node) =>
        node.class_type === "comfyui_h3_context.H3Context.ReferenceRegistry",
    )!;
    for (const name of Object.keys(registry.inputs))
      if (/^(images|videos|paired_audios|audios)\./.test(name))
        delete registry.inputs[name];
    for (let i = 0; i < profile.images; i++) {
      const id = String(100 + i);
      prompt[id] = {
        class_type: "LoadImage",
        inputs: { image: chosen.images[i] },
      };
      registry.inputs[`images.image${i}`] = [id, 0];
    }
    for (let i = 0; i < profile.videos; i++) {
      const id = String(200 + i),
        component = String(210 + i);
      prompt[id] = {
        class_type: "LoadVideo",
        inputs: { file: chosen.videos[i] },
      };
      registry.inputs[`videos.video${i}`] = [id, 0];
      if (profile.soundtrack === "included") {
        prompt[component] = {
          class_type: "GetVideoComponents",
          inputs: { video: [id, 0] },
        };
        registry.inputs[`paired_audios.paired_audio${i}`] = [component, 1];
      }
    }
    for (let i = 0; i < profile.audios; i++) {
      const id = String(300 + i);
      prompt[id] = {
        class_type: "LoadAudio",
        inputs: { audio: chosen.audios[i] },
      };
      registry.inputs[`audios.audio${i}`] = [id, 0];
    }
    await execute(prompt, profile.name, referenceLabels(profile).ordered, true);
  }
  const invalidRegistry = Object.values(invalid).find(
    (node) =>
      node.class_type === "comfyui_h3_context.H3Context.ReferenceRegistry",
  )!;
  // CRITICAL: this V1 host drops unknown unlinked literals before validation. The refusal probe
  // must be linked to a real producer, or it never exercises the receiver's unknown-key guard.
  invalidRegistry.inputs["images.image9"] = ["2", 0];
  const refused = await request.post(`${hostUrl}/prompt`, {
    data: { prompt: invalid },
  });
  expect(refused.status()).toBe(400);
  const refusal = await refused.json();
  expect(
    strings(refusal.node_errors).some((value) =>
      value.includes("unknown_reference_input"),
    ),
  ).toBe(true);
  const foreign = await page.evaluate(() => {
    const app = (window as any).comfyAPI.app.app;
    return (app.extensions ?? []).filter(
      (extension: any) =>
        !String(extension.name).startsWith("comfyui-h3-context."),
    ).length;
  });
  expect(foreign).toBeGreaterThan(0);
  await testInfo.attach("reference_execution", {
    body: Buffer.from(
      JSON.stringify({
        schema: "h3.context.reference_execution.v2",
        candidate: candidateIdentity(),
        success: true,
        executions: evidence,
        typed_unknown_link_refusal: true,
        consecutive_starts: 2,
        foreign_extension_count: foreign,
      }),
    ),
    contentType: "application/json",
  });
});

test("authorized Ref2VA sidebar sample executes all media kinds and writes a real artifact", async ({
  context,
  page,
  request,
}, testInfo) => {
  test.skip(
    process.env.H3_CONTEXT_WEIGHT_SAMPLE !== "1",
    "A per-session weight sample grant is required",
  );
  test.setTimeout(45 * 60_000);
  const injection = candidateInjectionCount(context);
  await initialize(page);
  await assertCandidateBundleInjection(page, context, injection);
  await setSupportedH3Language(page, "en");
  const media = readMedia();
  const images = media.images ?? [media.image],
    videos = media.videos ?? [media.video];
  const counts = {
    images: images.length,
    videos: videos.length,
    pairedAudios: videos.length,
    audios: 1,
  };
  await page.evaluate(async (locators) => {
    const app = (window as any).comfyAPI.app.app;
    await app.loadApiJson(
      {
        ...Object.fromEntries(
          (locators.images ?? [locators.image]).map((image, index) => [
            String(17 + index),
            { class_type: "LoadImage", inputs: { image } },
          ]),
        ),
        ...Object.fromEntries(
          (locators.videos ?? [locators.video]).map((file, index) => [
            String(22 + index),
            { class_type: "LoadVideo", inputs: { file } },
          ]),
        ),
        "31": { class_type: "LoadAudio", inputs: { audio: locators.audio } },
      },
      "authorized-reference-sources",
    );
  }, media);
  const container = await openH3AppModeTab(page, "h3-context-reference-sample");
  await container.getByLabel("Task mode").selectOption("ref2va");
  await container
    .getByRole("textbox", { name: "Intent" })
    .fill(
      "A colorful kite crosses a clear sky, preserving the supplied references.",
    );
  await container
    .getByRole("spinbutton", { name: "Clip duration (seconds)" })
    .fill("5");
  for (const label of [
    "Reference images",
    "Reference videos",
    "Reference audio",
  ]) {
    const source = container.getByRole("combobox", {
      name: label,
      exact: true,
    });
    await expect
      .poll(() => source.locator("option").count())
      .toBeGreaterThan(1);
    const count =
      label === "Reference images"
        ? images.length
        : label === "Reference videos"
          ? videos.length
          : 1;
    for (let i = 0; i < count; i++) await source.selectOption({ index: 1 });
  }
  const queuedIds: string[] = [];
  let queueNodeErrors = 0;
  let emittedNames = false;
  let emittedSampleResolution = false;
  let emittedQuantizedModel = false;
  page.on("response", async (response) => {
    if (
      new URL(response.url()).pathname.replace(/^\/api/, "") !== "/prompt" ||
      response.request().method() !== "POST"
    )
      return;
    const body = response.request().postDataJSON();
    const nodes = Object.values(body.prompt ?? {}) as Array<{
      class_type: string;
      inputs: Record<string, unknown>;
    }>;
    const registry = nodes.find(
      (node) =>
        node.class_type === "comfyui_h3_context.H3Context.ReferenceRegistry",
    );
    const native = nodes.find(
      (node) => node.class_type === "MiniMaxH3ReferenceToVideo",
    );
    emittedSampleResolution =
      nodes.filter((node) => node.class_type === "ResolutionSelector")
        .length === 1 &&
      nodes.some(
        (node) =>
          node.class_type === "ResolutionSelector" &&
          node.inputs.megapixels === 0.2,
      );
    emittedQuantizedModel = nodes.some(
      (node) =>
        node.class_type === "UNETLoader" &&
        typeof node.inputs.unet_name === "string" &&
        /_(int8_convrot|fp8_scaled)\.safetensors$/i.test(node.inputs.unet_name),
    );
    const entries = Object.entries(body.prompt ?? {}) as Array<
      [string, Prompt[string]]
    >;
    const source = (type: string, input: string, locator: string) => {
      const matches = entries.filter(
        ([, node]) =>
          node.class_type === type && node.inputs[input] === locator,
      );
      return matches.length === 1 ? matches[0]![0] : undefined;
    };
    const link = (value: unknown, id: string | undefined, slot: number) =>
      id !== undefined &&
      Array.isArray(value) &&
      value.length === 2 &&
      String(value[0]) === id &&
      value[1] === slot;
    const dense = (
      inputs: Record<string, unknown> | undefined,
      group: string,
      stem: string,
      count: number,
    ) => {
      const names = Object.keys(inputs ?? {}).filter(
        (key) => key === group || key.startsWith(`${group}.`),
      );
      return (
        names.length === count &&
        Array.from({ length: count }, (_, i) => `${group}.${stem}${i}`).every(
          (key) => names.includes(key),
        )
      );
    };
    emittedNames =
      registry !== undefined &&
      native !== undefined &&
      dense(registry.inputs, "images", "image", counts.images) &&
      dense(registry.inputs, "videos", "video", counts.videos) &&
      dense(
        registry.inputs,
        "paired_audios",
        "paired_audio",
        counts.pairedAudios,
      ) &&
      dense(registry.inputs, "audios", "audio", 1) &&
      dense(native.inputs, "ref_images", "ref_image_", counts.images) &&
      dense(native.inputs, "ref_videos", "ref_video_", counts.videos) &&
      dense(
        native.inputs,
        "ref_video_audios",
        "ref_video_audio_",
        counts.pairedAudios,
      ) &&
      dense(native.inputs, "ref_audios", "ref_audio_", 1) &&
      images.every((locator, i) => {
        const id = source("LoadImage", "image", locator);
        return (
          link(registry.inputs[`images.image${i}`], id, 0) &&
          link(native.inputs[`ref_images.ref_image_${i}`], id, 0)
        );
      }) &&
      videos.every((locator, i) => {
        const id = source("LoadVideo", "file", locator);
        const components = entries.filter(
          ([, node]) =>
            node.class_type === "GetVideoComponents" &&
            link(node.inputs.video, id, 0),
        );
        const component =
          components.length === 1 ? components[0]![0] : undefined;
        return (
          link(registry.inputs[`videos.video${i}`], id, 0) &&
          link(native.inputs[`ref_videos.ref_video_${i}`], component, 0) &&
          link(
            registry.inputs[`paired_audios.paired_audio${i}`],
            component,
            1,
          ) &&
          link(
            native.inputs[`ref_video_audios.ref_video_audio_${i}`],
            component,
            1,
          )
        );
      }) &&
      link(
        registry.inputs["audios.audio0"],
        source("LoadAudio", "audio", media.audio),
        0,
      ) &&
      link(
        native.inputs["ref_audios.ref_audio_0"],
        source("LoadAudio", "audio", media.audio),
        0,
      );
    const result = await response.json().catch(() => ({}));
    queueNodeErrors += Object.keys(result.node_errors ?? {}).length;
    if (typeof result.prompt_id === "string") queuedIds.push(result.prompt_id);
  });
  const submit = container.locator('button[type="submit"]');
  await expect(submit).toBeEnabled({ timeout: 120_000 });
  const shellReason = async () =>
    container.locator("[data-shell-status]").getAttribute("data-shell-reason");
  const shellState = async () =>
    `${await container.locator("[data-shell-status]").getAttribute("data-shell-status")}:${await shellReason()}`;
  await submit.click();
  // IMPORTANT: preparation is a separate press. A subsequent host census can turn canvas_ready
  // into native_preference; qualify the prepared graph below rather than waiting for a transient
  // reason and mistaking completed preparation for a timeout. No queue may precede that proof.
  await expect
    .poll(shellState, { timeout: 180_000 })
    .toMatch(
      /^(interactive:(canvas_ready|native_preference|dirty_graph|ambiguous_graph|malformed_graph|incompatible_graph)|error:)$/,
    );
  if (
    (await container
      .locator("[data-shell-status]")
      .getAttribute("data-shell-status")) === "error"
  ) {
    const visible = await container
      .locator(".h3-context-error p")
      .allTextContents();
    const codes = Object.entries(sidebarCopy("en").errorMessages)
      .filter(([, value]) => visible.includes(value))
      .map(([code]) => code);
    throw new Error(`Canvas preparation refused: ${JSON.stringify({ codes })}`);
  }
  const readyReasons = ["canvas_ready", "native_preference"];
  if (!readyReasons.includes((await shellReason()) ?? "")) {
    await submit.click();
    await expect
      .poll(shellReason, { timeout: 180_000 })
      .toMatch(/^(canvas_ready|native_preference)$/);
  }
  expect(queuedIds).toHaveLength(0);
  const preparedReferenceGraph = await page.evaluate(() => {
    const graph = (window as any).comfyAPI.app.app.graph.serialize();
    const registries = graph.nodes.filter(
      (node: any) =>
        node.type === "comfyui_h3_context.H3Context.ReferenceRegistry",
    );
    const anchors = graph.nodes.filter(
      (node: any) => node.type === "MiniMaxH3ReferenceToVideo",
    );
    const linked = (registries[0]?.inputs ?? [])
      .filter((input: any) => input.link != null)
      .map((input: any) => input.name);
    return (
      registries.length === 1 &&
      anchors.length === 1 &&
      [
        "images.image0",
        "videos.video0",
        "paired_audios.paired_audio0",
        "audios.audio0",
      ].every((name) => linked.includes(name))
    );
  });
  expect(
    preparedReferenceGraph,
    "The prepared visible graph must carry all four reference groups",
  ).toBe(true);
  const referenceModel = officialAssetManifest.slots.find(
    (slot) => slot.slot === "reference_unet",
  );
  if (referenceModel === undefined)
    throw new Error("The reference model role is missing");
  // IMPORTANT: the missing template default can make the advisory ladder choose full BF16.
  // The bounded sample must visibly choose an installed official quantized variant instead;
  // never change the product fallback order or silently rewrite a queued model selection.
  const selectedQuantizedModel = await page.evaluate((spec) => {
    const app = (window as any).comfyAPI.app.app;
    const nodes = app.graph._nodes.filter(
      (node: any) => node.type === spec.loader_type,
    );
    if (nodes.length !== 1) return false;
    const loader = nodes[0];
    const widget = loader.widgets.find(
      (value: any) => value.name === spec.widget_name,
    );
    const choices =
      loader.constructor.nodeData?.input?.required?.[spec.widget_name]?.[0];
    if (!widget || !Array.isArray(choices)) return false;
    const priority = spec.accepted_basenames
      .filter((value) =>
        /_(int8_convrot|fp8_scaled)\.safetensors$/i.test(value),
      )
      .reverse();
    const selected = priority.flatMap((basename) =>
      choices.filter(
        (value: unknown) =>
          typeof value === "string" &&
          value.replaceAll("\\", "/").split("/").at(-1)!.toLowerCase() ===
            basename.toLowerCase(),
      ),
    )[0];
    if (selected === undefined) return false;
    widget.value = selected;
    widget.callback?.(selected);
    app.graph.setDirtyCanvas(true, true);
    return widget.value === selected;
  }, referenceModel);
  expect(
    selectedQuantizedModel,
    "The visible sample model must be an installed official quantized variant",
  ).toBe(true);
  const videoVae = officialAssetManifest.slots.find(
    (slot) => slot.slot === "video_vae",
  );
  if (videoVae === undefined) throw new Error("The video VAE role is missing");
  // IMPORTANT: a template may name an uninstalled VAE variant. Select the installed official
  // role on the visible native widget; never rewrite a queued prompt or bypass queue validation.
  const selectedVae = await page.evaluate((spec) => {
    const app = (window as any).comfyAPI.app.app;
    const serialized = app.graph.serialize();
    const decoders = serialized.nodes.filter(
      (node: any) => node.type === "VAEDecode",
    );
    if (decoders.length !== 1) return false;
    const input = decoders[0].inputs.find((value: any) => value.name === "vae");
    const edge = serialized.links.find(
      (value: any[]) => value[0] === input?.link,
    );
    const loader = app.graph.getNodeById(edge?.[1]);
    if (loader?.type !== spec.loader_type) return false;
    const widget = loader.widgets.find(
      (value: any) => value.name === spec.widget_name,
    );
    const choices =
      loader.constructor.nodeData?.input?.required?.[spec.widget_name]?.[0];
    if (!widget || !Array.isArray(choices)) return false;
    const accepted = new Set(
      spec.accepted_basenames.map((value) => value.toLowerCase()),
    );
    const official = choices.filter(
      (value: unknown): value is string =>
        typeof value === "string" &&
        accepted.has(
          value.replaceAll("\\", "/").split("/").at(-1)!.toLowerCase(),
        ),
    );
    const selected =
      official.find((value: string) => value === spec.template_default) ??
      official[0];
    if (selected === undefined) return false;
    widget.value = selected;
    widget.callback?.(selected);
    app.graph.setDirtyCanvas(true, true);
    return widget.value === selected;
  }, videoVae);
  expect(
    selectedVae,
    "The visible native VAE must select an installed official role",
  ).toBe(true);
  const selectedResolution = await page.evaluate(() => {
    const app = (window as any).comfyAPI.app.app;
    const nodes = app.graph._nodes.filter(
      (node: any) => node.type === "ResolutionSelector",
    );
    if (nodes.length !== 1) return false;
    const widget = nodes[0].widgets.find(
      (value: any) => value.name === "megapixels",
    );
    if (!widget || widget.options.min > 0.2 || widget.options.max < 0.2)
      return false;
    widget.value = 0.2;
    widget.callback?.(0.2);
    app.graph.setDirtyCanvas(true, true);
    return widget.value === 0.2;
  });
  expect(selectedResolution, "The visible sample size must be 0.2 MP").toBe(
    true,
  );
  expectCompleteHostAssetResolution(
    await hostAssetResolutionReceipt(page, "reference_to_video"),
  );
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect.poll(() => queuedIds.length, { timeout: 180_000 }).toBe(1);
  expect(queueNodeErrors, "Every queued output must pass host validation").toBe(
    0,
  );
  expect(emittedSampleResolution, "The queued sample must retain 0.2 MP").toBe(
    true,
  );
  expect(
    emittedQuantizedModel,
    "The queued sample must retain the quantized model selection",
  ).toBe(true);
  expect(
    emittedNames,
    "The real queued prompt must use all exact dotted media inputs",
  ).toBe(true);
  expectCompleteHostAssetResolution(
    await hostAssetResolutionReceipt(page, "reference_to_video"),
  );
  let history: any;
  await expect
    .poll(
      async () => {
        const response = await request.get(
          `${hostUrl}/history/${queuedIds[0]}`,
        );
        history = (await response.json())[queuedIds[0]];
        return history?.status?.completed === true;
      },
      { timeout: 40 * 60_000, intervals: [2000] },
    )
    .toBe(true);
  if (history.status.status_str !== "success") {
    const errors = (history.status.messages ?? [])
      .filter((item: any[]) => item[0] === "execution_error")
      .map((item: any[]) => ({
        node_type: item[1]?.node_type,
        exception_type: item[1]?.exception_type,
      }));
    throw new Error(`Ref2VA sample failed: ${JSON.stringify({ errors })}`);
  }
  const collectArtifacts = (
    value: any,
  ): Array<{ filename: string; subfolder: string; type: string }> => {
    if (Array.isArray(value)) return value.flatMap(collectArtifacts);
    if (value === null || typeof value !== "object") return [];
    if (typeof value.filename === "string" && /\.mp4$/i.test(value.filename))
      return [
        {
          filename: value.filename,
          subfolder: value.subfolder ?? "",
          type: value.type ?? "output",
        },
      ];
    return Object.values(value).flatMap(collectArtifacts);
  };
  // IMPORTANT: LoadVideo also reports an input MP4 preview. Only the generation sink's
  // outputs can prove a new artifact; counting the reference would hide missing generation.
  const savedOutputs = Object.entries(history.outputs ?? {})
    .filter(([id]) => history.prompt[2]?.[id]?.class_type === "SaveVideo")
    .map(([, output]) => output);
  const artifacts = collectArtifacts(savedOutputs);
  expect(artifacts.length).toBeGreaterThan(0);
  for (const artifact of artifacts) {
    const readback = await request.get(`${hostUrl}/view`, { params: artifact });
    expect(readback.ok(), "The newly reported real video must read back").toBe(
      true,
    );
    const bytes = await readback.body();
    expect(bytes.length).toBeGreaterThan(1024);
    expect(bytes.subarray(4, 8).toString("ascii")).toBe("ftyp");
  }
  await testInfo.attach("reference_sample", {
    body: Buffer.from(
      JSON.stringify({
        schema: media.images
          ? "h3.context.reference_sample.v2"
          : "h3.context.reference_sample.v1",
        candidate: candidateIdentity(),
        success: true,
        image_count: images.length,
        video_count: videos.length,
        paired_audio_count: videos.length,
        standalone_audio_count: 1,
        exact_dotted_inputs: emittedNames,
        artifact_count: artifacts.length,
        artifact_verified: true,
      }),
    ),
    contentType: "application/json",
  });
});
