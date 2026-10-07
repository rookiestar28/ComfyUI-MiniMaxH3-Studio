import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  H3_SHELL_MANIFEST,
  inspectVisibleH3Graph,
} from "../src/host/graphAdapter";
import { compiledPromptMatchesVisibleGraph } from "../src/host/appMode";
import { H3_NODE_TYPES } from "../src/host/graphAdapter";
import {
  MODE_TEMPLATE,
  spliceContextPipeline,
  type Json,
} from "../src/host/templateMaterialization";
import { syntheticTemplate } from "./support/templateFixture";

/**
 * M17-20 phase 2c. The canvas App Mode materializes has to be a canvas App Mode
 * can then read back.
 *
 * Materialization (2b) and adoption were validated by two different rules: the
 * splice produced an official template with the context pipeline in it, while
 * `inspectVisibleH3Graph` still reported every node this repository does not own
 * as foreign. A graph that had just been written by this repository therefore
 * did not qualify as a compatible existing graph, so re-running after
 * materialization had to replace the canvas — discarding whatever the user had
 * resolved on it, which is exactly the affordance D2 depends on.
 *
 * These rows are the join: splice, then inspect, and require compatibility.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const TEMPLATES = join(
  resolve(HERE, "..", ".."),
  "reference",
  "rm02",
  "official",
  "workflow_templates",
  "templates",
);
const corpusPresent = existsSync(TEMPLATES);

const OPTIONS = {
  userIntent: "A red kite crosses the sky while the camera follows its arc.",
  durationSeconds: 5.167,
};

const IMAGE = { type: "LoadImage", widgetValues: ["chosen.png", "image"] };
const AUDIO = { type: "LoadAudio", widgetValues: ["chosen.wav"] };

function media(taskMode: string) {
  if (taskMode === "ref2va")
    return {
      referenceImages: [IMAGE],
      referenceVideos: [],
      referenceAudios: [AUDIO],
    };
  if (taskMode === "i2va") return { firstFrame: IMAGE };
  if (taskMode === "l2va") return { lastFrame: IMAGE };
  if (taskMode === "fl2va")
    return {
      firstFrame: IMAGE,
      lastFrame: { type: "LoadImage", widgetValues: ["tail.png", "image"] },
    };
  return {};
}

const MODES = ["t2va", "i2va", "fl2va", "l2va", "ref2va"] as const;

describe("a materialized canvas is a canvas App Mode can bind", () => {
  it.each(MODES)("admits the synthetic %s materialization", (taskMode) => {
    const spliced = spliceContextPipeline(
      syntheticTemplate(MODE_TEMPLATE[taskMode]!),
      { ...OPTIONS, taskMode, media: media(taskMode) },
    );
    expect(
      inspectVisibleH3Graph(spliced.workflow, H3_SHELL_MANIFEST),
    ).toMatchObject({ status: "ready", existingGraphCompatible: true });
  });

  it("still refuses a canvas whose prompt never reaches the anchor", () => {
    // The open-world rules stop enumerating the surrounding graph. The one edge
    // they do not stop enforcing is the one that carries the compiled prompt:
    // without it the canvas renders someone else's text.
    const spliced = spliceContextPipeline(
      syntheticTemplate(MODE_TEMPLATE.t2va!),
      { ...OPTIONS, taskMode: "t2va" },
    );
    const workflow = structuredClone(spliced.workflow) as Json;
    workflow.links = (workflow.links as unknown[][]).filter(
      (link) => link[5] !== "STRING",
    );
    expect(
      inspectVisibleH3Graph(workflow, H3_SHELL_MANIFEST),
    ).not.toMatchObject({ status: "ready", existingGraphCompatible: true });
  });

  it("still refuses a canvas whose context chain is incomplete", () => {
    const spliced = spliceContextPipeline(
      syntheticTemplate(MODE_TEMPLATE.t2va!),
      { ...OPTIONS, taskMode: "t2va" },
    );
    const workflow = structuredClone(spliced.workflow) as Json;
    workflow.nodes = (workflow.nodes as Json[]).filter(
      (node) => node.type !== "comfyui_h3_context.H3Context.Preview",
    );
    expect(
      inspectVisibleH3Graph(workflow, H3_SHELL_MANIFEST),
    ).not.toMatchObject({ status: "ready", existingGraphCompatible: true });
  });

  it("still refuses a node with no serialized type", () => {
    const spliced = spliceContextPipeline(
      syntheticTemplate(MODE_TEMPLATE.t2va!),
      { ...OPTIONS, taskMode: "t2va" },
    );
    const workflow = structuredClone(spliced.workflow) as Json;
    (workflow.nodes as Json[]).push({ id: 9001 });
    expect(inspectVisibleH3Graph(workflow, H3_SHELL_MANIFEST)).toMatchObject({
      status: "incompatible",
      reason: "untyped_node",
    });
  });

  describe.runIf(corpusPresent)("against the pinned template bytes", () => {
    const pinned = (name: string): Json =>
      JSON.parse(readFileSync(join(TEMPLATES, `${name}.json`), "utf8")) as Json;

    it.each(MODES)("admits the pinned %s materialization", (taskMode) => {
      const spliced = spliceContextPipeline(pinned(MODE_TEMPLATE[taskMode]!), {
        ...OPTIONS,
        taskMode,
        media: media(taskMode),
      });
      expect(
        inspectVisibleH3Graph(spliced.workflow, H3_SHELL_MANIFEST),
      ).toMatchObject({ status: "ready", existingGraphCompatible: true });
    });
  });
});

/**
 * The other half of the join, and the half only a real host could show.
 *
 * A packaged template puts the anchor inside a subgraph, promotes its widgets
 * onto the instance and feeds prompt and geometry through the subgraph's input
 * boundary. Two rules read that graph wrongly: the widget comparison assumed the
 * anchor's first three widget values were width, height and length, when the
 * template's first widget is the prompt and all three of those arrive as links;
 * and the binding comparison demanded that a boundary-fed input resolve to the
 * boundary, which is not something a compiled prompt can ever say. Together they
 * refused every graph this build materializes, so the adoption route -- which is
 * how D2's remediation reaches the queue -- could not run one.
 */
describe("a materialized canvas binds through its subgraph boundary", () => {
  const REQUEST = H3_NODE_TYPES.request;
  const SHELL = H3_NODE_TYPES.productShell;
  const ANCHOR = "MiniMaxH3ImageToVideo";
  const INTENT = "A red kite crosses the sky while the camera follows its arc.";

  const visibleGraph = () => ({
    nodes: [
      {
        id: 1,
        type: REQUEST,
        widgets_values: ["t2va", INTENT, 5.167],
        inputs: [],
      },
      { id: 2, type: "def-1", inputs: [] },
      { id: 3, type: "ResolutionSelector", inputs: [] },
      { id: 4, type: SHELL, inputs: [] },
    ],
    links: [],
    definitions: {
      subgraphs: [
        {
          id: "def-1",
          nodes: [
            {
              id: 5,
              type: ANCHOR,
              // The template's own prompt text, still sitting in the widget the
              // splice replaced with a link.
              widgets_values: ["a packaged default prompt", 1344, 768, 124],
              inputs: [
                { name: "prompt", link: 11, widget: true },
                { name: "width", link: 12, widget: true },
                { name: "height", link: 13, widget: true },
                { name: "length", link: 14, widget: true },
              ],
            },
            { id: 6, type: "ComfyMathExpression", inputs: [] },
          ],
          links: [
            {
              id: 11,
              origin_id: -10,
              origin_slot: 0,
              target_id: 5,
              target_slot: 0,
            },
            {
              id: 12,
              origin_id: -10,
              origin_slot: 1,
              target_id: 5,
              target_slot: 1,
            },
            {
              id: 13,
              origin_id: -10,
              origin_slot: 2,
              target_id: 5,
              target_slot: 2,
            },
            {
              id: 14,
              origin_id: 6,
              origin_slot: 1,
              target_id: 5,
              target_slot: 3,
            },
          ],
        },
      ],
    },
  });

  const compiledPrompt = (anchorInputs: Record<string, unknown>) =>
    ({
      output: {
        "1": {
          class_type: REQUEST,
          inputs: {
            task_mode: "t2va",
            user_intent: INTENT,
            duration_seconds: 5.167,
          },
        },
        "3": { class_type: "ResolutionSelector", inputs: {} },
        "4": { class_type: SHELL, inputs: {} },
        "2:5": { class_type: ANCHOR, inputs: anchorInputs },
        "2:6": { class_type: "ComfyMathExpression", inputs: {} },
      },
      workflow: {},
    }) as never;

  const boundAnchor = {
    prompt: ["4", 0],
    width: ["3", 0],
    height: ["3", 1],
    length: ["2:6", 1],
  };

  it("binds a prompt whose anchor is fed through the boundary", () => {
    expect(
      compiledPromptMatchesVisibleGraph(
        visibleGraph(),
        compiledPrompt(boundAnchor),
      ),
    ).toBe(true);
  });

  it("ignores native length wiring through a nested graph boundary", () => {
    expect(
      compiledPromptMatchesVisibleGraph(
        visibleGraph(),
        compiledPrompt({ ...boundAnchor, length: ["3", 0] }),
      ),
    ).toBe(true);
  });

  it("does not compare operator-owned native geometry widgets", () => {
    // A hand-built canvas keeps the anchor's own widget layout and may still
    // drive one value from a node. The widget behind a connected input is inert,
    // so comparing it would refuse a graph the host runs exactly as shown.
    const graph = {
      nodes: [
        {
          id: 1,
          type: REQUEST,
          widgets_values: ["t2va", INTENT, 5.167],
          inputs: [],
        },
        { id: 3, type: "ResolutionSelector", inputs: [] },
        { id: 4, type: SHELL, inputs: [] },
        {
          id: 5,
          type: ANCHOR,
          widgets_values: [1344, 768, 124],
          inputs: [
            { name: "prompt", link: 20, widget: true },
            { name: "width", link: 21, widget: true },
          ],
        },
      ],
      links: [
        { id: 20, origin_id: 4, origin_slot: 0, target_id: 5, target_slot: 0 },
        { id: 21, origin_id: 3, origin_slot: 0, target_id: 5, target_slot: 1 },
      ],
    };
    const compiled = {
      output: {
        "1": {
          class_type: REQUEST,
          inputs: {
            task_mode: "t2va",
            user_intent: INTENT,
            duration_seconds: 5.167,
          },
        },
        "3": { class_type: "ResolutionSelector", inputs: {} },
        "4": { class_type: SHELL, inputs: {} },
        "5": {
          class_type: ANCHOR,
          inputs: {
            prompt: ["4", 0],
            width: ["3", 0],
            height: 768,
            length: 124,
          },
        },
      },
      workflow: {},
    } as never;
    expect(compiledPromptMatchesVisibleGraph(graph, compiled)).toBe(true);
    const wrongPrompt = structuredClone(compiled) as unknown as {
      output: Record<string, { inputs: Record<string, unknown> }>;
    };
    wrongPrompt.output["5"]!.inputs.prompt = ["3", 0];
    expect(compiledPromptMatchesVisibleGraph(graph, wrongPrompt as never)).toBe(
      false,
    );
    // Native geometry is an observed host result, not an admission prediction.
    const drifted = structuredClone(compiled) as unknown as {
      output: Record<string, { inputs: Record<string, unknown> }>;
    };
    drifted.output["5"]!.inputs.height = 512;
    expect(compiledPromptMatchesVisibleGraph(graph, drifted as never)).toBe(
      true,
    );
  });

  it("does not prescribe whether native geometry is scalar or linked", () => {
    expect(
      compiledPromptMatchesVisibleGraph(
        visibleGraph(),
        compiledPrompt({ ...boundAnchor, width: 640 }),
      ),
    ).toBe(true);
  });
});
