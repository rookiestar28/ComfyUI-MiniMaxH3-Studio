import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  appModeArtifactPrefix,
  createAppModeController,
  type AppModeInputs,
} from "../src/host/appMode";
import {
  H3_SHELL_MANIFEST,
  inspectVisibleH3Graph,
} from "../src/host/graphAdapter";
import { createQualifiedBasePrompt } from "./support/qualifiedBasePrompt";
import { loadSyntheticTemplate } from "./support/templateFixture";
import { loadAvailableProfile } from "./support/generationProfileFixture";
import { fixtureBackedAppModeHost } from "./support/hostSeamTestDouble";
import { acceptedQueueResponse } from "./support/queuePromptTestDouble";

/**
 * M17-20 phase 4, D7. The edit path itself is not new: an accepted Context
 * revision goes through the existing sidebar workspace transaction and then
 * Production `replace_segment_from_context`, and the backend already refuses to
 * reuse a receipt whose segment identity moved (M17-12 selective rerun). What is
 * new is what App Mode does with the second revision, and that is what these
 * rows assert: a regeneration produces a different full graph, a different
 * compiled identity and a different artifact location, so the artifact of the
 * revision it replaced can neither satisfy it nor be overwritten by it.
 *
 * No edit surface is exercised here because M17-20 introduces none. What is
 * exercised is the consequence of an edit reaching the shell.
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

/**
 * A host whose canvas actually changes when a graph is loaded.
 *
 * The fixed-canvas stub used elsewhere cannot express this subject at all: two
 * revisions would report the same graph identity because the stub reports the
 * same graph.
 */
function mutableHost() {
  let graph: unknown = { nodes: [] };
  const { app, api } = fixtureBackedAppModeHost(
    {
      loadApiJson: vi.fn(),
      loadGraphData: vi.fn((value: unknown) => {
        graph = value;
      }),
      graphToPrompt: vi.fn(),
      graph: { serialize: () => graph },
    },
    { queuePrompt: vi.fn().mockResolvedValue(acceptedQueueResponse(1)) },
  );
  return {
    app,
    api,
    graph: () => graph,
  };
}

async function materialize(
  inputs: AppModeInputs,
  options: { artifactScope?: string } = {},
) {
  const host = mutableHost();
  host.app.graphToPrompt.mockImplementation(async () => ({
    output: createQualifiedBasePrompt(
      inputs,
      {},
      {
        filenamePrefix: appModeArtifactPrefix(inputs, options.artifactScope),
        sharedDurationSource: true,
      },
    ),
    workflow: {},
  }));
  const result = await createAppModeController(host.app, host.api, {
    loadTemplate,
    loadProfile,
  }).start(inputs, options);
  const nodes = (host.graph() as { nodes: { type?: string }[] }).nodes;
  const sink = nodes.find((node) => node.type === "SaveVideo") as {
    widgets_values?: unknown[];
  };
  return {
    ...host,
    result,
    prefix: sink?.widgets_values?.[0],
  };
}

describe("an edited revision regenerates into its own graph, identity and artifact", () => {
  it("moves all three when the accepted revision changes", async () => {
    const first = await materialize(revisionA);
    const second = await materialize(revisionB);

    expect(second.result.graphFingerprint).not.toBe(
      first.result.graphFingerprint,
    );
    expect(second.result.compiledPromptFingerprint).not.toBe(
      first.result.compiledPromptFingerprint,
    );
    // The artifact location is the part that makes the previous output
    // unreachable by the new revision: a regeneration cannot overwrite what it
    // replaced, and the old file cannot be mistaken for the new one.
    expect(second.prefix).not.toBe(first.prefix);
    expect(first.prefix).toBe(appModeArtifactPrefix(revisionA));
    expect(second.prefix).toBe(appModeArtifactPrefix(revisionB));
  });

  it("keeps a retry a retry", async () => {
    // Re-running the same accepted revision is not an edit, so it must not
    // scatter output across locations that no revision explains.
    const first = await materialize(revisionA);
    const again = await materialize(revisionA);
    expect(again.prefix).toBe(first.prefix);
    expect(again.result.graphFingerprint).toBe(first.result.graphFingerprint);
  });

  it("counts a media change as an edit", async () => {
    // An edit is not only text. Two runs that differ only in which visible node
    // supplies the first frame are different revisions and must not share an
    // artifact location.
    const withFirst: AppModeInputs = {
      ...revisionA,
      task_mode: "i2va",
      first_frame_source: "9",
    };
    const withOther: AppModeInputs = { ...withFirst, first_frame_source: "10" };
    expect(appModeArtifactPrefix(withFirst)).not.toBe(
      appModeArtifactPrefix(withOther),
    );
  });

  it("keeps two workspaces out of each other's output", async () => {
    const one = await materialize(revisionA, { artifactScope: "workspace.1" });
    const two = await materialize(revisionA, { artifactScope: "workspace.2" });
    expect(one.prefix).not.toBe(two.prefix);
    // The artifact location is inside the compiled prompt, so the identity the
    // sequence authority stores already distinguishes where an attempt wrote.
    // That is how preview and export join the exact attempt without the shell
    // reporting a path of its own.
    expect(one.result.compiledPromptFingerprint).not.toBe(
      two.result.compiledPromptFingerprint,
    );
    expect(one.result.graphFingerprint).not.toBe(two.result.graphFingerprint);
  });

  it("refuses the previous revision's identity for the new one", async () => {
    // The sequence authority hands App Mode the identity it planned. After an
    // edit that identity belongs to the revision being replaced, and accepting
    // it would queue the new graph under the old attempt.
    const first = await materialize(revisionA);
    const host = mutableHost();
    host.app.graphToPrompt.mockImplementation(async () => ({
      output: createQualifiedBasePrompt(
        revisionB,
        {},
        {
          filenamePrefix: appModeArtifactPrefix(revisionB),
          sharedDurationSource: true,
        },
      ),
      workflow: {},
    }));
    await expect(
      createAppModeController(host.app, host.api, {
        loadTemplate,
        loadProfile,
      }).start(revisionB, {
        expectedIdentity: {
          graphFingerprint: first.result.graphFingerprint,
          compiledPromptFingerprint: first.result.compiledPromptFingerprint,
        },
      }),
    ).rejects.toMatchObject({ code: "compile_failed" });
    expect(host.api.queuePrompt).not.toHaveBeenCalled();
  });

  it("leaves a canvas the user can keep working on", async () => {
    // AC-M17-20-05: continuing after a run must not require switching workflows.
    // The regenerated canvas has to be one App Mode can bind again, or the next
    // edit would have to discard it.
    const second = await materialize(revisionB);
    expect(
      inspectVisibleH3Graph(second.graph(), H3_SHELL_MANIFEST),
    ).toMatchObject({ status: "ready", existingGraphCompatible: true });
  });
});
