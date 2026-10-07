import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  CONTEXT_PIPELINE,
  FAMILY_ANCHOR,
  I2VA_SCALE_WIDGET_VALUES,
  MODE_TEMPLATE,
  OFFICIAL_LENGTH_EXPRESSION,
  TASK_MODE_FAMILY,
  TemplateSpliceError,
  rebindExistingContextAuthoring,
  resolveAnchor,
  spliceContextPipeline,
  type Json,
} from "../src/host/templateMaterialization";
import { syntheticTemplate } from "./support/templateFixture";

/**
 * M17-20 phase 2. Two kinds of row live here on purpose.
 *
 * The synthetic fixtures reproduce the two serialized shapes the pinned
 * templates use, so the rules are asserted on every machine. The real-bytes rows
 * run the same splice against the vendored pinned corpus when it is present,
 * because a rule about a serialized shape that has only ever been tested against
 * a fixture of that shape is a rule about the fixture.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..");
const TEMPLATES = join(
  REPO_ROOT,
  "reference",
  "rm02",
  "official",
  "workflow_templates",
  "templates",
);

const corpusPresent = existsSync(TEMPLATES);

function pinned(name: string): Json {
  return JSON.parse(
    readFileSync(join(TEMPLATES, `${name}.json`), "utf8"),
  ) as Json;
}

/** A subgraph-instance anchor, as the frame-driven templates ship it. */
function nestedFixture(): Json {
  return {
    last_node_id: 10,
    last_link_id: 20,
    nodes: [
      {
        id: 9,
        type: "PrimitiveInt",
        inputs: [],
        outputs: [{ name: "INT", type: "INT", links: [20] }],
        widgets_values: [1344],
      },
      {
        id: 10,
        type: "sub-definition-id",
        inputs: [
          { name: "first_frame", type: "IMAGE", link: null },
          { name: "last_frame", type: "IMAGE", link: null },
          { name: "width", type: "INT", widget: { name: "width" }, link: 20 },
        ],
        outputs: [{ name: "VIDEO", type: "VIDEO", links: [] }],
        widgets_values: [
          "template prompt",
          "1344",
          "768",
          2,
          7,
          "u",
          "c",
          "v",
          "a",
        ],
      },
    ],
    links: [[20, 9, 0, 10, 2, "INT"]],
    definitions: {
      subgraphs: [
        {
          id: "sub-definition-id",
          name: "Image to Video (MiniMax H3)",
          inputs: [
            { name: "first_frame", type: "IMAGE" },
            { name: "last_frame", type: "IMAGE" },
            { name: "prompt", type: "STRING" },
            { name: "width", type: "INT" },
            { name: "height", type: "INT" },
            { name: "value_1", type: "FLOAT" },
            { name: "noise_seed", type: "INT" },
            { name: "unet_name", type: "COMBO" },
            { name: "clip_name", type: "COMBO" },
            { name: "vae_name", type: "COMBO" },
            { name: "vae_name_1", type: "COMBO" },
          ],
          outputs: [{ name: "VIDEO", type: "VIDEO" }],
          nodes: [
            {
              id: 1,
              type: "MiniMaxH3ImageToVideo",
              inputs: [{ name: "length", type: "INT", link: 199 }],
              outputs: [],
              widgets_values: ["", 1344, 768, 124],
            },
            {
              id: 2,
              type: "PrimitiveFloat",
              inputs: [{ name: "value", type: "FLOAT", link: 206 }],
              outputs: [{ name: "FLOAT", type: "FLOAT", links: [205] }],
              widgets_values: [2],
            },
            {
              id: 3,
              type: "ComfyMathExpression",
              inputs: [{ name: "values.a", type: "FLOAT", link: 205 }],
              outputs: [
                { name: "FLOAT", type: "FLOAT", links: [] },
                { name: "INT", type: "INT", links: [199] },
              ],
              widgets_values: [OFFICIAL_LENGTH_EXPRESSION],
            },
          ],
          links: [
            {
              id: 206,
              origin_id: -10,
              origin_slot: 5,
              target_id: 2,
              target_slot: 0,
              type: "FLOAT",
            },
            {
              id: 205,
              origin_id: 2,
              origin_slot: 0,
              target_id: 3,
              target_slot: 0,
              type: "FLOAT",
            },
            {
              id: 199,
              origin_id: 3,
              origin_slot: 1,
              target_id: 1,
              target_slot: 0,
              type: "INT",
            },
          ],
        },
      ],
    },
  };
}

/** A flat anchor whose length is driven by a primitive through a math node. */
function flatFixture(): Json {
  return {
    last_node_id: 5,
    last_link_id: 5,
    nodes: [
      {
        id: 1,
        type: "MiniMaxH3ReferenceToVideo",
        inputs: [
          {
            name: "prompt",
            type: "STRING",
            widget: { name: "prompt" },
            link: 3,
          },
          { name: "length", type: "INT", widget: { name: "length" }, link: 2 },
        ],
        outputs: [{ name: "positive", type: "CONDITIONING", links: [] }],
        widgets_values: ["", 1344, 768, 124, "match"],
      },
      {
        id: 2,
        type: "ComfyMathExpression",
        inputs: [{ name: "values.a", type: "FLOAT", link: 1 }],
        outputs: [
          { name: "FLOAT", type: "FLOAT", links: [] },
          { name: "INT", type: "INT", links: [2] },
        ],
        widgets_values: ["max(5, round(a * 24))"],
      },
      {
        id: 3,
        type: "PrimitiveFloat",
        inputs: [],
        outputs: [{ name: "FLOAT", type: "FLOAT", links: [1] }],
        widgets_values: [5],
      },
      {
        id: 4,
        type: "PrimitiveStringMultiline",
        inputs: [],
        outputs: [{ name: "STRING", type: "STRING", links: [3] }],
        widgets_values: ["the template's own prompt text"],
      },
    ],
    links: [
      [1, 3, 0, 2, 0, "FLOAT"],
      [2, 2, 1, 1, 1, "INT"],
      [3, 4, 0, 1, 0, "STRING"],
    ],
  };
}

const OPTIONS = {
  taskMode: "t2va",
  userIntent: "A red kite crosses the sky.",
  durationSeconds: 5.167,
} as const;

type Link = [number, number, number, number, number, string];

function links(workflow: Json): Link[] {
  return (workflow.links as Link[]) ?? [];
}

function nodes(workflow: Json): Json[] {
  return (workflow.nodes as Json[]) ?? [];
}

const INFERENCE_SENTINELS = Object.freeze({
  baseSteps: 37,
  turboSteps: 73,
  samplerName: "canvas-sampler",
  scheduler: "canvas-scheduler",
  cfg: 6.75,
  denoise: 0.41,
  seed: 123456789,
  noiseSeed: 987654321,
  loraName: "canvas-lora",
  loraStrength: 0.62,
});

/**
 * Add an opaque, user-owned sampler branch to the nested graph. The values are
 * deliberately unlike the official 20/8 pair so a hard-coded App Mode default
 * cannot satisfy the preservation contract by accident.
 */
function nestedInferenceFixture(turboMode: boolean): Json {
  const workflow = nestedFixture();
  const anchor = nodes(workflow).find((node) => node.id === 10)!;
  anchor.canvas_inference_sentinel = {
    turbo_mode: turboMode,
    ...INFERENCE_SENTINELS,
  };
  const definitions = workflow.definitions as Json;
  const definition = (definitions.subgraphs as Json[])[0]!;
  const definitionNodes = definition.nodes as Json[];
  const definitionLinks = definition.links as Json[];

  definitionNodes.push(
    {
      id: 41,
      type: "PrimitiveInt",
      inputs: [],
      outputs: [{ name: "INT", type: "INT", links: [301] }],
      widgets_values: [INFERENCE_SENTINELS.baseSteps],
      properties: { canvas_role: "base_steps" },
    },
    {
      id: 42,
      type: "PrimitiveInt",
      inputs: [],
      outputs: [{ name: "INT", type: "INT", links: [302] }],
      widgets_values: [INFERENCE_SENTINELS.turboSteps],
      properties: { canvas_role: "turbo_steps" },
    },
    {
      id: 43,
      type: "PrimitiveBoolean",
      inputs: [],
      outputs: [{ name: "BOOLEAN", type: "BOOLEAN", links: [303] }],
      widgets_values: [turboMode],
      properties: { canvas_role: "turbo_mode" },
    },
    {
      id: 44,
      type: "ComfySwitchNode",
      inputs: [
        { name: "on_false", type: "INT", link: 301 },
        { name: "on_true", type: "INT", link: 302 },
        { name: "switch", type: "BOOLEAN", link: 303 },
      ],
      outputs: [{ name: "INT", type: "INT", links: [304] }],
      widgets_values: [],
      properties: { canvas_role: "steps_branch" },
    },
    {
      id: 45,
      type: "BasicScheduler",
      inputs: [{ name: "steps", type: "INT", link: 304 }],
      outputs: [{ name: "SIGMAS", type: "SIGMAS", links: [305] }],
      widgets_values: [INFERENCE_SENTINELS.scheduler, 0.73, 32],
      properties: { canvas_role: "scheduler" },
    },
    {
      id: 46,
      type: "UserInferenceSentinel",
      inputs: [{ name: "sigmas", type: "SIGMAS", link: 305 }],
      outputs: [],
      widgets_values: [],
      properties: {
        inference: {
          sampler_name: INFERENCE_SENTINELS.samplerName,
          scheduler: INFERENCE_SENTINELS.scheduler,
          cfg: INFERENCE_SENTINELS.cfg,
          denoise: INFERENCE_SENTINELS.denoise,
          seed: INFERENCE_SENTINELS.seed,
          noise_seed: INFERENCE_SENTINELS.noiseSeed,
          lora_name: INFERENCE_SENTINELS.loraName,
          lora_strength: INFERENCE_SENTINELS.loraStrength,
        },
      },
    },
  );
  definitionLinks.push(
    {
      id: 301,
      origin_id: 41,
      origin_slot: 0,
      target_id: 44,
      target_slot: 0,
      type: "INT",
    },
    {
      id: 302,
      origin_id: 42,
      origin_slot: 0,
      target_id: 44,
      target_slot: 1,
      type: "INT",
    },
    {
      id: 303,
      origin_id: 43,
      origin_slot: 0,
      target_id: 44,
      target_slot: 2,
      type: "BOOLEAN",
    },
    {
      id: 304,
      origin_id: 44,
      origin_slot: 0,
      target_id: 45,
      target_slot: 0,
      type: "INT",
    },
    {
      id: 305,
      origin_id: 45,
      origin_slot: 0,
      target_id: 46,
      target_slot: 0,
      type: "SIGMAS",
    },
  );
  return workflow;
}

type InferenceProjection = {
  turboMode: boolean;
  baseSteps: number;
  turboSteps: number;
  effectiveSteps: number;
  schedulerWidgets: unknown[];
  sentinelProperties: Json;
};

function nestedInferenceProjection(workflow: Json): InferenceProjection {
  const definitions = workflow.definitions as Json;
  const definition = (definitions.subgraphs as Json[])[0]!;
  const definitionNodes = definition.nodes as Json[];
  const byId = new Map(
    definitionNodes.map((node) => [String(node.id), node] as const),
  );
  const getNode = (id: number): Json => {
    const node = byId.get(String(id));
    if (node === undefined) throw new Error(`missing inference node ${id}`);
    return node;
  };
  const definitionLinks = definition.links as Json[];
  const getLink = (id: unknown): Json => {
    const link = definitionLinks.find((candidate) => candidate.id === id);
    if (link === undefined)
      throw new Error(`missing inference link ${String(id)}`);
    return link;
  };
  const stepsInput = (getNode(45).inputs as Json[]).find(
    (input) => input.name === "steps",
  )!;
  const schedulerEdge = getLink(stepsInput.link);
  const switchNode = byId.get(String(schedulerEdge.origin_id))!;
  const turboMode = Boolean((getNode(43).widgets_values as unknown[])[0]);
  const branchInput = (switchNode.inputs as Json[]).find(
    (input) => input.name === (turboMode ? "on_true" : "on_false"),
  )!;
  const branchEdge = getLink(branchInput.link);
  const selectedBranch = byId.get(String(branchEdge.origin_id))!;
  const properties = getNode(46).properties as Json;
  return {
    turboMode,
    baseSteps: Number((getNode(41).widgets_values as unknown[])[0]),
    turboSteps: Number((getNode(42).widgets_values as unknown[])[0]),
    effectiveSteps: Number((selectedBranch.widgets_values as unknown[])[0]),
    schedulerWidgets: structuredClone(getNode(45).widgets_values as unknown[]),
    sentinelProperties: structuredClone(properties),
  };
}

function i2vaTemplateWithVisibleSizeBranch(): Json {
  const workflow = syntheticTemplate("video_minimax_h3_i2v");
  const workflowNodes = nodes(workflow);
  const workflowLinks = links(workflow);
  const anchor = workflowNodes.find(
    (node) => node.type === "MiniMaxH3ImageToVideo",
  )!;
  const anchorInputs = anchor.inputs as Json[];
  anchorInputs.find((input) => input.name === "width")!.link = 22;
  anchorInputs.find((input) => input.name === "height")!.link = 23;
  workflowNodes.push(
    {
      id: 11,
      type: "ResolutionSelector",
      inputs: [],
      outputs: [
        { name: "width", type: "INT", links: [22] },
        { name: "height", type: "INT", links: [23] },
      ],
      widgets_values: ["16:9 (Widescreen)", 0.4, 32],
    },
    {
      id: 12,
      type: "ImageScaleToTotalPixels",
      inputs: [{ name: "image", type: "IMAGE", link: null }],
      outputs: [{ name: "IMAGE", type: "IMAGE", links: [24] }],
      widgets_values: ["bilinear", 0.4, 16],
    },
    {
      id: 13,
      type: "GetImageSize",
      inputs: [{ name: "image", type: "IMAGE", link: 24 }],
      outputs: [
        { name: "width", type: "INT", links: [] },
        { name: "height", type: "INT", links: [] },
        { name: "batch_size", type: "INT", links: [] },
      ],
      widgets_values: [],
    },
  );
  workflowLinks.push(
    [22, 11, 0, 3, 3, "INT"],
    [23, 11, 1, 3, 4, "INT"],
    [24, 12, 0, 13, 0, "IMAGE"],
  );
  workflow.last_node_id = 13;
  workflow.last_link_id = 24;
  return workflow;
}

describe("M17-20 template materialization", () => {
  it("does not carry a second conditioning node into the template", () => {
    // The shipped Base Assistant subgraph contains its own
    // `MiniMaxH3ImageToVideo`; splicing that package would leave two anchors on
    // one canvas. The pipeline declared here contributes the prompt and nothing
    // that could be mistaken for the template's own generation node.
    const types = CONTEXT_PIPELINE.map((node) => node.type);
    expect(types).not.toContain("MiniMaxH3ImageToVideo");
    expect(types).not.toContain("MiniMaxH3ReferenceToVideo");
    expect(new Set(types).size).toBe(types.length);
  });

  it("resolves an anchor nested inside a subgraph definition", () => {
    const resolved = resolveAnchor(nestedFixture(), "image_to_video");
    expect(resolved.nested).toBe(true);
    expect(resolved.node.id).toBe(10);
  });

  it("resolves a flat anchor without a subgraph definition", () => {
    const resolved = resolveAnchor(flatFixture(), "reference_to_video");
    expect(resolved.nested).toBe(false);
    expect(resolved.node.type).toBe(FAMILY_ANCHOR.reference_to_video);
  });

  it("fails closed on zero anchors and on an undesignated multi-anchor graph", () => {
    expect(() =>
      resolveAnchor({ nodes: [], links: [] }, "image_to_video"),
    ).toThrow(TemplateSpliceError);
    const twin = nestedFixture();
    const nestedAnchor = (twin.nodes as Json[]).find((node) => node.id === 10)!;
    (twin.nodes as Json[]).push({
      ...nestedAnchor,
      id: 11,
    });
    expect(() => resolveAnchor(twin, "image_to_video")).toThrowError(
      /candidate anchors/,
    );
  });

  it("adds a prompt input to a subgraph anchor that only had a widget", () => {
    const result = spliceContextPipeline(nestedFixture(), OPTIONS);
    const anchor = nodes(result.workflow).find((node) => node.id === 10)!;
    const inputs = anchor.inputs as Json[];
    const prompt = inputs.find((input) => input.name === "prompt")!;
    expect(prompt).toBeDefined();
    expect(prompt.type).toBe("STRING");
    // The widget marker has to survive: a promoted widget input that loses it
    // stops being editable when the link is removed again.
    expect(prompt.widget).toEqual({ name: "prompt" });

    const edge = links(result.workflow).find(
      (link) => link[0] === prompt.link,
    )!;
    expect(edge[1]).toBe(result.promptNodeId);
    expect(edge[3]).toBe(10);
    expect(edge[4]).toBe(inputs.indexOf(prompt));
    expect(edge[5]).toBe("STRING");
  });

  it("writes the authored duration into the promoted duration widget", () => {
    const result = spliceContextPipeline(nestedFixture(), OPTIONS);
    const anchor = nodes(result.workflow).find((node) => node.id === 10)!;
    // `value_1` is the fourth widget-backed promoted input, because the two
    // IMAGE inputs carry no widget. The index is derived from the definition
    // rather than hard-coded, so a template that promotes a new input still
    // lands the duration on the right control.
    expect((anchor.widgets_values as unknown[])[3]).toBe(5.167);
    expect(result.durationApplied).toBe(true);
    // The anchor's own length is untouched: the template drives it through the
    // official expression, and writing a frame count here would be a second
    // alignment authority.
    expect((anchor.inputs as Json[]).some((i) => i.name === "length")).toBe(
      false,
    );

    const requestType = CONTEXT_PIPELINE.find(
      (spec) => spec.key === "request",
    )!.type;
    const request = nodes(result.workflow).find(
      (node) => node.type === requestType,
    )!;
    const durationInput = (request.inputs as Json[]).find(
      (input) => input.name === "duration_seconds",
    )!;
    const promoted = (anchor.inputs as Json[]).find(
      (input) => input.name === "value_1",
    )!;
    const requestEdge = links(result.workflow).find(
      (link) => link[0] === durationInput.link,
    )!;
    const promotedEdge = links(result.workflow).find(
      (link) => link[0] === promoted.link,
    )!;
    expect(requestEdge[1]).toBe(promotedEdge[1]);
    const source = nodes(result.workflow).find(
      (node) => Number(node.id) === requestEdge[1],
    )!;
    expect(source.type).toBe("PrimitiveFloat");
    expect(source.widgets_values).toEqual([5.167]);
  });

  it("routes a prepared canonical prompt through one typed AuditOverride before Validator", () => {
    const lowering = {
      baseReportFingerprint: `sha256:${"7".repeat(64)}`,
      baseReportRevision: 0,
      overrideRevision: 1,
      reason: "Materialize approved Production segment prompt" as const,
    };
    const result = spliceContextPipeline(nestedFixture(), {
      ...OPTIONS,
      canonicalLowering: lowering,
    });
    const workflowNodes = nodes(result.workflow);
    const compiler = workflowNodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.Compiler",
    )!;
    const audit = workflowNodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.AuditOverride",
    )!;
    const validator = workflowNodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.Validator",
    )!;
    const request = workflowNodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.Request",
    )!;
    const auditReport = (audit.inputs as Json[]).find(
      (input) => input.name === "report",
    )!;
    const validatorDocument = (validator.inputs as Json[]).find(
      (input) => input.name === "prompt_document",
    )!;
    const auditReportLink = links(result.workflow).find(
      (link) => link[0] === auditReport.link,
    )!;
    const validatorDocumentLink = links(result.workflow).find(
      (link) => link[0] === validatorDocument.link,
    )!;

    expect(
      workflowNodes.filter(
        (node) => node.type === "comfyui_h3_context.H3Context.AuditOverride",
      ),
    ).toHaveLength(1);
    expect(audit.widgets_values).toEqual([
      lowering.baseReportFingerprint,
      lowering.overrideRevision,
      lowering.reason,
      OPTIONS.userIntent,
    ]);
    expect(auditReportLink.slice(1, 5)).toEqual([
      compiler.id,
      1,
      audit.id,
      (audit.inputs as Json[]).indexOf(auditReport),
    ]);
    expect(validatorDocumentLink.slice(1, 5)).toEqual([
      audit.id,
      3,
      validator.id,
      (validator.inputs as Json[]).indexOf(validatorDocument),
    ]);
    expect(result.authoredWidgetNodeIds).toEqual(
      expect.arrayContaining([Number(request.id), Number(audit.id)]),
    );
  });

  it("keeps ordinary materialization on the direct Compiler document route", () => {
    const result = spliceContextPipeline(nestedFixture(), OPTIONS);
    const workflowNodes = nodes(result.workflow);
    const compiler = workflowNodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.Compiler",
    )!;
    const validator = workflowNodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.Validator",
    )!;
    const promptDocument = (validator.inputs as Json[]).find(
      (input) => input.name === "prompt_document",
    )!;
    const edge = links(result.workflow).find(
      (link) => link[0] === promptDocument.link,
    )!;

    expect(
      workflowNodes.some(
        (node) => node.type === "comfyui_h3_context.H3Context.AuditOverride",
      ),
    ).toBe(false);
    expect(edge.slice(1, 5)).toEqual([
      compiler.id,
      2,
      validator.id,
      (validator.inputs as Json[]).indexOf(promptDocument),
    ]);
  });

  it("rebinds only the owned fields of a nested existing H3 graph", () => {
    const materialized = spliceContextPipeline(nestedFixture(), OPTIONS);
    const before = structuredClone(materialized.workflow);
    const rebound = rebindExistingContextAuthoring(materialized.workflow, {
      taskMode: "t2va",
      userIntent: "A synthetic nested existing-canvas request.",
      durationSeconds: 8,
    });
    const request = nodes(rebound.workflow).find(
      (node) => node.type === "comfyui_h3_context.H3Context.Request",
    )!;
    const durationInput = (request.inputs as Json[]).find(
      (input) => input.name === "duration_seconds",
    )!;
    const durationEdge = links(rebound.workflow).find(
      (edge) => edge[0] === durationInput.link,
    )!;
    const source = nodes(rebound.workflow).find(
      (node) => String(node.id) === String(durationEdge[1]),
    )!;
    expect(request.widgets_values).toEqual([
      "t2va",
      "A synthetic nested existing-canvas request.",
      5.167,
    ]);
    expect(source.widgets_values).toEqual([8]);
    expect(rebound).toMatchObject({
      compiledAnchorNodeId: `${String(rebound.anchorNodeId)}:1`,
      anchorNodeType: "MiniMaxH3ImageToVideo",
      productShellNodeId: expect.any(String),
    });

    request.widgets_values = ["t2va", OPTIONS.userIntent, 5.167];
    source.widgets_values = [OPTIONS.durationSeconds];
    expect(rebound.workflow).toEqual(before);
  });

  it.each([
    { turboMode: false, expectedEffectiveSteps: INFERENCE_SENTINELS.baseSteps },
    { turboMode: true, expectedEffectiveSteps: INFERENCE_SENTINELS.turboSteps },
  ] as const)(
    "preserves nested canvas inference state when turbo_mode=$turboMode",
    ({ turboMode, expectedEffectiveSteps }) => {
      const source = nestedInferenceFixture(turboMode);
      const sourceInference = nestedInferenceProjection(source);
      const materialized = spliceContextPipeline(source, OPTIONS);
      const before = structuredClone(materialized.workflow);
      const beforeInference = nestedInferenceProjection(before);
      // The template splice treats the sampler branch as opaque canvas state;
      // taking this baseline before the splice catches broad widget rewrites.
      expect(beforeInference).toEqual(sourceInference);
      expect(beforeInference).toMatchObject({
        turboMode,
        baseSteps: INFERENCE_SENTINELS.baseSteps,
        turboSteps: INFERENCE_SENTINELS.turboSteps,
        effectiveSteps: expectedEffectiveSteps,
      });

      const rebound = rebindExistingContextAuthoring(materialized.workflow, {
        taskMode: "t2va",
        userIntent: "A bounded inference-authority regression.",
        durationSeconds: 8,
      });

      // The selected branch is computed from the preserved canvas switch and
      // links. App Mode must not repair either branch by writing an official
      // 20/8 value or by silently enabling Turbo.
      expect(nestedInferenceProjection(rebound.workflow)).toEqual(
        beforeInference,
      );
      expect(nestedInferenceProjection(rebound.workflow).effectiveSteps).toBe(
        expectedEffectiveSteps,
      );

      const normalized = structuredClone(rebound.workflow);
      const baselineRequest = nodes(before).find(
        (node) => node.type === "comfyui_h3_context.H3Context.Request",
      )!;
      const baselineDurationSource = nodes(before).find(
        (node) =>
          node.type === "PrimitiveFloat" &&
          (node.widgets_values as unknown[] | undefined)?.[0] ===
            OPTIONS.durationSeconds,
      )!;
      const reboundRequest = nodes(normalized).find(
        (node) => node.type === "comfyui_h3_context.H3Context.Request",
      )!;
      const reboundDurationSource = nodes(normalized).find(
        (node) =>
          node.type === "PrimitiveFloat" &&
          (node.widgets_values as unknown[] | undefined)?.[0] === 8,
      )!;
      reboundRequest.widgets_values = structuredClone(
        baselineRequest.widgets_values,
      );
      reboundDurationSource.widgets_values = structuredClone(
        baselineDurationSource.widgets_values,
      );

      // Restoring context intent and the sole permitted generation override,
      // duration, must recover the complete workflow and nested bytes.
      expect(normalized).toEqual(before);
      expect(rebound.workflow.links).toEqual(before.links);
      expect((rebound.workflow.definitions as Json).subgraphs).toEqual(
        (before.definitions as Json).subgraphs,
      );
    },
  );

  it("resolves the requested existing anchor without rejecting an unrelated H3 family", () => {
    const materialized = spliceContextPipeline(nestedFixture(), OPTIONS);
    const workflow = structuredClone(materialized.workflow);
    const extraId = Number(workflow.last_node_id) + 1;
    (workflow.nodes as Json[]).push({
      id: extraId,
      type: "MiniMaxH3ReferenceToVideo",
      inputs: [],
      outputs: [],
      widgets_values: [],
    });
    workflow.last_node_id = extraId;

    expect(
      rebindExistingContextAuthoring(workflow, {
        taskMode: "t2va",
        userIntent: "Choose the requested image-family anchor only.",
        durationSeconds: 8,
      }).anchorNodeId,
    ).toBe(String(materialized.anchorNodeId));
  });

  it("leaves an existing anchor's unbound prompt socket outside admission", () => {
    const materialized = spliceContextPipeline(nestedFixture(), OPTIONS);
    const workflow = structuredClone(materialized.workflow);
    const anchor = nodes(workflow).find(
      (node) => String(node.id) === String(materialized.anchorNodeId),
    )!;
    const prompt = (anchor.inputs as Json[]).find(
      (input) => input.name === "prompt",
    )!;
    const promptLink = Number(prompt.link);
    prompt.link = null;
    workflow.links = links(workflow).filter((edge) => edge[0] !== promptLink);
    for (const node of nodes(workflow))
      for (const output of (node.outputs as Json[] | undefined) ?? [])
        if (Array.isArray(output.links))
          output.links = output.links.filter((value) => value !== promptLink);

    const rebound = rebindExistingContextAuthoring(workflow, {
      taskMode: "t2va",
      userIntent: "Prompt topology remains owned by the user and host.",
      durationSeconds: 8,
    });
    const reboundAnchor = nodes(rebound.workflow).find(
      (node) => String(node.id) === String(materialized.anchorNodeId),
    )!;
    expect(
      (reboundAnchor.inputs as Json[]).find((input) => input.name === "prompt")
        ?.link,
    ).toBeNull();
    expect(links(rebound.workflow).some((edge) => edge[0] === promptLink)).toBe(
      false,
    );
  });

  it("selects the native anchor reached by the shared duration source", () => {
    const materialized = spliceContextPipeline(nestedFixture(), OPTIONS);
    const workflow = structuredClone(materialized.workflow);
    const extraId = Number(workflow.last_node_id) + 1;
    (workflow.nodes as Json[]).push({
      id: extraId,
      type: "MiniMaxH3ImageToVideo",
      inputs: [],
      outputs: [],
      widgets_values: [],
    });
    workflow.last_node_id = extraId;

    const rebound = rebindExistingContextAuthoring(workflow, {
      taskMode: "t2va",
      userIntent: "Only shared duration determines the authoring target.",
      durationSeconds: 8,
    });
    expect(rebound.anchorNodeId).toBe(String(materialized.anchorNodeId));
    expect(nodes(rebound.workflow).find((node) => node.id === extraId)).toEqual(
      nodes(workflow).find((node) => node.id === extraId),
    );
  });

  it("preserves a selected anchor's user-owned prompt producer", () => {
    const materialized = spliceContextPipeline(nestedFixture(), OPTIONS);
    const workflow = structuredClone(materialized.workflow);
    const anchor = nodes(workflow).find(
      (node) => String(node.id) === String(materialized.anchorNodeId),
    )!;
    const prompt = (anchor.inputs as Json[]).find(
      (input) => input.name === "prompt",
    )!;
    const promptLink = Number(prompt.link);
    const productShell = nodes(workflow).find(
      (node) => node.type === "comfyui_h3_context.H3Context.ProductShell",
    )!;
    for (const output of productShell.outputs as Json[])
      if (Array.isArray(output.links))
        output.links = output.links.filter((value) => value !== promptLink);
    const foreignId = Number(workflow.last_node_id) + 1;
    (workflow.nodes as Json[]).push({
      id: foreignId,
      type: "PrimitiveString",
      inputs: [],
      outputs: [{ links: [promptLink], type: "STRING" }],
      widgets_values: ["foreign prompt"],
    });
    workflow.last_node_id = foreignId;
    const promptEdge = links(workflow).find(
      (edge) => Number(edge[0]) === promptLink,
    )!;
    promptEdge[1] = foreignId;
    promptEdge[2] = 0;
    const expectedPromptEdge = structuredClone(promptEdge);

    const rebound = rebindExistingContextAuthoring(workflow, {
      taskMode: "t2va",
      userIntent: "Prompt producers are not an App Mode admission concern.",
      durationSeconds: 8,
    });
    expect(
      links(rebound.workflow).find((edge) => Number(edge[0]) === promptLink),
    ).toEqual(expectedPromptEdge);
  });

  it("keeps positional and named live-widget state in one bounded rebind", () => {
    const materialized = spliceContextPipeline(nestedFixture(), OPTIONS);
    const request = nodes(materialized.workflow).find(
      (node) => node.type === "comfyui_h3_context.H3Context.Request",
    )!;
    const durationInput = (request.inputs as Json[]).find(
      (input) => input.name === "duration_seconds",
    )!;
    const durationEdge = links(materialized.workflow).find(
      (edge) => edge[0] === durationInput.link,
    )!;
    const source = nodes(materialized.workflow).find(
      (node) => String(node.id) === String(durationEdge[1]),
    )!;
    request.widgets_values_named = {
      task_mode: "t2va",
      user_intent: OPTIONS.userIntent,
      duration_seconds: 5.167,
    };
    source.widgets_values_named = { value: OPTIONS.durationSeconds };
    const before = structuredClone(materialized.workflow);

    const rebound = rebindExistingContextAuthoring(materialized.workflow, {
      taskMode: "t2va",
      userIntent: "A synthetic named-widget request.",
      durationSeconds: 8,
    });
    const reboundRequest = nodes(rebound.workflow).find(
      (node) => String(node.id) === String(request.id),
    )!;
    const reboundSource = nodes(rebound.workflow).find(
      (node) => String(node.id) === String(source.id),
    )!;
    expect(reboundRequest.widgets_values_named).toEqual({
      task_mode: "t2va",
      user_intent: "A synthetic named-widget request.",
      duration_seconds: 5.167,
    });
    expect(reboundSource.widgets_values_named).toEqual({ value: 8 });

    reboundRequest.widgets_values = ["t2va", OPTIONS.userIntent, 5.167];
    reboundRequest.widgets_values_named = {
      task_mode: "t2va",
      user_intent: OPTIONS.userIntent,
      duration_seconds: 5.167,
    };
    reboundSource.widgets_values = [OPTIONS.durationSeconds];
    reboundSource.widgets_values_named = { value: OPTIONS.durationSeconds };
    expect(rebound.workflow).toEqual(before);
  });

  it.each(["request", "duration"] as const)(
    "refuses conflicting %s named-widget state",
    (target) => {
      const materialized = spliceContextPipeline(nestedFixture(), OPTIONS);
      const request = nodes(materialized.workflow).find(
        (node) => node.type === "comfyui_h3_context.H3Context.Request",
      )!;
      const durationInput = (request.inputs as Json[]).find(
        (input) => input.name === "duration_seconds",
      )!;
      const durationEdge = links(materialized.workflow).find(
        (edge) => edge[0] === durationInput.link,
      )!;
      const source = nodes(materialized.workflow).find(
        (node) => String(node.id) === String(durationEdge[1]),
      )!;
      if (target === "request")
        request.widgets_values_named = {
          user_intent: "A conflicting named value.",
        };
      else source.widgets_values_named = { value: 99 };

      expect(() =>
        rebindExistingContextAuthoring(materialized.workflow, {
          taskMode: "t2va",
          userIntent: "A replacement must not guess conflicting state.",
          durationSeconds: 8,
        }),
      ).toThrowError(
        expect.objectContaining({ code: "unsafe_authoring_seam" }),
      );
    },
  );

  it("refuses a nested template whose promoted duration seam is missing", () => {
    const template = nestedFixture();
    const definitions = template.definitions as {
      subgraphs: Array<{ inputs: Json[] }>;
    };
    definitions.subgraphs[0]!.inputs = definitions.subgraphs[0]!.inputs.filter(
      (input) => input.name !== "value_1",
    );

    expect(() => spliceContextPipeline(template, OPTIONS)).toThrowError(
      expect.objectContaining({ code: "missing_duration_seam" }),
    );
  });

  it("replaces a flat anchor's existing prompt source and leaves no dangling link", () => {
    const result = spliceContextPipeline(flatFixture(), {
      ...OPTIONS,
      taskMode: "ref2va",
    });
    const anchor = nodes(result.workflow).find((node) => node.id === 1)!;
    const prompt = (anchor.inputs as Json[]).find(
      (input) => input.name === "prompt",
    )!;
    expect(prompt.link).not.toBe(3);

    // The template's own string node is detached rather than orphaned.
    const stringNode = nodes(result.workflow).find((node) => node.id === 4)!;
    expect((stringNode.outputs as { links: number[] }[])[0]!.links).toEqual([]);
    const ids = new Set(links(result.workflow).map((link) => link[0]));
    expect(ids.has(3)).toBe(false);

    // Every remaining link points at a node that exists.
    const present = new Set(
      nodes(result.workflow).map((node) => Number(node.id)),
    );
    for (const link of links(result.workflow)) {
      expect(present.has(link[1])).toBe(true);
      expect(present.has(link[3])).toBe(true);
    }
  });

  it("drives a flat template's duration through the edge, not through a node id", () => {
    const result = spliceContextPipeline(flatFixture(), {
      ...OPTIONS,
      taskMode: "ref2va",
      durationSeconds: 8,
    });
    const primitive = nodes(result.workflow).find((node) => node.id === 3)!;
    expect(primitive.widgets_values).toEqual([8]);
    expect(result.durationApplied).toBe(true);
    const requestType = CONTEXT_PIPELINE.find(
      (spec) => spec.key === "request",
    )!.type;
    const request = nodes(result.workflow).find(
      (node) => node.type === requestType,
    )!;
    const durationInput = (request.inputs as Json[]).find(
      (input) => input.name === "duration_seconds",
    )!;
    const requestEdge = links(result.workflow).find(
      (link) => link[0] === durationInput.link,
    )!;
    expect(requestEdge[1]).toBe(3);
  });

  it("refuses a flat template whose native length edge is missing", () => {
    const template = flatFixture();
    const anchor = nodes(template).find((node) => node.id === 1)!;
    const length = (anchor.inputs as Json[]).find(
      (input) => input.name === "length",
    )!;
    length.link = null;

    expect(() =>
      spliceContextPipeline(template, {
        ...OPTIONS,
        taskMode: "ref2va",
      }),
    ).toThrowError(expect.objectContaining({ code: "missing_duration_seam" }));
  });

  it("allocates ids above everything the template already used", () => {
    const template = nestedFixture();
    const result = spliceContextPipeline(template, OPTIONS);
    const created = nodes(result.workflow).filter(
      (node) => Number(node.id) > 10,
    );
    // Nested templates have no outer numeric source to share, so App Mode adds
    // one visible Sidebar-managed PrimitiveFloat beside the context pipeline.
    expect(created).toHaveLength(CONTEXT_PIPELINE.length + 1);
    const ids = nodes(result.workflow).map((node) => Number(node.id));
    expect(new Set(ids).size).toBe(ids.length);
    const linkIds = links(result.workflow).map((link) => link[0]);
    expect(new Set(linkIds).size).toBe(linkIds.length);
    expect(result.workflow.last_node_id).toBe(Math.max(...ids));
    expect(result.workflow.last_link_id).toBe(Math.max(...linkIds));
  });

  it("does not mutate the template it was given", () => {
    const template = nestedFixture();
    const before = JSON.stringify(template);
    spliceContextPipeline(template, OPTIONS);
    expect(JSON.stringify(template)).toBe(before);
  });

  it("rejects an unsupported mode and a non-producible duration shape", () => {
    expect(() =>
      spliceContextPipeline(nestedFixture(), { ...OPTIONS, taskMode: "nope" }),
    ).toThrow(TemplateSpliceError);
    for (const durationSeconds of [
      0,
      -1,
      Number.NaN,
      Number.POSITIVE_INFINITY,
    ]) {
      expect(() =>
        spliceContextPipeline(nestedFixture(), { ...OPTIONS, durationSeconds }),
      ).toThrow(TemplateSpliceError);
    }
  });

  it("maps every task mode to a family and every family to an anchor", () => {
    expect(Object.keys(TASK_MODE_FAMILY).sort()).toEqual([
      "fl2va",
      "i2va",
      "l2va",
      "ref2va",
      "t2va",
    ]);
    for (const family of Object.values(TASK_MODE_FAMILY)) {
      expect(FAMILY_ANCHOR[family]).toBeTruthy();
    }
  });

  it("routes a newly materialized i2va graph through the visible source-size branch", () => {
    const result = spliceContextPipeline(i2vaTemplateWithVisibleSizeBranch(), {
      ...OPTIONS,
      taskMode: "i2va",
      media: {
        firstFrame: {
          type: "LoadImage",
          widgetValues: ["synthetic-input.png", "image"],
        },
      },
    });
    const workflowNodes = nodes(result.workflow);
    const origin = (target: Json, inputName: string): unknown[] | undefined => {
      const input = (target.inputs as Json[]).find(
        (candidate) => candidate.name === inputName,
      );
      const edge = links(result.workflow).find(
        (candidate) => candidate[0] === input?.link,
      );
      if (edge === undefined) return undefined;
      const source = workflowNodes.find(
        (candidate) => Number(candidate.id) === edge[1],
      );
      return [source?.type, edge[2]];
    };
    const anchor = workflowNodes.find(
      (node) => Number(node.id) === result.anchorNodeId,
    )!;
    const scale = workflowNodes.find(
      (node) => node.type === "ImageScaleToTotalPixels",
    )!;
    const size = workflowNodes.find((node) => node.type === "GetImageSize")!;
    const selector = workflowNodes.find(
      (node) => node.type === "ResolutionSelector",
    )!;

    expect(origin(anchor, "first_frame")).toEqual(["LoadImage", 0]);
    expect(origin(scale, "image")).toEqual(["LoadImage", 0]);
    expect(origin(size, "image")).toEqual(["ImageScaleToTotalPixels", 0]);
    expect(origin(anchor, "width")).toEqual(["GetImageSize", 0]);
    expect(origin(anchor, "height")).toEqual(["GetImageSize", 1]);
    expect(scale.widgets_values).toEqual([...I2VA_SCALE_WIDGET_VALUES]);
    expect((selector.outputs as Json[]).map((output) => output.links)).toEqual([
      [],
      [],
    ]);
  });
});

describe.runIf(corpusPresent)(
  "M17-20 splice against the pinned template bytes",
  () => {
    it.each([
      ["video_minimax_h3_t2v", "t2va", true],
      ["video_minimax_h3_i2v", "i2va", true],
      ["video_minimax_h3_r2v", "ref2va", false],
    ] as const)("splices %s for %s", (name, taskMode, nestedAnchor) => {
      const result = spliceContextPipeline(pinned(name), {
        ...OPTIONS,
        taskMode,
      });
      expect(result.nestedAnchor).toBe(nestedAnchor);
      expect(result.durationApplied).toBe(true);

      const anchor = nodes(result.workflow).find(
        (node) => Number(node.id) === result.anchorNodeId,
      )!;
      const prompt = (anchor.inputs as Json[]).find(
        (input) => input.name === "prompt",
      )!;
      const edge = links(result.workflow).find(
        (link) => link[0] === prompt.link,
      )!;
      expect(edge[1]).toBe(result.promptNodeId);
      expect(edge[3]).toBe(result.anchorNodeId);

      // The template's own output sink survives untouched: the splice adds a
      // prompt source, it does not rebuild the generation graph.
      expect(
        nodes(result.workflow).some((node) => node.type === "SaveVideo"),
      ).toBe(true);

      // Exactly one anchor remains, so nothing downstream has to guess which
      // conditioning node the prompt belongs to.
      expect(() =>
        resolveAnchor(result.workflow, TASK_MODE_FAMILY[taskMode]!),
      ).not.toThrow();

      // Every link still resolves to a real node.
      const present = new Set(
        nodes(result.workflow).map((node) => Number(node.id)),
      );
      for (const link of links(result.workflow)) {
        expect(present.has(link[1])).toBe(true);
        expect(present.has(link[3])).toBe(true);
      }
    });

    it("puts the authored duration on the control the template drives length from", () => {
      const flat = spliceContextPipeline(pinned("video_minimax_h3_r2v"), {
        ...OPTIONS,
        taskMode: "ref2va",
        durationSeconds: 8,
      });
      const primitive = nodes(flat.workflow).find(
        (node) => node.type === "PrimitiveFloat",
      )!;
      expect(primitive.widgets_values).toEqual([8]);

      const nested = spliceContextPipeline(pinned("video_minimax_h3_t2v"), {
        ...OPTIONS,
        durationSeconds: 8,
      });
      const anchor = nodes(nested.workflow).find(
        (node) => Number(node.id) === nested.anchorNodeId,
      )!;
      expect((anchor.widgets_values as unknown[])[3]).toBe(8);
    });

    it("rebinds the shared nested authoring seam on the pinned t2v bytes", () => {
      const materialized = spliceContextPipeline(
        pinned("video_minimax_h3_t2v"),
        OPTIONS,
      );
      const rebound = rebindExistingContextAuthoring(materialized.workflow, {
        taskMode: "t2va",
        userIntent: "A synthetic pinned-template request.",
        durationSeconds: 8,
      });
      const request = nodes(rebound.workflow).find(
        (node) => node.type === "comfyui_h3_context.H3Context.Request",
      )!;
      const source = nodes(rebound.workflow).find(
        (node) => node.title === "H3 App Mode duration (Sidebar)",
      )!;
      expect((request.widgets_values as unknown[])[1]).toBe(
        "A synthetic pinned-template request.",
      );
      expect(source.widgets_values).toEqual([8]);
    });
  },
);

describe.runIf(corpusPresent)(
  "M17-20 media binding on the pinned bytes",
  () => {
    const loaderOf = (workflow: Json, id: number | undefined): Json =>
      nodes(workflow).find((node) => Number(node.id) === id)!;

    const inputLink = (workflow: Json, nodeId: number, name: string): Link => {
      const node = nodes(workflow).find(
        (entry) => Number(entry.id) === nodeId,
      )!;
      const input = (node.inputs as Json[]).find(
        (entry) => entry.name === name,
      )!;
      return links(workflow).find((link) => link[0] === input.link)!;
    };

    it("materializes t2va from the t2v bytes and binds no frame at all", () => {
      // The two bases share a subgraph definition and an anchor family, so the
      // only thing that separates them is whether a LoadImage reaches the promoted
      // `first_frame`. Materializing t2va from the i2v bytes would ship a
      // text-to-video request with the template's sample photograph wired into it.
      expect(MODE_TEMPLATE.t2va).toBe("video_minimax_h3_t2v");
      expect(MODE_TEMPLATE.i2va).toBe("video_minimax_h3_i2v");
      expect(MODE_TEMPLATE.l2va).toBe("video_minimax_h3_i2v");

      const result = spliceContextPipeline(
        pinned(MODE_TEMPLATE.t2va!),
        OPTIONS,
      );
      expect(result.mediaNodeIds).toEqual({
        referenceImages: [],
        referenceVideos: [],
        referenceAudios: [],
      });
      expect(
        nodes(result.workflow).some((node) => node.type === "LoadImage"),
      ).toBe(false);
      expect(
        nodes(result.workflow).some(
          (node) =>
            node.type === "comfyui_h3_context.H3Context.ReferenceRegistry",
        ),
      ).toBe(false);
    });

    it("moves the chosen image onto the template's own loader for i2va", () => {
      const result = spliceContextPipeline(pinned(MODE_TEMPLATE.i2va!), {
        ...OPTIONS,
        taskMode: "i2va",
        media: {
          firstFrame: {
            type: "LoadImage",
            widgetValues: ["chosen.png", "image"],
          },
        },
      });
      // The template ships exactly one LoadImage. Reusing it keeps one visible
      // image node on the canvas instead of leaving a sample beside the choice.
      const loaders = nodes(result.workflow).filter(
        (node) => node.type === "LoadImage",
      );
      expect(loaders).toHaveLength(1);
      expect(loaders[0]!.widgets_values).toEqual(["chosen.png", "image"]);
      expect(Number(loaders[0]!.id)).toBe(result.mediaNodeIds.firstFrame);

      const edge = inputLink(
        result.workflow,
        result.anchorNodeId,
        "first_frame",
      );
      expect(edge[1]).toBe(result.mediaNodeIds.firstFrame);
      expect(edge[5]).toBe("IMAGE");
      expect(result.mediaNodeIds.lastFrame).toBeUndefined();

      const registry = nodes(result.workflow).find(
        (node) =>
          node.type === "comfyui_h3_context.H3Context.ReferenceRegistry",
      )!;
      expect((registry.inputs as Json[]).map((input) => input.name)).toEqual([
        "first_frame",
      ]);
      expect(
        inputLink(result.workflow, Number(registry.id), "first_frame").slice(
          1,
          3,
        ),
      ).toEqual(edge.slice(1, 3));
      const plan = nodes(result.workflow).find(
        (node) => node.type === "comfyui_h3_context.H3Context.Plan",
      )!;
      expect(
        inputLink(result.workflow, Number(plan.id), "reference_registry").slice(
          1,
          3,
        ),
      ).toEqual([Number(registry.id), 0]);
    });

    it("keeps the published default when the caller chose nothing", () => {
      // D2: an unresolved input is answered on the visible canvas widget, so the
      // template's own value has to survive materialization untouched.
      const template = pinned(MODE_TEMPLATE.i2va!);
      const shipped = nodes(template).find(
        (node) => node.type === "LoadImage",
      )!;
      const result = spliceContextPipeline(template, {
        ...OPTIONS,
        taskMode: "i2va",
      });
      expect(
        loaderOf(result.workflow, result.mediaNodeIds.firstFrame)
          .widgets_values,
      ).toEqual(shipped.widgets_values);
    });

    it("rewires the single loader to last_frame for l2va", () => {
      const result = spliceContextPipeline(pinned(MODE_TEMPLATE.l2va!), {
        ...OPTIONS,
        taskMode: "l2va",
        media: {
          lastFrame: { type: "LoadImage", widgetValues: ["tail.png", "image"] },
        },
      });
      expect(result.mediaNodeIds.firstFrame).toBeUndefined();
      const edge = inputLink(
        result.workflow,
        result.anchorNodeId,
        "last_frame",
      );
      expect(edge[1]).toBe(result.mediaNodeIds.lastFrame);

      const registry = nodes(result.workflow).find(
        (node) =>
          node.type === "comfyui_h3_context.H3Context.ReferenceRegistry",
      )!;
      expect((registry.inputs as Json[]).map((input) => input.name)).toEqual([
        "last_frame",
      ]);
      expect(
        inputLink(result.workflow, Number(registry.id), "last_frame").slice(
          1,
          3,
        ),
      ).toEqual(edge.slice(1, 3));

      // `first_frame` must be genuinely unbound, not merely pointing elsewhere.
      const anchor = nodes(result.workflow).find(
        (node) => Number(node.id) === result.anchorNodeId,
      )!;
      const first = (anchor.inputs as Json[]).find(
        (input) => input.name === "first_frame",
      )!;
      expect(first.link).toBeNull();
    });

    it("adds the second loader fl2va needs and leaves no orphan behind", () => {
      const result = spliceContextPipeline(pinned(MODE_TEMPLATE.fl2va!), {
        ...OPTIONS,
        taskMode: "fl2va",
        media: {
          firstFrame: {
            type: "LoadImage",
            widgetValues: ["head.png", "image"],
          },
          lastFrame: { type: "LoadImage", widgetValues: ["tail.png", "image"] },
        },
      });
      const loaders = nodes(result.workflow).filter(
        (node) => node.type === "LoadImage",
      );
      expect(loaders).toHaveLength(2);
      expect(
        loaders.map((node) => (node.widgets_values as unknown[])[0]).sort(),
      ).toEqual(["head.png", "tail.png"]);
      expect(
        inputLink(result.workflow, result.anchorNodeId, "first_frame")[1],
      ).toBe(result.mediaNodeIds.firstFrame);
      expect(
        inputLink(result.workflow, result.anchorNodeId, "last_frame")[1],
      ).toBe(result.mediaNodeIds.lastFrame);
      const registry = nodes(result.workflow).find(
        (node) =>
          node.type === "comfyui_h3_context.H3Context.ReferenceRegistry",
      )!;
      expect((registry.inputs as Json[]).map((input) => input.name)).toEqual([
        "first_frame",
        "last_frame",
      ]);
      for (const role of ["first_frame", "last_frame"] as const) {
        expect(
          inputLink(result.workflow, Number(registry.id), role).slice(1, 3),
        ).toEqual(
          inputLink(result.workflow, result.anchorNodeId, role).slice(1, 3),
        );
      }
    });

    it.each(["i2va", "l2va", "fl2va"] as const)(
      "feeds the %s frame registry into Context Plan exactly once",
      (taskMode) => {
        const result = spliceContextPipeline(pinned(MODE_TEMPLATE[taskMode]!), {
          ...OPTIONS,
          taskMode,
          media:
            taskMode === "i2va"
              ? {
                  firstFrame: {
                    type: "LoadImage",
                    widgetValues: ["head.png", "image"],
                  },
                }
              : taskMode === "l2va"
                ? {
                    lastFrame: {
                      type: "LoadImage",
                      widgetValues: ["tail.png", "image"],
                    },
                  }
                : {
                    firstFrame: {
                      type: "LoadImage",
                      widgetValues: ["head.png", "image"],
                    },
                    lastFrame: {
                      type: "LoadImage",
                      widgetValues: ["tail.png", "image"],
                    },
                  },
        });
        const registry = nodes(result.workflow).find(
          (node) =>
            node.type === "comfyui_h3_context.H3Context.ReferenceRegistry",
        )!;
        const plan = nodes(result.workflow).find(
          (node) => node.type === "comfyui_h3_context.H3Context.Plan",
        )!;
        expect(
          inputLink(
            result.workflow,
            Number(plan.id),
            "reference_registry",
          ).slice(1, 3),
        ).toEqual([Number(registry.id), 0]);
      },
    );

    it("carries the reference set through the registry and the anchor alike", () => {
      const result = spliceContextPipeline(pinned(MODE_TEMPLATE.ref2va!), {
        ...OPTIONS,
        taskMode: "ref2va",
        media: {
          referenceImages: [
            { type: "LoadImage", widgetValues: ["ref-a.png", "image"] },
          ],
          referenceVideos: [{ type: "LoadVideo", widgetValues: ["clip.mp4"] }],
          referenceAudios: [{ type: "LoadAudio", widgetValues: ["score.wav"] }],
        },
      });
      // The template ships two sample LoadImage nodes; one selected image means one
      // survives and one is discarded, so the visible graph matches the request.
      expect(
        nodes(result.workflow).filter((node) => node.type === "LoadImage"),
      ).toHaveLength(1);
      expect(result.mediaNodeIds.referenceImages).toHaveLength(1);
      expect(result.mediaNodeIds.referenceVideos).toHaveLength(1);
      expect(result.mediaNodeIds.referenceAudios).toHaveLength(1);

      const registry = nodes(result.workflow).find(
        (node) =>
          node.type === "comfyui_h3_context.H3Context.ReferenceRegistry",
      )!;
      // The registry owns canonical reference identity, so it must see exactly the
      // same media the anchor does -- through the autogrow socket names the host
      // serializes, not the group names the API prompt uses.
      //
      // M17-17: that includes the reference video's own soundtrack. It used to
      // reach the anchor without reaching the registry, so the registry derived
      // no `ref_video_audio_*` binding for a graph that carried one.
      expect((registry.inputs as Json[]).map((input) => input.name)).toEqual([
        "images.image0",
        "videos.video0",
        "paired_audios.paired_audio0",
        "audios.audio0",
      ]);
      expect(
        inputLink(result.workflow, Number(registry.id), "images.image0")[1],
      ).toBe(result.mediaNodeIds.referenceImages[0]);

      // A reference video reaches the anchor as decomposed frames plus audio, and
      // reaches the registry as the video itself.
      const components = nodes(result.workflow).find(
        (node) => node.type === "GetVideoComponents",
      )!;
      expect(
        inputLink(result.workflow, Number(components.id), "video")[1],
      ).toBe(result.mediaNodeIds.referenceVideos[0]);
      expect(
        inputLink(
          result.workflow,
          result.anchorNodeId,
          "ref_videos.ref_video_0",
        )[1],
      ).toBe(Number(components.id));
      expect(
        inputLink(
          result.workflow,
          result.anchorNodeId,
          "ref_video_audios.ref_video_audio_0",
        ),
      ).toEqual([
        expect.any(Number),
        Number(components.id),
        1,
        result.anchorNodeId,
        expect.any(Number),
        "AUDIO",
      ]);
    });

    it("grows the anchor's autogrow group past the sockets the template declared", () => {
      const images = Array.from({ length: 5 }, (_unused, index) => ({
        type: "LoadImage",
        widgetValues: [`ref-${index}.png`, "image"],
      }));
      const result = spliceContextPipeline(pinned(MODE_TEMPLATE.ref2va!), {
        ...OPTIONS,
        taskMode: "ref2va",
        media: { referenceImages: images },
      });
      expect(result.mediaNodeIds.referenceImages).toHaveLength(5);
      const anchor = nodes(result.workflow).find(
        (node) => Number(node.id) === result.anchorNodeId,
      )!;
      const bound = (anchor.inputs as Json[]).filter(
        (input) =>
          String(input.name).startsWith("ref_images.") && input.link !== null,
      );
      expect(bound).toHaveLength(5);
    });

    it("refuses a reference set larger than the registry admits", () => {
      expect(() =>
        spliceContextPipeline(pinned(MODE_TEMPLATE.ref2va!), {
          ...OPTIONS,
          taskMode: "ref2va",
          media: {
            referenceImages: Array.from({ length: 10 }, () => ({
              type: "LoadImage",
              widgetValues: ["x.png", "image"],
            })),
          },
        }),
      ).toThrow(TemplateSpliceError);
    });

    it("leaves no dangling link after any media rebind", () => {
      for (const taskMode of ["t2va", "i2va", "l2va", "fl2va", "ref2va"]) {
        const result = spliceContextPipeline(pinned(MODE_TEMPLATE[taskMode]!), {
          ...OPTIONS,
          taskMode,
        });
        const present = new Set(
          nodes(result.workflow).map((node) => Number(node.id)),
        );
        for (const link of links(result.workflow)) {
          expect(present.has(link[1])).toBe(true);
          expect(present.has(link[3])).toBe(true);
        }
        // Every declared input link must resolve to a link that exists.
        const ids = new Set(links(result.workflow).map((link) => link[0]));
        for (const node of nodes(result.workflow))
          for (const input of (node.inputs as Json[] | undefined) ?? [])
            if (typeof input.link === "number")
              expect(ids.has(input.link)).toBe(true);
      }
    });
  },
);

describe("M17-20 D5 the sink writes where the run says it writes", () => {
  const options = {
    taskMode: "t2va",
    userIntent: "A red kite crosses the sky.",
    durationSeconds: 5.167,
  };

  it("replaces the template's shared default location", () => {
    const spliced = spliceContextPipeline(
      syntheticTemplate("video_minimax_h3_t2v"),
      {
        ...options,
        artifactPrefix: "video/h3-context/workspace.1/abcdef0123456789",
      },
    );
    expect(spliced.artifactPrefixApplied).toBe(true);
    const sink = (spliced.workflow.nodes as Record<string, unknown>[]).find(
      (node) => node.type === "SaveVideo",
    );
    expect((sink?.widgets_values as unknown[])[0]).toBe(
      "video/h3-context/workspace.1/abcdef0123456789",
    );
    // The rest of the sink's widgets are the template's and stay untouched.
    expect((sink?.widgets_values as unknown[]).slice(1)).toEqual([
      "auto",
      "auto",
    ]);
  });

  it("leaves the template alone when no location was supplied", () => {
    const spliced = spliceContextPipeline(
      syntheticTemplate("video_minimax_h3_t2v"),
      options,
    );
    expect(spliced.artifactPrefixApplied).toBe(false);
    const sink = (spliced.workflow.nodes as Record<string, unknown>[]).find(
      (node) => node.type === "SaveVideo",
    );
    expect((sink?.widgets_values as unknown[])[0]).toBe("video/MiniMax_H3");
  });

  it("reports failure rather than guessing when there is not exactly one sink", () => {
    // The positional write is only safe because there is exactly one sink; with
    // two, writing into either would be a guess about which one this run owns.
    const template = syntheticTemplate("video_minimax_h3_t2v");
    const nodes = template.nodes as Record<string, unknown>[];
    const sink = nodes.find((node) => node.type === "SaveVideo")!;
    nodes.push({ ...sink, id: 9100, inputs: [] });
    expect(
      spliceContextPipeline(template, {
        ...options,
        artifactPrefix: "video/x/y",
      }).artifactPrefixApplied,
    ).toBe(false);
  });
});
