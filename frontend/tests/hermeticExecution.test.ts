import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  appModeArtifactPrefix,
  createAppModeController,
  type AppModeInputs,
} from "../src/host/appMode";
import { communityCanvas } from "./support/connectFixture";
import { loadAvailableProfile } from "./support/generationProfileFixture";
import {
  createHermeticExecution,
  executeCompiledPrompt,
  type CompiledOutput,
} from "./support/hermeticExecutor";
import {
  ARTIFACT_SINK_IDS,
  createQualifiedBasePrompt,
} from "./support/qualifiedBasePrompt";
import { syntheticPromptUuid } from "./support/queuePromptTestDouble";
import { loadSyntheticTemplate } from "./support/templateFixture";
import { fixtureBackedAppModeHost } from "./support/hostSeamTestDouble";

/**
 * M17-20 phase 5b, plan section 5 lane 3.
 *
 * Every other lane in this item stops at the queue seam, because that is where
 * App Mode's authority stops. These rows carry on past it with a model-free
 * executor: the compiled prompt is traversed the way the host traverses it, and
 * what is asserted is control flow -- that the graph reaches an artifact sink,
 * that it reaches it once, that a failure or an interrupt reaches it not at all,
 * and that an edited revision reaches a different one.
 *
 * The lane claims nothing about output. No weight is resolved and no frame is
 * produced here; AC-M17-20-08 keeps that claim for the separately authorized
 * weight-backed sample, and a green run of this file is not evidence for it.
 */

const loadTemplate = vi.fn(loadSyntheticTemplate);
const loadProfile = vi.fn(loadAvailableProfile);

beforeEach(() => {
  loadTemplate.mockClear();
  loadProfile.mockClear();
});

const revisionA: AppModeInputs = {
  task_mode: "t2va",
  user_intent: "A red kite crosses the sky while the camera follows its arc.",
  duration_milliseconds: 5167,
  frame_count: 124,
};
const revisionB: AppModeInputs = {
  ...revisionA,
  user_intent: "A red kite crosses the sky and the camera holds still.",
};

type Execution = ReturnType<typeof createHermeticExecution>;

function hostOn(canvas: unknown, execution: Execution) {
  let graph: unknown = canvas;
  const { app, api } = fixtureBackedAppModeHost(
    {
      loadApiJson: vi.fn(),
      loadGraphData: vi.fn((value: unknown) => {
        graph = value;
      }),
      graphToPrompt: vi.fn(),
      graph: { serialize: () => graph },
    },
    { queuePrompt: vi.fn(execution.queuePrompt) },
  );
  return {
    app,
    api,
    graph: () => graph,
  };
}

async function materialize(
  inputs: AppModeInputs,
  execution: Execution,
  options: { filenamePrefix?: string } = {},
  start: Parameters<
    ReturnType<typeof createAppModeController>["start"]
  >[1] = {},
) {
  const host = hostOn({ nodes: [] }, execution);
  host.app.graphToPrompt.mockImplementation(async () => ({
    output: createQualifiedBasePrompt(
      inputs,
      {},
      {
        filenamePrefix: options.filenamePrefix ?? appModeArtifactPrefix(inputs),
        sharedDurationSource: true,
      },
    ),
    workflow: {},
  }));
  const result = await createAppModeController(host.app, host.api, {
    loadTemplate,
    loadProfile,
  }).start(inputs, start);
  return { ...host, result };
}

describe("a queued App Mode graph is one a model-free executor can run", () => {
  it("walks the whole flow, dependencies first, and writes where the run says", async () => {
    const execution = createHermeticExecution();
    await materialize(revisionA, execution);
    const outcome = execution.run();

    expect(outcome.status).toBe("executed");
    // The context chain, the native anchor and the decode/container/sink chain
    // all execute. A conditioning-only graph -- the defect this item exists to
    // stop -- would be missing everything from the decode onwards.
    expect(outcome.executed).toEqual(
      expect.arrayContaining([
        "1",
        "2",
        "3",
        "4",
        "5",
        "8",
        "6",
        ARTIFACT_SINK_IDS.decode,
        ARTIFACT_SINK_IDS.container,
        ARTIFACT_SINK_IDS.sink,
      ]),
    );
    expect(outcome.executed.indexOf("6")).toBeLessThan(
      outcome.executed.indexOf(ARTIFACT_SINK_IDS.sink),
    );
    expect(outcome.artifacts).toEqual([
      {
        nodeId: ARTIFACT_SINK_IDS.sink,
        filenamePrefix: appModeArtifactPrefix(revisionA),
      },
    ]);
  });

  it("submits once and produces one artifact", async () => {
    const execution = createHermeticExecution();
    const host = await materialize(revisionA, execution);
    const outcome = execution.run();

    expect(host.api.queuePrompt).toHaveBeenCalledTimes(1);
    expect(execution.submissions).toHaveLength(1);
    expect(outcome.artifacts).toHaveLength(1);
    expect(host.result.queuePromptId).toBe(execution.submissions[0]?.promptId);
  });

  it("executes the preview without counting it as an artifact", async () => {
    // The H3 Preview node is an OUTPUT_NODE on the host but writes no file.
    // Counting host output nodes instead of artifact sinks is how a shell ends
    // up believing a preview-only run produced something.
    const execution = createHermeticExecution();
    await materialize(revisionA, execution);
    const outcome = execution.run();

    expect(outcome.executed).toContain("7");
    expect(outcome.artifacts.map((artifact) => artifact.nodeId)).toEqual([
      ARTIFACT_SINK_IDS.sink,
    ]);
  });

  it("leaves a canvas whose second run writes somewhere else", async () => {
    const execution = createHermeticExecution();
    await materialize(revisionA, execution);
    const first = execution.run();
    await materialize(revisionB, execution);
    const second = execution.run();

    expect(first.artifacts[0]?.filenamePrefix).toBe(
      appModeArtifactPrefix(revisionA),
    );
    expect(second.artifacts[0]?.filenamePrefix).toBe(
      appModeArtifactPrefix(revisionB),
    );
    // The edited revision cannot overwrite the artifact of the revision it
    // replaced, and the replaced artifact cannot be mistaken for its result.
    expect(second.artifacts[0]?.filenamePrefix).not.toBe(
      first.artifacts[0]?.filenamePrefix,
    );
  });

  it("writes to the user's own sink on the connect route", async () => {
    const execution = createHermeticExecution();
    const host = hostOn(communityCanvas(), execution);
    host.app.graphToPrompt.mockImplementation(async () => ({
      output: createQualifiedBasePrompt(
        revisionA,
        {},
        { filenamePrefix: "their-project/take-1" },
      ),
      workflow: {},
    }));
    await createAppModeController(host.app, host.api, {
      loadTemplate,
      loadProfile,
    }).start(revisionA, { connectExisting: {} });
    const outcome = execution.run();

    // D13: the sink belongs to the graph the user assembled, so the run lands
    // where they said, not where a materialized run would have put it.
    expect(outcome.artifacts).toEqual([
      {
        nodeId: ARTIFACT_SINK_IDS.sink,
        filenamePrefix: "their-project/take-1",
      },
    ]);
  });
});

describe("a run without a compatible artifact remains unverified", () => {
  it("queues the operator graph without treating queue acceptance as delivery", async () => {
    const execution = createHermeticExecution();
    const host = hostOn({ nodes: [] }, execution);
    host.app.graphToPrompt.mockImplementation(async () => ({
      output: createQualifiedBasePrompt(
        revisionA,
        {},
        {
          artifactSink: false,
          sharedDurationSource: true,
        },
      ),
      workflow: {},
    }));
    await expect(
      createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(revisionA),
    ).resolves.toMatchObject({ queuePromptId: syntheticPromptUuid(1) });
    expect(host.api.queuePrompt).toHaveBeenCalledOnce();

    // The preview is an output node, so host execution can complete without a
    // file. The managed coordinator still requires a verified bounded video
    // event before Production may call the run succeeded.
    const output = createQualifiedBasePrompt(
      revisionA,
      {},
      {
        artifactSink: false,
      },
    ) as CompiledOutput;
    const outcome = executeCompiledPrompt("p-unqueued", output);
    expect(outcome.status).toBe("executed");
    expect(outcome.artifacts).toHaveLength(0);

    // With the preview gone as well there is nothing to execute at all.
    delete output["7"];
    expect(executeCompiledPrompt("p-empty", output)).toMatchObject({
      status: "error",
      reason: "no_output",
    });
  });

  it("keeps a queue acceptance from standing in for a result", async () => {
    // The queue returning a prompt id says the host took the work. It says
    // nothing about the work succeeding, and the identity join must not treat
    // the two as the same event.
    const execution = createHermeticExecution({
      failAt: "MiniMaxH3ImageToVideo",
    });
    const host = await materialize(revisionA, execution);
    const outcome = execution.run();

    expect(host.result.queuePromptId).toBe(execution.submissions[0]?.promptId);
    expect(outcome).toMatchObject({
      status: "error",
      reason: "node_failed",
      nodeId: "6",
    });
    expect(outcome.artifacts).toHaveLength(0);
    expect(outcome.executed).not.toContain(ARTIFACT_SINK_IDS.sink);
  });

  it("writes nothing when the run is interrupted", async () => {
    const execution = createHermeticExecution({ interruptAfter: 3 });
    await materialize(revisionA, execution);
    const outcome = execution.run();

    expect(outcome.status).toBe("interrupted");
    expect(outcome.artifacts).toHaveLength(0);
  });

  it("fails a run whose graph has a dangling link", async () => {
    // The executor has to be capable of failing, or every row above would pass
    // for a graph that could never run.
    const output = createQualifiedBasePrompt(revisionA) as CompiledOutput;
    delete output[ARTIFACT_SINK_IDS.container];
    expect(executeCompiledPrompt("p-dangling", output)).toMatchObject({
      status: "error",
      reason: "missing_node",
      nodeId: ARTIFACT_SINK_IDS.container,
    });
  });
});

describe("host ownership begins at submission", () => {
  it("does not unqueue work the user aborted after it was submitted", async () => {
    // App Mode deliberately does not claim a cancellation or a rollback once
    // queuePrompt has been called. This is what that decision looks like from
    // the host's side: the work runs, and the canvas is still the one that was
    // queued.
    const execution = createHermeticExecution();
    const controller = new AbortController();
    const host = await materialize(
      revisionA,
      execution,
      {},
      {
        signal: controller.signal,
        onQueueSubmitted: () => controller.abort(),
      },
    );
    const outcome = execution.run();

    expect(host.result.queuePromptId).toBe(execution.submissions[0]?.promptId);
    expect(outcome.status).toBe("executed");
    expect(outcome.artifacts).toHaveLength(1);
    const nodes = (host.graph() as { nodes: { type?: string }[] }).nodes;
    expect(nodes.some((node) => node.type === "SaveVideo")).toBe(true);
  });
});
