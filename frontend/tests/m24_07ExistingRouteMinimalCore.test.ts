import { describe, expect, it } from "vitest";

import {
  compiledPromptMatchesRequestedRoute,
  compiledPromptMatchesVisibleGraph,
  isCompatibleExistingPrompt,
  validateExistingInputs,
  type AppModeInputs,
  type ExistingCompiledAdmission,
} from "../src/host/appMode";
import { H3_NODE_TYPES } from "../src/host/graphAdapter";
import {
  rebindExistingContextAuthoring,
  type Json,
} from "../src/host/templateMaterialization";
import { existingManagedHostGraphFixture } from "./support/managedHostGraphFixture";

const INPUTS: AppModeInputs = {
  task_mode: "i2va",
  user_intent: "A sanitized existing-canvas request.",
  duration_milliseconds: 5167,
  frame_count: 124,
  first_frame_source: "9",
};

function nodes(workflow: Json): Json[] {
  return workflow.nodes as Json[];
}

function links(workflow: Json): unknown[][] {
  return workflow.links as unknown[][];
}

function nodeOf(workflow: Json, type: string): Json {
  const result = nodes(workflow).find((node) => node.type === type);
  if (result === undefined)
    throw new Error(`fixture node unavailable: ${type}`);
  return result;
}

function inputOf(node: Json, name: string): Json {
  const result = ((node.inputs as Json[] | undefined) ?? []).find(
    (input) => input.name === name,
  );
  if (result === undefined)
    throw new Error(`fixture input unavailable: ${name}`);
  return result;
}

function removeVisibleInputLink(
  workflow: Json,
  node: Json,
  name: string,
): void {
  const input = inputOf(node, name);
  const linkId = input.link;
  input.link = null;
  workflow.links = links(workflow).filter(
    (row) => String(row[0]) !== String(linkId),
  );
  for (const candidate of nodes(workflow)) {
    for (const output of (candidate.outputs as Json[] | undefined) ?? []) {
      if (!Array.isArray(output.links)) continue;
      output.links = output.links.filter(
        (value) => String(value) !== String(linkId),
      );
    }
  }
}

/**
 * Content-free structural equivalent of the supplied expanded-subgraph duration path.
 *
 * The root Sidebar primitive feeds Request directly and feeds the native length
 * transform through a linked PrimitiveFloat promoted-widget bridge.
 */
function expandedDurationBridgeFixture(): ReturnType<
  typeof existingManagedHostGraphFixture
> {
  const source = existingManagedHostGraphFixture(INPUTS);
  const workflow = structuredClone(source.workflow);
  const compiled = structuredClone(source.compiled);

  const request = nodeOf(workflow, H3_NODE_TYPES.request);
  const requestDuration = inputOf(request, "duration_seconds");
  const requestEdge = links(workflow).find(
    (row) => String(row[0]) === String(requestDuration.link),
  );
  if (requestEdge === undefined)
    throw new Error("request duration edge unavailable");
  const durationSource = nodes(workflow).find(
    (node) => String(node.id) === String(requestEdge[1]),
  );
  const math = nodeOf(workflow, "ComfyMathExpression");
  const operand = ((math.inputs as Json[] | undefined) ?? []).find((input) =>
    ["values.a", "a"].includes(String(input.name)),
  );
  const operandEdge = links(workflow).find(
    (row) => String(row[0]) === String(operand?.link),
  );
  if (
    durationSource === undefined ||
    operand === undefined ||
    operandEdge === undefined
  )
    throw new Error("duration fixture seam unavailable");

  const bridgeId =
    Math.max(...nodes(workflow).map((node) => Number(node.id))) + 1;
  const bridgeLinkId =
    Math.max(...links(workflow).map((row) => Number(row[0]))) + 1;
  for (const output of (durationSource.outputs as Json[] | undefined) ?? []) {
    if (!Array.isArray(output.links)) continue;
    output.links = [
      ...output.links.filter((value) => String(value) !== String(operand.link)),
      bridgeLinkId,
    ];
  }
  operandEdge[1] = bridgeId;
  operandEdge[2] = 0;
  nodes(workflow).push({
    id: bridgeId,
    type: "PrimitiveFloat",
    inputs: [
      {
        name: "value",
        type: "FLOAT",
        link: bridgeLinkId,
        widget: { name: "value" },
      },
    ],
    outputs: [
      {
        name: "FLOAT",
        type: "FLOAT",
        links: [operand.link],
      },
    ],
    widgets_values: [INPUTS.duration_milliseconds / 1000],
  });
  links(workflow).push([
    bridgeLinkId,
    durationSource.id,
    0,
    bridgeId,
    0,
    "FLOAT",
  ]);
  workflow.last_node_id = bridgeId;
  workflow.last_link_id = bridgeLinkId;

  const compiledRequest = Object.entries(compiled.output).find(
    ([, value]) => (value as Json).class_type === H3_NODE_TYPES.request,
  );
  const compiledAnchor = Object.entries(compiled.output).find(
    ([, value]) => (value as Json).class_type === H3_NODE_TYPES.imageGeneration,
  );
  if (compiledRequest === undefined || compiledAnchor === undefined)
    throw new Error("compiled fixture seam unavailable");
  const compiledRequestInputs = (compiledRequest[1] as Json).inputs as Json;
  const compiledDuration = compiledRequestInputs.duration_seconds;
  if (!Array.isArray(compiledDuration) || compiledDuration.length !== 2)
    throw new Error("compiled duration binding unavailable");
  const durationId = String(compiledDuration[0]);
  const compiledLength = ((compiledAnchor[1] as Json).inputs as Json)
    .length as unknown[];
  const compiledMath = compiled.output[String(compiledLength[0])] as Json;
  const compiledMathInputs = compiledMath.inputs as Json;
  const operandName = Object.keys(compiledMathInputs).find((name) =>
    ["values.a", "a"].includes(name),
  );
  if (operandName === undefined)
    throw new Error("compiled operand unavailable");
  const compiledBridgeId = "expanded-duration-bridge";
  compiledMathInputs[operandName] = [compiledBridgeId, 0];
  compiled.output[compiledBridgeId] = {
    class_type: "PrimitiveFloat",
    inputs: { value: [durationId, 0] },
  };

  return { workflow, compiled };
}

function subjectOf(workflow: Json): ExistingCompiledAdmission {
  const request = nodeOf(workflow, H3_NODE_TYPES.request);
  const requestEdge = links(workflow).find(
    (row) =>
      String(row[0]) === String(inputOf(request, "duration_seconds").link),
  );
  const anchor = nodeOf(workflow, H3_NODE_TYPES.imageGeneration);
  const productShell = nodeOf(workflow, H3_NODE_TYPES.productShell);
  if (requestEdge === undefined)
    throw new Error("request duration edge unavailable");
  return {
    requestNodeId: String(request.id),
    durationSourceNodeId: String(requestEdge[1]),
    visibleAnchorNodeId: String(anchor.id),
    anchorNodeId: String(anchor.id),
    anchorNodeType: H3_NODE_TYPES.imageGeneration,
    productShellNodeId: String(productShell.id),
  };
}

describe("M24-07 existing-route minimum core", () => {
  it("accepts the supplied expanded PrimitiveFloat bridge and writes only owned authoring", () => {
    const fixture = expandedDurationBridgeFixture();
    const before = structuredClone(fixture.workflow);
    const result = rebindExistingContextAuthoring(fixture.workflow, {
      taskMode: "i2va",
      userIntent: "An eight-second sanitized existing-canvas request.",
      durationSeconds: 8,
    });

    const expected = structuredClone(before);
    const request = nodeOf(expected, H3_NODE_TYPES.request);
    const requestWidgets = request.widgets_values as unknown[];
    requestWidgets[1] = "An eight-second sanitized existing-canvas request.";
    if (request.widgets_values_named !== undefined)
      (request.widgets_values_named as Json).user_intent =
        "An eight-second sanitized existing-canvas request.";
    const durationSource = nodes(expected).find(
      (node) => String(node.id) === result.durationSourceNodeId,
    )!;
    durationSource.widgets_values = [8];
    if (durationSource.widgets_values_named !== undefined)
      (durationSource.widgets_values_named as Json).value = 8;

    expect(result.workflow).toEqual(expected);
    expect(result.anchorNodeType).toBe(H3_NODE_TYPES.imageGeneration);
  });

  it("does not validate task mode, prompt topology or duration transform type", () => {
    const fixture = expandedDurationBridgeFixture();
    const request = nodeOf(fixture.workflow, H3_NODE_TYPES.request);
    (request.widgets_values as unknown[])[0] = "ref2va";
    const math = nodeOf(fixture.workflow, "ComfyMathExpression");
    math.type = "UserDurationTransform";
    math.widgets_values = ["user-owned duration transform"];
    const anchor = nodeOf(fixture.workflow, H3_NODE_TYPES.imageGeneration);
    removeVisibleInputLink(fixture.workflow, anchor, "prompt");
    nodeOf(fixture.workflow, "CLIPLoader").widgets_values = [
      "user-model-name.safetensors",
      "user-encoder-name.safetensors",
    ];

    expect(() =>
      rebindExistingContextAuthoring(fixture.workflow, {
        taskMode: "i2va",
        userIntent: "Keep the user's pipeline opaque.",
        durationSeconds: 8,
      }),
    ).not.toThrow();
  });

  it("still refuses when native length is not downstream of Request duration", () => {
    const fixture = expandedDurationBridgeFixture();
    const request = nodeOf(fixture.workflow, H3_NODE_TYPES.request);
    const requestEdge = links(fixture.workflow).find(
      (row) =>
        String(row[0]) === String(inputOf(request, "duration_seconds").link),
    );
    const rootPrimitive = nodes(fixture.workflow).find(
      (node) => String(node.id) === String(requestEdge?.[1]),
    );
    if (rootPrimitive === undefined)
      throw new Error("root duration source unavailable");
    const bridgeInput = inputOf(
      nodes(fixture.workflow).find(
        (node) =>
          node.type === "PrimitiveFloat" &&
          ((node.inputs as Json[] | undefined) ?? []).length > 0,
      )!,
      "value",
    );
    const bridgeEdge = links(fixture.workflow).find(
      (row) => String(row[0]) === String(bridgeInput.link),
    )!;
    const foreignId = Number(fixture.workflow.last_node_id) + 1;
    nodes(fixture.workflow).push({
      id: foreignId,
      type: "PrimitiveFloat",
      inputs: [],
      outputs: [{ name: "FLOAT", type: "FLOAT", links: [bridgeInput.link] }],
      widgets_values: [13],
    });
    for (const output of (rootPrimitive.outputs as Json[] | undefined) ?? [])
      if (Array.isArray(output.links))
        output.links = output.links.filter(
          (value) => String(value) !== String(bridgeInput.link),
        );
    bridgeEdge[1] = foreignId;
    fixture.workflow.last_node_id = foreignId;

    expect(() =>
      rebindExistingContextAuthoring(fixture.workflow, {
        taskMode: "i2va",
        userIntent: "A split duration authority must fail.",
        durationSeconds: 8,
      }),
    ).toThrowError(
      expect.objectContaining({ code: "non_shared_duration_seam" }),
    );
  });

  it("checks only minimum compiled identity plus authored intent and shared duration", () => {
    const fixture = expandedDurationBridgeFixture();
    const subject = subjectOf(fixture.workflow);
    const compiled = structuredClone(fixture.compiled);
    const request = compiled.output[subject.requestNodeId] as Json;
    (request.inputs as Json).task_mode = "ref2va";
    const anchor = compiled.output[subject.anchorNodeId] as Json;
    (anchor.inputs as Json).prompt = ["foreign-prompt", 0];
    compiled.output["foreign-prompt"] = {
      class_type: "UserPromptProducer",
      inputs: { model_name: "anything-the-user-installed.safetensors" },
    };

    expect(isCompatibleExistingPrompt(compiled, "existing", subject)).toBe(
      true,
    );
    expect(
      compiledPromptMatchesRequestedRoute(
        compiled,
        INPUTS,
        "existing",
        undefined,
        subject,
      ),
    ).toBe(true);
    expect(
      compiledPromptMatchesVisibleGraph(fixture.workflow, compiled, subject),
    ).toBe(true);
  });

  it("rejects a compiled subject whose native length lost the shared duration", () => {
    const fixture = expandedDurationBridgeFixture();
    const subject = subjectOf(fixture.workflow);
    const compiled = structuredClone(fixture.compiled);
    const anchor = compiled.output[subject.anchorNodeId] as Json;
    (anchor.inputs as Json).length = ["foreign-length", 0];
    compiled.output["foreign-length"] = {
      class_type: "UserLengthProducer",
      inputs: { value: 192 },
    };

    expect(isCompatibleExistingPrompt(compiled, "existing", subject)).toBe(
      false,
    );
  });

  it("does not claim or validate existing canvas media declarations", () => {
    expect(() =>
      validateExistingInputs({
        ...INPUTS,
        first_frame_source: undefined,
        last_frame_source: "arbitrary-visible-id",
        reference_image_sources: ["another-visible-id"],
        reference_video_soundtrack: "excluded",
      }),
    ).not.toThrow();
  });
});
