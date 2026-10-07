import { describe, expect, it, vi } from "vitest";

import rawOfficialAssetManifest from "../../comfyui_h3_context/contracts/official_h3_assets_v2.json" with { type: "json" };
import { sidebarCopy, SUPPORTED_LOCALES } from "../src/i18n/catalog";
import {
  compiledPromptMatchesOfficialAssetResolution,
  createAppModeController,
} from "../src/host/appMode";
import {
  OFFICIAL_ASSET_MANIFEST,
  OFFICIAL_ASSET_POLICY_ID,
  OfficialAssetResolutionError,
  parseOfficialAssetManifest,
  readOfficialAssetInventory,
  readOfficialAssetInventoryFromNodeDefinitions,
  resolveOfficialAssets,
  selectOfficialAsset,
  type OfficialAssetInventory,
  type OfficialAssetSpec,
} from "../src/host/officialAssetResolution";
import { createQualifiedBasePrompt } from "./support/qualifiedBasePrompt";
import {
  ASSET_RELOCATED,
  generationProfile,
  MISSING_ASSET,
} from "./support/generationProfileFixture";
import { syntheticTemplate, type Json } from "./support/templateFixture";
import { fixtureBackedAppModeHost } from "./support/hostSeamTestDouble";
import {
  acceptedQueueResponse,
  rejectedQueueError,
} from "./support/queuePromptTestDouble";
import {
  familySpecs,
  installedInventory,
  nodeDefinitionPayloads,
  nodeDefinitions,
  templateWithDirectLoaders,
} from "./support/officialAssetInventoryFixture";

const syntheticSpec: OfficialAssetSpec = {
  slot: "video_unet",
  folderCategory: "synthetic_models",
  loaderType: "SyntheticLoader",
  widgetName: "model_name",
  templateDefault: "default.bin",
  acceptedBasenames: ["variant-a.bin", "variant-b.bin", "default.bin"],
};

describe("M17-29 deterministic official asset choice", () => {
  it("separates the public profile roles from complete template materialization roles", () => {
    const manifest = rawOfficialAssetManifest as unknown as Json;
    expect(manifest.schema).toBe("h3.context.official_assets.v2");
    expect(manifest.profile_families).toEqual({
      image_to_video: ["video_unet", "text_encoder", "video_vae", "audio_vae"],
      reference_to_video: [
        "reference_unet",
        "text_encoder",
        "video_vae",
        "audio_vae",
      ],
    });
    expect(manifest.materialization_families).toEqual({
      image_to_video: [
        "video_unet",
        "text_encoder",
        "video_vae",
        "audio_vae",
        "image_turbo_lora",
      ],
      reference_to_video: [
        "reference_unet",
        "text_encoder",
        "video_vae",
        "audio_vae",
        "reference_turbo_lora",
      ],
    });
    expect((manifest.slots as Json[]).map((entry) => entry.slot)).toEqual([
      "video_unet",
      "reference_unet",
      "text_encoder",
      "video_vae",
      "audio_vae",
      "image_turbo_lora",
      "reference_turbo_lora",
    ]);
  });

  it("keeps one manifest and one disclosed policy", () => {
    expect(OFFICIAL_ASSET_POLICY_ID).toBe(
      "template_default_then_manifest_order_v1",
    );
    expect(OFFICIAL_ASSET_MANIFEST.profileFamilies).toEqual({
      image_to_video: ["video_unet", "text_encoder", "video_vae", "audio_vae"],
      reference_to_video: [
        "reference_unet",
        "text_encoder",
        "video_vae",
        "audio_vae",
      ],
    });
    expect(OFFICIAL_ASSET_MANIFEST.slots.map((entry) => entry.slot)).toEqual([
      "video_unet",
      "reference_unet",
      "text_encoder",
      "video_vae",
      "audio_vae",
      "image_turbo_lora",
      "reference_turbo_lora",
    ]);
    expect(
      new Set(OFFICIAL_ASSET_MANIFEST.slots.map((entry) => entry.slot)).size,
    ).toBe(7);
  });

  it.each([
    [
      "exact default",
      ["variant-a.bin", "nested/default.bin", "default.bin"],
      "default.bin",
      "template_default",
    ],
    [
      "relocated default",
      ["z/nested/default.bin", "A\\default.bin", "variant-a.bin"],
      "A\\default.bin",
      "relocated_default",
    ],
    [
      "unique official variant",
      ["private.bin", "Moved\\Variant-B.BIN"],
      "Moved\\Variant-B.BIN",
      "official_variant",
    ],
    [
      "manifest precedence among variants",
      ["variant-b.bin", "deep/variant-a.bin"],
      "deep/variant-a.bin",
      "official_variant",
    ],
  ] as const)(
    "selects %s and preserves the host string",
    (_label, inventory, value, reason) => {
      expect(selectOfficialAsset(syntheticSpec, inventory)).toEqual({
        value,
        reason,
      });
    },
  );

  it("does not select a near-miss or arbitrary installed asset", () => {
    expect(
      selectOfficialAsset(syntheticSpec, [
        "default.bin.bak",
        "my-variant-a.bin",
        "private.bin",
      ]),
    ).toBeUndefined();
  });

  it.each(["image_turbo_lora", "reference_turbo_lora"] as const)(
    "applies exact-default then relocated-default selection to %s",
    (role) => {
      const spec = OFFICIAL_ASSET_MANIFEST.slots.find(
        (entry) => entry.slot === role,
      )!;
      expect(
        selectOfficialAsset(spec, [
          `nested/${spec.templateDefault}`,
          spec.templateDefault,
        ]),
      ).toEqual({ value: spec.templateDefault, reason: "template_default" });
      expect(
        selectOfficialAsset(spec, [`Official\\${spec.templateDefault}`]),
      ).toEqual({
        value: `Official\\${spec.templateDefault}`,
        reason: "relocated_default",
      });
    },
  );

  it("uses declared official order for the image Turbo LoRA fallback", () => {
    const spec = OFFICIAL_ASSET_MANIFEST.slots.find(
      (entry) => entry.slot === "image_turbo_lora",
    )!;
    const fallback = spec.acceptedBasenames.find(
      (entry) => entry !== spec.templateDefault,
    )!;
    expect(selectOfficialAsset(spec, [`Official/${fallback}`])).toEqual({
      value: `Official/${fallback}`,
      reason: "official_variant",
    });
  });

  it("rejects manifest policy, order, loader, and bounded-name drift", () => {
    const candidates: Json[] = [];
    const mutate = (callback: (candidate: Json) => void): void => {
      const candidate = structuredClone(rawOfficialAssetManifest) as Json;
      callback(candidate);
      candidates.push(candidate);
    };
    mutate((candidate) => {
      candidate.policy_id = "another_policy";
    });
    mutate((candidate) => {
      const slots = candidate.slots as Json[];
      [slots[0], slots[1]] = [slots[1]!, slots[0]!];
    });
    mutate((candidate) => {
      (candidate.slots as Json[])[0]!.loader_type = "";
    });
    mutate((candidate) => {
      (candidate.slots as Json[])[0]!.widget_name = "../widget";
    });
    mutate((candidate) => {
      const accepted = (candidate.slots as Json[])[0]!
        .accepted_basenames as string[];
      accepted.push(accepted[0]!);
    });
    mutate((candidate) => {
      ((candidate.slots as Json[])[0]!.accepted_basenames as string[])[0] =
        "x".repeat(256);
    });
    mutate((candidate) => {
      delete candidate.profile_families;
    });
    mutate((candidate) => {
      const families = candidate.materialization_families as Json;
      (families.image_to_video as string[]).pop();
    });
    for (const candidate of candidates) {
      expect(() => parseOfficialAssetManifest(candidate)).toThrowError(
        expect.objectContaining<Partial<OfficialAssetResolutionError>>({
          code: "invalid_manifest",
        }),
      );
    }
  });

  it.each([
    "../default.bin",
    "nested/../default.bin",
    "/default.bin",
    "\\default.bin",
    "C:\\models\\default.bin",
    "https://weights.invalid/default.bin",
    "nested//default.bin",
    "nested/./default.bin",
    "nested/\u0000/default.bin",
    "\\\\server\\share\\default.bin",
  ])("rejects an unsafe matching inventory path (%j)", (entry) => {
    expect(() => selectOfficialAsset(syntheticSpec, [entry])).toThrowError(
      expect.objectContaining<Partial<OfficialAssetResolutionError>>({
        code: "invalid_host_inventory",
      }),
    );
  });
});

function nestedTemplate(): {
  workflow: Json;
  promotedIndex: Map<string, number>;
} {
  const specs = familySpecs("image_to_video");
  const definitionInputs: Json[] = [
    { id: "image-1", name: "first_frame", type: "IMAGE" },
    { id: "image-2", name: "last_frame", type: "IMAGE" },
  ];
  const definitionNodes: Json[] = [];
  const definitionLinks: Json[] = [];
  const promotedIndex = new Map<string, number>();
  const values: string[] = [];
  specs.forEach((spec, index) => {
    const promotedName = `${spec.widgetName}_${index}`;
    definitionInputs.push({
      id: `input-${index}`,
      name: promotedName,
      type: "COMBO",
    });
    const targetSlot = spec.slot.endsWith("_turbo_lora") ? 1 : 0;
    const innerInputs =
      targetSlot === 1
        ? [
            { name: "model", type: "MODEL", link: 200 + index },
            { name: spec.widgetName, type: "COMBO", link: 100 + index },
          ]
        : [{ name: spec.widgetName, type: "COMBO", link: 100 + index }];
    definitionNodes.push({
      id: 20 + index,
      type: spec.loaderType,
      inputs: innerInputs,
      widgets_values: [spec.templateDefault],
      widgets_values_named: { [spec.widgetName]: spec.templateDefault },
    });
    definitionLinks.push({
      id: 100 + index,
      origin_id: -10,
      origin_slot: 2 + index,
      target_id: 20 + index,
      target_slot: targetSlot,
      type: "COMBO",
    });
    promotedIndex.set(spec.slot, index);
    values.push(spec.templateDefault);
  });
  return {
    promotedIndex,
    workflow: {
      nodes: [
        {
          id: 7,
          type: "definition-1",
          widgets_values: values,
          widgets_values_named: Object.fromEntries(
            specs.map((spec, index) => [
              `${spec.widgetName}_${index}`,
              spec.templateDefault,
            ]),
          ),
        },
      ],
      links: [],
      definitions: {
        subgraphs: [
          {
            id: "definition-1",
            inputs: definitionInputs,
            nodes: definitionNodes,
            links: definitionLinks,
          },
        ],
      },
    },
  };
}

function directTemplate(
  family: "image_to_video" | "reference_to_video" = "reference_to_video",
): Json {
  return {
    nodes: familySpecs(family).map((spec, index) => ({
      id: 100 + index,
      type: spec.loaderType,
      inputs: spec.slot.endsWith("_turbo_lora")
        ? [{ name: "model", type: "MODEL", link: index + 1 }]
        : [{ name: spec.widgetName, type: "COMBO" }],
      widgets_values: [spec.templateDefault],
      widgets_values_named: { [spec.widgetName]: spec.templateDefault },
    })),
    links: [],
    definitions: { subgraphs: [] },
  };
}

describe("M17-29 serialized template resolution", () => {
  it.each(["image_to_video", "reference_to_video"] as const)(
    "keeps unfamiliar names in every %s loader for ComfyUI validation",
    (family) => {
      const workflow = directTemplate(family);
      for (const node of workflow.nodes as Json[]) {
        const value = `user-renamed-${node.id}.safetensors`;
        node.widgets_values = [value];
        const key = Object.keys(node.widgets_values_named as Json)[0]!;
        node.widgets_values_named = { [key]: value };
      }
      const before = structuredClone(workflow);
      const { inventory } = installedInventory(family);
      const result = resolveOfficialAssets(workflow, family, inventory);
      expect(result.workflow).toEqual(before);
      expect(result.unresolvedSlots).toEqual(
        OFFICIAL_ASSET_MANIFEST.materializationFamilies[family],
      );
      expect(result.bindings).toEqual([]);
    },
  );

  it("preserves the official INT8 video VAE in a nested template", () => {
    const { workflow, promotedIndex } = nestedTemplate();
    const definition = ((workflow.definitions as Json).subgraphs as Json[])[0]!;
    const video = familySpecs("image_to_video").find(
      (spec) => spec.slot === "video_vae",
    )!;
    const inner = (definition.nodes as Json[]).find(
      (node) =>
        (node.widgets_values_named as Json)[video.widgetName] ===
        video.templateDefault,
    )!;
    const value = "minimax_h3_video_vae_int8_convrot.safetensors";
    inner.widgets_values = [value];
    inner.widgets_values_named = { [video.widgetName]: value };
    const owner = (workflow.nodes as Json[])[0]!;
    const index = promotedIndex.get("video_vae")!;
    (owner.widgets_values as unknown[])[index] = value;
    (owner.widgets_values_named as Json)[
      Object.keys(owner.widgets_values_named as Json)[index]!
    ] = value;
    const { inventory } = installedInventory("image_to_video");
    const result = resolveOfficialAssets(workflow, "image_to_video", inventory);
    expect(result.unresolvedSlots).toEqual(["video_vae"]);
    expect(
      ((result.workflow.nodes as Json[])[0]!.widgets_values as unknown[])[
        index
      ],
    ).toBe(value);
  });

  it("writes nested loader choices to the promoted top-level instance", () => {
    const { workflow, promotedIndex } = nestedTemplate();
    const before = structuredClone(workflow);
    const { inventory, chosen } = installedInventory("image_to_video");
    const result = resolveOfficialAssets(workflow, "image_to_video", inventory);
    const owner = (result.workflow.nodes as Json[])[0]!;
    const values = owner.widgets_values as unknown[];
    for (const spec of familySpecs("image_to_video")) {
      expect(values[promotedIndex.get(spec.slot)!]).toBe(chosen.get(spec.slot));
    }
    expect(result.changedSlots).toEqual(
      OFFICIAL_ASSET_MANIFEST.materializationFamilies.image_to_video,
    );
    expect(result.unresolvedSlots).toEqual([]);
    expect(workflow).toEqual(before);
  });

  it("writes r2v direct loaders without looking for a subgraph", () => {
    const workflow = directTemplate();
    const { inventory, chosen } = installedInventory("reference_to_video");
    const result = resolveOfficialAssets(
      workflow,
      "reference_to_video",
      inventory,
    );
    const values = (result.workflow.nodes as Json[]).map(
      (node) => (node.widgets_values as unknown[])[0],
    );
    expect(values).toEqual(
      familySpecs("reference_to_video").map((spec) => chosen.get(spec.slot)),
    );
  });

  it("leaves a truly absent role unchanged and reports it", () => {
    const workflow = directTemplate("image_to_video");
    const { inventory } = installedInventory("image_to_video", ["audio_vae"]);
    const result = resolveOfficialAssets(workflow, "image_to_video", inventory);
    expect(result.unresolvedSlots).toEqual(["audio_vae"]);
    const audioIndex = familySpecs("image_to_video").findIndex(
      (entry) => entry.slot === "audio_vae",
    );
    expect(
      (
        (result.workflow.nodes as Json[])[audioIndex]!
          .widgets_values as unknown[]
      )[0],
    ).toBe(familySpecs("image_to_video")[audioIndex]!.templateDefault);
  });

  it("preserves ambiguous loader values instead of guessing an asset", () => {
    const { workflow } = nestedTemplate();
    (workflow.nodes as Json[]).push(
      structuredClone((workflow.nodes as Json[])[0]!),
    );
    const before = structuredClone(workflow);
    const { inventory } = installedInventory("image_to_video");
    const result = resolveOfficialAssets(workflow, "image_to_video", inventory);
    expect(result.unresolvedSlots).toEqual(
      OFFICIAL_ASSET_MANIFEST.materializationFamilies.image_to_video,
    );
    expect(result.workflow).toEqual(before);
    expect(workflow).toEqual(before);
  });

  it("leaves a duplicate loader join unresolved instead of guessing one", () => {
    const { workflow } = nestedTemplate();
    const subgraph = ((workflow.definitions as Json).subgraphs as Json[])[0]!;
    const links = subgraph.links as Json[];
    const loraLink = links.find((link) => link.target_slot === 1)!;
    links.push({ ...loraLink });
    const { inventory } = installedInventory("image_to_video");
    const result = resolveOfficialAssets(workflow, "image_to_video", inventory);
    expect(result.unresolvedSlots).toEqual(["image_turbo_lora"]);
  });

  it("qualifies exact compiled COMBO strings and rejects basename reconstruction", () => {
    const { inventory } = installedInventory("image_to_video");
    const resolution = resolveOfficialAssets(
      directTemplate("image_to_video"),
      "image_to_video",
      inventory,
    );
    const output = Object.fromEntries(
      resolution.bindings.map((binding, index) => [
        String(index + 1),
        {
          class_type: binding.loaderType,
          inputs: { [binding.widgetName]: binding.value },
        },
      ]),
    );
    const compiled = { output, workflow: {} };
    expect(
      compiledPromptMatchesOfficialAssetResolution(compiled, resolution),
    ).toBe(true);
    const lora = resolution.bindings.find(
      (binding) => binding.slot === "image_turbo_lora",
    )!;
    const compiledLora = Object.values(output).find(
      (node) => node.class_type === lora.loaderType,
    )!;
    compiledLora.inputs[lora.widgetName] = lora.value
      .replaceAll("\\", "/")
      .split("/")
      .at(-1)!;
    expect(
      compiledPromptMatchesOfficialAssetResolution(compiled, resolution),
    ).toBe(false);
  });

  it("keeps the changed-default notice projection role-only", () => {
    const { inventory } = installedInventory("image_to_video");
    const resolution = resolveOfficialAssets(
      directTemplate("image_to_video"),
      "image_to_video",
      inventory,
    );
    expect(resolution.changedSlots).toContain("image_turbo_lora");
    expect(
      resolution.selections.every(
        (selection) =>
          Object.keys(selection).sort().join(",") === "reason,slot",
      ),
    ).toBe(true);
    expect(JSON.stringify(resolution.selections)).not.toContain(".safetensors");
  });

  it("reads only exact host combo inventories and rejects malformed ones", () => {
    const output = readOfficialAssetInventory({
      UNETLoader: {
        input: { required: { unet_name: [["Exact\\HostValue.bin"]] } },
      },
      CLIPLoader: {
        input: { required: { clip_name: [["ExactClip.bin"]] } },
      },
      VAELoader: {
        input: { required: { vae_name: [["ExactVae.bin"]] } },
      },
      LoraLoaderModelOnly: {
        input: { required: { lora_name: [["ExactLora.bin"]] } },
      },
    });
    expect(output.diffusion_models).toEqual(["Exact\\HostValue.bin"]);
    expect(output.text_encoders).toEqual(["ExactClip.bin"]);
    expect(output.vae).toEqual(["ExactVae.bin"]);
    expect(() =>
      readOfficialAssetInventory({
        UNETLoader: { input: { required: { unet_name: ["not-an-array"] } } },
      }),
    ).toThrowError(
      expect.objectContaining<Partial<OfficialAssetResolutionError>>({
        code: "invalid_host_inventory",
      }),
    );
  });

  it("rejects unsafe values at the host combo boundary", () => {
    expect(() =>
      readOfficialAssetInventory(
        nodeDefinitionPayloads({
          diffusion_models: ["..\\minimax_h3_fl2va_bf16.safetensors"],
          text_encoders: ["ExactClip.bin"],
          vae: ["ExactVae.bin"],
        }),
      ),
    ).toThrowError(
      expect.objectContaining<Partial<OfficialAssetResolutionError>>({
        code: "invalid_host_inventory",
      }),
    );
  });
});

it("reads the unique loaded node-definition records lazily", () => {
  const { inventory } = installedInventory("image_to_video");
  expect(
    readOfficialAssetInventoryFromNodeDefinitions(nodeDefinitions(inventory)),
  ).toEqual(inventory);
});

it("enforces the derived collection ceiling before inspecting hostile entries", () => {
  const { inventory } = installedInventory("image_to_video");
  const base = nodeDefinitions(inventory);
  const exactCeiling = Object.fromEntries(
    Array.from(
      { length: 12_762 - Object.keys(base).length },
      (_value, index) => [
        `synthetic/${index}`,
        { nodeData: { name: `Synthetic${index}` } },
      ],
    ),
  );
  Object.assign(exactCeiling, base);
  expect(() =>
    readOfficialAssetInventoryFromNodeDefinitions(exactCeiling),
  ).not.toThrow();

  let inspected = false;
  const hostile = Object.fromEntries(
    Array.from({ length: 12_763 }, (_value, index) => [
      `hostile/${index}`,
      Object.defineProperty({}, "nodeData", {
        get() {
          inspected = true;
          return {};
        },
      }),
    ]),
  );
  expect(() =>
    readOfficialAssetInventoryFromNodeDefinitions(hostile),
  ).toThrowError(
    expect.objectContaining<Partial<OfficialAssetResolutionError>>({
      code: "invalid_host_inventory",
    }),
  );
  expect(inspected).toBe(false);
});

describe("M17-29 App Mode queue truth", () => {
  const inputs = {
    task_mode: "t2va" as const,
    user_intent: "A safe bounded intent for the resolution route.",
    duration_milliseconds: 5167,
    frame_count: 124,
  };

  function host(inventory: OfficialAssetInventory) {
    const output = createQualifiedBasePrompt(
      inputs,
      {},
      {
        sharedDurationSource: true,
      },
    ) as Record<string, Json>;
    for (const spec of familySpecs("image_to_video")) {
      const selected = selectOfficialAsset(
        spec,
        inventory[spec.folderCategory] ?? [],
      );
      if (selected === undefined) continue;
      output[`asset-${spec.slot}`] = {
        class_type: spec.loaderType,
        inputs: { [spec.widgetName]: selected.value },
      };
    }
    return fixtureBackedAppModeHost(
      {
        loadApiJson: vi.fn(),
        loadGraphData: vi.fn(),
        graphToPrompt: vi.fn().mockResolvedValue({
          output,
          workflow: {},
        }),
        graph: { serialize: () => ({ nodes: [] }) },
      },
      {
        queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(1)),
      },
    );
  }

  it("queues a relocated family after every official role is resolved", async () => {
    const { inventory, chosen } = installedInventory("image_to_video");
    const { app, api } = host(inventory);
    await createAppModeController(app, api, {
      loadTemplate: templateWithDirectLoaders,
      loadProfile: () => generationProfile({ text: ASSET_RELOCATED }),
      readNodeDefinitions: () => nodeDefinitions(inventory),
    }).start(inputs);
    expect(app.loadGraphData).toHaveBeenCalledTimes(1);
    expect(api.queuePrompt).toHaveBeenCalledTimes(1);
    const loaded = app.loadGraphData.mock.calls[0]![0] as Json;
    for (const spec of familySpecs("image_to_video")) {
      const loader = (loaded.nodes as Json[]).find(
        (node) =>
          node.type === spec.loaderType &&
          (node.widgets_values as unknown[])?.[0] === chosen.get(spec.slot),
      );
      expect(loader, spec.slot).toBeDefined();
    }
  });

  it("queues a missing-profile family after live inventory resolves every role", async () => {
    const { inventory, chosen } = installedInventory("image_to_video");
    const { app, api } = host(inventory);
    await createAppModeController(app, api, {
      loadTemplate: templateWithDirectLoaders,
      loadProfile: () => generationProfile({ text: MISSING_ASSET }),
      readNodeDefinitions: () => nodeDefinitions(inventory),
    }).start(inputs);
    expect(app.loadGraphData).toHaveBeenCalledTimes(1);
    expect(api.queuePrompt).toHaveBeenCalledTimes(1);
    const loaded = app.loadGraphData.mock.calls[0]![0] as Json;
    for (const spec of familySpecs("image_to_video")) {
      const loader = (loaded.nodes as Json[]).find(
        (node) =>
          node.type === spec.loaderType &&
          (node.widgets_values as unknown[])?.[0] === chosen.get(spec.slot),
      );
      expect(loader, spec.slot).toBeDefined();
    }
  });

  it.each([ASSET_RELOCATED, MISSING_ASSET, undefined])(
    "submits unresolved weights to ComfyUI instead of refusing the candidate (%j)",
    async (profileOverride) => {
      const { inventory } = installedInventory("image_to_video", ["audio_vae"]);
      const { app, api } = host(inventory);
      const result = await createAppModeController(app, api, {
        loadTemplate: templateWithDirectLoaders,
        loadProfile: () => generationProfile({ text: profileOverride }),
        readNodeDefinitions: () => nodeDefinitions(inventory),
      }).start(inputs);
      expect(result.route).toBe("new");
      expect(app.loadGraphData).toHaveBeenCalledOnce();
      expect(api.queuePrompt).toHaveBeenCalledOnce();
      const loaded = app.loadGraphData.mock.calls[0]![0] as Json;
      const audio = familySpecs("image_to_video").find(
        (spec) => spec.slot === "audio_vae",
      )!;
      expect(
        (loaded.nodes as Json[]).some(
          (node) =>
            node.type === audio.loaderType &&
            (node.widgets_values as unknown[])?.[0] === audio.templateDefault,
        ),
      ).toBe(true);
    },
  );

  it("preserves the native ComfyUI queue rejection for an unresolved weight", async () => {
    const { inventory } = installedInventory("image_to_video", ["audio_vae"]);
    const { app, api } = host(inventory);
    api.queuePrompt.mockRejectedValue(rejectedQueueError());
    await expect(
      createAppModeController(app, api, {
        loadTemplate: templateWithDirectLoaders,
        loadProfile: () => generationProfile({ text: MISSING_ASSET }),
        readNodeDefinitions: () => nodeDefinitions(inventory),
      }).start(inputs),
    ).rejects.toMatchObject({
      code: "queue_failed",
      reason: { kind: "queue_rejected" },
    });
    expect(api.queuePrompt).toHaveBeenCalledOnce();
    expect(app.loadGraphData).toHaveBeenCalledOnce();
  });

  it("does not qualify model names rewritten by the host compiler", async () => {
    const { inventory } = installedInventory("image_to_video");
    const { app, api } = host(inventory);
    const compiled = await app.graphToPrompt();
    const output = compiled.output as Record<string, Json>;
    (output["asset-video_vae"]!.inputs as Json).vae_name =
      "user-chosen-video-vae.safetensors";
    app.graphToPrompt.mockResolvedValue(compiled);
    await createAppModeController(app, api, {
      loadTemplate: templateWithDirectLoaders,
      loadProfile: () => generationProfile(),
      readNodeDefinitions: () => nodeDefinitions(inventory),
    }).start(inputs);
    expect(api.queuePrompt).toHaveBeenCalledOnce();
  });

  it.each(["absent", "invalid"])(
    "submits template defaults when optional inventory is %s",
    async (kind) => {
      const { inventory } = installedInventory("image_to_video");
      const { app, api } = host(inventory);
      const definitions = nodeDefinitions(inventory);
      if (kind === "invalid") {
        definitions["loaders/VAELoader"] = {
          nodeData: {
            name: "VAELoader",
            input: { required: { vae_name: [["../unsafe.safetensors"]] } },
          },
        };
      }
      await createAppModeController(app, api, {
        loadTemplate: templateWithDirectLoaders,
        loadProfile: () => generationProfile({ text: ASSET_RELOCATED }),
        readNodeDefinitions: () =>
          kind === "absent" ? undefined : definitions,
      }).start(inputs);
      expect(app.loadGraphData).toHaveBeenCalledOnce();
      expect(api.queuePrompt).toHaveBeenCalledOnce();
      const loaded = app.loadGraphData.mock.calls[0]![0] as Json;
      expect(JSON.stringify(loaded)).not.toContain("../unsafe.safetensors");
    },
  );

  it("fails closed when the node registry is present before host readiness", async () => {
    const { inventory } = installedInventory("image_to_video");
    const { app, api } = host(inventory);
    await expect(
      createAppModeController(app, api, {
        loadTemplate: templateWithDirectLoaders,
        loadProfile: () => generationProfile(),
        readNodeDefinitions: () => nodeDefinitions(inventory),
        nodeDefinitionReadinessReached: () => false,
      }).start(inputs),
    ).rejects.toMatchObject({ code: "incompatible_seam" });
    expect(app.loadGraphData).not.toHaveBeenCalled();
    expect(api.queuePrompt).not.toHaveBeenCalled();
  });
});

describe("M17-29 disclosure copy", () => {
  it.each(SUPPORTED_LOCALES)(
    "discloses automatic official resolution (%s)",
    (locale) => {
      const copy = sidebarCopy(locale).generation;
      expect(copy.assetRelocated).toMatch(/default|預設|默认/i);
      expect(copy.assetRelocated).toMatch(/official|官方/i);
      expect(copy.assetRelocated).toMatch(/canvas|畫布|画布/i);
      expect(copy.missingAsset).toMatch(/official|官方/i);
    },
  );
});
