import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { build } from "vite";
import { test, expect } from "../../host/fixture";
import {
  hostUrl,
  repositoryRoot,
  candidateBundle,
  candidateInjectionCount,
  assertCandidateBundleInjection,
} from "../../host/candidate";
import {
  waitForH3Registration,
  captureM17CanonicalIdentity,
} from "../../host/network";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import type { RuntimeCapabilityObservation } from "../../../../src/runtime/mediaCapabilities";

test("M25-12 observes supplied-host capability without adding a product surface", async ({
  page,
  context,
}, testInfo) => {
  if (!hostUrl || !process.env.H3_CONTEXT_HOST_ROOT || candidateBundle === null)
    throw new Error(
      "M25-12 requires an explicitly supplied pinned host and exact candidate bundle",
    );
  const built = await build({
    configFile: false,
    logLevel: "silent",
    build: {
      write: false,
      minify: false,
      lib: {
        entry: resolve(
          repositoryRoot,
          "frontend/src/runtime/mediaCapabilities.ts",
        ),
        name: "H3RuntimeProbe",
        formats: ["iife"],
      },
    },
  });
  const emitted = Array.isArray(built) ? built : [built];
  const outputs = emitted.flatMap((result) =>
    "output" in result ? result.output : [],
  );
  if (outputs.length !== 1 || outputs[0]?.type !== "chunk")
    throw new Error("capability-only probe must be one in-memory script");
  const script = outputs[0].code;
  const injectionBefore = candidateInjectionCount(context);
  const response = await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, injectionBefore);
  const graphBefore = await captureM17CanonicalIdentity(page);
  const pageCount = context.pages().length;
  let queueCalls = 0;
  const countQueue = (request: { method(): string; url(): string }) => {
    if (
      request.method() === "POST" &&
      /\/(?:api\/)?prompt$/.test(new URL(request.url()).pathname)
    )
      queueCalls += 1;
  };
  page.on("request", countQueue);
  // Only the actual pure capability module is evaluated. This harness never imports entry,
  // mounts a runtime, admits a source, changes workflow ownership or wraps a foreign callback.
  const measured = (await page.evaluate(`(() => {
    const app = window.comfyAPI.app.app;
    const store = app.extensionManager.workflow;
    if (!store || !Array.isArray(store.openWorkflows)) throw new Error('workflow identity unavailable');
    const active = store.activeWorkflow;
    const openCount = store.openWorkflows.length;
    const graph = app.graph;
    const mounts = document.querySelectorAll('[data-h3-context-mount]').length;
    const tabs = app.extensionManager.getSidebarTabs();
    const ownedTabs = tabs.filter(tab => tab.id === 'h3-context').length;
    ${script}
    const observation = H3RuntimeProbe.observeBrowserMediaCapabilities();
    return {
      observation,
      graphIdentityStable: app.graph === graph,
      workflowIdentityStable: store.activeWorkflow === active,
      openWorkflowCountStable: store.openWorkflows.length === openCount,
      ownedMountCountStable: document.querySelectorAll('[data-h3-context-mount]').length === mounts,
      ownedSidebarCountStable: app.extensionManager.getSidebarTabs().filter(tab => tab.id === 'h3-context').length === ownedTabs,
      installedSidebarCensus: tabs.map(tab => tab.id).filter(id => typeof id === 'string').sort()
    };
  })()`)) as {
    observation: RuntimeCapabilityObservation;
    graphIdentityStable: boolean;
    workflowIdentityStable: boolean;
    openWorkflowCountStable: boolean;
    ownedMountCountStable: boolean;
    ownedSidebarCountStable: boolean;
    installedSidebarCensus: string[];
  };
  page.off("request", countQueue);
  const graphAfter = await captureM17CanonicalIdentity(page);
  const surroundings = diffGraphSurroundings({
    beforeValue: JSON.parse(graphBefore.graph),
    afterValue: JSON.parse(graphAfter.graph),
    reference: {
      ownedNodeIds: [],
      ownedLinkIds: [],
      anchorNodeId: "",
      ownedProjectionEqual: true,
    },
  });
  const headers = await response?.allHeaders();
  expect(measured.observation.htmlMediaElement).toBe(true);
  expect(["maybe", "probably"]).toContain(
    measured.observation.canPlayMp4H264Aac,
  );
  expect(measured.observation.requestVideoFrameCallback).toBe(true);
  expect(measured.observation.canvas2d).toBe(true);
  expect(
    measured.graphIdentityStable &&
      measured.workflowIdentityStable &&
      measured.openWorkflowCountStable &&
      measured.ownedMountCountStable &&
      measured.ownedSidebarCountStable,
  ).toBe(true);
  expect(context.pages().length).toBe(pageCount);
  expect(queueCalls).toBe(0);
  await testInfo.attach("m25-12-host-capability", {
    contentType: "application/json",
    body: JSON.stringify({
      schema: "h3.context.m25_12.host_capability.v1",
      ...measured,
      queueCalls,
      pageCountStable: true,
      surroundings,
      candidateBundleSha256: candidateBundle.sha256,
      probeSha256: createHash("sha256").update(script).digest("hex"),
      headersPresent: {
        csp: Boolean(headers?.["content-security-policy"]),
        coop: Boolean(headers?.["cross-origin-opener-policy"]),
        coep: Boolean(headers?.["cross-origin-embedder-policy"]),
      },
      scope:
        "capability and no-new-surface only; encoded transport qualification is a separate hermetic receipt",
    }),
  });
});
