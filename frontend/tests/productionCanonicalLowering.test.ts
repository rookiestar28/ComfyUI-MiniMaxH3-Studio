import { describe, expect, it, vi } from "vitest";

import {
  PRODUCTION_CANONICAL_LOWERING_SCHEMA,
  createAppModeController,
  type AppModeCompiledPrompt,
  type AppModeInputs,
  type ManagedAppModePreflight,
  type ProductionCanonicalLowering,
} from "../src/host/appMode";
import { loadAvailableProfile } from "./support/generationProfileFixture";
import { fixtureBackedAppModeHost } from "./support/hostSeamTestDouble";
import { createQualifiedBasePrompt } from "./support/qualifiedBasePrompt";
import { acceptedQueueResponse } from "./support/queuePromptTestDouble";
import { loadSyntheticTemplate } from "./support/templateFixture";

const inputs: AppModeInputs = Object.freeze({
  task_mode: "t2va",
  user_intent: "A blue sphere turns slowly.",
  duration_milliseconds: 8_000,
  frame_count: 192,
});

const lowering: ProductionCanonicalLowering = Object.freeze({
  schema: PRODUCTION_CANONICAL_LOWERING_SCHEMA,
  canonicalPrompt: inputs.user_intent,
  baseReportFingerprint: `sha256:${"7".repeat(64)}`,
  baseReportRevision: 0,
  overrideRevision: 1,
  reason: "Materialize approved Production segment prompt",
});

function compiledPreparedPrompt(
  override: Partial<
    Record<"base_report_fingerprint" | "prompt_text", unknown>
  > = {},
): AppModeCompiledPrompt {
  const output = createQualifiedBasePrompt(
    inputs,
    {},
    {
      sharedDurationSource: true,
    },
  ) as Record<string, Record<string, unknown>>;
  (output["4"]!.inputs as Record<string, unknown>).prompt_document = ["9", 3];
  output["9"] = {
    class_type: "comfyui_h3_context.H3Context.AuditOverride",
    inputs: {
      report: ["3", 1],
      base_report_fingerprint: lowering.baseReportFingerprint,
      revision: lowering.overrideRevision,
      reason: lowering.reason,
      prompt_text: lowering.canonicalPrompt,
      ...override,
    },
  };
  return { output, workflow: {} };
}

function environment(
  compiled = compiledPreparedPrompt(),
  mutateLoaded?: (candidate: {
    nodes?: Array<Record<string, unknown>>;
  }) => void,
) {
  const activeWorkflow = {};
  let visible: { nodes?: Array<Record<string, unknown>>; links?: unknown[] } = {
    nodes: [],
    links: [],
  };
  const loadGraphData = vi.fn((candidate: unknown) => {
    visible = structuredClone(candidate) as typeof visible;
    mutateLoaded?.(visible);
  });
  const graphToPrompt = vi.fn().mockResolvedValue(compiled);
  const queuePrompt = vi.fn().mockResolvedValue(acceptedQueueResponse(31));
  const bound = fixtureBackedAppModeHost(
    {
      extensionManager: {
        workflow: {
          activeWorkflow,
          openWorkflows: [activeWorkflow],
        },
      },
      loadApiJson: vi.fn(),
      loadGraphData,
      graphToPrompt,
      graph: { serialize: () => structuredClone(visible) },
    },
    { queuePrompt },
  );
  const bindCanvasIdentity = vi.fn();
  const onQueueSubmitted = vi.fn();
  const onQueueAccepted = vi.fn();
  const onQueueFailed = vi.fn();
  const prepareManaged = vi.fn(async (preflight: ManagedAppModePreflight) => ({
    expectedIdentity: {
      graphFingerprint: preflight.observation.graph_fingerprint,
      compiledPromptFingerprint:
        preflight.observation.compiled_prompt_fingerprint,
    },
    bindCanvasIdentity,
    onQueueSubmitted,
    onQueueAccepted,
    onQueueFailed,
  }));
  return {
    ...bound,
    bindCanvasIdentity,
    graphToPrompt,
    loadGraphData,
    onQueueAccepted,
    onQueueFailed,
    onQueueSubmitted,
    prepareManaged,
    queuePrompt,
  };
}

describe("M26-03 Production canonical graph lowering", () => {
  it("materializes, compiles and queues the one certified AuditOverride graph", async () => {
    const subject = environment();
    await expect(
      createAppModeController(subject.app, subject.api, {
        loadProfile: loadAvailableProfile,
        loadTemplate: loadSyntheticTemplate,
      }).start(inputs, {
        replaceExisting: true,
        preparedCanonicalPrompt: lowering,
        prepareManaged: subject.prepareManaged,
      }),
    ).resolves.toMatchObject({ queuePromptId: expect.any(String) });

    expect(subject.loadGraphData).toHaveBeenCalledOnce();
    const candidate = subject.loadGraphData.mock.calls[0]![0] as {
      nodes: Array<Record<string, unknown>>;
      links: Array<[number, number, number, number, number, string]>;
    };
    const compiler = candidate.nodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.Compiler",
    )!;
    const audit = candidate.nodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.AuditOverride",
    )!;
    const validator = candidate.nodes.find(
      (node) => node.type === "comfyui_h3_context.H3Context.Validator",
    )!;
    const auditReport = (audit.inputs as Array<Record<string, unknown>>).find(
      (input) => input.name === "report",
    )!;
    const validatorDocument = (
      validator.inputs as Array<Record<string, unknown>>
    ).find((input) => input.name === "prompt_document")!;
    expect(audit.widgets_values).toEqual([
      lowering.baseReportFingerprint,
      lowering.overrideRevision,
      lowering.reason,
      lowering.canonicalPrompt,
    ]);
    expect(
      candidate.links.find((link) => link[0] === auditReport.link)?.slice(1, 5),
    ).toEqual([
      compiler.id,
      1,
      audit.id,
      (audit.inputs as unknown[]).indexOf(auditReport),
    ]);
    expect(
      candidate.links
        .find((link) => link[0] === validatorDocument.link)
        ?.slice(1, 5),
    ).toEqual([
      audit.id,
      3,
      validator.id,
      (validator.inputs as unknown[]).indexOf(validatorDocument),
    ]);
    expect(subject.graphToPrompt).toHaveBeenCalledOnce();
    expect(subject.prepareManaged).toHaveBeenCalledOnce();
    expect(subject.bindCanvasIdentity).toHaveBeenCalledOnce();
    expect(subject.onQueueSubmitted).toHaveBeenCalledOnce();
    expect(subject.onQueueAccepted).toHaveBeenCalledOnce();
    expect(subject.onQueueFailed).not.toHaveBeenCalled();
    expect(subject.queuePrompt).toHaveBeenCalledOnce();
  });

  it.each([
    ["wrong base revision", { ...lowering, baseReportRevision: 1 }],
    ["non-object authority", null],
  ])(
    "refuses a %s before template read, compile, canvas write or queue",
    async (_case, descriptor) => {
      const subject = environment();
      await expect(
        createAppModeController(subject.app, subject.api, {
          loadProfile: loadAvailableProfile,
          loadTemplate: vi.fn(loadSyntheticTemplate),
        }).start(inputs, {
          replaceExisting: true,
          preparedCanonicalPrompt: descriptor as ProductionCanonicalLowering,
          prepareManaged: subject.prepareManaged,
        }),
      ).rejects.toMatchObject({ code: "invalid_request" });
      expect(subject.loadGraphData).not.toHaveBeenCalled();
      expect(subject.graphToPrompt).not.toHaveBeenCalled();
      expect(subject.prepareManaged).not.toHaveBeenCalled();
      expect(subject.queuePrompt).not.toHaveBeenCalled();
    },
  );

  it("refuses a compiled AuditOverride that drifts from the prepared authority", async () => {
    const subject = environment(
      compiledPreparedPrompt({ prompt_text: "a stale canonical prompt" }),
    );
    await expect(
      createAppModeController(subject.app, subject.api, {
        loadProfile: loadAvailableProfile,
        loadTemplate: loadSyntheticTemplate,
      }).start(inputs, {
        replaceExisting: true,
        preparedCanonicalPrompt: lowering,
        prepareManaged: subject.prepareManaged,
      }),
    ).rejects.toMatchObject({ code: "compile_failed" });
    expect(subject.graphToPrompt).toHaveBeenCalledOnce();
    expect(subject.prepareManaged).not.toHaveBeenCalled();
    expect(subject.queuePrompt).not.toHaveBeenCalled();
  });

  it("refuses a host rewrite of the owned AuditOverride prompt before queue", async () => {
    const subject = environment(compiledPreparedPrompt(), (candidate) => {
      const audit = candidate.nodes?.find(
        (node) => node.type === "comfyui_h3_context.H3Context.AuditOverride",
      );
      if (Array.isArray(audit?.widgets_values))
        audit.widgets_values[3] = "host-rewritten canonical prompt";
    });
    await expect(
      createAppModeController(subject.app, subject.api, {
        loadProfile: loadAvailableProfile,
        loadTemplate: loadSyntheticTemplate,
      }).start(inputs, {
        replaceExisting: true,
        preparedCanonicalPrompt: lowering,
        prepareManaged: subject.prepareManaged,
      }),
    ).rejects.toMatchObject({ code: "stale_graph" });
    expect(subject.graphToPrompt).toHaveBeenCalledOnce();
    expect(subject.prepareManaged).toHaveBeenCalledOnce();
    expect(subject.onQueueFailed).toHaveBeenCalledWith("not_invoked");
    expect(subject.queuePrompt).not.toHaveBeenCalled();
  });
});
