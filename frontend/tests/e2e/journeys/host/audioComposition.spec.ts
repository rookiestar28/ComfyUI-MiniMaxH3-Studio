import { expect, test } from "../../host/fixture";
import { projectionFromOutput } from "../../../../src/host/sidebarHost";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import {
  hostUrl,
  candidateInjectionCount,
  assertCandidateBundleInjection,
  waitForH3Registration,
  openH3AppModeTab,
  waitForHostGraphSettled,
} from "../../host/environment";

test("audio-only reference exports a prompt but holds unqualified native execution", async ({
  context,
  page,
}) => {
  test.setTimeout(90_000);
  if (hostUrl === undefined)
    throw new Error("explicit supplied host is required");
  const injections = candidateInjectionCount(context);
  let submissions = 0;
  page.on("request", (request) => {
    if (
      new URL(request.url()).pathname === "/prompt" &&
      request.method() === "POST"
    )
      submissions++;
  });
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await waitForH3Registration(page);
  await assertCandidateBundleInjection(page, context, injections);
  const panel = await openH3AppModeTab(page, "h3-audio-composition-held");
  // A metadata-free opaque AUDIO placeholder exercises the public reference socket. It is
  // deliberately not decoded or generation-qualified; no media file or model is accessed.
  const prompt = {
    "1": {
      class_type: "comfyui_h3_context.H3Context.Request",
      inputs: {
        task_mode: "ref2va",
        user_intent: "Preserve the declared reference audio.",
        duration_seconds: 5,
      },
    },
    "2": {
      class_type: "comfyui_h3_context.H3Context.ReferenceRegistry",
      inputs: {
        audios: { waveform: [], sample_rate: 32000 },
      },
    },
    "4": {
      class_type: "comfyui_h3_context.H3Context.Plan",
      inputs: {
        request: ["1", 0],
        reference_registry: ["2", 0],
      },
    },
    "5": {
      class_type: "comfyui_h3_context.H3Context.Compiler",
      inputs: { plan: ["4", 0] },
    },
    "6": {
      class_type: "comfyui_h3_context.H3Context.Validator",
      inputs: {
        plan: ["4", 0],
        prompt_document: ["5", 2],
      },
    },
    "7": {
      class_type: "comfyui_h3_context.H3Context.NativeH3Adapter",
      inputs: { report: ["6", 1] },
    },
    "8": {
      class_type: "comfyui_h3_context.H3Context.ProductShell",
      inputs: {
        report: ["6", 1],
        native_h3_wiring: ["7", 1],
      },
    },
  };
  await page.evaluate(
    async (value) => {
      const runtime = window as any;
      await runtime.comfyAPI.app.app.loadApiJson(
        value,
        "audio-only-held-model-free",
      );
      runtime.__h3AudioHeld = null;
      runtime.comfyAPI.api.api.addEventListener(
        "executed",
        (event: CustomEvent) => {
          if (String(event.detail?.node) === "8")
            runtime.__h3AudioHeld = event.detail.output;
        },
      );
    },
    {
      ...prompt,
      // The sidebar requires its visible native anchor. It is never included in
      // the submitted prompt, so this observation cannot execute a native model.
      "9": {
        class_type: "MiniMaxH3ReferenceToVideo",
        inputs: { prompt: ["8", 0], width: 512, height: 512, length: 124 },
      },
    },
  );
  await waitForHostGraphSettled(page);
  const before = await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const store = app.extensionManager.workflow;
    runtime.__h3AudioWorkflow = store.activeWorkflow;
    return {
      tabs: store.openWorkflows.length,
      graph: app.graph.serialize(),
      anchorPromptLink: app.graph
        .getNodeById(9)
        .inputs.find((input: any) => input.name === "prompt").link,
      nodes: app.graph._nodes
        .filter((node: any) => node.type.startsWith("comfyui_h3_context."))
        .map((node: any) => ({
          id: node.id,
          type: node.type,
        })),
    };
  });
  const response = await page.evaluate(async (value) => {
    const api = (window as any).comfyAPI.api.api;
    const result = await fetch("/prompt", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ client_id: api.clientId, prompt: value }),
    });
    return { status: result.status };
  }, prompt);
  expect(response.status).toBe(200);
  await page.waitForFunction(() => (window as any).__h3AudioHeld !== null);
  const output = await page.evaluate(() => (window as any).__h3AudioHeld);
  const { workspace, projection: shell } = projectionFromOutput(output);
  expect(shell.task_mode).toBe("ref2va");
  expect(shell.bindings.map((binding) => binding.kind)).toEqual(["audio"]);
  expect(shell.prompt_export_ready).toBe(true);
  expect(shell.native_queue_ready).toBe(false);
  expect(shell.limitations).toContain("native_duration_unqualified");
  expect(workspace.lifecycle).toBe("blocked");
  expect(workspace.actions.export).toBe(false);
  expect(workspace.actions.copy_prompt).toBe(false);
  expect(workspace.media_receipt.queue_ready).toBe(false);
  await expect(panel.locator('[data-shell-status="projected"]')).toHaveCount(1);
  await expect(
    panel.locator('[data-native-readiness="unqualified"]'),
  ).toBeVisible();
  const after = await page.evaluate(() => {
    const runtime = window as any;
    const app = runtime.comfyAPI.app.app;
    const store = app.extensionManager.workflow;
    return {
      sameWorkflow: store.activeWorkflow === runtime.__h3AudioWorkflow,
      tabs: store.openWorkflows.length,
      graph: app.graph.serialize(),
      anchorPromptLink: app.graph
        .getNodeById(9)
        .inputs.find((input: any) => input.name === "prompt").link,
      packs: app.extensions
        .map((extension: any) => extension.name)
        .filter((name: unknown) => typeof name === "string"),
      nodes: app.graph._nodes
        .filter((node: any) => node.type.startsWith("comfyui_h3_context."))
        .map((node: any) => ({
          id: node.id,
          type: node.type,
        })),
    };
  });
  expect(after.sameWorkflow).toBe(true);
  expect(after.tabs).toBe(before.tabs);
  expect(after.nodes).toEqual(before.nodes);
  expect(before.anchorPromptLink).not.toBeNull();
  expect(after.anchorPromptLink).toBe(before.anchorPromptLink);
  // All links in this model-free graph connect the nodes written by this journey.
  // Compare their owned socket endpoints; foreign graph metadata remains evidence only.
  expect(after.graph.links).toEqual(before.graph.links);
  const surroundings = diffGraphSurroundings({
    beforeValue: before.graph,
    afterValue: after.graph,
    reference: {
      ownedNodeIds: before.nodes.map((node: any) => String(node.id)),
      ownedLinkIds: before.graph.links.map((link: any[]) => String(link[0])),
      anchorNodeId: "9",
      ownedProjectionEqual: true,
    },
  });
  expect(submissions).toBe(1); // The sole submission contains only our model-free context nodes.
  // Only content-free facts survive the host lane's deliberate snapshot cleanup.
  console.log(
    "AUDIO_COMPOSITION_RECEIPT " +
      JSON.stringify({
        task_mode: shell.task_mode,
        native_queue_ready: shell.native_queue_ready,
        reason: shell.readiness_reason,
        lifecycle: workspace.lifecycle,
        model_free_submissions: submissions,
        native_submissions: 0,
        same_workflow: after.sameWorkflow,
        open_tabs: after.tabs,
        surroundings,
        installed_pack_census: after.packs,
      }),
  );
});
