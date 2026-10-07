import { describe, expect, it } from "vitest";

import {
  HOST_BEHAVIOUR_CATALOGUE,
  HOST_WORKFLOW_BEHAVIOUR_VARIANT_IDS,
  assertHostBehaviourCatalogue,
  createHostWorkflowBehaviourFixture,
} from "./fixtures/hostBehaviourCatalogue";
import {
  createNoisyHostExtension,
  fixtureBackedAppModeHost,
} from "./support/hostSeamTestDouble";

describe("M23-26 host behaviour catalogue", () => {
  it("names every hermetic host-noise seam and its observed frontend", () => {
    expect(() =>
      assertHostBehaviourCatalogue(HOST_BEHAVIOUR_CATALOGUE),
    ).not.toThrow();
    expect(HOST_BEHAVIOUR_CATALOGUE.map(({ id }) => id)).toEqual([
      "workflow.empty_store_bootstrap",
      "workflow.null_capture_creates_temporary",
      "queue.envelope_metadata",
      "callbacks.foreign_graph_metadata",
      "widgets.seed_rerandomization",
    ]);
    expect(
      new Set(
        HOST_BEHAVIOUR_CATALOGUE.map(
          ({ observedFrontend }) => observedFrontend,
        ),
      ),
    ).toEqual(new Set(["1.48.7", "1.51.9"]));
  });

  it("is immutable fixture data without private observation payloads", () => {
    expect(Object.isFrozen(HOST_BEHAVIOUR_CATALOGUE)).toBe(true);
    for (const entry of HOST_BEHAVIOUR_CATALOGUE) {
      expect(Object.isFrozen(entry)).toBe(true);
      expect(Object.keys(entry).sort()).toEqual([
        "effect",
        "id",
        "observedFrontend",
      ]);
      expect(JSON.stringify(entry)).not.toMatch(
        /(?:https?:\/\/|[A-Z]:\\|prompt_text|credential|cookie)/i,
      );
    }
  });

  it("drives foreign metadata, layout and seed changes through the noisy host callbacks", () => {
    const extension = createNoisyHostExtension();
    const sampler = {
      id: 7,
      type: "KSampler",
      pos: [10, 20],
      properties: {},
      widgets_values: [41, "fixed"],
    };
    const workflow = { nodes: [sampler], extra: {} };

    extension.nodeCreated(sampler);
    extension.afterConfigureGraph(workflow);
    expect(sampler.properties).toEqual({ cnr_id: "fixture-noisy-pack" });
    expect(workflow).toMatchObject({
      extra: {
        ds: { scale: 1.01, offset: [1, 1] },
        h3NoisyHostFixture: { revision: 1 },
      },
      widget_idx_map: { "7": { seed: 0 } },
      seed_widgets: { "7": 0 },
    });
    expect(sampler.pos).toEqual([11, 20]);
    expect(sampler.widgets_values).toEqual([42, "fixed"]);

    extension.afterConfigureGraph(workflow);
    expect(workflow.extra).toMatchObject({
      ds: { scale: 1.02, offset: [2, 2] },
      h3NoisyHostFixture: { revision: 2 },
    });
    expect(sampler.pos).toEqual([13, 20]);
    expect(sampler.widgets_values).toEqual([44, "fixed"]);
  });

  it("binds every executable workflow variant without normalizing its initial state", () => {
    expect(HOST_WORKFLOW_BEHAVIOUR_VARIANT_IDS).toEqual([
      "null_empty_exact",
      "existing_active",
      "foreign_open_inconsistent",
      "identity_drift",
      "count_drift",
    ]);

    const nullHost = fixtureBackedAppModeHost(
      {
        extensionManager: {
          workflow: { activeWorkflow: {}, openWorkflows: [] },
        },
      },
      {},
      { workflowVariant: "null_empty_exact" },
    );
    expect(nullHost.app.extensionManager.workflow.activeWorkflow).toBeNull();
    expect(nullHost.app.extensionManager.workflow.openWorkflows).toEqual([]);
    expect(nullHost.workflowBehaviour?.trace).toEqual([]);

    const existingHost = fixtureBackedAppModeHost(
      {
        extensionManager: {
          workflow: { activeWorkflow: {}, openWorkflows: [] },
        },
      },
      {},
      { workflowVariant: "existing_active" },
    );
    expect(existingHost.app.extensionManager.workflow.activeWorkflow).toBe(
      existingHost.workflowBehaviour?.authority,
    );
    expect(existingHost.app.extensionManager.workflow.openWorkflows).toEqual([
      existingHost.workflowBehaviour?.authority,
    ]);

    const foreignHost = fixtureBackedAppModeHost(
      {
        extensionManager: {
          workflow: { activeWorkflow: {}, openWorkflows: [] },
        },
      },
      {},
      { workflowVariant: "foreign_open_inconsistent" },
    );
    expect(foreignHost.app.extensionManager.workflow.activeWorkflow).toBeNull();
    expect(foreignHost.app.extensionManager.workflow.openWorkflows).toEqual([
      foreignHost.workflowBehaviour?.foreignWorkflow,
    ]);
  });

  it.each([
    ["null_empty_exact", "opened", 1],
    ["identity_drift", "foreign", 1],
    ["count_drift", "opened", 2],
  ] as const)(
    "executes the catalogue-owned %s create/open transition",
    async (variant, activeDisposition, openCount) => {
      const behaviour = createHostWorkflowBehaviourFixture(variant);
      const created = behaviour.workflowStore.createNewTemporary(
        "m25-08-authorized-video-acceptance.json",
        { nodes: [], links: [] },
      );

      await expect(behaviour.workflowStore.openWorkflow(created)).resolves.toBe(
        created,
      );
      expect(behaviour.workflowStore.activeWorkflow).toBe(
        activeDisposition === "foreign"
          ? behaviour.foreignWorkflow
          : behaviour.openedAuthority,
      );
      expect(behaviour.workflowStore.openWorkflows).toHaveLength(openCount);
      expect(behaviour.trace).toEqual([
        "createNewTemporary",
        "openWorkflow",
        "activeWorkflow",
        "openMembership",
      ]);
    },
  );
});
