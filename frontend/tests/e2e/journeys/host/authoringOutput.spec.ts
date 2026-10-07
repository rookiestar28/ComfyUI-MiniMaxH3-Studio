import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { diffGraphSurroundings } from "../../../support/surroundingsDiff";
import { expect, test } from "../../host/fixture";
import {
  assertCandidateBundleInjection,
  candidateInjectionCount,
  hostUrl,
  repositoryRoot,
} from "../../host/candidate";
import {
  decodeOutputStatus,
  decodeOutputError,
  type OutputStatus,
} from "../../../../src/contracts/authoringOutputCodec";

// This unmounted leaf uses real supplied-host routes. Only private harness resources
// are fulfilled; output requests retain the browser's actual same-origin metadata.
test("supplied host final output status preview cancel and native download", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.H3_OUTPUT_HOST_SAMPLE !== "1",
    "explicit queue-free host sample required",
  );
  // The real legacy workspace is 150 seconds at 1080p; retain its extent and
  // allow the declared 900-second renderer deadline plus bounded preview/cleanup.
  test.setTimeout(1_080_000);
  if (!hostUrl) throw new Error("supplied host required");
  const evidence = resolve(repositoryRoot, ".planning/evidence/260906-M25-19");
  const fixture = JSON.parse(
    await readFile(resolve(evidence, "host-output-fixture.json"), "utf8"),
  );
  const bundle = await readFile(resolve(evidence, "host-leaf/leaf.js"));
  const origin = new URL(hostUrl).origin;
  let queueCalls = 0;
  context.on("request", (request) => {
    if (
      new URL(request.url()).pathname === "/prompt" &&
      request.method() === "POST"
    )
      queueCalls++;
  });
  const injections = candidateInjectionCount(context);
  await page.goto(hostUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() =>
    (window as any).comfyAPI?.app?.app?.extensionManager
      ?.getSidebarTabs?.()
      .some((tab: any) => tab.id === "h3-context"),
  );
  await assertCandidateBundleInjection(page, context, injections);
  // Registration precedes the host's asynchronous initial graph load. Capture
  // authority only after that load, not its all-zero provisional graph identity.
  await page.waitForFunction(
    () => {
      const app = (window as any).comfyAPI.app.app;
      const workflow = app.extensionManager.workflow;
      return (
        typeof app.graph?.id === "string" &&
        app.graph.id !== "00000000-0000-0000-0000-000000000000" &&
        Array.isArray(workflow?.openWorkflows) &&
        workflow.activeWorkflow !== null &&
        workflow.openWorkflows.includes(workflow.activeWorkflow)
      );
    },
    undefined,
    { timeout: 60_000 },
  );
  const hostIdentity = () =>
    page.evaluate(() => {
      const app = (window as any).comfyAPI.app.app;
      return {
        tabs: app.extensionManager
          .getSidebarTabs()
          .map((tab: any) => tab.id)
          .sort(),
        graphId: app.graph?.id ?? null,
      };
    });
  const before = await hostIdentity();
  const graphBefore = await page.evaluate(() => {
    const app = (window as any).comfyAPI.app.app;
    const workflow = app.extensionManager.workflow;
    if (!Array.isArray(workflow?.openWorkflows))
      throw new Error("host workflow store unavailable");
    (window as any).__m19Workflow = {
      active: workflow.activeWorkflow,
      count: workflow.openWorkflows.length,
    };
    return app.graph.serialize();
  });
  const pagesBefore = context.pages().length;
  const leaf = await context.newPage();
  await leaf.route(
    (url) => url.origin === origin && url.pathname === "/__m19_leaf",
    (route) =>
      route.fulfill({
        contentType: "text/html",
        body: '<!doctype html><div id="output-harness"></div><script src="/__m19_leaf.js"></script>',
      }),
  );
  await leaf.route(origin + "/__m19_leaf.js", (route) =>
    route.fulfill({ contentType: "text/javascript", body: bundle }),
  );
  await leaf.route(origin + "/__output_fixture/bootstrap", (route) =>
    route.fulfill({ json: fixture }),
  );
  await leaf.addInitScript(() => {
    const counts = { created: 0, revoked: 0, originalFetch: 0 };
    (window as any).outputCounts = counts;
    const originalFetch = window.fetch.bind(window),
      create = URL.createObjectURL.bind(URL),
      revoke = URL.revokeObjectURL.bind(URL);
    window.fetch = (input, init) => {
      if (String(input).includes("/download")) counts.originalFetch++;
      return originalFetch(input, init);
    };
    URL.createObjectURL = (body) => {
      counts.created++;
      return create(body);
    };
    URL.revokeObjectURL = (url) => {
      counts.revoked++;
      revoke(url);
    };
  });
  let succeeded: OutputStatus | null = null;
  let terminal: OutputStatus | null = null;
  leaf.on("response", (response) => {
    if (
      new URL(response.url()).pathname.startsWith(
        "/h3-context/v1/authoring/render",
      ) &&
      response.status() === 200
    )
      void response
        .json()
        .then((wire) => {
          const status = decodeOutputStatus(wire);
          if (status.phase === "succeeded") succeeded = status;
          if (["succeeded", "failed", "cancelled"].includes(status.phase))
            terminal = status;
        })
        .catch(() => undefined);
  });
  try {
    await leaf.goto(origin + "/__m19_leaf?qualified=1");
    const creation = leaf.waitForResponse(
      (response) =>
        new URL(response.url()).pathname ===
          "/h3-context/v1/authoring/render" &&
        response.request().method() === "POST",
    );
    await leaf.getByRole("button", { name: "Render final video" }).click();
    const created = await creation;
    if (created.status() !== 200) {
      const error = decodeOutputError(await created.json(), created.status());
      throw new Error(`closed output create refusal: ${error.code}`);
    }
    expect(created.status(), "actual output create HTTP status").toBe(200);
    // Fail on a decoded terminal refusal rather than hiding it behind a long UI wait.
    await expect
      .poll(() => terminal?.phase ?? "pending", { timeout: 930_000 })
      .not.toBe("pending");
    const observed = terminal as unknown as OutputStatus;
    expect(
      observed.phase,
      `closed render failure: ${observed.failure ?? "none"}`,
    ).toBe("succeeded");
    await expect(leaf.getByText("Video ready", { exact: true })).toBeVisible({
      timeout: 930_000,
    });
    await expect.poll(() => succeeded !== null).toBe(true);
    const status = succeeded as unknown as OutputStatus;
    const cancel = await leaf.evaluate(
      async ({ job, workspace }) => {
        const response = await fetch(
          `/h3-context/v1/authoring/render/${job}/cancel`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              schema: "h3.authoring.output_cancel.v1",
              workspace_handle: workspace,
            }),
          },
        );
        return { code: response.status, wire: await response.json() };
      },
      { job: status.job_handle, workspace: status.workspace_handle },
    );
    expect(cancel.code).toBe(200);
    expect(decodeOutputStatus(cancel.wire)).toEqual(status);
    await leaf.getByRole("button", { name: "Preview output" }).click();
    const video = leaf.getByLabel("Final video preview");
    await expect(video).toHaveAttribute("src", /^blob:/, { timeout: 90_000 });
    await expect
      .poll(() =>
        video.evaluate((node) => (node as HTMLVideoElement).readyState),
      )
      .toBeGreaterThanOrEqual(1);
    expect(
      await video.evaluate((node) => (node as HTMLVideoElement).duration),
    ).toBeCloseTo(status.output!.frame_count / 24, 2);
    const event = leaf.waitForEvent("download");
    await leaf.getByRole("link", { name: "Download original" }).click();
    const download = await event;
    expect(download.suggestedFilename()).toBe("authoring-final.mp4");
    expect(await download.failure()).toBeNull();
    const hash = createHash("sha256");
    let bytes = 0;
    for await (const chunk of (await download.createReadStream())!) {
      hash.update(chunk);
      bytes += chunk.length;
    }
    expect(bytes).toBe(status.output!.byte_length);
    expect("sha256:" + hash.digest("hex")).toBe(
      status.output!.output_fingerprint,
    );
    await download.delete();
    await leaf.getByRole("button", { name: "Toggle leaf" }).click();
    await expect(video).toHaveCount(0);
    const counts = await leaf.evaluate(() => (window as any).outputCounts);
    expect(counts).toEqual({ created: 1, revoked: 1, originalFetch: 0 });
    await leaf.close();
    expect(context.pages().length).toBe(pagesBefore);
    expect((await hostIdentity()).graphId).toBe(before.graphId);
    const after = await page.evaluate(() => {
      const app = (window as any).comfyAPI.app.app,
        workflow = app.extensionManager.workflow;
      const saved = (window as any).__m19Workflow;
      return {
        activeStable: workflow.activeWorkflow === saved.active,
        openDelta: workflow.openWorkflows.length - saved.count,
        graph: app.graph.serialize(),
      };
    });
    expect(after.activeStable).toBe(true);
    expect(after.openDelta).toBe(0);
    const surroundings = diffGraphSurroundings({
      beforeValue: graphBefore,
      afterValue: after.graph,
      reference: {
        ownedNodeIds: [],
        ownedLinkIds: [],
        anchorNodeId: "",
        ownedProjectionEqual: true,
      },
    });
    expect(queueCalls).toBe(0);
    const receipt = process.env.H3_OUTPUT_HOST_RECEIPT;
    if (receipt)
      await writeFile(
        receipt,
        JSON.stringify(
          {
            schema: "M25OutputHostSampleV1",
            scope:
              "unmounted leaf, actual supplied-host routes, genuine full-extent source workspace",
            hostCatalogue: before.tabs,
            hostGraphIdentityUnchanged: true,
            workflowIdentityUnchanged: after.activeStable,
            openWorkflowDelta: after.openDelta,
            surroundings,
            openPagesRestored: true,
            queueCalls,
            counts,
            output: status.output,
            cancelTerminalUnchanged: true,
            harnessSha256: createHash("sha256").update(bundle).digest("hex"),
          },
          null,
          2,
        ) + "\n",
      );
  } finally {
    if (!leaf.isClosed()) await leaf.close();
  }
});
