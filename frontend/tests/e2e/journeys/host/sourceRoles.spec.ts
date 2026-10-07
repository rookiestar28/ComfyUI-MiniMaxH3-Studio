import { resolve } from "node:path";
// prettier-ignore
import { expect, test, } from "../../host/fixture";
import { OFFICIAL_LENGTH_EXPRESSION } from "../../../../src/host/templateMaterialization";
// prettier-ignore
import { hostUrl, candidateBundle, expectedCoInstallSidebarIds, candidateInjectionCount, assertCandidateBundleInjection, monitorH3Network, monitorCandidateInitiatorNetwork, beginSettledH3InteractionPhase, expectCandidateInteractionNetworkLocal, setSupportedH3Language, instrumentHostGraphLoads, hostGraphLoads, resetHostGraphLoads, hostWorkflowAuthorityReceipt, supportedHostQueueCounts, waitForAppModeQueue, privateDiagnosticLeakReceipt, readVisibleGraph, hostAssetResolutionReceipt, expectCompleteHostAssetResolution, } from "../../host/environment";

// prettier-ignore
test("exact host keeps I2VA, FL2VA, and L2VA source roles opaque and ordered", async ({ context, page }) => {
  test.setTimeout(180_000);
  if (hostUrl === undefined) throw new Error("H3_CONTEXT_HOST_URL is required");
  const allowedOrigin = new URL(hostUrl).origin;
  // prettier-ignore
  const routes = [{ mode: "i2va", first: true, last: false }, { mode: "fl2va", first: true, last: true }, { mode: "l2va", first: false, last: true }] as const;
  for (const [index, route] of routes.entries()) {
    const routePage = index === 0 ? page : await context.newPage();
    const networkAttribution = monitorH3Network(routePage, allowedOrigin);
    const candidateNetworkAttribution = await monitorCandidateInitiatorNetwork(
      context,
      routePage,
      allowedOrigin,
    );
    const injectionCountBeforeNavigation = candidateInjectionCount(context);
    await routePage.goto(hostUrl, { waitUntil: "domcontentloaded" });
    await routePage.waitForFunction(() => {
      const app = (window as unknown as { comfyAPI?: { app?: { app?: any } } })
        .comfyAPI?.app?.app;
      return app?.extensionManager
        ?.getSidebarTabs?.()
        .some((tab: { id: string }) => tab.id === "h3-context");
    });
    await routePage.waitForFunction((expectedIds) => {
      const app = (window as unknown as { comfyAPI?: { app?: { app?: any } } })
        .comfyAPI?.app?.app;
      const ids = app?.extensionManager
        ?.getSidebarTabs?.()
        .map((tab: { id: string }) => tab.id);
      return expectedIds.every((id) => ids?.includes(id));
    }, expectedCoInstallSidebarIds);
    // CRITICAL: sidebar registration can precede ComfyUI's canvas; the graph write below otherwise
    // enters viewport persistence and fails with `getCanvas: canvas is null` before qualification.
    await routePage.waitForFunction(() => {
      const app = (window as unknown as { comfyAPI?: { app?: { app?: any } } })
        .comfyAPI?.app?.app;
      return (
        typeof app?.graph?.serialize === "function" && app.canvas != null
      );
    });
    await assertCandidateBundleInjection(
      routePage,
      context,
      injectionCountBeforeNavigation,
    );
    await setSupportedH3Language(routePage, "en");
    const MAX_PUBLIC_GRAPH_STABILITY_FRAMES = 120;
    const REQUIRED_STABLE_GRAPH_FRAMES = 8;
    await routePage.evaluate(
      async ({ maxStabilityFrames, stableFrames }) => {
        const runtime = window as unknown as {
          comfyAPI: { app: { app: any }; api: { api: any } };
          __h3M1515Queue?: unknown[];
          __h3M2308CompileReceipt?: () => Promise<Record<string, unknown>>;
        };
        const app = runtime.comfyAPI.app.app;
        const tab = app.extensionManager
          .getSidebarTabs()
          .find((candidate: { id: string }) => candidate.id === "h3-context");
        const panel = document.createElement("div");
        panel.className = "p-splitterpanel side-bar-panel";
        // IMPORTANT: the host paints `#graph-canvas` as `absolute inset-0`
        // over the whole viewport. A panel appended to the body without the
        // real sidebar's geometry sits under it, and every click this row
        // makes is intercepted by the canvas instead of reaching the control.
        // Reproduce the panel the host would give this tab so the row asserts
        // real interactions rather than DOM existence.
        panel.style.width = "44rem";
        panel.style.position = "fixed";
        panel.style.inset = "80px auto 0 58px";
        panel.style.zIndex = "2000";
        panel.style.background = "#202124";
        panel.style.display = "flex";
        panel.style.flexDirection = "column";
        const content = document.createElement("div");
        content.className = "sidebar-content-container";
        content.style.flex = "1";
        content.style.minHeight = "0";
        content.style.overflow = "auto";
        const container = document.createElement("div");
        container.id = "h3-context-m15-15-container";
        content.append(container);
        panel.append(content);
        document.body.append(panel);
        tab.render(container);
        app.loadApiJson(
          {
            "17": {
              class_type: "LoadImage",
              inputs: { image: "m15-15-private-first.png" },
            },
            "18": {
              class_type: "LoadImage",
              inputs: { image: "m15-15-private-last.png" },
            },
          },
          "m15-15-visible-host-sources",
        );
        await app.loadGraphData(app.graph.serialize());
        const requiredImageSourceCount = 2;
        const MAX_SYNTHETIC_IMAGE_ADDITIONS = 2;
        const importPublicHostModule = (
          specifier: string,
        ): Promise<Record<string, any>> => import(specifier);
        const [appModule, apiModule] = await Promise.all([
          importPublicHostModule("/scripts/app.js"),
          importPublicHostModule("/scripts/api.js"),
        ]);
        const importedApp = appModule.app;
        const importedApi = apiModule.api;
        const appSame = importedApp === app;
        const apiSame = importedApi === runtime.comfyAPI.api.api;
        const graphSame = importedApp?.graph === app.graph;
        if (!appSame || !apiSame || !graphSame)
          throw new Error(
            `public host singleton identity mismatch: app=${String(appSame)} api=${String(apiSame)} graph=${String(graphSame)}`,
          );
        const serializedNodeInventory = (): Array<{
          id: string;
          type: string;
        }> => {
          const serialized = app.graph.serialize() as {
            nodes?: Array<{ id?: unknown; type?: unknown }>;
          };
          const inventory: Array<{ id: string; type: string }> = [];
          for (const node of Array.isArray(serialized.nodes)
            ? serialized.nodes
            : []) {
            const id = node.id;
            if (!(
              (typeof id === "number" && Number.isSafeInteger(id) && id >= 0) ||
              (typeof id === "string" &&
                /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/.test(id))
            ))
              continue;
            if (typeof node.type !== "string") continue;
            inventory.push({ id: String(id), type: node.type });
          }
          return inventory.sort(
            (left, right) =>
              left.type.localeCompare(right.type) ||
              left.id.localeCompare(right.id),
          );
        };
        const serializedImageSourceIds = (): string[] => {
          const sourceIds = new Set(
            serializedNodeInventory()
              .filter((node) => node.type === "LoadImage")
              .map((node) => node.id),
          );
          return [...sourceIds].sort((left, right) =>
            left.localeCompare(right),
          );
        };
        const waitForPublicGraphStability = async (
          minimumImageSourceCount: number,
        ): Promise<void> => {
          let previousInventory = "";
          let stableInventoryFrames = 0;
          let observedImageSourceCount = 0;
          for (let frame = 0; frame < maxStabilityFrames; frame += 1) {
            await new Promise<void>((resolvePromise) =>
              requestAnimationFrame(() => resolvePromise()),
            );
            const inventory = JSON.stringify(serializedNodeInventory());
            observedImageSourceCount = serializedImageSourceIds().length;
            const graphConfiguring = app.configuringGraph === true;
            if (
              !graphConfiguring &&
              inventory === previousInventory &&
              observedImageSourceCount >= minimumImageSourceCount
            )
              stableInventoryFrames += 1;
            else stableInventoryFrames = 0;
            previousInventory = inventory;
            if (stableInventoryFrames >= stableFrames) return;
          }
          throw new Error(
            `public graph did not stabilize: required_sources=${minimumImageSourceCount} observed_sources=${observedImageSourceCount} stable_frames=${stableInventoryFrames}`,
          );
        };
        await waitForPublicGraphStability(0);
        for (
          let additions = 0;
          additions < MAX_SYNTHETIC_IMAGE_ADDITIONS &&
          serializedImageSourceIds().length < requiredImageSourceCount;
          additions += 1
        ) {
          const previousSerializedCount = serializedImageSourceIds().length;
          const liteGraph = (
            window as unknown as {
              LiteGraph?: { createNode?: (type: string) => any };
            }
          ).LiteGraph;
          const additionalImage = liteGraph?.createNode?.("LoadImage");
          if (additionalImage === undefined)
            throw new Error("The host cannot materialize a LoadImage node");
          additionalImage.pos = [420 + additions * 40, 120];
          app.graph.add(additionalImage);
          if (serializedImageSourceIds().length <= previousSerializedCount)
            throw new Error("serialized LoadImage inventory did not advance");
        }
        if (serializedImageSourceIds().length < requiredImageSourceCount)
          throw new Error(
            "The host cannot materialize two serialized LoadImage nodes within bounds",
          );
        await waitForPublicGraphStability(requiredImageSourceCount);
        // M17-00: the public destroy/render remount is the supported refresh seam.
        // Rendering an already-mounted tab does not promise a graph refresh.
        tab.destroy();
        tab.render(container);
        await waitForPublicGraphStability(requiredImageSourceCount);
        runtime.__h3M1515Queue = [];
        type CompiledNode = {
          class_type?: unknown;
          inputs?: Record<string, unknown>;
        };
        type CompiledPrompt = {
          output?: Record<string, CompiledNode>;
        };
        const nativePromptReceipt = (
          compiled: CompiledPrompt,
        ): Record<string, unknown> => {
          const output = compiled.output ?? {};
          const request = Object.values(output).find(
            (node) =>
              node.class_type === "comfyui_h3_context.H3Context.Request",
          )?.inputs;
          const generation = Object.values(output).find(
            (node) => node.class_type === "MiniMaxH3ImageToVideo",
          )?.inputs;
          const registryEntry = Object.entries(output).find(
            ([, node]) =>
              node.class_type ===
              "comfyui_h3_context.H3Context.ReferenceRegistry",
          );
          const plan = Object.values(output).find(
            (node) => node.class_type === "comfyui_h3_context.H3Context.Plan",
          )?.inputs;
          const imageNodeIds = new Set(
            Object.entries(output)
              .filter(([, node]) => node.class_type === "LoadImage")
              .map(([id]) => id),
          );
          const imageBinding = (value: unknown): boolean =>
            Array.isArray(value) &&
            value.length === 2 &&
            imageNodeIds.has(String(value[0])) &&
            value[1] === 0;
          const first = generation?.first_frame;
          const last = generation?.last_frame;
          const registryInputs = registryEntry?.[1].inputs ?? {};
          const sameLink = (left: unknown, right: unknown): boolean =>
            Array.isArray(left) &&
            Array.isArray(right) &&
            left.length === 2 &&
            right.length === 2 &&
            String(left[0]) === String(right[0]) &&
            left[1] === right[1];
          const registryId = registryEntry?.[0];
          const registryFeedsPlan =
            registryId !== undefined &&
            Array.isArray(plan?.reference_registry) &&
            plan.reference_registry.length === 2 &&
            String(plan.reference_registry[0]) === registryId &&
            plan.reference_registry[1] === 0;
          const byId = new Map(Object.entries(output));
          const scalarTerminal = (
            input: unknown,
          ): { source: string | null; value: unknown } => {
            let current = input;
            let source: string | null = null;
            for (let hop = 0; hop < 8; hop += 1) {
              if (!Array.isArray(current)) return { source, value: current };
              if (current.length !== 2 || current[1] !== 0)
                return { source: null, value: null };
              const nodeId = String(current[0]);
              const node = byId.get(nodeId);
              if (node === undefined) return { source: null, value: null };
              source = nodeId;
              if (Object.hasOwn(node.inputs ?? {}, "value"))
                current = node.inputs?.value;
              else if (Object.hasOwn(node.inputs ?? {}, "values.a"))
                current = node.inputs?.["values.a"];
              else return { source, value: null };
            }
            return { source: null, value: null };
          };
          const linkedNode = (input: unknown): CompiledNode | undefined =>
            Array.isArray(input) && input.length === 2 && input[1] === 0
              ? byId.get(String(input[0]))
              : undefined;
          const scheduler = Object.values(output).find(
            (node) =>
              node.class_type === "BasicScheduler" &&
              linkedNode(node.inputs?.steps)?.class_type === "ComfySwitchNode",
          );
          const stepSwitch = linkedNode(scheduler?.inputs?.steps);
          const stepControl = scalarTerminal(stepSwitch?.inputs?.switch);
          const baseSteps = scalarTerminal(stepSwitch?.inputs?.on_false).value;
          const turboSteps = scalarTerminal(stepSwitch?.inputs?.on_true).value;
          const loraSwitch = Object.values(output).find(
            (node) =>
              node.class_type === "ComfySwitchNode" &&
              linkedNode(node.inputs?.on_true)?.class_type ===
                "LoraLoaderModelOnly",
          );
          const requestDuration = scalarTerminal(request?.duration_seconds);
          const nativeLength = generation?.length;
          const nativeLengthNode =
            Array.isArray(nativeLength) &&
            nativeLength.length === 2 &&
            nativeLength[1] === 1
              ? byId.get(String(nativeLength[0]))
              : undefined;
          const nativeDurationOperand = scalarTerminal(
            nativeLengthNode?.inputs?.["values.a"] ??
              nativeLengthNode?.inputs?.a,
          );
          return {
            taskMode: request?.task_mode,
            requestDurationSeconds: requestDuration.value,
            nativeDurationOperandSeconds: nativeDurationOperand.value,
            nativeLengthOutputSlot:
              Array.isArray(nativeLength) && nativeLength.length === 2
                ? nativeLength[1]
                : null,
            nativeLengthExpression:
              nativeLengthNode?.inputs?.expression ?? null,
            sharedDurationSource:
              requestDuration.source !== null &&
              requestDuration.source === nativeDurationOperand.source,
            firstBound: first === undefined ? false : imageBinding(first),
            lastBound: last === undefined ? false : imageBinding(last),
            bindingsDistinct:
              first === undefined ||
              last === undefined ||
              String((first as unknown[])[0]) !==
                String((last as unknown[])[0]),
            typedFirstMatches:
              first === undefined
                ? registryInputs.first_frame === undefined
                : sameLink(first, registryInputs.first_frame),
            typedLastMatches:
              last === undefined
                ? registryInputs.last_frame === undefined
                : sameLink(last, registryInputs.last_frame),
            registryFeedsPlan,
            imageNodeCount: imageNodeIds.size,
            // These describe the pinned replacement candidate. They are not
            // values App Mode may overwrite on an existing user-selected canvas.
            templateTurboMode: stepControl.value,
            templateBaseSteps: baseSteps,
            templateTurboSteps: turboSteps,
            templateEffectiveSteps:
              stepControl.value === false
                ? baseSteps
                : stepControl.value === true
                  ? turboSteps
                  : null,
            templateTurboLoraSharesControl:
              stepSwitch !== undefined &&
              loraSwitch !== undefined &&
              sameLink(stepSwitch.inputs?.switch, loraSwitch.inputs?.switch),
          };
        };
        runtime.__h3M2308CompileReceipt = async () =>
          nativePromptReceipt((await app.graphToPrompt()) as CompiledPrompt);
        runtime.comfyAPI.api.api.queuePrompt = (
          queueNumber: number,
          compiled: CompiledPrompt,
        ) => {
          const output = compiled.output ?? {};
          const types = Object.values(output).map((node) =>
            String(node.class_type ?? ""),
          );
          const request = Object.values(output).find(
            (node) =>
              node.class_type === "comfyui_h3_context.H3Context.Request",
          )?.inputs;
          const nativeAnchorTypes = new Set([
            "MiniMaxH3ImageToVideo",
            "MiniMaxH3ReferenceToVideo",
          ]);
          const requiredSingletons = [
            "comfyui_h3_context.H3Context.Request",
            "comfyui_h3_context.H3Context.Plan",
            "comfyui_h3_context.H3Context.Compiler",
            "comfyui_h3_context.H3Context.Validator",
            "comfyui_h3_context.H3Context.NativeH3Adapter",
            "comfyui_h3_context.H3Context.ProductShell",
            "comfyui_h3_context.H3Context.ReferenceRegistry",
          ];
          const countType = (type: string): number =>
            types.filter((candidate) => candidate === type).length;
          const expectedImageLoaders = request?.task_mode === "fl2va" ? 2 : 1;
          // M23-19 (reconciled by M23-37): the one managed submission is the
          // whole model prompt — context pipeline, one native anchor and the
          // sink — with the ProductShell bootstrap executing inside it. There
          // is no separate pipeline-only closure to intercept any more.
          const qualifiedEnvelope =
            types.length > 0 &&
            types.length <= 128 &&
            requiredSingletons.every((type) => countType(type) === 1) &&
            types.filter((type) => nativeAnchorTypes.has(type)).length === 1 &&
            countType("SaveVideo") === 1 &&
            countType("LoadImage") === expectedImageLoaders;
          runtime.__h3M1515Queue!.push({
            phase: "bootstrap_intercepted",
            queueNumber,
            taskMode: request?.task_mode,
            qualifiedEnvelope,
            bootstrapNodeCount: types.length,
            hasProductShell: types.includes(
              "comfyui_h3_context.H3Context.ProductShell",
            ),
            hasNative: types.some((type) =>
              new Set([
                "MiniMaxH3ImageToVideo",
                "MiniMaxH3ReferenceToVideo",
              ]).has(type),
            ),
            hasSink: types.includes("SaveVideo"),
            imageLoaderCount: countType("LoadImage"),
          });
          return Promise.resolve({
            prompt_id: "00000000-0000-4000-8000-000000000023",
            number: -23,
            node_errors: {},
          });
        };
      },
      {
        maxStabilityFrames: MAX_PUBLIC_GRAPH_STABILITY_FRAMES,
        stableFrames: REQUIRED_STABLE_GRAPH_FRAMES,
      },
    );

    await instrumentHostGraphLoads(routePage);
    await resetHostGraphLoads(routePage);
    expect(await supportedHostQueueCounts(routePage)).toEqual({
      running: 0,
      pending: 0,
    });

    await beginSettledH3InteractionPhase(
      routePage,
      networkAttribution,
      candidateNetworkAttribution,
    );
    const container = routePage.locator("#h3-context-m15-15-container");
    await expect(container.getByLabel("Task mode")).toBeVisible();
    await container.getByLabel("Task mode").selectOption(route.mode);
    const intent = container.getByRole("textbox", { name: "Intent" });
    const duration = container.getByRole("spinbutton", {
      name: "Clip duration (seconds)",
    });
    const submit = container.locator('button[type="submit"]');
    const blockerByMode = {
      i2va: "Select a first frame source before submitting.",
      fl2va: "Select distinct first and last frame sources before submitting.",
      l2va: "Select a last frame source before submitting.",
    } as const;
    await expect(intent).toBeEnabled();
    await expect(duration).toBeEnabled();
    await expect(submit).toBeDisabled();
    await expect(
      container.getByText(blockerByMode[route.mode], { exact: true }),
    ).toHaveAttribute("role", "status");
    await intent.fill(`M17 ${route.mode} editable draft`);
    await duration.fill("8");
    await expect(intent).toHaveValue(`M17 ${route.mode} editable draft`);
    await expect(duration).toHaveValue("8");
    await expect(
      container.getByText("Delivers 8 s (192 frames).", { exact: true }),
    ).toBeVisible();
    const sourceLabel = route.first
      ? "First frame source"
      : "Last frame source";
    const requiredAvailableSourceCount = route.mode === "fl2va" ? 2 : 1;
    type AtomicImageSourceJoin = {
      appSame: boolean;
      apiSame: boolean;
      graphSame: boolean;
      graphConfiguring: boolean;
      serializedIds: string[];
      renderedIds: string[];
    };
    const readAtomicImageSourceJoin = (): Promise<AtomicImageSourceJoin> =>
      routePage.evaluate(
        async ({ focusKey }) => {
          const runtime = window as unknown as {
            comfyAPI: { app: { app: any }; api: { api: any } };
          };
          const importPublicHostModule = (
            specifier: string,
          ): Promise<Record<string, any>> => import(specifier);
          const [appModule, apiModule] = await Promise.all([
            importPublicHostModule("/scripts/app.js"),
            importPublicHostModule("/scripts/api.js"),
          ]);
          const app = runtime.comfyAPI.app.app;
          const serialized = app.graph.serialize() as {
            nodes?: Array<{ id?: unknown; type?: unknown }>;
          };
          const serializedIds = [
            ...new Set(
              (Array.isArray(serialized.nodes) ? serialized.nodes : [])
                .filter((node) => node.type === "LoadImage")
                .map((node) => node.id)
                .filter(
                  (id): id is string | number =>
                    (typeof id === "number" &&
                      Number.isSafeInteger(id) &&
                      id >= 0) ||
                    (typeof id === "string" &&
                      /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$/.test(id)),
                )
                .map(String),
            ),
          ].sort((left, right) => left.localeCompare(right));
          const select = document
            .getElementById("h3-context-m15-15-container")
            ?.querySelector<HTMLSelectElement>(
              `[data-h3-focus-key="${focusKey}"]`,
            );
          const renderedIds =
            select === undefined || select === null
              ? []
              : [...select.options]
                  .map((option) => option.value)
                  .filter((value) => value.length > 0);
          return {
            appSame: appModule.app === app,
            apiSame: apiModule.api === runtime.comfyAPI.api.api,
            graphSame: appModule.app?.graph === app.graph,
            graphConfiguring: app.configuringGraph === true,
            serializedIds,
            renderedIds,
          };
        },
        {
          focusKey: route.first ? "app-first-frame" : "app-last-frame",
        },
      );
    let previousSerializedIds = "";
    let stableAtomicJoinObservations = 0;
    let atomicJoinEvidence: AtomicImageSourceJoin | undefined;
    await expect
      .poll(async () => {
        const evidence = await readAtomicImageSourceJoin();
        atomicJoinEvidence = evidence;
        const serializedSignature = JSON.stringify(evidence.serializedIds);
        if (
          evidence.appSame &&
          evidence.apiSame &&
          evidence.graphSame &&
          !evidence.graphConfiguring &&
          serializedSignature === previousSerializedIds
        )
          stableAtomicJoinObservations += 1;
        else stableAtomicJoinObservations = 0;
        previousSerializedIds = serializedSignature;
        return {
          appSame: evidence.appSame,
          apiSame: evidence.apiSame,
          graphSame: evidence.graphSame,
          graphStable:
            stableAtomicJoinObservations >= REQUIRED_STABLE_GRAPH_FRAMES,
          enoughSerializedSources:
            evidence.serializedIds.length >= requiredAvailableSourceCount,
          joined:
            JSON.stringify(evidence.serializedIds) ===
            JSON.stringify(evidence.renderedIds),
        };
      })
      .toEqual({
        appSame: true,
        apiSame: true,
        graphSame: true,
        graphStable: true,
        enoughSerializedSources: true,
        joined: true,
      });
    if (atomicJoinEvidence === undefined)
      throw new Error("atomic image-source evidence is unavailable");
    const availableSources = atomicJoinEvidence.renderedIds;
    if (route.first)
      await container
        .getByLabel("First frame source")
        .selectOption(availableSources[0]);
    if (route.mode === "fl2va") {
      await container
        .getByLabel("Last frame source")
        .selectOption(availableSources[0]);
      await expect(submit).toBeDisabled();
      await container
        .getByLabel("Last frame source")
        .selectOption(availableSources[1]);
    } else if (route.last) {
      await container
        .getByLabel("Last frame source")
        .selectOption(availableSources[0]);
    }
    await expect(submit).toBeEnabled();
    await expect(container).not.toContainText("m15-15-private-first.png");
    await expect(container).not.toContainText("m15-15-private-last.png");
    await container
      .getByRole("button", { name: "Replace canvas and start H3 App Mode" })
      .evaluate((button) => (button as HTMLButtonElement).click());
    await waitForAppModeQueue(
      routePage,
      "h3-context-m15-15-container",
      "__h3M1515Queue",
      1,
      60_000,
    );
    const bootstrapReceipt = await routePage.evaluate(
      () =>
        (window as unknown as { __h3M1515Queue?: unknown[] })
          .__h3M1515Queue?.[0],
    );
    expect(bootstrapReceipt).toMatchObject({
      phase: "bootstrap_intercepted",
      queueNumber: -1,
      taskMode: route.mode,
      qualifiedEnvelope: true,
      hasProductShell: true,
      hasNative: true,
      hasSink: true,
      imageLoaderCount: route.mode === "fl2va" ? 2 : 1,
    });
    const receipt = await routePage.evaluate(async () => {
      const compileReceipt = (
        window as unknown as {
          __h3M2308CompileReceipt?: () => Promise<Record<string, unknown>>;
        }
      ).__h3M2308CompileReceipt;
      if (compileReceipt === undefined)
        throw new Error("the bounded native compile receipt is unavailable");
      return await compileReceipt();
    });
    expect(receipt).toMatchObject({
      taskMode: route.mode,
      requestDurationSeconds: 8,
      nativeDurationOperandSeconds: 8,
      nativeLengthOutputSlot: 1,
      nativeLengthExpression: OFFICIAL_LENGTH_EXPRESSION,
      sharedDurationSource: true,
      firstBound: route.first,
      lastBound: route.last,
      bindingsDistinct: true,
      typedFirstMatches: true,
      typedLastMatches: true,
      registryFeedsPlan: true,
      imageNodeCount: route.mode === "fl2va" ? 2 : 1,
      templateTurboMode: false,
      templateBaseSteps: 20,
      templateTurboSteps: 8,
      templateEffectiveSteps: 20,
      templateTurboLoraSharesControl: true,
    });
    if (candidateBundle !== null && route.mode === "i2va")
      expectCompleteHostAssetResolution(
        await hostAssetResolutionReceipt(routePage, "image_to_video"),
      );
    await routePage.evaluate((taskMode) => {
      const api = (
        window as unknown as {
          comfyAPI: { api: { api: EventTarget } };
        }
      ).comfyAPI.api.api;
      api.dispatchEvent(
        new CustomEvent("execution_error", {
          detail: {
            prompt_id: "00000000-0000-4000-8000-000000000023",
            exception_message: "m23-08-private-host-diagnostic",
            traceback: ["m23-08-private-host-traceback"],
            prompt: { private: true },
          },
        }),
      );
    }, route.mode);
    await expect(container.locator('[data-shell-status="error"]')).toHaveCount(
      1,
    );
    await expect(container.getByRole("alert")).toBeVisible();
    await expect(container).not.toContainText("m23-08-private-host-diagnostic");
    // M23-19 (reconciled by M23-37): a host execution error inside a managed
    // run ends it with the managed recovery action (continue with native
    // nodes, or retry the output verification when the coordinator allows
    // it), not with the pre-managed "Retry H3 App Mode" action.
    await expect(
      container.locator('[data-h3-focus-key="error-recovery"]'),
    ).toBeVisible();
    await routePage.evaluate(() => {
      const runtime = window as unknown as {
        __h3M2321CopiedDiagnostics?: string;
      };
      runtime.__h3M2321CopiedDiagnostics = undefined;
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: async (payload: string) => {
            runtime.__h3M2321CopiedDiagnostics = payload;
          },
        },
      });
    });
    await container.getByRole("button", { name: "Copy diagnostics" }).click();
    await expect
      .poll(
        async () =>
          await routePage.evaluate(
            () =>
              (
                window as unknown as {
                  __h3M2321CopiedDiagnostics?: string;
                }
              ).__h3M2321CopiedDiagnostics,
          ),
      )
      .not.toBeUndefined();
    const copiedPayload = await routePage.evaluate(
      () =>
        (window as unknown as { __h3M2321CopiedDiagnostics?: string })
          .__h3M2321CopiedDiagnostics ?? "",
    );
    expect(copiedPayload).toContain("stage bootstrap_started");
    // M23-19 (reconciled by M23-37): what this row owns is the diagnostics
    // copy — bounded, redacted and causal. The terminal error class depends
    // on where the managed chain stops, and this row fakes the host's
    // execution with a stubbed queue and a synthetic terminal event, so the
    // class is not this row's subject; the journal ending in a terminal error
    // state is. See F-14 for the attribution question this exposed.
    expect(copiedPayload).toContain("stage bootstrap_terminal_seen");
    expect(copiedPayload).toMatch(/state error code=[a-z_]+/);
    expect(copiedPayload).not.toContain("m23-08-private-host-diagnostic");
    expect(copiedPayload).not.toContain("m23-08-private-host-traceback");
    expect(copiedPayload).not.toContain('"private":true');
    const nativeRecovery = container.getByRole("button", {
      name: "Continue with native nodes",
    });
    await expect(nativeRecovery).toBeVisible();
    // M23-19 (reconciled by M23-37): the one legal write targets the workflow
    // that was active when the transaction began (omitting the fourth public
    // loadGraphData argument creates a new temporary tab per write on
    // supported frontend builds). A managed terminal error KEEPS that written
    // canvas — the recovery actions operate on it — so there is no rollback
    // load and the receipt records exactly one matching load.
    expect(await hostWorkflowAuthorityReceipt(routePage)).toEqual({
      activeStable: true,
      openWorkflowDelta: 0,
      graphLoadWorkflowMatches: [true],
    });
    // Snapshot only after the terminal error state is rendered: the canvas the
    // recovery actions operate on is the written one, and it must stay
    // untouched from here on.
    const keptGraph = await readVisibleGraph(routePage);
    const graphLoadsBeforeRecovery = await hostGraphLoads(routePage);
    // This row mounts a synthetic panel beside the host's real sidebar. After graph
    // replacement the canvas can cover that artificial panel even though the control is
    // rendered and enabled; dispatch the native DOM activation to test the product handler
    // instead of spending the action timeout on host z-order the product does not own.
    await nativeRecovery.evaluate((button) =>
      (button as HTMLButtonElement).click(),
    );
    await expect(
      container.locator('[data-shell-reason="native_preference"]'),
    ).toHaveCount(1);
    await expect(
      container.getByRole("textbox", { name: "Intent" }),
    ).toBeVisible();
    await expect(
      container.getByRole("button", { name: /queue current H3 graph/i }),
    ).toBeEnabled();
    expect(await readVisibleGraph(routePage)).toEqual(keptGraph);
    expect(await hostGraphLoads(routePage)).toBe(graphLoadsBeforeRecovery);
    expect(
      await routePage.evaluate(
        () =>
          (window as unknown as { __h3M1515Queue?: unknown[] }).__h3M1515Queue
            ?.length ?? 0,
      ),
    ).toBe(1);
    expect(
      await privateDiagnosticLeakReceipt(
        routePage,
        "h3-context-m15-15-container",
        ["__h3M1515Queue"],
      ),
    ).toEqual({ shellState: false, storage: false, evidence: false });
    expect(await supportedHostQueueCounts(routePage)).toEqual({
      running: 0,
      pending: 0,
    });
    expectCandidateInteractionNetworkLocal(candidateNetworkAttribution);
    if (routePage !== page) await routePage.close();
  }
});
